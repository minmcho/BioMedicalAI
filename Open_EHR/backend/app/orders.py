"""Phase 5 — General orders (CPOE core).

A general order framework on the FHIR store, built ON the Studio
customization engine like every other module (roadmap §6: configure,
don't fork):

- the order TYPE catalogue — which order types exist, which FHIR
  resource each maps to, and which fields each type requires — is
  terminology config, key ``order-types``;
- the order lifecycle (draft -> signed -> active -> completed /
  discontinued / cancelled, with per-transition roles) is workflow
  config, key ``order-flow``;
- ORDER SETS are Studio config of kind ``orderset``: named bundles that
  expand into individual orders on placement;

all seeded as published v1 at startup by :func:`ensure_orders_defaults`.

Orders are FHIR resources: medication orders are MedicationRequests,
everything else is a ServiceRequest carrying an order-type extension.
An order starts in `draft`; UNSIGNED (draft) orders are invisible to
every fulfilment surface (the fulfilment queue, the consult inbox and
the MAR all read signed/active orders only). Lab orders are NOT here:
they keep their own governed module (app.lab) and carry no order-type
extension, so the two surfaces never mix.

Decision support (honest, config-driven, and deliberately narrow):
- ALLERGY: a medication order whose code matches an active
  AllergyIntolerance code on the patient's chart is refused (422
  naming the allergen) unless an override reason is supplied; the
  reason is recorded on the order. The check runs at placement AND at
  signing (allergies recorded in between are caught).
- DUPLICATE: placing an order whose type+code already has a
  draft/signed/active order for the patient returns a warning flag on
  the response — never a hard stop.
- PRIOR AUTHORISATION (Phase 7, app.access): at SIGNING, a service
  flagged auth-required in the published payer configuration needs an
  approved authorisation covering the patient + service (422 naming
  the missing authorisation; `auth_override_reason` proceeds and is
  recorded on the order). Inert under the seeded payer defaults.
- Drug-interaction checking is NOT implemented: there is no interaction
  database in this system, and none is faked (see the Phase 5 SQA
  report's NOT RUN list).

Consults: a consult order also creates a FHIR Task for the receiving
role; the task surfaces in that role's inbox (GET /orders/inbox) once
the order is signed, and completing/discontinuing the order completes/
cancels the task.

MAR (medication administration record): GET /orders/mar/due computes
the due windows for a patient's signed/active medication orders from
each order's dosage timing (falling back to the medication order
type's `timingDefaults` in the order-types config) and matches Phase 4A
MedicationAdministration records (created through the BCMA path in
app.pharmacy — this module never writes administrations) into
given / due / missed / scheduled doses. Missed is COMPUTED at read
time from the window and the clock; it is never stored.
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir
from .audit import append_audit
from .configstore import (load_workflow_machine, published_config,
                          published_configs, transition_role_allowed)
from .models import ConfigStatus, FhirResource, StudioConfig, User
from .security import get_db, require_capability

router = APIRouter(prefix="/orders", tags=["orders"])

ORDER_TYPES_KEY = "order-types"
ORDER_WORKFLOW_KEY = "order-flow"
ORDER_TYPES_SYSTEM = "urn:opencare:order-type"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"

ORDER_TYPE_EXTENSION = "urn:opencare:extension/order-type"
ORDER_SET_EXTENSION = "urn:opencare:extension/order-set"
ALLERGY_OVERRIDE_EXTENSION = "urn:opencare:extension/allergy-override"
MODALITY_EXTENSION = "urn:opencare:extension/modality"
PERFORMER_ROLE_EXTENSION = "urn:opencare:extension/performer-role"

# Fallback copy of the order lifecycle — the live machine is the
# published workflow config (seeded v1 below with exactly these
# transitions); this constant is used only if that config is disabled.
DEFAULT_ORDER_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"signed", "cancelled"},
    "signed": {"active", "discontinued", "cancelled"},
    "active": {"completed", "discontinued", "cancelled"},
    "completed": set(),
    "discontinued": set(),
    "cancelled": set(),
}

# States in which an order is visible to fulfilment (queue/inbox/MAR).
FULFILMENT_STATES = ("signed", "active")
# States that count as "already ordered" for the duplicate warning.
DUPLICATE_STATES = ("draft", "signed", "active")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(raw) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# Default Studio configuration (seeded published v1 at startup)
# ---------------------------------------------------------------------------

def default_order_types_payload() -> dict:
    """The order-type catalogue. `requiredFields` names placement
    payload fields (camelCase) that must be present for the type;
    `timingDefaults` (medication) feeds the MAR when an order carries
    no dosage timing of its own."""
    def otype(code, display, resource, required, **extra):
        concept = {"code": code, "display": display, "active": True,
                   "resourceType": resource, "requiredFields": required}
        concept.update(extra)
        return concept

    return {
        "name": "OpenCare order types (CPOE catalogue)",
        "system": ORDER_TYPES_SYSTEM,
        "concepts": [
            otype("medication", "Medication", "MedicationRequest", ["code"],
                  timingDefaults={"frequencyHours": 24, "windowHours": 2}),
            otype("procedure", "Procedure", "ServiceRequest", ["code"]),
            otype("consult", "Consultation", "ServiceRequest",
                  ["code", "performerRole"]),
            otype("imaging", "Imaging", "ServiceRequest",
                  ["code", "modality"]),
            otype("nursing", "Nursing", "ServiceRequest", ["code"]),
            otype("diet", "Diet", "ServiceRequest", ["code"]),
            otype("activity", "Activity", "ServiceRequest", ["code"]),
        ],
    }


def default_order_workflow_payload() -> dict:
    states = sorted(DEFAULT_ORDER_TRANSITIONS.keys())
    transitions = []
    for src in sorted(DEFAULT_ORDER_TRANSITIONS):
        for dst in sorted(DEFAULT_ORDER_TRANSITIONS[src]):
            entry = {"from": src, "to": dst}
            if (src, dst) == ("draft", "signed"):
                # Signing is the ordering clinician's act (an admin may
                # sign in the dev/demo roles model); a configurator —
                # a systems role — cannot sign clinical orders.
                entry["roles"] = ["admin", "clinician"]
            transitions.append(entry)
    return {"states": states, "transitions": transitions}


def default_order_set_payloads() -> dict[str, dict]:
    return {
        "admission-general": {
            "name": "Admission — general",
            "description": "Synthetic general-admission order set: ECG, "
                           "vital-signs nursing, regular diet, ambulation.",
            "orders": [
                {"type": "procedure", "code": "93005",
                 "display": "Electrocardiogram"},
                {"type": "nursing", "code": "vital-signs",
                 "display": "Vital signs every 4 hours"},
                {"type": "diet", "code": "regular-diet",
                 "display": "Regular diet"},
                {"type": "activity", "code": "ambulate",
                 "display": "Ambulate three times daily"},
            ],
        },
        "diabetes-admission": {
            "name": "Diabetes — admission",
            "description": "Synthetic diabetes admission set: metformin, "
                           "lisinopril, glucose monitoring, diabetic diet.",
            "orders": [
                {"type": "medication", "code": "847232",
                 "display": "Metformin hydrochloride 500 MG Oral Tablet"},
                {"type": "medication", "code": "1049221",
                 "display": "Lisinopril 10 MG Oral Tablet"},
                {"type": "nursing", "code": "glucose-monitoring",
                 "display": "Point-of-care glucose before meals"},
                {"type": "diet", "code": "diabetic-diet",
                 "display": "Carbohydrate-consistent diet"},
            ],
        },
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


def ensure_orders_defaults(session: Session) -> None:
    """Seed the order-types catalogue, the order lifecycle and the two
    demo order sets as published Studio config v1 (idempotent)."""
    _seed_published(session, "terminology", ORDER_TYPES_KEY,
                    default_order_types_payload())
    _seed_published(session, "workflow", ORDER_WORKFLOW_KEY,
                    default_order_workflow_payload())
    for key, payload in default_order_set_payloads().items():
        _seed_published(session, "orderset", key, payload)
    session.commit()


# ---------------------------------------------------------------------------
# Config accessors
# ---------------------------------------------------------------------------

def _types_payload(session: Session) -> dict:
    cfg = published_config(session, "terminology", ORDER_TYPES_KEY)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=(f"No published order-type catalogue "
                    f"(terminology/{ORDER_TYPES_KEY}); publish one in "
                    "Studio before using orders"))
    return cfg.payload or {}


def _type_concept(session: Session, type_code: str) -> dict | None:
    for concept in _types_payload(session).get("concepts") or []:
        if isinstance(concept, dict) and concept.get("code") == type_code:
            return concept
    return None


def _order_machine(session: Session):
    return load_workflow_machine(session, ORDER_WORKFLOW_KEY,
                                 DEFAULT_ORDER_TRANSITIONS)


# ---------------------------------------------------------------------------
# Small body helpers
# ---------------------------------------------------------------------------

def _extension(body: dict, url: str) -> dict | None:
    for ext in (body or {}).get("extension") or []:
        if isinstance(ext, dict) and ext.get("url") == url:
            return ext
    return None


def _set_extension(body: dict, url: str, **values) -> None:
    extensions = [e for e in (body.get("extension") or [])
                  if not (isinstance(e, dict) and e.get("url") == url)]
    extensions.append({"url": url, **values})
    body["extension"] = extensions


def _code_of(body: dict, field: str = "code") -> tuple[str | None, str | None, str | None]:
    concept = (body or {}).get(field) or {}
    coding = concept.get("coding") or []
    if coding and isinstance(coding[0], dict):
        return (coding[0].get("system"), coding[0].get("code"),
                coding[0].get("display"))
    return None, None, concept.get("text") if isinstance(concept, dict) else None


def _field(payload: dict, name: str):
    """Read a placement field under its camelCase or snake_case name."""
    if payload.get(name) is not None:
        return payload.get(name)
    snake = "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")
    return payload.get(snake)


def _order_type_of(row: FhirResource) -> str | None:
    body = row.body or {}
    ext = _extension(body, ORDER_TYPE_EXTENSION)
    if ext is not None and ext.get("valueCode"):
        return ext["valueCode"]
    if row.resource_type == "MedicationRequest":
        return "medication"
    for category in body.get("category") or []:
        for coding in (category or {}).get("coding") or []:
            if isinstance(coding, dict) and coding.get("system") == ORDER_TYPES_SYSTEM:
                return coding.get("code")
    return None


def _is_order_row(row: FhirResource) -> bool:
    """CPOE orders only: MedicationRequests, plus ServiceRequests that
    carry the order-type marker (lab ServiceRequests do not — they
    belong to app.lab)."""
    if row.resource_type == "MedicationRequest":
        return True
    if row.resource_type == "ServiceRequest":
        return _order_type_of(row) is not None
    return False


def _find_order(session: Session, order_id: str) -> FhirResource:
    for resource_type in ("MedicationRequest", "ServiceRequest"):
        row = session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == resource_type,
                FhirResource.resource_id == order_id)).first()
        if row is not None and _is_order_row(row):
            return row
    raise HTTPException(status_code=404, detail=f"Order {order_id} not found")


def _patient_orders(session: Session, patient_ref: str) -> list[FhirResource]:
    rows = session.scalars(
        select(FhirResource)
        .where(FhirResource.resource_type.in_(("MedicationRequest", "ServiceRequest")),
               FhirResource.patient_ref == patient_ref)
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    return [r for r in rows if _is_order_row(r)]


def _order_code(row: FhirResource) -> tuple[str | None, str | None]:
    body = row.body or {}
    if row.resource_type == "MedicationRequest":
        _, code, display = _code_of(body, "medicationCodeableConcept")
        return code, display
    _, code, display = _code_of(body)
    return code, display


def _render_order(row: FhirResource) -> dict:
    out = fhir._render(row)
    out["orderType"] = _order_type_of(row)
    return out


# ---------------------------------------------------------------------------
# Decision support: allergy (hard stop, overridable with a recorded
# reason) + duplicate active order (warning only)
# ---------------------------------------------------------------------------

def _allergy_conflict(session: Session, patient_ref: str,
                      code: str) -> dict | None:
    """The patient's ACTIVE AllergyIntolerance whose code matches the
    ordered medication code, or None. An allergy whose clinicalStatus
    is recorded inactive/resolved does not block."""
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "AllergyIntolerance",
            FhirResource.patient_ref == patient_ref)).all()
    for row in rows:
        body = row.body or {}
        status = ((body.get("clinicalStatus") or {}).get("coding") or [{}])[0].get("code")
        if status in ("inactive", "resolved"):
            continue
        concept = body.get("code") or {}
        for coding in concept.get("coding") or []:
            if isinstance(coding, dict) and coding.get("code") == code:
                return {"code": code,
                        "display": coding.get("display") or concept.get("text") or code,
                        "allergy_id": row.resource_id}
    return None


def _enforce_allergy(session: Session, patient_ref: str, code: str,
                      override_reason) -> dict | None:
    """Returns the conflict when an override reason was supplied (the
    caller records it on the order); raises 422 naming the allergen
    when there is a conflict and no reason."""
    conflict = _allergy_conflict(session, patient_ref, code)
    if conflict is None:
        return None
    if isinstance(override_reason, str) and override_reason.strip():
        return conflict
    raise HTTPException(
        status_code=422,
        detail=(f"Allergy alert: this patient has a recorded allergy to "
                f"{conflict['display']} (code {conflict['code']}); the order "
                "was refused. To proceed, supply 'allergy_override_reason' — "
                "the reason is recorded on the order"))


def _enforce_auth(session: Session, row: FhirResource, body: dict,
                  payload: dict) -> None:
    """Prior-authorisation gate (Phase 7, app.access): when the
    published payer configuration flags this order's type or clinical
    code as auth-required, signing requires an APPROVED authorisation
    covering (patient, service) — in its validity window, with units
    remaining. A covering authorisation is linked onto the order and
    consumes one unit. An `auth_override_reason` in the transition
    payload proceeds without one and is recorded on the order (the
    allergy-override pattern). Otherwise 422 naming the missing
    authorisation. With no service flagged (the seeded default) this
    gate is inert."""
    from . import access
    required = access.auth_required_services(session)
    if not required:
        return
    type_code = _order_type_of(row)
    code, _ = _order_code(row)
    matched = {c for c in (type_code, code) if c} & required
    if not matched:
        return
    service = sorted(matched)[0]
    patient_ref = row.patient_ref or ""
    covering = access.find_covering_auth(session, patient_ref, matched)
    if covering is not None:
        covering.used_units += 1
        session.flush()
        _set_extension(body, access.AUTH_EXTENSION,
                       valueString=covering.auth_id)
        append_audit(session, actor="system", action="orders.auth.applied",
                     entity=f"{row.resource_type}/{row.resource_id}",
                     payload={"auth_id": covering.auth_id,
                              "auth_number": covering.auth_number,
                              "service_code": service,
                              "used_units": covering.used_units})
        return
    override = _field(payload, "authOverrideReason")
    if isinstance(override, str) and override.strip():
        _set_extension(body, access.AUTH_OVERRIDE_EXTENSION,
                       valueString=override.strip())
        return
    raise HTTPException(
        status_code=422,
        detail=(f"Prior authorisation required: service '{service}' is "
                "flagged as requiring authorisation in the published "
                "payer configuration, and no approved authorisation "
                f"covers {patient_ref} for it. Obtain an approved "
                "authorisation first, or supply 'auth_override_reason' "
                "— the reason is recorded on the order"))


def _duplicate_warnings(session: Session, patient_ref: str, type_code: str,
                        code: str, exclude_id: str | None = None) -> list[str]:
    warnings = []
    for row in _patient_orders(session, patient_ref):
        if exclude_id is not None and row.resource_id == exclude_id:
            continue
        if (row.body or {}).get("status") not in DUPLICATE_STATES:
            continue
        if _order_type_of(row) != type_code:
            continue
        other_code, _ = _order_code(row)
        if other_code == code:
            warnings.append(
                f"duplicate-active-order: {type_code} {code} already has a "
                f"{(row.body or {}).get('status')} order ({row.resource_id}) "
                "for this patient")
    return warnings


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


def _validate_placement(session: Session, payload: dict) -> tuple[dict, str]:
    """Validate one order placement against the published type
    catalogue. Returns (type concept, patient_ref). Raises 404 for an
    unknown patient, 422 for an unknown/inactive type or a missing
    per-type required field."""
    type_code = payload.get("type")
    if not isinstance(type_code, str) or not type_code:
        raise HTTPException(status_code=422,
                            detail="Order requires 'type' (an order-type code)")
    concept = _type_concept(session, type_code)
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown order type '{type_code}': not in the published "
                   "order-type catalogue")
    if concept.get("active") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Order type '{type_code}' is inactive in the published "
                   "order-type catalogue")
    for field_name in concept.get("requiredFields") or []:
        value = _field(payload, str(field_name))
        if value is None or (isinstance(value, str) and not value.strip()):
            raise HTTPException(
                status_code=422,
                detail=(f"Order type '{type_code}' requires field "
                        f"'{field_name}'"))
    subject = payload.get("subject") or {}
    patient_ref = subject.get("reference") if isinstance(subject, dict) else None
    if not patient_ref and payload.get("patient"):
        patient_ref = fhir._normalize_patient_ref(str(payload["patient"]))
    if not patient_ref or not patient_ref.startswith("Patient/"):
        raise HTTPException(
            status_code=422,
            detail="Order requires 'subject.reference' (or 'patient') naming a Patient")
    patient_id = patient_ref.split("/", 1)[1]
    fhir._get_row("Patient", patient_id, session)  # 404 if unknown
    return concept, patient_ref


def _create_order(session: Session, user: User, payload: dict,
                  concept: dict, patient_ref: str,
                  from_set: str | None = None) -> tuple[dict, list[str]]:
    """Create one order resource in `draft`. Assumes _validate_placement
    and the allergy gate have already run for medication orders."""
    type_code = concept["code"]
    code = str(_field(payload, "code"))
    display = payload.get("display") or payload.get("text")
    system = payload.get("system")
    authored_on = payload.get("authoredOn") or _now_iso()
    order_id = str(payload.get("id") or uuid.uuid4())
    extensions = [{"url": ORDER_TYPE_EXTENSION, "valueCode": type_code}]
    if from_set:
        extensions.append({"url": ORDER_SET_EXTENSION, "valueString": from_set})
    override_reason = _field(payload, "allergyOverrideReason")
    if type_code == "medication" and isinstance(override_reason, str) \
            and override_reason.strip():
        extensions.append({"url": ALLERGY_OVERRIDE_EXTENSION,
                           "valueString": override_reason.strip()})

    warnings = _duplicate_warnings(session, patient_ref, type_code, code)

    if concept.get("resourceType") == "MedicationRequest":
        coding = {"code": code}
        if system:
            coding["system"] = system
        if display:
            coding["display"] = display
        body = {
            "resourceType": "MedicationRequest",
            "id": order_id,
            "status": "draft",
            "intent": "order",
            "medicationCodeableConcept": {"coding": [coding],
                                          "text": display or code},
            "subject": {"reference": patient_ref},
            "authoredOn": authored_on,
            "requester": {"display": user.username},
            "extension": extensions,
        }
        dosage = payload.get("dosageInstruction")
        if isinstance(dosage, list) and dosage:
            body["dosageInstruction"] = dosage
        elif isinstance(payload.get("dosage_text"), str) and payload["dosage_text"]:
            body["dosageInstruction"] = [{"text": payload["dosage_text"]}]
        elif isinstance(payload.get("dosageText"), str) and payload["dosageText"]:
            body["dosageInstruction"] = [{"text": payload["dosageText"]}]
    else:
        coding = {"system": system or "urn:opencare:orders", "code": code}
        if display:
            coding["display"] = display
        body = {
            "resourceType": "ServiceRequest",
            "id": order_id,
            "status": "draft",
            "intent": "order",
            "category": [{"coding": [{
                "system": ORDER_TYPES_SYSTEM, "code": type_code,
                "display": concept.get("display")}]}],
            "code": {"coding": [coding], "text": display or code},
            "subject": {"reference": patient_ref},
            "authoredOn": authored_on,
            "requester": {"display": user.username},
            "extension": extensions,
        }
        if type_code == "consult":
            role = str(_field(payload, "performerRole"))
            body["performerType"] = [{"coding": [{
                "system": "urn:opencare:role", "code": role,
                "display": role}]}]
        if type_code == "imaging":
            _set_extension(body, MODALITY_EXTENSION,
                           valueString=str(_field(payload, "modality")))
    # bodySite is a standard FHIR order field (and the canonical home
    # of LATARALITY — e.g. ophthalmology's OD/OS/OU): carried through
    # verbatim from the placement payload / order-set item when given.
    # Generic and specialty-agnostic; per-type enforcement of its
    # presence is the catalogue's `requiredFields` mechanism, exactly
    # like any other required placement field (Phase 9).
    if isinstance(payload.get("bodySite"), dict) and payload["bodySite"]:
        body["bodySite"] = payload["bodySite"]
    if isinstance(payload.get("encounter"), dict) and payload["encounter"].get("reference"):
        body["encounter"] = {"reference": payload["encounter"]["reference"]}
    if payload.get("priority"):
        body["priority"] = payload["priority"]
    if isinstance(payload.get("note"), str) and payload["note"]:
        body["note"] = [{"text": payload["note"]}]

    rendered = fhir.create_resource(body["resourceType"], body, session, user)

    if type_code == "consult":
        task_body = {
            "resourceType": "Task",
            "id": str(uuid.uuid4()),
            "status": "requested",
            "intent": "order",
            "description": display or code,
            "focus": {"reference": f"ServiceRequest/{rendered['id']}"},
            "for": {"reference": patient_ref},
            "authoredOn": _now_iso(),
            "requester": {"display": user.username},
            "extension": [{"url": PERFORMER_ROLE_EXTENSION,
                           "valueString": str(_field(payload, "performerRole"))}],
        }
        fhir.create_resource("Task", task_body, session, user)

    append_audit(session, actor=user.username, action="orders.place",
                 entity=f"{body['resourceType']}/{rendered['id']}",
                 payload={"order_type": type_code, "code": code,
                          "patient": patient_ref, "from_set": from_set,
                          "allergy_override": bool(
                              _extension(body, ALLERGY_OVERRIDE_EXTENSION))})
    session.commit()
    return rendered, warnings


@router.get("/types")
def order_types(session: Session = Depends(get_db),
                user: User = Depends(require_capability("orders.read"))):
    cfg = published_config(session, "terminology", ORDER_TYPES_KEY)
    workflow_cfg = published_config(session, "workflow", ORDER_WORKFLOW_KEY)
    return {"order_types": cfg.payload if cfg else None,
            "version": cfg.version if cfg else None,
            "workflow": workflow_cfg.payload if workflow_cfg else None,
            "workflow_version": workflow_cfg.version if workflow_cfg else None}


@router.get("/sets")
def order_sets(session: Session = Depends(get_db),
               user: User = Depends(require_capability("orders.read"))):
    sets = [{"key": cfg.key, "version": cfg.version, **(cfg.payload or {})}
            for cfg in published_configs(session, "orderset")]
    return {"sets": sets, "total": len(sets)}


@router.post("", status_code=201)
async def place_order(request: Request, session: Session = Depends(get_db),
                      user: User = Depends(require_capability("orders.place"))):
    payload = await _json_body(request)
    concept, patient_ref = _validate_placement(session, payload)
    code = str(_field(payload, "code"))
    warnings: list[str] = []
    if concept["code"] == "medication":
        conflict = _enforce_allergy(
            session, patient_ref, code, _field(payload, "allergyOverrideReason"))
        if conflict is not None:
            warnings.append(
                f"allergy-overridden: {conflict['display']} (code "
                f"{conflict['code']}) — override reason recorded on the order")
    rendered, placement_warnings = _create_order(
        session, user, payload, concept, patient_ref)
    return {"order": rendered, "order_type": concept["code"],
            "warnings": warnings + placement_warnings}


@router.post("/from-set", status_code=201)
async def place_order_set(request: Request, session: Session = Depends(get_db),
                          user: User = Depends(require_capability("orders.place"))):
    """Expand a published order set into individual draft orders for
    one patient. Every item is validated (type catalogue, required
    fields, allergy gate) BEFORE any order is created, so a set that
    fails validation creates nothing."""
    payload = await _json_body(request)
    set_key = payload.get("set") or payload.get("set_key")
    if not isinstance(set_key, str) or not set_key:
        raise HTTPException(status_code=422,
                            detail="Order-set placement requires 'set' (the set key)")
    cfg = published_config(session, "orderset", set_key)
    if cfg is None:
        raise HTTPException(status_code=422,
                            detail=f"Unknown order set '{set_key}': "
                                   "no published orderset config")
    subject = payload.get("subject") or {}
    patient_ref = subject.get("reference") if isinstance(subject, dict) else None
    if not patient_ref and payload.get("patient"):
        patient_ref = fhir._normalize_patient_ref(str(payload["patient"]))
    if not patient_ref or not patient_ref.startswith("Patient/"):
        raise HTTPException(
            status_code=422,
            detail="Order-set placement requires 'subject.reference' (or 'patient')")
    fhir._get_row("Patient", patient_ref.split("/", 1)[1], session)

    items = (cfg.payload or {}).get("orders") or []
    prepared: list[tuple[dict, dict]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422,
                                detail=f"Order set '{set_key}' item {i} is malformed")
        merged = {**item, "subject": {"reference": patient_ref}}
        if payload.get("encounter"):
            merged.setdefault("encounter", payload["encounter"])
        override = _field(payload, "allergyOverrideReason")
        if override is not None:
            merged.setdefault("allergyOverrideReason", override)
        concept, _ = _validate_placement(session, merged)
        if concept["code"] == "medication":
            _enforce_allergy(session, patient_ref,
                             str(_field(merged, "code")), override)
        prepared.append((merged, concept))

    orders_out = []
    warnings: list[str] = []
    for merged, concept in prepared:
        rendered, item_warnings = _create_order(
            session, user, merged, concept, patient_ref, from_set=set_key)
        orders_out.append(rendered)
        warnings.extend(item_warnings)
    return {"set": set_key, "orders": orders_out, "warnings": warnings,
            "total": len(orders_out)}


# ---------------------------------------------------------------------------
# Reads: list / detail / fulfilment queue / consult inbox
# ---------------------------------------------------------------------------

@router.get("")
def list_orders(patient: str | None = Query(default=None),
                type: str | None = Query(default=None),
                status: str | None = Query(default=None),
                session: Session = Depends(get_db),
                user: User = Depends(require_capability("orders.read"))):
    if patient:
        rows = _patient_orders(session, fhir._normalize_patient_ref(patient))
    else:
        rows = [r for r in session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type.in_(
                    ("MedicationRequest", "ServiceRequest")))
            .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
            if _is_order_row(r)]
    out = []
    for row in rows:
        if type and _order_type_of(row) != type:
            continue
        if status and (row.body or {}).get("status") != status:
            continue
        out.append(_render_order(row))
    return {"items": out, "total": len(out)}


@router.get("/queue")
def fulfilment_queue(type: str | None = Query(default=None),
                     patient: str | None = Query(default=None),
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability("orders.read"))):
    """Orders awaiting or undergoing fulfilment: SIGNED and ACTIVE
    only. Draft (unsigned) orders never appear here — an unsigned order
    is not work anyone may act on."""
    if patient:
        rows = _patient_orders(session, fhir._normalize_patient_ref(patient))
    else:
        rows = [r for r in session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type.in_(("MedicationRequest", "ServiceRequest")),
                FhirResource.status.in_(FULFILMENT_STATES))
            .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
            if _is_order_row(r)]
    out = []
    for row in rows:
        if (row.body or {}).get("status") not in FULFILMENT_STATES:
            continue
        if type and _order_type_of(row) != type:
            continue
        out.append(_render_order(row))
    return {"items": out, "total": len(out)}


@router.get("/inbox")
def consult_inbox(role: str | None = Query(default=None),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability("orders.read"))):
    """The consult fulfilment inbox for one receiving role (default:
    the caller's own role): Tasks whose consult order is SIGNED or
    ACTIVE. A consult placed but not yet signed does not appear."""
    target_role = role or user.role.value
    tasks = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Task")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    items = []
    for task in tasks:
        body = task.body or {}
        ext = _extension(body, PERFORMER_ROLE_EXTENSION)
        if ext is None or ext.get("valueString") != target_role:
            continue
        focus = (body.get("focus") or {}).get("reference") or ""
        if not focus.startswith("ServiceRequest/"):
            continue
        try:
            order = _find_order(session, focus.split("/", 1)[1])
        except HTTPException:
            continue
        if (order.body or {}).get("status") not in FULFILMENT_STATES:
            continue
        items.append({"task": fhir._render(task), "order": _render_order(order)})
    return {"items": items, "total": len(items), "role": target_role}


@router.get("/{order_id}")
def get_order(order_id: str, session: Session = Depends(get_db),
              user: User = Depends(require_capability("orders.read"))):
    return _render_order(_find_order(session, order_id))


# ---------------------------------------------------------------------------
# Lifecycle transitions (published order-flow workflow config)
# ---------------------------------------------------------------------------

def _sync_consult_task(session: Session, order_row: FhirResource, target: str) -> str | None:
    """Completing / discontinuing / cancelling a consult order moves
    its Task in step (completed / cancelled). Returns the task id."""
    if _order_type_of(order_row) != "consult":
        return None
    focus_ref = f"ServiceRequest/{order_row.resource_id}"
    tasks = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Task")).all()
    for task in tasks:
        if ((task.body or {}).get("focus") or {}).get("reference") != focus_ref:
            continue
        body = dict(task.body or {})
        body["status"] = "completed" if target == "completed" else "cancelled"
        task.body = body
        task.status = body["status"]
        task.version += 1
        session.flush()
        return task.resource_id
    return None


@router.post("/{order_id}/transition")
async def transition_order(order_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("orders.place"))):
    """Advance an order through the published order-flow workflow
    config (unknown state 422 -> illegal transition 409 -> restricted
    role 403, the uniform order of the other machines). Signing a
    medication order re-runs the allergy gate: an allergy recorded
    after placement still blocks the signature unless an override
    reason is supplied with the transition (recorded on the order)."""
    payload = await _json_body(request)
    target = payload.get("to_status") or payload.get("status")
    if not target:
        raise HTTPException(status_code=422, detail="Transition requires 'to_status'")
    row = _find_order(session, order_id)
    body = dict(row.body or {})
    current = body.get("status")
    transitions, known_states, roles = _order_machine(session)
    if target not in known_states:
        raise HTTPException(status_code=422, detail=f"Unknown order status: {target}")
    if target not in transitions.get(current, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Illegal order transition '{current}' -> '{target}'")
    if not transition_role_allowed(roles, current, target, user.role.value):
        raise HTTPException(
            status_code=403,
            detail=(f"Role {user.role.value} may not perform the order "
                    f"transition '{current}' -> '{target}': the published "
                    "workflow config restricts it to "
                    f"{', '.join(roles.get((current, target), []))}"))

    if target == "signed" and row.resource_type == "MedicationRequest":
        _, code, _ = _code_of(body, "medicationCodeableConcept")
        patient_ref = row.patient_ref or ""
        override = _field(payload, "allergyOverrideReason")
        if not override:
            # The override recorded on the order at placement (or a
            # previous sign attempt) also authorises the signature.
            for ext in body.get("extension") or []:
                if ext.get("url") == ALLERGY_OVERRIDE_EXTENSION:
                    override = ext.get("valueString")
                    break
        if code:
            _enforce_allergy(session, patient_ref, code, override)
        if isinstance(override, str) and override.strip():
            _set_extension(body, ALLERGY_OVERRIDE_EXTENSION,
                           valueString=override.strip())

    if target == "signed":
        # Prior-authorisation gate (Phase 7): runs for every order
        # type, after the allergy gate; inert unless the published
        # payer config flags the service.
        _enforce_auth(session, row, body, payload)

    body["status"] = target
    row.body = body
    row.status = target
    row.version += 1
    session.flush()
    task_id = None
    if target in ("completed", "discontinued", "cancelled"):
        task_id = _sync_consult_task(session, row, target)
    append_audit(session, actor=user.username, action="orders.transition",
                 entity=f"{row.resource_type}/{order_id}",
                 payload={"from_status": current, "to_status": target,
                          "task_id": task_id, "version": row.version})
    if target == "signed":
        # Phase 8: queue the outbound ORM^O01 in the signing
        # transaction (interface outbox). Lazy import keeps the
        # dependency one-way (app.interop does not import app.orders).
        from .interop import enqueue_signed_order
        enqueue_signed_order(session, row)
    session.commit()
    return _render_order(row)


# ---------------------------------------------------------------------------
# MAR — medication administration record (computed, never stored)
# ---------------------------------------------------------------------------

_UNIT_HOURS = {"min": 1.0 / 60.0, "h": 1.0, "d": 24.0, "wk": 168.0}


def _order_interval_hours(session: Session, row: FhirResource) -> float:
    """Dosing interval for one medication order: its own FHIR dosage
    timing (frequency per period) when present, else the medication
    order type's `timingDefaults.frequencyHours` from the published
    catalogue, else 24h."""
    body = row.body or {}
    instructions = body.get("dosageInstruction") or []
    if instructions and isinstance(instructions[0], dict):
        repeat = ((instructions[0].get("timing") or {}).get("repeat") or {})
        frequency = repeat.get("frequency")
        period = repeat.get("period")
        unit = repeat.get("periodUnit") or "h"
        if isinstance(frequency, (int, float)) and not isinstance(frequency, bool) \
                and isinstance(period, (int, float)) and not isinstance(period, bool) \
                and frequency > 0 and period > 0 and unit in _UNIT_HOURS:
            return float(period) * _UNIT_HOURS[unit] / float(frequency)
    concept = _type_concept(session, "medication")
    defaults = (concept or {}).get("timingDefaults") or {}
    hours = defaults.get("frequencyHours")
    if isinstance(hours, (int, float)) and not isinstance(hours, bool) and hours > 0:
        return float(hours)
    return 24.0


def _window_hours(session: Session) -> float:
    concept = _type_concept(session, "medication")
    defaults = (concept or {}).get("timingDefaults") or {}
    hours = defaults.get("windowHours")
    if isinstance(hours, (int, float)) and not isinstance(hours, bool) and hours > 0:
        return float(hours)
    return 2.0


@router.get("/mar/due")
def mar_due(patient: str = Query(...),
            from_: str | None = Query(default=None, alias="from"),
            to: str | None = Query(default=None),
            session: Session = Depends(get_db),
            user: User = Depends(require_capability("orders.read"))):
    """The MAR due list for one patient over a window (default: the
    past 24h through the next 24h): one entry per scheduled dose of
    every signed/active medication order, each dose classified
    given / due / missed / scheduled by matching Phase 4A
    administrations (BCMA records) into the dose windows. `missed` is
    computed here from the window end and the clock — never stored."""
    patient_ref = fhir._normalize_patient_ref(patient)
    patient_id = patient_ref.split("/", 1)[1]
    fhir._get_row("Patient", patient_id, session)  # 404 if unknown
    now = _now()
    start = _parse_iso(from_) or (now - timedelta(hours=24))
    end = _parse_iso(to) or (now + timedelta(hours=24))
    if end <= start:
        raise HTTPException(status_code=422,
                            detail="MAR window 'to' must be after 'from'")
    window = timedelta(hours=_window_hours(session) / 2.0)

    administrations = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "MedicationAdministration",
            FhirResource.patient_ref == patient_ref)
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    admin_by_order: dict[str, list[dict]] = {}
    admin_out = []
    for admin in administrations:
        body = admin.body or {}
        request_ref = (body.get("request") or {}).get("reference") or ""
        order_id = (request_ref.split("/", 1)[1]
                    if request_ref.startswith("MedicationRequest/") else None)
        effective = _parse_iso(body.get("effectiveDateTime"))
        entry = {"id": admin.resource_id, "order_id": order_id,
                 "effective_at": body.get("effectiveDateTime"),
                 "_effective": effective}
        admin_out.append({k: v for k, v in entry.items() if not k.startswith("_")})
        if order_id and effective is not None:
            admin_by_order.setdefault(order_id, []).append(entry)

    doses: list[dict] = []
    # "dispensed" is written onto the request by the Phase 4A pharmacy
    # when it hands the medication over; the course is still being
    # administered, so the MAR keeps such orders until the order
    # lifecycle completes / discontinues / cancels them.
    mar_states = FULFILMENT_STATES + ("dispensed",)
    med_orders = [r for r in _patient_orders(session, patient_ref)
                  if r.resource_type == "MedicationRequest"
                  and (r.body or {}).get("status") in mar_states]
    for row in med_orders:
        body = row.body or {}
        _, code, display = _code_of(body, "medicationCodeableConcept")
        interval = timedelta(hours=_order_interval_hours(session, row))
        if interval.total_seconds() <= 0:
            continue
        anchor = _parse_iso(body.get("authoredOn")) or row.created_at
        if anchor is not None and anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        if anchor is None:
            continue
        given_pool = list(admin_by_order.get(row.resource_id, []))
        # First dose at or before the window start that could still be
        # open, then forward.
        k = 0
        if anchor < start - window:
            k = int((start - window - anchor) / interval)
        while len(doses) < 500:
            due = anchor + k * interval
            if due - window > end:
                break
            k += 1
            if due + window < start:
                continue
            dose = {"order_id": row.resource_id, "medication_code": code,
                    "medication_display": display,
                    "due_at": due.isoformat(),
                    "window_start": (due - window).isoformat(),
                    "window_end": (due + window).isoformat(),
                    "administration_id": None, "status": "scheduled"}
            match = None
            for candidate in given_pool:
                if due - window <= candidate["_effective"] <= due + window:
                    match = candidate
                    break
            if match is not None:
                given_pool.remove(match)
                dose["status"] = "given"
                dose["administration_id"] = match["id"]
            elif due + window < now:
                dose["status"] = "missed"
            elif due - window <= now:
                dose["status"] = "due"
            doses.append(dose)
    doses.sort(key=lambda d: d["due_at"])
    summary = {"given": 0, "due": 0, "missed": 0, "scheduled": 0}
    for dose in doses:
        summary[dose["status"]] += 1
    return {"patient": patient_ref,
            "from": start.isoformat(), "to": end.isoformat(),
            "doses": doses, "administrations": admin_out,
            "summary": summary}
