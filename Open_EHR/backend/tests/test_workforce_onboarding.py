"""Workforce, Teams & Panels pass — scope A: onboarding/offboarding
(app.onboarding).

The joiner pipeline over the existing stores: create (hr-profile)
lands at credentialing with the StaffMember inactive; credentialing
is gated on the job catalogue's required credentials in the EXISTING
StaffCredential store; privileging grants the role template through
app.privileging; the account stage provisions a real UserAccount
whose dev token is returned once and actually authenticates;
orientation is gated on the published form/orientation-checklist;
passing it activates the directory entry. Termination disables the
account (its token dies), ends team memberships and ends
empanelments naming the person as PCP.

The fixture wires the onboarding router + seeds into a fresh app
itself when the coordinator's main.py wiring is not present, so the
suite runs standalone either way (both are idempotent).

Synthetic data only.
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import onboarding
from app.main import create_app
from app.models import AuditLog, Empanelment, TeamMembership

from .conftest import ADMIN, CLINICIAN, CONFIG, VIEWER

FUTURE = (date.today() + timedelta(days=365)).isoformat()
PAST = (date.today() - timedelta(days=30)).isoformat()


@pytest.fixture()
def ob_client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
    paths = {getattr(route, "path", "") for route in app.routes}
    if "/onboarding" not in paths and "/onboarding/" not in paths:
        app.include_router(onboarding.router)
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            onboarding.ensure_onboarding_defaults(session)
        client.app_ref = app
        yield client


# Organisation placement (Org Placement pass): onboarding create now
# REQUIRES a full region -> location -> department -> unit placement.
# The department follows the unit the create carries.
_UNIT_DEPARTMENT = {"unit-medsurg": "dept-medicine",
                    "unit-icu": "dept-critical-care",
                    "ed": "dept-emergency",
                    "clinic": "dept-ambulatory"}


def _create(client, headers=ADMIN, **overrides):
    body = {"full_name": "Harriet Jones", "job_role": "physician",
            "unit": "unit-medsurg", "service_line": "medicine"}
    body.update(overrides)
    body.setdefault("region", "region-main")
    body.setdefault("location", "facility-main")
    if "department" not in overrides:
        body["department"] = _UNIT_DEPARTMENT.get(
            body.get("unit"), "dept-medicine")
    r = client.post("/onboarding", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _advance(client, onboarding_id, headers=ADMIN):
    return client.post(f"/onboarding/{onboarding_id}/advance",
                       headers=headers)


def _advance_ok(client, onboarding_id, headers=ADMIN):
    r = _advance(client, onboarding_id, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _add_credential(client, onboarding_id, cred_type="medical-licence",
                    expires_on=FUTURE, number="SYN-MD-9001"):
    r = client.post(f"/onboarding/{onboarding_id}/credentials",
                    json={"type": cred_type, "number": number,
                          "issued_on": "2026-01-01",
                          "expires_on": expires_on}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _complete_all_orientation(client, onboarding_id):
    detail = client.get(f"/onboarding/{onboarding_id}",
                        headers=ADMIN).json()
    for item in detail["orientation"]["items"]:
        if not item["completed"]:
            r = client.post(
                f"/onboarding/{onboarding_id}/orientation/"
                f"{item['linkId']}/complete", json={}, headers=ADMIN)
            assert r.status_code == 200, r.text


def _drive_to_account_provisioned(client, onboarding_id):
    """credentialing -> privileging -> account -> orientation."""
    _advance_ok(client, onboarding_id)
    _advance_ok(client, onboarding_id)
    return _advance_ok(client, onboarding_id)


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------

def test_seeded_records_for_existing_staff(ob_client):
    r = ob_client.get("/onboarding", headers=ADMIN)
    assert r.status_code == 200, r.text
    by_staff = {i["staff_id"]: i for i in r.json()["items"]}
    assert by_staff["staff-001"]["stage"] == "active"
    assert by_staff["staff-001"]["employee_id"] == "EMP-1001"
    assert by_staff["staff-019"]["stage"] == "orientation"
    detail = ob_client.get(
        f"/onboarding/{by_staff['staff-001']['onboarding_id']}",
        headers=ADMIN).json()
    assert detail["account"]["username"] == "dev-clinician"
    assert detail["account"]["status"] == "active"
    assert all(i["completed"] for i in detail["orientation"]["items"])
    assert len(detail["orientation"]["items"]) == 8
    assert detail["blockers"] == []
    detail19 = ob_client.get(
        f"/onboarding/{by_staff['staff-019']['onboarding_id']}",
        headers=ADMIN).json()
    done = {i["linkId"] for i in detail19["orientation"]["items"]
            if i["completed"]}
    assert done == {"verify-identity", "hipaa-training"}
    # No new StaffMember rows: the seeded directory total is untouched.
    assert ob_client.get("/people/staff", headers=ADMIN).json()["total"] == 19


def test_orientation_form_seeded_published(ob_client):
    r = ob_client.get("/studio/config/form/effective/orientation-checklist",
                      headers=ADMIN)
    assert r.status_code == 200, r.text
    items = r.json()["payload"]["item"]
    assert [i["linkId"] for i in items] == [
        "verify-identity", "verify-credentials", "hipaa-training",
        "safety-training", "system-access", "department-orientation",
        "equipment-competency", "policy-attestation"]
    assert all(i["type"] == "boolean" and i["required"] is True
               for i in items)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_physician_full_happy_path(ob_client):
    detail = _create(ob_client, supervisor_staff_id="staff-001",
                     employee_id="EMP-2001", fte=0.8,
                     start_date=date.today().isoformat())
    record = detail["record"]
    assert record["stage"] == "credentialing"
    assert record["supervisor_staff_id"] == "staff-001"
    assert record["employee_id"] == "EMP-2001"
    staff_id = record["staff_id"]
    assert detail["staff"]["status"] == "inactive"
    assert detail["staff"]["username"] == "hjones"
    assert detail["account"] is None
    # Credentialing gate: the missing licence is named as a blocker.
    assert detail["blockers"] == ["missing credential: medical-licence"]
    r = _advance(ob_client, record["onboarding_id"])
    assert r.status_code == 409
    assert "medical-licence" in r.json()["detail"]
    # Add the credential through the onboarding endpoint.
    cred = _add_credential(ob_client, record["onboarding_id"])
    assert cred["type"] == "medical-licence"
    assert cred["staff_id"] == staff_id
    detail = _advance_ok(ob_client, record["onboarding_id"])
    assert detail["record"]["stage"] == "privileging"
    # Privileging advance grants the template via app.privileging.
    detail = _advance_ok(ob_client, record["onboarding_id"])
    assert detail["record"]["stage"] == "account"
    codes = {p["privilege_code"] for p in detail["privileges"]}
    assert "attending-of-record" in codes
    grant = [p for p in detail["privileges"]
             if p["privilege_code"] == "attending-of-record"][0]
    assert grant["grant_id"] and grant["granted_by"] == "dev-admin"
    # Account stage: provisioned, dev token returned ONCE.
    detail = _advance_ok(ob_client, record["onboarding_id"])
    assert detail["record"]["stage"] == "orientation"
    assert detail["account"]["username"] == "hjones"
    assert detail["account"]["role"] == "clinician"
    token = detail["dev_token"]
    assert token.startswith("acct-hjones-")
    assert "shown once" in detail["dev_token_note"]
    # The token actually authenticates (account role clinician).
    r = ob_client.get("/people/staff",
                      headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    staff = ob_client.get(f"/people/staff/{staff_id}",
                          headers=ADMIN).json()
    assert staff["username"] == "hjones"
    assert staff["status"] == "inactive"  # not active until orientation
    # Orientation gate names the incomplete items.
    r = _advance(ob_client, record["onboarding_id"])
    assert r.status_code == 409
    assert "orientation item incomplete" in r.json()["detail"]
    _complete_all_orientation(ob_client, record["onboarding_id"])
    detail = _advance_ok(ob_client, record["onboarding_id"])
    assert detail["record"]["stage"] == "active"
    assert detail["blockers"] == []
    staff = ob_client.get(f"/people/staff/{staff_id}",
                          headers=ADMIN).json()
    assert staff["status"] == "active"
    # From active there is nowhere to advance.
    assert _advance(ob_client, record["onboarding_id"]).status_code == 409


def test_front_desk_nonclinical_path(ob_client):
    detail = _create(ob_client, full_name="Fred Desk",
                     job_role="front-desk", unit="clinic",
                     service_line="medicine")
    record = detail["record"]
    # No credentials required, template empty: no blockers anywhere.
    assert detail["blockers"] == []
    detail = _drive_to_account_provisioned(ob_client,
                                           record["onboarding_id"])
    assert detail["record"]["stage"] == "orientation"
    assert detail["privileges"] == []
    assert detail["account"]["role"] == "viewer"
    assert detail["dev_token"].startswith("acct-fdesk-")
    _complete_all_orientation(ob_client, record["onboarding_id"])
    detail = _advance_ok(ob_client, record["onboarding_id"])
    assert detail["record"]["stage"] == "active"
    staff = ob_client.get(f"/people/staff/{record['staff_id']}",
                          headers=ADMIN).json()
    assert staff["status"] == "active"


def test_generated_username_slug_collisions(ob_client):
    first = _create(ob_client, full_name="Sam Lee", job_role="nurse",
                    unit="unit-icu", service_line="critical-care")
    second = _create(ob_client, full_name="Sam Lee", job_role="nurse",
                     unit="unit-icu", service_line="critical-care")
    assert first["staff"]["username"] == "slee"
    assert second["staff"]["username"] == "slee-2"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_invalid_job_role_and_assignment_422(ob_client):
    r = ob_client.post("/onboarding", json={
        "full_name": "Bad Role", "job_role": "wizard"}, headers=ADMIN)
    assert r.status_code == 422
    assert "wizard" in r.json()["detail"]
    r = ob_client.post("/onboarding", json={
        "full_name": "Bad Unit", "job_role": "nurse", "unit": "moon"},
        headers=ADMIN)
    assert r.status_code == 422
    r = ob_client.post("/onboarding", json={
        "full_name": "Bad Line", "job_role": "nurse",
        "service_line": "astrology"}, headers=ADMIN)
    assert r.status_code == 422


def test_unknown_supervisor_422(ob_client):
    r = ob_client.post("/onboarding", json={
        "full_name": "No Boss", "job_role": "nurse",
        "supervisor_staff_id": "staff-999"}, headers=ADMIN)
    assert r.status_code == 422
    assert "staff-999" in r.json()["detail"]


def test_expired_credential_blocks_advance(ob_client):
    detail = _create(ob_client, full_name="Expired Erin")
    record = detail["record"]
    _add_credential(ob_client, record["onboarding_id"], expires_on=PAST)
    blockers = ob_client.get(f"/onboarding/{record['onboarding_id']}",
                             headers=ADMIN).json()["blockers"]
    assert blockers == [f"medical-licence expired on {PAST}"]
    r = _advance(ob_client, record["onboarding_id"])
    assert r.status_code == 409
    assert "expired" in r.json()["detail"]
    # A renewal (later expiry) supersedes the lapsed credential.
    _add_credential(ob_client, record["onboarding_id"],
                    number="SYN-MD-9002")
    assert _advance_ok(ob_client,
                       record["onboarding_id"])["record"]["stage"] \
        == "privileging"


def test_credential_endpoint_validation(ob_client):
    detail = _create(ob_client, full_name="Val Vera", job_role="nurse",
                     unit="unit-icu", service_line="critical-care")
    oid = detail["record"]["onboarding_id"]
    r = ob_client.post(f"/onboarding/{oid}/credentials", json={
        "type": "pilot-licence", "number": "X", "expires_on": FUTURE},
        headers=ADMIN)
    assert r.status_code == 422
    r = ob_client.post(f"/onboarding/{oid}/credentials", json={
        "type": "rn-licence", "number": "X", "expires_on": "not-a-date"},
        headers=ADMIN)
    assert r.status_code == 422
    assert ob_client.post("/onboarding/onb-missing/credentials", json={
        "type": "rn-licence", "number": "X", "expires_on": FUTURE},
        headers=ADMIN).status_code == 404


def test_viewer_and_clinician_cannot_onboard(ob_client):
    body = {"full_name": "Nope Nia", "job_role": "nurse"}
    assert ob_client.post("/onboarding", json=body,
                          headers=VIEWER).status_code == 403
    assert ob_client.post("/onboarding", json=body,
                          headers=CLINICIAN).status_code == 403
    # Configurator holds workforce.onboard too. (Placement added —
    # onboarding create requires a full placement, Org Placement pass.)
    r = ob_client.post("/onboarding", json={
        **body, "unit": "unit-icu", "region": "region-main",
        "location": "facility-main", "department": "dept-critical-care",
    }, headers=CONFIG)
    assert r.status_code == 201, r.text
    # Reads are workforce.read: the viewer may look.
    assert ob_client.get("/onboarding", headers=VIEWER).status_code == 200


def test_detail_404(ob_client):
    assert ob_client.get("/onboarding/onb-nope",
                         headers=ADMIN).status_code == 404


# ---------------------------------------------------------------------------
# Orientation items
# ---------------------------------------------------------------------------

def test_orientation_item_completion_rules(ob_client):
    detail = _create(ob_client, full_name="Ori Owen", job_role="front-desk",
                     unit="clinic", service_line="medicine")
    oid = detail["record"]["onboarding_id"]
    r = ob_client.post(f"/onboarding/{oid}/orientation/bogus/complete",
                       json={}, headers=ADMIN)
    assert r.status_code == 422
    r = ob_client.post(
        f"/onboarding/{oid}/orientation/verify-identity/complete",
        json={"note": "passport seen"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    item = [i for i in r.json()["orientation"]["items"]
            if i["linkId"] == "verify-identity"][0]
    assert item["completed"] is True
    assert item["completed_by"] == "dev-admin"
    r = ob_client.post(
        f"/onboarding/{oid}/orientation/verify-identity/complete",
        json={}, headers=ADMIN)
    assert r.status_code == 409


def test_orientation_advance_names_incomplete_items(ob_client):
    detail = _create(ob_client, full_name="Gate Gail",
                     job_role="front-desk", unit="clinic",
                     service_line="medicine")
    oid = detail["record"]["onboarding_id"]
    _drive_to_account_provisioned(ob_client, oid)
    r = _advance(ob_client, oid)
    assert r.status_code == 409
    assert "Identity verified" in r.json()["detail"]


# ---------------------------------------------------------------------------
# List filters
# ---------------------------------------------------------------------------

def test_list_filters(ob_client):
    _create(ob_client, full_name="Filter Fran", job_role="front-desk",
            unit="clinic", service_line="medicine")
    _create(ob_client, full_name="Filter Phil", job_role="physician")
    r = ob_client.get("/onboarding?stage=credentialing", headers=ADMIN)
    names = {i["staff_name"] for i in r.json()["items"]}
    assert {"Filter Fran", "Filter Phil"} <= names
    assert all(i["stage"] == "credentialing" for i in r.json()["items"])
    r = ob_client.get("/onboarding?job_role=physician", headers=ADMIN)
    assert r.json()["total"] >= 1
    assert all(i["job_role"] == "physician" for i in r.json()["items"])
    assert all("blockers_count" in i for i in r.json()["items"])
    r = ob_client.get("/onboarding?stage=orientation", headers=ADMIN)
    assert {i["staff_id"] for i in r.json()["items"]} == {"staff-019"}


# ---------------------------------------------------------------------------
# Termination (offboarding)
# ---------------------------------------------------------------------------

def test_terminate_mid_onboarding_ends_everything(ob_client):
    detail = _create(ob_client, full_name="Term Tom", job_role="front-desk",
                     unit="clinic", service_line="medicine")
    record = detail["record"]
    oid = record["onboarding_id"]
    staff_id = record["staff_id"]
    provisioned = _drive_to_account_provisioned(ob_client, oid)
    token = provisioned["dev_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert ob_client.get("/people/staff", headers=headers).status_code \
        == 200
    # A team membership + an empanelment naming Tom as PCP, inserted
    # directly (the /teams surface is a sibling workstream).
    app = ob_client.app_ref
    with app.state.session_factory() as session:
        session.add(TeamMembership(
            membership_id="memb-term-1", team_id="team-x",
            staff_id=staff_id, role_in_team="member", status="active",
            created_by="test", ended_reason=""))
        session.add(Empanelment(
            empanelment_id="emp-term-1", patient_ref="Patient/pat-1",
            pcp_staff_id=staff_id, team_id="team-x", status="active",
            assigned_by="test", ended_reason=""))
        session.commit()
    # Reason is required.
    r = ob_client.post(f"/onboarding/{oid}/terminate", json={},
                       headers=ADMIN)
    assert r.status_code == 422
    r = ob_client.post(f"/onboarding/{oid}/terminate",
                       json={"reason": "withdrew before start"},
                       headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["record"]["stage"] == "terminated"
    assert body["record"]["termination_reason"] == "withdrew before start"
    assert body["record"]["terminated_at"]
    assert body["staff"]["status"] == "inactive"
    assert body["account"]["status"] == "disabled"
    # The disabled account's token is dead.
    assert ob_client.get("/people/staff", headers=headers).status_code \
        == 401
    with app.state.session_factory() as session:
        membership = session.scalars(select(TeamMembership).where(
            TeamMembership.membership_id == "memb-term-1")).first()
        assert membership.status == "ended"
        assert membership.ended_reason == "employment terminated"
        empanelment = session.scalars(select(Empanelment).where(
            Empanelment.empanelment_id == "emp-term-1")).first()
        assert empanelment.status == "ended"
        assert empanelment.ended_reason == \
            "pcp terminated — patient unempanelled"
        actions = {row.action for row in
                   session.scalars(select(AuditLog)).all()}
        assert "onboarding.terminate" in actions
        assert "accounts.disable" in actions
    # Terminating twice is refused.
    r = ob_client.post(f"/onboarding/{oid}/terminate",
                       json={"reason": "again"}, headers=ADMIN)
    assert r.status_code == 409


def test_advance_from_seeded_active_record_409(ob_client):
    r = ob_client.get("/onboarding?stage=active", headers=ADMIN)
    oid = r.json()["items"][0]["onboarding_id"]
    assert _advance(ob_client, oid).status_code == 409
