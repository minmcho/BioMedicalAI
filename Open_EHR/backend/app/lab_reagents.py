"""Lab Depth W4 (LAB-D9) — reagent lot lifecycle in the lab.

The lab-side half of lot management (research report §1.7 / §3.4;
INVENTORY_DEPTH_MODEL §4). Inventory owns the stock state machine
and the release gate; the laboratory owns the PROTOCOL that
satisfies it. Neither duplicates the other:

- **Lot-to-lot verification protocol.** A run for one inventory lot
  carrying parallel patient-sample pairs (old lot vs new lot,
  spanning the range), QC results on BOTH lots, and a comparison
  computed server-side against DIRECTOR-CONFIGURED acceptance
  criteria in the governed Studio config (terminology/
  lab-reagent-qc, app.lab_qc.verification_criteria — per analyte:
  max bias % and/or absolute, minimum sample count). The seeded
  criteria are labelled GOVERNED DEFAULTS for the director to set;
  there is NO universal ±10% standard and none is presented as one.
  The accept/reject decision is signed by an authorised role
  (lab.verify). On acceptance the signed record drives Inventory's
  OWN verification surface — POST /inventory/lots/verify then
  POST /inventory/lots/stage (QUARANTINE -> RELEASED) — invoked
  through inventory's existing handler functions, never by writing
  inventory tables from here. On rejection a FAIL verification is
  stamped through the same surface and the lot stays quarantined.
- **Control-lot onboarding.** QC control materials are real lot
  records (LabControlLot): collecting -> established -> live. A
  governed run count (default 20) of establishment QC runs against
  the lot establishes its mean/SD; before the lot is LIVE it refuses
  use as a patient-QC material by name (enforced in app.lab's QC
  creation), and its establishment runs never satisfy the patient
  QC gates (app.lab._latest_qc excludes them until the lot is live).
  LabQcRun rows stamp the control-lot identity (control_lot_id);
  `control_material` remains the human-facing lot key.
- **Expiry/stage lockout at result entry.** app.lab._create_results
  calls reagent_lot_blocker() below BEFORE any write: a result
  entered against a quarantined, expired, recalled or
  failed-verification lot refuses BY NAME with zero writes.
- **Lot -> result traceability.** Result entry stamps every
  Observation (and its report) with the reagent lot + instrument
  (extensions below). GET /lab/reagent-lots/results enumerates every
  result produced with a given lot — the recall blast radius — and
  links each report to the EXISTING amended-report machinery
  (POST /lab/reports/{id}/amend, CLIA §493.1291(k)); no second
  correction system is built here.

JIT POSITION (explicit, matching Inventory Depth's stated
position): reagent release has NO emergency bypass. There is no
emergency-release pathway, override flag, or pressure route in
this module or in the result-entry lockout: a quarantined or
unverified lot cannot be used under any pressure pathway, full
stop. A laboratory that is about to stock out must verify a lot
through this protocol (or use an already-released lot) — urgency
changes the queue order, never the gate.

Governed Studio surfaces: terminology/lab-reagent-qc (criteria,
control-lot run count, shifts, calibration — app.lab_qc);
inventory/taxonomy class policy (verificationRequired /
verificationValidityDays — Inventory's gate, read through its
own handlers); terminology/lab-catalog (analyte validity).
"""
import json as _json
import statistics
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (Boolean, DateTime, Float, Integer, JSON, String,
                        Text, select)
from sqlalchemy.orm import Mapped, Session, mapped_column
from starlette.requests import Request as StarletteRequest

from . import inventory as inventory_mod
from . import lab_qc
from .audit import append_audit
from .db import Base
from .inventory import InventoryStockLot
from .models import FhirResource, LabQcRun, User, utcnow
from .security import get_db, require_capability

router = APIRouter(prefix="/lab", tags=["lab_reagents"])

REAGENT_LOT_EXTENSION = "urn:opencare:extension/reagent-lot"
INSTRUMENT_EXTENSION = "urn:opencare:extension/instrument"

#: Stages from which a lot may never produce a patient result.
_BLOCKING_STAGES = ("quarantine", "recalled", "received", "disposed",
                    "expired")


def lot_key_for(sku: str, location_code: str, lot_number: str) -> str:
    """The canonical reagent-lot key stamped on results and QC runs:
    sku|location_code|lot_number (the inventory lot identity)."""
    return f"{sku}|{location_code}|{lot_number}"


# ---------------------------------------------------------------------------
# Own tables
# ---------------------------------------------------------------------------

class LabLotVerificationRun(Base):
    """One lot-to-lot verification protocol run (CLSI EP26-A
    pattern): the evidence (sample pairs + QC on both lots), the
    governed criteria it was judged against (snapshotted), the
    server-computed comparison, and the signed decision. On
    acceptance, `inventory_verification` records what Inventory's
    own surface stamped and `inventory_stage` the transition it
    performed — this row is the lab's signed protocol record, not
    a second stock truth."""
    __tablename__ = "lab_lot_verification_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    location_code: Mapped[str] = mapped_column(String(120), nullable=False)
    lot_number: Mapped[str] = mapped_column(String(120), nullable=False)
    analyte_code: Mapped[str] = mapped_column(String(60), nullable=False)
    outgoing_lot_number: Mapped[str | None] = mapped_column(String(120),
                                                            nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="collecting")
    sample_pairs: Mapped[list] = mapped_column(JSON, nullable=False,
                                               default=list)
    qc_old_lot: Mapped[list] = mapped_column(JSON, nullable=False,
                                             default=list)
    qc_new_lot: Mapped[list] = mapped_column(JSON, nullable=False,
                                             default=list)
    criteria: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    comparison: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    inventory_verification: Mapped[dict | None] = mapped_column(
        JSON, nullable=True)
    inventory_stage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


class LabControlLot(Base):
    """One QC control-material lot: collecting -> established ->
    live. `required_runs` snapshots the governed count at
    registration; established mean/SD are computed server-side from
    the establishment LabQcRun rows stamped with this lot's id."""
    __tablename__ = "lab_control_lots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lot_key: Mapped[str] = mapped_column(String(200), unique=True,
                                         nullable=False, index=True)
    analyte_code: Mapped[str] = mapped_column(String(60), nullable=False)
    level: Mapped[str | None] = mapped_column(String(40), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False,
                                       default="collecting")
    required_runs: Mapped[int] = mapped_column(Integer, nullable=False,
                                                default=20)
    established_mean: Mapped[float | None] = mapped_column(Float,
                                                           nullable=True)
    established_sd: Mapped[float | None] = mapped_column(Float, nullable=True)
    established_by: Mapped[str | None] = mapped_column(String(120),
                                                       nullable=True)
    established_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    live_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    inventory_sku: Mapped[str | None] = mapped_column(String(120),
                                                      nullable=True)
    inventory_lot_number: Mapped[str | None] = mapped_column(String(120),
                                                             nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


# ---------------------------------------------------------------------------
# Helpers
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


def _require_str(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"'{field}' is required")
    return value.strip()


def _find_inventory_lot(session: Session, sku: str, location_code: str,
                        lot_number: str) -> InventoryStockLot | None:
    return session.scalars(
        select(InventoryStockLot).where(
            InventoryStockLot.sku == sku,
            InventoryStockLot.location_code == location_code,
            InventoryStockLot.lot_number == lot_number)).first()


def _lot_out_of_inventory(session: Session,
                          lot: InventoryStockLot) -> dict:
    return {"sku": lot.sku, "location_code": lot.location_code,
            "lot_number": lot.lot_number,
            "stage": getattr(lot, "stage", None) or "released",
            "expiry_date": lot.expiry_date,
            "verification_result": getattr(lot, "verification_result", None),
            "verified_by": getattr(lot, "verified_by", None),
            "verified_on": getattr(lot, "verified_on", None)}


def _inventory_request(payload: dict) -> StarletteRequest:
    """A request object carrying `payload` as its JSON body, for
    invoking Inventory's existing lot handlers in-process (the same
    functions its HTTP routes run — policy checks, audit and ledger
    writes included)."""
    body = _json.dumps(payload).encode()
    scope = {"type": "http", "method": "POST", "path": "/inventory/lots",
             "headers": [(b"content-type", b"application/json"),
                         (b"content-length", str(len(body)).encode())]}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return StarletteRequest(scope, receive)


# ---------------------------------------------------------------------------
# Result-entry lockout + control-lot gating (called from app.lab)
# ---------------------------------------------------------------------------

def reagent_lot_blocker(session: Session, reagent_lot) -> str | None:
    """Why a result may not be entered against this reagent lot, or
    None when the lot is usable. NAMED refusals for every blocking
    state: quarantined, expired, recalled, failed verification —
    plus any other non-released stage. NO emergency bypass exists:
    no caller, flag or pathway skips this check (see the module
    docstring's JIT position)."""
    if reagent_lot is None:
        return None
    if not isinstance(reagent_lot, dict):
        return ("'reagent_lot' must be an object naming the inventory "
                "lot {sku, location_code, lot_number}")
    sku = reagent_lot.get("sku")
    location_code = reagent_lot.get("location_code")
    lot_number = reagent_lot.get("lot_number")
    if not all(isinstance(v, str) and v.strip()
               for v in (sku, location_code, lot_number)):
        return ("'reagent_lot' must name sku, location_code and "
                "lot_number (the inventory lot identity)")
    lot = _find_inventory_lot(session, sku.strip(), location_code.strip(),
                               lot_number.strip())
    if lot is None:
        return (f"reagent lot '{lot_number}' of '{sku}' at "
                f"'{location_code}' was not found in inventory — result "
                "entry refused (an unregistered lot cannot produce "
                "patient results)")
    stage = getattr(lot, "stage", None) or "released"
    label = f"reagent lot '{lot.lot_number}' of '{sku}'"
    if stage == "quarantine":
        return (f"{label} is QUARANTINED — it has not been released "
                "through lot-to-lot verification; result entry refused. "
                "There is no emergency bypass for a quarantined lot")
    if stage == "recalled":
        return (f"{label} is RECALLED — result entry refused; results "
                "already produced with this lot are enumerated at "
                "GET /lab/reagent-lots/results")
    if stage in _BLOCKING_STAGES:
        return (f"{label} is at stage '{stage}' (not released) — "
                "result entry refused")
    if stage not in ("released", "in_use"):
        return (f"{label} is at stage '{stage}' — result entry refused "
                "(only released / in-use lots may produce results)")
    if lot.expiry_date:
        try:
            expired = date.fromisoformat(lot.expiry_date) \
                < datetime.now(timezone.utc).date()
        except ValueError:
            expired = True
        if expired:
            return (f"{label} is EXPIRED (expiry_date "
                    f"{lot.expiry_date}) — result entry refused")
    if getattr(lot, "verification_result", None) == "fail":
        return (f"{label} has a FAILED lot verification on record "
                f"(verified_by {lot.verified_by}, verified_on "
                f"{lot.verified_on}) — result entry refused until a "
                "later verification passes")
    return None


def enforce_reagent_lot(session: Session, reagent_lot) -> None:
    """409 by name when the reagent lot blocks result entry. Called
    from app.lab._create_results BEFORE any result is written, so a
    refusal leaves zero writes behind."""
    blocker = reagent_lot_blocker(session, reagent_lot)
    if blocker:
        raise HTTPException(status_code=409, detail=blocker)


def control_lot_for(session: Session, material: str) -> LabControlLot | None:
    return session.scalars(
        select(LabControlLot).where(LabControlLot.lot_key == material)).first()


def control_lot_by_id(session: Session,
                      lot_id: int | None) -> LabControlLot | None:
    if lot_id is None:
        return None
    return session.scalars(
        select(LabControlLot).where(LabControlLot.id == lot_id)).first()


def control_lot_use_blocker(session: Session, material: str) -> str | None:
    """Why this control material may not be used as a patient-QC
    material, or None: a registered control lot that is not yet LIVE
    (collecting / established) refuses patient-QC use by name.
    Materials with no control-lot record keep their pre-W4
    behaviour (programme config entries — onboarding is how a
    material becomes a governed lot)."""
    lot = control_lot_for(session, material)
    if lot is None or lot.state == "live":
        return None
    return (f"control lot '{material}' is in state '{lot.state}' — it "
            "refuses use as a patient-QC material until its "
            "establishment runs are complete and the lot is LIVE "
            "(POST /lab/control-lots/{id}/establish, then /activate). "
            "Establishment runs are recorded through the control-lot "
            "endpoint, not the patient-QC endpoint")


def qc_run_counts_for_gating(run: LabQcRun, lot: LabControlLot | None) -> bool:
    """Whether a QC run may satisfy the patient QC gates: runs
    stamped to a control lot count only once that lot is LIVE and
    only from its live_since (establishment-era runs are evidence
    for the lot's mean/SD, never patient QC)."""
    if run.control_lot_id is None:
        return True
    if lot is None or lot.state != "live" or lot.live_since is None:
        return False
    created = run.created_at
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    live_since = lot.live_since
    if live_since.tzinfo is None:
        live_since = live_since.replace(tzinfo=timezone.utc)
    return created >= live_since


# ---------------------------------------------------------------------------
# Verification protocol: computation
# ---------------------------------------------------------------------------

def _parse_pairs(payload: dict) -> list[dict]:
    pairs = payload.get("sample_pairs")
    if pairs is None:
        return []
    if not isinstance(pairs, list):
        raise HTTPException(status_code=422,
                            detail="'sample_pairs' must be a list")
    parsed = []
    for i, entry in enumerate(pairs):
        if not isinstance(entry, dict):
            raise HTTPException(status_code=422,
                                detail=f"sample_pairs[{i}] must be an object")
        old, new = entry.get("old_value"), entry.get("new_value")
        for field, value in (("old_value", old), ("new_value", new)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HTTPException(
                    status_code=422,
                    detail=f"sample_pairs[{i}].'{field}' must be a number")
        parsed.append({"sample_ref": str(entry.get("sample_ref")
                                         or f"sample-{i + 1}"),
                       "old_value": float(old), "new_value": float(new)})
    return parsed


def _parse_qc_evidence(payload: dict, field: str) -> list[dict]:
    entries = payload.get(field)
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be a list")
    parsed = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise HTTPException(status_code=422,
                                detail=f"{field}[{i}] must be an object")
        values = {}
        for name in ("measured_value", "expected_low", "expected_high"):
            value = entry.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HTTPException(
                    status_code=422,
                    detail=f"{field}[{i}].'{name}' must be a number")
            values[name] = float(value)
        parsed.append({
            "control_material": str(entry.get("control_material") or ""),
            **values,
            "passed": values["expected_low"] <= values["measured_value"]
            <= values["expected_high"]})
    return parsed


def compute_comparison(run: LabLotVerificationRun) -> dict:
    """The server-computed old-vs-new comparison against the run's
    snapshotted governed criteria: per-pair bias, mean/max bias,
    sample count vs the governed minimum, QC pass state on both
    lots, and the named reasons the comparison does not (yet) meet
    criteria. `meets_criteria` is computed, never caller-asserted."""
    criteria = run.criteria or {}
    min_samples = criteria.get("minSamples") or 0
    max_pct, max_abs = criteria.get("maxBiasPct"), criteria.get("maxBiasAbs")
    per_pair, reasons = [], []
    for pair in run.sample_pairs or []:
        bias = pair["new_value"] - pair["old_value"]
        bias_pct = (bias / pair["old_value"] * 100.0) if pair["old_value"] \
            else None
        within = True
        if max_pct is not None and bias_pct is not None \
                and abs(bias_pct) > float(max_pct):
            within = False
        if max_abs is not None and abs(bias) > float(max_abs):
            within = False
        per_pair.append({**pair, "bias": bias, "bias_pct": bias_pct,
                         "within_criteria": within})
        if not within:
            reasons.append(
                f"sample '{pair['sample_ref']}': bias {bias:+.4g} "
                f"({bias_pct if bias_pct is None else round(bias_pct, 3)}% "
                "vs old lot) exceeds the director-configured acceptance "
                "criteria")
    if len(per_pair) < int(min_samples):
        reasons.append(
            f"only {len(per_pair)} parallel patient sample(s) — the "
            f"governed minimum is {min_samples} spanning the range")
    qc_old_ok = bool(run.qc_old_lot) and all(e["passed"]
                                             for e in run.qc_old_lot)
    qc_new_ok = bool(run.qc_new_lot) and all(e["passed"]
                                             for e in run.qc_new_lot)
    if not qc_old_ok:
        reasons.append("QC on the OUTGOING lot is missing or failing — "
                       "QC results on both lots are required")
    if not qc_new_ok:
        reasons.append("QC on the NEW lot is missing or failing — QC "
                       "results on both lots are required")
    biases = [p["bias"] for p in per_pair]
    return {
        "sample_count": len(per_pair), "min_samples": int(min_samples),
        "per_pair": per_pair,
        "mean_bias": (sum(biases) / len(biases)) if biases else None,
        "max_abs_bias": max((abs(b) for b in biases), default=None),
        "qc_old_lot_passed": qc_old_ok, "qc_new_lot_passed": qc_new_ok,
        "meets_criteria": not reasons, "reasons": reasons,
        "criteria": criteria,
    }


def _run_out(run: LabLotVerificationRun, session: Session) -> dict:
    lot = _find_inventory_lot(session, run.sku, run.location_code,
                              run.lot_number)
    return {
        "id": run.id, "sku": run.sku, "location_code": run.location_code,
        "lot_number": run.lot_number, "lot_key": lot_key_for(
            run.sku, run.location_code, run.lot_number),
        "analyte_code": run.analyte_code,
        "outgoing_lot_number": run.outgoing_lot_number,
        "status": run.status, "sample_pairs": list(run.sample_pairs or []),
        "qc_old_lot": list(run.qc_old_lot or []),
        "qc_new_lot": list(run.qc_new_lot or []),
        "criteria": dict(run.criteria or {}),
        "comparison": run.comparison,
        "decision": run.decision, "decided_by": run.decided_by,
        "decided_at": run.decided_at.isoformat() if run.decided_at else None,
        "decision_note": run.decision_note,
        "inventory_verification": run.inventory_verification,
        "inventory_stage": run.inventory_stage,
        "inventory_lot": _lot_out_of_inventory(session, lot) if lot else None,
        "jit_position": ("NO emergency bypass: a quarantined or "
                         "unverified lot cannot be used under any "
                         "pressure pathway"),
        "created_by": run.created_by,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def _run_row(session: Session, run_id: int) -> LabLotVerificationRun:
    row = session.scalars(
        select(LabLotVerificationRun).where(
            LabLotVerificationRun.id == run_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Verification run {run_id} was not found")
    return row


# ---------------------------------------------------------------------------
# Endpoints: verification protocol
# ---------------------------------------------------------------------------

@router.post("/reagent-verifications", status_code=201)
async def create_verification(request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(
                                  require_capability("lab.work"))):
    """Open a lot-to-lot verification protocol run for an inventory
    lot. The lot must exist in Inventory; the acceptance criteria
    are snapshotted from the governed config for the analyte at
    creation (a later republication never silently re-judges an
    in-flight protocol)."""
    payload = await _json_body(request)
    sku = _require_str(payload, "sku")
    location_code = _require_str(payload, "location_code")
    lot_number = _require_str(payload, "lot_number")
    analyte_code = _require_str(payload, "analyte_code")
    from . import lab as lab_mod  # lazy: app.lab imports this module lazily
    if lab_mod._concept(session, analyte_code) is None:
        raise HTTPException(
            status_code=422,
            detail=f"Analyte '{analyte_code}' is not in the published "
                   "lab catalog")
    lot = _find_inventory_lot(session, sku, location_code, lot_number)
    if lot is None:
        raise HTTPException(
            status_code=404,
            detail=f"Lot '{lot_number}' of '{sku}' at '{location_code}' "
                   "was not found in inventory")
    criteria = lab_qc.verification_criteria(session, analyte_code)
    run = LabLotVerificationRun(
        sku=sku, location_code=location_code, lot_number=lot_number,
        analyte_code=analyte_code,
        outgoing_lot_number=payload.get("outgoing_lot_number")
        if isinstance(payload.get("outgoing_lot_number"), str) else None,
        criteria=criteria, created_by=user.username)
    session.add(run)
    session.flush()
    run.comparison = compute_comparison(run)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.reagent.verification.create",
                 entity=f"LabLotVerificationRun/{run.id}",
                 payload={"sku": sku, "location_code": location_code,
                          "lot_number": lot_number,
                          "analyte_code": analyte_code,
                          "criteria_source": criteria.get("source")})
    session.commit()
    return _run_out(run, session)


@router.get("/reagent-verifications")
def list_verifications(session: Session = Depends(get_db),
                       user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(
        select(LabLotVerificationRun).order_by(
            LabLotVerificationRun.id.desc())).all()
    return {"items": [_run_out(r, session) for r in rows],
            "total": len(rows)}


@router.get("/reagent-verifications/{run_id}")
def get_verification(run_id: int, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("lab.read"))):
    return _run_out(_run_row(session, run_id), session)


@router.post("/reagent-verifications/{run_id}/evidence")
async def add_evidence(run_id: int, request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("lab.work"))):
    """Record the protocol evidence (parallel sample pairs + QC on
    both lots) and recompute the comparison against the run's
    snapshotted governed criteria."""
    payload = await _json_body(request)
    run = _run_row(session, run_id)
    if run.status == "decided":
        raise HTTPException(
            status_code=409,
            detail=f"Verification run {run_id} is already decided "
                   f"({run.decision}); a decided protocol is a signed "
                   "record and cannot be edited — open a new run")
    if "sample_pairs" in payload:
        run.sample_pairs = _parse_pairs(payload)
    if "qc_old_lot" in payload:
        run.qc_old_lot = _parse_qc_evidence(payload, "qc_old_lot")
    if "qc_new_lot" in payload:
        run.qc_new_lot = _parse_qc_evidence(payload, "qc_new_lot")
    run.status = "comparing"
    run.comparison = compute_comparison(run)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.reagent.verification.evidence",
                 entity=f"LabLotVerificationRun/{run.id}",
                 payload={"sample_count": run.comparison["sample_count"],
                          "meets_criteria": run.comparison["meets_criteria"]})
    session.commit()
    return _run_out(run, session)


@router.post("/reagent-verifications/{run_id}/decide")
async def decide_verification(run_id: int, request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(
                                  require_capability("lab.verify"))):
    """Sign the accept/reject decision (authorised role: lab.verify).

    ACCEPT requires the computed comparison to meet the governed
    criteria; it then drives Inventory's own surface in-process —
    verify_lot(method = this protocol, result 'pass') and, when the
    lot is at quarantine/received, stage_lot(to 'released') — so the
    stock gate, its policy checks, audit and ledger all remain
    Inventory's. REJECT stamps a FAIL verification through the same
    surface (which blocks any release) and leaves the stage exactly
    where it was (quarantined)."""
    payload = await _json_body(request)
    decision = payload.get("decision")
    if decision not in ("accept", "reject"):
        raise HTTPException(status_code=422,
                            detail="'decision' must be 'accept' or 'reject'")
    run = _run_row(session, run_id)
    if run.status == "decided":
        raise HTTPException(
            status_code=409,
            detail=f"Verification run {run_id} is already decided "
                   f"({run.decision} by {run.decided_by})")
    run.comparison = compute_comparison(run)
    note = payload.get("note") if isinstance(payload.get("note"), str) \
        else None
    identity = {"sku": run.sku, "location_code": run.location_code,
                "lot_number": run.lot_number}
    if decision == "accept":
        if not run.comparison["meets_criteria"]:
            raise HTTPException(
                status_code=409,
                detail=("Acceptance refused: the computed comparison "
                        "does not meet the director-configured "
                        "acceptance criteria — "
                        + "; ".join(run.comparison["reasons"])))
        method = (f"lot-to-lot verification protocol run #{run.id} "
                  "(parallel patient samples + QC on both lots; "
                  "CLSI EP26-A pattern)")
        verified = await inventory_mod.verify_lot(
            _inventory_request({**identity, "method": method,
                                "result": "pass", "note": note}),
            session, user)
        run.inventory_verification = {
            "method": method, "result": "pass",
            "verified_by": verified.get("verified_by"),
            "verified_on": verified.get("verified_on"),
            "via": "POST /inventory/lots/verify (in-process)"}
        lot = _find_inventory_lot(session, run.sku, run.location_code,
                                  run.lot_number)
        stage_now = getattr(lot, "stage", None) or "released"
        if stage_now in ("quarantine", "received"):
            staged = await inventory_mod.stage_lot(
                _inventory_request({**identity, "to_stage": "released"}),
                session, user)
            run.inventory_stage = {
                "from_stage": stage_now, "to_stage": "released",
                "via": "POST /inventory/lots/stage (in-process)",
                "movement": staged.get("movement")}
        else:
            run.inventory_stage = {
                "from_stage": stage_now, "to_stage": stage_now,
                "via": None,
                "note": (f"lot already at stage '{stage_now}'; the "
                         "verification stamp above is the release "
                         "evidence — no transition was needed or "
                         "attempted")}
    else:
        method = (f"lot-to-lot verification protocol run #{run.id} "
                  "— REJECTED (see protocol record)")
        verified = await inventory_mod.verify_lot(
            _inventory_request({**identity, "method": method,
                                "result": "fail", "note": note}),
            session, user)
        run.inventory_verification = {
            "method": method, "result": "fail",
            "verified_by": verified.get("verified_by"),
            "verified_on": verified.get("verified_on"),
            "via": "POST /inventory/lots/verify (in-process)"}
        lot = _find_inventory_lot(session, run.sku, run.location_code,
                                  run.lot_number)
        run.inventory_stage = {
            "from_stage": getattr(lot, "stage", None),
            "to_stage": getattr(lot, "stage", None), "via": None,
            "note": ("no stage transition on rejection — the lot "
                     "stays at its current stage and the FAIL "
                     "verification blocks its release")}
    run.decision = decision
    run.decided_by = user.username
    run.decided_at = datetime.now(timezone.utc)
    run.decision_note = note
    run.status = "decided"
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.reagent.verification.decide",
                 entity=f"LabLotVerificationRun/{run.id}",
                 payload={"decision": decision, "sku": run.sku,
                          "lot_number": run.lot_number,
                          "meets_criteria": run.comparison["meets_criteria"],
                          "inventory_verification":
                              run.inventory_verification})
    session.commit()
    return _run_out(run, session)


# ---------------------------------------------------------------------------
# Endpoints: control-lot onboarding
# ---------------------------------------------------------------------------

def _control_lot_out(session: Session, lot: LabControlLot) -> dict:
    collected = session.scalars(
        select(LabQcRun).where(LabQcRun.control_lot_id == lot.id)).all()
    return {
        "id": lot.id, "lot_key": lot.lot_key,
        "analyte_code": lot.analyte_code, "level": lot.level,
        "state": lot.state, "required_runs": lot.required_runs,
        "collected_runs": len(collected),
        "runs_remaining": max(0, lot.required_runs - len(collected)),
        "established_mean": lot.established_mean,
        "established_sd": lot.established_sd,
        "established_by": lot.established_by,
        "established_at": lot.established_at.isoformat()
        if lot.established_at else None,
        "live_since": lot.live_since.isoformat() if lot.live_since else None,
        "inventory_sku": lot.inventory_sku,
        "inventory_lot_number": lot.inventory_lot_number,
        "usable_as_patient_qc": lot.state == "live",
        "created_by": lot.created_by,
        "created_at": lot.created_at.isoformat() if lot.created_at else None,
    }


def _control_lot_row(session: Session, lot_id: int) -> LabControlLot:
    row = session.scalars(
        select(LabControlLot).where(LabControlLot.id == lot_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Control lot {lot_id} was not found")
    return row


@router.post("/control-lots", status_code=201)
async def create_control_lot(request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(
                                 require_capability("lab.work"))):
    """Register a QC control material as a real lot record in state
    'collecting'. The establishment run count comes from the
    governed config unless the registration names its own."""
    payload = await _json_body(request)
    lot_key = _require_str(payload, "lot_key")
    analyte_code = _require_str(payload, "analyte_code")
    from . import lab as lab_mod
    if lab_mod._concept(session, analyte_code) is None:
        raise HTTPException(
            status_code=422,
            detail=f"Analyte '{analyte_code}' is not in the published "
                   "lab catalog")
    if control_lot_for(session, lot_key) is not None:
        raise HTTPException(status_code=409,
                            detail=f"Control lot '{lot_key}' already exists")
    cfg = lab_qc.reagent_qc_config(session)
    required = payload.get("required_runs")
    if required is None:
        required = (cfg.get("controlLot") or {}).get("requiredRunsDefault", 20)
    if isinstance(required, bool) or not isinstance(required, int) \
            or required < 1:
        raise HTTPException(status_code=422,
                            detail="'required_runs' must be a positive integer")
    lot = LabControlLot(
        lot_key=lot_key, analyte_code=analyte_code,
        level=payload.get("level") if isinstance(payload.get("level"), str)
        else None,
        required_runs=required,
        inventory_sku=payload.get("inventory_sku")
        if isinstance(payload.get("inventory_sku"), str) else None,
        inventory_lot_number=payload.get("inventory_lot_number")
        if isinstance(payload.get("inventory_lot_number"), str) else None,
        created_by=user.username)
    session.add(lot)
    session.flush()
    append_audit(session, actor=user.username, action="lab.control_lot.create",
                 entity=f"LabControlLot/{lot.id}",
                 payload={"lot_key": lot_key, "analyte_code": analyte_code,
                          "required_runs": required})
    session.commit()
    return _control_lot_out(session, lot)


@router.get("/control-lots")
def list_control_lots(session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(
        select(LabControlLot).order_by(LabControlLot.id.asc())).all()
    return {"items": [_control_lot_out(session, r) for r in rows],
            "total": len(rows)}


@router.post("/control-lots/{lot_id}/runs", status_code=201)
async def record_establishment_run(lot_id: int, request: Request,
                                   session: Session = Depends(get_db),
                                   user: User = Depends(
                                       require_capability("lab.work"))):
    """Record one ESTABLISHMENT QC run against a collecting control
    lot. The run is a real LabQcRun stamped with the lot's identity
    (control_lot_id + control_material = lot_key) — and, per
    qc_run_counts_for_gating, it never satisfies the patient QC
    gates while the lot is not live."""
    payload = await _json_body(request)
    lot = _control_lot_row(session, lot_id)
    if lot.state != "collecting":
        raise HTTPException(
            status_code=409,
            detail=f"Control lot '{lot.lot_key}' is '{lot.state}' — "
                   "establishment runs are only recorded while "
                   "collecting")
    numbers = {}
    for field in ("expected_low", "expected_high", "measured_value"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(status_code=422,
                                detail=f"'{field}' must be a number")
        numbers[field] = float(value)
    if numbers["expected_low"] > numbers["expected_high"]:
        raise HTTPException(
            status_code=422,
            detail="'expected_low' must not exceed 'expected_high'")
    cfg = lab_qc.reagent_qc_config(session)
    now = datetime.now(timezone.utc)
    run = LabQcRun(
        analyte_code=lot.analyte_code, control_material=lot.lot_key,
        expected_low=numbers["expected_low"],
        expected_high=numbers["expected_high"],
        measured_value=numbers["measured_value"],
        unit=payload.get("unit") if isinstance(payload.get("unit"), str)
        else None,
        passed=numbers["expected_low"] <= numbers["measured_value"]
        <= numbers["expected_high"],
        performed_by=user.username, level=lot.level, flags=[],
        z_score=None,
        instrument_id=payload.get("instrument_id")
        if isinstance(payload.get("instrument_id"), str) else None,
        reagent_lot_key=payload.get("reagent_lot_key")
        if isinstance(payload.get("reagent_lot_key"), str) else None,
        control_lot_id=lot.id,
        shift=lab_qc.shift_for_timestamp(cfg, now))
    session.add(run)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.control_lot.establishment_run",
                 entity=f"LabControlLot/{lot.id}",
                 payload={"qc_run_id": run.id, "lot_key": lot.lot_key,
                          "measured_value": run.measured_value,
                          "passed": run.passed})
    session.commit()
    out = _control_lot_out(session, lot)
    out["run"] = {"id": run.id, "measured_value": run.measured_value,
                  "passed": run.passed, "shift": run.shift,
                  "control_lot_id": run.control_lot_id}
    return out


@router.post("/control-lots/{lot_id}/establish")
async def establish_control_lot(lot_id: int,
                                session: Session = Depends(get_db),
                                user: User = Depends(
                                    require_capability("lab.verify"))):
    """Establish the lot: compute its mean/SD from its establishment
    runs. Refused by name until the governed run count is collected
    — a lot cannot be established early."""
    lot = _control_lot_row(session, lot_id)
    if lot.state != "collecting":
        raise HTTPException(
            status_code=409,
            detail=f"Control lot '{lot.lot_key}' is '{lot.state}', not "
                   "'collecting'")
    runs = session.scalars(
        select(LabQcRun).where(LabQcRun.control_lot_id == lot.id)
        .order_by(LabQcRun.id.asc())).all()
    if len(runs) < lot.required_runs:
        raise HTTPException(
            status_code=409,
            detail=(f"Control lot '{lot.lot_key}' cannot be "
                    f"established: {len(runs)} of {lot.required_runs} "
                    "required establishment runs collected — the "
                    "governed run count must be met before mean/SD "
                    "are established"))
    values = [r.measured_value for r in runs]
    lot.established_mean = statistics.fmean(values)
    lot.established_sd = statistics.pstdev(values) if len(values) > 1 \
        else 0.0
    lot.established_by = user.username
    lot.established_at = datetime.now(timezone.utc)
    lot.state = "established"
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.control_lot.establish",
                 entity=f"LabControlLot/{lot.id}",
                 payload={"lot_key": lot.lot_key, "runs": len(runs),
                          "mean": lot.established_mean,
                          "sd": lot.established_sd})
    session.commit()
    return _control_lot_out(session, lot)


@router.post("/control-lots/{lot_id}/activate")
async def activate_control_lot(lot_id: int,
                               session: Session = Depends(get_db),
                               user: User = Depends(
                                   require_capability("lab.verify"))):
    """Move an ESTABLISHED control lot to LIVE — from this moment
    it may be used as a patient-QC material and its (new) runs may
    satisfy the patient QC gates."""
    lot = _control_lot_row(session, lot_id)
    if lot.state != "established":
        raise HTTPException(
            status_code=409,
            detail=f"Control lot '{lot.lot_key}' is '{lot.state}' — "
                   "only an established lot can go live (collecting "
                   "lots must be established first)")
    lot.state = "live"
    lot.live_since = datetime.now(timezone.utc)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.control_lot.activate",
                 entity=f"LabControlLot/{lot.id}",
                 payload={"lot_key": lot.lot_key,
                          "mean": lot.established_mean,
                          "sd": lot.established_sd})
    session.commit()
    return _control_lot_out(session, lot)


# ---------------------------------------------------------------------------
# Endpoint: lot -> result traceability (recall blast radius)
# ---------------------------------------------------------------------------

@router.get("/reagent-lots/results")
def results_for_lot(sku: str = Query(...), lot_number: str = Query(...),
                    location_code: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability("lab.read"))):
    """Every result produced with a given reagent lot: the
    Observations stamped with the lot at result entry, their
    reports, and each report's correction path — the EXISTING
    amended-report endpoint (POST /lab/reports/{id}/amend, the
    §493.1291(k) machinery); this endpoint builds no second
    correction system, it enumerates and links."""
    from . import lab as lab_mod
    observations = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Observation")
        .order_by(FhirResource.id.asc())).all()
    reports = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "DiagnosticReport")).all()
    report_by_obs: dict[str, FhirResource] = {}
    for report in reports:
        for ref in (report.body or {}).get("result") or []:
            reference = ref.get("reference") if isinstance(ref, dict) else None
            if reference and reference.startswith("Observation/"):
                report_by_obs[reference.split("/", 1)[1]] = report
    items = []
    for obs in observations:
        body = obs.body or {}
        stamp = None
        instrument = None
        for ext in body.get("extension") or []:
            if not isinstance(ext, dict):
                continue
            if ext.get("url") == REAGENT_LOT_EXTENSION:
                stamp = ext.get("valueString")
            elif ext.get("url") == INSTRUMENT_EXTENSION:
                instrument = ext.get("valueString")
        if not stamp:
            continue
        parts = stamp.split("|")
        if len(parts) != 3 or parts[0] != sku or parts[2] != lot_number:
            continue
        if location_code and parts[1] != location_code:
            continue
        report = report_by_obs.get(obs.resource_id)
        amendment = lab_mod._report_amendment(session, report) \
            if report is not None else None
        quantity = body.get("valueQuantity") or {}
        items.append({
            "observation_id": obs.resource_id,
            "code": lab_mod._code_of(body),
            "value": quantity.get("value", body.get("valueString")),
            "unit": quantity.get("unit"),
            "status": body.get("status"),
            "patient_ref": obs.patient_ref,
            "effective": body.get("effectiveDateTime"),
            "reagent_lot_key": stamp, "instrument_id": instrument,
            "report_id": report.resource_id if report else None,
            "amendment": amendment,
            "correction_path": (
                {"endpoint": f"POST /lab/reports/{report.resource_id}/amend",
                 "machinery": "existing amended-report machinery "
                              "(CLIA §493.1291(k)) — a corrected report "
                              "is a new linked version, never a rewrite"}
                if report is not None else None),
        })
    return {"sku": sku, "lot_number": lot_number,
            "location_code": location_code,
            "lot_key": lot_key_for(sku, location_code, lot_number)
            if location_code else None,
            "items": items, "total": len(items)}
