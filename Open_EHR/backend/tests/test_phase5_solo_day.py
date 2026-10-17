"""Phase 5 SQA scenario (roadmap §5 gate): ONE SYNTHETIC SOLO-PRACTICE
DAY, end to end through the public APIs.

Enable the solo-practice profile pack -> book a 12-patient panel ->
for the first patient: check in the appointment, record vitals on the
flowsheet form, write + sign a progress note from the template, run a
POCT HbA1c that results AND releases in a single step, sign a refill
order, and generate the superbill claim from the encounter. Every
step's status is asserted through the APIs a real deployment uses.
All patients, names and values are fabricated.
"""
from datetime import datetime, time, timedelta, timezone

from tests.conftest import ADMIN, CLINICIAN

TODAY = datetime.now(timezone.utc).date()


def _at(hour, minute=0):
    return datetime.combine(TODAY, time(hour, minute),
                            tzinfo=timezone.utc).isoformat()


def _enable_solo_profile(client):
    inst = client.post("/studio/packs/profile-solo-practice/install",
                       headers=ADMIN).json()
    iid = inst["id"]
    assert client.post(f"/studio/installations/{iid}/approve",
                       headers=ADMIN).status_code == 200
    assert client.post(f"/studio/installations/{iid}/attach",
                       headers=ADMIN).status_code == 200
    r = client.post(f"/studio/installations/{iid}/enable",
                    json={"parameters": {"orgName": "Dr. Synthetic's Office"}},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "enabled"
    assert len(r.json()["applied_configuration"]) == 7


def test_solo_practice_day_end_to_end(client):
    _enable_solo_profile(client)
    branding = client.get("/studio/config/branding/effective/default",
                          headers=ADMIN).json()["payload"]
    assert branding["orgName"] == "Dr. Synthetic's Office"

    # --- The day's panel: 12 fabricated patients, each booked. --------
    patient_ids = [f"solo-{i:02d}" for i in range(1, 13)]
    for i, pid in enumerate(patient_ids):
        r = client.post("/fhir/Patient", json={
            "resourceType": "Patient", "id": pid,
            "name": [{"family": f"Synthetic-{i + 1:02d}",
                      "given": ["Panel"]}],
            "gender": "female" if i % 2 else "male",
            "birthDate": f"{1960 + i}-03-04"}, headers=CLINICIAN)
        assert r.status_code == 201, r.text
        start = datetime.combine(TODAY, time(8, 0), tzinfo=timezone.utc) \
            + timedelta(minutes=30 * i)
        r = client.post("/fhir/Appointment", json={
            "resourceType": "Appointment", "status": "booked",
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=25)).isoformat(),
            "participant": [
                {"actor": {"reference": f"Patient/{pid}"}, "status": "accepted"},
                {"actor": {"reference": "Practitioner/prac-1"},
                 "status": "accepted"}]}, headers=CLINICIAN)
        assert r.status_code == 201, r.text
    booked = client.get("/fhir/Appointment?status=booked",
                        headers=CLINICIAN).json()
    assert booked["total"] == 12

    pid = "solo-01"
    appt = client.get(f"/fhir/Appointment?patient={pid}",
                      headers=CLINICIAN).json()["entry"][0]["resource"]

    # --- Check-in ------------------------------------------------------
    r = client.post(f"/fhir/Appointment/{appt['id']}/transition",
                    json={"to_status": "arrived"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "arrived"

    # --- Encounter + problem (type 2 diabetes) -------------------------
    r = client.post("/fhir/Encounter", json={
        "resourceType": "Encounter", "id": "enc-solo-01",
        "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                  "code": "AMB", "display": "ambulatory"},
        "subject": {"reference": f"Patient/{pid}"},
        "period": {"start": _at(0, 0), "end": _at(23, 59)}},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/fhir/Condition", json={
        "resourceType": "Condition", "id": "cond-solo-01",
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"coding": [{"system": "http://snomed.info/sct",
                             "code": "44054006",
                             "display": "Type 2 diabetes mellitus"}]},
        "subject": {"reference": f"Patient/{pid}"},
        "encounter": {"reference": "Encounter/enc-solo-01"}},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text

    # --- Vitals on the flowsheet form ----------------------------------
    r = client.post("/flowsheets", json={
        "patient": pid, "form": "flowsheet-vitals",
        "answers": [
            {"linkId": "systolic", "answer": [{"valueInteger": 128}]},
            {"linkId": "diastolic", "answer": [{"valueInteger": 82}]},
            {"linkId": "heart-rate", "answer": [{"valueInteger": 74}]},
            {"linkId": "spo2", "answer": [{"valueInteger": 98}]}]},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    view = client.get(f"/flowsheets/flowsheet-vitals?patient={pid}",
                      headers=CLINICIAN).json()
    assert view["total"] == 1
    assert view["entries"][0]["answers"]["systolic"]["value"] == 128

    # --- Progress note from the template, signed ------------------------
    r = client.post("/notes", json={
        "patient": pid, "note_type": "progress",
        "encounter": "enc-solo-01",
        "answers": [
            {"linkId": "subjective",
             "answer": [{"valueString": "Here for diabetes review."}]},
            {"linkId": "assessment",
             "answer": [{"valueString": "T2DM, control to assess by A1c."}]},
            {"linkId": "plan",
             "answer": [{"valueString": "POCT A1c today; refill metformin."}]}]},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    note = r.json()
    assert note["docStatus"] == "preliminary"
    r = client.post(f"/notes/{note['id']}/sign", headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["docStatus"] == "final"

    # --- POCT HbA1c: resulted + released in ONE step --------------------
    r = client.post("/lab/orders", json={
        "code": "4548-4", "subject": {"reference": f"Patient/{pid}"},
        "encounter": {"reference": "Encounter/enc-solo-01"}, "poct": True},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    lab_order_id = r.json()["id"]
    for to in ("collected", "received"):
        r = client.post(f"/lab/orders/{lab_order_id}/transition",
                        json={"to_status": to}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    r = client.post(f"/lab/orders/{lab_order_id}/results",
                    json={"results": [{"code": "4548-4", "value": 7.4}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["report"]["status"] == "final"  # no verify step in POCT
    detail = client.get(f"/lab/orders/{lab_order_id}",
                        headers=CLINICIAN).json()
    assert detail["order"]["status"] == "released"
    inbox = client.get(f"/fhir/inbox?patient={pid}",
                       headers=CLINICIAN).json()
    # The patient-visible result is in the inbox immediately (the
    # bundle carries the report + its observation).
    assert any(e["resource"]["resourceType"] == "DiagnosticReport"
               for e in inbox["entry"])

    # --- Refill order: placed, signed, active ----------------------------
    r = client.post("/orders", json={
        "type": "medication", "code": "847232", "display": "Metformin",
        "patient": pid}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    refill_id = r.json()["order"]["id"]
    assert r.json()["order"]["status"] == "draft"
    for to, expected in (("signed", "signed"), ("active", "active")):
        r = client.post(f"/orders/{refill_id}/transition",
                        json={"to_status": to}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == expected

    # --- Superbill: claim generated from the encounter -------------------
    # Claim generation is a billing.manage surface (defaults: admin /
    # configurator) — in the solo profile the practice owner runs
    # billing under that role; the profile changes no billing defaults.
    r = client.post("/billing/claims/generate",
                    json={"encounter_id": "enc-solo-01"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    claim = r.json()
    codes = {item["productOrService"]["coding"][0]["code"]
             for item in claim["item"]}
    assert "44054006" in codes  # diabetes evaluation, from the problem list
    assert "4548-4" in codes    # the POCT A1c, captured from the encounter
    prices = {item["productOrService"]["coding"][0]["code"]:
              item["unitPrice"]["value"] for item in claim["item"]}
    assert prices["44054006"] == 55.00   # published fee schedule
    assert prices["4548-4"] == 28.00
    assert claim["total"]["value"] == 83.00
