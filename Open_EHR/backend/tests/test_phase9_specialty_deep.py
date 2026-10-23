"""Phase 9 — DEEP end-to-end proofs for three specialty packs.

1. OPHTHALMOLOGY (Bascom Palmer pattern): enabling the pack publishes
   an order-type catalogue with an `eye-procedure` type that REQUIRES
   bodySite — placing one without laterality is a 422; the cataract
   order set lands laterality (OD coding) on every eye order; the
   imaging-centric exam form validates through the ordinary form
   machinery (and refuses a submission missing its required
   laterality item).
2. REHABILITATION (Shirley Ryan pattern): the FIM-shaped functional
   assessment and the therapy-minutes flowsheets accept entries and
   read back as time series.
3. PSYCHIATRY: the medication-monitoring rules the pack publishes
   BLOCK clozapine dispensing (missing ANC -> below-threshold ANC ->
   stale ANC -> unreleased ANC) until a current, in-range, RELEASED
   ANC exists; lithium's flag rule warns instead of blocking; and
   with no rules published (no pack) dispensing is untouched.
"""
from datetime import datetime, timedelta, timezone

from tests.conftest import ADMIN, CLINICIAN

LOINC = "http://loinc.org"
ANC = "751-8"
CLOZAPINE = "2623"
LITHIUM = "lithium-carbonate"


def _enable(client, name):
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
        "name": [{"family": "Synth", "given": ["Deep"]}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _active_med_order(client, pid, code):
    r = client.post("/orders", json={
        "type": "medication", "code": code, "patient": pid}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    oid = r.json()["order"]["id"]
    for to in ("signed", "active"):
        r = client.post(f"/orders/{oid}/transition",
                        json={"to_status": to}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    return oid


def _observation(client, pid, code, value, when, tagged=False):
    body = {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": LOINC, "code": code}]},
        "subject": {"reference": f"Patient/{pid}"},
        "effectiveDateTime": when.isoformat(),
        "valueQuantity": {"value": value, "unit": "10^3/uL"},
    }
    if tagged:
        body["meta"] = {"tag": [{"system": "urn:opencare:visibility",
                                 "code": "lab-unreleased"}]}
    r = client.post("/fhir/Observation", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 1. Ophthalmology — laterality enforced + present, form validates
# ---------------------------------------------------------------------------

def test_ophthalmology_laterality_end_to_end(client):
    _enable(client, "specialty-ophthalmology")
    _patient(client, "eye-1")

    # The published order-type catalogue now carries the eye type.
    types = client.get("/orders/types", headers=CLINICIAN).json()
    codes = {c["code"] for c in types["order_types"]["concepts"]}
    assert "eye-procedure" in codes

    # Enforcement: an eye procedure with NO bodySite (no laterality)
    # is refused by the catalogue's requiredFields mechanism.
    r = client.post("/orders", json={
        "type": "eye-procedure", "code": "ophth-biometry",
        "patient": "eye-1"}, headers=CLINICIAN)
    assert r.status_code == 422
    assert "bodySite" in r.json()["detail"]

    # The cataract order set places; every eye order carries the site
    # with the RIGHT (OD) laterality coding (read back as the full
    # FHIR resources, not the compact summaries).
    def placed_resources(response):
        out = []
        for summary in response.json()["orders"]:
            got = client.get(f"/orders/{summary['id']}", headers=CLINICIAN)
            assert got.status_code == 200
            out.append(got.json())  # the rendered FHIR resource itself
        return out

    r = client.post("/orders/from-set", json={
        "set": "ophthalmology-cataract-pathway", "patient": "eye-1"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    resources = placed_resources(r)
    eye_orders = [res for res in resources
                  if res["resourceType"] == "ServiceRequest"
                  and "bodySite" in res]
    assert len(eye_orders) == 3
    for order in eye_orders:
        codings = order["bodySite"]["coding"]
        assert {"system": "http://snomed.info/sct", "code": "24028007",
                "display": "Right"} in codings
        assert order["bodySite"]["text"] == "Right eye (OD)"

    # The bilateral injection set is bilateral-AWARE: one order per
    # eye, each with its own laterality.
    r = client.post("/orders/from-set", json={
        "set": "ophthalmology-injection-clinic", "patient": "eye-1"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    laterality = sorted(
        next(c["code"] for c in res["bodySite"]["coding"]
             if c["code"] in ("24028007", "7771000"))
        for res in placed_resources(r) if "bodySite" in res)
    assert laterality == ["24028007", "7771000"]

    # The imaging-centric exam form goes through the ordinary form
    # machinery (the same validator the web FormRenderer targets):
    # complete answers validate; dropping the REQUIRED laterality
    # item is a 422.
    answers = [
        {"linkId": "laterality",
         "answer": [{"valueCoding": {"code": "OU"}}]},
        {"linkId": "visual-acuity-od", "answer": [{"valueDecimal": 0.5}]},
        {"linkId": "visual-acuity-os", "answer": [{"valueDecimal": 0.8}]},
        {"linkId": "iop-od", "answer": [{"valueInteger": 16}]},
        {"linkId": "iop-os", "answer": [{"valueInteger": 15}]},
        {"linkId": "oct-performed", "answer": [{"valueBoolean": True}]},
    ]
    r = client.post("/flowsheets", json={
        "form": "ophthalmology-exam", "patient": "eye-1",
        "answers": answers}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/flowsheets", json={
        "form": "ophthalmology-exam", "patient": "eye-1",
        "answers": [a for a in answers if a["linkId"] != "laterality"]},
        headers=CLINICIAN)
    assert r.status_code == 422
    assert "laterality" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 2. Rehabilitation — functional outcomes + therapy minutes time series
# ---------------------------------------------------------------------------

def test_rehabilitation_flowsheets_time_series(client):
    _enable(client, "specialty-rehabilitation")
    _patient(client, "rehab-1")

    def fim(eating, transfers, locomotion):
        return [
            {"linkId": "fim-eating", "answer": [{"valueInteger": eating}]},
            {"linkId": "fim-transfers-bed",
             "answer": [{"valueInteger": transfers}]},
            {"linkId": "fim-locomotion",
             "answer": [{"valueInteger": locomotion}]},
            {"linkId": "fim-grooming", "answer": [{"valueInteger": 3}]},
            {"linkId": "fim-memory", "answer": [{"valueInteger": 5}]},
        ]

    # Admission assessment, then a later one with functional gain.
    r = client.post("/flowsheets", json={
        "form": "rehab-functional-assessment", "patient": "rehab-1",
        "answers": fim(2, 2, 1)}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/flowsheets", json={
        "form": "rehab-functional-assessment", "patient": "rehab-1",
        "answers": fim(6, 5, 5)}, headers=CLINICIAN)
    assert r.status_code == 201, r.text

    view = client.get("/flowsheets/rehab-functional-assessment",
                      params={"patient": "rehab-1"},
                      headers=CLINICIAN).json()
    assert view["total"] == 2
    first, second = view["entries"]
    assert first["answers"]["fim-eating"]["value"] == 2
    assert second["answers"]["fim-eating"]["value"] == 6
    assert second["answers"]["fim-locomotion"]["value"] == 5
    # The form definition itself rides the view (rows of the grid).
    link_ids = {i["linkId"] for i in view["form"]["items"]}
    assert {"fim-eating", "fim-stairs", "fim-comprehension"} <= link_ids

    # Therapy minutes: delivered PT/OT/speech per therapy day.
    r = client.post("/flowsheets", json={
        "form": "rehab-therapy-minutes", "patient": "rehab-1",
        "answers": [
            {"linkId": "therapy-day", "answer": [{"valueInteger": 1}]},
            {"linkId": "pt-minutes", "answer": [{"valueInteger": 60}]},
            {"linkId": "ot-minutes", "answer": [{"valueInteger": 45}]},
            {"linkId": "speech-minutes", "answer": [{"valueInteger": 30}]},
        ]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    view = client.get("/flowsheets/rehab-therapy-minutes",
                      params={"patient": "rehab-1"},
                      headers=CLINICIAN).json()
    assert view["total"] == 1
    entry = view["entries"][0]["answers"]
    assert entry["pt-minutes"]["value"] == 60
    assert entry["ot-minutes"]["value"] == 45
    assert entry["speech-minutes"]["value"] == 30
    total_minutes = sum(entry[k]["value"] for k in
                        ("pt-minutes", "ot-minutes", "speech-minutes"))
    assert total_minutes == 135


# ---------------------------------------------------------------------------
# 3. Psychiatry — clozapine ANC gating (block) + lithium flag
# ---------------------------------------------------------------------------

def test_psychiatry_clozapine_anc_gate(client):
    _enable(client, "specialty-psychiatry")
    _patient(client, "psy-1")
    oid = _active_med_order(client, "psy-1", CLOZAPINE)

    # No ANC on file -> BLOCKED (409), and nothing is dispensed.
    r = client.post("/pharmacy/dispense",
                    json={"medication_request_id": oid}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "Medication monitoring block" in r.json()["detail"]
    assert "Absolute neutrophil count" in r.json()["detail"]
    queue = client.get("/pharmacy/dispense-queue", headers=CLINICIAN).json()
    assert any(i["request"]["id"] == oid for i in queue["items"])

    # ANC below the rule's minimum (1.5) -> still blocked.
    _observation(client, "psy-1", ANC, 1.2, _now())
    r = client.post("/pharmacy/dispense",
                    json={"medication_request_id": oid}, headers=CLINICIAN)
    assert r.status_code == 409
    assert "below the required minimum" in r.json()["detail"]

    # A current, in-range ANC -> the dispense proceeds, stock moves.
    # (Wave A: verification is recorded once the ANC is in range —
    # PHARM-B1/M1: the same blocker would refuse verification too.)
    _observation(client, "psy-1", ANC, 2.8, _now())
    r = client.post(f"/pharmacy/verification/{oid}/verify", json={},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/pharmacy/dispense",
                    json={"medication_request_id": oid}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["stock_after"] == 29  # 30 seeded by the pack - 1
    assert r.json()["monitoring_warnings"] == []


def test_psychiatry_clozapine_stale_and_unreleased_anc_do_not_count(client):
    _enable(client, "specialty-psychiatry")
    _patient(client, "psy-2")
    oid = _active_med_order(client, "psy-2", CLOZAPINE)

    # A stale ANC (40 days old; the rule allows 28) does not count.
    _observation(client, "psy-2", ANC, 3.0, _now() - timedelta(days=40))
    r = client.post("/pharmacy/dispense",
                    json={"medication_request_id": oid}, headers=CLINICIAN)
    assert r.status_code == 409
    assert "days old" in r.json()["detail"]

    # An UNRELEASED (lab-draft tagged) ANC does not count either —
    # monitoring evidence must be a released result.
    _patient(client, "psy-3")
    oid3 = _active_med_order(client, "psy-3", CLOZAPINE)
    _observation(client, "psy-3", ANC, 3.0, _now(), tagged=True)
    r = client.post("/pharmacy/dispense",
                    json={"medication_request_id": oid3}, headers=CLINICIAN)
    assert r.status_code == 409
    assert "no Absolute neutrophil count" in r.json()["detail"]


def test_psychiatry_lithium_flag_warns_but_dispenses(client):
    _enable(client, "specialty-psychiatry")
    _patient(client, "psy-4")
    oid = _active_med_order(client, "psy-4", LITHIUM)

    # No lithium level / creatinine on file: the rule's action is
    # FLAG — the dispense proceeds and carries the warnings. (Wave A:
    # verification first — PHARM-B1; FLAG warnings never block it.)
    r = client.post(f"/pharmacy/verification/{oid}/verify", json={},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/pharmacy/dispense",
                    json={"medication_request_id": oid}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    warnings = r.json()["monitoring_warnings"]
    assert {w["lab_code"] for w in warnings} == {"14334-7", "2160-0"}

    # With current level + creatinine, a second order dispenses clean.
    _observation(client, "psy-4", "14334-7", 0.9, _now())
    _observation(client, "psy-4", "2160-0", 1.0, _now())
    oid2 = _active_med_order(client, "psy-4", LITHIUM)
    r = client.post(f"/pharmacy/verification/{oid2}/verify", json={},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/pharmacy/dispense",
                    json={"medication_request_id": oid2}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["monitoring_warnings"] == []


def test_monitoring_gate_inert_without_pack(client):
    """No pack enabled -> no monitoring config published -> the gate
    changes nothing (Phase 4A behaviour preserved byte-for-byte in
    outcome)."""
    _patient(client, "plain-1")
    # Sertraline is in the SEEDED formulary and has no rule anywhere.
    oid = _active_med_order(client, "plain-1", "1048467")
    # Wave A (PHARM-B1): a current verification is the one new
    # precondition; the monitoring gate itself stays inert.
    r = client.post(f"/pharmacy/verification/{oid}/verify", json={},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/pharmacy/dispense",
                    json={"medication_request_id": oid}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["monitoring_warnings"] == []
    # And clozapine is not even in the formulary without the pack:
    # the 422 is the formulary's, proving the pack owns the extension.
    oid2 = _active_med_order(client, "plain-1", CLOZAPINE)
    r = client.post("/pharmacy/dispense",
                    json={"medication_request_id": oid2}, headers=CLINICIAN)
    assert r.status_code == 422
    assert "formulary" in r.json()["detail"]
