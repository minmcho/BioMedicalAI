"""Phase 9 — Patient engagement: secure messaging, telehealth visits,
self-scheduling.

Built ON the Studio customization engine and the existing modules,
like every other OpenCare module (roadmap §6: configure, don't fork):

- MESSAGES are FHIR Communication resources. A thread is a set of
  Communications sharing a thread-id extension; the patient's record
  is the thread's subject. Staff work threads under the
  `messages.use` capability; the patient side is scoped to the
  caller's own record exactly like the rest of the portal (another
  patient's thread is a 404, never a 403 — non-disclosure). Read
  state is a read-by extension on each message (usernames), so
  unread counts are computed, never stored separately.

- TELEHEALTH is an appointment TYPE, not a parallel system: the
  `telehealth` slot type is appended to the published slot-types
  terminology (and the `telehealth` note type to the published
  note-types catalogue) as new governed versions at startup — the
  pattern Phase 6 set for preop/pacu note types. A telehealth visit
  workspace assembles the appointment, the pre-visit intake form
  (a published Studio form) and the join details. OpenCare does NOT
  provide video: the join link is DATA, generated from the
  organisation's configured external provider URL template in the
  published `integration/telehealth` config, and the disclaimer in
  that config says so in the API and the UI.

- SELF-SCHEDULING is a patient-facing request over the PUBLISHED
  slot types (app.access owns that catalogue; nothing here forks
  scheduling): the request creates a real Appointment in `proposed`
  state for staff to confirm through the ordinary Appointment ADT
  machine — or `booked` immediately when the published
  `integration/self-scheduling` config sets `autoConfirm`. The
  endpoint itself is capability-gated (`portal.self-schedule`), so a
  site can switch the whole feature off in the permissions config.
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import access, documentation, fhir
from .audit import append_audit
from .configstore import (list_config_versions, published_config,
                          role_allowed)
from .models import (ConfigStatus, FhirResource, Role, StudioConfig, User)
from .security import get_current_user, get_db, require_roles

router = APIRouter(prefix="/messages", tags=["messages"])
engagement_router = APIRouter(prefix="/engagement", tags=["engagement"])
portal_router = APIRouter(prefix="/portal", tags=["portal-engagement"])

TELEHEALTH_KEY = "telehealth"
SELF_SCHEDULING_KEY = "self-scheduling"
TELEHEALTH_INTAKE_FORM = "telehealth-intake"
TELEHEALTH_NOTE_FORM = "note-telehealth"

THREAD_EXTENSION = "urn:opencare:extension/message-thread"
SENDER_ROLE_EXTENSION = "urn:opencare:extension/sender-role"
READ_BY_EXTENSION = "urn:opencare:extension/read-by"
APPOINTMENT_EXTENSION = "urn:opencare:extension/appointment"


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
# Default Studio configuration (seeded / governed-appended at startup)
# ---------------------------------------------------------------------------

def default_telehealth_payload() -> dict:
    return {
        "provider": {
            "name": "External video provider (organisation-configured)",
            "joinUrlTemplate": "https://video.example.invalid/opencare/{appointmentId}",
        },
        "intakeForm": TELEHEALTH_INTAKE_FORM,
        "noteType": "telehealth",
        "disclaimer": (
            "OpenCare does not provide video conferencing. The join link "
            "is generated from the organisation's configured external "
            "video-provider URL template; the visit itself takes place "
            "with that provider. OpenCare records the appointment, the "
            "pre-visit intake form and the visit note."),
    }


def default_self_scheduling_payload() -> dict:
    return {
        "autoConfirm": False,
        "description": ("Patient appointment requests create a real "
                        "Appointment in 'proposed' state for staff to "
                        "confirm, unless autoConfirm is true, in which "
                        "case the appointment is booked immediately."),
    }


def _form(title: str, items: list[dict]) -> dict:
    return {"resourceType": "Questionnaire", "title": title, "item": items}


def _item(link_id, text, type_, required=False):
    return {"linkId": link_id, "text": text, "type": type_, "required": required}


def default_form_payloads() -> dict[str, dict]:
    return {
        TELEHEALTH_INTAKE_FORM: _form("Telehealth pre-visit intake", [
            _item("chief-concern", "What would you like to discuss at this visit?",
                  "text", True),
            _item("technology-check",
                  "I have a working camera and microphone for the visit",
                  "boolean", True),
            _item("visit-location", "Where will you be during the visit?",
                  "string"),
            _item("medications-changed",
                  "My medication list has changed since my last visit",
                  "boolean"),
            _item("interpreter-needed", "I need an interpreter", "boolean"),
        ]),
        TELEHEALTH_NOTE_FORM: _form("Telehealth visit note", [
            _item("presenting-concern", "Presenting concern", "text", True),
            _item("examination", "Examination (as possible over video)", "text"),
            _item("assessment", "Assessment", "text", True),
            _item("plan", "Plan", "text", True),
            _item("follow-up", "Follow-up plan", "text"),
        ]),
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


def _extend_terminology(session: Session, key: str, additions: list[dict],
                        marker: str) -> None:
    """Append concepts to a PUBLISHED terminology catalogue as a new
    governed version (the Phase 6 note-types pattern): existing
    concepts — including any a site has customised — are preserved
    verbatim; nothing happens when every addition is already present
    or the catalogue is unpublished."""
    cfg = published_config(session, "terminology", key)
    if cfg is None:
        return
    concepts = [c for c in (cfg.payload or {}).get("concepts") or []
                if isinstance(c, dict)]
    have = {c.get("code") for c in concepts}
    new = [a for a in additions if a["code"] not in have]
    if not new:
        return
    payload = dict(cfg.payload or {})
    payload["concepts"] = concepts + new
    cfg.status = ConfigStatus.superseded
    session.flush()
    versions = list_config_versions(session, "terminology", key)
    row = StudioConfig(kind="terminology", key=key,
                       version=versions[-1].version + 1,
                       status=ConfigStatus.published, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    for action in ("studio.config.create", "studio.config.publish"):
        append_audit(session, actor="system", action=action,
                     entity=f"config:terminology/{key}@v{row.version}",
                     payload={"kind": "terminology", "key": key,
                              "version": row.version,
                              marker: [a["code"] for a in new]})


def ensure_engagement_defaults(session: Session) -> None:
    """Seed the Phase 9 engagement configuration as published Studio
    config v1 (idempotent), and governed-append the telehealth slot
    type + note type to their published catalogues."""
    _seed_published(session, "integration", TELEHEALTH_KEY,
                    default_telehealth_payload())
    _seed_published(session, "integration", SELF_SCHEDULING_KEY,
                    default_self_scheduling_payload())
    for key, payload in default_form_payloads().items():
        _seed_published(session, "form", key, payload)
    _extend_terminology(session, access.SLOT_TYPES_KEY, [
        {"code": "telehealth", "display": "Telehealth visit", "active": True,
         "system": access.SLOT_TYPES_SYSTEM, "durationMinutes": 30},
    ], "phase9_extension")
    _extend_terminology(session, documentation.NOTE_TYPES_KEY, [
        {"code": "telehealth", "display": "Telehealth visit note",
         "active": True, "templateForm": TELEHEALTH_NOTE_FORM},
    ], "phase9_extension")
    session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


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


def _thread_id_of(body: dict) -> str | None:
    ext = _extension(body, THREAD_EXTENSION)
    return ext.get("valueString") if ext else None


def _sender_role_of(body: dict) -> str | None:
    ext = _extension(body, SENDER_ROLE_EXTENSION)
    return ext.get("valueString") if ext else None


def _read_by(body: dict) -> list[str]:
    ext = _extension(body, READ_BY_EXTENSION)
    raw = ext.get("valueString") if ext else None
    return [u for u in (raw or "").split(",") if u]


def _message_text(body: dict) -> str:
    for payload in body.get("payload") or []:
        if isinstance(payload, dict) and payload.get("contentString"):
            return str(payload["contentString"])
    return ""


def _decorate_message(row: FhirResource, user: User) -> dict:
    out = fhir._render(row)
    body = row.body or {}
    out["threadId"] = _thread_id_of(body)
    out["senderRole"] = _sender_role_of(body)
    out["text"] = _message_text(body)
    out["read"] = user.username in _read_by(body)
    out["mine"] = (body.get("sender") or {}).get("display") == user.username
    return out


def _all_messages(session: Session) -> list[FhirResource]:
    return list(session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Communication")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all())


def _thread_rows(session: Session, thread_id: str) -> list[FhirResource]:
    return [r for r in _all_messages(session)
            if _thread_id_of(r.body or {}) == thread_id]


def _thread_summary(session: Session, thread_id: str, rows: list[FhirResource],
                    user: User) -> dict:
    first = rows[0].body or {}
    last = rows[-1]
    unread = sum(
        1 for r in rows
        if (r.body or {}).get("sender", {}).get("display") != user.username
        and user.username not in _read_by(r.body or {}))
    return {
        "thread_id": thread_id,
        "patient": rows[0].patient_ref,
        "topic": ((first.get("topic") or {}).get("text")),
        "message_count": len(rows),
        "unread_count": unread,
        "last_message_at": (last.body or {}).get("sent"),
        "last_text": _message_text(last.body or {}),
        "last_sender_role": _sender_role_of(last.body or {}),
    }


def _summaries(session: Session, user: User,
               patient_ref: str | None = None) -> list[dict]:
    threads: dict[str, list[FhirResource]] = {}
    for row in _all_messages(session):
        if patient_ref is not None and row.patient_ref != patient_ref:
            continue
        thread_id = _thread_id_of(row.body or {})
        if thread_id:
            threads.setdefault(thread_id, []).append(row)
    out = [_thread_summary(session, tid, rows, user)
           for tid, rows in threads.items()]
    out.sort(key=lambda s: s["last_message_at"] or "", reverse=True)
    return out


def _require_staff(session: Session, user: User) -> None:
    if user.role == Role.patient:
        raise HTTPException(status_code=403,
                            detail="Patient callers use the portal endpoints")
    if not role_allowed(session, user.role, "messages.use"):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.value} lacks capability 'messages.use'")


def _visible_thread(session: Session, thread_id: str,
                    user: User) -> list[FhirResource]:
    """Thread rows the caller may see. A patient sees only threads on
    their own record; another patient's thread is a 404 (the portal
    non-disclosure rule). Staff need messages.use."""
    rows = _thread_rows(session, thread_id)
    if not rows:
        raise HTTPException(status_code=404,
                            detail=f"Message thread {thread_id} not found")
    if user.role == Role.patient:
        if rows[0].patient_ref != user.patient_ref:
            raise HTTPException(status_code=404,
                                detail=f"Message thread {thread_id} not found")
    else:
        _require_staff(session, user)
    return rows


def _create_message(session: Session, user: User, patient_ref: str,
                    text: str, thread_id: str | None = None,
                    topic: str | None = None) -> FhirResource:
    message_id = str(uuid.uuid4())
    thread = thread_id or message_id
    body = {
        "resourceType": "Communication",
        "id": message_id,
        "status": "completed",
        "subject": {"reference": patient_ref},
        "sent": _now_iso(),
        "sender": {"display": user.username},
        "recipient": ([{"display": "care team"}]
                      if user.role == Role.patient
                      else [{"reference": patient_ref}]),
        "payload": [{"contentString": text}],
        "extension": [
            {"url": THREAD_EXTENSION, "valueString": thread},
            {"url": SENDER_ROLE_EXTENSION, "valueString": user.role.value},
            {"url": READ_BY_EXTENSION, "valueString": user.username},
        ],
    }
    if topic:
        body["topic"] = {"text": topic}
    fhir.create_resource("Communication", body, session, user)
    row = fhir._get_row("Communication", message_id, session)
    append_audit(session, actor=user.username, action="messages.send",
                 entity=f"Communication/{message_id}",
                 payload={"thread_id": thread, "patient": patient_ref,
                          "sender_role": user.role.value})
    session.commit()
    return row


# ---------------------------------------------------------------------------
# Messaging — staff surface (/messages)
# ---------------------------------------------------------------------------

@router.post("", status_code=201)
async def staff_send(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Start a thread with a patient (or, with thread_id, reply —
    prefer the explicit reply endpoint). Staff only."""
    _require_staff(session, user)
    payload = await _json_body(request)
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=422, detail="Message requires 'text'")
    patient_ref = None
    subject = payload.get("subject") or {}
    if isinstance(subject, dict) and subject.get("reference"):
        patient_ref = subject["reference"]
    elif payload.get("patient"):
        patient_ref = fhir._normalize_patient_ref(str(payload["patient"]))
    if not patient_ref or not patient_ref.startswith("Patient/"):
        raise HTTPException(
            status_code=422,
            detail="Message requires 'subject.reference' (or 'patient')")
    fhir._get_row("Patient", patient_ref.split("/", 1)[1], session)  # 404
    row = _create_message(session, user, patient_ref, text.strip(),
                          topic=payload.get("topic"))
    return _decorate_message(row, user)


@router.get("/inbox")
def staff_inbox(patient: str = Query(...),
                session: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """The staff message inbox for one patient — thread summaries in
    the same spirit as the results inbox (GET /fhir/inbox): one
    patient's items, newest activity first, unread counts computed
    from read receipts on the messages themselves."""
    _require_staff(session, user)
    patient_ref = fhir._normalize_patient_ref(patient)
    fhir._get_row("Patient", patient_ref.split("/", 1)[1], session)  # 404
    items = _summaries(session, user, patient_ref)
    return {"items": items, "total": len(items), "patient": patient_ref}


@router.get("/threads/{thread_id}")
def thread_detail(thread_id: str, session: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    rows = _visible_thread(session, thread_id, user)
    return {"thread_id": thread_id,
            "patient": rows[0].patient_ref,
            "messages": [_decorate_message(r, user) for r in rows],
            "total": len(rows)}


@router.post("/threads/{thread_id}/reply", status_code=201)
async def thread_reply(thread_id: str, request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    rows = _visible_thread(session, thread_id, user)
    payload = await _json_body(request)
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=422, detail="Reply requires 'text'")
    row = _create_message(session, user, rows[0].patient_ref or "",
                          text.strip(), thread_id=thread_id)
    return _decorate_message(row, user)


@router.post("/threads/{thread_id}/read")
def thread_read(thread_id: str, session: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Mark every message in the thread read by the caller (read
    receipts are recorded on the messages — the audit trail is the
    resource versions themselves)."""
    rows = _visible_thread(session, thread_id, user)
    marked = 0
    for row in rows:
        body = dict(row.body or {})
        readers = _read_by(body)
        if user.username in readers:
            continue
        readers.append(user.username)
        _set_extension(body, READ_BY_EXTENSION,
                       valueString=",".join(readers))
        row.body = body
        row.version += 1
        marked += 1
    session.flush()
    append_audit(session, actor=user.username, action="messages.read",
                 entity=f"Communication/thread:{thread_id}",
                 payload={"marked": marked})
    session.commit()
    return {"thread_id": thread_id, "marked": marked}


@router.get("/unread")
def unread_count(session: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """The caller's total unread messages (staff: across all threads;
    patient: across their own threads only)."""
    if user.role == Role.patient:
        summaries = _summaries(session, user, user.patient_ref)
    else:
        _require_staff(session, user)
        summaries = _summaries(session, user)
    return {"unread": sum(s["unread_count"] for s in summaries),
            "threads_with_unread": sum(1 for s in summaries
                                       if s["unread_count"] > 0)}


# ---------------------------------------------------------------------------
# Messaging — patient surface (/portal)
# ---------------------------------------------------------------------------

@portal_router.post("/messages", status_code=201)
async def patient_send(request: Request, session: Session = Depends(get_db),
                       user: User = Depends(require_roles(Role.patient))):
    """Start a message thread with the care team. The subject is
    ALWAYS the caller's own record — a patient cannot address a
    thread to anyone else's chart."""
    if not user.patient_ref:
        raise HTTPException(status_code=403,
                            detail="This patient account is not linked to a patient record")
    payload = await _json_body(request)
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=422, detail="Message requires 'text'")
    row = _create_message(session, user, user.patient_ref, text.strip(),
                          topic=payload.get("topic"))
    return _decorate_message(row, user)


@portal_router.get("/my-messages")
def patient_threads(session: Session = Depends(get_db),
                    user: User = Depends(require_roles(Role.patient))):
    if not user.patient_ref:
        raise HTTPException(status_code=403,
                            detail="This patient account is not linked to a patient record")
    items = _summaries(session, user, user.patient_ref)
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Telehealth
# ---------------------------------------------------------------------------

def _telehealth_config(session: Session) -> dict:
    cfg = published_config(session, "integration", TELEHEALTH_KEY)
    return (cfg.payload or {}) if cfg is not None else {}


def _appointment_type_code(body: dict) -> str | None:
    for coding in (body.get("appointmentType") or {}).get("coding") or []:
        if isinstance(coding, dict) and coding.get("code"):
            return coding["code"]
    return None


def _appointment_patient_ref(body: dict) -> str | None:
    for participant in body.get("participant") or []:
        if isinstance(participant, dict):
            ref = (participant.get("actor") or {}).get("reference")
            if ref and ref.startswith("Patient/"):
                return ref
    return None


def _telehealth_workspace(session: Session, appointment_id: str) -> dict:
    row = fhir._get_row("Appointment", appointment_id, session)
    body = row.body or {}
    if _appointment_type_code(body) != "telehealth":
        raise HTTPException(
            status_code=422,
            detail=(f"Appointment/{appointment_id} is not a telehealth "
                    "appointment (its appointmentType is not 'telehealth')"))
    config = _telehealth_config(session)
    provider = config.get("provider") or {}
    template = provider.get("joinUrlTemplate")
    join_url = template.replace("{appointmentId}", appointment_id) \
        if isinstance(template, str) and template else None
    intake_form = config.get("intakeForm") or TELEHEALTH_INTAKE_FORM
    patient_ref = _appointment_patient_ref(body)
    intake_response_id = None
    if patient_ref:
        for qr in session.scalars(
                select(FhirResource).where(
                    FhirResource.resource_type == "QuestionnaireResponse",
                    FhirResource.patient_ref == patient_ref)).all():
            qr_body = qr.body or {}
            if str(qr_body.get("questionnaire") or "").rsplit("/", 1)[-1] \
                    != intake_form:
                continue
            ext = _extension(qr_body, APPOINTMENT_EXTENSION)
            ref = ((ext or {}).get("valueReference") or {}).get("reference")
            if ref == f"Appointment/{appointment_id}":
                intake_response_id = qr.resource_id
    form_cfg = published_config(session, "form", intake_form)
    return {
        "appointment": fhir._render(row),
        "patient": patient_ref,
        "provider_name": provider.get("name"),
        "join_url": join_url,
        "disclaimer": config.get("disclaimer"),
        "intake_form": intake_form,
        # The published intake form itself rides the workspace so the
        # portal can render it (FormRenderer) without Studio read
        # access — published configuration is the org's own form, not
        # a secret.
        "intake_form_definition": form_cfg.payload if form_cfg else None,
        "intake_submitted": intake_response_id is not None,
        "intake_response_id": intake_response_id,
        "note_type": config.get("noteType") or "telehealth",
    }


@engagement_router.get("/telehealth/config")
def telehealth_config(session: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """The published telehealth configuration (provider + disclaimer)
    — shown verbatim in the UI so nobody mistakes the join link for
    an OpenCare video service."""
    return {"config": _telehealth_config(session)}


@engagement_router.get("/telehealth/appointments/{appointment_id}/workspace")
def telehealth_workspace_staff(appointment_id: str,
                               session: Session = Depends(get_db),
                               user: User = Depends(get_current_user)):
    _require_staff(session, user)
    return _telehealth_workspace(session, appointment_id)


@portal_router.get("/telehealth/appointments/{appointment_id}/workspace")
def telehealth_workspace_patient(appointment_id: str,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_roles(Role.patient))):
    workspace = _telehealth_workspace(session, appointment_id)
    if workspace["patient"] != user.patient_ref:
        raise HTTPException(status_code=404,
                            detail=f"Appointment/{appointment_id} not found")
    return workspace


@portal_router.post("/telehealth/appointments/{appointment_id}/intake",
                    status_code=201)
async def telehealth_intake(appointment_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_roles(Role.patient))):
    """Submit the pre-visit intake form for the patient's OWN
    telehealth appointment. Answers are validated against the
    PUBLISHED intake form by the same validator every other form
    uses (required items enforced)."""
    workspace = _telehealth_workspace(session, appointment_id)
    if workspace["patient"] != user.patient_ref:
        raise HTTPException(status_code=404,
                            detail=f"Appointment/{appointment_id} not found")
    payload = await _json_body(request)
    answers = payload.get("answers")
    if not isinstance(answers, list):
        raise HTTPException(status_code=422,
                            detail="'answers' must be a list of QuestionnaireResponse items")
    form_key = workspace["intake_form"]
    body = {
        "resourceType": "QuestionnaireResponse",
        "id": str(uuid.uuid4()),
        "status": "completed",
        "questionnaire": form_key,
        "subject": {"reference": user.patient_ref},
        "authored": _now_iso(),
        "author": {"display": user.username},
        "item": answers,
        "extension": [{"url": APPOINTMENT_EXTENSION,
                       "valueReference": {
                           "reference": f"Appointment/{appointment_id}"}}],
    }
    fhir._validate_questionnaire_response(session, body)
    rendered = fhir.create_resource("QuestionnaireResponse", body, session, user)
    append_audit(session, actor=user.username, action="engagement.telehealth.intake",
                 entity=f"QuestionnaireResponse/{rendered['id']}",
                 payload={"appointment_id": appointment_id, "form": form_key})
    session.commit()
    return rendered


# ---------------------------------------------------------------------------
# Self-scheduling (patient requests over the published slot types)
# ---------------------------------------------------------------------------

def _self_scheduling_config(session: Session) -> dict:
    cfg = published_config(session, "integration", SELF_SCHEDULING_KEY)
    return (cfg.payload or {}) if cfg is not None else {}


@portal_router.get("/self-scheduling/config")
def self_scheduling_config(session: Session = Depends(get_db),
                           user: User = Depends(require_roles(Role.patient))):
    """What the portal may promise the patient: whether a request
    books immediately (autoConfirm) or waits for staff confirmation,
    and the published slot types a request may name."""
    config = _self_scheduling_config(session)
    concepts = access._concepts(session, access.SLOT_TYPES_KEY)
    return {"auto_confirm": config.get("autoConfirm") is True,
            "slot_types": [c.get("code") for c in concepts
                           if c.get("active") is not False]}


@portal_router.post("/appointments/request", status_code=201)
async def request_appointment(request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """Request an appointment from the PUBLISHED slot types
    (app.access owns the catalogue — this endpoint reads it, it does
    not fork scheduling). Creates a real Appointment: `proposed`
    (staff confirm through the ordinary ADT machine) or `booked`
    when the published self-scheduling config sets autoConfirm.
    Gated by the `portal.self-schedule` capability."""
    if not role_allowed(session, user.role, "portal.self-schedule"):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.value} lacks capability 'portal.self-schedule'")
    if user.role != Role.patient or not user.patient_ref:
        raise HTTPException(
            status_code=403,
            detail="Self-scheduling requests are made by a patient "
                   "account linked to a patient record")
    payload = await _json_body(request)
    slot_type = payload.get("slot_type") or payload.get("slotType")
    concept = None
    cfg = published_config(session, "terminology", access.SLOT_TYPES_KEY)
    for candidate in (cfg.payload or {}).get("concepts") or [] \
            if cfg is not None else []:
        if isinstance(candidate, dict) and candidate.get("code") == slot_type:
            concept = candidate
            break
    if concept is None or concept.get("active") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown or inactive slot type '{slot_type}': not in "
                   "the published slot-types catalogue")
    start_raw = payload.get("start")
    start = _parse_iso(start_raw)
    if start is None:
        raise HTTPException(status_code=422,
                            detail="Request requires 'start' (ISO datetime)")
    end = _parse_iso(payload.get("end"))
    if end is None:
        minutes = concept.get("durationMinutes")
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) \
                or minutes <= 0:
            minutes = 30
        end = start + timedelta(minutes=float(minutes))
    if end <= start:
        raise HTTPException(status_code=422,
                            detail="Appointment 'end' must be after 'start'")
    auto_confirm = _self_scheduling_config(session).get("autoConfirm") is True
    participants = [{"actor": {"reference": user.patient_ref},
                     "status": "accepted"}]
    practitioner = payload.get("practitioner")
    if isinstance(practitioner, str) and practitioner:
        ref = practitioner if "/" in practitioner \
            else f"Practitioner/{practitioner}"
        participants.append({"actor": {"reference": ref},
                             "status": "needs-action"})
    body = {
        "resourceType": "Appointment",
        "id": str(uuid.uuid4()),
        "status": "booked" if auto_confirm else "proposed",
        "appointmentType": {"coding": [{
            "system": access.SLOT_TYPES_SYSTEM, "code": slot_type,
            "display": concept.get("display")}]},
        "start": start.isoformat(),
        "end": end.isoformat(),
        "participant": participants,
        "description": (payload.get("reason")
                        if isinstance(payload.get("reason"), str)
                        and payload.get("reason")
                        else "Patient self-scheduling request"),
    }
    rendered = fhir.create_resource("Appointment", body, session, user)
    append_audit(session, actor=user.username,
                 action="engagement.self_schedule.request",
                 entity=f"Appointment/{rendered['id']}",
                 payload={"slot_type": slot_type,
                          "auto_confirmed": auto_confirm,
                          "status": body["status"]})
    session.commit()
    return {"appointment": rendered, "auto_confirmed": auto_confirm,
            "status": body["status"]}


@portal_router.get("/my-appointments")
def my_appointments(session: Session = Depends(get_db),
                    user: User = Depends(require_roles(Role.patient))):
    """The patient's own appointments (any status), soonest first.
    FHIR search already force-scopes patient callers to their own
    record; this endpoint is the portal-shaped view of it."""
    if not user.patient_ref:
        raise HTTPException(status_code=403,
                            detail="This patient account is not linked to a patient record")
    bundle = fhir.search_resources("Appointment", session, user)
    items = [entry["resource"] for entry in bundle.get("entry") or []]
    items.sort(key=lambda a: a.get("start") or "")
    return {"items": items, "total": len(items)}
