"""Phase 15 closure (coordinator) — GET /portal/telehealth/visits:
the patient-scoped visit list that the portal outcome action needs
(BD-28 web closure). Ownership is the visit row's patient_ref — the
same check the by-id and outcome routes enforce. Synthetic data only.
"""
from tests.conftest import ADMIN, CLINICIAN, PATIENT


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic", "given": ["Test"]}]}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _telehealth_appointment(client, pid, when):
    body = {"resourceType": "Appointment", "status": "booked",
            "appointmentType": {"coding": [
                {"system": "urn:opencare:slot-types", "code": "telehealth"}]},
            "start": when, "end": when,
            "participant": [{"actor": {"reference": f"Patient/{pid}"},
                             "status": "accepted"}]}
    r = client.post("/fhir/Appointment", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _visit(client, appt):
    r = client.post("/telehealth/visits", json={
        "appointment_id": appt, "service_line": "general"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def test_portal_visit_list_shows_only_own_visits(client):
    _patient(client, "pat-1")
    _patient(client, "pat-2")
    mine = _visit(client, _telehealth_appointment(client, "pat-1", "2026-11-02T15:00:00Z"))
    _visit(client, _telehealth_appointment(client, "pat-2", "2026-11-02T16:00:00Z"))
    r = client.get("/portal/telehealth/visits", headers=PATIENT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["patient"] == "Patient/pat-1"
    assert body["total"] == 1
    assert body["items"][0]["visit_id"] == mine["visit_id"]
    assert body["items"][0]["patient_ref"] == "Patient/pat-1"
    # The fields the portal outcome action keys on are present.
    assert body["items"][0]["status"] == "scheduled"
    assert body["items"][0]["outcome_submitted"] is False


def test_portal_visit_list_gates(client):
    # Staff tokens cannot use the patient list (patient role only).
    assert client.get("/portal/telehealth/visits",
                      headers=CLINICIAN).status_code == 403
    # A patient with no visits gets an empty list, not an error.
    _patient(client, "pat-1")
    r = client.get("/portal/telehealth/visits", headers=PATIENT)
    assert r.status_code == 200 and r.json()["total"] == 0
