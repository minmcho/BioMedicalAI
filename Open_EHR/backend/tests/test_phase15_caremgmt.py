"""Phase 15 — payer / care-management operations (app.care_management).

Programmes as published Studio config (terminology/care-programmes),
payer attribution validated against the Phase 7 published payers,
enrolment lifecycle with directory-validated care managers, caseloads,
the outreach cadence worklist (UTC math; logging outreach clears the
due state), on-demand FHIR Task escalation for overdue outreach, and
the care-gaps read-through that REUSES the Phase 12 continuum panel
gap engine (app.continuum._gap_items) filtered to programme members.
"""
import datetime as dt

from .conftest import ADMIN, CLINICIAN, PATIENT, VIEWER

MGR = "care-mgr-1"
MGR2 = "care-mgr-2"


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Test", "given": ["CM"]}],
        "birthDate": "1980-01-01", "gender": "female"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _staff(client, username, job_role="nurse", status="active"):
    r = client.post("/people/staff", json={
        "staff_id": f"staff-{username}", "username": username,
        "full_name": f"Care Manager {username}", "job_role": job_role,
        "status": status}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return username


def _enrol(client, pid, programme="diabetes-care", manager=MGR, **kw):
    body = {"patient": pid, "programme": programme, "care_manager": manager}
    body.update(kw)
    r = client.post("/care/enrolments", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _days_ago(days):
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(days=days)).isoformat()


def _condition(client, pid, code):
    r = client.post("/fhir/Condition", json={
        "resourceType": "Condition",
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10",
                             "code": code}]},
        "subject": {"reference": f"Patient/{pid}"},
        "clinicalStatus": {"coding": [{"code": "active"}]}},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _observation(client, pid, code, value):
    r = client.post("/fhir/Observation", json={
        "resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
        "subject": {"reference": f"Patient/{pid}"},
        "valueQuantity": {"value": value, "unit": "%"},
        "effectiveDateTime": dt.datetime.now(dt.timezone.utc).isoformat()},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Programmes: published config read-through
# ---------------------------------------------------------------------------

def test_programmes_seeded_published_config(client):
    r = client.get("/care/programmes", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["config_version"] == 1
    codes = {p["code"] for p in body["items"]}
    assert codes == {"diabetes-care", "heart-failure", "copd"}
    diabetes = next(p for p in body["items"] if p["code"] == "diabetes-care")
    assert diabetes["outreachCadenceDays"] == 30
    assert diabetes["careManagerRole"] == "nurse"
    assert "E11.9" in diabetes["enrolmentCriteria"]
    assert "phone" in body["outreach_channels"]

    r = client.get("/care/programmes/heart-failure", headers=VIEWER)
    assert r.status_code == 200, r.text
    assert r.json()["outreachCadenceDays"] == 14
    r = client.get("/care/programmes/nope", headers=CLINICIAN)
    assert r.status_code == 404
    # Patients hold neither coord capability.
    r = client.get("/care/programmes", headers=PATIENT)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Payer attribution
# ---------------------------------------------------------------------------

def test_attribution_create_and_list(client):
    _patient(client, "cm-attr-1")
    r = client.post("/care/attributions", json={
        "patient": "cm-attr-1", "payer": "synthetic-payer",
        "programme": "diabetes-care", "attributed_from": "2026-01-01"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["patient_ref"] == "Patient/cm-attr-1"
    assert body["payer"] == "synthetic-payer"
    assert body["programme"] == "diabetes-care"
    assert body["attributed_from"] == "2026-01-01"
    assert body["attributed_to"] is None

    listing = client.get("/care/attributions?programme=diabetes-care",
                         headers=CLINICIAN).json()
    assert listing["total"] == 1
    listing = client.get("/care/attributions?payer=synthetic-payer-plus",
                         headers=CLINICIAN).json()
    assert listing["total"] == 0


def test_attribution_validation(client):
    _patient(client, "cm-attr-2")
    # Unknown payer: not in the published Phase 7 terminology/payers.
    r = client.post("/care/attributions", json={
        "patient": "cm-attr-2", "payer": "acme-health",
        "programme": "diabetes-care"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # Unknown programme: not in the published care-programmes config.
    r = client.post("/care/attributions", json={
        "patient": "cm-attr-2", "payer": "synthetic-payer",
        "programme": "asthma-care"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # Unknown patient: 404 from the shared patient-ref machinery.
    r = client.post("/care/attributions", json={
        "patient": "cm-ghost", "payer": "synthetic-payer",
        "programme": "diabetes-care"}, headers=CLINICIAN)
    assert r.status_code == 404, r.text
    # attributed_to before attributed_from.
    r = client.post("/care/attributions", json={
        "patient": "cm-attr-2", "payer": "synthetic-payer",
        "programme": "copd", "attributed_from": "2026-06-01",
        "attributed_to": "2026-01-01"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # Duplicate attribution (same patient + payer + programme + from).
    ok = {"patient": "cm-attr-2", "payer": "synthetic-payer",
          "programme": "copd", "attributed_from": "2026-06-01"}
    assert client.post("/care/attributions", json=ok,
                       headers=CLINICIAN).status_code == 201
    r = client.post("/care/attributions", json=ok, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    # Viewer may read but not write (coord.work).
    r = client.post("/care/attributions", json={
        "patient": "cm-attr-2", "payer": "synthetic-payer-plus",
        "programme": "copd"}, headers=VIEWER)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Enrolment + caseload
# ---------------------------------------------------------------------------

def test_enrolment_and_caseload(client):
    _staff(client, MGR)
    _staff(client, MGR2)
    _patient(client, "cm-enr-1")
    _patient(client, "cm-enr-2")
    first = _enrol(client, "cm-enr-1")
    assert first["status"] == "enrolled"
    assert first["care_manager"] == MGR
    assert first["programme"] == "diabetes-care"
    _enrol(client, "cm-enr-2", programme="heart-failure")
    _enrol(client, "cm-enr-2", programme="copd", manager=MGR2)

    caseload = client.get(f"/care/caseload/{MGR}", headers=CLINICIAN).json()
    assert caseload["total"] == 2
    assert caseload["by_programme"] == {"diabetes-care": 1, "heart-failure": 1}
    assert {i["patient_ref"] for i in caseload["items"]} == \
        {"Patient/cm-enr-1", "Patient/cm-enr-2"}
    other = client.get(f"/care/caseload/{MGR2}", headers=CLINICIAN).json()
    assert other["total"] == 1
    assert other["items"][0]["programme"] == "copd"
    # Unknown username: 404, not an empty caseload.
    r = client.get("/care/caseload/no-such-person", headers=CLINICIAN)
    assert r.status_code == 404


def test_enrolment_validation(client):
    _staff(client, MGR)
    _staff(client, "care-mgr-leave", status="leave")
    _patient(client, "cm-enr-3")
    # Unknown programme.
    r = client.post("/care/enrolments", json={
        "patient": "cm-enr-3", "programme": "asthma-care",
        "care_manager": MGR}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # Care manager not in the people directory.
    r = client.post("/care/enrolments", json={
        "patient": "cm-enr-3", "programme": "copd",
        "care_manager": "ghost-manager"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    assert "people" in r.json()["detail"] or "directory" in r.json()["detail"]
    # Care manager in the directory but not active.
    r = client.post("/care/enrolments", json={
        "patient": "cm-enr-3", "programme": "copd",
        "care_manager": "care-mgr-leave"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # Unknown patient.
    r = client.post("/care/enrolments", json={
        "patient": "cm-ghost", "programme": "copd",
        "care_manager": MGR}, headers=CLINICIAN)
    assert r.status_code == 404, r.text
    # Duplicate live enrolment -> 409.
    _enrol(client, "cm-enr-3", programme="copd")
    r = client.post("/care/enrolments", json={
        "patient": "cm-enr-3", "programme": "copd",
        "care_manager": MGR}, headers=CLINICIAN)
    assert r.status_code == 409, r.text


def test_enrolment_lifecycle_discharge_requires_reason(client):
    _staff(client, MGR)
    _patient(client, "cm-enr-4")
    enr = _enrol(client, "cm-enr-4")
    eid = enr["enrolment_id"]
    # enrolled -> active.
    r = client.post(f"/care/enrolments/{eid}/transition",
                    json={"status": "active"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    # Backwards is not a transition.
    r = client.post(f"/care/enrolments/{eid}/transition",
                    json={"status": "enrolled"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # Discharge without a reason is refused.
    r = client.post(f"/care/enrolments/{eid}/transition",
                    json={"status": "discharged"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    assert "reason" in r.json()["detail"]
    # Discharge with a reason lands, stamped.
    r = client.post(f"/care/enrolments/{eid}/transition",
                    json={"status": "discharged",
                          "reason": "goals met — stable on oral therapy"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "discharged"
    assert body["discharge_reason"].startswith("goals met")
    assert body["discharged_at"] is not None
    # Discharged is terminal, leaves the caseload, and frees the
    # patient for a fresh enrolment in the same programme.
    r = client.post(f"/care/enrolments/{eid}/transition",
                    json={"status": "active"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    caseload = client.get(f"/care/caseload/{MGR}", headers=CLINICIAN).json()
    assert caseload["total"] == 0
    again = _enrol(client, "cm-enr-4")
    assert again["enrolment_id"] != eid


# ---------------------------------------------------------------------------
# Outreach log, due worklist, escalation
# ---------------------------------------------------------------------------

def test_outreach_log_validation(client):
    _staff(client, MGR)
    _patient(client, "cm-out-1")
    enr = _enrol(client, "cm-out-1")
    eid = enr["enrolment_id"]
    r = client.post(f"/care/enrolments/{eid}/outreach", json={
        "channel": "phone", "outcome": "reached",
        "note": "Reviewed glucose log."}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["channel"] == "phone"
    log = client.get(f"/care/enrolments/{eid}/outreach",
                     headers=CLINICIAN).json()
    assert log["total"] == 1
    assert log["items"][0]["note"] == "Reviewed glucose log."
    # Channel / outcome are published-config value sets.
    r = client.post(f"/care/enrolments/{eid}/outreach", json={
        "channel": "smoke-signal", "outcome": "reached"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post(f"/care/enrolments/{eid}/outreach", json={
        "channel": "phone", "outcome": "teleported"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # No outreach on a discharged enrolment.
    client.post(f"/care/enrolments/{eid}/transition",
                json={"status": "discharged", "reason": "moved away"},
                headers=CLINICIAN)
    r = client.post(f"/care/enrolments/{eid}/outreach", json={
        "channel": "phone", "outcome": "reached"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text


def test_outreach_due_and_logging_clears_it(client):
    _staff(client, MGR)
    _patient(client, "cm-due-1")
    _patient(client, "cm-due-2")
    # Enrolled 40 days ago (diabetes cadence 30d): due.
    old = _enrol(client, "cm-due-1", enrolled_at=_days_ago(40))
    # Enrolled today: not due.
    _enrol(client, "cm-due-2")
    due = client.get("/care/outreach/due", headers=CLINICIAN).json()
    assert [i["enrolment_id"] for i in due["items"]] == [old["enrolment_id"]]
    entry = due["items"][0]
    assert entry["cadence_days"] == 30
    assert entry["days_since_contact"] >= 40
    assert entry["days_overdue"] >= 10
    # Logging outreach (now) clears the due state.
    r = client.post(f"/care/enrolments/{old['enrolment_id']}/outreach",
                    json={"channel": "phone", "outcome": "reached"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    due = client.get("/care/outreach/due", headers=CLINICIAN).json()
    assert due["total"] == 0


def test_outreach_due_cadence_per_programme_and_ordering(client):
    _staff(client, MGR)
    for pid in ("cm-due-3", "cm-due-4", "cm-due-5"):
        _patient(client, pid)
    # Heart-failure cadence is 14d: 20 days old is due even though it
    # would be inside the diabetes cadence.
    hf = _enrol(client, "cm-due-3", programme="heart-failure",
                enrolled_at=_days_ago(20))
    # Diabetes: 90 days with no outreach (oldest), 45 days (second).
    oldest = _enrol(client, "cm-due-4", enrolled_at=_days_ago(90))
    newer = _enrol(client, "cm-due-5", enrolled_at=_days_ago(45))
    due = client.get("/care/outreach/due", headers=CLINICIAN).json()
    assert [i["enrolment_id"] for i in due["items"]] == [
        oldest["enrolment_id"], newer["enrolment_id"], hf["enrolment_id"]]
    # A recent outreach re-bases the clock even on an old enrolment:
    # outreached 5 days ago, the 90-day enrolment leaves the list.
    client.post(f"/care/enrolments/{oldest['enrolment_id']}/outreach",
                json={"channel": "message", "outcome": "left-message",
                      "at": _days_ago(5)}, headers=CLINICIAN)
    due = client.get("/care/outreach/due", headers=CLINICIAN).json()
    assert oldest["enrolment_id"] not in \
        [i["enrolment_id"] for i in due["items"]]
    # The care_manager filter scopes the worklist.
    due = client.get("/care/outreach/due?care_manager=care-mgr-2",
                     headers=CLINICIAN).json()
    assert due["total"] == 0


def test_outreach_cadence_is_config_driven(client):
    """Publishing a new care-programmes version with a shorter
    diabetes cadence makes a 10-day-old enrolment due — the cadence
    is config, not code (configure, don't fork)."""
    _staff(client, MGR)
    _patient(client, "cm-due-6")
    _enrol(client, "cm-due-6", enrolled_at=_days_ago(10))
    due = client.get("/care/outreach/due", headers=CLINICIAN).json()
    assert due["total"] == 0  # inside the seeded 30d cadence

    from app.care_management import default_programmes_payload
    payload = default_programmes_payload()
    for concept in payload["concepts"]:
        if concept["code"] == "diabetes-care":
            concept["outreachCadenceDays"] = 7
    r = client.post("/studio/config/terminology/care-programmes",
                    json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/terminology/care-programmes/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text

    due = client.get("/care/outreach/due", headers=CLINICIAN).json()
    assert due["total"] == 1
    assert due["items"][0]["cadence_days"] == 7


def test_escalate_overdue_raises_task_for_care_manager(client):
    _staff(client, MGR)
    _patient(client, "cm-esc-1")
    _patient(client, "cm-esc-2")
    overdue = _enrol(client, "cm-esc-1", enrolled_at=_days_ago(45))
    fresh = _enrol(client, "cm-esc-2")
    # Not overdue -> refused.
    r = client.post("/care/outreach/escalate",
                    json={"enrolment_id": fresh["enrolment_id"]},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    # Unknown enrolment -> 404.
    r = client.post("/care/outreach/escalate",
                    json={"enrolment_id": "enr-nope"}, headers=CLINICIAN)
    assert r.status_code == 404, r.text
    # Overdue -> a FHIR Task for the patient, owned by the care manager.
    r = client.post("/care/outreach/escalate",
                    json={"enrolment_id": overdue["enrolment_id"]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["existing"] is False
    task = body["task"]
    assert task["resourceType"] == "Task"
    assert task["status"] == "requested"
    assert task["for"]["reference"] == "Patient/cm-esc-1"
    assert task["owner"]["display"] == MGR
    assert "diabetes-care" in task["description"]
    # Second escalation returns the same open Task, no duplicate.
    r = client.post("/care/outreach/escalate",
                    json={"enrolment_id": overdue["enrolment_id"]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["existing"] is True
    assert r.json()["task"]["id"] == task["id"]


# ---------------------------------------------------------------------------
# Care gaps read-through (continuum engine, programme members only)
# ---------------------------------------------------------------------------

def test_programme_gaps_reuse_continuum_engine(client):
    _staff(client, MGR)
    _patient(client, "cm-gap-1")
    _condition(client, "cm-gap-1", "E11.9")  # diabetic, no HbA1c: gap
    _patient(client, "cm-gap-2")
    _condition(client, "cm-gap-2", "E11.9")
    _observation(client, "cm-gap-2", "4548-4", 7.2)  # in range: no gap
    _patient(client, "cm-gap-3")
    _condition(client, "cm-gap-3", "E11.9")  # diabetic but NOT enrolled
    _enrol(client, "cm-gap-1")
    _enrol(client, "cm-gap-2")

    r = client.get("/care/programmes/diabetes-care/gaps", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2  # members only — cm-gap-3 is excluded
    assert body["gap_codes"] == ["diabetes-a1c-control"]
    assert "continuum" in body["gaps_basis"]
    by_patient = {m["patient_ref"]: m for m in body["members"]}
    gap_member = by_patient["Patient/cm-gap-1"]
    assert gap_member["open_gaps"] == 1
    assert gap_member["gaps"][0]["gap_code"] == "diabetes-a1c-control"
    assert gap_member["gaps"][0]["reason"] == "missing"
    assert by_patient["Patient/cm-gap-2"]["open_gaps"] == 0
    assert body["open_gaps"] == 1
    assert body["members_with_open_gaps"] == 1
    # Cross-check against the continuum panel surface itself: the
    # same engine sees all three diabetics, this surface only members.
    panel = client.get("/continuum/panels/gaps", headers=CLINICIAN).json()
    panel_refs = {i["patient_ref"] for i in panel["items"]
                  if i["gap_code"] == "diabetes-a1c-control"}
    assert panel_refs == {"Patient/cm-gap-1", "Patient/cm-gap-3"}


def test_programme_gaps_no_continuum_definition_returns_membership(client):
    _staff(client, MGR)
    _patient(client, "cm-gap-4")
    _condition(client, "cm-gap-4", "I50.9")
    _enrol(client, "cm-gap-4", programme="heart-failure")
    r = client.get("/care/programmes/heart-failure/gaps", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    # No continuum care-gap definition measures I50.9: membership is
    # returned, gaps are honestly empty with an explicit note.
    assert body["total"] == 1
    assert body["gap_codes"] == []
    assert body["members"][0]["gaps"] == []
    assert body["gaps_note"] is not None
    assert "I50.9" in body["gaps_note"]
    r = client.get("/care/programmes/nope/gaps", headers=CLINICIAN)
    assert r.status_code == 404
