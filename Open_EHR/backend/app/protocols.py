"""SOP / protocol workflow framework — Platform pass, Workstream 6.

Design doc §11: SOPs and protocol workflows are ONE system-wide
mechanism, not per-module inventions. A protocol DEFINITION is a
governed Studio artefact (kind ``protocol``) — ordered steps, the role
responsible for each step, checkpoints (governed forms), timers and
escalation paths — versioned under the §6.1 lifecycle (draft ->
publish; published immutable). The execution engine in this module
runs definitions uniformly for every module; every instance is
auditable step-by-step against the definition VERSION that governed
it (instances pin protocol_key + protocol_version at start).

GOVERNANCE NOTE (pending integration): the generic Studio API does
not know kind ``protocol`` until the configstore registration lands
(CONFIG_KINDS + VALIDATORS — listed as a pending integration edit in
the workstream handoff). Until then THIS module's routes are the
surface for protocol definitions: the lifecycle here mirrors
app.studio_config exactly (draft as the next StudioConfig version,
publish supersedes the previously published row, hash-chained audit
on create and publish), and payload validation lives in this module
(422 on violation) so the same validator can be registered in
configstore unchanged.

Capability gating (existing capabilities only — none invented):

- Definition manage/publish: ``studio.manage`` (admin/configurator)
  — definitions are Studio configuration, exactly like every other
  governed kind.
- Definition reads, coverage and instance reads: ``studio.read``
  (admin/configurator/clinician/viewer) — the staff-read pattern.
- Instance start / advance / escalate / abandon: ``fhir.write``
  (admin/configurator/clinician). Starting or advancing an SOP
  instance is clinical WRITE work performed against a subject's care
  context — the same act class as writing a FHIR resource — whereas
  ``studio.read`` is a read gate and ``studio.manage`` is a designer
  gate (neither describes doing clinical work). Viewer and patient
  are excluded by the fhir.write defaults, which is the point.
- ADVANCE additionally enforces the step's declared role: the
  caller's role must equal the step definition's ``role`` or be
  admin. The capability says "may do clinical write work"; the
  definition says "THIS step belongs to role X". Per-step role
  enforcement is the reason the framework exists.
- ABANDON additionally requires the caller to be the instance's
  starter or an admin.

Timers are COMPUTED AT READ: for the current step, due_at =
step_started_at + timer_minutes and `breached` is evaluated when the
instance is read; the applicable escalation is the definition
escalation whose after_minutes the current step has exceeded.
Escalation is APPLIED explicitly via POST .../escalate (409 when no
escalation is currently due). There is NO background daemon and none
is claimed — nothing fires while nobody is looking at the instance.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .audit import append_audit
from .configstore import (ConfigPayloadError, ROLE_NAMES, get_config_version,
                          list_config_versions, published_config,
                          published_configs, role_allowed)
from .db import Base
from .models import ConfigStatus, Role, StudioConfig, User, utcnow
from .security import get_db, require_capability

router = APIRouter(prefix="/protocols", tags=["protocols"])

KIND = "protocol"

# Canonical module list for the §11 per-module coverage checklist
# (design §11 final-gate item). Definition `module` values are grouped
# as authored; the aliases below map common authored spellings onto
# the canonical checklist entries so coverage stays honest without
# forcing sites to rename published definitions.
COVERAGE_MODULES = (
    "laboratory", "pharmacy", "ed-continuum", "inpatient", "orders",
    "revenue", "workforce", "devices", "engagement",
)
_MODULE_ALIASES = {
    "lab": "laboratory",
    "ed": "ed-continuum",
    "continuum": "ed-continuum",
    "care-continuum": "ed-continuum",
}


# ---------------------------------------------------------------------------
# Execution models (this module's own tables, on the shared Base)
# ---------------------------------------------------------------------------

class ProtocolInstance(Base):
    """One running (or finished) execution of one published protocol
    definition version, bound to a module object (`module_ref`, a free
    reference string such as "lab-order:123") and a subject
    (`subject_ref`, e.g. "Patient/pat-1")."""
    __tablename__ = "protocol_instances"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    protocol_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False)
    module_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    subject_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    current_step_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    current_step_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    started_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    escalation_to_role: Mapped[str | None] = mapped_column(String(60), nullable=True)
    escalation_action: Mapped[str | None] = mapped_column(String(300), nullable=True)
    abandon_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProtocolStepRecord(Base):
    """Per-step execution record for one instance: created pending for
    every definition step at start; `started_at` is stamped when the
    step becomes current (that stamp is the timer's origin)."""
    __tablename__ = "protocol_step_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(
        ForeignKey("protocol_instances.id"), nullable=False, index=True)
    step_key: Mapped[str] = mapped_column(String(120), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    acted_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    form_submission: Mapped[dict | None] = mapped_column(JSON, nullable=True)


# ---------------------------------------------------------------------------
# Payload validation (in-module; 422 via ConfigPayloadError)
# ---------------------------------------------------------------------------

def _fail(detail: str):
    raise ConfigPayloadError(detail, status_code=422)


def _require_str(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"'{field}' is required and must be a non-empty string")
    return value


def _positive_int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail(f"'{field}' must be a positive integer")
    return value


def validate_protocol_payload(payload) -> None:
    """Protocol definition: {name, module, title?, steps: [{key, title,
    role, checkpoint?: {formKey} | null, timer_minutes?: int|null,
    instructions?}], escalations?: [{after_minutes, to_role,
    action}]}."""
    if not isinstance(payload, dict):
        _fail("Protocol payload must be a JSON object")
    _require_str(payload.get("name"), "name")
    _require_str(payload.get("module"), "module")
    if "title" in payload and payload["title"] is not None \
            and not isinstance(payload["title"], str):
        _fail("'title' must be a string")
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        _fail("Protocol config requires a non-empty 'steps' list")
    seen: set[str] = set()
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            _fail(f"steps[{i}] must be an object")
        key = _require_str(step.get("key"), f"steps[{i}].key")
        if key in seen:
            _fail(f"Duplicate step key '{key}'")
        seen.add(key)
        _require_str(step.get("title"), f"steps[{i}].title")
        role = step.get("role")
        if role not in ROLE_NAMES:
            _fail(f"steps[{i}].role must be one of {list(ROLE_NAMES)}")
        checkpoint = step.get("checkpoint")
        if checkpoint is not None:
            if not isinstance(checkpoint, dict):
                _fail(f"steps[{i}].checkpoint must be an object or null")
            _require_str(checkpoint.get("formKey"), f"steps[{i}].checkpoint.formKey")
        timer = step.get("timer_minutes")
        if timer is not None:
            _positive_int(timer, f"steps[{i}].timer_minutes")
        if "instructions" in step and step["instructions"] is not None \
                and not isinstance(step["instructions"], str):
            _fail(f"steps[{i}].instructions must be a string")
    escalations = payload.get("escalations")
    if escalations is not None:
        if not isinstance(escalations, list):
            _fail("'escalations' must be a list")
        for i, esc in enumerate(escalations):
            if not isinstance(esc, dict):
                _fail(f"escalations[{i}] must be an object")
            _positive_int(esc.get("after_minutes"),
                          f"escalations[{i}].after_minutes")
            if esc.get("to_role") not in ROLE_NAMES:
                _fail(f"escalations[{i}].to_role must be one of {list(ROLE_NAMES)}")
            _require_str(esc.get("action"), f"escalations[{i}].action")


def _validate_or_422(payload) -> None:
    try:
        validate_protocol_payload(payload)
    except ConfigPayloadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


# ---------------------------------------------------------------------------
# Definition service (Studio lifecycle, mirrored from app.studio_config)
# ---------------------------------------------------------------------------

def create_draft(session: Session, user: User, key: str, payload) -> StudioConfig:
    _validate_or_422(payload)
    versions = list_config_versions(session, KIND, key)
    drafts = [v for v in versions if v.status == ConfigStatus.draft]
    if drafts:
        raise HTTPException(
            status_code=409,
            detail=(f"A draft already exists for protocol/{key} "
                    f"(v{drafts[-1].version}); publish it before drafting another"))
    next_version = (versions[-1].version + 1) if versions else 1
    row = StudioConfig(kind=KIND, key=key, version=next_version,
                       status=ConfigStatus.draft, payload=payload,
                       created_by=user.username, updated_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="protocols.definition.create",
                 entity=f"config:protocol/{key}@v{next_version}",
                 payload={"kind": KIND, "key": key, "version": next_version})
    session.commit()
    return row


def update_draft(session: Session, user: User, key: str, version: int,
                 payload) -> StudioConfig:
    row = get_config_version(session, KIND, key, version)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Protocol {key} v{version} not found")
    if row.status != ConfigStatus.draft:
        raise HTTPException(
            status_code=409,
            detail=(f"Protocol {key} v{version} is '{row.status.value}' and "
                    "immutable; create a new draft version to change it"))
    _validate_or_422(payload)
    row.payload = payload
    row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="protocols.definition.update",
                 entity=f"config:protocol/{key}@v{version}",
                 payload={"kind": KIND, "key": key, "version": version})
    session.commit()
    return row


def publish(session: Session, user: User, key: str, version: int) -> StudioConfig:
    row = get_config_version(session, KIND, key, version)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Protocol {key} v{version} not found")
    if row.status == ConfigStatus.published:
        raise HTTPException(status_code=409,
                            detail=f"Protocol {key} v{version} is already published")
    if row.status == ConfigStatus.superseded:
        raise HTTPException(
            status_code=409,
            detail=f"Protocol {key} v{version} was superseded and cannot be republished")
    current = published_config(session, KIND, key)
    superseded_version = None
    if current is not None and current.id != row.id:
        current.status = ConfigStatus.superseded
        superseded_version = current.version
        session.flush()
    row.status = ConfigStatus.published
    row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="protocols.definition.publish",
                 entity=f"config:protocol/{key}@v{row.version}",
                 payload={"kind": KIND, "key": key, "version": row.version,
                          "superseded_version": superseded_version})
    session.commit()
    return row


def published_definition(session: Session, key: str) -> StudioConfig | None:
    return published_config(session, KIND, key)


def definition_for_instance(session: Session, instance: ProtocolInstance) -> dict | None:
    """The definition payload of the exact version an instance runs
    under (published or since superseded — the pinned version governs
    the instance for its whole life)."""
    row = get_config_version(session, KIND, instance.protocol_key,
                             instance.protocol_version)
    return (row.payload or {}) if row is not None else None


def _config_out(row: StudioConfig) -> dict:
    return {
        "id": row.id, "kind": row.kind, "key": row.key, "version": row.version,
        "status": row.status.value, "payload": row.payload,
        "created_by": row.created_by, "updated_by": row.updated_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _can_see(session: Session, row: StudioConfig, user: User) -> bool:
    """Drafts are designer data (studio.manage); studio.read holders
    see published definitions — the studio_config visibility rule."""
    if role_allowed(session, user.role, "studio.manage"):
        return True
    return row.status == ConfigStatus.published


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


# ---------------------------------------------------------------------------
# Coverage (computed from published protocol configs — honest by
# construction: a module is covered only when a published definition
# names it)
# ---------------------------------------------------------------------------

def coverage(session: Session) -> dict:
    published = published_configs(session, KIND)
    by_module: dict[str, list[dict]] = {}
    for row in published:
        module = (row.payload or {}).get("module")
        if not isinstance(module, str) or not module:
            continue
        canonical = _MODULE_ALIASES.get(module, module)
        by_module.setdefault(canonical, []).append(
            {"key": row.key, "name": (row.payload or {}).get("name"),
             "version": row.version, "module_as_authored": module})
    modules = []
    for name in COVERAGE_MODULES:
        entries = sorted(by_module.get(name, []), key=lambda e: e["key"])
        modules.append({"module": name,
                        "has_published_protocol": bool(entries),
                        "protocol_count": len(entries),
                        "protocols": entries})
    extras = []
    for name in sorted(set(by_module) - set(COVERAGE_MODULES)):
        entries = sorted(by_module[name], key=lambda e: e["key"])
        extras.append({"module": name, "has_published_protocol": True,
                       "protocol_count": len(entries), "protocols": entries})
    return {"modules": modules, "additional_modules": extras,
            "total_published_protocols": len(published)}


# ---------------------------------------------------------------------------
# Execution engine
# ---------------------------------------------------------------------------

def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _step_def(definition: dict, step_key: str | None) -> dict | None:
    for step in (definition or {}).get("steps") or []:
        if isinstance(step, dict) and step.get("key") == step_key:
            return step
    return None


def timer_posture(instance: ProtocolInstance, definition: dict | None,
                  now: datetime | None = None) -> dict | None:
    """Computed-at-read timer posture for the instance's CURRENT step:
    due_at / breached / the escalation whose threshold the step has
    exceeded (the highest such threshold — the most escalated one that
    applies). None when the instance is not active, the step declares
    no timer, or the definition is unavailable."""
    if instance.status != "active" or not instance.current_step_key:
        return None
    step = _step_def(definition or {}, instance.current_step_key)
    if step is None:
        return None
    timer = step.get("timer_minutes")
    started = _as_utc(instance.current_step_started_at)
    if timer is None or started is None:
        return None
    now = now or utcnow()
    due_at = started + timedelta(minutes=timer)
    elapsed = (now - started).total_seconds() / 60.0
    applicable = None
    for esc in (definition or {}).get("escalations") or []:
        if isinstance(esc, dict) and isinstance(esc.get("after_minutes"), int) \
                and elapsed >= esc["after_minutes"]:
            if applicable is None or esc["after_minutes"] > applicable["after_minutes"]:
                applicable = esc
    return {"step_key": instance.current_step_key,
            "timer_minutes": timer,
            "step_started_at": started.isoformat(),
            "due_at": due_at.isoformat(),
            "elapsed_minutes": round(elapsed, 3),
            "breached": now > due_at,
            "escalation": applicable}


def start_instance(session: Session, user: User, key: str, module_ref: str,
                   subject_ref: str) -> ProtocolInstance:
    row = published_definition(session, key)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"No published protocol definition for '{key}'")
    existing = session.scalars(
        select(ProtocolInstance).where(
            ProtocolInstance.protocol_key == key,
            ProtocolInstance.module_ref == module_ref,
            ProtocolInstance.status == "active")).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(f"An active instance of '{key}' already exists for "
                    f"module_ref '{module_ref}' (instance {existing.id})"))
    definition = row.payload or {}
    steps = definition.get("steps") or []
    now = utcnow()
    instance = ProtocolInstance(
        protocol_key=key, protocol_version=row.version,
        module_ref=module_ref, subject_ref=subject_ref or "",
        status="active", current_step_key=steps[0]["key"],
        current_step_started_at=now, started_by=user.username,
        started_at=now, updated_at=now)
    session.add(instance)
    session.flush()
    for position, step in enumerate(steps):
        session.add(ProtocolStepRecord(
            instance_id=instance.id, step_key=step["key"], position=position,
            status="pending",
            started_at=now if position == 0 else None))
    session.flush()
    append_audit(session, actor=user.username, action="protocols.instance.start",
                 entity=f"protocol-instance:{instance.id}",
                 payload={"protocol_key": key, "protocol_version": row.version,
                          "module_ref": module_ref, "subject_ref": subject_ref or "",
                          "first_step": steps[0]["key"]})
    session.commit()
    return instance


def _get_instance_or_404(session: Session, instance_id: int) -> ProtocolInstance:
    instance = session.get(ProtocolInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404,
                            detail=f"Protocol instance {instance_id} not found")
    return instance


def advance_instance(session: Session, user: User, instance_id: int,
                     step_key: str, note: str | None,
                     form_submission) -> ProtocolInstance:
    instance = _get_instance_or_404(session, instance_id)
    if instance.status != "active":
        raise HTTPException(
            status_code=409,
            detail=f"Instance {instance_id} is '{instance.status}' and cannot advance")
    if step_key != instance.current_step_key:
        raise HTTPException(
            status_code=409,
            detail=(f"Step '{step_key}' is not the current step "
                    f"('{instance.current_step_key}'); steps advance in order"))
    definition = definition_for_instance(session, instance) or {}
    step = _step_def(definition, step_key)
    if step is None:
        raise HTTPException(status_code=409,
                            detail=f"Step '{step_key}' is not in the governing definition")
    if user.role != Role.admin and user.role.value != step.get("role"):
        raise HTTPException(
            status_code=403,
            detail=(f"Step '{step_key}' belongs to role '{step.get('role')}'; "
                    f"role '{user.role.value}' may not perform it"))
    checkpoint = step.get("checkpoint")
    if isinstance(checkpoint, dict) and checkpoint.get("formKey"):
        if not isinstance(form_submission, dict) or not form_submission:
            raise HTTPException(
                status_code=422,
                detail=(f"Step '{step_key}' requires a checkpoint submission for "
                        f"form '{checkpoint['formKey']}' (form_submission)"))
    records = list(session.scalars(
        select(ProtocolStepRecord).where(
            ProtocolStepRecord.instance_id == instance.id)
        .order_by(ProtocolStepRecord.position.asc())).all())
    record = next((r for r in records if r.step_key == step_key), None)
    now = utcnow()
    if record is not None:
        record.status = "done"
        record.acted_by = user.username
        record.acted_at = now
        record.note = note
        record.form_submission = form_submission if isinstance(form_submission, dict) else None
    steps = definition.get("steps") or []
    keys = [s["key"] for s in steps if isinstance(s, dict)]
    index = keys.index(step_key) if step_key in keys else len(keys) - 1
    next_key = keys[index + 1] if index + 1 < len(keys) else None
    instance.current_step_key = next_key
    instance.current_step_started_at = now if next_key else None
    if next_key is not None:
        nxt = next((r for r in records if r.step_key == next_key), None)
        if nxt is not None and nxt.started_at is None:
            nxt.started_at = now
    completed = next_key is None
    if completed:
        instance.status = "completed"
    instance.updated_at = now
    session.flush()
    append_audit(session, actor=user.username, action="protocols.instance.advance",
                 entity=f"protocol-instance:{instance.id}",
                 payload={"protocol_key": instance.protocol_key,
                          "step_key": step_key, "next_step": next_key,
                          "note": note,
                          "checkpoint_form": (checkpoint or {}).get("formKey")
                          if isinstance(checkpoint, dict) else None})
    if completed:
        append_audit(session, actor=user.username, action="protocols.instance.complete",
                     entity=f"protocol-instance:{instance.id}",
                     payload={"protocol_key": instance.protocol_key,
                              "protocol_version": instance.protocol_version,
                              "module_ref": instance.module_ref})
    session.commit()
    return instance


def escalate_instance(session: Session, user: User,
                      instance_id: int) -> ProtocolInstance:
    instance = _get_instance_or_404(session, instance_id)
    if instance.status != "active":
        raise HTTPException(
            status_code=409,
            detail=f"Instance {instance_id} is '{instance.status}'; no escalation is due")
    definition = definition_for_instance(session, instance)
    posture = timer_posture(instance, definition)
    escalation = posture.get("escalation") if posture else None
    if escalation is None:
        raise HTTPException(
            status_code=409,
            detail="No escalation is currently due for this instance")
    instance.status = "escalated"
    instance.escalation_to_role = escalation.get("to_role")
    instance.escalation_action = escalation.get("action")
    instance.updated_at = utcnow()
    session.flush()
    append_audit(session, actor=user.username, action="protocols.instance.escalate",
                 entity=f"protocol-instance:{instance.id}",
                 payload={"protocol_key": instance.protocol_key,
                          "step_key": instance.current_step_key,
                          "after_minutes": escalation.get("after_minutes"),
                          "to_role": escalation.get("to_role"),
                          "action": escalation.get("action")})
    session.commit()
    return instance


def abandon_instance(session: Session, user: User, instance_id: int,
                     reason: str | None) -> ProtocolInstance:
    instance = _get_instance_or_404(session, instance_id)
    if user.role != Role.admin and instance.started_by != user.username:
        raise HTTPException(
            status_code=403,
            detail="Only the instance starter or an admin may abandon an instance")
    if instance.status != "active":
        raise HTTPException(
            status_code=409,
            detail=f"Instance {instance_id} is '{instance.status}' and cannot be abandoned")
    instance.status = "abandoned"
    instance.abandon_reason = reason
    instance.updated_at = utcnow()
    session.flush()
    append_audit(session, actor=user.username, action="protocols.instance.abandon",
                 entity=f"protocol-instance:{instance.id}",
                 payload={"protocol_key": instance.protocol_key,
                          "reason": reason,
                          "current_step": instance.current_step_key})
    session.commit()
    return instance


def instance_out(session: Session, instance: ProtocolInstance) -> dict:
    definition = definition_for_instance(session, instance)
    records = list(session.scalars(
        select(ProtocolStepRecord).where(
            ProtocolStepRecord.instance_id == instance.id)
        .order_by(ProtocolStepRecord.position.asc())).all())
    steps_out = []
    for record in records:
        step = _step_def(definition or {}, record.step_key) or {}
        steps_out.append({
            "step_key": record.step_key,
            "title": step.get("title"),
            "role": step.get("role"),
            "checkpoint": step.get("checkpoint"),
            "timer_minutes": step.get("timer_minutes"),
            "status": record.status,
            "acted_by": record.acted_by,
            "acted_at": record.acted_at.isoformat() if record.acted_at else None,
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "note": record.note,
            "form_submission": record.form_submission,
        })
    return {
        "id": instance.id,
        "protocol_key": instance.protocol_key,
        "protocol_version": instance.protocol_version,
        "module_ref": instance.module_ref,
        "subject_ref": instance.subject_ref,
        "status": instance.status,
        "current_step_key": instance.current_step_key,
        "current_step_started_at": (instance.current_step_started_at.isoformat()
                                    if instance.current_step_started_at else None),
        "started_by": instance.started_by,
        "started_at": instance.started_at.isoformat() if instance.started_at else None,
        "updated_at": instance.updated_at.isoformat() if instance.updated_at else None,
        "escalation_to_role": instance.escalation_to_role,
        "escalation_action": instance.escalation_action,
        "abandon_reason": instance.abandon_reason,
        "steps": steps_out,
        "timer_posture": timer_posture(instance, definition),
    }


# ---------------------------------------------------------------------------
# Routes — coverage first, then definitions, then instances (literal
# segments must win over the {key} parameter routes below)
# ---------------------------------------------------------------------------

@router.get("/coverage")
def get_coverage(session: Session = Depends(get_db),
                 user: User = Depends(require_capability("studio.read"))):
    return coverage(session)


@router.post("/definitions/{key}", status_code=201)
async def create_definition(key: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("studio.manage"))):
    payload = await _json_body(request)
    return _config_out(create_draft(session, user, key, payload))


@router.put("/definitions/{key}/versions/{version}")
async def update_definition(key: str, version: int, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("studio.manage"))):
    payload = await _json_body(request)
    return _config_out(update_draft(session, user, key, version, payload))


@router.post("/definitions/{key}/versions/{version}/publish")
def publish_definition(key: str, version: int, session: Session = Depends(get_db),
                       user: User = Depends(require_capability("studio.manage"))):
    return _config_out(publish(session, user, key, version))


@router.get("/definitions")
def list_definitions(session: Session = Depends(get_db),
                     user: User = Depends(require_capability("studio.read"))):
    rows = list(session.scalars(
        select(StudioConfig).where(StudioConfig.kind == KIND)
        .order_by(StudioConfig.key, StudioConfig.version)).all())
    return [_config_out(r) for r in rows if _can_see(session, r, user)]


@router.get("/definitions/{key}")
def definition_history(key: str, session: Session = Depends(get_db),
                       user: User = Depends(require_capability("studio.read"))):
    rows = list_config_versions(session, KIND, key)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No protocol found for '{key}'")
    return [_config_out(r) for r in rows if _can_see(session, r, user)]


@router.get("/definitions/{key}/published")
def get_published_definition(key: str, session: Session = Depends(get_db),
                             user: User = Depends(require_capability("studio.read"))):
    row = published_definition(session, key)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"No published protocol for '{key}'")
    return _config_out(row)


@router.get("/definitions/{key}/versions/{version}")
def get_definition_version(key: str, version: int, session: Session = Depends(get_db),
                           user: User = Depends(require_capability("studio.read"))):
    row = get_config_version(session, KIND, key, version)
    if row is None or not _can_see(session, row, user):
        raise HTTPException(status_code=404,
                            detail=f"Protocol {key} v{version} not found")
    return _config_out(row)


@router.get("/instances")
def list_instances(module_ref: str | None = Query(default=None),
                   status: str | None = Query(default=None),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("studio.read"))):
    stmt = select(ProtocolInstance).order_by(ProtocolInstance.id.asc())
    if module_ref is not None:
        stmt = stmt.where(ProtocolInstance.module_ref == module_ref)
    if status is not None:
        stmt = stmt.where(ProtocolInstance.status == status)
    return [instance_out(session, i) for i in session.scalars(stmt).all()]


@router.get("/instances/{instance_id}")
def get_instance(instance_id: int, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("studio.read"))):
    return instance_out(session, _get_instance_or_404(session, instance_id))


@router.post("/instances/{instance_id}/advance")
async def advance(instance_id: int, request: Request,
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability("fhir.write"))):
    body = await _json_body(request)
    step_key = body.get("step_key")
    if not isinstance(step_key, str) or not step_key:
        raise HTTPException(status_code=422, detail="'step_key' is required")
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise HTTPException(status_code=422, detail="'note' must be a string")
    instance = advance_instance(session, user, instance_id, step_key, note,
                                body.get("form_submission"))
    return instance_out(session, instance)


@router.post("/instances/{instance_id}/escalate")
def escalate(instance_id: int, session: Session = Depends(get_db),
             user: User = Depends(require_capability("fhir.write"))):
    return instance_out(session, escalate_instance(session, user, instance_id))


@router.post("/instances/{instance_id}/abandon")
async def abandon(instance_id: int, request: Request,
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability("fhir.write"))):
    body = await _json_body(request)
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise HTTPException(status_code=422, detail="'reason' must be a string")
    return instance_out(session, abandon_instance(session, user, instance_id, reason))


@router.post("/{key}/instances", status_code=201)
async def start(key: str, request: Request, session: Session = Depends(get_db),
                user: User = Depends(require_capability("fhir.write"))):
    body = await _json_body(request)
    module_ref = body.get("module_ref")
    if not isinstance(module_ref, str) or not module_ref.strip():
        raise HTTPException(status_code=422,
                            detail="'module_ref' is required and must be a non-empty string")
    subject_ref = body.get("subject_ref")
    if subject_ref is not None and not isinstance(subject_ref, str):
        raise HTTPException(status_code=422, detail="'subject_ref' must be a string")
    instance = start_instance(session, user, key, module_ref, subject_ref or "")
    return instance_out(session, instance)
