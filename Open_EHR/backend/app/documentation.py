"""Phase 5 — Clinical documentation: notes + flowsheets.

Built ON the Studio customization engine like every other module:

- note TYPES are terminology config, key ``note-types`` — each type
  names the published Studio FORM that is its documentation template;
- the templates themselves are form configs (``note-progress``,
  ``note-hp``, ``note-procedure``, ``note-discharge``);
- flowsheets are form configs too (``flowsheet-vitals``,
  ``flowsheet-assessment``) — any published form can be collected as a
  flowsheet;

all seeded as published v1 at startup by
:func:`ensure_documentation_defaults`.

Notes are FHIR DocumentReference resources. The structured content
(a Composition-lite payload: template form, the attached
QuestionnaireResponse id, free-text sections) rides in an extension
as a JSON string — the same storage idiom the claim lifecycle uses
for its status history (app.billing) — and API responses decorate it
back out as `noteContent`. Lifecycle:

- POST /notes creates a DRAFT (docStatus `preliminary`). Drafts are
  visible to staff notes.read holders; there is deliberately no
  update endpoint — a draft is signed or left, never silently edited.
- POST /notes/{id}/sign moves draft -> signed (docStatus `final`,
  authenticator recorded). Signing an already-signed note -> 409.
- POST /notes/{id}/amend creates a NEW DocumentReference with
  docStatus `amended` and a `relatesTo: replaces` link to the signed
  original. The original is IMMUTABLE: amend never writes to it (its
  stored version does not change), and both versions stay readable
  and auditable.

When a note is authored from a template, the caller's answers are
stored as a real QuestionnaireResponse validated against the
PUBLISHED form by the same validator the generic FHIR surface uses
(app.fhir._validate_questionnaire_response) — required answers are
enforced identically.

Flowsheets: POST /flowsheets stores one QuestionnaireResponse per
entry against a published form; GET /flowsheets/{form_key}?patient=
returns the time-series (form items x entry columns) the chart grid
renders. Values are normalised out of the FHIR answer shapes into
plain scalars at read time.

Rich text (Home & Module UX pass, app.richtext): template items of
type ``text`` are prose and are seeded with ``"rich": true``.
QuestionnaireResponse answers for rich/text items, plus the note
``text``/``sections`` prose, are sanitized on WRITE
(:func:`app.richtext.sanitize_html`); reads return the stored
sanitized HTML. The DocumentReference also carries a FHIR Narrative
(``text.div``) rendered from the note prose with
:func:`app.richtext.html_to_text` — FHIR/print surfaces never carry
author markup — and decorated responses expose it as ``plainText``.
"""
import html as _html
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir, richtext
from .audit import append_audit
from .configstore import published_config
from .models import ConfigStatus, FhirResource, StudioConfig, User
from .security import get_db, require_capability

notes_router = APIRouter(prefix="/notes", tags=["notes"])
flowsheets_router = APIRouter(prefix="/flowsheets", tags=["flowsheets"])

NOTE_TYPES_KEY = "note-types"
NOTE_TYPES_SYSTEM = "urn:opencare:note-type"
NOTE_CONTENT_EXTENSION = "urn:opencare:extension/note-content"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Default Studio configuration (seeded published v1 at startup)
# ---------------------------------------------------------------------------

def default_note_types_payload() -> dict:
    def ntype(code, display, template):
        return {"code": code, "display": display, "active": True,
                "templateForm": template}

    return {
        "name": "OpenCare note types",
        "system": NOTE_TYPES_SYSTEM,
        "concepts": [
            ntype("progress", "Progress note", "note-progress"),
            ntype("hp", "History & physical", "note-hp"),
            ntype("procedure", "Procedure note", "note-procedure"),
            ntype("discharge", "Discharge summary", "note-discharge"),
        ],
    }


def _form(title: str, items: list[dict]) -> dict:
    return {"resourceType": "Questionnaire", "title": title, "item": items}


def _item(link_id, text, type_, required=False):
    item = {"linkId": link_id, "text": text, "type": type_,
            "required": required}
    if type_ == "text":
        # Prose item: the web FormRenderer shows the rich-text
        # editor for items marked rich, and the server sanitizes
        # these answers on write (app.richtext).
        item["rich"] = True
    return item


def _choice(link_id, text, options, required=False):
    item = _item(link_id, text, "choice", required)
    item["answerOption"] = [
        {"valueCoding": {"code": code, "display": display}}
        for code, display in options]
    return item


def default_form_payloads() -> dict[str, dict]:
    return {
        "note-progress": _form("Progress note", [
            _item("subjective", "Subjective", "text", True),
            _item("objective", "Objective", "text"),
            _item("assessment", "Assessment", "text", True),
            _item("plan", "Plan", "text", True),
        ]),
        "note-hp": _form("History & physical", [
            _item("chief-complaint", "Chief complaint", "text", True),
            _item("history", "History of present illness", "text", True),
            _item("examination", "Examination", "text"),
            _item("assessment-plan", "Assessment & plan", "text", True),
        ]),
        "note-procedure": _form("Procedure note", [
            _item("procedure-name", "Procedure performed", "string", True),
            _item("indication", "Indication", "text"),
            _item("findings", "Findings", "text", True),
            _item("complications", "Complications", "text"),
        ]),
        "note-discharge": _form("Discharge summary", [
            _item("hospital-course", "Hospital course", "text", True),
            _item("discharge-condition", "Condition at discharge", "string"),
            _item("follow-up", "Follow-up plan", "text", True),
            _item("medications-reviewed", "Medications reviewed", "boolean"),
        ]),
        "flowsheet-vitals": _form("Vital signs flowsheet", [
            _item("systolic", "Systolic BP (mmHg)", "integer", True),
            _item("diastolic", "Diastolic BP (mmHg)", "integer"),
            _item("heart-rate", "Heart rate (bpm)", "integer", True),
            _item("respiratory-rate", "Respiratory rate (/min)", "integer"),
            _item("temperature", "Temperature (°C)", "decimal"),
            _item("spo2", "SpO2 (%)", "integer", True),
            _item("pain-score", "Pain score (0-10)", "integer"),
            _item("weight", "Weight (kg)", "decimal"),
        ]),
        "flowsheet-assessment": _form("Nursing assessment flowsheet", [
            _choice("consciousness", "Consciousness",
                    [("alert", "Alert"), ("voice", "Responds to voice"),
                     ("pain", "Responds to pain"),
                     ("unresponsive", "Unresponsive")], True),
            _item("pain-score", "Pain score (0-10)", "integer"),
            _choice("skin-integrity", "Skin integrity",
                    [("intact", "Intact"), ("redness", "Redness"),
                     ("breakdown", "Breakdown")]),
            _choice("falls-risk", "Falls risk",
                    [("low", "Low"), ("moderate", "Moderate"),
                     ("high", "High")]),
            _item("notes", "Assessment notes", "text"),
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


def ensure_documentation_defaults(session: Session) -> None:
    """Seed the note-type catalogue + the note/flowsheet template
    forms as published Studio config v1 (idempotent)."""
    _seed_published(session, "terminology", NOTE_TYPES_KEY,
                    default_note_types_payload())
    for key, payload in default_form_payloads().items():
        _seed_published(session, "form", key, payload)
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


def _note_type_concept(session: Session, code: str) -> dict | None:
    cfg = published_config(session, "terminology", NOTE_TYPES_KEY)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=(f"No published note-type catalogue "
                    f"(terminology/{NOTE_TYPES_KEY}); publish one in "
                    "Studio before authoring notes"))
    for concept in (cfg.payload or {}).get("concepts") or []:
        if isinstance(concept, dict) and concept.get("code") == code:
            return concept
    return None


def _patient_ref_of(session: Session, payload: dict) -> str:
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


def _rich_link_ids(session: Session, form_key: str) -> set:
    """linkIds of a published form's prose items: those marked
    ``"rich": true`` in the template, plus every ``text``-type item
    (FHIR's long-prose type — covers governed forms seeded by other
    modules, e.g. continuum's discharge forms, whose templates do
    not carry the rich flag). Group children are walked too."""
    form = published_config(session, "form", form_key)
    if form is None:
        return set()
    ids: set = set()

    def walk(items) -> None:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "group":
                walk(item.get("item"))
            elif item.get("rich") is True or item.get("type") == "text":
                if item.get("linkId"):
                    ids.add(item["linkId"])

    walk((form.payload or {}).get("item"))
    return ids


def _sanitize_answers(session: Session, form_key: str, answers):
    """Sanitize the valueString answers of prose (rich/text) items
    on write. All other answer shapes pass through untouched."""
    if not isinstance(answers, list):
        return answers
    rich_ids = _rich_link_ids(session, form_key)
    if not rich_ids:
        return answers
    out = []
    for item in answers:
        if not isinstance(item, dict) or item.get("linkId") not in rich_ids:
            out.append(item)
            continue
        new_item = dict(item)
        new_answers = []
        for answer in item.get("answer") or []:
            if isinstance(answer, dict) \
                    and isinstance(answer.get("valueString"), str):
                answer = dict(answer)
                answer["valueString"] = richtext.sanitize_html(
                    answer["valueString"])
            new_answers.append(answer)
        new_item["answer"] = new_answers
        out.append(new_item)
    return out


def _sanitize_prose_value(value):
    """Recursively sanitize every string inside a prose structure
    (note ``sections``): strings -> sanitize_html, lists/dicts are
    walked, scalars pass through. Shape is preserved."""
    if isinstance(value, str):
        return richtext.sanitize_html(value)
    if isinstance(value, list):
        return [_sanitize_prose_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize_prose_value(v) for k, v in value.items()}
    return value


def _prose_strings(value) -> list:
    """Every string inside a prose structure, in document order."""
    found: list = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, list):
        for entry in value:
            found.extend(_prose_strings(entry))
    elif isinstance(value, dict):
        for entry in value.values():
            found.extend(_prose_strings(entry))
    return found


def _content_plain_text(content: dict, answer_texts=()) -> str:
    """The note's plain-text rendering (app.richtext.html_to_text):
    template answers first, then sections, then the free text."""
    parts = [richtext.html_to_text(t) for t in answer_texts]
    parts += [richtext.html_to_text(s)
              for s in _prose_strings((content or {}).get("sections"))]
    if isinstance((content or {}).get("text"), str):
        parts.append(richtext.html_to_text(content["text"]))
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def _note_fhir_plain_text(session: Session, content: dict) -> str:
    """Plain text for the FHIR Narrative: the stored (sanitized)
    QuestionnaireResponse prose answers + the note's own prose."""
    answer_texts: list = []
    response_id = (content or {}).get("questionnaireResponseId")
    if response_id:
        try:
            row = fhir._get_row("QuestionnaireResponse",
                                str(response_id), session)
        except HTTPException:
            row = None
        if row is not None:
            for item in (row.body or {}).get("item") or []:
                if not isinstance(item, dict):
                    continue
                for answer in item.get("answer") or []:
                    if isinstance(answer, dict) \
                            and isinstance(answer.get("valueString"), str):
                        answer_texts.append(answer["valueString"])
    return _content_plain_text(content, answer_texts)


def _narrative(plain_text: str) -> dict | None:
    """A FHIR Narrative whose div carries ONLY the plain-text
    rendering (escaped, paragraph-wrapped) — no author markup."""
    if not plain_text:
        return None
    inner = "".join(
        "<p>" + _html.escape(paragraph, quote=False).replace(
            "\n", "<br/>") + "</p>"
        for paragraph in plain_text.split("\n\n"))
    return {"status": "generated",
            "div": f'<div xmlns="http://www.w3.org/1999/xhtml">{inner}</div>'}


def _store_response(session: Session, user: User, form_key: str,
                    patient_ref: str, answers,
                    encounter_ref: str | None = None) -> dict:
    """Validate + store a QuestionnaireResponse against a PUBLISHED
    form (the generic surface's validator: unpublished form -> 422,
    missing required answers -> 422). Prose (rich/text) answers are
    sanitized on write before storage."""
    if not isinstance(answers, list):
        raise HTTPException(status_code=422,
                            detail="'answers' must be a list of QuestionnaireResponse items")
    answers = _sanitize_answers(session, form_key, answers)
    body = {
        "resourceType": "QuestionnaireResponse",
        "id": str(uuid.uuid4()),
        "status": "completed",
        "questionnaire": form_key,
        "subject": {"reference": patient_ref},
        "authored": _now_iso(),
        "author": {"display": user.username},
        "item": answers,
    }
    if encounter_ref:
        body["encounter"] = {"reference": encounter_ref}
    fhir._validate_questionnaire_response(session, body)
    return fhir.create_resource("QuestionnaireResponse", body, session, user)


def _note_content_of(body: dict) -> dict:
    for ext in (body or {}).get("extension") or []:
        if isinstance(ext, dict) and ext.get("url") == NOTE_CONTENT_EXTENSION:
            try:
                parsed = json.loads(ext.get("valueString") or "{}")
            except (TypeError, ValueError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
    return {}


def _decorate_note(row: FhirResource) -> dict:
    out = fhir._render(row)
    body = row.body or {}
    coding = ((body.get("type") or {}).get("coding") or [{}])[0]
    out["noteType"] = coding.get("code")
    content = _note_content_of(body)
    out["noteContent"] = content
    # Plain-text rendering for FHIR/print consumers: from the stored
    # Narrative when present (it was rendered from the full note,
    # answers included), else from the note's own prose.
    narrative_div = ((body.get("text") or {}).get("div")
                     if isinstance(body.get("text"), dict) else None)
    if narrative_div:
        out["plainText"] = richtext.html_to_text(narrative_div)
    else:
        out["plainText"] = _content_plain_text(content)
    return out


def _note_row(session: Session, note_id: str) -> FhirResource:
    return fhir._get_row("DocumentReference", note_id, session)


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

@notes_router.post("", status_code=201)
async def create_note(request: Request, session: Session = Depends(get_db),
                      user: User = Depends(require_capability("notes.write"))):
    """Author a draft note. The note type must exist (active) in the
    published note-types catalogue. When `answers` are supplied they
    are stored as a QuestionnaireResponse against the template form
    (the type's configured template, or an explicit `form_key`)."""
    payload = await _json_body(request)
    note_type = payload.get("note_type") or payload.get("noteType")
    if not isinstance(note_type, str) or not note_type:
        raise HTTPException(status_code=422,
                            detail="Note requires 'note_type' (a note-type code)")
    concept = _note_type_concept(session, note_type)
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown note type '{note_type}': not in the published "
                   "note-type catalogue")
    if concept.get("active") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Note type '{note_type}' is inactive in the published "
                   "note-type catalogue")
    patient_ref = _patient_ref_of(session, payload)
    encounter = payload.get("encounter") or {}
    encounter_ref = encounter.get("reference") if isinstance(encounter, dict) else None

    form_key = payload.get("form_key") or payload.get("formKey") \
        or concept.get("templateForm")
    response_id = None
    if payload.get("answers") is not None:
        if not form_key:
            raise HTTPException(
                status_code=422,
                detail=(f"Note type '{note_type}' has no template form; "
                        "answers cannot be attached"))
        rendered = _store_response(session, user, str(form_key), patient_ref,
                                   payload["answers"], encounter_ref)
        response_id = rendered["id"]

    content = {
        "templateForm": form_key,
        "questionnaireResponseId": response_id,
        "sections": (_sanitize_prose_value(payload.get("sections"))
                     if isinstance(payload.get("sections"), list) else []),
        "text": (richtext.sanitize_html(payload.get("text"))
                 if isinstance(payload.get("text"), str) else None),
    }
    body = {
        "resourceType": "DocumentReference",
        "id": str(uuid.uuid4()),
        "status": "current",
        "docStatus": "preliminary",
        "type": {"coding": [{"system": NOTE_TYPES_SYSTEM, "code": note_type,
                             "display": concept.get("display")}],
                 "text": concept.get("display")},
        "subject": {"reference": patient_ref},
        "date": _now_iso(),
        "author": [{"display": user.username}],
        "description": payload.get("title") or concept.get("display"),
        "content": [{"attachment": {"contentType": "application/opencare.note+json"}}],
        "extension": [{"url": NOTE_CONTENT_EXTENSION,
                       "valueString": json.dumps(content)}],
    }
    narrative = _narrative(_note_fhir_plain_text(session, content))
    if narrative is not None:
        body["text"] = narrative
    if encounter_ref:
        body["context"] = {"encounter": [{"reference": encounter_ref}]}
    rendered = fhir.create_resource("DocumentReference", body, session, user)
    row = _note_row(session, rendered["id"])
    append_audit(session, actor=user.username, action="notes.create",
                 entity=f"DocumentReference/{rendered['id']}",
                 payload={"note_type": note_type, "patient": patient_ref,
                          "template_form": form_key,
                          "questionnaire_response_id": response_id})
    session.commit()
    return _decorate_note(row)


@notes_router.get("")
def list_notes(patient: str = Query(...),
               session: Session = Depends(get_db),
               user: User = Depends(require_capability("notes.read"))):
    patient_ref = fhir._normalize_patient_ref(patient)
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "DocumentReference",
            FhirResource.patient_ref == patient_ref)
        .order_by(FhirResource.created_at.desc(), FhirResource.id.desc())).all()
    return {"items": [_decorate_note(r) for r in rows], "total": len(rows)}


@notes_router.get("/types")
def note_types(session: Session = Depends(get_db),
               user: User = Depends(require_capability("notes.read"))):
    cfg = published_config(session, "terminology", NOTE_TYPES_KEY)
    return {"note_types": cfg.payload if cfg else None,
            "version": cfg.version if cfg else None}


@notes_router.get("/{note_id}")
def get_note(note_id: str, session: Session = Depends(get_db),
             user: User = Depends(require_capability("notes.read"))):
    return _decorate_note(_note_row(session, note_id))



@notes_router.post("/{note_id}/sign")
def sign_note(note_id: str, session: Session = Depends(get_db),
              user: User = Depends(require_capability("notes.write"))):
    """Sign a draft note: docStatus preliminary -> final, with the
    authenticator recorded. A signed note is immutable through this
    API; corrections go through amend."""
    row = _note_row(session, note_id)
    body = dict(row.body or {})
    if body.get("docStatus") != "preliminary":
        raise HTTPException(
            status_code=409,
            detail=(f"Note {note_id} has docStatus "
                    f"'{body.get('docStatus')}', not 'preliminary'; only a "
                    "draft can be signed"))
    body["docStatus"] = "final"
    body["authenticator"] = {"display": user.username}
    row.body = body
    row.version += 1
    session.flush()
    append_audit(session, actor=user.username, action="notes.sign",
                 entity=f"DocumentReference/{note_id}",
                 payload={"signed_by": user.username, "version": row.version})
    session.commit()
    return _decorate_note(row)


@notes_router.post("/{note_id}/amend", status_code=201)
async def amend_note(note_id: str, request: Request,
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability("notes.write"))):
    """Amend a SIGNED note: creates a new DocumentReference with
    docStatus 'amended' and relatesTo: replaces -> the original. The
    original row is never written to — its stored version is part of
    the test evidence. New content comes from the payload when given
    (answers/sections/text), else it is copied from the original."""
    payload = await _json_body(request)
    original = _note_row(session, note_id)
    original_body = original.body or {}
    if original_body.get("docStatus") not in ("final", "amended"):
        raise HTTPException(
            status_code=409,
            detail=(f"Note {note_id} has docStatus "
                    f"'{original_body.get('docStatus')}'; only a signed "
                    "(final) note can be amended — a draft is signed "
                    "first or left as-is"))
    patient_ref = original.patient_ref or ""
    old_content = _note_content_of(original_body)
    form_key = payload.get("form_key") or payload.get("formKey") \
        or old_content.get("templateForm")

    response_id = old_content.get("questionnaireResponseId")
    if payload.get("answers") is not None:
        if not form_key:
            raise HTTPException(
                status_code=422,
                detail="This note has no template form; answers cannot be attached")
        encounter = payload.get("encounter") or {}
        encounter_ref = encounter.get("reference") if isinstance(encounter, dict) else None
        rendered = _store_response(session, user, str(form_key), patient_ref,
                                   payload["answers"], encounter_ref)
        response_id = rendered["id"]

    content = {
        "templateForm": form_key,
        "questionnaireResponseId": response_id,
        "sections": (_sanitize_prose_value(payload.get("sections"))
                     if isinstance(payload.get("sections"), list)
                     else _sanitize_prose_value(
                         old_content.get("sections") or [])),
        "text": (richtext.sanitize_html(payload.get("text"))
                 if isinstance(payload.get("text"), str)
                 else (richtext.sanitize_html(old_content["text"])
                       if isinstance(old_content.get("text"), str)
                       else None)),
    }
    body = {
        "resourceType": "DocumentReference",
        "id": str(uuid.uuid4()),
        "status": "current",
        "docStatus": "amended",
        "type": original_body.get("type"),
        "subject": original_body.get("subject"),
        "date": _now_iso(),
        "author": [{"display": user.username}],
        "description": payload.get("title") or original_body.get("description"),
        "content": [{"attachment": {"contentType": "application/opencare.note+json"}}],
        "relatesTo": [{"code": "replaces",
                       "target": {"reference": f"DocumentReference/{note_id}"}}],
        "extension": [{"url": NOTE_CONTENT_EXTENSION,
                       "valueString": json.dumps(content)}],
    }
    narrative = _narrative(_note_fhir_plain_text(session, content))
    if narrative is not None:
        body["text"] = narrative
    if original_body.get("context"):
        body["context"] = original_body["context"]
    if original_body.get("authenticator"):
        body["authenticator"] = original_body["authenticator"]
    rendered = fhir.create_resource("DocumentReference", body, session, user)
    row = _note_row(session, rendered["id"])
    append_audit(session, actor=user.username, action="notes.amend",
                 entity=f"DocumentReference/{rendered['id']}",
                 payload={"amends": note_id, "patient": patient_ref,
                          "questionnaire_response_id": response_id})
    session.commit()
    return _decorate_note(row)


# ---------------------------------------------------------------------------
# Flowsheets: QuestionnaireResponse collections against published forms
# ---------------------------------------------------------------------------

def _flatten_items(items) -> list[dict]:
    """Form items with one level of group nesting flattened (groups
    contribute their children, not a row of their own)."""
    out: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "group" and isinstance(item.get("item"), list):
            out.extend(_flatten_items(item["item"]))
        elif item.get("type") != "display":
            out.append(item)
    return out


def _answer_value(answer: dict):
    """Normalise one FHIR answer into (value, unit) plain scalars."""
    quantity = answer.get("valueQuantity")
    if isinstance(quantity, dict):
        return quantity.get("value"), quantity.get("unit")
    coding = answer.get("valueCoding")
    if isinstance(coding, dict):
        return coding.get("display") or coding.get("code"), None
    for field in ("valueDecimal", "valueInteger", "valueBoolean",
                  "valueString", "valueDate", "valueDateTime", "valueTime"):
        if field in answer:
            return answer.get(field), None
    return None, None


@flowsheets_router.post("", status_code=201)
async def submit_flowsheet(request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("notes.write"))):
    """Record one flowsheet entry: a QuestionnaireResponse against a
    PUBLISHED form (e.g. form/flowsheet-vitals). Validation is the
    generic surface's — an unpublished form or a missing required
    answer is a 422."""
    payload = await _json_body(request)
    form_key = payload.get("form") or payload.get("form_key") or payload.get("formKey")
    if not isinstance(form_key, str) or not form_key:
        raise HTTPException(status_code=422,
                            detail="Flowsheet entry requires 'form' (the form key)")
    patient_ref = _patient_ref_of(session, payload)
    encounter = payload.get("encounter") or {}
    encounter_ref = encounter.get("reference") if isinstance(encounter, dict) else None
    rendered = _store_response(session, user, form_key, patient_ref,
                               payload.get("answers"), encounter_ref)
    append_audit(session, actor=user.username, action="flowsheets.submit",
                 entity=f"QuestionnaireResponse/{rendered['id']}",
                 payload={"form": form_key, "patient": patient_ref})
    session.commit()
    return rendered


@flowsheets_router.get("/{form_key}")
def flowsheet_view(form_key: str, patient: str = Query(...),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("notes.read"))):
    """Time-series view of one flowsheet form for one patient: the
    form's items (rows) and one entry per recorded response (columns),
    values normalised to scalars."""
    form = published_config(session, "form", form_key)
    if form is None:
        raise HTTPException(status_code=404,
                            detail=f"Form '{form_key}' is not published in Studio")
    patient_ref = fhir._normalize_patient_ref(patient)
    items = _flatten_items((form.payload or {}).get("item"))
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "QuestionnaireResponse",
            FhirResource.patient_ref == patient_ref)
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    entries = []
    for row in rows:
        body = row.body or {}
        ref = str(body.get("questionnaire") or "")
        if ref.rstrip("/").rsplit("/", 1)[-1] != form_key:
            continue
        answers: dict[str, dict] = {}
        for item in body.get("item") or []:
            if not isinstance(item, dict) or not item.get("linkId"):
                continue
            answer_list = item.get("answer") or []
            if answer_list and isinstance(answer_list[0], dict):
                value, unit = _answer_value(answer_list[0])
                answers[item["linkId"]] = {"value": value, "unit": unit}
        entries.append({"id": row.resource_id,
                        "authored": body.get("authored"),
                        "answers": answers})
    return {
        "form_key": form_key,
        "patient": patient_ref,
        "form": {"title": (form.payload or {}).get("title"),
                 "items": [{"linkId": i.get("linkId"), "text": i.get("text"),
                            "type": i.get("type"),
                            "required": bool(i.get("required")),
                            "rich": i.get("rich") is True}
                           for i in items]},
        "entries": entries,
        "total": len(entries),
    }

