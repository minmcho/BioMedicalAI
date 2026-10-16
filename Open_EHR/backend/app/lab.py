"""Phase 2 — Lab LIS (Laboratory Information System).

Order -> specimen -> result -> QC workflow, built ON the Studio
customization engine (docs/STUDIO_CUSTOMIZATION.md): everything
site-specific is published Studio configuration, never hardcoded —

- the orderable catalog (panels, analytes, units, reference ranges and
  critical thresholds) is terminology config, key ``lab-catalog``;
- the order/specimen state machine is workflow config, key
  ``lab-order-flow`` (the constant below is only the seed/fallback);

both seeded as published v1 at startup by :func:`ensure_lab_defaults`,
and editable through the normal /studio/config draft -> publish flow.

Flow and governance:
- Orders are FHIR ServiceRequest resources placed via POST /lab/orders;
  the ordered code must be an ACTIVE concept in the published catalog
  (unknown/inactive -> 422).
- Collection creates a linked FHIR Specimen with a server-generated
  accession id; lifecycle transitions run through the workflow config
  (illegal -> 409, every transition audit-logged).
- Results (manual entry, HL7 v2 ORU^R01 ingest, or CSV ingest) are
  created as PRELIMINARY Observations + a DiagnosticReport, all carrying
  the lab-unreleased visibility tag (app.fhir): they are invisible in the
  clinician inbox, the patient portal, and every patient read path until
  RELEASED. Verification (report preliminary -> final) and release
  (observations -> final, tag removed) are separate audited steps gated
  by the lab.verify capability — nothing is ever auto-released.
- Reference ranges + critical thresholds from the catalog are applied at
  result creation: Observation.interpretation is set to N / L / H / LL /
  HH (HL7 v3 ObservationInterpretation codes) and a report containing a
  critical (LL/HH) value carries a critical-result extension.
- QC runs are first-class records (app.models.LabQcRun) with pass/fail
  computed server-side. If the LATEST QC run for an analyte failed,
  verification and release of any report containing that analyte are
  blocked (409) until a passing QC run exists after it.

Ranges in v1 are age/sex-agnostic (one range per analyte from config) —
a stated limitation, see docs/sqa/PHASE2_SQA_REPORT.md.
"""
import csv
import io
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir
from .audit import append_audit
from .configstore import published_config
from .models import (ConfigStatus, FhirResource, LabQcRun, Role, StudioConfig,
                     User)
from .security import get_db, get_current_user, require_capability, require_roles

router = APIRouter(prefix="/lab", tags=["lab"])

STAFF_READ = (Role.admin, Role.configurator, Role.clinician, Role.viewer)

LAB_CATALOG_KEY = "lab-catalog"
LAB_WORKFLOW_KEY = "lab-order-flow"
LOINC = "http://loinc.org"
INTERPRETATION_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
CRITICAL_EXTENSION_URL = "urn:opencare:extension/critical-result"

# Fallback copy of the lab order state machine — the live machine is the
# published workflow config (seeded v1 below with exactly these
# transitions); this constant is used only if that config is disabled.
# resulted/verified/released are reachable only through the result /
# verify / release endpoints, not the generic transition endpoint.
DEFAULT_LAB_TRANSITIONS: dict[str, set[str]] = {
    "ordered": {"collected", "cancelled"},
    "collected": {"received", "rejected"},
    "received": {"in-process", "rejected"},
    "in-process": {"resulted", "rejected"},
    "resulted": {"verified"},
    "verified": {"released"},
    "released": set(),
    "rejected": set(),
    "cancelled": set(),
}
# States the generic transition endpoint will not move INTO: each has a
# dedicated endpoint that performs the associated work (result creation,
# verification sign-off, release) rather than a bare status flip.
GOVERNED_STATES = ("resulted", "verified", "released")
_SPECIMEN_MIRROR = {"collected", "received", "in-process", "resulted",
                    "verified", "released", "rejected"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Default Studio configuration (seeded published v1 at startup)
# ---------------------------------------------------------------------------

def default_lab_catalog_payload() -> dict:
    def analyte(code, display, unit, low=None, high=None,
                critical_low=None, critical_high=None):
        rng = {"unit": unit}
        if low is not None:
            rng["low"] = low
        if high is not None:
            rng["high"] = high
        if critical_low is not None:
            rng["criticalLow"] = critical_low
        if critical_high is not None:
            rng["criticalHigh"] = critical_high
        return {"code": code, "display": display, "active": True,
                "unit": unit, "referenceRange": rng}

    concepts = [
        # CBC analytes
        analyte("718-7", "Hemoglobin [Mass/volume] in Blood", "g/dL",
                12.0, 15.5, critical_low=7.0, critical_high=20.0),
        analyte("4544-3", "Hematocrit [Volume Fraction] of Blood by Automated count",
                "%", 36.0, 46.0, critical_low=20.0, critical_high=60.0),
        analyte("6690-2", "Leukocytes [#/volume] in Blood by Automated count",
                "10^9/L", 4.0, 11.0, critical_high=30.0),
        analyte("777-3", "Platelets [#/volume] in Blood by Automated count",
                "10^9/L", 150.0, 400.0, critical_low=50.0, critical_high=1000.0),
        # BMP analytes
        analyte("2345-7", "Glucose [Mass/volume] in Serum or Plasma", "mg/dL",
                70.0, 99.0, critical_low=40.0, critical_high=500.0),
        analyte("3094-0", "Urea nitrogen [Mass/volume] in Serum or Plasma", "mg/dL",
                7.0, 20.0, critical_high=100.0),
        analyte("2160-0", "Creatinine [Mass/volume] in Serum or Plasma", "mg/dL",
                0.5, 1.1, critical_high=5.0),
        analyte("2951-2", "Sodium [Moles/volume] in Serum or Plasma", "mmol/L",
                136.0, 145.0, critical_low=120.0, critical_high=160.0),
        analyte("2823-3", "Potassium [Moles/volume] in Serum or Plasma", "mmol/L",
                3.5, 5.1, critical_low=2.8, critical_high=6.0),
        # Diabetes monitoring (also charted by the SMART app / seed bundle)
        analyte("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "%",
                4.0, 5.6),
        # Panels (orderable; components are the resultable analytes)
        {"code": "58410-2", "display": "Complete blood count (hemogram) panel",
         "active": True, "panel": True,
         "components": ["718-7", "4544-3", "6690-2", "777-3"]},
        {"code": "51990-0", "display": "Basic metabolic panel", "active": True,
         "panel": True,
         "components": ["2345-7", "3094-0", "2160-0", "2951-2", "2823-3"]},
    ]
    return {"name": "OpenCare laboratory catalog (LOINC)",
            "system": LOINC, "concepts": concepts}


def default_lab_workflow_payload() -> dict:
    states = sorted(DEFAULT_LAB_TRANSITIONS.keys())
    transitions = [
        {"from": src, "to": dst}
        for src in sorted(DEFAULT_LAB_TRANSITIONS)
        for dst in sorted(DEFAULT_LAB_TRANSITIONS[src])
    ]
    return {"states": states, "transitions": transitions}


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


def ensure_lab_defaults(session: Session) -> None:
    """Seed the lab catalog + lab workflow as published Studio config v1
    (idempotent), exactly like the ADT workflow seed in studio_config."""
    _seed_published(session, "terminology", LAB_CATALOG_KEY,
                    default_lab_catalog_payload())
    _seed_published(session, "workflow", LAB_WORKFLOW_KEY,
                    default_lab_workflow_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Config accessors
# ---------------------------------------------------------------------------

def _catalog(session: Session) -> dict:
    cfg = published_config(session, "terminology", LAB_CATALOG_KEY)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=(f"No published lab catalog (terminology/{LAB_CATALOG_KEY}); "
                    "publish one in Studio before using the lab"))
    return cfg.payload or {}


def _concept(session: Session, code: str) -> dict | None:
    for concept in _catalog(session).get("concepts") or []:
        if isinstance(concept, dict) and concept.get("code") == code:
            return concept
    return None


def _lab_machine(session: Session) -> tuple[dict[str, set[str]], set[str]]:
    """(transitions, known states) from the published lab workflow config;
    falls back to the built-in map when no config is published."""
    cfg = published_config(session, "workflow", LAB_WORKFLOW_KEY)
    if cfg is not None:
        payload = cfg.payload or {}
        transitions: dict[str, set[str]] = {}
        for t in payload.get("transitions") or []:
            if isinstance(t, dict) and t.get("from") and t.get("to"):
                transitions.setdefault(t["from"], set()).add(t["to"])
        states = set(payload.get("states") or transitions.keys())
        for targets in transitions.values():
            states |= targets
        return transitions, states
    return DEFAULT_LAB_TRANSITIONS, set(DEFAULT_LAB_TRANSITIONS.keys())


# ---------------------------------------------------------------------------
# Resource helpers (staff-side: lab routes are role-gated, so internal row
# access here does not apply patient scoping — patients never reach /lab)
# ---------------------------------------------------------------------------

def _order_row(session: Session, order_id: str) -> FhirResource:
    return fhir._get_row("ServiceRequest", order_id, session)


def _specimen_row(session: Session, order_row: FhirResource) -> FhirResource | None:
    for ref in (order_row.body or {}).get("specimen") or []:
        reference = ref.get("reference") if isinstance(ref, dict) else None
        if reference and reference.startswith("Specimen/"):
            return fhir._get_row("Specimen", reference.split("/", 1)[1], session)
    return None


def _report_order(session: Session, report_row: FhirResource) -> FhirResource | None:
    for ref in (report_row.body or {}).get("basedOn") or []:
        reference = ref.get("reference") if isinstance(ref, dict) else None
        if reference and reference.startswith("ServiceRequest/"):
            try:
                return fhir._get_row("ServiceRequest", reference.split("/", 1)[1], session)
            except HTTPException:
                return None
    return None


def _result_observations(session: Session, report_row: FhirResource) -> list[FhirResource]:
    rows = []
    for ref in (report_row.body or {}).get("result") or []:
        reference = ref.get("reference") if isinstance(ref, dict) else None
        if reference and reference.startswith("Observation/"):
            rows.append(fhir._get_row("Observation", reference.split("/", 1)[1], session))
    return rows


def _set_status(row: FhirResource, status: str) -> None:
    body = dict(row.body or {})
    body["status"] = status
    row.body = body
    row.status = status
    row.version += 1


def _code_of(concept_body: dict) -> str | None:
    code = (concept_body or {}).get("code") or {}
    coding = code.get("coding") or []
    if coding and isinstance(coding[0], dict):
        return coding[0].get("code")
    return code.get("text") if isinstance(code, dict) else None


def _order_code(session: Session, order_row: FhirResource) -> dict:
    code = _code_of(order_row.body or {})
    concept = _concept(session, code) if code else None
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=f"Order {order_row.resource_id} names code '{code}', which is "
                   "not in the published lab catalog")
    return concept


# ---------------------------------------------------------------------------
# Interpretation (reference ranges + critical thresholds from config)
# ---------------------------------------------------------------------------

_INTERPRETATION_TEXT = {
    "N": "Normal", "L": "Low", "H": "High",
    "LL": "Critical low", "HH": "Critical high",
}


def interpret_value(value: float, concept: dict) -> str | None:
    """HL7 v3 ObservationInterpretation code for a numeric result against
    the analyte's configured range. Critical thresholds win over the
    reference range; returns None when the analyte has no range data."""
    rng = concept.get("referenceRange") or {}
    low, high = rng.get("low"), rng.get("high")
    crit_low, crit_high = rng.get("criticalLow"), rng.get("criticalHigh")
    if crit_low is None and crit_high is None and low is None and high is None:
        return None
    if crit_low is not None and value <= crit_low:
        return "LL"
    if crit_high is not None and value >= crit_high:
        return "HH"
    if low is not None and value < low:
        return "L"
    if high is not None and value > high:
        return "H"
    return "N"


def _is_critical(code: str | None) -> bool:
    return code in ("LL", "HH")


# ---------------------------------------------------------------------------
# QC gating
# ---------------------------------------------------------------------------

def _qc_out(run: LabQcRun) -> dict:
    return {
        "id": run.id, "analyte_code": run.analyte_code,
        "control_material": run.control_material,
        "expected_low": run.expected_low, "expected_high": run.expected_high,
        "measured_value": run.measured_value, "unit": run.unit,
        "passed": run.passed, "performed_by": run.performed_by,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def _latest_qc(session: Session, analyte_code: str) -> LabQcRun | None:
    return session.scalars(
        select(LabQcRun).where(LabQcRun.analyte_code == analyte_code)
        .order_by(LabQcRun.id.desc())).first()


def qc_blockers(session: Session, analyte_codes) -> list[dict]:
    """Analytes whose LATEST QC run failed. A patient result for such an
    analyte cannot be verified or released until a passing QC run exists
    after the failure. No QC history at all does not block (only a failed
    run does) — that is the rule the phase specifies."""
    blockers = []
    for code in sorted(set(analyte_codes)):
        latest = _latest_qc(session, code)
        if latest is not None and not latest.passed:
            blockers.append({"analyte_code": code, "qc_run": _qc_out(latest)})
    return blockers


def _enforce_qc(session: Session, report_row: FhirResource) -> None:
    codes = [_code_of(o.body or {}) for o in _result_observations(session, report_row)]
    blockers = qc_blockers(session, [c for c in codes if c])
    if blockers:
        names = ", ".join(b["analyte_code"] for b in blockers)
        raise HTTPException(
            status_code=409,
            detail=(f"QC block: the latest QC run failed for {names}; "
                    "record a passing QC run for each blocked analyte "
                    "before this report can proceed"))


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


def _extract_order_code(body: dict) -> str | None:
    raw = body.get("code")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return _code_of(body)
    return None


@router.post("/orders", status_code=201)
async def place_order(request: Request, session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.order"))):
    """Place a lab order. The code must be an ACTIVE concept (panel or
    analyte) in the published Studio lab catalog — ordering is configuration
    validation, not a free-text request."""
    body = await _json_body(request)
    code = _extract_order_code(body)
    if not code:
        raise HTTPException(status_code=422,
                            detail="Lab order requires 'code' (a catalog code or "
                                   "a CodeableConcept naming one)")
    concept = _concept(session, code)
    if concept is None:
        raise HTTPException(status_code=422,
                            detail=f"Unknown lab code '{code}': not in the "
                                   "published lab catalog")
    if concept.get("active") is False:
        raise HTTPException(status_code=422,
                            detail=f"Lab code '{code}' ({concept.get('display')}) "
                                   "is inactive in the published lab catalog")
    subject = body.get("subject") or {}
    subject_ref = subject.get("reference") if isinstance(subject, dict) else None
    if not subject_ref and body.get("patient"):
        subject_ref = fhir._normalize_patient_ref(str(body["patient"]))
    if not subject_ref:
        raise HTTPException(status_code=422,
                            detail="Lab order requires 'subject.reference' "
                                   "(or 'patient')")
    if not subject_ref.startswith("Patient/"):
        raise HTTPException(status_code=422,
                            detail="Lab order subject must reference a Patient")
    patient_id = subject_ref.split("/", 1)[1]
    fhir._get_row("Patient", patient_id, session)  # 404 if the patient is unknown

    order_body = {
        "resourceType": "ServiceRequest",
        "status": "ordered",
        "intent": "order",
        "category": [{"coding": [{
            "system": "http://snomed.info/sct", "code": "108252007",
            "display": "Laboratory procedure"}]}],
        "code": {"coding": [{"system": LOINC, "code": code,
                             "display": concept.get("display")}],
                 "text": concept.get("display")},
        "subject": {"reference": subject_ref},
        "authoredOn": _now_iso(),
        "requester": {"display": user.username},
    }
    if body.get("id"):
        order_body["id"] = str(body["id"])
    if isinstance(body.get("encounter"), dict) and body["encounter"].get("reference"):
        order_body["encounter"] = {"reference": body["encounter"]["reference"]}
    if body.get("priority"):
        order_body["priority"] = body["priority"]
    return fhir.create_resource("ServiceRequest", order_body, session, user)


@router.get("/orders")
def list_orders(status: str | None = Query(default=None),
                patient: str | None = Query(default=None),
                session: Session = Depends(get_db),
                user: User = Depends(require_roles(*STAFF_READ))):
    stmt = select(FhirResource).where(FhirResource.resource_type == "ServiceRequest")
    if status:
        stmt = stmt.where(FhirResource.status == status)
    if patient:
        stmt = stmt.where(FhirResource.patient_ref == fhir._normalize_patient_ref(patient))
    rows = session.scalars(stmt.order_by(FhirResource.created_at.asc(),
                                         FhirResource.id.asc())).all()
    return fhir._bundle(rows)


@router.get("/orders/{order_id}")
def order_detail(order_id: str, session: Session = Depends(get_db),
                 user: User = Depends(require_roles(*STAFF_READ))):
    order = _order_row(session, order_id)
    specimen = _specimen_row(session, order)
    reports = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "DiagnosticReport")
        .order_by(FhirResource.created_at.asc())).all()
    own_reports = []
    for report in reports:
        refs = [(ref or {}).get("reference")
                for ref in (report.body or {}).get("basedOn") or []]
        if f"ServiceRequest/{order_id}" in refs:
            own_reports.append(fhir._render(report))
    return {"order": fhir._render(order),
            "specimen": fhir._render(specimen) if specimen else None,
            "reports": own_reports}


@router.post("/orders/{order_id}/transition")
async def transition_order(order_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.work"))):
    """Advance an order (and its specimen, mirrored) through the workflow
    config. Resulted/verified/released are governed states with dedicated
    endpoints — a bare status flip into them is refused with 422."""
    payload = await _json_body(request)
    target = payload.get("to_status") or payload.get("status")
    if not target:
        raise HTTPException(status_code=422, detail="Transition requires 'to_status'")
    if target in GOVERNED_STATES:
        raise HTTPException(
            status_code=422,
            detail=(f"Status '{target}' is governed: it is reached through the "
                    "result-entry, verify and release endpoints, not a bare "
                    "transition"))
    order = _order_row(session, order_id)
    current = (order.body or {}).get("status")
    transitions, known_states = _lab_machine(session)
    if target not in known_states:
        raise HTTPException(status_code=422, detail=f"Unknown lab order status: {target}")
    if target not in transitions.get(current, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Illegal lab order transition '{current}' -> '{target}'")

    specimen = _specimen_row(session, order)
    specimen_id = None
    specimen_fresh = False
    if target == "collected":
        if specimen is not None:
            raise HTTPException(status_code=409,
                                detail="Order already has a collected specimen")
        specimen_type = payload.get("specimen_type") or "Blood specimen"
        count = len(session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Specimen")).all())
        accession = (f"ACC-{datetime.now(timezone.utc):%Y%m%d}-{count + 1:04d}")
        specimen_body = {
            "resourceType": "Specimen",
            "id": str(uuid.uuid4()),
            "status": "collected",
            "accessionIdentifier": {"system": "urn:opencare:accession",
                                    "value": accession},
            "type": {"text": specimen_type},
            "subject": {"reference": (order.body or {}).get("subject", {}).get("reference")},
            "request": [{"reference": f"ServiceRequest/{order_id}"}],
            "collection": {"collectedDateTime": _now_iso()},
        }
        rendered = fhir.create_resource("Specimen", specimen_body, session, user)
        specimen_id = rendered["id"]
        specimen_fresh = True  # created already carrying the target status
        order_body = dict(order.body or {})
        order_body["specimen"] = [{"reference": f"Specimen/{specimen_id}"}]
        order.body = order_body
        specimen = fhir._get_row("Specimen", specimen_id, session)
    elif target == "received":
        if specimen is None:
            raise HTTPException(status_code=409,
                                detail="Order has no specimen to receive")
        body = dict(specimen.body or {})
        body["receivedDateTime"] = _now_iso()
        specimen.body = body
    elif target == "rejected":
        reason = payload.get("reason")
        if not reason or not isinstance(reason, str):
            raise HTTPException(status_code=422,
                                detail="Rejecting a specimen requires 'reason'")
        if specimen is not None:
            body = dict(specimen.body or {})
            body["note"] = [{"text": f"Rejected: {reason}"}]
            specimen.body = body
        order_body = dict(order.body or {})
        order_body["statusReason"] = {"text": reason}
        order.body = order_body

    _set_status(order, target)
    if specimen is not None and target in _SPECIMEN_MIRROR and not specimen_fresh:
        _set_status(specimen, target)
        specimen_id = specimen.resource_id
    session.flush()
    append_audit(session, actor=user.username, action="lab.order.transition",
                 entity=f"ServiceRequest/{order_id}",
                 payload={"from_status": current, "to_status": target,
                          "specimen_id": specimen_id,
                          "reason": payload.get("reason")})
    session.commit()
    return {"order": fhir._render(order),
            "specimen": fhir._render(specimen) if specimen else None}


# ---------------------------------------------------------------------------
# Specimens
# ---------------------------------------------------------------------------

@router.get("/specimens")
def list_specimens(status: str | None = Query(default=None),
                   patient: str | None = Query(default=None),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_roles(*STAFF_READ))):
    stmt = select(FhirResource).where(FhirResource.resource_type == "Specimen")
    if status:
        stmt = stmt.where(FhirResource.status == status)
    if patient:
        stmt = stmt.where(FhirResource.patient_ref == fhir._normalize_patient_ref(patient))
    rows = session.scalars(stmt.order_by(FhirResource.created_at.asc(),
                                         FhirResource.id.asc())).all()
    return fhir._bundle(rows)


# ---------------------------------------------------------------------------
# Results: creation core shared by manual entry and analyser ingest
# ---------------------------------------------------------------------------

def _validate_results(session: Session, order: FhirResource,
                      results) -> list[tuple[dict, float, str | None]]:
    """Validate raw [{code, value, unit?}] against the order + catalog.
    Returns [(concept, value, unit)]. Panel orders accept only their
    component analytes; single-analyte orders accept only their own code."""
    if not isinstance(results, list) or not results:
        raise HTTPException(status_code=422,
                            detail="'results' must be a non-empty list")
    concept = _order_code(session, order)
    if concept.get("panel"):
        allowed = set(concept.get("components") or [])
        order_label = f"panel {concept['code']} components"
    else:
        allowed = {concept["code"]}
        order_label = f"ordered analyte {concept['code']}"
    checked = []
    for i, item in enumerate(results):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail=f"results[{i}] must be an object")
        code = item.get("code")
        analyte = _concept(session, code) if code else None
        if analyte is None:
            raise HTTPException(status_code=422,
                                detail=f"results[{i}].code '{code}' is not in the "
                                       "published lab catalog")
        if analyte.get("panel"):
            raise HTTPException(status_code=422,
                                detail=f"results[{i}].code '{code}' is a panel; "
                                       "results are entered per analyte")
        if analyte.get("active") is False:
            raise HTTPException(status_code=422,
                                detail=f"results[{i}].code '{code}' is inactive in "
                                       "the published lab catalog")
        if code not in allowed:
            raise HTTPException(status_code=422,
                                detail=f"results[{i}].code '{code}' is not one of "
                                       f"the {order_label} for this order")
        value = item.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(status_code=422,
                                detail=f"results[{i}].value must be a number")
        unit = item.get("unit") or analyte.get("unit") or \
            (analyte.get("referenceRange") or {}).get("unit")
        checked.append((analyte, float(value), unit))
    return checked


def _create_results(session: Session, user: User, order: FhirResource,
                      results, source: str) -> dict:
    """Create preliminary, unreleased-tagged Observations + DiagnosticReport
    for an order in received/in-process state, and move order + specimen to
    resulted. Used identically by manual entry and analyser ingest: ingest
    results are drafts in exactly the same way (no auto-release)."""
    current = (order.body or {}).get("status")
    if current not in ("received", "in-process"):
        raise HTTPException(
            status_code=409,
            detail=(f"Results can only be entered for an order whose specimen "
                    f"has been received (status is '{current}')"))
    checked = _validate_results(session, order, results)
    order_concept = _order_code(session, order)
    subject_ref = (order.body or {}).get("subject", {}).get("reference")
    specimen = _specimen_row(session, order)
    now = _now_iso()

    observations = []
    any_critical = False
    for analyte, value, unit in checked:
        interp_code = interpret_value(value, analyte)
        if _is_critical(interp_code):
            any_critical = True
        obs_body = {
            "resourceType": "Observation",
            "id": str(uuid.uuid4()),
            "status": "preliminary",
            "category": [{"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                "code": "laboratory", "display": "Laboratory"}]}],
            "code": {"coding": [{"system": LOINC, "code": analyte["code"],
                                 "display": analyte.get("display")}],
                     "text": analyte.get("display")},
            "subject": {"reference": subject_ref},
            "basedOn": [{"reference": f"ServiceRequest/{order.resource_id}"}],
            "effectiveDateTime": now,
            "valueQuantity": {"value": value, "unit": unit},
            "meta": {"tag": [dict(fhir.LAB_UNRELEASED_TAG)]},
        }
        if specimen is not None:
            obs_body["specimen"] = {"reference": f"Specimen/{specimen.resource_id}"}
        if (order.body or {}).get("encounter"):
            obs_body["encounter"] = order.body["encounter"]
        if interp_code:
            obs_body["interpretation"] = [{
                "coding": [{"system": INTERPRETATION_SYSTEM, "code": interp_code,
                            "display": _INTERPRETATION_TEXT[interp_code]}],
                "text": _INTERPRETATION_TEXT[interp_code]}]
        rng = analyte.get("referenceRange") or {}
        if rng.get("low") is not None or rng.get("high") is not None:
            ref_range = {}
            if rng.get("low") is not None:
                ref_range["low"] = {"value": rng["low"], "unit": unit}
            if rng.get("high") is not None:
                ref_range["high"] = {"value": rng["high"], "unit": unit}
            obs_body["referenceRange"] = [ref_range]
        observations.append(fhir.create_resource("Observation", obs_body, session, user))

    report_body = {
        "resourceType": "DiagnosticReport",
        "id": str(uuid.uuid4()),
        "status": "preliminary",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
            "code": "LAB", "display": "Laboratory"}]}],
        "code": {"coding": [{"system": LOINC, "code": order_concept["code"],
                             "display": order_concept.get("display")}],
                 "text": order_concept.get("display")},
        "subject": {"reference": subject_ref},
        "basedOn": [{"reference": f"ServiceRequest/{order.resource_id}"}],
        "effectiveDateTime": now,
        "result": [{"reference": f"Observation/{o['id']}"} for o in observations],
        "meta": {"tag": [dict(fhir.LAB_UNRELEASED_TAG)]},
    }
    if specimen is not None:
        report_body["specimen"] = [{"reference": f"Specimen/{specimen.resource_id}"}]
    if (order.body or {}).get("encounter"):
        report_body["encounter"] = order.body["encounter"]
    if any_critical:
        report_body["extension"] = [{"url": CRITICAL_EXTENSION_URL,
                                     "valueBoolean": True}]
    report = fhir.create_resource("DiagnosticReport", report_body, session, user)

    _set_status(order, "resulted")
    if specimen is not None:
        _set_status(specimen, "resulted")
    session.flush()
    append_audit(session, actor=user.username, action="lab.results.create",
                 entity=f"DiagnosticReport/{report['id']}",
                 payload={"order_id": order.resource_id, "source": source,
                          "result_count": len(observations),
                          "critical": any_critical})
    session.commit()
    return {"report": report, "observations": observations,
            "critical": any_critical}


@router.post("/orders/{order_id}/results", status_code=201)
async def enter_results(order_id: str, request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.work"))):
    payload = await _json_body(request)
    order = _order_row(session, order_id)
    return _create_results(session, user, order, payload.get("results"),
                            source="manual")


# ---------------------------------------------------------------------------
# Analyser ingest: HL7 v2 ORU^R01 and CSV
# ---------------------------------------------------------------------------

def _hl7_field(parts: list[str], index: int) -> str:
    return parts[index] if index < len(parts) else ""


def _hl7_component(value: str, component: int = 0) -> str:
    pieces = value.split("^")
    return pieces[component] if component < len(pieces) else ""


def parse_hl7_oru(content: str) -> tuple[str, str, list[dict]]:
    """Minimal dependency-free HL7 v2 ORU^R01 reader.

    Returns (order_id, patient_id, [{code, value, unit}]). Reads PID-3
    (patient id), OBR-2/OBR-3 (order id) and OBX-3/5/6 (code, value,
    units). Malformed input raises 422 with the parse problem — ingest
    never guesses."""
    lines = [ln.strip() for ln in
             content.replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
    if not lines or not lines[0].startswith("MSH"):
        raise HTTPException(status_code=422,
                            detail="HL7 ingest requires a message starting with MSH")
    sep = lines[0][3] if len(lines[0]) > 3 else "|"
    patient_id = order_id = None
    results = []
    saw_obx = False
    for line in lines:
        parts = line.split(sep)
        kind = parts[0]
        if kind == "PID":
            patient_id = _hl7_component(_hl7_field(parts, 3), 0) or patient_id
        elif kind == "OBR":
            order_id = (_hl7_field(parts, 3) or _hl7_field(parts, 2) or order_id)
        elif kind == "OBX":
            saw_obx = True
            code = _hl7_component(_hl7_field(parts, 3), 0)
            raw_value = _hl7_field(parts, 5)
            # OBX-6 taken whole: units like 10^9/L legitimately contain '^'.
            unit = _hl7_field(parts, 6) or None
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=422,
                    detail=f"OBX value '{raw_value}' for code '{code}' is not numeric")
            if not code:
                raise HTTPException(status_code=422,
                                    detail="OBX segment is missing its observation code (OBX-3)")
            results.append({"code": code, "value": value, "unit": unit})
    if not order_id:
        raise HTTPException(status_code=422,
                            detail="HL7 message has no order id (OBR-2/OBR-3)")
    if not patient_id:
        raise HTTPException(status_code=422,
                            detail="HL7 message has no patient id (PID-3)")
    if not saw_obx or not results:
        raise HTTPException(status_code=422,
                            detail="HL7 message contains no OBX result segments")
    return order_id, patient_id, results


def parse_csv_results(content: str) -> list[tuple[str, str | None, list[dict]]]:
    """CSV fallback analyser format: one row per result with headers
    order_id, code, value and optional patient_id, unit. Rows are grouped
    by order. Returns [(order_id, patient_id, [results])]."""
    reader = csv.DictReader(io.StringIO(content))
    fields = {(f or "").strip().lower() for f in (reader.fieldnames or [])}
    missing = {"order_id", "code", "value"} - fields
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"CSV ingest is missing required column(s): {', '.join(sorted(missing))}")
    grouped: dict[str, tuple[str | None, list[dict]]] = {}
    order_sequence: list[str] = []
    for row in reader:
        order_id = (row.get("order_id") or "").strip()
        code = (row.get("code") or "").strip()
        raw_value = (row.get("value") or "").strip()
        if not order_id and not code and not raw_value:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"CSV value '{raw_value}' for code '{code}' is not numeric")
        if order_id not in grouped:
            grouped[order_id] = ((row.get("patient_id") or "").strip() or None, [])
            order_sequence.append(order_id)
        grouped[order_id][1].append(
            {"code": code, "value": value,
             "unit": (row.get("unit") or "").strip() or None})
    if not order_sequence:
        raise HTTPException(status_code=422, detail="CSV ingest contains no result rows")
    return [(oid, grouped[oid][0], grouped[oid][1]) for oid in order_sequence]


@router.post("/ingest", status_code=201)
async def ingest(request: Request, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("lab.work"))):
    """Analyser ingest. Accepts {"format": "hl7"|"csv", "content": "..."}.
    Ingested results are DRAFTS (preliminary + unreleased tag) created
    through the exact same path as manual entry — verification and
    release remain separate human steps; nothing is auto-released."""
    payload = await _json_body(request)
    fmt = str(payload.get("format") or "").strip().lower()
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=422, detail="Ingest requires 'content'")
    if fmt == "hl7":
        groups = [parse_hl7_oru(content)]
    elif fmt == "csv":
        groups = parse_csv_results(content)
    else:
        raise HTTPException(status_code=422,
                            detail="Ingest 'format' must be 'hl7' or 'csv'")

    created = []
    for order_id, patient_id, results in groups:
        try:
            order = fhir._get_row("ServiceRequest", order_id, session)
        except HTTPException:
            raise HTTPException(
                status_code=422,
                detail=f"Ingest references unknown lab order '{order_id}'")
        subject_ref = (order.body or {}).get("subject", {}).get("reference")
        if patient_id and subject_ref != f"Patient/{patient_id}":
            raise HTTPException(
                status_code=422,
                detail=(f"Ingest patient '{patient_id}' does not match order "
                        f"{order_id}'s patient ({subject_ref})"))
        outcome = _create_results(session, user, order, results, source=f"ingest-{fmt}")
        created.append({"order_id": order_id,
                        "report_id": outcome["report"]["id"],
                        "result_count": len(outcome["observations"]),
                        "critical": outcome["critical"]})
    return {"ingested": created,
            "report_count": len(created),
            "result_count": sum(c["result_count"] for c in created)}


# ---------------------------------------------------------------------------
# Verification queue, verify, release
# ---------------------------------------------------------------------------

def _report_summary(session: Session, report: FhirResource,
                    order: FhirResource | None) -> dict:
    observations = _result_observations(session, report)
    results = []
    critical = False
    for obs in observations:
        body = obs.body or {}
        interp = ((body.get("interpretation") or [{}])[0].get("coding") or [{}])[0].get("code")
        if _is_critical(interp):
            critical = True
        quantity = body.get("valueQuantity") or {}
        results.append({
            "observation_id": obs.resource_id,
            "code": _code_of(body),
            "display": (body.get("code") or {}).get("text"),
            "value": quantity.get("value"), "unit": quantity.get("unit"),
            "interpretation": interp, "status": body.get("status"),
        })
    codes = [r["code"] for r in results if r["code"]]
    order_status = (order.body or {}).get("status") if order else None
    return {
        "report": fhir._render(report),
        "order_id": order.resource_id if order else None,
        "order_status": order_status,
        "stage": ("awaiting-verification" if order_status == "resulted"
                  else "awaiting-release" if order_status == "verified"
                  else order_status),
        "critical": critical,
        "qc_blocked": qc_blockers(session, codes),
        "results": results,
    }


@router.get("/verification-queue")
def verification_queue(session: Session = Depends(get_db),
                       user: User = Depends(require_roles(*STAFF_READ))):
    """Lab drafts awaiting a human step: reports whose order is resulted
    (awaiting verification) or verified (awaiting release), each with its
    results, critical flags and live QC-block state."""
    reports = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "DiagnosticReport")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    items = []
    for report in reports:
        order = _report_order(session, report)
        if order is None:
            continue
        if (order.body or {}).get("status") in ("resulted", "verified"):
            items.append(_report_summary(session, report, order))
    return {"items": items, "total": len(items)}


def _lab_report(session: Session, report_id: str) -> tuple[FhirResource, FhirResource]:
    report = fhir._get_row("DiagnosticReport", report_id, session)
    order = _report_order(session, report)
    if order is None:
        raise HTTPException(status_code=422,
                            detail=f"DiagnosticReport/{report_id} is not a lab "
                                   "report (no ServiceRequest in basedOn)")
    return report, order


@router.post("/reports/{report_id}/verify")
def verify_report(report_id: str, session: Session = Depends(get_db),
                  user: User = Depends(require_capability("lab.verify"))):
    """Clinical verification: report preliminary -> final, order resulted
    -> verified. Blocked (409) while the latest QC run for any resulted
    analyte has failed. The report remains unreleased (invisible to the
    patient and the inbox) until the separate release step."""
    report, order = _lab_report(session, report_id)
    if (report.body or {}).get("status") != "preliminary" or \
            (order.body or {}).get("status") != "resulted":
        raise HTTPException(
            status_code=409,
            detail=(f"Report {report_id} is not awaiting verification "
                    f"(report status '{(report.body or {}).get('status')}', "
                    f"order status '{(order.body or {}).get('status')}')"))
    _enforce_qc(session, report)
    _set_status(report, "final")
    _set_status(order, "verified")
    specimen = _specimen_row(session, order)
    if specimen is not None:
        _set_status(specimen, "verified")
    session.flush()
    append_audit(session, actor=user.username, action="lab.report.verify",
                 entity=f"DiagnosticReport/{report_id}",
                 payload={"order_id": order.resource_id, "verified_by": user.username})
    session.commit()
    return {"report": fhir._render(report), "order": fhir._render(order)}


@router.post("/reports/{report_id}/release")
def release_report(report_id: str, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.verify"))):
    """Release a verified report: observations become final, the
    unreleased visibility tag is removed from report + observations (this
    is the moment results appear in the clinician inbox and the patient
    portal), and the order/specimen move to released. QC gate enforced
    again — a QC failure recorded after verification still blocks."""
    report, order = _lab_report(session, report_id)
    if (order.body or {}).get("status") != "verified":
        raise HTTPException(
            status_code=409,
            detail=(f"Report {report_id} cannot be released before verification "
                    f"(order status '{(order.body or {}).get('status')}')"))
    _enforce_qc(session, report)
    now = _now_iso()
    for obs in _result_observations(session, report):
        body = dict(obs.body or {})
        body["status"] = "final"
        obs.body = body
        obs.status = "final"
        obs.version += 1
        _strip_unreleased(obs)
    report_body = dict(report.body or {})
    report_body["issued"] = now
    report.body = report_body
    report.version += 1
    _strip_unreleased(report)
    _set_status(order, "released")
    specimen = _specimen_row(session, order)
    if specimen is not None:
        _set_status(specimen, "released")
    session.flush()
    append_audit(session, actor=user.username, action="lab.report.release",
                 entity=f"DiagnosticReport/{report_id}",
                 payload={"order_id": order.resource_id, "released_by": user.username})
    session.commit()
    return {"report": fhir._render(report), "order": fhir._render(order)}


def _strip_unreleased(row: FhirResource) -> None:
    body = dict(row.body or {})
    meta = dict(body.get("meta") or {})
    tags = [t for t in (meta.get("tag") or [])
            if not (isinstance(t, dict)
                    and t.get("system") == fhir.LAB_VISIBILITY_SYSTEM
                    and t.get("code") == fhir.LAB_UNRELEASED_CODE)]
    if tags:
        meta["tag"] = tags
    else:
        meta.pop("tag", None)
    body["meta"] = meta
    row.body = body


# ---------------------------------------------------------------------------
# QC endpoints
# ---------------------------------------------------------------------------

@router.post("/qc", status_code=201)
async def create_qc_run(request: Request, session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.work"))):
    """Record a QC run. `passed` is computed here from the expected range
    — a caller-supplied verdict is never trusted."""
    payload = await _json_body(request)
    code = payload.get("analyte_code")
    if not code or _concept(session, str(code)) is None:
        raise HTTPException(status_code=422,
                            detail=f"QC 'analyte_code' '{code}' is not in the "
                                   "published lab catalog")
    numbers = {}
    for field in ("expected_low", "expected_high", "measured_value"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(status_code=422, detail=f"QC '{field}' must be a number")
        numbers[field] = float(value)
    if numbers["expected_low"] > numbers["expected_high"]:
        raise HTTPException(status_code=422,
                            detail="QC 'expected_low' must not exceed 'expected_high'")
    material = payload.get("control_material")
    if not isinstance(material, str) or not material.strip():
        raise HTTPException(status_code=422,
                            detail="QC requires 'control_material' (control lot/material)")
    run = LabQcRun(
        analyte_code=str(code), control_material=material,
        expected_low=numbers["expected_low"], expected_high=numbers["expected_high"],
        measured_value=numbers["measured_value"],
        unit=payload.get("unit") if isinstance(payload.get("unit"), str) else None,
        passed=numbers["expected_low"] <= numbers["measured_value"] <= numbers["expected_high"],
        performed_by=user.username)
    session.add(run)
    session.flush()
    append_audit(session, actor=user.username, action="lab.qc.run",
                 entity=f"LabQcRun/{run.id}",
                 payload={"analyte_code": run.analyte_code,
                          "control_material": run.control_material,
                          "measured_value": run.measured_value,
                          "passed": run.passed})
    session.commit()
    return _qc_out(run)


@router.get("/qc")
def list_qc_runs(analyte_code: str | None = Query(default=None),
                 session: Session = Depends(get_db),
                 user: User = Depends(require_roles(*STAFF_READ))):
    stmt = select(LabQcRun).order_by(LabQcRun.id.desc())
    if analyte_code:
        stmt = stmt.where(LabQcRun.analyte_code == analyte_code)
    return [_qc_out(r) for r in session.scalars(stmt).all()]


@router.get("/qc/status")
def qc_status(analyte_code: str = Query(...), session: Session = Depends(get_db),
              user: User = Depends(require_roles(*STAFF_READ))):
    latest = _latest_qc(session, analyte_code)
    return {"analyte_code": analyte_code,
            "blocked": bool(latest is not None and not latest.passed),
            "latest": _qc_out(latest) if latest else None}


# ---------------------------------------------------------------------------
# Catalog (effective Studio config, for lab clients)
# ---------------------------------------------------------------------------

@router.get("/catalog")
def lab_catalog(session: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    if user.role == Role.patient:
        raise HTTPException(status_code=403,
                            detail="The lab catalog is staff configuration data")
    catalog_cfg = published_config(session, "terminology", LAB_CATALOG_KEY)
    workflow_cfg = published_config(session, "workflow", LAB_WORKFLOW_KEY)
    return {
        "catalog": catalog_cfg.payload if catalog_cfg else None,
        "catalog_version": catalog_cfg.version if catalog_cfg else None,
        "workflow": workflow_cfg.payload if workflow_cfg else None,
        "workflow_version": workflow_cfg.version if workflow_cfg else None,
    }
