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
from .models import (ConfigStatus, FhirResource, PharmacyStock, Role,
                     StudioConfig, User)
from .security import get_db, require_capability, require_roles

router = APIRouter(prefix="/pharmacy", tags=["pharmacy"])

STAFF_READ = (Role.admin, Role.configurator, Role.clinician, Role.viewer)

FORMULARY_KEY = "formulary"
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
# Formulary / stock / dispense queue reads (staff)
# ---------------------------------------------------------------------------

@router.get("/formulary")
def get_formulary(session: Session = Depends(get_db),
                  user: User = Depends(require_roles(*STAFF_READ))):
    cfg = published_config(session, "terminology", FORMULARY_KEY)
    return {"formulary": _formulary(session),
            "version": cfg.version if cfg else None}


@router.get("/stock")
def get_stock(session: Session = Depends(get_db),
              user: User = Depends(require_roles(*STAFF_READ))):
    return {"items": _stock_out(session)}


@router.get("/dispense-queue")
def dispense_queue(session: Session = Depends(get_db),
                   user: User = Depends(require_roles(*STAFF_READ))):
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
                          "stock_after": stock_after})
    session.commit()
    return {"dispense": rendered,
            "medication_request": fhir._render(med_request),
            "stock_after": stock_after}


@router.get("/dispenses")
def list_dispenses(patient: str | None = Query(default=None),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_roles(*STAFF_READ))):
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

    The three enforced rights, checked in order, each failing as a 422
    that NAMES the right:
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
    if str(med_code) != dispense_code:
        raise HTTPException(
            status_code=422,
            detail=(f"Right medication failed: scanned medication '{med_code}' "
                    f"does not match the dispensed medication '{dispense_code}'"))
    expected_patient = fhir._normalize_patient_ref(str(wristband))
    actual_patient = (dispense_body.get("subject") or {}).get("reference")
    if actual_patient != expected_patient:
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
                          "checks": ["right-patient", "right-medication",
                                     "right-dispense"]})
    session.commit()
    return rendered
