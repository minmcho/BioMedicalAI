"""Wave A — CHART-B2 (chart allergies) + CHART-B3 (sig builder).

CHART-B2 rides the generic FHIR surface: AllergyIntolerance is a
first-class resource (app.fhir validates code + patient.reference),
create_resource upserts on an existing id (the chart's edit path),
and app.orders._allergy_conflict is the consumer that must see a
chart-recorded allergy. NKDA is an explicit AllergyIntolerance with
code ``no-known-allergy`` (system urn:opencare:allergy) — recorded,
confirmed, and never a medication code, so it blocks nothing.

CHART-B3: the sig vocabulary is published terminology
(terminology/medication-sig, seeded by ensure_orders_defaults) and
placement validates a structured dosageInstruction server-side —
422 naming the field — while text-only dosage stays accepted.
The emitted shape mirrors what the tree already parses:
app.pharmacy.dose_quantity_of reads doseAndRate[0].doseQuantity and
app.pharmacy.doses_per_day_of reads timing.repeat.
"""
from app import pharmacy
from tests.conftest import CLINICIAN

RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
SIG_SYSTEM = "urn:opencare:medication-sig"


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synth", "given": ["WaveA"]}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _sig(**overrides):
    entry = {
        "text": "Amoxicillin 500 mg oral three times daily for 7 days",
        "doseAndRate": [{"doseQuantity": {"value": 500, "unit": "mg",
                                           "code": "mg"}}],
        "route": {"coding": [{"system": SIG_SYSTEM, "code": "oral",
                              "display": "Oral"}]},
        "timing": {"repeat": {"frequency": 3, "period": 1,
                              "periodUnit": "d",
                              "boundsDuration": {"value": 7, "unit": "d",
                                                 "code": "d"}}},
    }
    entry.update(overrides)
    return [entry]


def _place_med(client, pid, dosage=None, code="amox-500"):
    body = {"type": "medication", "code": code, "display": "Amoxicillin",
            "patient": pid}
    if dosage is not None:
        body["dosageInstruction"] = dosage
    return client.post("/orders", json=body, headers=CLINICIAN)


# ---------------------------------------------------------------------------
# CHART-B3 — vocabulary + sig validation
# ---------------------------------------------------------------------------

def test_sig_vocabulary_is_published_terminology(client):
    r = client.get("/orders/types", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    sig = r.json()["medication_sig"]
    assert sig is not None
    assert {c["code"] for c in sig["routes"]} == {
        "oral", "intravenous", "intramuscular", "subcutaneous",
        "inhaled", "topical", "rectal", "sublingual"}
    freqs = {c["code"]: c for c in sig["frequencies"]}
    assert freqs["once-daily"]["timesPerDay"] == 1
    assert freqs["twice-daily"]["timesPerDay"] == 2
    assert freqs["three-times-daily"]["timesPerDay"] == 3
    assert freqs["four-times-daily"]["timesPerDay"] == 4
    assert freqs["every-6-hours"]["timesPerDay"] == 4
    assert freqs["every-8-hours"]["timesPerDay"] == 3
    assert freqs["every-12-hours"]["timesPerDay"] == 2
    assert freqs["as-needed"]["timesPerDay"] == 0
    assert {c["code"] for c in sig["units"]} == {
        "mg", "mcg", "g", "mL", "units", "tablets", "drops", "puffs"}
    # Frequency repeat triples must agree with timesPerDay under the
    # pharmacy doses-per-day maths (the compatibility contract).
    for entry in sig["frequencies"]:
        body = {"dosageInstruction": [{"timing": {"repeat": entry["repeat"]}}]} \
            if "repeat" in entry else {"dosageInstruction": [{}]}
        parsed = pharmacy.doses_per_day_of(body)
        if entry["timesPerDay"] == 0:
            assert parsed is None
        else:
            assert parsed == entry["timesPerDay"], entry["code"]


def test_valid_structured_sig_accepted_and_persisted_verbatim(client):
    pid = _patient(client, "wa-sig-ok")
    r = _place_med(client, pid, dosage=_sig())
    assert r.status_code == 201, r.text
    order = r.json()["order"]
    assert order["dosageInstruction"] == _sig()
    # The pharmacy readers parse the persisted shape.
    assert pharmacy.dose_quantity_of(order) == (500.0, "mg")
    assert pharmacy.doses_per_day_of(order) == 3.0
    # And it survives a read-back through the orders list.
    listed = client.get(f"/orders?patient={pid}", headers=CLINICIAN).json()
    assert listed["items"][0]["dosageInstruction"] == _sig()


def test_sig_zero_dose_is_422_naming_field(client):
    pid = _patient(client, "wa-sig-zero")
    bad = _sig(doseAndRate=[{"doseQuantity": {"value": 0, "unit": "mg"}}])
    r = _place_med(client, pid, dosage=bad)
    assert r.status_code == 422, r.text
    assert "doseQuantity.value" in r.json()["detail"]


def test_sig_unknown_unit_is_422_naming_field(client):
    pid = _patient(client, "wa-sig-unit")
    bad = _sig(doseAndRate=[{"doseQuantity": {"value": 5, "unit": "furlongs"}}])
    r = _place_med(client, pid, dosage=bad)
    assert r.status_code == 422, r.text
    assert "doseQuantity.unit" in r.json()["detail"]
    assert "furlongs" in r.json()["detail"]


def test_sig_unknown_route_is_422_naming_field(client):
    pid = _patient(client, "wa-sig-route")
    bad = _sig(route={"coding": [{"system": SIG_SYSTEM, "code": "ocular",
                                  "display": "Ocular"}]})
    r = _place_med(client, pid, dosage=bad)
    assert r.status_code == 422, r.text
    assert "route" in r.json()["detail"]


def test_sig_missing_frequency_is_422_naming_field(client):
    pid = _patient(client, "wa-sig-freq")
    bad = _sig()
    del bad[0]["timing"]
    r = _place_med(client, pid, dosage=bad)
    assert r.status_code == 422, r.text
    assert "timing" in r.json()["detail"]


def test_sig_missing_text_is_422_naming_field(client):
    pid = _patient(client, "wa-sig-text")
    bad = _sig()
    del bad[0]["text"]
    r = _place_med(client, pid, dosage=bad)
    assert r.status_code == 422, r.text
    assert "text" in r.json()["detail"]


def test_text_only_dosage_still_accepted(client):
    pid = _patient(client, "wa-sig-textonly")
    r = client.post("/orders", json={
        "type": "medication", "code": "amox-500", "display": "Amoxicillin",
        "patient": pid, "dosage_text": "500 mg by mouth three times daily"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["order"]["dosageInstruction"] == [
        {"text": "500 mg by mouth three times daily"}]
    # No dosageInstruction at all also remains accepted (pre-B3 shape).
    r = _place_med(client, pid, code="other-med")
    assert r.status_code == 201, r.text


def test_non_medication_order_ignores_dosage_validation(client):
    pid = _patient(client, "wa-sig-nonmed")
    r = client.post("/orders", json={
        "type": "procedure", "code": "93005", "patient": pid},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# CHART-B2 — allergies via the chart's (generic FHIR) path
# ---------------------------------------------------------------------------

def _create_allergy(client, pid, code="847232", display="Metformin",
                    aid=None, clinical="active", verification="confirmed"):
    body = {
        "resourceType": "AllergyIntolerance",
        "id": aid or f"alg-{pid}-{code}",
        "clinicalStatus": {"coding": [{"code": clinical}]},
        "verificationStatus": {"coding": [{"code": verification}]},
        "type": "allergy", "category": ["medication"],
        "criticality": "high",
        "code": {"coding": [{"system": RXNORM, "code": code,
                             "display": display}], "text": display},
        "patient": {"reference": f"Patient/{pid}"},
        "recordedDate": "2026-10-20T00:00:00Z",
        "reaction": [{"manifestation": [{"text": "Rash"}],
                      "severity": "moderate"}],
    }
    return client.post("/fhir/AllergyIntolerance", json=body,
                       headers=CLINICIAN)


def test_allergy_created_via_chart_path_blocks_conflicting_order(client):
    pid = _patient(client, "wa-alg-1")
    r = _create_allergy(client, pid)
    assert r.status_code == 201, r.text
    # Read back through the patient-scoped search the tab uses.
    bundle = client.get(f"/fhir/AllergyIntolerance?patient={pid}",
                        headers=CLINICIAN).json()
    assert bundle["total"] == 1
    assert bundle["entry"][0]["resource"]["id"] == f"alg-{pid}-847232"
    # _allergy_conflict sees it: a conflicting medication order is a
    # 422 naming the allergen, a non-conflicting one passes.
    r = client.post("/orders", json={
        "type": "medication", "code": "847232", "patient": pid},
        headers=CLINICIAN)
    assert r.status_code == 422, r.text
    assert "Metformin" in r.json()["detail"]
    r = client.post("/orders", json={
        "type": "medication", "code": "1049221", "patient": pid},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text


def test_allergy_edit_via_upsert_changes_status_and_unblocks(client):
    pid = _patient(client, "wa-alg-2")
    assert _create_allergy(client, pid).status_code == 201
    # The chart edit path: POST the same id with new statuses
    # (create_resource upserts — version increments, body replaces).
    r = _create_allergy(client, pid, clinical="resolved")
    assert r.status_code == 201, r.text
    assert r.json()["meta"]["versionId"] == "2"
    assert r.json()["clinicalStatus"]["coding"][0]["code"] == "resolved"
    r = client.post("/orders", json={
        "type": "medication", "code": "847232", "patient": pid},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text


def test_allergy_requires_code_and_patient(client):
    pid = _patient(client, "wa-alg-3")
    r = client.post("/fhir/AllergyIntolerance", json={
        "resourceType": "AllergyIntolerance",
        "patient": {"reference": f"Patient/{pid}"}}, headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post("/fhir/AllergyIntolerance", json={
        "resourceType": "AllergyIntolerance",
        "code": {"coding": [{"code": "x"}]}}, headers=CLINICIAN)
    assert r.status_code == 422


def test_nkda_record_is_distinguishable_and_blocks_nothing(client):
    pid = _patient(client, "wa-alg-nkda")
    # Before recording: the chart posture is "not recorded" — the
    # patient-scoped list is empty.
    bundle = client.get(f"/fhir/AllergyIntolerance?patient={pid}",
                        headers=CLINICIAN).json()
    assert bundle["total"] == 0
    # Record NKDA exactly as AllergiesTab does.
    r = client.post("/fhir/AllergyIntolerance", json={
        "resourceType": "AllergyIntolerance", "id": f"nkda-{pid}",
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "verificationStatus": {"coding": [{"code": "confirmed"}]},
        "code": {"coding": [{"system": "urn:opencare:allergy",
                             "code": "no-known-allergy",
                             "display": "No known allergies"}],
                 "text": "No known allergies"},
        "patient": {"reference": f"Patient/{pid}"},
        "recordedDate": "2026-10-20T00:00:00Z"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    bundle = client.get(f"/fhir/AllergyIntolerance?patient={pid}",
                        headers=CLINICIAN).json()
    assert bundle["total"] == 1
    assert bundle["entry"][0]["resource"]["code"]["coding"][0]["code"] == \
        "no-known-allergy"
    # NKDA is not a medication code: ordering proceeds.
    r = client.post("/orders", json={
        "type": "medication", "code": "847232", "patient": pid},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
