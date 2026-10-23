"""Phase 15 — Oncology regimen depth (specialty-oncology pack v1.1.0).

Deep proofs, following the Phase 9 specialty-deep pattern:

1. Pack lifecycle is governed and enabling applies the Phase 15
   configuration (regimen order sets, treatment-plan / staging /
   assessment / chemo-verification forms, formulary + monitoring
   terminology append, cancer registry cohort).
2. The named regimen order sets (FOLFOX, AC) place real orders, and
   the cycle/day structure documented in each set rides each item's
   note onto the placed orders.
3. The new governed forms validate through the ordinary flowsheet
   machinery — complete submissions pass, a missing required item
   (BSA on the treatment plan, the second clinician on the chemo
   verification) is a 422.
4. Chemotherapy safety is machinery-backed through the GENERIC
   pharmacy medication-monitoring hook: the pack's FLAG rule for
   methotrexate (narrow therapeutic index) warns on dispense when
   the level / creatinine are missing, and dispenses clean once
   current results are on file. (Config-only vs machinery-backed is
   stated in the workstream report.)
"""
from datetime import datetime, timezone

from tests.conftest import ADMIN, CLINICIAN

LOINC = "http://loinc.org"
METHOTREXATE = "methotrexate"
MTX_LEVEL = "1485-3"
CREATININE = "2160-0"


def _enable(client, name="specialty-oncology"):
    inst = client.post(f"/studio/packs/{name}/install", headers=ADMIN).json()
    iid = inst["id"]
    assert client.post(f"/studio/installations/{iid}/approve",
                       headers=ADMIN).status_code == 200
    assert client.post(f"/studio/installations/{iid}/attach",
                       headers=ADMIN).status_code == 200
    r = client.post(f"/studio/installations/{iid}/enable",
                    json={"parameters": {}}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synth", "given": ["Onc"]}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _active_med_order(client, pid, code):
    r = client.post("/orders", json={
        "type": "medication", "code": code, "patient": pid},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    oid = r.json()["order"]["id"]
    for to in ("signed", "active"):
        r = client.post(f"/orders/{oid}/transition",
                        json={"to_status": to}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    return oid


def _observation(client, pid, code, value):
    body = {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": LOINC, "code": code}]},
        "subject": {"reference": f"Patient/{pid}"},
        "effectiveDateTime": datetime.now(timezone.utc).isoformat(),
        "valueQuantity": {"value": value, "unit": "mg/L"},
    }
    r = client.post("/fhir/Observation", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# 1. Lifecycle + applied configuration
# ---------------------------------------------------------------------------

def test_oncology_pack_lifecycle_and_config(client):
    packs = {p["name"]: p for p in
             client.get("/studio/packs", headers=ADMIN).json()}
    assert packs["specialty-oncology"]["version"] == "1.1.0"

    inst = client.post("/studio/packs/specialty-oncology/install",
                       headers=ADMIN).json()
    iid = inst["id"]
    assert inst["status"] == "installed_draft"
    # A pack never self-enables.
    r = client.post(f"/studio/installations/{iid}/enable",
                    json={"parameters": {}}, headers=ADMIN)
    assert r.status_code == 409

    out = _enable(client)
    applied = {(a["kind"], a["key"]) for a in out["applied_configuration"]}
    assert ("orderset", "oncology-regimen-folfox") in applied
    assert ("orderset", "oncology-regimen-ac") in applied
    assert ("form", "oncology-treatment-plan") in applied
    assert ("form", "oncology-staging") in applied
    assert ("form", "oncology-assessment") in applied
    assert ("form", "oncology-chemo-verification") in applied
    assert ("terminology", "formulary") in applied
    assert ("terminology", "medication-monitoring") in applied
    assert ("cohort", "oncology-registry") in applied

    registries = {e["key"] for e in
                  client.get("/pophealth/registries",
                             headers=CLINICIAN).json()}
    assert "oncology-registry" in registries


# ---------------------------------------------------------------------------
# 2. Regimen order sets place, cycle/day notes ride the orders
# ---------------------------------------------------------------------------

def test_regimen_order_sets_place_with_cycle_day_notes(client):
    _enable(client)
    _patient(client, "onc-1")

    sets = {s["key"]: s for s in
            client.get("/orders/sets", headers=CLINICIAN).json()["sets"]}
    for key, regimen, cycle_days in (
            ("oncology-regimen-folfox", "FOLFOX", "14-day"),
            ("oncology-regimen-ac", "AC", "21-day")):
        assert key in sets
        # The cycle structure is documented in the set itself.
        assert cycle_days in sets[key]["description"]

        r = client.post("/orders/from-set", json={
            "set": key, "patient": "onc-1"}, headers=CLINICIAN)
        assert r.status_code == 201, r.text
        summaries = r.json()["orders"]
        assert len(summaries) == len(sets[key]["orders"]) >= 5
        notes = []
        for summary in summaries:
            got = client.get(f"/orders/{summary['id']}",
                             headers=CLINICIAN)
            assert got.status_code == 200
            body = got.json()
            assert body["status"] == "draft"
            note = (body.get("note") or [{}])[0].get("text") or ""
            notes.append(note)
        # Every item's cycle/day note rode onto its placed order.
        assert all("Cycle 1 Day 1" in n or "Cycle 1 Day" in n
                   for n in notes), notes
        assert any(regimen in n for n in notes)
        assert any("Day 2" in n for n in notes)  # multi-day structure


# ---------------------------------------------------------------------------
# 3. Treatment-plan / staging / assessment / verification forms
# ---------------------------------------------------------------------------

def _submit(client, form, patient, answers):
    return client.post("/flowsheets", json={
        "form": form, "patient": patient, "answers": answers},
        headers=CLINICIAN)


def test_oncology_forms_validate(client):
    _enable(client)
    _patient(client, "onc-2")

    treatment_plan = [
        {"linkId": "regimen",
         "answer": [{"valueCoding": {"code": "oncology-regimen-folfox"}}]},
        {"linkId": "cycle-number", "answer": [{"valueInteger": 3}]},
        {"linkId": "intent",
         "answer": [{"valueCoding": {"code": "adjuvant"}}]},
        {"linkId": "bsa-m2", "answer": [{"valueDecimal": 1.82}]},
        {"linkId": "planned-cycles", "answer": [{"valueInteger": 6}]},
    ]
    r = _submit(client, "oncology-treatment-plan", "onc-2", treatment_plan)
    assert r.status_code == 201, r.text
    view = client.get("/flowsheets/oncology-treatment-plan",
                      params={"patient": "onc-2"},
                      headers=CLINICIAN).json()
    assert view["total"] == 1
    entry = view["entries"][0]["answers"]
    assert entry["cycle-number"]["value"] == 3
    assert entry["bsa-m2"]["value"] == 1.82
    # Dropping the REQUIRED BSA is a 422 naming the item.
    r = _submit(client, "oncology-treatment-plan", "onc-2",
                [a for a in treatment_plan if a["linkId"] != "bsa-m2"])
    assert r.status_code == 422
    assert "bsa-m2" in r.json()["detail"]

    staging = [
        {"linkId": "primary-site",
         "answer": [{"valueString": "Colon"}]},
        {"linkId": "tnm-t", "answer": [{"valueCoding": {"code": "T3"}}]},
        {"linkId": "tnm-n", "answer": [{"valueCoding": {"code": "N1"}}]},
        {"linkId": "tnm-m", "answer": [{"valueCoding": {"code": "M0"}}]},
        {"linkId": "stage-group",
         "answer": [{"valueCoding": {"code": "III"}}]},
    ]
    r = _submit(client, "oncology-staging", "onc-2", staging)
    assert r.status_code == 201, r.text

    assessment = [
        {"linkId": "assessment-summary",
         "answer": [{"valueString": "Cycle 3 review, tolerating well"}]},
        {"linkId": "ecog", "answer": [{"valueInteger": 1}]},
        {"linkId": "allergies-reviewed",
         "answer": [{"valueBoolean": True}]},
    ]
    r = _submit(client, "oncology-assessment", "onc-2", assessment)
    assert r.status_code == 201, r.text

    verification = [
        {"linkId": "regimen",
         "answer": [{"valueString": "FOLFOX"}]},
        {"linkId": "cycle-number", "answer": [{"valueInteger": 3}]},
        {"linkId": "patient-identity-verified",
         "answer": [{"valueBoolean": True}]},
        {"linkId": "dose-and-bsa-verified",
         "answer": [{"valueBoolean": True}]},
        {"linkId": "first-clinician",
         "answer": [{"valueString": "dev-clinician"}]},
        {"linkId": "second-clinician",
         "answer": [{"valueString": "dev-admin"}]},
        {"linkId": "verification-outcome",
         "answer": [{"valueCoding": {"code": "verified"}}]},
    ]
    r = _submit(client, "oncology-chemo-verification", "onc-2",
                verification)
    assert r.status_code == 201, r.text
    # The two-clinician check is real: no second clinician, no record.
    r = _submit(client, "oncology-chemo-verification", "onc-2",
                [a for a in verification
                 if a["linkId"] != "second-clinician"])
    assert r.status_code == 422
    assert "second-clinician" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 4. Methotrexate FLAG monitoring rule via the generic pharmacy hook
# ---------------------------------------------------------------------------

def test_methotrexate_flag_rule_warns_then_clears(client):
    _enable(client)
    _patient(client, "onc-3")
    oid = _active_med_order(client, "onc-3", METHOTREXATE)

    # No level / creatinine on file: the pack rule's action is FLAG —
    # the dispense proceeds and carries both warnings. (Wave A: a
    # current verification is recorded first — PHARM-B1; FLAG
    # warnings do not block verification.)
    r = client.post(f"/pharmacy/verification/{oid}/verify", json={},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/pharmacy/dispense",
                    json={"medication_request_id": oid},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    warnings = r.json()["monitoring_warnings"]
    assert {w["lab_code"] for w in warnings} == {MTX_LEVEL, CREATININE}
    assert all(w["medication_code"] == METHOTREXATE for w in warnings)
    assert r.json()["stock_after"] == 39  # 40 seeded by the pack - 1

    # With a current level + creatinine, a second order is clean.
    _observation(client, "onc-3", MTX_LEVEL, 0.05)
    _observation(client, "onc-3", CREATININE, 1.0)
    oid2 = _active_med_order(client, "onc-3", METHOTREXATE)
    r = client.post(f"/pharmacy/verification/{oid2}/verify", json={},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/pharmacy/dispense",
                    json={"medication_request_id": oid2},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["monitoring_warnings"] == []
