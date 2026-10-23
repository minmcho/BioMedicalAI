"""Phase 16, WS-G2 / BD-48 — inbound fax / document AI intake.

Documents (faxes, scanned referrals, external reports) land in a
GOVERNED intake queue — never directly in a chart:

1. **Receive** (`POST /intake/documents`, JSON base64 body — chosen
   over multipart for test simplicity, per the Phase 16 contracts):
   the exact bytes are stored with their SHA-256. Storage reuses
   **app.tiering's cold store** (the content-addressed
   ``FilesystemColdStore``: ``put(sha256, bytes) -> cold://<sha256>``,
   byte-exact for binary payloads). app.lake's raw zone was NOT used:
   ``LakeRawEntry.payload_text`` is a text column, so binary PDFs
   cannot round-trip through it byte-exactly; the tiering cold store
   is the one existing raw store that takes bytes. It is used here
   purely as a content-addressed byte store — no tiering POLICY is
   run and no lake row is moved. A second submission of the same
   bytes (same sha256) is refused **409** naming the existing intake
   document — the same fax arriving twice is one document.
2. **Extract**: ``text/plain`` decodes directly; ``application/pdf``
   extracts text via **pypdf, import-guarded** — if pypdf is missing
   (or the PDF cannot be parsed, or the media type is unsupported)
   the document parks in ``extraction-failed`` with the reason
   RECORDED on the row, never silently.
3. **Classify**: the seeded DRAFT Studio agent
   ``document-intake-classifier`` (seeded via
   ``app.agents._seed_draft``, so it follows the ordinary agent
   governance: a site reviews it, publishes it, enables it) is run
   through the app.agents run machinery (``app.agents._post_run``
   transport + an ``AgentRun`` record + the ``agents.run`` audit) and
   its ``{patientRef, docType}`` output is stored as a SUGGESTION
   ONLY. Honest-off like every agent surface: no published/enabled
   agent, a runtime 503 (no model), an unreachable runtime, or an
   unparseable suggestion all park the document in
   ``awaiting-classification`` — nothing is fabricated, no run is
   recorded for a 503 — and a human may classify manually via
   ``POST /intake/documents/{id}/classify``.
4. **Confirm / file** (`POST .../confirm`): a HUMAN confirms the
   patient + type and only then is a real FHIR ``DocumentReference``
   created in that patient's chart through the FHIR store
   (app.fhir.create_resource — the same path app.documentation uses),
   carrying the intake provenance extension (intake id + classifier
   run id + sha256). Misfile safeguard: if the confirmed patient
   differs from the classifier's suggestion, a ``reason`` is REQUIRED
   (422 without one).
5. **Unfile** (`POST .../unfile`): the misfile correction path — the
   filed DocumentReference is marked ``entered-in-error`` and the
   intake document returns to ``classified`` so it can be re-filed
   correctly. Nothing is ever deleted.

Every transition writes a hash-chained audit entry (``intake.*``) in
the same transaction as the change. Live fax hardware / carrier
integration is NOT RUN — documents arrive through this API only.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import (DateTime, Integer, String, Text, select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import agents as agents_module
from . import fhir
from . import tiering
from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import AgentRun, ConfigStatus, FhirResource, StudioConfig, User, utcnow
from .security import get_db, require_capability

router = APIRouter(prefix="/intake", tags=["intake"])

CLASSIFIER_KEY = "document-intake-classifier"
DOC_TYPES_KEY = "intake-doc-types"          # terminology/intake-doc-types
PROVENANCE_EXTENSION = "urn:opencare:extension/intake-provenance"
INTAKE_SYSTEM = "urn:opencare:intake"
DOC_TYPE_SYSTEM = "urn:opencare:intake-doc-type"
STATUSES = ("received", "extracted", "awaiting-classification",
            "classified", "filed", "extraction-failed", "unfiled")
# Prompt excerpt cap: the classifier sees the head of the document —
# enough to identify patient + type without unbounded run payloads.
_PROMPT_TEXT_LIMIT = 8000


# ---------------------------------------------------------------------------
# This module's own table
# ---------------------------------------------------------------------------

class IntakeDocument(Base):
    """One inbound document in the governed intake queue.

    The bytes themselves live in the tiering cold store under their
    sha256 (``storage_ref`` = ``cold://<sha256>``); this row is the
    workflow state: extraction outcome, the classifier's SUGGESTION
    (never an instruction), and — only after human confirmation — the
    chart DocumentReference it was filed as."""
    __tablename__ = "intake_documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    intake_id: Mapped[str] = mapped_column(String(120), unique=True,
                                           nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(300), nullable=False,
                                          default="")
    sha256: Mapped[str] = mapped_column(String(64), nullable=False,
                                        unique=True, index=True)
    storage_ref: Mapped[str] = mapped_column(String(300), nullable=False,
                                             default="")
    media_type: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False,
                                        default="received", index=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_patient_ref: Mapped[str | None] = mapped_column(
        String(200), nullable=True)
    suggested_type: Mapped[str | None] = mapped_column(String(120),
                                                       nullable=True)
    classifier_run_id: Mapped[str | None] = mapped_column(String(120),
                                                           nullable=True)
    classification_note: Mapped[str | None] = mapped_column(Text,
                                                            nullable=True)
    confirmed_patient_ref: Mapped[str | None] = mapped_column(
        String(200), nullable=True)
    filed_document_id: Mapped[str | None] = mapped_column(String(120),
                                                          nullable=True)
    filed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    received_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


# ---------------------------------------------------------------------------
# Governed configuration seeds
# ---------------------------------------------------------------------------

def default_classifier_payload() -> dict:
    """The document-intake classifier agent definition (seeded DRAFT
    v1, empty model route — the agents.py seeded pattern). Its ONLY
    job is to suggest a patient + document type as JSON; the output is
    stored as a suggestion and a human confirms before filing."""
    return {
        "description": ("Phase 16 (BD-48): suggests a patient match and "
                        "document type for an inbound fax/document in "
                        "the intake queue, from the extracted text. "
                        "SUGGESTION ONLY — a human confirms patient + "
                        "type before anything files to a chart, and a "
                        "confirmation naming a different patient than "
                        "the suggestion requires a recorded reason."),
        "model": "",
        "instructions": (
            "You are the OpenCare document intake classifier. Given "
            "the extracted text of ONE inbound document, reply with a "
            "single JSON object and nothing else: "
            "{\"patientRef\": \"Patient/<id>\", \"docType\": \"<type>\"} "
            "where docType is one of: lab-report, radiology-report, "
            "discharge-summary, referral-letter, operative-note, "
            "other. Use ONLY identifiers that appear verbatim in the "
            "text (a patient reference, MRN or full name as printed). "
            "If the text does not identify a patient, use an empty "
            "string for patientRef — never guess or invent a patient."),
        "tools": ["opencare_api_call"],
        "apiAllowlist": ["/fhir"],
        "modalities": {"text": True, "image": False, "audio": False},
        "temperature": 0.0,
        "maxSteps": 4,
    }


def default_doc_types_payload() -> dict:
    def dtype(code, display):
        return {"code": code, "display": display, "active": True}

    return {
        "name": "Intake document types",
        "system": DOC_TYPE_SYSTEM,
        "concepts": [
            dtype("lab-report", "Laboratory report"),
            dtype("radiology-report", "Radiology report"),
            dtype("discharge-summary", "Discharge summary"),
            dtype("referral-letter", "Referral letter"),
            dtype("operative-note", "Operative note"),
            dtype("other", "Other document"),
        ],
    }


def _seed_published(session: Session, kind: str, key: str,
                    payload: dict) -> None:
    if list_config_versions(session, kind, key):
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


def ensure_doc_intake_defaults(session: Session) -> None:
    """Seed the classifier agent as a DRAFT (via agents._seed_draft —
    never self-published, never enabled) and the intake document-type
    catalogue as published terminology v1. Idempotent."""
    agents_module._seed_draft(session, CLASSIFIER_KEY,
                              default_classifier_payload())
    _seed_published(session, "terminology", DOC_TYPES_KEY,
                    default_doc_types_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422,
                            detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422,
                            detail="Request body must be a JSON object")
    return body


async def _optional_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _doc_out(doc: IntakeDocument) -> dict:
    return {
        "id": doc.intake_id,
        "intake_id": doc.intake_id,
        "filename": doc.filename,
        "media_type": doc.media_type,
        "mediaType": doc.media_type,
        "sha256": doc.sha256,
        "storage_ref": doc.storage_ref,
        "storageRef": doc.storage_ref,
        "status": doc.status,
        "extracted_text": doc.extracted_text,
        "extraction_error": doc.extraction_error,
        "suggested_patient_ref": doc.suggested_patient_ref,
        "suggestedPatientRef": doc.suggested_patient_ref,
        "suggested_type": doc.suggested_type,
        "suggestedType": doc.suggested_type,
        "classifier_run_id": doc.classifier_run_id,
        "classifierRunId": doc.classifier_run_id,
        "classification_note": doc.classification_note,
        "confirmed_patient_ref": doc.confirmed_patient_ref,
        "confirmedPatientRef": doc.confirmed_patient_ref,
        "filed_document_id": doc.filed_document_id,
        "filedDocumentId": doc.filed_document_id,
        "filed_by": doc.filed_by,
        "filedBy": doc.filed_by,
        "received_by": doc.received_by,
        "created_at": _aware(doc.created_at).isoformat()
        if doc.created_at else None,
        "updated_at": _aware(doc.updated_at).isoformat()
        if doc.updated_at else None,
    }


def _doc_row(session: Session, doc_id: str) -> IntakeDocument:
    doc = session.scalars(select(IntakeDocument).where(
        IntakeDocument.intake_id == doc_id)).first()
    if doc is None and str(doc_id).isdigit():
        doc = session.get(IntakeDocument, int(doc_id))
    if doc is None:
        raise HTTPException(status_code=404,
                            detail=f"Intake document '{doc_id}' not found")
    return doc


def _normalise_patient_ref(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    return value if "/" in value else f"Patient/{value}"


def _require_patient_ref(payload: dict) -> str:
    ref = _normalise_patient_ref(
        payload.get("patientRef") or payload.get("patient_ref")
        or payload.get("patient"))
    if ref is None or not ref.startswith("Patient/"):
        raise HTTPException(
            status_code=422,
            detail="Requires 'patientRef' naming a Patient (e.g. 'Patient/pat-1')")
    return ref


def _require_doc_type(payload: dict) -> str:
    value = payload.get("docType") or payload.get("doc_type") \
        or payload.get("type")
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422,
                            detail="Requires 'docType' (the document type code)")
    return value.strip()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _extract_text(media_type: str, data: bytes) -> tuple[str | None, str | None]:
    """(text, error) — exactly one is not None. pypdf is import-guarded:
    its absence is a RECORDED extraction failure, never a silent park
    and never a crash."""
    mt = (media_type or "").split(";")[0].strip().lower()
    if mt == "text/plain":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            return None, f"text/plain payload is not valid UTF-8: {exc}"
        # UX pass: the web intake editor submits sanitized HTML for
        # prose documents. The stored bytes stay exactly as received;
        # the EXTRACTED text (classifier input, search) is the
        # plain-text rendering, so editor markup never pollutes
        # classification. Only editor-shaped payloads (whitelisted
        # block tags present) are converted — plain text is untouched.
        lowered = text.lower()
        if any(tag in lowered for tag in
               ("<p>", "<p ", "<h2", "<h3", "<ul", "<ol", "<blockquote")):
            from app.richtext import html_to_text
            text = html_to_text(text)
        return text, None
    if mt == "application/pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            return None, (f"pypdf is not installed, so PDF text "
                          f"extraction is unavailable: {exc}")
        try:
            reader = PdfReader(io.BytesIO(data))
            parts = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    parts.append(text)
            return "\n".join(parts), None
        except Exception as exc:  # malformed PDF: recorded, not hidden
            return None, f"PDF text extraction failed: {exc}"
    return None, (f"Unsupported media type '{media_type}': extraction "
                  "supports text/plain and application/pdf only")


# ---------------------------------------------------------------------------
# Classification — the governed agent, suggestion only
# ---------------------------------------------------------------------------

def _parse_suggestion(output: str) -> dict | None:
    """The classifier contract is a single JSON object; tolerate a
    fenced/prose-wrapped object by taking the first {...} span.
    Anything else is unparseable -> the document parks (no guess is
    ever manufactured here)."""
    if not output:
        return None
    candidates = [output]
    match = re.search(r"\{.*\}", output, flags=re.DOTALL)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _attempt_classification(session: Session, doc: IntakeDocument,
                            user: User, api_token: str) -> None:
    """Run the classifier if governance allows it; otherwise park.

    Mirrors app.agents.run_agent's gates exactly: no published
    version or not enabled -> park (the agent does not run); runtime
    unreachable / runtime 503 (no model) -> park, NOTHING recorded as
    a run (the agents.py honest-off rule). A successful run records
    an AgentRun + the agents.run audit (the run machinery), and only
    its parsed suggestion is stored — on the intake row, labelled a
    suggestion."""
    def park(note: str, action: str = "intake.classification_deferred"):
        doc.status = "awaiting-classification"
        doc.classification_note = note
        session.flush()
        append_audit(session, actor=user.username, action=action,
                     entity=f"IntakeDocument/{doc.intake_id}",
                     payload={"intake_id": doc.intake_id, "note": note})

    cfg = published_config(session, "agent", CLASSIFIER_KEY)
    if cfg is None:
        park(f"Classifier agent '{CLASSIFIER_KEY}' has no published "
             "version (it is seeded as a draft); a human may classify "
             "manually")
        return
    if not agents_module._is_enabled(session, CLASSIFIER_KEY):
        park(f"Classifier agent '{CLASSIFIER_KEY}' is published but "
             "not enabled; a human may classify manually")
        return
    # C-AI runtime gate (AI-G4): a governance refusal (expired
    # record/statement, suspension, kill switch) parks exactly like
    # the not-enabled path — no classification is generated.
    _refusal = agents_module.runtime_refusal(session, CLASSIFIER_KEY)
    if _refusal:
        park(_refusal)
        return
    payload = dict(cfg.payload or {})
    prompt = (
        "Classify this inbound document. Extracted text follows.\n"
        f"Filename: {doc.filename}\nMedia type: {doc.media_type}\n"
        "---\n" + (doc.extracted_text or "")[:_PROMPT_TEXT_LIMIT])
    runtime_payload = {
        "agent": {**payload, "key": CLASSIFIER_KEY},
        "prompt": prompt,
        "apiBaseUrl": agents_module.api_base_url(),
        "apiToken": api_token,
    }
    try:
        status, result = agents_module._post_run(runtime_payload)
    except httpx.HTTPError:
        park(f"Agent runtime unavailable at {agents_module.runtime_url()}; "
             "no classification was generated")
        return
    if status == 503:
        park(str(result.get("error") or
                 "Agent runtime unavailable (no model configured); no "
                 "classification was generated"))
        return
    if status != 200:
        park(f"Agent runtime returned {status}: "
             f"{result.get('error') or 'unknown error'}")
        return
    output = str(result.get("output") or "")
    suggestion = _parse_suggestion(output)
    if suggestion is None:
        park("Classifier output was not a parseable "
             "{patientRef, docType} JSON object; a human may classify "
             "manually")
        return
    # Record the run exactly as app.agents.run_agent does.
    input_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    run = AgentRun(run_id=str(uuid.uuid4()), agent_key=CLASSIFIER_KEY,
                   agent_version=cfg.version, actor=user.username,
                   patient_ref=_normalise_patient_ref(
                       suggestion.get("patientRef")
                       or suggestion.get("patient_ref")),
                   input_hash=input_hash, output=output,
                   model=result.get("model"))
    session.add(run)
    session.flush()
    append_audit(session, actor=user.username, action="agents.run",
                 entity=f"agent:{CLASSIFIER_KEY}@v{cfg.version}",
                 payload={"agent": CLASSIFIER_KEY, "version": cfg.version,
                          "run_id": run.run_id, "input_hash": input_hash,
                          "patient": run.patient_ref,
                          "model": result.get("model"),
                          "output": output, "disposition": "pending",
                          "intake_id": doc.intake_id})
    patient_ref = _normalise_patient_ref(
        suggestion.get("patientRef") or suggestion.get("patient_ref"))
    doc_type = suggestion.get("docType") or suggestion.get("doc_type") \
        or suggestion.get("type")
    doc.suggested_patient_ref = patient_ref
    doc.suggested_type = str(doc_type).strip() if doc_type else None
    doc.classifier_run_id = run.run_id
    doc.classification_note = None
    doc.status = "classified"
    session.flush()
    append_audit(session, actor=user.username, action="intake.classified",
                 entity=f"IntakeDocument/{doc.intake_id}",
                 payload={"intake_id": doc.intake_id,
                          "suggested_patient_ref": doc.suggested_patient_ref,
                          "suggested_type": doc.suggested_type,
                          "classifier_run_id": run.run_id,
                          "source": "classifier-agent"})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.post("/documents", status_code=201)
async def receive_document(request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("intake.work"))):
    """Receive one inbound document: {filename, mediaType,
    contentBase64}. Bytes are hash-stored (dedupe 409 on the same
    sha256), text is extracted, and classification is attempted
    through the governed classifier agent — the response is the
    intake record in whatever state it honestly reached (filed is
    NEVER a receive outcome: filing needs human confirmation)."""
    payload = await _json_body(request)
    filename = payload.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        raise HTTPException(status_code=422, detail="'filename' is required")
    media_type = payload.get("mediaType") or payload.get("media_type")
    if not isinstance(media_type, str) or not media_type.strip():
        raise HTTPException(status_code=422, detail="'mediaType' is required")
    encoded = payload.get("contentBase64") or payload.get("content_base64")
    if not isinstance(encoded, str) or not encoded.strip():
        raise HTTPException(status_code=422,
                            detail="'contentBase64' is required")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422,
                            detail="'contentBase64' is not valid base64")
    if not data:
        raise HTTPException(status_code=422,
                            detail="'contentBase64' decodes to zero bytes")
    digest = hashlib.sha256(data).hexdigest()
    existing = session.scalars(select(IntakeDocument).where(
        IntakeDocument.sha256 == digest)).first()
    if existing is not None:
        return JSONResponse(status_code=409, content={
            "detail": (f"Duplicate document: sha256 {digest} was already "
                       f"received as intake document {existing.intake_id}"),
            "id": existing.intake_id,
            "existing_id": existing.intake_id,
            "document": _doc_out(existing)})
    store = tiering.default_cold_store()
    storage_ref = store.put(digest, data, metadata={
        "intake_filename": filename.strip(), "media_type": media_type.strip(),
        "content_hash": digest, "source": "doc-intake"})
    doc = IntakeDocument(
        intake_id=str(uuid.uuid4()), filename=filename.strip(),
        sha256=digest, storage_ref=storage_ref,
        media_type=media_type.strip(), status="received",
        received_by=user.username)
    session.add(doc)
    session.flush()
    append_audit(session, actor=user.username, action="intake.received",
                 entity=f"IntakeDocument/{doc.intake_id}",
                 payload={"intake_id": doc.intake_id,
                          "filename": doc.filename,
                          "media_type": doc.media_type, "sha256": digest,
                          "storage_ref": storage_ref, "bytes": len(data)})
    text, error = _extract_text(doc.media_type, data)
    if error is not None:
        doc.status = "extraction-failed"
        doc.extraction_error = error
        session.flush()
        append_audit(session, actor=user.username,
                     action="intake.extraction_failed",
                     entity=f"IntakeDocument/{doc.intake_id}",
                     payload={"intake_id": doc.intake_id, "reason": error})
        session.commit()
        return _doc_out(doc)
    doc.extracted_text = text
    doc.status = "extracted"
    session.flush()
    append_audit(session, actor=user.username, action="intake.extracted",
                 entity=f"IntakeDocument/{doc.intake_id}",
                 payload={"intake_id": doc.intake_id,
                          "chars": len(text or "")})
    token = request.headers.get("Authorization", "").removeprefix(
        "Bearer ").strip()
    _attempt_classification(session, doc, user, token)
    session.commit()
    return _doc_out(doc)


@router.get("/documents")
def list_documents(status: str | None = Query(default=None),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("intake.work"))):
    stmt = select(IntakeDocument).order_by(IntakeDocument.id.desc())
    if status:
        stmt = stmt.where(IntakeDocument.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_doc_out(d) for d in rows], "total": len(rows)}


@router.get("/documents/{doc_id}")
def get_document(doc_id: str, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("intake.work"))):
    return _doc_out(_doc_row(session, doc_id))


@router.post("/documents/{doc_id}/classify")
async def classify_manually(doc_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("intake.work"))):
    """Human classification: {patientRef, docType}. The human's call
    becomes the operative suggestion the confirmation is checked
    against (a matching confirmation then needs no override reason).
    A filed document cannot be re-classified — unfile it first."""
    payload = await _json_body(request)
    doc = _doc_row(session, doc_id)
    if doc.status == "filed":
        raise HTTPException(
            status_code=409,
            detail=(f"Intake document {doc_id} is already filed as "
                    f"DocumentReference/{doc.filed_document_id}; unfile "
                    "it before re-classifying"))
    patient_ref = _require_patient_ref(payload)
    doc_type = _require_doc_type(payload)
    previous = doc.status
    doc.suggested_patient_ref = patient_ref
    doc.suggested_type = doc_type
    doc.classification_note = None
    doc.status = "classified"
    session.flush()
    append_audit(session, actor=user.username, action="intake.classify",
                 entity=f"IntakeDocument/{doc.intake_id}",
                 payload={"intake_id": doc.intake_id, "from_status": previous,
                          "suggested_patient_ref": patient_ref,
                          "suggested_type": doc_type, "source": "manual"})
    session.commit()
    return _doc_out(doc)


@router.post("/documents/{doc_id}/confirm")
async def confirm_and_file(doc_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("intake.work"))):
    """HUMAN confirmation + filing: {patientRef, docType, reason?}.

    Files a real DocumentReference into the confirmed patient's chart
    via the FHIR store, with the intake provenance extension. If the
    confirmed patient differs from the suggestion, `reason` is
    REQUIRED (422 without) — the misfile safeguard."""
    payload = await _json_body(request)
    doc = _doc_row(session, doc_id)
    if doc.status == "filed":
        raise HTTPException(
            status_code=409,
            detail=(f"Intake document {doc_id} is already filed as "
                    f"DocumentReference/{doc.filed_document_id}"))
    if doc.status != "classified":
        raise HTTPException(
            status_code=409,
            detail=(f"Intake document {doc_id} is in status "
                    f"'{doc.status}'; it must be classified (by the "
                    "classifier agent or manually) before a human can "
                    "confirm filing"))
    patient_ref = _require_patient_ref(payload)
    doc_type = _require_doc_type(payload)
    reason = payload.get("reason")
    reason = reason.strip() if isinstance(reason, str) else None
    if doc.suggested_patient_ref and patient_ref != doc.suggested_patient_ref \
            and not reason:
        raise HTTPException(
            status_code=422,
            detail=(f"Confirmed patient {patient_ref} differs from the "
                    f"suggested patient {doc.suggested_patient_ref}; a "
                    "'reason' is required to file against a different "
                    "patient than the classification suggested"))
    # 404 if the patient does not exist — never file into a void.
    fhir._get_row("Patient", patient_ref.split("/", 1)[1], session)
    provenance = {
        "intakeId": doc.intake_id,
        "classifierRunId": doc.classifier_run_id,
        "sha256": doc.sha256,
        "storageRef": doc.storage_ref,
        "suggestedPatientRef": doc.suggested_patient_ref,
        "suggestedType": doc.suggested_type,
    }
    body = {
        "resourceType": "DocumentReference",
        "id": str(uuid.uuid4()),
        "status": "current",
        "docStatus": "final",
        "type": {"coding": [{"system": DOC_TYPE_SYSTEM, "code": doc_type,
                             "display": doc_type}],
                 "text": doc_type},
        "subject": {"reference": patient_ref},
        "date": datetime.now(timezone.utc).isoformat(),
        "author": [{"display": user.username}],
        "description": doc.filename,
        "identifier": [{"system": INTAKE_SYSTEM, "value": doc.intake_id}],
        "content": [{"attachment": {
            "contentType": doc.media_type,
            "url": doc.storage_ref,
            "title": doc.filename,
            "hash": base64.b64encode(
                bytes.fromhex(doc.sha256)).decode("ascii"),
        }}],
        "extension": [{"url": PROVENANCE_EXTENSION,
                       "valueString": json.dumps(provenance)}],
    }
    rendered = fhir.create_resource("DocumentReference", body, session, user)
    doc.status = "filed"
    doc.confirmed_patient_ref = patient_ref
    doc.filed_document_id = rendered["id"]
    doc.filed_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="intake.filed",
                 entity=f"IntakeDocument/{doc.intake_id}",
                 payload={"intake_id": doc.intake_id,
                          "document_reference": rendered["id"],
                          "patient": patient_ref, "doc_type": doc_type,
                          "suggested_patient_ref": doc.suggested_patient_ref,
                          "override_reason": reason,
                          "classifier_run_id": doc.classifier_run_id})
    session.commit()
    return _doc_out(doc)


@router.post("/documents/{doc_id}/unfile")
async def unfile_document(doc_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("intake.work"))):
    """Misfile correction: mark the filed DocumentReference
    entered-in-error (NEVER delete it) and return the intake document
    to `classified`, ready to be re-filed to the right chart. The
    entered-in-error resource stays readable in the wrong chart's
    history — that is the safeguard working as FHIR intends."""
    payload = await _optional_body(request)
    doc = _doc_row(session, doc_id)
    if doc.status != "filed" or not doc.filed_document_id:
        raise HTTPException(
            status_code=409,
            detail=(f"Intake document {doc_id} is in status "
                    f"'{doc.status}'; only a filed document can be "
                    "unfiled"))
    reason = payload.get("reason")
    reason = reason.strip() if isinstance(reason, str) else None
    row = fhir._get_row("DocumentReference", doc.filed_document_id, session)
    body = dict(row.body or {})
    body["status"] = "entered-in-error"
    row.body = body
    row.version += 1
    row.status = "entered-in-error"
    session.flush()
    append_audit(session, actor=user.username, action="fhir.update",
                 entity=f"DocumentReference/{doc.filed_document_id}",
                 payload={"resource_type": "DocumentReference",
                          "version": row.version,
                          "changed_fields": ["status"],
                          "entered_in_error_via": doc.intake_id})
    filed_id = doc.filed_document_id
    doc.status = "classified"
    session.flush()
    append_audit(session, actor=user.username, action="intake.unfiled",
                 entity=f"IntakeDocument/{doc.intake_id}",
                 payload={"intake_id": doc.intake_id,
                          "document_reference": filed_id,
                          "marked": "entered-in-error",
                          "reason": reason,
                          "was_patient": doc.confirmed_patient_ref})
    session.commit()
    return _doc_out(doc)
