"""Phase 16 (WS-E, Scope F) — radiology vision-model agentic assist.

A governed multimodal copilot over Phase 15's DICOM pixel pipeline
(app.radiology + app.tiering's cold store). The radiologist disposes;
the agent only drafts. There is deliberately NO code path in this
module that verifies a report or moves one past `preliminary`:

- the seeded agent `radiology-vision-assistant` is a governed Studio
  DRAFT (kind=agent, modalities {image: true}, EMPTY model route),
  seeded through app.agents._seed_draft from
  ensure_imaging_assist_defaults — a site reviews it, sets a model
  route, publishes and enables it through the ordinary agent
  governance before it can run at all;
- POST /imaging-assist/studies/{study_id}/draft loads the study's
  archived DICOM instances through app.radiology's instance table +
  the tiering cold store, VERIFIES each object's SHA-256 against the
  instance row (the WADO-RS precedent: unverified bytes are never
  used), converts pixel data to PNG bytes with pydicom + the standard
  library only, and runs the agent through app.agents' own run
  machinery with the first converted frame as the image part. A frame
  that cannot be converted is reported in the response as skipped —
  never replaced with an invented image. A study with NO convertible
  frames is a 422 with the reasons. Honest-off: when the agent
  runtime is down or has no model, app.agents' 503 surfaces verbatim
  and NO ImagingAssistDraft row is written;
- the output persists as an ImagingAssistDraft (findings / impression
  / protocol suggestion / triage suggestion / measurements, all from
  the ONE run's structured sections). Disposition rides the EXISTING
  flow: the underlying AgentRun is accepted/rejected through
  POST /agents/runs/{id}/disposition, and this module's draft status
  follows that disposition (draft -> accepted / rejected; the read
  path syncs it). POST /imaging-assist/drafts/{id}/accept-into-report
  dispositions a still-pending run as accepted through that same
  machinery, then creates a PRELIMINARY DiagnosticReport through
  boards' own imaging-report creation (the radiology report flow) —
  verification stays boards' /radiology/reports/{id}/verify, behind
  the radiology.verify capability, exactly as before;
- prior comparison: when an earlier study for the same patient has a
  report, that report's text is included in the prompt context and
  the response names the prior study id used.

A live vision-model run is NOT RUN (no model endpoint is configured
in this environment); the test suite substitutes the agent transport,
which proves plumbing only. Synthetic data only; no real PHI.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import struct
import zlib

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import (Column, DateTime, Integer, JSON, String, Text,
                        select)
from sqlalchemy.orm import Session
from starlette.requests import Request as StarletteRequest

from . import agents, boards, fhir, radiology
from .audit import append_audit
from .db import Base
from .models import AgentRun, User, utcnow
from .security import get_db, require_capability
from .tiering import default_cold_store

try:
    import pydicom
except ImportError:  # pragma: no cover
    pydicom = None

router = APIRouter(prefix="/imaging-assist", tags=["imaging-assist"])

AGENT_KEY = "radiology-vision-assistant"

# Uncompressed transfer syntaxes whose PixelData this module decodes
# byte-wise: Explicit VR Little Endian and Implicit VR Little Endian.
# Anything else (encapsulated / compressed / big-endian) is reported
# as a skipped frame with the reason — never guessed at.
_UNCOMPRESSED_LE = {"1.2.840.10008.1.2.1", "1.2.840.10008.1.2"}


# ---------------------------------------------------------------------------
# Own table
# ---------------------------------------------------------------------------

class ImagingAssistDraft(Base):
    """One vision-assist draft for one ImagingStudy: the structured
    sections of ONE governed agent run, the run's id (disposition
    rides the AgentRun), the prior study used for comparison, and —
    once a radiologist accepts it into the chart — the PRELIMINARY
    DiagnosticReport it became. Status: draft | accepted | edited |
    rejected (accepted/edited are set by accept-into-report; rejected
    follows the AgentRun's disposition)."""
    __tablename__ = "imaging_assist_drafts"
    id = Column(Integer, primary_key=True)
    study_id = Column(String(120), nullable=False, index=True)
    patient_ref = Column(String(200), nullable=False, default="")
    findings_draft = Column(Text, nullable=False, default="")
    impression_draft = Column(Text, nullable=False, default="")
    protocol_suggestion = Column(Text, nullable=True)
    triage_suggestion = Column(Text, nullable=True)
    measurements = Column(JSON, nullable=False, default=list)
    prior_study_id = Column(String(120), nullable=True)
    agent_run_id = Column(String(120), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="draft",
                    index=True)
    report_id = Column(String(120), nullable=True)
    created_by = Column(String(120), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow,
                        onupdate=utcnow)


# ---------------------------------------------------------------------------
# Seeded governed agent (DRAFT, empty model route — agents._seed_draft)
# ---------------------------------------------------------------------------

def default_vision_assistant_payload() -> dict:
    return {
        "description": ("Phase 16: multimodal radiology copilot over the "
                        "Phase 15 DICOM pixel pipeline. Drafts findings / "
                        "impression / protocol + triage suggestions from "
                        "the attached image(s) and the order context for "
                        "a radiologist to accept, edit or reject. Draft "
                        "only — it never signs, verifies or finalises a "
                        "report, and it never invents findings the image "
                        "and inputs do not support."),
        "riskTier": "T3",
        "model": "",
        "instructions": (
            "You are the OpenCare radiology vision assistant. You are "
            "given one imaging study's converted image frame(s), the "
            "order/protocol context, and — when present — a prior "
            "study's report text for comparison. Draft a radiology "
            "report for a radiologist to review, using ONLY what the "
            "image and the supplied context support; never invent "
            "findings, measurements or history. If the image is "
            "insufficient for a section, say so in that section. "
            "Answer with EXACTLY these headed sections, in this order: "
            "FINDINGS:, IMPRESSION:, PROTOCOL_SUGGESTION:, "
            "TRIAGE_SUGGESTION:, MEASUREMENTS: (one 'name: value unit' "
            "line per measurement you can actually support from the "
            "image, or 'none'). Note explicitly in FINDINGS how the "
            "study compares with the prior report when one was "
            "supplied. This is a draft for human disposition — it is "
            "not a diagnosis and not a signed report."),
        "tools": ["opencare_api_call"],
        "apiAllowlist": ["/fhir", "/radiology"],
        "modalities": {"text": True, "image": True, "audio": False},
        "temperature": 0.2,
        "maxSteps": 6,
    }


def ensure_imaging_assist_defaults(session: Session) -> None:
    """Seed the DRAFT vision agent (idempotent; never self-published,
    never enabled, empty model route)."""
    agents._seed_draft(session, AGENT_KEY,
                       default_vision_assistant_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Pixel conversion — pydicom + stdlib only (no numpy in this runtime)
# ---------------------------------------------------------------------------

def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _encode_png(rows: int, cols: int, rgb: bool, raster: bytes) -> bytes:
    color_type = 2 if rgb else 0
    channels = 3 if rgb else 1
    ihdr = struct.pack(">IIBBBBB", cols, rows, 8, color_type, 0, 0, 0)
    stride = cols * channels
    raw = b"".join(b"\x00" + raster[i * stride:(i + 1) * stride]
                   for i in range(rows))
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(raw))
            + _png_chunk(b"IEND", b""))


def _dicom_to_pngs(data: bytes) -> list[bytes]:
    """Convert one DICOM object's pixel data to one PNG per frame.

    Reads the raw PixelData element byte-wise and decodes uncompressed
    little-endian 8/16-bit samples (grayscale or interleaved RGB),
    min-max scaling each frame to 8-bit. Raises ValueError with the
    reason for anything else (no PixelData, compressed/encapsulated
    transfer syntax, unsupported bit depth / photometric / sample
    layout, truncated data) — the caller reports the frame skipped."""
    if pydicom is None:
        raise ValueError("pydicom is not installed on this runtime")
    try:
        ds = pydicom.dcmread(io.BytesIO(data))
    except Exception as exc:
        raise ValueError(f"pydicom could not parse the object: {exc}")
    transfer = ""
    if getattr(ds, "file_meta", None) is not None:
        transfer = str(getattr(ds.file_meta, "TransferSyntaxUID", "") or "")
    if transfer and transfer not in _UNCOMPRESSED_LE:
        raise ValueError(
            f"transfer syntax {transfer} is not an uncompressed "
            "little-endian syntax this converter decodes")
    if "PixelData" not in ds:
        raise ValueError("the object carries no PixelData element")
    raw = bytes(ds.PixelData)
    rows = int(getattr(ds, "Rows", 0) or 0)
    cols = int(getattr(ds, "Columns", 0) or 0)
    if rows <= 0 or cols <= 0:
        raise ValueError("the object has no Rows/Columns")
    bits = int(getattr(ds, "BitsAllocated", 0) or 0)
    if bits not in (8, 16):
        raise ValueError(f"BitsAllocated {bits} is not supported (8/16)")
    spp = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    if spp not in (1, 3):
        raise ValueError(f"SamplesPerPixel {spp} is not supported (1/3)")
    photometric = str(getattr(ds, "PhotometricInterpretation", "") or "")
    if spp == 1 and photometric not in ("MONOCHROME1", "MONOCHROME2"):
        raise ValueError(
            f"photometric interpretation '{photometric}' is not "
            "supported for single-sample data")
    if spp == 3:
        if photometric != "RGB":
            raise ValueError(
                f"photometric interpretation '{photometric}' is not "
                "supported for 3-sample data (RGB only)")
        if int(getattr(ds, "PlanarConfiguration", 0) or 0) != 0:
            raise ValueError("planar (non-interleaved) RGB is not "
                             "supported")
    frames_raw = getattr(ds, "NumberOfFrames", None)
    try:
        frames = int(frames_raw) if frames_raw is not None else 1
    except (TypeError, ValueError):
        frames = 1
    if frames < 1:
        frames = 1
    signed = int(getattr(ds, "PixelRepresentation", 0) or 0) == 1
    fmt = {8: ("b" if signed else "B"), 16: ("h" if signed else "H")}[bits]
    sample_count = rows * cols * spp
    frame_samples = sample_count * frames
    needed = frame_samples * (bits // 8)
    if len(raw) < needed:
        raise ValueError(
            f"PixelData is truncated: {len(raw)} bytes for "
            f"{frames} frame(s) of {rows}x{cols}x{spp} at {bits} bits "
            f"({needed} bytes needed)")
    values = list(struct.unpack_from(
        f"<{frame_samples}{fmt}", raw, 0))
    try:
        slope = float(getattr(ds, "RescaleSlope", 1) or 1)
    except (TypeError, ValueError):
        slope = 1.0
    try:
        intercept = float(getattr(ds, "RescaleIntercept", 0) or 0)
    except (TypeError, ValueError):
        intercept = 0.0
    if slope != 1.0 or intercept != 0.0:
        values = [v * slope + intercept for v in values]
    invert = photometric == "MONOCHROME1"
    pngs: list[bytes] = []
    for frame in range(frames):
        chunk = values[frame * sample_count:(frame + 1) * sample_count]
        lo, hi = min(chunk), max(chunk)
        if hi > lo:
            raster = bytes(
                round((v - lo) * 255.0 / (hi - lo)) for v in chunk)
        else:
            raster = bytes(len(chunk))
        if invert:
            raster = bytes(255 - b for b in raster)
        pngs.append(_encode_png(rows, cols, spp == 3, raster))
    return pngs


# ---------------------------------------------------------------------------
# Internal request plumbing — run/disposition/report creation ride the
# EXISTING endpoint functions (app.agents, app.boards) unchanged.
# ---------------------------------------------------------------------------

def _internal_request(body: dict, authorization: str | None) -> StarletteRequest:
    raw = json.dumps(body).encode("utf-8")
    headers = [(b"content-type", b"application/json")]
    if authorization:
        headers.append((b"authorization", authorization.encode("utf-8")))
    scope = {"type": "http", "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": "/internal", "query_string": b"",
             "headers": headers, "client": ("127.0.0.1", 50000),
             "server": ("internal", 80)}
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent["done"] = True
        return {"type": "http.request", "body": raw, "more_body": False}

    return StarletteRequest(scope, receive)


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


# ---------------------------------------------------------------------------
# Prior-study comparison context
# ---------------------------------------------------------------------------

def _prior_report_context(session: Session, study) -> tuple[str | None, str]:
    """(prior study id, prompt text) for the most recent EARLIER study
    of the same patient that has a DiagnosticReport — ("", no) when
    none exists. The report text is quoted verbatim from the chart."""
    body = study.body or {}
    patient_ref = radiology._study_patient(body)
    if not patient_ref:
        return None, ""
    from .models import FhirResource
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "ImagingStudy")
        .order_by(FhirResource.created_at.asc(),
                  FhirResource.id.asc())).all()
    ordered_ids = [r.resource_id for r in rows
                   if radiology._study_patient(r.body or {}) == patient_ref]
    if study.resource_id not in ordered_ids:
        return None, ""
    earlier = ordered_ids[:ordered_ids.index(study.resource_id)]
    for prior_id in reversed(earlier):
        reports = boards._study_reports(session, prior_id)
        if not reports:
            continue
        latest = reports[-1]
        conclusion = str((latest.body or {}).get("conclusion") or "")
        status = str((latest.body or {}).get("status") or "")
        return prior_id, (f"Prior study {prior_id} report "
                          f"(DiagnosticReport/{latest.resource_id}, "
                          f"status {status}):\n{conclusion}")
    return None, ""


def _order_context(session: Session, study_body: dict) -> str:
    based_on = study_body.get("basedOn") or []
    if based_on and isinstance(based_on[0], dict):
        ref = str(based_on[0].get("reference") or "")
        if ref.startswith("ServiceRequest/"):
            try:
                sr_body = fhir._get_row(
                    "ServiceRequest", ref.split("/", 1)[1],
                    session).body or {}
            except HTTPException:
                return ""
            coding = ((sr_body.get("code") or {}).get("coding") or [{}])[0]
            label = coding.get("display") or coding.get("code") or ""
            priority = str(sr_body.get("priority") or "")
            return (f"Order {ref}: {label}"
                    + (f" (priority {priority})" if priority else ""))
    return ""


# ---------------------------------------------------------------------------
# Structured-output parsing (the agent's headed sections)
# ---------------------------------------------------------------------------

_HEADINGS = {
    "FINDINGS": "findings",
    "IMPRESSION": "impression",
    "PROTOCOL_SUGGESTION": "protocol",
    "PROTOCOL SUGGESTION": "protocol",
    "PROTOCOL": "protocol",
    "TRIAGE_SUGGESTION": "triage",
    "TRIAGE SUGGESTION": "triage",
    "TRIAGE": "triage",
    "MEASUREMENTS": "measurements",
}
_HEADING_RE = re.compile(r"^\s*(?:#{1,4}\s*)?([A-Za-z][A-Za-z _\-]{2,})"
                         r"\s*:\s*(.*)$")
_MEASUREMENT_RE = re.compile(
    r"^(?P<name>.+?):\s*(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>\S.*)?$")


def _parse_sections(output: str) -> dict:
    sections: dict[str, list[str]] = {"findings": [], "impression": [],
                                      "protocol": [], "triage": [],
                                      "measurements": []}
    current: str | None = None
    recognised = False
    for line in (output or "").splitlines():
        match = _HEADING_RE.match(line)
        key = None
        if match:
            key = _HEADINGS.get(match.group(1).strip().upper())
        if key is not None:
            current = key
            recognised = True
            rest = match.group(2).strip()
            if rest:
                sections[key].append(rest)
            continue
        if current is not None:
            sections[current].append(line)
    if not recognised:
        # Unstructured output is preserved whole as the findings draft
        # rather than dropped or invented into sections.
        return {"findings": (output or "").strip(), "impression": "",
                "protocol": "", "triage": "", "measurements": []}
    measurements = []
    for line in sections["measurements"]:
        text = line.strip().lstrip("-•* ").strip()
        if not text or text.lower() == "none":
            continue
        m = _MEASUREMENT_RE.match(text)
        if m:
            entry = {"name": m.group("name").strip(),
                     "value": float(m.group("value"))}
            if m.group("unit"):
                entry["unit"] = m.group("unit").strip()
            measurements.append(entry)
        elif ":" in text:
            name, value = text.split(":", 1)
            measurements.append({"name": name.strip(),
                                 "value": value.strip()})
        else:
            measurements.append({"name": text, "value": ""})
    return {
        "findings": "\n".join(sections["findings"]).strip(),
        "impression": "\n".join(sections["impression"]).strip(),
        "protocol": "\n".join(sections["protocol"]).strip(),
        "triage": "\n".join(sections["triage"]).strip(),
        "measurements": measurements,
    }


# ---------------------------------------------------------------------------
# Draft serialisation + disposition sync (status follows the AgentRun)
# ---------------------------------------------------------------------------

def _sync_status(session: Session, draft: ImagingAssistDraft) -> None:
    if draft.report_id:
        return  # accepted/edited into a report is terminal here
    run = session.scalars(select(AgentRun).where(
        AgentRun.run_id == draft.agent_run_id)).first()
    if run is None:
        return
    if run.disposition == "rejected" and draft.status != "rejected":
        draft.status = "rejected"
        session.flush()
    elif run.disposition == "accepted" and draft.status == "draft":
        draft.status = "accepted"
        session.flush()


def _draft_out(session: Session, draft: ImagingAssistDraft,
               extra: dict | None = None) -> dict:
    _sync_status(session, draft)
    out = {
        "id": draft.id,
        "studyId": draft.study_id,
        "patientRef": draft.patient_ref,
        "findingsDraft": draft.findings_draft,
        "impressionDraft": draft.impression_draft,
        "protocolSuggestion": draft.protocol_suggestion,
        "triageSuggestion": draft.triage_suggestion,
        "measurements": draft.measurements or [],
        "priorStudyId": draft.prior_study_id,
        "agentRunId": draft.agent_run_id,
        "status": draft.status,
        "reportId": draft.report_id,
        "createdBy": draft.created_by,
        "createdAt": draft.created_at.isoformat()
        if draft.created_at else None,
    }
    if extra:
        out.update(extra)
    return out


def _draft_row(session: Session, draft_id: int) -> ImagingAssistDraft:
    draft = session.scalars(select(ImagingAssistDraft).where(
        ImagingAssistDraft.id == draft_id)).first()
    if draft is None:
        raise HTTPException(status_code=404,
                            detail=f"Imaging assist draft {draft_id} "
                                   "not found")
    return draft


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/studies/{study_id}/draft", status_code=201)
async def create_draft(study_id: str, request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(
                           require_capability("radiology.work"))):
    """Draft a report for one study with the governed vision agent.

    Loads the study's archived instances (hash-verified against the
    instance rows before use), converts pixels to PNG, builds the
    prompt from the order + assigned protocol + the prior study's
    report text when one exists, and runs the agent through
    app.agents' run machinery with the first converted frame as the
    image part. Frames that cannot be converted are named in
    `skipped_instances`; NO convertible frames -> 422. A runtime /
    no-model 503 from app.agents surfaces verbatim and nothing is
    written (no draft row, no run row)."""
    payload = await _json_body(request)
    instruction = payload.get("instruction")
    if instruction is not None and not isinstance(instruction, str):
        raise HTTPException(status_code=422,
                            detail="'instruction' must be a string")
    study = radiology._study_row(session, study_id)
    study_body = study.body or {}
    patient_ref = radiology._study_patient(study_body)
    modality = radiology._study_modality(study_body)

    instances = session.scalars(
        select(radiology.RadDicomInstance).where(
            radiology.RadDicomInstance.study_id == study_id)
        .order_by(radiology.RadDicomInstance.id.asc())).all()
    if not instances:
        raise HTTPException(
            status_code=422,
            detail=f"Study {study_id} has no archived DICOM instances: "
                   "the vision assistant needs pixel data — ingest "
                   "instances through POST /radiology/dicom/ingest "
                   "first")

    store = default_cold_store()
    converted: list[dict] = []
    skipped: list[dict] = []
    for record in instances:
        try:
            data = store.get(record.cold_ref)
        except OSError as exc:
            skipped.append({"sop_uid": record.sop_uid,
                            "reason": f"archived object could not be "
                                      f"read: {exc}"})
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest != record.content_hash:
            skipped.append({"sop_uid": record.sop_uid,
                            "reason": "archived object failed SHA-256 "
                                      "verification against the instance "
                                      "row; bytes not used"})
            continue
        try:
            pngs = _dicom_to_pngs(data)
        except ValueError as exc:
            skipped.append({"sop_uid": record.sop_uid,
                            "reason": str(exc)})
            continue
        for index, png in enumerate(pngs):
            converted.append({"sop_uid": record.sop_uid,
                              "frame": index, "png": png})
    if not converted:
        reasons = "; ".join(f"{s['sop_uid']}: {s['reason']}"
                            for s in skipped) or "no reason recorded"
        raise HTTPException(
            status_code=422,
            detail=f"Study {study_id} has no convertible pixel "
                   f"frames; nothing was sent to the vision agent. "
                   f"Skipped: {reasons}")

    prior_id, prior_text = _prior_report_context(session, study)
    entry = radiology._entry_for(session, study)
    protocol_code = entry.protocol_code if entry is not None else None

    lines = [
        "Draft a radiology report for the study below. The first "
        "converted image frame is attached as an image part.",
        f"Study: ImagingStudy/{study_id}"
        + (f" (modality {modality})" if modality else ""),
        f"Description: {study_body.get('description') or ''}",
    ]
    order = _order_context(session, study_body)
    if order:
        lines.append(order)
    if protocol_code:
        lines.append(f"Assigned protocol: {protocol_code}")
    lines.append(
        f"Images: {len(converted)} frame(s) converted from "
        f"{len(instances)} archived instance(s); attached frame is "
        f"instance {converted[0]['sop_uid']} frame "
        f"{converted[0]['frame']}.")
    if skipped:
        lines.append(
            "Skipped instances (NOT imaged — do not describe them): "
            + "; ".join(f"{s['sop_uid']} ({s['reason']})"
                        for s in skipped))
    if prior_text:
        lines.append("Comparison prior — " + prior_text)
    else:
        lines.append("No prior study report is available for "
                     "comparison.")
    if instruction and instruction.strip():
        lines.append(f"Radiologist instruction: {instruction.strip()}")
    prompt = "\n".join(lines)

    run_request = _internal_request(
        {"prompt": prompt, "patient_ref": patient_ref or None,
         "imageBase64": base64.b64encode(converted[0]["png"])
         .decode("ascii"),
         "imageMime": "image/png"},
        request.headers.get("Authorization"))
    # app.agents raises its honest 503/404/409 itself; on any such
    # failure nothing below runs and no draft row exists.
    run = await agents.run_agent(AGENT_KEY, run_request, session, user)
    parsed = _parse_sections(str(run.get("output") or ""))

    draft = ImagingAssistDraft(
        study_id=study_id, patient_ref=patient_ref,
        findings_draft=parsed["findings"],
        impression_draft=parsed["impression"],
        protocol_suggestion=parsed["protocol"] or None,
        triage_suggestion=parsed["triage"] or None,
        measurements=parsed["measurements"],
        prior_study_id=prior_id, agent_run_id=run["run_id"],
        status="draft", created_by=user.username)
    session.add(draft)
    session.flush()
    append_audit(session, actor=user.username,
                 action="imaging_assist.draft.create",
                 entity=f"ImagingStudy/{study_id}",
                 payload={"draft_id": draft.id,
                          "agent_run_id": run["run_id"],
                          "prior_study_id": prior_id,
                          "frames_converted": len(converted),
                          "instances_skipped": len(skipped),
                          "model": run.get("model")})
    session.commit()
    return _draft_out(session, draft, extra={
        "skipped_instances": skipped,
        "framesConverted": len(converted),
        "attachedInstance": converted[0]["sop_uid"],
        "attachedFrame": converted[0]["frame"],
        "agentOutput": run.get("output"),
        "model": run.get("model"),
    })


@router.get("/drafts/{draft_id}")
def get_draft(draft_id: int, session: Session = Depends(get_db),
              user: User = Depends(require_capability("boards.read"))):
    return _draft_out(session, _draft_row(session, draft_id))


@router.get("/studies/{study_id}/drafts")
def list_drafts(study_id: str, session: Session = Depends(get_db),
                user: User = Depends(require_capability("boards.read"))):
    radiology._study_row(session, study_id)
    rows = session.scalars(
        select(ImagingAssistDraft).where(
            ImagingAssistDraft.study_id == study_id)
        .order_by(ImagingAssistDraft.id.asc())).all()
    items = [_draft_out(session, row) for row in rows]
    session.commit()
    return {"items": items, "total": len(items)}


@router.post("/drafts/{draft_id}/accept-into-report", status_code=201)
async def accept_into_report(draft_id: int, request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(
                                 require_capability("radiology.work"))):
    """Accept one draft into the chart as a PRELIMINARY report.

    A still-pending AgentRun is first disposed `accepted` through the
    existing /agents disposition machinery (same audit trail); a
    rejected draft can never be accepted (409), and a draft already
    accepted into a report cannot be accepted twice (409). The report
    itself is created by boards' imaging-report function — status
    `preliminary`, one open preliminary per study — with the draft's
    findings/impression as its conclusion, or `editedText` verbatim
    when the radiologist edited it (the draft then reads `edited`).
    This endpoint contains no verification step: the study's
    verified-report state is exactly what it was before the call."""
    payload = await _json_body(request)
    draft = _draft_row(session, draft_id)
    _sync_status(session, draft)
    if draft.status == "rejected":
        raise HTTPException(
            status_code=409,
            detail=f"Imaging assist draft {draft_id} was rejected via "
                   "its agent run's disposition; it cannot be accepted "
                   "into a report")
    if draft.report_id:
        raise HTTPException(
            status_code=409,
            detail=f"Imaging assist draft {draft_id} is already "
                   f"accepted into DiagnosticReport/{draft.report_id}")
    edited = payload.get("editedText")
    if edited is not None and (not isinstance(edited, str)
                               or not edited.strip()):
        raise HTTPException(
            status_code=422,
            detail="'editedText' must be a non-empty string when given")
    if edited:
        conclusion = edited.strip()
    else:
        parts = []
        if draft.findings_draft:
            parts.append(f"Findings: {draft.findings_draft}")
        if draft.impression_draft:
            parts.append(f"Impression: {draft.impression_draft}")
        conclusion = "\n".join(parts).strip()
    if not conclusion:
        raise HTTPException(
            status_code=422,
            detail=f"Imaging assist draft {draft_id} has no findings "
                   "or impression text to file, and no 'editedText' "
                   "was supplied")

    authorization = request.headers.get("Authorization")
    run = session.scalars(select(AgentRun).where(
        AgentRun.run_id == draft.agent_run_id)).first()
    if run is not None and run.disposition == "pending":
        await agents.set_disposition(
            draft.agent_run_id,
            _internal_request({"disposition": "accepted"},
                              authorization),
            session, user)
    report = await boards.create_imaging_report(
        draft.study_id,
        _internal_request({"conclusion": conclusion}, authorization),
        session, user)
    if report.get("status") != "preliminary":
        raise HTTPException(
            status_code=500,
            detail="The radiology report flow did not return a "
                   "preliminary report; refusing to record acceptance")
    draft = _draft_row(session, draft_id)
    draft.status = "edited" if edited else "accepted"
    draft.report_id = report["id"]
    session.flush()
    append_audit(session, actor=user.username,
                 action="imaging_assist.draft.accept",
                 entity=f"DiagnosticReport/{report['id']}",
                 payload={"draft_id": draft.id,
                          "study_id": draft.study_id,
                          "agent_run_id": draft.agent_run_id,
                          "edited": bool(edited),
                          "report_status": report["status"]})
    session.commit()
    return _draft_out(session, draft, extra={
        "report": {"id": report["id"], "status": report["status"]}})
