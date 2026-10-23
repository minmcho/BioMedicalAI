"""Phase 15 — care coordination (BD-04/07/24/25/27).

eConsults (manual lifecycle + TACo-style trigger scan), multispecialty
episodes, coordinating-physician assignments + panels, the ICU module
(board over the inpatient census + governed flowsheets + handoffs),
and therapy scheduling with prior-authorisation visit enforcement.
"""
from datetime import datetime, timezone

from tests.conftest import ADMIN, CLINICIAN, VIEWER

TODAY = datetime.now(timezone.utc).date().isoformat()


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Coord", "given": ["Test"]}],
        "gender": "female", "birthDate": "1980-05-01",
    }, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _observation(client, pid, code, value):
    r = client.post("/fhir/Observation", json={
        "resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
        "subject": {"reference": f"Patient/{pid}"},
        "valueQuantity": {"value": value, "unit": "%"},
        "effectiveDateTime": datetime.now(timezone.utc).isoformat(),
    }, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _staff(client, staff_id, username, name="Coord Person, MD"):
    r = client.post("/people/staff", json={
        "staff_id": staff_id, "username": username, "full_name": name,
        "job_role": "physician", "unit": "unit-medsurg",
        "service_line": "medicine", "status": "active",
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _verify_staff_for_privileges(client, staff_id):
    """Wave A (WF-B1) fixture: attending assignment now requires the
    attending's licence to be primary-source verified with the
    demanded exclusion kinds clear — record that PSV for a SEEDED
    staff member (licence found via the credentials list)."""
    items = client.get("/people/credentials", headers=ADMIN).json()["items"]
    held = [i for i in items if i["staff_id"] == staff_id
            and i["type"] == "medical-licence"]
    assert held, f"seeded staff {staff_id} has no medical licence"
    latest = max(held, key=lambda i: i["expires_on"])
    resp = client.post(
        f"/people/credentials/{latest['credential_id']}/verifications",
        json={"source_code": "state-licence-board",
              "method": "direct-source-query", "outcome": "verified",
              "verified_on": TODAY, "evidence": "SYN-CONF-1"},
        headers=ADMIN)
    assert resp.status_code == 201, resp.text
    for kind in ("licence-board", "oig", "sam"):
        resp = client.post(
            f"/people/staff/{staff_id}/exclusion-checks",
            json={"kind": kind, "outcome": "clear",
                  "checked_on": TODAY, "evidence": "SYN-EV-1"},
            headers=ADMIN)
        assert resp.status_code == 201, resp.text


def _econsult(client, pid, specialty="endocrinology", requester="dr-request", **kw):
    body = {"patient": pid, "specialty": specialty,
            "question": "Please advise on management.", "requester": requester}
    body.update(kw)
    r = client.post("/coord/econsults", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# BD-27 — eConsults: manual lifecycle
# ---------------------------------------------------------------------------

def test_econsult_manual_lifecycle_answer_in_chart_and_referral(client):
    _patient(client, "ec-1")
    r = client.post("/coord/econsults", json={
        "patient": "ec-1", "specialty": "no-such-specialty",
        "question": "?"}, headers=CLINICIAN)
    assert r.status_code == 422 and "no-such-specialty" in r.json()["detail"]

    created = _econsult(client, "ec-1",
                          chart_refs=["Observation/obs-1"])
    assert created["state"] == "open"
    assert created["sla_hours"] == 24  # from terminology/econsult-specialties
    assert created["sla_breached"] is False
    eid = created["id"]

    queue = client.get("/coord/econsults/queue/specialty/endocrinology",
                       headers=CLINICIAN).json()
    assert [i["id"] for i in queue["items"]] == [eid]
    by_requester = client.get("/coord/econsults/queue/requester/dr-request",
                              headers=VIEWER).json()
    assert by_requester["total"] == 1

    answered = client.post(f"/coord/econsults/{eid}/answer", json={
        "advice": "Increase metformin; recheck in 3 months.",
        "recommendations": "Dietitian referral",
        "needs_visit": True}, headers=CLINICIAN)
    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert body["state"] == "answered"
    assert body["needs_visit"] is True
    assert body["referral_id"]
    # The unit of work completed IN CHART: no visit/appointment was made,
    # but needs_visit created a real draft ServiceRequest referral.
    referral = client.get(f"/fhir/ServiceRequest/{body['referral_id']}",
                          headers=CLINICIAN)
    assert referral.status_code == 200, referral.text
    ref = referral.json()
    assert ref["status"] == "draft"
    assert ref["intent"] == "proposal"
    assert ref["reasonCode"][0]["text"] == "eConsult follow-up"
    assert ref["category"][0]["coding"][0]["system"] == "urn:opencare:referral"
    assert ref["code"]["coding"][0]["code"] == "endocrinology"
    ext = next(e for e in ref["extension"]
               if e["url"] == "urn:opencare:extension/econsult")
    assert ext["valueString"] == eid

    closed = client.post(f"/coord/econsults/{eid}/close", headers=CLINICIAN)
    assert closed.status_code == 200 and closed.json()["state"] == "closed"
    again = client.post(f"/coord/econsults/{eid}/answer",
                        json={"advice": "late"}, headers=CLINICIAN)
    assert again.status_code == 409


def test_econsult_answer_without_visit_creates_no_referral_and_cancel(client):
    _patient(client, "ec-2")
    created = _econsult(client, "ec-2", specialty="dermatology")
    answered = client.post(f"/coord/econsults/{created['id']}/answer", json={
        "advice": "Topical treatment is sufficient.", "needs_visit": False},
        headers=CLINICIAN).json()
    assert answered["state"] == "answered"
    assert answered["referral_id"] is None

    other = _econsult(client, "ec-2", specialty="cardiology")
    r = client.post(f"/coord/econsults/{other['id']}/cancel",
                    json={}, headers=CLINICIAN)
    assert r.status_code == 422  # a reason is required
    cancelled = client.post(f"/coord/econsults/{other['id']}/cancel",
                            json={"reason": "Question resolved by phone"},
                            headers=CLINICIAN)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    assert cancelled.json()["cancel_reason"] == "Question resolved by phone"


# ---------------------------------------------------------------------------
# BD-27 — rule-triggered initiation (TACo pattern)
# ---------------------------------------------------------------------------

def test_econsult_scan_diabetes_trigger_creates_one_and_is_idempotent(client):
    _patient(client, "ec-high")
    obs_id = _observation(client, "ec-high", "4548-4", 9.5)  # > 9.0 threshold
    _patient(client, "ec-low")
    _observation(client, "ec-low", "4548-4", 7.0)  # below threshold
    _patient(client, "ec-none")  # no HbA1c at all

    r = client.post("/coord/econsults/scan", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["created_count"] == 1, result
    created = result["created"][0]
    assert created["patient"] == "Patient/ec-high"
    assert created["specialty"] == "diabetes"
    assert created["trigger_id"] == "diabetes-hba1c-high"
    # The triggering observation is cited in the record.
    assert created["triggering_ref"] == f"Observation/{obs_id}"
    assert f"Observation/{obs_id}" in created["chart_refs"]

    second = client.post("/coord/econsults/scan", headers=CLINICIAN).json()
    assert second["created_count"] == 0  # idempotent while one is open

    low = client.get("/coord/econsults", params={"patient": "ec-low"},
                     headers=CLINICIAN).json()
    assert low["total"] == 0  # below-threshold patient untouched


# ---------------------------------------------------------------------------
# BD-24 — coordinating physician
# ---------------------------------------------------------------------------

def test_coordinator_assignment_history_supersedes_never_edits(client):
    _patient(client, "coord-1")
    _staff(client, "coord-100", "dr-coord")
    _staff(client, "coord-101", "dr-coord-two", "Second Coord, MD")

    r = client.post("/coord/patients/coord-1/coordinator", json={
        "staff_username": "no-such-person", "role_label": "Coordinator"},
        headers=CLINICIAN)
    assert r.status_code == 422  # validated against the people directory

    first = client.post("/coord/patients/coord-1/coordinator", json={
        "staff_username": "dr-coord", "role_label": "Coordinating physician"},
        headers=CLINICIAN)
    assert first.status_code == 201, first.text
    assert first.json()["staff_id"] == "coord-100"

    second = client.post("/coord/patients/coord-1/coordinator", json={
        "staff_username": "dr-coord-two", "role_label": "Coordinating physician"},
        headers=CLINICIAN)
    assert second.status_code == 201, second.text

    current = client.get("/coord/patients/coord-1/coordinator",
                         headers=VIEWER).json()
    assert current["coordinator"]["staff_username"] == "dr-coord-two"
    assert current["coordinator"]["active"] is True
    assert len(current["history"]) == 2
    assert current["history"][0]["staff_username"] == "dr-coord"
    assert current["history"][0]["active"] is False  # superseded, not edited

    _patient(client, "coord-none")
    empty = client.get("/coord/patients/coord-none/coordinator",
                       headers=CLINICIAN).json()
    assert empty["coordinator"] is None


# ---------------------------------------------------------------------------
# BD-25 — multispecialty episodes
# ---------------------------------------------------------------------------

def test_episode_two_specialties_one_plan_assembly_and_links(client):
    _patient(client, "ep-1")
    r = client.post("/coord/episodes", json={
        "patient": "ep-1", "title": "Diabetes with cardiac disease",
        "condition": "Type 2 diabetes + coronary artery disease"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    episode = r.json()
    assert episode["status"] == "active"
    eid = episode["id"]

    for specialty, summary in (
            ("endocrinology", "Tighten glycaemic control; HbA1c target 7.5."),
            ("cardiology", "Continue statin; echo in 6 months.")):
        c = client.post(f"/coord/episodes/{eid}/contributions", json={
            "specialty": specialty, "summary": summary}, headers=CLINICIAN)
        assert c.status_code == 201, c.text

    # Typed link validation: an Encounter link must name an Encounter.
    enc = client.post("/fhir/Encounter", json={
        "resourceType": "Encounter", "status": "in-progress",
        "class": {"code": "AMB"},
        "subject": {"reference": "Patient/ep-1"}}, headers=CLINICIAN)
    assert enc.status_code == 201, enc.text
    bad = client.post(f"/coord/episodes/{eid}/links", json={
        "type": "note", "ref": f"Encounter/{enc.json()['id']}"},
        headers=CLINICIAN)
    assert bad.status_code == 422
    bad2 = client.post(f"/coord/episodes/{eid}/links", json={
        "type": "bogus", "ref": f"Encounter/{enc.json()['id']}"},
        headers=CLINICIAN)
    assert bad2.status_code == 422
    linked = client.post(f"/coord/episodes/{eid}/links", json={
        "type": "encounter", "ref": f"Encounter/{enc.json()['id']}"},
        headers=CLINICIAN)
    assert linked.status_code == 201, linked.text
    assert linked.json()["links"][0]["type"] == "encounter"

    assembly = client.get(f"/coord/episodes/{eid}/assembly",
                          headers=CLINICIAN).json()
    assert assembly["episode"]["id"] == eid
    grouped = assembly["contributions_by_specialty"]
    assert set(grouped) == {"endocrinology", "cardiology"}
    assert grouped["cardiology"][0]["summary"].startswith("Continue statin")
    assert len(assembly["links"]) == 1


def test_episode_inherits_patient_coordinator_and_panel_shows_it(client):
    _patient(client, "ep-2")
    _staff(client, "coord-200", "dr-panel")
    client.post("/coord/patients/ep-2/coordinator", json={
        "staff_username": "dr-panel", "role_label": "Coordinating physician"},
        headers=CLINICIAN)
    episode = client.post("/coord/episodes", json={
        "patient": "ep-2", "title": "Complex multimorbidity case"},
        headers=CLINICIAN).json()
    # BD-24 carry-through: no explicit coordinator -> inherits current.
    assert episode["coordinator_username"] == "dr-panel"
    assert episode["coordinator_staff_id"] == "coord-200"

    # Attending carried on the patient's active inpatient encounter.
    # WF-B1: the attending's licence must be PSV-verified first.
    _verify_staff_for_privileges(client, "staff-001")
    admitted = client.post("/inpatient/admissions", json={
        "patient": "ep-2", "bed": "MS-101A",
        "attending_staff_id": "staff-001"}, headers=CLINICIAN)
    assert admitted.status_code == 201, admitted.text

    panel = client.get("/coord/panels/dr-panel", headers=CLINICIAN).json()
    assert panel["total"] == 1
    item = panel["items"][0]
    assert item["patient"] == "Patient/ep-2"
    assert item["active_episode"]["id"] == episode["id"]
    assert item["attending_staff_id"] == "staff-001"
    assert item["attending_name"] == "Maya Chen, MD"


# ---------------------------------------------------------------------------
# BD-04 — ICU module
# ---------------------------------------------------------------------------

def _admit_icu(client, pid, bed="ICU-201"):
    _patient(client, pid)
    r = client.post("/inpatient/admissions", json={
        "patient": pid, "bed": bed}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _flowsheet(client, pid, form, answers, encounter_id=None):
    body = {"form": form, "patient": pid, "answers": answers}
    if encounter_id:
        body["encounter"] = {"reference": f"Encounter/{encounter_id}"}
    r = client.post("/flowsheets", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def test_icu_unit_type_in_published_locations_config(client):
    locations = client.get("/inpatient/locations", headers=CLINICIAN).json()
    units = {loc["code"]: loc for loc in locations["locations"]
             if loc["type"] == "unit"}
    assert "unit-icu" in units
    assert units["unit-icu"]["display"] == "Intensive Care Unit"
    icu_beds = [loc for loc in locations["locations"]
                if loc["type"] == "bed" and loc["parent"] == "unit-icu"]
    assert len(icu_beds) == 6


def test_icu_board_census_flowsheets_and_handoff(client):
    admitted = _admit_icu(client, "icu-1")
    encounter_id = admitted["encounter"]["id"]

    _flowsheet(client, "icu-1", "icu-acuity", [
        {"linkId": "acuity-score", "answer": [{"valueInteger": 14}]},
        {"linkId": "sofa-score", "answer": [{"valueInteger": 6}]},
    ], encounter_id)
    _flowsheet(client, "icu-1", "icu-devices", [
        {"linkId": "device-count", "answer": [{"valueInteger": 3}]},
        {"linkId": "central-line", "answer": [{"valueBoolean": True}]},
        {"linkId": "ett", "answer": [{"valueBoolean": True}]},
    ], encounter_id)

    handoff = client.post("/coord/icu/handoffs", json={
        "encounter_id": encounter_id,
        "answers": [
            {"linkId": "illness-severity", "answer": [{"valueString": "Unstable"}]},
            {"linkId": "patient-summary", "answer": [{"valueString": "Sepsis, ICU day 1"}]},
            {"linkId": "action-list", "answer": [{"valueString": "Repeat lactate"}]},
            {"linkId": "situation-awareness", "answer": [{"valueString": "Watch MAP"}]},
            {"linkId": "synthesis", "answer": [{"valueString": "Read back confirmed"}]},
        ]}, headers=CLINICIAN)
    assert handoff.status_code == 201, handoff.text
    assert handoff.json()["form"] == "icu-handoff"

    board = client.get("/coord/icu/board", headers=CLINICIAN).json()
    # Census comes from the inpatient machinery: ICU unit, 6 beds, 1 occupied.
    assert board["census"]["code"] == "unit-icu"
    assert board["census"]["beds_total"] == 6
    assert board["census"]["occupied"] == 1
    assert board["total"] == 1
    patient = board["patients"][0]
    assert patient["patient"] == "Patient/icu-1"
    assert patient["bed"] == "ICU-201"
    assert patient["latest_acuity"] == 14
    assert patient["device_count"] == 3
    assert patient["handoff_count"] == 1

    listed = client.get("/coord/icu/handoffs",
                        params={"encounter_id": encounter_id},
                        headers=CLINICIAN).json()
    assert listed["total"] == 1


# ---------------------------------------------------------------------------
# BD-07 — therapy scheduling
# ---------------------------------------------------------------------------

def _authorisation(client, pid, units, service="physical-therapy"):
    r = client.post("/access/authorisations", json={
        "patient": pid, "payer": "synthetic-payer",
        "service_code": service, "approved_units": units}, headers=ADMIN)
    assert r.status_code == 201, r.text
    auth = r.json()
    decided = client.post(f"/access/authorisations/{auth['id']}/transition",
                          json={"to_status": "approved",
                                "approved_units": units}, headers=CLINICIAN)
    assert decided.status_code == 200, decided.text
    return auth["id"]


def _plan(client, pid, starts, discipline="physical-therapy", therapist="therapist-pat"):
    r = client.post("/coord/therapy/plans", json={
        "patient": pid, "discipline": discipline,
        "frequency_pattern": "3x/week", "therapist": therapist,
        "session_starts": starts}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def test_therapy_authorisation_count_enforcement(client):
    _patient(client, "th-1")
    _authorisation(client, "th-1", units=2)
    starts = [f"2026-10-{day}T09:00:00+00:00" for day in (20, 21, 22)]
    plan = _plan(client, "th-1", starts)
    assert plan["state"] == "authorised"
    assert plan["authorised_visits"] == 2
    assert plan["session_count"] == 3  # booked; completion is what consumes
    sessions = plan["sessions"]
    # Each session is a real FHIR Appointment.
    appt = client.get(f"/fhir/Appointment/{sessions[0]['appointment_id']}",
                      headers=CLINICIAN)
    assert appt.status_code == 200 and appt.json()["status"] == "booked"

    first = client.post(f"/coord/therapy/sessions/{sessions[0]['id']}/complete",
                        headers=CLINICIAN)
    assert first.status_code == 200 and first.json()["auth_consumed"] is True
    # Cancelling the third session consumes nothing.
    cancelled = client.post(f"/coord/therapy/sessions/{sessions[2]['id']}/cancel",
                            headers=CLINICIAN)
    assert cancelled.status_code == 200
    assert cancelled.json()["auth_consumed"] is False
    second = client.post(f"/coord/therapy/sessions/{sessions[1]['id']}/complete",
                         headers=CLINICIAN)
    assert second.status_code == 200, second.text

    # The covering authorisation now shows both visits used.
    auths = client.get("/access/authorisations",
                       params={"patient": "th-1"}, headers=CLINICIAN).json()
    assert auths["items"][0]["used_units"] == 2

    # A further plan/session against the exhausted authorisation: the
    # plan finds no covering auth (units remaining = 0) -> no-authorisation.
    plan2 = _plan(client, "th-1", ["2026-10-23T09:00:00+00:00"])
    assert plan2["authorisation_status"] == "no-authorisation"
    assert plan2["session_count"] == 0


def test_therapy_completion_beyond_authorised_count_is_409(client):
    _patient(client, "th-2")
    _authorisation(client, "th-2", units=1)
    plan = _plan(client, "th-2",
                 ["2026-10-20T10:00:00+00:00", "2026-10-21T10:00:00+00:00"])
    s1, s2 = plan["sessions"]
    assert client.post(f"/coord/therapy/sessions/{s1['id']}/complete",
                       headers=CLINICIAN).status_code == 200
    over = client.post(f"/coord/therapy/sessions/{s2['id']}/complete",
                       headers=CLINICIAN)
    assert over.status_code == 409, over.text
    assert "1/1" in over.json()["detail"]  # the count is named


def test_therapy_plan_without_authorisation_books_nothing(client):
    _patient(client, "th-3")
    r = client.post("/coord/therapy/plans", json={
        "patient": "th-3", "discipline": "speech-therapy",
        "frequency_pattern": "2x/week", "therapist": "therapist-sam",
        "session_starts": ["2026-10-20T11:00:00+00:00"]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["state"] == "no-authorisation"
    assert plan["authorisation_status"] == "no-authorisation"
    assert plan["sessions"] == []
    assert plan["session_count"] == 0
    # Unknown discipline is a 422 against the published terminology.
    bad = client.post("/coord/therapy/plans", json={
        "patient": "th-3", "discipline": "music-therapy"}, headers=CLINICIAN)
    assert bad.status_code == 422


def test_therapy_calendar_by_therapist_and_date(client):
    _patient(client, "th-4")
    _authorisation(client, "th-4", units=5, service="occupational-therapy")
    plan = _plan(client, "th-4",
                 ["2026-10-20T08:00:00+00:00", "2026-10-21T08:00:00+00:00"],
                 discipline="occupational-therapy", therapist="therapist-olivia")
    assert plan["session_count"] == 2
    calendar = client.get("/coord/therapy/calendar", params={
        "therapist": "therapist-olivia", "date": "2026-10-20"},
        headers=CLINICIAN).json()
    assert calendar["total"] == 1
    assert calendar["items"][0]["discipline"] == "occupational-therapy"
    other_day = client.get("/coord/therapy/calendar", params={
        "therapist": "therapist-olivia", "date": "2026-10-22"},
        headers=CLINICIAN).json()
    assert other_day["total"] == 0
