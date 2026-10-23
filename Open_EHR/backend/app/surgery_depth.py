"""Phase 15 (E) — surgery department depth.

Builds department depth ON the Phase 4B surgery board (app.boards:
a case is a FHIR Procedure + linked Appointment) and the Phase 6
perioperative machinery (app.inpatient form attachment), following
the standing rule — configure, don't fork. Everything site-specific
is published Studio configuration, seeded published by
:func:`ensure_surgery_depth_defaults` (idempotent):

- case-request urgency levels: terminology/surgery-urgency
  (concepts carry a `rank` used by the waitlist ordering);
- OR block templates: terminology/or-blocks (payload `blocks`:
  surgeon/service x room x weekday x start/end);
- instrument tray types: terminology/instrument-trays;
- sterile processing: terminology/sterile-processing (payload:
  the tray lifecycle `states` in order, `shelfLifeDays`, and the
  sterilisation `cycleTypes` with their temperature/time floors);
- PACU scoring: terminology/pacu-scoring (Aldrete-style criteria
  with 0-2 scales, `dischargeThreshold`, `mustPass` criteria);
- outcome vocabulary: terminology/surgery-complications (payload
  `concepts` for complications + `statuses` for outcome status);
- intra-operative forms: form/intraop-counts,
  form/intraop-positioning, form/intraop-specimens.

Surfaces:

- Case requests + waitlist: POST /surgery/requests, approve /
  decline (reason required) / schedule. Scheduling an approved
  request creates a REAL case through the boards machinery's own
  shapes — the Procedure + Appointment bodies replicate
  app.boards.schedule_case exactly, reusing boards' room-concept
  and room-overlap validation helpers. GET /surgery/waitlist lists
  approved-unscheduled requests by urgency rank then wait.
- Block utilisation: GET /surgery/utilisation?date= — per room and
  per block, scheduled case minutes vs block minutes, in-block /
  non-block minutes and turnover gaps, all computed from the real
  stored appointments. A case's in-block posture is REPORTED (flag
  on the schedule response + case record), never enforced.
- Preference cards: data records keyed (surgeon, procedure code)
  with CRUD under /surgery/preference-cards. POST
  /surgery/cases/{id}/pick-list generates the case's pick list from
  its surgeon's card; implant items draw FEFO from the implant lot
  table below (mirroring app.pharmacy_ops' lot book + append-only
  DSCSA-style trace ledger; oversell is refused 409 before any lot
  moves).
- Sterile processing: tray instances under /surgery/trays walk the
  configured lifecycle (sterilising -> sterile only through a
  recorded sterilisation cycle whose parameters validate against
  the configured cycle types); assigning a tray to a case refuses
  409 unless the tray is sterile and within its shelf life.
- Intra-op record: POST /surgery/cases/{id}/intraop/{form_key}
  submits one of the three governed forms through the Phase 5/6
  form-attachment machinery; unknown form keys are 404.
- PACU gate: ensure_* publishes a new governed version of
  workflow/surgery-flow adding `pacuScoreGate: true` when absent
  (the governed-append precedent of app.inpatient note types). The
  boards pacu -> done transition consults :func:`check_pacu_gate`
  (lazy import in app.boards): with the gate on, done refuses 409
  naming the missing / below-threshold score; with it published
  off, the transition behaves exactly as before.
- Outcomes + SSI: POST /surgery/cases/{id}/outcome records the
  outcome (complications validated against published terminology);
  GET /surgery/outcomes/ssi is the infection-prevention
  SURVEILLANCE FEED of SSI cases (labelled as such in the payload).

This module's own tables live on the shared Base (the
app.protocols / app.report_engine precedent). Every mutation is
hash-audit-chained in the same transaction.
"""
import json
import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import DateTime, Integer, JSON, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import boards, documentation, fhir
from .audit import append_audit
from .configstore import (list_config_versions, published_config,
                          workflow_payload)
from .db import Base
from .models import ConfigStatus, FhirResource, StudioConfig, User, utcnow
from .security import get_db, require_capability

router = APIRouter(prefix="/surgery", tags=["surgery_depth"])

URGENCY_KEY = "surgery-urgency"
BLOCKS_KEY = "or-blocks"
TRAY_TYPES_KEY = "instrument-trays"
STERILE_KEY = "sterile-processing"
PACU_KEY = "pacu-scoring"
COMPLICATIONS_KEY = "surgery-complications"
SURGERY_WORKFLOW_KEY = boards.SURGERY_WORKFLOW_KEY

INTRAOP_FORM_KEYS = ("intraop-counts", "intraop-positioning",
                     "intraop-specimens")
INTRAOP_EXTENSION = "urn:opencare:extension/intraop-forms"
PACU_EXTENSION = "urn:opencare:extension/pacu-scores"
OUTCOME_EXTENSION = "urn:opencare:extension/surgery-outcome"

PICK_ITEM_KINDS = ("supply", "implant", "instrument-tray")
DEFAULT_BLOCK_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday",
                          "friday", "saturday", "sunday")


# ---------------------------------------------------------------------------
# This module's own tables (shared Base; protocols/report_engine precedent)
# ---------------------------------------------------------------------------

class SurgeryRequest(Base):
    """One surgical case request: requested -> approved/declined ->
    scheduled (scheduling materialises the real boards case)."""
    __tablename__ = "surgery_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(120), unique=True,
                                            nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    procedure_code: Mapped[str] = mapped_column(String(60), nullable=False)
    procedure_display: Mapped[str | None] = mapped_column(String(300), nullable=True)
    surgeon: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    urgency: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    requested_start: Mapped[str | None] = mapped_column(String(40), nullable=True)
    requested_end: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="requested", index=True)
    decline_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    case_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class PreferenceCard(Base):
    """A surgeon's preference card for one procedure code: the item
    list a case pick list is generated from."""
    __tablename__ = "surgery_preference_cards"
    __table_args__ = (UniqueConstraint("surgeon", "procedure_code"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    card_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    surgeon: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    procedure_code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class ImplantLot(Base):
    """Implant lot stock — mirrors app.models.PharmacyLot: one row
    per (item, lot) in the implant store, carrying the serial (when
    the implant is unit-serialised) and the expiry FEFO draws from."""
    __tablename__ = "surgery_implant_lots"
    __table_args__ = (UniqueConstraint("item_code", "lot_number"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    display: Mapped[str | None] = mapped_column(String(300), nullable=True)
    lot_number: Mapped[str] = mapped_column(String(120), nullable=False)
    serial: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expiry_date: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO date
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class ImplantTraceEntry(Base):
    """Append-only implant traceability ledger — mirrors
    app.models.DscsaLedgerEntry (receiving / pick events)."""
    __tablename__ = "surgery_implant_trace"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    item_code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    lot_number: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    serial: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expiry_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reference: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CasePick(Base):
    """One generated pick list for one case: the card it came from
    and the resolved items (implant items carry their FEFO lot
    allocations)."""
    __tablename__ = "surgery_case_picks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pick_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    card_id: Mapped[str] = mapped_column(String(120), nullable=False)
    surgeon: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    procedure_code: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrayInstance(Base):
    """One physical instrument tray: its configured lifecycle state,
    its sterility window (stamped by a passing sterilisation cycle),
    and the case it is currently issued to."""
    __tablename__ = "surgery_tray_instances"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tray_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    tray_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    sterilised_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    expires_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_cycle_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    assigned_case_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class SterilisationCycle(Base):
    """One sterilisation cycle run for one tray, with the parameters
    validated against the configured cycle type."""
    __tablename__ = "surgery_sterilisation_cycles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    tray_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    cycle_type: Mapped[str] = mapped_column(String(60), nullable=False)
    temperature_c: Mapped[float] = mapped_column(nullable=False)
    duration_minutes: Mapped[float] = mapped_column(nullable=False)
    indicator_result: Mapped[str] = mapped_column(String(10), nullable=False)
    run_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PacuScoreRecord(Base):
    """One recorded PACU (Aldrete-style) score for one case. The
    total and the discharge verdict are computed server-side from
    the published pacu-scoring config; the config snapshot rides on
    the row so a later config change never rewrites history."""
    __tablename__ = "surgery_pacu_scores"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    score_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    scores: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    must_pass_failures: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    passes: Mapped[bool] = mapped_column(nullable=False, default=False)
    recorded_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SurgeryOutcomeRecord(Base):
    """One case outcome (upserted per case): status, validated
    complication codes, and the SSI surveillance fields."""
    __tablename__ = "surgery_outcomes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outcome_id: Mapped[str] = mapped_column(String(120), unique=True,
                                             nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(60), nullable=False)
    complications: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    ssi: Mapped[bool] = mapped_column(nullable=False, default=False)
    ssi_onset_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    organism: Mapped[str | None] = mapped_column(String(300), nullable=True)
    recorded_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


# ---------------------------------------------------------------------------
# Small helpers (mirroring app.boards / app.inpatient conventions)
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_dt(raw) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
        raise HTTPException(status_code=422, detail=f"'{field}' is required")
    return value.strip()


def _get_extension(body: dict, url: str) -> dict | None:
    for ext in (body or {}).get("extension") or []:
        if isinstance(ext, dict) and ext.get("url") == url:
            return ext
    return None


def _set_extension(body: dict, url: str, **values) -> None:
    extensions = [e for e in (body.get("extension") or [])
                  if not (isinstance(e, dict) and e.get("url") == url)]
    extensions.append({"url": url, **values})
    body["extension"] = extensions


def _concepts(session: Session, key: str, label: str) -> list[dict]:
    cfg = published_config(session, "terminology", key)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=(f"No published {label} (terminology/{key}); publish one "
                    "in Studio before using surgery depth"))
    return [c for c in (cfg.payload or {}).get("concepts") or []
            if isinstance(c, dict)]


def _concept(session: Session, key: str, label: str, code: str) -> dict | None:
    for concept in _concepts(session, key, label):
        if concept.get("code") == code:
            return concept
    return None


def _case_or_404(session: Session, case_id: str) -> FhirResource:
    """A surgery case row, or 404 (the app.inpatient perioperative
    convention: a non-case Procedure is 'not found' as a case)."""
    row = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Procedure",
                                   FhirResource.resource_id == case_id)).first()
    if row is None or not boards._is_surgery_case(row.body or {}):
        raise HTTPException(status_code=404,
                            detail=f"Surgery case {case_id} not found")
    return row


def _case_code(body: dict) -> tuple[str | None, str | None]:
    _, code, display = boards._first_coding(body or {})
    return code, display


def _case_surgeon(session: Session, case_row: FhirResource) -> str | None:
    """The case's surgeon, from its Appointment's Practitioner
    participant (the boards schedule_case shape). Returned bare
    (no Practitioner/ prefix) — the form preference cards key on."""
    appointment = boards._case_appointment(session, case_row)
    if appointment is None:
        return None
    for participant in (appointment.body or {}).get("participant") or []:
        if isinstance(participant, dict):
            ref = ((participant.get("actor") or {}).get("reference") or "")
            if ref.startswith("Practitioner/"):
                return ref.split("/", 1)[1]
    return None


# ---------------------------------------------------------------------------
# Default Studio configuration (seeded published; governed-append for
# the surgery workflow's PACU gate flag)
# ---------------------------------------------------------------------------

def default_urgency_payload() -> dict:
    def urg(code, display, rank):
        return {"code": code, "display": display, "rank": rank, "active": True}

    return {"name": "Surgical case-request urgency levels",
            "system": "urn:opencare:surgery-urgency",
            "concepts": [
                urg("emergent", "Emergent — proceed as soon as possible", 1),
                urg("urgent", "Urgent — within days", 2),
                urg("elective", "Elective — scheduled routinely", 3),
            ]}


def default_blocks_payload() -> dict:
    return {"name": "Operating-room block templates (synthetic defaults)",
            "blocks": [
                {"code": "blk-or1-mon-ortho", "surgeon": "dr-smith",
                 "service": "orthopaedics", "room": "OR-1",
                 "weekday": "monday", "start": "08:00", "end": "16:00"},
                {"code": "blk-or1-wed-ortho", "surgeon": "dr-smith",
                 "service": "orthopaedics", "room": "OR-1",
                 "weekday": "wednesday", "start": "08:00", "end": "12:00"},
                {"code": "blk-or2-tue-general", "surgeon": "dr-jones",
                 "service": "general", "room": "OR-2",
                 "weekday": "tuesday", "start": "08:00", "end": "12:00"},
            ]}


def default_tray_types_payload() -> dict:
    def tray(code, display):
        return {"code": code, "display": display, "active": True}

    return {"name": "Instrument tray types",
            "system": "urn:opencare:instrument-tray",
            "concepts": [
                tray("major-orthopaedic", "Major orthopaedic tray"),
                tray("minor-general", "Minor general tray"),
                tray("laparoscopic", "Laparoscopic tray"),
                tray("arthroscopy", "Arthroscopy tray"),
            ]}


def default_sterile_payload() -> dict:
    return {"name": "Sterile processing (lifecycle, shelf life, cycles)",
            "states": ["in-use", "decontamination", "assembly",
                       "sterilising", "sterile", "issued"],
            "shelfLifeDays": 30,
            "cycleTypes": [
                {"code": "steam-134", "display": "Steam 134C pre-vacuum",
                 "temperatureC": 134, "minMinutes": 18},
                {"code": "steam-121", "display": "Steam 121C gravity",
                 "temperatureC": 121, "minMinutes": 30},
            ]}


def default_pacu_payload() -> dict:
    def crit(code, display):
        return {"code": code, "display": display, "max": 2, "active": True}

    return {"name": "PACU discharge scoring (modified Aldrete, site config)",
            "system": "urn:opencare:pacu-scoring",
            "concepts": [
                crit("activity", "Activity — moves extremities"),
                crit("respiration", "Respiration"),
                crit("circulation", "Circulation — BP vs pre-anaesthetic"),
                crit("consciousness", "Consciousness"),
                crit("oxygen-saturation", "Oxygen saturation"),
            ],
            "dischargeThreshold": 9,
            "mustPass": [
                {"code": "respiration", "minimum": 1},
                {"code": "oxygen-saturation", "minimum": 1},
            ]}


def default_complications_payload() -> dict:
    def comp(code, display):
        return {"code": code, "display": display, "active": True}

    return {"name": "Surgical outcome complications",
            "system": "urn:opencare:surgery-complication",
            "statuses": ["recovered", "recovering", "complication",
                         "readmitted", "deceased"],
            "concepts": [
                comp("haemorrhage", "Post-operative haemorrhage"),
                comp("wound-dehiscence", "Wound dehiscence"),
                comp("wound-infection", "Wound infection"),
                comp("deep-vein-thrombosis", "Deep vein thrombosis"),
                comp("pulmonary-embolism", "Pulmonary embolism"),
                comp("anastomotic-leak", "Anastomotic leak"),
                comp("sepsis", "Sepsis"),
                comp("myocardial-infarction", "Myocardial infarction"),
                comp("acute-kidney-injury", "Acute kidney injury"),
                comp("other", "Other complication"),
            ]}


def _form(title: str, items: list[dict]) -> dict:
    return {"resourceType": "Questionnaire", "title": title, "item": items}


def _item(link_id, text, type_, required=False):
    return {"linkId": link_id, "text": text, "type": type_, "required": required}


def _choice(link_id, text, options, required=False):
    item = _item(link_id, text, "choice", required)
    item["answerOption"] = [
        {"valueCoding": {"code": code, "display": display}}
        for code, display in options]
    return item


def default_intraop_form_payloads() -> dict[str, dict]:
    return {
        "intraop-counts": _form("Intra-operative counts", [
            _item("sponge-count-correct", "Sponge count correct", "boolean", True),
            _item("sharps-count-correct", "Sharps count correct", "boolean", True),
            _item("instrument-count-correct", "Instrument count correct",
                  "boolean", True),
            _item("sponges-used", "Sponges used", "integer"),
            _item("discrepancy-notes", "Count discrepancy notes", "text"),
        ]),
        "intraop-positioning": _form("Intra-operative positioning", [
            _choice("position", "Patient position",
                    [("supine", "Supine"), ("prone", "Prone"),
                     ("lateral", "Lateral"), ("lithotomy", "Lithotomy"),
                     ("beach-chair", "Beach chair")], True),
            _item("pressure-areas-padded", "Pressure areas padded",
                  "boolean", True),
            _item("eyes-protected", "Eyes protected", "boolean"),
            _item("notes", "Positioning notes", "text"),
        ]),
        "intraop-specimens": _form("Intra-operative specimens", [
            _item("specimen-description", "Specimen description", "text", True),
            _item("specimen-count", "Number of specimens", "integer"),
            _item("specimen-labelled", "Specimen labelled", "boolean", True),
            _item("destination", "Specimen destination", "text"),
        ]),
    }


def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
    existing = session.scalars(
        select(StudioConfig).where(StudioConfig.kind == kind,
                                   StudioConfig.key == key)).first()
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


def _extend_surgery_flow_pacu_gate(session: Session) -> None:
    """Governed-append (the app.inpatient._extend_note_types
    precedent): publish a NEW version of workflow/surgery-flow that
    adds `pacuScoreGate: true` when the published payload lacks the
    flag entirely. An explicit published true OR false is a site
    decision and is never overwritten; all other payload content is
    preserved verbatim."""
    cfg = published_config(session, "workflow", SURGERY_WORKFLOW_KEY)
    if cfg is None:
        return
    payload = cfg.payload or {}
    if "pacuScoreGate" in payload:
        return
    new_payload = dict(payload)
    new_payload["pacuScoreGate"] = True
    cfg.status = ConfigStatus.superseded
    session.flush()
    versions = list_config_versions(session, "workflow", SURGERY_WORKFLOW_KEY)
    row = StudioConfig(kind="workflow", key=SURGERY_WORKFLOW_KEY,
                       version=versions[-1].version + 1,
                       status=ConfigStatus.published, payload=new_payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    for action in ("studio.config.create", "studio.config.publish"):
        append_audit(session, actor="system", action=action,
                     entity=f"config:workflow/{SURGERY_WORKFLOW_KEY}@v{row.version}",
                     payload={"kind": "workflow", "key": SURGERY_WORKFLOW_KEY,
                              "version": row.version,
                              "phase15_extension": ["pacuScoreGate"]})


def ensure_surgery_depth_defaults(session: Session) -> None:
    """Seed the Phase 15 (E) Studio config as published v1
    (idempotent) and governed-append the PACU gate flag onto the
    published surgery workflow."""
    _seed_published(session, "terminology", URGENCY_KEY,
                    default_urgency_payload())
    _seed_published(session, "terminology", BLOCKS_KEY,
                    default_blocks_payload())
    _seed_published(session, "terminology", TRAY_TYPES_KEY,
                    default_tray_types_payload())
    _seed_published(session, "terminology", STERILE_KEY,
                    default_sterile_payload())
    _seed_published(session, "terminology", PACU_KEY,
                    default_pacu_payload())
    _seed_published(session, "terminology", COMPLICATIONS_KEY,
                    default_complications_payload())
    for key, payload in default_intraop_form_payloads().items():
        _seed_published(session, "form", key, payload)
    _extend_surgery_flow_pacu_gate(session)
    session.commit()


# ---------------------------------------------------------------------------
# Block templates + in-block assessment (terminology/or-blocks)
# ---------------------------------------------------------------------------

def _blocks(session: Session) -> list[dict]:
    cfg = published_config(session, "terminology", BLOCKS_KEY)
    if cfg is None:
        return []
    return [b for b in (cfg.payload or {}).get("blocks") or []
            if isinstance(b, dict) and b.get("room")]


def _block_window(block: dict, day: date) -> tuple[datetime, datetime] | None:
    """The block's concrete [start, end) datetimes on `day`, or None
    when the block does not fall on that weekday / is malformed."""
    weekday = str(block.get("weekday") or "").strip().lower()
    if weekday != day.strftime("%A").lower():
        return None
    try:
        start_t = time.fromisoformat(str(block.get("start")))
        end_t = time.fromisoformat(str(block.get("end")))
    except ValueError:
        return None
    start = datetime.combine(day, start_t, tzinfo=timezone.utc)
    end = datetime.combine(day, end_t, tzinfo=timezone.utc)
    if end <= start:
        return None
    return start, end


def _block_assessment(session: Session, surgeon: str | None, room: str,
                      start: datetime, end: datetime) -> dict:
    """Is this case inside one of its surgeon's published blocks?
    REPORTED, never enforced: a case is in-block when a block for the
    same surgeon + room covers the case interval on that weekday."""
    for block in _blocks(session):
        if block.get("room") != room:
            continue
        if surgeon and block.get("surgeon") != surgeon:
            continue
        window = _block_window(block, start.date())
        if window is not None and window[0] <= start and end <= window[1]:
            return {"in_block": True, "block_code": block.get("code"),
                    "block": block}
    return {"in_block": False, "block_code": None, "block": None}


# ---------------------------------------------------------------------------
# Case requests + surgical waitlist
# ---------------------------------------------------------------------------

def _urgency_rank(session: Session, code: str) -> int:
    concept = _concept(session, URGENCY_KEY, "surgery urgency levels", code)
    rank = (concept or {}).get("rank")
    if isinstance(rank, int) and not isinstance(rank, bool):
        return rank
    return 999


def _request_out(session: Session, row: SurgeryRequest, now: datetime) -> dict:
    created = _aware(row.created_at) or now
    return {
        "request_id": row.request_id,
        "patient_ref": row.patient_ref,
        "procedure_code": row.procedure_code,
        "procedure_display": row.procedure_display,
        "surgeon": row.surgeon,
        "urgency": row.urgency,
        "urgency_rank": _urgency_rank(session, row.urgency),
        "requested_window": {"start": row.requested_start,
                             "end": row.requested_end},
        "status": row.status,
        "decline_reason": row.decline_reason,
        "case_id": row.case_id,
        "created_by": row.created_by,
        "decided_by": row.decided_by,
        "created_at": created.isoformat(),
        "wait_minutes": max(0, int((now - created).total_seconds() // 60)),
        "wait_hours": round((now - created).total_seconds() / 3600, 1),
    }


def _request_or_404(session: Session, request_id: str) -> SurgeryRequest:
    row = session.scalars(select(SurgeryRequest).where(
        SurgeryRequest.request_id == request_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Surgery request '{request_id}' was not found")
    return row


@router.post("/requests", status_code=201)
async def create_request(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(require_capability("surgery.work"))):
    """Raise a surgical case request. The urgency must be an ACTIVE
    concept in the published surgery-urgency terminology; the
    requested window (when supplied) must be a valid interval."""
    payload = await _json_body(request)
    subject_ref = None
    if isinstance(payload.get("subject"), dict):
        subject_ref = payload["subject"].get("reference")
    if not subject_ref and payload.get("patient"):
        subject_ref = fhir._normalize_patient_ref(str(payload["patient"]))
    if not subject_ref and payload.get("patient_id"):
        subject_ref = f"Patient/{payload['patient_id']}"
    if not subject_ref or not subject_ref.startswith("Patient/"):
        raise HTTPException(status_code=422,
                            detail="A request requires a patient ('patient_id', "
                                   "'patient' or 'subject.reference')")
    fhir._get_row("Patient", subject_ref.split("/", 1)[1], session)  # 404
    code = _require_str(payload, "procedure_code") \
        if payload.get("procedure_code") else None
    if code is None:
        # Accept the boards shape too: 'code' / a CodeableConcept.
        raw = payload.get("code")
        if isinstance(raw, dict):
            _, code, _ = boards._first_coding({"code": raw})
        elif isinstance(raw, str):
            code = raw
    if not code:
        raise HTTPException(status_code=422,
                            detail="A request requires 'procedure_code'")
    display = payload.get("procedure_display") or payload.get("display")
    surgeon = _require_str(payload, "surgeon")
    urgency = _require_str(payload, "urgency")
    concept = _concept(session, URGENCY_KEY, "surgery urgency levels", urgency)
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown urgency '{urgency}': not in the published "
                   "surgery-urgency config")
    if concept.get("active") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Urgency '{urgency}' is inactive in the published "
                   "surgery-urgency config")
    win_start = payload.get("requested_start") or payload.get("window_start")
    win_end = payload.get("requested_end") or payload.get("window_end")
    start_dt = _parse_dt(win_start) if win_start else None
    end_dt = _parse_dt(win_end) if win_end else None
    if win_start and start_dt is None:
        raise HTTPException(status_code=422,
                            detail=f"Invalid 'requested_start' datetime: {win_start}")
    if win_end and end_dt is None:
        raise HTTPException(status_code=422,
                            detail=f"Invalid 'requested_end' datetime: {win_end}")
    if start_dt is not None and end_dt is not None and end_dt <= start_dt:
        raise HTTPException(status_code=422,
                            detail="The requested window's end must be after "
                                   "its start")
    row = SurgeryRequest(
        request_id=f"req-{uuid.uuid4()}", patient_ref=subject_ref,
        procedure_code=code,
        procedure_display=display if isinstance(display, str) else None,
        surgeon=surgeon, urgency=urgency,
        requested_start=start_dt.isoformat() if start_dt else None,
        requested_end=end_dt.isoformat() if end_dt else None,
        status="requested", created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="surgery.request.create",
                 entity=f"SurgeryRequest/{row.request_id}",
                 payload={"patient": subject_ref, "procedure_code": code,
                          "surgeon": surgeon, "urgency": urgency})
    session.commit()
    return _request_out(session, row, _now())


@router.get("/requests")
def list_requests(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("boards.read"))):
    rows = session.scalars(select(SurgeryRequest).order_by(
        SurgeryRequest.created_at.asc(), SurgeryRequest.id.asc())).all()
    now = _now()
    return {"items": [_request_out(session, r, now) for r in rows],
            "total": len(rows)}


@router.get("/requests/{request_id}")
def get_request(request_id: str, session: Session = Depends(get_db),
                user: User = Depends(require_capability("boards.read"))):
    return _request_out(session, _request_or_404(session, request_id), _now())


def _decide(session: Session, user: User, request_id: str, target: str,
            reason: str | None) -> SurgeryRequest:
    row = _request_or_404(session, request_id)
    if row.status != "requested":
        raise HTTPException(
            status_code=409,
            detail=f"Request {request_id} is '{row.status}' and can no "
                   "longer be decided")
    row.status = target
    row.decided_by = user.username
    if target == "declined":
        row.decline_reason = reason
    session.flush()
    append_audit(session, actor=user.username,
                 action=f"surgery.request.{target}",
                 entity=f"SurgeryRequest/{request_id}",
                 payload={"patient": row.patient_ref,
                          "procedure_code": row.procedure_code,
                          "reason": reason})
    session.commit()
    return row


@router.post("/requests/{request_id}/approve")
def approve_request(request_id: str, session: Session = Depends(get_db),
                    user: User = Depends(require_capability("surgery.work"))):
    row = _decide(session, user, request_id, "approved", None)
    return _request_out(session, row, _now())


@router.post("/requests/{request_id}/decline")
async def decline_request(request_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("surgery.work"))):
    """Declining a request REQUIRES a reason (422 without one) — a
    declined patient is owed an explanation on the record."""
    payload = await _json_body(request)
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise HTTPException(status_code=422,
                            detail="Declining a surgery request requires "
                                   "a 'reason'")
    row = _decide(session, user, request_id, "declined", reason.strip())
    return _request_out(session, row, _now())


@router.get("/waitlist")
def waitlist(session: Session = Depends(get_db),
             user: User = Depends(require_capability("boards.read"))):
    """The surgical waitlist: approved-but-unscheduled requests,
    ordered by urgency rank (published config) then by wait duration
    (longest waiting first), computed against now (UTC)."""
    rows = session.scalars(select(SurgeryRequest).where(
        SurgeryRequest.status == "approved")).all()
    now = _now()
    items = [_request_out(session, r, now) for r in rows]
    items.sort(key=lambda i: (i["urgency_rank"], -i["wait_minutes"],
                              i["created_at"], i["request_id"]))
    return {"items": items, "total": len(items), "generated_at": now.isoformat()}


@router.post("/requests/{request_id}/schedule", status_code=201)
async def schedule_request(request_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("surgery.work"))):
    """Schedule an APPROVED request into a real OR case. The case is
    created with the boards machinery's own resource shapes — the
    Procedure (OR-room + appointment + timeline extensions) and the
    Appointment bodies replicate app.boards.schedule_case exactly,
    reusing boards' room-concept validation and room-overlap check —
    so the case is indistinguishable from a board-scheduled case.
    The response REPORTS the case's block posture (in_block flag);
    being outside a block never blocks scheduling."""
    payload = await _json_body(request)
    row = _request_or_404(session, request_id)
    if row.status != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"Request {request_id} is '{row.status}'; only an "
                   "approved request can be scheduled")
    room_code = payload.get("room")
    if not isinstance(room_code, str) or not room_code.strip():
        raise HTTPException(status_code=422, detail="Scheduling requires 'room'")
    room = boards._concept(session, boards.OR_ROOMS_KEY, "operating rooms",
                           room_code)
    if room is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown room '{room_code}': not in the published "
                   "or-rooms config")
    if room.get("active") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Room '{room_code}' is inactive in the published "
                   "or-rooms config")
    start = boards._parse_dt(payload.get("start"))
    end = boards._parse_dt(payload.get("end"))
    if start is None or end is None:
        raise HTTPException(status_code=422,
                            detail="Scheduling requires ISO 'start' and 'end' "
                                   "datetimes")
    if end <= start:
        raise HTTPException(status_code=422,
                            detail="A case's 'end' must be after its 'start'")
    boards._check_room_overlap(session, room_code, start, end)

    case_id = str(uuid.uuid4())
    appointment_id = str(uuid.uuid4())
    display = row.procedure_display
    procedure_body = {
        "resourceType": "Procedure",
        "id": case_id,
        "status": "scheduled",
        "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt",
                             "code": row.procedure_code,
                             "display": display}],
                 "text": display or row.procedure_code},
        "subject": {"reference": row.patient_ref},
        "extension": [
            {"url": boards.OR_ROOM_EXTENSION, "valueString": room_code},
            {"url": boards.CASE_APPOINTMENT_EXTENSION,
             "valueReference": {"reference": f"Appointment/{appointment_id}"}},
            {"url": boards.CASE_TIMELINE_EXTENSION,
             "valueString": json.dumps({"events": [
                 {"event": "scheduled", "state": "scheduled",
                  "at": _now_iso(), "by": user.username}]})},
        ],
    }
    fhir.create_resource("Procedure", procedure_body, session, user)
    participants = [
        {"actor": {"reference": row.patient_ref}, "status": "accepted"},
        {"actor": {"reference": f"Practitioner/{row.surgeon}"},
         "status": "accepted"},
        {"actor": {"reference": f"Location/{room_code}",
                   "display": room.get("display")}, "status": "accepted"},
    ]
    appointment_body = {
        "resourceType": "Appointment",
        "id": appointment_id,
        "status": "booked",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "description": display or row.procedure_code,
        "participant": participants,
        "extension": [
            {"url": boards.APPOINTMENT_CASE_EXTENSION,
             "valueReference": {"reference": f"Procedure/{case_id}"}},
        ],
    }
    fhir.create_resource("Appointment", appointment_body, session, user)
    block = _block_assessment(session, row.surgeon, room_code, start, end)
    row.status = "scheduled"
    row.case_id = case_id
    session.flush()
    append_audit(session, actor=user.username, action="surgery.case.schedule",
                 entity=f"Procedure/{case_id}",
                 payload={"room": room_code, "patient": row.patient_ref,
                          "code": row.procedure_code,
                          "start": start.isoformat(), "end": end.isoformat(),
                          "appointment_id": appointment_id,
                          "request_id": request_id,
                          "in_block": block["in_block"]})
    append_audit(session, actor=user.username, action="surgery.request.schedule",
                 entity=f"SurgeryRequest/{request_id}",
                 payload={"case_id": case_id, "room": room_code,
                          "in_block": block["in_block"]})
    session.commit()
    case_row = fhir._get_row("Procedure", case_id, session)
    appt_row = fhir._get_row("Appointment", appointment_id, session)
    return {"case": fhir._render(case_row),
            "appointment": fhir._render(appt_row),
            "room": room_code, "state": "scheduled",
            "request": _request_out(session, row, _now()),
            "in_block": block["in_block"], "block_code": block["block_code"],
            "block": block["block"]}


# ---------------------------------------------------------------------------
# Block scheduling + utilisation
# ---------------------------------------------------------------------------

def _overlap_minutes(a_start: datetime, a_end: datetime,
                     b_start: datetime, b_end: datetime) -> float:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    if end <= start:
        return 0.0
    return (end - start).total_seconds() / 60.0


def _minutes(value: float):
    """Exact minutes: an int when whole, else the float."""
    return int(value) if float(value).is_integer() else value


@router.get("/utilisation")
def utilisation(date: str = Query(...),
                session: Session = Depends(get_db),
                user: User = Depends(require_capability("boards.read"))):
    """One day's OR utilisation, computed from the real stored
    appointments: per room — scheduled case minutes, block minutes
    (published or-blocks for that weekday), in-block vs non-block
    scheduled minutes, utilisation % of block time, and the turnover
    gaps between consecutive cases; per block — its scheduled
    minutes and utilisation."""
    try:
        day = datetime.fromisoformat(date).date()
    except (TypeError, ValueError):
        raise HTTPException(status_code=422,
                            detail=f"Invalid 'date' (expected YYYY-MM-DD): {date}")
    room_concepts = boards._concepts(session, boards.OR_ROOMS_KEY,
                                      "operating rooms")
    cases = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Procedure")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    by_room: dict[str, list[dict]] = {}
    for case in cases:
        if not boards._is_surgery_case(case.body or {}):
            continue
        if (case.body or {}).get("status") == "cancelled":
            continue
        appointment = boards._case_appointment(session, case)
        if appointment is None:
            continue
        appt_body = appointment.body or {}
        if (appt_body.get("status") or "") == "cancelled":
            continue
        start = boards._parse_dt(appt_body.get("start"))
        end = boards._parse_dt(appt_body.get("end"))
        if start is None or end is None or start.date() != day:
            continue
        room_code = boards._appointment_room(appt_body)
        if room_code is None:
            room_ext = _get_extension(case.body or {}, boards.OR_ROOM_EXTENSION) or {}
            room_code = room_ext.get("valueString")
        by_room.setdefault(room_code, []).append(
            {"case_id": case.resource_id, "start": start, "end": end})

    blocks = _blocks(session)
    rooms_out = []
    for concept in room_concepts:
        room_code = concept["code"]
        entries = sorted(by_room.get(room_code, []),
                         key=lambda e: (e["start"], e["case_id"]))
        scheduled = sum((e["end"] - e["start"]).total_seconds() / 60.0
                        for e in entries)
        room_blocks = []
        block_minutes = 0.0
        in_block = 0.0
        for block in blocks:
            if block.get("room") != room_code:
                continue
            window = _block_window(block, day)
            if window is None:
                continue
            span = (window[1] - window[0]).total_seconds() / 60.0
            inside = sum(_overlap_minutes(e["start"], e["end"],
                                          window[0], window[1])
                         for e in entries)
            block_minutes += span
            in_block += inside
            room_blocks.append({
                "block_code": block.get("code"), "surgeon": block.get("surgeon"),
                "service": block.get("service"),
                "start": window[0].isoformat(), "end": window[1].isoformat(),
                "block_minutes": _minutes(span),
                "scheduled_minutes": _minutes(inside),
                "utilisation_pct": (round(100.0 * inside / span, 2)
                                    if span else None),
            })
        gaps = []
        for prev, nxt in zip(entries, entries[1:]):
            gap = (nxt["start"] - prev["end"]).total_seconds() / 60.0
            if gap > 0:
                gaps.append({"after_case": prev["case_id"],
                             "before_case": nxt["case_id"],
                             "minutes": _minutes(gap)})
        rooms_out.append({
            "room": room_code, "display": concept.get("display"),
            "cases": len(entries),
            "scheduled_minutes": _minutes(scheduled),
            "block_minutes": _minutes(block_minutes),
            "in_block_minutes": _minutes(in_block),
            "non_block_minutes": _minutes(scheduled - in_block),
            "utilisation_pct": (round(100.0 * in_block / block_minutes, 2)
                                if block_minutes else None),
            "blocks": room_blocks,
            "turnover": {
                "gaps": gaps,
                "total_gap_minutes": _minutes(sum(g["minutes"] for g in gaps)),
                "count": len(gaps),
            },
        })
    return {"date": day.isoformat(),
            "weekday": day.strftime("%A").lower(),
            "rooms": rooms_out}


# ---------------------------------------------------------------------------
# Preference cards (data records keyed surgeon x procedure code)
# ---------------------------------------------------------------------------

def _validate_card_items(items) -> list[dict]:
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=422,
                            detail="'items' must be a non-empty list")
    out = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422,
                                detail=f"items[{i}] must be an object")
        kind = item.get("kind")
        if kind not in PICK_ITEM_KINDS:
            raise HTTPException(
                status_code=422,
                detail=f"items[{i}]: 'kind' must be one of "
                       f"{', '.join(PICK_ITEM_KINDS)}")
        code = item.get("code")
        if not isinstance(code, str) or not code.strip():
            raise HTTPException(status_code=422,
                                detail=f"items[{i}]: 'code' is required")
        qty = item.get("qty")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            raise HTTPException(
                status_code=422,
                detail=f"items[{i}]: 'qty' must be a positive integer")
        out.append({"kind": kind, "code": code.strip(),
                    "display": item.get("display")
                    if isinstance(item.get("display"), str) else None,
                    "qty": qty})
    return out


def _card_out(row: PreferenceCard) -> dict:
    return {"card_id": row.card_id, "surgeon": row.surgeon,
            "procedure_code": row.procedure_code, "items": row.items or [],
            "created_by": row.created_by,
            "created_at": _aware(row.created_at).isoformat()
            if row.created_at else None}


def _card_or_404(session: Session, card_id: str) -> PreferenceCard:
    row = session.scalars(select(PreferenceCard).where(
        PreferenceCard.card_id == card_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Preference card '{card_id}' was not found")
    return row


@router.post("/preference-cards", status_code=201)
async def create_card(request: Request, session: Session = Depends(get_db),
                      user: User = Depends(require_capability("surgery.work"))):
    payload = await _json_body(request)
    surgeon = _require_str(payload, "surgeon")
    code = _require_str(payload, "procedure_code")
    items = _validate_card_items(payload.get("items"))
    existing = session.scalars(select(PreferenceCard).where(
        PreferenceCard.surgeon == surgeon,
        PreferenceCard.procedure_code == code)).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A preference card already exists for surgeon "
                   f"'{surgeon}' / procedure '{code}' "
                   f"({existing.card_id}); update it instead")
    row = PreferenceCard(card_id=f"card-{uuid.uuid4()}", surgeon=surgeon,
                         procedure_code=code, items=items,
                         created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="surgery.card.create",
                 entity=f"PreferenceCard/{row.card_id}",
                 payload={"surgeon": surgeon, "procedure_code": code,
                          "items": items})
    session.commit()
    return _card_out(row)


@router.get("/preference-cards")
def list_cards(session: Session = Depends(get_db),
               user: User = Depends(require_capability("boards.read"))):
    rows = session.scalars(select(PreferenceCard).order_by(
        PreferenceCard.surgeon.asc(), PreferenceCard.procedure_code.asc())).all()
    return {"items": [_card_out(r) for r in rows], "total": len(rows)}


@router.get("/preference-cards/{card_id}")
def get_card(card_id: str, session: Session = Depends(get_db),
             user: User = Depends(require_capability("boards.read"))):
    return _card_out(_card_or_404(session, card_id))


@router.put("/preference-cards/{card_id}")
async def update_card(card_id: str, request: Request,
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("surgery.work"))):
    payload = await _json_body(request)
    row = _card_or_404(session, card_id)
    items = _validate_card_items(payload.get("items"))
    row.items = items
    session.flush()
    append_audit(session, actor=user.username, action="surgery.card.update",
                 entity=f"PreferenceCard/{card_id}",
                 payload={"surgeon": row.surgeon,
                          "procedure_code": row.procedure_code,
                          "items": items})
    session.commit()
    return _card_out(row)


@router.delete("/preference-cards/{card_id}")
def delete_card(card_id: str, session: Session = Depends(get_db),
                user: User = Depends(require_capability("surgery.work"))):
    row = _card_or_404(session, card_id)
    out = _card_out(row)
    session.delete(row)
    session.flush()
    append_audit(session, actor=user.username, action="surgery.card.delete",
                 entity=f"PreferenceCard/{card_id}",
                 payload={"surgeon": out["surgeon"],
                          "procedure_code": out["procedure_code"]})
    session.commit()
    return {"deleted": True, "card": out}


# ---------------------------------------------------------------------------
# Implant lots + trace ledger (mirroring pharmacy_ops lot book / DSCSA)
# ---------------------------------------------------------------------------

def _lot_out(lot: ImplantLot) -> dict:
    days = None
    try:
        days = (date.fromisoformat(lot.expiry_date) - date.today()).days
    except (ValueError, TypeError):
        days = None
    return {"item_code": lot.item_code, "display": lot.display,
            "lot_number": lot.lot_number, "serial": lot.serial,
            "expiry_date": lot.expiry_date, "quantity": lot.quantity,
            "days_to_expiry": days,
            "expired": days is not None and days < 0}


@router.post("/implants/lots", status_code=201)
async def receive_implant_lot(request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(require_capability("surgery.work"))):
    """Receive implant stock into a lot (the ONLY way implant stock
    arrives): lot + expiry captured, merged into an existing lot of
    the same number only when the expiry matches (422 otherwise —
    the pharmacy receiving rule), and written to the append-only
    trace ledger in the same transaction."""
    payload = await _json_body(request)
    item_code = _require_str(payload, "item_code")
    lot_number = _require_str(payload, "lot_number")
    expiry = _require_str(payload, "expiry_date")
    try:
        date.fromisoformat(expiry)
    except ValueError:
        raise HTTPException(status_code=422,
                            detail="'expiry_date' must be an ISO date")
    quantity = payload.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise HTTPException(status_code=422,
                            detail="'quantity' must be a positive integer")
    serial = payload.get("serial")
    if serial is not None and not isinstance(serial, str):
        raise HTTPException(status_code=422, detail="'serial' must be a string")
    lot = session.scalars(select(ImplantLot).where(
        ImplantLot.item_code == item_code,
        ImplantLot.lot_number == lot_number)).first()
    if lot is not None and lot.expiry_date != expiry:
        raise HTTPException(
            status_code=422,
            detail=f"Lot '{lot_number}' of '{item_code}' already exists "
                   f"with expiry {lot.expiry_date}, not {expiry}")
    if lot is None:
        lot = ImplantLot(item_code=item_code,
                         display=payload.get("display")
                         if isinstance(payload.get("display"), str) else None,
                         lot_number=lot_number, serial=serial,
                         expiry_date=expiry, quantity=0)
        session.add(lot)
        session.flush()
    lot.quantity += quantity
    session.add(ImplantTraceEntry(
        entry_id=f"trace-{uuid.uuid4()}", event="receiving",
        item_code=item_code, lot_number=lot_number, serial=serial,
        expiry_date=expiry, quantity=quantity, reference="receiving",
        detail={"display": lot.display}, actor=user.username))
    session.flush()
    append_audit(session, actor=user.username, action="surgery.implant.receive",
                 entity=f"ImplantLot/{item_code}/{lot_number}",
                 payload={"item_code": item_code, "lot_number": lot_number,
                          "expiry_date": expiry, "quantity": quantity,
                          "serial": serial, "on_hand": lot.quantity})
    session.commit()
    return _lot_out(lot)


@router.get("/implants/lots")
def list_implant_lots(session: Session = Depends(get_db),
                      user: User = Depends(require_capability("boards.read"))):
    lots = session.scalars(select(ImplantLot).order_by(
        ImplantLot.item_code.asc(), ImplantLot.expiry_date.asc(),
        ImplantLot.id.asc())).all()
    return {"items": [_lot_out(l) for l in lots], "total": len(lots)}


@router.get("/implants/trace")
def implant_trace(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("boards.read"))):
    """The append-only implant trace ledger, newest first."""
    rows = session.scalars(select(ImplantTraceEntry).order_by(
        ImplantTraceEntry.id.desc()).limit(200)).all()
    return {"items": [{
        "entry_id": r.entry_id, "event": r.event, "item_code": r.item_code,
        "lot_number": r.lot_number, "serial": r.serial,
        "expiry_date": r.expiry_date, "quantity": r.quantity,
        "reference": r.reference, "detail": r.detail or {}, "actor": r.actor,
        "created_at": _aware(r.created_at).isoformat() if r.created_at else None,
    } for r in rows], "total": len(rows)}


# ---------------------------------------------------------------------------
# Case pick lists
# ---------------------------------------------------------------------------

def _pick_out(row: CasePick) -> dict:
    return {"pick_id": row.pick_id, "case_id": row.case_id,
            "card_id": row.card_id, "surgeon": row.surgeon,
            "procedure_code": row.procedure_code, "items": row.items or [],
            "created_by": row.created_by,
            "created_at": _aware(row.created_at).isoformat()
            if row.created_at else None}


@router.post("/cases/{case_id}/pick-list", status_code=201)
def generate_pick_list(case_id: str, session: Session = Depends(get_db),
                       user: User = Depends(require_capability("surgery.work"))):
    """Generate the case's pick list from its surgeon's preference
    card for the case's procedure. Implant items are allocated FEFO
    from the implant lot book (earliest expiry first; expired lots
    never allocate). The whole pick is validated BEFORE any lot
    moves: if any implant item cannot be covered in full, the pick
    is refused 409 (oversell) and nothing is decremented. Allocations
    are written to the trace ledger and the pick is recorded against
    the case + audited."""
    case = _case_or_404(session, case_id)
    code, _display = _case_code(case.body or {})
    surgeon = _case_surgeon(session, case)
    if not code or not surgeon:
        raise HTTPException(
            status_code=422,
            detail=f"Case {case_id} lacks a procedure code or surgeon; "
                   "no preference card can be resolved")
    card = session.scalars(select(PreferenceCard).where(
        PreferenceCard.surgeon == surgeon,
        PreferenceCard.procedure_code == code)).first()
    if card is None:
        raise HTTPException(
            status_code=404,
            detail=f"No preference card for surgeon '{surgeon}' / "
                   f"procedure '{code}'")
    today = date.today()
    # ---- validation / allocation plan (nothing written yet) ----
    planned_items = []
    allocations: list[tuple[ImplantLot, int]] = []
    for item in card.items or []:
        entry = {"kind": item["kind"], "code": item["code"],
                 "display": item.get("display"), "qty": item["qty"],
                 "allocations": []}
        if item["kind"] == "implant":
            lots = session.scalars(
                select(ImplantLot).where(
                    ImplantLot.item_code == item["code"],
                    ImplantLot.quantity > 0)
                .order_by(ImplantLot.expiry_date.asc(),
                          ImplantLot.id.asc())).all()
            usable = []
            for lot in lots:
                try:
                    expired = date.fromisoformat(lot.expiry_date) < today
                except ValueError:
                    expired = True
                if not expired:
                    usable.append(lot)
            available = sum(l.quantity for l in usable)
            if available < item["qty"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"Implant '{item['code']}': pick needs "
                           f"{item['qty']} but only {available} in-date "
                           "units are on hand — pick refused (oversell)")
            remaining = item["qty"]
            for lot in usable:
                if remaining <= 0:
                    break
                take = min(lot.quantity, remaining)
                remaining -= take
                allocations.append((lot, take))
                entry["allocations"].append(
                    {"lot_number": lot.lot_number, "serial": lot.serial,
                     "expiry_date": lot.expiry_date, "qty": take})
        planned_items.append(entry)
    # ---- apply ----
    for lot, take in allocations:
        lot.quantity -= take
        session.add(ImplantTraceEntry(
            entry_id=f"trace-{uuid.uuid4()}", event="pick",
            item_code=lot.item_code, lot_number=lot.lot_number,
            serial=lot.serial, expiry_date=lot.expiry_date, quantity=take,
            reference=f"Procedure/{case_id}",
            detail={"surgeon": surgeon, "procedure_code": code},
            actor=user.username))
    pick = CasePick(pick_id=f"pick-{uuid.uuid4()}", case_id=case_id,
                    card_id=card.card_id, surgeon=surgeon,
                    procedure_code=code, items=planned_items,
                    created_by=user.username)
    session.add(pick)
    session.flush()
    append_audit(session, actor=user.username, action="surgery.case.pick-list",
                 entity=f"Procedure/{case_id}",
                 payload={"pick_id": pick.pick_id, "card_id": card.card_id,
                          "surgeon": surgeon, "procedure_code": code,
                          "implant_allocations": [
                              {"item_code": lot.item_code,
                               "lot_number": lot.lot_number, "qty": take}
                              for lot, take in allocations]})
    session.commit()
    return _pick_out(pick)


@router.get("/cases/{case_id}/picks")
def list_case_picks(case_id: str, session: Session = Depends(get_db),
                    user: User = Depends(require_capability("boards.read"))):
    _case_or_404(session, case_id)
    rows = session.scalars(select(CasePick).where(
        CasePick.case_id == case_id).order_by(CasePick.id.asc())).all()
    return {"items": [_pick_out(r) for r in rows], "total": len(rows)}


# ---------------------------------------------------------------------------
# Sterile processing (trays + sterilisation cycles)
# ---------------------------------------------------------------------------

def _sterile_config(session: Session) -> dict:
    cfg = published_config(session, "terminology", STERILE_KEY)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=f"No published sterile-processing config "
                   f"(terminology/{STERILE_KEY}); publish one in Studio")
    return cfg.payload or {}


def _tray_states(session: Session) -> list[str]:
    states = _sterile_config(session).get("states")
    if isinstance(states, list) and states and all(
            isinstance(s, str) and s for s in states):
        return list(states)
    return ["in-use", "decontamination", "assembly", "sterilising",
            "sterile", "issued"]


def _shelf_life_days(session: Session) -> int:
    days = _sterile_config(session).get("shelfLifeDays")
    if isinstance(days, int) and not isinstance(days, bool) and days > 0:
        return days
    return 30


def _cycle_types(session: Session) -> list[dict]:
    return [c for c in _sterile_config(session).get("cycleTypes") or []
            if isinstance(c, dict) and c.get("code")]


def _tray_out(row: TrayInstance) -> dict:
    return {"tray_id": row.tray_id, "tray_type": row.tray_type,
            "status": row.status, "sterilised_at": row.sterilised_at,
            "expires_at": row.expires_at, "last_cycle_id": row.last_cycle_id,
            "assigned_case_id": row.assigned_case_id}


def _tray_or_404(session: Session, tray_id: str) -> TrayInstance:
    row = session.scalars(select(TrayInstance).where(
        TrayInstance.tray_id == tray_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Tray '{tray_id}' was not found")
    return row


@router.post("/trays", status_code=201)
async def create_tray(request: Request, session: Session = Depends(get_db),
                      user: User = Depends(require_capability("surgery.work"))):
    """Register a tray instance of a published tray type. The initial
    state must be one of the configured lifecycle states (default:
    the first configured state)."""
    payload = await _json_body(request)
    tray_type = _require_str(payload, "tray_type")
    concept = _concept(session, TRAY_TYPES_KEY, "instrument tray types",
                       tray_type)
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown tray type '{tray_type}': not in the published "
                   "instrument-trays config")
    if concept.get("active") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Tray type '{tray_type}' is inactive in the published "
                   "instrument-trays config")
    states = _tray_states(session)
    status = payload.get("status") or states[0]
    if status not in states:
        raise HTTPException(
            status_code=422,
            detail=f"Initial tray state '{status}' is not in the published "
                   f"sterile-processing lifecycle ({', '.join(states)})")
    tray_id = payload.get("tray_id")
    if tray_id is not None:
        if not isinstance(tray_id, str) or not tray_id.strip():
            raise HTTPException(status_code=422,
                                detail="'tray_id' must be a non-empty string")
        tray_id = tray_id.strip()
        if session.scalars(select(TrayInstance).where(
                TrayInstance.tray_id == tray_id)).first() is not None:
            raise HTTPException(status_code=409,
                                detail=f"Tray '{tray_id}' already exists")
    else:
        tray_id = f"tray-{uuid.uuid4()}"
    row = TrayInstance(tray_id=tray_id, tray_type=tray_type, status=status,
                       created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="surgery.tray.create",
                 entity=f"Tray/{tray_id}",
                 payload={"tray_type": tray_type, "status": status})
    session.commit()
    return _tray_out(row)


@router.get("/trays")
def list_trays(session: Session = Depends(get_db),
               user: User = Depends(require_capability("boards.read"))):
    rows = session.scalars(select(TrayInstance).order_by(
        TrayInstance.id.asc())).all()
    return {"items": [_tray_out(r) for r in rows], "total": len(rows),
            "states": _tray_states(session),
            "shelf_life_days": _shelf_life_days(session)}


@router.get("/trays/{tray_id}")
def get_tray(tray_id: str, session: Session = Depends(get_db),
             user: User = Depends(require_capability("boards.read"))):
    row = _tray_or_404(session, tray_id)
    cycles = session.scalars(select(SterilisationCycle).where(
        SterilisationCycle.tray_id == tray_id).order_by(
        SterilisationCycle.id.asc())).all()
    out = _tray_out(row)
    out["cycles"] = [{
        "cycle_id": c.cycle_id, "cycle_type": c.cycle_type,
        "temperature_c": c.temperature_c, "duration_minutes": c.duration_minutes,
        "indicator_result": c.indicator_result, "run_by": c.run_by,
        "at": _aware(c.created_at).isoformat() if c.created_at else None,
    } for c in cycles]
    return out


@router.post("/trays/{tray_id}/transition")
async def transition_tray(tray_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("surgery.work"))):
    """Walk a tray one step through the CONFIGURED lifecycle (the
    published states list, in order; the list is a cycle — the last
    state wraps to the first). Two steps are governed and refused
    here: sterilising -> sterile happens only through a recorded
    sterilisation cycle, and sterile -> issued only by assignment
    to a case."""
    payload = await _json_body(request)
    target = payload.get("to_status") or payload.get("status")
    if not target:
        raise HTTPException(status_code=422,
                            detail="Transition requires 'to_status'")
    row = _tray_or_404(session, tray_id)
    states = _tray_states(session)
    if target not in states:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown tray state '{target}': not in the published "
                   "sterile-processing lifecycle")
    current = row.status
    if current not in states:
        raise HTTPException(
            status_code=409,
            detail=f"Tray {tray_id} is in unconfigured state '{current}'")
    nxt = states[(states.index(current) + 1) % len(states)]
    if target != nxt:
        raise HTTPException(
            status_code=409,
            detail=f"Illegal tray transition '{current}' -> '{target}': "
                   f"the lifecycle's next state is '{nxt}'")
    if current == "sterilising" and target == "sterile":
        raise HTTPException(
            status_code=409,
            detail="A tray becomes sterile only through a recorded "
                   "sterilisation cycle (POST /surgery/trays/"
                   f"{tray_id}/cycles)")
    if current == "sterile" and target == "issued":
        raise HTTPException(
            status_code=409,
            detail="A tray is issued only by assignment to a case "
                   "(POST /surgery/cases/{case_id}/trays)")
    row.status = target
    if target != "issued":
        row.assigned_case_id = None
    session.flush()
    append_audit(session, actor=user.username, action="surgery.tray.transition",
                 entity=f"Tray/{tray_id}",
                 payload={"from_status": current, "to_status": target,
                          "tray_type": row.tray_type})
    session.commit()
    return _tray_out(row)


@router.post("/trays/{tray_id}/cycles", status_code=201)
async def run_sterilisation_cycle(tray_id: str, request: Request,
                                  session: Session = Depends(get_db),
                                  user: User = Depends(require_capability("surgery.work"))):
    """Record a sterilisation cycle for a tray in `sterilising`.
    The cycle type must be published config; the recorded
    temperature and duration must meet the cycle type's configured
    floors (422), and a chemical indicator result is REQUIRED (422).
    A passing cycle makes the tray sterile and stamps its shelf-life
    expiry (run time + configured shelfLifeDays); a failed indicator
    sends the tray back to decontamination."""
    payload = await _json_body(request)
    row = _tray_or_404(session, tray_id)
    if row.status != "sterilising":
        raise HTTPException(
            status_code=409,
            detail=f"Tray {tray_id} is '{row.status}'; a sterilisation "
                   "cycle can only run from 'sterilising'")
    cycle_type = _require_str(payload, "cycle_type")
    cycle_cfg = next((c for c in _cycle_types(session)
                      if c.get("code") == cycle_type), None)
    if cycle_cfg is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown cycle type '{cycle_type}': not in the "
                   "published sterile-processing config")
    temperature = payload.get("temperature_c")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise HTTPException(status_code=422,
                            detail="'temperature_c' is required (a number)")
    required_temp = cycle_cfg.get("temperatureC")
    if isinstance(required_temp, (int, float)) \
            and not isinstance(required_temp, bool) \
            and temperature < required_temp:
        raise HTTPException(
            status_code=422,
            detail=f"Cycle '{cycle_type}' requires temperature >= "
                   f"{required_temp}C; recorded {temperature}C")
    duration = payload.get("duration_minutes")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise HTTPException(status_code=422,
                            detail="'duration_minutes' is required (a number)")
    min_minutes = cycle_cfg.get("minMinutes")
    if isinstance(min_minutes, (int, float)) \
            and not isinstance(min_minutes, bool) \
            and duration < min_minutes:
        raise HTTPException(
            status_code=422,
            detail=f"Cycle '{cycle_type}' requires duration >= "
                   f"{min_minutes} minutes; recorded {duration}")
    indicator = payload.get("indicator_result")
    if indicator not in ("pass", "fail"):
        raise HTTPException(
            status_code=422,
            detail="A sterilisation cycle requires 'indicator_result': "
                   "'pass' or 'fail'")
    cycle = SterilisationCycle(
        cycle_id=f"cycle-{uuid.uuid4()}", tray_id=tray_id,
        cycle_type=cycle_type, temperature_c=float(temperature),
        duration_minutes=float(duration), indicator_result=indicator,
        run_by=user.username)
    session.add(cycle)
    if indicator == "pass":
        run_at = _now()
        row.status = "sterile"
        row.sterilised_at = run_at.isoformat()
        row.expires_at = (run_at + timedelta(
            days=_shelf_life_days(session))).isoformat()
        row.last_cycle_id = cycle.cycle_id
    else:
        states = _tray_states(session)
        row.status = "decontamination" if "decontamination" in states \
            else states[0]
        row.sterilised_at = None
        row.expires_at = None
        row.last_cycle_id = cycle.cycle_id
    session.flush()
    append_audit(session, actor=user.username, action="surgery.tray.cycle",
                 entity=f"Tray/{tray_id}",
                 payload={"cycle_id": cycle.cycle_id, "cycle_type": cycle_type,
                          "temperature_c": float(temperature),
                          "duration_minutes": float(duration),
                          "indicator_result": indicator,
                          "tray_status": row.status,
                          "expires_at": row.expires_at})
    session.commit()
    return {"cycle": {"cycle_id": cycle.cycle_id, "tray_id": tray_id,
                      "cycle_type": cycle_type,
                      "temperature_c": float(temperature),
                      "duration_minutes": float(duration),
                      "indicator_result": indicator},
            "tray": _tray_out(row)}


@router.post("/cases/{case_id}/trays", status_code=201)
async def assign_tray(case_id: str, request: Request,
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("surgery.work"))):
    """Issue a tray to a case. Refused 409 unless the tray is STERILE
    and WITHIN its configured shelf life — the sterile-processing
    guarantee the OR relies on."""
    payload = await _json_body(request)
    _case_or_404(session, case_id)
    tray_id = _require_str(payload, "tray_id")
    row = _tray_or_404(session, tray_id)
    if row.status != "sterile":
        raise HTTPException(
            status_code=409,
            detail=f"Tray {tray_id} is '{row.status}' and cannot be "
                   "issued to a case; only a sterile tray can be issued")
    expires = _parse_dt(row.expires_at)
    if expires is None or expires <= _now():
        raise HTTPException(
            status_code=409,
            detail=f"Tray {tray_id} is past its sterile shelf life "
                   f"(expired {row.expires_at}); it must be "
                   "reprocessed before issue")
    row.status = "issued"
    row.assigned_case_id = case_id
    session.flush()
    append_audit(session, actor=user.username, action="surgery.case.tray",
                 entity=f"Procedure/{case_id}",
                 payload={"tray_id": tray_id, "tray_type": row.tray_type,
                          "expires_at": row.expires_at})
    session.commit()
    return {"tray": _tray_out(row), "case_id": case_id}


# ---------------------------------------------------------------------------
# Intra-operative record (governed forms via the Phase 5/6 machinery)
# ---------------------------------------------------------------------------

def _intraop_meta(body: dict) -> dict:
    ext = _get_extension(body or {}, INTRAOP_EXTENSION)
    if ext is not None and isinstance(ext.get("valueString"), str):
        try:
            parsed = json.loads(ext["valueString"])
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


@router.post("/cases/{case_id}/intraop/{form_key}", status_code=201)
async def submit_intraop(case_id: str, form_key: str, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("surgery.work"))):
    """Submit one governed intra-operative form for a case. The form
    key must be one of this module's seeded forms (404 otherwise);
    the response is validated + stored by the Phase 5 documentation
    machinery against the PUBLISHED form and linked onto the case —
    the Phase 6 _attach_form_response pattern, generalised to a
    per-form map so all three forms live on the case record."""
    if form_key not in INTRAOP_FORM_KEYS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown intra-operative form '{form_key}': expected "
                   f"one of {', '.join(INTRAOP_FORM_KEYS)}")
    payload = await _json_body(request)
    case = _case_or_404(session, case_id)
    cfg = published_config(session, "form", form_key)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=f"No published intra-operative form (form/{form_key}); "
                   "publish one in Studio before recording it")
    patient_ref = case.patient_ref or boards._subject_ref(case.body or {})
    stored = documentation._store_response(
        session, user, form_key, patient_ref, payload.get("answers"))
    body = dict(case.body or {})
    meta = _intraop_meta(body)
    meta[form_key] = {"responseId": stored["id"], "formVersion": cfg.version,
                      "recordedBy": user.username, "recordedAt": _now_iso()}
    _set_extension(body, INTRAOP_EXTENSION, valueString=json.dumps(meta))
    case.body = body
    case.version += 1
    session.flush()
    append_audit(session, actor=user.username, action="surgery.case.intraop",
                 entity=f"Procedure/{case_id}",
                 payload={"form_key": form_key, "response_id": stored["id"],
                          "form_version": cfg.version})
    session.commit()
    return {"case_id": case_id, "form_key": form_key, "response": stored,
            "intraop": meta}


# ---------------------------------------------------------------------------
# PACU scoring + the discharge gate consulted by app.boards
# ---------------------------------------------------------------------------

def _pacu_config(session: Session) -> dict:
    cfg = published_config(session, "terminology", PACU_KEY)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=f"No published PACU scoring config "
                   f"(terminology/{PACU_KEY}); publish one in Studio")
    return cfg.payload or {}


def _score_out(row: PacuScoreRecord) -> dict:
    return {"score_id": row.score_id, "case_id": row.case_id,
            "scores": row.scores or {}, "total": row.total,
            "threshold": row.threshold,
            "must_pass_failures": row.must_pass_failures or [],
            "passes": bool(row.passes),
            "discharge_ready": bool(row.passes),
            "recorded_by": row.recorded_by,
            "recorded_at": _aware(row.created_at).isoformat()
            if row.created_at else None}


def _latest_pacu_score(session: Session, case_id: str) -> PacuScoreRecord | None:
    return session.scalars(select(PacuScoreRecord).where(
        PacuScoreRecord.case_id == case_id).order_by(
        PacuScoreRecord.id.desc())).first()


@router.post("/cases/{case_id}/pacu-score", status_code=201)
async def record_pacu_score(case_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("surgery.work"))):
    """Record a PACU score for a case. Every configured criterion
    must be scored inside its configured scale; the TOTAL and the
    discharge verdict (threshold + must-pass criteria) are computed
    HERE from the published pacu-scoring config — never trusted
    from the caller."""
    payload = await _json_body(request)
    _case_or_404(session, case_id)
    cfg = _pacu_config(session)
    criteria = [c for c in cfg.get("concepts") or []
                if isinstance(c, dict) and c.get("code")
                and c.get("active") is not False]
    if not criteria:
        raise HTTPException(status_code=503,
                            detail="The published pacu-scoring config has "
                                   "no criteria")
    scores_in = payload.get("scores")
    if not isinstance(scores_in, dict) or not scores_in:
        raise HTTPException(status_code=422,
                            detail="'scores' must be an object mapping "
                                   "criterion code -> points")
    known = {c["code"]: c for c in criteria}
    for code in scores_in:
        if code not in known:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown PACU criterion '{code}': not in the "
                       "published pacu-scoring config")
    missing = [c["code"] for c in criteria if c["code"] not in scores_in]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"PACU score is missing criteria: {', '.join(missing)}")
    scores = {}
    for code, value in scores_in.items():
        max_points = known[code].get("max")
        max_points = max_points if isinstance(max_points, int) \
            and not isinstance(max_points, bool) else 2
        if isinstance(value, bool) or not isinstance(value, int) \
                or not 0 <= value <= max_points:
            raise HTTPException(
                status_code=422,
                detail=f"Criterion '{code}' must be an integer 0-{max_points}")
        scores[code] = value
    total = sum(scores.values())
    threshold = cfg.get("dischargeThreshold")
    threshold = threshold if isinstance(threshold, int) \
        and not isinstance(threshold, bool) else 9
    failures = []
    for rule in cfg.get("mustPass") or []:
        if isinstance(rule, dict) and rule.get("code") in scores:
            minimum = rule.get("minimum")
            minimum = minimum if isinstance(minimum, int) \
                and not isinstance(minimum, bool) else 1
            if scores[rule["code"]] < minimum:
                failures.append({"code": rule["code"], "minimum": minimum,
                                 "scored": scores[rule["code"]]})
    passes = total >= threshold and not failures
    row = PacuScoreRecord(score_id=f"pacu-{uuid.uuid4()}", case_id=case_id,
                          scores=scores, total=total, threshold=threshold,
                          must_pass_failures=failures, passes=passes,
                          recorded_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="surgery.case.pacu-score",
                 entity=f"Procedure/{case_id}",
                 payload={"score_id": row.score_id, "total": total,
                          "threshold": threshold, "passes": passes,
                          "must_pass_failures": failures})
    session.commit()
    return _score_out(row)


@router.get("/cases/{case_id}/pacu-scores")
def list_pacu_scores(case_id: str, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("boards.read"))):
    _case_or_404(session, case_id)
    rows = session.scalars(select(PacuScoreRecord).where(
        PacuScoreRecord.case_id == case_id).order_by(
        PacuScoreRecord.id.asc())).all()
    return {"items": [_score_out(r) for r in rows], "total": len(rows),
            "latest": _score_out(rows[-1]) if rows else None}


def pacu_gate_enabled(session: Session) -> bool:
    """The gate toggle from the PUBLISHED surgery workflow payload
    (`pacuScoreGate`); absent/false = gate off (the same reader
    shape as boards._safety_gate_enabled)."""
    payload = workflow_payload(session, SURGERY_WORKFLOW_KEY) or {}
    return payload.get("pacuScoreGate") is True


def check_pacu_gate(session: Session, case_id: str) -> None:
    """The PACU discharge gate, consulted by the boards pacu -> done
    transition (lazy import there — this module builds on boards).
    With the gate published ON, a case in PACU may finish only with
    a recorded, passing PACU score: no score -> 409 naming the
    missing score; a failing latest score -> 409 naming the total
    vs the threshold (and any must-pass failures). With the gate
    off this is a no-op and the transition behaves as before."""
    if not pacu_gate_enabled(session):
        return
    latest = _latest_pacu_score(session, case_id)
    if latest is None:
        raise HTTPException(
            status_code=409,
            detail=(f"PACU discharge gate: case {case_id} cannot move "
                    "from 'pacu' to 'done' — no PACU score recorded "
                    "(missing PACU score; record one via POST "
                    f"/surgery/cases/{case_id}/pacu-score)"))
    if not latest.passes:
        reasons = [f"latest PACU score {latest.total} is below the "
                   f"discharge threshold {latest.threshold}"]
        if latest.total >= latest.threshold and latest.must_pass_failures:
            reasons = ["must-pass criteria not met: " + ", ".join(
                f"{f['code']} scored {f['scored']} (minimum {f['minimum']})"
                for f in latest.must_pass_failures)]
        raise HTTPException(
            status_code=409,
            detail=(f"PACU discharge gate: case {case_id} cannot move "
                    "from 'pacu' to 'done' — " + "; ".join(reasons)))


# ---------------------------------------------------------------------------
# Outcomes + SSI surveillance feed
# ---------------------------------------------------------------------------

def _outcome_out(row: SurgeryOutcomeRecord) -> dict:
    return {"outcome_id": row.outcome_id, "case_id": row.case_id,
            "patient_ref": row.patient_ref, "status": row.status,
            "complications": row.complications or [], "ssi": bool(row.ssi),
            "ssi_onset_date": row.ssi_onset_date, "organism": row.organism,
            "recorded_by": row.recorded_by,
            "recorded_at": _aware(row.updated_at).isoformat()
            if row.updated_at else None}


@router.post("/cases/{case_id}/outcome", status_code=201)
async def record_outcome(case_id: str, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("surgery.work"))):
    """Record (or correct) a case's outcome. The status and every
    complication code validate against the published
    surgery-complications terminology. An SSI flag REQUIRES an
    onset date (the surveillance feed is meaningless without it);
    the organism is recorded when known."""
    payload = await _json_body(request)
    case = _case_or_404(session, case_id)
    cfg = published_config(session, "terminology", COMPLICATIONS_KEY)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=f"No published surgery complications terminology "
                   f"(terminology/{COMPLICATIONS_KEY}); publish one in Studio")
    cfg_payload = cfg.payload or {}
    statuses = [s for s in cfg_payload.get("statuses") or []
                if isinstance(s, str)]
    status = _require_str(payload, "status")
    if statuses and status not in statuses:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown outcome status '{status}': expected one of "
                   f"{', '.join(statuses)}")
    complications = payload.get("complications") or []
    if not isinstance(complications, list) or any(
            not isinstance(c, str) for c in complications):
        raise HTTPException(status_code=422,
                            detail="'complications' must be a list of codes")
    known = {c.get("code") for c in cfg_payload.get("concepts") or []
             if isinstance(c, dict)}
    for code in complications:
        if code not in known:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown complication '{code}': not in the "
                       "published surgery-complications config")
    ssi = payload.get("ssi") is True
    onset = payload.get("ssi_onset_date") or payload.get("onset_date")
    organism = payload.get("organism")
    if ssi:
        if not isinstance(onset, str) or not onset:
            raise HTTPException(
                status_code=422,
                detail="A surgical-site infection requires "
                       "'ssi_onset_date'")
        try:
            date.fromisoformat(onset)
        except ValueError:
            raise HTTPException(status_code=422,
                                detail="'ssi_onset_date' must be an ISO date")
    elif onset is not None and not isinstance(onset, str):
        raise HTTPException(status_code=422,
                            detail="'ssi_onset_date' must be an ISO date")
    if organism is not None and not isinstance(organism, str):
        raise HTTPException(status_code=422,
                            detail="'organism' must be a string")
    patient_ref = case.patient_ref or boards._subject_ref(case.body or {})
    row = session.scalars(select(SurgeryOutcomeRecord).where(
        SurgeryOutcomeRecord.case_id == case_id)).first()
    created = row is None
    if row is None:
        row = SurgeryOutcomeRecord(outcome_id=f"outcome-{uuid.uuid4()}",
                                   case_id=case_id)
        session.add(row)
    row.patient_ref = patient_ref
    row.status = status
    row.complications = list(complications)
    row.ssi = ssi
    row.ssi_onset_date = onset if ssi else None
    row.organism = organism if ssi else None
    row.recorded_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="surgery.case.outcome",
                 entity=f"Procedure/{case_id}",
                 payload={"outcome_id": row.outcome_id, "status": status,
                          "complications": list(complications), "ssi": ssi,
                          "ssi_onset_date": row.ssi_onset_date,
                          "organism": row.organism, "created": created})
    session.commit()
    return _outcome_out(row)


@router.get("/outcomes/ssi")
def ssi_feed(session: Session = Depends(get_db),
             user: User = Depends(require_capability("boards.read"))):
    """The surgical-site-infection SURVEILLANCE FEED for infection
    prevention: every case whose recorded outcome carries the SSI
    flag, with onset + organism — this payload is a surveillance
    feed, not a clinical record view."""
    rows = session.scalars(select(SurgeryOutcomeRecord).where(
        SurgeryOutcomeRecord.ssi.is_(True)).order_by(
        SurgeryOutcomeRecord.ssi_onset_date.asc(),
        SurgeryOutcomeRecord.id.asc())).all()
    items = []
    for row in rows:
        item = _outcome_out(row)
        case = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Procedure",
            FhirResource.resource_id == row.case_id)).first()
        if case is not None:
            code, display = _case_code(case.body or {})
            item["procedure_code"] = code
            item["procedure_display"] = display
        items.append(item)
    return {"feed": "surgical-site-infection surveillance",
            "purpose": ("Infection-prevention surveillance feed: cases "
                        "with a recorded surgical-site infection, for "
                        "review by the infection prevention team."),
            "items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# The depth case record (everything Phase 15 hangs off one case)
# ---------------------------------------------------------------------------

@router.get("/cases/{case_id}/record")
def case_record(case_id: str, session: Session = Depends(get_db),
                user: User = Depends(require_capability("boards.read"))):
    """The surgery-depth case record: the boards case entry plus the
    intra-op form responses, PACU scores, pick lists, issued trays,
    the outcome, and the case's block posture."""
    case = _case_or_404(session, case_id)
    appointment = boards._case_appointment(session, case)
    entry = boards._case_entry(case, appointment) \
        if appointment is not None else {"case": fhir._render(case)}
    intraop = {}
    for form_key, meta in _intraop_meta(case.body or {}).items():
        resp = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "QuestionnaireResponse",
            FhirResource.resource_id == meta.get("responseId"))).first()
        intraop[form_key] = {
            "meta": meta,
            "response": fhir._render(resp) if resp is not None else None}
    scores = session.scalars(select(PacuScoreRecord).where(
        PacuScoreRecord.case_id == case_id).order_by(
        PacuScoreRecord.id.asc())).all()
    picks = session.scalars(select(CasePick).where(
        CasePick.case_id == case_id).order_by(CasePick.id.asc())).all()
    trays = session.scalars(select(TrayInstance).where(
        TrayInstance.assigned_case_id == case_id)).all()
    outcome = session.scalars(select(SurgeryOutcomeRecord).where(
        SurgeryOutcomeRecord.case_id == case_id)).first()
    block = None
    if appointment is not None:
        start = boards._parse_dt((appointment.body or {}).get("start"))
        end = boards._parse_dt((appointment.body or {}).get("end"))
        room_code = entry.get("room")
        if start is not None and end is not None and room_code:
            block = _block_assessment(session, _case_surgeon(session, case),
                                      room_code, start, end)
    return {"case": entry, "intraop": intraop,
            "pacu_scores": [_score_out(s) for s in scores],
            "latest_pacu_score": _score_out(scores[-1]) if scores else None,
            "pacu_gate_enabled": pacu_gate_enabled(session),
            "picks": [_pick_out(p) for p in picks],
            "trays": [_tray_out(t) for t in trays],
            "outcome": _outcome_out(outcome) if outcome is not None else None,
            "block": block}
