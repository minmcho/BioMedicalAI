"""Phase 15 — Care coordination (BD-04/07/24/25/27).

- **BD-27 eConsults (advice-only consults)**: an eConsult is its own
  table record — requester, patient, specialty (validated against the
  published ``terminology/econsult-specialties`` config, which also
  carries each specialty's SLA hours), question, chart-context
  references, lifecycle open -> answered -> closed (or cancelled with
  a reason). The specialist's answer completes the unit of work IN
  CHART: no visit is created. When the answer flags ``needs_visit``,
  it creates a real FHIR ServiceRequest referral (status ``draft``,
  reason "eConsult follow-up" — see :func:`_create_followup_referral`
  for the exact shape). Rule-triggered initiation follows the TACo
  pattern: the published ``terminology/econsult-triggers`` config
  (seeded with ONE diabetes trigger — latest HbA1c Observation above
  a configured threshold and no open diabetes eConsult for the
  patient) is evaluated deterministically over the FHIR store by
  ``POST /coord/econsults/scan``; every automatic creation is audited
  with the trigger id and cites the triggering Observation in the
  record. The scan is idempotent while an eConsult is open.

- **BD-25 Multispecialty episodes**: FHIR ``EpisodeOfCare`` is NOT
  in ``app.fhir.SUPPORTED``, so episodes are own-table records (the
  app.protocols / app.barcodes precedent for module-owned tables).
  One episode per case spanning specialties: patient, coordinating
  clinician, condition/title, status, typed linked references, and
  per-specialty contributions; an assembly GET returns the whole
  coordinated plan.

- **BD-24 Coordinating physician**: patient-level assignment records.
  History is kept — a new assignment supersedes the previous one,
  never edits it. Assignments validate the staff member against the
  people directory (app.models.StaffMember, by username or staff id).
  Episodes created without an explicit coordinator inherit the
  patient's current coordinator.

- **BD-04 ICU module**: the ICU unit already exists in the published
  locations config (Phase 6, ``unit-icu``). ICU flowsheets are
  governed Studio forms seeded published here (``form/icu-acuity``,
  ``form/icu-devices``) and submitted through the documentation
  flowsheet machinery (QuestionnaireResponses); the ICU board reads
  the census from app.inpatient's census machinery (imported, not
  recomputed) and joins the latest flowsheet facts. ICU handoffs are
  governed ``form/icu-handoff`` submissions attached to the patient's
  current encounter (the app.continuum handoff pattern).

- **BD-07 Therapy scheduling (PT/OT/SLP)**: treatment plans validate
  the discipline against ``terminology/therapy-disciplines`` and read
  total authorised visits from the patient's covering prior
  authorisation (app.access.find_covering_auth over app.models.
  PriorAuth — imported, not forked). With no covering authorisation
  the plan is created in an honestly labelled ``no-authorisation``
  state that books nothing. Sessions are real FHIR Appointments plus
  a session record; a session consumes one authorised visit ON
  COMPLETION (the PriorAuth row's used_units is incremented in the
  same transaction); completing beyond the authorised count refuses
  409 naming the count; cancelling consumes nothing.

Configure, don't fork: specialties + SLAs, triggers, therapy
disciplines and the ICU forms are all published Studio config seeded
idempotently below. Every mutation is hash-audit-chained.
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import DateTime, Integer, JSON, String, Text, Boolean, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import access, documentation, fhir, inpatient
from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import (ConfigStatus, FhirResource, PriorAuth, StaffMember,
                     StudioConfig, User, utcnow)
from .security import get_db, require_capability

router = APIRouter(prefix="/coord", tags=["care_coordination"])

READ = "coord.read"
WORK = "coord.work"

SPECIALTIES_KEY = "econsult-specialties"
SPECIALTIES_SYSTEM = "urn:opencare:econsult-specialty"
TRIGGERS_KEY = "econsult-triggers"
THERAPY_KEY = "therapy-disciplines"
THERAPY_SYSTEM = "urn:opencare:therapy-discipline"

ECONSULT_EXTENSION = "urn:opencare:extension/econsult"

EPISODE_LINK_TYPES = {
    "encounter": ("Encounter",),
    "order": ("ServiceRequest", "MedicationRequest"),
    "note": ("DocumentReference",),
    "observation": ("Observation",),
    "appointment": ("Appointment",),
    "task": ("Task",),
    "referral": ("ServiceRequest",),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


def _require_str(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422,
                            detail=f"'{field}' is required and must be a non-empty string")
    return value.strip()


def _patient_ref(session: Session, payload: dict) -> str:
    subject = payload.get("subject") or {}
    patient_ref = subject.get("reference") if isinstance(subject, dict) else None
    if not patient_ref and payload.get("patient"):
        patient_ref = fhir._normalize_patient_ref(str(payload["patient"]))
    if not patient_ref or not patient_ref.startswith("Patient/"):
        raise HTTPException(
            status_code=422,
            detail="Requires 'subject.reference' (or 'patient') naming a Patient")
    fhir._get_row("Patient", patient_ref.split("/", 1)[1], session)
    return patient_ref


def _patient_ref_for_id(session: Session, patient_id: str) -> str:
    fhir._get_row("Patient", patient_id, session)
    return f"Patient/{patient_id}"


# ---------------------------------------------------------------------------
# Own tables (module-owned, on the shared Base — the protocols precedent)
# ---------------------------------------------------------------------------

class EConsult(Base):
    __tablename__ = "coord_econsults"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    econsult_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    requester: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    specialty: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False, default="")
    chart_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="open", index=True)
    sla_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommendations: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_visit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    referral_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    answered_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    triggering_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Episode(Base):
    __tablename__ = "coord_episodes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    episode_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    coordinator_username: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    coordinator_staff_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    links: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class EpisodeContribution(Base):
    __tablename__ = "coord_episode_contributions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contribution_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    episode_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    specialty: Mapped[str] = mapped_column(String(80), nullable=False)
    author: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    linked_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CoordinatorAssignment(Base):
    __tablename__ = "coord_coordinator_assignments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    staff_username: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    staff_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    staff_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    role_label: Mapped[str] = mapped_column(String(120), nullable=False, default="Coordinating physician")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    assigned_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TherapyPlan(Base):
    __tablename__ = "coord_therapy_plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    discipline: Mapped[str] = mapped_column(String(80), nullable=False)
    frequency_pattern: Mapped[str | None] = mapped_column(String(120), nullable=True)
    therapist: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="authorised")
    authorisation_status: Mapped[str] = mapped_column(String(30), nullable=False, default="authorised")
    auth_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    auth_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    authorised_visits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TherapySession(Base):
    __tablename__ = "coord_therapy_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    plan_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    discipline: Mapped[str] = mapped_column(String(80), nullable=False)
    therapist: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    appointment_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled")
    scheduled_at: Mapped[str | None] = mapped_column(String(60), nullable=True)
    auth_consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Published Studio configuration (seeded defaults)
# ---------------------------------------------------------------------------

def default_specialties_payload() -> dict:
    def spec(code, display, sla):
        return {"code": code, "display": display, "active": True,
                "system": SPECIALTIES_SYSTEM, "slaHours": sla}
    return {"name": "eConsult specialties + SLA (hours)",
            "system": SPECIALTIES_SYSTEM,
            "concepts": [
                spec("endocrinology", "Endocrinology", 24),
                spec("cardiology", "Cardiology", 24),
                spec("dermatology", "Dermatology", 72),
                spec("neurology", "Neurology", 48),
                spec("psychiatry", "Psychiatry", 48),
                spec("gastroenterology", "Gastroenterology", 48),
                # The diabetes trigger's specialty: diabetes advice is
                # an endocrinology eConsult in this seeded taxonomy.
                spec("diabetes", "Diabetes (endocrinology)", 24),
            ]}


def default_triggers_payload() -> dict:
    return {"name": "eConsult rule triggers (TACo pattern)",
            "system": "urn:opencare:econsult-trigger",
            "concepts": [
                {"code": "diabetes-hba1c-high", "display": "Diabetes: latest HbA1c above threshold",
                 "active": True, "system": "urn:opencare:econsult-trigger"},
            ],
            "triggers": [
                {"id": "diabetes-hba1c-high",
                 "specialty": "diabetes",
                 "observationCode": "4548-4",
                 "observationSystem": "http://loinc.org",
                 "comparator": "gt",
                 "threshold": 9.0,
                 "question": ("Auto-triggered eConsult (rule diabetes-hba1c-high): "
                              "latest HbA1c is above the configured threshold — "
                              "please advise on diabetes management.")},
            ]}


def default_therapy_payload() -> dict:
    def disc(code, display):
        return {"code": code, "display": display, "active": True,
                "system": THERAPY_SYSTEM}
    return {"name": "Therapy disciplines (PT/OT/SLP)",
            "system": THERAPY_SYSTEM,
            "concepts": [
                disc("physical-therapy", "Physical therapy (PT)"),
                disc("occupational-therapy", "Occupational therapy (OT)"),
                disc("speech-therapy", "Speech-language therapy (SLP)"),
            ]}


def _form(title, items):
    return {"resourceType": "Questionnaire", "title": title, "item": items}


def _item(link_id, text, type_, required=False):
    return {"linkId": link_id, "text": text, "type": type_, "required": required}


def default_icu_form_payloads() -> dict[str, dict]:
    return {
        "icu-acuity": _form("ICU acuity flowsheet", [
            _item("acuity-score", "Acuity score", "integer", True),
            _item("sofa-score", "SOFA score", "integer"),
            _item("gcs", "Glasgow Coma Scale", "integer"),
            _item("notes", "Acuity notes", "text"),
        ]),
        "icu-devices": _form("ICU devices daily check", [
            _item("device-count", "Total devices / lines in situ", "integer", True),
            _item("central-line", "Central line in situ", "boolean"),
            _item("arterial-line", "Arterial line in situ", "boolean"),
            _item("ett", "Endotracheal tube in situ", "boolean"),
            _item("urinary-catheter", "Urinary catheter in situ", "boolean"),
            _item("notes", "Device check notes", "text"),
        ]),
        "icu-handoff": _form("ICU handoff (I-PASS)", [
            _item("illness-severity", "I — Illness severity", "text", True),
            _item("patient-summary", "P — Patient summary", "text", True),
            _item("action-list", "A — Action list", "text", True),
            _item("situation-awareness", "S — Situation awareness and contingency planning", "text", True),
            _item("synthesis", "S — Synthesis by receiver (read-back)", "text", True),
        ]),
    }


def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
    if list_config_versions(session, kind, key):
        return
    row = StudioConfig(kind=kind, key=key, version=1,
                       status=ConfigStatus.published, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:{kind}/{key}@v1",
                 payload={"kind": kind, "key": key, "version": 1, "seeded_default": True})
    append_audit(session, actor="system", action="studio.config.publish",
                 entity=f"config:{kind}/{key}@v1",
                 payload={"kind": kind, "key": key, "version": 1,
                          "superseded_version": None, "seeded_default": True})


def ensure_care_coordination_defaults(session: Session) -> None:
    _seed_published(session, "terminology", SPECIALTIES_KEY, default_specialties_payload())
    _seed_published(session, "terminology", TRIGGERS_KEY, default_triggers_payload())
    _seed_published(session, "terminology", THERAPY_KEY, default_therapy_payload())
    for key, payload in default_icu_form_payloads().items():
        _seed_published(session, "form", key, payload)
    session.commit()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _concepts(session: Session, key: str) -> list[dict]:
    cfg = published_config(session, "terminology", key)
    if cfg is None:
        return []
    return [c for c in (cfg.payload or {}).get("concepts") or [] if isinstance(c, dict)]


def _specialty(session: Session, code: str) -> dict:
    for concept in _concepts(session, SPECIALTIES_KEY):
        if concept.get("code") == code:
            if concept.get("active") is False:
                raise HTTPException(status_code=422,
                                    detail=f"Specialty '{code}' is inactive in the published econsult-specialties terminology")
            return concept
    raise HTTPException(status_code=422,
                        detail=f"Unknown specialty '{code}': not in the published econsult-specialties terminology")


def _discipline(session: Session, code: str) -> dict:
    for concept in _concepts(session, THERAPY_KEY):
        if concept.get("code") == code:
            if concept.get("active") is False:
                raise HTTPException(status_code=422,
                                    detail=f"Discipline '{code}' is inactive in the published therapy-disciplines terminology")
            return concept
    raise HTTPException(status_code=422,
                        detail=f"Unknown discipline '{code}': not in the published therapy-disciplines terminology")


def _staff(session: Session, identifier: str) -> StaffMember:
    row = session.scalars(select(StaffMember).where(
        StaffMember.username == identifier)).first()
    if row is None:
        row = session.scalars(select(StaffMember).where(
            StaffMember.staff_id == identifier)).first()
    if row is None:
        raise HTTPException(status_code=422,
                            detail=f"Unknown staff '{identifier}': no such entry in the people directory (by username or staff id)")
    return row


# ---------------------------------------------------------------------------
# BD-24 — Coordinating physician (defined early: episodes inherit from it)
# ---------------------------------------------------------------------------

def _assignment_out(row: CoordinatorAssignment) -> dict:
    return {"assignment_id": row.assignment_id, "patient": row.patient_ref,
            "patient_ref": row.patient_ref,
            "staff_username": row.staff_username, "staff_id": row.staff_id,
            "staff_name": row.staff_name, "coordinator": row.staff_username or row.staff_id,
            "role_label": row.role_label, "role": row.role_label,
            "active": row.active, "assigned_by": row.assigned_by,
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None,
            "superseded_at": _aware(row.superseded_at).isoformat() if row.superseded_at else None}


def _current_assignment(session: Session, patient_ref: str) -> CoordinatorAssignment | None:
    return session.scalars(
        select(CoordinatorAssignment).where(
            CoordinatorAssignment.patient_ref == patient_ref,
            CoordinatorAssignment.active.is_(True))
        .order_by(CoordinatorAssignment.id.desc())).first()


@router.post("/patients/{patient_id}/coordinator", status_code=201)
async def assign_coordinator(patient_id: str, request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability(WORK))):
    """Assign the patient's coordinating clinician. History is kept:
    the previous active assignment is superseded, never edited."""
    payload = await _json_body(request)
    patient_ref = _patient_ref_for_id(session, patient_id)
    identifier = payload.get("staff_username") or payload.get("staff_id") \
        or payload.get("coordinator") or payload.get("username")
    if not isinstance(identifier, str) or not identifier.strip():
        raise HTTPException(status_code=422, detail="Assignment requires 'staff_username' (a people-directory username or staff id)")
    staff = _staff(session, identifier.strip())
    role_label = payload.get("role_label") or payload.get("role") or "Coordinating physician"
    previous = _current_assignment(session, patient_ref)
    if previous is not None:
        previous.active = False
        previous.superseded_at = _now()
        session.flush()
    row = CoordinatorAssignment(
        assignment_id=str(uuid.uuid4()), patient_ref=patient_ref,
        staff_username=staff.username, staff_id=staff.staff_id,
        staff_name=staff.full_name, role_label=str(role_label),
        active=True, assigned_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="coord.coordinator.assign",
                 entity=f"Patient/{patient_id}",
                 payload={"assignment_id": row.assignment_id, "staff_username": staff.username,
                          "staff_id": staff.staff_id, "role_label": row.role_label,
                          "superseded_assignment_id": previous.assignment_id if previous else None})
    session.commit()
    return _assignment_out(row)


@router.get("/patients/{patient_id}/coordinator")
def get_coordinator(patient_id: str, session: Session = Depends(get_db),
                    user: User = Depends(require_capability(READ))):
    """Chart surface: the patient's CURRENT coordinator (or null),
    with the full assignment history alongside."""
    patient_ref = _patient_ref_for_id(session, patient_id)
    history = session.scalars(
        select(CoordinatorAssignment).where(
            CoordinatorAssignment.patient_ref == patient_ref)
        .order_by(CoordinatorAssignment.id.asc())).all()
    current = next((r for r in reversed(history) if r.active), None)
    return {"patient": patient_ref, "patient_ref": patient_ref,
            "coordinator": _assignment_out(current) if current else None,
            "history": [_assignment_out(r) for r in history]}


@router.get("/panels/{staff_username}")
def coordinator_panel(staff_username: str, session: Session = Depends(get_db),
                      user: User = Depends(require_capability(READ))):
    """The coordinator's panel: every patient they currently
    coordinate, with the patient's active episode(s) and the attending
    of record where an active inpatient encounter carries the
    front-door attending extension (urn:opencare:extension/attending)."""
    staff = None
    try:
        staff = _staff(session, staff_username)
    except HTTPException:
        staff = None
    stmt = select(CoordinatorAssignment).where(CoordinatorAssignment.active.is_(True))
    if staff is not None:
        stmt = stmt.where(
            (CoordinatorAssignment.staff_id == staff.staff_id)
            | (CoordinatorAssignment.staff_username == staff_username))
    else:
        stmt = stmt.where(CoordinatorAssignment.staff_username == staff_username)
    assignments = session.scalars(stmt.order_by(CoordinatorAssignment.id.asc())).all()
    items = []
    for assignment in assignments:
        episodes = session.scalars(
            select(Episode).where(Episode.patient_ref == assignment.patient_ref,
                                  Episode.status == "active")
            .order_by(Episode.id.asc())).all()
        attending_id, attending_encounter = None, None
        enc_rows = session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "Encounter",
                FhirResource.patient_ref == assignment.patient_ref,
                FhirResource.status == "in-progress")
            .order_by(FhirResource.created_at.desc(), FhirResource.id.desc())).all()
        for enc in enc_rows:
            if inpatient._class_code(enc.body or {}) != "IMP":
                continue
            ext = inpatient._get_extension(enc.body or {}, inpatient.ATTENDING_EXTENSION)
            if ext is not None and ext.get("valueString"):
                attending_id = ext["valueString"]
                attending_encounter = enc.resource_id
                break
        attending_name = None
        if attending_id:
            member = session.scalars(select(StaffMember).where(
                StaffMember.staff_id == attending_id)).first()
            attending_name = member.full_name if member else None
        items.append({
            "patient": assignment.patient_ref, "patient_ref": assignment.patient_ref,
            "coordinator_role": assignment.role_label,
            "active_episode": _episode_out(episodes[0]) if episodes else None,
            "active_episodes": [_episode_out(e) for e in episodes],
            "attending_staff_id": attending_id, "attending": attending_id,
            "attending_name": attending_name,
            "attending_encounter_id": attending_encounter,
        })
    return {"staff_username": staff_username,
            "staff_id": staff.staff_id if staff else None,
            "staff_name": staff.full_name if staff else None,
            "items": items, "patients": items, "total": len(items)}


# ---------------------------------------------------------------------------
# BD-27 — eConsults
# ---------------------------------------------------------------------------

def _econsult_out(row: EConsult, now: datetime | None = None) -> dict:
    now = now or _now()
    created = _aware(row.created_at)
    deadline = created + timedelta(hours=row.sla_hours) if created and row.sla_hours else None
    if row.state == "open":
        breached = bool(deadline and now > deadline)
    elif row.answered_at and deadline:
        breached = _aware(row.answered_at) > deadline
    else:
        breached = False
    return {"id": row.econsult_id, "econsult_id": row.econsult_id,
            "patient": row.patient_ref, "patient_ref": row.patient_ref,
            "requester": row.requester, "specialty": row.specialty,
            "question": row.question, "chart_refs": row.chart_refs or [],
            "chart_context": row.chart_refs or [],
            "state": row.state, "status": row.state,
            "sla_hours": row.sla_hours,
            "sla_deadline": deadline.isoformat() if deadline else None,
            "sla_breached": breached, "breached": breached,
            "answer": row.answer_text, "answer_text": row.answer_text,
            "recommendations": row.recommendations,
            "needs_visit": row.needs_visit, "referral_id": row.referral_id,
            "referral_ref": f"ServiceRequest/{row.referral_id}" if row.referral_id else None,
            "answered_by": row.answered_by,
            "answered_at": _aware(row.answered_at).isoformat() if row.answered_at else None,
            "cancel_reason": row.cancel_reason, "cancelled_reason": row.cancel_reason,
            "trigger_id": row.trigger_id, "triggering_ref": row.triggering_ref,
            "triggering_observation": row.triggering_ref,
            "created_by": row.created_by,
            "created_at": created.isoformat() if created else None}


def _econsult_row(session: Session, econsult_id: str) -> EConsult:
    row = session.scalars(select(EConsult).where(
        EConsult.econsult_id == econsult_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"eConsult {econsult_id} not found")
    return row


def _create_econsult(session: Session, user_name: str, *, patient_ref, specialty_concept,
                     question, chart_refs, requester, trigger_id=None,
                     triggering_ref=None) -> EConsult:
    sla = specialty_concept.get("slaHours")
    row = EConsult(
        econsult_id=str(uuid.uuid4()), patient_ref=patient_ref,
        requester=requester, specialty=specialty_concept["code"],
        question=question, chart_refs=list(chart_refs or []), state="open",
        sla_hours=int(sla) if isinstance(sla, int) and not isinstance(sla, bool) else None,
        trigger_id=trigger_id, triggering_ref=triggering_ref,
        created_by=user_name)
    session.add(row)
    session.flush()
    return row


@router.post("/econsults", status_code=201)
async def create_econsult(request: Request, session: Session = Depends(get_db),
                          user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    patient_ref = _patient_ref(session, payload)
    specialty = _specialty(session, _require_str(payload, "specialty"))
    question = _require_str(payload, "question")
    chart_refs = payload.get("chart_refs") or payload.get("chart_context") or []
    if not isinstance(chart_refs, list):
        raise HTTPException(status_code=422, detail="'chart_refs' must be a list of references")
    requester = payload.get("requester") if isinstance(payload.get("requester"), str) \
        and payload.get("requester") else user.username
    row = _create_econsult(session, user.username, patient_ref=patient_ref,
                           specialty_concept=specialty, question=question,
                           chart_refs=[str(r) for r in chart_refs],
                           requester=requester)
    append_audit(session, actor=user.username, action="coord.econsult.create",
                 entity=f"EConsult/{row.econsult_id}",
                 payload={"patient": patient_ref, "specialty": row.specialty,
                          "requester": requester, "sla_hours": row.sla_hours})
    session.commit()
    return _econsult_out(row)


@router.get("/econsults/queue/specialty/{specialty}")
def econsult_specialty_queue(specialty: str, session: Session = Depends(get_db),
                             user: User = Depends(require_capability(READ))):
    """Specialist worklist: OPEN eConsults for one specialty,
    SLA-breach first, then oldest first."""
    _specialty(session, specialty)
    rows = session.scalars(select(EConsult).where(
        EConsult.specialty == specialty, EConsult.state == "open")
        .order_by(EConsult.created_at.asc(), EConsult.id.asc())).all()
    now = _now()
    items = [_econsult_out(r, now) for r in rows]
    items.sort(key=lambda i: (not i["sla_breached"], i["created_at"] or ""))
    return {"specialty": specialty, "items": items, "total": len(items),
            "breached": sum(1 for i in items if i["sla_breached"])}


@router.get("/econsults/queue/requester/{requester}")
def econsult_requester_queue(requester: str, session: Session = Depends(get_db),
                             user: User = Depends(require_capability(READ))):
    rows = session.scalars(select(EConsult).where(
        EConsult.requester == requester)
        .order_by(EConsult.created_at.asc(), EConsult.id.asc())).all()
    items = [_econsult_out(r) for r in rows]
    return {"requester": requester, "items": items, "total": len(items)}


@router.get("/econsults")
def list_econsults(patient: str | None = Query(default=None),
                   specialty: str | None = Query(default=None),
                   state: str | None = Query(default=None),
                   requester: str | None = Query(default=None),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability(READ))):
    stmt = select(EConsult)
    if patient:
        stmt = stmt.where(EConsult.patient_ref == fhir._normalize_patient_ref(patient))
    if specialty:
        stmt = stmt.where(EConsult.specialty == specialty)
    if state:
        stmt = stmt.where(EConsult.state == state)
    if requester:
        stmt = stmt.where(EConsult.requester == requester)
    rows = session.scalars(stmt.order_by(EConsult.created_at.asc(), EConsult.id.asc())).all()
    return {"items": [_econsult_out(r) for r in rows], "total": len(rows)}


@router.get("/econsults/{econsult_id}")
def get_econsult(econsult_id: str, session: Session = Depends(get_db),
                 user: User = Depends(require_capability(READ))):
    return _econsult_out(_econsult_row(session, econsult_id))


def _create_followup_referral(session: Session, user: User, row: EConsult) -> str:
    """The needs_visit referral — exact shape: a FHIR ServiceRequest,
    status 'draft', intent 'proposal', category coding
    {system: urn:opencare:referral, code: referral}, code coding
    {system: urn:opencare:referral-service, code: <specialty>},
    subject = the patient, requester.display = the answering user,
    reasonCode text 'eConsult follow-up', an eConsult extension
    (urn:opencare:extension/econsult, valueString = eConsult id) and
    a note citing the source eConsult."""
    referral_id = str(uuid.uuid4())
    body = {
        "resourceType": "ServiceRequest",
        "id": referral_id,
        "status": "draft",
        "intent": "proposal",
        "category": [{"coding": [{"system": "urn:opencare:referral",
                                  "code": "referral", "display": "Referral"}]}],
        "code": {"coding": [{"system": "urn:opencare:referral-service",
                             "code": row.specialty, "display": row.specialty}],
                 "text": row.specialty},
        "subject": {"reference": row.patient_ref},
        "authoredOn": _now_iso(),
        "requester": {"display": user.username},
        "reasonCode": [{"text": "eConsult follow-up"}],
        "note": [{"text": f"Created from eConsult {row.econsult_id}: the "
                          "specialist's advice-only answer flagged needs_visit."}],
        "extension": [{"url": ECONSULT_EXTENSION, "valueString": row.econsult_id}],
    }
    fhir.create_resource("ServiceRequest", body, session, user)
    return referral_id


@router.post("/econsults/{econsult_id}/answer")
async def answer_econsult(econsult_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability(WORK))):
    """Answer IN CHART: advice + recommendations (+ needs_visit flag)
    complete the eConsult — no visit is created. needs_visit creates
    the draft follow-up referral (see _create_followup_referral)."""
    payload = await _json_body(request)
    row = _econsult_row(session, econsult_id)
    if row.state != "open":
        raise HTTPException(status_code=409,
                            detail=f"eConsult {econsult_id} is '{row.state}', not 'open'; only an open eConsult can be answered")
    advice = payload.get("advice") or payload.get("answer") or payload.get("answer_text")
    if not isinstance(advice, str) or not advice.strip():
        raise HTTPException(status_code=422, detail="Answer requires 'advice' (the specialist's advice text)")
    recommendations = payload.get("recommendations")
    if recommendations is not None and not isinstance(recommendations, str):
        raise HTTPException(status_code=422, detail="'recommendations' must be a string")
    needs_visit = payload.get("needs_visit", payload.get("needsVisit", False))
    if not isinstance(needs_visit, bool):
        raise HTTPException(status_code=422, detail="'needs_visit' must be a boolean")
    row.answer_text = advice
    row.recommendations = recommendations
    row.needs_visit = needs_visit
    row.answered_by = user.username
    row.answered_at = _now()
    row.state = "answered"
    session.flush()
    referral_id = None
    if needs_visit:
        referral_id = _create_followup_referral(session, user, row)
        row.referral_id = referral_id
        session.flush()
    append_audit(session, actor=user.username, action="coord.econsult.answer",
                 entity=f"EConsult/{econsult_id}",
                 payload={"patient": row.patient_ref, "specialty": row.specialty,
                          "needs_visit": needs_visit, "referral_id": referral_id})
    session.commit()
    return _econsult_out(row)


@router.post("/econsults/{econsult_id}/close")
def close_econsult(econsult_id: str, session: Session = Depends(get_db),
                   user: User = Depends(require_capability(WORK))):
    row = _econsult_row(session, econsult_id)
    if row.state != "answered":
        raise HTTPException(status_code=409,
                            detail=f"eConsult {econsult_id} is '{row.state}', not 'answered'; only an answered eConsult can close")
    row.state = "closed"
    session.flush()
    append_audit(session, actor=user.username, action="coord.econsult.close",
                 entity=f"EConsult/{econsult_id}", payload={"patient": row.patient_ref})
    session.commit()
    return _econsult_out(row)


@router.post("/econsults/{econsult_id}/cancel")
async def cancel_econsult(econsult_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    row = _econsult_row(session, econsult_id)
    if row.state not in ("open", "answered"):
        raise HTTPException(status_code=409,
                            detail=f"eConsult {econsult_id} is '{row.state}' and cannot be cancelled")
    reason = payload.get("reason") or payload.get("cancel_reason")
    if not isinstance(reason, str) or not reason.strip():
        raise HTTPException(status_code=422, detail="Cancellation requires 'reason'")
    row.state = "cancelled"
    row.cancel_reason = reason
    session.flush()
    append_audit(session, actor=user.username, action="coord.econsult.cancel",
                 entity=f"EConsult/{econsult_id}",
                 payload={"patient": row.patient_ref, "reason": reason})
    session.commit()
    return _econsult_out(row)


_COMPARATORS = {"gt": lambda v, t: v > t, "gte": lambda v, t: v >= t,
                "lt": lambda v, t: v < t, "lte": lambda v, t: v <= t,
                "eq": lambda v, t: v == t}


def _latest_observation_row(session: Session, patient_ref: str, code: str):
    """The FHIR Observation row carrying the patient's latest value
    for `code` (the citation source for a triggering reference). The
    VALUE evaluation itself reuses app.continuum's latest-measurement
    helper over the FHIR store (app.pophealth's helpers operate on
    the OMOP warehouse, not the chart, so they do not apply here)."""
    from .continuum import _latest_observation, _coding_code, _parse_dt
    value, at = _latest_observation(session, patient_ref, code)
    if value is None:
        return None, None, None
    best = None
    for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Observation",
            FhirResource.patient_ref == patient_ref)).all():
        if _coding_code(row.body or {}) != code:
            continue
        qty = (row.body or {}).get("valueQuantity") or {}
        if qty.get("value") != value:
            continue
        when = _parse_dt((row.body or {}).get("effectiveDateTime")) or _aware(row.created_at)
        if when == at:
            best = row
            break
        if best is None:
            best = row
    return value, at, best


@router.post("/econsults/scan")
def scan_econsult_triggers(session: Session = Depends(get_db),
                           user: User = Depends(require_capability(WORK))):
    """Rule-triggered initiation (TACo pattern): evaluate every
    published eConsult trigger deterministically over the FHIR store.
    A patient fires a trigger when their latest Observation for the
    trigger's code satisfies the configured comparator/threshold AND
    they have no OPEN eConsult for that specialty. Idempotent: a
    second scan creates nothing while one is open. Every automatic
    creation is audited with the trigger id and cites the triggering
    Observation reference in the record."""
    from .continuum import _latest_observation  # noqa: F401  (evaluation helper)
    cfg = published_config(session, "terminology", TRIGGERS_KEY)
    triggers = (cfg.payload or {}).get("triggers") or [] if cfg else []
    created, evaluated = [], set()
    for trigger in triggers:
        if not isinstance(trigger, dict) or not trigger.get("id"):
            continue
        code = trigger.get("observationCode")
        compare = _COMPARATORS.get(trigger.get("comparator") or "gt")
        threshold = trigger.get("threshold")
        if not code or compare is None or not isinstance(threshold, (int, float)):
            continue
        try:
            specialty = _specialty(session, str(trigger.get("specialty")))
        except HTTPException:
            continue
        patient_refs = sorted({r.patient_ref for r in session.scalars(
            select(FhirResource).where(FhirResource.resource_type == "Observation")).all()
            if r.patient_ref})
        for patient_ref in patient_refs:
            value, _at, obs_row = _latest_observation_row(session, patient_ref, str(code))
            if value is None:
                continue
            evaluated.add(patient_ref)
            if not compare(float(value), float(threshold)):
                continue
            open_existing = session.scalars(select(EConsult).where(
                EConsult.patient_ref == patient_ref,
                EConsult.specialty == specialty["code"],
                EConsult.state == "open")).first()
            if open_existing is not None:
                continue
            triggering_ref = f"Observation/{obs_row.resource_id}" if obs_row else None
            row = _create_econsult(
                session, user.username, patient_ref=patient_ref,
                specialty_concept=specialty,
                question=str(trigger.get("question") or f"Auto-triggered eConsult ({trigger['id']})"),
                chart_refs=[triggering_ref] if triggering_ref else [],
                requester=f"trigger:{trigger['id']}",
                trigger_id=str(trigger["id"]), triggering_ref=triggering_ref)
            append_audit(session, actor=user.username,
                         action="coord.econsult.trigger_create",
                         entity=f"EConsult/{row.econsult_id}",
                         payload={"trigger_id": trigger["id"],
                                  "triggering_observation": triggering_ref,
                                  "observation_code": code, "value": value,
                                  "threshold": threshold,
                                  "patient": patient_ref,
                                  "specialty": specialty["code"]})
            created.append(_econsult_out(row))
    session.commit()
    return {"created": created, "created_count": len(created),
            "evaluated_patients": len(evaluated),
            "triggers": [t.get("id") for t in triggers if isinstance(t, dict)]}


# ---------------------------------------------------------------------------
# BD-25 — Multispecialty episodes (own table: EpisodeOfCare is not in
# app.fhir.SUPPORTED)
# ---------------------------------------------------------------------------

def _episode_out(row: Episode) -> dict:
    return {"id": row.episode_id, "episode_id": row.episode_id,
            "patient": row.patient_ref, "patient_ref": row.patient_ref,
            "coordinator": row.coordinator_username,
            "coordinator_username": row.coordinator_username,
            "coordinator_staff_id": row.coordinator_staff_id,
            "title": row.title, "condition": row.condition,
            "status": row.status, "links": row.links or [],
            "created_by": row.created_by,
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None}


def _contribution_out(row: EpisodeContribution) -> dict:
    return {"id": row.contribution_id, "contribution_id": row.contribution_id,
            "episode_id": row.episode_id, "specialty": row.specialty,
            "author": row.author, "summary": row.summary,
            "linked_refs": row.linked_refs or [],
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None}


def _episode_row(session: Session, episode_id: str) -> Episode:
    row = session.scalars(select(Episode).where(Episode.episode_id == episode_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")
    return row


@router.post("/episodes", status_code=201)
async def create_episode(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(require_capability(WORK))):
    """One episode per case. Without an explicit coordinator, the
    episode inherits the patient's CURRENT coordinator assignment
    (BD-24 carry-through)."""
    payload = await _json_body(request)
    patient_ref = _patient_ref(session, payload)
    title = _require_str(payload, "title") if payload.get("title") else \
        (_require_str(payload, "condition") if payload.get("condition") else None)
    if not title:
        raise HTTPException(status_code=422, detail="Episode requires 'title' (or 'condition')")
    condition = payload.get("condition") if isinstance(payload.get("condition"), str) else title
    coordinator_username = coordinator_staff_id = None
    explicit = payload.get("coordinator") or payload.get("coordinator_username") \
        or payload.get("coordinator_staff_id")
    if explicit:
        staff = _staff(session, str(explicit))
        coordinator_username, coordinator_staff_id = staff.username, staff.staff_id
    else:
        current = _current_assignment(session, patient_ref)
        if current is not None:
            coordinator_username = current.staff_username
            coordinator_staff_id = current.staff_id
    row = Episode(episode_id=str(uuid.uuid4()), patient_ref=patient_ref,
                  coordinator_username=coordinator_username,
                  coordinator_staff_id=coordinator_staff_id,
                  title=title, condition=condition, status="active",
                  links=[], created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="coord.episode.create",
                 entity=f"Episode/{row.episode_id}",
                 payload={"patient": patient_ref, "title": title,
                          "coordinator": coordinator_username,
                          "inherited_coordinator": bool(not explicit and coordinator_username)})
    session.commit()
    return _episode_out(row)


@router.get("/episodes")
def list_episodes(patient: str | None = Query(default=None),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability(READ))):
    stmt = select(Episode)
    if patient:
        stmt = stmt.where(Episode.patient_ref == fhir._normalize_patient_ref(patient))
    rows = session.scalars(stmt.order_by(Episode.id.asc())).all()
    return {"items": [_episode_out(r) for r in rows], "total": len(rows)}


@router.get("/episodes/{episode_id}")
def get_episode(episode_id: str, session: Session = Depends(get_db),
                user: User = Depends(require_capability(READ))):
    return _episode_out(_episode_row(session, episode_id))


@router.post("/episodes/{episode_id}/links", status_code=201)
async def add_episode_link(episode_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability(WORK))):
    """Attach a typed linked reference (encounter / order / note /
    observation / appointment / task / referral). The type is
    validated and the reference must name a real FHIR resource of a
    matching type (404 otherwise)."""
    payload = await _json_body(request)
    row = _episode_row(session, episode_id)
    link_type = _require_str(payload, "type")
    if link_type not in EPISODE_LINK_TYPES:
        raise HTTPException(status_code=422,
                            detail=f"Unknown link type '{link_type}': must be one of {sorted(EPISODE_LINK_TYPES)}")
    ref = payload.get("ref") or payload.get("reference")
    if not isinstance(ref, str) or "/" not in ref:
        raise HTTPException(status_code=422, detail="Link requires 'ref' as a FHIR reference (Type/id)")
    resource_type, resource_id = ref.split("/", 1)
    if resource_type not in EPISODE_LINK_TYPES[link_type]:
        raise HTTPException(status_code=422,
                            detail=f"Link type '{link_type}' requires a reference of type "
                                   f"{' or '.join(EPISODE_LINK_TYPES[link_type])}, got '{resource_type}'")
    fhir._get_row(resource_type, resource_id, session)  # 404 if unknown
    link = {"ref": ref, "reference": ref, "type": link_type,
            "added_by": user.username, "added_at": _now_iso()}
    row.links = [*(row.links or []), link]
    session.flush()
    append_audit(session, actor=user.username, action="coord.episode.link",
                 entity=f"Episode/{episode_id}",
                 payload={"ref": ref, "type": link_type, "patient": row.patient_ref})
    session.commit()
    return _episode_out(row)


@router.post("/episodes/{episode_id}/contributions", status_code=201)
async def add_contribution(episode_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    episode = _episode_row(session, episode_id)
    specialty = _specialty(session, _require_str(payload, "specialty"))
    summary = _require_str(payload, "summary")
    author = payload.get("author") if isinstance(payload.get("author"), str) \
        and payload.get("author") else user.username
    linked_refs = payload.get("linked_refs") or payload.get("linkedRefs") or []
    if not isinstance(linked_refs, list):
        raise HTTPException(status_code=422, detail="'linked_refs' must be a list")
    row = EpisodeContribution(
        contribution_id=str(uuid.uuid4()), episode_id=episode_id,
        specialty=specialty["code"], author=author, summary=summary,
        linked_refs=[str(r) for r in linked_refs])
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="coord.episode.contribution",
                 entity=f"Episode/{episode_id}",
                 payload={"contribution_id": row.contribution_id,
                          "specialty": row.specialty, "author": author,
                          "patient": episode.patient_ref})
    session.commit()
    return _contribution_out(row)


@router.get("/episodes/{episode_id}/assembly")
def episode_assembly(episode_id: str, session: Session = Depends(get_db),
                     user: User = Depends(require_capability(READ))):
    """The whole coordinated plan: the episode, its coordinator, all
    contributions grouped by specialty, and every linked reference."""
    episode = _episode_row(session, episode_id)
    contributions = session.scalars(
        select(EpisodeContribution).where(
            EpisodeContribution.episode_id == episode_id)
        .order_by(EpisodeContribution.id.asc())).all()
    grouped: dict[str, list[dict]] = {}
    for contribution in contributions:
        grouped.setdefault(contribution.specialty, []).append(
            _contribution_out(contribution))
    coordinator = None
    if episode.coordinator_staff_id or episode.coordinator_username:
        coordinator = {"username": episode.coordinator_username,
                       "staff_id": episode.coordinator_staff_id}
        member = None
        if episode.coordinator_staff_id:
            member = session.scalars(select(StaffMember).where(
                StaffMember.staff_id == episode.coordinator_staff_id)).first()
        if member is not None:
            coordinator["name"] = member.full_name
            coordinator["role_label"] = None
        current = _current_assignment(session, episode.patient_ref)
        if current is not None:
            coordinator["role_label"] = current.role_label
    return {"episode": _episode_out(episode),
            "coordinator": coordinator,
            "contributions": [_contribution_out(c) for c in contributions],
            "contributions_by_specialty": grouped,
            "by_specialty": grouped,
            "links": episode.links or []}


# ---------------------------------------------------------------------------
# BD-04 — ICU module
# ---------------------------------------------------------------------------

def _latest_form_answers(session: Session, patient_ref: str, form_key: str) -> dict | None:
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "QuestionnaireResponse",
            FhirResource.patient_ref == patient_ref)
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    latest = None
    for row in rows:
        body = row.body or {}
        if str(body.get("questionnaire") or "").rstrip("/").rsplit("/", 1)[-1] != form_key:
            continue
        latest = row
    if latest is None:
        return None
    answers: dict = {}
    for item in (latest.body or {}).get("item") or []:
        if not isinstance(item, dict) or not item.get("linkId"):
            continue
        answer_list = item.get("answer") or []
        if answer_list and isinstance(answer_list[0], dict):
            value, _unit = documentation._answer_value(answer_list[0])
            answers[item["linkId"]] = value
    return {"response_id": latest.resource_id, "answers": answers,
            "authored": (latest.body or {}).get("authored")}


def _icu_handoffs(session: Session, encounter_id: str) -> list[dict]:
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "QuestionnaireResponse")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    out = []
    for row in rows:
        body = row.body or {}
        if body.get("questionnaire") != "icu-handoff":
            continue
        if ((body.get("encounter") or {}).get("reference")) != f"Encounter/{encounter_id}":
            continue
        out.append({"id": row.resource_id, "response_id": row.resource_id,
                    "encounter_id": encounter_id, "patient_ref": row.patient_ref,
                    "authored": body.get("authored"),
                    "author": (body.get("author") or {}).get("display")})
    return out


@router.get("/icu/board")
def icu_board(session: Session = Depends(get_db),
              user: User = Depends(require_capability(READ))):
    """ICU board: the ICU census FROM app.inpatient's census
    machinery (imported — the unit view is taken from its result,
    never recomputed here), joined per patient with the latest
    icu-acuity flowsheet facts, the latest icu-devices device count,
    and the encounter's ICU handoffs."""
    census_data = inpatient.census(session=session, user=user)
    icu_unit = next((u for u in census_data.get("units") or []
                     if u.get("code") == "unit-icu"), None)
    patients = []
    for bed in (icu_unit or {}).get("beds") or []:
        if bed.get("status") != "occupied" or not bed.get("patient_ref"):
            continue
        patient_ref = bed["patient_ref"]
        acuity = _latest_form_answers(session, patient_ref, "icu-acuity")
        devices = _latest_form_answers(session, patient_ref, "icu-devices")
        device_count = None
        if devices:
            raw = devices["answers"].get("device-count")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                device_count = int(raw)
            else:
                device_count = sum(1 for k in ("central-line", "arterial-line",
                                               "ett", "urinary-catheter")
                                   if devices["answers"].get(k) is True)
        handoffs = _icu_handoffs(session, bed.get("encounter_id")) \
            if bed.get("encounter_id") else []
        patients.append({
            "patient": patient_ref, "patient_ref": patient_ref,
            "encounter_id": bed.get("encounter_id"), "bed": bed.get("code"),
            "admitted_at": bed.get("admitted_at"), "los_days": bed.get("los_days"),
            "latest_acuity": (acuity["answers"].get("acuity-score") if acuity else None),
            "acuity": acuity, "devices": devices,
            "device_count": device_count,
            "handoffs": handoffs, "handoff_count": len(handoffs),
        })
    return {"unit": "unit-icu", "census": icu_unit,
            "patients": patients, "items": patients,
            "total": len(patients),
            "generated_at": _now_iso()}


@router.post("/icu/handoffs", status_code=201)
async def create_icu_handoff(request: Request, session: Session = Depends(get_db),
                             user: User = Depends(require_capability(WORK))):
    """ICU handoff: a governed form/icu-handoff submission attached
    to the patient's CURRENT (in-progress inpatient) encounter — the
    inpatient form-attachment pattern (app.continuum handoffs)."""
    payload = await _json_body(request)
    encounter_id = payload.get("encounter_id") or payload.get("encounterId")
    if not encounter_id:
        # Attach to the patient's current encounter.
        patient_ref = _patient_ref(session, payload)
        current = inpatient._active_admission_row(session, patient_ref)
        if current is None:
            raise HTTPException(status_code=409,
                                detail=f"No active inpatient encounter for {patient_ref} to attach the ICU handoff to")
        encounter_id = current.resource_id
    row = inpatient._inpatient_row(session, str(encounter_id))
    patient_ref = inpatient._subject_ref(row.body or {})
    stored = documentation._store_response(
        session, user, "icu-handoff", patient_ref, payload.get("answers"),
        encounter_ref=f"Encounter/{encounter_id}")
    append_audit(session, actor=user.username, action="coord.icu.handoff",
                 entity=f"Encounter/{encounter_id}",
                 payload={"response_id": stored["id"], "patient": patient_ref,
                          "form": "icu-handoff"})
    session.commit()
    return {"response": stored, "form": "icu-handoff",
            "encounter_id": str(encounter_id), "patient": patient_ref}


@router.get("/icu/handoffs")
def list_icu_handoffs(encounter_id: str | None = Query(default=None),
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability(READ))):
    if encounter_id:
        items = _icu_handoffs(session, encounter_id)
    else:
        rows = session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "QuestionnaireResponse")
            .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
        items = []
        for row in rows:
            body = row.body or {}
            if body.get("questionnaire") != "icu-handoff":
                continue
            enc_ref = (body.get("encounter") or {}).get("reference") or ""
            items.append({"id": row.resource_id, "response_id": row.resource_id,
                          "encounter_id": enc_ref.split("/", 1)[1] if "/" in enc_ref else None,
                          "patient_ref": row.patient_ref,
                          "authored": body.get("authored"),
                          "author": (body.get("author") or {}).get("display")})
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# BD-07 — Therapy scheduling (PT/OT/SLP)
# ---------------------------------------------------------------------------

def _plan_out(session: Session, row: TherapyPlan) -> dict:
    sessions = session.scalars(select(TherapySession).where(
        TherapySession.plan_id == row.plan_id).order_by(TherapySession.id.asc())).all()
    used = None
    if row.auth_id:
        auth = session.scalars(select(PriorAuth).where(
            PriorAuth.auth_id == row.auth_id)).first()
        used = auth.used_units if auth else None
    return {"id": row.plan_id, "plan_id": row.plan_id,
            "patient": row.patient_ref, "patient_ref": row.patient_ref,
            "discipline": row.discipline, "frequency_pattern": row.frequency_pattern,
            "frequency": row.frequency_pattern, "therapist": row.therapist,
            "state": row.state, "status": row.state,
            "authorisation_status": row.authorisation_status,
            "auth_id": row.auth_id, "auth_number": row.auth_number,
            "authorised_visits": row.authorised_visits,
            "used_visits": used,
            "sessions": [_session_out(s) for s in sessions],
            "session_count": len(sessions),
            "created_by": row.created_by,
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None}


def _session_out(row: TherapySession) -> dict:
    return {"id": row.session_id, "session_id": row.session_id,
            "plan_id": row.plan_id, "patient": row.patient_ref,
            "patient_ref": row.patient_ref, "discipline": row.discipline,
            "therapist": row.therapist, "appointment_id": row.appointment_id,
            "appointment_ref": f"Appointment/{row.appointment_id}" if row.appointment_id else None,
            "status": row.status, "scheduled_at": row.scheduled_at,
            "start": row.scheduled_at, "auth_consumed": row.auth_consumed,
            "completed_by": row.completed_by,
            "completed_at": _aware(row.completed_at).isoformat() if row.completed_at else None}


def _plan_row(session: Session, plan_id: str) -> TherapyPlan:
    row = session.scalars(select(TherapyPlan).where(TherapyPlan.plan_id == plan_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Therapy plan {plan_id} not found")
    return row


def _session_row(session: Session, session_id: str) -> TherapySession:
    row = session.scalars(select(TherapySession).where(
        TherapySession.session_id == session_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Therapy session {session_id} not found")
    return row


@router.post("/therapy/plans", status_code=201)
async def create_therapy_plan(request: Request, session: Session = Depends(get_db),
                              user: User = Depends(require_capability(WORK))):
    """Create a treatment plan. Total authorised visits are read
    from the patient's covering prior authorisation (app.access,
    service code = the discipline code). No covering authorisation ->
    the plan is created in state 'no-authorisation' (honestly
    labelled via authorisation_status) and books NOTHING."""
    payload = await _json_body(request)
    patient_ref = _patient_ref(session, payload)
    discipline = _discipline(session, _require_str(payload, "discipline"))
    frequency = payload.get("frequency_pattern") or payload.get("frequency")
    if frequency is not None and not isinstance(frequency, str):
        raise HTTPException(status_code=422, detail="'frequency_pattern' must be a string")
    therapist = payload.get("therapist") if isinstance(payload.get("therapist"), str) else None
    starts = payload.get("session_starts") or payload.get("sessions") or []
    if not isinstance(starts, list) or any(not isinstance(s, str) for s in starts):
        raise HTTPException(status_code=422, detail="'session_starts' must be a list of ISO datetime strings")
    auth = access.find_covering_auth(session, patient_ref, {discipline["code"]})
    plan = TherapyPlan(
        plan_id=str(uuid.uuid4()), patient_ref=patient_ref,
        discipline=discipline["code"], frequency_pattern=frequency,
        therapist=therapist,
        state="authorised" if auth else "no-authorisation",
        authorisation_status="authorised" if auth else "no-authorisation",
        auth_id=auth.auth_id if auth else None,
        auth_number=auth.auth_number if auth else None,
        authorised_visits=auth.approved_units if auth else None,
        created_by=user.username)
    session.add(plan)
    session.flush()
    made = []
    if auth:
        for start in starts:
            appointment_id = str(uuid.uuid4())
            participants = [{"actor": {"reference": patient_ref}, "status": "accepted"}]
            if therapist:
                participants.append({"actor": {"reference": f"Practitioner/{therapist}",
                                                "display": therapist},
                                     "status": "accepted"})
            body = {"resourceType": "Appointment", "id": appointment_id,
                    "status": "booked", "start": start,
                    "description": f"{discipline.get('display')} session",
                    "appointmentType": {"coding": [{
                        "system": THERAPY_SYSTEM, "code": discipline["code"],
                        "display": discipline.get("display")}]},
                    "participant": participants}
            fhir.create_resource("Appointment", body, session, user)
            record = TherapySession(
                session_id=str(uuid.uuid4()), plan_id=plan.plan_id,
                patient_ref=patient_ref, discipline=discipline["code"],
                therapist=therapist, appointment_id=appointment_id,
                status="scheduled", scheduled_at=start)
            session.add(record)
            session.flush()
            made.append(record)
    append_audit(session, actor=user.username, action="coord.therapy.plan.create",
                 entity=f"TherapyPlan/{plan.plan_id}",
                 payload={"patient": patient_ref, "discipline": plan.discipline,
                          "auth_id": plan.auth_id,
                          "authorised_visits": plan.authorised_visits,
                          "sessions_booked": len(made),
                          "authorisation_status": plan.authorisation_status})
    session.commit()
    return _plan_out(session, plan)


@router.get("/therapy/plans")
def list_therapy_plans(patient: str | None = Query(default=None),
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability(READ))):
    stmt = select(TherapyPlan)
    if patient:
        stmt = stmt.where(TherapyPlan.patient_ref == fhir._normalize_patient_ref(patient))
    rows = session.scalars(stmt.order_by(TherapyPlan.id.asc())).all()
    return {"items": [_plan_out(session, r) for r in rows], "total": len(rows)}


@router.get("/therapy/plans/{plan_id}")
def get_therapy_plan(plan_id: str, session: Session = Depends(get_db),
                     user: User = Depends(require_capability(READ))):
    return _plan_out(session, _plan_row(session, plan_id))


@router.get("/therapy/calendar")
def therapy_calendar(therapist: str = Query(...), date: str = Query(...),
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability(READ))):
    """Therapist calendar: one therapist's sessions on one date
    (YYYY-MM-DD), in scheduled order."""
    rows = session.scalars(select(TherapySession).where(
        TherapySession.therapist == therapist)
        .order_by(TherapySession.scheduled_at.asc(), TherapySession.id.asc())).all()
    items = [_session_out(r) for r in rows
             if (r.scheduled_at or "").startswith(date)]
    return {"therapist": therapist, "date": date,
            "items": items, "sessions": items, "total": len(items)}


@router.post("/therapy/sessions/{session_id}/complete")
def complete_therapy_session(session_id: str, session: Session = Depends(get_db),
                             user: User = Depends(require_capability(WORK))):
    """Complete a session: consumes ONE authorised visit (the
    covering PriorAuth's used_units increments in this transaction).
    Completing beyond the authorised count refuses 409 naming the
    count. Cancelling never consumes (see the cancel endpoint)."""
    row = _session_row(session, session_id)
    if row.status != "scheduled":
        raise HTTPException(status_code=409,
                            detail=f"Therapy session {session_id} is '{row.status}', not 'scheduled'")
    plan = _plan_row(session, row.plan_id)
    auth = None
    if plan.auth_id:
        auth = session.scalars(select(PriorAuth).where(
            PriorAuth.auth_id == plan.auth_id)).first()
    if auth is not None:
        if auth.approved_units is not None and auth.used_units >= auth.approved_units:
            raise HTTPException(
                status_code=409,
                detail=(f"Authorised visit limit reached for authorisation "
                        f"{auth.auth_number}: {auth.used_units}/{auth.approved_units} "
                        "authorised visits already used — this session cannot "
                        "be completed against it"))
        auth.used_units += 1
        row.auth_consumed = True
    elif plan.authorised_visits is not None:
        used = session.scalars(select(TherapySession).where(
            TherapySession.plan_id == plan.plan_id,
            TherapySession.status == "completed")).all()
        if len(used) >= plan.authorised_visits:
            raise HTTPException(
                status_code=409,
                detail=(f"Authorised visit limit reached: {len(used)}/{plan.authorised_visits} "
                        "authorised visits already used — this session cannot be completed"))
        row.auth_consumed = True
    row.status = "completed"
    row.completed_by = user.username
    row.completed_at = _now()
    session.flush()
    if row.appointment_id:
        appt = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Appointment",
            FhirResource.resource_id == row.appointment_id)).first()
        if appt is not None:
            body = dict(appt.body or {})
            body["status"] = "fulfilled"
            appt.body = body
            appt.status = "fulfilled"
            appt.version += 1
            session.flush()
    append_audit(session, actor=user.username, action="coord.therapy.session.complete",
                 entity=f"TherapySession/{session_id}",
                 payload={"plan_id": plan.plan_id, "patient": row.patient_ref,
                          "auth_id": plan.auth_id,
                          "used_units": auth.used_units if auth else None,
                          "approved_units": auth.approved_units if auth else plan.authorised_visits})
    session.commit()
    return _session_out(row)


@router.post("/therapy/sessions/{session_id}/cancel")
def cancel_therapy_session(session_id: str, session: Session = Depends(get_db),
                           user: User = Depends(require_capability(WORK))):
    """Cancel a session: consumes NO authorised visit."""
    row = _session_row(session, session_id)
    if row.status != "scheduled":
        raise HTTPException(status_code=409,
                            detail=f"Therapy session {session_id} is '{row.status}', not 'scheduled'")
    row.status = "cancelled"
    session.flush()
    if row.appointment_id:
        appt = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Appointment",
            FhirResource.resource_id == row.appointment_id)).first()
        if appt is not None:
            body = dict(appt.body or {})
            body["status"] = "cancelled"
            appt.body = body
            appt.status = "cancelled"
            appt.version += 1
            session.flush()
    append_audit(session, actor=user.username, action="coord.therapy.session.cancel",
                 entity=f"TherapySession/{session_id}",
                 payload={"plan_id": row.plan_id, "patient": row.patient_ref,
                          "auth_consumed": False})
    session.commit()
    return _session_out(row)
