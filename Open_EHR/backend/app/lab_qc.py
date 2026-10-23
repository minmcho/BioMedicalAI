"""Lab Depth W4 (LAB-D8) — QC & calibration completeness.

Builds ON the Phase 2/6B QC spine in app.lab without weakening it:
the report-level gate (app.lab.qc_blockers, read at verify AND at
release) is untouched. This module adds the instrument-path layer
the reality check found missing (report §2 #3 / §3.3 LAB-D8):

- **Full Westgard set.** The rule evaluation itself lives in
  app.lab._westgard_flags (W4-owned region) and now evaluates
  1-2s / 1-3s / 2-2s / R-4s / 4-1s / 10x, each flag naming the rule
  that fired. This module keys the underlying series (below).
- **QC keyed to instrument + reagent lot + shift.** LabQcRun gains
  nullable instrument_id / reagent_lot_key / control_lot_id / shift
  columns (existing databases: the inspector-guarded
  ensure_lab_qc_columns hook below — Run-16 rule). SHIFT CHOICE:
  the shift is DERIVED from governed shift windows in the published
  terminology/lab-reagent-qc config applied to the run's UTC
  timestamp (no free-text shift is ever accepted from a caller).
- **QC failure locks the instrument path.** While the latest QC run
  for an instrument + analyte is failed (range failure OR a Westgard
  rejection flag), analyser ingest AND manual result entry for that
  instrument/analyte refuse by name (409, zero writes) until a
  passing QC posts — enforced in app.lab._create_results, which both
  paths share. This is IN ADDITION TO the report-level qc_blockers.
- **Calibration with one truth.** Calibration RECORDS (measured
  values, acceptance criteria, due dates) and the calibration-
  verification (AMR) workflow live here, but the DUE-DATE TRUTH
  lives in the Phase 14 device registry (app.devices): when the
  instrument is a registered device (device_key == instrument_id),
  recording a calibration WRITES THROUGH to the registry
  (last_calibrated_at + calibration_due from the published device-
  type interval — the registry's own record_calibration rule), and
  every posture/lock read defers to devices.calibration_state.
  Only for a lab instrument with NO device-registry twin does the
  lab record's own due_date govern. There is never a second,
  divergent due date. Overdue or failed calibration locks the
  instrument path exactly like a QC failure.

Governed Studio surfaces (configure, don't fork):
- terminology/lab-reagent-qc (this module's seed): verification
  criteria per analyte (LAB-D9), control-lot establishment runs,
  shift windows, calibration/AMR parameters. Seeded values are
  labelled GOVERNED DEFAULTS for the laboratory director to set —
  in particular there is NO universal ±10% acceptance standard;
  the seeded bias figures are placeholders the director replaces.
- terminology/lab-catalog qc.programme (existing): Westgard rule
  switches, incl. the new rejectR_4s / reject4_1s / reject10x keys.
- terminology/devices (existing): calibration intervals per device
  type — the registry truth this module defers to.
"""
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (Boolean, DateTime, Float, Integer, JSON, String,
                        Text, inspect, select, text)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import devices as devices_mod
from .audit import append_audit
from .configstore import published_config
from .db import Base
from .models import (ConfigStatus, Device, LabInstrument,
                     LabInstrumentLog, LabQcRun, StudioConfig, User,
                     utcnow)
from .security import get_db, require_capability

router = APIRouter(prefix="/lab", tags=["lab_qc"])

REAGENT_QC_KEY = "lab-reagent-qc"
REAGENT_QC_KIND = "terminology"

#: Westgard flags that REJECT a run for instrument-path purposes
#: (1-2s is a warning only, exactly as in the classic rule set).
WESTGARD_REJECTION_FLAGS = ("1-3s", "2-2s", "R-4s", "4-1s", "10x")


# ---------------------------------------------------------------------------
# Own table: calibration records (the device registry stays the
# due-date truth — see the module docstring)
# ---------------------------------------------------------------------------

class LabCalibrationRecord(Base):
    """One calibration or calibration-verification (AMR) record for
    a lab instrument: the measured values per level, the acceptance
    criteria applied, the server-computed verdict, and the due date
    that resulted. `registry_due` snapshots what the device registry
    computed at write-through time so the record stays auditable
    even though the registry remains the live truth."""
    __tablename__ = "lab_calibration_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(80), nullable=False,
                                                index=True)
    device_key: Mapped[str | None] = mapped_column(String(120),
                                                   nullable=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False,
                                      default="calibration")
    performed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)
    performed_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                              default="")
    levels: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    criteria: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    level_results: Mapped[list] = mapped_column(JSON, nullable=False,
                                                default=list)
    passed: Mapped[bool] = mapped_column(Boolean, default=False,
                                         nullable=False)
    failure_reasons: Mapped[list] = mapped_column(JSON, nullable=False,
                                                  default=list)
    reportable_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    reportable_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    spans_range: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    due_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    registry_due: Mapped[str | None] = mapped_column(String(40), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


# ---------------------------------------------------------------------------
# Run-16 hook: LabQcRun keying columns on pre-W4 databases
# ---------------------------------------------------------------------------

LAB_QC_ADD_COLUMNS = (
    ("lab_qc_runs", (
        ("instrument_id", "VARCHAR(80)"),
        ("reagent_lot_key", "VARCHAR(240)"),
        ("control_lot_id", "INTEGER"),
        ("shift", "VARCHAR(40)"),
    )),
)


def ensure_lab_qc_columns(engine) -> None:
    """Additive LabQcRun keying columns for pre-existing databases
    (the inventory.ensure_inventory_columns precedent: create_all
    never alters an existing table). Inspector-guarded; a no-op on
    fresh databases. Wired from app.main.create_app — see the W4
    INTEGRATION NOTE."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in LAB_QC_ADD_COLUMNS:
            if table not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table)}
            for column, ddl in columns:
                if column not in present:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


# ---------------------------------------------------------------------------
# Governed config: terminology/lab-reagent-qc
# ---------------------------------------------------------------------------

def default_lab_reagent_qc_payload() -> dict:
    """Seed for the governed reagent/QC config. Every numeric
    acceptance figure below is a GOVERNED DEFAULT the laboratory
    director must set per analyte/method before the laboratory
    relies on it — labelled as such in the payload itself. There
    is deliberately NO claim that any figure (e.g. a 10% bias) is
    a universal standard: CAP/CLSI EP26-A put the clinically
    significant difference with the director, per analyte."""
    return {
        "name": "OpenCare laboratory reagent & QC governance (Lab Depth)",
        "concepts": [
            {"code": "day", "display": "Day shift (governed window)",
             "active": True},
            {"code": "night", "display": "Night shift (governed window)",
             "active": True},
        ],
        "verificationCriteria": {
            "label": ("GOVERNED DEFAULTS — set by the laboratory "
                      "director per analyte before reliance; NOT a "
                      "universal standard (no universal ±10% rule "
                      "exists; CLSI EP26-A pattern)."),
            "default": {"minSamples": 3, "maxBiasPct": 10.0,
                        "maxBiasAbs": None},
            "byAnalyte": {
                "2823-3": {"minSamples": 3, "maxBiasPct": 10.0,
                           "maxBiasAbs": None},
                "2345-7": {"minSamples": 3, "maxBiasPct": 10.0,
                           "maxBiasAbs": None},
            },
        },
        "controlLot": {
            "label": ("GOVERNED DEFAULT — runs required to establish "
                      "a new control lot's mean/SD before live use "
                      "(~20 runs over several days is the researched "
                      "practice; the director sets the exact count)."),
            "requiredRunsDefault": 20,
        },
        "shifts": [
            {"code": "day", "start": "07:00", "end": "19:00"},
            {"code": "night", "start": "19:00", "end": "07:00"},
        ],
        "calibration": {
            "label": ("GOVERNED DEFAULTS — AMR level count follows "
                      "CLIA §493.1255 (at least 3 levels spanning "
                      "the reportable range); bias figures are "
                      "director-set defaults, not a standard."),
            "amrMinLevels": 3,
            "amrMaxBiasPct": 10.0,
            "amrMaxBiasAbs": None,
            "defaultIntervalDays": 180,
            "dueSoonDays": 30,
        },
    }


def ensure_lab_reagent_qc_defaults(session: Session) -> None:
    """Seed terminology/lab-reagent-qc as published v1 (insert-only,
    the lab._seed_published pattern). Wired from app.main — see the
    W4 INTEGRATION NOTE."""
    existing = session.scalars(
        select(StudioConfig).where(
            StudioConfig.kind == REAGENT_QC_KIND,
            StudioConfig.key == REAGENT_QC_KEY)).first()
    if existing is not None:
        return
    row = StudioConfig(kind=REAGENT_QC_KIND, key=REAGENT_QC_KEY, version=1,
                       status=ConfigStatus.published,
                       payload=default_lab_reagent_qc_payload(),
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    for action in ("studio.config.create", "studio.config.publish"):
        append_audit(session, actor="system", action=action,
                     entity=f"config:{REAGENT_QC_KIND}/{REAGENT_QC_KEY}@v1",
                     payload={"kind": REAGENT_QC_KIND, "key": REAGENT_QC_KEY,
                              "version": 1, "seeded_default": True})
    session.commit()


def reagent_qc_config(session: Session) -> dict:
    """The effective reagent/QC governance config: the published
    Studio payload merged over this module's governed defaults, so
    an unpublished or partial config still yields complete, safe
    behaviour (and a site republishing one section keeps the rest)."""
    cfg = default_lab_reagent_qc_payload()
    published = published_config(session, REAGENT_QC_KIND, REAGENT_QC_KEY)
    if published is not None and isinstance(published.payload, dict):
        for key, value in published.payload.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                merged = dict(cfg[key])
                merged.update(value)
                cfg[key] = merged
            else:
                cfg[key] = value
    return cfg


def verification_criteria(session: Session, analyte_code: str) -> dict:
    """Director-configured lot-verification acceptance criteria for
    one analyte: byAnalyte entry merged over the governed default.
    {minSamples, maxBiasPct, maxBiasAbs, source} — a comparison
    passes when EVERY configured bound holds (pct and/or absolute)."""
    section = reagent_qc_config(session).get("verificationCriteria") or {}
    default = section.get("default") or {}
    per = (section.get("byAnalyte") or {}).get(analyte_code) or {}
    merged = dict(default)
    merged.update(per)
    source = "byAnalyte" if per else "default"
    min_samples = merged.get("minSamples")
    if not isinstance(min_samples, int) or isinstance(min_samples, bool) \
            or min_samples < 1:
        min_samples = 3
    return {"minSamples": min_samples,
            "maxBiasPct": merged.get("maxBiasPct"),
            "maxBiasAbs": merged.get("maxBiasAbs"),
            "source": source,
            "label": section.get("label")}


def shift_for_timestamp(cfg: dict, at: datetime) -> str:
    """The governed shift code for a UTC timestamp: the shift whose
    [start, end) window (which may wrap midnight) contains the
    timestamp's UTC time. 'unassigned' when no window matches —
    derived, never caller-asserted (the documented W4 choice)."""
    moment = at.astimezone(timezone.utc).time() if at.tzinfo else at.time()
    for shift in cfg.get("shifts") or []:
        if not isinstance(shift, dict) or not shift.get("code"):
            continue
        try:
            start = datetime.strptime(shift["start"], "%H:%M").time()
            end = datetime.strptime(shift["end"], "%H:%M").time()
        except (KeyError, TypeError, ValueError):
            continue
        if start <= end:
            if start <= moment < end:
                return str(shift["code"])
        elif moment >= start or moment < end:
            return str(shift["code"])
    return "unassigned"


# ---------------------------------------------------------------------------
# Instrument-path QC lock (in ADDITION TO app.lab.qc_blockers)
# ---------------------------------------------------------------------------

def run_is_rejected(run: LabQcRun) -> bool:
    """A QC run rejects its instrument path when the range verdict
    failed OR a Westgard REJECTION flag fired on it (1-2s alone is a
    warning and never locks)."""
    if not run.passed:
        return True
    flags = set(run.flags or [])
    return bool(flags & set(WESTGARD_REJECTION_FLAGS))


def latest_qc_for(session: Session, instrument_id: str,
                  analyte_code: str) -> LabQcRun | None:
    return session.scalars(
        select(LabQcRun).where(LabQcRun.instrument_id == instrument_id,
                               LabQcRun.analyte_code == analyte_code)
        .order_by(LabQcRun.id.desc())).first()


def instrument_path_blockers(session: Session, instrument_id: str | None,
                             analyte_codes) -> list[dict]:
    """Named blockers on the instrument path for one instrument:
    a failed latest QC per analyte, plus calibration lock (once, for
    the instrument). Empty instrument -> no instrument path to lock
    (the analyte-level report gate still applies downstream)."""
    if not instrument_id:
        return []
    blockers: list[dict] = []
    for code in sorted(set(analyte_codes)):
        latest = latest_qc_for(session, instrument_id, code)
        if latest is not None and run_is_rejected(latest):
            blockers.append({
                "instrument_id": instrument_id, "analyte_code": code,
                "reason": "qc-failed",
                "detail": (f"latest QC run {latest.id} for instrument "
                           f"'{instrument_id}' / analyte '{code}' "
                           f"failed (passed={latest.passed}, "
                           f"flags={list(latest.flags or [])}); a passing "
                           "QC run for this instrument + analyte is "
                           "required before results can be entered")})
    cal_reason = calibration_lock_reason(session, instrument_id)
    if cal_reason:
        blockers.append({"instrument_id": instrument_id,
                         "analyte_code": None,
                         "reason": "calibration", "detail": cal_reason})
    return blockers


def enforce_instrument_path(session: Session, instrument_id: str | None,
                            analyte_codes) -> None:
    """409 by name when the instrument path is locked. Called from
    app.lab._create_results BEFORE any result is written, so a
    refused entry/ingest leaves zero writes behind."""
    blockers = instrument_path_blockers(session, instrument_id, analyte_codes)
    if blockers:
        names = "; ".join(b["detail"] for b in blockers)
        raise HTTPException(
            status_code=409,
            detail=(f"Instrument path locked for '{instrument_id}': "
                    f"{names}"))


# ---------------------------------------------------------------------------
# Calibration: one truth (the device registry), lab records as evidence
# ---------------------------------------------------------------------------

def _device_for(session: Session, instrument_id: str) -> Device | None:
    return session.scalars(
        select(Device).where(Device.device_key == instrument_id)).first()


def _latest_calibration(session: Session, instrument_id: str,
                        kind: str | None = None) -> LabCalibrationRecord | None:
    stmt = select(LabCalibrationRecord).where(
        LabCalibrationRecord.instrument_id == instrument_id)
    if kind:
        stmt = stmt.where(LabCalibrationRecord.kind == kind)
    return session.scalars(stmt.order_by(LabCalibrationRecord.id.desc())).first()


def calibration_posture(session: Session, instrument_id: str) -> dict:
    """The ONE calibration posture for an instrument. When a device-
    registry twin exists, its devices.calibration_state IS the
    posture (source 'device-registry'); otherwise the latest passing
    lab calibration record's due_date governs (source 'lab-record').
    A failed latest calibration/AMR record reports 'failed' in both
    worlds — evidence of failure is never hidden by a due date."""
    cfg = reagent_qc_config(session)
    due_soon = (cfg.get("calibration") or {}).get("dueSoonDays", 30)
    latest_any = _latest_calibration(session, instrument_id)
    failed = latest_any is not None and not latest_any.passed
    device = _device_for(session, instrument_id)
    if device is not None:
        state = devices_mod.calibration_state(devices_mod.devices_config(session),
                                              device)
        status = "failed" if failed else state["status"]
        return {"instrument_id": instrument_id, "source": "device-registry",
                "status": status, "due": state["due"],
                "days_remaining": state["days_remaining"],
                "latest_record_id": latest_any.id if latest_any else None,
                "latest_passed": latest_any.passed if latest_any else None}
    latest_pass = session.scalars(
        select(LabCalibrationRecord).where(
            LabCalibrationRecord.instrument_id == instrument_id,
            LabCalibrationRecord.passed.is_(True))
        .order_by(LabCalibrationRecord.id.desc())).first()
    if latest_pass is None or not latest_pass.due_date:
        return {"instrument_id": instrument_id, "source": "lab-record",
                "status": "failed" if failed else "unknown",
                "due": None, "days_remaining": None,
                "latest_record_id": latest_any.id if latest_any else None,
                "latest_passed": latest_any.passed if latest_any else None}
    try:
        due = date.fromisoformat(latest_pass.due_date)
    except ValueError:
        return {"instrument_id": instrument_id, "source": "lab-record",
                "status": "unknown", "due": latest_pass.due_date,
                "days_remaining": None,
                "latest_record_id": latest_any.id if latest_any else None,
                "latest_passed": latest_any.passed if latest_any else None}
    days = (due - datetime.now(timezone.utc).date()).days
    status = "overdue" if days < 0 else ("due-soon" if days <= due_soon
                                         else "ok")
    if failed:
        status = "failed"
    return {"instrument_id": instrument_id, "source": "lab-record",
            "status": status, "due": latest_pass.due_date,
            "days_remaining": days,
            "latest_record_id": latest_any.id if latest_any else None,
            "latest_passed": latest_any.passed if latest_any else None}


def calibration_lock_reason(session: Session,
                            instrument_id: str | None) -> str | None:
    """Why calibration locks this instrument's path, or None: the
    latest calibration/AMR record failed, or calibration is overdue
    under the one truth (registry first, lab record otherwise)."""
    if not instrument_id:
        return None
    posture = calibration_posture(session, instrument_id)
    if posture["status"] == "failed":
        return (f"instrument '{instrument_id}' has a FAILED latest "
                f"calibration record (#{posture['latest_record_id']}); "
                "a passing calibration is required before results "
                "can be entered")
    if posture["status"] == "overdue":
        return (f"instrument '{instrument_id}' calibration is overdue "
                f"(due {posture['due']}, source {posture['source']}); "
                "a passing calibration is required before results "
                "can be entered")
    return None


# ---------------------------------------------------------------------------
# HTTP helpers + record computation
# ---------------------------------------------------------------------------

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


def _require_instrument(session: Session, instrument_id) -> str:
    if not isinstance(instrument_id, str) or not instrument_id.strip():
        raise HTTPException(status_code=422,
                            detail="'instrument_id' is required")
    instrument_id = instrument_id.strip()
    known = session.scalars(
        select(LabInstrument).where(
            LabInstrument.instrument_id == instrument_id)).first()
    if known is None and _device_for(session, instrument_id) is None:
        raise HTTPException(
            status_code=422,
            detail=(f"Instrument '{instrument_id}' is not in the lab "
                    "instrument registry or the device registry"))
    return instrument_id


def _parse_levels(payload: dict) -> list[dict]:
    levels = payload.get("levels")
    if not isinstance(levels, list) or not levels:
        raise HTTPException(
            status_code=422,
            detail="'levels' must be a non-empty list of "
                   "{label, nominal, measured} measured values")
    parsed = []
    for i, entry in enumerate(levels):
        if not isinstance(entry, dict):
            raise HTTPException(status_code=422,
                                detail=f"levels[{i}] must be an object")
        nominal, measured = entry.get("nominal"), entry.get("measured")
        for field, value in (("nominal", nominal), ("measured", measured)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HTTPException(
                    status_code=422,
                    detail=f"levels[{i}].'{field}' must be a number "
                           "(a measured value, not an attestation)")
        parsed.append({"label": str(entry.get("label") or f"L{i + 1}"),
                       "nominal": float(nominal),
                       "measured": float(measured)})
    return parsed


def _criteria_for(session: Session, payload: dict) -> dict:
    cal_cfg = (reagent_qc_config(session).get("calibration") or {})
    raw = payload.get("criteria") if isinstance(payload.get("criteria"), dict) \
        else {}
    return {
        "maxBiasPct": raw.get("maxBiasPct", cal_cfg.get("amrMaxBiasPct")),
        "maxBiasAbs": raw.get("maxBiasAbs", cal_cfg.get("amrMaxBiasAbs")),
        "source": "request" if raw else "governed-default",
        "label": cal_cfg.get("label"),
    }


def _evaluate_levels(levels: list[dict], criteria: dict):
    results, reasons = [], []
    for level in levels:
        nominal, measured = level["nominal"], level["measured"]
        bias_abs = measured - nominal
        bias_pct = (bias_abs / nominal * 100.0) if nominal else None
        within = True
        if criteria.get("maxBiasPct") is not None and bias_pct is not None \
                and abs(bias_pct) > float(criteria["maxBiasPct"]):
            within = False
        if criteria.get("maxBiasAbs") is not None \
                and abs(bias_abs) > float(criteria["maxBiasAbs"]):
            within = False
        if not within:
            reasons.append(
                f"level '{level['label']}': bias {bias_abs:+.4g} "
                f"({bias_pct:+.2f}% vs nominal {nominal:g}) exceeds the "
                "acceptance criteria")
        results.append({**level, "bias_abs": bias_abs,
                        "bias_pct": bias_pct, "within": within})
    return results, reasons


def _record_out(record: LabCalibrationRecord) -> dict:
    return {
        "id": record.id, "instrument_id": record.instrument_id,
        "device_key": record.device_key, "kind": record.kind,
        "performed_at": record.performed_at.isoformat()
        if record.performed_at else None,
        "performed_by": record.performed_by,
        "levels": list(record.levels or []),
        "criteria": dict(record.criteria or {}),
        "level_results": list(record.level_results or []),
        "passed": record.passed,
        "failure_reasons": list(record.failure_reasons or []),
        "reportable_low": record.reportable_low,
        "reportable_high": record.reportable_high,
        "spans_range": record.spans_range,
        "due_date": record.due_date, "registry_due": record.registry_due,
        "note": record.note,
        "created_at": record.created_at.isoformat()
        if record.created_at else None,
    }


def _write_through_registry(session: Session, instrument_id: str,
                            performed: datetime,
                            record: LabCalibrationRecord) -> None:
    """One calibration truth: a passing calibration for an instrument
    with a device-registry twin updates the REGISTRY exactly as
    devices.record_calibration does (last_calibrated_at + due from
    the published type interval). No interval configured -> the
    registry is left untouched and the lab record's governed-default
    interval due_date is the fallback the posture reads."""
    device = _device_for(session, instrument_id)
    if device is None:
        return
    record.device_key = device.device_key
    cfg = devices_mod.devices_config(session)
    type_cfg = devices_mod._type_cfg(cfg, device.device_type) or {}
    interval = type_cfg.get("calibrationIntervalDays")
    if not interval:
        return
    device.last_calibrated_at = performed
    device.calibration_due = performed + timedelta(days=int(interval))
    record.registry_due = device.calibration_due.isoformat()
    record.due_date = device.calibration_due.date().isoformat()
    session.flush()


def _mirror_instrument_log(session: Session, user: User,
                           record: LabCalibrationRecord) -> None:
    """Mirror the event onto the lab instrument log (the Phase 6B
    surface) when the instrument is a lab-registry instrument."""
    known = session.scalars(
        select(LabInstrument).where(
            LabInstrument.instrument_id == record.instrument_id)).first()
    if known is None:
        return
    session.add(LabInstrumentLog(
        instrument_id=record.instrument_id, kind="calibration",
        note=(f"{record.kind} #{record.id}: "
              f"{'PASS' if record.passed else 'FAIL'}"
              + (f" — {'; '.join(record.failure_reasons)}"
                 if record.failure_reasons else "")),
        performed_by=user.username))
    session.flush()


# ---------------------------------------------------------------------------
# Endpoints: QC status by instrument / lot / shift + instrument path
# ---------------------------------------------------------------------------

@router.get("/qc/instrument-status")
def qc_instrument_status(instrument_id: str = Query(...),
                         analyte_code: str | None = Query(default=None),
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("lab.read"))):
    """QC status keyed by instrument (+ reagent lot + shift): the
    latest run per analyte for this instrument, its lot/shift keys,
    whether the instrument path is locked for it, and the one-truth
    calibration posture riding alongside."""
    from . import lab as lab_mod  # lazy: app.lab imports this module lazily too
    stmt = select(LabQcRun).where(LabQcRun.instrument_id == instrument_id)
    if analyte_code:
        stmt = stmt.where(LabQcRun.analyte_code == analyte_code)
    rows = session.scalars(stmt.order_by(LabQcRun.id.desc())).all()
    latest_by_analyte: dict[str, LabQcRun] = {}
    for run in rows:
        latest_by_analyte.setdefault(run.analyte_code, run)
    items = []
    for code, run in sorted(latest_by_analyte.items()):
        items.append({
            "analyte_code": code, "latest": lab_mod._qc_out(run),
            "reagent_lot_key": run.reagent_lot_key,
            "control_lot_id": run.control_lot_id,
            "shift": run.shift, "rejected": run_is_rejected(run),
            "path_locked": run_is_rejected(run),
        })
    blockers = instrument_path_blockers(
        session, instrument_id, list(latest_by_analyte))
    return {"instrument_id": instrument_id, "items": items,
            "total": len(items), "path_locked": bool(blockers),
            "blockers": blockers,
            "calibration": calibration_posture(session, instrument_id)}


@router.get("/qc/path-status")
def qc_path_status(instrument_id: str = Query(...),
                   analyte_code: str = Query(...),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.read"))):
    """The instrument-path lock for one instrument + analyte: QC
    failure and calibration lock, named. (The report-level gate is
    GET /lab/qc/status + qc_blockers — separate, unchanged.)"""
    blockers = instrument_path_blockers(session, instrument_id,
                                         [analyte_code])
    latest = latest_qc_for(session, instrument_id, analyte_code)
    from . import lab as lab_mod
    return {"instrument_id": instrument_id, "analyte_code": analyte_code,
            "locked": bool(blockers), "blockers": blockers,
            "latest_qc": lab_mod._qc_out(latest) if latest else None,
            "calibration": calibration_posture(session, instrument_id)}


# ---------------------------------------------------------------------------
# Endpoints: calibration records + AMR verification
# ---------------------------------------------------------------------------

@router.post("/calibrations", status_code=201)
async def record_calibration(request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(
                                 require_capability("lab.work"))):
    """Record a calibration with MEASURED values per level against
    acceptance criteria (governed defaults unless the record names
    its own). The verdict is computed here, never caller-asserted.
    A passing record writes through to the device registry when the
    instrument has a registry twin (one calibration truth)."""
    payload = await _json_body(request)
    instrument_id = _require_instrument(session, payload.get("instrument_id"))
    levels = _parse_levels(payload)
    criteria = _criteria_for(session, payload)
    results, reasons = _evaluate_levels(levels, criteria)
    performed = datetime.now(timezone.utc)
    cal_cfg = (reagent_qc_config(session).get("calibration") or {})
    interval = cal_cfg.get("defaultIntervalDays")
    due_date = (performed.date() + timedelta(days=int(interval))).isoformat() \
        if isinstance(interval, int) and interval > 0 else None
    if isinstance(payload.get("next_due_date"), str):
        due_date = payload["next_due_date"]
    record = LabCalibrationRecord(
        instrument_id=instrument_id, kind="calibration",
        performed_at=performed, performed_by=user.username,
        levels=levels, criteria=criteria, level_results=results,
        passed=not reasons, failure_reasons=reasons,
        due_date=due_date,
        note=payload.get("note") if isinstance(payload.get("note"), str)
        else None)
    session.add(record)
    session.flush()
    if record.passed:
        _write_through_registry(session, instrument_id, performed, record)
    _mirror_instrument_log(session, user, record)
    append_audit(session, actor=user.username, action="lab.calibration.record",
                 entity=f"LabInstrument/{instrument_id}",
                 payload={"record_id": record.id, "kind": record.kind,
                          "passed": record.passed,
                          "due_date": record.due_date,
                          "registry_due": record.registry_due})
    session.commit()
    return _record_out(record)


@router.post("/calibrations/verify-amr", status_code=201)
async def verify_amr(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("lab.work"))):
    """Calibration verification (AMR, CLIA §493.1255): at least the
    governed minimum number of levels (default 3) SPANNING the
    reportable range, each level's measured value inside the
    acceptance criteria. Fewer levels than the minimum is refused
    outright (422, zero writes — the workflow cannot be satisfied);
    a recorded verification that fails span or criteria is a FAILED
    record and locks the instrument path until a passing one posts."""
    payload = await _json_body(request)
    instrument_id = _require_instrument(session, payload.get("instrument_id"))
    cal_cfg = (reagent_qc_config(session).get("calibration") or {})
    min_levels = cal_cfg.get("amrMinLevels")
    if not isinstance(min_levels, int) or isinstance(min_levels, bool) \
            or min_levels < 1:
        min_levels = 3
    levels = _parse_levels(payload)
    if len(levels) < min_levels:
        raise HTTPException(
            status_code=422,
            detail=(f"Calibration verification (AMR) requires at least "
                    f"{min_levels} levels spanning the reportable range "
                    f"(got {len(levels)}); the verification cannot be "
                    "recorded with fewer"))
    low, high = payload.get("reportable_low"), payload.get("reportable_high")
    for field, value in (("reportable_low", low), ("reportable_high", high)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(
                status_code=422,
                detail=f"'{field}' must be a number (the reportable "
                       "range the levels must span)")
    if float(low) >= float(high):
        raise HTTPException(status_code=422,
                            detail="'reportable_low' must be below "
                                   "'reportable_high'")
    criteria = _criteria_for(session, payload)
    results, reasons = _evaluate_levels(levels, criteria)
    nominals = [lvl["nominal"] for lvl in levels]
    spans = min(nominals) <= float(low) and max(nominals) >= float(high)
    if not spans:
        reasons.append(
            f"levels span nominal {min(nominals):g}..{max(nominals):g}, "
            f"which does not cover the reportable range "
            f"{float(low):g}..{float(high):g}")
    performed = datetime.now(timezone.utc)
    record = LabCalibrationRecord(
        instrument_id=instrument_id, kind="amr-verification",
        performed_at=performed, performed_by=user.username,
        levels=levels, criteria=criteria, level_results=results,
        passed=not reasons, failure_reasons=reasons,
        reportable_low=float(low), reportable_high=float(high),
        spans_range=spans,
        note=payload.get("note") if isinstance(payload.get("note"), str)
        else None)
    session.add(record)
    session.flush()
    _mirror_instrument_log(session, user, record)
    append_audit(session, actor=user.username,
                 action="lab.calibration.verify_amr",
                 entity=f"LabInstrument/{instrument_id}",
                 payload={"record_id": record.id, "passed": record.passed,
                          "spans_range": spans,
                          "levels": len(levels)})
    session.commit()
    return _record_out(record)


@router.get("/calibrations")
def list_calibrations(instrument_id: str | None = Query(default=None),
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.read"))):
    stmt = select(LabCalibrationRecord).order_by(
        LabCalibrationRecord.id.desc())
    if instrument_id:
        stmt = stmt.where(
            LabCalibrationRecord.instrument_id == instrument_id)
    rows = session.scalars(stmt).all()
    return {"items": [_record_out(r) for r in rows], "total": len(rows)}


@router.get("/calibrations/status")
def calibration_status(instrument_id: str = Query(...),
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("lab.read"))):
    """The one-truth calibration view for an instrument: posture
    (registry-first), lock state, and the latest records as the
    evidence behind it."""
    posture = calibration_posture(session, instrument_id)
    latest_cal = _latest_calibration(session, instrument_id, "calibration")
    latest_amr = _latest_calibration(session, instrument_id,
                                     "amr-verification")
    return {"instrument_id": instrument_id, "posture": posture,
            "locked": calibration_lock_reason(session, instrument_id)
            is not None,
            "lock_reason": calibration_lock_reason(session, instrument_id),
            "latest_calibration": _record_out(latest_cal) if latest_cal
            else None,
            "latest_amr_verification": _record_out(latest_amr) if latest_amr
            else None}
