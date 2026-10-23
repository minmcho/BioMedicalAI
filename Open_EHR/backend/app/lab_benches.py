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
  Wave A (LAB-B1 / LAB-M1): issue is a HARD safety gate — the unit's
  ABO/Rh is checked against the patient's resulted (released) blood
  group through the clinical compatibility matrices in this module,
  a negative antibody screen must be inside the governed validity
  window, and a unit-specific resulted crossmatch is required unless
  the patient meets the governed electronic-crossmatch eligibility;
  the only override is a governed emergency release, which bypasses
  MISSING evidence only, never contradictory evidence. The
  transfusion record is a BCMA-pattern administration record: two
  patient identifiers (wristband id + date of birth), pre-transfusion
  vitals and start/stop times are verified server-side before any
  write. Gate parameters are governed Studio configuration (the
  published lab catalog's `bloodBankGate` section); the matrices are
  a clinical standard and are NOT configuration.
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
import json
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import documentation, fhir, lab
from .audit import append_audit
from .configstore import published_config
from .models import (AuditLog, BloodBankHold, BloodUnit, Device,
                     FhirResource, LabEqaRecord, LabHistoricalAntibody,
                     LabInstrument, LabInstrumentLog,
                     LabTransfusionRequirement, LabTsSpecimen,
                     LabUnitAttribute, LabUnitPreparation,
                     LabUnitReservation, PoctCompetencyRecord,
                     PoctQcRecord, User)
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
    # LAB-D12 / register LAB-m3: finalising a culture whose organism
    # is flagged MDRO/reportable in governed config raises the alert
    # into Infection Prevention (app.lab_micro.raise_mdrom_alerts —
    # lazily imported; lab_micro imports this module's bench
    # machinery at call time, never at module import). The workup
    # record is stamped finalised either way (the antibiogram's
    # period key).
    from . import lab_micro
    organism_codes = []
    for obs in lab._result_observations(session, report):
        for coding in ((obs.body or {}).get("code") or {}).get("coding") or []:
            if isinstance(coding, dict) and \
                    coding.get("system") == ORGANISM_SYSTEM:
                organism_codes.append(coding.get("code"))
    workup_row = lab_micro._workup_row(session, order_id)
    if workup_row is not None:
        for isolate in (workup_row.body or {}).get("isolates") or []:
            if isolate.get("organism_code"):
                organism_codes.append(isolate["organism_code"])
    mdrom_alerts = lab_micro.raise_mdrom_alerts(
        session, user, order, report, organism_codes)
    if not mdrom_alerts:
        lab_micro.mark_finalized(session, order)
    # Phase 8: finalising a culture report is a release — queue the
    # outbound ORU^R01 (+ ELR when reportable) in this transaction.
    from .interop import enqueue_released_report
    enqueue_released_report(session, report, order)
    session.commit()
    return {"report": fhir._render(report), "order": fhir._render(order),
            "mdro_alerts": mdrom_alerts}


# ---------------------------------------------------------------------------
# Blood bank / transfusion
# ---------------------------------------------------------------------------

def _emergency_release_for(session: Session, unit_id: str) -> dict:
    """Wave A (LAB-B1): the emergency-release recording FOR A UNIT,
    read from its lab.bloodbank.unit.emergency_issue audit row.

    Deliberately NOT BloodUnit columns: app.main creates the app
    against the deployment's existing database at import time and
    other modules query BloodUnit during startup seeding, so new
    columns would break every boot on a pre-existing database
    before any additive-column hook could run (main.py is outside
    this worker's file ownership). The audit row IS the recording —
    it is written in the same transaction as the issue — and this
    projection carries it on the unit's representation."""
    row = session.scalars(
        select(AuditLog).where(
            AuditLog.action == "lab.bloodbank.unit.emergency_issue",
            AuditLog.entity == f"BloodUnit/{unit_id}")
        .order_by(AuditLog.id.desc())).first()
    if row is None:
        return {}
    emergency = (row.payload or {}).get("emergency_release") or {}
    return {"emergency_release_reason_code": emergency.get("reason_code"),
            "emergency_release_note": emergency.get("note"),
            "emergency_released_by": row.actor,
            "emergency_released_at": row.created_at.isoformat()
            if row.created_at else None}


def _unit_out(unit: BloodUnit, session: Session | None = None) -> dict:
    out = {"unit_id": unit.unit_id, "product_type": unit.product_type,
           "blood_group": unit.blood_group, "expiry_date": unit.expiry_date,
           "status": unit.status, "patient_ref": unit.patient_ref,
           "order_id": unit.order_id,
           "issued_at": unit.issued_at.isoformat() if unit.issued_at else None,
           # Wave A (LAB-B1): emergency-release recording (all None
           # for a standard issue) — see _emergency_release_for.
           "emergency_release_reason_code": None,
           "emergency_release_note": None,
           "emergency_released_by": None,
           "emergency_released_at": None,
           "created_at": unit.created_at.isoformat() if unit.created_at else None}
    if session is not None:
        out.update(_emergency_release_for(session, unit.unit_id))
    return out


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


# ---------------------------------------------------------------------------
# Wave A (LAB-B1): the blood-bank issue gate
#
# Compatibility matrices — a CLINICAL STANDARD, encoded in code with
# explicit per-product tables. They are deliberately NOT Studio
# configuration: no site may publish its way to a different matrix.
#
# RBC (and, as the documented default, platelets + cryoprecipitate):
# donor ABO -> recipient ABOs it may be given to. O is the universal
# RBC donor; AB the universal RBC recipient. Rh rule (same products):
# an Rh-negative recipient must not receive an Rh-positive unit; an
# Rh-positive recipient may receive either.
# PLASMA is the inverse ABO matrix (the antibodies are in the plasma):
# AB is the universal plasma donor; an O recipient can receive any
# plasma. Rh does not apply to plasma (it contains no red cells).
# ---------------------------------------------------------------------------

RBC_ABO_COMPATIBILITY = {
    "O": {"O", "A", "B", "AB"},
    "A": {"A", "AB"},
    "B": {"B", "AB"},
    "AB": {"AB"},
}

PLASMA_ABO_COMPATIBILITY = {
    "AB": {"O", "A", "B", "AB"},
    "A": {"A", "O"},
    "B": {"B", "O"},
    "O": {"O"},
}

CROSSMATCH_UNIT_EXTENSION = "urn:opencare:extension/crossmatch-unit"


def _split_group(group: str | None) -> tuple[str | None, str | None]:
    """'AB POS' -> ('AB', 'POS'); anything else -> (None, None)."""
    parts = str(group or "").split()
    if len(parts) == 2 and parts[0] in ("O", "A", "B", "AB") \
            and parts[1] in ("POS", "NEG"):
        return parts[0], parts[1]
    return None, None


def _abo_rh_compatible(product_type: str, unit_group: str,
                       patient_group: str) -> tuple[bool, str]:
    """(compatible, reason) for one unit -> patient pairing under the
    per-product matrix above."""
    unit_abo, unit_rh = _split_group(unit_group)
    patient_abo, patient_rh = _split_group(patient_group)
    if unit_abo is None or patient_abo is None:
        return False, (f"blood group '{unit_group}' or '{patient_group}' "
                       "is not a recognised ABO/Rh group")
    table = PLASMA_ABO_COMPATIBILITY if product_type == "plasma" \
        else RBC_ABO_COMPATIBILITY
    if patient_abo not in table[unit_abo]:
        return False, (
            f"ABO-incompatible: {product_type} unit {unit_group} cannot "
            f"be given to a patient with blood group {patient_group}")
    if product_type != "plasma" and unit_rh == "POS" and patient_rh == "NEG":
        return False, (
            f"Rh-incompatible: Rh-positive {product_type} unit "
            f"{unit_group} must not be given to an Rh-negative patient "
            f"({patient_group})")
    return True, (f"{product_type} unit {unit_group} is ABO/Rh-compatible "
                  f"with patient group {patient_group}")


def _is_universal_unit(product_type: str, unit_group: str) -> bool:
    """A unit that may be given to a patient of UNKNOWN group under an
    emergency release: O NEG for RBC (and for platelets /
    cryoprecipitate under the RBC-matrix default); AB plasma (either
    Rh — Rh does not apply to plasma)."""
    abo, rh = _split_group(unit_group)
    if product_type == "plasma":
        return abo == "AB"
    return abo == "O" and rh == "NEG"


def _latest_released_result(session: Session, patient_ref: str,
                            code: str) -> dict | None:
    """The patient's latest RELEASED qualitative lab result for a
    catalog code: an Observation with status final and WITHOUT the
    lab-unreleased tag (resulted + verified + released on the
    standard spine — drafts never count, the lab._previous_released
    precedent). Returns {value, effective, observation_id} or None."""
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Observation",
                                   FhirResource.patient_ref == patient_ref)
        .order_by(FhirResource.id.desc())).all()
    for row in rows:
        body = row.body or {}
        if body.get("status") != "final" or fhir.has_unreleased_tag(body):
            continue
        if lab._code_of(body) != code:
            continue
        value = body.get("valueString")
        if not isinstance(value, str) or not value.strip():
            continue
        return {"value": value.strip(),
                "effective": _parse_ts(body.get("effectiveDateTime")),
                "observation_id": row.resource_id}
    return None


def _crossmatch_result_for_unit(session: Session, patient_ref: str,
                                unit_id: str) -> dict | None:
    """The latest RELEASED crossmatch (BB-XM) result recorded FOR
    THIS UNIT + patient: a BB-XM Observation carrying the
    crossmatch-unit extension stamped by the unit-specific
    result-entry path (POST /lab/blood-bank/orders/{id}/
    crossmatch-result). A BB-XM resulted without that extension is
    patient-level only and never satisfies the gate."""
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Observation",
                                   FhirResource.patient_ref == patient_ref)
        .order_by(FhirResource.id.desc())).all()
    for row in rows:
        body = row.body or {}
        if body.get("status") != "final" or fhir.has_unreleased_tag(body):
            continue
        if lab._code_of(body) != "BB-XM":
            continue
        linked_unit = None
        for ext in body.get("extension") or []:
            if isinstance(ext, dict) \
                    and ext.get("url") == CROSSMATCH_UNIT_EXTENSION:
                linked_unit = ext.get("valueString")
        if linked_unit != unit_id:
            continue
        value = body.get("valueString")
        if not isinstance(value, str) or not value.strip():
            continue
        return {"value": value.strip(),
                "effective": _parse_ts(body.get("effectiveDateTime")),
                "observation_id": row.resource_id}
    return None


# ---------------------------------------------------------------------------
# LAB-D13 (transfusion completion): the depth records the issue gate
# consults. These readers live here — inside the gate's own module —
# so the gate's composition stays single-sourced: app.lab_transfusion
# WRITES these tables (models.py LAB-D13 block) and the gate READS
# them here. No gate logic is duplicated in lab_transfusion.
# ---------------------------------------------------------------------------

def _aware(value):
    """Stored datetimes may come back naive from SQLite; all gate
    comparisons are against aware UTC now."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _ts_specimen_for_screen(session: Session, patient_ref: str,
                            screen: dict | None) -> LabTsSpecimen | None:
    """The T&S specimen lifecycle record (LAB-D13) for the screen
    the gate is evaluating: an exact link to that screen's
    Observation wins; otherwise an unlinked specimen for the patient
    (registered before its screen was released). Newest first."""
    if screen is None:
        return None
    rows = session.scalars(
        select(LabTsSpecimen).where(
            LabTsSpecimen.patient_ref == patient_ref)
        .order_by(LabTsSpecimen.id.desc())).all()
    for row in rows:
        if row.screen_observation_id == screen["observation_id"]:
            return row
    for row in rows:
        if row.screen_observation_id is None:
            return row
    return None


def _historical_antibodies(session: Session,
                           patient_ref: str) -> list[LabHistoricalAntibody]:
    """The patient's PERMANENT historical antibody record (LAB-D13):
    active, clinically-significant specificities. Before LAB-D13 the
    gate had NO antibody-history input — its only history-sensitive
    rule was the current screen; this record is now that input."""
    return session.scalars(
        select(LabHistoricalAntibody).where(
            LabHistoricalAntibody.patient_ref == patient_ref,
            LabHistoricalAntibody.active.is_(True),
            LabHistoricalAntibody.clinically_significant.is_(True))
        .order_by(LabHistoricalAntibody.id.asc())).all()


def _patient_requirements(session: Session,
                          patient_ref: str) -> list[LabTransfusionRequirement]:
    return session.scalars(
        select(LabTransfusionRequirement).where(
            LabTransfusionRequirement.patient_ref == patient_ref,
            LabTransfusionRequirement.active.is_(True))
        .order_by(LabTransfusionRequirement.id.asc())).all()


def _unit_attributes(session: Session,
                     unit_id: str) -> list[LabUnitAttribute]:
    return session.scalars(
        select(LabUnitAttribute).where(
            LabUnitAttribute.unit_id == unit_id)
        .order_by(LabUnitAttribute.id.asc())).all()


def _active_unit_reservation(session: Session,
                             unit_id: str) -> LabUnitReservation | None:
    """The unit's active crossmatch reservation (LAB-D13), honouring
    governed expiry. Pure read — an expired reservation is simply
    not active here; its stored row is swept to 'expired' by the
    next write path that touches it."""
    row = session.scalars(
        select(LabUnitReservation).where(
            LabUnitReservation.unit_id == unit_id,
            LabUnitReservation.status == "active")
        .order_by(LabUnitReservation.id.desc())).first()
    if row is None:
        return None
    expires = _aware(row.expires_at)
    if expires is not None and expires <= datetime.now(timezone.utc):
        return None
    return row


def _reservation_expiry_hours(session: Session) -> float:
    """Governed reservation expiry (terminology/transfusion-service,
    LAB-D13) with the seeded default as fallback — read here so the
    crossmatch path can stamp a reservation without importing
    app.lab_transfusion (which imports this module)."""
    cfg = published_config(session, "terminology", "transfusion-service")
    if cfg is not None and isinstance(cfg.payload, dict):
        try:
            hours = float(cfg.payload.get("reservationExpiryHours"))
            if hours > 0:
                return hours
        except (TypeError, ValueError):
            pass
    return 72.0


def _create_unit_reservation(session: Session, actor: str,
                             unit: BloodUnit, patient_ref: str,
                             order_id) -> LabUnitReservation:
    """Create the crossmatch reservation (LAB-D13) in the caller's
    transaction: a Compatible crossmatch reserves the unit for that
    patient until the governed expiry, issue, or a manual release
    with a reason (app.lab_transfusion)."""
    row = LabUnitReservation(
        reservation_id=f"res-{uuid.uuid4().hex[:10]}",
        unit_id=unit.unit_id, patient_ref=patient_ref,
        order_id=str(order_id) if order_id else None, status="active",
        expires_at=datetime.now(timezone.utc)
        + timedelta(hours=_reservation_expiry_hours(session)),
        created_by=actor)
    session.add(row)
    session.flush()
    append_audit(session, actor=actor,
                 action="lab.transfusion.reservation.create",
                 entity=f"LabUnitReservation/{row.reservation_id}",
                 payload={"unit_id": unit.unit_id,
                          "patient_ref": patient_ref,
                          "source": "crossmatch-result",
                          "expires_at": row.expires_at.isoformat()})
    return row


def _latest_unit_preparation(session: Session,
                             unit_id: str) -> LabUnitPreparation | None:
    """The unit's latest completed thaw/preparation record (LAB-D13)
    carrying a post-thaw expiry, if any."""
    rows = session.scalars(
        select(LabUnitPreparation).where(
            LabUnitPreparation.unit_id == unit_id,
            LabUnitPreparation.expires_at.is_not(None))
        .order_by(LabUnitPreparation.id.desc())).all()
    return rows[0] if rows else None


def _evaluate_issue_gate(session: Session, unit: BloodUnit,
                         patient_ref: str,
                         emergency_release) -> dict:
    """Evaluate EVERY LAB-B1 issue check for (unit, patient) and
    return {checks, failures, bypassed, emergency_used,
    emergency_reason_code, patient_group, allowed}. Pure read — this
    function never writes. Each check is {code, label, status
    (pass|fail|bypassed), detail, failure_class
    (missing|contradictory|None)}; the refusal detail names every
    failed check verbatim."""
    gate = lab.blood_bank_gate_config(session)
    try:
        validity_hours = float(gate.get("antibodyScreenValidityHours"))
        if validity_hours <= 0:
            validity_hours = 72.0
    except (TypeError, ValueError):
        validity_hours = 72.0
    now = datetime.now(timezone.utc)
    checks: list[dict] = []

    def add(code, label, status, detail, failure_class=None):
        checks.append({"code": code, "label": label, "status": status,
                       "detail": detail, "failure_class": failure_class})

    # 1. Patient's resulted ABO group --------------------------------
    group = _latest_released_result(session, patient_ref, "883-9")
    patient_group = group["value"] if group else None
    if group is not None:
        add("patient_group", "Patient ABO group resulted", "pass",
            f"Patient blood group on file: {patient_group} "
            f"(883-9, Observation/{group['observation_id']})")
    else:
        add("patient_group", "Patient ABO group resulted", "fail",
            f"type and screen required before issue: no resulted ABO "
            f"group (883-9) on file for {patient_ref}",
            failure_class="missing")

    # 3. Antibody screen (evaluated before compatibility so its state
    # feeds electronic-crossmatch eligibility) -----------------------
    screen = _latest_released_result(session, patient_ref, "890-4")
    screen_negative_valid = False
    if screen is None:
        add("antibody_screen", "Antibody screen negative & in window",
            "fail",
            f"no resulted antibody screen (890-4) on file for "
            f"{patient_ref}; a negative screen within "
            f"{validity_hours:g} hours is required before issue",
            failure_class="missing")
    elif screen["value"] == "Positive":
        add("antibody_screen", "Antibody screen negative & in window",
            "fail",
            f"antibody screen (890-4) is Positive for {patient_ref} "
            f"(Observation/{screen['observation_id']}): issue refused "
            "— a positive screen requires specialist review and "
            "release, which is outside this gate and cannot be "
            "overridden here",
            failure_class="contradictory")
    elif screen["value"] == "Negative":
        # LAB-D13: the T&S specimen-validity lifecycle is the
        # screen-freshness source when a specimen record exists for
        # this screen — its computed expiry (standard window, or the
        # extended presurgical window when the governed eligibility
        # was granted at registration) decides. With NO specimen
        # record the Wave A catalogue-window behaviour below applies
        # unchanged.
        specimen = _ts_specimen_for_screen(session, patient_ref, screen)
        if specimen is not None:
            expires = _aware(specimen.expires_at)
            if expires is not None and now <= expires:
                screen_negative_valid = True
                add("antibody_screen",
                    "Antibody screen negative & in window", "pass",
                    f"Antibody screen Negative; T&S specimen "
                    f"{specimen.specimen_id} "
                    f"({'extended presurgical' if specimen.extended_granted else 'standard'} "
                    f"validity) is in window until "
                    f"{expires.isoformat()} (LAB-D13 specimen-validity "
                    "lifecycle)")
            else:
                add("antibody_screen",
                    "Antibody screen negative & in window", "fail",
                    f"T&S specimen {specimen.specimen_id} for "
                    f"{patient_ref} is expired: its "
                    f"{'extended presurgical' if specimen.extended_granted else 'standard'} "
                    f"validity window ended "
                    f"{expires.isoformat() if expires else 'at an unknown time'} "
                    "— a repeat type and screen is required "
                    "(LAB-D13 specimen-validity lifecycle)",
                    failure_class="missing")
        else:
            effective = screen["effective"]
            age_hours = ((now - effective).total_seconds() / 3600.0
                         if effective is not None else None)
            if age_hours is not None and age_hours <= validity_hours:
                screen_negative_valid = True
                add("antibody_screen", "Antibody screen negative & in window",
                    "pass",
                    f"Antibody screen Negative, resulted "
                    f"{effective.isoformat()} ({age_hours:.1f}h ago, within "
                    f"the governed {validity_hours:g}h validity window)")
            else:
                add("antibody_screen", "Antibody screen negative & in window",
                    "fail",
                    f"antibody screen (890-4) Negative for {patient_ref} is "
                    f"expired: resulted "
                    f"{effective.isoformat() if effective else 'at an unknown time'}"
                    f", outside the governed {validity_hours:g}-hour "
                    "validity window — a repeat type and screen is required",
                    failure_class="missing")
    else:
        add("antibody_screen", "Antibody screen negative & in window",
            "fail",
            f"antibody screen (890-4) for {patient_ref} has unexpected "
            f"value '{screen['value']}'; a Negative screen is required",
            failure_class="contradictory")

    # 2. ABO/Rh compatibility ------------------------------------------
    if patient_group is not None:
        compatible, reason = _abo_rh_compatible(
            unit.product_type, unit.blood_group, patient_group)
        if compatible:
            add("compatibility", "ABO/Rh compatibility", "pass", reason)
        else:
            add("compatibility", "ABO/Rh compatibility", "fail", reason,
                failure_class="contradictory")
    else:
        add("compatibility", "ABO/Rh compatibility", "fail",
            f"ABO/Rh compatibility cannot be verified: the patient's "
            f"blood group is unknown, so only a universally compatible "
            f"{unit.product_type} unit may be issued, and only under "
            "an emergency release",
            failure_class="missing")

    # 4. Crossmatch / electronic crossmatch ---------------------------
    xm = _crossmatch_result_for_unit(session, patient_ref, unit.unit_id)
    if xm is not None and xm["value"] == "Incompatible":
        add("crossmatch", "Crossmatch compatible (or eXM-eligible)",
            "fail",
            f"crossmatch (BB-XM) for unit {unit.unit_id} and "
            f"{patient_ref} is Incompatible "
            f"(Observation/{xm['observation_id']}): this unit must "
            "never be issued to this patient",
            failure_class="contradictory")
    elif xm is not None and xm["value"] == "Compatible":
        add("crossmatch", "Crossmatch compatible (or eXM-eligible)",
            "pass",
            f"Crossmatch Compatible for unit {unit.unit_id} "
            f"(Observation/{xm['observation_id']})")
    else:
        exm = gate.get("electronicCrossmatch") or {}
        exm_products = exm.get("products") or []
        exm_reasons = []
        if exm.get("enabled") is not True:
            exm_reasons.append("electronic crossmatch is not enabled "
                               "in the governed bloodBankGate config")
        if unit.product_type not in exm_products:
            exm_reasons.append(
                f"electronic crossmatch is not enabled for product "
                f"'{unit.product_type}' (governed products: "
                f"{', '.join(exm_products) or 'none'})")
        if patient_group is None:
            exm_reasons.append("no resulted patient blood group on file")
        if not screen_negative_valid:
            exm_reasons.append("no negative antibody screen within "
                               "the validity window")
        historical = _historical_antibodies(session, patient_ref)
        if historical:
            exm_reasons.append(
                "historical clinically-significant antibody on file "
                f"({', '.join(h.specificity for h in historical)}; "
                "LAB-D13 historical antibody record) — electronic "
                "crossmatch is not permitted with an antibody history; "
                "a unit-specific crossmatch is required")
        if not exm_reasons:
            add("crossmatch", "Crossmatch compatible (or eXM-eligible)",
                "pass",
                "Electronic crossmatch: no unit crossmatch resulted, "
                "but the governed eligibility criteria are met "
                f"(product '{unit.product_type}' enabled, resulted "
                "group on file, negative in-window antibody screen)")
        else:
            add("crossmatch", "Crossmatch compatible (or eXM-eligible)",
                "fail",
                f"no resulted crossmatch (BB-XM) 'Compatible' for unit "
                f"{unit.unit_id} and {patient_ref}, and electronic-"
                f"crossmatch eligibility is not met: "
                f"{'; '.join(exm_reasons)}",
                failure_class="missing")

    # 4b. LAB-D13: historical antibody record -------------------------
    # A historical clinically-significant antibody affects today's
    # evaluation even with a negative current screen: the unit must
    # carry an attested antigen-negative attribute for EVERY
    # historical specificity. Contradictory class — an emergency
    # release may not override a known antibody.
    historical = _historical_antibodies(session, patient_ref)
    unit_attributes = _unit_attributes(session, unit.unit_id)
    if not historical:
        add("historical_antibody", "Historical antibody record consulted",
            "pass",
            f"No historical clinically-significant antibody on file "
            f"for {patient_ref} (LAB-D13 historical antibody record)")
    else:
        unmet_history = []
        for antibody in historical:
            attested = any(
                a.attribute_code == "antigen-negative"
                and a.specificity == antibody.specificity
                for a in unit_attributes)
            if not attested:
                unmet_history.append(antibody.specificity)
        if unmet_history:
            add("historical_antibody",
                "Historical antibody record consulted", "fail",
                f"historical clinically-significant antibody on file "
                f"for {patient_ref} ({', '.join(h.specificity for h in historical)}): "
                f"unit {unit.unit_id} carries no attested "
                f"antigen-negative attribute for "
                f"{', '.join(unmet_history)} — a patient with an "
                "antibody history requires an antigen-negative unit "
                "for each historical specificity, whatever today's "
                "screen shows (LAB-D13)",
                failure_class="contradictory")
        else:
            add("historical_antibody",
                "Historical antibody record consulted", "pass",
                f"Historical antibody "
                f"({', '.join(h.specificity for h in historical)}) "
                f"on file for {patient_ref}; unit {unit.unit_id} "
                "carries the attested antigen-negative attribute for "
                "each historical specificity (LAB-D13)")

    # 4c. LAB-D13: standing special requirements ----------------------
    requirements = _patient_requirements(session, patient_ref)
    if not requirements:
        add("special_requirements", "Special requirements satisfied",
            "pass",
            f"No standing special transfusion requirements on file "
            f"for {patient_ref} (LAB-D13)")
    else:
        unmet_requirements = []
        for requirement in requirements:
            satisfied = any(
                a.attribute_code == requirement.requirement_code
                and (requirement.requirement_code != "antigen-negative"
                     or requirement.specificity is None
                     or a.specificity == requirement.specificity)
                for a in unit_attributes)
            if not satisfied:
                unmet_requirements.append(
                    requirement.requirement_code
                    + (f" ({requirement.specificity})"
                       if requirement.specificity else ""))
        if unmet_requirements:
            add("special_requirements", "Special requirements satisfied",
                "fail",
                f"special requirement unmet for {patient_ref}: "
                f"{', '.join(unmet_requirements)} — unit "
                f"{unit.unit_id} carries no attested attribute "
                "satisfying this standing requirement (LAB-D13)",
                failure_class="contradictory")
        else:
            add("special_requirements", "Special requirements satisfied",
                "pass",
                f"Standing special requirements for {patient_ref} "
                f"({', '.join(r.requirement_code for r in requirements)}) "
                f"are satisfied by unit {unit.unit_id}'s attested "
                "attributes (LAB-D13)")

    # 4d. LAB-D13: crossmatch reservation ------------------------------
    reservation = _active_unit_reservation(session, unit.unit_id)
    if reservation is None:
        add("unit_reservation", "Unit reservation", "pass",
            f"Unit {unit.unit_id} carries no active reservation for "
            "another patient (LAB-D13)")
    elif reservation.patient_ref != patient_ref:
        add("unit_reservation", "Unit reservation", "fail",
            f"unit {unit.unit_id} is reserved for "
            f"{reservation.patient_ref} (reservation "
            f"{reservation.reservation_id}, expires "
            f"{_aware(reservation.expires_at).isoformat() if reservation.expires_at else '—'}) "
            "— a reserved unit cannot be issued to another patient "
            "(LAB-D13)",
            failure_class="contradictory")
    else:
        add("unit_reservation", "Unit reservation", "pass",
            f"Unit {unit.unit_id} is reserved for this patient "
            f"(reservation {reservation.reservation_id}, LAB-D13)")

    # 4e. LAB-D13: post-thaw / preparation expiry ----------------------
    preparation = _latest_unit_preparation(session, unit.unit_id)
    if preparation is None:
        add("preparation_expiry", "Post-thaw expiry", "pass",
            f"Unit {unit.unit_id} carries no thaw/preparation record "
            "— post-thaw expiry is not applicable (LAB-D13)")
    else:
        prep_expires = _aware(preparation.expires_at)
        if prep_expires is not None and prep_expires <= now:
            add("preparation_expiry", "Post-thaw expiry", "fail",
                f"post-thaw expiry: unit {unit.unit_id}'s preparation "
                f"{preparation.prep_id} ({preparation.prep_kind}) "
                f"expired at {prep_expires.isoformat()} — an expired "
                "post-thaw unit cannot be issued (LAB-D13)",
                failure_class="contradictory")
        else:
            add("preparation_expiry", "Post-thaw expiry", "pass",
                f"Unit {unit.unit_id}'s preparation "
                f"{preparation.prep_id} ({preparation.prep_kind}) is "
                f"within its post-thaw window until "
                f"{prep_expires.isoformat() if prep_expires else '—'} "
                "(LAB-D13)")

    # 5. Emergency release (the ONLY override) -------------------------
    emergency_used = False
    emergency_reason_code = None
    failures = [c for c in checks if c["status"] == "fail"]
    if emergency_release is not None:
        codes = [c.get("code") for c in
                 (gate.get("emergencyReleaseReasonCodes") or [])
                 if isinstance(c, dict)]
        if not isinstance(emergency_release, dict):
            add("emergency_release", "Emergency release", "fail",
                "emergency_release must be an object "
                "{reason_code, note} naming a governed reason code",
                failure_class="contradictory")
        elif emergency_release.get("reason_code") not in codes:
            add("emergency_release", "Emergency release", "fail",
                f"emergency release reason_code "
                f"'{emergency_release.get('reason_code')}' is not one "
                f"of the governed bloodBankGate reason codes "
                f"({', '.join(sorted(str(c) for c in codes))})",
                failure_class="contradictory")
        else:
            emergency_reason_code = emergency_release["reason_code"]
            if failures and all(c["failure_class"] == "missing"
                                for c in failures):
                if patient_group is None and not _is_universal_unit(
                        unit.product_type, unit.blood_group):
                    add("emergency_release", "Emergency release", "fail",
                        f"emergency release cannot be used: the "
                        f"patient's blood group is unknown and unit "
                        f"{unit.unit_id} ({unit.product_type}, "
                        f"{unit.blood_group}) is not universally "
                        "compatible for its product (RBC/platelets/"
                        "cryoprecipitate require O NEG; plasma "
                        "requires AB)",
                        failure_class="contradictory")
                else:
                    for check in failures:
                        check["status"] = "bypassed"
                    emergency_used = True
                    add("emergency_release", "Emergency release", "pass",
                        f"Emergency release '{emergency_reason_code}' "
                        "applied: bypassing missing evidence only "
                        f"({', '.join(c['code'] for c in failures)})")
            elif failures:
                add("emergency_release", "Emergency release", "fail",
                    "emergency release cannot override contradictory "
                    "evidence: "
                    + "; ".join(c["detail"] for c in failures
                                if c["failure_class"] == "contradictory"),
                    failure_class="contradictory")
        failures = [c for c in checks if c["status"] == "fail"]
    bypassed = [c for c in checks if c["status"] == "bypassed"]
    return {"checks": checks, "failures": failures, "bypassed": bypassed,
            "emergency_used": emergency_used,
            "emergency_reason_code": emergency_reason_code,
            "patient_group": patient_group,
            "allowed": not failures}


def _gate_refusal_detail(unit: BloodUnit, evaluation: dict) -> str:
    return (f"Blood-bank issue refused for unit {unit.unit_id}: "
            + "; ".join(c["detail"] for c in evaluation["failures"]))


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
    return _unit_out(unit, session)


@router.get("/blood-bank/units")
def list_blood_units(status: str | None = Query(default=None),
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability("lab.read"))):
    stmt = select(BloodUnit).order_by(BloodUnit.id.asc())
    if status:
        stmt = stmt.where(BloodUnit.status == status)
    return [_unit_out(u, session) for u in session.scalars(stmt).all()]


@router.post("/blood-bank/units/{unit_id}/issue")
async def issue_blood_unit(unit_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.work"))):
    """Issue a unit against a patient's blood-bank order.

    Wave A (LAB-B1) HARD GATE: after the pre-existing refusals (unit
    not available, patient under a transfusion-reaction hold), the
    issue gate (_evaluate_issue_gate) must pass BEFORE any write:
    the patient's resulted ABO group must be on file and ABO/Rh
    compatible with the unit, the latest antibody screen must be
    Negative within the governed validity window, and a resulted
    crossmatch for THIS unit must be Compatible — or the patient
    must meet the governed electronic-crossmatch eligibility. A gate
    refusal is a 409 whose detail names EVERY failed check, with
    zero state change. The only override is a governed emergency
    release ({emergency_release: {reason_code, note}}), which may
    bypass missing evidence only — never contradictory evidence —
    and is recorded on the unit + audited with the distinct action
    lab.bloodbank.unit.emergency_issue."""
    payload = await _json_body(request)
    unit = _unit_row(session, unit_id)
    if unit.status != "available":
        raise HTTPException(status_code=409,
                            detail=f"Unit {unit_id} is '{unit.status}', not available")
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
    evaluation = _evaluate_issue_gate(session, unit, patient_ref,
                                      payload.get("emergency_release"))
    if not evaluation["allowed"]:
        raise HTTPException(status_code=409,
                            detail=_gate_refusal_detail(unit, evaluation))
    if date.fromisoformat(unit.expiry_date) < date.today():
        unit.status = "expired"
        session.commit()
        raise HTTPException(
            status_code=409,
            detail=f"Unit {unit_id} expired on {unit.expiry_date} and cannot be issued")
    emergency = payload.get("emergency_release") \
        if evaluation["emergency_used"] else None
    # LAB-D13: issuing is a reservation release path. The gate has
    # already refused any active reservation for another patient, so
    # the stored active row here is either this patient's (release
    # it, reason 'issued') or past its governed expiry (sweep it to
    # expired) — both transitions audited in this transaction.
    stored_reservation = session.scalars(
        select(LabUnitReservation).where(
            LabUnitReservation.unit_id == unit.unit_id,
            LabUnitReservation.status == "active")
        .order_by(LabUnitReservation.id.desc())).first()
    if stored_reservation is not None:
        expires = _aware(stored_reservation.expires_at)
        if expires is not None and expires <= datetime.now(timezone.utc):
            stored_reservation.status = "expired"
            release_path = "expiry"
        else:
            stored_reservation.status = "released"
            stored_reservation.release_reason = "issued"
            stored_reservation.released_by = user.username
            stored_reservation.released_at = datetime.now(timezone.utc)
            release_path = "issued"
        session.flush()
        append_audit(
            session, actor=user.username,
            action="lab.transfusion.reservation.release",
            entity=f"LabUnitReservation/{stored_reservation.reservation_id}",
            payload={"unit_id": unit.unit_id,
                     "patient_ref": stored_reservation.patient_ref,
                     "reason": stored_reservation.release_reason,
                     "path": release_path})
    unit.status = "issued"
    unit.patient_ref = patient_ref
    unit.order_id = order.resource_id
    unit.issued_at = datetime.now(timezone.utc)
    session.flush()
    append_audit(
        session, actor=user.username,
        action=("lab.bloodbank.unit.emergency_issue" if emergency is not None
                else "lab.bloodbank.unit.issue"),
        entity=f"BloodUnit/{unit_id}",
        payload={"order_id": order.resource_id,
                 "patient_ref": patient_ref,
                 "patient_group": evaluation["patient_group"],
                 "checks": [{"code": c["code"], "status": c["status"]}
                            for c in evaluation["checks"]],
                 **({"emergency_release": {
                     "reason_code": evaluation["emergency_reason_code"],
                     "note": str(emergency.get("note") or ""),
                     "bypassed_checks": [c["code"]
                                         for c in evaluation["bypassed"]]}}
                    if emergency is not None else {})})
    session.commit()
    return _unit_out(unit, session)


@router.get("/blood-bank/units/{unit_id}/gate-status")
def blood_unit_gate_status(unit_id: str, order_id: str = Query(...),
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.read"))):
    """Wave A (LAB-B1): the issue gate's posture for one unit +
    blood-bank order, computed by the SAME evaluator the issue path
    enforces (dry run — no writes, no emergency release applied).
    The UI renders these checks so staff see compatibility posture
    BEFORE attempting an issue."""
    unit = _unit_row(session, unit_id)
    order = lab._order_row(session, order_id)
    concept = lab._order_code(session, order)
    if concept.get("bench") != "blood-bank":
        raise HTTPException(
            status_code=422,
            detail=f"Order {order_id} is not a blood-bank order")
    patient_ref = (order.body or {}).get("subject", {}).get("reference")
    evaluation = _evaluate_issue_gate(session, unit, patient_ref, None)
    gate = lab.blood_bank_gate_config(session)
    return {"unit_id": unit.unit_id, "product_type": unit.product_type,
            "blood_group": unit.blood_group, "status": unit.status,
            "patient_ref": patient_ref,
            "patient_group": evaluation["patient_group"],
            "checks": evaluation["checks"],
            "issue_allowed": evaluation["allowed"]
            and unit.status == "available",
            "antibody_screen_validity_hours":
                gate.get("antibodyScreenValidityHours"),
            "electronic_crossmatch": gate.get("electronicCrossmatch")}


@router.post("/blood-bank/orders/{order_id}/crossmatch-result",
             status_code=201)
async def result_crossmatch(order_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("lab.work"))):
    """Wave A (LAB-B1): result a crossmatch FOR A SPECIFIC UNIT.

    The unit-specific result-entry path: the BB-XM order is resulted
    through the standard spine (lab._create_results — same custody
    states, same draft/visibility rules), then the created
    Observation(s) and DiagnosticReport are stamped with the
    crossmatch-unit extension naming the unit. The issue gate
    consumes exactly those stamped, released observations. A BB-XM
    resulted through generic result entry carries no unit linkage
    and does not satisfy the gate."""
    payload = await _json_body(request)
    unit_id = payload.get("unit_id")
    if not isinstance(unit_id, str) or not unit_id.strip():
        raise HTTPException(
            status_code=422,
            detail="Crossmatch result requires 'unit_id' (the unit "
                   "this crossmatch was performed against)")
    unit = _unit_row(session, unit_id.strip())
    value = payload.get("value")
    if value not in ("Compatible", "Incompatible"):
        raise HTTPException(
            status_code=422,
            detail="Crossmatch 'value' must be one of "
                   "['Compatible', 'Incompatible']")
    order = lab._order_row(session, order_id)
    concept = lab._order_code(session, order)
    if concept.get("code") != "BB-XM":
        raise HTTPException(
            status_code=422,
            detail=f"Order {order_id} is a '{concept.get('code')}' "
                   "order, not a crossmatch (BB-XM) order")
    # LAB-D13: a unit reserved for another patient cannot be
    # crossmatched for this one — named refusal, zero partial write
    # (checked BEFORE any result is created).
    patient_ref = (order.body or {}).get("subject", {}).get("reference")
    active_reservation = _active_unit_reservation(session, unit.unit_id)
    if active_reservation is not None \
            and active_reservation.patient_ref != patient_ref:
        raise HTTPException(
            status_code=409,
            detail=f"Crossmatch refused for unit {unit.unit_id}: the "
                   f"unit is reserved for "
                   f"{active_reservation.patient_ref} (reservation "
                   f"{active_reservation.reservation_id}) — a reserved "
                   "unit cannot be crossmatched for another patient "
                   "(LAB-D13)")
    outcome = lab._create_results(
        session, user, order, [{"code": "BB-XM", "value": value}],
        source="manual")
    report_row = fhir._get_row("DiagnosticReport",
                               outcome["report"]["id"], session)
    stamped = []
    rows = [report_row] + [
        fhir._get_row("Observation", obs["id"], session)
        for obs in outcome["observations"]]
    for row in rows:
        body = dict(row.body or {})
        exts = [e for e in (body.get("extension") or [])
                if not (isinstance(e, dict)
                        and e.get("url") == CROSSMATCH_UNIT_EXTENSION)]
        exts.append({"url": CROSSMATCH_UNIT_EXTENSION,
                     "valueString": unit.unit_id})
        body["extension"] = exts
        row.body = body
        row.version += 1
        stamped.append(row.resource_id)
    session.flush()
    # LAB-D13: a Compatible crossmatch RESERVES the unit for this
    # patient (governed expiry) in the same transaction.
    if value == "Compatible" and active_reservation is None:
        _create_unit_reservation(session, user.username, unit,
                                 patient_ref, order.resource_id)
    append_audit(session, actor=user.username,
                 action="lab.bloodbank.crossmatch.result",
                 entity=f"DiagnosticReport/{report_row.resource_id}",
                 payload={"order_id": order.resource_id,
                          "unit_id": unit.unit_id, "value": value,
                          "patient_ref": (order.body or {}).get(
                              "subject", {}).get("reference"),
                          "stamped_resources": stamped})
    session.commit()
    return {"report": fhir._render(report_row),
            "observations": [fhir._render(r) for r in rows[1:]],
            "unit_id": unit.unit_id,
            "order": fhir._render(order)}


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
    return _unit_out(unit, session)


# Wave A (LAB-M1): sane pre-transfusion vital-sign ranges (server-side
# validation; a value outside the range is a data-entry error or a
# patient in extremis — either way it is named, never silently stored).
_VITAL_RANGES = {
    "temperature_c": (30.0, 45.0),
    "pulse_bpm": (20.0, 250.0),
    "systolic_bp": (40.0, 300.0),
    "diastolic_bp": (20.0, 200.0),
    "resp_rate": (4.0, 60.0),
}


def _transfusion_verification_failures(session: Session, unit: BloodUnit,
                                       payload: dict) -> tuple[list[str], dict]:
    """LAB-M1 bedside verification (BCMA pattern). Returns
    (failures, parsed) — EVERY failed check is named; `parsed`
    carries the normalised verification data for the success path.
    Pure read: no writes happen here."""
    failures: list[str] = []
    parsed: dict = {}
    expected_ref = unit.patient_ref or ""
    expected_id = expected_ref.split("/", 1)[-1] if expected_ref else ""

    # Identifier 1: the wristband patient id (scan or typed).
    wristband = payload.get("wristband_patient_id") \
        or payload.get("patient_identifier") or payload.get("identifier_1")
    if not isinstance(wristband, str) or not wristband.strip():
        failures.append(
            "wristband_patient_id is required (identifier 1: scan or "
            "type the patient id from the wristband)")
    else:
        wristband_ref = fhir._normalize_patient_ref(wristband.strip())
        if wristband_ref != expected_ref:
            failures.append(
                f"wristband patient id '{wristband.strip()}' does not "
                f"match the patient this unit was issued to "
                f"({expected_ref})")
        else:
            parsed["wristband_patient_id"] = expected_id

    # Identifier 2: the patient's date of birth vs the Patient record.
    dob = payload.get("date_of_birth") or payload.get("patient_dob")
    patient_dob = None
    if expected_id:
        try:
            patient_row = fhir._get_row("Patient", expected_id, session)
            patient_dob = (patient_row.body or {}).get("birthDate")
        except HTTPException:
            patient_dob = None
    if not isinstance(dob, str) or not dob.strip():
        failures.append(
            "date_of_birth is required (identifier 2: the patient's "
            "date of birth, checked against the Patient record)")
    elif not patient_dob:
        failures.append(
            f"date of birth cannot be verified: Patient {expected_ref} "
            "has no birthDate on record")
    elif dob.strip() != str(patient_dob):
        failures.append(
            f"date_of_birth '{dob.strip()}' does not match the "
            f"birthDate on the Patient record for {expected_ref}")
    else:
        parsed["date_of_birth_matched"] = True

    second_checker = payload.get("second_checker")
    if second_checker is not None \
            and (not isinstance(second_checker, str)
                 or not second_checker.strip()):
        failures.append("second_checker, when supplied, must be a "
                        "non-empty name or id")
    elif isinstance(second_checker, str):
        parsed["second_checker"] = second_checker.strip()

    # Pre-transfusion vitals: all five, numeric, in sane ranges.
    vitals = payload.get("vitals") or payload.get("pre_transfusion_vitals")
    if not isinstance(vitals, dict):
        failures.append(
            "vitals is required: pre-transfusion vitals "
            "{temperature_c, pulse_bpm, systolic_bp, diastolic_bp, "
            "resp_rate} must be recorded before transfusion")
    else:
        clean_vitals = {}
        for field, (low, high) in _VITAL_RANGES.items():
            value = vitals.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                failures.append(
                    f"vitals.{field} is required and must be a number")
                continue
            if not (low <= float(value) <= high):
                failures.append(
                    f"vitals.{field} {value} is outside the sane "
                    f"range {low:g}-{high:g}")
                continue
            clean_vitals[field] = float(value)
        if "systolic_bp" in clean_vitals and "diastolic_bp" in clean_vitals \
                and clean_vitals["diastolic_bp"] >= clean_vitals["systolic_bp"]:
            failures.append(
                "vitals.diastolic_bp must be lower than "
                "vitals.systolic_bp")
        if len(clean_vitals) == len(_VITAL_RANGES):
            parsed["vitals"] = clean_vitals

    # Start / stop times.
    started_raw = payload.get("started_at") or payload.get("performed_at")
    started = _parse_ts(started_raw) if started_raw else None
    if started_raw and started is None:
        failures.append(
            f"started_at '{started_raw}' is not a valid ISO timestamp")
    parsed["started_at"] = (started.isoformat() if started
                             else _now_iso())
    stopped_raw = payload.get("stopped_at")
    if stopped_raw:
        stopped = _parse_ts(stopped_raw)
        if stopped is None:
            failures.append(
                f"stopped_at '{stopped_raw}' is not a valid ISO "
                "timestamp")
        elif started is not None and stopped < started:
            failures.append(
                "stopped_at must not be earlier than started_at")
        else:
            parsed["stopped_at"] = stopped.isoformat()
    return failures, parsed


@router.post("/blood-bank/transfusions", status_code=201)
async def record_transfusion(request: Request, session: Session = Depends(get_db),
                             user: User = Depends(require_capability("lab.work"))):
    """The transfusion administration record: a FHIR Procedure linking
    the issued unit to its patient (the blood-bank analogue of the MAR
    administration record). The unit must be issued; recording the
    transfusion moves it to transfused.

    Wave A (LAB-M1) — BCMA-pattern bedside verification: the payload
    must carry two patient identifiers (wristband patient id +
    patient date of birth, both checked server-side against the
    issued unit's patient and the Patient record), the unit_id,
    pre-transfusion vitals (all five, range-validated) and start/stop
    times. ANY verification failure is a 422 naming every failed
    check, with zero writes and an audit row
    (lab.bloodbank.transfusion.verification_refused). On success the
    verification block + vitals + times are recorded on the
    Procedure."""
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
    failures, parsed = _transfusion_verification_failures(session, unit,
                                                          payload)
    if failures:
        append_audit(session, actor=user.username,
                     action="lab.bloodbank.transfusion.verification_refused",
                     entity=f"BloodUnit/{unit.unit_id}",
                     payload={"unit_id": unit.unit_id,
                              "patient_ref": unit.patient_ref,
                              "failed_checks": failures})
        session.commit()
        raise HTTPException(
            status_code=422,
            detail=("Transfusion verification refused: "
                    + "; ".join(failures)))
    verification = {
        "wristband_patient_id": parsed["wristband_patient_id"],
        "date_of_birth_matched": True,
        "verified_by": user.username,
        "second_checker": parsed.get("second_checker"),
        "verified_at": _now_iso(),
    }
    performed_period = {"start": parsed["started_at"]}
    if parsed.get("stopped_at"):
        performed_period["end"] = parsed["stopped_at"]
    procedure = fhir.create_resource("Procedure", {
        "resourceType": "Procedure", "id": str(uuid.uuid4()),
        "status": "completed",
        "code": {"coding": [{"system": "http://snomed.info/sct",
                             "code": "5447007",
                             "display": "Transfusion of blood product"}],
                 "text": "Transfusion of blood product"},
        "subject": {"reference": unit.patient_ref},
        "performedDateTime": parsed["started_at"],
        "performedPeriod": performed_period,
        "usedCode": [{"coding": [{
            "system": "urn:opencare:blood-product", "code": unit.product_type,
            "display": unit.product_type}]}],
        "extension": [
            {"url": "urn:opencare:extension/blood-unit",
             "valueString": unit.unit_id},
            {"url": "urn:opencare:extension/blood-group",
             "valueString": unit.blood_group},
            {"url": "urn:opencare:extension/transfusion-verification",
             "valueString": json.dumps(verification)},
            {"url": "urn:opencare:extension/pre-transfusion-vitals",
             "valueString": json.dumps(parsed["vitals"])},
        ],
        "note": [{"text": f"Unit {unit.unit_id} ({unit.product_type}, "
                          f"{unit.blood_group})"},
                 {"text": "Bedside verification: wristband patient id "
                          f"{verification['wristband_patient_id']} and "
                          "date of birth matched by "
                          f"{verification['verified_by']}"
                          + (f"; second checker "
                             f"{verification['second_checker']}"
                             if verification["second_checker"] else "")
                          + ". Pre-transfusion vitals: "
                          + ", ".join(
                              f"{k}={v:g}" for k, v in
                              parsed["vitals"].items()) + "."}],
    }, session, user)
    unit.status = "transfused"
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.bloodbank.transfusion",
                 entity=f"Procedure/{procedure['id']}",
                 payload={"unit_id": unit.unit_id,
                          "patient_ref": unit.patient_ref,
                          "order_id": unit.order_id,
                          "verification": verification,
                          "vitals": parsed["vitals"],
                          "started_at": parsed["started_at"],
                          "stopped_at": parsed.get("stopped_at")})
    session.commit()
    return {"transfusion": procedure, "unit": _unit_out(unit, session)}


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
    return {"unit": _unit_out(unit, session),
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
    concept_pre = lab._order_code(session, order)
    if concept_pre.get("cytology") is True:
        raise HTTPException(
            status_code=422,
            detail=(f"Order {order_id} is a cytology case "
                    f"({concept_pre.get('display')}); report it with the "
                    "cytology form (POST /lab/pathology/orders/"
                    "{id}/cytology-report), not the synoptic form"))
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


# ---------------------------------------------------------------------------
# Cytology (Depth pass) — gyn + non-gyn, on the pathology bench
# ---------------------------------------------------------------------------

@router.get("/pathology/cytology-form")
def cytology_form(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("lab.read"))):
    cfg = published_config(session, "form", lab.CYTOLOGY_FORM_KEY)
    if cfg is None:
        raise HTTPException(status_code=503,
                            detail="No published cytology form "
                                   f"(form/{lab.CYTOLOGY_FORM_KEY})")
    return {"form": cfg.payload, "version": cfg.version}


@router.post("/pathology/orders/{order_id}/cytology-report", status_code=201)
async def cytology_report(order_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("lab.work"))):
    """Write the cytology draft report for a HISTOLOGY-stage case
    whose catalog concept carries the `cytology` flag: answers are
    stored as a real QuestionnaireResponse against the published
    cytology form, and a preliminary, visibility-tagged
    DiagnosticReport (category CP) carries it. Sign-out is the
    existing pathology sign endpoint — one governance path for the
    whole bench."""
    payload = await _json_body(request)
    order = _bench_order(session, order_id, "pathology")
    concept = lab._order_code(session, order)
    if concept.get("cytology") is not True:
        raise HTTPException(
            status_code=422,
            detail=(f"Order {order_id} is a surgical pathology case "
                    f"({concept.get('display')}); report it with the "
                    "synoptic form (POST /lab/pathology/orders/"
                    "{id}/report), not the cytology form"))
    _require_status(order, "histology", action="report cytology for")
    if _bench_report(session, order, "CP") is not None:
        raise HTTPException(status_code=409,
                            detail=f"Order {order_id} already has a "
                                   "cytology report")
    patient_ref = (order.body or {}).get("subject", {}).get("reference")
    response = documentation._store_response(
        session, user, lab.CYTOLOGY_FORM_KEY, patient_ref,
        payload.get("answers"))
    report_body = {
        "resourceType": "DiagnosticReport", "id": str(uuid.uuid4()),
        "status": "preliminary",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
            "code": "CP", "display": "Cytopathology"}]}],
        "code": {"coding": [{"system": lab.LOINC, "code": concept["code"],
                             "display": concept.get("display")}],
                 "text": concept.get("display")},
        "subject": {"reference": patient_ref},
        "basedOn": [{"reference": f"ServiceRequest/{order_id}"}],
        "effectiveDateTime": _now_iso(),
        "result": [],
        "presentedForm": [{"contentType": "application/json",
                           "title": "Cytology report (QuestionnaireResponse)",
                           "data": response["id"]}],
        "extension": [{"url": "urn:opencare:extension/cytology-response",
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
    append_audit(session, actor=user.username,
                 action="lab.pathology.cytology_report",
                 entity=f"DiagnosticReport/{report['id']}",
                 payload={"order_id": order_id,
                          "questionnaire_response_id": response["id"]})
    session.commit()
    return {"report": report, "order": fhir._render(order),
            "questionnaire_response": response}


# ---------------------------------------------------------------------------
# Send-outs / reference laboratory (Depth pass)
# ---------------------------------------------------------------------------

def _sendout_labs(session: Session) -> list[dict]:
    cfg = published_config(session, "terminology", lab.SENDOUT_LABS_KEY)
    if cfg is None:
        return []
    return [entry for entry in (cfg.payload or {}).get("labs") or []
            if isinstance(entry, dict)]


def _sendout_lab_for(session: Session, code: str) -> dict | None:
    """The reference laboratory a catalog code routes to, per the
    published sendout-labs config — or None (performed in-house)."""
    for entry in _sendout_labs(session):
        if code in (entry.get("tests") or []):
            return entry
    return None


def _sendout_state(order: FhirResource) -> dict | None:
    state = (order.body or {}).get("sendout")
    return state if isinstance(state, dict) else None


def _set_sendout_state(order: FhirResource, **fields) -> None:
    body = dict(order.body or {})
    state = dict(body.get("sendout") or {})
    state.update(fields)
    body["sendout"] = state
    order.body = body
    order.version += 1


@router.get("/sendouts")
def sendout_queue(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("lab.read"))):
    """Every order whose test routes to a reference laboratory, with
    its send-out state (awaiting dispatch until a manifest ships it).
    Routing is the published sendout-labs config; state rides on the
    order (the rejectionHistory precedent)."""
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "ServiceRequest")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    items = []
    for row in rows:
        code = lab._code_of(row.body or {})
        if not code:
            continue
        entry = _sendout_lab_for(session, code)
        if entry is None:
            continue
        concept = lab._concept(session, code) or {}
        items.append({
            "order_id": row.resource_id,
            "order_status": (row.body or {}).get("status"),
            "code": code, "display": concept.get("display"),
            "patient": (row.body or {}).get("subject", {}).get("reference"),
            "reference_lab": {"code": entry.get("code"),
                              "display": entry.get("display"),
                              "tat_hours": entry.get("tatHours")},
            "sendout": _sendout_state(row) or {"status": "awaiting-dispatch"},
        })
    return {"items": items, "total": len(items),
            "laboratories": _sendout_labs(session)}


@router.post("/sendouts/dispatch", status_code=201)
async def sendout_dispatch(request: Request, session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.work"))):
    """Ship a batch of collected send-out specimens on ONE manifest
    to ONE reference laboratory (a manifest never mixes labs). Each
    order moves collected -> in-process (the specimen is in transit
    / in analysis at the reference laboratory) and carries its
    manifest id + dispatch timestamp."""
    payload = await _json_body(request)
    order_ids = payload.get("order_ids")
    if not isinstance(order_ids, list) or not order_ids:
        raise HTTPException(status_code=422,
                            detail="'order_ids' must be a non-empty list")
    orders = []
    for raw_id in order_ids:
        order = lab._order_row(session, str(raw_id))
        concept = lab._order_code(session, order)
        entry = _sendout_lab_for(session, concept["code"])
        if entry is None:
            raise HTTPException(
                status_code=422,
                detail=(f"Order {raw_id} ({concept.get('display')}) is not "
                        "a send-out test in the published sendout-labs "
                        "config"))
        if _sendout_state(order) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Order {raw_id} has already been dispatched")
        _require_status(order, "collected", action="dispatch")
        orders.append((order, entry))
    lab_codes = {entry.get("code") for _order, entry in orders}
    if len(lab_codes) > 1:
        raise HTTPException(
            status_code=422,
            detail=("A send-out manifest ships to ONE reference "
                    "laboratory; split the batch by laboratory"))
    entry = orders[0][1]
    manifest_id = str(uuid.uuid4())
    now = _now_iso()
    tracking_ref = payload.get("tracking_ref")
    for order, _entry in orders:
        _set_sendout_state(order, status="dispatched",
                           lab=entry.get("code"), manifest_id=manifest_id,
                           dispatched_at=now, tracking_ref=tracking_ref)
        lab._set_status(order, "in-process")
        specimen = lab._specimen_row(session, order)
        if specimen is not None:
            lab._set_status(specimen, "in-process")
        append_audit(session, actor=user.username,
                     action="lab.sendout.dispatch",
                     entity=f"ServiceRequest/{order.resource_id}",
                     payload={"manifest_id": manifest_id,
                              "lab": entry.get("code"),
                              "tracking_ref": tracking_ref})
    session.commit()
    return {"manifest": {"id": manifest_id, "lab": entry.get("code"),
                         "display": entry.get("display"),
                         "dispatched_at": now,
                         "tracking_ref": tracking_ref,
                         "orders": [o.resource_id for o, _e in orders]}}


@router.post("/sendouts/{order_id}/receive-result", status_code=201)
async def sendout_receive_result(order_id: str, request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_capability("lab.work"))):
    """File the reference laboratory's returned result: it enters
    through the SAME result-creation core as in-house results (a
    preliminary, unreleased draft awaiting verification — a send-out
    result is never auto-released), stamped source=reference-lab,
    and the send-out state records the return time the TAT view
    measures."""
    payload = await _json_body(request)
    order = lab._order_row(session, order_id)
    concept = lab._order_code(session, order)
    if _sendout_lab_for(session, concept["code"]) is None:
        raise HTTPException(
            status_code=422,
            detail=(f"Order {order_id} ({concept.get('display')}) is not "
                    "a send-out test in the published sendout-labs config"))
    state = _sendout_state(order)
    if state is None or state.get("status") != "dispatched":
        raise HTTPException(
            status_code=409,
            detail=(f"Order {order_id} is not awaiting a send-out result "
                    f"(send-out status: "
                    f"{(state or {}).get('status', 'not dispatched')})"))
    outcome = lab._create_results(session, user, order,
                                  payload.get("results"),
                                  source="reference-lab")
    _set_sendout_state(order, status="resulted", returned_at=_now_iso())
    append_audit(session, actor=user.username, action="lab.sendout.result",
                 entity=f"ServiceRequest/{order_id}",
                 payload={"lab": state.get("lab"),
                          "manifest_id": state.get("manifest_id"),
                          "report_id": outcome["report"]["id"]})
    session.commit()
    return {**outcome, "sendout": _sendout_state(order)}


@router.get("/sendouts/tat")
def sendout_tat(session: Session = Depends(get_db),
                user: User = Depends(require_capability("lab.read"))):
    """Turnaround by reference laboratory: dispatched -> resulted
    hours for every resulted send-out, against the contracted
    tatHours in the published config. A live query over the send-out
    state timestamps (no warehouse dependency)."""
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "ServiceRequest")).all()
    by_lab: dict[str, dict] = {}
    for entry in _sendout_labs(session):
        by_lab[entry.get("code")] = {
            "lab": entry.get("code"), "display": entry.get("display"),
            "tat_hours": entry.get("tatHours"), "resulted": 0,
            "in_flight": 0, "_hours": [], "_within": 0}
    for row in rows:
        state = _sendout_state(row)
        if not state or state.get("lab") not in by_lab:
            continue
        bucket = by_lab[state["lab"]]
        if state.get("status") == "dispatched":
            bucket["in_flight"] += 1
        elif state.get("status") == "resulted":
            bucket["resulted"] += 1
            dispatched = lab._parse_ts(state.get("dispatched_at"))
            returned = lab._parse_ts(state.get("returned_at"))
            if dispatched and returned:
                hours = (returned - dispatched).total_seconds() / 3600.0
                bucket["_hours"].append(hours)
                tat = bucket["tat_hours"]
                if isinstance(tat, (int, float)) and hours <= tat:
                    bucket["_within"] += 1
    out = []
    for bucket in by_lab.values():
        hours = bucket.pop("_hours")
        within = bucket.pop("_within")
        bucket["avg_hours"] = (round(sum(hours) / len(hours), 2)
                               if hours else None)
        bucket["within_tat_pct"] = (round(within / len(hours) * 100, 1)
                                    if hours else None)
        out.append(bucket)
    return {"laboratories": out, "generated_at": _now_iso(),
            "source": "live-query"}


# ---------------------------------------------------------------------------
# POCT programme (Depth pass) — device QC + operator competency,
# built on the Phase 14 device registry (no second registry)
# ---------------------------------------------------------------------------

def _poct_qc_out(row: PoctQcRecord) -> dict:
    return {"id": row.id, "device_key": row.device_key, "level": row.level,
            "result": row.result, "operator": row.operator,
            "recorded_at": row.created_at.isoformat()
            if row.created_at else None}


def _poct_competency_out(programme: dict, row: PoctCompetencyRecord) -> dict:
    status, expires = lab._competency_status(programme, row.assessed_on)
    return {"id": row.id, "staff_id": row.staff_id,
            "test_code": row.test_code, "method": row.method,
            "assessed_on": row.assessed_on, "assessor": row.assessor,
            "status": status, "expires_on": expires,
            "recorded_at": row.created_at.isoformat()
            if row.created_at else None}


def _poct_device_or_404(session: Session, device_key: str) -> Device:
    device = session.scalars(
        select(Device).where(Device.device_key == device_key)).first()
    if device is None:
        raise HTTPException(status_code=404,
                            detail=(f"Unknown device '{device_key}' — POCT "
                                    "devices live in the Phase 14 device "
                                    "registry"))
    return device


@router.get("/poct/devices")
def poct_devices(session: Session = Depends(get_db),
                 user: User = Depends(require_capability("lab.read"))):
    """The programme's devices: Phase 14 registry rows whose type is
    one of the published programme's POCT device types, each with
    today's QC state per required level."""
    programme = lab._poct_programme(session)
    types = [t for t in (programme.get("poctDeviceTypes") or [])
             if isinstance(t, str)]
    rows = []
    if types:
        rows = session.scalars(
            select(Device).where(Device.device_type.in_(types))
            .order_by(Device.device_key.asc())).all()
    items = []
    for device in rows:
        levels = {}
        for level in programme.get("qcLevels") or []:
            latest = session.scalars(
                select(PoctQcRecord).where(
                    PoctQcRecord.device_key == device.device_key,
                    PoctQcRecord.level == level)
                .order_by(PoctQcRecord.id.desc())).first()
            levels[level] = (
                latest.result if latest is not None
                and latest.created_at is not None
                and latest.created_at.date()
                == datetime.now(timezone.utc).date() else None)
        items.append({"device_key": device.device_key, "name": device.name,
                      "device_type": device.device_type,
                      "location": device.location, "status": device.status,
                      "qc_today": levels,
                      "qc_current": lab._poct_qc_current(
                          session, programme, device.device_key)})
    return {"programme_configured": bool(programme),
            "device_types": types, "items": items, "total": len(items)}


@router.post("/poct/qc", status_code=201)
async def poct_qc_record(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(require_capability("lab.work"))):
    """Record one QC run on a POCT device. The level must be one of
    the programme's published levels and the result pass/fail — the
    outcome is recorded, never asserted into currency (currency is
    computed from today's rows)."""
    payload = await _json_body(request)
    programme = lab._poct_programme(session)
    device = _poct_device_or_404(session, str(payload.get("device_key") or ""))
    level = payload.get("level")
    levels = programme.get("qcLevels") or []
    if level not in levels:
        raise HTTPException(
            status_code=422,
            detail=(f"'level' must be one of the POCT programme's "
                    f"published QC levels {levels}"))
    result = payload.get("result")
    if result not in ("pass", "fail"):
        raise HTTPException(status_code=422,
                            detail="'result' must be 'pass' or 'fail'")
    row = PoctQcRecord(device_key=device.device_key, level=level,
                       result=result, operator=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="lab.poct.qc",
                 entity=f"Device/{device.device_key}",
                 payload={"level": level, "result": result})
    session.commit()
    return {"qc": _poct_qc_out(row),
            "qc_current": lab._poct_qc_current(session, programme,
                                               device.device_key)}


@router.get("/poct/qc")
def poct_qc_list(device_key: str | None = Query(default=None),
                 session: Session = Depends(get_db),
                 user: User = Depends(require_capability("lab.read"))):
    stmt = select(PoctQcRecord).order_by(PoctQcRecord.id.desc()).limit(200)
    if device_key:
        stmt = stmt.where(PoctQcRecord.device_key == device_key)
    rows = session.scalars(stmt).all()
    return {"items": [_poct_qc_out(row) for row in rows],
            "total": len(rows)}


@router.post("/poct/competency", status_code=201)
async def poct_competency_record(request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_capability("lab.work"))):
    """Record an operator competency assessment (CLIA model): the
    staff member, the catalog test, the assessment method, and the
    assessment date. Currency + expiry are computed at read from the
    programme's validity window — never stored as a flag."""
    payload = await _json_body(request)
    programme = lab._poct_programme(session)
    staff_id = payload.get("staff_id")
    if not isinstance(staff_id, str) or not staff_id.strip():
        raise HTTPException(status_code=422,
                            detail="'staff_id' is required (the "
                                   "staff-directory id of the operator)")
    test_code = payload.get("test_code")
    if not isinstance(test_code, str) \
            or lab._concept(session, test_code) is None:
        raise HTTPException(
            status_code=422,
            detail=(f"'test_code' '{test_code}' is not in the published "
                    "lab catalog"))
    method = payload.get("method")
    if not isinstance(method, str) or not method.strip():
        raise HTTPException(
            status_code=422,
            detail="'method' is required (how competency was assessed)")
    assessed_raw = payload.get("assessed_on")
    try:
        assessed = date.fromisoformat(str(assessed_raw))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="'assessed_on' must be an ISO date (YYYY-MM-DD)")
    if assessed > date.today():
        raise HTTPException(
            status_code=422,
            detail="'assessed_on' cannot be in the future")
    row = PoctCompetencyRecord(staff_id=staff_id.strip(),
                               test_code=test_code, method=method.strip(),
                               assessed_on=assessed.isoformat(),
                               assessor=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="lab.poct.competency",
                 entity=f"Staff/{staff_id.strip()}",
                 payload={"test_code": test_code, "method": method.strip(),
                          "assessed_on": assessed.isoformat()})
    session.commit()
    return {"competency": _poct_competency_out(programme, row)}


@router.get("/poct/competency")
def poct_competency_list(staff_id: str | None = Query(default=None),
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("lab.read"))):
    programme = lab._poct_programme(session)
    stmt = select(PoctCompetencyRecord).order_by(
        PoctCompetencyRecord.id.desc()).limit(500)
    if staff_id:
        stmt = stmt.where(PoctCompetencyRecord.staff_id == staff_id)
    rows = session.scalars(stmt).all()
    return {"items": [_poct_competency_out(programme, row) for row in rows],
            "total": len(rows)}


@router.get("/poct/overview")
def poct_overview(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("lab.read"))):
    """The programme at a glance: devices due QC today, competency
    records expiring inside the published window, expired records,
    and the enforcement state — reported as data, with the seeded
    OFF state unmistakable."""
    programme = lab._poct_programme(session)
    devices = poct_devices(session=session, user=user)
    due_qc = [item["device_key"] for item in devices["items"]
              if not item["qc_current"]]
    window_days = programme.get("competencyExpiringWindowDays")
    if not isinstance(window_days, int) or isinstance(window_days, bool):
        window_days = 30
    today = date.today()
    expiring, expired = [], []
    rows = session.scalars(select(PoctCompetencyRecord)).all()
    for row in rows:
        status, expires = lab._competency_status(programme, row.assessed_on)
        if status == "expired":
            expired.append(_poct_competency_out(programme, row))
        elif status == "current" and expires:
            days_left = (date.fromisoformat(expires) - today).days
            if days_left <= window_days:
                expiring.append(_poct_competency_out(programme, row))
    enforcement = programme.get("enforcement") or {}
    return {
        "programme_configured": bool(programme),
        "config": {"kind": "terminology", "key": lab.POCT_PROGRAMME_KEY,
                   "edit_in": "System Studio -> Terminology -> poct-programme"},
        "enforcement": {
            "block_uncompetent_operator":
                enforcement.get("blockUncompetentOperator") is True,
            "block_missing_daily_qc":
                enforcement.get("blockMissingDailyQc") is True,
            "state_label": (
                "ENFORCEMENT ON — published blocks apply at POCT result "
                "entry" if (enforcement.get("blockUncompetentOperator")
                             or enforcement.get("blockMissingDailyQc"))
                else "TRACKING ONLY — enforcement is OFF (the seeded "
                     "default); QC and competency are recorded but do "
                     "not block result entry"),
        },
        "devices_total": devices["total"],
        "devices_due_qc": due_qc,
        "qc_levels": programme.get("qcLevels") or [],
        "competency_expiring": expiring,
        "competency_expired": expired,
    }
