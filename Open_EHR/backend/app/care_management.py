"""Phase 15 (standing scope) — payer / care-management operations
(Tapestry-class, app.care_management).

What lives here:

- **Care programmes as published config** (`terminology/care-programmes`,
  seeded published v1 below): diabetes-care, heart-failure, COPD —
  each entry carries its enrolment-criteria text, its outreach
  cadence in days, and the care-manager job role. Programme CRUD is
  Studio's; this module only READS the published config, so a site
  retunes a cadence or adds a programme by publishing a new version
  (standing rule: configure, don't fork).

- **Payer attribution** (`CareAttribution`, this module's own table —
  the protocols.py / barcodes.py pattern): a patient attributed to a
  payer + programme for a date window. The payer code is validated
  against the published `terminology/payers` from Phase 7
  (app.access) and the programme code against the published
  care-programmes config — unknown codes are 422s, never free text.

- **Enrolment** (`CareEnrolment`): patient + programme, lifecycle
  enrolled -> active -> discharged (discharge REQUIRES a reason).
  A duplicate live enrolment for the same patient + programme is a
  409. The care manager is an EXPLICIT staff username, validated
  against the Phase 13 people directory (app.people StaffMember,
  status active) — it is never defaulted from the programme's role.

- **Outreach** (`CareOutreach` log entries on an enrolment) and the
  due worklist: an enrolment is due when its last outreach (or, with
  none, its enrolment instant) is older than the programme's
  published cadence — computed in UTC on every read, oldest first.
  Logging outreach therefore clears the due state by construction.
  An overdue enrolment can be escalated explicitly
  (POST /care/outreach/escalate) into a FHIR Task owned by the care
  manager; there is NO background daemon — escalation happens only
  when a person asks for it, and a second escalation while one is
  open returns the existing Task.

- **Care gaps read-through** (GET /care/programmes/{code}/gaps):
  programme members with their open care gaps, computed by REUSING
  the Phase 12 continuum panel care-gap engine
  (app.continuum._gap_items — importable and callable with just a
  session, so this path is taken, not a fallback). This module does
  not reimplement gap evaluation; it filters the continuum items to
  the gap definitions whose condition code matches the programme's
  configured condition code, and to the programme's live members.
  A programme whose condition has no continuum gap definition
  returns its membership with an explicit note and no gaps.

Every mutation is hash-audit-chained in the same transaction.
Gates: reads coord.read, work coord.work (Phase 15 capability pair).
Synthetic data only; no live payer connection is claimed anywhere —
attribution is a local operational record, like Phase 7 eligibility.
"""
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import DateTime, Integer, JSON, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import boards, continuum, fhir, inpatient
from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import (ConfigStatus, FhirResource, StaffMember, StudioConfig,
                     User, utcnow)
from .security import get_db, require_capability

router = APIRouter(prefix="/care", tags=["care_management"])

READ = "coord.read"
WORK = "coord.work"

PROGRAMMES_KEY = "care-programmes"   # terminology/care-programmes
PAYERS_KEY = "payers"                # terminology/payers (Phase 7)

ENROLMENT_STATUSES = ("enrolled", "active", "discharged")
ENROLMENT_TRANSITIONS: dict[str, set[str]] = {
    "enrolled": {"active", "discharged"},
    "active": {"discharged"},
    "discharged": set(),
}
LIVE_STATUSES = ("enrolled", "active")
ENROLMENT_EXTENSION = "urn:opencare:extension/care-enrolment"
PROGRAMME_EXTENSION = "urn:opencare:extension/care-programme"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_dt(raw, field: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be an ISO datetime string")
    return _aware(parsed)


def _parse_date(raw, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(raw.strip()).isoformat()
    except ValueError:
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be an ISO date (YYYY-MM-DD)")


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


# ---------------------------------------------------------------------------
# This module's own tables (the protocols.py / barcodes.py pattern)
# ---------------------------------------------------------------------------

class CareAttribution(Base):
    """One payer attribution: a patient attributed to a payer +
    programme from a date (optionally to a date). Local operational
    record only — no live payer feed exists or is claimed."""
    __tablename__ = "care_attributions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attribution_id: Mapped[str] = mapped_column(String(120), unique=True,
                                                nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    payer_code: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    programme_code: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    attributed_from: Mapped[str] = mapped_column(String(20), nullable=False)
    attributed_to: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CareEnrolment(Base):
    """One patient's enrolment in one care programme, with its
    explicit care manager (a people-directory username) and its
    lifecycle state. `enrolled_at` is the cadence clock's start when
    the patient has never been outreached."""
    __tablename__ = "care_enrolments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enrolment_id: Mapped[str] = mapped_column(String(120), unique=True,
                                              nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    programme_code: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    care_manager: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False,
                                        default="enrolled", index=True)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=utcnow)
    discharge_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    discharged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class CareOutreach(Base):
    """One outreach log entry on an enrolment: channel, outcome, note
    and when it happened (`at` — the cadence clock reads the latest
    of these)."""
    __tablename__ = "care_outreach_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outreach_id: Mapped[str] = mapped_column(String(120), unique=True,
                                             nullable=False, index=True)
    enrolment_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(60), nullable=False)
    outcome: Mapped[str] = mapped_column(String(60), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    recorded_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Published care-programmes config (terminology/care-programmes)
# ---------------------------------------------------------------------------

def default_programmes_payload() -> dict:
    """Synthetic care programmes. Cadence, criteria and the
    care-manager job role are this config object's data — a site
    retunes them by publishing a new version in Studio, never by
    editing this module. `conditionCode` links a programme to the
    Phase 12 continuum care-gap definition that measures it."""
    def programme(code, display, criteria, cadence, condition):
        return {"code": code, "display": display, "active": True,
                "enrolmentCriteria": criteria,
                "outreachCadenceDays": cadence,
                "careManagerRole": "nurse",
                "conditionCode": condition}

    return {
        "name": "Care programmes (synthetic defaults)",
        "system": "urn:opencare:care-programme",
        "concepts": [
            programme(
                "diabetes-care", "Diabetes care management",
                "Type 2 diabetes on the attributed payer panel "
                "(Condition E11.9); enrol when the latest HbA1c is "
                "above target or no HbA1c is on file within 365 days.",
                30, "E11.9"),
            programme(
                "heart-failure", "Heart failure care management",
                "Heart failure on the attributed payer panel "
                "(Condition I50.9); enrol after a heart-failure "
                "admission or a new heart-failure diagnosis.",
                14, "I50.9"),
            programme(
                "copd", "COPD care management",
                "Chronic obstructive pulmonary disease on the "
                "attributed payer panel (Condition J44.9); enrol "
                "after an exacerbation or a new COPD diagnosis.",
                21, "J44.9"),
        ],
        "outreachChannels": ["phone", "message", "visit", "letter"],
        "outreachOutcomes": ["reached", "no-answer", "left-message",
                             "declined", "wrong-number"],
    }


def _programmes_config(session: Session) -> dict:
    cfg = published_config(session, "terminology", PROGRAMMES_KEY)
    if cfg is not None and isinstance(cfg.payload, dict) and cfg.payload:
        return cfg.payload
    return default_programmes_payload()


def _programme(session: Session, code: str) -> dict | None:
    for entry in _programmes_config(session).get("concepts") or []:
        if isinstance(entry, dict) and entry.get("code") == code \
                and entry.get("active") is not False:
            return entry
    return None


def _require_programme(session: Session, code: str) -> dict:
    programme = _programme(session, code)
    if programme is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown care programme '{code}': not in the published "
                   "terminology/care-programmes config")
    return programme


def _cadence_days(programme: dict) -> int:
    value = programme.get("outreachCadenceDays")
    return int(value) if isinstance(value, int) and value > 0 else 30


def _payer_codes(session: Session) -> set[str]:
    cfg = published_config(session, "terminology", PAYERS_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    return {str(c["code"]) for c in payload.get("concepts") or []
            if isinstance(c, dict) and c.get("code")
            and c.get("active") is not False}


def _care_manager_staff(session: Session, username: str) -> StaffMember:
    """The care manager must be a real, active person in the Phase 13
    people directory — a username nobody holds (or a person on leave
    / inactive) is a 422, never a free-text assignee."""
    row = session.scalars(select(StaffMember).where(
        StaffMember.username == username)).first()
    if row is None:
        raise HTTPException(
            status_code=422,
            detail=f"Care manager '{username}' is not in the people "
                   "directory (no staff entry carries that username)")
    if row.status != "active":
        raise HTTPException(
            status_code=422,
            detail=f"Care manager '{username}' is '{row.status}' in the "
                   "people directory; only active staff hold caseloads")
    return row


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------

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
                 payload={"kind": kind, "key": key, "version": 1,
                          "seeded_default": True})
    append_audit(session, actor="system", action="studio.config.publish",
                 entity=f"config:{kind}/{key}@v1",
                 payload={"kind": kind, "key": key, "version": 1,
                          "superseded_version": None, "seeded_default": True})


def ensure_care_management_defaults(session: Session) -> None:
    """Seed the care-programmes terminology as published v1
    (idempotent). Programme CRUD beyond this seed is Studio's."""
    _seed_published(session, "terminology", PROGRAMMES_KEY,
                    default_programmes_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Serializers + lookups
# ---------------------------------------------------------------------------

def _attribution_out(row: CareAttribution) -> dict:
    return {"attribution_id": row.attribution_id,
            "patient_ref": row.patient_ref, "payer": row.payer_code,
            "programme": row.programme_code,
            "attributed_from": row.attributed_from,
            "attributed_to": row.attributed_to,
            "created_by": row.created_by,
            "created_at": _aware(row.created_at).isoformat()
            if row.created_at else None}


def _enrolment_out(session: Session, row: CareEnrolment) -> dict:
    last = _last_outreach_at(session, row.enrolment_id)
    return {"enrolment_id": row.enrolment_id,
            "patient_ref": row.patient_ref,
            "programme": row.programme_code,
            "care_manager": row.care_manager,
            "status": row.status,
            "enrolled_at": _aware(row.enrolled_at).isoformat()
            if row.enrolled_at else None,
            "last_outreach_at": last.isoformat() if last else None,
            "discharge_reason": row.discharge_reason,
            "discharged_at": _aware(row.discharged_at).isoformat()
            if row.discharged_at else None,
            "created_by": row.created_by}


def _outreach_out(row: CareOutreach) -> dict:
    return {"outreach_id": row.outreach_id, "enrolment_id": row.enrolment_id,
            "channel": row.channel, "outcome": row.outcome,
            "note": row.note,
            "at": _aware(row.at).isoformat() if row.at else None,
            "recorded_by": row.recorded_by}


def _enrolment_row(session: Session, enrolment_id: str) -> CareEnrolment:
    row = session.scalars(select(CareEnrolment).where(
        CareEnrolment.enrolment_id == enrolment_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Enrolment {enrolment_id} not found")
    return row


def _last_outreach_at(session: Session, enrolment_id: str) -> datetime | None:
    rows = session.scalars(select(CareOutreach).where(
        CareOutreach.enrolment_id == enrolment_id)).all()
    times = [_aware(r.at) for r in rows if r.at is not None]
    return max(times) if times else None


def _due_entry(session: Session, row: CareEnrolment,
               now: datetime) -> dict | None:
    """The due-outreach entry for one live enrolment, or None when it
    is inside its programme's cadence. The clock is the last outreach
    (or the enrolment instant when never outreached), in UTC."""
    if row.status not in LIVE_STATUSES:
        return None
    programme = _programme(session, row.programme_code)
    if programme is None:
        return None
    cadence = _cadence_days(programme)
    last_contact = _last_outreach_at(session, row.enrolment_id) \
        or _aware(row.enrolled_at) or _aware(row.created_at) or now
    age_seconds = (now - last_contact).total_seconds()
    if age_seconds <= cadence * 86400:
        return None
    return {"enrolment_id": row.enrolment_id,
            "patient_ref": row.patient_ref,
            "programme": row.programme_code,
            "care_manager": row.care_manager,
            "status": row.status,
            "last_contact_at": last_contact.isoformat(),
            "days_since_contact": int(age_seconds // 86400),
            "cadence_days": cadence,
            "due_since": (last_contact + timedelta(days=cadence)).isoformat(),
            "days_overdue": int((age_seconds - cadence * 86400) // 86400)}


def _due_items(session: Session, now: datetime,
               care_manager: str | None = None) -> list[dict]:
    rows = session.scalars(select(CareEnrolment).where(
        CareEnrolment.status.in_(LIVE_STATUSES))).all()
    items = []
    for row in rows:
        if care_manager and row.care_manager != care_manager:
            continue
        entry = _due_entry(session, row, now)
        if entry is not None:
            items.append(entry)
    items.sort(key=lambda i: i["last_contact_at"])
    return items


# ---------------------------------------------------------------------------
# Programmes (read-through of the published Studio config)
# ---------------------------------------------------------------------------

@router.get("/programmes")
def list_programmes(session: Session = Depends(get_db),
                    user: User = Depends(require_capability(READ))):
    cfg = published_config(session, "terminology", PROGRAMMES_KEY)
    payload = _programmes_config(session)
    items = [dict(c) for c in payload.get("concepts") or []
             if isinstance(c, dict) and c.get("active") is not False]
    return {"items": items, "total": len(items),
            "config_version": cfg.version if cfg is not None else None,
            "outreach_channels": payload.get("outreachChannels") or [],
            "outreach_outcomes": payload.get("outreachOutcomes") or []}


@router.get("/programmes/{code}")
def get_programme(code: str, session: Session = Depends(get_db),
                  user: User = Depends(require_capability(READ))):
    programme = _programme(session, code)
    if programme is None:
        raise HTTPException(status_code=404,
                            detail=f"No published care programme '{code}'")
    return dict(programme)


@router.get("/programmes/{code}/gaps")
def programme_gaps(code: str, session: Session = Depends(get_db),
                   user: User = Depends(require_capability(READ))):
    """The programme's live members with their open care gaps.

    Gap computation is REUSED, not forked: continuum._gap_items (the
    Phase 12 panel engine — importable, callable with a session) is
    filtered to (a) the gap definitions whose conditionCode matches
    this programme's configured conditionCode and (b) this
    programme's live members. When no continuum gap definition
    measures the programme's condition, membership is returned with
    gaps: [] per member and an explicit gaps_note — no second gap
    engine is silently invented here."""
    programme = _programme(session, code)
    if programme is None:
        raise HTTPException(status_code=404,
                            detail=f"No published care programme '{code}'")
    members = session.scalars(select(CareEnrolment).where(
        CareEnrolment.programme_code == code,
        CareEnrolment.status.in_(LIVE_STATUSES))
        .order_by(CareEnrolment.enrolled_at.asc())).all()
    ops = continuum._ops(session)
    condition = programme.get("conditionCode")
    gap_codes = {g["code"] for g in ops.get("careGaps") or []
                 if isinstance(g, dict) and g.get("code")
                 and g.get("conditionCode") == condition}
    gaps_by_patient: dict[str, list[dict]] = {}
    if gap_codes:
        member_refs = {m.patient_ref for m in members}
        for item in continuum._gap_items(session, ops):
            if item["gap_code"] in gap_codes \
                    and item["patient_ref"] in member_refs:
                gaps_by_patient.setdefault(item["patient_ref"], []).append(item)
    items = []
    for member in members:
        gaps = gaps_by_patient.get(member.patient_ref, [])
        items.append({"patient_ref": member.patient_ref,
                      "enrolment_id": member.enrolment_id,
                      "care_manager": member.care_manager,
                      "status": member.status,
                      "gaps": gaps, "open_gaps": len(gaps)})
    return {"programme": code,
            "condition_code": condition,
            "gap_codes": sorted(gap_codes),
            "members": items, "total": len(items),
            "members_with_open_gaps": sum(1 for i in items if i["open_gaps"]),
            "open_gaps": sum(i["open_gaps"] for i in items),
            "gaps_basis": ("computed by the Phase 12 continuum panel "
                           "care-gap engine (app.continuum._gap_items), "
                           "filtered to this programme's members"
                           if gap_codes else
                           "membership only — no continuum care-gap "
                           "definition measures this programme's condition"),
            "gaps_note": (None if gap_codes else
                          f"No care-gap definition in the published "
                          f"continuum-ops config has conditionCode "
                          f"'{condition}'; members are listed with no "
                          f"computed gaps.")}


# ---------------------------------------------------------------------------
# Payer attribution
# ---------------------------------------------------------------------------

@router.post("/attributions", status_code=201)
async def create_attribution(request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    patient_ref = inpatient._patient_ref_of(session, payload)
    payer = _require_str(payload, "payer") if payload.get("payer") \
        else _require_str(payload, "payer_code")
    if payer not in _payer_codes(session):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown payer '{payer}': not in the published "
                   "terminology/payers config (Phase 7)")
    programme_code = _require_str(payload, "programme") \
        if payload.get("programme") else _require_str(payload, "programme_code")
    _require_programme(session, programme_code)
    attributed_from = _parse_date(payload["attributed_from"], "attributed_from") \
        if payload.get("attributed_from") else _now().date().isoformat()
    attributed_to = None
    if payload.get("attributed_to"):
        attributed_to = _parse_date(payload["attributed_to"], "attributed_to")
        if attributed_to < attributed_from:
            raise HTTPException(
                status_code=422,
                detail="'attributed_to' must not be before 'attributed_from'")
    duplicate = session.scalars(select(CareAttribution).where(
        CareAttribution.patient_ref == patient_ref,
        CareAttribution.payer_code == payer,
        CareAttribution.programme_code == programme_code,
        CareAttribution.attributed_from == attributed_from)).first()
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{patient_ref} is already attributed to payer "
                   f"'{payer}' + programme '{programme_code}' from "
                   f"{attributed_from} ({duplicate.attribution_id})")
    row = CareAttribution(attribution_id=f"attr-{uuid.uuid4().hex[:12]}",
                          patient_ref=patient_ref, payer_code=payer,
                          programme_code=programme_code,
                          attributed_from=attributed_from,
                          attributed_to=attributed_to,
                          created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="care.attribution.create",
                 entity=f"care-attribution:{row.attribution_id}",
                 payload={"patient": patient_ref, "payer": payer,
                          "programme": programme_code,
                          "attributed_from": attributed_from,
                          "attributed_to": attributed_to})
    session.commit()
    return _attribution_out(row)


@router.get("/attributions")
def list_attributions(patient_ref: str | None = Query(default=None),
                      payer: str | None = Query(default=None),
                      programme: str | None = Query(default=None),
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability(READ))):
    stmt = select(CareAttribution).order_by(CareAttribution.id.asc())
    if patient_ref:
        stmt = stmt.where(CareAttribution.patient_ref == patient_ref)
    if payer:
        stmt = stmt.where(CareAttribution.payer_code == payer)
    if programme:
        stmt = stmt.where(CareAttribution.programme_code == programme)
    rows = session.scalars(stmt).all()
    return {"items": [_attribution_out(r) for r in rows], "total": len(rows)}


# ---------------------------------------------------------------------------
# Enrolment + caseload
# ---------------------------------------------------------------------------

@router.post("/enrolments", status_code=201)
async def create_enrolment(request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    patient_ref = inpatient._patient_ref_of(session, payload)
    programme_code = _require_str(payload, "programme") \
        if payload.get("programme") else _require_str(payload, "programme_code")
    _require_programme(session, programme_code)
    care_manager = _require_str(payload, "care_manager")
    _care_manager_staff(session, care_manager)
    duplicate = session.scalars(select(CareEnrolment).where(
        CareEnrolment.patient_ref == patient_ref,
        CareEnrolment.programme_code == programme_code,
        CareEnrolment.status.in_(LIVE_STATUSES))).first()
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{patient_ref} already has a live enrolment in "
                   f"'{programme_code}' ({duplicate.enrolment_id}, "
                   f"status '{duplicate.status}')")
    enrolled_at = _parse_dt(payload["enrolled_at"], "enrolled_at") \
        if payload.get("enrolled_at") else _now()
    row = CareEnrolment(enrolment_id=f"enr-{uuid.uuid4().hex[:12]}",
                        patient_ref=patient_ref,
                        programme_code=programme_code,
                        care_manager=care_manager, status="enrolled",
                        enrolled_at=enrolled_at, created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="care.enrolment.create",
                 entity=f"care-enrolment:{row.enrolment_id}",
                 payload={"patient": patient_ref, "programme": programme_code,
                          "care_manager": care_manager})
    session.commit()
    return _enrolment_out(session, row)


@router.get("/enrolments")
def list_enrolments(patient_ref: str | None = Query(default=None),
                    programme: str | None = Query(default=None),
                    care_manager: str | None = Query(default=None),
                    status: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability(READ))):
    stmt = select(CareEnrolment).order_by(CareEnrolment.id.asc())
    if patient_ref:
        stmt = stmt.where(CareEnrolment.patient_ref == patient_ref)
    if programme:
        stmt = stmt.where(CareEnrolment.programme_code == programme)
    if care_manager:
        stmt = stmt.where(CareEnrolment.care_manager == care_manager)
    if status:
        stmt = stmt.where(CareEnrolment.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_enrolment_out(session, r) for r in rows],
            "total": len(rows)}


@router.get("/enrolments/{enrolment_id}")
def get_enrolment(enrolment_id: str, session: Session = Depends(get_db),
                  user: User = Depends(require_capability(READ))):
    return _enrolment_out(session, _enrolment_row(session, enrolment_id))


@router.post("/enrolments/{enrolment_id}/transition")
async def transition_enrolment(enrolment_id: str, request: Request,
                               session: Session = Depends(get_db),
                               user: User = Depends(require_capability(WORK))):
    """Lifecycle: enrolled -> active -> discharged (enrolled may also
    discharge directly). Discharge REQUIRES a reason; the reason and
    the discharge instant stay on the record."""
    payload = await _json_body(request)
    target = payload.get("to_status") or payload.get("status")
    if not isinstance(target, str) or target not in ENROLMENT_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"'status' must be one of {list(ENROLMENT_STATUSES)}")
    row = _enrolment_row(session, enrolment_id)
    allowed = ENROLMENT_TRANSITIONS.get(row.status, set())
    if target not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Enrolment {enrolment_id} is '{row.status}'; allowed "
                   f"transitions: {sorted(allowed) or 'none (terminal)'}")
    reason = payload.get("reason") or payload.get("discharge_reason")
    if target == "discharged":
        if not isinstance(reason, str) or not reason.strip():
            raise HTTPException(
                status_code=422,
                detail="Discharging an enrolment requires a 'reason'")
        row.discharge_reason = reason.strip()
        row.discharged_at = _now()
    previous = row.status
    row.status = target
    session.flush()
    append_audit(session, actor=user.username,
                 action="care.enrolment.transition",
                 entity=f"care-enrolment:{enrolment_id}",
                 payload={"from_status": previous, "to_status": target,
                          "reason": row.discharge_reason
                          if target == "discharged" else None,
                          "patient": row.patient_ref,
                          "programme": row.programme_code})
    session.commit()
    return _enrolment_out(session, row)


@router.get("/caseload/{staff_username}")
def caseload(staff_username: str, session: Session = Depends(get_db),
             user: User = Depends(require_capability(READ))):
    """One care manager's caseload: their LIVE enrolments (enrolled +
    active, never discharged), with per-enrolment due state and a
    by-programme count. An unknown username is a 404 — a caseload for
    a person who is not in the directory is not an empty caseload."""
    staff = session.scalars(select(StaffMember).where(
        StaffMember.username == staff_username)).first()
    if staff is None:
        raise HTTPException(
            status_code=404,
            detail=f"No staff member with username '{staff_username}' "
                   "in the people directory")
    now = _now()
    rows = session.scalars(select(CareEnrolment).where(
        CareEnrolment.care_manager == staff_username,
        CareEnrolment.status.in_(LIVE_STATUSES))
        .order_by(CareEnrolment.enrolled_at.asc())).all()
    items = []
    by_programme: dict[str, int] = {}
    overdue = 0
    for row in rows:
        entry = _enrolment_out(session, row)
        due = _due_entry(session, row, now)
        entry["outreach_due"] = due is not None
        entry["due_since"] = due["due_since"] if due else None
        items.append(entry)
        by_programme[row.programme_code] = \
            by_programme.get(row.programme_code, 0) + 1
        overdue += 1 if due else 0
    return {"care_manager": staff_username, "items": items,
            "total": len(items), "by_programme": by_programme,
            "overdue_outreach": overdue}


# ---------------------------------------------------------------------------
# Outreach log + due worklist + escalation
# ---------------------------------------------------------------------------

@router.post("/enrolments/{enrolment_id}/outreach", status_code=201)
async def log_outreach(enrolment_id: str, request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    row = _enrolment_row(session, enrolment_id)
    if row.status == "discharged":
        raise HTTPException(
            status_code=409,
            detail=f"Enrolment {enrolment_id} is discharged; outreach "
                   "cannot be logged on a closed enrolment")
    config = _programmes_config(session)
    channel = _require_str(payload, "channel")
    channels = config.get("outreachChannels") or []
    if channels and channel not in channels:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown outreach channel '{channel}': not in the "
                   f"published care-programmes config {channels}")
    outcome = _require_str(payload, "outcome")
    outcomes = config.get("outreachOutcomes") or []
    if outcomes and outcome not in outcomes:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown outreach outcome '{outcome}': not in the "
                   f"published care-programmes config {outcomes}")
    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        raise HTTPException(status_code=422, detail="'note' must be a string")
    at = _parse_dt(payload["at"], "at") if payload.get("at") else _now()
    entry = CareOutreach(outreach_id=f"out-{uuid.uuid4().hex[:12]}",
                         enrolment_id=enrolment_id, channel=channel,
                         outcome=outcome, note=note, at=at,
                         recorded_by=user.username)
    session.add(entry)
    session.flush()
    append_audit(session, actor=user.username, action="care.outreach.log",
                 entity=f"care-enrolment:{enrolment_id}",
                 payload={"outreach_id": entry.outreach_id,
                          "channel": channel, "outcome": outcome,
                          "at": at.isoformat(), "patient": row.patient_ref,
                          "programme": row.programme_code})
    session.commit()
    return _outreach_out(entry)


@router.get("/enrolments/{enrolment_id}/outreach")
def list_outreach(enrolment_id: str, session: Session = Depends(get_db),
                  user: User = Depends(require_capability(READ))):
    _enrolment_row(session, enrolment_id)
    rows = session.scalars(select(CareOutreach).where(
        CareOutreach.enrolment_id == enrolment_id)
        .order_by(CareOutreach.at.asc(), CareOutreach.id.asc())).all()
    return {"items": [_outreach_out(r) for r in rows], "total": len(rows)}


@router.get("/outreach/due")
def outreach_due(care_manager: str | None = Query(default=None),
                 session: Session = Depends(get_db),
                 user: User = Depends(require_capability(READ))):
    """Due-outreach worklist: live enrolments whose last outreach
    (or enrolment) is older than their programme's published cadence,
    oldest contact first — computed in UTC on every read, so logging
    outreach removes an enrolment from this list immediately."""
    items = _due_items(session, _now(), care_manager=care_manager)
    return {"items": items, "total": len(items),
            "generated_at": _now_iso(),
            "basis": ("last outreach (or enrolment) older than the "
                      "programme's published outreachCadenceDays")}


def _open_escalation_task(session: Session,
                          enrolment_id: str) -> FhirResource | None:
    for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Task",
            FhirResource.status.in_(["requested", "in-progress",
                                     "accepted"]))).all():
        ext = boards._get_extension(row.body or {}, ENROLMENT_EXTENSION)
        if ext and ext.get("valueString") == enrolment_id:
            return row
    return None


@router.post("/outreach/escalate", status_code=201)
async def escalate_outreach(request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability(WORK))):
    """Raise a FHIR Task for an OVERDUE enrolment, owned by its care
    manager. Explicit, on-demand escalation only — no daemon or
    scheduler is claimed. Not-yet-due enrolments are refused (409);
    a second escalation while one is open returns the open Task."""
    payload = await _json_body(request)
    enrolment_id = _require_str(payload, "enrolment_id")
    row = _enrolment_row(session, enrolment_id)
    due = _due_entry(session, row, _now())
    if due is None:
        raise HTTPException(
            status_code=409,
            detail=f"Enrolment {enrolment_id} is not overdue for "
                   "outreach; escalation requires an overdue enrolment")
    existing = _open_escalation_task(session, enrolment_id)
    if existing is not None:
        return {"task": fhir._render(existing), "existing": True,
                "enrolment_id": enrolment_id}
    body = {"resourceType": "Task", "id": str(uuid.uuid4()),
            "status": "requested", "intent": "order",
            "description": (f"Overdue care-management outreach: "
                            f"{row.programme_code} for {row.patient_ref} "
                            f"(last contact {due['last_contact_at']}, "
                            f"cadence {due['cadence_days']}d)"),
            "for": {"reference": row.patient_ref},
            "owner": {"display": row.care_manager},
            "authoredOn": _now_iso(),
            "requester": {"display": user.username}}
    boards._set_extension(body, ENROLMENT_EXTENSION, valueString=enrolment_id)
    boards._set_extension(body, PROGRAMME_EXTENSION,
                          valueString=row.programme_code)
    created = fhir.create_resource("Task", body, session, user)
    append_audit(session, actor=user.username,
                 action="care.outreach.escalate",
                 entity=f"Task/{created['id']}",
                 payload={"enrolment_id": enrolment_id,
                          "patient": row.patient_ref,
                          "programme": row.programme_code,
                          "care_manager": row.care_manager,
                          "days_overdue": due["days_overdue"]})
    session.commit()
    return {"task": created, "existing": False,
            "enrolment_id": enrolment_id}
