"""FHIR R4 surface.

Phase 0: Patient, Encounter, Observation. Phase 1 adds Condition, Procedure,
MedicationRequest, DiagnosticReport, Appointment (with scheduling/ADT status
transitions), a results inbox, and patient-portal read scoping. Phase 2 (Lab
LIS, app.lab) adds ServiceRequest, Specimen and Task to the generic surface;
the governed lab workflow itself lives under /lab.

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

Studio customization engine additions: QuestionnaireResponse (validated
against the PUBLISHED Studio form it references), the Appointment ADT state
machine loaded from published workflow config, and write/inbox gates driven
by capabilities (built-in defaults merged with published permissions config).
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import append_audit
from .configstore import (ADT_WORKFLOW_KEY, DEFAULT_ADT_TRANSITIONS,
                          load_workflow_machine, published_config,
                          transition_role_allowed)
from .models import FhirResource, Role, User
from .security import get_db, get_current_user, require_capability, require_roles

router = APIRouter(prefix="/fhir", tags=["fhir"])
portal_router = APIRouter(prefix="/portal", tags=["portal"])

SUPPORTED = ("Patient", "Encounter", "Observation", "Condition", "Procedure",
             "MedicationRequest", "DiagnosticReport", "Appointment",
             "ServiceRequest", "Specimen", "Task",
             # Phase 4A: billing + pharmacy resources live on the generic
             # surface too; the governed workflows are app.billing (/billing)
             # and app.pharmacy (/pharmacy).
             "Claim", "MedicationDispense", "MedicationAdministration",
             # Phase 4B: imaging studies; the governed radiology workflow
             # (study creation, DICOMweb, report verify) is app.boards.
             "ImagingStudy")

# Lab result visibility (Phase 2): results created by the lab module carry
# this meta.tag until they are RELEASED through the lab verification flow.
# Tagged resources are hidden from the results inbox and from every
# patient-role read path (portal, search, by-id) — draft lab results are
# visible only through the /lab staff endpoints. Staff FHIR reads other
# than the inbox are unaffected. See app.lab and contracts/fhir-resources.md.
LAB_VISIBILITY_SYSTEM = "urn:opencare:visibility"
LAB_UNRELEASED_CODE = "lab-unreleased"
LAB_UNRELEASED_TAG = {"system": LAB_VISIBILITY_SYSTEM, "code": LAB_UNRELEASED_CODE}


def has_unreleased_tag(body: dict | None) -> bool:
    if not isinstance(body, dict):
        return False
    tags = (body.get("meta") or {}).get("tag") or []
    return any(isinstance(t, dict) and t.get("system") == LAB_VISIBILITY_SYSTEM
               and t.get("code") == LAB_UNRELEASED_CODE for t in tags)
# Built-in defaults for the fhir.write / fhir.inbox capabilities. The live
# gate is app.security.require_capability, which merges these defaults with
# any published Studio permissions config (kind=permissions).
WRITE_ROLES = (Role.admin, Role.configurator, Role.clinician)
INBOX_ROLES = (Role.admin, Role.configurator, Role.clinician)

# Appointment (ADT) status state machine — FALLBACK. The live machine is
# Studio configuration (kind=workflow, key="appointment-adt"), seeded as
# published v1 at startup with exactly these transitions; this constant is
# used only if that config has been disabled. fulfilled/cancelled/noshow
# are terminal. Anything not listed for the current status is illegal ->
# 409. The map itself lives in ONE place (configstore.DEFAULT_ADT_
# TRANSITIONS, from which the seeded config payload is also built); this
# alias remains for existing importers.
APPOINTMENT_TRANSITIONS = DEFAULT_ADT_TRANSITIONS


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
    elif resource_type == "ServiceRequest":
        _require(bool(body.get("status")), "ServiceRequest requires 'status'")
        _require(bool(body.get("intent")), "ServiceRequest requires 'intent'")
        _require(bool(body.get("code")), "ServiceRequest requires 'code'")
        _require(bool(_subject_ref(body)), "ServiceRequest requires 'subject.reference'")
    elif resource_type == "Specimen":
        _require(bool(body.get("status")), "Specimen requires 'status'")
        _require(bool(_subject_ref(body)), "Specimen requires 'subject.reference'")
    elif resource_type == "Task":
        _require(bool(body.get("status")), "Task requires 'status'")
        _require(bool(body.get("intent")), "Task requires 'intent'")
    elif resource_type == "Claim":
        _require(bool(body.get("status")), "Claim requires 'status'")
        patient = body.get("patient") or {}
        _require(isinstance(patient, dict) and bool(patient.get("reference")),
                 "Claim requires 'patient.reference'")
    elif resource_type == "MedicationDispense":
        _require(bool(body.get("status")), "MedicationDispense requires 'status'")
        _require(bool(body.get("medicationCodeableConcept") or body.get("medicationReference")),
                 "MedicationDispense requires 'medicationCodeableConcept' or 'medicationReference'")
        _require(bool(_subject_ref(body)), "MedicationDispense requires 'subject.reference'")
    elif resource_type == "MedicationAdministration":
        _require(bool(body.get("status")), "MedicationAdministration requires 'status'")
        _require(bool(body.get("medicationCodeableConcept") or body.get("medicationReference")),
                 "MedicationAdministration requires 'medicationCodeableConcept' or 'medicationReference'")
        _require(bool(_subject_ref(body)), "MedicationAdministration requires 'subject.reference'")
    elif resource_type == "ImagingStudy":
        _require(bool(body.get("status")), "ImagingStudy requires 'status'")
        _require(bool(_subject_ref(body)), "ImagingStudy requires 'subject.reference'")
        identifiers = body.get("identifier") or []
        has_uid = any(isinstance(i, dict) and i.get("system") == "urn:dicom:uid"
                      and i.get("value") for i in identifiers)
        _require(has_uid,
                 "ImagingStudy requires an 'identifier' with system 'urn:dicom:uid' "
                 "(the DICOM Study Instance UID)")


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
    if resource_type == "Claim":
        # FHIR Claim names the patient in `patient`, not `subject`.
        patient = body.get("patient") or {}
        ref = patient.get("reference") if isinstance(patient, dict) else None
        return ref if ref and ref.startswith("Patient/") else None
    if resource_type == "Appointment":
        return _participant_ref(body, "Patient/")
    if resource_type == "Task":
        for_ref = body.get("for") or {}
        ref = for_ref.get("reference") if isinstance(for_ref, dict) else None
        return ref if ref and ref.startswith("Patient/") else None
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
    if user.role == Role.patient:
        # Non-disclosure: another patient's resource looks identical to a
        # missing one. Documented in contracts/fhir-resources.md. The same
        # applies to the caller's OWN unreleased lab results (Phase 2): a
        # draft result does not exist as far as the patient is concerned
        # until the lab releases it.
        if row.patient_ref != user.patient_ref or has_unreleased_tag(row.body):
            raise HTTPException(status_code=404,
                                detail=f"{resource_type}/{resource_id} not found")
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
    if user.role == Role.patient:
        rows = [r for r in rows if not has_unreleased_tag(r.body)]
    return _bundle(rows)


def results_bundle(session: Session, patient_ref: str, status: str | None = None) -> dict:
    """Observation + DiagnosticReport for one patient, newest first.

    Unreleased lab drafts (Phase 2 visibility tag) are excluded for EVERY
    caller of this bundle — staff use the /lab verification queue for
    drafts, so an unreleased result can never leak into the clinician
    inbox or the patient portal through this path."""
    stmt = (select(FhirResource)
            .where(FhirResource.resource_type.in_(("Observation", "DiagnosticReport")),
                   FhirResource.patient_ref == patient_ref)
            .order_by(FhirResource.created_at.desc(), FhirResource.id.desc()))
    if status:
        stmt = stmt.where(FhirResource.status == status)
    rows = [r for r in session.scalars(stmt).all() if not has_unreleased_tag(r.body)]
    return _bundle(rows)


# --------------------------------------------------------------------------
# Appointment ADT transition (state machine loaded from Studio config)
# --------------------------------------------------------------------------

def _adt_machine(session: Session):
    """(transitions map, known states, per-transition roles) from the
    published ADT workflow config; falls back to the built-in map when no
    config is published."""
    return load_workflow_machine(session, ADT_WORKFLOW_KEY, APPOINTMENT_TRANSITIONS)


def transition_appointment(resource_id: str, payload: dict, session: Session, user: User) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Transition body must be a JSON object")
    target = payload.get("to_status") or payload.get("status")
    if not target:
        raise HTTPException(status_code=422, detail="Transition requires 'to_status'")
    row = _get_row("Appointment", resource_id, session)
    current = (row.body or {}).get("status")
    transitions, known_states, roles = _adt_machine(session)
    if target not in known_states:
        raise HTTPException(status_code=422, detail=f"Unknown Appointment status: {target}")
    allowed = transitions.get(current, set())
    if target not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"Illegal Appointment transition '{current}' -> '{target}'")
    if not transition_role_allowed(roles, current, target, user.role.value):
        raise HTTPException(
            status_code=403,
            detail=(f"Role {user.role.value} may not perform the Appointment "
                    f"transition '{current}' -> '{target}': the published "
                    "workflow config restricts it to "
                    f"{', '.join(roles.get((current, target), []))}"))
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
                     user: User = Depends(require_capability("fhir.write"))):
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
                                 user: User = Depends(require_capability("fhir.write"))):
    return transition_appointment(resource_id, await request.json(), session, user)


@router.get("/inbox")
def inbox(patient: str = Query(...), status: str | None = Query(default=None),
          session: Session = Depends(get_db),
          user: User = Depends(require_capability("fhir.inbox"))):
    """Results inbox for staff: one patient's results, newest first."""
    return results_bundle(session, _normalize_patient_ref(patient), status=status)


# --------------------------------------------------------------------------
# QuestionnaireResponse — answers to a Studio form (FHIR Questionnaire)
# --------------------------------------------------------------------------

def _validate_questionnaire_response(session: Session, body: dict) -> None:
    """A response is only accepted against a PUBLISHED Studio form, and
    every form item marked required must carry at least one answer."""
    if not isinstance(body, dict) or body.get("resourceType") != "QuestionnaireResponse":
        raise HTTPException(status_code=422,
                            detail="resourceType must be 'QuestionnaireResponse'")
    ref = body.get("questionnaire")
    if not isinstance(ref, str) or not ref.strip():
        raise HTTPException(
            status_code=422,
            detail="QuestionnaireResponse requires 'questionnaire' (the Studio form key)")
    form_key = ref.rstrip("/").rsplit("/", 1)[-1]  # bare key or Questionnaire/<key>
    form = published_config(session, "form", form_key)
    if form is None:
        raise HTTPException(
            status_code=422,
            detail=f"Referenced form '{form_key}' is not published in Studio")
    answers: dict[str, dict] = {}
    for item in body.get("item") or []:
        if isinstance(item, dict) and item.get("linkId"):
            answers[item["linkId"]] = item
    missing = []
    for form_item in (form.payload or {}).get("item") or []:
        if not isinstance(form_item, dict) or not form_item.get("required"):
            continue
        answer = answers.get(form_item.get("linkId"))
        if answer is None or not answer.get("answer"):
            missing.append(form_item.get("linkId"))
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing answers for required form items: {', '.join(missing)}")


@router.post("/QuestionnaireResponse", status_code=201)
async def create_questionnaire_response(request: Request,
                                        session: Session = Depends(get_db),
                                        user: User = Depends(require_capability("fhir.write"))):
    body = await request.json()
    _validate_questionnaire_response(session, body)
    return create_resource("QuestionnaireResponse", body, session, user)


@router.get("/QuestionnaireResponse/{resource_id}")
def read_questionnaire_response(resource_id: str, session: Session = Depends(get_db),
                                user: User = Depends(get_current_user)):
    return get_resource("QuestionnaireResponse", resource_id, session, user)


@router.get("/QuestionnaireResponse")
def search_questionnaire_responses(patient: str | None = Query(default=None),
                                   session: Session = Depends(get_db),
                                   user: User = Depends(get_current_user)):
    return search_resources("QuestionnaireResponse", session, user, patient=patient)


@portal_router.get("/my-results")
def my_results(session: Session = Depends(get_db),
               user: User = Depends(require_roles(Role.patient))):
    """Patient portal: the caller's own results only, newest first."""
    if not user.patient_ref:
        raise HTTPException(status_code=403,
                            detail="This patient account is not linked to a patient record")
    return results_bundle(session, user.patient_ref)
