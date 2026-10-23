"""Lab Depth W3 (LAB-D6) — analyser integration reality.

The laboratory has TWO instrument registries, and this module is
where they stop being silently divergent:

- the LAB registry — app.models.LabInstrument (app.lab_benches),
  statuses active / maintenance / down;
- the DEVICE registry — app.models.Device (app.devices, Phase 14),
  statuses from the published terminology/devices config (seeded:
  active / maintenance / out-of-service / retired), which also owns
  the MLLP sender gate and calibration due-date truth.

RECONCILIATION DESIGN (implemented, not aspirational): statuses are
normalised to one vocabulary (in-service / in-maintenance /
out-of-service); ingest reads the EFFECTIVE status — the most
restrictive status across both registries — through the single
function :func:`effective_status` in this module; any instrument
whose two registries disagree is named on the divergence report
(GET /lab/analyser/registry-divergences); and the one status-write
surface here (POST /lab/analyser/instruments/{id}/status) writes
THROUGH to both registries in their own vocabularies, so a status
change made on this surface can never create a new divergence.

What D6 builds on top of that:

(a) LAB-m2 (safety): EVERY result path that names an instrument —
    HL7 ORU and CSV posts to /lab/ingest, the MLLP listener (via
    app.interop -> app.lab.ingest_hl7_groups), the direct/API result
    entry, and the rerun loop below — passes
    :func:`enforce_ingest_gate` inside app.lab._create_results /
    ingest_hl7_groups BEFORE any result is written. An instrument
    whose effective status is in-maintenance or out-of-service
    produces a NAMED quarantine record (lab_ingest_quarantine) and a
    409 refusal; zero results are written, silently or otherwise.
    Quarantine disposition: `reject` (terminal) or `reprocess` —
    reprocess is allowed ONLY once the instrument is back in service
    and creates NEW draft results at reprocess time through the same
    ingest core; the quarantine row and its payload are never edited
    or backdated (history is not rewritten).
(b) Bidirectional worklist: GET /lab/analyser/worklist/{id} is the
    governed download of what is pending for that analyser (open
    received / in-process orders routed to its bench, plus its
    pending reruns), and analyser FLAGS land on results as
    first-class data (OBX-8 on HL7, a `flags` column on CSV, a
    `flags` list on direct entry) stored as Observation extensions
    that feed D7 autoverification and the rerun queue.
(c) Rerun/dilution loop: results carrying a governed rerun flag
    (terminology/lab-analyser `rerunFlags`) enter the rerun queue;
    submitting a rerun re-enters app.lab._create_results — the SAME
    validation core (catalog validation, QC gate, W4 instrument-path
    lock, reagent-lot lock, delta + autoverification evaluation) —
    after reopening the order's analysis state under audit. There
    is no side door: a rerun on a QC-failed analyte or a locked
    instrument path blocks exactly like a first entry.

HONEST BOUND: this is worklist + flags + rerun over the existing
HL7/CSV/MLLP pipes. Parity with commercial middleware (Data
Innovations Instrument Manager class — driver libraries, connection
management, middleware-resident rule engines) is NOT claimed.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (DateTime, Float, Integer, JSON, String, Text, select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from .audit import append_audit
from .configstore import published_config
from .db import Base
from .models import (ConfigStatus, Device, LabInstrument, StudioConfig,
                     User, utcnow)
from .security import get_db, require_capability

router = APIRouter(prefix="/lab/analyser", tags=["lab-analyser"])

ANALYSER_KIND = "terminology"
ANALYSER_KEY = "lab-analyser"
FLAG_EXTENSION = "urn:opencare:extension/analyser-flag"

#: Status normalisation — the reconciliation vocabulary. Both
#: registries keep their own raw statuses; every consumer in this
#: module (and the ingest gate in app.lab) reads through these maps.
LAB_STATUS_MAP = {"active": "in-service", "maintenance": "in-maintenance",
                  "down": "out-of-service"}
DEVICE_STATUS_MAP = {"active": "in-service",
                     "maintenance": "in-maintenance",
                     "out-of-service": "out-of-service",
                     "retired": "out-of-service"}
_RANK = {"in-service": 0, "in-maintenance": 1, "out-of-service": 2}
#: Normalised -> each registry's own vocabulary (write-through).
_TO_LAB = {"in-service": "active", "in-maintenance": "maintenance",
           "out-of-service": "down"}
_TO_DEVICE = {"in-service": "active", "in-maintenance": "maintenance",
              "out-of-service": "out-of-service"}
BLOCKING = ("in-maintenance", "out-of-service")

DEFAULT_RERUN_FLAGS = ["DIL", "RR", "CLOT"]
DEFAULT_DILUTION_FLAGS = ["DIL"]


# ---------------------------------------------------------------------------
# Own tables: ingest quarantine + rerun queue
# ---------------------------------------------------------------------------

class LabIngestQuarantine(Base):
    """One quarantined ingest batch (LAB-D6a / LAB-m2): results that
    arrived from an instrument whose effective registry status was
    in-maintenance or out-of-service. The batch payload is stored
    verbatim and NEVER edited; disposition is a state change on this
    row (held -> rejected | reprocessed) with the decider, time and
    note, plus the NEW report ids a reprocess created. Zero patient
    results are written for a held batch."""
    __tablename__ = "lab_ingest_quarantine"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    quarantine_id: Mapped[str] = mapped_column(String(120), unique=True,
                                               nullable=False, index=True)
    instrument_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                               index=True)
    source_format: Mapped[str] = mapped_column(String(40), nullable=False,
                                               default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    lab_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    device_status: Mapped[str | None] = mapped_column(String(40),
                                                     nullable=True)
    effective_status: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="held", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reprocessed_report_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class LabRerunItem(Base):
    """One rerun/dilution queue item (LAB-D6c): a flagged result that
    must be re-run. The item completes only by submitting the rerun
    output through app.lab._create_results (the same validation
    core); the rerun report it created is recorded here. A cancelled
    item records who cancelled it and why — it is never deleted."""
    __tablename__ = "lab_rerun_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    order_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                          index=True)
    report_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                           default="")
    analyte_code: Mapped[str] = mapped_column(String(60), nullable=False)
    instrument_id: Mapped[str | None] = mapped_column(String(120),
                                                      nullable=True)
    flags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="pending", index=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    completed_by: Mapped[str | None] = mapped_column(String(120),
                                                     nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    rerun_report_id: Mapped[str | None] = mapped_column(String(120),
                                                        nullable=True)
    measured_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    dilution_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    corrected_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    cancel_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


# ---------------------------------------------------------------------------
# Governed config: terminology/lab-analyser (seeded published v1)
# ---------------------------------------------------------------------------

def default_analyser_payload() -> dict:
    """Governed analyser-integration config: the flag vocabulary
    (concepts), which flags route a result to the rerun queue, and
    which of those demand a dilution protocol. Seeded values are
    governed defaults a site republishes for its own analysers."""
    def flag(code, display):
        return {"code": code, "display": display, "active": True}
    return {
        "name": "OpenCare analyser integration (Lab Depth D6)",
        "system": "urn:opencare:lab-analyser",
        "concepts": [
            flag("HEM", "Haemolysed (HIL haemolysis flag)"),
            flag("ICT", "Icteric (HIL icterus flag)"),
            flag("LIP", "Lipemic (HIL lipemia flag)"),
            flag("CLOT", "Clot detected"),
            flag("QNS", "Quantity not sufficient"),
            flag("DIL", "Dilution required / performed"),
            flag("RR", "Rerun required"),
            flag("H", "High abnormal flag"),
            flag("L", "Low abnormal flag"),
        ],
        "rerunFlags": list(DEFAULT_RERUN_FLAGS),
        "dilutionFlags": list(DEFAULT_DILUTION_FLAGS),
    }


def ensure_lab_analyser_defaults(session: Session) -> None:
    """Seed terminology/lab-analyser as published v1 (insert-only,
    the lab seed pattern). Wired from app.main — see INTEGRATION NOTE."""
    from .configstore import list_config_versions
    if list_config_versions(session, ANALYSER_KIND, ANALYSER_KEY):
        return
    row = StudioConfig(kind=ANALYSER_KIND, key=ANALYSER_KEY, version=1,
                       status=ConfigStatus.published,
                       payload=default_analyser_payload(),
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    for action in ("studio.config.create", "studio.config.publish"):
        append_audit(session, actor="system", action=action,
                     entity=f"config:{ANALYSER_KIND}/{ANALYSER_KEY}@v1",
                     payload={"kind": ANALYSER_KIND, "key": ANALYSER_KEY,
                              "version": 1, "seeded_default": True})
    session.commit()


def analyser_config(session: Session) -> dict:
    cfg = default_analyser_payload()
    published = published_config(session, ANALYSER_KIND, ANALYSER_KEY)
    if published is not None and isinstance(published.payload, dict):
        for key, value in published.payload.items():
            cfg[key] = value
    return cfg


def rerun_flags(session: Session) -> list[str]:
    flags = analyser_config(session).get("rerunFlags")
    return [str(f) for f in flags] if isinstance(flags, list) \
        else list(DEFAULT_RERUN_FLAGS)


# ---------------------------------------------------------------------------
# Registry reconciliation: one effective status, divergences named
# ---------------------------------------------------------------------------

def _lab_row(session: Session, instrument_id: str) -> LabInstrument | None:
    return session.scalars(select(LabInstrument).where(
        LabInstrument.instrument_id == instrument_id)).first()


def _device_row(session: Session, instrument_id: str) -> Device | None:
    return session.scalars(select(Device).where(
        Device.device_key == instrument_id)).first()


def registry_statuses(session: Session, instrument_id: str) -> dict:
    """Both registries' raw + normalised statuses for one instrument,
    the EFFECTIVE status (most restrictive wins), and whether the
    registries diverge. An instrument known to neither registry is
    `known: False` with effective None — the gate treats unknown as
    unblocked (registration itself is enforced by the ingest
    endpoint, as before), but the surface below never hides it."""
    lab_row = _lab_row(session, instrument_id)
    device_row = _device_row(session, instrument_id)
    lab_norm = LAB_STATUS_MAP.get(lab_row.status) if lab_row else None
    dev_norm = DEVICE_STATUS_MAP.get(device_row.status) if device_row else None
    known_norms = [n for n in (lab_norm, dev_norm) if n is not None]
    effective = max(known_norms, key=lambda n: _RANK[n]) if known_norms else None
    divergent = lab_norm is not None and dev_norm is not None \
        and lab_norm != dev_norm
    return {"instrument_id": instrument_id,
            "known": lab_row is not None or device_row is not None,
            "in_lab_registry": lab_row is not None,
            "in_device_registry": device_row is not None,
            "lab_status": lab_row.status if lab_row else None,
            "device_status": device_row.status if device_row else None,
            "lab_status_normalised": lab_norm,
            "device_status_normalised": dev_norm,
            "effective_status": effective,
            "divergent": divergent,
            "bench": lab_row.bench if lab_row else None,
            "name": (lab_row.name if lab_row else
                     device_row.name if device_row else None),
            "device_type": device_row.device_type if device_row else None}


def effective_status(session: Session, instrument_id: str) -> str | None:
    """THE status the ingest gate reads — the single source every
    ingest path consults (the reconciliation contract)."""
    return registry_statuses(session, instrument_id)["effective_status"]


def instrument_known(session: Session, instrument_id: str) -> bool:
    return registry_statuses(session, instrument_id)["known"]


def all_instrument_ids(session: Session) -> list[str]:
    ids = {r.instrument_id for r in session.scalars(select(LabInstrument)).all()}
    ids |= {d.device_key for d in session.scalars(select(Device)).all()}
    return sorted(ids)


def divergences(session: Session) -> list[dict]:
    """The explicit cross-check: every instrument whose two
    registries disagree on normalised status, named with both raw
    statuses and the effective status ingest will use."""
    return [s for s in (registry_statuses(session, i)
                        for i in all_instrument_ids(session))
            if s["divergent"]]


def ingest_block_reason(session: Session, instrument_id: str | None) -> str | None:
    """Why ingest from this instrument is refused right now, or None.
    Blocking = effective status in-maintenance or out-of-service
    (LAB-m2), read across BOTH registries via the reconciliation."""
    if not instrument_id:
        return None
    status = registry_statuses(session, instrument_id)
    if status["effective_status"] in BLOCKING:
        sources = []
        if status["in_lab_registry"]:
            sources.append(f"lab registry status '{status['lab_status']}'")
        if status["in_device_registry"]:
            sources.append(f"device registry status '{status['device_status']}'")
        return (f"instrument '{instrument_id}' is "
                f"{status['effective_status']} ({'; '.join(sources)}); "
                "results from an instrument that is in maintenance or "
                "out of service are quarantined, never written")
    return None


def enforce_ingest_gate(session: Session, instrument_id: str | None, *,
                        source_format: str, payload: dict,
                        actor: str = "system:analyser-gate") -> None:
    """The LAB-m2 gate, called from app.lab BEFORE any result write.
    On a blocking status: a named quarantine row is created and
    COMMITTED (so the refusal record survives the 409, the devices
    quarantine precedent), then HTTPException(409) is raised naming
    the quarantine id, the instrument and both registry statuses.
    Zero results are written for the batch."""
    reason = ingest_block_reason(session, instrument_id)
    if reason is None:
        return
    status = registry_statuses(session, str(instrument_id))
    row = LabIngestQuarantine(
        quarantine_id=f"liq-{uuid.uuid4().hex[:12]}",
        instrument_id=str(instrument_id), source_format=source_format,
        reason=reason, lab_status=status["lab_status"],
        device_status=status["device_status"],
        effective_status=status["effective_status"] or "",
        payload=payload, status="held")
    session.add(row)
    session.flush()
    append_audit(session, actor=actor, action="lab.analyser.quarantine",
                 entity=f"LabInstrument/{instrument_id}",
                 payload={"quarantine_id": row.quarantine_id,
                          "source_format": source_format,
                          "effective_status": row.effective_status,
                          "lab_status": row.lab_status,
                          "device_status": row.device_status,
                          "reason": reason})
    session.commit()
    raise HTTPException(
        status_code=409,
        detail=(f"Ingest quarantined ({row.quarantine_id}): {reason}. "
                "The batch is held in the analyser quarantine queue — "
                "no results were written."))


# ---------------------------------------------------------------------------
# Rerun queue registration (called from app.lab._create_results)
# ---------------------------------------------------------------------------

def register_rerun_items(session: Session, *, order_id: str, report_id: str,
                         checked: list[dict], instrument_id: str | None,
                         actor: str) -> list[str]:
    """Route flagged results to the rerun queue: one pending item per
    resulted analyte carrying a governed rerun flag. Called by
    app.lab._create_results in the caller's transaction (the caller
    commits). Returns the created item ids."""
    triggers = set(rerun_flags(session))
    created = []
    for entry in checked:
        flags = [str(f) for f in (entry.get("flags") or [])]
        if not (set(flags) & triggers):
            continue
        item = LabRerunItem(
            item_id=f"rerun-{uuid.uuid4().hex[:12]}",
            order_id=order_id, report_id=report_id,
            analyte_code=entry["concept"]["code"],
            instrument_id=instrument_id, flags=flags, status="pending",
            created_by=actor)
        session.add(item)
        session.flush()
        created.append(item.item_id)
    if created:
        append_audit(session, actor=actor, action="lab.analyser.rerun.queue",
                     entity=f"ServiceRequest/{order_id}",
                     payload={"item_ids": created, "report_id": report_id,
                              "instrument_id": instrument_id})
        session.flush()
    return created


# ---------------------------------------------------------------------------
# HTTP helpers + serialisers
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


def _quarantine_out(row: LabIngestQuarantine) -> dict:
    return {"id": row.id, "quarantine_id": row.quarantine_id,
            "instrument_id": row.instrument_id,
            "source_format": row.source_format, "reason": row.reason,
            "lab_status": row.lab_status, "device_status": row.device_status,
            "effective_status": row.effective_status,
            "payload": row.payload or {}, "status": row.status,
            "decided_by": row.decided_by,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            "decision_note": row.decision_note,
            "reprocessed_report_ids": list(row.reprocessed_report_ids or []),
            "created_at": row.created_at.isoformat() if row.created_at else None}


def _rerun_out(row: LabRerunItem) -> dict:
    return {"id": row.id, "item_id": row.item_id, "order_id": row.order_id,
            "report_id": row.report_id, "analyte_code": row.analyte_code,
            "instrument_id": row.instrument_id, "flags": list(row.flags or []),
            "status": row.status, "created_by": row.created_by,
            "completed_by": row.completed_by,
            "completed_at": row.completed_at.isoformat()
            if row.completed_at else None,
            "rerun_report_id": row.rerun_report_id,
            "measured_value": row.measured_value,
            "dilution_factor": row.dilution_factor,
            "corrected_value": row.corrected_value,
            "cancel_note": row.cancel_note,
            "created_at": row.created_at.isoformat() if row.created_at else None}


def _instrument_out(session: Session, instrument_id: str) -> dict:
    status = registry_statuses(session, instrument_id)
    from . import lab_qc
    status["calibration"] = lab_qc.calibration_posture(session, instrument_id) \
        if status["known"] else None
    status["ingest_blocked"] = status["effective_status"] in BLOCKING
    status["block_reason"] = ingest_block_reason(session, instrument_id)
    return status


# ---------------------------------------------------------------------------
# Endpoints: registries + reconciliation
# ---------------------------------------------------------------------------

@router.get("/instruments")
def list_instruments(session: Session = Depends(get_db),
                     user: User = Depends(require_capability("lab.read"))):
    """The reconciled instrument view: the union of both registries,
    each instrument with both raw statuses, the effective status
    ingest uses, its divergence flag, and the W4 calibration posture."""
    items = [_instrument_out(session, i) for i in all_instrument_ids(session)]
    return {"items": items, "total": len(items),
            "divergent": sum(1 for i in items if i["divergent"]),
            "ingest_blocked": sum(1 for i in items if i["ingest_blocked"]),
            "reconciliation": (
                "Effective status = the most restrictive normalised "
                "status across the lab registry (LabInstrument) and "
                "the device registry (Device); ingest reads only the "
                "effective status. Divergent instruments are named "
                "here and on /lab/analyser/registry-divergences.")}


@router.get("/registry-divergences")
def registry_divergences(session: Session = Depends(get_db),
                         user: User = Depends(require_capability("lab.read"))):
    """The explicit registry cross-check: instruments whose lab and
    device registries disagree, with both statuses and the effective
    status the ingest gate will use. Empty = the registries agree."""
    items = divergences(session)
    return {"items": items, "total": len(items)}


@router.post("/instruments/{instrument_id}/status")
async def set_instrument_status(instrument_id: str, request: Request,
                                session: Session = Depends(get_db),
                                user: User = Depends(
                                    require_capability("lab.work"))):
    """Set an instrument's status — the reconciled write surface: the
    normalised status (in-service | in-maintenance | out-of-service;
    raw registry synonyms accepted) is written THROUGH to every
    registry that knows the instrument, in that registry's own
    vocabulary, in one transaction. A status change here can never
    create a divergence."""
    payload = await _json_body(request)
    raw = str(payload.get("status") or "").strip()
    normalised = LAB_STATUS_MAP.get(raw) or DEVICE_STATUS_MAP.get(raw) \
        or (raw if raw in _RANK else None)
    if normalised is None:
        raise HTTPException(
            status_code=422,
            detail=("'status' must be one of in-service / in-maintenance / "
                    "out-of-service (or a registry synonym: active, "
                    "maintenance, down, retired)"))
    status = registry_statuses(session, instrument_id)
    if not status["known"]:
        raise HTTPException(
            status_code=404,
            detail=(f"Instrument '{instrument_id}' is in neither the lab "
                    "instrument registry nor the device registry"))
    lab_row = _lab_row(session, instrument_id)
    if lab_row is not None:
        lab_row.status = _TO_LAB[normalised]
    device_row = _device_row(session, instrument_id)
    if device_row is not None:
        device_row.status = _TO_DEVICE[normalised]
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.analyser.instrument.status",
                 entity=f"LabInstrument/{instrument_id}",
                 payload={"to_normalised": normalised,
                          "lab_status": _TO_LAB[normalised]
                          if lab_row is not None else None,
                          "device_status": _TO_DEVICE[normalised]
                          if device_row is not None else None,
                          "note": payload.get("note") or ""})
    session.commit()
    return _instrument_out(session, instrument_id)


# ---------------------------------------------------------------------------
# Endpoints: worklist download (bidirectional — orders down to the
# analyser; flags + results come back up through the ingest paths)
# ---------------------------------------------------------------------------

@router.get("/worklist/{instrument_id}")
def worklist(instrument_id: str, session: Session = Depends(get_db),
             user: User = Depends(require_capability("lab.read"))):
    """The governed worklist download for one analyser: open orders
    (specimen received / in-process) routed to the instrument's bench
    by the published catalog, plus its pending rerun items. An
    instrument with no lab-registry bench has no routed worklist —
    the response says so instead of inventing one."""
    from . import lab as lab_mod
    from .models import FhirResource
    status = registry_statuses(session, instrument_id)
    if not status["known"]:
        raise HTTPException(
            status_code=404,
            detail=(f"Instrument '{instrument_id}' is in neither the lab "
                    "instrument registry nor the device registry"))
    bench = status["bench"]
    items = []
    if bench:
        rows = session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "ServiceRequest")
            .order_by(FhirResource.created_at.asc(),
                      FhirResource.id.asc())).all()
        for row in rows:
            order_status = (row.body or {}).get("status")
            if order_status not in ("received", "in-process"):
                continue
            if lab_mod._order_bench(session, row) != bench:
                continue
            specimen = lab_mod._specimen_row(session, row)
            items.append({
                "order_id": row.resource_id, "status": order_status,
                "code": lab_mod._code_of(row.body or {}),
                "patient_ref": (row.body or {}).get("subject", {})
                .get("reference"),
                "priority": (row.body or {}).get("priority"),
                "specimen_id": specimen.resource_id if specimen else None,
                "accession": ((specimen.body or {}).get("accessionIdentifier")
                              or {}).get("value") if specimen else None})
    reruns = session.scalars(
        select(LabRerunItem).where(LabRerunItem.instrument_id == instrument_id,
                                   LabRerunItem.status == "pending")
        .order_by(LabRerunItem.id.asc())).all()
    return {"instrument_id": instrument_id, "bench": bench,
            "effective_status": status["effective_status"],
            "ingest_blocked": status["effective_status"] in BLOCKING,
            "items": items, "total": len(items),
            "rerun_items": [_rerun_out(r) for r in reruns],
            "note": ("Worklist = orders whose catalog bench matches the "
                     "instrument's lab-registry bench, in received / "
                     "in-process state."
                     if bench else
                     "This instrument has no lab-registry bench, so no "
                     "catalogue work routes to it; register it in the "
                     "lab instrument registry with a bench to receive a "
                     "worklist.")}


# ---------------------------------------------------------------------------
# Endpoints: quarantine disposition
# ---------------------------------------------------------------------------

@router.get("/quarantine")
def list_quarantine(status: str | None = Query(default=None),
                    instrument_id: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability("lab.read"))):
    stmt = select(LabIngestQuarantine).order_by(LabIngestQuarantine.id.desc())
    if status:
        stmt = stmt.where(LabIngestQuarantine.status == status)
    if instrument_id:
        stmt = stmt.where(LabIngestQuarantine.instrument_id == instrument_id)
    rows = session.scalars(stmt).all()
    return {"items": [_quarantine_out(r) for r in rows], "total": len(rows),
            "held": sum(1 for r in rows if r.status == "held")}


def _quarantine_row(session: Session, quarantine_id: str) -> LabIngestQuarantine:
    row = session.scalars(select(LabIngestQuarantine).where(
        LabIngestQuarantine.quarantine_id == quarantine_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Quarantine batch '{quarantine_id}' not found")
    return row


@router.post("/quarantine/{quarantine_id}/reject")
async def reject_quarantine(quarantine_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(
                                require_capability("lab.work"))):
    """Reject a held batch (terminal): the results will never land.
    The row and its payload stay as the record of what was refused."""
    payload = await _json_body(request)
    note = payload.get("note")
    if not isinstance(note, str) or not note.strip():
        raise HTTPException(status_code=422,
                            detail="Rejecting a quarantined batch requires "
                                   "'note' (the disposition reason)")
    row = _quarantine_row(session, quarantine_id)
    if row.status != "held":
        raise HTTPException(
            status_code=409,
            detail=f"Quarantine batch '{quarantine_id}' is already {row.status}")
    row.status = "rejected"
    row.decided_by = user.username
    row.decided_at = datetime.now(timezone.utc)
    row.decision_note = note.strip()
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.analyser.quarantine.reject",
                 entity=f"LabInstrument/{row.instrument_id}",
                 payload={"quarantine_id": row.quarantine_id,
                          "note": row.decision_note})
    session.commit()
    return _quarantine_out(row)


@router.post("/quarantine/{quarantine_id}/reprocess")
async def reprocess_quarantine(quarantine_id: str, request: Request,
                               session: Session = Depends(get_db),
                               user: User = Depends(
                                   require_capability("lab.work"))):
    """Reprocess a held batch — ONLY when the instrument is back in
    service (its effective status across both registries). The stored
    payload re-enters the SAME ingest core and creates NEW draft
    results timestamped now; the quarantine row keeps its original
    payload and timestamps (history is not rewritten). While the
    instrument is still down this refuses 409 and the batch stays
    held."""
    payload = await _json_body(request)
    row = _quarantine_row(session, quarantine_id)
    if row.status != "held":
        raise HTTPException(
            status_code=409,
            detail=f"Quarantine batch '{quarantine_id}' is already {row.status}")
    reason = ingest_block_reason(session, row.instrument_id)
    if reason is not None:
        raise HTTPException(
            status_code=409,
            detail=(f"Cannot reprocess: {reason}. Reprocessing is only "
                    "allowed once the instrument is back in service."))
    from . import lab as lab_mod
    stored = row.payload or {}
    kind = stored.get("kind")
    created_reports: list[str] = []
    try:
        if kind == "groups":
            groups = [(g[0], g[1], g[2]) for g in stored.get("groups") or []]
            created = lab_mod.ingest_hl7_groups(
                session, user, groups,
                source_format=row.source_format or "hl7",
                instrument_id=row.instrument_id)
            created_reports = [c["report_id"] for c in created]
        elif kind == "results":
            order = lab_mod._order_row(session, stored["order_id"])
            outcome = lab_mod._create_results(
                session, user, order, stored.get("results"),
                source=f"reprocess-{row.source_format}",
                instrument_id=row.instrument_id)
            created_reports = [outcome["report"]["id"]]
        else:
            raise HTTPException(
                status_code=409,
                detail=f"Quarantine batch '{quarantine_id}' carries an "
                       f"unrecognised payload kind '{kind}'; it can be "
                       "rejected but not reprocessed")
    except HTTPException as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(f"Reprocessing still cannot land this batch: "
                    f"{exc.detail}"))
    row.status = "reprocessed"
    row.decided_by = user.username
    row.decided_at = datetime.now(timezone.utc)
    note = payload.get("note")
    row.decision_note = note if isinstance(note, str) else None
    row.reprocessed_report_ids = created_reports
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.analyser.quarantine.reprocess",
                 entity=f"LabInstrument/{row.instrument_id}",
                 payload={"quarantine_id": row.quarantine_id,
                          "report_ids": created_reports,
                          "note": row.decision_note})
    session.commit()
    return _quarantine_out(row)


# ---------------------------------------------------------------------------
# Endpoints: rerun queue
# ---------------------------------------------------------------------------

@router.get("/rerun-queue")
def rerun_queue(status: str | None = Query(default="pending"),
                session: Session = Depends(get_db),
                user: User = Depends(require_capability("lab.read"))):
    stmt = select(LabRerunItem).order_by(LabRerunItem.id.asc())
    if status:
        stmt = stmt.where(LabRerunItem.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_rerun_out(r) for r in rows], "total": len(rows)}


def _rerun_row(session: Session, item_id: str) -> LabRerunItem:
    row = session.scalars(select(LabRerunItem).where(
        LabRerunItem.item_id == item_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Rerun item '{item_id}' not found")
    return row


@router.post("/rerun/{item_id}/submit")
async def submit_rerun(item_id: str, request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("lab.work"))):
    """Submit rerun/dilution output for one queue item. The output
    re-enters app.lab._create_results — the SAME validation core as
    first entry (catalog validation, QC gate, W4 instrument-path
    lock, reagent-lot lock, delta + autoverification) — so a rerun
    result on a QC-failed analyte or from a down instrument blocks
    exactly like a first entry, and the item stays pending. With a
    dilution_factor, the reported value is measured x factor and both
    are recorded on the item. The order's analysis state is reopened
    under audit (resulted/verified -> in-process) because the spine
    only takes results for received/in-process orders; the original
    flagged report is never edited."""
    payload = await _json_body(request)
    item = _rerun_row(session, item_id)
    if item.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Rerun item '{item_id}' is already {item.status}")
    measured = payload.get("value")
    if isinstance(measured, bool) or not isinstance(measured, (int, float)):
        raise HTTPException(status_code=422,
                            detail="Rerun requires numeric 'value' (the "
                                   "measured rerun output)")
    factor = payload.get("dilution_factor")
    if factor is not None:
        if isinstance(factor, bool) or not isinstance(factor, (int, float)) \
                or float(factor) <= 0:
            raise HTTPException(status_code=422,
                                detail="'dilution_factor' must be a "
                                       "positive number")
        factor = float(factor)
    corrected = float(measured) * (factor or 1.0)
    instrument_id = payload.get("instrument_id") or item.instrument_id
    flags = payload.get("flags")
    flags = [str(f) for f in flags] if isinstance(flags, list) else []
    from . import lab as lab_mod
    # Pre-checks BEFORE the order is reopened, so a blocked rerun
    # leaves the order, the report and the queue item exactly as they
    # were: (1) the LAB-m2 instrument gate (quarantines by name when
    # the instrument is down — the same gate _create_results runs);
    # (2) the report-level QC gate for the analyte; (3) the W4
    # instrument-path lock. _create_results re-runs (2)'s core gates
    # at verify/release and (1)+(3) before writing, identically.
    enforce_ingest_gate(
        session, instrument_id, source_format="rerun",
        actor=user.username,
        payload={"kind": "results", "order_id": item.order_id,
                 "results": [{"code": item.analyte_code,
                              "value": corrected, "flags": flags}]})
    qc_blocks = lab_mod.qc_blockers(session, [item.analyte_code])
    if qc_blocks:
        raise HTTPException(
            status_code=409,
            detail=("Rerun blocked by the QC gate: "
                    + lab_mod._qc_block_message(qc_blocks)
                    + " — the rerun output cannot be accepted until a "
                      "passing QC run posts"))
    if instrument_id:
        from . import lab_qc
        lab_qc.enforce_instrument_path(session, instrument_id,
                                       [item.analyte_code])
    order = lab_mod._order_row(session, item.order_id)
    order_status = (order.body or {}).get("status")
    if order_status == "released":
        raise HTTPException(
            status_code=409,
            detail=(f"Order {item.order_id} is already released; a rerun "
                    "of a released result runs through the amended-report "
                    "path, not the rerun queue"))
    if order_status in ("resulted", "verified"):
        lab_mod._set_status(order, "in-process")
        specimen = lab_mod._specimen_row(session, order)
        if specimen is not None:
            lab_mod._set_status(specimen, "in-process")
        session.flush()
        append_audit(session, actor=user.username,
                     action="lab.analyser.rerun.reopen",
                     entity=f"ServiceRequest/{order.resource_id}",
                     payload={"item_id": item.item_id,
                              "from_status": order_status,
                              "analyte_code": item.analyte_code})
        session.commit()
    outcome = lab_mod._create_results(
        session, user, order,
        [{"code": item.analyte_code, "value": corrected, "flags": flags}],
        source="rerun", instrument_id=instrument_id)
    item.status = "completed"
    item.completed_by = user.username
    item.completed_at = datetime.now(timezone.utc)
    item.rerun_report_id = outcome["report"]["id"]
    item.measured_value = float(measured)
    item.dilution_factor = factor
    item.corrected_value = corrected
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.analyser.rerun.submit",
                 entity=f"ServiceRequest/{order.resource_id}",
                 payload={"item_id": item.item_id,
                          "analyte_code": item.analyte_code,
                          "measured_value": float(measured),
                          "dilution_factor": factor,
                          "corrected_value": corrected,
                          "rerun_report_id": item.rerun_report_id})
    session.commit()
    return {"item": _rerun_out(item), "outcome": {
        "report_id": outcome["report"]["id"],
        "critical": outcome["critical"],
        "auto_verified": outcome.get("auto_verified", False),
        "auto_released": outcome.get("auto_released", False)}}


@router.post("/rerun/{item_id}/cancel")
async def cancel_rerun(item_id: str, request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("lab.work"))):
    """Cancel a pending rerun item with a named reason (the item is
    kept as the record; it is never deleted)."""
    payload = await _json_body(request)
    note = payload.get("note")
    if not isinstance(note, str) or not note.strip():
        raise HTTPException(status_code=422,
                            detail="Cancelling a rerun item requires 'note'")
    item = _rerun_row(session, item_id)
    if item.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Rerun item '{item_id}' is already {item.status}")
    item.status = "cancelled"
    item.completed_by = user.username
    item.completed_at = datetime.now(timezone.utc)
    item.cancel_note = note.strip()
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.analyser.rerun.cancel",
                 entity=f"ServiceRequest/{item.order_id}",
                 payload={"item_id": item.item_id, "note": item.cancel_note})
    session.commit()
    return _rerun_out(item)


@router.get("/config")
def get_config(session: Session = Depends(get_db),
               user: User = Depends(require_capability("lab.read"))):
    """The effective governed analyser config (flag vocabulary, rerun
    + dilution flag lists) with its Studio surface."""
    cfg = published_config(session, ANALYSER_KIND, ANALYSER_KEY)
    return {"config": {"kind": ANALYSER_KIND, "key": ANALYSER_KEY,
                       "version": cfg.version if cfg else None,
                       "publish_in": ("System Studio -> Terminology -> "
                                      "lab-analyser")},
            "payload": analyser_config(session),
            "honest_bound": ("Worklist + flags + rerun over the existing "
                             "HL7/CSV/MLLP pipes; parity with commercial "
                             "middleware (Data Innovations class) is NOT "
                             "claimed.")}
