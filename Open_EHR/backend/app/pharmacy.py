"""Phase 4A — Pharmacy: formulary, dispensing with stock control, and
BCMA-lite barcode medication administration.

Built ON the Studio customization engine like the other modules:

- the formulary is terminology config, key ``formulary`` (RxNorm-coded
  concepts carrying a ``stockTracked`` flag and an ``initialStock``),
  seeded published v1 at startup by :func:`ensure_pharmacy_defaults`;
- stock itself is server-side state (app.models.PharmacyStock), seeded
  once from the formulary's ``initialStock`` for stock-tracked meds and
  decremented in the same transaction as each dispense. Dispensing more
  than the on-hand quantity is refused with 409 BEFORE any write — stock
  can never go negative and a refused dispense changes nothing.

Flow:
- ``POST /pharmacy/dispense`` takes an ACTIVE MedicationRequest, creates
  a completed MedicationDispense linked back to it
  (``authorizingPrescription``), decrements stock when the medication is
  stock-tracked, and moves the request to status ``dispensed``.
- ``POST /pharmacy/administer`` is BCMA-lite (barcode medication
  administration): the caller scans the patient wristband id, the
  medication code, and the dispense id. The "five rights" subset this
  module enforces is right patient / right medication / right dispense —
  any mismatch is a 422 naming the right that failed, and administering
  the same dispense twice is a 409. A pass creates a
  MedicationAdministration linked to the dispense.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir
from .audit import append_audit
from .configstore import published_config
from .models import (ConfigStatus, FhirResource, PharmacyStock,
                     StudioConfig, User)
from .security import get_db, require_capability

router = APIRouter(prefix="/pharmacy", tags=["pharmacy"])

FORMULARY_KEY = "formulary"
MONITORING_KEY = "medication-monitoring"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
DISPENSE_EXTENSION = "urn:opencare:extension/dispense"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Default Studio configuration (seeded published v1 at startup)
# ---------------------------------------------------------------------------

def default_formulary_payload() -> dict:
    """Synthetic formulary: the RxNorm medications the seed data actually
    prescribes. Albuterol is deliberately NOT stock-tracked (a ward-stock
    inhaler) to exercise the flag; the rest carry demo stock levels."""
    def med(code, display, tracked, initial=None):
        concept = {"code": code, "display": display, "active": True,
                   "system": RXNORM, "stockTracked": tracked}
        if initial is not None:
            concept["initialStock"] = initial
        return concept

    return {
        "name": "OpenCare formulary (RxNorm, synthetic)",
        "system": RXNORM,
        # BCMA enforced rights, in enforcement order (read by
        # _bcma_rights): a site may publish a subset; the default is the
        # original three checks. right-dose / right-time are not
        # implemented in v1 and so cannot be enabled by config either.
        "bcmaRights": ["right-dispense", "right-medication", "right-patient"],
        "concepts": [
            med("1049221", "Lisinopril 10 MG Oral Tablet", True, 120),
            med("847232", "Metformin hydrochloride 500 MG Oral Tablet", True, 200),
            med("1044910", "Atorvastatin 20 MG Oral Tablet", True, 90),
            med("1048467", "Sertraline 50 MG Oral Tablet", True, 60),
            med("1351886", "Apixaban 5 MG Oral Tablet", True, 45),
            med("245314", "Albuterol 0.09 MG/ACTUAT Metered Dose Inhaler", False),
        ],
    }


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


def ensure_pharmacy_defaults(session: Session) -> None:
    """Seed the formulary as published Studio config v1, and seed stock
    rows from it (idempotent: an existing stock row is the truth and is
    never reset by a restart)."""
    _seed_published(session, "terminology", FORMULARY_KEY,
                    default_formulary_payload())
    formulary = _formulary(session)
    for concept in formulary.get("concepts") or []:
        if not isinstance(concept, dict) or not concept.get("stockTracked"):
            continue
        code = concept.get("code")
        if not code:
            continue
        existing = session.scalars(
            select(PharmacyStock).where(PharmacyStock.medication_code == code)).first()
        if existing is None:
            initial = concept.get("initialStock")
            quantity = int(initial) if isinstance(initial, (int, float)) \
                and not isinstance(initial, bool) else 0
            session.add(PharmacyStock(medication_code=code, quantity=quantity))
    session.commit()


# ---------------------------------------------------------------------------
# Formulary + stock accessors
# ---------------------------------------------------------------------------

def _formulary(session: Session) -> dict:
    cfg = published_config(session, "terminology", FORMULARY_KEY)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=(f"No published formulary (terminology/{FORMULARY_KEY}); "
                    "publish one in Studio before using the pharmacy"))
    return cfg.payload or {}


def _formulary_entry(session: Session, code: str) -> dict | None:
    for concept in _formulary(session).get("concepts") or []:
        if isinstance(concept, dict) and concept.get("code") == code:
            return concept
    return None


def _stock_row(session: Session, code: str, concept: dict) -> PharmacyStock:
    row = session.scalars(
        select(PharmacyStock).where(PharmacyStock.medication_code == code)).first()
    if row is None:
        initial = concept.get("initialStock")
        row = PharmacyStock(
            medication_code=code,
            quantity=int(initial) if isinstance(initial, (int, float))
            and not isinstance(initial, bool) else 0)
        session.add(row)
        session.flush()
    return row


def _stock_out(session: Session) -> list[dict]:
    rows = {r.medication_code: r.quantity for r in session.scalars(
        select(PharmacyStock)).all()}
    out = []
    for concept in _formulary(session).get("concepts") or []:
        if not isinstance(concept, dict) or not concept.get("code"):
            continue
        tracked = bool(concept.get("stockTracked"))
        out.append({
            "code": concept["code"],
            "display": concept.get("display"),
            "system": concept.get("system") or RXNORM,
            "active": concept.get("active") is not False,
            "stock_tracked": tracked,
            "quantity": rows.get(concept["code"]) if tracked else None,
        })
    return out


def _medication_of(body: dict) -> tuple[str | None, str | None, dict | None]:
    """(system, code, CodeableConcept) from medicationCodeableConcept."""
    concept = body.get("medicationCodeableConcept") or {}
    coding = concept.get("coding") or []
    if coding and isinstance(coding[0], dict):
        return coding[0].get("system"), coding[0].get("code"), concept
    return None, None, concept if isinstance(concept, dict) else None


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


# ---------------------------------------------------------------------------
# Medication monitoring gate (Phase 9) — the Phase 2 QC-gate pattern
# applied to high-risk medications: a published terminology config
# (key `medication-monitoring`) declares, per medication code, the
# monitoring labs that must be on file, current and in range before
# the medication may be dispensed (the clozapine/ANC pattern from
# roadmap §4 psychiatry). The EVALUATION is generic code; the RULES
# are configuration. With no published rules config — the default —
# the gate is inert and dispensing behaves exactly as before.
#
# Rule shape:
#   {"rules": [{"medicationCode": ..., "medicationDisplay": ...,
#               "action": "block" | "flag",          (default "block")
#               "requiredLabs": [{"code": <LOINC>, "display": ...,
#                                  "minValue": n?, "maxValue": n?,
#                                  "maxAgeDays": n?}]}]}
# "block" refuses the dispense with 409 (nothing written); "flag"
# lets it proceed and returns the problems as `monitoring_warnings`.
# Only RELEASED results count: lab drafts carrying the Phase 2
# unreleased-visibility tag are invisible here, exactly as they are
# to every other consumer.
# ---------------------------------------------------------------------------

def _monitoring_rules(session: Session) -> list[dict]:
    cfg = published_config(session, "terminology", MONITORING_KEY)
    if cfg is None:
        return []
    rules = (cfg.payload or {}).get("rules")
    if not isinstance(rules, list):
        return []
    return [r for r in rules if isinstance(r, dict) and r.get("medicationCode")]


def _parse_dt(raw) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _latest_result(session: Session, patient_ref: str,
                   code: str) -> tuple[float, datetime] | None:
    """(value, effective datetime) of the patient's LATEST released
    Observation carrying `code`, or None. Unreleased lab drafts
    (Phase 2 visibility tag) never count as monitoring evidence."""
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Observation",
            FhirResource.patient_ref == patient_ref)).all()
    best: tuple[float, datetime] | None = None
    for row in rows:
        body = row.body or {}
        if fhir.has_unreleased_tag(body):
            continue
        concept = body.get("code") or {}
        codes = [c.get("code") for c in concept.get("coding") or []
                 if isinstance(c, dict)]
        if code not in codes:
            continue
        quantity = body.get("valueQuantity") or {}
        value = quantity.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        effective = _parse_dt(body.get("effectiveDateTime")) \
            or _parse_dt(body.get("issued"))
        if effective is None:
            created = row.created_at
            effective = created if created.tzinfo is not None \
                else created.replace(tzinfo=timezone.utc)
        if best is None or effective > best[1]:
            best = (float(value), effective)
    return best


def _monitoring_problem(session: Session, patient_ref: str,
                        lab: dict) -> str | None:
    """Why one required monitoring lab fails for this patient, or
    None when it is on file, current and in range."""
    code = str(lab.get("code"))
    display = lab.get("display") or code
    latest = _latest_result(session, patient_ref, code)
    if latest is None:
        return f"no {display} ({code}) result on file"
    value, effective = latest
    max_age = lab.get("maxAgeDays")
    if isinstance(max_age, (int, float)) and not isinstance(max_age, bool) \
            and max_age > 0:
        age_days = (datetime.now(timezone.utc) - effective).total_seconds() / 86400.0
        if age_days > float(max_age):
            return (f"latest {display} ({code}) is {int(age_days)} days old "
                    f"(must be within {max_age} days)")
    min_value = lab.get("minValue")
    if isinstance(min_value, (int, float)) and not isinstance(min_value, bool) \
            and value < float(min_value):
        return (f"latest {display} ({code}) is {value}, below the required "
                f"minimum {min_value}")
    max_value = lab.get("maxValue")
    if isinstance(max_value, (int, float)) and not isinstance(max_value, bool) \
            and value > float(max_value):
        return (f"latest {display} ({code}) is {value}, above the allowed "
                f"maximum {max_value}")
    return None


def medication_monitoring_check(session: Session, patient_ref: str,
                                medication_code: str) -> tuple[list[dict], list[dict]]:
    """Evaluate the published monitoring rules for one dispense.
    Returns (blockers, warnings): each entry names the rule's
    medication, the lab and the problem. An empty rules config (the
    default) returns two empty lists — the gate is inert."""
    blockers: list[dict] = []
    warnings: list[dict] = []
    for rule in _monitoring_rules(session):
        if str(rule.get("medicationCode")) != str(medication_code):
            continue
        action = rule.get("action") if rule.get("action") in ("block", "flag") \
            else "block"
        for lab in rule.get("requiredLabs") or []:
            if not isinstance(lab, dict) or not lab.get("code"):
                continue
            problem = _monitoring_problem(session, patient_ref, lab)
            if problem is None:
                continue
            entry = {"medication_code": str(medication_code),
                     "medication_display": rule.get("medicationDisplay"),
                     "lab_code": str(lab.get("code")),
                     "problem": problem}
            (blockers if action == "block" else warnings).append(entry)
    return blockers, warnings


def _monitoring_block_message(blockers: list[dict]) -> str:
    meds = ", ".join(sorted({b["medication_display"] or b["medication_code"]
                             for b in blockers}))
    problems = "; ".join(b["problem"] for b in blockers)
    return (f"Medication monitoring block: {meds} requires current "
            f"monitoring labs before dispensing — {problems}. Record "
            "the required results (and have them released) before "
            "this medication can be dispensed")


# ---------------------------------------------------------------------------
# Formulary / stock / dispense queue reads (staff)
# ---------------------------------------------------------------------------

@router.get("/formulary")
def get_formulary(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("pharmacy.read"))):
    cfg = published_config(session, "terminology", FORMULARY_KEY)
    return {"formulary": _formulary(session),
            "version": cfg.version if cfg else None}


@router.get("/stock")
def get_stock(session: Session = Depends(get_db),
              user: User = Depends(require_capability("pharmacy.read"))):
    return {"items": _stock_out(session)}


@router.get("/dispense-queue")
def dispense_queue(session: Session = Depends(get_db),
                   user: User = Depends(require_capability("pharmacy.read"))):
    """Active MedicationRequests awaiting dispensing, each with its
    formulary entry and live stock level, oldest first."""
    rows = session.scalars(
        select(FhirResource)
        .where(FhirResource.resource_type == "MedicationRequest",
               FhirResource.status == "active")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    stock = {item["code"]: item for item in _stock_out(session)}
    items = []
    for row in rows:
        _, code, _ = _medication_of(row.body or {})
        entry = stock.get(code or "")
        items.append({
            "request": fhir._render(row),
            "medication_code": code,
            "in_formulary": entry is not None,
            "stock_tracked": entry["stock_tracked"] if entry else None,
            "stock_quantity": entry["quantity"] if entry else None,
        })
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Dispensing
# ---------------------------------------------------------------------------

@router.post("/dispense", status_code=201)
async def dispense(request: Request, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("pharmacy.dispense"))):
    """Dispense an active MedicationRequest: create the MedicationDispense,
    decrement stock (stock-tracked meds only), and move the request to
    'dispensed'. Dispensing more than the on-hand stock -> 409 with
    nothing written; a request that is not active -> 409; a medication
    that is not in the published formulary -> 422."""
    payload = await _json_body(request)
    request_id = (payload.get("medication_request_id") or payload.get("request_id")
                  or payload.get("medicationRequestId"))
    if not request_id and isinstance(payload.get("medicationRequest"), dict):
        reference = payload["medicationRequest"].get("reference") or ""
        request_id = reference.split("/", 1)[1] if "/" in reference else reference
    if not request_id:
        raise HTTPException(status_code=422,
                            detail="Dispense requires 'medication_request_id'")
    med_request = fhir._get_row("MedicationRequest", str(request_id), session)
    request_body = med_request.body or {}
    if request_body.get("status") != "active":
        raise HTTPException(
            status_code=409,
            detail=(f"MedicationRequest/{request_id} is "
                    f"'{request_body.get('status')}', not 'active'; only an active "
                    "request can be dispensed"))
    _, code, concept = _medication_of(request_body)
    if not code:
        raise HTTPException(status_code=422,
                            detail="MedicationRequest carries no medication code")
    entry = _formulary_entry(session, code)
    if entry is None or entry.get("active") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Medication code '{code}' is not an active entry in the "
                   "published formulary")
    quantity = payload.get("quantity", 1)
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise HTTPException(status_code=422,
                            detail="Dispense 'quantity' must be a positive integer")

    # Medication monitoring gate (Phase 9): published rules may BLOCK
    # this dispense (409, nothing written — the QC-gate posture) or
    # FLAG it (warnings ride the response + audit). Inert when no
    # monitoring config is published.
    patient_ref = (request_body.get("subject") or {}).get("reference") or ""
    blockers, monitoring_warnings = medication_monitoring_check(
        session, patient_ref, code)
    if blockers:
        raise HTTPException(status_code=409,
                            detail=_monitoring_block_message(blockers))

    stock_row = None
    if entry.get("stockTracked"):
        stock_row = _stock_row(session, code, entry)
        if stock_row.quantity < quantity:
            raise HTTPException(
                status_code=409,
                detail=(f"Insufficient stock for '{code}' "
                        f"({entry.get('display')}): {stock_row.quantity} on hand, "
                        f"{quantity} requested"))

    dispense_body = {
        "resourceType": "MedicationDispense",
        "id": str(uuid.uuid4()),
        "status": "completed",
        "medicationCodeableConcept": concept,
        "subject": request_body.get("subject"),
        "authorizingPrescription": [
            {"reference": f"MedicationRequest/{request_id}"}],
        "quantity": {"value": quantity, "unit": "unit"},
        "whenHandedOver": _now_iso(),
        "performer": [{"actor": {"display": user.username}}],
    }
    if request_body.get("encounter"):
        dispense_body["encounter"] = request_body["encounter"]
    rendered = fhir.create_resource("MedicationDispense", dispense_body, session, user)

    stock_after = None
    if stock_row is not None:
        stock_row.quantity -= quantity
        stock_after = stock_row.quantity
        session.flush()
    body = dict(request_body)
    body["status"] = "dispensed"
    med_request.body = body
    med_request.status = "dispensed"
    med_request.version += 1
    session.flush()
    append_audit(session, actor=user.username, action="pharmacy.dispense",
                 entity=f"MedicationDispense/{rendered['id']}",
                 payload={"medication_request_id": str(request_id),
                          "medication_code": code, "quantity": quantity,
                          "stock_tracked": bool(entry.get("stockTracked")),
                          "stock_after": stock_after,
                          "monitoring_warnings": monitoring_warnings})
    session.commit()
    return {"dispense": rendered,
            "medication_request": fhir._render(med_request),
            "stock_after": stock_after,
            "monitoring_warnings": monitoring_warnings}


@router.get("/dispenses")
def list_dispenses(patient: str | None = Query(default=None),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("pharmacy.read"))):
    stmt = select(FhirResource).where(
        FhirResource.resource_type == "MedicationDispense")
    if patient:
        stmt = stmt.where(FhirResource.patient_ref == fhir._normalize_patient_ref(patient))
    rows = session.scalars(stmt.order_by(FhirResource.created_at.asc(),
                                         FhirResource.id.asc())).all()
    return fhir._bundle(rows)


# ---------------------------------------------------------------------------
# BCMA-lite: barcode medication administration
# ---------------------------------------------------------------------------

# The BCMA rights v1 implements, in enforcement order.
BCMA_RIGHTS = ("right-dispense", "right-medication", "right-patient")


def _bcma_rights(session: Session) -> list[str]:
    """Which BCMA rights are enforced, from the published formulary's
    `bcmaRights` list (unknown names dropped; order preserved). The
    default is all three — the original behaviour. An explicitly empty
    list enforces none. (The dispense LOOKUP itself is not optional: a
    nonexistent dispense cannot be administered under any policy and
    still fails with the right-dispense 422.)"""
    cfg = published_config(session, "terminology", FORMULARY_KEY)
    raw = (cfg.payload or {}).get("bcmaRights") if cfg is not None else None
    if isinstance(raw, list):
        return [r for r in raw if r in BCMA_RIGHTS]
    return list(BCMA_RIGHTS)

def _administered_dispense_ids(session: Session) -> set[str]:
    ids = set()
    rows = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "MedicationAdministration")).all()
    for row in rows:
        for ext in (row.body or {}).get("extension") or []:
            if isinstance(ext, dict) and ext.get("url") == DISPENSE_EXTENSION:
                reference = ((ext.get("valueReference") or {}).get("reference") or "")
                if reference.startswith("MedicationDispense/"):
                    ids.add(reference.split("/", 1)[1])
    return ids


@router.post("/administer", status_code=201)
async def administer(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("pharmacy.administer"))):
    """BCMA-lite scan: {wristband_id, medication_code, dispense_id}.

    The enforced rights (which ones is Studio configuration — the
    formulary's `bcmaRights`, defaulting to all three), checked in
    order, each failing as a 422 that NAMES the right:
    - right dispense: the scanned dispense exists;
    - right medication: the dispense's medication code equals the scan;
    - right patient: the dispense's patient equals the wristband id.
    Administering an already-administered dispense -> 409. A clean scan
    creates the MedicationAdministration linked to the dispense."""
    payload = await _json_body(request)
    wristband = payload.get("wristband_id") or payload.get("patient_id")
    med_code = payload.get("medication_code") or payload.get("code")
    dispense_id = payload.get("dispense_id")
    if not wristband:
        raise HTTPException(status_code=422,
                            detail="Med-admin scan requires 'wristband_id' "
                                   "(the patient's wristband / id)")
    if not med_code:
        raise HTTPException(status_code=422,
                            detail="Med-admin scan requires 'medication_code'")
    if not dispense_id:
        raise HTTPException(status_code=422,
                            detail="Med-admin scan requires 'dispense_id' "
                                   "(right dispense cannot be verified)")

    rights = _bcma_rights(session)
    dispense = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "MedicationDispense",
            FhirResource.resource_id == str(dispense_id))).first()
    if dispense is None:
        raise HTTPException(
            status_code=422,
            detail=(f"Right dispense failed: MedicationDispense/{dispense_id} "
                    "was not found"))
    dispense_body = dispense.body or {}
    _, dispense_code, concept = _medication_of(dispense_body)
    if "right-medication" in rights and str(med_code) != dispense_code:
        raise HTTPException(
            status_code=422,
            detail=(f"Right medication failed: scanned medication '{med_code}' "
                    f"does not match the dispensed medication '{dispense_code}'"))
    expected_patient = fhir._normalize_patient_ref(str(wristband))
    actual_patient = (dispense_body.get("subject") or {}).get("reference")
    if "right-patient" in rights and actual_patient != expected_patient:
        raise HTTPException(
            status_code=422,
            detail=(f"Right patient failed: wristband '{expected_patient}' does "
                    f"not match the dispense's patient '{actual_patient}'"))
    if str(dispense_id) in _administered_dispense_ids(session):
        raise HTTPException(
            status_code=409,
            detail=(f"MedicationDispense/{dispense_id} has already been "
                    "administered; a dispense is administered once"))

    admin_body = {
        "resourceType": "MedicationAdministration",
        "id": str(uuid.uuid4()),
        "status": "completed",
        "medicationCodeableConcept": concept,
        "subject": dispense_body.get("subject"),
        "effectiveDateTime": _now_iso(),
        "performer": [{"actor": {"display": user.username}}],
        "extension": [{"url": DISPENSE_EXTENSION,
                       "valueReference": {
                           "reference": f"MedicationDispense/{dispense_id}"}}],
    }
    prescriptions = dispense_body.get("authorizingPrescription") or []
    if prescriptions:
        admin_body["request"] = prescriptions[0]
    if dispense_body.get("encounter"):
        admin_body["encounter"] = dispense_body["encounter"]
    rendered = fhir.create_resource("MedicationAdministration", admin_body,
                                    session, user)
    append_audit(session, actor=user.username, action="pharmacy.administer",
                 entity=f"MedicationAdministration/{rendered['id']}",
                 payload={"dispense_id": str(dispense_id),
                          "medication_code": dispense_code,
                          "patient": actual_patient,
                          # The rights actually enforced for this scan,
                          # in the audit's canonical display order.
                          "checks": [r for r in ("right-patient",
                                                 "right-medication",
                                                 "right-dispense")
                                     if r in rights]})
    session.commit()
    return rendered
