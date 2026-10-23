"""UX audit F-1 — front-desk check-in (register → book → CHECK IN).

Before this pass the journey ended at booking: the FrontDeskSteps
strip advertised a check-in step no screen rendered, the Home hint
pointed at a Scheduling page the front-desk role cannot open (and
which has no check-in action), and the only arrival-shaped action
("Room patient") belongs to the clinical team. Check-in is now a
real step in the front desk's own flow: POST
/continuum/clinic/appointments/{id}/check-in accepts
registration.work (the front desk's capability, the same widening
booking already uses) or continuum.work; the clinic-day view lists
the day's booked appointments as `arrivals`; rooming stays
continuum.work-only (403 for the front desk, by design).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.models import AuditLog

from .conftest import ADMIN, CLINICIAN, PATIENT, VIEWER

TODAY = date.today().isoformat()


def _patient(client, pid):
    resp = client.post("/fhir/Patient",
                       json={"resourceType": "Patient", "id": pid,
                             "name": [{"family": "Checkin",
                                       "given": ["Test"]}],
                             "gender": "female",
                             "birthDate": "1990-01-01"},
                       headers=CLINICIAN)
    assert resp.status_code == 201, resp.text


def _book(client, headers=VIEWER, patient="ux-in-1"):
    _patient(client, patient)
    resp = client.post("/continuum/clinic/book",
                       json={"patient": patient, "session": "am-clinic",
                             "date": TODAY},
                       headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["appointment"]["id"]


def _day(client, headers=VIEWER):
    resp = client.get("/continuum/clinic/day", params={"date": TODAY},
                      headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_front_desk_completes_register_book_checkin(client):
    """The whole F-1 journey on the front desk's own role: book,
    see the booking in the day's arrivals, check in, and watch it
    move to the rooming queue for the clinical team."""
    appt = _book(client, patient="ux-in-1")
    day = _day(client)
    assert appt in [a["appointment_id"] for a in day["arrivals"]]
    assert day["rooming_queue"] == []

    resp = client.post(f"/continuum/clinic/appointments/{appt}/check-in",
                       headers=VIEWER)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "arrived"
    assert resp.json()["patient"] == "Patient/ux-in-1"

    day = _day(client)
    assert appt not in [a["appointment_id"] for a in day["arrivals"]]
    assert appt in [e["appointment_id"] for e in day["rooming_queue"]]


def test_checkin_is_audited(client):
    appt = _book(client, patient="ux-in-2")
    client.post(f"/continuum/clinic/appointments/{appt}/check-in",
                headers=VIEWER)
    factory = client.app.state.session_factory
    with factory() as session:
        rows = session.scalars(select(AuditLog).where(
            AuditLog.action == "continuum.clinic.check-in")).all()
    assert len(rows) == 1
    assert rows[0].entity == f"Appointment/{appt}"
    assert rows[0].payload["patient"] == "Patient/ux-in-2"
    assert rows[0].payload["from_status"] == "booked"


def test_checkin_twice_is_409_and_unknown_is_404(client):
    appt = _book(client, patient="ux-in-3")
    first = client.post(f"/continuum/clinic/appointments/{appt}/check-in",
                        headers=VIEWER)
    assert first.status_code == 200
    again = client.post(f"/continuum/clinic/appointments/{appt}/check-in",
                        headers=VIEWER)
    assert again.status_code == 409, again.text
    missing = client.post(
        "/continuum/clinic/appointments/appt-nope/check-in",
        headers=VIEWER)
    assert missing.status_code == 404, missing.text


def test_rooming_stays_clinical_after_frontdesk_checkin(client):
    """The widening is exactly one step wide: the front desk checks
    in, but rooming the patient stays continuum.work (403)."""
    appt = _book(client, patient="ux-in-4")
    client.post(f"/continuum/clinic/appointments/{appt}/check-in",
                headers=VIEWER)
    room = client.post(
        f"/continuum/clinic/appointments/{appt}/room",
        json={"room": "Exam 3"}, headers=VIEWER)
    assert room.status_code == 403, room.text
    # The clinician rooms the checked-in patient as before.
    roomed = client.post(
        f"/continuum/clinic/appointments/{appt}/room",
        json={"room": "Exam 3"}, headers=CLINICIAN)
    assert roomed.status_code == 200, roomed.text


def test_checkin_gates_patient_role_and_accepts_clinician(client):
    appt = _book(client, patient="ux-in-5")
    refused = client.post(
        f"/continuum/clinic/appointments/{appt}/check-in",
        headers=PATIENT)
    assert refused.status_code == 403, refused.text
    ok = client.post(f"/continuum/clinic/appointments/{appt}/check-in",
                     headers=CLINICIAN)
    assert ok.status_code == 200, ok.text
    # Admin reads the day view too (continuum.read).
    assert _day(client, headers=ADMIN)["appointments_total"] >= 1
