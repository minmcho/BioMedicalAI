"""Phase 4B — Specialty boards: emergency department, radiology, surgery.

Three boards in one module, all built ON the Studio customization engine
like every other OpenCare module — everything site-specific is published
Studio configuration, never hardcoded:

- the ED tracking machine is workflow config, key ``ed-flow``;
- the surgery case machine is workflow config, key ``surgery-flow``;
- imaging modalities are terminology config, key ``imaging-modalities``;
- operating rooms are terminology config, key ``or-rooms``;
- the external DICOM viewer link target is integration config, key
  ``dicom-viewer`` (payload path ``viewer.baseUrl``);

all seeded as published v1 at startup by :func:`ensure_boards_defaults`
(the constants below are only the seed/fallback copies).

ED: an arrival creates a FHIR Encounter with class EMER whose `status`
carries the ED tracking state (the same convention as the lab order
status in app.lab). Triage records the ESI acuity (1-5) as an extension.
The board endpoint computes wait times server-side from the encounter
period start. Admission creates a linked inpatient Encounter
(`partOf` -> the ED encounter); departure stamps the period end. The
`departed`/`admitted` states are governed: they are reached only through
the depart/admit endpoints, never a bare transition.

Radiology: an ImagingStudy (generic FHIR layer) is created from a
ServiceRequest with a modality validated against the published config.
DICOMweb-shaped reads (QIDO-RS style study-level DICOM JSON, PS3.18
attribute tags) are derived from the stored ImagingStudy. The report
flow is DiagnosticReport preliminary -> final, with verification gated
by the `radiology.verify` capability. Studies are METADATA-ONLY in v1:
synthetic studies carry no pixel data, there is no embedded viewer —
the UI links out to an external OHIF-style viewer whose base URL is
Studio integration config.

Surgery: a case is a FHIR Procedure (its `status` carries the case
workflow state, the lab convention) linked to a scheduling Appointment
that carries the room (a Location participant) and the start/end times.
Double-booking a room with overlapping times is refused server-side
(409) on both create and reschedule, comparing the real stored times.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir
from .audit import append_audit
from .configstore import published_config
from .models import ConfigStatus, FhirResource, Role, StudioConfig, User
from .security import get_db, require_capability, require_roles

ed_router = APIRouter(prefix="/ed", tags=["ed"])
radiology_router = APIRouter(prefix="/radiology", tags=["radiology"])
dicomweb_router = APIRouter(prefix="/dicomweb", tags=["dicomweb"])
surgery_router = APIRouter(prefix="/surgery", tags=["surgery"])

STAFF_READ = (Role.admin, Role.configurator, Role.clinician, Role.viewer)

ED_WORKFLOW_KEY = "ed-flow"
SURGERY_WORKFLOW_KEY = "surgery-flow"
MODALITIES_KEY = "imaging-modalities"
OR_ROOMS_KEY = "or-rooms"
VIEWER_KEY = "dicom-viewer"

ACT_CODE_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
DCM_SYSTEM = "http://dicom.nema.org/resources/ontology/DCM"
DICOM_UID_SYSTEM = "urn:dicom:uid"

ACUITY_EXTENSION = "urn:opencare:extension/ed-acuity"
ADMISSION_EXTENSION = "urn:opencare:extension/admission-encounter"
OR_ROOM_EXTENSION = "urn:opencare:extension/or-room"
CASE_APPOINTMENT_EXTENSION = "urn:opencare:extension/scheduled-appointment"
APPOINTMENT_CASE_EXTENSION = "urn:opencare:extension/surgery-case"

# ED states in which the patient is physically in the department (the
# board census). departed/admitted are terminal and leave the board.
ED_ACTIVE_STATES = ("waiting", "triage", "in-treatment", "disposition")
# Terminal ED states are governed: reached only via the admit/depart
# endpoints (which do the associated work), never a bare transition.
ED_GOVERNED_STATES = ("departed", "admitted")

# Fallback copies of the board state machines — the live machines are the
# published workflow configs (seeded v1 below with exactly these
# transitions); these constants are used only if a config is disabled.
DEFAULT_ED_TRANSITIONS: dict[str, set[str]] = {
    "waiting": {"triage"},
    "triage": {"in-treatment"},
    "in-treatment": {"disposition"},
    "disposition": {"departed", "admitted"},
    "departed": set(),
    "admitted": set(),
}
DEFAULT_SURGERY_TRANSITIONS: dict[str, set[str]] = {
    "scheduled": {"pre-op", "cancelled"},
    "pre-op": {"in-op", "cancelled"},
    "in-op": {"pacu"},
    "pacu": {"done"},
    "done": set(),
    "cancelled": set(),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_dt(raw) -> datetime | None:
    """Parse an ISO date/datetime to an aware UTC datetime (naive input
    is read as UTC). Returns None for unparseable input."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


# ---------------------------------------------------------------------------
# Default Studio configuration (seeded published v1 at startup)
# ---------------------------------------------------------------------------

def _workflow_payload(transitions_map: dict[str, set[str]]) -> dict:
    states = sorted(transitions_map.keys())
    transitions = [
        {"from": src, "to": dst}
        for src in sorted(transitions_map)
        for dst in sorted(transitions_map[src])
    ]
    return {"states": states, "transitions": transitions}


def default_modalities_payload() -> dict:
    def modality(code, display):
        return {"code": code, "display": display, "active": True}

    return {"name": "OpenCare imaging modalities (DICOM PS3.16)",
            "system": DCM_SYSTEM,
            "concepts": [
                modality("CT", "Computed Tomography"),
                modality("MR", "Magnetic Resonance Imaging"),
                modality("CR", "Computed Radiography"),
                modality("DX", "Digital Radiography"),
                modality("US", "Ultrasound"),
                modality("MG", "Mammography"),
                modality("NM", "Nuclear Medicine"),
            ]}


def default_or_rooms_payload() -> dict:
    return {"name": "OpenCare operating rooms",
            "concepts": [
                {"code": f"OR-{n}", "display": f"Operating Room {n}", "active": True}
                for n in (1, 2, 3, 4)
            ]}


def default_viewer_payload() -> dict:
    # The public OHIF demo viewer is the seeded default link target so the
    # link works out of the box; a deployment points viewer.baseUrl at
    # its own OHIF (or compatible) instance in Studio. The viewer itself
    # is external software — OpenCare only constructs the link.
    return {"viewer": {"name": "OHIF viewer (public demo)",
                       "baseUrl": "https://viewer.ohif.org"}}


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


def ensure_boards_defaults(session: Session) -> None:
    """Seed the Phase 4B Studio config as published v1 (idempotent),
    exactly like the lab/billing/pophealth config seeds."""
    _seed_published(session, "workflow", ED_WORKFLOW_KEY,
                    _workflow_payload(DEFAULT_ED_TRANSITIONS))
    _seed_published(session, "workflow", SURGERY_WORKFLOW_KEY,
                    _workflow_payload(DEFAULT_SURGERY_TRANSITIONS))
    _seed_published(session, "terminology", MODALITIES_KEY,
                    default_modalities_payload())
    _seed_published(session, "terminology", OR_ROOMS_KEY,
                    default_or_rooms_payload())
    _seed_published(session, "integration", VIEWER_KEY,
                    default_viewer_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Config accessors + state machines
# ---------------------------------------------------------------------------

def _machine(session: Session, key: str,
             defaults: dict[str, set[str]]) -> tuple[dict[str, set[str]], set[str]]:
    """(transitions, known states) from a published workflow config;
    falls back to the built-in map when no config is published."""
    cfg = published_config(session, "workflow", key)
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
    return defaults, set(defaults.keys())


def _concepts(session: Session, key: str, label: str) -> list[dict]:
    cfg = published_config(session, "terminology", key)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=(f"No published {label} (terminology/{key}); publish one "
                    "in Studio before using this board"))
    return [c for c in (cfg.payload or {}).get("concepts") or []
            if isinstance(c, dict)]


def _concept(session: Session, key: str, label: str, code: str) -> dict | None:
    for concept in _concepts(session, key, label):
        if concept.get("code") == code:
            return concept
    return None


def _viewer_url(session: Session, study_uid: str) -> str | None:
    """OHIF-style external viewer link for a study, from the published
    integration config (viewer.baseUrl). None when no base URL is
    configured — the UI then says so instead of inventing a link."""
    cfg = published_config(session, "integration", VIEWER_KEY)
    if cfg is None:
        return None
    viewer = (cfg.payload or {}).get("viewer") or {}
    base = viewer.get("baseUrl") if isinstance(viewer, dict) else None
    if not isinstance(base, str) or not base.strip():
        return None
    return f"{base.rstrip('/')}/viewer?StudyInstanceUIDs={study_uid}"


# ---------------------------------------------------------------------------
# Small body helpers
# ---------------------------------------------------------------------------

def _get_extension(body: dict, url: str) -> dict | None:
    for ext in (body or {}).get("extension") or []:
        if isinstance(ext, dict) and ext.get("url") == url:
            return ext
    return None


def _set_extension(body: dict, url: str, **values) -> None:
    extensions = [e for e in (body.get("extension") or [])
                  if not (isinstance(e, dict) and e.get("url") == url)]
    extensions.append({"url": url, **values})
    body["extension"] = extensions


def _set_status(row: FhirResource, status: str) -> None:
    body = dict(row.body or {})
    body["status"] = status
    row.body = body
    row.status = status
    row.version += 1


def _class_code(body: dict) -> str | None:
    """Encounter class code, tolerating the single-Coding shape this
    codebase stores and the FHIR list-of-Coding shape."""
    cls = (body or {}).get("class")
    if isinstance(cls, dict):
        return cls.get("code")
    if isinstance(cls, list):
        for coding in cls:
            if isinstance(coding, dict) and coding.get("code"):
                return coding["code"]
    return None


def _subject_ref(body: dict) -> str | None:
    subject = (body or {}).get("subject") or {}
    return subject.get("reference") if isinstance(subject, dict) else None


def _first_coding(body: dict, field: str = "code") -> tuple[str | None, str | None, str | None]:
    concept = (body or {}).get(field) or {}
    coding = concept.get("coding") or []
    if coding and isinstance(coding[0], dict):
        return coding[0].get("system"), coding[0].get("code"), coding[0].get("display")
    return None, None, concept.get("text") if isinstance(concept, dict) else None


# ===========================================================================
# ED board
# ===========================================================================

def _ed_row(session: Session, encounter_id: str) -> FhirResource:
    row = fhir._get_row("Encounter", encounter_id, session)
    if _class_code(row.body or {}) != "EMER":
        raise HTTPException(
            status_code=422,
            detail=f"Encounter/{encounter_id} is not an emergency (EMER) encounter")
    return row


def _acuity(body: dict) -> int | None:
    ext = _get_extension(body, ACUITY_EXTENSION)
    if ext is not None and isinstance(ext.get("valueInteger"), int):
        return ext["valueInteger"]
    return None


def _chief_complaint(body: dict) -> str | None:
    for reason in (body or {}).get("reasonCode") or []:
        if isinstance(reason, dict):
            if reason.get("text"):
                return reason["text"]
            _, _, display = _first_coding({"code": reason})
            if display:
                return display
    return None


def _arrival_dt(row: FhirResource) -> datetime:
    start = _parse_dt(((row.body or {}).get("period") or {}).get("start"))
    if start is not None:
        return start
    created = row.created_at or _now()
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created


def _wait_minutes(row: FhirResource, now: datetime) -> int:
    return max(0, int((now - _arrival_dt(row)).total_seconds() // 60))


def _board_entry(row: FhirResource, now: datetime) -> dict:
    body = row.body or {}
    return {
        "encounter": fhir._render(row),
        "patient_ref": _subject_ref(body),
        "arrived_at": _arrival_dt(row).isoformat(),
        "wait_minutes": _wait_minutes(row, now),
        "acuity": _acuity(body),
        "chief_complaint": _chief_complaint(body),
        "state": body.get("status"),
    }


@ed_router.post("/arrivals", status_code=201)
async def ed_arrival(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("ed.work"))):
    """Register an ED arrival: an Encounter with class EMER in the
    `waiting` tracking state. `arrived_at` may be supplied (registration
    systems record the real arrival time); it defaults to now and drives
    the board's wait-time computation."""
    payload = await _json_body(request)
    subject_ref = None
    if isinstance(payload.get("subject"), dict):
        subject_ref = payload["subject"].get("reference")
    if not subject_ref and payload.get("patient"):
        subject_ref = fhir._normalize_patient_ref(str(payload["patient"]))
    if not subject_ref or not subject_ref.startswith("Patient/"):
        raise HTTPException(status_code=422,
                            detail="ED arrival requires 'subject.reference' "
                                   "(or 'patient') naming a Patient")
    patient_id = subject_ref.split("/", 1)[1]
    fhir._get_row("Patient", patient_id, session)  # 404 if the patient is unknown

    arrived_raw = payload.get("arrived_at") or payload.get("arrivedAt")
    arrived = _parse_dt(arrived_raw) if arrived_raw else _now()
    if arrived is None:
        raise HTTPException(status_code=422,
                            detail=f"Invalid 'arrived_at' datetime: {arrived_raw}")
    body = {
        "resourceType": "Encounter",
        "status": "waiting",
        "class": {"system": ACT_CODE_SYSTEM, "code": "EMER",
                  "display": "emergency"},
        "subject": {"reference": subject_ref},
        "period": {"start": arrived.isoformat()},
    }
    if payload.get("id"):
        body["id"] = str(payload["id"])
    complaint = payload.get("chief_complaint") or payload.get("reason")
    if isinstance(complaint, str) and complaint.strip():
        body["reasonCode"] = [{"text": complaint}]
    encounter = fhir.create_resource("Encounter", body, session, user)
    append_audit(session, actor=user.username, action="ed.arrival",
                 entity=f"Encounter/{encounter['id']}",
                 payload={"patient": subject_ref,
                          "arrived_at": arrived.isoformat(),
                          "chief_complaint": complaint
                          if isinstance(complaint, str) else None})
    session.commit()
    return encounter


@ed_router.get("/board")
def ed_board(session: Session = Depends(get_db),
             user: User = Depends(require_roles(*STAFF_READ))):
    """Current ED census: every EMER encounter in an active tracking
    state, grouped by state, longest-waiting first, with the wait time
    (minutes since arrival) computed here, server-side."""
    now = _now()
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Encounter")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    groups: dict[str, list[dict]] = {state: [] for state in ED_ACTIVE_STATES}
    for row in rows:
        body = row.body or {}
        if _class_code(body) != "EMER":
            continue
        state = body.get("status")
        if state in groups:
            groups[state].append(_board_entry(row, now))
    for entries in groups.values():
        entries.sort(key=lambda e: e["arrived_at"])
    counts = {state: len(entries) for state, entries in groups.items()}
    return {"generated_at": now.isoformat(),
            "counts": {**counts, "total": sum(counts.values())},
            "groups": groups}


@ed_router.post("/encounters/{encounter_id}/transition")
async def ed_transition(encounter_id: str, request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(require_capability("ed.work"))):
    """Advance an ED encounter through the published ed-flow workflow
    config. Triage (waiting -> triage) requires an ESI `acuity` of 1-5.
    departed/admitted are governed states with dedicated endpoints."""
    payload = await _json_body(request)
    target = payload.get("to_status") or payload.get("status")
    if not target:
        raise HTTPException(status_code=422, detail="Transition requires 'to_status'")
    if target in ED_GOVERNED_STATES:
        raise HTTPException(
            status_code=422,
            detail=(f"Status '{target}' is governed: it is reached through the "
                    "admit/depart endpoints, not a bare transition"))
    row = _ed_row(session, encounter_id)
    body = dict(row.body or {})
    current = body.get("status")
    transitions, known_states = _machine(session, ED_WORKFLOW_KEY,
                                         DEFAULT_ED_TRANSITIONS)
    if target not in known_states:
        raise HTTPException(status_code=422, detail=f"Unknown ED status: {target}")
    if target not in transitions.get(current, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Illegal ED transition '{current}' -> '{target}'")
    acuity = None
    if target == "triage":
        acuity = payload.get("acuity")
        if isinstance(acuity, bool) or not isinstance(acuity, int) \
                or not 1 <= acuity <= 5:
            raise HTTPException(
                status_code=422,
                detail="Triage requires 'acuity': an ESI level, an integer 1-5")
        _set_extension(body, ACUITY_EXTENSION, valueInteger=acuity)
    body["status"] = target
    row.body = body
    row.status = target
    row.version += 1
    session.flush()
    append_audit(session, actor=user.username, action="ed.encounter.transition",
                 entity=f"Encounter/{encounter_id}",
                 payload={"from_status": current, "to_status": target,
                          "acuity": acuity, "version": row.version})
    session.commit()
    return fhir._render(row)


def _ed_finish(session: Session, user: User, row: FhirResource,
               target: str, action: str) -> None:
    """Shared governed-exit check: the published ed-flow machine must
    allow current -> target, else 409."""
    current = (row.body or {}).get("status")
    transitions, _ = _machine(session, ED_WORKFLOW_KEY, DEFAULT_ED_TRANSITIONS)
    if target not in transitions.get(current, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Illegal ED transition '{current}' -> '{target}' "
                   f"({action} is only possible from a state the workflow "
                   "allows to reach it)")


@ed_router.post("/encounters/{encounter_id}/admit")
async def ed_admit(encounter_id: str, request: Request,
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("ed.work"))):
    """Admit from the ED: creates an inpatient Encounter (class IMP,
    status in-progress) linked back via `partOf`, and moves the ED
    encounter to the terminal `admitted` state with its period closed."""
    payload = await _json_body(request)
    row = _ed_row(session, encounter_id)
    _ed_finish(session, user, row, "admitted", "admission")
    body = dict(row.body or {})
    subject_ref = _subject_ref(body)
    inpatient_body = {
        "resourceType": "Encounter",
        "id": str(uuid.uuid4()),
        "status": "in-progress",
        "class": {"system": ACT_CODE_SYSTEM, "code": "IMP",
                  "display": "inpatient encounter"},
        "subject": {"reference": subject_ref},
        "period": {"start": _now_iso()},
        "partOf": {"reference": f"Encounter/{encounter_id}"},
    }
    complaint = _chief_complaint(body)
    if complaint:
        inpatient_body["reasonCode"] = [{"text": complaint}]
    ward = payload.get("ward") or payload.get("location")
    if isinstance(ward, str) and ward.strip():
        inpatient_body["location"] = [
            {"location": {"display": ward}, "status": "active"}]
    inpatient = fhir.create_resource("Encounter", inpatient_body, session, user)

    body["status"] = "admitted"
    period = dict(body.get("period") or {})
    period["end"] = _now_iso()
    body["period"] = period
    _set_extension(body, ADMISSION_EXTENSION,
                   valueReference={"reference": f"Encounter/{inpatient['id']}"})
    row.body = body
    row.status = "admitted"
    row.version += 1
    session.flush()
    append_audit(session, actor=user.username, action="ed.encounter.admit",
                 entity=f"Encounter/{encounter_id}",
                 payload={"inpatient_encounter_id": inpatient["id"],
                          "patient": subject_ref,
                          "ward": ward if isinstance(ward, str) else None})
    session.commit()
    return {"ed_encounter": fhir._render(row), "inpatient_encounter": inpatient}


@ed_router.post("/encounters/{encounter_id}/depart")
async def ed_depart(encounter_id: str, request: Request,
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability("ed.work"))):
    """Discharge/depart from the ED: the encounter moves to the terminal
    `departed` state and its period is closed (the length of stay and
    wait are then fixed historical facts)."""
    payload = await _json_body(request)
    row = _ed_row(session, encounter_id)
    _ed_finish(session, user, row, "departed", "departure")
    body = dict(row.body or {})
    body["status"] = "departed"
    period = dict(body.get("period") or {})
    period["end"] = _now_iso()
    body["period"] = period
    row.body = body
    row.status = "departed"
    row.version += 1
    session.flush()
    note = payload.get("note")
    append_audit(session, actor=user.username, action="ed.encounter.depart",
                 entity=f"Encounter/{encounter_id}",
                 payload={"patient": _subject_ref(body),
                          "note": note if isinstance(note, str) else None,
                          "version": row.version})
    session.commit()
    return {"ed_encounter": fhir._render(row)}


# ===========================================================================
# Radiology: ImagingStudy creation, DICOMweb reads, report flow
# ===========================================================================

def _study_uid(body: dict) -> str | None:
    for identifier in (body or {}).get("identifier") or []:
        if isinstance(identifier, dict) \
                and identifier.get("system") == DICOM_UID_SYSTEM \
                and identifier.get("value"):
            return str(identifier["value"])
    return None


def _new_study_uid() -> str:
    # DICOM UID under the 2.25 (UUID) root: "2.25." + the UUID as a
    # decimal integer — 44 characters, inside the 64-char UID limit.
    return f"2.25.{uuid.uuid4().int}"


def _study_reports(session: Session, study_id: str) -> list[FhirResource]:
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "DiagnosticReport")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    own = []
    for report in rows:
        refs = [(ref or {}).get("reference")
                for ref in (report.body or {}).get("imagingStudy") or []]
        if f"ImagingStudy/{study_id}" in refs:
            own.append(report)
    return own


def _study_item(session: Session, row: FhirResource) -> dict:
    uid = _study_uid(row.body or {})
    reports = _study_reports(session, row.resource_id)
    latest = reports[-1] if reports else None
    return {
        "study": fhir._render(row),
        "study_uid": uid,
        "viewer_url": _viewer_url(session, uid) if uid else None,
        "report": ({"id": latest.resource_id,
                    "status": (latest.body or {}).get("status")}
                   if latest is not None else None),
        # Metadata-only in v1: a synthetic study has no series/instances
        # and no pixel data anywhere in the system.
        "has_images": False,
    }


@radiology_router.get("/modalities")
def radiology_modalities(session: Session = Depends(get_db),
                         user: User = Depends(require_roles(*STAFF_READ))):
    cfg = published_config(session, "terminology", MODALITIES_KEY)
    concepts = _concepts(session, MODALITIES_KEY, "imaging modalities")
    return {"modalities": concepts, "version": cfg.version if cfg else None}


@radiology_router.post("/studies", status_code=201)
async def create_study(request: Request, session: Session = Depends(get_db),
                       user: User = Depends(require_capability("radiology.work"))):
    """Create an ImagingStudy from a ServiceRequest. The modality must
    be an ACTIVE concept in the published imaging-modalities config —
    free-text modalities are refused (422), like lab catalog codes."""
    payload = await _json_body(request)
    sr_id = payload.get("service_request_id") or payload.get("serviceRequestId")
    if not sr_id and isinstance(payload.get("based_on"), dict):
        reference = payload["based_on"].get("reference") or ""
        sr_id = reference.split("/", 1)[1] if "/" in reference else reference
    if not sr_id:
        raise HTTPException(status_code=422,
                            detail="Study creation requires 'service_request_id'")
    service_request = fhir._get_row("ServiceRequest", str(sr_id), session)
    sr_body = service_request.body or {}
    subject_ref = _subject_ref(sr_body)
    if not subject_ref:
        raise HTTPException(
            status_code=422,
            detail=f"ServiceRequest/{sr_id} has no subject patient to image")

    modality = payload.get("modality")
    if not isinstance(modality, str) or not modality.strip():
        raise HTTPException(status_code=422, detail="Study creation requires 'modality'")
    concept = _concept(session, MODALITIES_KEY, "imaging modalities", modality)
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown modality '{modality}': not in the published "
                   "imaging-modalities config")
    if concept.get("active") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Modality '{modality}' ({concept.get('display')}) is "
                   "inactive in the published imaging-modalities config")

    uid = _new_study_uid()
    _, sr_code, sr_display = _first_coding(sr_body)
    description = payload.get("description")
    if not isinstance(description, str) or not description.strip():
        description = sr_display or sr_code or "Imaging study"
    study_body = {
        "resourceType": "ImagingStudy",
        "id": str(uuid.uuid4()),
        "status": "available",
        "identifier": [{"system": DICOM_UID_SYSTEM, "value": uid}],
        "modality": [{"system": DCM_SYSTEM, "code": modality,
                      "display": concept.get("display")}],
        "subject": {"reference": subject_ref},
        "basedOn": [{"reference": f"ServiceRequest/{sr_id}"}],
        "started": _now_iso(),
        "numberOfSeries": 0,
        "numberOfInstances": 0,
        "description": description,
        "note": [{"text": "Metadata-only study (OpenCare v1): no pixel "
                          "data is stored; DICOMweb serves study metadata "
                          "only."}],
    }
    if isinstance(sr_body.get("encounter"), dict) and sr_body["encounter"].get("reference"):
        study_body["encounter"] = {"reference": sr_body["encounter"]["reference"]}
    study = fhir.create_resource("ImagingStudy", study_body, session, user)
    append_audit(session, actor=user.username, action="radiology.study.create",
                 entity=f"ImagingStudy/{study['id']}",
                 payload={"service_request_id": str(sr_id), "modality": modality,
                          "study_uid": uid, "patient": subject_ref})
    session.commit()
    row = fhir._get_row("ImagingStudy", study["id"], session)
    return _study_item(session, row)


@radiology_router.get("/studies")
def list_studies(session: Session = Depends(get_db),
                 user: User = Depends(require_roles(*STAFF_READ))):
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "ImagingStudy")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    items = [_study_item(session, row) for row in rows]
    return {"items": items, "total": len(items)}


@radiology_router.get("/studies/{study_id}")
def study_detail(study_id: str, session: Session = Depends(get_db),
                 user: User = Depends(require_roles(*STAFF_READ))):
    row = fhir._get_row("ImagingStudy", study_id, session)
    item = _study_item(session, row)
    item["reports"] = [fhir._render(r)
                       for r in _study_reports(session, study_id)]
    return item


@radiology_router.post("/studies/{study_id}/report", status_code=201)
async def create_imaging_report(study_id: str, request: Request,
                                session: Session = Depends(get_db),
                                user: User = Depends(require_capability("radiology.work"))):
    """Draft the imaging report for a study: a DiagnosticReport in
    `preliminary` status, linked to the study via `imagingStudy` and to
    the order via `basedOn`. One open (preliminary) report per study —
    verify it before drafting another (409)."""
    payload = await _json_body(request)
    study = fhir._get_row("ImagingStudy", study_id, session)
    study_body = study.body or {}
    conclusion = payload.get("conclusion") or payload.get("findings")
    if not isinstance(conclusion, str) or not conclusion.strip():
        raise HTTPException(
            status_code=422,
            detail="An imaging report requires 'conclusion' (the report text)")
    for existing in _study_reports(session, study_id):
        if (existing.body or {}).get("status") == "preliminary":
            raise HTTPException(
                status_code=409,
                detail=(f"ImagingStudy/{study_id} already has a preliminary "
                        f"report (DiagnosticReport/{existing.resource_id}); "
                        "verify it before drafting another"))
    based_on = study_body.get("basedOn") or []
    code = {"text": "Imaging study report"}
    if based_on and isinstance(based_on[0], dict) \
            and str(based_on[0].get("reference", "")).startswith("ServiceRequest/"):
        sr_id = based_on[0]["reference"].split("/", 1)[1]
        try:
            sr_body = fhir._get_row("ServiceRequest", sr_id, session).body or {}
            sr_code = sr_body.get("code")
            if isinstance(sr_code, dict) and sr_code:
                code = sr_code
        except HTTPException:
            pass
    report_body = {
        "resourceType": "DiagnosticReport",
        "id": str(uuid.uuid4()),
        "status": "preliminary",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
            "code": "RAD", "display": "Radiology"}]}],
        "code": code,
        "subject": {"reference": _subject_ref(study_body)},
        "basedOn": based_on,
        "imagingStudy": [{"reference": f"ImagingStudy/{study_id}"}],
        "effectiveDateTime": _now_iso(),
        "conclusion": conclusion,
    }
    report = fhir.create_resource("DiagnosticReport", report_body, session, user)
    append_audit(session, actor=user.username, action="radiology.report.create",
                 entity=f"DiagnosticReport/{report['id']}",
                 payload={"study_id": study_id,
                          "study_uid": _study_uid(study_body)})
    session.commit()
    return report


@radiology_router.post("/reports/{report_id}/verify")
def verify_imaging_report(report_id: str, session: Session = Depends(get_db),
                          user: User = Depends(require_capability("radiology.verify"))):
    """Radiologist verification: an imaging DiagnosticReport moves
    preliminary -> final. This is a clinical sign-off (radiology.verify:
    admin/clinician by default)."""
    report = fhir._get_row("DiagnosticReport", report_id, session)
    body = dict(report.body or {})
    if not body.get("imagingStudy"):
        raise HTTPException(
            status_code=422,
            detail=f"DiagnosticReport/{report_id} is not an imaging report "
                   "(no imagingStudy link)")
    if body.get("status") != "preliminary":
        raise HTTPException(
            status_code=409,
            detail=(f"Report {report_id} is not awaiting verification "
                    f"(status '{body.get('status')}')"))
    body["status"] = "final"
    body["issued"] = _now_iso()
    report.body = body
    report.status = "final"
    report.version += 1
    session.flush()
    append_audit(session, actor=user.username, action="radiology.report.verify",
                 entity=f"DiagnosticReport/{report_id}",
                 payload={"verified_by": user.username, "version": report.version})
    session.commit()
    return fhir._render(report)


# ---------------------------------------------------------------------------
# DICOMweb (QIDO-RS shaped): study-level DICOM JSON derived from the
# stored ImagingStudy resources. Metadata only — no WADO pixel data
# exists in v1 and none is implied by these responses.
# ---------------------------------------------------------------------------

def _patient_name_pn(session: Session, patient_ref: str | None) -> tuple[str, str]:
    """(DICOM PN "Family^Given", patient id) for a Patient/* reference."""
    if not patient_ref or not patient_ref.startswith("Patient/"):
        return "", ""
    patient_id = patient_ref.split("/", 1)[1]
    row = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Patient",
                                   FhirResource.resource_id == patient_id)).first()
    if row is None:
        return "", patient_id
    name = ((row.body or {}).get("name") or [{}])[0]
    if not isinstance(name, dict):
        return "", patient_id
    family = name.get("family") or ""
    given = "^".join(str(g) for g in (name.get("given") or []))
    pn = f"{family}^{given}" if (family or given) else (name.get("text") or "")
    return pn, patient_id


def _dicom_study_json(session: Session, row: FhirResource) -> dict:
    """PS3.18 DICOM JSON for the study level: tag -> {vr, Value}."""
    body = row.body or {}
    uid = _study_uid(body) or ""
    pn, patient_id = _patient_name_pn(session, _subject_ref(body))
    started = _parse_dt(body.get("started"))
    modalities = []
    for modality in body.get("modality") or []:
        if isinstance(modality, dict) and modality.get("code"):
            modalities.append(str(modality["code"]))
    study: dict[str, dict] = {
        "0020000D": {"vr": "UI", "Value": [uid]},
        "00100010": {"vr": "PN", "Value": [{"Alphabetic": pn}]},
        "00100020": {"vr": "LO", "Value": [patient_id]},
        "00080020": {"vr": "DA",
                     "Value": [started.strftime("%Y%m%d")] if started else []},
        "00080030": {"vr": "TM",
                     "Value": [started.strftime("%H%M%S")] if started else []},
        "00080061": {"vr": "CS", "Value": modalities},
        "00200010": {"vr": "SH", "Value": [row.resource_id]},
        "00201206": {"vr": "IS",
                     "Value": [int(body.get("numberOfSeries") or 0)]},
        "00201208": {"vr": "IS",
                     "Value": [int(body.get("numberOfInstances") or 0)]},
    }
    if body.get("description"):
        study["00081030"] = {"vr": "LO", "Value": [str(body["description"])]}
    return study


@dicomweb_router.get("/studies")
def dicomweb_studies(session: Session = Depends(get_db),
                     user: User = Depends(require_roles(*STAFF_READ))):
    """QIDO-RS study search: a JSON array of study-level DICOM JSON
    objects, one per stored ImagingStudy."""
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "ImagingStudy")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    return [_dicom_study_json(session, row) for row in rows]


@dicomweb_router.get("/studies/{study_uid}")
def dicomweb_study(study_uid: str, session: Session = Depends(get_db),
                   user: User = Depends(require_roles(*STAFF_READ))):
    """Retrieve one study's DICOM JSON by its Study Instance UID."""
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "ImagingStudy")).all()
    for row in rows:
        if _study_uid(row.body or {}) == study_uid:
            return _dicom_study_json(session, row)
    raise HTTPException(status_code=404,
                        detail=f"No study with Study Instance UID '{study_uid}'")


# ===========================================================================
# Surgery board
# ===========================================================================

def _case_appointment(session: Session, case_row: FhirResource) -> FhirResource | None:
    ext = _get_extension(case_row.body or {}, CASE_APPOINTMENT_EXTENSION)
    reference = ((ext or {}).get("valueReference") or {}).get("reference")
    if reference and reference.startswith("Appointment/"):
        try:
            return fhir._get_row("Appointment", reference.split("/", 1)[1], session)
        except HTTPException:
            return None
    return None


def _get_case(session: Session, case_id: str) -> tuple[FhirResource, FhirResource]:
    case = fhir._get_row("Procedure", case_id, session)
    if _get_extension(case.body or {}, OR_ROOM_EXTENSION) is None:
        raise HTTPException(
            status_code=422,
            detail=f"Procedure/{case_id} is not a surgery case (no OR room "
                   "assignment)")
    appointment = _case_appointment(session, case)
    if appointment is None:
        raise HTTPException(
            status_code=422,
            detail=f"Surgery case {case_id} has no linked scheduling Appointment")
    return case, appointment


def _appointment_room(body: dict) -> str | None:
    for participant in (body or {}).get("participant") or []:
        if isinstance(participant, dict):
            actor = participant.get("actor") or {}
            reference = actor.get("reference") if isinstance(actor, dict) else None
            if reference and reference.startswith("Location/"):
                return reference.split("/", 1)[1]
    return None


def _check_room_overlap(session: Session, room_code: str, start: datetime,
                        end: datetime, exclude_appointment_id: str | None = None) -> None:
    """Refuse (409) when another live appointment already occupies the
    room over an overlapping interval. Compares the REAL stored start/end
    of every appointment scheduled into the room; cancelled appointments
    (and appointments whose case was cancelled — cancelling a case
    cancels its appointment) do not block."""
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Appointment")).all()
    for other in rows:
        if other.resource_id == exclude_appointment_id:
            continue
        other_body = other.body or {}
        if (other_body.get("status") or "") == "cancelled":
            continue
        if _appointment_room(other_body) != room_code:
            continue
        other_start = _parse_dt(other_body.get("start"))
        other_end = _parse_dt(other_body.get("end"))
        if other_start is None or other_end is None:
            continue
        if start < other_end and other_start < end:
            raise HTTPException(
                status_code=409,
                detail=(f"Room {room_code} is already booked over that time "
                        f"(Appointment/{other.resource_id}, "
                        f"{other_body.get('start')} – {other_body.get('end')})"))


def _case_entry(case: FhirResource, appointment: FhirResource) -> dict:
    room_ext = _get_extension(case.body or {}, OR_ROOM_EXTENSION) or {}
    appt_body = appointment.body or {}
    return {
        "case": fhir._render(case),
        "appointment": fhir._render(appointment),
        "room": room_ext.get("valueString"),
        "state": (case.body or {}).get("status"),
        "start": appt_body.get("start"),
        "end": appt_body.get("end"),
        "patient_ref": _subject_ref(case.body or {}),
    }


@surgery_router.get("/rooms")
def surgery_rooms(session: Session = Depends(get_db),
                  user: User = Depends(require_roles(*STAFF_READ))):
    cfg = published_config(session, "terminology", OR_ROOMS_KEY)
    rooms = _concepts(session, OR_ROOMS_KEY, "operating rooms")
    return {"rooms": rooms, "version": cfg.version if cfg else None}


@surgery_router.post("/cases", status_code=201)
async def schedule_case(request: Request, session: Session = Depends(get_db),
                        user: User = Depends(require_capability("surgery.work"))):
    """Schedule an OR case: a Procedure (the case, in the `scheduled`
    workflow state) + a linked Appointment carrying the room and the
    start/end times. The room must be an ACTIVE concept in the published
    or-rooms config, and the room must be free over the interval (409)."""
    payload = await _json_body(request)
    subject_ref = None
    if isinstance(payload.get("subject"), dict):
        subject_ref = payload["subject"].get("reference")
    if not subject_ref and payload.get("patient"):
        subject_ref = fhir._normalize_patient_ref(str(payload["patient"]))
    if not subject_ref and payload.get("patient_id"):
        subject_ref = f"Patient/{payload['patient_id']}"
    if not subject_ref or not subject_ref.startswith("Patient/"):
        raise HTTPException(status_code=422,
                            detail="A case requires a patient ('patient_id', "
                                   "'patient' or 'subject.reference')")
    patient_id = subject_ref.split("/", 1)[1]
    fhir._get_row("Patient", patient_id, session)  # 404 if the patient is unknown

    code = payload.get("code")
    display = payload.get("display")
    if isinstance(code, dict):  # a CodeableConcept names the procedure
        _, code, display = _first_coding({"code": code})
    if not isinstance(code, str) or not code.strip():
        raise HTTPException(status_code=422,
                            detail="A case requires 'code' (the procedure code)")
    system = payload.get("system")
    if not isinstance(system, str) or not system.strip():
        system = "http://www.ama-assn.org/go/cpt"

    room_code = payload.get("room")
    if not isinstance(room_code, str) or not room_code.strip():
        raise HTTPException(status_code=422, detail="A case requires 'room'")
    room = _concept(session, OR_ROOMS_KEY, "operating rooms", room_code)
    if room is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown room '{room_code}': not in the published "
                   "or-rooms config")
    if room.get("active") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Room '{room_code}' ({room.get('display')}) is inactive "
                   "in the published or-rooms config")

    start = _parse_dt(payload.get("start"))
    end = _parse_dt(payload.get("end"))
    if start is None or end is None:
        raise HTTPException(status_code=422,
                            detail="A case requires ISO 'start' and 'end' datetimes")
    if end <= start:
        raise HTTPException(status_code=422,
                            detail="A case's 'end' must be after its 'start'")
    _check_room_overlap(session, room_code, start, end)

    case_id = str(uuid.uuid4())
    appointment_id = str(uuid.uuid4())
    procedure_body = {
        "resourceType": "Procedure",
        "id": case_id,
        "status": "scheduled",
        "code": {"coding": [{"system": system, "code": code,
                             "display": display}],
                 "text": display or code},
        "subject": {"reference": subject_ref},
        "extension": [
            {"url": OR_ROOM_EXTENSION, "valueString": room_code},
            {"url": CASE_APPOINTMENT_EXTENSION,
             "valueReference": {"reference": f"Appointment/{appointment_id}"}},
        ],
    }
    case = fhir.create_resource("Procedure", procedure_body, session, user)

    participants = [
        {"actor": {"reference": subject_ref}, "status": "accepted"},
        {"actor": {"reference": f"Location/{room_code}",
                   "display": room.get("display")}, "status": "accepted"},
    ]
    surgeon = payload.get("surgeon")
    if isinstance(surgeon, str) and surgeon.strip():
        ref = surgeon if "/" in surgeon else f"Practitioner/{surgeon}"
        participants.insert(1, {"actor": {"reference": ref}, "status": "accepted"})
    appointment_body = {
        "resourceType": "Appointment",
        "id": appointment_id,
        "status": "booked",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "description": display or code,
        "participant": participants,
        "extension": [
            {"url": APPOINTMENT_CASE_EXTENSION,
             "valueReference": {"reference": f"Procedure/{case_id}"}},
        ],
    }
    appointment = fhir.create_resource("Appointment", appointment_body, session, user)
    append_audit(session, actor=user.username, action="surgery.case.schedule",
                 entity=f"Procedure/{case_id}",
                 payload={"room": room_code, "patient": subject_ref,
                          "code": code, "start": start.isoformat(),
                          "end": end.isoformat(),
                          "appointment_id": appointment_id})
    session.commit()
    case_row = fhir._get_row("Procedure", case_id, session)
    appt_row = fhir._get_row("Appointment", appointment_id, session)
    return {"case": fhir._render(case_row), "appointment": fhir._render(appt_row),
            "room": room_code, "state": "scheduled"}


@surgery_router.get("/board")
def surgery_board(date: str | None = Query(default=None),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_roles(*STAFF_READ))):
    """One day's OR schedule, grouped by room: every surgery case whose
    appointment falls on `date` (default: today, UTC), with its current
    workflow state. All configured rooms appear, even when empty."""
    if date is None:
        date = _now().date().isoformat()
    try:
        day = datetime.fromisoformat(date).date()
    except (TypeError, ValueError):
        raise HTTPException(status_code=422,
                            detail=f"Invalid 'date' (expected YYYY-MM-DD): {date}")
    room_concepts = _concepts(session, OR_ROOMS_KEY, "operating rooms")
    by_room: dict[str, list[dict]] = {}
    displays: dict[str, str | None] = {}
    for concept in room_concepts:
        by_room[concept["code"]] = []
        displays[concept["code"]] = concept.get("display")

    cases = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Procedure")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    total = 0
    for case in cases:
        if _get_extension(case.body or {}, OR_ROOM_EXTENSION) is None:
            continue  # not a surgery case
        appointment = _case_appointment(session, case)
        if appointment is None:
            continue
        start = _parse_dt((appointment.body or {}).get("start"))
        if start is None or start.date() != day:
            continue
        entry = _case_entry(case, appointment)
        room_code = entry["room"]
        by_room.setdefault(room_code, [])
        displays.setdefault(room_code, room_code)
        by_room[room_code].append(entry)
        total += 1
    rooms = []
    for room_code in sorted(by_room):
        entries = sorted(by_room[room_code], key=lambda e: e["start"] or "")
        rooms.append({"code": room_code, "display": displays.get(room_code),
                      "cases": entries})
    return {"date": day.isoformat(), "rooms": rooms, "total": total}


@surgery_router.post("/cases/{case_id}/transition")
async def transition_case(case_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("surgery.work"))):
    """Advance a case through the published surgery-flow workflow config
    (scheduled -> pre-op -> in-op -> pacu -> done, cancellable from the
    early states). done/cancelled also settle the linked Appointment
    (fulfilled/cancelled)."""
    payload = await _json_body(request)
    target = payload.get("to_status") or payload.get("status")
    if not target:
        raise HTTPException(status_code=422, detail="Transition requires 'to_status'")
    case, appointment = _get_case(session, case_id)
    current = (case.body or {}).get("status")
    transitions, known_states = _machine(session, SURGERY_WORKFLOW_KEY,
                                         DEFAULT_SURGERY_TRANSITIONS)
    if target not in known_states:
        raise HTTPException(status_code=422, detail=f"Unknown surgery case status: {target}")
    if target not in transitions.get(current, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Illegal surgery case transition '{current}' -> '{target}'")
    _set_status(case, target)
    if target == "done":
        _set_status(appointment, "fulfilled")
    elif target == "cancelled":
        _set_status(appointment, "cancelled")
    session.flush()
    append_audit(session, actor=user.username, action="surgery.case.transition",
                 entity=f"Procedure/{case_id}",
                 payload={"from_status": current, "to_status": target,
                          "appointment_id": appointment.resource_id,
                          "version": case.version})
    session.commit()
    return {"case": fhir._render(case), "appointment": fhir._render(appointment),
            "state": target}


@surgery_router.post("/cases/{case_id}/reschedule")
async def reschedule_case(case_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("surgery.work"))):
    """Move a case to new start/end times. The room-overlap rule is
    enforced again on the move, excluding the case's own appointment;
    a finished or cancelled case cannot move (409)."""
    payload = await _json_body(request)
    case, appointment = _get_case(session, case_id)
    state = (case.body or {}).get("status")
    if state in ("done", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail=f"Case {case_id} is '{state}' and can no longer be rescheduled")
    start = _parse_dt(payload.get("start"))
    end = _parse_dt(payload.get("end"))
    if start is None or end is None:
        raise HTTPException(status_code=422,
                            detail="Reschedule requires ISO 'start' and 'end' datetimes")
    if end <= start:
        raise HTTPException(status_code=422,
                            detail="A case's 'end' must be after its 'start'")
    room_code = (_get_extension(case.body or {}, OR_ROOM_EXTENSION) or {}).get("valueString")
    _check_room_overlap(session, room_code, start, end,
                          exclude_appointment_id=appointment.resource_id)
    body = dict(appointment.body or {})
    body["start"] = start.isoformat()
    body["end"] = end.isoformat()
    appointment.body = body
    appointment.start_at = fhir._parse_start(body)
    appointment.version += 1
    session.flush()
    append_audit(session, actor=user.username, action="surgery.case.reschedule",
                 entity=f"Procedure/{case_id}",
                 payload={"appointment_id": appointment.resource_id,
                          "room": room_code, "start": start.isoformat(),
                          "end": end.isoformat()})
    session.commit()
    return {"case": fhir._render(case), "appointment": fhir._render(appointment),
            "room": room_code, "state": state}
