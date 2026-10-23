"""UX pass (WS3a) — virtual plates: governed plate templates,
plate-run instances with materialised wells, and a QC gate.

Builds ON the Phase 2/6B lab spine (app.lab) and the Phase 15
specimen machinery (app.lab_specimens):

- Templates are Studio terminology config, key
  ``lab-plate-templates``; a concept is
  ``{code, display, active, assay, format, wells: [{position, role,
  expected?}]}`` with role in control-pos | control-neg |
  calibrator | blank | patient | replicate and ``expected`` either
  ``{low, high}`` (numeric, inclusive) or ``{result}`` (qualitative,
  exact match, case-insensitive). The terminology validator passes
  the extra concept fields (assay / format / wells / expected)
  through, exactly as the catalog's bench / delta fields ride it.
  Two templates are seeded as published v1 at startup
  (insert-if-absent, system actor, audited — the app.lab seed
  pattern): ``chem-96`` and ``micro-48``.
- Instances are this module's OWN tables (the lab_specimens
  own-tables precedent): PlateRun + PlateWell. Creating a run
  materialises one well per template well.
- The QC gate: a run's gate is OPEN only when every
  control / calibrator / blank well has been resulted AND none of
  them failed its expected check. Patient (and replicate) well
  results are refused 409 while the gate is closed — both here and,
  via :func:`plate_gate_blocker`, in the direct result-entry path
  in app.lab — and a patient result that does land is entered on
  the linked order through app.lab._create_results, the SAME
  internal path manual entry uses, so ranges / delta /
  auto-verify semantics are never forked.
- Specimen assignment resolves through app.lab_specimens'
  machinery (barcode value, node key or FHIR specimen id) and a
  specimen may sit on only one OPEN plate at a time.

Synthetic data only, like the rest of the system.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import lab
from .audit import append_audit
from .configstore import published_config
from .db import Base
from .models import FhirResource, User, utcnow
from .security import get_db, require_capability

router = APIRouter(prefix="/lab", tags=["lab_plates"])

PLATE_TEMPLATES_KEY = "lab-plate-templates"
CONTROL_ROLES = ("control-pos", "control-neg", "calibrator", "blank")
PATIENT_ROLES = ("patient", "replicate")
ALL_ROLES = CONTROL_ROLES + PATIENT_ROLES


# ---------------------------------------------------------------------------
# Own tables
# ---------------------------------------------------------------------------

class PlateRun(Base):
    """One virtual plate run — an instance of a published plate
    template, worked through its wells until completed."""
    __tablename__ = "lab_plate_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_code: Mapped[str] = mapped_column(String(80), nullable=False,
                                               index=True)
    assay: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    format: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="open", index=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)
    completed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class PlateWell(Base):
    """One well of a plate run, materialised from the template well:
    its role and expected check are copied at creation, so a later
    template republication never rewrites a run in progress."""
    __tablename__ = "lab_plate_wells"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plate_id: Mapped[int] = mapped_column(ForeignKey("lab_plate_runs.id"),
                                          nullable=False, index=True)
    position: Mapped[str] = mapped_column(String(20), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    expected: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    specimen_id: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                                    index=True)
    order_id: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                                 index=True)
    result_value: Mapped[object | None] = mapped_column(JSON, nullable=True)
    result_status: Mapped[str] = mapped_column(String(20), nullable=False,
                                               default="pending")
    assigned_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    resulted_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resulted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Seed templates (terminology/lab-plate-templates, published v1)
# ---------------------------------------------------------------------------

def _chem_96_wells() -> list[dict]:
    """96-well chemistry plate: row A is the QC row (positive /
    negative controls, calibrators, blanks — each with a numeric
    expected range); every remaining well is a patient well."""
    row_a = {
        "A1": ("control-pos", {"low": 90.0, "high": 110.0}),
        "A2": ("control-pos", {"low": 180.0, "high": 220.0}),
        "A3": ("control-neg", {"low": 0.0, "high": 5.0}),
        "A4": ("control-neg", {"low": 0.0, "high": 5.0}),
        "A5": ("calibrator", {"low": 95.0, "high": 105.0}),
        "A6": ("calibrator", {"low": 190.0, "high": 210.0}),
        "A7": ("calibrator", {"low": 45.0, "high": 55.0}),
        "A8": ("calibrator", {"low": 280.0, "high": 320.0}),
        "A9": ("blank", {"low": 0.0, "high": 2.0}),
        "A10": ("blank", {"low": 0.0, "high": 2.0}),
        "A11": ("blank", {"low": 0.0, "high": 2.0}),
        "A12": ("control-pos", {"low": 90.0, "high": 110.0}),
    }
    wells = []
    for row in "ABCDEFGH":
        for col in range(1, 13):
            position = f"{row}{col}"
            if position in row_a:
                role, expected = row_a[position]
                wells.append({"position": position, "role": role,
                              "expected": expected})
            else:
                wells.append({"position": position, "role": "patient"})
    return wells


def _micro_48_wells() -> list[dict]:
    """48-well microbiology plate: row A is the QC row with
    qualitative expected results; every remaining well is a patient
    well."""
    row_a = {
        "A1": ("control-pos", {"result": "Positive"}),
        "A2": ("control-pos", {"result": "Positive"}),
        "A3": ("control-neg", {"result": "Negative"}),
        "A4": ("control-neg", {"result": "Negative"}),
        "A5": ("calibrator", {"result": "Positive"}),
        "A6": ("blank", {"result": "Negative"}),
        "A7": ("blank", {"result": "Negative"}),
        "A8": ("control-pos", {"result": "Positive"}),
    }
    wells = []
    for row in "ABCDEF":
        for col in range(1, 9):
            position = f"{row}{col}"
            if position in row_a:
                role, expected = row_a[position]
                wells.append({"position": position, "role": role,
                              "expected": expected})
            else:
                wells.append({"position": position, "role": "patient"})
    return wells


def default_plate_templates_payload() -> dict:
    return {
        "name": "OpenCare laboratory plate templates",
        "concepts": [
            {"code": "chem-96", "display": "Chemistry 96-well plate",
             "active": True, "assay": "chemistry", "format": "96-well",
             "wells": _chem_96_wells()},
            {"code": "micro-48", "display": "Microbiology 48-well plate",
             "active": True, "assay": "microbiology", "format": "48-well",
             "wells": _micro_48_wells()},
        ],
    }


def ensure_lab_plate_defaults(session: Session) -> None:
    """Seed the plate templates as published Studio config v1
    (idempotent, insert-if-absent, system actor, audited — the
    app.lab._seed_published pattern)."""
    lab._seed_published(session, "terminology", PLATE_TEMPLATES_KEY,
                        default_plate_templates_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Published-template loader (honest 503 when unpublished — the
# app.inpatient locations-config pattern)
# ---------------------------------------------------------------------------

def _templates(session: Session) -> list[dict]:
    cfg = published_config(session, "terminology", PLATE_TEMPLATES_KEY)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=(f"No published lab plate templates "
                    f"(terminology/{PLATE_TEMPLATES_KEY}); publish one "
                    "in Studio before using virtual plates"))
    return [c for c in (cfg.payload or {}).get("concepts") or []
            if isinstance(c, dict)]


def _template(session: Session, code: str) -> dict:
    for concept in _templates(session):
        if concept.get("code") == code:
            if concept.get("active") is False:
                raise HTTPException(
                    status_code=422,
                    detail=f"Plate template '{code}' is inactive in the "
                           "published lab plate templates")
            return concept
    raise HTTPException(
        status_code=404,
        detail=f"Plate template '{code}' not found in the published "
               "lab plate templates")


# ---------------------------------------------------------------------------
# Serialisation + QC gate
# ---------------------------------------------------------------------------

def _well_out(well: PlateWell) -> dict:
    return {
        "position": well.position, "role": well.role,
        "expected": well.expected,
        "specimen_id": well.specimen_id, "order_id": well.order_id,
        "result_value": well.result_value,
        "result_status": well.result_status,
        "assigned_by": well.assigned_by,
        "assigned_at": well.assigned_at.isoformat() if well.assigned_at else None,
        "resulted_by": well.resulted_by,
        "resulted_at": well.resulted_at.isoformat() if well.resulted_at else None,
    }


def _wells(session: Session, plate_id: int) -> list[PlateWell]:
    return list(session.scalars(
        select(PlateWell).where(PlateWell.plate_id == plate_id)
        .order_by(PlateWell.id.asc())).all())


def _qc(wells: list[PlateWell]) -> dict:
    """The QC summary for a run: gate_open only when EVERY control /
    calibrator / blank well has been resulted and NONE failed."""
    controls = [w for w in wells if w.role in CONTROL_ROLES]
    passed = sum(1 for w in controls if w.result_status == "passed")
    failed = sum(1 for w in controls if w.result_status == "failed")
    resulted = sum(1 for w in controls
                   if w.result_status in ("passed", "failed"))
    return {"controls_total": len(controls), "controls_passed": passed,
            "controls_failed": failed,
            "gate_open": resulted == len(controls) and failed == 0}


def _run_out(session: Session, run: PlateRun, detail: bool = False) -> dict:
    wells = _wells(session, run.id)
    out = {"id": run.id, "template_code": run.template_code,
           "assay": run.assay, "format": run.format, "status": run.status,
           "created_by": run.created_by,
           "created_at": run.created_at.isoformat() if run.created_at else None,
           "completed_by": run.completed_by,
           "completed_at": (run.completed_at.isoformat()
                            if run.completed_at else None),
           "qc": _qc(wells)}
    if detail:
        out["wells"] = [_well_out(w) for w in wells]
    return out


def _run_row(session: Session, plate_id: int) -> PlateRun:
    run = session.get(PlateRun, plate_id)
    if run is None:
        raise HTTPException(status_code=404,
                            detail=f"Plate run {plate_id} not found")
    return run


def plate_gate_blocker(session: Session, specimen_id: str) -> int | None:
    """The id of an OPEN plate whose closed QC gate blocks result
    entry for this specimen, or None. Used by the plate endpoints
    here and by the direct result-entry path in app.lab."""
    rows = session.scalars(
        select(PlateWell).where(PlateWell.specimen_id == specimen_id)).all()
    for well in rows:
        run = session.get(PlateRun, well.plate_id)
        if run is None or run.status != "open":
            continue
        if not _qc(_wells(session, run.id))["gate_open"]:
            return run.id
    return None


def plate_assignment(session: Session, specimen_id: str) -> PlateWell | None:
    """The most recent plate well assigned to a specimen (any run
    state), or None — the test-detail 'plated' step reads this."""
    return session.scalars(
        select(PlateWell).where(PlateWell.specimen_id == specimen_id)
        .order_by(PlateWell.id.desc())).first()


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


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

@router.post("/plates", status_code=201)
async def create_plate(request: Request, session: Session = Depends(get_db),
                       user: User = Depends(require_capability("lab.work"))):
    """Create a plate run from a published template: wells are
    materialised from the template (role + expected copied onto each
    well), all unresulted."""
    payload = await _json_body(request)
    template_code = payload.get("template_code") or payload.get("code")
    if not template_code:
        raise HTTPException(status_code=422,
                            detail="'template_code' is required")
    template = _template(session, str(template_code))
    wells_spec = [w for w in template.get("wells") or []
                  if isinstance(w, dict) and w.get("position")]
    if not wells_spec:
        raise HTTPException(
            status_code=422,
            detail=f"Plate template '{template_code}' declares no wells")
    run = PlateRun(template_code=str(template_code),
                   assay=str(template.get("assay") or ""),
                   format=str(template.get("format") or ""),
                   status="open", created_by=user.username)
    session.add(run)
    session.flush()
    for spec in wells_spec:
        role = spec.get("role") if spec.get("role") in ALL_ROLES else "patient"
        session.add(PlateWell(
            plate_id=run.id, position=str(spec["position"]), role=role,
            expected=spec.get("expected") if isinstance(
                spec.get("expected"), dict) else None))
    session.flush()
    append_audit(session, actor=user.username, action="lab.plate.create",
                 entity=f"PlateRun/{run.id}",
                 payload={"template_code": run.template_code,
                          "assay": run.assay, "format": run.format,
                          "well_count": len(wells_spec)})
    session.commit()
    return _run_out(session, run, detail=True)


@router.get("/plates")
def list_plates(status: str | None = Query(default=None),
                session: Session = Depends(get_db),
                user: User = Depends(require_capability("lab.read"))):
    stmt = select(PlateRun).order_by(PlateRun.id.asc())
    if status:
        stmt = stmt.where(PlateRun.status == status)
    runs = session.scalars(stmt).all()
    return {"items": [_run_out(session, r) for r in runs],
            "total": len(runs)}


@router.get("/plates/{plate_id}")
def plate_detail(plate_id: int, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("lab.read"))):
    run = _run_row(session, plate_id)
    return _run_out(session, run, detail=True)


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------

def _order_id_for_specimen(session: Session, node) -> str | None:
    """The order a specimen belongs to: the lifecycle node's own
    link first, else the ServiceRequest whose specimen reference
    names it (the app.lab order body shape)."""
    if node.order_id:
        return node.order_id
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "ServiceRequest")).all()
    for row in rows:
        for ref in (row.body or {}).get("specimen") or []:
            if isinstance(ref, dict) and \
                    ref.get("reference") == f"Specimen/{node.fhir_specimen_id}":
                return row.resource_id
    return None


@router.post("/plates/{plate_id}/assign")
async def assign_specimen(plate_id: int, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("lab.work"))):
    """Assign a specimen (by barcode value, node key or FHIR
    specimen id — app.lab_specimens resolution) to a patient /
    replicate well of an open plate. No position -> the first empty
    patient well."""
    payload = await _json_body(request)
    run = _run_row(session, plate_id)
    if run.status == "complete":
        raise HTTPException(status_code=409,
                            detail=f"Plate {plate_id} is complete; "
                                   "no further assignments are accepted")
    specimen_ref = payload.get("specimen_ref") or payload.get("specimen_id") \
        or payload.get("identifier") or payload.get("barcode")
    if not specimen_ref:
        raise HTTPException(status_code=422,
                            detail="'specimen_ref' (specimen id or barcode) "
                                   "is required")
    from .lab_specimens import _resolve_node
    node = _resolve_node(session, str(specimen_ref))  # 404 when unknown
    specimen_id = node.fhir_specimen_id
    # One open plate per specimen.
    existing = session.scalars(
        select(PlateWell).where(PlateWell.specimen_id == specimen_id)).all()
    for other in existing:
        other_run = session.get(PlateRun, other.plate_id)
        if other_run is not None and other_run.status == "open":
            raise HTTPException(
                status_code=409,
                detail=(f"Specimen {specimen_id} is already assigned to "
                        f"open plate {other_run.id} (well "
                        f"{other.position})"))
    wells = _wells(session, plate_id)
    position = payload.get("position")
    if position:
        well = next((w for w in wells if w.position == str(position)), None)
        if well is None:
            raise HTTPException(
                status_code=422,
                detail=f"Plate {plate_id} has no well '{position}'")
        if well.role not in PATIENT_ROLES:
            raise HTTPException(
                status_code=422,
                detail=(f"Well {position} on plate {plate_id} is a "
                        f"'{well.role}' well; specimens are assigned to "
                        "patient/replicate wells"))
        if well.specimen_id is not None:
            raise HTTPException(
                status_code=409,
                detail=(f"Well {position} on plate {plate_id} is already "
                        f"occupied by specimen {well.specimen_id}"))
    else:
        well = next((w for w in wells
                     if w.role == "patient" and w.specimen_id is None), None)
        if well is None:
            raise HTTPException(
                status_code=409,
                detail=f"Plate {plate_id} has no empty patient well")
    well.specimen_id = specimen_id
    well.order_id = _order_id_for_specimen(session, node)
    well.assigned_by = user.username
    well.assigned_at = datetime.now(timezone.utc)
    session.flush()
    append_audit(session, actor=user.username, action="lab.plate.assign",
                 entity=f"PlateRun/{plate_id}",
                 payload={"position": well.position,
                          "specimen_id": specimen_id,
                          "order_id": well.order_id,
                          "barcode": node.barcode_value})
    session.commit()
    return {"plate": _run_out(session, run, detail=True),
            "well": _well_out(well)}


# ---------------------------------------------------------------------------
# Results (bulk; control wells first so one payload can open its gate)
# ---------------------------------------------------------------------------

def _evaluate_control(expected: dict | None, value) -> bool:
    """One control / calibrator / blank result against its expected
    check: numeric {low, high} is an inclusive range; qualitative
    {result} is an exact match, case-insensitive. A control with no
    expected check passes on any resulted value; a non-numeric value
    against a numeric range fails (it is a failed control, recorded
    as such — never silently dropped)."""
    if not isinstance(expected, dict) or not expected:
        return True
    if "result" in expected:
        return str(value).strip().lower() == \
            str(expected["result"]).strip().lower()
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    low, high = expected.get("low"), expected.get("high")
    if isinstance(low, (int, float)) and not isinstance(low, bool) \
            and numeric < float(low):
        return False
    if isinstance(high, (int, float)) and not isinstance(high, bool) \
            and numeric > float(high):
        return False
    return True


@router.post("/plates/{plate_id}/results")
async def plate_results(plate_id: int, request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.work"))):
    """Enter results for plate wells (bulk; a single result is a
    list of one). Control / calibrator / blank wells are evaluated
    against their template expected check -> passed / failed.
    Patient / replicate wells are refused 409 while the plate's QC
    gate is closed; once open, the value is entered on the linked
    order through app.lab._create_results (the manual-entry path —
    identical validation, ranges, delta and auto-verify).

    Control entries in the payload are processed FIRST (and
    committed) so one payload can open its own gate before its
    patient entries are judged."""
    payload = await _json_body(request)
    run = _run_row(session, plate_id)
    if run.status == "complete":
        raise HTTPException(status_code=409,
                            detail=f"Plate {plate_id} is complete; "
                                   "results are no longer accepted")
    entries = payload.get("results")
    if not isinstance(entries, list) or not entries:
        raise HTTPException(status_code=422,
                            detail="'results' must be a non-empty list of "
                                   "{position, value} entries")
    wells = {w.position: w for w in _wells(session, plate_id)}
    parsed = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or "position" not in entry \
                or "value" not in entry:
            raise HTTPException(
                status_code=422,
                detail=f"results[{i}] must be an object with 'position' "
                       "and 'value'")
        well = wells.get(str(entry["position"]))
        if well is None:
            raise HTTPException(
                status_code=422,
                detail=f"Plate {plate_id} has no well '{entry['position']}'")
        parsed.append((well, entry))
    controls = [(w, e) for w, e in parsed if w.role in CONTROL_ROLES]
    patients = [(w, e) for w, e in parsed if w.role in PATIENT_ROLES]
    now = datetime.now(timezone.utc)
    processed = []
    # 1) Controls first.
    for well, entry in controls:
        passed = _evaluate_control(well.expected, entry["value"])
        well.result_value = entry["value"]
        well.result_status = "passed" if passed else "failed"
        well.resulted_by = user.username
        well.resulted_at = now
        processed.append({"position": well.position, "role": well.role,
                          "result_status": well.result_status,
                          "value": entry["value"]})
    if controls:
        session.flush()
        append_audit(session, actor=user.username,
                     action="lab.plate.results",
                     entity=f"PlateRun/{plate_id}",
                     payload={"controls": [
                         {"position": p["position"],
                          "result_status": p["result_status"]}
                         for p in processed],
                         "gate_open": _qc(_wells(session, plate_id))["gate_open"]})
        # Committed now: a patient entry refused by the gate below
        # must not roll the control results back with it.
        session.commit()
    # 2) Patient / replicate wells, judged against the (possibly
    #    just-opened) gate.
    gate_open = _qc(_wells(session, plate_id))["gate_open"]
    for well, entry in patients:
        if well.specimen_id is None or well.order_id is None:
            raise HTTPException(
                status_code=409,
                detail=(f"Well {well.position} on plate {plate_id} has "
                        "no assigned specimen/order to result"))
        if not gate_open:
            raise HTTPException(
                status_code=409,
                detail=(f"Plate {plate_id} QC gate is closed: patient "
                        f"well {well.position} cannot be resulted until "
                        "every control/calibrator/blank well has "
                        "resulted and none has failed (QC gate)"))
        order = lab._order_row(session, well.order_id)
        code = entry.get("code")
        if not code:
            concept = lab._order_code(session, order)
            if concept.get("panel"):
                raise HTTPException(
                    status_code=422,
                    detail=(f"Well {well.position}: order "
                            f"{well.order_id} is panel "
                            f"{concept['code']}; name the analyte with "
                            "'code' in the result entry"))
            code = concept["code"]
        analyte = lab._concept(session, str(code))
        value = entry["value"]
        if analyte is not None and not analyte.get("qualitative") \
                and isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                pass  # the shared validator refuses it honestly (422)
        outcome = lab._create_results(
            session, user, order, [{"code": str(code), "value": value}],
            source="plate")
        well.result_value = entry["value"]
        well.result_status = "resulted"
        well.resulted_by = user.username
        well.resulted_at = datetime.now(timezone.utc)
        session.flush()
        append_audit(session, actor=user.username,
                     action="lab.plate.results",
                     entity=f"PlateRun/{plate_id}",
                     payload={"position": well.position,
                              "specimen_id": well.specimen_id,
                              "order_id": well.order_id, "code": str(code),
                              "value": entry["value"],
                              "report_id": outcome["report"]["id"]})
        session.commit()
        processed.append({"position": well.position, "role": well.role,
                          "result_status": "resulted",
                          "value": entry["value"],
                          "order_id": well.order_id,
                          "report_id": outcome["report"]["id"]})
    return {"plate": _run_out(session, run, detail=True),
            "processed": processed}


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------

@router.post("/plates/{plate_id}/complete")
def complete_plate(plate_id: int, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.work"))):
    """Complete a plate run. Refused 409 while any ASSIGNED patient /
    replicate well is still unresulted; patient wells that were never
    assigned are fine."""
    run = _run_row(session, plate_id)
    if run.status == "complete":
        raise HTTPException(status_code=409,
                            detail=f"Plate {plate_id} is already complete")
    wells = _wells(session, plate_id)
    blocking = [w.position for w in wells
                if w.role in PATIENT_ROLES and w.specimen_id is not None
                and w.result_status != "resulted"]
    if blocking:
        raise HTTPException(
            status_code=409,
            detail=(f"Plate {plate_id} cannot complete: assigned patient "
                    f"well(s) {', '.join(blocking)} have not been resulted"))
    run.status = "complete"
    run.completed_by = user.username
    run.completed_at = datetime.now(timezone.utc)
    session.flush()
    append_audit(session, actor=user.username, action="lab.plate.complete",
                 entity=f"PlateRun/{plate_id}",
                 payload={"template_code": run.template_code,
                          "qc": _qc(wells)})
    session.commit()
    return _run_out(session, run, detail=True)
