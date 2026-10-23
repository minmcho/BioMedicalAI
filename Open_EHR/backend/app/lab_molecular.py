"""LAB-D14 — Molecular diagnostics discipline (Lab Depth pass, W8).

The depth layer that turns the catalogue's NAAT rows from qualitative
catalogue entries on the standard spine into a molecular DISCIPLINE
(research report §1.5 + §3.6): extraction batches and amplification
runs as first-class records, Ct values with governed interpretation
bands, per-sample amplification/extraction controls, a unidirectional
clean -> dirty workflow record per batch, a contamination workflow
with a decontamination gate — and, at the centre, the NTC
RUN-VALIDITY GATE: a positive no-template control invalidates the
ENTIRE run (every result discarded, never released) and a run whose
NTC status is unresolved cannot release ANY result.

Run/batch patterns follow app.lab_plates (own tables, materialised
rows, a gate evaluated at the point of use) — NOT its assay
templates: Wave F owns assay-plate realism and nothing here builds
it. Results are never written by this module directly: releasing a
run hands each valid sample to app.lab._create_results, the SAME
validation core manual entry and analyser ingest use, so the QC
gates, the W4 instrument-path lock, the W4 reagent-lot lockout (when
a lot is named — molecular entry names none), the D6(a) ingest
quarantine and the W9 competency gate all apply unchanged. Clinical
verify/release of the created reports remain the standard lab steps.

Configure, don't fork — governed Studio configuration
(terminology/lab-molecular, seeded published v1 by
ensure_lab_molecular_defaults): per-assay targets with Ct bands
(detected / inconclusive), control requirements, batching rhythm
(max batch size + run windows), the unidirectional area list (in
clean -> dirty order), the NTC positivity ceiling, repeat options
and decontamination methods. The seeded assays are the catalogue's
existing NAAT assays, honestly labelled GOVERNED DEFAULTS.

Storage: NEW tables owned by this module (the app.lab_plates
own-tables precedent). No existing table gains a column, so no
ensure-columns hook is owed.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import lab
from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import ConfigStatus, StudioConfig, User, utcnow
from .security import get_db, require_capability

router = APIRouter(prefix="/lab/molecular", tags=["lab-molecular"])

MOLECULAR_KEY = "lab-molecular"

REPEAT_OPTIONS = ("extraction", "amplification")


# ---------------------------------------------------------------------------
# Own tables
# ---------------------------------------------------------------------------

class MolecularBatch(Base):
    """One extraction batch: a set of samples extracted together for
    one assay, carrying the unidirectional-workflow area stamps
    recorded as the batch moved through the laboratory."""
    __tablename__ = "lab_molecular_batches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assay_code: Mapped[str] = mapped_column(String(80), nullable=False,
                                            index=True)
    instrument_id: Mapped[str | None] = mapped_column(String(120),
                                                      nullable=True)
    operator: Mapped[str] = mapped_column(String(120), nullable=False,
                                          default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="open", index=True)
    # [{order_id, specimen_id, patient_ref}]
    samples: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # [{area, at, by, flagged}] in recorded order.
    area_stamps: Mapped[list] = mapped_column(JSON, nullable=False,
                                              default=list)
    workflow_flagged: Mapped[bool] = mapped_column(default=False)
    workflow_flag_reason: Mapped[str | None] = mapped_column(Text,
                                                             nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class MolecularRun(Base):
    """One amplification run built from one extraction batch. The
    NTC status is the run-validity centre: unresolved blocks release,
    positive invalidates the whole run."""
    __tablename__ = "lab_molecular_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("lab_molecular_batches.id"), nullable=False, index=True)
    assay_code: Mapped[str] = mapped_column(String(80), nullable=False,
                                            index=True)
    instrument_id: Mapped[str | None] = mapped_column(String(120),
                                                      nullable=True)
    operator: Mapped[str] = mapped_column(String(120), nullable=False,
                                          default="")
    # open | invalidated | released
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="open", index=True)
    # unresolved | negative | positive
    ntc_status: Mapped[str] = mapped_column(String(20), nullable=False,
                                            default="unresolved")
    ntc_ct: Mapped[float | None] = mapped_column(nullable=True)
    positive_control_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True)
    positive_control_ct: Mapped[float | None] = mapped_column(nullable=True)
    invalidated_reason: Mapped[str | None] = mapped_column(Text,
                                                           nullable=True)
    invalidated_by: Mapped[str | None] = mapped_column(String(120),
                                                      nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    repeat_of_run_id: Mapped[int | None] = mapped_column(Integer,
                                                         nullable=True)
    repeat_from: Mapped[str | None] = mapped_column(String(40),
                                                    nullable=True)
    repeat_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    released_by: Mapped[str | None] = mapped_column(String(120),
                                                    nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class MolecularRunSample(Base):
    """One sample well of a run: its Ct values per target, its
    internal amplification + extraction controls, and the computed
    interpretation / validity. On run invalidation the computed
    interpretation is moved to `discarded_interpretation` and the
    live interpretation cleared — discarded, visibly, never merely
    flagged."""
    __tablename__ = "lab_molecular_run_samples"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("lab_molecular_runs.id"), nullable=False, index=True)
    order_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                          index=True)
    specimen_id: Mapped[str | None] = mapped_column(String(120),
                                                    nullable=True)
    patient_ref: Mapped[str | None] = mapped_column(String(200),
                                                    nullable=True)
    # {target_code: ct-or-null}; None until resulted.
    ct_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    target_interpretations: Mapped[dict | None] = mapped_column(
        JSON, nullable=True)
    # {status: passed|failed, ct} per control kind.
    internal_control: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    extraction_control: Mapped[dict | None] = mapped_column(JSON,
                                                            nullable=True)
    interpretation: Mapped[str | None] = mapped_column(String(40),
                                                       nullable=True)
    discarded_interpretation: Mapped[str | None] = mapped_column(
        String(40), nullable=True)
    # unresulted | valid | inconclusive | invalidated
    validity: Mapped[str] = mapped_column(String(20), nullable=False,
                                          default="unresulted")
    validity_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resulted_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resulted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class MolecularContaminationEvent(Base):
    """One contamination event: its trigger, its blast radius
    (affected runs / batches / instruments / areas, snapshotted at
    raise time) and — before the affected scope returns to service —
    its decontamination record with verification swabs."""
    __tablename__ = "lab_molecular_contamination_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(40), nullable=False)
    # open | cleared
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="open", index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    affected_run_ids: Mapped[list] = mapped_column(JSON, nullable=False,
                                                   default=list)
    affected_batch_ids: Mapped[list] = mapped_column(JSON, nullable=False,
                                                     default=list)
    instruments: Mapped[list] = mapped_column(JSON, nullable=False,
                                              default=list)
    areas: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    decontamination: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raised_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                           default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)
    cleared_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    cleared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Governed configuration (terminology/lab-molecular)
# ---------------------------------------------------------------------------

def _opt(code, display, **extra):
    return {"code": code, "display": display, "active": True, **extra}


def _target(code, display, detected=37.0, inconclusive=40.0):
    return {"code": code, "display": display,
            "detectedMaxCt": detected, "inconclusiveMaxCt": inconclusive}


def _assay_entry(code, display, targets, max_batch=24,
                 windows=("08:00", "14:00")):
    return {"code": code, "display": display, "active": True,
            "targets": targets,
            "requiresInternalControl": True,
            "requiresExtractionControl": True,
            "requiresPositiveControl": True,
            "maxBatchSize": max_batch,
            "runWindows": list(windows)}


def default_molecular_payload() -> dict:
    """Molecular governed defaults — seeded from the catalogue's
    existing NAAT assays (virology bench). Every Ct band, batch
    limit and window is a GOVERNED DEFAULT (director to set for the
    site's validated assays), labelled as such in the payload name,
    never presented as a universal standard."""
    return {
        "name": "OpenCare molecular diagnostics (LAB-D14) — GOVERNED "
                "DEFAULTS seeded from the catalogue NAAT assays; "
                "director to set validated Ct bands per assay",
        "concepts": [{"code": "lab-molecular",
                      "display": "Molecular diagnostics configuration",
                      "active": True}],
        "assays": [
            _assay_entry("94500-6",
                   "SARS-CoV-2 RNA [Presence] in Respiratory specimen by NAA",
                   [_target("sars-cov-2", "SARS-CoV-2 RNA")]),
            _assay_entry("VIR-INFA", "Influenza A RNA [Presence] by NAAT",
                   [_target("influenza-a", "Influenza A RNA")]),
            _assay_entry("VIR-INFB", "Influenza B RNA [Presence] by NAAT",
                   [_target("influenza-b", "Influenza B RNA")]),
            _assay_entry("VIR-RSV",
                   "Respiratory syncytial virus RNA [Presence] by NAAT",
                   [_target("rsv", "RSV RNA")]),
        ],
        # Unidirectional workflow, clean -> dirty, IN ORDER: a batch
        # stamp whose area order is lower than the highest order
        # already recorded is a dirty -> clean progression and is
        # flagged by name.
        "areas": [
            _opt("clean-pre-amp", "Clean / pre-amplification area", order=0),
            _opt("specimen-extraction", "Specimen preparation / extraction",
                 order=1),
            _opt("post-amp-dirty", "Amplification / post-amplification area",
                 order=2),
        ],
        # An NTC amplification at or below this Ct is POSITIVE.
        "ntcPositiveMaxCt": 40.0,
        "repeatOptions": [
            _opt("extraction", "Repeat from extraction (new batch + run)"),
            _opt("amplification", "Repeat from amplification (new run)"),
        ],
        "decontaminationMethods": [
            _opt("sodium-hypochlorite",
                 "10% sodium hypochlorite surface decontamination"),
            _opt("uv-irradiation", "UV irradiation of the work area"),
            _opt("dna-decontaminant",
                 "Commercial DNA decontaminant (enzymatic / oxidative)"),
            _opt("full-area-clean",
                 "Full area clean + environmental swabbing"),
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


def ensure_lab_molecular_defaults(session: Session) -> None:
    """Seed terminology/lab-molecular as published v1 (idempotent).
    Coordinator wires this in app.main startup."""
    _seed_published(session, "terminology", MOLECULAR_KEY,
                    default_molecular_payload())
    session.commit()


def _cfg(session: Session) -> tuple[dict, int | None]:
    cfg = published_config(session, "terminology", MOLECULAR_KEY)
    if cfg is not None and isinstance(cfg.payload, dict):
        merged = default_molecular_payload()
        merged.update(cfg.payload)
        return merged, cfg.version
    return default_molecular_payload(), None


def _assay(session: Session, code: str) -> dict:
    payload, _ = _cfg(session)
    for assay in payload.get("assays") or []:
        if isinstance(assay, dict) and assay.get("code") == code:
            if assay.get("active") is False:
                raise HTTPException(
                    status_code=422,
                    detail=f"Molecular assay '{code}' is inactive in the "
                           "published lab-molecular config")
            return assay
    raise HTTPException(
        status_code=422,
        detail=f"Assay '{code}' is not a governed molecular assay in "
               "terminology/lab-molecular (publish it in Studio first)")


def _areas(session: Session) -> list[dict]:
    payload, _ = _cfg(session)
    return [a for a in payload.get("areas") or [] if isinstance(a, dict)]


def _area_order(session: Session) -> dict[str, int]:
    return {a["code"]: int(a.get("order", 0)) for a in _areas(session)
            if a.get("code")}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


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


def _parse_ts(raw, field: str) -> str:
    if raw is None:
        return _now_iso()
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be an ISO timestamp")
    try:
        datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be an ISO timestamp")
    return raw.strip()


def _evaluate_area_progression(session: Session,
                               stamps: list[dict]) -> tuple[bool, str | None]:
    """Dirty -> clean detection over the recorded stamp sequence:
    flagged when any stamp's governed area order is below the
    highest order already seen (the batch moved backwards, from a
    dirtier area into a cleaner one). Returns (flagged, reason)."""
    order = _area_order(session)
    highest = -1
    highest_area = None
    for stamp in stamps:
        area = stamp.get("area")
        rank = order.get(area)
        if rank is None:
            continue
        if rank < highest:
            return True, (
                f"Unidirectional workflow violation: batch moved from "
                f"'{highest_area}' back to '{area}' — a dirty-to-clean "
                f"progression. Molecular work flows clean to dirty "
                f"only; this batch is flagged and its movement must "
                f"be reviewed before its results are relied on.")
        if rank > highest:
            highest, highest_area = rank, area
    return False, None


def _validate_stamps(session: Session, raw) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HTTPException(status_code=422,
                            detail="'area_stamps' must be a list of "
                                   "{area, at} entries")
    known = _area_order(session)
    stamps = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or entry.get("area") not in known:
            raise HTTPException(
                status_code=422,
                detail=f"area_stamps[{i}].area must be one of the "
                       f"governed unidirectional areas {sorted(known)}")
        stamps.append({"area": entry["area"],
                       "at": _parse_ts(entry.get("at"),
                                       f"area_stamps[{i}].at"),
                       "by": entry.get("by") or ""})
    return stamps


def _batch_row(session: Session, batch_id: int) -> MolecularBatch:
    row = session.get(MolecularBatch, batch_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Molecular batch {batch_id} not found")
    return row


def _run_row(session: Session, run_id: int) -> MolecularRun:
    row = session.get(MolecularRun, run_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Molecular run {run_id} not found")
    return row


def _run_samples(session: Session, run_id: int) -> list[MolecularRunSample]:
    return list(session.scalars(
        select(MolecularRunSample).where(
            MolecularRunSample.run_id == run_id)
        .order_by(MolecularRunSample.id.asc())).all())


# ---------------------------------------------------------------------------
# Contamination: blast radius + the return-to-service block
# ---------------------------------------------------------------------------

def _open_events(session: Session) -> list[MolecularContaminationEvent]:
    return list(session.scalars(
        select(MolecularContaminationEvent).where(
            MolecularContaminationEvent.status == "open")
        .order_by(MolecularContaminationEvent.id.asc())).all())


def contamination_blocker(session: Session,
                            instrument_id: str | None,
                            areas: list[str]) -> MolecularContaminationEvent | None:
    """The OPEN contamination event blocking this instrument/area,
    or None. An affected instrument or area stays out of service
    for NEW work until its event's decontamination is recorded."""
    for event in _open_events(session):
        if instrument_id and instrument_id in (event.instruments or []):
            return event
        if any(a in (event.areas or []) for a in areas):
            return event
    return None


def _enforce_not_contaminated(session: Session, instrument_id,
                              areas: list[str], action: str) -> None:
    event = contamination_blocker(session, instrument_id, areas)
    if event is not None:
        scope = []
        if instrument_id and instrument_id in (event.instruments or []):
            scope.append(f"instrument '{instrument_id}'")
        hit = [a for a in areas if a in (event.areas or [])]
        if hit:
            scope.append(f"area(s) {', '.join(hit)}")
        raise HTTPException(
            status_code=409,
            detail=(f"Cannot {action}: {' and '.join(scope)} under "
                    f"contamination event {event.id} "
                    f"(trigger: {event.trigger}), which is not yet "
                    f"cleared. Record the decontamination (method, "
                    f"areas, verification swabs) for event {event.id} "
                    f"before the affected scope returns to service."))


def _blast_radius(session: Session, *, source_run: MolecularRun | None,
                  instrument_id: str | None,
                  areas: list[str]) -> dict:
    """Affected runs/batches for an event: the source run, plus
    every run sharing the instrument, plus every batch/run whose
    recorded areas intersect the event areas."""
    instruments = {i for i in [instrument_id] if i}
    area_set = set(areas)
    if source_run is not None:
        if source_run.instrument_id:
            instruments.add(source_run.instrument_id)
        batch = session.get(MolecularBatch, source_run.batch_id)
        if batch is not None:
            area_set.update(s.get("area") for s in batch.area_stamps or [])
    runs = session.scalars(select(MolecularRun)).all()
    batches = {b.id: b for b in session.scalars(
        select(MolecularBatch)).all()}
    affected_runs, affected_batches = set(), set()
    for run in runs:
        batch = batches.get(run.batch_id)
        batch_areas = {s.get("area") for s in (batch.area_stamps or [])
                       if batch} if batch else set()
        if (source_run is not None and run.id == source_run.id) or \
                (run.instrument_id and run.instrument_id in instruments) or \
                (batch_areas & area_set):
            affected_runs.add(run.id)
            affected_batches.add(run.batch_id)
    if source_run is not None:
        affected_batches.add(source_run.batch_id)
    for batch in batches.values():
        if {s.get("area") for s in batch.area_stamps or []} & area_set:
            affected_batches.add(batch.id)
    return {"affected_run_ids": sorted(affected_runs),
            "affected_batch_ids": sorted(affected_batches),
            "instruments": sorted(instruments),
            "areas": sorted(a for a in area_set if a)}


def raise_contamination_event(session: Session, *, trigger: str,
                              actor: str, note: str | None = None,
                              source_run: MolecularRun | None = None,
                              instrument_id: str | None = None,
                              areas: list[str] | None = None,
                              ) -> MolecularContaminationEvent:
    """Raise (or return the existing open) contamination event for
    a scope. One open event per (trigger scope): a second NTC
    positive on an already quarantined instrument extends the
    existing event's blast radius instead of stacking duplicates."""
    radius = _blast_radius(session, source_run=source_run,
                           instrument_id=instrument_id,
                           areas=areas or [])
    for event in _open_events(session):
        if set(event.instruments or []) & set(radius["instruments"]) or \
                set(event.areas or []) & set(radius["areas"]):
            event.affected_run_ids = sorted(set(
                (event.affected_run_ids or []) +
                radius["affected_run_ids"]))
            event.affected_batch_ids = sorted(set(
                (event.affected_batch_ids or []) +
                radius["affected_batch_ids"]))
            event.instruments = sorted(set(
                (event.instruments or []) + radius["instruments"]))
            event.areas = sorted(set((event.areas or []) +
                                     radius["areas"]))
            session.flush()
            return event
    event = MolecularContaminationEvent(
        trigger=trigger, status="open", note=note,
        source_run_id=source_run.id if source_run else None,
        affected_run_ids=radius["affected_run_ids"],
        affected_batch_ids=radius["affected_batch_ids"],
        instruments=radius["instruments"], areas=radius["areas"],
        raised_by=actor)
    session.add(event)
    session.flush()
    append_audit(session, actor=actor,
                 action="lab.molecular.contamination.raise",
                 entity=f"MolecularContaminationEvent/{event.id}",
                 payload={"trigger": trigger,
                          "affected_run_ids": event.affected_run_ids,
                          "affected_batch_ids": event.affected_batch_ids,
                          "instruments": event.instruments,
                          "areas": event.areas})
    return event


def _event_out(event: MolecularContaminationEvent) -> dict:
    return {
        "id": event.id, "trigger": event.trigger, "status": event.status,
        "note": event.note, "source_run_id": event.source_run_id,
        "affected_run_ids": event.affected_run_ids or [],
        "affected_batch_ids": event.affected_batch_ids or [],
        "instruments": event.instruments or [],
        "areas": event.areas or [],
        "decontamination": event.decontamination,
        "raised_by": event.raised_by,
        "created_at": event.created_at.isoformat()
        if event.created_at else None,
        "cleared_by": event.cleared_by,
        "cleared_at": event.cleared_at.isoformat()
        if event.cleared_at else None,
    }


# ---------------------------------------------------------------------------
# Serialisation + interpretation
# ---------------------------------------------------------------------------

def _batch_out(session: Session, batch: MolecularBatch) -> dict:
    order = _area_order(session)
    stamps = []
    highest = -1
    for stamp in batch.area_stamps or []:
        rank = order.get(stamp.get("area"), -1)
        flagged = rank < highest
        if rank > highest:
            highest = rank
        stamps.append({**stamp, "flagged": flagged})
    return {
        "id": batch.id, "assay_code": batch.assay_code,
        "instrument_id": batch.instrument_id, "operator": batch.operator,
        "status": batch.status, "samples": batch.samples or [],
        "sample_count": len(batch.samples or []),
        "area_stamps": stamps,
        "workflow_flagged": bool(batch.workflow_flagged),
        "workflow_flag_reason": batch.workflow_flag_reason,
        "created_by": batch.created_by,
        "created_at": batch.created_at.isoformat()
        if batch.created_at else None,
    }


def _sample_out(sample: MolecularRunSample) -> dict:
    return {
        "id": sample.id, "run_id": sample.run_id,
        "order_id": sample.order_id, "specimen_id": sample.specimen_id,
        "patient_ref": sample.patient_ref,
        "ct_values": sample.ct_values,
        "target_interpretations": sample.target_interpretations,
        "internal_control": sample.internal_control,
        "extraction_control": sample.extraction_control,
        "interpretation": sample.interpretation,
        "discarded_interpretation": sample.discarded_interpretation,
        "validity": sample.validity,
        "validity_reason": sample.validity_reason,
        "report_id": sample.report_id,
        "resulted_by": sample.resulted_by,
        "resulted_at": sample.resulted_at.isoformat()
        if sample.resulted_at else None,
    }


def _release_blockers(session: Session, run: MolecularRun,
                      samples: list[MolecularRunSample]) -> list[str]:
    """Every named reason this run cannot release right now. The
    NTC gate is first and named: unresolved blocks, positive has
    already invalidated the run (status check above it)."""
    blockers: list[str] = []
    if run.status == "invalidated":
        blockers.append(
            f"Run {run.id} is INVALIDATED ({run.invalidated_reason}); "
            f"its results are discarded and can never be released — "
            f"use the repeat workflow")
        return blockers
    if run.status == "released":
        blockers.append(f"Run {run.id} has already been released")
        return blockers
    if run.ntc_status == "unresolved":
        blockers.append(
            f"NTC gate: run {run.id} cannot release any result while "
            f"its no-template control status is UNRESOLVED — record "
            f"the NTC result for this run first")
    elif run.ntc_status == "positive":
        blockers.append(
            f"NTC gate: run {run.id}'s no-template control is "
            f"POSITIVE — the run is invalid and no result may be "
            f"released")
    if run.positive_control_status == "failed":
        blockers.append(
            f"Run {run.id}'s positive control FAILED — the run has "
            f"not demonstrated it can amplify; no result may be "
            f"released")
    unresulted = [s.order_id for s in samples
                  if s.validity == "unresulted"]
    if unresulted:
        blockers.append(
            f"Run {run.id} has unresulted sample(s): "
            f"{', '.join(unresulted)}")
    batch = session.get(MolecularBatch, run.batch_id)
    areas = [s.get("area") for s in (batch.area_stamps or [])
             if batch] if batch else []
    event = contamination_blocker(session, run.instrument_id, areas)
    if event is not None:
        blockers.append(
            f"Contamination event {event.id} (trigger: "
            f"{event.trigger}) is open for this run's instrument/"
            f"area scope and has not been cleared by a recorded "
            f"decontamination")
    return blockers


def _run_out(session: Session, run: MolecularRun,
             detail: bool = False) -> dict:
    samples = _run_samples(session, run.id)
    blockers = _release_blockers(session, run, samples)
    out = {
        "id": run.id, "batch_id": run.batch_id,
        "assay_code": run.assay_code, "instrument_id": run.instrument_id,
        "operator": run.operator, "status": run.status,
        "ntc": {"status": run.ntc_status, "ct": run.ntc_ct},
        "positive_control": {"status": run.positive_control_status,
                             "ct": run.positive_control_ct},
        "invalidated_reason": run.invalidated_reason,
        "invalidated_by": run.invalidated_by,
        "invalidated_at": run.invalidated_at.isoformat()
        if run.invalidated_at else None,
        "repeat_of_run_id": run.repeat_of_run_id,
        "repeat_from": run.repeat_from,
        "repeat_run_id": run.repeat_run_id,
        "released_by": run.released_by,
        "released_at": run.released_at.isoformat()
        if run.released_at else None,
        "gate": {"can_release": not blockers, "blockers": blockers},
        "sample_count": len(samples),
        "created_by": run.created_by,
        "created_at": run.created_at.isoformat()
        if run.created_at else None,
    }
    if detail:
        out["samples"] = [_sample_out(s) for s in samples]
        batch = session.get(MolecularBatch, run.batch_id)
        out["batch"] = _batch_out(session, batch) if batch else None
    return out


def _target_band(target: dict, ct) -> str:
    if ct is None:
        return "not-detected"
    if ct <= float(target["detectedMaxCt"]):
        return "detected"
    if ct <= float(target["inconclusiveMaxCt"]):
        return "inconclusive"
    return "not-detected"


def _interpret_sample(assay: dict, ct_values: dict,
                      internal: dict | None,
                      extraction: dict | None) -> dict:
    """Compute one sample's interpretation + validity from its Ct
    values and controls. The control-failure semantics are exact:
    a failed internal amplification (or extraction) control
    invalidates THAT sample's NEGATIVE — the sample becomes
    inconclusive, named, never a silent negative. A detected target
    stands (amplification demonstrably occurred in that well)."""
    targets = {t["code"]: t for t in assay.get("targets") or []
               if isinstance(t, dict)}
    per_target = {}
    for code, target in targets.items():
        per_target[code] = _target_band(target, ct_values.get(code))
    bands = list(per_target.values())
    if "detected" in bands:
        overall = "Detected"
    elif "inconclusive" in bands:
        overall = "Inconclusive"
    else:
        overall = "Not detected"
    failed = []
    if assay.get("requiresInternalControl") and internal is not None \
            and internal.get("status") == "failed":
        failed.append("internal amplification control")
    if assay.get("requiresExtractionControl") and extraction is not None \
            and extraction.get("status") == "failed":
        failed.append("extraction control")
    reasons = []
    if overall == "Inconclusive" and "inconclusive" in bands:
        reasons.append(
            "Ct in the governed inconclusive band for at least one "
            "target")
    validity = "valid"
    if failed and overall == "Not detected":
        overall = "Inconclusive"
        validity = "inconclusive"
        reasons.append(
            f"{' and '.join(failed)} FAILED — a failed control "
            f"invalidates this sample's negative: the sample is "
            f"INCONCLUSIVE, not negative")
    elif failed:
        reasons.append(
            f"{' and '.join(failed)} failed, but a target was "
            f"detected — amplification occurred in this well, so the "
            f"positive stands and the control failure is recorded")
    if validity == "valid" and overall == "Inconclusive":
        validity = "inconclusive"
    return {"interpretation": overall, "validity": validity,
            "target_interpretations": per_target,
            "validity_reason": "; ".join(reasons) if reasons else None,
            "failed_controls": failed}


def _validate_control(raw, field: str, required: bool) -> dict | None:
    if raw is None:
        if required:
            raise HTTPException(
                status_code=422,
                detail=f"'{field}' is required for this assay "
                       f"(governed control requirement)")
        return None
    if not isinstance(raw, dict) or raw.get("status") not in (
            "passed", "failed"):
        raise HTTPException(
            status_code=422,
            detail=f"'{field}.status' must be 'passed' or 'failed'")
    ct = raw.get("ct")
    if ct is not None and (isinstance(ct, bool) or
                           not isinstance(ct, (int, float))):
        raise HTTPException(status_code=422,
                            detail=f"'{field}.ct' must be a number or null")
    return {"status": raw["status"],
            "ct": float(ct) if ct is not None else None}


def _invalidate_run(session: Session, run: MolecularRun, *,
                    reason: str, actor: str) -> None:
    """Discard EVERY result in the run: each sample's computed
    interpretation is moved to `discarded_interpretation`, its live
    interpretation cleared and its validity set to invalidated.
    Nothing in an invalidated run can be released (the release
    blockers refuse by name, and _create_results is never called
    for it)."""
    run.status = "invalidated"
    run.invalidated_reason = reason
    run.invalidated_by = actor
    run.invalidated_at = _now()
    for sample in _run_samples(session, run.id):
        if sample.interpretation is not None:
            sample.discarded_interpretation = sample.interpretation
        sample.interpretation = None
        sample.validity = "invalidated"
        sample.validity_reason = reason
    session.flush()
    append_audit(session, actor=actor, action="lab.molecular.run.invalidate",
                 entity=f"MolecularRun/{run.id}",
                 payload={"reason": reason, "assay_code": run.assay_code})


# ---------------------------------------------------------------------------
# Config surface
# ---------------------------------------------------------------------------

@router.get("/config")
def molecular_config(session: Session = Depends(get_db),
                     user: User = Depends(require_capability("lab.read"))):
    payload, version = _cfg(session)
    return {
        "config": {"kind": "terminology", "key": MOLECULAR_KEY,
                   "version": version},
        "assays": payload.get("assays") or [],
        "areas": payload.get("areas") or [],
        "ntc_positive_max_ct": payload.get("ntcPositiveMaxCt"),
        "repeat_options": payload.get("repeatOptions") or [],
        "decontamination_methods":
            payload.get("decontaminationMethods") or [],
    }


# ---------------------------------------------------------------------------
# Extraction batches
# ---------------------------------------------------------------------------

@router.post("/batches", status_code=201)
async def create_batch(request: Request, session: Session = Depends(get_db),
                       user: User = Depends(require_capability("lab.work"))):
    """Create an extraction batch for one governed assay. Samples
    name lab orders for that assay (the order's code must match the
    assay); the governed max batch size is enforced by name; the
    unidirectional area stamps are recorded and a dirty -> clean
    progression is flagged by name (recorded, not silently
    accepted). Creation on a contaminated-not-cleared instrument or
    area refuses by name."""
    payload = await _json_body(request)
    assay_code = payload.get("assay_code")
    if not assay_code:
        raise HTTPException(status_code=422,
                            detail="'assay_code' is required")
    assay = _assay(session, str(assay_code))
    instrument_id = payload.get("instrument_id")
    if instrument_id is not None and not str(instrument_id).strip():
        instrument_id = None
    raw_samples = payload.get("samples")
    if not isinstance(raw_samples, list) or not raw_samples:
        raise HTTPException(status_code=422,
                            detail="'samples' must be a non-empty list of "
                                   "{order_id} entries")
    max_batch = assay.get("maxBatchSize")
    if isinstance(max_batch, int) and len(raw_samples) > max_batch:
        raise HTTPException(
            status_code=422,
            detail=f"Batch of {len(raw_samples)} samples exceeds the "
                   f"governed max batch size {max_batch} for assay "
                   f"'{assay_code}' (terminology/lab-molecular batching "
                   f"rhythm)")
    stamps = _validate_stamps(session, payload.get("area_stamps"))
    for stamp in stamps:
        stamp["by"] = user.username
    _enforce_not_contaminated(
        session, str(instrument_id) if instrument_id else None,
        [s["area"] for s in stamps], "create a molecular batch")
    samples, seen = [], set()
    for i, entry in enumerate(raw_samples):
        order_id = entry.get("order_id") if isinstance(entry, dict) else None
        if not order_id:
            raise HTTPException(
                status_code=422,
                detail=f"samples[{i}].order_id is required")
        if order_id in seen:
            raise HTTPException(
                status_code=422,
                detail=f"Order '{order_id}' appears twice in this batch")
        seen.add(order_id)
        order = lab._order_row(session, str(order_id))
        concept = lab._order_code(session, order)
        if concept["code"] != str(assay_code):
            raise HTTPException(
                status_code=422,
                detail=f"Order {order_id} is for '{concept['code']}', "
                       f"not molecular assay '{assay_code}'")
        specimen = lab._specimen_row(session, order)
        samples.append({
            "order_id": str(order_id),
            "specimen_id": specimen.resource_id if specimen else None,
            "patient_ref": (order.body or {}).get(
                "subject", {}).get("reference")})
    flagged, reason = _evaluate_area_progression(session, stamps)
    batch = MolecularBatch(
        assay_code=str(assay_code),
        instrument_id=str(instrument_id) if instrument_id else None,
        operator=str(payload.get("operator") or user.username),
        status="open", samples=samples, area_stamps=stamps,
        workflow_flagged=flagged, workflow_flag_reason=reason,
        created_by=user.username)
    session.add(batch)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.molecular.batch.create",
                 entity=f"MolecularBatch/{batch.id}",
                 payload={"assay_code": batch.assay_code,
                          "sample_count": len(samples),
                          "workflow_flagged": flagged})
    session.commit()
    return _batch_out(session, batch)


@router.get("/batches")
def list_batches(session: Session = Depends(get_db),
                 user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(select(MolecularBatch)
                           .order_by(MolecularBatch.id.asc())).all()
    return {"items": [_batch_out(session, b) for b in rows],
            "total": len(rows)}


@router.get("/batches/{batch_id}")
def batch_detail(batch_id: int, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("lab.read"))):
    return _batch_out(session, _batch_row(session, batch_id))


@router.post("/batches/{batch_id}/area-stamps", status_code=201)
async def add_area_stamps(batch_id: int, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("lab.work"))):
    """Append unidirectional-workflow area stamps to a batch (the
    batch physically moved). The progression is re-evaluated over
    the whole recorded sequence; a dirty -> clean move flags the
    batch by name."""
    payload = await _json_body(request)
    batch = _batch_row(session, batch_id)
    new = _validate_stamps(session, payload.get("area_stamps"))
    if not new:
        raise HTTPException(status_code=422,
                            detail="'area_stamps' must name at least one "
                                   "governed area")
    for stamp in new:
        stamp["by"] = user.username
    stamps = list(batch.area_stamps or []) + new
    flagged, reason = _evaluate_area_progression(session, stamps)
    batch.area_stamps = stamps
    batch.workflow_flagged = flagged
    batch.workflow_flag_reason = reason
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.molecular.batch.area_stamp",
                 entity=f"MolecularBatch/{batch.id}",
                 payload={"areas": [s["area"] for s in new],
                          "workflow_flagged": flagged})
    session.commit()
    return _batch_out(session, batch)


# ---------------------------------------------------------------------------
# Amplification runs
# ---------------------------------------------------------------------------

def _create_run(session: Session, user: User, batch: MolecularBatch, *,
                instrument_id, repeat_of: int | None = None,
                repeat_from: str | None = None) -> MolecularRun:
    run = MolecularRun(
        batch_id=batch.id, assay_code=batch.assay_code,
        instrument_id=instrument_id, operator=user.username,
        status="open", ntc_status="unresolved",
        repeat_of_run_id=repeat_of, repeat_from=repeat_from,
        created_by=user.username)
    session.add(run)
    session.flush()
    for sample in batch.samples or []:
        session.add(MolecularRunSample(
            run_id=run.id, order_id=sample.get("order_id"),
            specimen_id=sample.get("specimen_id"),
            patient_ref=sample.get("patient_ref")))
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.molecular.run.create",
                 entity=f"MolecularRun/{run.id}",
                 payload={"batch_id": batch.id,
                          "assay_code": run.assay_code,
                          "repeat_of_run_id": repeat_of,
                          "repeat_from": repeat_from})
    return run


@router.post("/runs", status_code=201)
async def create_run(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("lab.work"))):
    """Build an amplification run from an extraction batch: one
    sample row per batch sample, NTC unresolved, nothing resulted.
    Creation on a contaminated-not-cleared instrument/area refuses
    by name."""
    payload = await _json_body(request)
    batch_id = payload.get("batch_id")
    if batch_id is None:
        raise HTTPException(status_code=422,
                            detail="'batch_id' is required")
    batch = _batch_row(session, int(batch_id))
    instrument_id = payload.get("instrument_id") or batch.instrument_id
    areas = [s.get("area") for s in batch.area_stamps or []]
    _enforce_not_contaminated(
        session, str(instrument_id) if instrument_id else None, areas,
        "create a molecular run")
    run = _create_run(session, user, batch,
                      instrument_id=str(instrument_id)
                      if instrument_id else None)
    if payload.get("operator"):
        run.operator = str(payload["operator"])
    session.commit()
    return _run_out(session, run, detail=True)


@router.get("/runs")
def list_runs(status: str | None = Query(default=None),
              session: Session = Depends(get_db),
              user: User = Depends(require_capability("lab.read"))):
    stmt = select(MolecularRun).order_by(MolecularRun.id.asc())
    if status:
        stmt = stmt.where(MolecularRun.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_run_out(session, r) for r in rows],
            "total": len(rows)}


@router.get("/runs/{run_id}")
def run_detail(run_id: int, session: Session = Depends(get_db),
               user: User = Depends(require_capability("lab.read"))):
    return _run_out(session, _run_row(session, run_id), detail=True)


# ---------------------------------------------------------------------------
# Run controls — the NTC gate lives here
# ---------------------------------------------------------------------------

@router.post("/runs/{run_id}/controls")
async def record_controls(run_id: int, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("lab.work"))):
    """Record the run-level controls. The no-template control is
    judged server-side: an NTC Ct at/below the governed positivity
    ceiling (or an explicit detected flag) is POSITIVE, and a
    positive NTC INVALIDATES THE ENTIRE RUN in this transaction —
    every sample result discarded — and raises a contamination
    event for the run's instrument/area scope. A null NTC Ct with
    the field present is a true negative (no amplification)."""
    payload = await _json_body(request)
    run = _run_row(session, run_id)
    if run.status == "invalidated":
        raise HTTPException(
            status_code=409,
            detail=f"Run {run_id} is invalidated ({run.invalidated_reason}); "
                   "controls are no longer accepted — use the repeat "
                   "workflow")
    if run.status == "released":
        raise HTTPException(status_code=409,
                            detail=f"Run {run_id} has already been released")
    cfg, _ = _cfg(session)
    if "ntc_ct" not in payload and "ntc_detected" not in payload:
        raise HTTPException(
            status_code=422,
            detail="Recording run controls requires the NTC result: "
                   "'ntc_ct' (a number, or null when the no-template "
                   "control did not amplify) or 'ntc_detected'")
    ntc_ct = payload.get("ntc_ct")
    if ntc_ct is not None and (isinstance(ntc_ct, bool) or
                               not isinstance(ntc_ct, (int, float))):
        raise HTTPException(status_code=422,
                            detail="'ntc_ct' must be a number or null")
    if payload.get("ntc_detected") is not None and \
            not isinstance(payload.get("ntc_detected"), bool):
        raise HTTPException(status_code=422,
                            detail="'ntc_detected' must be a boolean")
    ceiling = cfg.get("ntcPositiveMaxCt")
    ceiling = float(ceiling) if isinstance(ceiling, (int, float)) else 40.0
    if payload.get("ntc_detected") is not None:
        positive = payload["ntc_detected"] is True
    elif ntc_ct is not None:
        positive = float(ntc_ct) <= ceiling
    else:
        positive = False
    run.ntc_ct = float(ntc_ct) if ntc_ct is not None else None
    run.ntc_status = "positive" if positive else "negative"
    pc_status = payload.get("positive_control_status")
    if pc_status is not None:
        if pc_status not in ("passed", "failed"):
            raise HTTPException(
                status_code=422,
                detail="'positive_control_status' must be 'passed' or "
                       "'failed'")
        run.positive_control_status = pc_status
    pc_ct = payload.get("positive_control_ct")
    if pc_ct is not None:
        if isinstance(pc_ct, bool) or not isinstance(pc_ct, (int, float)):
            raise HTTPException(
                status_code=422,
                detail="'positive_control_ct' must be a number")
        run.positive_control_ct = float(pc_ct)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.molecular.run.controls",
                 entity=f"MolecularRun/{run_id}",
                 payload={"ntc_status": run.ntc_status,
                          "ntc_ct": run.ntc_ct,
                          "positive_control_status":
                              run.positive_control_status})
    if positive:
        reason = (f"NTC positive: the no-template control amplified "
                  f"(Ct {run.ntc_ct}) — the run is contaminated and "
                  f"INVALID; every result in run {run_id} is discarded")
        _invalidate_run(session, run, reason=reason, actor=user.username)
        batch = session.get(MolecularBatch, run.batch_id)
        areas = [s.get("area") for s in (batch.area_stamps or [])
                 if batch] if batch else []
        event = raise_contamination_event(
            session, trigger="ntc-positive", actor=user.username,
            note=reason, source_run=run,
            instrument_id=run.instrument_id, areas=areas)
        session.commit()
        out = _run_out(session, run, detail=True)
        out["contamination_event"] = _event_out(event)
        return out
    session.commit()
    return _run_out(session, run, detail=True)


# ---------------------------------------------------------------------------
# Per-sample Ct results + controls
# ---------------------------------------------------------------------------

@router.post("/runs/{run_id}/results")
async def record_results(run_id: int, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("lab.work"))):
    """Enter Ct values + per-sample controls for run samples.
    Interpretation is computed from the governed assay bands; a
    failed internal amplification / extraction control turns that
    sample's negative into a named INCONCLUSIVE. Results on an
    invalidated or released run refuse by name. Two or more control
    failures in one run raise a control-failure-pattern
    contamination event."""
    payload = await _json_body(request)
    run = _run_row(session, run_id)
    if run.status == "invalidated":
        raise HTTPException(
            status_code=409,
            detail=f"Run {run_id} is invalidated ({run.invalidated_reason}); "
                   "its results are discarded and no further results "
                   "are accepted — use the repeat workflow")
    if run.status == "released":
        raise HTTPException(status_code=409,
                            detail=f"Run {run_id} has already been released")
    assay = _assay(session, run.assay_code)
    entries = payload.get("results")
    if not isinstance(entries, list) or not entries:
        raise HTTPException(status_code=422,
                            detail="'results' must be a non-empty list of "
                                   "{order_id, ct_values, internal_control, "
                                   "extraction_control} entries")
    samples = {s.order_id: s for s in _run_samples(session, run_id)}
    target_codes = {t["code"] for t in assay.get("targets") or []
                    if isinstance(t, dict)}
    parsed = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise HTTPException(status_code=422,
                                detail=f"results[{i}] must be an object")
        sample = samples.get(entry.get("order_id"))
        if sample is None:
            raise HTTPException(
                status_code=422,
                detail=f"results[{i}].order_id '{entry.get('order_id')}' "
                       f"is not a sample of run {run_id}")
        ct_values = entry.get("ct_values")
        if not isinstance(ct_values, dict) or not ct_values:
            raise HTTPException(
                status_code=422,
                detail=f"results[{i}].ct_values must be an object mapping "
                       f"target codes {sorted(target_codes)} to a Ct "
                       f"number or null (no amplification)")
        unknown = set(ct_values) - target_codes
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"results[{i}].ct_values names unknown target(s) "
                       f"{sorted(unknown)} for assay '{run.assay_code}' "
                       f"(governed targets: {sorted(target_codes)})")
        cleaned = {}
        for code, ct in ct_values.items():
            if ct is not None and (isinstance(ct, bool) or
                                   not isinstance(ct, (int, float)) or
                                   float(ct) <= 0):
                raise HTTPException(
                    status_code=422,
                    detail=f"results[{i}].ct_values['{code}'] must be a "
                           f"positive Ct number or null")
            cleaned[code] = float(ct) if ct is not None else None
        internal = _validate_control(
            entry.get("internal_control"), "internal_control",
            bool(assay.get("requiresInternalControl")))
        extraction = _validate_control(
            entry.get("extraction_control"), "extraction_control",
            bool(assay.get("requiresExtractionControl")))
        parsed.append((sample, cleaned, internal, extraction))
    now = _now()
    for sample, cleaned, internal, extraction in parsed:
        outcome = _interpret_sample(assay, cleaned, internal, extraction)
        sample.ct_values = cleaned
        sample.target_interpretations = outcome["target_interpretations"]
        sample.internal_control = internal
        sample.extraction_control = extraction
        sample.interpretation = outcome["interpretation"]
        sample.validity = outcome["validity"]
        sample.validity_reason = outcome["validity_reason"]
        sample.resulted_by = user.username
        sample.resulted_at = now
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.molecular.run.results",
                 entity=f"MolecularRun/{run_id}",
                 payload={"order_ids": [s.order_id for s, *_ in parsed]})
    # Control-failure PATTERN: two or more samples in the run with a
    # failed amplification/extraction control is a contamination
    # signal, not two isolated accidents.
    all_samples = _run_samples(session, run_id)
    failed_count = sum(
        1 for s in all_samples
        if (s.internal_control or {}).get("status") == "failed"
        or (s.extraction_control or {}).get("status") == "failed")
    event = None
    if failed_count >= 2:
        batch = session.get(MolecularBatch, run.batch_id)
        areas = [s.get("area") for s in (batch.area_stamps or [])
                 if batch] if batch else []
        event = raise_contamination_event(
            session, trigger="control-failure-pattern",
            actor=user.username,
            note=(f"{failed_count} samples in run {run_id} have a "
                  f"failed internal amplification / extraction "
                  f"control — a control-failure pattern"),
            source_run=run, instrument_id=run.instrument_id, areas=areas)
    session.commit()
    out = _run_out(session, run, detail=True)
    if event is not None:
        out["contamination_event"] = _event_out(event)
    return out


# ---------------------------------------------------------------------------
# Release — the gate is evaluated here, results land on the lab spine
# ---------------------------------------------------------------------------

@router.post("/runs/{run_id}/release")
def release_run(run_id: int, session: Session = Depends(get_db),
                user: User = Depends(require_capability("lab.verify"))):
    """Release a run's results onto the lab spine. The gate is
    evaluated FIRST and refuses BY NAME (NTC unresolved / positive,
    positive-control failure, unresulted samples, open contamination
    event, unrepresentable interpretation, orders not in a
    resultable state) before ANY result is written — zero partial
    release. Each sample then lands through app.lab._create_results
    (the shared validation core: QC gates, W4 instrument-path lock,
    W9 competency gate), producing the standard preliminary,
    unreleased-tagged DiagnosticReport that the normal verify /
    release steps then govern."""
    run = _run_row(session, run_id)
    samples = _run_samples(session, run_id)
    blockers = _release_blockers(session, run, samples)
    if blockers:
        raise HTTPException(status_code=409, detail="; ".join(blockers))
    # Pre-write representability + order-state checks (zero partial).
    for sample in samples:
        concept = lab._concept(session, run.assay_code)
        values = (concept or {}).get("values") or []
        if values and sample.interpretation not in values:
            raise HTTPException(
                status_code=409,
                detail=(f"Sample {sample.order_id}: interpretation "
                        f"'{sample.interpretation}' cannot be reported "
                        f"for assay '{run.assay_code}' — the published "
                        f"catalogue values are {values}; the molecular "
                        f"release is refused by name rather than "
                        f"coerced"))
        order = lab._order_row(session, sample.order_id)
        status = (order.body or {}).get("status")
        if status not in ("received", "in-process"):
            raise HTTPException(
                status_code=409,
                detail=(f"Sample {sample.order_id}: order is in status "
                        f"'{status}'; molecular results can only land "
                        f"on a received / in-process order (the shared "
                        f"result core's rule)"))
    released = []
    for sample in samples:
        order = lab._order_row(session, sample.order_id)
        outcome = lab._create_results(
            session, user, order,
            [{"code": run.assay_code, "value": sample.interpretation}],
            source="molecular", instrument_id=run.instrument_id)
        sample.report_id = outcome["report"]["id"]
        released.append({"order_id": sample.order_id,
                         "report_id": sample.report_id,
                         "interpretation": sample.interpretation})
    run.status = "released"
    run.released_by = user.username
    run.released_at = _now()
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.molecular.run.release",
                 entity=f"MolecularRun/{run_id}",
                 payload={"released": released})
    session.commit()
    out = _run_out(session, run, detail=True)
    out["released"] = released
    return out


# ---------------------------------------------------------------------------
# Repeat workflow (from an invalidated run)
# ---------------------------------------------------------------------------

@router.post("/runs/{run_id}/repeat", status_code=201)
async def repeat_run(run_id: int, request: Request,
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability("lab.work"))):
    """The explicit repeat workflow for an invalidated run. The
    governed choice — repeat from EXTRACTION (a new batch carrying
    the same samples + areas, and a new run on it) or from
    AMPLIFICATION (a new run on the same batch) — is recorded on
    both runs. Creation still refuses on a contaminated-not-cleared
    instrument/area, by name: a repeat does not launder a
    contamination block."""
    payload = await _json_body(request)
    run = _run_row(session, run_id)
    if run.status != "invalidated":
        raise HTTPException(
            status_code=409,
            detail=f"Run {run_id} is in status '{run.status}'; the "
                   "repeat workflow applies to an INVALIDATED run")
    cfg, _ = _cfg(session)
    allowed = [o.get("code") for o in cfg.get("repeatOptions") or []
               if isinstance(o, dict)] or list(REPEAT_OPTIONS)
    repeat_from = payload.get("repeat_from")
    if repeat_from not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"'repeat_from' must be one of the governed repeat "
                   f"options {allowed} (got {repeat_from!r})")
    batch = _batch_row(session, run.batch_id)
    areas = [s.get("area") for s in batch.area_stamps or []]
    _enforce_not_contaminated(session, run.instrument_id, areas,
                              "repeat a molecular run")
    if repeat_from == "extraction":
        new_batch = MolecularBatch(
            assay_code=batch.assay_code,
            instrument_id=batch.instrument_id,
            operator=user.username, status="open",
            samples=list(batch.samples or []),
            area_stamps=list(batch.area_stamps or []),
            workflow_flagged=batch.workflow_flagged,
            workflow_flag_reason=batch.workflow_flag_reason,
            created_by=user.username)
        session.add(new_batch)
        session.flush()
        append_audit(session, actor=user.username,
                     action="lab.molecular.batch.create",
                     entity=f"MolecularBatch/{new_batch.id}",
                     payload={"assay_code": new_batch.assay_code,
                              "repeat_of_batch_id": batch.id,
                              "repeat_of_run_id": run.id})
        batch = new_batch
    new_run = _create_run(session, user, batch,
                          instrument_id=run.instrument_id,
                          repeat_of=run.id, repeat_from=repeat_from)
    run.repeat_run_id = new_run.id
    run.repeat_from = repeat_from
    session.commit()
    out = _run_out(session, new_run, detail=True)
    out["repeat_of_run_id"] = run.id
    return out


# ---------------------------------------------------------------------------
# Contamination events + decontamination
# ---------------------------------------------------------------------------

@router.post("/contamination-events", status_code=201)
async def create_contamination_event(
        request: Request, session: Session = Depends(get_db),
        user: User = Depends(require_capability("lab.work"))):
    """Manually raise a contamination event (the third trigger,
    alongside NTC-positive and control-failure-pattern). Names the
    instrument and/or area scope; the blast radius (affected runs /
    batches sharing that instrument or those areas) is computed and
    listed on the event."""
    payload = await _json_body(request)
    instrument_id = payload.get("instrument_id")
    areas = payload.get("areas") or []
    if not isinstance(areas, list) or any(
            a not in _area_order(session) for a in areas):
        raise HTTPException(
            status_code=422,
            detail=f"'areas' must be governed unidirectional area "
                   f"codes {sorted(_area_order(session))}")
    source_run = None
    if payload.get("run_id") is not None:
        source_run = _run_row(session, int(payload["run_id"]))
    if not instrument_id and not areas and source_run is None:
        raise HTTPException(
            status_code=422,
            detail="A contamination event must name its scope: "
                   "'instrument_id', 'areas', or 'run_id'")
    event = raise_contamination_event(
        session, trigger="manual", actor=user.username,
        note=payload.get("note"), source_run=source_run,
        instrument_id=str(instrument_id) if instrument_id else None,
        areas=list(areas))
    session.commit()
    return _event_out(event)


@router.get("/contamination-events")
def list_contamination_events(
        session: Session = Depends(get_db),
        user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(select(MolecularContaminationEvent)
                           .order_by(MolecularContaminationEvent.id.asc())
                           ).all()
    return {"items": [_event_out(e) for e in rows], "total": len(rows)}


@router.post("/contamination-events/{event_id}/decontaminate")
async def decontaminate(event_id: int, request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.work"))):
    """Record the decontamination that returns the affected scope
    to service: governed method, the areas decontaminated, and
    verification swabs — EVERY swab must be negative, or the event
    stays open and the scope stays blocked (refused by name)."""
    payload = await _json_body(request)
    event = session.get(MolecularContaminationEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404,
                            detail=f"Contamination event {event_id} "
                                   "not found")
    if event.status == "cleared":
        raise HTTPException(
            status_code=409,
            detail=f"Contamination event {event_id} is already cleared")
    cfg, _ = _cfg(session)
    methods = [m.get("code") for m in
               cfg.get("decontaminationMethods") or []
               if isinstance(m, dict)]
    method = payload.get("method")
    if methods and method not in methods:
        raise HTTPException(
            status_code=422,
            detail=f"'method' must be one of the governed "
                   f"decontamination methods {methods}")
    if not method:
        raise HTTPException(status_code=422,
                            detail="'method' is required")
    areas = payload.get("areas")
    if not isinstance(areas, list) or not areas or any(
            a not in _area_order(session) for a in areas):
        raise HTTPException(
            status_code=422,
            detail=f"'areas' must name the governed area(s) "
                   f"decontaminated {sorted(_area_order(session))}")
    swabs = payload.get("verification_swabs")
    if not isinstance(swabs, list) or not swabs:
        raise HTTPException(
            status_code=422,
            detail="'verification_swabs' must be a non-empty list of "
                   "{area, result} entries — decontamination is only "
                   "verified by swabs")
    for i, swab in enumerate(swabs):
        if not isinstance(swab, dict) or swab.get("area") not in areas \
                or swab.get("result") not in ("negative", "positive"):
            raise HTTPException(
                status_code=422,
                detail=f"verification_swabs[{i}] must be "
                       "{area: <a decontaminated area>, result: "
                       "'negative'|'positive'}")
    positives = [s["area"] for s in swabs if s["result"] == "positive"]
    if positives:
        raise HTTPException(
            status_code=409,
            detail=(f"Decontamination NOT verified: verification swab(s) "
                    f"positive in area(s) {', '.join(positives)} — "
                    f"contamination event {event_id} stays OPEN and its "
                    f"instrument/area scope stays out of service"))
    record = {"method": method, "areas": list(areas),
              "verification_swabs": swabs,
              "note": payload.get("note"),
              "recorded_by": user.username, "recorded_at": _now_iso()}
    event.decontamination = record
    event.status = "cleared"
    event.cleared_by = user.username
    event.cleared_at = _now()
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.molecular.contamination.decontaminate",
                 entity=f"MolecularContaminationEvent/{event_id}",
                 payload={"method": method, "areas": list(areas)})
    session.commit()
    return _event_out(event)
