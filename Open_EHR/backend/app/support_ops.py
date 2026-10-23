"""Phase 15 (BD-11..14) — support operations.

Four operating stacks, all extending existing machinery rather than
forking it:

- **BD-11 Hospital-at-home operating stack.** The operation behind
  Phase 12's home-census board (app.continuum `/aftercare/hah`):
  telemetry bindings bind a home patient to a Phase 14 device
  (app.devices registry — the device must exist there), readings are
  evaluated against the published `terminology/hah-ops` thresholds,
  an out-of-range reading creates a REAL FHIR Task for the home-care
  team and records the breach on the binding, dispatches run a
  visit/supply/rapid-response queue, and supply kits are provisioned,
  issued and returned with a contents check-off.

- **BD-12 Patient transport dispatch.** Built ON the Phase 12
  TransportRequest table (app.continuum's command-centre transport
  list reads the same rows): this module adds the dispatch detail
  (mode, dispatcher, pickup/dropoff instants) in a companion table
  and the timing metrics computed from those instants. Modes and
  priorities are the published `terminology/transport-ops` config.

- **BD-13 Command centre as a staffed unit.** Desks, their role
  lists and huddle cadences are the published
  `terminology/command-desks` config; shift staffing, capacity
  huddle artefacts and ICU triage decisions are recorded here. A
  triage decision is only accepted from the config's
  triage-authority role (the HTRIP-equivalent) or an admin, and
  every decision is audited with the authority recorded.

- **BD-14 Access-line call centre.** Calls are logged against the
  published `terminology/access-line` referral sources; a
  `transfer-requested` disposition creates a REAL Phase 12
  TransferRequest (the transfer centre's own record, decided in
  /continuum/command/transfers) and this module exposes it as a
  queued intake record referencing it — the transfer centre's
  machinery is never forked.

Standing rules apply throughout: behaviour differences live in
published Studio config seeded idempotently below; every mutation
is hash-audit-chained in the same transaction; staff references are
validated against the Phase 13 staff directory (app.people's
StaffMember table); synthetic data only.
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (Boolean, DateTime, Float, Integer, JSON, String,
                        Text, select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import fhir
from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import (ConfigStatus, Device, FhirResource, StaffMember,
                     StudioConfig, TransferRequest, TransportRequest, User,
                     utcnow)
from .security import get_db, require_capability

router = APIRouter(prefix="/support", tags=["support_ops"])

READ = "support.read"
WORK = "support.work"

HAH_KEY = "hah-ops"                # terminology/hah-ops
TRANSPORT_KEY = "transport-ops"    # terminology/transport-ops
DESKS_KEY = "command-desks"        # terminology/command-desks
ACCESS_KEY = "access-line"         # terminology/access-line


# ---------------------------------------------------------------------------
# Own tables (the barcodes/protocols pattern: module tables on the
# shared Base; Phase 12 tables are reused, never duplicated)
# ---------------------------------------------------------------------------

class HahBinding(Base):
    """One home patient bound to one Phase 14 registry device."""
    __tablename__ = "support_hah_bindings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    binding_id: Mapped[str] = mapped_column(String(120), unique=True,
                                            nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    device_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active")
    breach_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    breaches: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class HahReading(Base):
    """One telemetry reading evaluated against the published thresholds."""
    __tablename__ = "support_hah_readings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reading_id: Mapped[str] = mapped_column(String(120), unique=True,
                                            nullable=False, index=True)
    binding_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    device_key: Mapped[str] = mapped_column(String(120), nullable=False)
    reading_type: Mapped[str] = mapped_column(String(80), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    in_range: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    breach: Mapped[str | None] = mapped_column(String(20), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HahDispatch(Base):
    """One hospital-at-home dispatch (visit / supply / rapid-response)."""
    __tablename__ = "support_hah_dispatches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dispatch_id: Mapped[str] = mapped_column(String(120), unique=True,
                                             nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    dispatch_type: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="requested")
    assignee: Mapped[str | None] = mapped_column(String(200), nullable=True)
    assignee_staff_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class HahKit(Base):
    """One provisioned supply kit and its issue/return lifecycle."""
    __tablename__ = "support_hah_kits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kit_id: Mapped[str] = mapped_column(String(120), unique=True,
                                        nullable=False, index=True)
    kit_type: Mapped[str] = mapped_column(String(80), nullable=False)
    contents: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="available")
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    issued_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expected_return_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    returned_contents: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SupportTransportDetail(Base):
    """Dispatch detail for a Phase 12 TransportRequest (companion
    row, 1:1 by request_id): mode, dispatcher and the pickup /
    dropoff instants the metrics are computed from. The request row
    itself stays the continuum TransportRequest — the command
    centre's transport list keeps working on the same data."""
    __tablename__ = "support_transport_details"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(120), unique=True,
                                            nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(60), nullable=False, default="wheelchair-van")
    dispatcher: Mapped[str | None] = mapped_column(String(200), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pickup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dropoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CommandStaffing(Base):
    """One staff member staffing one command desk for one shift window."""
    __tablename__ = "support_command_staffing"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    staffing_id: Mapped[str] = mapped_column(String(120), unique=True,
                                             nullable=False, index=True)
    desk: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    staff_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    staff_username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    staff_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CommandHuddle(Base):
    """One capacity huddle artefact — the structured record IS the
    meeting record (census / boarding / expected discharges,
    decisions taken, who chaired)."""
    __tablename__ = "support_command_huddles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    huddle_id: Mapped[str] = mapped_column(String(120), unique=True,
                                           nullable=False, index=True)
    desk: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    census: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    boarding: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_discharges: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    decisions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    chaired_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    held_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TriageDecision(Base):
    """One ICU triage decision, with the authority it was made under."""
    __tablename__ = "support_triage_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(120), unique=True,
                                            nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(40), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    authority_role: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    authority_basis: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AccessCall(Base):
    """One access-line call. A transfer-requested disposition links
    to the Phase 12 TransferRequest it created."""
    __tablename__ = "support_access_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    caller_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    caller_org: Mapped[str | None] = mapped_column(String(200), nullable=True)
    referral_source: Mapped[str] = mapped_column(String(80), nullable=False)
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    patient_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    patient_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    disposition: Mapped[str] = mapped_column(String(60), nullable=False)
    transfer_request_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    call_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    aware = _aware(dt)
    return aware.isoformat() if aware else None


def _parse_dt(value, field: str) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be an ISO date/datetime")
    text = value.strip()
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        return _aware(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        raise HTTPException(status_code=422,
                            detail=f"'{field}' is not a valid ISO date/datetime: '{value}'")


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


def _require_str(payload: dict, field: str, *aliases: str) -> str:
    for name in (field, *aliases):
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise HTTPException(status_code=422,
                        detail=f"'{field}' is required and must be a non-empty string")


def _opt_str(payload: dict, *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _patient_ref(session: Session, payload: dict) -> str:
    """Resolve + validate a patient reference (Patient must exist)."""
    raw = _opt_str(payload, "patient_ref", "patient", "patient_id")
    subject = payload.get("subject")
    if raw is None and isinstance(subject, dict):
        raw = subject.get("reference")
    if not raw:
        raise HTTPException(status_code=422,
                            detail="Requires 'patient_ref' (or 'patient') naming a Patient")
    ref = raw if raw.startswith("Patient/") else f"Patient/{raw}"
    fhir._get_row("Patient", ref.split("/", 1)[1], session)  # 404 if unknown
    return ref


def _staff(session: Session, value: str | None) -> StaffMember | None:
    """A staff-directory entry by staff_id OR by directory username."""
    if not value:
        return None
    row = session.scalars(select(StaffMember).where(
        StaffMember.staff_id == value)).first()
    if row is None:
        row = session.scalars(select(StaffMember).where(
            StaffMember.username == value)).first()
    return row


def _require_staff(session: Session, value: str | None, field: str) -> StaffMember:
    row = _staff(session, value)
    if row is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown {field} '{value}': not in the staff directory "
                   "(app.people StaffMember — match by staff_id or username)")
    return row


def _cfg(session: Session, key: str, default_fn) -> dict:
    cfg = published_config(session, "terminology", key)
    if cfg is not None and isinstance(cfg.payload, dict) and cfg.payload:
        return cfg.payload
    return default_fn()


def _codes(entries) -> set[str]:
    return {str(e["code"]) for e in entries or []
            if isinstance(e, dict) and e.get("code")}


# ---------------------------------------------------------------------------
# Published config defaults (terminology/*)
# ---------------------------------------------------------------------------

def default_hah_payload() -> dict:
    thresholds = [
        {"type": "heart-rate", "display": "Heart rate", "unit": "bpm",
         "min": 50, "max": 120},
        {"type": "systolic-bp", "display": "Systolic blood pressure",
         "unit": "mmHg", "min": 90, "max": 180},
        {"type": "spo2", "display": "Oxygen saturation (SpO2)",
         "unit": "%", "min": 92, "max": 100},
        {"type": "temperature", "display": "Body temperature",
         "unit": "degC", "min": 35.5, "max": 38.5},
        {"type": "blood-glucose", "display": "Blood glucose",
         "unit": "mg/dL", "min": 70, "max": 180},
        {"type": "respiratory-rate", "display": "Respiratory rate",
         "unit": "/min", "min": 10, "max": 24},
    ]
    return {
        "name": "Hospital-at-home operations (synthetic defaults)",
        "concepts": [{"code": t["type"], "display": t["display"], "active": True}
                     for t in thresholds],
        "readingThresholds": thresholds,
        # Alias key (some consumers name them thresholds).
        "thresholds": thresholds,
        "dispatchTypes": [
            {"code": "visit", "display": "Clinician visit", "slaMinutes": 240},
            {"code": "supply", "display": "Supply delivery", "slaMinutes": 480},
            {"code": "rapid-response", "display": "Rapid response",
             "slaMinutes": 30},
        ],
        "dispatchSlaMinutes": {"visit": 240, "supply": 480,
                              "rapid-response": 30},
        "homeCareTeam": "home-care team",
        "taskOwnerRole": "home-care",
        "kitTypes": [
            {"code": "standard-hah", "display": "Standard HaH kit",
             "contents": ["tablet-monitor", "bp-cuff", "pulse-oximeter",
                          "thermometer", "medication-pack"]},
            {"code": "respiratory", "display": "Respiratory kit",
             "contents": ["oxygen-concentrator", "pulse-oximeter",
                          "nebuliser", "tubing-set"]},
            {"code": "infusion", "display": "Infusion kit",
             "contents": ["infusion-pump", "iv-stand", "line-set",
                          "medication-pack"]},
        ],
        "kitLoanDays": 7,
    }


def default_transport_payload() -> dict:
    modes = [
        {"code": "wheelchair-van", "display": "Wheelchair van"},
        {"code": "stretcher-van", "display": "Stretcher van"},
        {"code": "ambulance", "display": "Ambulance"},
        {"code": "internal-porter", "display": "Internal porter"},
    ]
    priorities = [
        {"code": "routine", "display": "Routine"},
        {"code": "urgent", "display": "Urgent"},
        {"code": "stat", "display": "STAT"},
    ]
    return {
        "name": "Patient transport operations (synthetic defaults)",
        "concepts": [{"code": m["code"], "display": m["display"], "active": True}
                     for m in modes],
        "modes": modes,
        "priorities": priorities,
    }


def default_desks_payload() -> dict:
    desks = [
        {"code": "bed-management", "display": "Bed management desk",
         "roles": ["admin", "clinician", "unit-manager"],
         "huddleCadence": "every-4-hours"},
        {"code": "access-transfer", "display": "Access & transfer desk",
         "roles": ["admin", "clinician"],
         "huddleCadence": "twice-daily"},
        {"code": "transport", "display": "Transport desk",
         "roles": ["admin", "clinician", "support-staff"],
         "huddleCadence": "daily"},
    ]
    return {
        "name": "Command-centre desks (synthetic defaults)",
        "concepts": [{"code": d["code"], "display": d["display"], "active": True}
                     for d in desks],
        "desks": desks,
        # The HTRIP-equivalent: the ONE job role (staff-directory
        # job_role) authorised to record ICU triage decisions, besides
        # an admin sign-in.
        "triageAuthorityRole": "physician",
        "triageAuthorityDisplay": "ICU triage authority (HTRIP-equivalent)",
        "triageDecisions": ["admit-icu", "defer", "decline"],
    }


def default_access_payload() -> dict:
    sources = [
        {"code": "referring-hospital", "display": "Referring hospital"},
        {"code": "primary-care", "display": "Primary care"},
        {"code": "clinic", "display": "Clinic"},
        {"code": "community", "display": "Community / self-referral"},
        {"code": "payer", "display": "Payer / insurer"},
        {"code": "other", "display": "Other"},
    ]
    dispositions = ["advice", "transfer-requested", "clinic-referral", "other"]
    return {
        "name": "Access-line call centre (synthetic defaults)",
        "concepts": [{"code": s["code"], "display": s["display"], "active": True}
                     for s in sources],
        "referralSources": sources,
        "sources": [s["code"] for s in sources],
        "dispositions": dispositions,
    }


def _hah(session: Session) -> dict:
    return _cfg(session, HAH_KEY, default_hah_payload)


def _transport_cfg(session: Session) -> dict:
    return _cfg(session, TRANSPORT_KEY, default_transport_payload)


def _desks_cfg(session: Session) -> dict:
    return _cfg(session, DESKS_KEY, default_desks_payload)


def _access_cfg(session: Session) -> dict:
    return _cfg(session, ACCESS_KEY, default_access_payload)


def _threshold(cfg: dict, reading_type: str) -> dict | None:
    for entry in cfg.get("readingThresholds") or cfg.get("thresholds") or []:
        if isinstance(entry, dict) and entry.get("type") == reading_type:
            return entry
    return None


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


def ensure_support_ops_defaults(session: Session) -> None:
    """Seed the four published terminology configs (idempotent)."""
    _seed_published(session, "terminology", HAH_KEY, default_hah_payload())
    _seed_published(session, "terminology", TRANSPORT_KEY,
                    default_transport_payload())
    _seed_published(session, "terminology", DESKS_KEY, default_desks_payload())
    _seed_published(session, "terminology", ACCESS_KEY, default_access_payload())
    session.commit()


@router.get("/config")
def support_config(session: Session = Depends(get_db),
                   user: User = Depends(require_capability(READ))):
    """All four published support-ops configs, as the server reads them."""
    return {"hah-ops": _hah(session), "transport-ops": _transport_cfg(session),
            "command-desks": _desks_cfg(session),
            "access-line": _access_cfg(session)}


# ---------------------------------------------------------------------------
# BD-11a — Telemetry bindings + readings
# ---------------------------------------------------------------------------

def _binding_out(row: HahBinding, session: Session) -> dict:
    device = session.scalars(select(Device).where(
        Device.device_key == row.device_key)).first()
    return {"id": row.binding_id, "binding_id": row.binding_id,
            "patient_ref": row.patient_ref,
            "device_id": row.device_key, "device_key": row.device_key,
            "device_name": device.name if device else None,
            "status": row.status, "active": row.status == "active",
            "breach_count": row.breach_count,
            "breaches": row.breaches or [],
            "created_by": row.created_by,
            "created_at": _iso(row.created_at)}


def _binding_row(session: Session, binding_id: str) -> HahBinding:
    row = session.scalars(select(HahBinding).where(
        HahBinding.binding_id == binding_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"HaH binding '{binding_id}' not found")
    return row


@router.post("/hah/bindings", status_code=201)
async def create_binding(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(require_capability(WORK))):
    """Bind a home patient to a Phase 14 registry device. The device
    must exist in the device registry (app.devices); one ACTIVE
    binding per device — a second active binding is a 409."""
    payload = await _json_body(request)
    patient_ref = _patient_ref(session, payload)
    device_key = _require_str(payload, "device_id", "device_key", "device")
    device = session.scalars(select(Device).where(
        Device.device_key == device_key)).first()
    if device is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown device '{device_key}': not in the Phase 14 "
                   "device registry (app.devices)")
    clash = session.scalars(select(HahBinding).where(
        HahBinding.device_key == device_key,
        HahBinding.status == "active")).first()
    if clash is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Device '{device_key}' already has an active binding "
                   f"({clash.binding_id}) for {clash.patient_ref}")
    row = HahBinding(binding_id=f"hahb-{uuid.uuid4().hex[:10]}",
                     patient_ref=patient_ref, device_key=device_key,
                     status="active", breaches=[], created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="support.hah.binding.create",
                 entity=f"hah-binding/{row.binding_id}",
                 payload={"patient": patient_ref, "device_key": device_key})
    session.commit()
    return _binding_out(row, session)


@router.get("/hah/bindings")
def list_bindings(patient_ref: str | None = Query(default=None),
                  device_key: str | None = Query(default=None),
                  status: str | None = Query(default=None),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability(READ))):
    stmt = select(HahBinding).order_by(HahBinding.id.asc())
    if patient_ref:
        stmt = stmt.where(HahBinding.patient_ref == patient_ref)
    if device_key:
        stmt = stmt.where(HahBinding.device_key == device_key)
    if status:
        stmt = stmt.where(HahBinding.status == status)
    rows = session.scalars(stmt).all()
    items = [_binding_out(r, session) for r in rows]
    return {"items": items, "total": len(items)}


@router.post("/hah/bindings/{binding_id}/deactivate")
def deactivate_binding(binding_id: str, session: Session = Depends(get_db),
                       user: User = Depends(require_capability(WORK))):
    row = _binding_row(session, binding_id)
    if row.status != "active":
        raise HTTPException(status_code=409,
                            detail=f"Binding '{binding_id}' is already '{row.status}'")
    row.status = "inactive"
    session.flush()
    append_audit(session, actor=user.username,
                 action="support.hah.binding.deactivate",
                 entity=f"hah-binding/{binding_id}",
                 payload={"patient": row.patient_ref, "device_key": row.device_key})
    session.commit()
    return _binding_out(row, session)


def _reading_out(row: HahReading) -> dict:
    return {"id": row.reading_id, "reading_id": row.reading_id,
            "binding_id": row.binding_id, "patient_ref": row.patient_ref,
            "device_id": row.device_key, "device_key": row.device_key,
            "reading_type": row.reading_type, "type": row.reading_type,
            "value": row.value, "unit": row.unit,
            "at": _iso(row.at), "recorded_at": _iso(row.at),
            "in_range": row.in_range, "breach": row.breach,
            "task_id": row.task_id,
            "task_ref": f"Task/{row.task_id}" if row.task_id else None}


@router.post("/hah/readings", status_code=201)
async def create_reading(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(require_capability(WORK))):
    """Record one telemetry reading (synthetic feed in this
    deployment) and evaluate it against the published hah-ops
    thresholds. Out-of-range: a real FHIR Task for the home-care
    team is created AND the breach is recorded on the binding.
    In-range: nothing is created."""
    payload = await _json_body(request)
    binding = _binding_row(session, _require_str(payload, "binding_id", "binding"))
    if binding.status != "active":
        raise HTTPException(status_code=409,
                            detail=f"Binding '{binding.binding_id}' is "
                                   f"'{binding.status}'; readings need an active binding")
    cfg = _hah(session)
    reading_type = _require_str(payload, "reading_type", "type")
    threshold = _threshold(cfg, reading_type)
    if threshold is None:
        known = [t.get("type") for t in cfg.get("readingThresholds") or []]
        raise HTTPException(
            status_code=422,
            detail=f"Unknown reading type '{reading_type}': not in the "
                   f"published hah-ops thresholds {known}")
    value = payload.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(status_code=422, detail="Reading 'value' must be numeric")
    at = _parse_dt(payload["at"], "at") if payload.get("at") else (
        _parse_dt(payload["recorded_at"], "recorded_at")
        if payload.get("recorded_at") else _now())
    low, high = threshold.get("min"), threshold.get("max")
    breach = None
    if low is not None and float(value) < float(low):
        breach = "low"
    elif high is not None and float(value) > float(high):
        breach = "high"
    in_range = breach is None
    reading = HahReading(
        reading_id=f"hahr-{uuid.uuid4().hex[:10]}",
        binding_id=binding.binding_id, patient_ref=binding.patient_ref,
        device_key=binding.device_key, reading_type=reading_type,
        value=float(value), unit=str(payload.get("unit") or threshold.get("unit") or ""),
        at=at, in_range=in_range, breach=breach, created_by=user.username)
    session.add(reading)
    task_id = None
    if not in_range:
        breach_entry = {"reading_id": reading.reading_id,
                        "reading_type": reading_type, "value": float(value),
                        "unit": reading.unit, "breach": breach,
                        "at": at.isoformat(), "task_id": None}
        binding.breaches = [*(binding.breaches or []), breach_entry]
        binding.breach_count = len(binding.breaches)
        session.flush()
        team = cfg.get("homeCareTeam") or "home-care team"
        body = {"resourceType": "Task", "id": str(uuid.uuid4()),
                "status": "requested", "intent": "order",
                "description": (f"Hospital-at-home telemetry breach: "
                                f"{reading_type} = {float(value)} {reading.unit} "
                                f"({breach} of published range {low}-{high}) "
                                f"for {binding.patient_ref} via device "
                                f"{binding.device_key} — {team} to review"),
                "for": {"reference": binding.patient_ref},
                "authoredOn": _now().isoformat(),
                "requester": {"display": user.username},
                "owner": {"display": team}}
        created = fhir.create_resource("Task", body, session, user)
        task_id = created["id"]
        reading.task_id = task_id
        # fhir.create_resource commits; re-attach the breach's task link.
        binding = _binding_row(session, binding.binding_id)
        breaches = [dict(b) for b in binding.breaches or []]
        for entry in breaches:
            if entry.get("reading_id") == reading.reading_id:
                entry["task_id"] = task_id
        binding.breaches = breaches
        session.flush()
    append_audit(session, actor=user.username, action="support.hah.reading",
                 entity=f"hah-binding/{binding.binding_id}",
                 payload={"reading_id": reading.reading_id,
                          "reading_type": reading_type, "value": float(value),
                          "in_range": in_range, "breach": breach,
                          "task_id": task_id, "patient": binding.patient_ref,
                          "source_label": "simulated"})
    session.commit()
    return _reading_out(reading)


@router.get("/hah/readings")
def list_readings(binding_id: str | None = Query(default=None),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability(READ))):
    stmt = select(HahReading).order_by(HahReading.id.asc())
    if binding_id:
        stmt = stmt.where(HahReading.binding_id == binding_id)
    rows = session.scalars(stmt).all()
    items = [_reading_out(r) for r in rows]
    return {"items": items, "total": len(items),
            "breaches": sum(1 for i in items if not i["in_range"])}


# ---------------------------------------------------------------------------
# BD-11b — Dispatch queue
# ---------------------------------------------------------------------------

_DISPATCH_FLOW = {"requested": {"assigned", "cancelled"},
                  "assigned": {"en-route", "cancelled"},
                  "en-route": {"arrived", "cancelled"},
                  "arrived": {"completed", "cancelled"},
                  "completed": set(), "cancelled": set()}


def _dispatch_out(row: HahDispatch, cfg: dict) -> dict:
    sla = (cfg.get("dispatchSlaMinutes") or {}).get(row.dispatch_type)
    return {"id": row.dispatch_id, "dispatch_id": row.dispatch_id,
            "patient_ref": row.patient_ref, "type": row.dispatch_type,
            "dispatch_type": row.dispatch_type,
            "status": row.status, "state": row.status,
            "assignee": row.assignee, "assignee_staff_id": row.assignee_staff_id,
            "note": row.note, "sla_minutes": sla,
            "due_at": _iso(_aware(row.created_at) + timedelta(minutes=sla))
            if sla and row.created_at else None,
            "history": row.history or [],
            "created_by": row.created_by, "created_at": _iso(row.created_at)}


def _dispatch_row(session: Session, dispatch_id: str) -> HahDispatch:
    row = session.scalars(select(HahDispatch).where(
        HahDispatch.dispatch_id == dispatch_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"HaH dispatch '{dispatch_id}' not found")
    return row


def _apply_assignee(session: Session, payload: dict, row: HahDispatch) -> None:
    value = _opt_str(payload, "assignee", "assignee_staff_id", "assigned_to",
                     "staff_id", "staff")
    if value is not None:
        staff = _require_staff(session, value, "assignee")
        row.assignee = staff.username or staff.staff_id
        row.assignee_staff_id = staff.staff_id


@router.post("/hah/dispatches", status_code=201)
async def create_dispatch(request: Request, session: Session = Depends(get_db),
                          user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    cfg = _hah(session)
    patient_ref = _patient_ref(session, payload)
    dtype = _require_str(payload, "type", "dispatch_type")
    if dtype not in _codes(cfg.get("dispatchTypes")):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown dispatch type '{dtype}': not in the published "
                   "hah-ops dispatch types")
    row = HahDispatch(dispatch_id=f"hahd-{uuid.uuid4().hex[:10]}",
                      patient_ref=patient_ref, dispatch_type=dtype,
                      status="requested", note=payload.get("note"),
                      history=[{"state": "requested", "at": _now().isoformat(),
                                "by": user.username}],
                      created_by=user.username)
    _apply_assignee(session, payload, row)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="support.hah.dispatch.create",
                 entity=f"hah-dispatch/{row.dispatch_id}",
                 payload={"patient": patient_ref, "type": dtype,
                          "assignee": row.assignee})
    session.commit()
    return _dispatch_out(row, cfg)


@router.get("/hah/dispatches")
def list_dispatches(state: str | None = Query(default=None),
                    status: str | None = Query(default=None),
                    type: str | None = Query(default=None),
                    patient_ref: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability(READ))):
    cfg = _hah(session)
    stmt = select(HahDispatch).order_by(HahDispatch.id.asc())
    wanted = state or status
    if wanted:
        stmt = stmt.where(HahDispatch.status == wanted)
    if type:
        stmt = stmt.where(HahDispatch.dispatch_type == type)
    if patient_ref:
        stmt = stmt.where(HahDispatch.patient_ref == patient_ref)
    rows = session.scalars(stmt).all()
    items = [_dispatch_out(r, cfg) for r in rows]
    return {"items": items, "total": len(items)}


@router.get("/hah/dispatches/{dispatch_id}")
def get_dispatch(dispatch_id: str, session: Session = Depends(get_db),
                 user: User = Depends(require_capability(READ))):
    return _dispatch_out(_dispatch_row(session, dispatch_id), _hah(session))


@router.post("/hah/dispatches/{dispatch_id}/transition")
async def dispatch_transition(dispatch_id: str, request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    cfg = _hah(session)
    target = _require_str(payload, "to_state", "to_status", "state", "status")
    target = target.replace("_", "-")
    row = _dispatch_row(session, dispatch_id)
    if target not in _DISPATCH_FLOW.get(row.status, set()):
        raise HTTPException(status_code=409,
                            detail=f"Illegal dispatch transition "
                                   f"'{row.status}' -> '{target}'")
    _apply_assignee(session, payload, row)
    if target == "assigned" and not row.assignee:
        raise HTTPException(status_code=422,
                            detail="Assigning a dispatch requires an 'assignee' "
                                   "validated against the staff directory")
    row.status = target
    row.history = [*(row.history or []),
                   {"state": target, "at": _now().isoformat(),
                    "by": user.username, "assignee": row.assignee}]
    session.flush()
    append_audit(session, actor=user.username,
                 action="support.hah.dispatch.transition",
                 entity=f"hah-dispatch/{dispatch_id}",
                 payload={"to_state": target, "assignee": row.assignee,
                          "patient": row.patient_ref})
    session.commit()
    return _dispatch_out(row, cfg)


# ---------------------------------------------------------------------------
# BD-11c — Kit provisioning
# ---------------------------------------------------------------------------

def _kit_out(row: HahKit, now: datetime | None = None) -> dict:
    now = now or _now()
    overdue = bool(row.status == "issued" and row.expected_return_at
                   and _aware(row.expected_return_at) < now)
    return {"id": row.kit_id, "kit_id": row.kit_id, "kit_type": row.kit_type,
            "type": row.kit_type, "contents": row.contents or [],
            "status": row.status, "patient_ref": row.patient_ref,
            "issued_by": row.issued_by, "issued_at": _iso(row.issued_at),
            "expected_return_at": _iso(row.expected_return_at),
            "expected_return": _iso(row.expected_return_at),
            "returned_at": _iso(row.returned_at),
            "returned_contents": row.returned_contents,
            "overdue": overdue, "created_by": row.created_by,
            "created_at": _iso(row.created_at)}


def _kit_row(session: Session, kit_id: str) -> HahKit:
    row = session.scalars(select(HahKit).where(HahKit.kit_id == kit_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"HaH kit '{kit_id}' not found")
    return row


@router.post("/hah/kits", status_code=201)
async def create_kit(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    cfg = _hah(session)
    kit_type = _require_str(payload, "kit_type", "type")
    type_cfg = next((k for k in cfg.get("kitTypes") or []
                     if isinstance(k, dict) and k.get("code") == kit_type), None)
    if type_cfg is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown kit type '{kit_type}': not in the published "
                   "hah-ops kit types")
    contents = payload.get("contents")
    if contents is not None:
        if not isinstance(contents, list) or \
                not all(isinstance(c, str) and c for c in contents):
            raise HTTPException(status_code=422,
                                detail="'contents' must be a list of item names")
    else:
        contents = list(type_cfg.get("contents") or [])
    kit_id = _opt_str(payload, "kit_id") or f"kit-{uuid.uuid4().hex[:10]}"
    if session.scalars(select(HahKit).where(HahKit.kit_id == kit_id)).first():
        raise HTTPException(status_code=409, detail=f"Kit '{kit_id}' already exists")
    row = HahKit(kit_id=kit_id, kit_type=kit_type, contents=contents,
                 status="available", created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="support.hah.kit.create",
                 entity=f"hah-kit/{kit_id}",
                 payload={"kit_type": kit_type, "contents": contents})
    session.commit()
    return _kit_out(row)


@router.get("/hah/kits/overdue")
def overdue_kits(session: Session = Depends(get_db),
                 user: User = Depends(require_capability(READ))):
    """Kits still issued whose expected return (UTC) has passed."""
    now = _now()
    rows = session.scalars(select(HahKit).where(
        HahKit.status == "issued").order_by(HahKit.expected_return_at.asc())).all()
    items = []
    for row in rows:
        if row.expected_return_at and _aware(row.expected_return_at) < now:
            out = _kit_out(row, now)
            out["overdue_minutes"] = int(
                (now - _aware(row.expected_return_at)).total_seconds() // 60)
            items.append(out)
    return {"items": items, "total": len(items), "generated_at": now.isoformat()}


@router.get("/hah/kits")
def list_kits(status: str | None = Query(default=None),
              session: Session = Depends(get_db),
              user: User = Depends(require_capability(READ))):
    stmt = select(HahKit).order_by(HahKit.id.asc())
    if status:
        stmt = stmt.where(HahKit.status == status)
    rows = session.scalars(stmt).all()
    items = [_kit_out(r) for r in rows]
    return {"items": items, "total": len(items)}


@router.post("/hah/kits/{kit_id}/issue")
async def issue_kit(kit_id: str, request: Request,
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    cfg = _hah(session)
    row = _kit_row(session, kit_id)
    if row.status != "available":
        raise HTTPException(status_code=409,
                            detail=f"Kit '{kit_id}' is '{row.status}'; only an "
                                   "available kit can be issued")
    patient_ref = _patient_ref(session, payload)
    raw_expected = _opt_str(payload, "expected_return_at", "expected_return",
                            "due_at")
    if raw_expected:
        expected = _parse_dt(raw_expected, "expected_return_at")
    else:
        loan = cfg.get("kitLoanDays")
        days = int(loan) if isinstance(loan, int) and loan > 0 else 7
        expected = _now() + timedelta(days=days)
    row.status = "issued"
    row.patient_ref = patient_ref
    row.issued_by = user.username
    row.issued_at = _now()
    row.expected_return_at = expected
    session.flush()
    append_audit(session, actor=user.username, action="support.hah.kit.issue",
                 entity=f"hah-kit/{kit_id}",
                 payload={"patient": patient_ref,
                          "contents_checked_out": row.contents,
                          "expected_return_at": expected.isoformat()})
    session.commit()
    return _kit_out(row)


@router.post("/hah/kits/{kit_id}/return")
async def return_kit(kit_id: str, request: Request,
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability(WORK))):
    """Return a kit with a contents check-off. A missing item refuses
    the return (422, naming the item) — the kit stays issued."""
    payload = await _json_body(request)
    row = _kit_row(session, kit_id)
    if row.status != "issued":
        raise HTTPException(status_code=409,
                            detail=f"Kit '{kit_id}' is '{row.status}'; only an "
                                   "issued kit can be returned")
    returned = None
    for name in ("returned_contents", "returned", "contents",
                 "checked_contents", "checkoff", "contents_checkoff"):
        if isinstance(payload.get(name), list):
            returned = [str(c) for c in payload[name]]
            break
    if returned is not None:
        missing = [item for item in row.contents or [] if item not in returned]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"Kit return refused: missing item(s): "
                       f"{', '.join(missing)} — every issued item must be "
                       "checked off before the kit can be returned")
        row.returned_contents = returned
    else:
        row.returned_contents = list(row.contents or [])
    row.status = "returned"
    row.returned_at = (_parse_dt(payload["at"], "at")
                       if payload.get("at") else _now())
    session.flush()
    append_audit(session, actor=user.username, action="support.hah.kit.return",
                 entity=f"hah-kit/{kit_id}",
                 payload={"patient": row.patient_ref,
                          "returned_contents": row.returned_contents})
    session.commit()
    return _kit_out(row)


# ---------------------------------------------------------------------------
# BD-12 — Patient transport dispatch (on the Phase 12 TransportRequest)
# ---------------------------------------------------------------------------

_TRANSPORT_FLOW = {"requested": {"assigned", "cancelled"},
                   "assigned": {"in-transit", "cancelled"},
                   "in-transit": {"completed", "cancelled"},
                   "completed": set(), "cancelled": set()}


def _transport_out(row: TransportRequest, detail: SupportTransportDetail | None) -> dict:
    return {"id": row.request_id, "request_id": row.request_id,
            "patient_ref": row.patient_ref,
            "from_location": row.from_location, "to_location": row.to_location,
            "mode": detail.mode if detail else None,
            "priority": row.priority, "reason": row.reason,
            "status": row.status, "state": row.status,
            "dispatcher": detail.dispatcher if detail else row.transporter,
            "transporter": row.transporter,
            "requested_by": row.requested_by,
            "requested_at": _iso(detail.requested_at) if detail
            else _iso(row.created_at),
            "pickup_at": _iso(detail.pickup_at) if detail else None,
            "dropoff_at": _iso(detail.dropoff_at) if detail else None,
            "created_at": _iso(row.created_at)}


def _transport_rows(session: Session, request_id: str):
    row = session.scalars(select(TransportRequest).where(
        TransportRequest.request_id == request_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Transport request '{request_id}' not found")
    detail = session.scalars(select(SupportTransportDetail).where(
        SupportTransportDetail.request_id == request_id)).first()
    return row, detail


@router.post("/transport", status_code=201)
@router.post("/transport/requests", status_code=201)
async def create_transport(request: Request, session: Session = Depends(get_db),
                           user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    cfg = _transport_cfg(session)
    patient_ref = _patient_ref(session, payload)
    mode = _opt_str(payload, "mode") or "wheelchair-van"
    if mode not in _codes(cfg.get("modes")):
        raise HTTPException(status_code=422,
                            detail=f"Unknown transport mode '{mode}': not in "
                                   "the published transport-ops config")
    priority = _opt_str(payload, "priority") or "routine"
    if priority not in _codes(cfg.get("priorities")):
        raise HTTPException(status_code=422,
                            detail=f"Unknown transport priority '{priority}': "
                                   "not in the published transport-ops config")
    dispatcher = None
    raw_dispatcher = _opt_str(payload, "dispatcher", "assigned_to", "transporter")
    if raw_dispatcher:
        staff = _require_staff(session, raw_dispatcher, "dispatcher")
        dispatcher = staff.username or staff.staff_id
    requested_at = _now()
    raw_req = _opt_str(payload, "requested_at", "at", "created_at")
    if raw_req:
        requested_at = _parse_dt(raw_req, "requested_at")
    row = TransportRequest(
        request_id=str(uuid.uuid4()), patient_ref=patient_ref,
        from_location=_require_str(payload, "from_location", "from"),
        to_location=_require_str(payload, "to_location", "to"),
        priority=priority, reason=payload.get("reason"),
        status="assigned" if dispatcher else "requested",
        transporter=dispatcher, requested_by=user.username,
        created_at=requested_at)
    session.add(row)
    detail = SupportTransportDetail(request_id=row.request_id, mode=mode,
                                    dispatcher=dispatcher,
                                    requested_at=requested_at)
    session.add(detail)
    session.flush()
    append_audit(session, actor=user.username, action="support.transport.create",
                 entity=f"transport/{row.request_id}",
                 payload={"patient": patient_ref, "mode": mode,
                          "priority": priority, "dispatcher": dispatcher,
                          "from": row.from_location, "to": row.to_location})
    session.commit()
    return _transport_out(row, detail)


@router.get("/transport/metrics")
def transport_metrics(session: Session = Depends(get_db),
                      user: User = Depends(require_capability(READ))):
    """Timing metrics from the recorded instants: request->pickup
    and pickup->dropoff minutes (averages + counts). Exact arithmetic
    over the stored timestamps — no model, no estimate."""
    details = session.scalars(select(SupportTransportDetail)).all()
    req_pickup, pickup_drop = [], []
    for d in details:
        if d.pickup_at:
            req_pickup.append((_aware(d.pickup_at) - _aware(d.requested_at))
                              .total_seconds() / 60.0)
            if d.dropoff_at:
                pickup_drop.append((_aware(d.dropoff_at) - _aware(d.pickup_at))
                                   .total_seconds() / 60.0)

    def _stat(values: list[float]) -> dict:
        avg = round(sum(values) / len(values), 3) if values else None
        return {"count": len(values), "average_minutes": avg, "avg_minutes": avg,
                "total_minutes": round(sum(values), 3) if values else 0.0}

    total = session.scalars(select(TransportRequest)).all()
    return {"request_to_pickup": _stat(req_pickup),
            "pickup_to_dropoff": _stat(pickup_drop),
            "total_requests": len(total),
            "completed": sum(1 for r in total if r.status == "completed"),
            "basis": ("exact arithmetic over the recorded requested_at / "
                      "pickup_at / dropoff_at instants on each dispatch")}


@router.get("/transport")
@router.get("/transport/requests")
def list_transport(status: str | None = Query(default=None),
                   state: str | None = Query(default=None),
                   mode: str | None = Query(default=None),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability(READ))):
    stmt = select(TransportRequest).order_by(TransportRequest.id.asc())
    wanted = status or state
    if wanted:
        stmt = stmt.where(TransportRequest.status == wanted)
    rows = session.scalars(stmt).all()
    items = []
    for row in rows:
        detail = session.scalars(select(SupportTransportDetail).where(
            SupportTransportDetail.request_id == row.request_id)).first()
        if mode and (detail is None or detail.mode != mode):
            continue
        items.append(_transport_out(row, detail))
    return {"items": items, "total": len(items)}


@router.get("/transport/{request_id}")
def get_transport(request_id: str, session: Session = Depends(get_db),
                  user: User = Depends(require_capability(READ))):
    row, detail = _transport_rows(session, request_id)
    return _transport_out(row, detail)


@router.post("/transport/{request_id}/transition")
async def transport_transition(request_id: str, request: Request,
                               session: Session = Depends(get_db),
                               user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    target = _require_str(payload, "to_status", "to_state", "status", "state")
    row, detail = _transport_rows(session, request_id)
    if target not in _TRANSPORT_FLOW.get(row.status, set()):
        raise HTTPException(status_code=409,
                            detail=f"Illegal transport transition "
                                   f"'{row.status}' -> '{target}'")
    if detail is None:  # a Phase 12-created request being dispatched here
        detail = SupportTransportDetail(
            request_id=request_id, mode=_opt_str(payload, "mode") or "wheelchair-van",
            requested_at=_aware(row.created_at) or _now())
        session.add(detail)
    raw_dispatcher = _opt_str(payload, "dispatcher", "assigned_to",
                              "transporter", "assignee")
    if raw_dispatcher:
        staff = _require_staff(session, raw_dispatcher, "dispatcher")
        detail.dispatcher = staff.username or staff.staff_id
        row.transporter = detail.dispatcher
    at = None
    raw_at = _opt_str(payload, "at", "timestamp")
    if raw_at:
        at = _parse_dt(raw_at, "at")
    if target == "assigned" and not detail.dispatcher:
        raise HTTPException(status_code=422,
                            detail="Assigning a transport request requires a "
                                   "'dispatcher' validated against the staff directory")
    if target == "in-transit":
        detail.pickup_at = (_parse_dt(payload["pickup_at"], "pickup_at")
                            if payload.get("pickup_at") else (at or _now()))
    if target == "completed":
        detail.dropoff_at = (_parse_dt(payload["dropoff_at"], "dropoff_at")
                             if payload.get("dropoff_at") else (at or _now()))
    row.status = target
    session.flush()
    append_audit(session, actor=user.username,
                 action="support.transport.transition",
                 entity=f"transport/{request_id}",
                 payload={"to_status": target, "dispatcher": detail.dispatcher,
                          "pickup_at": _iso(detail.pickup_at),
                          "dropoff_at": _iso(detail.dropoff_at)})
    session.commit()
    return _transport_out(row, detail)


# ---------------------------------------------------------------------------
# BD-13 — Command centre as a staffed unit
# ---------------------------------------------------------------------------

def _desk(cfg: dict, code: str) -> dict | None:
    for entry in cfg.get("desks") or []:
        if isinstance(entry, dict) and entry.get("code") == code:
            return entry
    return None


@router.get("/command/desks")
def list_desks(session: Session = Depends(get_db),
               user: User = Depends(require_capability(READ))):
    cfg = _desks_cfg(session)
    return {"items": cfg.get("desks") or [], "total": len(cfg.get("desks") or []),
            "triage_authority_role": cfg.get("triageAuthorityRole")}


def _staffing_out(row: CommandStaffing) -> dict:
    return {"id": row.staffing_id, "staffing_id": row.staffing_id,
            "desk": row.desk, "staff_id": row.staff_id,
            "staff_username": row.staff_username, "staff": row.staff_username,
            "staff_name": row.staff_name,
            "starts_at": _iso(row.starts_at), "ends_at": _iso(row.ends_at),
            "shift_start": _iso(row.starts_at), "shift_end": _iso(row.ends_at),
            "created_by": row.created_by}


@router.post("/command/staffing", status_code=201)
async def create_staffing(request: Request, session: Session = Depends(get_db),
                          user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    cfg = _desks_cfg(session)
    desk_code = _require_str(payload, "desk")
    if _desk(cfg, desk_code) is None:
        raise HTTPException(status_code=422,
                            detail=f"Unknown command desk '{desk_code}': not in "
                                   "the published command-desks config")
    staff_value = _opt_str(payload, "staff", "staff_id", "staff_username",
                           "username")
    if not staff_value:
        raise HTTPException(status_code=422, detail="'staff' is required")
    staff = _require_staff(session, staff_value, "staff")
    starts_raw = _opt_str(payload, "starts_at", "start", "shift_start",
                          "shift_starts_at")
    ends_raw = _opt_str(payload, "ends_at", "end", "shift_end", "shift_ends_at")
    if not starts_raw or not ends_raw:
        raise HTTPException(status_code=422,
                            detail="A shift window is required ('starts_at' / 'ends_at')")
    starts_at = _parse_dt(starts_raw, "starts_at")
    ends_at = _parse_dt(ends_raw, "ends_at")
    if ends_at <= starts_at:
        raise HTTPException(status_code=422, detail="'ends_at' must be after 'starts_at'")
    overlap = session.scalars(select(CommandStaffing).where(
        CommandStaffing.desk == desk_code,
        CommandStaffing.staff_id == staff.staff_id,
        CommandStaffing.starts_at < ends_at,
        CommandStaffing.ends_at > starts_at)).first()
    if overlap is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{staff.full_name} already staffs desk '{desk_code}' over "
                   f"part of that window ({overlap.staffing_id})")
    row = CommandStaffing(staffing_id=f"cmds-{uuid.uuid4().hex[:10]}",
                          desk=desk_code, staff_id=staff.staff_id,
                          staff_username=staff.username,
                          staff_name=staff.full_name,
                          starts_at=starts_at, ends_at=ends_at,
                          created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="support.command.staffing",
                 entity=f"command-desk/{desk_code}",
                 payload={"staffing_id": row.staffing_id,
                          "staff_id": staff.staff_id,
                          "starts_at": starts_at.isoformat(),
                          "ends_at": ends_at.isoformat()})
    session.commit()
    return _staffing_out(row)


@router.get("/command/staffing")
def list_staffing(desk: str | None = Query(default=None),
                  staff_id: str | None = Query(default=None),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability(READ))):
    stmt = select(CommandStaffing).order_by(CommandStaffing.starts_at.asc())
    if desk:
        stmt = stmt.where(CommandStaffing.desk == desk)
    if staff_id:
        stmt = stmt.where(CommandStaffing.staff_id == staff_id)
    rows = session.scalars(stmt).all()
    items = [_staffing_out(r) for r in rows]
    return {"items": items, "total": len(items)}


def _huddle_out(row: CommandHuddle) -> dict:
    return {"id": row.huddle_id, "huddle_id": row.huddle_id, "desk": row.desk,
            "census": row.census, "boarding": row.boarding,
            "expected_discharges": row.expected_discharges,
            "expected_discharge_count": row.expected_discharges,
            "decisions": row.decisions or [], "chaired_by": row.chaired_by,
            "note": row.note, "held_at": _iso(row.held_at),
            "created_by": row.created_by, "created_at": _iso(row.created_at),
            "artefact": ("This structured huddle record is the capacity "
                         "huddle meeting artefact.")}


def _huddle_number(payload: dict, *names: str) -> int:
    for name in names:
        value = payload.get(name)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HTTPException(status_code=422,
                                    detail=f"'{name}' must be a non-negative integer")
            return value
    return 0


@router.post("/command/huddles", status_code=201)
async def create_huddle(request: Request, session: Session = Depends(get_db),
                        user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    cfg = _desks_cfg(session)
    desk_code = _opt_str(payload, "desk")
    if desk_code is not None and _desk(cfg, desk_code) is None:
        raise HTTPException(status_code=422,
                            detail=f"Unknown command desk '{desk_code}'")
    decisions = payload.get("decisions")
    if decisions is None:
        decisions = []
    if not isinstance(decisions, list):
        raise HTTPException(status_code=422, detail="'decisions' must be a list")
    row = CommandHuddle(
        huddle_id=f"cmdh-{uuid.uuid4().hex[:10]}", desk=desk_code,
        census=_huddle_number(payload, "census", "census_count"),
        boarding=_huddle_number(payload, "boarding", "boarding_count"),
        expected_discharges=_huddle_number(
            payload, "expected_discharges", "expected_discharge_count",
            "expectedDischarges"),
        decisions=decisions,
        chaired_by=_require_str(payload, "chaired_by", "chairedBy", "chair"),
        note=payload.get("note") if isinstance(payload.get("note"), str) else None,
        held_at=_parse_dt(payload["at"], "at") if payload.get("at") else _now(),
        created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="support.command.huddle",
                 entity=f"command-huddle/{row.huddle_id}",
                 payload={"desk": desk_code, "census": row.census,
                          "boarding": row.boarding,
                          "expected_discharges": row.expected_discharges,
                          "decisions": len(decisions),
                          "chaired_by": row.chaired_by})
    session.commit()
    return _huddle_out(row)


@router.get("/command/huddles")
def list_huddles(desk: str | None = Query(default=None),
                 session: Session = Depends(get_db),
                 user: User = Depends(require_capability(READ))):
    stmt = select(CommandHuddle).order_by(CommandHuddle.id.desc())
    if desk:
        stmt = stmt.where(CommandHuddle.desk == desk)
    rows = session.scalars(stmt).all()
    items = [_huddle_out(r) for r in rows]
    return {"items": items, "total": len(items)}


@router.get("/command/huddles/{huddle_id}")
def get_huddle(huddle_id: str, session: Session = Depends(get_db),
               user: User = Depends(require_capability(READ))):
    row = session.scalars(select(CommandHuddle).where(
        CommandHuddle.huddle_id == huddle_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Command huddle '{huddle_id}' not found")
    return _huddle_out(row)


def _triage_out(row: TriageDecision) -> dict:
    return {"id": row.decision_id, "decision_id": row.decision_id,
            "patient_ref": row.patient_ref, "decision": row.decision,
            "decided_by": row.decided_by,
            "authority_role": row.authority_role,
            "triage_authority_role": row.authority_role,
            "authority_basis": row.authority_basis, "reason": row.reason,
            "created_by": row.created_by, "created_at": _iso(row.created_at)}


@router.post("/command/triage-decisions", status_code=201)
async def create_triage_decision(request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_capability(WORK))):
    """ICU triage decision. Only the published triage-authority role
    (command-desks `triageAuthorityRole`, the HTRIP-equivalent —
    matched against the decider's staff-directory job role) or an
    admin may decide; anyone else is a 403. The authority the
    decision was made under is recorded on the row AND in the audit."""
    payload = await _json_body(request)
    cfg = _desks_cfg(session)
    patient_ref = _patient_ref(session, payload)
    decision = _require_str(payload, "decision").replace("_", "-")
    allowed = cfg.get("triageDecisions") or ["admit-icu", "defer", "decline"]
    if decision not in allowed:
        raise HTTPException(status_code=422,
                            detail=f"Unknown triage decision '{decision}': "
                                   f"must be one of {allowed}")
    decided_by = _opt_str(payload, "decided_by", "decidedBy") or user.username
    authority_role = cfg.get("triageAuthorityRole") or "physician"
    staff = _staff(session, decided_by)
    decider_user = session.scalars(select(User).where(
        User.username == decided_by)).first()
    basis = None
    recorded_authority = authority_role
    if staff is not None and staff.job_role == authority_role:
        basis = (f"decided_by staff job_role '{staff.job_role}' equals the "
                 f"published triageAuthorityRole")
    elif decider_user is not None and decider_user.role.value == "admin":
        basis = "decided_by is an admin"
        recorded_authority = "admin"
    elif decided_by == user.username and user.role.value == "admin":
        basis = "caller is an admin"
        recorded_authority = "admin"
    if basis is None:
        raise HTTPException(
            status_code=403,
            detail=f"Triage decisions require the published triage-authority "
                   f"role '{authority_role}' (HTRIP-equivalent) or an admin; "
                   f"'{decided_by}' holds neither")
    row = TriageDecision(decision_id=f"cmdt-{uuid.uuid4().hex[:10]}",
                         patient_ref=patient_ref, decision=decision,
                         decided_by=decided_by,
                         authority_role=recorded_authority,
                         authority_basis=basis,
                         reason=payload.get("reason")
                         if isinstance(payload.get("reason"), str) else None,
                         created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="support.command.triage-decision",
                 entity=patient_ref,
                 payload={"decision_id": row.decision_id, "decision": decision,
                          "decided_by": decided_by,
                          "authority_role": recorded_authority,
                          "authority_basis": basis})
    session.commit()
    return _triage_out(row)


@router.get("/command/triage-decisions")
def list_triage_decisions(patient_ref: str | None = Query(default=None),
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability(READ))):
    stmt = select(TriageDecision).order_by(TriageDecision.id.asc())
    if patient_ref:
        stmt = stmt.where(TriageDecision.patient_ref == patient_ref)
    rows = session.scalars(stmt).all()
    items = [_triage_out(r) for r in rows]
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# BD-14 — Access-line call centre
# ---------------------------------------------------------------------------

def _call_out(row: AccessCall) -> dict:
    out = {"id": row.call_id, "call_id": row.call_id,
           "caller_name": row.caller_name, "caller_org": row.caller_org,
           "caller_organization": row.caller_org,
           "referral_source": row.referral_source, "source": row.referral_source,
           "patient_ref": row.patient_ref, "patient_name": row.patient_name,
           "patient_summary": row.patient_summary,
           "summary": row.patient_summary, "disposition": row.disposition,
           "transfer_request_id": row.transfer_request_id,
           "call_at": _iso(row.call_at), "created_by": row.created_by,
           "created_at": _iso(row.created_at)}
    if row.transfer_request_id:
        out["transfer_linkage"] = {
            "model": "queued-intake-reference",
            "transfer_request_id": row.transfer_request_id,
            "transfer_centre": "Phase 12 transfer centre (app.continuum)",
            "transfer_centre_endpoint": "/continuum/command/transfers",
            "note": ("This access-line record is a queued intake record "
                     "referencing the Phase 12 TransferRequest "
                     f"{row.transfer_request_id}; acceptance/decline is "
                     "decided in the transfer centre, not here — its "
                     "machinery is not forked.")}
    return out


@router.post("/access-line/calls", status_code=201)
async def create_call(request: Request, session: Session = Depends(get_db),
                      user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    cfg = _access_cfg(session)
    caller_name = _require_str(payload, "caller_name", "caller")
    source = _require_str(payload, "referral_source", "source")
    source_codes = _codes(cfg.get("referralSources")) | set(cfg.get("sources") or [])
    if source not in source_codes:
        raise HTTPException(status_code=422,
                            detail=f"Unknown referral source '{source}': not "
                                   "in the published access-line config")
    summary = _require_str(payload, "patient_summary", "summary")
    disposition = _require_str(payload, "disposition")
    if disposition not in (cfg.get("dispositions") or []):
        raise HTTPException(status_code=422,
                            detail=f"Unknown disposition '{disposition}': not "
                                   "in the published access-line config")
    patient_ref = None
    if _opt_str(payload, "patient_ref", "patient", "patient_id"):
        patient_ref = _patient_ref(session, payload)
    call_at = _now()
    raw_at = _opt_str(payload, "call_at", "at", "called_at")
    if raw_at:
        call_at = _parse_dt(raw_at, "call_at")
    row = AccessCall(call_id=f"acc-{uuid.uuid4().hex[:10]}",
                     caller_name=caller_name,
                     caller_org=_opt_str(payload, "caller_org", "caller_organization",
                                         "organization", "org"),
                     referral_source=source, patient_ref=patient_ref,
                     patient_name=_opt_str(payload, "patient_name"),
                     patient_summary=summary, disposition=disposition,
                     call_at=call_at, created_by=user.username)
    session.add(row)
    session.flush()
    transfer_id = None
    if disposition == "transfer-requested":
        # A REAL Phase 12 transfer-centre record — the access line
        # queues the intake; the transfer centre decides it.
        transfer = TransferRequest(
            request_id=str(uuid.uuid4()),
            patient_name=row.patient_name or caller_name,
            referring_facility=row.caller_org or source,
            referring_clinician=caller_name,
            reason=summary,
            service_line=_opt_str(payload, "service_line"),
            urgency=_opt_str(payload, "urgency") or "routine",
            contact=_opt_str(payload, "contact") or row.caller_org,
            status="pending", created_by=user.username)
        session.add(transfer)
        session.flush()
        transfer_id = transfer.request_id
        row.transfer_request_id = transfer_id
    append_audit(session, actor=user.username, action="support.access-line.call",
                 entity=f"access-call/{row.call_id}",
                 payload={"referral_source": source, "disposition": disposition,
                          "transfer_request_id": transfer_id})
    session.commit()
    return _call_out(row)


@router.get("/access-line/calls")
def list_calls(disposition: str | None = Query(default=None),
               referral_source: str | None = Query(default=None),
               session: Session = Depends(get_db),
               user: User = Depends(require_capability(READ))):
    stmt = select(AccessCall).order_by(AccessCall.id.asc())
    if disposition:
        stmt = stmt.where(AccessCall.disposition == disposition)
    if referral_source:
        stmt = stmt.where(AccessCall.referral_source == referral_source)
    rows = session.scalars(stmt).all()
    items = [_call_out(r) for r in rows]
    return {"items": items, "total": len(items)}


@router.get("/access-line/transfers")
def list_access_transfers(session: Session = Depends(get_db),
                          user: User = Depends(require_capability(READ))):
    """Transfer-requested access-line calls, exposed as queued intake
    records referencing the Phase 12 transfer centre's own
    TransferRequest rows (decided there, never here)."""
    calls = session.scalars(select(AccessCall).where(
        AccessCall.disposition == "transfer-requested",
        AccessCall.transfer_request_id.is_not(None))
        .order_by(AccessCall.id.asc())).all()
    items = []
    for call in calls:
        transfer = session.scalars(select(TransferRequest).where(
            TransferRequest.request_id == call.transfer_request_id)).first()
        items.append({
            "call_id": call.call_id,
            "transfer_request_id": call.transfer_request_id,
            "transfer_ref": f"TransferRequest/{call.transfer_request_id}",
            "status": transfer.status if transfer else None,
            "patient_name": transfer.patient_name if transfer else call.patient_name,
            "referring_facility": transfer.referring_facility if transfer else None,
            "referral_source": call.referral_source,
            "patient_summary": call.patient_summary,
            "call_at": _iso(call.call_at),
            "linkage": {
                "model": "queued-intake-reference",
                "joins": ("Phase 12 transfer centre (app.continuum "
                          "TransferRequest), via transfer_request_id"),
                "transfer_centre_endpoint": "/continuum/command/transfers",
                "note": ("Queued intake record referencing the transfer "
                         "centre's record; the transfer centre's own "
                         "machinery decides acceptance — nothing is forked "
                         "or duplicated here."),
            },
        })
    return {"items": items, "total": len(items)}


@router.get("/access-line/volume")
def access_volume(date_from: str | None = Query(default=None),
                  date_to: str | None = Query(default=None),
                  from_: str | None = Query(default=None, alias="from"),
                  to: str | None = Query(default=None),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability(READ))):
    """Call volume over a date range: counts by referral source and
    by disposition. Bounds are inclusive; a date-only bound covers
    its whole UTC day."""
    start_raw = date_from or from_
    end_raw = date_to or to
    start = _parse_dt(start_raw, "date_from") if start_raw else None
    end = None
    if end_raw:
        end = _parse_dt(end_raw, "date_to")
        if len(end_raw.strip()) == 10:
            end = end + timedelta(days=1) - timedelta(microseconds=1)
    rows = session.scalars(select(AccessCall).order_by(AccessCall.id.asc())).all()
    by_source: dict[str, int] = {}
    by_disposition: dict[str, int] = {}
    total = 0
    for row in rows:
        at = _aware(row.call_at)
        if start and at < start:
            continue
        if end and at > end:
            continue
        total += 1
        by_source[row.referral_source] = by_source.get(row.referral_source, 0) + 1
        by_disposition[row.disposition] = by_disposition.get(row.disposition, 0) + 1
    return {"total": total, "by_source": by_source,
            "by_disposition": by_disposition,
            "sources": [{"source": k, "count": v}
                        for k, v in sorted(by_source.items())],
            "dispositions": [{"disposition": k, "count": v}
                             for k, v in sorted(by_disposition.items())],
            "date_from": start.isoformat() if start else None,
            "date_to": end.isoformat() if end else None}
