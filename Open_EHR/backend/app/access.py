"""Phase 7 — Access: referrals, prior authorisations, eligibility,
waitlist (registration & scheduling depth, roadmap §2/§3).

Built ON the Studio customization engine like every other module
(roadmap §6: configure, don't fork):

- PAYERS are terminology config, key ``payers`` — each payer concept
  carries its `eligibility` figures (copay / coinsurance) and its
  `authRequiredServices` list as DATA;
- referral services are terminology config, key ``referral-services``;
- slot types are terminology config, key ``slot-types``;
- the referral lifecycle (received -> triaged -> authorised / denied
  -> scheduled -> completed), the authorisation lifecycle (requested
  -> approved / denied / expired / cancelled) and the waitlist
  lifecycle are workflow config, keys ``referral-flow``,
  ``prior-auth-flow`` and ``waitlist-flow``;

all seeded as published v1 at startup by :func:`ensure_access_defaults`.
The seeded payers require authorisation for NO services, so the order
signing gate is inert until a site publishes a payer config that flags
services — existing behaviour is preserved by default.

REFERRALS are FHIR ServiceRequest resources (category `referral`,
deliberately carrying NO CPOE order-type extension, so they never mix
into the orders module's surfaces) plus a FHIR Task that carries the
triage queue. AUTHORISATIONS and ELIGIBILITY CHECKS and WAITLIST
ENTRIES are operational/administrative records in their own tables
(app.models) — the same separation as lab QC runs and blood units.

ELIGIBILITY — honesty rule: there is NO live payer network in this
system (no EDI 270/271 connection). An eligibility check is a
deterministic evaluation of the published payer configuration for one
patient/payer/service/date; the response and the stored record both
carry `basis: "local-config"` and a disclaimer saying exactly that.
It is never presented as a real-time payer response.

AUTHORISATION GATE (consumed by app.orders at signing time): a service
(order type code OR clinical code) flagged in ANY active payer's
`authRequiredServices` may not be signed without an approved
authorisation covering (patient, service) that is in its validity
window and has units remaining — unless an override reason is
supplied, which is recorded on the order (the Phase 5 allergy pattern).
Union semantics across payers are deliberate: with no Coverage
resource in the system there is no patient->payer link to consult, so
the gate fails safe (an authorisation may be demanded that one of the
patient's possible payers would not require — never the reverse).
"""
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir
from .audit import append_audit
from .configstore import (load_workflow_machine, payload_state_set,
                          published_config, role_allowed,
                          transition_role_allowed, workflow_payload)
from .models import (ConfigStatus, EligibilityCheck, FhirResource, PriorAuth,
                     Role, StaffMember, StudioConfig, User, WaitlistEntry)
from .security import get_current_user, get_db, require_capability

router = APIRouter(prefix="/access", tags=["access"])

PAYERS_KEY = "payers"
PAYERS_SYSTEM = "urn:opencare:payer"
REFERRAL_SERVICES_KEY = "referral-services"
REFERRAL_SERVICES_SYSTEM = "urn:opencare:referral-service"
SLOT_TYPES_KEY = "slot-types"
SLOT_TYPES_SYSTEM = "urn:opencare:slot-type"
REFERRAL_WORKFLOW_KEY = "referral-flow"
AUTH_WORKFLOW_KEY = "prior-auth-flow"
WAITLIST_WORKFLOW_KEY = "waitlist-flow"

REFERRAL_CATEGORY_SYSTEM = "urn:opencare:referral"
SOURCE_EXTENSION = "urn:opencare:extension/referral-source"
PAYER_EXTENSION = "urn:opencare:extension/payer"
AUTH_EXTENSION = "urn:opencare:extension/prior-auth"
AUTH_OVERRIDE_EXTENSION = "urn:opencare:extension/auth-override"

ELIGIBILITY_BASIS = "local-config"
ELIGIBILITY_DISCLAIMER = (
    "Eligibility determined from the published Studio payer "
    "configuration only (terminology/payers). This system has NO live "
    "payer connection (no EDI 270/271): this is a local configuration "
    "check, not a real-time payer response.")

# Fallback copies of the three lifecycles — the live machines are the
# published workflow configs (seeded v1 below with exactly these
# transitions); the constants are used only if a config is disabled.
DEFAULT_REFERRAL_TRANSITIONS: dict[str, set[str]] = {
    "received": {"triaged", "cancelled"},
    "triaged": {"authorised", "denied", "cancelled"},
    "authorised": {"scheduled", "cancelled"},
    "denied": set(),
    "scheduled": {"completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}
REFERRAL_QUEUE_STATES = ("received", "triaged")
# Referral state -> FHIR Task status for the linked triage task.
REFERRAL_TASK_STATUSES = {
    "received": "received", "triaged": "in-progress",
    "authorised": "accepted", "denied": "rejected",
    "scheduled": "in-progress", "completed": "completed",
    "cancelled": "cancelled",
}

DEFAULT_AUTH_TRANSITIONS: dict[str, set[str]] = {
    "requested": {"approved", "denied", "cancelled"},
    "approved": {"expired", "cancelled"},
    "denied": set(),
    "expired": set(),
    "cancelled": set(),
}

DEFAULT_WAITLIST_TRANSITIONS: dict[str, set[str]] = {
    "waiting": {"offered", "expired", "cancelled"},
    "offered": {"booked", "declined", "expired", "cancelled"},
    "declined": {"waiting", "cancelled"},
    "booked": set(),
    "expired": set(),
    "cancelled": set(),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Default Studio configuration (seeded published v1 at startup)
# ---------------------------------------------------------------------------

def default_payers_payload() -> dict:
    """Synthetic payers. `eligibility` figures and
    `authRequiredServices` are per-payer DATA: a site changes who
    needs authorisation for what by publishing a new version — the
    seeded lists are empty, so the signing gate is inert by default."""
    def payer(code, display, copay, coinsurance):
        return {"code": code, "display": display, "active": True,
                "system": PAYERS_SYSTEM,
                "eligibility": {"copay": copay,
                                "coinsurancePercent": coinsurance},
                "authRequiredServices": []}

    return {"name": "OpenCare payers (synthetic)",
            "system": PAYERS_SYSTEM,
            "concepts": [
                payer("synthetic-payer", "Synthetic Payer", 25.0, 20),
                payer("synthetic-payer-plus", "Synthetic Payer Plus", 15.0, 10),
            ]}


def default_referral_services_payload() -> dict:
    def svc(code, display):
        return {"code": code, "display": display, "active": True,
                "system": REFERRAL_SERVICES_SYSTEM}

    return {"name": "OpenCare referral services",
            "system": REFERRAL_SERVICES_SYSTEM,
            "concepts": [
                svc("cardiology", "Cardiology referral"),
                svc("ophthalmology", "Ophthalmology referral"),
                svc("orthopaedics", "Orthopaedics referral"),
                svc("gastroenterology", "Gastroenterology referral"),
                svc("rehabilitation", "Rehabilitation referral"),
                svc("psychiatry", "Psychiatry referral"),
                svc("neurology", "Neurology referral"),
                svc("dermatology", "Dermatology referral"),
            ]}


def default_slot_types_payload() -> dict:
    def slot(code, display, minutes):
        return {"code": code, "display": display, "active": True,
                "system": SLOT_TYPES_SYSTEM, "durationMinutes": minutes}

    return {"name": "OpenCare appointment slot types",
            "system": SLOT_TYPES_SYSTEM,
            "concepts": [
                slot("new-patient", "New patient visit", 60),
                slot("follow-up", "Follow-up visit", 30),
                slot("procedure", "Procedure slot", 45),
            ]}


def _workflow_payload(transitions: dict[str, set[str]],
                      restricted: dict[tuple[str, str], list[str]] | None = None,
                      **extra) -> dict:
    states = sorted(transitions.keys())
    entries = []
    for src in sorted(transitions):
        for dst in sorted(transitions[src]):
            entry = {"from": src, "to": dst}
            if restricted and (src, dst) in restricted:
                entry["roles"] = restricted[(src, dst)]
            entries.append(entry)
    return {"states": states, "transitions": entries, **extra}


def default_referral_workflow_payload() -> dict:
    # Authorising / denying a referral is a clinical decision (the
    # Phase 5 order-signing pattern): the configurator — a systems
    # role — may work the queue but not decide it.
    restricted = {("triaged", "authorised"): ["admin", "clinician"],
                  ("triaged", "denied"): ["admin", "clinician"]}
    return _workflow_payload(DEFAULT_REFERRAL_TRANSITIONS, restricted,
                             queueStates=list(REFERRAL_QUEUE_STATES))


def default_auth_workflow_payload() -> dict:
    restricted = {("requested", "approved"): ["admin", "clinician"],
                  ("requested", "denied"): ["admin", "clinician"]}
    return _workflow_payload(DEFAULT_AUTH_TRANSITIONS, restricted)


def default_waitlist_workflow_payload() -> dict:
    return _workflow_payload(DEFAULT_WAITLIST_TRANSITIONS)


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


def ensure_access_defaults(session: Session) -> None:
    """Seed payers, referral services, slot types and the three
    access lifecycles as published Studio config v1 (idempotent)."""
    _seed_published(session, "terminology", PAYERS_KEY, default_payers_payload())
    _seed_published(session, "terminology", REFERRAL_SERVICES_KEY,
                    default_referral_services_payload())
    _seed_published(session, "terminology", SLOT_TYPES_KEY,
                    default_slot_types_payload())
    _seed_published(session, "workflow", REFERRAL_WORKFLOW_KEY,
                    default_referral_workflow_payload())
    _seed_published(session, "workflow", AUTH_WORKFLOW_KEY,
                    default_auth_workflow_payload())
    _seed_published(session, "workflow", WAITLIST_WORKFLOW_KEY,
                    default_waitlist_workflow_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Config accessors
# ---------------------------------------------------------------------------

def _concepts(session: Session, key: str) -> list[dict]:
    cfg = published_config(session, "terminology", key)
    if cfg is None:
        return []
    return [c for c in (cfg.payload or {}).get("concepts") or []
            if isinstance(c, dict)]


def _concept(session: Session, key: str, code: str) -> dict | None:
    for concept in _concepts(session, key):
        if concept.get("code") == code:
            return concept
    return None


def payer_concept(session: Session, payer_code: str) -> dict | None:
    return _concept(session, PAYERS_KEY, payer_code)


def auth_required_services(session: Session) -> set[str]:
    """Service codes (order type codes or clinical codes) that require
    prior authorisation, per the published payer configuration: the
    union over ACTIVE payers (see the module docstring for why)."""
    required: set[str] = set()
    for concept in _concepts(session, PAYERS_KEY):
        if concept.get("active") is False:
            continue
        for code in concept.get("authRequiredServices") or []:
            if isinstance(code, str) and code:
                required.add(code)
    return required


def find_covering_auth(session: Session, patient_ref: str,
                       service_codes: set[str],
                       on_date: str | None = None) -> PriorAuth | None:
    """An APPROVED authorisation covering one of `service_codes` for
    this patient on `on_date` (default today): inside its validity
    window (open-ended when a bound is absent) and with units
    remaining (unlimited when approved_units is null)."""
    day = on_date or _today()
    rows = session.scalars(
        select(PriorAuth)
        .where(PriorAuth.patient_ref == patient_ref,
               PriorAuth.status == "approved",
               PriorAuth.service_code.in_(sorted(service_codes)))
        .order_by(PriorAuth.created_at.asc(), PriorAuth.id.asc())).all()
    for row in rows:
        if row.valid_from and day < row.valid_from:
            continue
        if row.valid_to and day > row.valid_to:
            continue
        if row.approved_units is not None and row.used_units >= row.approved_units:
            continue
        return row
    return None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


def _patient_ref(session: Session, payload: dict) -> str:
    subject = payload.get("subject") or {}
    patient_ref = subject.get("reference") if isinstance(subject, dict) else None
    if not patient_ref and payload.get("patient"):
        patient_ref = fhir._normalize_patient_ref(str(payload["patient"]))
    if not patient_ref or not patient_ref.startswith("Patient/"):
        raise HTTPException(
            status_code=422,
            detail="Requires 'subject.reference' (or 'patient') naming a Patient")
    fhir._get_row("Patient", patient_ref.split("/", 1)[1], session)  # 404 if unknown
    return patient_ref


def _check_date(value, field: str) -> str:
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"'{field}' must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be an ISO date (YYYY-MM-DD)")
    return parsed.isoformat()


def _transition(session: Session, machine_key: str, defaults, current: str,
                target, label: str, role_value: str) -> None:
    """Validate one workflow transition (uniform error order with the
    other machines: unknown state 422 -> illegal 409 -> role 403)."""
    if not target:
        raise HTTPException(status_code=422, detail="Transition requires 'to_status'")
    transitions, known_states, roles = load_workflow_machine(
        session, machine_key, defaults)
    if target not in known_states:
        raise HTTPException(status_code=422, detail=f"Unknown {label} status: {target}")
    if target not in transitions.get(current, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Illegal {label} transition '{current}' -> '{target}'")
    if not transition_role_allowed(roles, current, target, role_value):
        raise HTTPException(
            status_code=403,
            detail=(f"Role {role_value} may not perform the {label} "
                    f"transition '{current}' -> '{target}': the published "
                    "workflow config restricts it to "
                    f"{', '.join(roles.get((current, target), []))}"))


def _extension(body: dict, url: str) -> dict | None:
    for ext in (body or {}).get("extension") or []:
        if isinstance(ext, dict) and ext.get("url") == url:
            return ext
    return None


# ---------------------------------------------------------------------------
# Referrals (FHIR ServiceRequest + triage Task)
# ---------------------------------------------------------------------------

def _referral_row(session: Session, referral_id: str) -> FhirResource:
    row = fhir._get_row("ServiceRequest", referral_id, session)
    body = row.body or {}
    categories = body.get("category") or []
    is_referral = any(
        isinstance(coding, dict) and coding.get("system") == REFERRAL_CATEGORY_SYSTEM
        for category in categories if isinstance(category, dict)
        for coding in (category.get("coding") or []))
    if not is_referral:
        raise HTTPException(status_code=404,
                            detail=f"Referral {referral_id} not found")
    return row


def _referral_task(session: Session, referral_id: str) -> FhirResource | None:
    focus = f"ServiceRequest/{referral_id}"
    for task in session.scalars(
            select(FhirResource).where(FhirResource.resource_type == "Task")
            .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all():
        if ((task.body or {}).get("focus") or {}).get("reference") == focus:
            return task
    return None


def _decorate_referral(session: Session, row: FhirResource) -> dict:
    out = fhir._render(row)
    body = row.body or {}
    task = _referral_task(session, row.resource_id)
    out["taskId"] = task.resource_id if task else None
    source = _extension(body, SOURCE_EXTENSION)
    out["referringOrganization"] = source.get("valueString") if source else None
    payer = _extension(body, PAYER_EXTENSION)
    out["payer"] = payer.get("valueString") if payer else None
    return out


@router.post("/referrals", status_code=201)
async def create_referral(request: Request, session: Session = Depends(get_db),
                          user: User = Depends(require_capability("access.work"))):
    """Inbound referral intake: an external-style payload (referring
    organisation/provider, service, reason) becomes a ServiceRequest
    in `received` plus its triage Task. The service must exist in the
    published referral-services terminology (422 otherwise)."""
    payload = await _json_body(request)
    patient_ref = _patient_ref(session, payload)
    service_code = payload.get("service_code") or payload.get("serviceCode")
    if not isinstance(service_code, str) or not service_code:
        raise HTTPException(status_code=422,
                            detail="Referral requires 'service_code'")
    concept = _concept(session, REFERRAL_SERVICES_KEY, service_code)
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown referral service '{service_code}': not in the "
                   "published referral-services terminology")
    if concept.get("active") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Referral service '{service_code}' is inactive in the "
                   "published referral-services terminology")
    organisation = payload.get("referring_organization") \
        or payload.get("referringOrganization")
    if not isinstance(organisation, str) or not organisation.strip():
        raise HTTPException(
            status_code=422,
            detail="Referral requires 'referring_organization' (the external "
                   "organisation sending the referral)")
    payer = payload.get("payer")
    if payer is not None and payer_concept(session, str(payer)) is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown payer '{payer}': not in the published payers "
                   "terminology")

    referral_id = str(uuid.uuid4())
    requester_display = payload.get("referring_provider") \
        or payload.get("referringProvider") or organisation
    extensions = [{"url": SOURCE_EXTENSION, "valueString": organisation}]
    if payer:
        extensions.append({"url": PAYER_EXTENSION, "valueString": str(payer)})
    body = {
        "resourceType": "ServiceRequest",
        "id": referral_id,
        "status": "received",
        "intent": "order",
        "category": [{"coding": [{"system": REFERRAL_CATEGORY_SYSTEM,
                                  "code": "referral", "display": "Referral"}]}],
        "code": {"coding": [{"system": REFERRAL_SERVICES_SYSTEM,
                             "code": service_code,
                             "display": concept.get("display")}],
                 "text": concept.get("display")},
        "subject": {"reference": patient_ref},
        "authoredOn": _now_iso(),
        "requester": {"display": str(requester_display)},
        "extension": extensions,
    }
    if isinstance(payload.get("reason"), str) and payload["reason"]:
        body["reasonCode"] = [{"text": payload["reason"]}]
    if isinstance(payload.get("priority"), str) and payload["priority"]:
        body["priority"] = payload["priority"]
    if isinstance(payload.get("note"), str) and payload["note"]:
        body["note"] = [{"text": payload["note"]}]
    rendered = fhir.create_resource("ServiceRequest", body, session, user)

    task_body = {
        "resourceType": "Task",
        "id": str(uuid.uuid4()),
        "status": "received",
        "intent": "order",
        "description": f"Referral triage: {concept.get('display')}",
        "focus": {"reference": f"ServiceRequest/{referral_id}"},
        "for": {"reference": patient_ref},
        "authoredOn": _now_iso(),
        "requester": {"display": str(requester_display)},
    }
    task = fhir.create_resource("Task", task_body, session, user)
    append_audit(session, actor=user.username, action="access.referral.create",
                 entity=f"ServiceRequest/{referral_id}",
                 payload={"service_code": service_code, "patient": patient_ref,
                          "referring_organization": organisation,
                          "task_id": task["id"]})
    session.commit()
    return {"referral": _decorate_referral(session, _referral_row(session, referral_id)),
            "task": task}


@router.get("/referrals")
def list_referrals(status: str | None = Query(default=None),
                   patient: str | None = Query(default=None),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("access.read"))):
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "ServiceRequest")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    wanted = {s.strip() for s in status.split(",")} if status else None
    out = []
    for row in rows:
        try:
            _referral_row(session, row.resource_id)
        except HTTPException:
            continue
        if wanted and (row.body or {}).get("status") not in wanted:
            continue
        if patient and row.patient_ref != fhir._normalize_patient_ref(patient):
            continue
        out.append(_decorate_referral(session, row))
    return {"items": out, "total": len(out)}


@router.get("/referrals/queue")
def referral_queue(session: Session = Depends(get_db),
                   user: User = Depends(require_capability("access.read"))):
    """The triage queue: referrals in the queue states named by the
    published referral-flow config (`queueStates`; fallback
    received + triaged), oldest first."""
    payload = workflow_payload(session, REFERRAL_WORKFLOW_KEY) or {}
    queue_states = payload_state_set(payload, "queueStates", REFERRAL_QUEUE_STATES)
    return list_referrals(status=",".join(sorted(queue_states)), patient=None,
                          session=session, user=user)


@router.get("/referrals/{referral_id}")
def get_referral(referral_id: str, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("access.read"))):
    row = _referral_row(session, referral_id)
    task = _referral_task(session, referral_id)
    auths = session.scalars(
        select(PriorAuth).where(PriorAuth.referral_id == referral_id)
        .order_by(PriorAuth.created_at.asc(), PriorAuth.id.asc())).all()
    return {"referral": _decorate_referral(session, row),
            "task": fhir._render(task) if task else None,
            "authorisations": [_render_auth(a) for a in auths]}


@router.post("/referrals/{referral_id}/transition")
async def transition_referral(referral_id: str, request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(require_capability("access.work"))):
    """Advance a referral through the published referral-flow config;
    the linked triage Task's status follows the referral (mapping in
    REFERRAL_TASK_STATUSES; an unmapped state leaves the task as-is)."""
    payload = await _json_body(request)
    target = payload.get("to_status") or payload.get("status")
    row = _referral_row(session, referral_id)
    body = dict(row.body or {})
    current = body.get("status")
    _transition(session, REFERRAL_WORKFLOW_KEY, DEFAULT_REFERRAL_TRANSITIONS,
                current, target, "referral", user.role.value)
    body["status"] = target
    row.body = body
    row.status = target
    row.version += 1
    session.flush()
    task_id = None
    task = _referral_task(session, referral_id)
    if task is not None and target in REFERRAL_TASK_STATUSES:
        task_body = dict(task.body or {})
        task_body["status"] = REFERRAL_TASK_STATUSES[target]
        task.body = task_body
        task.status = task_body["status"]
        task.version += 1
        session.flush()
        task_id = task.resource_id
    append_audit(session, actor=user.username,
                 action="access.referral.transition",
                 entity=f"ServiceRequest/{referral_id}",
                 payload={"from_status": current, "to_status": target,
                          "task_id": task_id,
                          "note": payload.get("note")
                          if isinstance(payload.get("note"), str) else None})
    session.commit()
    return _decorate_referral(session, row)


# ---------------------------------------------------------------------------
# Prior authorisations (table records, linked to referrals / orders)
# ---------------------------------------------------------------------------

def _render_auth(row: PriorAuth) -> dict:
    return {"id": row.auth_id, "auth_number": row.auth_number,
            "patient": row.patient_ref, "payer": row.payer_code,
            "service_code": row.service_code, "referral_id": row.referral_id,
            "status": row.status, "approved_units": row.approved_units,
            "used_units": row.used_units, "valid_from": row.valid_from,
            "valid_to": row.valid_to, "note": row.note,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def _auth_row(session: Session, auth_id: str) -> PriorAuth:
    row = session.scalars(
        select(PriorAuth).where(PriorAuth.auth_id == auth_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Authorisation {auth_id} not found")
    return row


@router.post("/authorisations", status_code=201)
async def create_authorisation(request: Request, session: Session = Depends(get_db),
                               user: User = Depends(require_capability("access.work"))):
    """Open a prior-authorisation request: patient + payer + service,
    optionally linked to a referral. The payer must exist in the
    published payers terminology (422 otherwise). Decision data
    (approved units, validity window) may be supplied now or when the
    authorisation is approved."""
    payload = await _json_body(request)
    patient_ref = _patient_ref(session, payload)
    payer = payload.get("payer")
    if not isinstance(payer, str) or not payer:
        raise HTTPException(status_code=422, detail="Authorisation requires 'payer'")
    if payer_concept(session, payer) is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown payer '{payer}': not in the published payers terminology")
    service_code = payload.get("service_code") or payload.get("serviceCode")
    if not isinstance(service_code, str) or not service_code:
        raise HTTPException(status_code=422,
                            detail="Authorisation requires 'service_code'")
    referral_id = payload.get("referral_id") or payload.get("referralId")
    if referral_id:
        _referral_row(session, str(referral_id))  # 404 unless a real referral
    units = payload.get("approved_units", payload.get("approvedUnits"))
    if units is not None and (isinstance(units, bool)
                              or not isinstance(units, int) or units <= 0):
        raise HTTPException(status_code=422,
                            detail="'approved_units' must be a positive integer")
    valid_from = _check_date(payload["valid_from"], "valid_from") \
        if payload.get("valid_from") else None
    valid_to = _check_date(payload["valid_to"], "valid_to") \
        if payload.get("valid_to") else None
    if valid_from and valid_to and valid_to < valid_from:
        raise HTTPException(status_code=422,
                            detail="'valid_to' must not precede 'valid_from'")
    auth_number = payload.get("auth_number") or payload.get("authNumber")
    if auth_number is not None and not isinstance(auth_number, str):
        raise HTTPException(status_code=422, detail="'auth_number' must be a string")
    row = PriorAuth(
        auth_id=str(uuid.uuid4()),
        auth_number=auth_number or f"AUTH-{uuid.uuid4().hex[:8].upper()}",
        patient_ref=patient_ref, payer_code=payer,
        service_code=service_code,
        referral_id=str(referral_id) if referral_id else None,
        status="requested", approved_units=units, used_units=0,
        valid_from=valid_from, valid_to=valid_to,
        note=payload.get("note") if isinstance(payload.get("note"), str) else None,
        created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="access.authorisation.create",
                 entity=f"PriorAuth/{row.auth_id}",
                 payload={"auth_number": row.auth_number, "patient": patient_ref,
                          "payer": payer, "service_code": service_code,
                          "referral_id": row.referral_id})
    session.commit()
    return _render_auth(row)


@router.get("/authorisations")
def list_authorisations(patient: str | None = Query(default=None),
                        status: str | None = Query(default=None),
                        service_code: str | None = Query(default=None),
                        session: Session = Depends(get_db),
                        user: User = Depends(require_capability("access.read"))):
    stmt = select(PriorAuth)
    if patient:
        stmt = stmt.where(PriorAuth.patient_ref == fhir._normalize_patient_ref(patient))
    if status:
        stmt = stmt.where(PriorAuth.status == status)
    if service_code:
        stmt = stmt.where(PriorAuth.service_code == service_code)
    rows = session.scalars(
        stmt.order_by(PriorAuth.created_at.asc(), PriorAuth.id.asc())).all()
    return {"items": [_render_auth(r) for r in rows], "total": len(rows)}


@router.get("/authorisations/{auth_id}")
def get_authorisation(auth_id: str, session: Session = Depends(get_db),
                      user: User = Depends(require_capability("access.read"))):
    return _render_auth(_auth_row(session, auth_id))


@router.post("/authorisations/{auth_id}/transition")
async def transition_authorisation(auth_id: str, request: Request,
                                   session: Session = Depends(get_db),
                                   user: User = Depends(require_capability("access.work"))):
    """Decide / move an authorisation through the published
    prior-auth-flow config. Decision data arriving with the transition
    (auth_number — the payer-issued number is DATA — approved_units,
    valid_from, valid_to, note) is applied to the record."""
    payload = await _json_body(request)
    target = payload.get("to_status") or payload.get("status")
    row = _auth_row(session, auth_id)
    _transition(session, AUTH_WORKFLOW_KEY, DEFAULT_AUTH_TRANSITIONS,
                row.status, target, "authorisation", user.role.value)
    if payload.get("auth_number") or payload.get("authNumber"):
        row.auth_number = str(payload.get("auth_number") or payload.get("authNumber"))
    units = payload.get("approved_units", payload.get("approvedUnits"))
    if units is not None:
        if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
            raise HTTPException(status_code=422,
                                detail="'approved_units' must be a positive integer")
        row.approved_units = units
    if payload.get("valid_from"):
        row.valid_from = _check_date(payload["valid_from"], "valid_from")
    if payload.get("valid_to"):
        row.valid_to = _check_date(payload["valid_to"], "valid_to")
    if row.valid_from and row.valid_to and row.valid_to < row.valid_from:
        raise HTTPException(status_code=422,
                            detail="'valid_to' must not precede 'valid_from'")
    if isinstance(payload.get("note"), str):
        row.note = payload["note"]
    previous = row.status
    row.status = target
    session.flush()
    append_audit(session, actor=user.username,
                 action="access.authorisation.transition",
                 entity=f"PriorAuth/{auth_id}",
                 payload={"from_status": previous, "to_status": target,
                          "auth_number": row.auth_number,
                          "approved_units": row.approved_units})
    session.commit()
    return _render_auth(row)


# ---------------------------------------------------------------------------
# Eligibility (deterministic local-config evaluation, recorded)
# ---------------------------------------------------------------------------

@router.get("/payers")
def list_payers(session: Session = Depends(get_db),
                user: User = Depends(require_capability("access.read"))):
    cfg = published_config(session, "terminology", PAYERS_KEY)
    return {"payers": cfg.payload if cfg else None,
            "version": cfg.version if cfg else None}


@router.post("/eligibility/check", status_code=201)
async def eligibility_check(request: Request, session: Session = Depends(get_db),
                            user: User = Depends(require_capability("access.work"))):
    """Evaluate coverage for one patient + payer (+ optional service +
    service date) against the PUBLISHED payer configuration and RECORD
    the check. Unknown payer -> 422. The result is labelled
    `basis: "local-config"` with the no-live-payer disclaimer — it is
    never a real-time payer response."""
    payload = await _json_body(request)
    patient_ref = _patient_ref(session, payload)
    payer = payload.get("payer")
    if not isinstance(payer, str) or not payer:
        raise HTTPException(status_code=422, detail="Eligibility check requires 'payer'")
    concept = payer_concept(session, payer)
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown payer '{payer}': not in the published payers "
                   "terminology — eligibility cannot be evaluated")
    service_code = payload.get("service_code") or payload.get("serviceCode")
    service_date = _check_date(payload["service_date"], "service_date") \
        if payload.get("service_date") else _today()

    eligibility = concept.get("eligibility") or {}
    copay = eligibility.get("copay")
    coinsurance = eligibility.get("coinsurancePercent")
    covered_services = eligibility.get("coveredServices")
    active = concept.get("active") is not False
    if not active:
        covered, reason = False, "payer-inactive"
    elif isinstance(covered_services, list) and service_code \
            and service_code not in covered_services:
        covered, reason = False, "service-not-covered"
    else:
        covered, reason = True, "covered-by-payer-config"

    result = {
        "patient": patient_ref, "payer": payer,
        "payer_display": concept.get("display"),
        "service_code": service_code, "service_date": service_date,
        "covered": covered, "reason": reason,
        "copay": copay if isinstance(copay, (int, float))
        and not isinstance(copay, bool) else None,
        "coinsurance_percent": coinsurance
        if isinstance(coinsurance, (int, float))
        and not isinstance(coinsurance, bool) else None,
        "basis": ELIGIBILITY_BASIS, "disclaimer": ELIGIBILITY_DISCLAIMER,
        "checked_by": user.username, "checked_at": _now_iso(),
    }
    row = EligibilityCheck(
        check_id=str(uuid.uuid4()), patient_ref=patient_ref, payer_code=payer,
        service_code=service_code, service_date=service_date, covered=covered,
        copay=result["copay"], coinsurance_percent=result["coinsurance_percent"],
        detail=result, checked_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="access.eligibility.check",
                 entity=f"EligibilityCheck/{row.check_id}",
                 payload={"patient": patient_ref, "payer": payer,
                          "service_code": service_code, "covered": covered,
                          "basis": ELIGIBILITY_BASIS})
    session.commit()
    return {"id": row.check_id, **result}


@router.get("/eligibility/checks")
def list_eligibility_checks(patient: str | None = Query(default=None),
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("access.read"))):
    stmt = select(EligibilityCheck)
    if patient:
        stmt = stmt.where(
            EligibilityCheck.patient_ref == fhir._normalize_patient_ref(patient))
    rows = session.scalars(
        stmt.order_by(EligibilityCheck.created_at.asc(),
                      EligibilityCheck.id.asc())).all()
    return {"items": [{"id": r.check_id, **(r.detail or {})} for r in rows],
            "total": len(rows)}


# ---------------------------------------------------------------------------
# Waitlist (slot-type requests; offer on cancellation; accept books)
# ---------------------------------------------------------------------------

def _render_entry(row: WaitlistEntry) -> dict:
    return {"id": row.entry_id, "patient": row.patient_ref,
            "slot_type": row.slot_type, "priority": row.priority,
            "status": row.status, "note": row.note,
            "offered_appointment_id": row.offered_appointment_id,
            "offered_at": row.offered_at.isoformat() if row.offered_at else None,
            "appointment_id": row.appointment_id,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def _entry_row(session: Session, entry_id: str) -> WaitlistEntry:
    row = session.scalars(
        select(WaitlistEntry).where(WaitlistEntry.entry_id == entry_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Waitlist entry {entry_id} not found")
    return row


def _waitlist_user(session: Session = Depends(get_db),
                   user: User = Depends(get_current_user)) -> User:
    """Waitlist gate with the patient carve-out (the app.billing
    pattern): a patient caller may act ONLY on their own entries
    (enforced per-endpoint); staff need the access capabilities —
    access.work to write, access.read to read."""
    if user.role == Role.patient:
        return user
    if role_allowed(session, user.role, "access.work") \
            or role_allowed(session, user.role, "access.read"):
        return user
    raise HTTPException(
        status_code=403,
        detail=f"Role {user.role.value} lacks capability 'access.read'")


def _require_staff_write(session: Session, user: User) -> None:
    if user.role == Role.patient:
        return
    if not role_allowed(session, user.role, "access.work"):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.value} lacks capability 'access.work'")


@router.post("/waitlist", status_code=201)
async def join_waitlist(request: Request, session: Session = Depends(get_db),
                        user: User = Depends(_waitlist_user)):
    """Request a slot of a published slot type. Staff (access.work)
    may queue any patient; a patient caller may only queue
    themselves. The slot type must exist in the published slot-types
    terminology (422 otherwise)."""
    _require_staff_write(session, user)
    payload = await _json_body(request)
    patient_ref = _patient_ref(session, payload)
    if user.role == Role.patient and patient_ref != user.patient_ref:
        raise HTTPException(
            status_code=403,
            detail="Patient callers may only join the waitlist for themselves")
    slot_type = payload.get("slot_type") or payload.get("slotType")
    if not isinstance(slot_type, str) or not slot_type:
        raise HTTPException(status_code=422, detail="Waitlist entry requires 'slot_type'")
    concept = _concept(session, SLOT_TYPES_KEY, slot_type)
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown slot type '{slot_type}': not in the published "
                   "slot-types terminology")
    if concept.get("active") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Slot type '{slot_type}' is inactive in the published "
                   "slot-types terminology")
    priority = payload.get("priority")
    if priority is None:
        default_priority = concept.get("defaultPriority")
        priority = default_priority if isinstance(default_priority, int) \
            and not isinstance(default_priority, bool) else 100
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise HTTPException(status_code=422, detail="'priority' must be an integer")
    row = WaitlistEntry(
        entry_id=str(uuid.uuid4()), patient_ref=patient_ref,
        slot_type=slot_type, priority=priority, status="waiting",
        note=payload.get("note") if isinstance(payload.get("note"), str) else None,
        created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="access.waitlist.join",
                 entity=f"WaitlistEntry/{row.entry_id}",
                 payload={"patient": patient_ref, "slot_type": slot_type,
                          "priority": priority})
    session.commit()
    return _render_entry(row)


@router.get("/waitlist")
def list_waitlist(status: str | None = Query(default=None),
                  patient: str | None = Query(default=None),
                  slot_type: str | None = Query(default=None),
                  session: Session = Depends(get_db),
                  user: User = Depends(_waitlist_user)):
    """The waitlist in queue order (priority, then oldest first). A
    patient caller sees only their own entries."""
    if user.role == Role.patient:
        patient = user.patient_ref
    stmt = select(WaitlistEntry)
    if status:
        stmt = stmt.where(WaitlistEntry.status == status)
    if patient:
        stmt = stmt.where(
            WaitlistEntry.patient_ref == fhir._normalize_patient_ref(patient))
    if slot_type:
        stmt = stmt.where(WaitlistEntry.slot_type == slot_type)
    rows = session.scalars(stmt.order_by(
        WaitlistEntry.priority.asc(), WaitlistEntry.created_at.asc(),
        WaitlistEntry.id.asc())).all()
    return {"items": [_render_entry(r) for r in rows], "total": len(rows)}


@router.post("/waitlist/offers")
async def offer_waitlist_slot(request: Request, session: Session = Depends(get_db),
                              user: User = Depends(require_capability("access.work"))):
    """A slot freed up: name the CANCELLED appointment whose slot
    opened. The earliest waiting entry whose slot type matches the
    appointment's type (appointmentType, else serviceType, coding)
    moves to `offered`. No matching entry is a normal outcome
    ({entry: null}), not an error."""
    payload = await _json_body(request)
    appointment_id = payload.get("appointment_id") or payload.get("appointmentId")
    if not appointment_id:
        raise HTTPException(status_code=422,
                            detail="Offer requires 'appointment_id' (the cancelled "
                                   "appointment whose slot freed up)")
    appt = fhir._get_row("Appointment", str(appointment_id), session)
    appt_body = appt.body or {}
    if appt_body.get("status") != "cancelled":
        raise HTTPException(
            status_code=409,
            detail=f"Appointment {appointment_id} is in status "
                   f"'{appt_body.get('status')}', not 'cancelled'; only a "
                   "cancelled appointment frees a slot to offer")
    slot_code = None
    for field in ("appointmentType", "serviceType"):
        coding = ((appt_body.get(field) or {}).get("coding") or [])
        if coding and isinstance(coding[0], dict) and coding[0].get("code"):
            slot_code = coding[0]["code"]
            break
    if not slot_code:
        raise HTTPException(
            status_code=422,
            detail="Appointment carries no appointmentType/serviceType code "
                   "to match waitlist slot types against")
    rows = session.scalars(
        select(WaitlistEntry)
        .where(WaitlistEntry.status == "waiting",
               WaitlistEntry.slot_type == slot_code)
        .order_by(WaitlistEntry.priority.asc(), WaitlistEntry.created_at.asc(),
                  WaitlistEntry.id.asc())).all()
    if not rows:
        return {"entry": None, "appointment_id": str(appointment_id),
                "slot_type": slot_code,
                "reason": "no waiting entry for this slot type"}
    row = rows[0]
    _transition(session, WAITLIST_WORKFLOW_KEY, DEFAULT_WAITLIST_TRANSITIONS,
                row.status, "offered", "waitlist entry", user.role.value)
    row.status = "offered"
    row.offered_appointment_id = str(appointment_id)
    row.offered_at = datetime.now(timezone.utc)
    session.flush()
    append_audit(session, actor=user.username, action="access.waitlist.offer",
                 entity=f"WaitlistEntry/{row.entry_id}",
                 payload={"appointment_id": str(appointment_id),
                          "slot_type": slot_code, "patient": row.patient_ref})
    session.commit()
    return {"entry": _render_entry(row), "appointment_id": str(appointment_id),
            "slot_type": slot_code}


@router.post("/waitlist/{entry_id}/transition")
async def transition_waitlist(entry_id: str, request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(_waitlist_user)):
    """Move a waitlist entry through the published waitlist-flow
    config (decline / expire / cancel / re-queue). Staff need
    access.work; a patient may only decline or cancel their OWN
    entry. Booking happens through /accept, never a bare transition."""
    payload = await _json_body(request)
    target = payload.get("to_status") or payload.get("status")
    row = _entry_row(session, entry_id)
    if user.role == Role.patient:
        if row.patient_ref != user.patient_ref:
            raise HTTPException(status_code=404,
                                detail=f"Waitlist entry {entry_id} not found")
        if target not in ("declined", "cancelled"):
            raise HTTPException(
                status_code=403,
                detail="Patient callers may only decline or cancel their own "
                       "waitlist entries")
    else:
        _require_staff_write(session, user)
    if target == "booked":
        raise HTTPException(
            status_code=422,
            detail="A waitlist entry is booked via POST "
                   "/access/waitlist/{id}/accept (which creates the "
                   "Appointment), not by a bare transition")
    _transition(session, WAITLIST_WORKFLOW_KEY, DEFAULT_WAITLIST_TRANSITIONS,
                row.status, target, "waitlist entry", user.role.value)
    previous = row.status
    row.status = target
    session.flush()
    append_audit(session, actor=user.username, action="access.waitlist.transition",
                 entity=f"WaitlistEntry/{entry_id}",
                 payload={"from_status": previous, "to_status": target})
    session.commit()
    return _render_entry(row)


@router.post("/waitlist/{entry_id}/accept", status_code=201)
async def accept_waitlist_offer(entry_id: str, request: Request,
                                session: Session = Depends(get_db),
                                user: User = Depends(_waitlist_user)):
    """Accept an offer: creates a real Appointment (status `booked`)
    of the entry's slot type for the patient at the requested start,
    and moves the entry to `booked` linked to it. Staff (access.work)
    may accept for any patient; a patient only their own offer."""
    payload = await _json_body(request)
    row = _entry_row(session, entry_id)
    if user.role == Role.patient:
        if row.patient_ref != user.patient_ref:
            raise HTTPException(status_code=404,
                                detail=f"Waitlist entry {entry_id} not found")
    else:
        _require_staff_write(session, user)
    if row.status != "offered":
        raise HTTPException(
            status_code=409,
            detail=f"Waitlist entry {entry_id} is in status '{row.status}', "
                   "not 'offered'; only an offered entry can be accepted")
    start = payload.get("start")
    if not isinstance(start, str) or not start:
        raise HTTPException(status_code=422,
                            detail="Accept requires 'start' (the appointment "
                                   "start, ISO datetime)")
    concept = _concept(session, SLOT_TYPES_KEY, row.slot_type) or {}
    participants = [{"actor": {"reference": row.patient_ref}, "status": "accepted"}]
    practitioner = payload.get("practitioner")
    if isinstance(practitioner, str) and practitioner:
        ref = practitioner if "/" in practitioner else f"Practitioner/{practitioner}"
        participants.append({"actor": {"reference": ref}, "status": "accepted"})
    # Phase 15 (BD-31): credential-gated scheduling. When the accept
    # names BOTH a practitioner and a required privilege, the booking
    # is gated by app.access_services.evaluate_credential_check: the
    # practitioner id resolves to a staff-directory username, and the
    # booking is refused (409) unless a covering credential is
    # unexpired at the appointment start. No privilege named -> no
    # gate: every pre-Phase-15 caller behaves exactly as before. The
    # check is audited like the standalone checker endpoint.
    privilege = payload.get("privilege")
    if isinstance(practitioner, str) and practitioner \
            and isinstance(privilege, str) and privilege:
        from . import access_services
        prac_id = practitioner.split("/", 1)[-1]
        staff_row = session.scalars(select(StaffMember).where(
            StaffMember.staff_id == prac_id)).first()
        staff_username = staff_row.username if staff_row else prac_id
        try:
            booking_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            booking_at = None
        verdict = access_services.evaluate_credential_check(
            session, staff_username, privilege, at=booking_at)
        append_audit(session, actor=user.username,
                     action="scheduling.credential_check",
                     entity=f"staff:{verdict.get('staff_id') or staff_username}",
                     payload={"staff_username": staff_username,
                              "privilege": privilege,
                              "allowed": verdict["allowed"],
                              "reason": verdict["reason"],
                              "via": "access.waitlist.accept",
                              "credential_id": (verdict.get("deciding_credential")
                                                or {}).get("credential_id")})
        if not verdict["allowed"]:
            session.commit()
            raise HTTPException(
                status_code=409,
                detail=(f"Credential-gated scheduling: {practitioner} may not "
                        f"be booked for '{privilege}': {verdict['detail']}"))
    appt_body = {
        "resourceType": "Appointment",
        "id": str(uuid.uuid4()),
        "status": "booked",
        "appointmentType": {"coding": [{
            "system": SLOT_TYPES_SYSTEM, "code": row.slot_type,
            "display": concept.get("display")}]},
        "start": start,
        "participant": participants,
        "description": f"Booked from waitlist entry {row.entry_id}",
    }
    if isinstance(payload.get("end"), str) and payload["end"]:
        appt_body["end"] = payload["end"]
    appointment = fhir.create_resource("Appointment", appt_body, session, user)
    _transition(session, WAITLIST_WORKFLOW_KEY, DEFAULT_WAITLIST_TRANSITIONS,
                row.status, "booked", "waitlist entry", user.role.value)
    row.status = "booked"
    row.appointment_id = appointment["id"]
    session.flush()
    append_audit(session, actor=user.username, action="access.waitlist.accept",
                 entity=f"WaitlistEntry/{entry_id}",
                 payload={"appointment_id": appointment["id"],
                          "patient": row.patient_ref, "slot_type": row.slot_type})
    session.commit()
    return {"entry": _render_entry(row), "appointment": appointment}
