"""Phase 15 (standing scope) — infection prevention.

HAI surveillance, isolation precautions and stewardship referral,
built ON the Studio customization engine like every other module:

- **HAI definitions are published Studio config**
  (`terminology/hai-definitions`, seeded below): each healthcare-
  associated infection type (SSI, CLABSI, CAUTI, ventilator-
  associated events, MRSA, *C. difficile*) carries its surveillance
  criteria text and the denominator it rates against. Case intake
  validates the type against this published list — a type the site
  has not published cannot be recorded.
- **Isolation precaution types are published Studio config**
  (`terminology/isolation-precautions`).
- **The stewardship audience is config**: the hai-definitions
  payload's `stewardship` section names the pharmacy / antimicrobial-
  stewardship audience a referral Task is addressed to.

Case records, device-day denominators and precautions are this
module's own tables (the protocols.py / barcodes.py pattern —
operational surveillance data beside the FHIR chart). Every mutation
is hash-audit-chained in the same transaction.

Rates are EXACT arithmetic over confirmed cases and recorded
device-days: rate = confirmed x 1000 / device-days. A zero (or
inapplicable) denominator yields rate null with an explicit reason —
never a division by zero, never a fabricated number.

The SSI surveillance feed reads the surgery workstream's outcome
records (app.surgery_depth, built in parallel) DEFENSIVELY: its
tables are resolved through SQLAlchemy metadata by table-name
candidates and column-name fallbacks, and an absent table degrades
to an empty feed with an honest `source: unavailable` note. The
guaranteed intake path is POST /infection/cases, which needs nothing
from the surgery module.
"""
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import fhir
from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import ConfigStatus, StudioConfig, User, utcnow
from .security import get_db, require_capability

router = APIRouter(prefix="/infection", tags=["infection_prevention"])

READ = "ip.read"
WORK = "ip.work"

HAI_KEY = "hai-definitions"
ISOLATION_KEY = "isolation-precautions"

CASE_STATUSES = ("reported", "under-review", "confirmed", "ruled-out")
CASE_TRANSITIONS: dict[str, set[str]] = {
    "reported": {"under-review"},
    "under-review": {"confirmed", "ruled-out"},
    "confirmed": set(),
    "ruled-out": set(),
}

# Device-day denominator type (from the HAI definitions config) ->
# the device-days record field it sums.
DENOMINATOR_FIELDS = {
    "central-line-days": "central_line_days",
    "urinary-catheter-days": "urinary_catheter_days",
    "ventilator-days": "ventilator_days",
}


# ---------------------------------------------------------------------------
# This module's own tables
# ---------------------------------------------------------------------------

class InfectionCase(Base):
    """One HAI surveillance case: a suspected/confirmed infection
    under infection-prevention review. Status runs reported ->
    under-review -> confirmed | ruled-out; ruled-out requires a
    recorded note (the reviewer's reasoning is the record)."""
    __tablename__ = "infection_cases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    hai_type: Mapped[str] = mapped_column(String(60), nullable=False,
                                          index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                             index=True)
    onset_date: Mapped[str] = mapped_column(String(10), nullable=False,
                                            index=True)  # ISO date
    source_ref: Mapped[str] = mapped_column(String(300), nullable=False,
                                            default="")
    unit: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="reported", index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    stewardship_task_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


class DeviceDayRecord(Base):
    """One unit-day denominator record: how many central-line,
    urinary-catheter and ventilator days a unit accumulated on one
    date (UTC). Rates sum these rows — the denominator is recorded
    surveillance data, never estimated."""
    __tablename__ = "infection_device_days"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[str] = mapped_column(String(120), unique=True,
                                           nullable=False, index=True)
    unit: Mapped[str] = mapped_column(String(120), nullable=False,
                                      index=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False,
                                      index=True)  # ISO date (UTC)
    central_line_days: Mapped[int] = mapped_column(Integer, nullable=False,
                                                   default=0)
    urinary_catheter_days: Mapped[int] = mapped_column(Integer,
                                                       nullable=False,
                                                       default=0)
    ventilator_days: Mapped[int] = mapped_column(Integer, nullable=False,
                                                 default=0)
    recorded_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class IsolationPrecaution(Base):
    """One isolation precaution episode for one patient: the type
    (from the published isolation-precautions terminology), the
    reason, and the start/stop lifecycle. Active precautions are the
    unit's infection-control board state."""
    __tablename__ = "infection_precautions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    precaution_id: Mapped[str] = mapped_column(String(120), unique=True,
                                               nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                             index=True)
    precaution_type: Mapped[str] = mapped_column(String(60), nullable=False,
                                                 index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    unit: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                             index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="active", index=True)
    started_on: Mapped[str] = mapped_column(String(40), nullable=False,
                                            default="")
    stopped_at: Mapped[str | None] = mapped_column(String(40),
                                                   nullable=True)
    stop_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


# ---------------------------------------------------------------------------
# Published Studio configuration (seeded published v1 at startup)
# ---------------------------------------------------------------------------

def default_hai_definitions_payload() -> dict:
    """HAI surveillance definitions (synthetic local adoption of the
    usual NHSN-style definitions — criteria are summary text for the
    review workflow, not a reproduction of any external standard).
    `denominatorType` names what the type rates against:
    a device-days field key from DENOMINATOR_FIELDS, or "none" for
    types (SSI, MRSA, C. difficile) that are NOT device-day rated —
    their rates are reported null with that reason, never invented
    against a device denominator."""
    def hai(code, display, criteria, denominator):
        return {"code": code, "display": display, "active": True,
                "criteria": criteria, "denominatorType": denominator}

    return {
        "name": "Healthcare-associated infection (HAI) definitions",
        "system": "urn:opencare:hai-definitions",
        "concepts": [
            hai("ssi", "Surgical site infection (SSI)",
                "Infection at the site of a surgical procedure within "
                "the surveillance window for the procedure type: "
                "purulent drainage, organisms isolated from the site, "
                "or signs/symptoms (pain, swelling, erythema, heat) "
                "with the site deliberately opened or the diagnosis "
                "made by the treating team.",
                "none"),
            hai("clabsi", "Central line-associated bloodstream "
                          "infection (CLABSI)",
                "A laboratory-confirmed bloodstream infection where a "
                "central line was in place for more than two calendar "
                "days on the date of event and the infection is not "
                "attributable to another site.",
                "central-line-days"),
            hai("cauti", "Catheter-associated urinary tract infection "
                         "(CAUTI)",
                "A urinary tract infection (symptoms plus a qualifying "
                "urine culture) where an indwelling urinary catheter "
                "was in place for more than two calendar days on the "
                "date of event.",
                "urinary-catheter-days"),
            hai("vae", "Ventilator-associated event (VAE)",
                "A deterioration in oxygenation (sustained rise in "
                "daily minimum FiO2 or PEEP) after a period of "
                "stability on mechanical ventilation, with the "
                "infection-related and pneumonia tiers assessed per "
                "the ventilator-event algorithm.",
                "ventilator-days"),
            hai("mrsa", "MRSA bacteraemia",
                "Methicillin-resistant Staphylococcus aureus isolated "
                "from a blood culture in a patient whose infection is "
                "healthcare-associated by onset timing and prior "
                "exposure review.",
                "none"),
            hai("c-difficile", "Clostridioides difficile infection",
                "A positive C. difficile toxin/NAAT result on an "
                "unformed stool specimen from a patient without a "
                "positive result in the preceding surveillance "
                "window, classified by onset timing.",
                "none"),
        ],
        # The stewardship referral audience: a referral Task is
        # addressed to this team (the referral endpoint reads the
        # audience from here, never from code constants).
        "stewardship": {
            "team": "antimicrobial-stewardship",
            "display": "Pharmacy — antimicrobial stewardship team",
            "ownerRole": "pharmacist",
        },
    }


def default_isolation_payload() -> dict:
    def prec(code, display, description):
        return {"code": code, "display": display, "active": True,
                "description": description}

    return {
        "name": "Isolation precautions",
        "system": "urn:opencare:isolation-precautions",
        "concepts": [
            prec("contact", "Contact precautions",
                 "Gloves and gown on entry; dedicated equipment; for "
                 "organisms spread by direct/indirect contact "
                 "(e.g. MRSA, C. difficile — soap-and-water hand "
                 "hygiene for spore-forming organisms)."),
            prec("droplet", "Droplet precautions",
                 "Surgical mask on entry; for pathogens spread by "
                 "respiratory droplets (e.g. influenza)."),
            prec("airborne", "Airborne precautions",
                 "Fit-tested respirator and airborne infection "
                 "isolation room; for pathogens spread by airborne "
                 "particles (e.g. tuberculosis, measles)."),
            prec("contact-enteric", "Contact-enteric precautions",
                 "Contact precautions with enteric emphasis: soap-"
                 "and-water hand hygiene and sporicidal cleaning."),
            prec("protective", "Protective (reverse) precautions",
                 "Protection of the highly immunocompromised patient "
                 "from environmental organisms."),
        ],
    }


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


def ensure_infection_prevention_defaults(session: Session) -> None:
    """Seed the HAI definitions and the isolation-precautions
    terminology as published Studio config v1 (idempotent)."""
    _seed_published(session, "terminology", HAI_KEY,
                    default_hai_definitions_payload())
    _seed_published(session, "terminology", ISOLATION_KEY,
                    default_isolation_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Config accessors + body helpers
# ---------------------------------------------------------------------------

def _hai_payload(session: Session) -> dict:
    cfg = published_config(session, "terminology", HAI_KEY)
    if cfg is not None and isinstance(cfg.payload, dict) and cfg.payload:
        return cfg.payload
    return default_hai_definitions_payload()


def _hai_types(session: Session) -> dict[str, dict]:
    return {c["code"]: c for c in _hai_payload(session).get("concepts") or []
            if isinstance(c, dict) and c.get("code")
            and c.get("active") is not False}


def _isolation_types(session: Session) -> dict[str, dict]:
    cfg = published_config(session, "terminology", ISOLATION_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    if not payload.get("concepts"):
        payload = default_isolation_payload()
    return {c["code"]: c for c in payload.get("concepts") or []
            if isinstance(c, dict) and c.get("code")
            and c.get("active") is not False}


def _stewardship(session: Session) -> dict:
    stewardship = _hai_payload(session).get("stewardship")
    if isinstance(stewardship, dict) and stewardship.get("display"):
        return stewardship
    return {"team": "antimicrobial-stewardship",
            "display": "Pharmacy — antimicrobial stewardship team",
            "ownerRole": "pharmacist"}


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


def _require_str(payload: dict, *fields: str) -> str:
    for field in fields:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise HTTPException(
        status_code=422,
        detail=f"'{fields[0]}' is required and must be a non-empty string")


def _optional_str(payload: dict, *fields: str) -> str | None:
    for field in fields:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _patient_ref(session: Session, payload: dict) -> str:
    raw = _require_str(payload, "patient", "patient_ref", "patientRef")
    ref = fhir._normalize_patient_ref(raw)
    fhir._get_row("Patient", ref.split("/", 1)[1], session)  # 404 if unknown
    return ref


def _iso_date(payload: dict, *fields: str) -> str:
    raw = _require_str(payload, *fields)
    try:
        return date.fromisoformat(raw[:10]).isoformat() \
            if len(raw) >= 10 else date.fromisoformat(raw).isoformat()
    except ValueError:
        raise HTTPException(status_code=422,
                            detail=f"'{fields[0]}' must be an ISO date "
                                   f"(YYYY-MM-DD); got '{raw}'")


def _non_negative_int(payload: dict, *fields: str) -> int:
    value = None
    for field in fields:
        if payload.get(field) is not None:
            value = payload[field]
            break
    if value is None:
        raise HTTPException(status_code=422,
                            detail=f"'{fields[0]}' is required")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HTTPException(
            status_code=422,
            detail=f"'{fields[0]}' must be a non-negative integer")
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _case_out(row: InfectionCase) -> dict:
    return {"id": row.case_id, "case_id": row.case_id,
            "type": row.hai_type, "hai_type": row.hai_type,
            "patient_ref": row.patient_ref,
            "patient": row.patient_ref,
            "onset_date": row.onset_date,
            "source_ref": row.source_ref, "unit": row.unit,
            "status": row.status, "note": row.note,
            "stewardship_task_id": row.stewardship_task_id,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def _case_row(session: Session, case_id: str) -> InfectionCase:
    row = session.scalars(select(InfectionCase).where(
        InfectionCase.case_id == case_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Infection case {case_id} not found")
    return row


# ---------------------------------------------------------------------------
# Definitions reads
# ---------------------------------------------------------------------------

@router.get("/definitions")
def get_definitions(session: Session = Depends(get_db),
                    user: User = Depends(require_capability(READ))):
    """The published HAI definitions + isolation precaution types
    the intake endpoints validate against (Studio config, not code
    constants)."""
    hai_cfg = published_config(session, "terminology", HAI_KEY)
    iso_cfg = published_config(session, "terminology", ISOLATION_KEY)
    return {
        "hai_definitions": _hai_payload(session),
        "hai_version": hai_cfg.version if hai_cfg else None,
        "isolation_precautions": (iso_cfg.payload if iso_cfg else
                                  default_isolation_payload()),
        "isolation_version": iso_cfg.version if iso_cfg else None,
    }


# ---------------------------------------------------------------------------
# HAI cases
# ---------------------------------------------------------------------------

@router.post("/cases", status_code=201)
async def create_case(request: Request, session: Session = Depends(get_db),
                      user: User = Depends(require_capability(WORK))):
    """Record one HAI surveillance case (the guaranteed intake path).
    The type must be one of the published hai-definitions; the case
    starts in status `reported`."""
    payload = await _json_body(request)
    hai_type = _require_str(payload, "type", "hai_type", "case_type")
    types = _hai_types(session)
    if hai_type not in types:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown HAI type '{hai_type}': not in the published "
                   f"hai-definitions config (types: {sorted(types)})")
    patient_ref = _patient_ref(session, payload)
    onset = _iso_date(payload, "onset_date", "onsetDate", "onset")
    source_ref = _optional_str(payload, "source_ref", "source_reference",
                               "sourceRef", "source") or ""
    case = InfectionCase(
        case_id=str(uuid.uuid4()), hai_type=hai_type,
        patient_ref=patient_ref, onset_date=onset, source_ref=source_ref,
        unit=_optional_str(payload, "unit"), status="reported",
        note=_optional_str(payload, "note"), created_by=user.username)
    session.add(case)
    session.flush()
    append_audit(session, actor=user.username, action="infection.case.create",
                 entity=f"InfectionCase/{case.case_id}",
                 payload={"type": hai_type, "patient": patient_ref,
                          "onset_date": onset, "source_ref": source_ref})
    session.commit()
    return _case_out(case)


@router.get("/cases")
def list_cases(type: str | None = Query(default=None),
               status: str | None = Query(default=None),
               patient: str | None = Query(default=None),
               session: Session = Depends(get_db),
               user: User = Depends(require_capability(READ))):
    stmt = select(InfectionCase).order_by(InfectionCase.created_at.asc(),
                                           InfectionCase.id.asc())
    if type:
        stmt = stmt.where(InfectionCase.hai_type == type)
    if status:
        stmt = stmt.where(InfectionCase.status == status)
    if patient:
        stmt = stmt.where(
            InfectionCase.patient_ref == fhir._normalize_patient_ref(patient))
    rows = session.scalars(stmt).all()
    return {"items": [_case_out(r) for r in rows], "total": len(rows)}


@router.get("/cases/{case_id}")
def get_case(case_id: str, session: Session = Depends(get_db),
             user: User = Depends(require_capability(READ))):
    return _case_out(_case_row(session, case_id))


@router.post("/cases/{case_id}/transition")
async def transition_case(case_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability(WORK))):
    """Move a case reported -> under-review -> confirmed | ruled-out.
    Ruling a case OUT requires a note (the reviewer's reasoning):
    without one the transition is refused 409 and nothing changes."""
    payload = await _json_body(request)
    target = _require_str(payload, "to_status", "status", "to")
    if target not in CASE_STATUSES:
        raise HTTPException(status_code=422,
                            detail=f"Unknown case status '{target}'")
    row = _case_row(session, case_id)
    note = _optional_str(payload, "note")
    if target == "ruled-out" and not note:
        raise HTTPException(
            status_code=409,
            detail="A case can only be ruled out with a note recording "
                   "the review reasoning; supply 'note'")
    if target not in CASE_TRANSITIONS.get(row.status, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Illegal case transition '{row.status}' -> '{target}'")
    previous = row.status
    row.status = target
    if note:
        row.note = note
    session.flush()
    append_audit(session, actor=user.username,
                 action="infection.case.transition",
                 entity=f"InfectionCase/{case_id}",
                 payload={"from_status": previous, "to_status": target,
                          "note_recorded": bool(note)})
    session.commit()
    return _case_out(row)


@router.post("/cases/{case_id}/stewardship-referral", status_code=201)
async def stewardship_referral(case_id: str, request: Request,
                               session: Session = Depends(get_db),
                               user: User = Depends(require_capability(WORK))):
    """Refer a case to antimicrobial stewardship: creates a real FHIR
    Task addressed to the stewardship audience named in the published
    hai-definitions config (the system's Task pattern — the Task is
    the work item the pharmacy/stewardship team works from its
    inbox). One open referral per case: a repeat call returns the
    existing Task."""
    row = _case_row(session, case_id)
    if row.stewardship_task_id:
        try:
            existing = fhir._get_row("Task", row.stewardship_task_id, session)
            return {"task": fhir._render(existing), "existing": True,
                    "case_id": case_id}
        except HTTPException:
            pass  # the recorded task is gone; fall through and re-refer
    audience = _stewardship(session)
    try:
        payload = await _json_body(request)
    except HTTPException:
        payload = {}
    note = _optional_str(payload, "note")
    task_body = {
        "resourceType": "Task",
        "id": str(uuid.uuid4()),
        "status": "requested",
        "intent": "order",
        "description": (f"Antimicrobial stewardship referral: review "
                        f"infection case {case_id} ({row.hai_type}) for "
                        f"{row.patient_ref}, onset {row.onset_date}"),
        "for": {"reference": row.patient_ref},
        "owner": {"display": audience.get("display")},
        "authoredOn": _now_iso(),
        "requester": {"display": user.username},
        "extension": [
            {"url": "urn:opencare:extension/infection-case",
             "valueString": case_id},
            {"url": "urn:opencare:extension/stewardship-team",
             "valueString": str(audience.get("team") or "")},
        ],
    }
    if note:
        task_body["note"] = [{"text": note}]
    created = fhir.create_resource("Task", task_body, session, user)
    row.stewardship_task_id = created["id"]
    session.flush()
    append_audit(session, actor=user.username,
                 action="infection.case.stewardship_referral",
                 entity=f"InfectionCase/{case_id}",
                 payload={"task_id": created["id"],
                          "audience": audience.get("team"),
                          "audience_display": audience.get("display"),
                          "patient": row.patient_ref})
    session.commit()
    return {"task": created, "existing": False, "case_id": case_id}


# ---------------------------------------------------------------------------
# SSI surveillance feed — defensive read of the surgery workstream's
# outcome records (app.surgery_depth, built in parallel)
# ---------------------------------------------------------------------------

_SURGERY_TABLE_TOKENS = ("surgery", "surgical", "operative", "operation")
_ID_FIELDS = ("outcome_id", "case_id", "record_id", "id")
_PATIENT_FIELDS = ("patient_ref", "patient", "subject_ref")
_DATE_FIELDS = ("onset_date", "event_date", "procedure_date", "date",
                "performed_on", "created_at")
_TYPE_FIELDS = ("infection_type", "outcome_type", "type", "outcome",
                "complication", "event")


def _cell(mapping, names) -> object:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _looks_like_ssi(mapping) -> bool:
    for key, value in mapping.items():
        key_l = str(key).lower()
        if "ssi" in key_l and value not in (None, "", False, 0):
            if value is True or (isinstance(value, str)
                                 and value.strip().lower()
                                 not in ("false", "no", "none")):
                return True
        if isinstance(value, str):
            text = value.lower()
            if "surgical site" in text or text.strip() == "ssi" \
                    or "surgical-site" in text:
                return True
    return False


@router.get("/ssi-feed")
def ssi_feed(session: Session = Depends(get_db),
             user: User = Depends(require_capability(READ))):
    """Surgical-site-infection cases derived from the surgery
    workstream's outcome records. DEFENSIVE by design (parallel
    build): outcome tables are resolved through SQLAlchemy metadata
    by table-name candidates and read with column-name fallbacks —
    if the surgery tables are absent the feed is empty with
    `source: unavailable`, never an error and never invented data.
    The guaranteed SSI intake path is POST /infection/cases with
    type `ssi`."""
    items: list[dict] = []
    tables_checked: list[str] = []
    try:
        for table in Base.metadata.sorted_tables:
            name = table.name.lower()
            if not any(token in name for token in _SURGERY_TABLE_TOKENS):
                continue
            if "outcome" not in name and "case" not in name \
                    and "ssi" not in name:
                continue
            tables_checked.append(table.name)
            try:
                rows = session.execute(select(table)).mappings().all()
            except Exception:
                continue
            for mapping in rows:
                data = dict(mapping)
                if not _looks_like_ssi(data):
                    continue
                raw_patient = _cell(data, _PATIENT_FIELDS)
                patient_ref = None
                if isinstance(raw_patient, str) and raw_patient:
                    patient_ref = fhir._normalize_patient_ref(raw_patient)
                raw_date = _cell(data, _DATE_FIELDS)
                onset = str(raw_date)[:10] if raw_date is not None else None
                source_id = _cell(data, _ID_FIELDS)
                items.append({
                    "type": "ssi",
                    "patient_ref": patient_ref,
                    "onset_date": onset,
                    "source_ref": f"{table.name}/{source_id}"
                    if source_id is not None else table.name,
                    "source_table": table.name,
                    "status": _cell(data, ("status",)) or "reported",
                })
    except Exception:
        items = []
    available = bool(tables_checked)
    return {"items": items, "total": len(items),
            "source": "surgery-outcomes" if available else "unavailable",
            "tables_checked": tables_checked,
            "note": ("Derived live from the surgery workstream's "
                     "outcome records." if available else
                     "The surgery outcome tables are not present in "
                     "this deployment; SSI cases arrive through the "
                     "guaranteed intake path (POST /infection/cases, "
                     "type 'ssi').")}


# ---------------------------------------------------------------------------
# Device-day denominators + rates
# ---------------------------------------------------------------------------

def _device_day_out(row: DeviceDayRecord) -> dict:
    return {"id": row.record_id, "record_id": row.record_id,
            "unit": row.unit, "date": row.date,
            "central_line_days": row.central_line_days,
            "urinary_catheter_days": row.urinary_catheter_days,
            "ventilator_days": row.ventilator_days,
            "recorded_by": row.recorded_by}


@router.post("/device-days", status_code=201)
async def record_device_days(request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability(WORK))):
    """Record one unit's device-day denominators for one date (UTC):
    central-line, urinary-catheter and ventilator days, each a
    validated non-negative integer."""
    payload = await _json_body(request)
    unit = _require_str(payload, "unit")
    day = _iso_date(payload, "date")
    record = DeviceDayRecord(
        record_id=str(uuid.uuid4()), unit=unit, date=day,
        central_line_days=_non_negative_int(
            payload, "central_line_days", "centralLineDays"),
        urinary_catheter_days=_non_negative_int(
            payload, "urinary_catheter_days", "urinaryCatheterDays"),
        ventilator_days=_non_negative_int(
            payload, "ventilator_days", "ventilatorDays"),
        recorded_by=user.username)
    session.add(record)
    session.flush()
    append_audit(session, actor=user.username,
                 action="infection.device_days.record",
                 entity=f"DeviceDays/{record.record_id}",
                 payload={"unit": unit, "date": day,
                          "central_line_days": record.central_line_days,
                          "urinary_catheter_days":
                              record.urinary_catheter_days,
                          "ventilator_days": record.ventilator_days})
    session.commit()
    return _device_day_out(record)


@router.get("/device-days")
def list_device_days(unit: str | None = Query(default=None),
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability(READ))):
    stmt = select(DeviceDayRecord).order_by(DeviceDayRecord.date.asc(),
                                             DeviceDayRecord.id.asc())
    if unit:
        stmt = stmt.where(DeviceDayRecord.unit == unit)
    rows = session.scalars(stmt).all()
    return {"items": [_device_day_out(r) for r in rows],
            "total": len(rows)}


@router.get("/rates")
def get_rates(from_: str | None = Query(default=None, alias="from"),
              to: str | None = Query(default=None),
              unit: str | None = Query(default=None),
              session: Session = Depends(get_db),
              user: User = Depends(require_capability(READ))):
    """Confirmed HAI counts per 1,000 device-days by type over a
    date range (onset date / device-day date within [from, to],
    inclusive). Exact arithmetic; a type whose denominator is zero
    — or which is not device-day rated at all — reports rate null
    with an explicit reason."""
    for label, raw in (("from", from_), ("to", to)):
        if raw is not None:
            try:
                date.fromisoformat(raw[:10])
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"'{label}' must be an ISO date (YYYY-MM-DD)")

    def in_range(day: str) -> bool:
        if from_ and day < from_[:10]:
            return False
        if to and day > to[:10]:
            return False
        return True

    # Confirmed cases in range; with a unit filter, only cases
    # recorded against that unit count (unit-less cases count only
    # in the house-wide view).
    cases = [c for c in session.scalars(select(InfectionCase).where(
        InfectionCase.status == "confirmed")).all()
        if in_range(c.onset_date) and (unit is None or c.unit == unit)]
    day_rows = [r for r in session.scalars(select(DeviceDayRecord)).all()
                if in_range(r.date) and (unit is None or r.unit == unit)]
    denominators = {field: sum(getattr(r, field) for r in day_rows)
                    for field in DENOMINATOR_FIELDS.values()}
    items = []
    for code, concept in sorted(_hai_types(session).items()):
        count = sum(1 for c in cases if c.hai_type == code)
        denom_type = concept.get("denominatorType") or "none"
        field = DENOMINATOR_FIELDS.get(str(denom_type))
        if field is None:
            items.append({
                "type": code, "display": concept.get("display"),
                "confirmed_cases": count,
                "denominator_type": denom_type, "device_days": None,
                "rate_per_1000": None,
                "reason": (f"'{code}' is not rated against device-days "
                           f"(denominator: {denom_type}); no device-day "
                           "rate is computed for it")})
            continue
        total_days = denominators[field]
        if total_days == 0:
            items.append({
                "type": code, "display": concept.get("display"),
                "confirmed_cases": count,
                "denominator_type": denom_type, "device_days": 0,
                "rate_per_1000": None,
                "reason": (f"zero {denom_type} recorded in the range; "
                           "a rate per 1,000 device-days is undefined")})
        else:
            items.append({
                "type": code, "display": concept.get("display"),
                "confirmed_cases": count,
                "denominator_type": denom_type,
                "device_days": total_days,
                "rate_per_1000": count * 1000.0 / total_days,
                "reason": None})
    return {"from": from_, "to": to, "unit": unit, "items": items,
            "total_confirmed": sum(i["confirmed_cases"] for i in items)}


# ---------------------------------------------------------------------------
# Isolation precautions
# ---------------------------------------------------------------------------

def _precaution_out(row: IsolationPrecaution) -> dict:
    return {"id": row.precaution_id, "precaution_id": row.precaution_id,
            "patient_ref": row.patient_ref, "patient": row.patient_ref,
            "type": row.precaution_type,
            "precaution_type": row.precaution_type,
            "reason": row.reason, "unit": row.unit,
            "status": row.status, "started_on": row.started_on,
            "stopped_at": row.stopped_at, "stop_note": row.stop_note,
            "created_by": row.created_by}


@router.post("/precautions", status_code=201)
async def start_precaution(request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability(WORK))):
    """Start an isolation precaution for a patient. The type must be
    one of the published isolation-precautions concepts."""
    payload = await _json_body(request)
    ptype = _require_str(payload, "type", "precaution_type",
                         "precautionType")
    types = _isolation_types(session)
    if ptype not in types:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown isolation precaution type '{ptype}': not "
                   f"in the published isolation-precautions config "
                   f"(types: {sorted(types)})")
    patient_ref = _patient_ref(session, payload)
    reason = _require_str(payload, "reason")
    started = _optional_str(payload, "start", "start_date", "started_on",
                            "started_at") \
        or datetime.now(timezone.utc).date().isoformat()
    row = IsolationPrecaution(
        precaution_id=str(uuid.uuid4()), patient_ref=patient_ref,
        precaution_type=ptype, reason=reason,
        unit=_optional_str(payload, "unit"), status="active",
        started_on=started, created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="infection.precaution.start",
                 entity=f"IsolationPrecaution/{row.precaution_id}",
                 payload={"type": ptype, "patient": patient_ref,
                          "unit": row.unit, "reason": reason})
    session.commit()
    return _precaution_out(row)


@router.get("/precautions")
def list_precautions(patient: str | None = Query(default=None),
                     unit: str | None = Query(default=None),
                     active_only: bool = Query(default=False),
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability(READ))):
    """Precaution list — per patient (`?patient=`), per unit
    (`?unit=`), or house-wide; `active_only=true` gives the live
    isolation board."""
    stmt = select(IsolationPrecaution).order_by(
        IsolationPrecaution.created_at.asc(), IsolationPrecaution.id.asc())
    if patient:
        stmt = stmt.where(IsolationPrecaution.patient_ref
                          == fhir._normalize_patient_ref(patient))
    if unit:
        stmt = stmt.where(IsolationPrecaution.unit == unit)
    if active_only:
        stmt = stmt.where(IsolationPrecaution.status == "active")
    rows = session.scalars(stmt).all()
    return {"items": [_precaution_out(r) for r in rows],
            "total": len(rows)}


@router.post("/precautions/{precaution_id}/stop")
async def stop_precaution(precaution_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability(WORK))):
    """Stop an active precaution (clearance / discharge / review).
    Stopping an already-stopped precaution is a 409."""
    row = session.scalars(select(IsolationPrecaution).where(
        IsolationPrecaution.precaution_id == precaution_id)).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Isolation precaution {precaution_id} not found")
    if row.status != "active":
        raise HTTPException(
            status_code=409,
            detail=f"Isolation precaution {precaution_id} is already "
                   f"'{row.status}'")
    try:
        payload = await _json_body(request)
    except HTTPException:
        payload = {}
    row.status = "stopped"
    row.stopped_at = _now_iso()
    row.stop_note = _optional_str(payload, "note")
    session.flush()
    append_audit(session, actor=user.username,
                 action="infection.precaution.stop",
                 entity=f"IsolationPrecaution/{precaution_id}",
                 payload={"patient": row.patient_ref, "type":
                          row.precaution_type, "unit": row.unit})
    session.commit()
    return _precaution_out(row)
