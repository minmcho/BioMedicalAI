"""FHIR R4 surface.

Phase 0: Patient, Encounter, Observation. Phase 1 adds Condition, Procedure,
MedicationRequest, DiagnosticReport, Appointment (with scheduling/ADT status
transitions), a results inbox, and patient-portal read scoping.

Resources are validated for resourceType + the minimal required fields listed
in contracts/fhir-resources.md (in the spirit of US Core must-support
elements — full US Core profile validation is NOT claimed), stored as JSON
with relational projections (patient_ref, practitioner_ref, status, start_at)
for portable search, and every write is audit-logged. Responses are the stored
FHIR JSON with id + meta.versionId.

Patient read scoping (role=patient): a patient caller can only ever see
resources whose patient_ref equals their own User.patient_ref. By-id reads of
another patient's resource return 404 (non-disclosure: we do not confirm the
resource exists), and a search explicitly naming another patient returns 403.
Staff roles (admin/configurator/clinician/viewer) are unaffected.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import append_audit
from .models import FhirResource, Role, User
from .security import get_db, get_current_user, require_roles

router = APIRouter(prefix="/fhir", tags=["fhir"])
portal_router = APIRouter(prefix="/portal", tags=["portal"])

SUPPORTED = ("Patient", "Encounter", "Observation", "Condition", "Procedure",
             "MedicationRequest", "DiagnosticReport", "Appointment")
WRITE_ROLES = (Role.admin, Role.configurator, Role.clinician)
INBOX_ROLES = (Role.admin, Role.configurator, Role.clinician)

# Appointment (ADT) status state machine. fulfilled/cancelled/noshow are
# terminal. Anything not listed for the current status is illegal -> 409.
APPOINTMENT_TRANSITIONS = {
    "proposed": {"booked", "cancelled"},
    "pending": {"booked", "cancelled"},
    "booked": {"arrived", "cancelled"},
    "arrived": {"fulfilled", "cancelled"},
    "checked-in": {"fulfilled", "cancelled"},
    "fulfilled": set(),
    "cancelled": set(),
    "noshow": set(),
}


# --------------------------------------------------------------------------
# Validation + projections
# --------------------------------------------------------------------------

def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise HTTPException(status_code=422, detail=detail)


def _subject_ref(body: dict):
    subject = body.get("subject") or {}
    return subject.get("reference") if isinstance(subject, dict) else None


def _validate(resource_type: str, body: dict) -> None:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="FHIR resource must be a JSON object")
    if body.get("resourceType") != resource_type:
        raise HTTPException(status_code=422,
                            detail=f"resourceType must be '{resource_type}'")
    if resource_type == "Patient":
        _require(bool(body.get("name") or body.get("identifier")),
                 "Patient requires 'name' or 'identifier'")
    elif resource_type == "Observation":
        _require(bool(body.get("code")), "Observation requires 'code'")
        _require(bool(_subject_ref(body)), "Observation requires 'subject.reference'")
    elif resource_type == "Encounter":
        _require(bool(_subject_ref(body)), "Encounter requires 'subject.reference'")
    elif resource_type == "Condition":
        _require(bool(body.get("code")), "Condition requires 'code'")
        _require(bool(_subject_ref(body)), "Condition requires 'subject.reference'")
    elif resource_type == "Procedure":
        _require(bool(body.get("code")), "Procedure requires 'code'")
        _require(bool(_subject_ref(body)), "Procedure requires 'subject.reference'")
    elif resource_type == "MedicationRequest":
        _require(bool(body.get("medicationCodeableConcept") or body.get("medicationReference")),
                 "MedicationRequest requires 'medicationCodeableConcept' or 'medicationReference'")
        _require(bool(_subject_ref(body)), "MedicationRequest requires 'subject.reference'")
        _require(bool(body.get("intent")), "MedicationRequest requires 'intent'")
    elif resource_type == "DiagnosticReport":
        _require(bool(body.get("code")), "DiagnosticReport requires 'code'")
        _require(bool(body.get("status")), "DiagnosticReport requires 'status'")
    elif resource_type == "Appointment":
        _require(bool(body.get("status")), "Appointment requires 'status'")
        participants = body.get("participant") or []
        has_actor = any(isinstance(p, dict) and isinstance(p.get("actor"), dict)
                        and p["actor"].get("reference") for p in participants)
        _require(has_actor,
                 "Appointment requires at least one 'participant' with an 'actor.reference'")


def _participant_ref(body: dict, prefix: str) -> str | None:
    for participant in body.get("participant") or []:
        if isinstance(participant, dict):
            actor = participant.get("actor") or {}
            ref = actor.get("reference") if isinstance(actor, dict) else None
            if ref and ref.startswith(prefix):
                return ref
    return None


def _patient_ref(resource_type: str, body: dict, resource_id: str) -> str | None:
    if resource_type == "Patient":
        return f"Patient/{resource_id}"
    if resource_type == "Appointment":
        return _participant_ref(body, "Patient/")
    return _subject_ref(body)


def _parse_start(body: dict) -> datetime | None:
    raw = body.get("start")
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _parse_query_dt(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid datetime: {raw}")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _apply_projections(row: FhirResource, resource_type: str, body: dict, resource_id: str) -> None:
    row.patient_ref = _patient_ref(resource_type, body, resource_id)
    row.practitioner_ref = _participant_ref(body, "Practitioner/")
    row.status = body.get("status") if isinstance(body.get("status"), str) else None
    row.start_at = _parse_start(body)


def _render(row: FhirResource) -> dict:
    body = dict(row.body)
    body["id"] = row.resource_id
    body["meta"] = {**(body.get("meta") or {}), "versionId": str(row.version)}
    return body


def _bundle(rows) -> dict:
    return {"resourceType": "Bundle", "type": "searchset", "total": len(rows),
            "entry": [{"resource": _render(r)} for r in rows]}


def _get_row(resource_type: str, resource_id: str, session: Session) -> FhirResource:
    row = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == resource_type,
                                   FhirResource.resource_id == resource_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"{resource_type}/{resource_id} not found")
    return row


def _normalize_patient_ref(patient: str) -> str:
    return patient if "/" in patient else f"Patient/{patient}"


# --------------------------------------------------------------------------
# Core create / read / search (shared by all resource routes)
# --------------------------------------------------------------------------

def create_resource(resource_type: str, request_body: dict, session: Session, user: User) -> dict:
    _validate(resource_type, request_body)
    resource_id = str(request_body.get("id") or uuid.uuid4())
    existing = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == resource_type,
                                   FhirResource.resource_id == resource_id)).first()
    if existing is not None:
        existing.version += 1
        existing.body = request_body
        _apply_projections(existing, resource_type, request_body, resource_id)
        row = existing
    else:
        row = FhirResource(resource_type=resource_type, resource_id=resource_id, version=1,
                           body=request_body)
        _apply_projections(row, resource_type, request_body, resource_id)
        session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="fhir.create",
                 entity=f"{resource_type}/{resource_id}",
                 payload={"resource_type": resource_type, "version": row.version})
    session.commit()
    return _render(row)


def get_resource(resource_type: str, resource_id: str, session: Session, user: User) -> dict:
    row = _get_row(resource_type, resource_id, session)
    if user.role == Role.patient and row.patient_ref != user.patient_ref:
        # Non-disclosure: another patient's resource looks identical to a
        # missing one. Documented in contracts/fhir-resources.md.
        raise HTTPException(status_code=404, detail=f"{resource_type}/{resource_id} not found")
    return _render(row)


def search_resources(resource_type: str, session: Session, user: User,
                     patient: str | None = None, practitioner: str | None = None,
                     start: str | None = None, end: str | None = None) -> dict:
    stmt = select(FhirResource).where(FhirResource.resource_type == resource_type)
    if user.role == Role.patient:
        if patient and _normalize_patient_ref(patient) != user.patient_ref:
            raise HTTPException(status_code=403,
                                detail="Patient callers may only search their own record")
        patient = user.patient_ref
    if patient:
        stmt = stmt.where(FhirResource.patient_ref == _normalize_patient_ref(patient))
    if practitioner:
        ref = practitioner if "/" in practitioner else f"Practitioner/{practitioner}"
        stmt = stmt.where(FhirResource.practitioner_ref == ref)
    if start:
        stmt = stmt.where(FhirResource.start_at >= _parse_query_dt(start))
    if end:
        stmt = stmt.where(FhirResource.start_at <= _parse_query_dt(end))
    rows = session.scalars(stmt).all()
    return _bundle(rows)


def results_bundle(session: Session, patient_ref: str, status: str | None = None) -> dict:
    """Observation + DiagnosticReport for one patient, newest first."""
    stmt = (select(FhirResource)
            .where(FhirResource.resource_type.in_(("Observation", "DiagnosticReport")),
                   FhirResource.patient_ref == patient_ref)
            .order_by(FhirResource.created_at.desc(), FhirResource.id.desc()))
    if status:
        stmt = stmt.where(FhirResource.status == status)
    return _bundle(session.scalars(stmt).all())


# --------------------------------------------------------------------------
# Appointment ADT transition
# --------------------------------------------------------------------------

def transition_appointment(resource_id: str, payload: dict, session: Session, user: User) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Transition body must be a JSON object")
    target = payload.get("to_status") or payload.get("status")
    if not target:
        raise HTTPException(status_code=422, detail="Transition requires 'to_status'")
    row = _get_row("Appointment", resource_id, session)
    current = (row.body or {}).get("status")
    if target not in APPOINTMENT_TRANSITIONS:
        raise HTTPException(status_code=422, detail=f"Unknown Appointment status: {target}")
    allowed = APPOINTMENT_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"Illegal Appointment transition '{current}' -> '{target}'")
    body = dict(row.body)
    body["status"] = target
    row.body = body
    row.version += 1
    row.status = target
    session.flush()
    append_audit(session, actor=user.username, action="fhir.appointment.transition",
                 entity=f"Appointment/{resource_id}",
                 payload={"from_status": current, "to_status": target, "version": row.version})
    session.commit()
    return _render(row)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def _make_routes(resource_type: str):
    async def create(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(require_roles(*WRITE_ROLES))):
        return create_resource(resource_type, await request.json(), session, user)

    def read(resource_id: str, session: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
        return get_resource(resource_type, resource_id, session, user)

    def search(patient: str | None = Query(default=None),
               practitioner: str | None = Query(default=None),
               start: str | None = Query(default=None),
               end: str | None = Query(default=None),
               session: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
        return search_resources(resource_type, session, user, patient=patient,
                                practitioner=practitioner, start=start, end=end)

    router.add_api_route(f"/{resource_type}", create, methods=["POST"], status_code=201)
    router.add_api_route(f"/{resource_type}/{{resource_id}}", read, methods=["GET"])
    router.add_api_route(f"/{resource_type}", search, methods=["GET"])


for _t in SUPPORTED:
    _make_routes(_t)


@router.post("/Appointment/{resource_id}/transition")
async def appointment_transition(resource_id: str, request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_roles(*WRITE_ROLES))):
    return transition_appointment(resource_id, await request.json(), session, user)


@router.get("/inbox")
def inbox(patient: str = Query(...), status: str | None = Query(default=None),
          session: Session = Depends(get_db),
          user: User = Depends(require_roles(*INBOX_ROLES))):
    """Results inbox for staff: one patient's results, newest first."""
    return results_bundle(session, _normalize_patient_ref(patient), status=status)


@portal_router.get("/my-results")
def my_results(session: Session = Depends(get_db),
               user: User = Depends(require_roles(Role.patient))):
    """Patient portal: the caller's own results only, newest first."""
    if not user.patient_ref:
        raise HTTPException(status_code=403,
                            detail="This patient account is not linked to a patient record")
    return results_bundle(session, user.patient_ref)
