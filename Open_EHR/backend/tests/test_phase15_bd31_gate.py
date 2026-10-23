"""Phase 15 (BD-31) — coordinator integration test: the credential
gate wired into the REAL booking path (app.access
POST /access/waitlist/{id}/accept), not just the standalone checker
endpoint (whose unit behaviour lives in
tests/test_phase15_access_services.py).

Gate contract: the accept names BOTH a practitioner and a privilege
-> the booking is refused (409, naming the gate) unless
app.access_services.evaluate_credential_check finds a covering,
unexpired credential for that clinician at the appointment start.
No privilege named -> no gate (pre-Phase-15 behaviour unchanged).
Synthetic data only.
"""
from datetime import datetime, timedelta, timezone

from tests.conftest import ADMIN, CLINICIAN


def _today():
    return datetime.now(timezone.utc).date()


def _date(offset_days):
    return (_today() + timedelta(days=offset_days)).isoformat()


def _start(offset_days):
    return f"{_date(offset_days)}T09:00:00Z"


def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic", "given": ["Test"]}]}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _staff(client, staff_id, username):
    r = client.post("/people/staff", json={
        "staff_id": staff_id, "username": username,
        "full_name": f"Dr {username}", "job_role": "physician"}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _credential(client, staff_id, cred_id, expires, ctype="medical-licence"):
    r = client.post("/people/credentials", json={
        "staff_id": staff_id, "credential_id": cred_id, "type": ctype,
        "number": f"LIC-{cred_id}", "expires_on": expires}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _offered_entry(client, appt_id):
    """A waitlist entry in `offered` state (phase-7 flow)."""
    body = {"resourceType": "Appointment", "id": appt_id, "status": "booked",
            "appointmentType": {"coding": [{
                "system": "urn:opencare:slot-type", "code": "follow-up",
                "display": "Follow-up visit"}]},
            "start": "2026-10-20T09:00:00Z",
            "participant": [{"actor": {"reference": "Patient/pat-1"},
                             "status": "accepted"}]}
    r = client.post("/fhir/Appointment", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/access/waitlist",
                    json={"patient": "pat-1", "slot_type": "follow-up"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    entry = r.json()
    r = client.post(f"/fhir/Appointment/{appt_id}/transition",
                    json={"to_status": "cancelled"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    r = client.post("/access/waitlist/offers",
                    json={"appointment_id": appt_id}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["entry"]["id"] == entry["id"]
    return entry["id"]


def _entry_status(client, entry_id):
    rows = client.get("/access/waitlist", headers=ADMIN).json()["items"]
    return next(e["status"] for e in rows if e["id"] == entry_id)


def test_accept_without_privilege_is_ungated(client):
    _patient(client)
    entry_id = _offered_entry(client, "appt-g0")
    # Practitioner named, NO privilege: books exactly as before.
    r = client.post(f"/access/waitlist/{entry_id}/accept",
                    json={"start": _start(1), "practitioner": "dr-1"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["entry"]["status"] == "booked"


def test_accept_with_valid_credential_books(client):
    _patient(client)
    _staff(client, "staff-valid", "dr-valid")
    _credential(client, "staff-valid", "cred-valid", _date(365))
    entry_id = _offered_entry(client, "appt-g1")
    r = client.post(f"/access/waitlist/{entry_id}/accept",
                    json={"start": _start(1), "practitioner": "staff-valid",
                          "privilege": "medical-licence"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    appt = r.json()["appointment"]
    assert appt["status"] == "booked"
    refs = [p["actor"]["reference"] for p in appt["participant"]]
    assert "Practitioner/staff-valid" in refs


def test_accept_with_expired_credential_refused_and_entry_stays_offered(client):
    _patient(client)
    _staff(client, "staff-expired", "dr-expired")
    _credential(client, "staff-expired", "cred-expired", _date(-1))
    entry_id = _offered_entry(client, "appt-g2")
    r = client.post(f"/access/waitlist/{entry_id}/accept",
                    json={"start": _start(1), "practitioner": "staff-expired",
                          "privilege": "medical-licence"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "Credential-gated scheduling" in r.json()["detail"]
    assert _entry_status(client, entry_id) == "offered"


def test_accept_with_absent_or_unknown_credential_refused(client):
    _patient(client)
    _staff(client, "staff-none", "dr-none")
    entry_id = _offered_entry(client, "appt-g3")
    r = client.post(f"/access/waitlist/{entry_id}/accept",
                    json={"start": _start(1), "practitioner": "staff-none",
                          "privilege": "medical-licence"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "Credential-gated scheduling" in r.json()["detail"]
    # A practitioner with no staff-directory row at all is refused too.
    entry2 = _offered_entry(client, "appt-g4")
    r = client.post(f"/access/waitlist/{entry2}/accept",
                    json={"start": _start(1), "practitioner": "ghost-1",
                          "privilege": "medical-licence"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert _entry_status(client, entry2) == "offered"


def test_accept_gate_is_instant_dependent(client):
    _patient(client)
    _staff(client, "staff-soon", "dr-soon")
    _credential(client, "staff-soon", "cred-soon", _date(10))
    # Booking inside the credential's validity books.
    entry_id = _offered_entry(client, "appt-g5")
    r = client.post(f"/access/waitlist/{entry_id}/accept",
                    json={"start": _start(5), "practitioner": "staff-soon",
                          "privilege": "medical-licence"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # Booking after its expiry is refused.
    entry2 = _offered_entry(client, "appt-g6")
    r = client.post(f"/access/waitlist/{entry2}/accept",
                    json={"start": _start(20), "practitioner": "staff-soon",
                          "privilege": "medical-licence"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert _entry_status(client, entry2) == "offered"
