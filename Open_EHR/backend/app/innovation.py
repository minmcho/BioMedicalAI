"""Digital-health innovation pipeline — Phase 16, WS-C4 (BD-43).

A CDHI-class intake -> pilot -> evaluation -> decision pipeline for
candidate digital capabilities, built ON the existing governed
lifecycles (Studio packs: install -> approve -> attach -> enable in
app.studio; Studio agents: draft -> publish -> enable in app.agents)
rather than beside them:

- **Intake register** (`InnovationIntake`): a proposed capability
  (type pack | agent | config, plus its governed key), who proposed
  it, and its pipeline status
  proposed -> triaged -> pilot-approved -> piloting -> evaluated ->
  decided. Triage notes are recorded on the intake.
- **Pilot** (`Pilot`): ONE governed trial of the capability, scoped
  to ONE unit and/or ONE pophealth cohort, inside a date window. A
  pilot is never a deployment: see "trial scope" below.
- **Exercise** (`POST /innovation/pilots/{id}/exercise`): exercises
  the capability IN TRIAL SCOPE ONLY.
  - agent: the agent is run through app.agents' runtime transport
    (module-level `_post_run`, so the honesty contract is identical:
    runtime unreachable / no model -> the runtime's 503 is surfaced
    verbatim and NOTHING is recorded). The run is recorded as an
    ordinary `AgentRun` (the human disposition flow applies) plus a
    pilot-scoped hash-chained audit entry. A pilot run does NOT
    require — and never performs — global agent enablement: the
    trial authorisation is the pilot itself, and the
    `AgentEnablement` state is byte-identical before and after.
  - pack: returns the pack's declared surface (manifest
    api_endpoints / modules) and its installation state WITHOUT
    installing, approving, attaching or enabling anything.
  - config: returns the published version of the named Studio config
    (key is `kind/key`) without publishing anything.
  Patient scope guard (when a patientRef is supplied): the patient
  must be shown to belong to the pilot scope — via the unit of the
  patient's ACTIVE inpatient encounter (app.continuum's admission
  unit, derived from the encounter's active location through the
  published locations config), or via pophealth cohort membership
  (published `cohort` config evaluated by app.pophealth over the
  warehouse; membership maps back through the warehouse Person's
  `source_patient_id`). If neither path can establish belonging, the
  exercise is refused 403 with the reasons, and the refusal itself
  is audited.
- **Evaluation** (`GET /innovation/pilots/{id}/evaluation`): aggregates
  the pilot's audit stream (exercise successes + refusals, the AgentRun
  dispositions of pilot runs) and carries the governed outcome payload
  a human submits via `POST .../evaluation`.
- **Decision** (`PilotDecision`): scale | kill | extend with a
  rationale, recorded + audited. THIS MODULE NEVER ENABLES A PACK OR
  AGENT GLOBALLY. A `scale` decision creates a `ScaleHandoff` record
  naming the exact governed lifecycle step a human must take in
  System Studio; `extend` re-opens the pilot (optionally with a new
  end date); `kill` closes it.

Route gating note (BD-01): app.pack_gate ships ON by default
since Phase 17 (seeded published integration/pack-gating
{"enabled": true}; an operator publishes {"enabled": false} to
turn it off); nothing here depends on it, and no endpoint declared
by a pilot pack is served by this module.

Studio config surface: published `integration/innovation`
{requireOutcomeBeforeDecision, maxPilotDays, decisions} — seeded as
published v1 by `ensure_innovation_defaults`, editable/republishable
through the ordinary Studio config lifecycle. Capability:
`innovation.manage` gates every write (and, this being a governance
register, the reads too).
"""
import hashlib
import uuid
from datetime import date, datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (DateTime, ForeignKey, Integer, JSON, String, Text,
                        select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import agents as agents_module
from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import (AgentRun, AuditLog, ConfigStatus, ModulePack,
                     PackInstallation, StudioConfig, User, utcnow)
from .security import get_db, require_capability

router = APIRouter(prefix="/innovation", tags=["innovation"])

INNOVATION_KEY = "innovation"       # integration/innovation
CAPABILITY = "innovation.manage"

CAPABILITY_TYPES = ("pack", "agent", "config")
INTAKE_STATUSES = ("proposed", "triaged", "pilot-approved", "piloting",
                   "evaluated", "decided")
DECISIONS = ("scale", "kill", "extend")

# The governed lifecycle step a human must take to scale each
# capability type. The innovation module performs NONE of these.
HANDOFF_STEPS = {
    "agent": ("System Studio -> Agents: publish the agent definition "
              "if unpublished, then enable agent '{key}' "
              "(POST /agents/{key}/enable, capability studio.manage). "
              "The innovation module has not enabled it."),
    "pack": ("System Studio -> Module packs: install pack '{key}', "
             "then approve -> attach -> enable its installation "
             "(POST /studio/packs/{key}/install and "
             "/studio/installations/{{id}}/approve|attach|enable, "
             "capability studio.manage). The innovation module has "
             "not installed or enabled it."),
    "config": ("System Studio -> Configuration: publish config "
               "'{key}' through the draft -> publish lifecycle "
               "(capability studio.manage). The innovation module "
               "has not published it."),
}


# ---------------------------------------------------------------------------
# This module's own tables
# ---------------------------------------------------------------------------

class InnovationIntake(Base):
    """One candidate digital capability in the intake register."""
    __tablename__ = "innovation_intakes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    capability_type: Mapped[str] = mapped_column(String(20), nullable=False,
                                                 index=True)
    capability_key: Mapped[str] = mapped_column(String(200), nullable=False,
                                                index=True)
    proposer: Mapped[str] = mapped_column(String(120), nullable=False,
                                          default="")
    status: Mapped[str] = mapped_column(String(30), nullable=False,
                                        default="proposed", index=True)
    triage_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    triaged_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


class Pilot(Base):
    """One governed trial of one intake's capability, in trial scope
    only: scope = {"unit": <unit code>, "cohortKey": <published cohort
    key>} (either or both), inside [starts_on, ends_on]."""
    __tablename__ = "innovation_pilots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    intake_id: Mapped[int] = mapped_column(
        ForeignKey("innovation_intakes.id"), nullable=False, index=True)
    scope: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    starts_on: Mapped[str] = mapped_column(String(10), nullable=False)
    ends_on: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False,
                                        default="piloting", index=True)
    outcome_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    outcome_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    outcome_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


class PilotDecision(Base):
    """The scale | kill | extend decision artefact for one pilot."""
    __tablename__ = "innovation_pilot_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pilot_id: Mapped[int] = mapped_column(
        ForeignKey("innovation_pilots.id"), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decided_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class ScaleHandoff(Base):
    """Audited hand-off record created by a `scale` decision: the
    governed lifecycle step a HUMAN must take to scale the capability.
    Status stays `pending-human-action` — this module never performs
    the step, so nothing here can flip to done by itself."""
    __tablename__ = "innovation_scale_handoffs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pilot_id: Mapped[int] = mapped_column(
        ForeignKey("innovation_pilots.id"), nullable=False, index=True)
    decision_id: Mapped[int] = mapped_column(
        ForeignKey("innovation_pilot_decisions.id"), nullable=False)
    capability_type: Mapped[str] = mapped_column(String(20), nullable=False)
    capability_key: Mapped[str] = mapped_column(String(200), nullable=False)
    required_step: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False,
                                        default="pending-human-action")
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


# ---------------------------------------------------------------------------
# Published config: integration/innovation
# ---------------------------------------------------------------------------

def default_innovation_payload() -> dict:
    return {
        "name": "Digital-health innovation pipeline (synthetic defaults)",
        "description": ("Governance parameters for the BD-43 innovation "
                        "pipeline: a scale/kill/extend decision requires "
                        "a submitted evaluation outcome, and a pilot may "
                        "run at most maxPilotDays."),
        "requireOutcomeBeforeDecision": True,
        "maxPilotDays": 90,
        "decisions": list(DECISIONS),
    }


def _innovation_config(session: Session) -> dict:
    cfg = published_config(session, "integration", INNOVATION_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    return payload if payload else default_innovation_payload()


def _seed_published(session: Session, kind: str, key: str,
                    payload: dict) -> None:
    if list_config_versions(session, kind, key):
        return
    row = StudioConfig(kind=kind, key=key, version=1,
                       status=ConfigStatus.published, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    for action in ("studio.config.create", "studio.config.publish"):
        append_audit(session, actor="system", action=action,
                     entity=f"config:{kind}/{key}@v1",
                     payload={"kind": kind, "key": key, "version": 1,
                              "seeded_default": True})


def ensure_innovation_defaults(session: Session) -> None:
    """Seed the published integration/innovation governance config
    (idempotent — existing versions are the truth)."""
    _seed_published(session, "integration", INNOVATION_KEY,
                    default_innovation_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> date:
    return _now().date()


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


def _parse_date(value, field: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be an ISO date (YYYY-MM-DD)")


def _intake_out(row: InnovationIntake) -> dict:
    return {"id": row.id, "title": row.title, "description": row.description,
            "capability_type": row.capability_type,
            "capability_key": row.capability_key, "proposer": row.proposer,
            "status": row.status, "triage_notes": row.triage_notes,
            "triaged_by": row.triaged_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def _pilot_out(session: Session, row: Pilot) -> dict:
    intake = session.get(InnovationIntake, row.intake_id)
    return {"id": row.id, "intake_id": row.intake_id,
            "capability_type": intake.capability_type if intake else None,
            "capability_key": intake.capability_key if intake else None,
            "scope": row.scope or {},
            "starts_on": row.starts_on, "ends_on": row.ends_on,
            "status": row.status,
            "outcome": row.outcome_payload,
            "outcome_by": row.outcome_by,
            "outcome_at": row.outcome_at.isoformat() if row.outcome_at else None,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None}


def _decision_out(row: PilotDecision) -> dict:
    return {"id": row.id, "pilot_id": row.pilot_id, "decision": row.decision,
            "rationale": row.rationale, "decided_by": row.decided_by,
            "created_at": row.created_at.isoformat() if row.created_at else None}


def _handoff_out(row: ScaleHandoff) -> dict:
    return {"id": row.id, "pilot_id": row.pilot_id,
            "decision_id": row.decision_id,
            "capability_type": row.capability_type,
            "capability_key": row.capability_key,
            "required_step": row.required_step, "status": row.status,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None}


def _get_intake(session: Session, intake_id: int) -> InnovationIntake:
    row = session.get(InnovationIntake, intake_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Innovation intake {intake_id} not found")
    return row


def _get_pilot(session: Session, pilot_id: int) -> Pilot:
    row = session.get(Pilot, pilot_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Innovation pilot {pilot_id} not found")
    return row


def _transition_intake(session: Session, intake: InnovationIntake,
                       user: User, action: str, required: str,
                       target: str) -> None:
    if intake.status != required:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot {action} an intake in status "
                   f"'{intake.status}'; requires '{required}'")
    previous = intake.status
    intake.status = target
    session.flush()
    append_audit(session, actor=user.username,
                 action=f"innovation.intake.{action}",
                 entity=f"innovation-intake:{intake.id}",
                 payload={"from_status": previous, "to_status": target,
                          "capability_type": intake.capability_type,
                          "capability_key": intake.capability_key})


# ---------------------------------------------------------------------------
# Patient scope guard
# ---------------------------------------------------------------------------

def _patient_unit(session: Session, patient_ref: str) -> tuple[str | None, str]:
    """The unit of the patient's ACTIVE inpatient encounter (via
    app.continuum's admission-unit derivation over the encounter's
    active location), plus a human-readable basis/reason."""
    from . import continuum, inpatient
    bed_units = continuum._bed_unit_map(session)
    for row in continuum._active_admissions(session):
        if inpatient._subject_ref(row.body or {}) == patient_ref:
            unit = continuum._admission_unit(session, row.body or {},
                                             bed_units)
            if unit:
                return unit, f"active encounter unit is '{unit}'"
            return None, ("patient has an active inpatient encounter "
                          "but its location maps to no unit")
    return None, "patient has no active inpatient encounter"


def _patient_in_cohort(session: Session, cohort_key: str,
                       patient_ref: str) -> tuple[bool, str]:
    """Pophealth cohort membership: the published cohort definition is
    evaluated by app.pophealth over the warehouse; membership maps
    back to the FHIR patient via Person.source_patient_id."""
    from . import pophealth, warehouse as wh
    cfg = published_config(session, "cohort", cohort_key)
    if cfg is None:
        return False, f"no published cohort config '{cohort_key}'"
    patient_id = patient_ref.split("/", 1)[1] if "/" in patient_ref \
        else patient_ref
    try:
        factory = wh.warehouse_session_factory()
        with factory() as wh_session:
            wh.ensure_concept_map(wh_session)
            members = pophealth._members(wh_session, cfg.payload or {})
    except Exception as exc:  # warehouse unavailable: cannot establish
        return False, f"cohort membership could not be evaluated ({exc})"
    member_ids = {p.source_patient_id for p in members}
    if patient_id in member_ids:
        return True, f"patient is a member of cohort '{cohort_key}'"
    return False, f"patient is not a member of cohort '{cohort_key}'"


def _assert_patient_in_scope(session: Session, pilot: Pilot,
                             patient_ref: str) -> str:
    """Return the scope basis that established belonging, or raise
    403 with every reason belonging could not be established."""
    scope = pilot.scope or {}
    unit = scope.get("unit")
    cohort_key = scope.get("cohortKey")
    if not unit and not cohort_key:
        raise HTTPException(
            status_code=403,
            detail=("Pilot scope defines neither a unit nor a cohortKey, "
                    "so patient scope cannot be established for "
                    f"{patient_ref}; exercise refused"))
    reasons: list[str] = []
    if unit:
        patient_unit, basis = _patient_unit(session, patient_ref)
        if patient_unit == unit:
            return f"unit:{unit} ({basis})"
        reasons.append(
            f"unit scope '{unit}': {basis}" if patient_unit is None
            else f"unit scope '{unit}': active encounter unit is "
                 f"'{patient_unit}'")
    if cohort_key:
        member, basis = _patient_in_cohort(session, cohort_key, patient_ref)
        if member:
            return f"cohort:{cohort_key} ({basis})"
        reasons.append(f"cohort scope '{cohort_key}': {basis}")
    raise HTTPException(
        status_code=403,
        detail=(f"Patient {patient_ref} is outside this pilot's trial "
                "scope — " + "; ".join(reasons)))


def _refuse_exercise(session: Session, user: User, pilot: Pilot,
                     intake: InnovationIntake, patient_ref: str | None,
                     reason: str) -> None:
    """Audit a refused exercise, commit the audit entry, then raise
    403. (The audit must survive the refusal, so it is committed
    before the exception unwinds the request transaction.)"""
    append_audit(session, actor=user.username,
                 action="innovation.pilot.exercise.refused",
                 entity=f"innovation-pilot:{pilot.id}",
                 payload={"pilot_id": pilot.id, "intake_id": intake.id,
                          "capability_type": intake.capability_type,
                          "capability_key": intake.capability_key,
                          "patient": patient_ref, "reason": reason})
    session.commit()
    raise HTTPException(status_code=403, detail=reason)


# ---------------------------------------------------------------------------
# Intake register
# ---------------------------------------------------------------------------

@router.post("/intakes", status_code=201)
async def create_intake(request: Request, session: Session = Depends(get_db),
                        user: User = Depends(require_capability(CAPABILITY))):
    body = await _json_body(request)
    title = _require_str(body, "title")
    capability_type = _require_str(body, "capabilityType")
    if capability_type not in CAPABILITY_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"'capabilityType' must be one of {list(CAPABILITY_TYPES)}")
    capability_key = _require_str(body, "capabilityKey")
    description = body.get("description") or ""
    if not isinstance(description, str):
        raise HTTPException(status_code=422,
                            detail="'description' must be a string")
    proposer = body.get("proposer") or user.username
    if not isinstance(proposer, str) or not proposer.strip():
        raise HTTPException(status_code=422,
                            detail="'proposer' must be a non-empty string")
    row = InnovationIntake(title=title, description=description,
                           capability_type=capability_type,
                           capability_key=capability_key,
                           proposer=proposer.strip(), status="proposed")
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="innovation.intake.create",
                 entity=f"innovation-intake:{row.id}",
                 payload={"title": title, "capability_type": capability_type,
                          "capability_key": capability_key,
                          "proposer": row.proposer})
    session.commit()
    return _intake_out(row)


@router.get("/intakes")
def list_intakes(status: str | None = Query(default=None),
                 session: Session = Depends(get_db),
                 user: User = Depends(require_capability(CAPABILITY))):
    stmt = select(InnovationIntake).order_by(InnovationIntake.id.asc())
    if status:
        stmt = stmt.where(InnovationIntake.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_intake_out(r) for r in rows], "total": len(rows)}


@router.get("/intakes/{intake_id}")
def get_intake(intake_id: int, session: Session = Depends(get_db),
               user: User = Depends(require_capability(CAPABILITY))):
    row = _get_intake(session, intake_id)
    pilots = session.scalars(
        select(Pilot).where(Pilot.intake_id == intake_id)
        .order_by(Pilot.id.asc())).all()
    out = _intake_out(row)
    out["pilots"] = [_pilot_out(session, p) for p in pilots]
    return out


@router.post("/intakes/{intake_id}/triage")
async def triage_intake(intake_id: int, request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(require_capability(CAPABILITY))):
    """Triage a proposed intake: records the triage notes and moves
    proposed -> triaged."""
    intake = _get_intake(session, intake_id)
    body = await _json_body(request)
    notes = body.get("notes", body.get("triageNotes", ""))
    if not isinstance(notes, str):
        raise HTTPException(status_code=422, detail="'notes' must be a string")
    _transition_intake(session, intake, user, "triage", "proposed", "triaged")
    intake.triage_notes = notes
    intake.triaged_by = user.username
    session.flush()
    append_audit(session, actor=user.username,
                 action="innovation.intake.triage.notes",
                 entity=f"innovation-intake:{intake.id}",
                 payload={"notes_recorded": bool(notes)})
    session.commit()
    return _intake_out(intake)


@router.post("/intakes/{intake_id}/approve-pilot")
def approve_pilot(intake_id: int, session: Session = Depends(get_db),
                  user: User = Depends(require_capability(CAPABILITY))):
    """Approve a triaged intake for a pilot (triaged ->
    pilot-approved). Approval authorises CREATING a pilot; it does
    not start one and enables nothing."""
    intake = _get_intake(session, intake_id)
    _transition_intake(session, intake, user, "approve-pilot", "triaged",
                       "pilot-approved")
    session.commit()
    return _intake_out(intake)


# ---------------------------------------------------------------------------
# Pilots
# ---------------------------------------------------------------------------

@router.post("/pilots", status_code=201)
async def create_pilot(request: Request, session: Session = Depends(get_db),
                       user: User = Depends(require_capability(CAPABILITY))):
    """Scope a pilot for a pilot-approved intake: ONE unit and/or ONE
    cohort, inside a date window bounded by the published
    integration/innovation maxPilotDays. Creating the pilot moves the
    intake to `piloting` and the pilot starts in `piloting` — trial
    scope only; nothing is enabled deployment-wide."""
    body = await _json_body(request)
    intake_id = body.get("intakeId", body.get("intake_id"))
    if isinstance(intake_id, bool) or not isinstance(intake_id, int):
        raise HTTPException(status_code=422,
                            detail="'intakeId' is required (integer)")
    intake = _get_intake(session, intake_id)
    if intake.status != "pilot-approved":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot scope a pilot for an intake in status "
                   f"'{intake.status}'; requires 'pilot-approved'")
    scope_in = body.get("scope") if isinstance(body.get("scope"), dict) else {}
    unit = scope_in.get("unit", body.get("unit"))
    cohort_key = scope_in.get("cohortKey", body.get("cohortKey",
                                                    body.get("cohort_key")))
    if unit is not None and (not isinstance(unit, str) or not unit.strip()):
        raise HTTPException(status_code=422,
                            detail="'unit' must be a non-empty string")
    if cohort_key is not None and (not isinstance(cohort_key, str)
                                   or not cohort_key.strip()):
        raise HTTPException(status_code=422,
                            detail="'cohortKey' must be a non-empty string")
    if not unit and not cohort_key:
        raise HTTPException(
            status_code=422,
            detail="Pilot scope requires at least one of 'unit' or "
                   "'cohortKey' (a pilot is one unit and/or one cohort)")
    if cohort_key and published_config(session, "cohort", cohort_key) is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown cohortKey '{cohort_key}': no published cohort "
                   "config with that key")
    starts = _parse_date(body.get("startsOn", body.get("starts_on")),
                         "startsOn")
    ends = _parse_date(body.get("endsOn", body.get("ends_on")), "endsOn")
    if ends < starts:
        raise HTTPException(status_code=422,
                            detail="'endsOn' must not be before 'startsOn'")
    config = _innovation_config(session)
    max_days = config.get("maxPilotDays")
    if isinstance(max_days, int) and not isinstance(max_days, bool) \
            and max_days > 0 and (ends - starts).days + 1 > max_days:
        raise HTTPException(
            status_code=422,
            detail=f"Pilot window of {(ends - starts).days + 1} days "
                   f"exceeds the published maxPilotDays ({max_days})")
    scope = {}
    if unit:
        scope["unit"] = unit.strip()
    if cohort_key:
        scope["cohortKey"] = cohort_key.strip()
    pilot = Pilot(intake_id=intake.id, scope=scope,
                  starts_on=starts.isoformat(), ends_on=ends.isoformat(),
                  status="piloting", created_by=user.username)
    session.add(pilot)
    intake.status = "piloting"
    session.flush()
    append_audit(session, actor=user.username, action="innovation.pilot.create",
                 entity=f"innovation-pilot:{pilot.id}",
                 payload={"intake_id": intake.id, "scope": scope,
                          "starts_on": pilot.starts_on,
                          "ends_on": pilot.ends_on,
                          "capability_type": intake.capability_type,
                          "capability_key": intake.capability_key})
    session.commit()
    return _pilot_out(session, pilot)


@router.get("/pilots")
def list_pilots(session: Session = Depends(get_db),
                user: User = Depends(require_capability(CAPABILITY))):
    rows = session.scalars(select(Pilot).order_by(Pilot.id.asc())).all()
    return {"items": [_pilot_out(session, r) for r in rows],
            "total": len(rows)}


@router.get("/pilots/{pilot_id}")
def get_pilot(pilot_id: int, session: Session = Depends(get_db),
              user: User = Depends(require_capability(CAPABILITY))):
    pilot = _get_pilot(session, pilot_id)
    out = _pilot_out(session, pilot)
    decisions = session.scalars(
        select(PilotDecision).where(PilotDecision.pilot_id == pilot_id)
        .order_by(PilotDecision.id.asc())).all()
    out["decisions"] = [_decision_out(d) for d in decisions]
    handoffs = session.scalars(
        select(ScaleHandoff).where(ScaleHandoff.pilot_id == pilot_id)
        .order_by(ScaleHandoff.id.asc())).all()
    out["handoffs"] = [_handoff_out(h) for h in handoffs]
    return out


# ---------------------------------------------------------------------------
# Exercise — trial scope only
# ---------------------------------------------------------------------------

def _exercise_agent(session: Session, request: Request, user: User,
                    pilot: Pilot, intake: InnovationIntake,
                    patient_ref: str | None, prompt: str,
                    scope_basis: str) -> dict:
    """Run the pilot's agent through app.agents' runtime transport.
    Trial authorisation comes from the PILOT, not from global
    enablement: the AgentEnablement row is read (for the report) but
    never written here."""
    key = intake.capability_key
    cfg = published_config(session, "agent", key)
    if cfg is None:
        raise HTTPException(status_code=404,
                            detail=f"No published agent config for '{key}'")
    globally_enabled = agents_module._is_enabled(session, key)
    # C-AI runtime gate (AI-G4): even a pilot-authorised exercise
    # refuses when governance blocks the agent (expired record or
    # statement, suspension, active kill switch) — 409 naming the
    # gate's reason. The pilot authorises the trial scope; it does
    # not override AI governance.
    _refusal = agents_module.runtime_refusal(session, key)
    if _refusal:
        raise HTTPException(status_code=409, detail=_refusal)
    payload = dict(cfg.payload or {})
    runtime_payload = {
        "agent": {**payload, "key": key},
        "prompt": prompt,
        "apiBaseUrl": agents_module.api_base_url(),
        "apiToken": request.headers.get("Authorization", "")
        .removeprefix("Bearer ").strip(),
    }
    try:
        status, result = agents_module._post_run(runtime_payload)
    except httpx.HTTPError:
        raise HTTPException(
            status_code=503,
            detail=(f"Agent runtime unavailable at "
                    f"{agents_module.runtime_url()} — start the ADK "
                    "service (backend/adk) or set ADK_RUNTIME_URL. "
                    "No answer was generated."))
    if status == 503:
        raise HTTPException(status_code=503,
                            detail=result.get("error")
                            or "Agent runtime unavailable")
    if status != 200:
        raise HTTPException(
            status_code=status if status in (400, 422) else 502,
            detail=result.get("error") or f"Agent runtime returned {status}")
    output = str(result.get("output") or "")
    input_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    run = AgentRun(run_id=str(uuid.uuid4()), agent_key=key,
                   agent_version=cfg.version, actor=user.username,
                   patient_ref=patient_ref, input_hash=input_hash,
                   output=output, model=result.get("model"))
    session.add(run)
    session.flush()
    append_audit(session, actor=user.username,
                 action="innovation.pilot.exercise",
                 entity=f"innovation-pilot:{pilot.id}",
                 payload={"pilot_id": pilot.id, "intake_id": intake.id,
                          "capability_type": "agent",
                          "capability_key": key,
                          "patient": patient_ref,
                          "scope_basis": scope_basis,
                          "run_id": run.run_id,
                          "agent_version": cfg.version,
                          "model": result.get("model"),
                          "disposition": "pending",
                          "globally_enabled": globally_enabled,
                          "global_enablement_changed": False})
    session.commit()
    return {"pilot_id": pilot.id, "capability_type": "agent",
            "capability_key": key, "exercised": True,
            "scope_basis": scope_basis, "patient_ref": patient_ref,
            "run": {"run_id": run.run_id, "agent": key,
                    "version": cfg.version, "output": output,
                    "model": result.get("model"),
                    "disposition": "pending"},
            "globally_enabled": globally_enabled,
            "global_enablement_changed": False}


def _exercise_pack(session: Session, user: User, pilot: Pilot,
                   intake: InnovationIntake, patient_ref: str | None,
                   scope_basis: str) -> dict:
    """Report the pack's declared surface + installation state.
    Read-only by construction: no install/approve/attach/enable call
    exists on this path."""
    key = intake.capability_key
    pack = session.scalars(
        select(ModulePack).where(ModulePack.name == key)
        .order_by(ModulePack.id.desc())).first()
    if pack is None:
        raise HTTPException(
            status_code=404,
            detail=f"No module pack named '{key}' (packs are loaded "
                   "from manifests / authored in Studio)")
    manifest = pack.manifest or {}
    installations = session.scalars(
        select(PackInstallation)
        .where(PackInstallation.pack_id == pack.id)
        .order_by(PackInstallation.id.asc())).all()
    statuses = [i.status.value for i in installations]
    installation_state = ("enabled" if "enabled" in statuses
                          else statuses[-1] if statuses
                          else "not-installed")
    surface = {
        "api_endpoints": manifest.get("api_endpoints") or [],
        "modules": manifest.get("modules") or [],
        "description": manifest.get("description"),
    }
    append_audit(session, actor=user.username,
                 action="innovation.pilot.exercise",
                 entity=f"innovation-pilot:{pilot.id}",
                 payload={"pilot_id": pilot.id, "intake_id": intake.id,
                          "capability_type": "pack",
                          "capability_key": key,
                          "patient": patient_ref,
                          "scope_basis": scope_basis,
                          "installation_state": installation_state,
                          "global_enablement_changed": False})
    session.commit()
    return {"pilot_id": pilot.id, "capability_type": "pack",
            "capability_key": key, "exercised": True,
            "scope_basis": scope_basis, "patient_ref": patient_ref,
            "pack": {"name": pack.name, "version": pack.version},
            "declared_surface": surface,
            "installation_state": installation_state,
            "installations": statuses,
            "globally_enabled": installation_state == "enabled",
            "global_enablement_changed": False}


def _exercise_config(session: Session, user: User, pilot: Pilot,
                     intake: InnovationIntake, patient_ref: str | None,
                     scope_basis: str) -> dict:
    """Report the published Studio config named by the capability key
    (`kind/key`). Read-only: nothing is published here."""
    key = intake.capability_key
    if "/" not in key:
        raise HTTPException(
            status_code=422,
            detail="Config capabilityKey must be 'kind/key' "
                   f"(got '{key}')")
    kind, config_key = key.split("/", 1)
    cfg = published_config(session, kind, config_key)
    if cfg is None:
        raise HTTPException(
            status_code=404,
            detail=f"No published config for '{key}'")
    append_audit(session, actor=user.username,
                 action="innovation.pilot.exercise",
                 entity=f"innovation-pilot:{pilot.id}",
                 payload={"pilot_id": pilot.id, "intake_id": intake.id,
                          "capability_type": "config",
                          "capability_key": key,
                          "patient": patient_ref,
                          "scope_basis": scope_basis,
                          "config_version": cfg.version,
                          "global_enablement_changed": False})
    session.commit()
    return {"pilot_id": pilot.id, "capability_type": "config",
            "capability_key": key, "exercised": True,
            "scope_basis": scope_basis, "patient_ref": patient_ref,
            "config": {"kind": kind, "key": config_key,
                       "version": cfg.version, "status": cfg.status.value,
                       "payload": cfg.payload},
            "global_enablement_changed": False}


@router.post("/pilots/{pilot_id}/exercise")
async def exercise_pilot(pilot_id: int, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability(CAPABILITY))):
    """Exercise the pilot's capability IN TRIAL SCOPE ONLY.

    Body: {"patientRef"?, "input"} — `input` is the prompt string for
    an agent capability (or {"prompt": ...}); for pack/config
    capabilities it is accepted and ignored beyond the audit scope
    record. A supplied patientRef must belong to the pilot scope
    (unit via active encounter, cohort via pophealth membership);
    if belonging cannot be established the exercise is refused 403
    with the reasons, and the refusal is audited."""
    pilot = _get_pilot(session, pilot_id)
    intake = _get_intake(session, pilot.intake_id)
    body = await _json_body(request)
    if pilot.status != "piloting":
        raise HTTPException(
            status_code=409,
            detail=f"Pilot {pilot_id} is in status '{pilot.status}'; "
                   "only a piloting pilot can be exercised")
    today = _today()
    if today < date.fromisoformat(pilot.starts_on) \
            or today > date.fromisoformat(pilot.ends_on):
        _refuse_exercise(
            session, user, pilot, intake, body.get("patientRef"),
            f"Pilot {pilot_id} trial window is {pilot.starts_on} to "
            f"{pilot.ends_on}; today ({today.isoformat()}) is outside it")
    patient_ref = body.get("patientRef", body.get("patient_ref"))
    if patient_ref is not None and (not isinstance(patient_ref, str)
                                    or not patient_ref.strip()):
        raise HTTPException(status_code=422,
                            detail="'patientRef' must be a non-empty string")
    scope_basis = "no-patient-context"
    if patient_ref:
        patient_ref = patient_ref.strip()
        try:
            scope_basis = _assert_patient_in_scope(session, pilot,
                                                   patient_ref)
        except HTTPException as exc:
            _refuse_exercise(session, user, pilot, intake, patient_ref,
                             str(exc.detail))
    raw_input = body.get("input", body.get("prompt"))
    if isinstance(raw_input, dict):
        prompt = raw_input.get("prompt")
        if not isinstance(prompt, str):
            prompt = None
    elif isinstance(raw_input, str):
        prompt = raw_input
    else:
        prompt = None
    if intake.capability_type == "agent":
        if not prompt or not prompt.strip():
            raise HTTPException(
                status_code=422,
                detail="Exercising an agent capability requires a "
                       "non-empty 'input' prompt")
        return _exercise_agent(session, request, user, pilot, intake,
                               patient_ref, prompt, scope_basis)
    if intake.capability_type == "pack":
        return _exercise_pack(session, user, pilot, intake, patient_ref,
                              scope_basis)
    return _exercise_config(session, user, pilot, intake, patient_ref,
                            scope_basis)


# ---------------------------------------------------------------------------
# Evaluation + decision
# ---------------------------------------------------------------------------

def _evaluation(session: Session, pilot: Pilot) -> dict:
    intake = _get_intake(session, pilot.intake_id)
    entity = f"innovation-pilot:{pilot.id}"
    exercises = session.scalars(
        select(AuditLog).where(
            AuditLog.action == "innovation.pilot.exercise",
            AuditLog.entity == entity).order_by(AuditLog.id.asc())).all()
    refusals = session.scalars(
        select(AuditLog).where(
            AuditLog.action == "innovation.pilot.exercise.refused",
            AuditLog.entity == entity).order_by(AuditLog.id.asc())).all()
    run_ids = [(r.payload or {}).get("run_id") for r in exercises
               if (r.payload or {}).get("run_id")]
    runs = []
    if run_ids:
        runs = session.scalars(
            select(AgentRun).where(AgentRun.run_id.in_(run_ids))).all()
    dispositions: dict[str, int] = {}
    for run in runs:
        dispositions[run.disposition] = dispositions.get(run.disposition, 0) + 1
    decisions = session.scalars(
        select(PilotDecision).where(PilotDecision.pilot_id == pilot.id)
        .order_by(PilotDecision.id.asc())).all()
    handoffs = session.scalars(
        select(ScaleHandoff).where(ScaleHandoff.pilot_id == pilot.id)
        .order_by(ScaleHandoff.id.asc())).all()
    return {
        "pilot": _pilot_out(session, pilot),
        "intake": {"id": intake.id, "title": intake.title,
                   "capability_type": intake.capability_type,
                   "capability_key": intake.capability_key,
                   "status": intake.status},
        "exercises": {
            "total": len(exercises),
            "succeeded": len(exercises),
            "refused": len(refusals),
            "refusals": [{"patient": (r.payload or {}).get("patient"),
                          "reason": (r.payload or {}).get("reason"),
                          "at": r.created_at.isoformat()
                          if r.created_at else None} for r in refusals],
            "patients": sorted({(r.payload or {}).get("patient")
                                for r in exercises
                                if (r.payload or {}).get("patient")}),
        },
        "runs": {"total": len(runs), "run_ids": [r.run_id for r in runs],
                 "dispositions": dispositions,
                 "items": [{"run_id": r.run_id, "agent": r.agent_key,
                            "disposition": r.disposition,
                            "disposition_by": r.disposition_by}
                           for r in runs]},
        "outcome": pilot.outcome_payload,
        "outcome_by": pilot.outcome_by,
        "decisions": [_decision_out(d) for d in decisions],
        "handoffs": [_handoff_out(h) for h in handoffs],
    }


@router.get("/pilots/{pilot_id}/evaluation")
def get_evaluation(pilot_id: int, session: Session = Depends(get_db),
                   user: User = Depends(require_capability(CAPABILITY))):
    """Aggregate the pilot's audit stream: exercise counts (succeeded
    + refused, with refusal reasons), the dispositions of the pilot's
    agent runs, the governed outcome payload (once submitted), and
    any decision / hand-off artefacts."""
    pilot = _get_pilot(session, pilot_id)
    return _evaluation(session, pilot)


@router.post("/pilots/{pilot_id}/evaluation")
async def submit_evaluation(pilot_id: int, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability(CAPABILITY))):
    """Submit the governed outcome payload for a piloting pilot
    (e.g. {"summary": ..., "metrics": {...}, "outcomeNotes": ...}).
    Moves pilot + intake to `evaluated`."""
    pilot = _get_pilot(session, pilot_id)
    intake = _get_intake(session, pilot.intake_id)
    body = await _json_body(request)
    outcome = body.get("outcome") if isinstance(body.get("outcome"), dict) \
        else body
    if not outcome:
        raise HTTPException(status_code=422,
                            detail="Evaluation requires an outcome payload")
    if pilot.status != "piloting":
        raise HTTPException(
            status_code=409,
            detail=f"Pilot {pilot_id} is in status '{pilot.status}'; "
                   "only a piloting pilot can be evaluated")
    pilot.outcome_payload = outcome
    pilot.outcome_by = user.username
    pilot.outcome_at = _now()
    pilot.status = "evaluated"
    intake.status = "evaluated"
    session.flush()
    append_audit(session, actor=user.username,
                 action="innovation.pilot.evaluate",
                 entity=f"innovation-pilot:{pilot.id}",
                 payload={"pilot_id": pilot.id, "intake_id": intake.id,
                          "outcome_keys": sorted(outcome.keys())})
    session.commit()
    return _evaluation(session, pilot)


@router.post("/pilots/{pilot_id}/decision", status_code=201)
async def decide_pilot(pilot_id: int, request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability(CAPABILITY))):
    """Record the scale | kill | extend decision artefact.

    `scale` NEVER enables anything here: it creates a ScaleHandoff
    record naming the governed lifecycle step a human must take in
    System Studio. `extend` re-opens the pilot (optional newEndsOn);
    `kill` closes pilot + intake as decided."""
    pilot = _get_pilot(session, pilot_id)
    intake = _get_intake(session, pilot.intake_id)
    body = await _json_body(request)
    decision_value = _require_str(body, "decision")
    if decision_value not in DECISIONS:
        raise HTTPException(
            status_code=422,
            detail=f"'decision' must be one of {list(DECISIONS)}")
    rationale = _require_str(body, "rationale")
    config = _innovation_config(session)
    if pilot.status != "evaluated":
        if config.get("requireOutcomeBeforeDecision") is not False \
                or pilot.status != "piloting":
            raise HTTPException(
                status_code=409,
                detail=f"Pilot {pilot_id} is in status '{pilot.status}'; "
                       "a decision requires an evaluated pilot (submit "
                       "the evaluation outcome first)")
    row = PilotDecision(pilot_id=pilot.id, decision=decision_value,
                        rationale=rationale, decided_by=user.username)
    session.add(row)
    session.flush()
    handoff = None
    if decision_value == "scale":
        step = HANDOFF_STEPS[intake.capability_type].format(
            key=intake.capability_key)
        handoff = ScaleHandoff(
            pilot_id=pilot.id, decision_id=row.id,
            capability_type=intake.capability_type,
            capability_key=intake.capability_key, required_step=step,
            status="pending-human-action", created_by=user.username)
        session.add(handoff)
        session.flush()
        pilot.status = "decided"
        intake.status = "decided"
    elif decision_value == "kill":
        pilot.status = "decided"
        intake.status = "decided"
    else:  # extend
        new_ends = body.get("newEndsOn", body.get("new_ends_on"))
        if new_ends is not None:
            ends = _parse_date(new_ends, "newEndsOn")
            if ends < date.fromisoformat(pilot.starts_on):
                raise HTTPException(
                    status_code=422,
                    detail="'newEndsOn' must not be before the pilot start")
            pilot.ends_on = ends.isoformat()
        pilot.status = "piloting"
        intake.status = "piloting"
    session.flush()
    append_audit(session, actor=user.username,
                 action="innovation.pilot.decision",
                 entity=f"innovation-pilot:{pilot.id}",
                 payload={"pilot_id": pilot.id, "intake_id": intake.id,
                          "decision_id": row.id, "decision": decision_value,
                          "capability_type": intake.capability_type,
                          "capability_key": intake.capability_key,
                          "handoff_id": handoff.id if handoff else None})
    if handoff is not None:
        append_audit(session, actor=user.username,
                     action="innovation.handoff.created",
                     entity=f"innovation-pilot:{pilot.id}",
                     payload={"pilot_id": pilot.id, "handoff_id": handoff.id,
                              "decision_id": row.id,
                              "capability_type": intake.capability_type,
                              "capability_key": intake.capability_key,
                              "required_step": handoff.required_step,
                              "status": handoff.status})
    session.commit()
    out = _decision_out(row)
    out["pilot_status"] = pilot.status
    out["intake_status"] = intake.status
    out["handoff"] = _handoff_out(handoff) if handoff else None
    return out
