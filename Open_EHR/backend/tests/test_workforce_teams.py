"""Workforce, Teams & Panels pass — care teams, privilege gates,
patient assignment + handoff, coverage, empanelment and panels
(app.privileging + app.teams).

The institutional operating model, proven end to end: a physician
without the attending grant is REFUSED team-attending membership
with the missing privilege named (and the grant then fixes it); an
expired credential refuses even a granted privilege; a second
primary assignment is a handoff that supersedes atomically on
accept; coverage merges the covered team's patients into the
covering team's panel, flagged via_coverage; empanelment is
PCP-gated and supersede-never-edit; panel weighting is exact
deterministic arithmetic over the published config, and capacity
flips when the thresholds are republished through Studio.

The module-local `client` fixture mounts app.teams exactly as the
coordinator's main.py wiring does (router + ensure_teams_defaults)
and skips both steps when main already wired them.

Synthetic data only.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import teams as teams_module
from app.main import create_app

from .conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
    if not any(getattr(r, "path", "") == "/teams/config"
               for r in app.routes):
        app.include_router(teams_module.router)
        with app.state.session_factory() as session:
            teams_module.ensure_teams_defaults(session)
    with TestClient(app) as c:
        yield c


def _publish(client, kind, key, payload, headers=ADMIN):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload,
                    headers=headers)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/{kind}/{key}/versions/{version}/publish",
        headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _patient(client, pid, birth_date="1986-06-15"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic", "given": ["Ana"]}],
        "gender": "female", "birthDate": birth_date}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return pid


def _condition(client, pid, code):
    r = client.post("/fhir/Condition", json={
        "resourceType": "Condition",
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10",
                             "code": code}]},
        "subject": {"reference": f"Patient/{pid}"},
        "clinicalStatus": {"coding": [{"code": "active"}]}},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _record_psv(client, staff_id, credential_id):
    """Wave A (WF-B1) fixture: record a current primary-source
    verification + clear demanded exclusion checks for a staff
    member, so a privilege demand judges their credentials as
    verified — check_privileges now requires the verified state,
    not a self-asserted credential row."""
    today = datetime.now(timezone.utc).date().isoformat()
    r = client.post(
        f"/people/credentials/{credential_id}/verifications", json={
            "source_code": "state-licence-board",
            "method": "direct-source-query", "outcome": "verified",
            "verified_on": today, "evidence": "SYN-CONF-1"},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    for kind in ("licence-board", "oig", "sam"):
        r = client.post(f"/people/staff/{staff_id}/exclusion-checks",
                        json={"kind": kind, "outcome": "clear",
                              "checked_on": today,
                              "evidence": "SYN-EV-1"}, headers=ADMIN)
        assert r.status_code == 201, r.text


def _physician(client, staff_id, licence_days_from_today):
    r = client.post("/people/staff", json={
        "staff_id": staff_id, "full_name": f"Dr {staff_id}",
        "job_role": "physician", "unit": "clinic",
        "service_line": "medicine"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    today = datetime.now(timezone.utc).date()
    expires = today + timedelta(days=licence_days_from_today)
    r = client.post("/people/credentials", json={
        "credential_id": f"cred-{staff_id}", "staff_id": staff_id,
        "type": "medical-licence", "number": f"SYN-{staff_id}",
        "issued_on": (expires - timedelta(days=730)).isoformat(),
        "expires_on": expires.isoformat()}, headers=ADMIN)
    assert r.status_code == 201, r.text
    # WF-B1: the licence is primary-source verified (the expired-
    # licence test still refuses on expiry, which is judged first).
    _record_psv(client, staff_id, f"cred-{staff_id}")
    return expires.isoformat()


def _team(client, team_id, team_type="unit", unit="clinic",
          service_line="medicine", headers=ADMIN):
    r = client.post("/teams/", json={
        "team_id": team_id, "name": f"Team {team_id}",
        "team_type": team_type, "unit": unit,
        "service_line": service_line}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _admit(client, pid, bed):
    r = client.post("/inpatient/admissions",
                    json={"patient": pid, "bed": bed}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["encounter"]["id"]


def _assign(client, patient_ref, team_id, role="primary", headers=ADMIN,
            **extra):
    r = client.post("/teams/assignments", json={
        "patient_ref": patient_ref, "team_id": team_id, "role": role,
        **extra}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _accept(client, assignment_id, headers=ADMIN):
    r = client.post(f"/teams/assignments/{assignment_id}/accept",
                    json={}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _ensure_pcp_verified(client, pcp="staff-015"):
    """WF-B1 fixture: the SEEDED PCP's medical licence must carry a
    recorded PSV + clear demanded exclusion checks before any
    empanelment can pass check_privileges. Finds the licence through
    the credentials list (latest expiry of the type) rather than a
    hard-coded seed credential id."""
    items = client.get("/people/credentials", headers=ADMIN).json()["items"]
    held = [i for i in items if i["staff_id"] == pcp
            and i["type"] == "medical-licence"]
    assert held, "seeded PCP has no medical licence"
    latest = max(held, key=lambda i: i["expires_on"])
    _record_psv(client, pcp, latest["credential_id"])


def _empanel(client, patient_ref, pcp="staff-015",
             team_id="team-clinic-pc", headers=ADMIN):
    _ensure_pcp_verified(client, pcp)
    r = client.post("/teams/empanelment", json={
        "patient_ref": patient_ref, "pcp_staff_id": pcp,
        "team_id": team_id}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


NOW = datetime.now(timezone.utc)
WINDOW = {"starts_at": (NOW - timedelta(hours=1)).isoformat(),
          "ends_at": (NOW + timedelta(hours=5)).isoformat()}


# ---------------------------------------------------------------------------
# Config surface + seeds
# ---------------------------------------------------------------------------

def test_config_surface_and_capability_gates(client):
    r = client.get("/teams/config", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    type_codes = {t["code"] for t in body["teams"]["teamTypes"]}
    assert {"unit", "service-line", "consult-service",
            "primary-care"} <= type_codes
    priv_codes = {p["code"] for p in body["privileges"]["privileges"]}
    assert priv_codes == {"attending-of-record", "icu-practice",
                          "primary-care-panel",
                          "prescribe-controlled-substances"}
    assert body["privileges"]["serviceRequirements"] == {
        "critical-care": ["icu-practice"]}
    roles = {r["jobRole"] for r in body["jobCatalogue"]["roles"]}
    assert len(roles) == 11 and "front-desk" in roles
    physician = next(r for r in body["jobCatalogue"]["roles"]
                     if r["jobRole"] == "physician")
    assert physician["privilegeTemplate"] == ["attending-of-record"]
    assert physician["requiredCredentials"] == ["medical-licence"]
    # Gates: patients see nothing; viewers may read but not work.
    assert client.get("/teams/config", headers=PATIENT).status_code == 403
    assert client.get("/teams/", headers=VIEWER).status_code == 200
    assert client.post("/teams/", json={
        "name": "Nope", "team_type": "unit"},
        headers=VIEWER).status_code == 403
    assert client.post("/teams/privileges", json={
        "staff_id": "staff-001", "privilege_code": "icu-practice"},
        headers=VIEWER).status_code == 403


def test_seeded_teams_memberships_and_grants(client):
    r = client.get("/teams/", headers=ADMIN)
    assert r.status_code == 200
    by_id = {t["team_id"]: t for t in r.json()["items"]}
    assert {"team-medsurg-a", "team-icu", "team-ed", "team-clinic-pc",
            "team-cardiology-consult"} <= set(by_id)
    assert by_id["team-medsurg-a"]["member_count"] == 4
    assert by_id["team-icu"]["service_line"] == "critical-care"
    assert by_id["team-clinic-pc"]["team_type"] == "primary-care"
    detail = client.get("/teams/team-icu", headers=CLINICIAN).json()
    members = {m["staff_id"]: m for m in detail["members"]}
    assert members["staff-002"]["role_in_team"] == "attending"
    assert members["staff-002"]["staff_name"] == "James Okafor, MD"
    grants = client.get("/teams/privileges?staff_id=staff-002",
                        headers=ADMIN).json()
    by_code = {g["privilege_code"]: g for g in grants["items"]}
    assert set(by_code) == {"attending-of-record", "icu-practice"}
    assert by_code["icu-practice"]["service_line"] == "critical-care"
    assert by_code["attending-of-record"]["granted_by"] == "system"


# ---------------------------------------------------------------------------
# Team + membership validation
# ---------------------------------------------------------------------------

def test_team_create_validation(client):
    created = _team(client, "team-x1", headers=CLINICIAN)
    assert created["status"] == "active"
    r = client.post("/teams/", json={
        "name": "Bad type", "team_type": "squad"}, headers=ADMIN)
    assert r.status_code == 422 and "team type" in r.json()["detail"]
    r = client.post("/teams/", json={
        "name": "Bad unit", "team_type": "unit", "unit": "unit-moon"},
        headers=ADMIN)
    assert r.status_code == 422 and "unit-moon" in r.json()["detail"]
    r = client.post("/teams/", json={
        "team_id": "team-x1", "name": "Dup", "team_type": "unit"},
        headers=ADMIN)
    assert r.status_code == 409


def test_member_add_end_and_readd(client):
    _team(client, "team-x2")
    r = client.post("/teams/team-x2/members", json={
        "staff_id": "staff-003", "role_in_team": "rn"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    membership_id = r.json()["membership_id"]
    assert client.post("/teams/team-x2/members", json={
        "staff_id": "staff-003", "role_in_team": "rn"},
        headers=ADMIN).status_code == 409  # duplicate active membership
    assert client.post("/teams/team-x2/members", json={
        "staff_id": "staff-005", "role_in_team": "wizard"},
        headers=ADMIN).status_code == 422
    assert client.post("/teams/team-x2/members", json={
        "staff_id": "staff-999", "role_in_team": "rn"},
        headers=ADMIN).status_code == 404
    # staff-016 is on leave: not active, cannot join.
    assert client.post("/teams/team-x2/members", json={
        "staff_id": "staff-016", "role_in_team": "rn"},
        headers=ADMIN).status_code == 409
    r = client.post(f"/teams/team-x2/members/{membership_id}/end",
                    json={"reason": "rotated off"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "ended"
    assert r.json()["ended_reason"] == "rotated off"
    # After ending, the same person can join again.
    r = client.post("/teams/team-x2/members", json={
        "staff_id": "staff-003", "role_in_team": "rn"}, headers=ADMIN)
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# The privilege gate
# ---------------------------------------------------------------------------

def test_attending_membership_refused_without_grant_then_granted(client):
    _physician(client, "staff-900", licence_days_from_today=365)
    _team(client, "team-x3")
    r = client.post("/teams/team-x3/members", json={
        "staff_id": "staff-900", "role_in_team": "attending"},
        headers=ADMIN)
    assert r.status_code == 409, r.text
    assert "missing privilege: Attending of record (attending-of-record)" \
        in r.json()["detail"]
    # Grant the privilege through the admin endpoint: membership passes.
    r = client.post("/teams/privileges", json={
        "staff_id": "staff-900", "privilege_code": "attending-of-record"},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/teams/team-x3/members", json={
        "staff_id": "staff-900", "role_in_team": "attending"},
        headers=ADMIN)
    assert r.status_code == 201, r.text


def test_attending_membership_refused_for_expired_credential(client):
    expires = _physician(client, "staff-901", licence_days_from_today=-5)
    r = client.post("/teams/privileges", json={
        "staff_id": "staff-901", "privilege_code": "attending-of-record"},
        headers=ADMIN)
    assert r.status_code == 201, r.text  # the grant itself is recorded
    _team(client, "team-x4")
    r = client.post("/teams/team-x4/members", json={
        "staff_id": "staff-901", "role_in_team": "attending"},
        headers=ADMIN)
    assert r.status_code == 409, r.text
    # The grant does not save an expired licence: the refusal names it.
    assert f"medical-licence expired on {expires}" in r.json()["detail"]


def test_icu_attending_requires_service_line_privilege(client):
    # staff-001 holds attending-of-record but NOT icu-practice; the
    # ICU team's service line (critical-care) demands both.
    r = client.post("/teams/team-icu/members", json={
        "staff_id": "staff-001", "role_in_team": "attending"},
        headers=ADMIN)
    assert r.status_code == 409, r.text
    assert "missing privilege: ICU practice (icu-practice)" \
        in r.json()["detail"]


def test_privilege_grant_validation_and_revoke_restores_refusal(client):
    assert client.post("/teams/privileges", json={
        "staff_id": "staff-001", "privilege_code": "time-travel"},
        headers=ADMIN).status_code == 422
    _physician(client, "staff-902", licence_days_from_today=365)
    _team(client, "team-x5")
    grant = client.post("/teams/privileges", json={
        "staff_id": "staff-902", "privilege_code": "attending-of-record"},
        headers=ADMIN).json()
    member = client.post("/teams/team-x5/members", json={
        "staff_id": "staff-902", "role_in_team": "attending"},
        headers=ADMIN)
    assert member.status_code == 201, member.text
    client.post(
        f"/teams/team-x5/members/{member.json()['membership_id']}/end",
        json={}, headers=ADMIN)
    r = client.post(f"/teams/privileges/{grant['grant_id']}/revoke",
                    json={"reason": "appraisal: lapsed supervision"},
                    headers=ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert client.post(f"/teams/privileges/{grant['grant_id']}/revoke",
                       json={}, headers=ADMIN).status_code == 409
    # Revoked grant no longer satisfies the gate.
    r = client.post("/teams/team-x5/members", json={
        "staff_id": "staff-902", "role_in_team": "attending"},
        headers=ADMIN)
    assert r.status_code == 409
    assert "missing privilege" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Assignments: lifecycle, consulting, handoff, unassigned queue
# ---------------------------------------------------------------------------

def test_assignment_lifecycle_decline_and_end(client):
    _patient(client, "wt-pat-1")
    first = _assign(client, "Patient/wt-pat-1", "team-medsurg-a")
    assert first["state"] == "requested" and first["handoff"] is False
    # A second primary request while one is undecided is a conflict.
    assert client.post("/teams/assignments", json={
        "patient_ref": "Patient/wt-pat-1", "team_id": "team-icu",
        "role": "primary"}, headers=ADMIN).status_code == 409
    r = client.post(f"/teams/assignments/{first['assignment_id']}/decline",
                    json={"reason": "at capacity"}, headers=ADMIN)
    assert r.json()["state"] == "declined"
    second = _assign(client, "Patient/wt-pat-1", "team-medsurg-a")
    accepted = _accept(client, second["assignment_id"])
    assert accepted["state"] == "active" and accepted["decided_by"]
    # Accepting again is a conflict; ending works exactly once.
    assert client.post(
        f"/teams/assignments/{second['assignment_id']}/accept",
        json={}, headers=ADMIN).status_code == 409
    r = client.post(f"/teams/assignments/{second['assignment_id']}/end",
                    json={"reason": "discharged"}, headers=ADMIN)
    assert r.json()["state"] == "ended"
    assert client.post(
        f"/teams/assignments/{second['assignment_id']}/end",
        json={}, headers=ADMIN).status_code == 409


def test_assignment_unknown_patient_and_consulting_duplicate(client):
    r = client.post("/teams/assignments", json={
        "patient_ref": "Patient/nope-1", "team_id": "team-medsurg-a",
        "role": "primary"}, headers=ADMIN)
    assert r.status_code == 422 and "Unknown patient" in r.json()["detail"]
    _patient(client, "wt-pat-2")
    consult = _assign(client, "Patient/wt-pat-2",
                      "team-cardiology-consult", role="consulting")
    _accept(client, consult["assignment_id"])
    assert client.post("/teams/assignments", json={
        "patient_ref": "Patient/wt-pat-2",
        "team_id": "team-cardiology-consult", "role": "consulting"},
        headers=ADMIN).status_code == 409
    # A different team may still consult on the same patient.
    other = _assign(client, "Patient/wt-pat-2", "team-icu",
                    role="consulting")
    assert other["state"] == "requested"


def test_handoff_supersedes_on_accept(client):
    _patient(client, "wt-pat-3")
    first = _accept(client, _assign(
        client, "Patient/wt-pat-3", "team-medsurg-a")["assignment_id"])
    handoff = _assign(client, "Patient/wt-pat-3", "team-icu")
    assert handoff["handoff"] is True
    assert handoff["supersedes_assignment_id"] == first["assignment_id"]
    _accept(client, handoff["assignment_id"])
    rows = client.get("/teams/assignments?patient_ref=Patient/wt-pat-3",
                      headers=ADMIN).json()["items"]
    by_id = {r["assignment_id"]: r for r in rows}
    assert by_id[first["assignment_id"]]["state"] == "ended"
    assert by_id[first["assignment_id"]]["note"] == \
        "superseded by handoff to team-icu"
    assert by_id[handoff["assignment_id"]]["state"] == "active"


def test_unassigned_queue_shrinks_after_assignment_accept(client):
    _patient(client, "wt-pat-4")
    encounter_id = _admit(client, "wt-pat-4", "MS-101A")
    queue = client.get("/teams/unassigned", headers=CLINICIAN).json()
    mine = [i for i in queue["items"]
            if i["encounter_id"] == encounter_id]
    assert len(mine) == 1
    assert mine[0]["unit"] == "unit-medsurg"
    assert mine[0]["bed"] == "MS-101A"
    assert mine[0]["patient_name"] == "Ana Synthetic"
    scoped = client.get("/teams/unassigned?unit=unit-icu",
                        headers=CLINICIAN).json()
    assert all(i["encounter_id"] != encounter_id for i in scoped["items"])
    assignment = _assign(client, "Patient/wt-pat-4", "team-medsurg-a",
                         encounter_id=encounter_id)
    # Merely requested does not clear the queue; accepted does.
    queue = client.get("/teams/unassigned", headers=CLINICIAN).json()
    assert any(i["encounter_id"] == encounter_id for i in queue["items"])
    _accept(client, assignment["assignment_id"])
    queue = client.get("/teams/unassigned", headers=CLINICIAN).json()
    assert all(i["encounter_id"] != encounter_id for i in queue["items"])


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

def test_coverage_validation_and_cancel(client):
    base = {"covered_staff_id": "staff-001",
            "covering_staff_id": "staff-002", **WINDOW}
    both = dict(base, covered_team_id="team-icu")
    assert client.post("/teams/coverage", json=both,
                       headers=ADMIN).status_code == 422
    none_covering = {"covered_staff_id": "staff-001", **WINDOW}
    assert client.post("/teams/coverage", json=none_covering,
                       headers=ADMIN).status_code == 422
    backwards = dict(base, ends_at=WINDOW["starts_at"])
    assert client.post("/teams/coverage", json=backwards,
                       headers=ADMIN).status_code == 422
    unknown = dict(base, covered_staff_id="staff-999")
    assert client.post("/teams/coverage", json=unknown,
                       headers=ADMIN).status_code == 422
    r = client.post("/teams/coverage", json=base, headers=ADMIN)
    assert r.status_code == 201, r.text
    coverage_id = r.json()["coverage_id"]
    current = client.get("/teams/coverage?current=true",
                         headers=CLINICIAN).json()
    assert any(c["coverage_id"] == coverage_id for c in current["items"])
    by_staff = client.get("/teams/coverage?staff_id=staff-002",
                          headers=CLINICIAN).json()
    assert any(c["coverage_id"] == coverage_id for c in by_staff["items"])
    r = client.post(f"/teams/coverage/{coverage_id}/cancel",
                    json={"reason": "no longer needed"}, headers=ADMIN)
    assert r.json()["status"] == "cancelled"
    assert client.post(f"/teams/coverage/{coverage_id}/cancel",
                       json={}, headers=ADMIN).status_code == 409
    current = client.get("/teams/coverage?current=true",
                         headers=CLINICIAN).json()
    assert all(c["coverage_id"] != coverage_id for c in current["items"])


def test_team_panel_merges_covered_patients_via_coverage(client):
    _patient(client, "wt-pat-5")
    encounter_id = _admit(client, "wt-pat-5", "MS-102A")
    assignment = _assign(client, "Patient/wt-pat-5", "team-medsurg-a",
                         encounter_id=encounter_id)
    _accept(client, assignment["assignment_id"])
    r = client.post("/teams/coverage", json={
        "covered_team_id": "team-medsurg-a",
        "covering_team_id": "team-icu", **WINDOW}, headers=ADMIN)
    assert r.status_code == 201, r.text
    coverage_id = r.json()["coverage_id"]
    panel = client.get("/teams/team-icu/panel", headers=CLINICIAN).json()
    covered = [p for p in panel["patients"]
               if p["patient_ref"] == "Patient/wt-pat-5"]
    assert len(covered) == 1
    assert covered[0]["via_coverage"] == coverage_id
    assert covered[0]["coverage"] is True
    # The ICU gives this coverage: it is the ICU panel's coverage_out.
    assert any(c["coverage_id"] == coverage_id
               for c in panel["coverage_out"])
    own = client.get("/teams/team-medsurg-a/panel",
                     headers=CLINICIAN).json()
    mine = [p for p in own["patients"]
            if p["patient_ref"] == "Patient/wt-pat-5"]
    assert len(mine) == 1
    assert mine[0]["bed"] == "MS-102A"
    # Med-Surg is the covered side (coverage_in), and its covered
    # patient is flagged as covered via the delegation.
    assert mine[0]["via_coverage"] == coverage_id
    assert mine[0]["coverage"] is True
    assert any(c["coverage_id"] == coverage_id
               for c in own["coverage_in"])


def test_team_panel_pending_inbound_and_unassigned_scope(client):
    _patient(client, "wt-pat-6")
    encounter_id = _admit(client, "wt-pat-6", "MS-103A")
    pending = _assign(client, "Patient/wt-pat-6", "team-medsurg-a",
                      encounter_id=encounter_id)
    panel = client.get("/teams/team-medsurg-a/panel",
                       headers=CLINICIAN).json()
    inbound = [p for p in panel["pending_inbound"]
               if p["assignment_id"] == pending["assignment_id"]]
    assert len(inbound) == 1 and inbound[0]["handoff"] is False
    assert any(u["encounter_id"] == encounter_id
               for u in panel["unassigned"])
    members = {m["staff_id"] for m in panel["members"]}
    assert members == {"staff-001", "staff-003", "staff-018", "staff-009"}
    _accept(client, pending["assignment_id"])
    panel = client.get("/teams/team-medsurg-a/panel",
                       headers=CLINICIAN).json()
    assert panel["pending_inbound"] == []
    assert any(p["patient_ref"] == "Patient/wt-pat-6"
               for p in panel["todays_assignments"])


# ---------------------------------------------------------------------------
# Empanelment + panels
# ---------------------------------------------------------------------------

def test_empanelment_pcp_gate_and_supersede(client):
    _patient(client, "wt-pat-7")
    # staff-001 has no primary-care-panel grant: refused, named.
    r = client.post("/teams/empanelment", json={
        "patient_ref": "Patient/wt-pat-7", "pcp_staff_id": "staff-001",
        "team_id": "team-clinic-pc"}, headers=ADMIN)
    assert r.status_code == 409, r.text
    assert "missing privilege: Primary care panel (primary-care-panel)" \
        in r.json()["detail"]
    first = _empanel(client, "Patient/wt-pat-7")
    assert first["status"] == "active"
    second = _empanel(client, "Patient/wt-pat-7")
    assert second["empanelment_id"] != first["empanelment_id"]
    rows = client.get("/teams/empanelment?patient_ref=Patient/wt-pat-7",
                      headers=ADMIN).json()["items"]
    by_id = {r["empanelment_id"]: r for r in rows}
    assert by_id[first["empanelment_id"]]["status"] == "ended"
    assert by_id[first["empanelment_id"]]["ended_reason"] == "superseded"
    assert by_id[second["empanelment_id"]]["status"] == "active"
    r = client.post(f"/teams/empanelment/{second['empanelment_id']}/end",
                    json={"reason": "patient moved"}, headers=ADMIN)
    assert r.json()["status"] == "ended"
    assert r.json()["ended_reason"] == "patient moved"


def test_empanelment_requires_primary_care_team(client):
    _patient(client, "wt-pat-8")
    r = client.post("/teams/empanelment", json={
        "patient_ref": "Patient/wt-pat-8", "pcp_staff_id": "staff-015",
        "team_id": "team-medsurg-a"}, headers=ADMIN)
    assert r.status_code == 422, r.text
    assert "primary-care" in r.json()["detail"]


def test_panels_weighted_math_exact(client):
    _patient(client, "wt-pat-9", birth_date="1986-06-15")  # age band 1.0
    _condition(client, "wt-pat-9", "E11.9")
    _condition(client, "wt-pat-9", "I10")
    _empanel(client, "Patient/wt-pat-9")
    detail = client.get("/teams/panels/staff-015", headers=CLINICIAN).json()
    assert detail["panel_size"] == 1
    # 1.0 base x 1.0 band + 2 conditions x 0.25 = 1.5, no frailty.
    assert detail["weighted_size"] == 1.5
    assert detail["capacity"] == "under"  # below minWeighted 2.0
    entry = detail["patients"][0]
    assert entry["weight"] == 1.5
    assert entry["basis"]["conditions"] == 2
    assert entry["basis"]["condition_weight"] == 0.5
    assert entry["basis"]["age_band_weight"] == 1.0
    assert entry["basis"]["frailty"] is False
    assert detail["team"]["team_id"] == "team-clinic-pc"
    overview = client.get("/teams/panels", headers=CLINICIAN).json()
    mine = [p for p in overview["panels"]
            if p["pcp_staff_id"] == "staff-015"]
    assert len(mine) == 1 and mine[0]["weighted_size"] == 1.5
    assert overview["coverage"] == {"patients_total": 1, "empanelled": 1,
                                    "unempanelled": 0,
                                    "coverage_pct": 100.0}
    assert overview["unempanelled_patient_refs"] == []


def test_panels_capacity_flips_with_republished_thresholds(client):
    # Age 90: band weight 1.8 + frailty uplift 0.75 = 2.55.
    _patient(client, "wt-pat-10", birth_date="1936-01-01")
    _empanel(client, "Patient/wt-pat-10")
    overview = client.get("/teams/panels", headers=ADMIN).json()
    mine = next(p for p in overview["panels"]
                if p["pcp_staff_id"] == "staff-015")
    assert mine["weighted_size"] == 2.55
    assert mine["capacity"] == "within"
    detail = client.get("/teams/panels/staff-015", headers=ADMIN).json()
    assert detail["patients"][0]["basis"]["frailty"] is True
    assert detail["patients"][0]["basis"]["frailty_weight"] == 0.75
    # Republish the teams config with a lower ceiling through Studio:
    # capacity is config, so the same panel now reads over.
    payload = client.get("/teams/config", headers=ADMIN).json()["teams"]
    payload["panelThresholds"] = {"maxWeighted": 2.0, "minWeighted": 1.0}
    _publish(client, "terminology", "teams", payload)
    overview = client.get("/teams/panels", headers=ADMIN).json()
    mine = next(p for p in overview["panels"]
                if p["pcp_staff_id"] == "staff-015")
    assert mine["capacity"] == "over"
    rebalance = client.get("/teams/panels/rebalance",
                           headers=ADMIN).json()
    assert rebalance["basis"] == ("suggestions only — a move is an "
                                  "explicit audited empanelment action")
    suggestion = next(s for s in rebalance["suggestions"]
                      if s["pcp_staff_id"] == "staff-015")
    assert suggestion["excess"] == 0.55
    assert suggestion["candidate_patient_refs"] == ["Patient/wt-pat-10"]
    assert suggestion["receiving_pcp_staff_ids"] == []


def test_panel_detail_care_gaps_filtered_to_panel(client):
    _patient(client, "wt-pat-11")
    _condition(client, "wt-pat-11", "E11.9")  # diabetes gap: no A1c
    _patient(client, "wt-pat-12")
    _condition(client, "wt-pat-12", "E11.9")  # same gap, NOT empanelled
    _empanel(client, "Patient/wt-pat-11")
    detail = client.get("/teams/panels/staff-015", headers=ADMIN).json()
    gap_refs = {g["patient_ref"] for g in detail["care_gaps"]}
    assert gap_refs == {"Patient/wt-pat-11"}
    assert detail["care_gaps"][0]["gap_code"] == "diabetes-a1c-control"


# ---------------------------------------------------------------------------
# My teams
# ---------------------------------------------------------------------------

def test_my_teams_for_dev_clinician(client):
    r = client.get("/teams/my-teams", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["staff_id"] == "staff-001"  # dev-clinician's directory row
    by_id = {t["team_id"]: t for t in body["teams"]}
    assert by_id["team-medsurg-a"]["role_in_team"] == "attending"
    assert by_id["team-cardiology-consult"]["role_in_team"] == "member"
    assert body["panel"] is None  # staff-001 is not a PCP
    assert body["coverage_giving"] == []
    assert body["coverage_receiving"] == []
    # The configurator's directory row has no memberships: honest empty.
    body = client.get("/teams/my-teams", headers=CONFIG).json()
    assert body["staff_id"] == "staff-010" and body["teams"] == []
    assert client.get("/teams/my-teams", headers=PATIENT).status_code == 403


def test_my_teams_panel_and_coverage(client):
    _patient(client, "wt-pat-13")
    _empanel(client, "Patient/wt-pat-13")
    r = client.post("/teams/coverage", json={
        "covered_staff_id": "staff-015",
        "covering_staff_id": "staff-001", **WINDOW}, headers=ADMIN)
    assert r.status_code == 201, r.text
    # The covering clinician (staff-001) sees the delegation as giving.
    body = client.get("/teams/my-teams", headers=CLINICIAN).json()
    assert len(body["coverage_giving"]) == 1
    assert body["coverage_giving"][0]["covered_staff_id"] == "staff-015"
    assert body["coverage_receiving"] == []
