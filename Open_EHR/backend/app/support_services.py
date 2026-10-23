"""Phase 15 — Support services & department operations.

BD-17 supply chain / materials, BD-18 diet office, BD-19 interpreter
services, BD-20 patient experience + service recovery, BD-15 CUSP
programme support, BD-16 HICS incident command, BD-26 GME operations.

Built ON the Studio customization engine like every other module
(configure, don't fork): catalogues, limits, modalities, roles and
thresholds are published Studio config seeded below; operational
records live in this module's own tables (the app.protocols pattern);
every mutation is hash-audit-chained in the same transaction.

Honesty notes:
- The procurement exchange is a FILE fixture: no live exchange
  exists. Export/import payloads are labelled basis="file-fixture".
- Interpreter video is NOT a video stack: the join URL is rendered
  from the SAME published integration/telehealth provider template
  Phase 9 uses (app.engagement), labelled an external-provider join.
- HICS activation engages the downtime POSTURE by publishing a new
  governed version of integration/downtime carrying `enabled` +
  a notice naming the incident. It deliberately does NOT set
  readOnly: the downtime middleware exempts only /studio writes, so
  read-only would lock the incident-command endpoints (log,
  situation reports, deactivation) themselves out mid-incident.
"""
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (DateTime, Float, Integer, JSON, String, Text,
                        UniqueConstraint, select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import documentation, fhir
from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import (AdtEvent, ConfigStatus, FhirResource, SafetyEvent,
                     StaffMember, StudioConfig, User, utcnow)
from .security import get_db, require_capability

router = APIRouter(prefix="/support", tags=["support_services"])

READ = "support.read"
WORK = "support.work"

SUPPLIES_KEY = "supplies"
DIET_TYPES_KEY = "diet-types"
LANGUAGES_KEY = "languages"
HICS_ROLES_KEY = "hics-roles"
GME_LIMITS_KEY = "gme-duty-limits"
SUPPLY_EXCHANGE_KEY = "supply-exchange"
INTERPRETER_CFG_KEY = "interpreter-services"
EXPERIENCE_CFG_KEY = "patient-experience"
SURVEY_FORM = "patient-experience-survey"
GME_EVAL_FORM = "gme-evaluation"
DOWNTIME_KIND, DOWNTIME_KEY = "integration", "downtime"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_dt(raw):
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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


def _require_qty(payload: dict, field: str = "quantity") -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be a positive integer")
    return value


def _patient_ref(session: Session, payload: dict) -> str:
    subject = payload.get("subject") or {}
    ref = subject.get("reference") if isinstance(subject, dict) else None
    if not ref and payload.get("patient"):
        ref = fhir._normalize_patient_ref(str(payload["patient"]))
    if not ref or not ref.startswith("Patient/"):
        raise HTTPException(status_code=422,
                            detail="Requires 'subject.reference' (or 'patient') naming a Patient")
    fhir._get_row("Patient", ref.split("/", 1)[1], session)  # 404 if unknown
    return ref


def _staff(session: Session, staff_id) -> StaffMember:
    if not isinstance(staff_id, str) or not staff_id.strip():
        raise HTTPException(status_code=422,
                            detail="'staff_id' must be a staff-directory id")
    row = session.scalars(select(StaffMember).where(
        StaffMember.staff_id == staff_id)).first()
    if row is None:
        raise HTTPException(status_code=422,
                            detail=f"Unknown staff '{staff_id}': no such staff-directory entry")
    return row


# ---------------------------------------------------------------------------
# Own tables (operational records; config stays in Studio)
# ---------------------------------------------------------------------------

class SupplyStock(Base):
    __tablename__ = "support_supply_stock"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_code: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UnitParLevel(Base):
    __tablename__ = "support_unit_par"
    __table_args__ = (UniqueConstraint("unit", "item_code"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    unit: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    item_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    par_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Requisition(Base):
    __tablename__ = "support_requisitions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    req_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    unit: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    item_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    case_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="requested", index=True)
    requested_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    approved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DietOrder(Base):
    __tablename__ = "support_diet_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    diet_type: Mapped[str] = mapped_column(String(80), nullable=False)
    texture: Mapped[str | None] = mapped_column(String(80), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active", index=True)
    tray_checked: Mapped[bool] = mapped_column(default=False)
    tray_items: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tray_checked_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class InterpreterRequest(Base):
    __tablename__ = "support_interpreter_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(80), nullable=False)
    modality: Mapped[str] = mapped_column(String(40), nullable=False)
    encounter_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    needed_by: Mapped[str | None] = mapped_column(String(60), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="requested", index=True)
    interpreter_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    agency: Mapped[str | None] = mapped_column(String(200), nullable=True)
    join_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SurveyInstance(Base):
    __tablename__ = "support_survey_instances"
    __table_args__ = (UniqueConstraint("encounter_id", "patient_ref"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    encounter_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    adt_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    form_key: Mapped[str] = mapped_column(String(120), nullable=False, default=SURVEY_FORM)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="distributed", index=True)
    response_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RecoveryItem(Base):
    __tablename__ = "support_recovery_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False)  # survey | complaint
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    survey_instance_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="open", index=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CuspTeam(Base):
    __tablename__ = "support_cusp_teams"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    unit: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    members: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    facilitator: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CuspMeeting(Base):
    __tablename__ = "support_cusp_meetings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    unit: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    meeting_date: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    attendees: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    topics: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decisions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CuspDefect(Base):
    __tablename__ = "support_cusp_defects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    defect_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    unit: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    safety_event_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="open", index=True)
    follow_up_due: Mapped[str | None] = mapped_column(String(20), nullable=True)  # ISO date
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class HicsActivation(Base):
    __tablename__ = "support_hics_activations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    activation_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    incident_name: Mapped[str] = mapped_column(String(300), nullable=False)
    incident_type: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    assignments: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active", index=True)
    downtime_prior: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    activated_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HicsRecord(Base):
    __tablename__ = "support_hics_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    activation_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)  # log | situation-report
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    author: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GmeDutyLog(Base):
    __tablename__ = "support_gme_duty_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    log_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    trainee_staff_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hours: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GmeRotation(Base):
    __tablename__ = "support_gme_rotations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rotation_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    trainee_staff_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    service_line: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    start_date: Mapped[str] = mapped_column(String(20), nullable=False)
    end_date: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GmeEvaluation(Base):
    __tablename__ = "support_gme_evaluations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    eval_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    trainee_staff_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    response_id: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Default Studio configuration (seeded published v1 at startup)
# ---------------------------------------------------------------------------

def _form(title, items):
    return {"resourceType": "Questionnaire", "title": title, "item": items}


def _likert(link_id, text, required=False):
    return {"linkId": link_id, "text": text, "type": "choice",
            "required": required,
            "answerOption": [{"valueCoding": {"code": str(n), "display": str(n)}}
                             for n in (1, 2, 3, 4, 5)]}


def _item(link_id, text, type_, required=False):
    return {"linkId": link_id, "text": text, "type": type_, "required": required}


def default_supplies_payload() -> dict:
    def sup(code, display, unit, stock, category="general"):
        return {"code": code, "display": display, "active": True,
                "unit": unit, "category": category, "initialStock": stock}
    return {"name": "OpenCare non-pharmacy supplies catalogue",
            "system": "urn:opencare:supplies",
            "concepts": [
                sup("gloves-nitrile", "Nitrile gloves", "box", 100, "ppe"),
                sup("surgical-mask", "Surgical masks", "box", 25, "ppe"),
                sup("gauze-pads", "Gauze pads", "pack", 50, "wound-care"),
                sup("wound-dressing", "Wound dressing", "each", 30, "wound-care"),
                sup("iv-catheter-20g", "IV catheter 20G", "each", 40, "iv"),
                sup("syringe-10ml", "Syringe 10 mL", "each", 60, "iv"),
            ]}


def default_diet_types_payload() -> dict:
    def diet(code, display):
        return {"code": code, "display": display, "active": True}
    return {"name": "OpenCare diet types",
            "system": "urn:opencare:diet-type",
            "concepts": [diet("regular", "Regular"), diet("diabetic", "Diabetic / carbohydrate-consistent"),
                         diet("renal", "Renal"), diet("cardiac", "Cardiac"),
                         diet("low-sodium", "Low sodium"), diet("clear-liquid", "Clear liquid"),
                         diet("full-liquid", "Full liquid"), diet("soft", "Soft"),
                         diet("npo", "NPO (nil by mouth)")],
            "textures": [{"code": "normal", "display": "Normal"},
                         {"code": "mechanical-soft", "display": "Mechanical soft"},
                         {"code": "minced", "display": "Minced"},
                         {"code": "pureed", "display": "Pureed"},
                         {"code": "liquid", "display": "Liquid"}]}


def default_languages_payload() -> dict:
    def lang(code, display):
        return {"code": code, "display": display, "active": True}
    return {"name": "OpenCare interpreter languages",
            "system": "urn:opencare:language",
            "concepts": [lang("spanish", "Spanish"), lang("mandarin", "Mandarin"),
                         lang("cantonese", "Cantonese"), lang("vietnamese", "Vietnamese"),
                         lang("korean", "Korean"), lang("arabic", "Arabic"),
                         lang("russian", "Russian"), lang("tagalog", "Tagalog"),
                         lang("hindi", "Hindi"), lang("asl", "American Sign Language")]}


def default_hics_roles_payload() -> dict:
    def role(code, display):
        return {"code": code, "display": display, "active": True}
    return {"name": "HICS incident command roles",
            "system": "urn:opencare:hics-role",
            "concepts": [role("incident-commander", "Incident Commander"),
                         role("operations", "Operations Section Chief"),
                         role("planning", "Planning Section Chief"),
                         role("logistics", "Logistics Section Chief"),
                         role("liaison", "Liaison Officer"),
                         role("safety-officer", "Safety Officer"),
                         role("public-information", "Public Information Officer")]}


def default_gme_limits_payload() -> dict:
    return {"name": "GME duty-hour limits (synthetic defaults)",
            "system": "urn:opencare:gme-duty-limit",
            "concepts": [
                {"code": "max-weekly-hours", "display": "Maximum weekly hours", "active": True, "value": 80},
                {"code": "max-continuous-hours", "display": "Maximum continuous hours", "active": True, "value": 24},
                {"code": "min-rest-hours", "display": "Minimum rest hours between shifts", "active": True, "value": 8}],
            "maxWeeklyHours": 80, "maxContinuousHours": 24, "minRestHours": 8}


def default_form_payloads() -> dict[str, dict]:
    return {
        SURVEY_FORM: _form("Patient experience survey (HCAHPS-class)", [
            _likert("overall-rating", "Overall rating of this hospital (1 = worst, 5 = best)", True),
            _likert("nurse-communication", "Nurses communicated well", True),
            _likert("doctor-communication", "Doctors communicated well"),
            _likert("responsiveness", "Staff responded promptly to the call button"),
            _likert("cleanliness", "Room and bathroom were clean"),
            _likert("discharge-information", "Discharge information was clear"),
            _item("comments", "Anything else you would like to tell us?", "text"),
        ]),
        GME_EVAL_FORM: _form("GME trainee evaluation", [
            _item("clinical-competence", "Clinical competence (1-5)", "integer", True),
            _item("professionalism", "Professionalism (1-5)", "integer", True),
            _item("comments", "Evaluator comments", "text"),
        ]),
    }


def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
    existing = session.scalars(select(StudioConfig).where(
        StudioConfig.kind == kind, StudioConfig.key == key)).first()
    if existing is not None:
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


def ensure_support_services_defaults(session: Session) -> None:
    """Seed this module's published Studio configuration defaults
    (idempotent) and the central stock counters from the supplies
    catalogue's initialStock (the pharmacy-stock pattern)."""
    _seed_published(session, "terminology", SUPPLIES_KEY, default_supplies_payload())
    _seed_published(session, "terminology", DIET_TYPES_KEY, default_diet_types_payload())
    _seed_published(session, "terminology", LANGUAGES_KEY, default_languages_payload())
    _seed_published(session, "terminology", HICS_ROLES_KEY, default_hics_roles_payload())
    _seed_published(session, "terminology", GME_LIMITS_KEY, default_gme_limits_payload())
    _seed_published(session, "integration", SUPPLY_EXCHANGE_KEY, {
        "mode": "file", "basis": "file-fixture",
        "description": ("Procurement exchange over exchanged JSON files. "
                        "No live supply exchange is connected; exports and "
                        "import confirmations are file fixtures, labelled "
                        "basis=file-fixture in every payload.")})
    _seed_published(session, "integration", INTERPRETER_CFG_KEY, {
        "modalities": ["in-person", "phone", "video"],
        "description": ("Interpreter fulfilment modalities. Video joins "
                        "render from the published integration/telehealth "
                        "provider template (Phase 9) — an external "
                        "provider; OpenCare builds no video stack.")})
    _seed_published(session, "integration", EXPERIENCE_CFG_KEY, {
        "recoveryThreshold": 2, "smallCellThreshold": 5,
        "description": ("A survey response with any Likert score at or "
                        "below recoveryThreshold opens a service-recovery "
                        "workqueue item. Summary cells below "
                        "smallCellThreshold responses are suppressed.")})
    for key, payload in default_form_payloads().items():
        _seed_published(session, "form", key, payload)
    for concept in default_supplies_payload()["concepts"]:
        code = concept["code"]
        if session.scalars(select(SupplyStock).where(
                SupplyStock.item_code == code)).first() is None:
            session.add(SupplyStock(item_code=code,
                                    quantity=int(concept.get("initialStock") or 0)))
    session.flush()
    session.commit()


# ---------------------------------------------------------------------------
# Config accessors
# ---------------------------------------------------------------------------

def _published(session: Session, kind: str, key: str) -> dict:
    cfg = published_config(session, kind, key)
    if cfg is None:
        raise HTTPException(status_code=503,
                            detail=f"No published config {kind}/{key}; publish one in Studio first")
    return cfg.payload or {}


def _concepts(session: Session, key: str) -> list[dict]:
    return [c for c in _published(session, "terminology", key).get("concepts") or []
            if isinstance(c, dict)]


def _concept(session: Session, key: str, code) -> dict | None:
    for concept in _concepts(session, key):
        if concept.get("code") == code:
            return concept
    return None


def _require_concept(session: Session, key: str, code, label: str) -> dict:
    concept = _concept(session, key, code) if isinstance(code, str) else None
    if concept is None:
        raise HTTPException(status_code=422,
                            detail=f"Unknown {label} '{code}': not in the published terminology/{key} catalogue")
    if concept.get("active") is False:
        raise HTTPException(status_code=422,
                            detail=f"{label} '{code}' is inactive in the published terminology/{key} catalogue")
    return concept


def _publish_new_version(session: Session, actor: str, kind: str, key: str,
                         payload: dict, action_prefix: str) -> int:
    """Publish `payload` as the next governed version of kind/key
    (superseding the current published row) with hash-chained audit —
    the studio_config publish machinery, driven programmatically."""
    current = published_config(session, kind, key)
    versions = list_config_versions(session, kind, key)
    if current is not None:
        current.status = ConfigStatus.superseded
        session.flush()
    version = (versions[-1].version + 1) if versions else 1
    row = StudioConfig(kind=kind, key=key, version=version,
                       status=ConfigStatus.published, payload=payload,
                       created_by=actor, updated_by=actor)
    session.add(row)
    session.flush()
    for action in ("studio.config.create", "studio.config.publish"):
        append_audit(session, actor=actor, action=action,
                     entity=f"config:{kind}/{key}@v{version}",
                     payload={"kind": kind, "key": key, "version": version,
                              "via": action_prefix})
    return version


# ---------------------------------------------------------------------------
# BD-17 — Supply chain / materials
# ---------------------------------------------------------------------------

def _stock_row(session: Session, item_code: str) -> SupplyStock | None:
    return session.scalars(select(SupplyStock).where(
        SupplyStock.item_code == item_code)).first()


def _req_out(row: Requisition) -> dict:
    return {"id": row.req_id, "unit": row.unit, "item_code": row.item_code,
            "quantity": row.quantity, "case_ref": row.case_ref,
            "status": row.status, "requested_by": row.requested_by,
            "approved_by": row.approved_by, "reject_reason": row.reject_reason,
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None}


def _req_row(session: Session, req_id: str) -> Requisition:
    row = session.scalars(select(Requisition).where(
        Requisition.req_id == req_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Requisition {req_id} not found")
    return row


def _fulfil(session: Session, user: User, row: Requisition) -> None:
    """Deduct a requisition from central stock (409 on insufficient —
    the pharmacy oversell pattern: refused before any write)."""
    stock = _stock_row(session, row.item_code)
    on_hand = stock.quantity if stock else 0
    if on_hand < row.quantity:
        raise HTTPException(status_code=409,
                            detail=f"Insufficient central stock for '{row.item_code}': "
                                   f"{on_hand} on hand, {row.quantity} requested")
    stock.quantity -= row.quantity
    row.status = "fulfilled"
    session.flush()
    append_audit(session, actor=user.username, action="support.supply.fulfil",
                 entity=f"requisition:{row.req_id}",
                 payload={"item_code": row.item_code, "quantity": row.quantity,
                          "central_remaining": stock.quantity})


@router.get("/supplies/catalogue")
def supplies_catalogue(session: Session = Depends(get_db),
                       user: User = Depends(require_capability(READ))):
    cfg = published_config(session, "terminology", SUPPLIES_KEY)
    return {"catalogue": cfg.payload if cfg else None,
            "version": cfg.version if cfg else None}


@router.get("/supplies/stock")
def supplies_stock(session: Session = Depends(get_db),
                   user: User = Depends(require_capability(READ))):
    rows = session.scalars(select(SupplyStock).order_by(SupplyStock.item_code)).all()
    items = [{"item_code": r.item_code, "quantity": r.quantity} for r in rows]
    return {"items": items, "total": len(items)}


@router.post("/supplies/stock", status_code=201)
async def supplies_stock_set(request: Request, session: Session = Depends(get_db),
                             user: User = Depends(require_capability(WORK))):
    """Set the central stock counter for one catalogue item (absolute)."""
    payload = await _json_body(request)
    item_code = _require_str(payload, "item_code")
    _require_concept(session, SUPPLIES_KEY, item_code, "supply item")
    quantity = payload.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        raise HTTPException(status_code=422, detail="'quantity' must be a non-negative integer")
    row = _stock_row(session, item_code)
    if row is None:
        row = SupplyStock(item_code=item_code, quantity=quantity)
        session.add(row)
    else:
        row.quantity = quantity
    session.flush()
    append_audit(session, actor=user.username, action="support.supply.stock.set",
                 entity=f"supply-stock:{item_code}", payload={"quantity": quantity})
    session.commit()
    return {"item_code": item_code, "quantity": quantity}


@router.get("/supplies/par")
def supplies_par(unit: str | None = Query(default=None),
                 session: Session = Depends(get_db),
                 user: User = Depends(require_capability(READ))):
    stmt = select(UnitParLevel).order_by(UnitParLevel.unit, UnitParLevel.item_code)
    if unit:
        stmt = stmt.where(UnitParLevel.unit == unit)
    rows = session.scalars(stmt).all()
    items = [{"unit": r.unit, "item_code": r.item_code, "par_level": r.par_level,
              "on_hand": r.on_hand, "below_par": r.on_hand < r.par_level} for r in rows]
    return {"items": items, "total": len(items)}


@router.post("/supplies/par", status_code=201)
async def supplies_par_set(request: Request, session: Session = Depends(get_db),
                           user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    unit = _require_str(payload, "unit")
    item_code = _require_str(payload, "item_code")
    _require_concept(session, SUPPLIES_KEY, item_code, "supply item")
    par = payload.get("par_level")
    on_hand = payload.get("on_hand", 0)
    for field, value in (("par_level", par), ("on_hand", on_hand)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HTTPException(status_code=422, detail=f"'{field}' must be a non-negative integer")
    row = session.scalars(select(UnitParLevel).where(
        UnitParLevel.unit == unit, UnitParLevel.item_code == item_code)).first()
    if row is None:
        row = UnitParLevel(unit=unit, item_code=item_code, par_level=par, on_hand=on_hand)
        session.add(row)
    else:
        row.par_level, row.on_hand = par, on_hand
    session.flush()
    append_audit(session, actor=user.username, action="support.supply.par.set",
                 entity=f"unit-par:{unit}/{item_code}",
                 payload={"par_level": par, "on_hand": on_hand})
    session.commit()
    return {"unit": unit, "item_code": item_code, "par_level": par, "on_hand": on_hand}


@router.post("/requisitions", status_code=201)
async def create_requisition(request: Request, session: Session = Depends(get_db),
                             user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    unit = _require_str(payload, "unit")
    item_code = _require_str(payload, "item_code")
    _require_concept(session, SUPPLIES_KEY, item_code, "supply item")
    quantity = _require_qty(payload)
    case_ref = payload.get("case_ref") or payload.get("caseRef")
    if case_ref is not None and not isinstance(case_ref, str):
        raise HTTPException(status_code=422, detail="'case_ref' must be a string")
    row = Requisition(req_id=str(uuid.uuid4()), unit=unit, item_code=item_code,
                      quantity=quantity, case_ref=case_ref,
                      requested_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="support.supply.requisition.create",
                 entity=f"requisition:{row.req_id}",
                 payload={"unit": unit, "item_code": item_code, "quantity": quantity,
                          "case_ref": case_ref})
    session.commit()
    return _req_out(row)


@router.get("/requisitions")
def list_requisitions(status: str | None = Query(default=None),
                      unit: str | None = Query(default=None),
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability(READ))):
    stmt = select(Requisition).order_by(Requisition.created_at.asc(), Requisition.id.asc())
    if status:
        stmt = stmt.where(Requisition.status == status)
    if unit:
        stmt = stmt.where(Requisition.unit == unit)
    rows = session.scalars(stmt).all()
    return {"items": [_req_out(r) for r in rows], "total": len(rows)}


def _transition_req(session: Session, user: User, row: Requisition,
                    allowed_from: set[str], target: str) -> None:
    if row.status not in allowed_from:
        raise HTTPException(status_code=409,
                            detail=f"Requisition {row.req_id} is '{row.status}'; "
                                   f"cannot move to '{target}'")


@router.post("/requisitions/{req_id}/approve")
def approve_requisition(req_id: str, session: Session = Depends(get_db),
                        user: User = Depends(require_capability(WORK))):
    """Approve a requisition. The approver must not be the requester
    (separation of duties, enforced server-side)."""
    row = _req_row(session, req_id)
    _transition_req(session, user, row, {"requested"}, "approved")
    if row.requested_by == user.username:
        raise HTTPException(status_code=403,
                            detail="The approver must not be the requester")
    row.status = "approved"
    row.approved_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="support.supply.requisition.approve",
                 entity=f"requisition:{req_id}", payload={"requested_by": row.requested_by})
    session.commit()
    return _req_out(row)


@router.post("/requisitions/{req_id}/reject")
async def reject_requisition(req_id: str, request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    reason = _require_str(payload, "reason")
    row = _req_row(session, req_id)
    _transition_req(session, user, row, {"requested", "approved"}, "rejected")
    row.status = "rejected"
    row.reject_reason = reason
    session.flush()
    append_audit(session, actor=user.username, action="support.supply.requisition.reject",
                 entity=f"requisition:{req_id}", payload={"reason": reason})
    session.commit()
    return _req_out(row)


@router.post("/requisitions/{req_id}/fulfil")
def fulfil_requisition(req_id: str, session: Session = Depends(get_db),
                       user: User = Depends(require_capability(WORK))):
    row = _req_row(session, req_id)
    _transition_req(session, user, row, {"approved"}, "fulfilled")
    _fulfil(session, user, row)
    session.commit()
    return _req_out(row)


@router.post("/requisitions/{req_id}/receive")
def receive_requisition(req_id: str, session: Session = Depends(get_db),
                        user: User = Depends(require_capability(WORK))):
    """Confirm receipt on the requesting unit: the fulfilled quantity
    lands on the unit's PAR on-hand."""
    row = _req_row(session, req_id)
    _transition_req(session, user, row, {"fulfilled"}, "received")
    row.status = "received"
    par = session.scalars(select(UnitParLevel).where(
        UnitParLevel.unit == row.unit,
        UnitParLevel.item_code == row.item_code)).first()
    if par is None:
        par = UnitParLevel(unit=row.unit, item_code=row.item_code,
                           par_level=0, on_hand=row.quantity)
        session.add(par)
    else:
        par.on_hand += row.quantity
    session.flush()
    append_audit(session, actor=user.username, action="support.supply.requisition.receive",
                 entity=f"requisition:{req_id}",
                 payload={"unit": row.unit, "item_code": row.item_code,
                          "quantity": row.quantity, "unit_on_hand": par.on_hand})
    session.commit()
    return _req_out(row)


@router.get("/supplies/exchange/export")
def supply_exchange_export(session: Session = Depends(get_db),
                           user: User = Depends(require_capability(READ))):
    """The requisition file artefact (JSON) for the file-based
    procurement exchange. Fixture only — no live exchange exists."""
    cfg = published_config(session, "integration", SUPPLY_EXCHANGE_KEY)
    payload = (cfg.payload or {}) if cfg else {}
    rows = session.scalars(select(Requisition).order_by(
        Requisition.created_at.asc(), Requisition.id.asc())).all()
    artefact = {"basis": "file-fixture", "mode": payload.get("mode", "file"),
                "generated_at": _now().isoformat(),
                "requisitions": [_req_out(r) for r in rows]}
    append_audit(session, actor=user.username, action="support.supply.exchange.export",
                 entity="supply-exchange:export",
                 payload={"count": len(rows), "basis": "file-fixture"})
    session.commit()
    return artefact


@router.post("/supplies/exchange/import")
async def supply_exchange_import(request: Request, session: Session = Depends(get_db),
                                 user: User = Depends(require_capability(WORK))):
    """Accept fulfilment confirmations from an exchanged file:
    each confirmation names a requisition and an outcome
    (fulfilled | rejected). A fulfilment deducts central stock
    exactly like the in-system fulfil path (409 per item is
    reported in the results, never silently dropped)."""
    payload = await _json_body(request)
    confirmations = payload.get("confirmations")
    if not isinstance(confirmations, list) or not confirmations:
        raise HTTPException(status_code=422,
                            detail="'confirmations' must be a non-empty list")
    results = []
    for conf in confirmations:
        if not isinstance(conf, dict) or not conf.get("requisition_id"):
            results.append({"requisition_id": None, "outcome": "error",
                            "detail": "confirmation requires 'requisition_id'"})
            continue
        req_id = str(conf["requisition_id"])
        outcome = conf.get("outcome")
        row = session.scalars(select(Requisition).where(
            Requisition.req_id == req_id)).first()
        if row is None:
            results.append({"requisition_id": req_id, "outcome": "error",
                            "detail": "unknown requisition"})
            continue
        if outcome == "fulfilled":
            if row.status != "approved":
                results.append({"requisition_id": req_id, "outcome": "error",
                                "detail": f"requisition is '{row.status}', not 'approved'"})
                continue
            try:
                _fulfil(session, user, row)
            except HTTPException as err:
                results.append({"requisition_id": req_id, "outcome": "error",
                                "detail": err.detail})
                continue
            results.append({"requisition_id": req_id, "outcome": "fulfilled"})
        elif outcome == "rejected":
            if row.status not in ("requested", "approved"):
                results.append({"requisition_id": req_id, "outcome": "error",
                                "detail": f"requisition is '{row.status}'"})
                continue
            row.status = "rejected"
            row.reject_reason = str(conf.get("reason") or "rejected by supplier (file fixture)")
            session.flush()
            results.append({"requisition_id": req_id, "outcome": "rejected"})
        else:
            results.append({"requisition_id": req_id, "outcome": "error",
                            "detail": f"unknown outcome '{outcome}'"})
    append_audit(session, actor=user.username, action="support.supply.exchange.import",
                 entity="supply-exchange:import",
                 payload={"basis": "file-fixture", "results": results})
    session.commit()
    return {"basis": "file-fixture", "results": results,
            "applied": sum(1 for r in results if r["outcome"] in ("fulfilled", "rejected"))}


# ---------------------------------------------------------------------------
# BD-18 — Diet office
# ---------------------------------------------------------------------------

def _diet_out(row: DietOrder) -> dict:
    return {"id": row.order_id, "patient": row.patient_ref,
            "diet_type": row.diet_type, "texture": row.texture,
            "notes": row.notes, "unit": row.unit, "status": row.status,
            "tray_checked": bool(row.tray_checked),
            "tray_items": row.tray_items, "tray_checked_by": row.tray_checked_by,
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None}


def _diet_row(session: Session, order_id: str) -> DietOrder:
    row = session.scalars(select(DietOrder).where(
        DietOrder.order_id == order_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Diet order {order_id} not found")
    return row


def _derive_unit(session: Session, patient_ref: str) -> str | None:
    """The unit of the patient's current bed, derived from the
    inpatient machinery (active IMP admission -> current bed ->
    the bed concept's parent unit in terminology/locations)."""
    from . import inpatient
    admission = inpatient._active_admission_row(session, patient_ref)
    if admission is None:
        return None
    bed_code = inpatient._current_bed(admission.body or {})
    if not bed_code:
        return None
    concept = inpatient._concept(session, inpatient.LOCATIONS_KEY,
                                 "locations", bed_code)
    return concept.get("parent") if concept else None


@router.post("/diet/orders", status_code=201)
async def create_diet_order(request: Request, session: Session = Depends(get_db),
                            user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    patient_ref = _patient_ref(session, payload)
    diet_type = _require_str(payload, "diet_type")
    _require_concept(session, DIET_TYPES_KEY, diet_type, "diet type")
    texture = payload.get("texture")
    if texture is not None:
        textures = {t.get("code") for t in
                    _published(session, "terminology", DIET_TYPES_KEY).get("textures") or []
                    if isinstance(t, dict)}
        if texture not in textures:
            raise HTTPException(status_code=422,
                                detail=f"Unknown texture '{texture}': not in the published "
                                       "terminology/diet-types texture list")
    unit = payload.get("unit")
    if unit is not None and not isinstance(unit, str):
        raise HTTPException(status_code=422, detail="'unit' must be a string")
    row = DietOrder(order_id=str(uuid.uuid4()), patient_ref=patient_ref,
                    diet_type=diet_type, texture=texture,
                    notes=payload.get("notes") if isinstance(payload.get("notes"), str) else None,
                    unit=unit or None)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="support.diet.order.create",
                 entity=f"diet-order:{row.order_id}",
                 payload={"patient": patient_ref, "diet_type": diet_type,
                          "texture": texture, "unit": row.unit})
    session.commit()
    return _diet_out(row)


@router.get("/diet/orders")
def list_diet_orders(status: str | None = Query(default=None),
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability(READ))):
    """The diet-office worklist."""
    stmt = select(DietOrder).order_by(DietOrder.created_at.asc(), DietOrder.id.asc())
    if status:
        stmt = stmt.where(DietOrder.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_diet_out(r) for r in rows], "total": len(rows)}


@router.get("/diet/census")
def diet_census(unit: str | None = Query(default=None),
                session: Session = Depends(get_db),
                user: User = Depends(require_capability(READ))):
    """Meal-rounding census: ACTIVE diet orders grouped by unit +
    diet type. The unit is the order's explicit unit when one was
    recorded, else derived from the patient's current inpatient bed
    (app.inpatient); orders with neither are grouped as 'unassigned'.
    Each item names its unit source so the derivation is never hidden."""
    rows = session.scalars(select(DietOrder).where(
        DietOrder.status == "active").order_by(DietOrder.id.asc())).all()
    groups: dict[tuple[str, str], dict] = {}
    for row in rows:
        if row.unit:
            order_unit, source = row.unit, "order"
        else:
            derived = _derive_unit(session, row.patient_ref)
            order_unit, source = (derived, "bed") if derived else ("unassigned", "none")
        if unit and order_unit != unit:
            continue
        key = (order_unit, row.diet_type)
        entry = groups.setdefault(key, {"unit": order_unit, "diet_type": row.diet_type,
                                        "unit_source": source, "count": 0})
        entry["count"] += 1
    items = sorted(groups.values(), key=lambda e: (e["unit"], e["diet_type"]))
    return {"items": items, "total_orders": sum(i["count"] for i in items)}


@router.post("/diet/orders/{order_id}/tray-check")
async def tray_check(order_id: str, request: Request,
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability(WORK))):
    """Tray-assembly allergy check (Phase 5 pattern): the tray items
    are cross-checked against the patient's ACTIVE AllergyIntolerance
    resources; a match is a HARD STOP 422 naming the allergen. A clean
    tray records the check (audited) and marks the order tray-checked."""
    payload = await _json_body(request)
    items = payload.get("items")
    if not isinstance(items, list) or not items or not all(
            isinstance(i, str) and i.strip() for i in items):
        raise HTTPException(status_code=422,
                            detail="'items' must be a non-empty list of tray item names/codes")
    row = _diet_row(session, order_id)
    from . import orders as orders_mod
    # Active allergies for this patient, resolved once (code + display):
    # tray items are kitchen names, allergy codes are clinical codes,
    # so both the code and the display text are matched.
    allergies = []
    for allergy in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "AllergyIntolerance",
            FhirResource.patient_ref == row.patient_ref)).all():
        body = allergy.body or {}
        status = ((body.get("clinicalStatus") or {}).get("coding") or [{}])[0].get("code")
        if status in ("inactive", "resolved"):
            continue
        concept = body.get("code") or {}
        for coding in concept.get("coding") or []:
            if isinstance(coding, dict) and coding.get("code"):
                allergies.append({"code": coding["code"],
                                  "display": coding.get("display") or concept.get("text") or coding["code"]})
    for item in items:
        conflict = orders_mod._allergy_conflict(session, row.patient_ref, item.strip())
        if conflict is None:
            for allergy in allergies:
                if item.strip().lower() in (allergy["code"].lower(),
                                            allergy["display"].lower()):
                    conflict = allergy
                    break
        if conflict is not None:
            raise HTTPException(
                status_code=422,
                detail=f"Allergy alert: tray item '{item}' matches this patient's "
                       f"recorded allergy to {conflict['display']} (code {conflict['code']}); "
                       "the tray was refused at assembly")
    row.tray_checked = True
    row.tray_items = list(items)
    row.tray_checked_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="support.diet.tray_check",
                 entity=f"diet-order:{order_id}",
                 payload={"patient": row.patient_ref, "items": list(items),
                          "result": "clear"})
    session.commit()
    return _diet_out(row)


# ---------------------------------------------------------------------------
# BD-19 — Interpreter services
# ---------------------------------------------------------------------------

def _interp_out(row: InterpreterRequest) -> dict:
    out = {"id": row.request_id, "patient": row.patient_ref,
           "language": row.language, "modality": row.modality,
           "encounter_ref": row.encounter_ref, "needed_by": row.needed_by,
           "status": row.status, "interpreter_name": row.interpreter_name,
           "agency": row.agency,
           "requested_at": _aware(row.requested_at).isoformat() if row.requested_at else None,
           "assigned_at": _aware(row.assigned_at).isoformat() if row.assigned_at else None,
           "completed_at": _aware(row.completed_at).isoformat() if row.completed_at else None}
    if row.modality == "video" and row.join_url:
        out["join_url"] = row.join_url
        out["join_basis"] = "external-provider join URL rendered from the published integration/telehealth template"
    return out


def _interp_row(session: Session, request_id: str) -> InterpreterRequest:
    row = session.scalars(select(InterpreterRequest).where(
        InterpreterRequest.request_id == request_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Interpreter request {request_id} not found")
    return row


def _render_join_url(session: Session, request_id: str) -> str | None:
    """Render the video join URL from the SAME published telehealth
    provider template Phase 9 uses (app.engagement's config)."""
    from . import engagement
    config = engagement._telehealth_config(session)
    template = (config.get("provider") or {}).get("joinUrlTemplate")
    if not isinstance(template, str) or not template:
        return None
    return (template.replace("{appointmentId}", request_id)
            .replace("{requestId}", request_id).replace("{id}", request_id))


@router.post("/interpreter/requests", status_code=201)
async def create_interpreter_request(request: Request, session: Session = Depends(get_db),
                                     user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    patient_ref = _patient_ref(session, payload)
    language = _require_str(payload, "language")
    _require_concept(session, LANGUAGES_KEY, language, "language")
    modality = _require_str(payload, "modality")
    cfg = _published(session, "integration", INTERPRETER_CFG_KEY)
    modalities = cfg.get("modalities") or ["in-person", "phone", "video"]
    if modality not in modalities:
        raise HTTPException(status_code=422,
                            detail=f"Unknown modality '{modality}': not in the published "
                                   "integration/interpreter-services config")
    row = InterpreterRequest(
        request_id=str(uuid.uuid4()), patient_ref=patient_ref, language=language,
        modality=modality,
        encounter_ref=payload.get("encounter_ref") if isinstance(payload.get("encounter_ref"), str) else None,
        needed_by=payload.get("needed_by") if isinstance(payload.get("needed_by"), str) else None)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="support.interpreter.request",
                 entity=f"interpreter-request:{row.request_id}",
                 payload={"patient": patient_ref, "language": language, "modality": modality})
    session.commit()
    return _interp_out(row)


@router.get("/interpreter/requests")
def list_interpreter_requests(status: str | None = Query(default=None),
                              session: Session = Depends(get_db),
                              user: User = Depends(require_capability(READ))):
    stmt = select(InterpreterRequest).order_by(InterpreterRequest.requested_at.asc(),
                                               InterpreterRequest.id.asc())
    if status:
        stmt = stmt.where(InterpreterRequest.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_interp_out(r) for r in rows], "total": len(rows)}


@router.get("/interpreter/requests/{request_id}")
def get_interpreter_request(request_id: str, session: Session = Depends(get_db),
                            user: User = Depends(require_capability(READ))):
    return _interp_out(_interp_row(session, request_id))


@router.get("/interpreter/metrics")
def interpreter_metrics(session: Session = Depends(get_db),
                        user: User = Depends(require_capability(READ))):
    """Fulfilment timing metrics, computed exactly from the stored
    timestamps (minutes = elapsed seconds / 60, never rounded counts):
    request->assign and request->complete per request, plus means."""
    rows = session.scalars(select(InterpreterRequest).order_by(
        InterpreterRequest.id.asc())).all()
    items = []
    assign_minutes, complete_minutes = [], []
    for row in rows:
        requested = _aware(row.requested_at)
        to_assign = ((_aware(row.assigned_at) - requested).total_seconds() / 60.0
                     if row.assigned_at and requested else None)
        to_complete = ((_aware(row.completed_at) - requested).total_seconds() / 60.0
                       if row.completed_at and requested else None)
        if to_assign is not None:
            assign_minutes.append(to_assign)
        if to_complete is not None:
            complete_minutes.append(to_complete)
        items.append({"id": row.request_id, "status": row.status,
                      "request_to_assign_minutes": to_assign,
                      "request_to_complete_minutes": to_complete})
    return {"items": items, "total": len(items),
            "mean_request_to_assign_minutes": (
                sum(assign_minutes) / len(assign_minutes) if assign_minutes else None),
            "mean_request_to_complete_minutes": (
                sum(complete_minutes) / len(complete_minutes) if complete_minutes else None)}


def _interp_transition(session: Session, user: User, row: InterpreterRequest,
                       allowed_from: set[str], target: str) -> None:
    if row.status not in allowed_from:
        raise HTTPException(status_code=409,
                            detail=f"Interpreter request {row.request_id} is '{row.status}'; "
                                   f"cannot move to '{target}'")


@router.post("/interpreter/requests/{request_id}/assign")
async def assign_interpreter(request_id: str, request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    name = _require_str(payload, "interpreter_name")
    row = _interp_row(session, request_id)
    _interp_transition(session, user, row, {"requested"}, "assigned")
    row.status = "assigned"
    row.interpreter_name = name
    row.agency = payload.get("agency") if isinstance(payload.get("agency"), str) else None
    row.assigned_at = _now()
    if row.modality == "video":
        row.join_url = _render_join_url(session, request_id)
    session.flush()
    append_audit(session, actor=user.username, action="support.interpreter.assign",
                 entity=f"interpreter-request:{request_id}",
                 payload={"interpreter_name": name, "agency": row.agency,
                          "join_url_rendered": row.join_url is not None})
    session.commit()
    return _interp_out(row)


@router.post("/interpreter/requests/{request_id}/start")
def start_interpreter(request_id: str, session: Session = Depends(get_db),
                      user: User = Depends(require_capability(WORK))):
    row = _interp_row(session, request_id)
    _interp_transition(session, user, row, {"assigned"}, "in-progress")
    row.status = "in-progress"
    session.flush()
    append_audit(session, actor=user.username, action="support.interpreter.start",
                 entity=f"interpreter-request:{request_id}", payload={})
    session.commit()
    return _interp_out(row)


@router.post("/interpreter/requests/{request_id}/complete")
def complete_interpreter(request_id: str, session: Session = Depends(get_db),
                         user: User = Depends(require_capability(WORK))):
    row = _interp_row(session, request_id)
    _interp_transition(session, user, row, {"in-progress"}, "completed")
    row.status = "completed"
    row.completed_at = _now()
    session.flush()
    append_audit(session, actor=user.username, action="support.interpreter.complete",
                 entity=f"interpreter-request:{request_id}", payload={})
    session.commit()
    return _interp_out(row)


@router.post("/interpreter/requests/{request_id}/cancel")
def cancel_interpreter(request_id: str, session: Session = Depends(get_db),
                       user: User = Depends(require_capability(WORK))):
    row = _interp_row(session, request_id)
    _interp_transition(session, user, row,
                       {"requested", "assigned", "in-progress"}, "cancelled")
    row.status = "cancelled"
    session.flush()
    append_audit(session, actor=user.username, action="support.interpreter.cancel",
                 entity=f"interpreter-request:{request_id}", payload={})
    session.commit()
    return _interp_out(row)


# ---------------------------------------------------------------------------
# BD-20 — Patient experience + service recovery
# ---------------------------------------------------------------------------

def _survey_out(row: SurveyInstance) -> dict:
    return {"id": row.instance_id, "patient": row.patient_ref,
            "encounter_id": row.encounter_id, "adt_event_id": row.adt_event_id,
            "form_key": row.form_key, "status": row.status,
            "response_id": row.response_id,
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None}


def _recovery_out(row: RecoveryItem) -> dict:
    return {"id": row.item_id, "source": row.source, "patient": row.patient_ref,
            "survey_instance_id": row.survey_instance_id, "detail": row.detail or {},
            "status": row.status, "resolution_note": row.resolution_note,
            "created_by": row.created_by,
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None}


def _open_recovery(session: Session, user: User, source: str, patient_ref,
                   detail: dict, survey_instance_id=None) -> RecoveryItem:
    item = RecoveryItem(item_id=str(uuid.uuid4()), source=source,
                        patient_ref=patient_ref, detail=detail,
                        survey_instance_id=survey_instance_id,
                        created_by=user.username)
    session.add(item)
    session.flush()
    append_audit(session, actor=user.username, action="support.experience.recovery.open",
                 entity=f"recovery-item:{item.item_id}",
                 payload={"source": source, "patient": patient_ref, "detail": detail})
    return item


@router.post("/experience/distribute")
def distribute_surveys(session: Session = Depends(get_db),
                       user: User = Depends(require_capability(WORK))):
    """Create survey instances for discharged patients from the
    Phase 6 ADT A03 event log. Idempotent per discharge: a discharge
    (encounter + patient) that already has an instance is skipped."""
    events = session.scalars(select(AdtEvent).where(
        AdtEvent.event_type == "A03").order_by(AdtEvent.id.asc())).all()
    created = []
    for event in events:
        if not event.patient_ref or not event.encounter_id:
            continue
        existing = session.scalars(select(SurveyInstance).where(
            SurveyInstance.encounter_id == event.encounter_id,
            SurveyInstance.patient_ref == event.patient_ref)).first()
        if existing is not None:
            continue
        row = SurveyInstance(instance_id=str(uuid.uuid4()),
                             patient_ref=event.patient_ref,
                             encounter_id=event.encounter_id,
                             adt_event_id=event.id)
        session.add(row)
        session.flush()
        created.append(row)
    append_audit(session, actor=user.username, action="support.experience.distribute",
                 entity="survey:distribution",
                 payload={"discharges_seen": len(events), "created": len(created)})
    session.commit()
    return {"created": len(created),
            "instances": [_survey_out(r) for r in created]}


@router.get("/experience/instances")
def list_survey_instances(session: Session = Depends(get_db),
                          user: User = Depends(require_capability(READ))):
    rows = session.scalars(select(SurveyInstance).order_by(
        SurveyInstance.created_at.asc(), SurveyInstance.id.asc())).all()
    return {"items": [_survey_out(r) for r in rows], "total": len(rows)}


def _answer_scores(answers) -> dict[str, float]:
    """Numeric score per form linkId from QuestionnaireResponse items
    (valueInteger / valueDecimal / valueCoding code)."""
    scores: dict[str, float] = {}
    for item in answers or []:
        if not isinstance(item, dict) or not item.get("linkId"):
            continue
        for answer in item.get("answer") or []:
            if not isinstance(answer, dict):
                continue
            value = None
            if isinstance(answer.get("valueInteger"), int):
                value = float(answer["valueInteger"])
            elif isinstance(answer.get("valueDecimal"), (int, float)):
                value = float(answer["valueDecimal"])
            elif isinstance(answer.get("valueCoding"), dict):
                try:
                    value = float(answer["valueCoding"].get("code"))
                except (TypeError, ValueError):
                    value = None
            if value is not None:
                scores[item["linkId"]] = value
                break
    return scores


@router.post("/experience/instances/{instance_id}/respond", status_code=201)
async def respond_survey(instance_id: str, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability(WORK))):
    """Record a survey response through the governed form machinery
    (a QuestionnaireResponse validated against the published survey
    form, via app.documentation). Any Likert score at or below the
    published recovery threshold opens a service-recovery item."""
    payload = await _json_body(request)
    answers = payload.get("answers")
    if not isinstance(answers, list):
        raise HTTPException(status_code=422, detail="'answers' must be a list")
    row = session.scalars(select(SurveyInstance).where(
        SurveyInstance.instance_id == instance_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Survey instance {instance_id} not found")
    if row.status == "responded":
        raise HTTPException(status_code=409,
                            detail=f"Survey instance {instance_id} already has a response")
    rendered = documentation._store_response(
        session, user, row.form_key, row.patient_ref, answers)
    row.status = "responded"
    row.response_id = rendered["id"]
    session.flush()
    append_audit(session, actor=user.username, action="support.experience.respond",
                 entity=f"survey-instance:{instance_id}",
                 payload={"response_id": rendered["id"], "patient": row.patient_ref})
    cfg = _published(session, "integration", EXPERIENCE_CFG_KEY)
    threshold = cfg.get("recoveryThreshold")
    recovery = None
    if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
        low = {k: v for k, v in _answer_scores(answers).items() if v <= threshold}
        if low:
            recovery = _open_recovery(
                session, user, "survey", row.patient_ref,
                {"reason": "low survey score", "threshold": threshold,
                 "low_scores": low, "response_id": rendered["id"]},
                survey_instance_id=instance_id)
    session.commit()
    return {"instance": _survey_out(row), "response_id": rendered["id"],
            "recovery_item": _recovery_out(recovery) if recovery else None}


@router.get("/experience/summary")
def experience_summary(session: Session = Depends(get_db),
                       user: User = Depends(require_capability(READ))):
    """Per-question means + response counts over recorded survey
    responses. Small-cell suppression (the pophealth pattern): with
    fewer than the published threshold of responses, means are
    suppressed and the count renders as "<N"."""
    cfg = _published(session, "integration", EXPERIENCE_CFG_KEY)
    threshold = cfg.get("smallCellThreshold")
    threshold = threshold if isinstance(threshold, int) and threshold >= 1 else 5
    form = _published(session, "form", SURVEY_FORM)
    questions = [i.get("linkId") for i in form.get("item") or []
                 if isinstance(i, dict) and i.get("linkId")]
    instances = session.scalars(select(SurveyInstance).where(
        SurveyInstance.status == "responded")).all()
    per_question: dict[str, list[float]] = {q: [] for q in questions}
    for instance in instances:
        if not instance.response_id:
            continue
        try:
            qr = fhir._get_row("QuestionnaireResponse", instance.response_id, session)
        except HTTPException:
            continue
        for link_id, value in _answer_scores((qr.body or {}).get("item")).items():
            if link_id in per_question:
                per_question[link_id].append(value)
    n = len(instances)
    suppressed = n < threshold
    items = []
    for link_id in questions:
        values = per_question[link_id]
        items.append({
            "link_id": link_id,
            "responses": ("<" + str(threshold)) if suppressed else len(values),
            "mean": None if suppressed or not values else sum(values) / len(values),
            "suppressed": suppressed})
    return {"form_key": SURVEY_FORM, "response_count": ("<" + str(threshold)) if suppressed else n,
            "suppressed": suppressed, "small_cell_threshold": threshold,
            "questions": items}


@router.post("/experience/complaints", status_code=201)
async def create_complaint(request: Request, session: Session = Depends(get_db),
                           user: User = Depends(require_capability(WORK))):
    """A complaint record opens a service-recovery workqueue item."""
    payload = await _json_body(request)
    patient_ref = _patient_ref(session, payload)
    description = _require_str(payload, "description")
    item = _open_recovery(session, user, "complaint", patient_ref,
                          {"description": description})
    session.commit()
    return _recovery_out(item)


@router.get("/experience/recovery")
def list_recovery(status: str | None = Query(default=None),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability(READ))):
    stmt = select(RecoveryItem).order_by(RecoveryItem.created_at.asc(), RecoveryItem.id.asc())
    if status:
        stmt = stmt.where(RecoveryItem.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_recovery_out(r) for r in rows], "total": len(rows)}


_RECOVERY_TRANSITIONS = {"open": {"contacted", "resolved"},
                         "contacted": {"resolved"}, "resolved": set()}


@router.post("/experience/recovery/{item_id}/transition")
async def transition_recovery(item_id: str, request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    target = payload.get("to_status") or payload.get("status")
    if not target:
        raise HTTPException(status_code=422, detail="Transition requires 'to_status'")
    row = session.scalars(select(RecoveryItem).where(
        RecoveryItem.item_id == item_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Recovery item {item_id} not found")
    if target not in _RECOVERY_TRANSITIONS.get(row.status, set()):
        raise HTTPException(status_code=409,
                            detail=f"Illegal recovery transition '{row.status}' -> '{target}'")
    if target == "resolved":
        note = payload.get("resolution_note")
        if not isinstance(note, str) or not note.strip():
            raise HTTPException(status_code=422,
                                detail="Resolving a recovery item requires 'resolution_note'")
        row.resolution_note = note.strip()
    row.status = target
    session.flush()
    append_audit(session, actor=user.username, action="support.experience.recovery.transition",
                 entity=f"recovery-item:{item_id}", payload={"to_status": target})
    session.commit()
    return _recovery_out(row)


# ---------------------------------------------------------------------------
# BD-15 — CUSP programme support
# ---------------------------------------------------------------------------

@router.post("/cusp/teams", status_code=201)
async def create_cusp_team(request: Request, session: Session = Depends(get_db),
                           user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    unit = _require_str(payload, "unit")
    members = payload.get("members")
    if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
        raise HTTPException(status_code=422, detail="'members' must be a list of names")
    facilitator = _require_str(payload, "facilitator")
    row = CuspTeam(team_id=str(uuid.uuid4()), unit=unit, members=members,
                   facilitator=facilitator)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="support.cusp.team.create",
                 entity=f"cusp-team:{row.team_id}", payload={"unit": unit})
    session.commit()
    return {"id": row.team_id, "unit": row.unit, "members": row.members,
            "facilitator": row.facilitator}


@router.get("/cusp/teams")
def list_cusp_teams(session: Session = Depends(get_db),
                    user: User = Depends(require_capability(READ))):
    rows = session.scalars(select(CuspTeam).order_by(CuspTeam.id.asc())).all()
    items = [{"id": r.team_id, "unit": r.unit, "members": r.members,
              "facilitator": r.facilitator} for r in rows]
    return {"items": items, "total": len(items)}


@router.post("/cusp/meetings", status_code=201)
async def create_cusp_meeting(request: Request, session: Session = Depends(get_db),
                              user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    unit = _require_str(payload, "unit")
    attendees = payload.get("attendees") or []
    if not isinstance(attendees, list) or not all(isinstance(a, str) for a in attendees):
        raise HTTPException(status_code=422, detail="'attendees' must be a list of names")
    row = CuspMeeting(meeting_id=str(uuid.uuid4()), unit=unit,
                      meeting_date=str(payload.get("date") or date.today().isoformat()),
                      attendees=attendees,
                      topics=str(payload.get("topics") or ""),
                      decisions=str(payload.get("decisions") or ""))
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="support.cusp.meeting.create",
                 entity=f"cusp-meeting:{row.meeting_id}", payload={"unit": unit})
    session.commit()
    return {"id": row.meeting_id, "unit": row.unit, "date": row.meeting_date,
            "attendees": row.attendees, "topics": row.topics,
            "decisions": row.decisions}


@router.get("/cusp/meetings")
def list_cusp_meetings(unit: str | None = Query(default=None),
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability(READ))):
    stmt = select(CuspMeeting).order_by(CuspMeeting.id.asc())
    if unit:
        stmt = stmt.where(CuspMeeting.unit == unit)
    rows = session.scalars(stmt).all()
    items = [{"id": r.meeting_id, "unit": r.unit, "date": r.meeting_date,
              "attendees": r.attendees, "topics": r.topics,
              "decisions": r.decisions} for r in rows]
    return {"items": items, "total": len(items)}


def _defect_out(row: CuspDefect, today: date | None = None) -> dict:
    overdue = bool(row.follow_up_due and row.status != "closed"
                   and row.follow_up_due < (today or datetime.now(timezone.utc).date()).isoformat())
    return {"id": row.defect_id, "unit": row.unit, "description": row.description,
            "safety_event_id": row.safety_event_id, "status": row.status,
            "follow_up_due": row.follow_up_due, "follow_up_overdue": overdue}


@router.post("/cusp/defects", status_code=201)
async def create_cusp_defect(request: Request, session: Session = Depends(get_db),
                             user: User = Depends(require_capability(WORK))):
    """Defect-learning log entry. A linked safety-event id is
    validated against app.continuum's SafetyEvent model — an unknown
    event is refused (422), never stored as a dangling reference."""
    payload = await _json_body(request)
    unit = _require_str(payload, "unit")
    description = _require_str(payload, "description")
    event_id = payload.get("safety_event_id")
    if event_id is not None:
        if not isinstance(event_id, str) or not event_id:
            raise HTTPException(status_code=422, detail="'safety_event_id' must be a string")
        event = session.scalars(select(SafetyEvent).where(
            SafetyEvent.event_id == event_id)).first()
        if event is None:
            raise HTTPException(status_code=422,
                                detail=f"Unknown safety event '{event_id}': no such "
                                       "event in the continuum safety log")
    due = payload.get("follow_up_due")
    if due is not None and not isinstance(due, str):
        raise HTTPException(status_code=422, detail="'follow_up_due' must be an ISO date string")
    row = CuspDefect(defect_id=str(uuid.uuid4()), unit=unit,
                     description=description, safety_event_id=event_id,
                     follow_up_due=due)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="support.cusp.defect.create",
                 entity=f"cusp-defect:{row.defect_id}",
                 payload={"unit": unit, "safety_event_id": event_id})
    session.commit()
    return _defect_out(row)


_DEFECT_TRANSITIONS = {"open": {"learning-shared", "closed"},
                       "learning-shared": {"closed"}, "closed": set()}


@router.post("/cusp/defects/{defect_id}/transition")
async def transition_cusp_defect(defect_id: str, request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    target = payload.get("to_status") or payload.get("status")
    if not target:
        raise HTTPException(status_code=422, detail="Transition requires 'to_status'")
    row = session.scalars(select(CuspDefect).where(
        CuspDefect.defect_id == defect_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"CUSP defect {defect_id} not found")
    if target not in _DEFECT_TRANSITIONS.get(row.status, set()):
        raise HTTPException(status_code=409,
                            detail=f"Illegal defect transition '{row.status}' -> '{target}'")
    row.status = target
    session.flush()
    append_audit(session, actor=user.username, action="support.cusp.defect.transition",
                 entity=f"cusp-defect:{defect_id}", payload={"to_status": target})
    session.commit()
    return _defect_out(row)


@router.get("/cusp/worklist")
def cusp_worklist(unit: str | None = Query(default=None),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability(READ))):
    """Per-unit CUSP worklist: open defects (not closed) and the
    overdue follow-ups among them (follow_up_due before today, UTC)."""
    stmt = select(CuspDefect).where(CuspDefect.status != "closed")
    if unit:
        stmt = stmt.where(CuspDefect.unit == unit)
    rows = session.scalars(stmt.order_by(CuspDefect.id.asc())).all()
    today = datetime.now(timezone.utc).date()
    items = [_defect_out(r, today) for r in rows]
    return {"items": items, "total": len(items),
            "overdue": sum(1 for i in items if i["follow_up_overdue"])}


# ---------------------------------------------------------------------------
# BD-16 — HICS incident command
# ---------------------------------------------------------------------------

def _hics_out(session: Session, row: HicsActivation) -> dict:
    records = session.scalars(select(HicsRecord).where(
        HicsRecord.activation_id == row.activation_id)
        .order_by(HicsRecord.id.asc())).all()
    return {"id": row.activation_id, "incident_name": row.incident_name,
            "incident_type": row.incident_type, "assignments": row.assignments or {},
            "status": row.status, "activated_by": row.activated_by,
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None,
            "deactivated_at": _aware(row.deactivated_at).isoformat() if row.deactivated_at else None,
            "records": [{"id": r.record_id, "kind": r.kind, "text": r.text,
                         "author": r.author,
                         "created_at": _aware(r.created_at).isoformat() if r.created_at else None}
                        for r in records]}


def _hics_row(session: Session, activation_id: str) -> HicsActivation:
    row = session.scalars(select(HicsActivation).where(
        HicsActivation.activation_id == activation_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"HICS activation {activation_id} not found")
    return row


@router.post("/hics/activate", status_code=201)
async def hics_activate(request: Request, session: Session = Depends(get_db),
                        user: User = Depends(require_capability(WORK))):
    """Activate incident command: role->staff assignments validated
    against the published HICS roles + the staff directory. Activation
    ALSO engages the downtime posture: a new governed version of the
    published integration/downtime config (enabled + a notice naming
    the incident) is published through the governed machinery; the
    prior payload is kept on the activation for deactivation."""
    payload = await _json_body(request)
    name = _require_str(payload, "incident_name")
    incident_type = payload.get("incident_type")
    if incident_type is not None and not isinstance(incident_type, str):
        raise HTTPException(status_code=422, detail="'incident_type' must be a string")
    assignments = payload.get("assignments")
    if not isinstance(assignments, dict) or not assignments:
        raise HTTPException(status_code=422,
                            detail="'assignments' must be a non-empty object of role -> staff_id")
    valid_roles = {c.get("code") for c in _concepts(session, HICS_ROLES_KEY)}
    for role_code, staff_id in assignments.items():
        if role_code not in valid_roles:
            raise HTTPException(status_code=422,
                                detail=f"Unknown HICS role '{role_code}': not in the published "
                                       "terminology/hics-roles catalogue")
        _staff(session, staff_id)
    existing = session.scalars(select(HicsActivation).where(
        HicsActivation.status == "active")).first()
    if existing is not None:
        raise HTTPException(status_code=409,
                            detail=f"HICS activation {existing.activation_id} "
                                   f"('{existing.incident_name}') is already active")
    prior_cfg = published_config(session, DOWNTIME_KIND, DOWNTIME_KEY)
    prior = dict(prior_cfg.payload or {}) if prior_cfg else {"readOnly": False, "message": ""}
    notice = f"HICS incident active: {name} — downtime posture engaged; expect downtime procedures."
    downtime_payload = {"enabled": True, "notice": notice, "message": notice,
                        "incident": name, "incidentType": incident_type or ""}
    version = _publish_new_version(session, user.username, DOWNTIME_KIND,
                                   DOWNTIME_KEY, downtime_payload,
                                   "support.hics.activate")
    row = HicsActivation(activation_id=str(uuid.uuid4()), incident_name=name,
                         incident_type=incident_type or "",
                         assignments=dict(assignments), downtime_prior=prior,
                         activated_by=user.username)
    session.add(row)
    session.flush()
    session.add(HicsRecord(record_id=str(uuid.uuid4()),
                           activation_id=row.activation_id, kind="log",
                           text=f"Incident command activated for '{name}' "
                                f"(downtime config published as v{version}).",
                           author=user.username))
    session.flush()
    append_audit(session, actor=user.username, action="support.hics.activate",
                 entity=f"hics-activation:{row.activation_id}",
                 payload={"incident_name": name, "incident_type": incident_type,
                          "assignments": dict(assignments),
                          "downtime_version": version})
    session.commit()
    return {**_hics_out(session, row), "downtime_version": version,
            "downtime": downtime_payload}


@router.get("/hics/activations")
def list_hics(session: Session = Depends(get_db),
              user: User = Depends(require_capability(READ))):
    rows = session.scalars(select(HicsActivation).order_by(
        HicsActivation.id.asc())).all()
    return {"items": [_hics_out(session, r) for r in rows], "total": len(rows)}


@router.get("/hics/{activation_id}")
def get_hics(activation_id: str, session: Session = Depends(get_db),
             user: User = Depends(require_capability(READ))):
    return _hics_out(session, _hics_row(session, activation_id))


async def _hics_record(request: Request, session: Session, user: User,
                       activation_id: str, kind: str) -> dict:
    payload = await _json_body(request)
    text = _require_str(payload, "text")
    row = _hics_row(session, activation_id)
    if row.status != "active":
        raise HTTPException(status_code=409,
                            detail=f"HICS activation {activation_id} is '{row.status}'")
    record = HicsRecord(record_id=str(uuid.uuid4()),
                        activation_id=activation_id, kind=kind, text=text,
                        author=user.username)
    session.add(record)
    session.flush()
    append_audit(session, actor=user.username,
                 action=f"support.hics.{'log' if kind == 'log' else 'situation_report'}",
                 entity=f"hics-activation:{activation_id}",
                 payload={"record_id": record.record_id})
    session.commit()
    return {"id": record.record_id, "activation_id": activation_id,
            "kind": kind, "text": text, "author": user.username}


@router.post("/hics/{activation_id}/log", status_code=201)
async def hics_log(activation_id: str, request: Request,
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability(WORK))):
    return await _hics_record(request, session, user, activation_id, "log")


@router.post("/hics/{activation_id}/situation-reports", status_code=201)
async def hics_sitrep(activation_id: str, request: Request,
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability(WORK))):
    return await _hics_record(request, session, user, activation_id,
                              "situation-report")


@router.post("/hics/{activation_id}/deactivate")
def hics_deactivate(activation_id: str, session: Session = Depends(get_db),
                    user: User = Depends(require_capability(WORK))):
    """Deactivate: restore the downtime config to the payload that
    was published before activation (as a new governed version)."""
    row = _hics_row(session, activation_id)
    if row.status != "active":
        raise HTTPException(status_code=409,
                            detail=f"HICS activation {activation_id} is '{row.status}'")
    prior = dict(row.downtime_prior or {"readOnly": False, "message": ""})
    restored = {**prior, "enabled": False, "notice": ""}
    version = _publish_new_version(session, user.username, DOWNTIME_KIND,
                                   DOWNTIME_KEY, restored,
                                   "support.hics.deactivate")
    row.status = "deactivated"
    row.deactivated_at = _now()
    session.add(HicsRecord(record_id=str(uuid.uuid4()),
                           activation_id=activation_id, kind="log",
                           text=f"Incident command deactivated; downtime "
                                f"config restored (published as v{version}).",
                           author=user.username))
    session.flush()
    append_audit(session, actor=user.username, action="support.hics.deactivate",
                 entity=f"hics-activation:{activation_id}",
                 payload={"downtime_version": version})
    session.commit()
    return {**_hics_out(session, row), "downtime_version": version,
            "downtime": restored}


# ---------------------------------------------------------------------------
# BD-26 — GME operations
# ---------------------------------------------------------------------------

def _gme_limits(session: Session) -> dict:
    payload = _published(session, "terminology", GME_LIMITS_KEY)
    def num(key, default):
        value = payload.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    return {"max_weekly": num("maxWeeklyHours", 80.0),
            "max_continuous": num("maxContinuousHours", 24.0),
            "min_rest": num("minRestHours", 8.0)}


@router.post("/gme/duty-hours", status_code=201)
async def log_duty_hours(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(require_capability(WORK))):
    """Log one duty period. Hours are computed server-side (UTC)
    from start/end — never caller-asserted."""
    payload = await _json_body(request)
    trainee = _staff(session, payload.get("trainee_staff_id") or payload.get("trainee"))
    start = _parse_dt(payload.get("start"))
    end = _parse_dt(payload.get("end"))
    if start is None or end is None:
        raise HTTPException(status_code=422, detail="'start' and 'end' must be ISO datetimes")
    if end <= start:
        raise HTTPException(status_code=422, detail="Duty 'end' must be after 'start'")
    hours = (end - start).total_seconds() / 3600.0
    row = GmeDutyLog(log_id=str(uuid.uuid4()), trainee_staff_id=trainee.staff_id,
                     starts_at=start, ends_at=end, hours=hours)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="support.gme.duty_hours.log",
                 entity=f"gme-duty-log:{row.log_id}",
                 payload={"trainee": trainee.staff_id, "hours": hours})
    session.commit()
    return {"id": row.log_id, "trainee_staff_id": row.trainee_staff_id,
            "start": _aware(row.starts_at).isoformat(),
            "end": _aware(row.ends_at).isoformat(), "hours": row.hours}


@router.get("/gme/duty-hours")
def list_duty_hours(trainee: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability(READ))):
    stmt = select(GmeDutyLog).order_by(GmeDutyLog.starts_at.asc(), GmeDutyLog.id.asc())
    if trainee:
        stmt = stmt.where(GmeDutyLog.trainee_staff_id == trainee)
    rows = session.scalars(stmt).all()
    items = [{"id": r.log_id, "trainee_staff_id": r.trainee_staff_id,
              "start": _aware(r.starts_at).isoformat(),
              "end": _aware(r.ends_at).isoformat(), "hours": r.hours} for r in rows]
    return {"items": items, "total": len(items)}


@router.get("/gme/violations")
def gme_violations(session: Session = Depends(get_db),
                   user: User = Depends(require_capability(READ))):
    """Duty-hour violations computed from the logs against the
    published limits: weekly total per ISO week, single continuous
    period, and rest gap between consecutive periods."""
    limits = _gme_limits(session)
    rows = session.scalars(select(GmeDutyLog).order_by(
        GmeDutyLog.trainee_staff_id, GmeDutyLog.starts_at.asc(),
        GmeDutyLog.id.asc())).all()
    violations = []
    by_trainee: dict[str, list[GmeDutyLog]] = {}
    for row in rows:
        by_trainee.setdefault(row.trainee_staff_id, []).append(row)
    for trainee, logs in by_trainee.items():
        weekly: dict[tuple[int, int], float] = {}
        for log in logs:
            start = _aware(log.starts_at)
            if log.hours > limits["max_continuous"]:
                violations.append({"trainee": trainee, "rule": "max-continuous-hours",
                                   "actual": log.hours, "limit": limits["max_continuous"],
                                   "log_id": log.log_id})
            iso = start.isocalendar()
            weekly[(iso[0], iso[1])] = weekly.get((iso[0], iso[1]), 0.0) + log.hours
        for (year, week), total in sorted(weekly.items()):
            if total > limits["max_weekly"]:
                violations.append({"trainee": trainee, "rule": "max-weekly-hours",
                                   "actual": total, "limit": limits["max_weekly"],
                                   "iso_week": f"{year}-W{week:02d}"})
        for prev, curr in zip(logs, logs[1:]):
            gap = (_aware(curr.starts_at) - _aware(prev.ends_at)).total_seconds() / 3600.0
            if 0 <= gap < limits["min_rest"]:
                violations.append({"trainee": trainee, "rule": "min-rest-hours",
                                   "actual": gap, "limit": limits["min_rest"],
                                   "log_id": curr.log_id})
    return {"items": violations, "total": len(violations), "limits": limits}


@router.post("/gme/evaluations", status_code=201)
async def submit_gme_evaluation(request: Request, session: Session = Depends(get_db),
                                user: User = Depends(require_capability(WORK))):
    """Submit a trainee evaluation on the governed GME evaluation
    form, through the documentation machinery (a validated
    QuestionnaireResponse whose subject is the trainee)."""
    payload = await _json_body(request)
    trainee = _staff(session, payload.get("trainee_staff_id") or payload.get("trainee"))
    answers = payload.get("answers")
    if not isinstance(answers, list):
        raise HTTPException(status_code=422, detail="'answers' must be a list")
    rendered = documentation._store_response(
        session, user, GME_EVAL_FORM, f"Practitioner/{trainee.staff_id}", answers)
    row = GmeEvaluation(eval_id=str(uuid.uuid4()),
                        trainee_staff_id=trainee.staff_id,
                        response_id=rendered["id"])
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="support.gme.evaluation.submit",
                 entity=f"gme-evaluation:{row.eval_id}",
                 payload={"trainee": trainee.staff_id, "response_id": rendered["id"]})
    session.commit()
    return {"id": row.eval_id, "trainee_staff_id": row.trainee_staff_id,
            "response_id": row.response_id, "form_key": GME_EVAL_FORM}


@router.get("/gme/evaluations")
def list_gme_evaluations(trainee: str | None = Query(default=None),
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability(READ))):
    stmt = select(GmeEvaluation).order_by(GmeEvaluation.id.asc())
    if trainee:
        stmt = stmt.where(GmeEvaluation.trainee_staff_id == trainee)
    rows = session.scalars(stmt).all()
    items = [{"id": r.eval_id, "trainee_staff_id": r.trainee_staff_id,
              "response_id": r.response_id} for r in rows]
    return {"items": items, "total": len(items)}


def _date_or_422(value, field: str) -> str:
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"'{field}' must be an ISO date string")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"'{field}' must be an ISO date (YYYY-MM-DD)")
    return value


@router.post("/gme/rotations", status_code=201)
async def create_rotation(request: Request, session: Session = Depends(get_db),
                          user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    trainee = _staff(session, payload.get("trainee_staff_id") or payload.get("trainee"))
    service_line = _require_str(payload, "service_line")
    start = _date_or_422(payload.get("start_date") or payload.get("start"), "start_date")
    end = _date_or_422(payload.get("end_date") or payload.get("end"), "end_date")
    if end < start:
        raise HTTPException(status_code=422, detail="Rotation 'end_date' must be on/after 'start_date'")
    overlap = session.scalars(select(GmeRotation).where(
        GmeRotation.trainee_staff_id == trainee.staff_id,
        GmeRotation.start_date <= end, GmeRotation.end_date >= start)).first()
    if overlap is not None:
        raise HTTPException(status_code=409,
                            detail=f"Trainee {trainee.staff_id} already has a rotation "
                                   f"({overlap.service_line}, {overlap.start_date}..{overlap.end_date}) "
                                   "overlapping this range")
    row = GmeRotation(rotation_id=str(uuid.uuid4()),
                      trainee_staff_id=trainee.staff_id,
                      service_line=service_line, start_date=start, end_date=end)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="support.gme.rotation.create",
                 entity=f"gme-rotation:{row.rotation_id}",
                 payload={"trainee": trainee.staff_id, "service_line": service_line,
                          "start_date": start, "end_date": end})
    session.commit()
    return {"id": row.rotation_id, "trainee_staff_id": row.trainee_staff_id,
            "service_line": row.service_line, "start_date": row.start_date,
            "end_date": row.end_date}


@router.get("/gme/rotations")
def list_rotations(trainee: str | None = Query(default=None),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability(READ))):
    stmt = select(GmeRotation).order_by(GmeRotation.start_date.asc(), GmeRotation.id.asc())
    if trainee:
        stmt = stmt.where(GmeRotation.trainee_staff_id == trainee)
    rows = session.scalars(stmt).all()
    items = [{"id": r.rotation_id, "trainee_staff_id": r.trainee_staff_id,
              "service_line": r.service_line, "start_date": r.start_date,
              "end_date": r.end_date} for r in rows]
    return {"items": items, "total": len(items)}
