"""Care teams, panels & coverage — Workforce, Teams & Panels pass
(app.teams).

How a real institution actually runs, modelled as governed data:

- **Care teams**: named groups of staff — unit teams, service-line
  teams, consult services, primary-care teams (types, team roles and
  panel rules are the published `terminology/teams` config, seeded
  by app.privileging). Membership is ended, never deleted. Joining
  as a team's ATTENDING is privilege-gated: app.privileging judges
  the grants and credentials, and a refusal is a 409 naming every
  problem, audited as teams.membership.refused — never a silent no.
- **Patient assignment**: a team is requested for a patient
  (primary = accountable, consulting = advising) and accepts or
  declines. A second primary request while one is active is a
  HANDOFF: accepting it ends the superseded assignment in the same
  transaction, so a patient never has two accountable teams — and
  never has none mid-handoff.
- **Coverage**: explicit delegations (staff or team covering staff
  or team, over a window). While a delegation is current, the
  covering side's team panel includes the covered side's patients,
  merged and flagged via_coverage — coverage is data, not a favour
  system.
- **Empanelment** (Kaiser pattern): a patient is empanelled to a
  PCP on a primary-care team. The PCP must hold the
  primary-care-panel privilege (gate refusal audited). Re-empanelment
  supersedes — the old row is ended in the same transaction, never
  edited — so panel history is preserved.
- **Panels**: panel size is counted from ACTIVE empanelments;
  weighted size is deterministic arithmetic from the published
  weights (age band x base + per-condition + frailty uplift), and
  capacity is judged against the published thresholds. Rebalance
  SUGGESTIONS are computed, never applied: a move is an explicit
  audited empanelment action by a human.

Every mutation is hash-chain audited (app.audit.append_audit) in the
same transaction as the change.
"""
import math
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import continuum, inpatient, org_hierarchy, people, privileging
from .audit import append_audit
from .models import (CareTeam, ComplianceRequirement, CoverageDelegation,
                     Empanelment, FhirResource, NurseAssignment, StaffMember,
                     StaffPrivilege, TeamMembership, TeamPatientAssignment,
                     User)
from .privileging import (attending_privilege_codes, check_privileges,
                          grant_privilege, job_catalogue, privileges_config,
                          revoke_privilege, teams_config)
from .registration import patient_display_name
from .security import get_db, require_capability

router = APIRouter(prefix="/teams", tags=["teams"])

READ = "teams.read"
WORK = "teams.work"

ASSIGNMENT_ROLES = ("primary", "consulting")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    aware = _aware(dt)
    return aware.isoformat() if aware is not None else None


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422,
                            detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422,
                            detail="Request body must be a JSON object")
    return body


def _require_str(body: dict, field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=422,
            detail=f"'{field}' is required and must be a non-empty string")
    return value.strip()


def _opt_str(body: dict, field: str) -> str | None:
    value = body.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be a string")
    return value.strip() or None


# ---------------------------------------------------------------------------
# Lookups + serializers
# ---------------------------------------------------------------------------

def _get_team(session: Session, team_id: str) -> CareTeam:
    row = session.scalars(select(CareTeam).where(
        CareTeam.team_id == team_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Care team '{team_id}' not found")
    return row


def _patient_row(session: Session, patient_ref: str) -> FhirResource | None:
    if not isinstance(patient_ref, str) \
            or not patient_ref.startswith("Patient/"):
        return None
    return session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Patient",
        FhirResource.resource_id == patient_ref.split("/", 1)[1])).first()


def _require_patient(session: Session, patient_ref) -> FhirResource:
    if not isinstance(patient_ref, str) or not patient_ref.strip():
        raise HTTPException(
            status_code=422,
            detail="'patient_ref' is required and must be a non-empty string")
    row = _patient_row(session, patient_ref.strip())
    if row is None:
        raise HTTPException(status_code=422,
                            detail=f"Unknown patient '{patient_ref}'")
    return row


def _patient_name(session: Session, patient_ref: str | None) -> str | None:
    if not patient_ref:
        return None
    row = _patient_row(session, patient_ref)
    return patient_display_name(row.body or {}) if row is not None \
        else patient_ref


def _validate_scope(session: Session, unit, service_line) -> None:
    """Unit / service-line validation against the published
    workforce config — the app.people rule (a team's unit may name
    a workforce unit or, like a staff unit, a service line)."""
    ops = people._ops(session)
    if unit is not None:
        known = people._config_codes(ops, "units") \
            | people._config_codes(ops, "serviceLines")
        if unit not in known:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown unit '{unit}': not in the published "
                       "workforce config")
    if service_line is not None \
            and service_line not in people._config_codes(ops, "serviceLines"):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown service line '{service_line}': not in the "
                   "published workforce config")


def _team_out(session: Session, row: CareTeam) -> dict:
    # Org placement (Organisation & Registration Placement pass):
    # the team's unit resolved up the governed REGION -> LOCATION
    # -> DEPARTMENT -> UNIT hierarchy, as {code, display} per level.
    # A team with no unit — or whose unit does not resolve in the
    # hierarchy (a team unit may legitimately name a service line) —
    # carries placement None, never a partial guess.
    placement = None
    if row.unit:
        chain = org_hierarchy.placement_for_unit(session, row.unit)
        if chain is not None:
            placement = org_hierarchy.placement_view(session, chain)
    return {"team_id": row.team_id, "name": row.name,
            "team_type": row.team_type, "unit": row.unit,
            "service_line": row.service_line,
            "description": row.description, "status": row.status,
            "placement": placement,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at)}


def _membership_out(row: TeamMembership,
                    staff_names: dict[str, str]) -> dict:
    return {"membership_id": row.membership_id, "team_id": row.team_id,
            "staff_id": row.staff_id,
            "staff_name": staff_names.get(row.staff_id),
            "role_in_team": row.role_in_team, "status": row.status,
            "created_by": row.created_by,
            "ended_reason": row.ended_reason,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at)}


def _assignment_out(row: TeamPatientAssignment) -> dict:
    return {"assignment_id": row.assignment_id,
            "patient_ref": row.patient_ref,
            "encounter_id": row.encounter_id, "team_id": row.team_id,
            "role": row.role, "state": row.state, "handoff": row.handoff,
            "supersedes_assignment_id": row.supersedes_assignment_id,
            "requested_by": row.requested_by,
            "decided_by": row.decided_by,
            "decided_at": _iso(row.decided_at), "note": row.note,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at)}


def _coverage_out(row: CoverageDelegation) -> dict:
    return {"coverage_id": row.coverage_id,
            "covered_staff_id": row.covered_staff_id,
            "covered_team_id": row.covered_team_id,
            "covering_staff_id": row.covering_staff_id,
            "covering_team_id": row.covering_team_id,
            "starts_at": _iso(row.starts_at), "ends_at": _iso(row.ends_at),
            "reason": row.reason, "status": row.status,
            "created_by": row.created_by,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at)}


def _empanelment_out(row: Empanelment,
                     staff_names: dict[str, str]) -> dict:
    return {"empanelment_id": row.empanelment_id,
            "patient_ref": row.patient_ref,
            "pcp_staff_id": row.pcp_staff_id,
            "pcp_name": staff_names.get(row.pcp_staff_id),
            "team_id": row.team_id, "status": row.status,
            "assigned_by": row.assigned_by,
            "ended_reason": row.ended_reason,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at)}


def _grant_out(row: StaffPrivilege, staff_names: dict[str, str]) -> dict:
    return {"grant_id": row.grant_id, "staff_id": row.staff_id,
            "staff_name": staff_names.get(row.staff_id),
            "privilege_code": row.privilege_code,
            "service_line": row.service_line, "status": row.status,
            "granted_by": row.granted_by,
            "granted_at": _iso(row.granted_at),
            "revoked_by": row.revoked_by,
            "revoked_at": _iso(row.revoked_at), "note": row.note}


def _active_assignments(session: Session, team_id: str | None = None,
                        patient_ref: str | None = None,
                        role: str | None = None) -> list[TeamPatientAssignment]:
    stmt = select(TeamPatientAssignment).where(
        TeamPatientAssignment.state == "active")
    if team_id:
        stmt = stmt.where(TeamPatientAssignment.team_id == team_id)
    if patient_ref:
        stmt = stmt.where(TeamPatientAssignment.patient_ref == patient_ref)
    if role:
        stmt = stmt.where(TeamPatientAssignment.role == role)
    return list(session.scalars(stmt).all())


def _is_current(row: CoverageDelegation, now: datetime) -> bool:
    starts, ends = _aware(row.starts_at), _aware(row.ends_at)
    return row.status == "active" and starts is not None \
        and ends is not None and starts <= now < ends


# ---------------------------------------------------------------------------
# Config surface
# ---------------------------------------------------------------------------

@router.get("/config")
def teams_config_surface(session: Session = Depends(get_db),
                         user: User = Depends(require_capability(READ))):
    """The published teams / privileges / job-catalogue documents —
    the same configuration the gates validate against."""
    return {"teams": teams_config(session),
            "privileges": privileges_config(session),
            "jobCatalogue": job_catalogue(session)}


# ---------------------------------------------------------------------------
# Teams + membership
# ---------------------------------------------------------------------------

@router.post("/", status_code=201)
async def create_team(request: Request, session: Session = Depends(get_db),
                      user: User = Depends(require_capability(WORK))):
    body = await _json_body(request)
    name = _require_str(body, "name")
    team_type = _require_str(body, "team_type")
    cfg = teams_config(session)
    type_codes = {str(t["code"]) for t in cfg.get("teamTypes") or []
                  if isinstance(t, dict) and t.get("code")}
    if team_type not in type_codes:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown team type '{team_type}': not in the "
                   "published teams config")
    unit = _opt_str(body, "unit")
    service_line = _opt_str(body, "service_line")
    _validate_scope(session, unit, service_line)
    description = _opt_str(body, "description") or ""
    team_id = _opt_str(body, "team_id") or f"team-{uuid.uuid4().hex[:8]}"
    if session.scalars(select(CareTeam).where(
            CareTeam.team_id == team_id)).first() is not None:
        raise HTTPException(status_code=409,
                            detail=f"Care team '{team_id}' already exists")
    row = CareTeam(team_id=team_id, name=name, team_type=team_type,
                   unit=unit, service_line=service_line,
                   description=description, status="active")
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="teams.team.create",
                 entity=f"team:{team_id}",
                 payload={"name": name, "team_type": team_type,
                          "unit": unit, "service_line": service_line})
    session.commit()
    return _team_out(session, row)


@router.get("/")
def list_teams(team_type: str | None = Query(default=None),
               unit: str | None = Query(default=None),
               service_line: str | None = Query(default=None),
               status: str | None = Query(default=None),
               session: Session = Depends(get_db),
               user: User = Depends(require_capability(READ))):
    stmt = select(CareTeam).order_by(CareTeam.name.asc())
    if team_type:
        stmt = stmt.where(CareTeam.team_type == team_type)
    if unit:
        stmt = stmt.where(CareTeam.unit == unit)
    if service_line:
        stmt = stmt.where(CareTeam.service_line == service_line)
    if status:
        stmt = stmt.where(CareTeam.status == status)
    rows = session.scalars(stmt).all()
    items = []
    for row in rows:
        member_count = len(session.scalars(select(TeamMembership).where(
            TeamMembership.team_id == row.team_id,
            TeamMembership.status == "active")).all())
        patients = {a.patient_ref for a in
                    _active_assignments(session, team_id=row.team_id)}
        items.append({**_team_out(session, row), "member_count": member_count,
                      "active_patient_count": len(patients)})
    return {"items": items, "total": len(items)}


@router.post("/{team_id}/members", status_code=201)
async def add_member(team_id: str, request: Request,
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability(WORK))):
    """Add a staff member to a team. The ATTENDING role is
    privilege-gated: the member must hold the attending privileges
    for the team's service line, grounded in unexpired credentials —
    a refusal is a 409 naming every problem, and the refusal itself
    is audited (teams.membership.refused)."""
    team = _get_team(session, team_id)
    body = await _json_body(request)
    staff = people._get_staff(session, _require_str(body, "staff_id"))
    role_in_team = _require_str(body, "role_in_team")
    cfg = teams_config(session)
    team_roles = cfg.get("teamRoles") or []
    if role_in_team not in team_roles:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown team role '{role_in_team}': not in the "
                   "published teams config")
    if staff.status != "active":
        raise HTTPException(
            status_code=409,
            detail=f"Staff member '{staff.staff_id}' is "
                   f"'{staff.status}'; only active staff can join a team")
    duplicate = session.scalars(select(TeamMembership).where(
        TeamMembership.team_id == team_id,
        TeamMembership.staff_id == staff.staff_id,
        TeamMembership.status == "active")).first()
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{staff.full_name} is already an active member of "
                   f"team '{team_id}' ({duplicate.membership_id})")
    if role_in_team == "attending":
        codes = attending_privilege_codes(session, team.service_line)
        problems = check_privileges(session, staff, codes,
                                    team.service_line)
        if problems:
            append_audit(session, actor=user.username,
                         action="teams.membership.refused",
                         entity=f"team:{team_id}",
                         payload={"staff_id": staff.staff_id,
                                  "role_in_team": role_in_team,
                                  "problems": problems})
            session.commit()
            raise HTTPException(status_code=409,
                                detail="; ".join(problems))
    row = TeamMembership(
        membership_id=f"tmem-{uuid.uuid4().hex[:10]}", team_id=team_id,
        staff_id=staff.staff_id, role_in_team=role_in_team,
        status="active", created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="teams.membership.add", entity=f"team:{team_id}",
                 payload={"membership_id": row.membership_id,
                          "staff_id": staff.staff_id,
                          "role_in_team": role_in_team})
    session.commit()
    return _membership_out(row, people._staff_names(session))


@router.post("/{team_id}/members/{membership_id}/end")
async def end_member(team_id: str, membership_id: str, request: Request,
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability(WORK))):
    _get_team(session, team_id)
    body = await _json_body(request)
    row = session.scalars(select(TeamMembership).where(
        TeamMembership.membership_id == membership_id,
        TeamMembership.team_id == team_id)).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Membership '{membership_id}' not found on team "
                   f"'{team_id}'")
    if row.status != "active":
        raise HTTPException(
            status_code=409,
            detail=f"Membership '{membership_id}' is already "
                   f"'{row.status}'")
    row.status = "ended"
    row.ended_reason = _opt_str(body, "reason") or ""
    session.flush()
    append_audit(session, actor=user.username,
                 action="teams.membership.end", entity=f"team:{team_id}",
                 payload={"membership_id": membership_id,
                          "staff_id": row.staff_id,
                          "reason": row.ended_reason})
    session.commit()
    return _membership_out(row, people._staff_names(session))


# ---------------------------------------------------------------------------
# Patient assignments (request -> accept / decline; handoff on
# second primary)
# ---------------------------------------------------------------------------

@router.post("/assignments", status_code=201)
async def request_assignment(request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability(WORK))):
    body = await _json_body(request)
    patient_ref = _require_str(body, "patient_ref")
    _require_patient(session, patient_ref)
    team = _get_team(session, _require_str(body, "team_id"))
    role = _require_str(body, "role")
    if role not in ASSIGNMENT_ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"'role' must be one of {list(ASSIGNMENT_ROLES)}")
    encounter_id = _opt_str(body, "encounter_id")
    note = _opt_str(body, "note") or ""
    handoff = False
    supersedes = None
    if role == "primary":
        requested = session.scalars(select(TeamPatientAssignment).where(
            TeamPatientAssignment.patient_ref == patient_ref,
            TeamPatientAssignment.role == "primary",
            TeamPatientAssignment.state == "requested")).first()
        if requested is not None:
            raise HTTPException(
                status_code=409,
                detail=f"A primary assignment for {patient_ref} is "
                       f"already awaiting a decision "
                       f"({requested.assignment_id})")
        active = session.scalars(select(TeamPatientAssignment).where(
            TeamPatientAssignment.patient_ref == patient_ref,
            TeamPatientAssignment.role == "primary",
            TeamPatientAssignment.state == "active")).first()
        if active is not None:
            handoff = True
            supersedes = active.assignment_id
    else:  # consulting
        duplicate = session.scalars(select(TeamPatientAssignment).where(
            TeamPatientAssignment.patient_ref == patient_ref,
            TeamPatientAssignment.team_id == team.team_id,
            TeamPatientAssignment.role == "consulting",
            TeamPatientAssignment.state.in_(["requested", "active"]))).first()
        if duplicate is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Team '{team.team_id}' already has a "
                       f"'{duplicate.state}' consulting assignment for "
                       f"{patient_ref} ({duplicate.assignment_id})")
    row = TeamPatientAssignment(
        assignment_id=f"tassign-{uuid.uuid4().hex[:10]}",
        patient_ref=patient_ref, encounter_id=encounter_id,
        team_id=team.team_id, role=role, state="requested",
        handoff=handoff, supersedes_assignment_id=supersedes,
        requested_by=user.username, note=note)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="teams.assignment.request",
                 entity=f"team:{team.team_id}",
                 payload={"assignment_id": row.assignment_id,
                          "patient_ref": patient_ref, "role": role,
                          "handoff": handoff,
                          "supersedes": supersedes})
    session.commit()
    return _assignment_out(row)


@router.get("/assignments")
def list_assignments(patient_ref: str | None = Query(default=None),
                     team_id: str | None = Query(default=None),
                     state: str | None = Query(default=None),
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability(READ))):
    stmt = select(TeamPatientAssignment).order_by(
        TeamPatientAssignment.created_at.asc(),
        TeamPatientAssignment.id.asc())
    if patient_ref:
        stmt = stmt.where(TeamPatientAssignment.patient_ref == patient_ref)
    if team_id:
        stmt = stmt.where(TeamPatientAssignment.team_id == team_id)
    if state:
        stmt = stmt.where(TeamPatientAssignment.state == state)
    rows = session.scalars(stmt).all()
    return {"items": [_assignment_out(r) for r in rows],
            "total": len(rows)}


def _get_assignment(session: Session,
                    assignment_id: str) -> TeamPatientAssignment:
    row = session.scalars(select(TeamPatientAssignment).where(
        TeamPatientAssignment.assignment_id == assignment_id)).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Team assignment '{assignment_id}' not found")
    return row


@router.post("/assignments/{assignment_id}/accept")
async def accept_assignment(assignment_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability(WORK))):
    """Accept a requested assignment. For a handoff, the superseded
    active primary is ended in the SAME transaction — the patient
    passes from one accountable team to the other atomically."""
    row = _get_assignment(session, assignment_id)
    if row.state != "requested":
        raise HTTPException(
            status_code=409,
            detail=f"Assignment '{assignment_id}' is '{row.state}'; "
                   "only a requested assignment can be accepted")
    superseded = None
    if row.handoff and row.supersedes_assignment_id:
        old = session.scalars(select(TeamPatientAssignment).where(
            TeamPatientAssignment.assignment_id
            == row.supersedes_assignment_id)).first()
        if old is not None and old.state == "active":
            old.state = "ended"
            old.note = f"superseded by handoff to {row.team_id}"
            superseded = old.assignment_id
    row.state = "active"
    row.decided_by = user.username
    row.decided_at = _now()
    session.flush()
    append_audit(session, actor=user.username,
                 action="teams.assignment.accept",
                 entity=f"team:{row.team_id}",
                 payload={"assignment_id": assignment_id,
                          "patient_ref": row.patient_ref,
                          "role": row.role, "handoff": row.handoff})
    if superseded is not None:
        append_audit(session, actor=user.username,
                     action="teams.assignment.superseded",
                     entity=f"team:{row.team_id}",
                     payload={"assignment_id": superseded,
                              "patient_ref": row.patient_ref,
                              "superseded_by": assignment_id})
    session.commit()
    return _assignment_out(row)


@router.post("/assignments/{assignment_id}/decline")
async def decline_assignment(assignment_id: str, request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability(WORK))):
    body = await _json_body(request)
    row = _get_assignment(session, assignment_id)
    if row.state != "requested":
        raise HTTPException(
            status_code=409,
            detail=f"Assignment '{assignment_id}' is '{row.state}'; "
                   "only a requested assignment can be declined")
    row.state = "declined"
    row.decided_by = user.username
    row.decided_at = _now()
    reason = _opt_str(body, "reason")
    if reason:
        row.note = reason
    session.flush()
    append_audit(session, actor=user.username,
                 action="teams.assignment.decline",
                 entity=f"team:{row.team_id}",
                 payload={"assignment_id": assignment_id,
                          "patient_ref": row.patient_ref,
                          "reason": reason})
    session.commit()
    return _assignment_out(row)


@router.post("/assignments/{assignment_id}/end")
async def end_assignment(assignment_id: str, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability(WORK))):
    body = await _json_body(request)
    row = _get_assignment(session, assignment_id)
    if row.state != "active":
        raise HTTPException(
            status_code=409,
            detail=f"Assignment '{assignment_id}' is '{row.state}'; "
                   "only an active assignment can be ended")
    row.state = "ended"
    reason = _opt_str(body, "reason")
    if reason:
        row.note = reason
    session.flush()
    append_audit(session, actor=user.username,
                 action="teams.assignment.end",
                 entity=f"team:{row.team_id}",
                 payload={"assignment_id": assignment_id,
                          "patient_ref": row.patient_ref,
                          "reason": reason})
    session.commit()
    return _assignment_out(row)


# ---------------------------------------------------------------------------
# Unassigned queue
# ---------------------------------------------------------------------------

def _unassigned_items(session: Session, unit: str | None = None,
                      service_line: str | None = None) -> list[dict]:
    """Active admissions with NO active primary team assignment —
    the queue the assignment desk works. Scoped by unit directly, or
    by service line through the unit's workforce-config line."""
    ops = people._ops(session)
    unit_service = {u["code"]: u.get("serviceLine")
                    for u in ops.get("units") or []
                    if isinstance(u, dict) and u.get("code")}
    assigned = {a.patient_ref for a in _active_assignments(
        session, role="primary")}
    bed_units = continuum._bed_unit_map(session)
    now = _now()
    rows = []
    for admission in continuum._active_admissions(session):
        body = admission.body or {}
        patient_ref = inpatient._subject_ref(body)
        if patient_ref in assigned:
            continue
        admission_unit = continuum._admission_unit(session, body, bed_units)
        if unit is not None and admission_unit != unit:
            continue
        if service_line is not None \
                and unit_service.get(admission_unit or "") != service_line:
            continue
        rows.append(admission)
    names = people._patient_names(
        session, [inpatient._subject_ref(r.body or {}) for r in rows])
    items = []
    for admission in rows:
        body = admission.body or {}
        patient_ref = inpatient._subject_ref(body)
        items.append({
            "encounter_id": admission.resource_id,
            "patient_ref": patient_ref,
            "patient_name": names.get(patient_ref) if patient_ref else None,
            "bed": inpatient._current_bed(body),
            "unit": continuum._admission_unit(session, body, bed_units),
            "admitted_at": inpatient._los(admission, now)["admitted_at"],
        })
    return items


@router.get("/unassigned")
def unassigned_queue(unit: str | None = Query(default=None),
                     service_line: str | None = Query(default=None),
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability(READ))):
    items = _unassigned_items(session, unit=unit,
                              service_line=service_line)
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Coverage delegations
# ---------------------------------------------------------------------------

def _parse_window(body: dict) -> tuple[datetime, datetime]:
    starts_raw = _require_str(body, "starts_at")
    ends_raw = _require_str(body, "ends_at")
    try:
        starts_at = datetime.fromisoformat(starts_raw.replace("Z", "+00:00"))
        ends_at = datetime.fromisoformat(ends_raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="'starts_at'/'ends_at' must be ISO datetimes")
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=timezone.utc)
    if ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=timezone.utc)
    if ends_at <= starts_at:
        raise HTTPException(status_code=422,
                            detail="'ends_at' must be after 'starts_at'")
    return starts_at, ends_at


@router.post("/coverage", status_code=201)
async def create_coverage(request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability(WORK))):
    """Create a coverage delegation: exactly one covered side and
    exactly one covering side, each a staff member XOR a team."""
    body = await _json_body(request)
    covered_staff = _opt_str(body, "covered_staff_id")
    covered_team = _opt_str(body, "covered_team_id")
    covering_staff = _opt_str(body, "covering_staff_id")
    covering_team = _opt_str(body, "covering_team_id")
    if sum(v is not None for v in (covered_staff, covered_team)) != 1:
        raise HTTPException(
            status_code=422,
            detail="Exactly one of 'covered_staff_id'/'covered_team_id' "
                   "is required")
    if sum(v is not None for v in (covering_staff, covering_team)) != 1:
        raise HTTPException(
            status_code=422,
            detail="Exactly one of 'covering_staff_id'/'covering_team_id' "
                   "is required")
    starts_at, ends_at = _parse_window(body)
    for label, staff_id in (("covered", covered_staff),
                            ("covering", covering_staff)):
        if staff_id is not None and session.scalars(
                select(StaffMember).where(
                    StaffMember.staff_id == staff_id)).first() is None:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown {label} staff '{staff_id}'")
    for label, team_id in (("covered", covered_team),
                           ("covering", covering_team)):
        if team_id is not None and session.scalars(
                select(CareTeam).where(
                    CareTeam.team_id == team_id)).first() is None:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown {label} team '{team_id}'")
    row = CoverageDelegation(
        coverage_id=f"cov-{uuid.uuid4().hex[:10]}",
        covered_staff_id=covered_staff, covered_team_id=covered_team,
        covering_staff_id=covering_staff, covering_team_id=covering_team,
        starts_at=starts_at, ends_at=ends_at,
        reason=_opt_str(body, "reason") or "", status="active",
        created_by=user.username)
    session.add(row)
    session.flush()
    entity = f"team:{covered_team}" if covered_team \
        else f"staff:{covered_staff}"
    append_audit(session, actor=user.username,
                 action="teams.coverage.create", entity=entity,
                 payload={"coverage_id": row.coverage_id,
                          "covered_staff_id": covered_staff,
                          "covered_team_id": covered_team,
                          "covering_staff_id": covering_staff,
                          "covering_team_id": covering_team,
                          "starts_at": starts_at.isoformat(),
                          "ends_at": ends_at.isoformat()})
    session.commit()
    return _coverage_out(row)


@router.get("/coverage")
def list_coverage(team_id: str | None = Query(default=None),
                  staff_id: str | None = Query(default=None),
                  current: bool = Query(default=False),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability(READ))):
    rows = session.scalars(select(CoverageDelegation).order_by(
        CoverageDelegation.starts_at.asc(),
        CoverageDelegation.id.asc())).all()
    now = _now()
    items = []
    for row in rows:
        if team_id and team_id not in (row.covered_team_id,
                                       row.covering_team_id):
            continue
        if staff_id and staff_id not in (row.covered_staff_id,
                                         row.covering_staff_id):
            continue
        if current and not _is_current(row, now):
            continue
        items.append(_coverage_out(row))
    return {"items": items, "total": len(items)}


@router.post("/coverage/{coverage_id}/cancel")
async def cancel_coverage(coverage_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability(WORK))):
    body = await _json_body(request)
    row = session.scalars(select(CoverageDelegation).where(
        CoverageDelegation.coverage_id == coverage_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Coverage '{coverage_id}' not found")
    if row.status != "active":
        raise HTTPException(
            status_code=409,
            detail=f"Coverage '{coverage_id}' is already '{row.status}'")
    row.status = "cancelled"
    reason = _opt_str(body, "reason")
    if reason:
        row.reason = f"{row.reason}; cancelled: {reason}" if row.reason \
            else f"cancelled: {reason}"
    session.flush()
    append_audit(session, actor=user.username,
                 action="teams.coverage.cancel",
                 entity=f"coverage:{coverage_id}",
                 payload={"coverage_id": coverage_id, "reason": reason})
    session.commit()
    return _coverage_out(row)


# ---------------------------------------------------------------------------
# Empanelment
# ---------------------------------------------------------------------------

@router.post("/empanelment", status_code=201)
async def create_empanelment(request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability(WORK))):
    """Empanel a patient to a PCP on a primary-care team. The PCP
    must hold the primary-care-panel privilege (refusal audited);
    an existing active empanelment is superseded in the same
    transaction — never edited."""
    body = await _json_body(request)
    patient_ref = _require_str(body, "patient_ref")
    _require_patient(session, patient_ref)
    pcp = people._get_staff(session, _require_str(body, "pcp_staff_id"))
    team = _get_team(session, _require_str(body, "team_id"))
    if team.team_type != "primary-care":
        raise HTTPException(
            status_code=422,
            detail=f"Team '{team.team_id}' is a '{team.team_type}' team; "
                   "empanelment requires a 'primary-care' team")
    problems = check_privileges(session, pcp, ["primary-care-panel"],
                                team.service_line)
    if problems:
        append_audit(session, actor=user.username,
                     action="teams.empanelment.refused",
                     entity=f"staff:{pcp.staff_id}",
                     payload={"patient_ref": patient_ref,
                              "team_id": team.team_id,
                              "problems": problems})
        session.commit()
        raise HTTPException(status_code=409, detail="; ".join(problems))
    superseded = []
    for old in session.scalars(select(Empanelment).where(
            Empanelment.patient_ref == patient_ref,
            Empanelment.status == "active")).all():
        old.status = "ended"
        old.ended_reason = "superseded"
        superseded.append(old.empanelment_id)
    row = Empanelment(
        empanelment_id=f"emp-{uuid.uuid4().hex[:10]}",
        patient_ref=patient_ref, pcp_staff_id=pcp.staff_id,
        team_id=team.team_id, status="active", assigned_by=user.username)
    session.add(row)
    session.flush()
    for old_id in superseded:
        append_audit(session, actor=user.username,
                     action="teams.empanelment.supersede",
                     entity=patient_ref,
                     payload={"empanelment_id": old_id,
                              "superseded_by": row.empanelment_id})
    append_audit(session, actor=user.username,
                 action="teams.empanelment.create", entity=patient_ref,
                 payload={"empanelment_id": row.empanelment_id,
                          "pcp_staff_id": pcp.staff_id,
                          "team_id": team.team_id})
    session.commit()
    return _empanelment_out(row, people._staff_names(session))


@router.post("/empanelment/{empanelment_id}/end")
async def end_empanelment(empanelment_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability(WORK))):
    body = await _json_body(request)
    row = session.scalars(select(Empanelment).where(
        Empanelment.empanelment_id == empanelment_id)).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Empanelment '{empanelment_id}' not found")
    if row.status != "active":
        raise HTTPException(
            status_code=409,
            detail=f"Empanelment '{empanelment_id}' is already "
                   f"'{row.status}'")
    row.status = "ended"
    row.ended_reason = _opt_str(body, "reason") or ""
    session.flush()
    append_audit(session, actor=user.username,
                 action="teams.empanelment.end", entity=row.patient_ref,
                 payload={"empanelment_id": empanelment_id,
                          "reason": row.ended_reason})
    session.commit()
    return _empanelment_out(row, people._staff_names(session))


@router.get("/empanelment")
def list_empanelments(patient_ref: str | None = Query(default=None),
                      pcp_staff_id: str | None = Query(default=None),
                      team_id: str | None = Query(default=None),
                      status: str | None = Query(default=None),
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability(READ))):
    stmt = select(Empanelment).order_by(Empanelment.created_at.asc(),
                                        Empanelment.id.asc())
    if patient_ref:
        stmt = stmt.where(Empanelment.patient_ref == patient_ref)
    if pcp_staff_id:
        stmt = stmt.where(Empanelment.pcp_staff_id == pcp_staff_id)
    if team_id:
        stmt = stmt.where(Empanelment.team_id == team_id)
    if status:
        stmt = stmt.where(Empanelment.status == status)
    rows = session.scalars(stmt).all()
    names = people._staff_names(session)
    return {"items": [_empanelment_out(r, names) for r in rows],
            "total": len(rows)}


# ---------------------------------------------------------------------------
# Panels: weighting, capacity, rebalance suggestions
# ---------------------------------------------------------------------------

def _num(value, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _patient_weight(session: Session, patient_ref: str,
                    weights_cfg: dict) -> tuple[float, dict]:
    """One empanelled patient's deterministic panel weight:
    base x age-band weight + per-condition x condition count +
    frailty uplift. The basis dict names every component, so a
    panel figure can always be explained patient by patient."""
    weights = weights_cfg or {}
    base = _num(weights.get("base"), 1.0)
    per_condition = _num(weights.get("perCondition"), 0.0)
    frailty_extra = _num(weights.get("frailtyExtra"), 0.0)
    frailty_age = weights.get("frailtyAge")
    frailty_age = int(frailty_age) if isinstance(frailty_age, int) \
        and not isinstance(frailty_age, bool) else None
    frailty_codes = {str(c) for c in weights.get("frailtyConditionCodes")
                     or [] if isinstance(c, str)}
    age = None
    age_unknown = False
    row = _patient_row(session, patient_ref)
    birth_raw = (row.body or {}).get("birthDate") if row is not None \
        else None
    if isinstance(birth_raw, str):
        try:
            born = date.fromisoformat(birth_raw)
            today = _now().date()
            age = today.year - born.year - (
                (today.month, today.day) < (born.month, born.day))
        except ValueError:
            age = None
    if age is None:
        age_unknown = True
    band_weight = 1.0
    if age is not None:
        for band in weights.get("ageBands") or []:
            if not isinstance(band, dict):
                continue
            low = band.get("min")
            high = band.get("max")
            low = int(low) if isinstance(low, int) \
                and not isinstance(low, bool) else 0
            if age < low:
                continue
            if high is not None and (not isinstance(high, int)
                                     or isinstance(high, bool)
                                     or age > high):
                continue
            band_weight = _num(band.get("weight"), 1.0)
            break
    conditions = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Condition",
        FhirResource.patient_ref == patient_ref)).all()
    codes = {code for code in
             (continuum._coding_code(c.body or {}) for c in conditions)
             if code}
    frailty = bool((age is not None and frailty_age is not None
                    and age >= frailty_age) or codes & frailty_codes)
    condition_weight = len(conditions) * per_condition
    frailty_weight = frailty_extra if frailty else 0.0
    weight = base * band_weight + condition_weight + frailty_weight
    basis = {"base": base, "age_band_weight": band_weight,
             "conditions": len(conditions),
             "condition_weight": condition_weight,
             "frailty": frailty, "frailty_weight": frailty_weight}
    if age_unknown:
        basis["age_unknown"] = True
    return weight, basis


def _capacity(weighted: float, thresholds: dict) -> str:
    max_weighted = _num(thresholds.get("maxWeighted"), None)
    min_weighted = _num(thresholds.get("minWeighted"), None)
    if max_weighted is not None and weighted > max_weighted:
        return "over"
    if min_weighted is not None and weighted < min_weighted:
        return "under"
    return "within"


def _pcp_empanelments(session: Session,
                      pcp_staff_id: str) -> list[Empanelment]:
    return list(session.scalars(select(Empanelment).where(
        Empanelment.pcp_staff_id == pcp_staff_id,
        Empanelment.status == "active")
        .order_by(Empanelment.created_at.asc(), Empanelment.id.asc())).all())


def _panel_summary(session: Session, pcp_staff_id: str,
                   weights_cfg: dict, thresholds: dict,
                   staff_names: dict[str, str]) -> dict:
    rows = _pcp_empanelments(session, pcp_staff_id)
    total = 0.0
    for row in rows:
        weight, _basis = _patient_weight(session, row.patient_ref,
                                         weights_cfg)
        total += weight
    team_id = rows[0].team_id if rows else None
    return {"pcp_staff_id": pcp_staff_id,
            "pcp_name": staff_names.get(pcp_staff_id),
            "team_id": team_id, "panel_size": len(rows),
            "weighted_size": round(total, 2),
            "capacity": _capacity(total, thresholds)}


def _all_panel_summaries(session: Session) -> tuple[list[dict], dict, dict]:
    cfg = teams_config(session)
    weights_cfg = cfg.get("panelWeights") or {}
    thresholds = cfg.get("panelThresholds") or {}
    staff_names = people._staff_names(session)
    pcp_ids = sorted({r.pcp_staff_id for r in session.scalars(
        select(Empanelment).where(Empanelment.status == "active")).all()})
    summaries = [_panel_summary(session, pcp_id, weights_cfg, thresholds,
                                staff_names) for pcp_id in pcp_ids]
    return summaries, weights_cfg, thresholds


@router.get("/panels")
def panels_overview(session: Session = Depends(get_db),
                    user: User = Depends(require_capability(READ))):
    """Every PCP's panel (size + deterministic weighted size +
    capacity against the published thresholds), per-team rollups,
    and house-wide empanelment coverage."""
    summaries, _weights, _thresholds = _all_panel_summaries(session)
    by_team: dict[str, dict] = {}
    for summary in summaries:
        bucket = by_team.setdefault(
            summary["team_id"] or "",
            {"team_id": summary["team_id"], "pcps": 0, "panel_size": 0,
             "weighted_size": 0.0})
        bucket["pcps"] += 1
        bucket["panel_size"] += summary["panel_size"]
        bucket["weighted_size"] = round(
            bucket["weighted_size"] + summary["weighted_size"], 2)
    patients = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Patient")).all()
    empanelled_refs = {r.patient_ref for r in session.scalars(
        select(Empanelment).where(Empanelment.status == "active")).all()}
    all_refs = sorted(f"Patient/{p.resource_id}" for p in patients)
    unempanelled = [ref for ref in all_refs if ref not in empanelled_refs]
    total = len(all_refs)
    return {
        "panels": summaries,
        "teams": [by_team[k] for k in sorted(by_team)],
        "coverage": {
            "patients_total": total,
            "empanelled": len(empanelled_refs & set(all_refs)),
            "unempanelled": len(unempanelled),
            "coverage_pct": (round(100.0 * len(empanelled_refs
                                               & set(all_refs)) / total, 1)
                             if total else 0.0),
        },
        "unempanelled_patient_refs": unempanelled[:100],
    }


@router.get("/panels/rebalance")
def panels_rebalance(session: Session = Depends(get_db),
                     user: User = Depends(require_capability(READ))):
    """Rebalance SUGGESTIONS for over-capacity panels: the patients
    who would move (the most recently empanelled, enough to cover
    the excess weight) and the under/within-capacity PCPs on the
    same team who could receive them. Nothing is moved here — a
    move is an explicit audited empanelment action."""
    summaries, weights_cfg, thresholds = _all_panel_summaries(session)
    max_weighted = _num(thresholds.get("maxWeighted"), None)
    suggestions = []
    for summary in summaries:
        if summary["capacity"] != "over" or max_weighted is None:
            continue
        excess = round(summary["weighted_size"] - max_weighted, 2)
        rows = _pcp_empanelments(session, summary["pcp_staff_id"])
        avg = (summary["weighted_size"] / summary["panel_size"]
               if summary["panel_size"] else 0.0)
        count = min(10, max(1, math.ceil(excess / max(avg, 0.1))))
        recent = sorted(rows, key=lambda r: (r.created_at, r.id),
                        reverse=True)
        receiving = [s["pcp_staff_id"] for s in summaries
                     if s["team_id"] == summary["team_id"]
                     and s["pcp_staff_id"] != summary["pcp_staff_id"]
                     and s["capacity"] in ("under", "within")]
        suggestions.append({
            "pcp_staff_id": summary["pcp_staff_id"],
            "team_id": summary["team_id"],
            "weighted_size": summary["weighted_size"],
            "excess": excess,
            "candidate_patient_refs": [r.patient_ref for r in recent[:count]],
            "receiving_pcp_staff_ids": receiving,
        })
    return {"suggestions": suggestions,
            "basis": "suggestions only — a move is an explicit audited "
                     "empanelment action"}


@router.get("/panels/{staff_id}")
def panel_detail(staff_id: str, session: Session = Depends(get_db),
                 user: User = Depends(require_capability(READ))):
    """One PCP's panel: every empanelled patient with their weight
    and its basis, plus the panel's open care gaps (the continuum
    gap computation filtered to this panel's patients)."""
    staff = people._get_staff(session, staff_id)
    cfg = teams_config(session)
    weights_cfg = cfg.get("panelWeights") or {}
    thresholds = cfg.get("panelThresholds") or {}
    staff_names = people._staff_names(session)
    rows = _pcp_empanelments(session, staff_id)
    refs = [r.patient_ref for r in rows]
    names = people._patient_names(session, refs)
    patients = []
    total = 0.0
    for row in rows:
        weight, basis = _patient_weight(session, row.patient_ref,
                                        weights_cfg)
        total += weight
        patients.append({"patient_ref": row.patient_ref,
                         "patient_name": names.get(row.patient_ref),
                         "weight": round(weight, 2), "basis": basis})
    team = None
    if rows:
        team_row = session.scalars(select(CareTeam).where(
            CareTeam.team_id == rows[0].team_id)).first()
        team = _team_out(session, team_row) if team_row is not None else None
    gaps = [g for g in continuum._gap_items(session,
                                             continuum._ops(session))
            if g.get("patient_ref") in set(refs)]
    return {"pcp": people._staff_out(staff), "team": team,
            "panel_size": len(rows), "weighted_size": round(total, 2),
            "capacity": _capacity(total, thresholds),
            "patients": patients, "care_gaps": gaps}


# ---------------------------------------------------------------------------
# My teams (the caller's own view)
# ---------------------------------------------------------------------------

@router.get("/my-teams")
def my_teams(session: Session = Depends(get_db),
             user: User = Depends(require_capability(READ))):
    """The caller's teams, own panel (when they are a PCP) and the
    coverage they give / receive in the current window — resolved
    through the staff-directory username link, like the role-aware
    home. No directory entry -> an honest empty shape."""
    staff = people._caller_staff(session, user)
    if staff is None:
        return {"staff_id": None, "teams": [], "panel": None,
                "coverage_giving": [], "coverage_receiving": []}
    memberships = session.scalars(select(TeamMembership).where(
        TeamMembership.staff_id == staff.staff_id,
        TeamMembership.status == "active")).all()
    teams = []
    my_team_ids = set()
    for membership in memberships:
        team = session.scalars(select(CareTeam).where(
            CareTeam.team_id == membership.team_id)).first()
        if team is None:
            continue
        my_team_ids.add(team.team_id)
        teams.append({**_team_out(session, team),
                      "role_in_team": membership.role_in_team,
                      "membership_id": membership.membership_id})
    cfg = teams_config(session)
    panel = None
    if _pcp_empanelments(session, staff.staff_id):
        panel = _panel_summary(session, staff.staff_id,
                               cfg.get("panelWeights") or {},
                               cfg.get("panelThresholds") or {},
                               people._staff_names(session))
    now = _now()
    giving, receiving = [], []
    for row in session.scalars(select(CoverageDelegation).where(
            CoverageDelegation.status == "active")).all():
        if not _is_current(row, now):
            continue
        if row.covering_staff_id == staff.staff_id \
                or (row.covering_team_id
                    and row.covering_team_id in my_team_ids):
            giving.append(_coverage_out(row))
        if row.covered_staff_id == staff.staff_id \
                or (row.covered_team_id
                    and row.covered_team_id in my_team_ids):
            receiving.append(_coverage_out(row))
    return {"staff_id": staff.staff_id, "teams": teams, "panel": panel,
            "coverage_giving": giving, "coverage_receiving": receiving}


# ---------------------------------------------------------------------------
# Privilege administration
# ---------------------------------------------------------------------------

@router.post("/privileges", status_code=201)
async def create_privilege_grant(request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_capability(WORK))):
    body = await _json_body(request)
    staff = people._get_staff(session, _require_str(body, "staff_id"))
    code = _require_str(body, "privilege_code")
    cfg = privileges_config(session)
    known = {str(p["code"]) for p in cfg.get("privileges") or []
             if isinstance(p, dict) and p.get("code")}
    if code not in known:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown privilege '{code}': not in the published "
                   "privileges config")
    service_line = _opt_str(body, "service_line")
    if service_line is not None:
        _validate_scope(session, None, service_line)
    row = grant_privilege(session, user.username, staff.staff_id, code,
                          service_line=service_line,
                          note=_opt_str(body, "note") or "")
    session.commit()
    return _grant_out(row, people._staff_names(session))


@router.post("/privileges/{grant_id}/revoke")
async def revoke_privilege_grant(grant_id: str, request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_capability(WORK))):
    body = await _json_body(request)
    row = revoke_privilege(session, user.username, grant_id,
                           _opt_str(body, "reason") or "")
    session.commit()
    return _grant_out(row, people._staff_names(session))


@router.get("/privileges")
def list_privilege_grants(staff_id: str | None = Query(default=None),
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability(READ))):
    stmt = select(StaffPrivilege).order_by(
        StaffPrivilege.granted_at.desc(), StaffPrivilege.id.desc())
    if staff_id:
        stmt = stmt.where(StaffPrivilege.staff_id == staff_id)
    rows = session.scalars(stmt).all()
    names = people._staff_names(session)
    return {"items": [_grant_out(r, names) for r in rows],
            "total": len(rows)}


# ---------------------------------------------------------------------------
# Team detail + the team panel
# ---------------------------------------------------------------------------

@router.get("/{team_id}")
def get_team(team_id: str, session: Session = Depends(get_db),
             user: User = Depends(require_capability(READ))):
    row = _get_team(session, team_id)
    names = people._staff_names(session)
    members = session.scalars(select(TeamMembership).where(
        TeamMembership.team_id == team_id,
        TeamMembership.status == "active")
        .order_by(TeamMembership.created_at.asc())).all()
    return {**_team_out(session, row),
            "members": [_membership_out(m, names) for m in members]}


@router.get("/{team_id}/panel")
def team_panel(team_id: str, session: Session = Depends(get_db),
               user: User = Depends(require_capability(READ))):
    """THE team panel: who is on the team, the patients the team is
    accountable for / advising on (with bed, attending of record and
    primary nurse per patient), the requested assignments awaiting a
    decision, the unassigned admissions in the team's scope, and the
    coverage flowing in (this team is covered — its patients are
    flagged via_coverage) and out (this team gives coverage — the
    covered team's patients are merged in, flagged via_coverage)."""
    team = _get_team(session, team_id)
    staff_names = people._staff_names(session)
    memberships = session.scalars(select(TeamMembership).where(
        TeamMembership.team_id == team_id,
        TeamMembership.status == "active")).all()
    members = [{"staff_id": m.staff_id,
                "name": staff_names.get(m.staff_id),
                "role_in_team": m.role_in_team} for m in memberships]
    member_ids = {m.staff_id for m in memberships}
    admissions = {r.resource_id: r
                  for r in continuum._active_admissions(session)}
    nurse_by_encounter = {n.encounter_id: n for n in
                          session.scalars(select(NurseAssignment)).all()}

    def patient_entry(assignment: TeamPatientAssignment,
                      names: dict[str, str],
                      via_coverage: str | None = None) -> dict:
        encounter = admissions.get(assignment.encounter_id or "")
        bed = attending_id = None
        if encounter is not None:
            body = encounter.body or {}
            bed = inpatient._current_bed(body)
            ext = inpatient._get_extension(body,
                                           inpatient.ATTENDING_EXTENSION)
            attending_id = ext.get("valueString") if ext else None
        nurse_row = nurse_by_encounter.get(assignment.encounter_id or "")
        entry = {
            "assignment_id": assignment.assignment_id,
            "patient_ref": assignment.patient_ref,
            "patient_name": names.get(assignment.patient_ref),
            "role": assignment.role, "state": assignment.state,
            "encounter_id": assignment.encounter_id, "bed": bed,
            "attending_staff_id": attending_id,
            "attending_name": (staff_names.get(attending_id)
                               if attending_id else None),
            "primary_nurse": (staff_names.get(nurse_row.nurse,
                                              nurse_row.nurse)
                              if nurse_row is not None else None),
            "primary_nurse_staff_id": (nurse_row.nurse
                                       if nurse_row is not None else None),
        }
        if via_coverage is not None:
            entry["coverage"] = True
            entry["via_coverage"] = via_coverage
        return entry

    active = _active_assignments(session, team_id=team_id)
    refs = [a.patient_ref for a in active]
    names = people._patient_names(session, refs)
    patients = [patient_entry(a, names) for a in active]
    seen_assignments = {a.assignment_id for a in active}

    now = _now()
    coverage_rows = session.scalars(select(CoverageDelegation).where(
        CoverageDelegation.status == "active")).all()
    # coverage_in: delegations under which THIS team (or one of its
    # members) is the covered side — our patients are being covered,
    # so they are flagged coverage: true / via_coverage in place.
    # coverage_out: coverage this team/staff GIVE — and because the
    # covering side works the covered side's patients, a covered
    # TEAM's active primary patients are merged into this panel,
    # flagged via_coverage, so the covering team sees its full
    # working list in one place.
    coverage_in, coverage_out = [], []
    for row in coverage_rows:
        if not _is_current(row, now):
            continue
        covered_is_us = row.covered_team_id == team_id or (
            row.covered_staff_id is not None
            and row.covered_staff_id in member_ids)
        given_by_us = row.covering_team_id == team_id or (
            row.covering_staff_id is not None
            and row.covering_staff_id in member_ids)
        if covered_is_us:
            coverage_in.append(_coverage_out(row))
            for entry in patients:
                if "via_coverage" not in entry:
                    entry["coverage"] = True
                    entry["via_coverage"] = row.coverage_id
        if given_by_us:
            coverage_out.append(_coverage_out(row))
            if row.covered_team_id and row.covered_team_id != team_id:
                covered_active = _active_assignments(
                    session, team_id=row.covered_team_id, role="primary")
                extra_refs = [a.patient_ref for a in covered_active
                              if a.assignment_id not in seen_assignments]
                names.update(people._patient_names(session, extra_refs))
                for covered in covered_active:
                    if covered.assignment_id in seen_assignments:
                        continue
                    seen_assignments.add(covered.assignment_id)
                    patients.append(patient_entry(
                        covered, names, via_coverage=row.coverage_id))

    requested = session.scalars(select(TeamPatientAssignment).where(
        TeamPatientAssignment.team_id == team_id,
        TeamPatientAssignment.state == "requested")
        .order_by(TeamPatientAssignment.created_at.asc())).all()
    pending_names = people._patient_names(
        session, [a.patient_ref for a in requested])
    pending = [{"assignment_id": a.assignment_id,
                "patient_ref": a.patient_ref,
                "patient_name": pending_names.get(a.patient_ref),
                "role": a.role, "handoff": a.handoff,
                "requested_by": a.requested_by,
                "created_at": _iso(a.created_at)} for a in requested]
    if team.unit:
        unassigned = _unassigned_items(session, unit=team.unit)
    elif team.service_line:
        unassigned = _unassigned_items(session,
                                       service_line=team.service_line)
    else:
        unassigned = []
    todays = [{"patient_ref": p["patient_ref"],
               "encounter_id": p["encounter_id"],
               "attending_staff_id": p["attending_staff_id"],
               "attending_name": p["attending_name"],
               "primary_nurse": p["primary_nurse"],
               "primary_nurse_staff_id": p["primary_nurse_staff_id"]}
              for p in patients]
    return {"team": _team_out(session, team), "members": members,
            "patients": patients, "pending_inbound": pending,
            "unassigned": unassigned, "coverage_in": coverage_in,
            "coverage_out": coverage_out,
            "todays_assignments": todays}


# ---------------------------------------------------------------------------
# Seeds: the three terminology documents, privilege grants, teams +
# memberships, empanelments, the CMS privileging register entry
# ---------------------------------------------------------------------------

_SEED_GRANTS = [
    # staff_id, privilege_code, service_line
    ("staff-001", "attending-of-record", None),
    ("staff-002", "attending-of-record", None),
    ("staff-002", "icu-practice", "critical-care"),
    ("staff-012", "attending-of-record", None),
    ("staff-015", "attending-of-record", None),
    ("staff-015", "primary-care-panel", None),
]

_SEED_TEAMS = [
    # team_id, name, team_type, unit, service_line, description,
    # [(staff_id, role_in_team)]
    ("team-medsurg-a", "Med-Surg Team A", "unit", "unit-medsurg",
     "medicine", "Medical-surgical unit team A",
     [("staff-001", "attending"), ("staff-003", "rn"),
      ("staff-018", "rn"), ("staff-009", "care-coordinator")]),
    ("team-icu", "ICU Team", "unit", "unit-icu", "critical-care",
     "Intensive care unit team",
     [("staff-002", "attending"), ("staff-005", "rn"),
      ("staff-013", "rn")]),
    ("team-ed", "ED Team", "unit", "ed", "emergency",
     "Emergency department team",
     [("staff-012", "attending"), ("staff-006", "rn")]),
    ("team-clinic-pc", "Clinic Primary Care Team", "primary-care",
     "clinic", "medicine", "Clinic primary care team",
     [("staff-015", "pcp"), ("staff-019", "care-coordinator")]),
    ("team-cardiology-consult", "Cardiology Consult Service",
     "consult-service", None, "medicine", "Cardiology consult service",
     [("staff-001", "member")]),
]


def ensure_teams_defaults(session: Session) -> None:
    """Seed the teams pass: the job-catalogue / privileges / teams
    terminology documents (published v1, through governance), the
    synthetic privilege grants, teams + memberships and primary-care
    empanelments, and the CMS medical-staff register entry. All
    idempotent; empanelments seed only for demo patients that exist
    (skipped silently otherwise)."""
    people._seed_published(session, "terminology",
                           privileging.JOB_CATALOGUE_KEY,
                           privileging.default_job_catalogue_payload())
    people._seed_published(session, "terminology",
                           privileging.PRIVILEGES_KEY,
                           privileging.default_privileges_payload())
    people._seed_published(session, "terminology", privileging.TEAMS_KEY,
                           privileging.default_teams_payload())
    staff_ids = {s.staff_id for s in
                 session.scalars(select(StaffMember)).all()}
    added = {"grants": 0, "teams": 0, "memberships": 0, "empanelments": 0}
    for staff_id, code, line in _SEED_GRANTS:
        if staff_id not in staff_ids:
            continue
        dupe = session.scalars(select(StaffPrivilege).where(
            StaffPrivilege.staff_id == staff_id,
            StaffPrivilege.privilege_code == code,
            StaffPrivilege.service_line.is_(line) if line is None
            else StaffPrivilege.service_line == line,
            StaffPrivilege.status == "active")).first()
        if dupe is not None:
            continue
        session.add(StaffPrivilege(
            grant_id=f"grant-seed-{staff_id}-{code}",
            staff_id=staff_id, privilege_code=code, service_line=line,
            status="active", granted_by="system", granted_at=_now()))
        added["grants"] += 1
    session.flush()
    for team_id, name, team_type, unit, line, description, members \
            in _SEED_TEAMS:
        if session.scalars(select(CareTeam).where(
                CareTeam.team_id == team_id)).first() is None:
            session.add(CareTeam(team_id=team_id, name=name,
                                 team_type=team_type, unit=unit,
                                 service_line=line,
                                 description=description, status="active"))
            added["teams"] += 1
        session.flush()
        for staff_id, role_in_team in members:
            if staff_id not in staff_ids:
                continue
            dupe = session.scalars(select(TeamMembership).where(
                TeamMembership.team_id == team_id,
                TeamMembership.staff_id == staff_id,
                TeamMembership.status == "active")).first()
            if dupe is not None:
                continue
            session.add(TeamMembership(
                membership_id=f"tmem-{team_id}-{staff_id}",
                team_id=team_id, staff_id=staff_id,
                role_in_team=role_in_team, status="active",
                created_by="system"))
            added["memberships"] += 1
    session.flush()
    if session.scalars(select(CareTeam).where(
            CareTeam.team_id == "team-clinic-pc")).first() is not None:
        for index in range(1, 7):
            patient_ref = f"Patient/pat-{index}"
            if _patient_row(session, patient_ref) is None:
                continue
            dupe = session.scalars(select(Empanelment).where(
                Empanelment.patient_ref == patient_ref,
                Empanelment.status == "active")).first()
            if dupe is not None:
                continue
            session.add(Empanelment(
                empanelment_id=f"emp-seed-pat-{index}",
                patient_ref=patient_ref, pcp_staff_id="staff-015",
                team_id="team-clinic-pc", status="active",
                assigned_by="system"))
            added["empanelments"] += 1
    session.flush()
    existing = {(r.body, r.citation) for r in
                session.scalars(select(ComplianceRequirement)).all()}
    compliance_added = 0
    if ("CMS", "42 CFR 482.22") not in existing:
        session.add(ComplianceRequirement(
            body="CMS", citation="42 CFR 482.22",
            title="Medical staff — credentialing, privileging and appraisal",
            control=("An organised medical staff governs care: "
                     "privilege grants are recorded per clinician and "
                     "grounded in verified credentials (the published "
                     "privileges config names the eligible roles and "
                     "required credentials per privilege); assignment "
                     "gates (attending membership, empanelment) check "
                     "the ACTIVE grants and the latest credential "
                     "expiry on every assignment and refuse "
                     "unprivileged or expired-credential clinicians "
                     "with the reason named; every refusal is written "
                     "to the hash-chained audit as appraisal evidence, "
                     "and revocations are state changes that keep the "
                     "full grant history."),
            implementation_ref=("backend/app/privileging.py; "
                                "backend/app/teams.py; "
                                "backend/app/onboarding.py"),
            evidence_source="audit:teams.", created_by="system"))
        compliance_added = 1
    session.flush()
    if any(added.values()) or compliance_added:
        append_audit(session, actor="system", action="teams.seed",
                     entity="teams",
                     payload={**added,
                              "compliance_added": compliance_added,
                              "synthetic": True})
    session.commit()
