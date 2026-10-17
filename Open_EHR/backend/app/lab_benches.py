"""Phase 6B — laboratory benches + operations (roadmap §7).

The bench surfaces that sit on the Phase 2 order -> specimen spine
(app.lab) without forking it:

- MICROBIOLOGY: culture orders (catalog concepts with bench
  "microbiology") run the published `workflow/lab-micro-flow` machine
  through the generic /lab transition endpoint (collection + plating);
  the staged reporting steps live here — preliminary report, organism
  identification, susceptibility (S/I/R) — each writing tagged,
  unreleased Observations on ONE DiagnosticReport, and a finalize
  step (lab.verify) that releases it. Until finalize, the report
  carries the lab-unreleased visibility tag, so preliminary culture
  results are invisible to the portal, the inbox and every patient
  read path — the Phase 2 invariant, extended, not weakened.
- BLOOD BANK: type & screen results are qualitative results on the
  standard spine (app.lab); here: the unit inventory (own table —
  inventory is not a FHIR resource), issue/return against a patient
  order, the transfusion record (a FHIR Procedure), and the reaction
  workflow — a reaction quarantines the unit and places a HOLD on the
  patient that blocks further issues until a clinician clears it.
- ANATOMIC PATHOLOGY: orders (bench "pathology") run the published
  `workflow/lab-pathology-flow` machine; the synoptic report is a
  QuestionnaireResponse against the published `form/pathology-synoptic`
  stored through the Phase 5 documentation machinery, attached to a
  preliminary tagged DiagnosticReport; sign-out (lab.verify) is the
  release-equivalent two-step completion — unsigned reports are
  invisible outside the lab.
- OPERATIONS: EQA/proficiency records, the instrument registry +
  maintenance/calibration log, and the TAT dashboard (live query over
  the FHIR store's custody timestamps — no warehouse dependency).

Everything site-specific stays Studio configuration (catalog benches,
terminology lists, workflow payloads, the synoptic form); capabilities
are the existing lab.read / lab.work / lab.verify.
"""
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import documentation, fhir, lab
from .audit import append_audit
from .configstore import published_config
from .models import (BloodBankHold, BloodUnit, FhirResource, LabEqaRecord,
                     LabInstrument, LabInstrumentLog, User)
from .security import get_db, require_capability

router = APIRouter(prefix="/lab", tags=["lab-benches"])

ORGANISM_SYSTEM = "urn:opencare:micro-organism"
ANTIBIOTIC_SYSTEM = "urn:opencare:micro-antibiotic"
SUSCEPTIBILITY_VALUES = ("S", "I", "R")
INSTRUMENT_STATUSES = ("active", "maintenance", "down")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


def _bench_order(session: Session, order_id: str, bench: str) -> FhirResource:
    order = lab._order_row(session, order_id)
    concept = lab._order_code(session, order)
    if concept.get("bench") != bench:
        raise HTTPException(
            status_code=422,
            detail=(f"Order {order_id} is on the "
                    f"'{concept.get('bench') or 'standard'}' bench, not "
                    f"'{bench}'"))
    return order


def _bench_report(session: Session, order: FhirResource,
                  category_code: str) -> FhirResource | None:
    """The bench DiagnosticReport for an order (matched by basedOn +
    report category code), or None when not yet created."""
    reports = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "DiagnosticReport")
        .order_by(FhirResource.created_at.asc())).all()
    for report in reports:
        refs = [(ref or {}).get("reference")
                for ref in (report.body or {}).get("basedOn") or []]
        if f"ServiceRequest/{order.resource_id}" not in refs:
            continue
        categories = (report.body or {}).get("category") or []
        codes = [cd.get("code") for c in categories if isinstance(c, dict)
                 for cd in (c.get("coding") or []) if isinstance(cd, dict)]
        if category_code in codes:
            return report
    return None


def _require_status(order: FhirResource, *statuses: str, action: str) -> None:
    current = (order.body or {}).get("status")
    if current not in statuses:
        raise HTTPException(
            status_code=409,
            detail=(f"Cannot {action} order {order.resource_id} in status "
                    f"'{current}' (requires {' or '.join(statuses)})"))


def _terminology_concept(session: Session, kind_key: str, code: str) -> dict | None:
    cfg = published_config(session, "terminology", kind_key)
    for concept in (cfg.payload or {}).get("concepts") or [] if cfg else []:
        if isinstance(concept, dict) and concept.get("code") == code:
            return concept
    return None


# ---------------------------------------------------------------------------
# Microbiology
# ---------------------------------------------------------------------------

@router.get("/micro/terminology")
def micro_terminology(session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.read"))):
    """The published organism + antibiotic lists the bench validates
    against (Studio terminology config)."""
    organisms = published_config(session, "terminology", lab.ORGANISMS_KEY)
    antibiotics = published_config(session, "terminology", lab.ANTIBIOTICS_KEY)
    return {
        "organisms": organisms.payload if organisms else None,
        "organisms_version": organisms.version if organisms else None,
        "antibiotics": antibiotics.payload if antibiotics else None,
        "antibiotics_version": antibiotics.version if antibiotics else None,
    }


def _micro_observation(session: Session, user: User, order: FhirResource,
                       report: FhirResource, *, code_coding: dict,
                       code_text: str, value_key: str, value,
                       interpretation: str | None = None) -> dict:
    subject_ref = (order.body or {}).get("subject", {}).get("reference")
    obs_body = {
        "resourceType": "Observation", "id": str(uuid.uuid4()),
        "status": "preliminary",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
            "code": "laboratory", "display": "Laboratory"}]}],
        "code": {"coding": [code_coding], "text": code_text},
        "subject": {"reference": subject_ref},
        "basedOn": [{"reference": f"ServiceRequest/{order.resource_id}"}],
        "effectiveDateTime": _now_iso(),
        value_key: value,
        "meta": {"tag": [dict(fhir.LAB_UNRELEASED_TAG)]},
    }
    if interpretation:
        obs_body["interpretation"] = [{
            "coding": [{"system": lab.INTERPRETATION_SYSTEM,
                        "code": interpretation, "display": interpretation}],
            "text": interpretation}]
    specimen = lab._specimen_row(session, order)
    if specimen is not None:
        obs_body["specimen"] = {"reference": f"Specimen/{specimen.resource_id}"}
    return fhir.create_resource("Observation", obs_body, session, user)


def _append_to_report(session: Session, report: FhirResource,
                      observation_id: str) -> None:
    body = dict(report.body or {})
    refs = list(body.get("result") or [])
    refs.append({"reference": f"Observation/{observation_id}"})
    body["result"] = refs
    report.body = body
    report.version += 1
    session.flush()


@router.post("/micro/orders/{order_id}/preliminary", status_code=201)
async def micro_preliminary(order_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("lab.work"))):
    """Stage 1 of the culture report: the preliminary finding (e.g. a
    Gram-stain result) on a PLATED culture. Creates the bench
    DiagnosticReport — preliminary and visibility-tagged, so staff see
    it in the lab and NOBODY sees it in the portal/inbox yet."""
    payload = await _json_body(request)
    text = payload.get("text") or payload.get("finding")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=422,
                            detail="Preliminary report requires 'text' "
                                   "(the preliminary finding)")
    order = _bench_order(session, order_id, "microbiology")
    _require_status(order, "plated", action="report a preliminary result for")
    if _bench_report(session, order, "MB") is not None:
        raise HTTPException(status_code=409,
                            detail=f"Order {order_id} already has a culture report")
    concept = lab._order_code(session, order)
    subject_ref = (order.body or {}).get("subject", {}).get("reference")
    report_body = {
        "resourceType": "DiagnosticReport", "id": str(uuid.uuid4()),
        "status": "preliminary",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
            "code": "MB", "display": "Microbiology"}]}],
        "code": {"coding": [{"system": lab.LOINC, "code": concept["code"],
                             "display": concept.get("display")}],
                 "text": concept.get("display")},
        "subject": {"reference": subject_ref},
        "basedOn": [{"reference": f"ServiceRequest/{order_id}"}],
        "effectiveDateTime": _now_iso(),
        "result": [],
        "meta": {"tag": [dict(fhir.LAB_UNRELEASED_TAG)]},
    }
    specimen = lab._specimen_row(session, order)
    if specimen is not None:
        report_body["specimen"] = [{"reference": f"Specimen/{specimen.resource_id}"}]
    report = fhir.create_resource("DiagnosticReport", report_body, session, user)
    report_row = fhir._get_row("DiagnosticReport", report["id"], session)
    obs = _micro_observation(
        session, user, order, report_row,
        code_coding={"system": lab.LOINC, "code": concept["code"],
                     "display": concept.get("display")},
        code_text=concept.get("display"),
        value_key="valueString", value=text.strip())
    _append_to_report(session, report_row, obs["id"])
    lab._set_status(order, "preliminary-reported")
    if specimen is not None:
        lab._set_status(specimen, "preliminary-reported")
    session.flush()
    append_audit(session, actor=user.username, action="lab.micro.preliminary",
                 entity=f"DiagnosticReport/{report['id']}",
                 payload={"order_id": order_id})
    session.commit()
    return {"report": fhir._render(report_row), "order": fhir._render(order)}


@router.post("/micro/orders/{order_id}/organism", status_code=201)
async def micro_organism(order_id: str, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("lab.work"))):
    """Stage 2: organism identification. The organism must be in the
    published micro-organisms terminology (422 otherwise)."""
    payload = await _json_body(request)
    code = payload.get("organism_code") or payload.get("code")
    concept = _terminology_concept(session, lab.ORGANISMS_KEY, str(code)) \
        if code else None
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=(f"Organism '{code}' is not in the published "
                    "micro-organisms terminology"))
    if concept.get("active") is False:
        raise HTTPException(status_code=422,
                            detail=f"Organism '{code}' is inactive")
    order = _bench_order(session, order_id, "microbiology")
    _require_status(order, "preliminary-reported",
                    action="identify an organism for")
    report = _bench_report(session, order, "MB")
    obs = _micro_observation(
        session, user, order, report,
        code_coding={"system": ORGANISM_SYSTEM, "code": concept["code"],
                     "display": concept["display"]},
        code_text="Organism identified",
        value_key="valueCodeableConcept",
        value={"coding": [{"system": ORGANISM_SYSTEM, "code": concept["code"],
                           "display": concept["display"]}],
               "text": concept["display"]})
    _append_to_report(session, report, obs["id"])
    lab._set_status(order, "organism-identified")
    specimen = lab._specimen_row(session, order)
    if specimen is not None:
        lab._set_status(specimen, "organism-identified")
    session.flush()
    append_audit(session, actor=user.username, action="lab.micro.organism",
                 entity=f"DiagnosticReport/{report.resource_id}",
                 payload={"order_id": order_id, "organism": concept["code"]})
    session.commit()
    return {"report": fhir._render(report), "order": fhir._render(order)}


@router.post("/micro/orders/{order_id}/susceptibility", status_code=201)
async def micro_susceptibility(order_id: str, request: Request,
                               session: Session = Depends(get_db),
                               user: User = Depends(require_capability("lab.work"))):
    """Stage 3: susceptibility results — one Observation per antibiotic
    with an S / I / R interpretation, validated against the published
    micro-antibiotics terminology."""
    payload = await _json_body(request)
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        raise HTTPException(status_code=422,
                            detail="'results' must be a non-empty list of "
                                   "{antibiotic, interpretation}")
    order = _bench_order(session, order_id, "microbiology")
    _require_status(order, "organism-identified",
                    action="report susceptibilities for")
    report = _bench_report(session, order, "MB")
    created = []
    for i, item in enumerate(results):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422,
                                detail=f"results[{i}] must be an object")
        abx_code = item.get("antibiotic") or item.get("code")
        abx = _terminology_concept(session, lab.ANTIBIOTICS_KEY,
                                   str(abx_code)) if abx_code else None
        if abx is None:
            raise HTTPException(
                status_code=422,
                detail=(f"results[{i}] antibiotic '{abx_code}' is not in the "
                        "published micro-antibiotics terminology"))
        interp = str(item.get("interpretation") or "").upper()
        if interp not in SUSCEPTIBILITY_VALUES:
            raise HTTPException(
                status_code=422,
                detail=(f"results[{i}].interpretation must be one of "
                        f"{list(SUSCEPTIBILITY_VALUES)} (got "
                        f"'{item.get('interpretation')}')"))
        obs = _micro_observation(
            session, user, order, report,
            code_coding={"system": ANTIBIOTIC_SYSTEM, "code": abx["code"],
                         "display": abx["display"]},
            code_text=f"Susceptibility to {abx['display']}",
            value_key="valueCodeableConcept",
            value={"coding": [{"system": ANTIBIOTIC_SYSTEM,
                                "code": abx["code"],
                                "display": abx["display"]}],
                   "text": abx["display"]},
            interpretation=interp)
        _append_to_report(session, report, obs["id"])
        created.append(obs["id"])
    lab._set_status(order, "susceptibility-reported")
    specimen = lab._specimen_row(session, order)
    if specimen is not None:
        lab._set_status(specimen, "susceptibility-reported")
    session.flush()
    append_audit(session, actor=user.username, action="lab.micro.susceptibility",
                 entity=f"DiagnosticReport/{report.resource_id}",
                 payload={"order_id": order_id, "result_count": len(created)})
    session.commit()
    return {"report": fhir._render(report), "order": fhir._render(order),
            "observations": created}


@router.post("/micro/orders/{order_id}/finalize")
def micro_finalize(order_id: str, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.verify"))):
    """Final release of a culture report (lab.verify sign-off): the
    report + its observations become final and the visibility tag is
    stripped — this is the moment the result appears in the inbox and
    the patient portal. Allowed once an organism is identified (a
    no-growth culture finalises without susceptibilities)."""
    order = _bench_order(session, order_id, "microbiology")
    _require_status(order, "organism-identified", "susceptibility-reported",
                    action="finalize")
    report = _bench_report(session, order, "MB")
    for obs in lab._result_observations(session, report):
        body = dict(obs.body or {})
        body["status"] = "final"
        obs.body = body
        obs.status = "final"
        obs.version += 1
        lab._strip_unreleased(obs)
    body = dict(report.body or {})
    body["status"] = "final"
    body["issued"] = _now_iso()
    report.body = body
    report.status = "final"
    report.version += 1
    lab._strip_unreleased(report)
    lab._set_status(order, "final")
    specimen = lab._specimen_row(session, order)
    if specimen is not None:
        lab._set_status(specimen, "final")
    session.flush()
    append_audit(session, actor=user.username, action="lab.micro.finalize",
                 entity=f"DiagnosticReport/{report.resource_id}",
                 payload={"order_id": order_id, "released_by": user.username})
    # Phase 8: finalising a culture report is a release — queue the
    # outbound ORU^R01 (+ ELR when reportable) in this transaction.
    from .interop import enqueue_released_report
    enqueue_released_report(session, report, order)
    session.commit()
    return {"report": fhir._render(report), "order": fhir._render(order)}


# ---------------------------------------------------------------------------
# Blood bank / transfusion
# ---------------------------------------------------------------------------

def _unit_out(unit: BloodUnit) -> dict:
    return {"unit_id": unit.unit_id, "product_type": unit.product_type,
            "blood_group": unit.blood_group, "expiry_date": unit.expiry_date,
            "status": unit.status, "patient_ref": unit.patient_ref,
            "order_id": unit.order_id,
            "issued_at": unit.issued_at.isoformat() if unit.issued_at else None,
            "created_at": unit.created_at.isoformat() if unit.created_at else None}


def _unit_row(session: Session, unit_id: str) -> BloodUnit:
    unit = session.scalars(
        select(BloodUnit).where(BloodUnit.unit_id == unit_id)).first()
    if unit is None:
        raise HTTPException(status_code=404,
                            detail=f"Blood unit '{unit_id}' not found")
    return unit


def _blood_groups(session: Session) -> list[str]:
    concept = lab._concept(session, "883-9")
    return list((concept or {}).get("values") or [])


def _active_hold(session: Session, patient_ref: str) -> BloodBankHold | None:
    return session.scalars(
        select(BloodBankHold).where(BloodBankHold.patient_ref == patient_ref,
                                    BloodBankHold.active.is_(True))).first()


@router.post("/blood-bank/units", status_code=201)
async def create_blood_unit(request: Request, session: Session = Depends(get_db),
                            user: User = Depends(require_capability("lab.work"))):
    """Register a unit in inventory. Product type validates against
    the published catalog's bloodProducts; blood group against the
    ABO concept's value list; expiry must be an ISO date."""
    payload = await _json_body(request)
    unit_id = payload.get("unit_id")
    if not isinstance(unit_id, str) or not unit_id.strip():
        raise HTTPException(status_code=422, detail="Unit requires 'unit_id'")
    products = {p.get("code") for p in lab._catalog(session).get("bloodProducts") or []
                if isinstance(p, dict)}
    if payload.get("product_type") not in products:
        raise HTTPException(
            status_code=422,
            detail=(f"product_type '{payload.get('product_type')}' is not in "
                    "the published catalog's bloodProducts"))
    if payload.get("blood_group") not in _blood_groups(session):
        raise HTTPException(
            status_code=422,
            detail=(f"blood_group '{payload.get('blood_group')}' is not an "
                    "allowed ABO value in the published lab catalog"))
    try:
        date.fromisoformat(str(payload.get("expiry_date")))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422,
                            detail="'expiry_date' must be an ISO date (YYYY-MM-DD)")
    if session.scalars(select(BloodUnit).where(
            BloodUnit.unit_id == unit_id.strip())).first() is not None:
        raise HTTPException(status_code=409,
                            detail=f"Blood unit '{unit_id.strip()}' already exists")
    unit = BloodUnit(unit_id=unit_id.strip(),
                     product_type=payload["product_type"],
                     blood_group=payload["blood_group"],
                     expiry_date=str(payload["expiry_date"]),
                     status="available")
    session.add(unit)
    session.flush()
    append_audit(session, actor=user.username, action="lab.bloodbank.unit.create",
                 entity=f"BloodUnit/{unit.unit_id}",
                 payload={"product_type": unit.product_type,
                          "blood_group": unit.blood_group,
                          "expiry_date": unit.expiry_date})
    session.commit()
    return _unit_out(unit)


@router.get("/blood-bank/units")
def list_blood_units(status: str | None = Query(default=None),
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability("lab.read"))):
    stmt = select(BloodUnit).order_by(BloodUnit.id.asc())
    if status:
        stmt = stmt.where(BloodUnit.status == status)
    return [_unit_out(u) for u in session.scalars(stmt).all()]


@router.post("/blood-bank/units/{unit_id}/issue")
async def issue_blood_unit(unit_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.work"))):
    """Issue a unit against a patient's blood-bank order. Refused
    (409) when the unit is not available, is past its expiry date, or
    the patient is under a transfusion-reaction hold."""
    payload = await _json_body(request)
    unit = _unit_row(session, unit_id)
    if unit.status != "available":
        raise HTTPException(status_code=409,
                            detail=f"Unit {unit_id} is '{unit.status}', not available")
    if date.fromisoformat(unit.expiry_date) < date.today():
        unit.status = "expired"
        session.commit()
        raise HTTPException(
            status_code=409,
            detail=f"Unit {unit_id} expired on {unit.expiry_date} and cannot be issued")
    order_id = payload.get("order_id")
    if not order_id:
        raise HTTPException(status_code=422, detail="Issue requires 'order_id'")
    order = lab._order_row(session, str(order_id))
    concept = lab._order_code(session, order)
    if concept.get("bench") != "blood-bank":
        raise HTTPException(
            status_code=422,
            detail=f"Order {order_id} is not a blood-bank order")
    patient_ref = (order.body or {}).get("subject", {}).get("reference")
    hold = _active_hold(session, patient_ref)
    if hold is not None:
        raise HTTPException(
            status_code=409,
            detail=(f"Patient {patient_ref} is under a transfusion-reaction "
                    f"hold (unit {hold.unit_id}: {hold.reason}); no units "
                    "may be issued until a clinician clears the hold"))
    unit.status = "issued"
    unit.patient_ref = patient_ref
    unit.order_id = order.resource_id
    unit.issued_at = datetime.now(timezone.utc)
    session.flush()
    append_audit(session, actor=user.username, action="lab.bloodbank.unit.issue",
                 entity=f"BloodUnit/{unit_id}",
                 payload={"order_id": order.resource_id,
                          "patient_ref": patient_ref})
    session.commit()
    return _unit_out(unit)


@router.post("/blood-bank/units/{unit_id}/return")
async def return_blood_unit(unit_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("lab.work"))):
    """Return an issued unit: unused units go back to available;
    a unit recorded as transfused is terminal."""
    payload = await _json_body(request)
    unit = _unit_row(session, unit_id)
    if unit.status != "issued":
        raise HTTPException(status_code=409,
                            detail=f"Unit {unit_id} is '{unit.status}', not issued")
    if payload.get("transfused") is True:
        unit.status = "transfused"
    else:
        unit.status = "available"
        unit.patient_ref = None
        unit.order_id = None
        unit.issued_at = None
    session.flush()
    append_audit(session, actor=user.username, action="lab.bloodbank.unit.return",
                 entity=f"BloodUnit/{unit_id}",
                 payload={"transfused": payload.get("transfused") is True,
                          "status": unit.status})
    session.commit()
    return _unit_out(unit)


@router.post("/blood-bank/transfusions", status_code=201)
async def record_transfusion(request: Request, session: Session = Depends(get_db),
                             user: User = Depends(require_capability("lab.work"))):
    """The transfusion administration record: a FHIR Procedure linking
    the issued unit to its patient (the blood-bank analogue of the MAR
    administration record). The unit must be issued; recording the
    transfusion moves it to transfused."""
    payload = await _json_body(request)
    unit_id = payload.get("unit_id")
    if not unit_id:
        raise HTTPException(status_code=422, detail="Transfusion requires 'unit_id'")
    unit = _unit_row(session, str(unit_id))
    if unit.status != "issued":
        raise HTTPException(
            status_code=409,
            detail=f"Unit {unit_id} is '{unit.status}'; only an issued unit "
                   "can be transfused")
    procedure = fhir.create_resource("Procedure", {
        "resourceType": "Procedure", "id": str(uuid.uuid4()),
        "status": "completed",
        "code": {"coding": [{"system": "http://snomed.info/sct",
                             "code": "5447007",
                             "display": "Transfusion of blood product"}],
                 "text": "Transfusion of blood product"},
        "subject": {"reference": unit.patient_ref},
        "performedDateTime": payload.get("performed_at") or _now_iso(),
        "usedCode": [{"coding": [{
            "system": "urn:opencare:blood-product", "code": unit.product_type,
            "display": unit.product_type}]}],
        "extension": [
            {"url": "urn:opencare:extension/blood-unit",
             "valueString": unit.unit_id},
            {"url": "urn:opencare:extension/blood-group",
             "valueString": unit.blood_group},
        ],
        "note": [{"text": f"Unit {unit.unit_id} ({unit.product_type}, "
                          f"{unit.blood_group})"}],
    }, session, user)
    unit.status = "transfused"
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.bloodbank.transfusion",
                 entity=f"Procedure/{procedure['id']}",
                 payload={"unit_id": unit.unit_id,
                          "patient_ref": unit.patient_ref,
                          "order_id": unit.order_id})
    session.commit()
    return {"transfusion": procedure, "unit": _unit_out(unit)}


@router.post("/blood-bank/units/{unit_id}/reaction")
async def report_reaction(unit_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("lab.work"))):
    """Transfusion reaction workup: the implicated unit is
    quarantined, its transfusion record is annotated, and a HOLD is
    placed on the patient — no further unit can be issued to them
    until a clinician clears the hold (below)."""
    payload = await _json_body(request)
    symptoms = payload.get("symptoms") or payload.get("description")
    if not isinstance(symptoms, str) or not symptoms.strip():
        raise HTTPException(status_code=422,
                            detail="Reaction report requires 'symptoms'")
    unit = _unit_row(session, unit_id)
    if unit.status not in ("issued", "transfused"):
        raise HTTPException(
            status_code=409,
            detail=(f"Unit {unit_id} is '{unit.status}'; a reaction can only "
                    "be reported for an issued or transfused unit"))
    patient_ref = unit.patient_ref
    unit.status = "quarantined"
    # Annotate the transfusion record, when one exists.
    procedures = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Procedure")
        .order_by(FhirResource.id.desc())).all()
    annotated = None
    for proc in procedures:
        for ext in (proc.body or {}).get("extension") or []:
            if isinstance(ext, dict) \
                    and ext.get("url") == "urn:opencare:extension/blood-unit" \
                    and ext.get("valueString") == unit_id:
                body = dict(proc.body or {})
                exts = list(body.get("extension") or [])
                exts.append({"url": "urn:opencare:extension/transfusion-reaction",
                             "valueString": symptoms.strip()})
                body["extension"] = exts
                proc.body = body
                proc.version += 1
                annotated = proc.resource_id
                break
        if annotated:
            break
    hold = _active_hold(session, patient_ref) if patient_ref else None
    if hold is None and patient_ref:
        hold = BloodBankHold(patient_ref=patient_ref, unit_id=unit_id,
                             reason=symptoms.strip(), active=True,
                             created_by=user.username)
        session.add(hold)
        session.flush()
    append_audit(session, actor=user.username, action="lab.bloodbank.reaction",
                 entity=f"BloodUnit/{unit_id}",
                 payload={"patient_ref": patient_ref, "symptoms": symptoms.strip(),
                          "transfusion_id": annotated,
                          "hold_id": hold.id if hold else None})
    session.commit()
    return {"unit": _unit_out(unit),
            "hold": {"id": hold.id, "patient_ref": hold.patient_ref,
                     "unit_id": hold.unit_id, "reason": hold.reason,
                     "active": hold.active} if hold else None,
            "transfusion_id": annotated}


@router.get("/blood-bank/holds")
def list_holds(session: Session = Depends(get_db),
               user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(
        select(BloodBankHold).order_by(BloodBankHold.id.desc())).all()
    return [{"id": h.id, "patient_ref": h.patient_ref, "unit_id": h.unit_id,
             "reason": h.reason, "active": h.active,
             "created_by": h.created_by,
             "created_at": h.created_at.isoformat() if h.created_at else None,
             "cleared_by": h.cleared_by,
             "cleared_at": h.cleared_at.isoformat() if h.cleared_at else None}
            for h in rows]


@router.post("/blood-bank/holds/{patient_id}/clear")
def clear_hold(patient_id: str, session: Session = Depends(get_db),
               user: User = Depends(require_capability("lab.verify"))):
    """Clear a patient's transfusion-reaction hold (clinician action,
    lab.verify): the workup is done and issuing may resume."""
    patient_ref = fhir._normalize_patient_ref(patient_id)
    hold = _active_hold(session, patient_ref)
    if hold is None:
        raise HTTPException(status_code=404,
                            detail=f"No active transfusion hold for {patient_ref}")
    hold.active = False
    hold.cleared_by = user.username
    hold.cleared_at = datetime.now(timezone.utc)
    session.flush()
    append_audit(session, actor=user.username, action="lab.bloodbank.hold.clear",
                 entity=f"BloodBankHold/{hold.id}",
                 payload={"patient_ref": patient_ref, "unit_id": hold.unit_id})
    session.commit()
    return {"id": hold.id, "patient_ref": patient_ref, "active": False,
            "cleared_by": hold.cleared_by}


# ---------------------------------------------------------------------------
# Anatomic pathology
# ---------------------------------------------------------------------------

@router.get("/pathology/synoptic-form")
def synoptic_form(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("lab.read"))):
    cfg = published_config(session, "form", lab.SYNOPTIC_FORM_KEY)
    if cfg is None:
        raise HTTPException(status_code=503,
                            detail="No published synoptic form "
                                   f"(form/{lab.SYNOPTIC_FORM_KEY})")
    return {"form": cfg.payload, "version": cfg.version}


@router.post("/pathology/orders/{order_id}/report", status_code=201)
async def pathology_report(order_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.work"))):
    """Write the synoptic draft report for a HISTOLOGY-stage case: the
    answers are stored as a real QuestionnaireResponse against the
    published synoptic form (Phase 5 machinery — required items are
    enforced there), and a preliminary, visibility-tagged
    DiagnosticReport carries it. Unsigned = invisible outside the lab."""
    payload = await _json_body(request)
    order = _bench_order(session, order_id, "pathology")
    _require_status(order, "histology", action="report")
    if _bench_report(session, order, "SP") is not None:
        raise HTTPException(status_code=409,
                            detail=f"Order {order_id} already has a pathology report")
    patient_ref = (order.body or {}).get("subject", {}).get("reference")
    response = documentation._store_response(
        session, user, lab.SYNOPTIC_FORM_KEY, patient_ref,
        payload.get("answers"))
    concept = lab._order_code(session, order)
    report_body = {
        "resourceType": "DiagnosticReport", "id": str(uuid.uuid4()),
        "status": "preliminary",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
            "code": "SP", "display": "Surgical pathology"}]}],
        "code": {"coding": [{"system": lab.LOINC, "code": concept["code"],
                             "display": concept.get("display")}],
                 "text": concept.get("display")},
        "subject": {"reference": patient_ref},
        "basedOn": [{"reference": f"ServiceRequest/{order_id}"}],
        "effectiveDateTime": _now_iso(),
        "result": [],
        "presentedForm": [{"contentType": "application/json",
                           "title": "Synoptic report (QuestionnaireResponse)",
                           "data": response["id"]}],
        "extension": [{"url": "urn:opencare:extension/synoptic-response",
                       "valueReference": {
                           "reference": f"QuestionnaireResponse/{response['id']}"}}],
        "meta": {"tag": [dict(fhir.LAB_UNRELEASED_TAG)]},
    }
    specimen = lab._specimen_row(session, order)
    if specimen is not None:
        report_body["specimen"] = [{"reference": f"Specimen/{specimen.resource_id}"}]
    report = fhir.create_resource("DiagnosticReport", report_body, session, user)
    lab._set_status(order, "reported")
    if specimen is not None:
        lab._set_status(specimen, "reported")
    session.flush()
    append_audit(session, actor=user.username, action="lab.pathology.report",
                 entity=f"DiagnosticReport/{report['id']}",
                 payload={"order_id": order_id,
                          "questionnaire_response_id": response["id"]})
    session.commit()
    return {"report": report, "order": fhir._render(order),
            "questionnaire_response": response}


@router.post("/pathology/reports/{report_id}/sign")
def pathology_sign(report_id: str, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.verify"))):
    """Pathologist sign-out (lab.verify): the two-step completion —
    the draft report becomes final, the visibility tag is stripped,
    and the order reaches signed-out."""
    report = fhir._get_row("DiagnosticReport", report_id, session)
    order = lab._report_order(session, report)
    if order is None:
        raise HTTPException(status_code=422,
                            detail=f"DiagnosticReport/{report_id} is not a lab report")
    _require_status(order, "reported", action="sign out")
    body = dict(report.body or {})
    body["status"] = "final"
    body["issued"] = _now_iso()
    report.body = body
    report.status = "final"
    report.version += 1
    lab._strip_unreleased(report)
    lab._set_status(order, "signed-out")
    specimen = lab._specimen_row(session, order)
    if specimen is not None:
        lab._set_status(specimen, "signed-out")
    session.flush()
    append_audit(session, actor=user.username, action="lab.pathology.sign",
                 entity=f"DiagnosticReport/{report_id}",
                 payload={"order_id": order.resource_id,
                          "signed_by": user.username})
    # Phase 8: sign-out is the pathology release — queue the outbound
    # ORU^R01 (+ ELR when reportable) in this transaction.
    from .interop import enqueue_released_report
    enqueue_released_report(session, report, order)
    session.commit()
    return {"report": fhir._render(report), "order": fhir._render(order)}


# ---------------------------------------------------------------------------
# Laboratory operations: EQA, instruments, TAT
# ---------------------------------------------------------------------------

def _eqa_out(record: LabEqaRecord) -> dict:
    return {"id": record.id, "event": record.event,
            "analyte_code": record.analyte_code,
            "reported_value": record.reported_value,
            "acceptable_low": record.acceptable_low,
            "acceptable_high": record.acceptable_high,
            "passed": record.passed, "recorded_by": record.recorded_by,
            "created_at": record.created_at.isoformat()
            if record.created_at else None}


@router.post("/eqa", status_code=201)
async def create_eqa(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("lab.work"))):
    """Record an EQA / proficiency-testing result. `passed` is
    computed server-side from the acceptable range."""
    payload = await _json_body(request)
    event = payload.get("event")
    if not isinstance(event, str) or not event.strip():
        raise HTTPException(status_code=422, detail="EQA requires 'event'")
    code = payload.get("analyte_code")
    if not code or lab._concept(session, str(code)) is None:
        raise HTTPException(status_code=422,
                            detail=f"EQA 'analyte_code' '{code}' is not in "
                                   "the published lab catalog")
    numbers = {}
    for field in ("reported_value", "acceptable_low", "acceptable_high"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(status_code=422, detail=f"EQA '{field}' must be a number")
        numbers[field] = float(value)
    if numbers["acceptable_low"] > numbers["acceptable_high"]:
        raise HTTPException(status_code=422,
                            detail="EQA 'acceptable_low' must not exceed 'acceptable_high'")
    record = LabEqaRecord(
        event=event.strip(), analyte_code=str(code),
        reported_value=numbers["reported_value"],
        acceptable_low=numbers["acceptable_low"],
        acceptable_high=numbers["acceptable_high"],
        passed=numbers["acceptable_low"] <= numbers["reported_value"]
        <= numbers["acceptable_high"],
        recorded_by=user.username)
    session.add(record)
    session.flush()
    append_audit(session, actor=user.username, action="lab.eqa.create",
                 entity=f"LabEqaRecord/{record.id}",
                 payload={"event": record.event,
                          "analyte_code": record.analyte_code,
                          "passed": record.passed})
    session.commit()
    return _eqa_out(record)


@router.get("/eqa")
def list_eqa(session: Session = Depends(get_db),
             user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(
        select(LabEqaRecord).order_by(LabEqaRecord.id.desc())).all()
    return [_eqa_out(r) for r in rows]


def _instrument_out(inst: LabInstrument) -> dict:
    return {"instrument_id": inst.instrument_id, "name": inst.name,
            "bench": inst.bench, "status": inst.status,
            "created_at": inst.created_at.isoformat() if inst.created_at else None,
            "updated_at": inst.updated_at.isoformat() if inst.updated_at else None}


def _instrument_row(session: Session, instrument_id: str) -> LabInstrument:
    inst = session.scalars(
        select(LabInstrument).where(
            LabInstrument.instrument_id == instrument_id)).first()
    if inst is None:
        raise HTTPException(status_code=404,
                            detail=f"Instrument '{instrument_id}' not found")
    return inst


@router.post("/instruments", status_code=201)
async def create_instrument(request: Request, session: Session = Depends(get_db),
                            user: User = Depends(require_capability("lab.work"))):
    payload = await _json_body(request)
    instrument_id = payload.get("instrument_id")
    name = payload.get("name")
    if not isinstance(instrument_id, str) or not instrument_id.strip():
        raise HTTPException(status_code=422, detail="Instrument requires 'instrument_id'")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=422, detail="Instrument requires 'name'")
    status = payload.get("status") or "active"
    if status not in INSTRUMENT_STATUSES:
        raise HTTPException(status_code=422,
                            detail=f"Instrument 'status' must be one of "
                                   f"{list(INSTRUMENT_STATUSES)}")
    if session.scalars(select(LabInstrument).where(
            LabInstrument.instrument_id == instrument_id.strip())).first():
        raise HTTPException(status_code=409,
                            detail=f"Instrument '{instrument_id.strip()}' already exists")
    inst = LabInstrument(instrument_id=instrument_id.strip(), name=name.strip(),
                         bench=str(payload.get("bench") or ""), status=status)
    session.add(inst)
    session.flush()
    append_audit(session, actor=user.username, action="lab.instrument.create",
                 entity=f"LabInstrument/{inst.instrument_id}",
                 payload={"name": inst.name, "bench": inst.bench})
    session.commit()
    return _instrument_out(inst)


@router.get("/instruments")
def list_instruments(session: Session = Depends(get_db),
                     user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(
        select(LabInstrument).order_by(LabInstrument.id.asc())).all()
    return [_instrument_out(i) for i in rows]


@router.post("/instruments/{instrument_id}/status")
async def set_instrument_status(instrument_id: str, request: Request,
                                session: Session = Depends(get_db),
                                user: User = Depends(require_capability("lab.work"))):
    payload = await _json_body(request)
    inst = _instrument_row(session, instrument_id)
    status = payload.get("status")
    if status not in INSTRUMENT_STATUSES:
        raise HTTPException(status_code=422,
                            detail=f"'status' must be one of {list(INSTRUMENT_STATUSES)}")
    inst.status = status
    session.flush()
    append_audit(session, actor=user.username, action="lab.instrument.status",
                 entity=f"LabInstrument/{instrument_id}",
                 payload={"status": status})
    session.commit()
    return _instrument_out(inst)


@router.post("/instruments/{instrument_id}/log", status_code=201)
async def add_instrument_log(instrument_id: str, request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability("lab.work"))):
    payload = await _json_body(request)
    inst = _instrument_row(session, instrument_id)
    kind = payload.get("kind")
    if kind not in ("maintenance", "calibration"):
        raise HTTPException(status_code=422,
                            detail="'kind' must be 'maintenance' or 'calibration'")
    entry = LabInstrumentLog(instrument_id=inst.instrument_id, kind=kind,
                             note=str(payload.get("note") or ""),
                             performed_by=user.username)
    session.add(entry)
    session.flush()
    append_audit(session, actor=user.username, action="lab.instrument.log",
                 entity=f"LabInstrument/{instrument_id}",
                 payload={"kind": kind, "log_id": entry.id})
    session.commit()
    return {"id": entry.id, "instrument_id": entry.instrument_id,
            "kind": entry.kind, "note": entry.note,
            "performed_by": entry.performed_by,
            "created_at": entry.created_at.isoformat() if entry.created_at else None}


@router.get("/instruments/{instrument_id}/log")
def list_instrument_log(instrument_id: str, session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.read"))):
    _instrument_row(session, instrument_id)
    rows = session.scalars(
        select(LabInstrumentLog).where(
            LabInstrumentLog.instrument_id == instrument_id)
        .order_by(LabInstrumentLog.id.desc())).all()
    return [{"id": r.id, "instrument_id": r.instrument_id, "kind": r.kind,
             "note": r.note, "performed_by": r.performed_by,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows]


def _percentile(values: list[float], pct: float) -> float | None:
    import math
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * pct))
    return ordered[min(rank, len(ordered)) - 1]


def _parse_ts(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@router.get("/tat")
def tat_stats(session: Session = Depends(get_db),
              user: User = Depends(require_capability("lab.read"))):
    """Turnaround-time dashboard (Phase 6B): per-bench and per-analyte
    order->release and received->release percentiles in hours, computed
    LIVE over the FHIR store's custody timestamps (order authoredOn,
    specimen receivedDateTime, report issued) for released orders —
    no warehouse dependency."""
    orders = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "ServiceRequest",
                                   FhirResource.status == "released")).all()
    by_bench: dict[str, dict[str, list[float]]] = {}
    by_analyte: dict[str, dict[str, list[float]]] = {}

    def bucket(store: dict, key: str) -> dict[str, list[float]]:
        return store.setdefault(key, {"order_to_release": [],
                                       "received_to_release": []})

    for order in orders:
        body = order.body or {}
        code = lab._code_of(body)
        concept = lab._concept(session, code) if code else None
        if concept is None:
            continue
        report = None
        for candidate in session.scalars(
                select(FhirResource).where(
                    FhirResource.resource_type == "DiagnosticReport")).all():
            refs = [(r or {}).get("reference")
                    for r in (candidate.body or {}).get("basedOn") or []]
            if f"ServiceRequest/{order.resource_id}" in refs:
                report = candidate
                break
        if report is None:
            continue
        issued = _parse_ts((report.body or {}).get("issued"))
        authored = _parse_ts(body.get("authoredOn"))
        specimen = lab._specimen_row(session, order)
        received = _parse_ts((specimen.body or {}).get("receivedDateTime")) \
            if specimen is not None else None
        if issued is None:
            continue
        bench = concept.get("bench") or "chemistry"
        for store, key in ((by_bench, bench), (by_analyte, code)):
            target = bucket(store, key)
            if authored is not None:
                target["order_to_release"].append(
                    (issued - authored).total_seconds() / 3600.0)
            if received is not None:
                target["received_to_release"].append(
                    (issued - received).total_seconds() / 3600.0)

    def summarise(store: dict) -> dict:
        out = {}
        for key, series in sorted(store.items()):
            out[key] = {
                "count": len(series["order_to_release"]),
                "order_to_release_hours": {
                    "p50": _percentile(series["order_to_release"], 0.50),
                    "p90": _percentile(series["order_to_release"], 0.90)},
                "received_to_release_hours": {
                    "p50": _percentile(series["received_to_release"], 0.50),
                    "p90": _percentile(series["received_to_release"], 0.90)},
            }
        return out

    return {"generated_at": _now_iso(), "source": "live-query",
            "benches": summarise(by_bench), "analytes": summarise(by_analyte)}
