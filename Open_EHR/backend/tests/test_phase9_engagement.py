"""Phase 9 — Part B: secure messaging, telehealth, self-scheduling.

Invariants under test:
- Messages are FHIR Communications in per-patient threads; a patient
  sees and writes ONLY their own threads (another patient's thread is
  a 404, not a 403 — non-disclosure), unread counts move with replies
  and read receipts, and staff access is capability-gated.
- Telehealth: the visit workspace exists only for appointments typed
  `telehealth` in the published slot-type config; the join URL is
  rendered from the org's configured provider template (data, not a
  video stack); the pre-visit intake is the published form; the
  telehealth note type is authorable through the notes machinery.
- Self-scheduling: requests create proposed appointments by default;
  publishing the config flag flips them to auto-confirmed (booked) —
  no code change; unknown slot types are refused; patients see only
  their own appointments.
"""
from tests.conftest import ADMIN, CLINICIAN, PATIENT, VIEWER


def _publish(client, kind, key, payload):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synth", "given": ["Engage"]}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _telehealth_appointment(client, pid, when="2026-11-02T15:00:00Z"):
    body = {
        "resourceType": "Appointment", "status": "booked",
        "appointmentType": {"coding": [
            {"system": "urn:opencare:slot-types", "code": "telehealth"}]},
        "start": when, "end": "2026-11-02T15:30:00Z",
        "participant": [{"actor": {"reference": f"Patient/{pid}"},
                         "status": "accepted"}],
    }
    r = client.post("/fhir/Appointment", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _portal_unread(client):
    mine = client.get("/portal/my-messages", headers=PATIENT).json()
    return sum(t["unread_count"] for t in mine["items"])


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------

def test_messaging_thread_round_trip(client):
    _patient(client, "pat-1")
    # Patient opens a thread from the portal.
    r = client.post("/portal/messages",
                    json={"text": "Question about my lab"}, headers=PATIENT)
    assert r.status_code == 201, r.text
    thread_id = r.json()["threadId"]
    assert r.json()["senderRole"] == "patient"

    # Staff inbox (per patient) shows the thread with one unread.
    inbox = client.get("/messages/inbox", params={"patient": "pat-1"},
                       headers=CLINICIAN).json()
    assert inbox["total"] == 1
    assert inbox["items"][0]["thread_id"] == thread_id
    assert inbox["items"][0]["unread_count"] == 1
    assert inbox["items"][0]["last_text"] == "Question about my lab"

    # Staff replies; the patient now has one unread.
    r = client.post(f"/messages/threads/{thread_id}/reply",
                    json={"text": "Your result is ready"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert _portal_unread(client) == 1
    detail = client.get(f"/messages/threads/{thread_id}",
                        headers=PATIENT).json()
    assert len(detail["messages"]) == 2
    assert detail["messages"][1]["senderRole"] == "clinician"
    assert detail["messages"][1]["text"] == "Your result is ready"

    # Read receipt clears the patient's unread count.
    r = client.post(f"/messages/threads/{thread_id}/read", headers=PATIENT)
    assert r.status_code == 200
    assert _portal_unread(client) == 0

    # Read receipts are explicit: replying did not mark the patient's
    # message read for staff — the staff unread count is still 1 until
    # the clinician opens (reads) the thread.
    unread = client.get("/messages/unread", headers=CLINICIAN).json()
    assert unread["unread"] == 1
    r = client.post(f"/messages/threads/{thread_id}/read", headers=CLINICIAN)
    assert r.status_code == 200
    unread = client.get("/messages/unread", headers=CLINICIAN).json()
    assert unread["unread"] == 0
    # Staff starting a thread does not make their own message unread.
    r = client.post("/messages", json={"patient": "pat-1",
                                       "text": "Care-plan update"},
                    headers=CLINICIAN)
    assert r.status_code == 201
    unread = client.get("/messages/unread", headers=CLINICIAN).json()
    assert unread["unread"] == 0


def test_messaging_patient_scoping(client):
    _patient(client, "pat-1")
    _patient(client, "pat-2")
    # Staff opens a thread on pat-2's record.
    r = client.post("/messages", json={"patient": "pat-2",
                                       "text": "For pat-2 only"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    other_thread = r.json()["threadId"]

    # The dev patient (linked to pat-1) cannot see it AT ALL: 404 on
    # read and reply, and it is absent from their message list.
    assert client.get(f"/messages/threads/{other_thread}",
                      headers=PATIENT).status_code == 404
    assert client.post(f"/messages/threads/{other_thread}/reply",
                       json={"text": "intrusion"},
                       headers=PATIENT).status_code == 404
    assert client.post(f"/messages/threads/{other_thread}/read",
                       headers=PATIENT).status_code == 404
    mine = client.get("/portal/my-messages", headers=PATIENT).json()
    assert mine["total"] == 0

    # Capability gates: viewers cannot use staff messaging; the
    # portal endpoints refuse staff tokens.
    assert client.get("/messages/inbox", params={"patient": "pat-1"},
                      headers=VIEWER).status_code == 403
    assert client.post("/messages", json={"patient": "pat-1", "text": "x"},
                       headers=VIEWER).status_code == 403
    assert client.get("/portal/my-messages",
                      headers=CLINICIAN).status_code == 403
    # Validation: empty text.
    assert client.post("/portal/messages", json={"text": "  "},
                       headers=PATIENT).status_code == 422


# ---------------------------------------------------------------------------
# Telehealth
# ---------------------------------------------------------------------------

WS = "/portal/telehealth/appointments/{}/workspace"
WS_INTAKE = "/portal/telehealth/appointments/{}/intake"
WS_STAFF = "/engagement/telehealth/appointments/{}/workspace"


def test_telehealth_workspace_and_intake(client):
    _patient(client, "pat-1")
    appt = _telehealth_appointment(client, "pat-1")

    ws = client.get(WS.format(appt), headers=PATIENT)
    assert ws.status_code == 200, ws.text
    body = ws.json()
    # Join details are CONFIG DATA: the URL renders the published
    # provider template with the appointment id substituted.
    assert body["join_url"].endswith(f"/{appt}")
    assert body["provider_name"]
    assert "does not provide video" in body["disclaimer"]
    assert body["intake_submitted"] is False
    assert body["intake_form"] == "telehealth-intake"
    assert body["note_type"] == "telehealth"

    # The intake is the published form: a missing required answer 422s.
    r = client.post(WS_INTAKE.format(appt), json={"answers": []},
                    headers=PATIENT)
    assert r.status_code == 422
    answers = [
        {"linkId": "chief-concern",
         "answer": [{"valueString": "Follow-up on blood pressure"}]},
        {"linkId": "technology-check", "answer": [{"valueBoolean": True}]},
    ]
    r = client.post(WS_INTAKE.format(appt), json={"answers": answers},
                    headers=PATIENT)
    assert r.status_code == 201, r.text
    ws = client.get(WS.format(appt), headers=PATIENT).json()
    assert ws["intake_submitted"] is True

    # Staff see the same workspace (with the intake status).
    staff = client.get(WS_STAFF.format(appt), headers=CLINICIAN)
    assert staff.status_code == 200
    assert staff.json()["intake_submitted"] is True

    # The telehealth NOTE type is real: a note can be authored on it
    # through the ordinary notes machinery.
    note_answers = [
        {"linkId": "presenting-concern",
         "answer": [{"valueString": "Blood pressure follow-up"}]},
        {"linkId": "assessment", "answer": [{"valueString": "Stable"}]},
        {"linkId": "plan", "answer": [{"valueString": "Continue meds"}]},
    ]
    r = client.post("/notes", json={
        "note_type": "telehealth", "patient": "pat-1",
        "answers": note_answers}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def test_telehealth_workspace_refusals(client):
    _patient(client, "pat-1")
    _patient(client, "pat-2")
    # A non-telehealth appointment has no telehealth workspace.
    body = {
        "resourceType": "Appointment", "status": "booked",
        "appointmentType": {"coding": [
            {"system": "urn:opencare:slot-types", "code": "follow-up"}]},
        "start": "2026-11-03T10:00:00Z", "end": "2026-11-03T10:30:00Z",
        "participant": [{"actor": {"reference": "Patient/pat-1"},
                         "status": "accepted"}],
    }
    r = client.post("/fhir/Appointment", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    in_person = r.json()["id"]
    assert client.get(WS.format(in_person),
                      headers=PATIENT).status_code == 422
    # Another patient's telehealth appointment: 404 for the portal.
    other = _telehealth_appointment(client, "pat-2")
    assert client.get(WS.format(other), headers=PATIENT).status_code == 404
    # The published config is visible to staff for the Studio record.
    cfg = client.get("/engagement/telehealth/config",
                     headers=CLINICIAN).json()
    assert cfg["config"]["provider"]["joinUrlTemplate"]


# ---------------------------------------------------------------------------
# Self-scheduling
# ---------------------------------------------------------------------------

def test_self_scheduling_default_staff_confirm(client):
    _patient(client, "pat-1")
    cfg = client.get("/portal/self-scheduling/config",
                     headers=PATIENT).json()
    assert cfg["auto_confirm"] is False
    assert "follow-up" in cfg["slot_types"]
    assert "telehealth" in cfg["slot_types"]

    r = client.post("/portal/appointments/request", json={
        "slot_type": "follow-up", "start": "2026-11-05T09:00:00Z"},
        headers=PATIENT)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "proposed"  # staff-confirm is default
    assert r.json()["auto_confirmed"] is False
    appt_id = r.json()["appointment"]["id"]

    # The patient sees it in their own list; a staff confirm books it
    # through the ordinary appointment flow.
    mine = client.get("/portal/my-appointments", headers=PATIENT).json()
    assert [a["id"] for a in mine["items"]] == [appt_id]
    r = client.post(f"/fhir/Appointment/{appt_id}/transition",
                    json={"to_status": "booked"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    mine = client.get("/portal/my-appointments", headers=PATIENT).json()
    assert mine["items"][0]["status"] == "booked"


def test_self_scheduling_auto_confirm_config_flip(client):
    _patient(client, "pat-1")
    _publish(client, "integration", "self-scheduling", {
        "autoConfirm": True,
        "note": "Front desk auto-confirms portal requests.",
    })
    r = client.post("/portal/appointments/request", json={
        "slot_type": "telehealth", "start": "2026-11-06T09:00:00Z"},
        headers=PATIENT)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "booked"
    assert r.json()["auto_confirmed"] is True
    # The booked telehealth appointment has a workspace immediately.
    assert client.get(WS.format(r.json()["appointment"]["id"]),
                      headers=PATIENT).status_code == 200


def test_self_scheduling_validation_and_scoping(client):
    _patient(client, "pat-1")
    _patient(client, "pat-2")
    # Unknown published slot type -> 422, naming the configured set.
    r = client.post("/portal/appointments/request", json={
        "slot_type": "walk-in", "start": "2026-11-05T09:00:00Z"},
        headers=PATIENT)
    assert r.status_code == 422
    assert "slot type" in r.json()["detail"]
    # Staff/viewer roles cannot use the portal endpoint.
    assert client.post("/portal/appointments/request", json={
        "slot_type": "follow-up", "start": "2026-11-05T09:00:00Z"},
        headers=VIEWER).status_code == 403
    # Another patient's appointments never leak into the list.
    _telehealth_appointment(client, "pat-2")
    mine = client.get("/portal/my-appointments", headers=PATIENT).json()
    assert mine["total"] == 0
