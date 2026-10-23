"""Phase 15 — Infection prevention (app.infection_prevention).

Proofs:
1. HAI definitions + isolation types are published Studio config
   (all six HAI types with criteria + denominator type), read-gated
   by ip.read.
2. Case lifecycle: intake validates the type against the published
   definitions; reported -> under-review -> confirmed works;
   ruled-out REQUIRES a note (409 without, and the case does not
   move); illegal jumps are 409; writes are ip.work-gated.
3. SSI: the feed endpoint works whether or not the surgery
   workstream's outcome tables exist (guarded assertions), and the
   guaranteed intake path carries SSI cases end-to-end.
4. Device-day denominators + rates are exact arithmetic over
   confirmed cases only; a zero denominator and a non-device-rated
   type both report rate null with an explicit reason.
5. Isolation precautions: start (type from published terminology),
   per-patient + per-unit active lists, stop, double-stop 409.
6. Stewardship referral creates a real FHIR Task addressed to the
   stewardship audience named in config, linked back on the case.
"""
import pytest

from tests.conftest import CLINICIAN, PATIENT, VIEWER

JAN = {"from": "2026-01-01", "to": "2026-01-31"}


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synth", "given": ["Ip"]}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _case(client, hai_type, patient, onset="2026-01-10", unit=None,
          source="unit surveillance"):
    body = {"type": hai_type, "patient": patient, "onset_date": onset,
            "source_ref": source}
    if unit:
        body["unit"] = unit
    r = client.post("/infection/cases", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _confirm(client, case_id):
    r = client.post(f"/infection/cases/{case_id}/transition",
                    json={"to_status": "under-review"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/infection/cases/{case_id}/transition",
                    json={"to_status": "confirmed"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def _device_days(client, unit, day, central, catheter, vent):
    r = client.post("/infection/device-days", json={
        "unit": unit, "date": day, "central_line_days": central,
        "urinary_catheter_days": catheter, "ventilator_days": vent},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _rates(client, **params):
    r = client.get("/infection/rates", params=params, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return {i["type"]: i for i in r.json()["items"]}


# ---------------------------------------------------------------------------
# 1. Definitions are published config
# ---------------------------------------------------------------------------

def test_definitions_are_published_config(client):
    r = client.get("/infection/definitions", headers=VIEWER)
    assert r.status_code == 200, r.text
    body = r.json()
    concepts = {c["code"]: c
                for c in body["hai_definitions"]["concepts"]}
    assert {"ssi", "clabsi", "cauti", "vae", "mrsa", "c-difficile"} \
        <= set(concepts)
    for concept in concepts.values():
        assert concept["criteria"]  # criteria text on every type
        assert concept["denominatorType"]
    assert concepts["clabsi"]["denominatorType"] == "central-line-days"
    assert concepts["cauti"]["denominatorType"] == "urinary-catheter-days"
    assert concepts["vae"]["denominatorType"] == "ventilator-days"
    iso = {c["code"] for c in body["isolation_precautions"]["concepts"]}
    assert {"contact", "droplet", "airborne"} <= iso
    stewardship = body["hai_definitions"]["stewardship"]
    assert stewardship["team"] == "antimicrobial-stewardship"
    # The patient role holds neither ip capability.
    assert client.get("/infection/definitions",
                      headers=PATIENT).status_code == 403


# ---------------------------------------------------------------------------
# 2. Case lifecycle
# ---------------------------------------------------------------------------

def test_case_lifecycle_and_ruled_out_note_gate(client):
    _patient(client, "ip-1")
    case = _case(client, "clabsi", "ip-1")
    assert case["status"] == "reported"
    cid = case["case_id"]

    # Unknown type (not in the published definitions) -> 422.
    r = client.post("/infection/cases", json={
        "type": "not-a-hai", "patient": "ip-1",
        "onset_date": "2026-01-10"}, headers=CLINICIAN)
    assert r.status_code == 422
    # Writes are ip.work-gated: the viewer may read, never record.
    r = client.post("/infection/cases", json={
        "type": "cauti", "patient": "ip-1",
        "onset_date": "2026-01-10"}, headers=VIEWER)
    assert r.status_code == 403

    # reported -> confirmed directly is illegal (must be reviewed).
    r = client.post(f"/infection/cases/{cid}/transition",
                    json={"to_status": "confirmed"}, headers=CLINICIAN)
    assert r.status_code == 409
    r = client.post(f"/infection/cases/{cid}/transition",
                    json={"to_status": "under-review"}, headers=CLINICIAN)
    assert r.status_code == 200
    assert r.json()["status"] == "under-review"

    # Ruled-out REQUIRES a note: 409 without, and the case does not move.
    r = client.post(f"/infection/cases/{cid}/transition",
                    json={"to_status": "ruled-out"}, headers=CLINICIAN)
    assert r.status_code == 409
    assert "note" in r.json()["detail"]
    got = client.get(f"/infection/cases/{cid}", headers=CLINICIAN).json()
    assert got["status"] == "under-review"
    # With the reviewer's note it lands, and the note is on the record.
    r = client.post(f"/infection/cases/{cid}/transition",
                    json={"to_status": "ruled-out",
                          "note": "Culture attributed to skin flora; "
                                  "repeat cultures negative."},
                    headers=CLINICIAN)
    assert r.status_code == 200
    assert r.json()["status"] == "ruled-out"
    assert "skin flora" in r.json()["note"]
    # Terminal: no further transitions.
    r = client.post(f"/infection/cases/{cid}/transition",
                    json={"to_status": "under-review"}, headers=CLINICIAN)
    assert r.status_code == 409

    # The confirm path, and list filters.
    case2 = _case(client, "cauti", "ip-1")
    _confirm(client, case2["case_id"])
    items = client.get("/infection/cases", params={"status": "confirmed"},
                       headers=CLINICIAN).json()
    assert [i["case_id"] for i in items["items"]] == [case2["case_id"]]
    items = client.get("/infection/cases", params={"patient": "ip-1"},
                       headers=CLINICIAN).json()
    assert items["total"] == 2


# ---------------------------------------------------------------------------
# 3. SSI feed (guarded) + guaranteed intake path
# ---------------------------------------------------------------------------

def test_ssi_feed_guarded_and_intake_path(client):
    r = client.get("/infection/ssi-feed", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    feed = r.json()
    # Guarded: assert the endpoint works, not the other workstream's
    # data — if its tables are absent the feed is honestly empty.
    assert isinstance(feed["items"], list)
    assert feed["source"] in ("unavailable", "surgery-outcomes")
    if feed["source"] == "unavailable":
        assert feed["items"] == []
        assert "intake" in feed["note"]

    # The guaranteed intake path carries an SSI case end-to-end.
    _patient(client, "ip-ssi")
    case = _case(client, "ssi", "ip-ssi", source="Procedure/proc-1 outcome")
    _confirm(client, case["case_id"])
    items = client.get("/infection/cases", params={"type": "ssi"},
                       headers=CLINICIAN).json()
    assert items["total"] == 1
    assert items["items"][0]["status"] == "confirmed"


# ---------------------------------------------------------------------------
# 4. Device-days + exact rates
# ---------------------------------------------------------------------------

def test_device_days_and_rates_exact_math(client):
    _patient(client, "ip-2")
    _device_days(client, "ICU", "2026-01-10", 250, 200, 100)
    _device_days(client, "ICU", "2026-01-11", 250, 100, 50)
    # Totals: central 500, urinary 300, ventilator 150.

    _confirm(client, _case(client, "clabsi", "ip-2", unit="ICU")["case_id"])
    _confirm(client, _case(client, "clabsi", "ip-2", unit="ICU",
                           onset="2026-01-12")["case_id"])
    _confirm(client, _case(client, "cauti", "ip-2", unit="ICU")["case_id"])
    _confirm(client, _case(client, "ssi", "ip-2", unit="ICU")["case_id"])
    # A merely-reported VAE must NOT count (confirmed cases only).
    _case(client, "vae", "ip-2", unit="ICU")

    rates = _rates(client, **JAN)
    assert rates["clabsi"]["confirmed_cases"] == 2
    assert rates["clabsi"]["device_days"] == 500
    assert rates["clabsi"]["rate_per_1000"] == pytest.approx(4.0)
    assert rates["cauti"]["confirmed_cases"] == 1
    assert rates["cauti"]["device_days"] == 300
    assert rates["cauti"]["rate_per_1000"] == pytest.approx(1000.0 / 300.0)
    assert rates["vae"]["confirmed_cases"] == 0
    assert rates["vae"]["device_days"] == 150
    assert rates["vae"]["rate_per_1000"] == 0.0
    # SSI is not device-day rated: null rate, explicit reason.
    assert rates["ssi"]["confirmed_cases"] == 1
    assert rates["ssi"]["rate_per_1000"] is None
    assert "not rated" in rates["ssi"]["reason"]

    # Zero denominator: February has no device-days recorded.
    rates = _rates(client, **{"from": "2026-02-01", "to": "2026-02-28"})
    assert rates["clabsi"]["device_days"] == 0
    assert rates["clabsi"]["rate_per_1000"] is None
    assert "zero" in rates["clabsi"]["reason"]

    # Unit filter: Ward denominators + a Ward case rate separately.
    _device_days(client, "Ward", "2026-01-10", 100, 0, 0)
    _confirm(client, _case(client, "clabsi", "ip-2", unit="Ward")["case_id"])
    rates = _rates(client, unit="Ward", **JAN)
    assert rates["clabsi"]["device_days"] == 100
    assert rates["clabsi"]["confirmed_cases"] == 1
    assert rates["clabsi"]["rate_per_1000"] == pytest.approx(10.0)
    # Zero recorded catheter days on Ward: CAUTI rate is null, not 0/0.
    assert rates["cauti"]["rate_per_1000"] is None
    assert "zero" in rates["cauti"]["reason"]


def test_device_days_validation(client):
    r = client.post("/infection/device-days", json={
        "unit": "ICU", "date": "2026-01-10", "central_line_days": -1,
        "urinary_catheter_days": 0, "ventilator_days": 0},
        headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post("/infection/device-days", json={
        "unit": "ICU", "date": "2026-01-10", "central_line_days": 1.5,
        "urinary_catheter_days": 0, "ventilator_days": 0},
        headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post("/infection/device-days", json={
        "unit": "ICU", "date": "2026-01-10", "central_line_days": True,
        "urinary_catheter_days": 0, "ventilator_days": 0},
        headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post("/infection/device-days", json={
        "unit": "ICU", "date": "not-a-date", "central_line_days": 1,
        "urinary_catheter_days": 0, "ventilator_days": 0},
        headers=CLINICIAN)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 5. Isolation precautions
# ---------------------------------------------------------------------------

def test_isolation_precautions_lifecycle(client):
    _patient(client, "ip-3")
    r = client.post("/infection/precautions", json={
        "patient": "ip-3", "type": "contact",
        "reason": "MRSA bacteraemia under review", "unit": "ICU",
        "start": "2026-01-10"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    precaution = r.json()
    assert precaution["status"] == "active"
    pid = precaution["precaution_id"]

    # Unknown type (not in the published terminology) -> 422.
    r = client.post("/infection/precautions", json={
        "patient": "ip-3", "type": "force-field", "reason": "x"},
        headers=CLINICIAN)
    assert r.status_code == 422
    # Writes are ip.work-gated.
    r = client.post("/infection/precautions", json={
        "patient": "ip-3", "type": "droplet", "reason": "flu"},
        headers=VIEWER)
    assert r.status_code == 403

    # Per-patient and per-unit active lists.
    items = client.get("/infection/precautions",
                       params={"patient": "ip-3", "active_only": True},
                       headers=CLINICIAN).json()
    assert [i["precaution_id"] for i in items["items"]] == [pid]
    items = client.get("/infection/precautions",
                       params={"unit": "ICU", "active_only": True},
                       headers=CLINICIAN).json()
    assert items["total"] == 1
    items = client.get("/infection/precautions",
                       params={"unit": "Ward", "active_only": True},
                       headers=CLINICIAN).json()
    assert items["total"] == 0

    # Stop: leaves the active board; double-stop is a 409.
    r = client.post(f"/infection/precautions/{pid}/stop",
                    json={"note": "Two negative screens"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "stopped"
    assert r.json()["stopped_at"]
    items = client.get("/infection/precautions",
                       params={"patient": "ip-3", "active_only": True},
                       headers=CLINICIAN).json()
    assert items["total"] == 0
    r = client.post(f"/infection/precautions/{pid}/stop",
                    json={}, headers=CLINICIAN)
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# 6. Stewardship referral -> real FHIR Task
# ---------------------------------------------------------------------------

def test_stewardship_referral_creates_task(client):
    _patient(client, "ip-4")
    case = _case(client, "mrsa", "ip-4")
    _confirm(client, case["case_id"])

    r = client.post(
        f"/infection/cases/{case['case_id']}/stewardship-referral",
        json={"note": "Review vancomycin course"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["existing"] is False
    task = body["task"]
    assert task["resourceType"] == "Task"
    assert task["status"] == "requested"
    assert task["for"]["reference"] == "Patient/ip-4"
    # Addressed to the audience named in the published config.
    assert "antimicrobial stewardship" in task["owner"]["display"]
    assert "mrsa" in task["description"]

    # The Task is a real FHIR resource, readable through the generic
    # surface, and the case links back to it.
    got = client.get(f"/fhir/Task/{task['id']}", headers=CLINICIAN)
    assert got.status_code == 200
    detail = client.get(f"/infection/cases/{case['case_id']}",
                        headers=CLINICIAN).json()
    assert detail["stewardship_task_id"] == task["id"]

    # A repeat referral returns the same Task, never a duplicate.
    r = client.post(
        f"/infection/cases/{case['case_id']}/stewardship-referral",
        json={}, headers=CLINICIAN)
    assert r.status_code == 201
    assert r.json()["existing"] is True
    assert r.json()["task"]["id"] == task["id"]
