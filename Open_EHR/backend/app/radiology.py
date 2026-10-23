"""Phase 15 (D/BD-35) — the radiology department.

app.boards built the radiology *board*: ImagingStudy creation from a
ServiceRequest, study-level DICOMweb metadata, and the DiagnosticReport
preliminary -> final flow verified under `radiology.verify`. This module
builds the *department* around that board, without touching its routes
(every path here is a subpath boards does not claim):

1. RIS worklist — an operational overlay (own table, keyed to the
   ImagingStudy) with the workflow ordered -> scheduled -> arrived ->
   in-progress -> completed -> reported, driven by the published
   workflow config `radiology-flow` (the surgery-flow pattern in
   app.boards). Priorities, TAT targets, the protocol-required and
   screening-required gates, dose quantities, station AE titles and the
   critical-result SLA all live in the published terminology config
   `radiology-ops`; protocols live in terminology `imaging-protocols`.
2. DICOM ingest + archive (BD-35) — POST /radiology/dicom/ingest takes
   a JSON body {"dicom_base64": "..."} (the documented ingest shape:
   base64 JSON, not multipart), parses it with pydicom, validates the
   Study/Series/SOP Instance UIDs + Modality + PatientID, and stores
   the EXACT received bytes through app.tiering's ColdStore
   (content-addressed by SHA-256; the ref + hash + size ride on an
   instance row). Ingest is for ORDERED studies: the Study Instance UID
   must match an existing ImagingStudy, else 422.
3. Retrieval — WADO-RS-style GET under /radiology/dicomweb/... serves
   the stored bytes as application/dicom ONLY after re-hashing them
   against the instance row (the tiering precedent: unverified bytes
   are never served), plus series/instance-level QIDO-RS metadata.
4. DICOM Modality Worklist — GET /radiology/mwl returns DICOM JSON
   (PS3.18) scheduled-procedure-step entries for scheduled/arrived
   studies, station AE + date/time from the worklist entry / config.
5. Protocoling + pre-exam screening — protocols are assigned from
   `imaging-protocols`; the screening forms (contrast, pregnancy,
   MR safety) are governed Studio forms submitted through the Phase 5
   documentation machinery (documentation._store_response) and attached
   to the study. The in-progress transition refuses (409) until the
   protocol is assigned and its required screenings are complete when
   the published ops flags say so — config-gated, flip-proven in tests.
6. Radiation dose — per-study dose records whose quantities/units come
   from `radiology-ops` per modality, plus a per-patient cumulative
   dose history.
7. Structured reporting + addenda — report templates are governed
   forms (radiology-report-ct-chest, radiology-report-xr-chest); a
   submission stores the QuestionnaireResponse and composes its answers
   into the conclusion of a boards-shape DiagnosticReport (preliminary;
   verification stays boards' /radiology/reports/{id}/verify — this
   module never verifies). An addendum creates a NEW final report
   linked to the original and marks the original `corrected`; the
   original's content is preserved and both writes are audited.
8. Critical results closed loop — flagging a FINAL imaging report
   opens a record + a FHIR Task to the ordering clinician; read-back
   requires a named reader and the read-back text; closure is refused
   until read-back exists. A scan endpoint emits the
   `radiology.critical.overdue` event into the rules engine for
   unacknowledged results past the config SLA, and the seeded
   published rule `radiology-critical-escalation` turns it into an
   escalation Task (the Phase 12 lab critical-value precedent).
9. TAT dashboard data — GET /radiology/stats/tat computes
   order -> final-report minutes per priority against the config
   targets (breach counts, averages), and the published dashboard
   config `radiology-operations` binds widgets to these live
   endpoints (the Phase 10/12 dashboard mechanism: widgets name an
   endpoint + field — no data lives in the config).

Demo seeding: ensure_radiology_defaults seeds a small living
department (three synthetic studies with tiny synthetic DICOM pixel
instances in the cold store) ONLY when the demo patients pat-1/2/3
exist in the store — in unit tests they do not, so tests stay clean.

Synthetic data only; no real PHI. The cold store is the local
filesystem implementation behind tiering's S3-shaped interface — no
external PACS, S3 or DICOM network service is claimed anywhere here.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import (Column, DateTime, Float, Integer, JSON, String,
                        Text, select)
from sqlalchemy.orm import Session

from . import boards, documentation, fhir
from .audit import append_audit
from .configstore import (load_workflow_machine, published_config)
from .db import Base
from .models import ConfigStatus, FhirResource, StudioConfig, User, utcnow
from .rules import evaluate_rules
from .rules import _seed_published as _seed_rule
from .security import get_db, require_capability
from .tiering import default_cold_store

try:  # pydicom is a declared requirement (backend/requirements.txt);
    # the guard keeps a missing optional install from breaking the app
    # import — ingest then honestly 503s instead of faking success.
    import pydicom
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian
except ImportError:  # pragma: no cover
    pydicom = None
    FileDataset = FileMetaDataset = None  # type: ignore
    ExplicitVRLittleEndian = None  # type: ignore

router = APIRouter(prefix="/radiology", tags=["radiology"])

WORKFLOW_KEY = "radiology-flow"
OPS_KEY = "radiology-ops"
PROTOCOLS_KEY = "imaging-protocols"
DASHBOARD_KEY = "radiology-operations"
CRITICAL_RULE_KEY = "radiology-critical-escalation"
CRITICAL_EVENT = "radiology.critical.overdue"

SCREENING_FORM_KEYS = ("radiology-screening-contrast",
                       "radiology-screening-pregnancy",
                       "radiology-screening-mr-safety")
REPORT_TEMPLATE_KEYS = ("radiology-report-ct-chest",
                        "radiology-report-xr-chest")

SCREENING_EXTENSION = "urn:opencare:extension/radiology-screening"
STRUCTURED_EXTENSION = "urn:opencare:extension/radiology-structured-report"
ADDENDUM_OF_EXTENSION = "urn:opencare:extension/addendum-of"
ADDENDUM_EXTENSION = "urn:opencare:extension/addendum"

RAD_CATEGORY = [{"coding": [{
    "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
    "code": "RAD", "display": "Radiology"}]}]

DEFAULT_RAD_TRANSITIONS: dict[str, set[str]] = {
    "ordered": {"scheduled"},
    "scheduled": {"arrived"},
    "arrived": {"in-progress"},
    "in-progress": {"completed"},
    "completed": {"reported"},
    "reported": set(),
}

# FHIR ServiceRequest.priority -> radiology-ops priority code.
_SR_PRIORITY_MAP = {"stat": "STAT", "asap": "STAT",
                    "urgent": "URGENT", "routine": "ROUTINE"}


# ---------------------------------------------------------------------------
# Own tables (the protocols.py pattern: module tables on the shared Base)
# ---------------------------------------------------------------------------

class RadWorklistEntry(Base):
    """RIS worklist overlay for one ImagingStudy: workflow state,
    priority, protocol, scheduling. The ImagingStudy stays the clinical
    record; this row is the department machinery (Phase 12 pattern)."""
    __tablename__ = "rad_worklist_entries"
    id = Column(Integer, primary_key=True)
    study_id = Column(String(120), unique=True, nullable=False, index=True)
    study_uid = Column(String(120), nullable=False, default="")
    patient_ref = Column(String(200), nullable=False, default="")
    modality = Column(String(20), nullable=False, default="")
    priority = Column(String(20), nullable=False, default="ROUTINE")
    state = Column(String(30), nullable=False, default="ordered", index=True)
    protocol_code = Column(String(120), nullable=True)
    protocolled_by = Column(String(120), nullable=True)
    protocolled_at = Column(DateTime(timezone=True), nullable=True)
    scheduled_at = Column(DateTime(timezone=True), nullable=True)
    station_ae = Column(String(60), nullable=True)
    ordered_at = Column(DateTime(timezone=True), nullable=False,
                        default=utcnow)
    state_history = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow,
                        onupdate=utcnow)


class RadDicomInstance(Base):
    """One archived DICOM instance: the dataset bytes live in the cold
    store (content-addressed); this row carries the ref + SHA-256 +
    size that retrieval verifies before serving a single byte."""
    __tablename__ = "rad_dicom_instances"
    id = Column(Integer, primary_key=True)
    study_id = Column(String(120), nullable=False, index=True)
    study_uid = Column(String(120), nullable=False, index=True)
    series_uid = Column(String(120), nullable=False, index=True)
    sop_uid = Column(String(120), unique=True, nullable=False, index=True)
    sop_class_uid = Column(String(120), nullable=False, default="")
    modality = Column(String(20), nullable=False, default="")
    patient_id = Column(String(120), nullable=False, default="")
    series_number = Column(Integer, nullable=True)
    instance_number = Column(Integer, nullable=True)
    cold_ref = Column(String(200), nullable=False)
    content_hash = Column(String(64), nullable=False)
    size_bytes = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=utcnow)


class RadDoseRecord(Base):
    """One radiation-dose quantity recorded for a study. Quantities and
    units are validated against the published radiology-ops config for
    the study's modality (CTDIvol/DLP for CT, DAP for X-ray/fluoro...)."""
    __tablename__ = "rad_dose_records"
    id = Column(Integer, primary_key=True)
    study_id = Column(String(120), nullable=False, index=True)
    patient_ref = Column(String(200), nullable=False, default="")
    modality = Column(String(20), nullable=False, default="")
    quantity = Column(String(60), nullable=False)
    value = Column(Float, nullable=False)
    unit = Column(String(40), nullable=False, default="")
    recorded_by = Column(String(120), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), default=utcnow)


class RadCriticalResult(Base):
    """Critical-result closed-loop record for one imaging
    DiagnosticReport (the LabCriticalNotification separation: the
    notification internals never ride on the patient-facing report).
    State: notified -> read-back -> closed."""
    __tablename__ = "rad_critical_results"
    id = Column(Integer, primary_key=True)
    report_id = Column(String(120), unique=True, nullable=False, index=True)
    study_id = Column(String(120), nullable=False, default="")
    patient_ref = Column(String(200), nullable=False, default="")
    ordering_clinician = Column(String(200), nullable=False, default="")
    state = Column(String(20), nullable=False, default="notified")
    task_id = Column(String(120), nullable=True)
    flagged_by = Column(String(120), nullable=False, default="")
    flagged_at = Column(DateTime(timezone=True), nullable=False,
                        default=utcnow)
    readback_by = Column(String(200), nullable=True)
    readback_text = Column(Text, nullable=True)
    readback_at = Column(DateTime(timezone=True), nullable=True)
    closed_by = Column(String(120), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    escalated_for = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow,
                        onupdate=utcnow)


# ---------------------------------------------------------------------------
# Small helpers (boards.py conventions)
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _ensure_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_dt(raw) -> datetime | None:
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
        raise HTTPException(status_code=422,
                            detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422,
                            detail="Request body must be a JSON object")
    return body


def _seed_published(session: Session, kind: str, key: str,
                    payload: dict) -> None:
    existing = session.scalars(select(StudioConfig).where(
        StudioConfig.kind == kind, StudioConfig.key == key)).first()
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


def _ops(session: Session) -> dict:
    """The effective radiology-ops payload: the published terminology
    config, falling back to the seeded defaults when unpublished."""
    cfg = published_config(session, "terminology", OPS_KEY)
    if cfg is not None and isinstance(cfg.payload, dict):
        return cfg.payload
    return default_ops_payload()


def _priority_concepts(ops: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for concept in ops.get("concepts") or []:
        if isinstance(concept, dict) and concept.get("code"):
            out[str(concept["code"])] = concept
    return out


def _tat_target(ops: dict, priority: str) -> int | None:
    concept = _priority_concepts(ops).get(priority) or {}
    target = concept.get("tatMinutes")
    if isinstance(target, (int, float)) and not isinstance(target, bool):
        return int(target)
    return None


def _study_row(session: Session, study_id: str) -> FhirResource:
    return fhir._get_row("ImagingStudy", study_id, session)


def _study_modality(study_body: dict) -> str:
    for modality in study_body.get("modality") or []:
        if isinstance(modality, dict) and modality.get("code"):
            return str(modality["code"])
    return ""


def _study_patient(study_body: dict) -> str:
    subject = study_body.get("subject") or {}
    return str(subject.get("reference") or "")


def _derive_priority(session: Session, study_body: dict, ops: dict) -> str:
    based_on = study_body.get("basedOn") or []
    if based_on and isinstance(based_on[0], dict):
        ref = str(based_on[0].get("reference") or "")
        if ref.startswith("ServiceRequest/"):
            try:
                sr = fhir._get_row("ServiceRequest",
                                   ref.split("/", 1)[1], session)
            except HTTPException:
                sr = None
            if sr is not None:
                mapped = _SR_PRIORITY_MAP.get(
                    str((sr.body or {}).get("priority") or "").lower())
                if mapped and mapped in _priority_concepts(ops):
                    return mapped
    default = str(ops.get("defaultPriority") or "ROUTINE")
    return default if default in _priority_concepts(ops) else "ROUTINE"


def _entry_for(session: Session, study: FhirResource, *,
               create: bool = False) -> RadWorklistEntry | None:
    entry = session.scalars(select(RadWorklistEntry).where(
        RadWorklistEntry.study_id == study.resource_id)).first()
    if entry is None and create:
        body = study.body or {}
        ops = _ops(session)
        entry = RadWorklistEntry(
            study_id=study.resource_id,
            study_uid=boards._study_uid(body) or "",
            patient_ref=_study_patient(body),
            modality=_study_modality(body),
            priority=_derive_priority(session, body, ops),
            state="ordered",
            ordered_at=_ensure_aware(study.created_at) or _now(),
            state_history=[{"state": "ordered", "at": _now_iso(),
                            "by": "system"}])
        session.add(entry)
        session.flush()
    return entry


def _screening_state(study_body: dict) -> dict:
    for ext in study_body.get("extension") or []:
        if isinstance(ext, dict) and ext.get("url") == SCREENING_EXTENSION:
            try:
                parsed = json.loads(ext.get("valueString") or "{}")
            except (TypeError, ValueError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
    return {}


def _set_study_extension(study: FhirResource, url: str,
                         value_string: str) -> None:
    body = dict(study.body or {})
    extensions = [e for e in (body.get("extension") or [])
                  if not (isinstance(e, dict) and e.get("url") == url)]
    extensions.append({"url": url, "valueString": value_string})
    body["extension"] = extensions
    study.body = body
    study.version += 1


def _protocol_concept(session: Session, code: str) -> dict | None:
    cfg = published_config(session, "terminology", PROTOCOLS_KEY)
    concepts = (cfg.payload or {}).get("concepts") if cfg else None
    for concept in concepts or []:
        if isinstance(concept, dict) and concept.get("code") == code:
            return concept
    return None


def _latest_report(session: Session, study_id: str) -> FhirResource | None:
    reports = boards._study_reports(session, study_id)
    return reports[-1] if reports else None


def _first_final_report(session: Session,
                        study_id: str) -> FhirResource | None:
    for report in boards._study_reports(session, study_id):
        if (report.body or {}).get("status") == "final":
            return report
    return None


def _entry_out(session: Session, study: FhirResource,
               entry: RadWorklistEntry | None) -> dict:
    body = study.body or {}
    ops = _ops(session)
    if entry is not None:
        state = entry.state
        priority = entry.priority
        protocol = entry.protocol_code
        scheduled_at = _ensure_aware(entry.scheduled_at)
        station = entry.station_ae
        ordered_at = _ensure_aware(entry.ordered_at)
        history = entry.state_history or []
    else:
        state = "ordered"
        priority = _derive_priority(session, body, ops)
        protocol = None
        scheduled_at = None
        station = None
        ordered_at = _ensure_aware(study.created_at)
        history = []
    report = _latest_report(session, study.resource_id)
    return {
        "study_id": study.resource_id,
        "study_uid": boards._study_uid(body),
        "patient_ref": _study_patient(body),
        "modality": _study_modality(body),
        "description": body.get("description"),
        "priority": priority,
        "tat_target_minutes": _tat_target(ops, priority),
        "state": state,
        "protocol_code": protocol,
        "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
        "station_ae": station,
        "ordered_at": ordered_at.isoformat() if ordered_at else None,
        "screening": _screening_state(body),
        "report": ({"id": report.resource_id,
                    "status": (report.body or {}).get("status")}
                   if report is not None else None),
        "state_history": history,
    }


# ---------------------------------------------------------------------------
# Default Studio configuration (seeded published v1 at startup)
# ---------------------------------------------------------------------------

def _workflow_payload() -> dict:
    transitions = [
        {"from": src, "to": dst}
        for src in sorted(DEFAULT_RAD_TRANSITIONS)
        for dst in sorted(DEFAULT_RAD_TRANSITIONS[src])
    ]
    return {"states": sorted(DEFAULT_RAD_TRANSITIONS.keys()),
            "transitions": transitions}


def default_ops_payload() -> dict:
    """Radiology operations config (terminology/radiology-ops). The
    priority value set rides in `concepts` (each with its TAT target in
    minutes) so the payload stays a valid terminology config; the rest
    is department policy data read by this module at runtime."""
    return {
        "name": "Radiology operations (synthetic defaults)",
        "concepts": [
            {"code": "STAT", "display": "STAT — immediate", "active": True,
             "tatMinutes": 60},
            {"code": "URGENT", "display": "Urgent", "active": True,
             "tatMinutes": 240},
            {"code": "ROUTINE", "display": "Routine", "active": True,
             "tatMinutes": 1440},
        ],
        "defaultPriority": "ROUTINE",
        # Gates (flip-proven in tests): a study may not start until it
        # is protocolled, and a protocol's screening forms must be
        # complete, while these flags are on.
        "protocolRequired": True,
        "screeningRequired": True,
        # Critical-result read-back SLA, minutes from flagging.
        "criticalSlaMinutes": 30,
        # Scheduled station AE titles per modality (DICOM MWL).
        "stations": {"CT": "CT-SCAN-1", "MR": "MR-SCAN-1",
                     "CR": "XR-ROOM-1", "DX": "XR-ROOM-1",
                     "US": "US-ROOM-1", "MG": "MG-ROOM-1",
                     "NM": "NM-ROOM-1"},
        "defaultStationAe": "OPENCare-MWL",
        # Modality-appropriate dose quantities + units.
        "doseQuantities": {
            "CT": [{"code": "CTDIvol", "unit": "mGy"},
                   {"code": "DLP", "unit": "mGy.cm"}],
            "MR": [],
            "CR": [{"code": "DAP", "unit": "Gy.cm2"}],
            "DX": [{"code": "DAP", "unit": "Gy.cm2"}],
            "US": [],
            "MG": [{"code": "AGD", "unit": "mGy"}],
            "NM": [{"code": "administered-activity", "unit": "MBq"}],
        },
    }


def default_protocols_payload() -> dict:
    def proto(code, display, modality, screenings, contrast=False):
        return {"code": code, "display": display, "active": True,
                "modality": modality, "contrast": contrast,
                "screeningForms": screenings}

    return {"name": "OpenCare imaging protocols",
            "system": "urn:opencare:terminology/imaging-protocols",
            "concepts": [
                proto("CT-CHEST-CONTRAST", "CT chest with IV contrast",
                      "CT", ["radiology-screening-contrast"],
                      contrast=True),
                proto("CT-HEAD-NC", "CT head without contrast", "CT", []),
                proto("CT-ABDPELVIS-CONTRAST",
                      "CT abdomen/pelvis with IV contrast", "CT",
                      ["radiology-screening-contrast",
                       "radiology-screening-pregnancy"], contrast=True),
                proto("XR-CHEST-2V", "Chest X-ray, 2 views", "DX", []),
                proto("MR-BRAIN", "MR brain without contrast", "MR",
                      ["radiology-screening-mr-safety"]),
                proto("MR-LSPINE", "MR lumbar spine without contrast",
                      "MR", ["radiology-screening-mr-safety"]),
            ]}


def _form(title: str, items: list[dict]) -> dict:
    return {"resourceType": "Questionnaire", "title": title, "item": items}


def _item(link_id, text, type_="boolean", required=True):
    return {"linkId": link_id, "text": text, "type": type_,
            "required": required}


def default_form_payloads() -> dict[str, dict]:
    return {
        "radiology-screening-contrast": _form(
            "Pre-exam screening — iodinated contrast safety", [
                _item("contrast-allergy",
                      "Known allergy to iodinated contrast media?"),
                _item("renal-impairment",
                      "Known renal impairment or eGFR below 30?"),
                _item("metformin", "Currently taking metformin?"),
                _item("prior-reaction",
                      "Details of any prior contrast reaction",
                      "string", False),
            ]),
        "radiology-screening-pregnancy": _form(
            "Pre-exam screening — pregnancy", [
                _item("pregnancy-possible", "Is pregnancy possible?"),
                _item("pregnancy-status",
                      "Pregnancy status (confirmed / excluded by test / "
                      "not applicable)", "string"),
                _item("lmp-date", "Last menstrual period (date)",
                      "date", False),
            ]),
        "radiology-screening-mr-safety": _form(
            "Pre-exam screening — MR safety", [
                _item("pacemaker",
                      "Pacemaker or implanted cardiac device?"),
                _item("metal-implants",
                      "Metal implants, clips, shrapnel or ocular metal?"),
                _item("claustrophobia",
                      "Claustrophobia or inability to lie still?"),
            ]),
        "radiology-report-ct-chest": _form(
            "Structured report — CT chest", [
                _item("findings", "Findings", "text"),
                _item("impression", "Impression", "text"),
                _item("recommendation", "Recommendation", "string", False),
            ]),
        "radiology-report-xr-chest": _form(
            "Structured report — Chest X-ray", [
                _item("findings", "Findings", "text"),
                _item("impression", "Impression", "text"),
                _item("recommendation", "Recommendation", "string", False),
            ]),
    }


def default_dashboard_payload() -> dict:
    return {
        "title": "Radiology operations",
        "description": ("Phase 15: the RIS worklist and turnaround "
                        "picture, computed live from the radiology "
                        "department endpoints (widgets bind endpoint + "
                        "field; no data lives in this config)."),
        "widgets": [
            {"id": "worklist-total", "title": "Studies on the RIS worklist",
             "endpoint": "/radiology/worklist", "display": "total"},
            {"id": "tat-breaches", "title": "TAT breaches (reported)",
             "endpoint": "/radiology/stats/tat", "display": "value",
             "field": "breaches"},
            {"id": "unreported-stat",
             "title": "STAT studies not yet reported",
             "endpoint": "/radiology/stats/tat", "display": "value",
             "field": "unreported_stat"},
            {"id": "critical-open",
             "title": "Critical results awaiting closure",
             "endpoint": "/radiology/critical", "display": "total"},
        ]}


def default_critical_rule_payload() -> dict:
    return {
        "name": "Radiology critical-result escalation",
        "description": ("Phase 15: when the radiology critical-result "
                        "scan finds a flagged result still "
                        "unacknowledged past the radiology-ops SLA, it "
                        "emits radiology.critical.overdue; this rule "
                        "opens an escalation Task for the radiology "
                        "supervisor. The SLA itself lives in "
                        "terminology/radiology-ops (criticalSlaMinutes)."),
        "event": CRITICAL_EVENT,
        "condition": {"signal": "overdue", "op": "eq", "value": True},
        "action": {"type": "create_task",
                   "params": {"title": "Critical radiology result "
                                       "unacknowledged past SLA — escalate",
                              "ownerRole": "radiology-supervisor"}},
        "enabled": True,
    }


# ---------------------------------------------------------------------------
# Synthetic DICOM generation (demo seeding; tests generate their own)
# ---------------------------------------------------------------------------

def make_synthetic_dicom(*, study_uid: str, series_uid: str,
                         sop_uid: str, modality: str, patient_id: str,
                         patient_name: str = "Synthetic^Demo") -> bytes:
    """A tiny but real DICOM object (2x2, 8-bit pixel data) built with
    pydicom — the demo archive's pixel content. Raises when pydicom is
    unavailable rather than fabricating bytes."""
    if pydicom is None:
        raise RuntimeError("pydicom is not installed; cannot generate "
                           "synthetic DICOM objects")
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=file_meta,
                     preamble=b"\0" * 128)
    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.Modality = modality
    ds.PatientID = patient_id
    ds.PatientName = patient_name
    ds.StudyID = "SYNTH"
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    ds.Rows = 2
    ds.Columns = 2
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = bytes([0, 85, 170, 255])
    buffer = io.BytesIO()
    ds.save_as(buffer, enforce_file_format=True)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Demo department seeding (guarded on the demo patients existing)
# ---------------------------------------------------------------------------

_DEMO_STUDIES = (
    {"key": "rad-demo-ct-stat", "patient": "pat-1", "modality": "CT",
     "priority": "STAT", "sr_priority": "stat", "state": "ordered",
     "description": "CT chest with contrast — rule out pulmonary "
                    "embolism (synthetic demo)",
     "code": "71260", "display": "CT chest with contrast",
     "instance": False},
    {"key": "rad-demo-xr-done", "patient": "pat-2", "modality": "DX",
     "priority": "ROUTINE", "sr_priority": "routine", "state": "reported",
     "description": "Chest X-ray, 2 views (synthetic demo)",
     "code": "71046", "display": "Chest X-ray, 2 views",
     "instance": True,
     "conclusion": "No acute cardiopulmonary process. Heart size "
                   "normal; lungs clear. (Synthetic demo report.)"},
    {"key": "rad-demo-mr-arrived", "patient": "pat-3", "modality": "MR",
     "priority": "URGENT", "sr_priority": "urgent", "state": "arrived",
     "description": "MR brain without contrast (synthetic demo)",
     "code": "70551", "display": "MR brain without contrast",
     "instance": True},
)

_MODALITY_DISPLAYS = {"CT": "Computed Tomography",
                      "DX": "Digital Radiography",
                      "MR": "Magnetic Resonance Imaging"}


def _demo_fhir_row(session: Session, resource_type: str,
                   resource_id: str, body: dict) -> FhirResource:
    row = FhirResource(resource_type=resource_type,
                       resource_id=resource_id, version=1, body=body)
    fhir._apply_projections(row, resource_type, body, resource_id)
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="fhir.create",
                 entity=f"{resource_type}/{resource_id}",
                 payload={"resource_type": resource_type, "version": 1,
                          "seeded_demo": True})
    return row


def _seed_demo_department(session: Session) -> None:
    """Seed the living department ONLY when the referenced demo
    patients (pat-1/2/3 from the synthetic seed bundle) exist in the
    store. Idempotent per study id."""
    patients: dict[str, FhirResource] = {}
    for pid in ("pat-1", "pat-2", "pat-3"):
        row = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Patient",
            FhirResource.resource_id == pid)).first()
        if row is None:
            return
        patients[pid] = row
    ops = _ops(session)
    stations = ops.get("stations") or {}
    store = default_cold_store()
    chain = ["ordered", "scheduled", "arrived", "in-progress",
             "completed", "reported"]
    for spec in _DEMO_STUDIES:
        existing = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "ImagingStudy",
            FhirResource.resource_id == spec["key"])).first()
        if existing is not None:
            continue
        patient_row = patients[spec["patient"]]
        name = ((patient_row.body or {}).get("name") or [{}])[0]
        pn = ""
        if isinstance(name, dict):
            pn = f"{name.get('family') or ''}^" \
                 f"{'^'.join(str(g) for g in (name.get('given') or []))}"
        study_uid = boards._new_study_uid()
        sr_id = f"{spec['key']}-sr"
        _demo_fhir_row(session, "ServiceRequest", sr_id, {
            "resourceType": "ServiceRequest", "id": sr_id,
            "status": "active", "intent": "order",
            "priority": spec["sr_priority"],
            "code": {"coding": [{
                "system": "http://www.ama-assn.org/go/cpt",
                "code": spec["code"], "display": spec["display"]}]},
            "subject": {"reference": f"Patient/{spec['patient']}"},
        })
        instances: list[dict] = []
        if spec["instance"] and pydicom is not None:
            series_uid = boards._new_study_uid()
            sop_uid = boards._new_study_uid()
            data = make_synthetic_dicom(
                study_uid=study_uid, series_uid=series_uid,
                sop_uid=sop_uid, modality=spec["modality"],
                patient_id=spec["patient"], patient_name=pn or
                "Synthetic^Demo")
            digest = hashlib.sha256(data).hexdigest()
            ref = store.put(digest, data, metadata={
                "study_uid": study_uid, "sop_uid": sop_uid,
                "seeded_demo": True})
            instances.append({"series_uid": series_uid,
                              "sop_uid": sop_uid, "ref": ref,
                              "hash": digest, "size": len(data)})
        study_body = {
            "resourceType": "ImagingStudy", "id": spec["key"],
            "status": "available",
            "identifier": [{"system": boards.DICOM_UID_SYSTEM,
                            "value": study_uid}],
            "modality": [{"system": boards.DCM_SYSTEM,
                          "code": spec["modality"],
                          "display": _MODALITY_DISPLAYS.get(
                              spec["modality"], spec["modality"])}],
            "subject": {"reference": f"Patient/{spec['patient']}"},
            "basedOn": [{"reference": f"ServiceRequest/{sr_id}"}],
            "started": _now_iso(),
            "numberOfSeries": 1 if instances else 0,
            "numberOfInstances": len(instances),
            "description": spec["description"],
        }
        _demo_fhir_row(session, "ImagingStudy", spec["key"], study_body)
        for inst in instances:
            session.add(RadDicomInstance(
                study_id=spec["key"], study_uid=study_uid,
                series_uid=inst["series_uid"], sop_uid=inst["sop_uid"],
                sop_class_uid="1.2.840.10008.5.1.4.1.1.7",
                modality=spec["modality"], patient_id=spec["patient"],
                series_number=1, instance_number=1,
                cold_ref=inst["ref"], content_hash=inst["hash"],
                size_bytes=inst["size"]))
        session.flush()
        state = spec["state"]
        history = [{"state": s, "at": _now_iso(), "by": "system"}
                   for s in chain[:chain.index(state) + 1]]
        session.add(RadWorklistEntry(
            study_id=spec["key"], study_uid=study_uid,
            patient_ref=f"Patient/{spec['patient']}",
            modality=spec["modality"], priority=spec["priority"],
            state=state,
            scheduled_at=_now() if state in ("arrived", "reported")
            else None,
            station_ae=stations.get(spec["modality"]),
            ordered_at=_now(), state_history=history))
        session.flush()
        if state == "reported":
            report_id = f"{spec['key']}-report"
            _demo_fhir_row(session, "DiagnosticReport", report_id, {
                "resourceType": "DiagnosticReport", "id": report_id,
                "status": "final", "category": RAD_CATEGORY,
                "code": {"coding": [{
                    "system": "http://www.ama-assn.org/go/cpt",
                    "code": spec["code"], "display": spec["display"]}]},
                "subject": {"reference": f"Patient/{spec['patient']}"},
                "basedOn": [{"reference": f"ServiceRequest/{sr_id}"}],
                "imagingStudy": [{"reference":
                                  f"ImagingStudy/{spec['key']}"}],
                "effectiveDateTime": _now_iso(), "issued": _now_iso(),
                "conclusion": spec["conclusion"],
            })
        append_audit(session, actor="system",
                     action="radiology.demo.seed",
                     entity=f"ImagingStudy/{spec['key']}",
                     payload={"study_uid": study_uid, "state": state,
                              "priority": spec["priority"],
                              "instances": len(instances)})
        session.flush()


def ensure_radiology_defaults(session: Session) -> None:
    """Seed this module's published Studio configuration defaults
    (idempotent), plus the guarded synthetic demo department."""
    _seed_published(session, "workflow", WORKFLOW_KEY, _workflow_payload())
    _seed_published(session, "terminology", OPS_KEY, default_ops_payload())
    _seed_published(session, "terminology", PROTOCOLS_KEY,
                    default_protocols_payload())
    for key, payload in default_form_payloads().items():
        _seed_published(session, "form", key, payload)
    _seed_published(session, "dashboard", DASHBOARD_KEY,
                    default_dashboard_payload())
    _seed_rule(session, "rule", CRITICAL_RULE_KEY,
               default_critical_rule_payload())
    _seed_demo_department(session)
    session.commit()


# ---------------------------------------------------------------------------
# RIS worklist
# ---------------------------------------------------------------------------

@router.get("/worklist")
def get_worklist(state: str | None = Query(default=None),
                 priority: str | None = Query(default=None),
                 modality: str | None = Query(default=None),
                 session: Session = Depends(get_db),
                 user: User = Depends(require_capability("boards.read"))):
    """The RIS worklist: every ImagingStudy joined to its worklist
    overlay (studies nobody has touched yet read as `ordered` with the
    priority derived from their order). Filters: state / priority /
    modality."""
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "ImagingStudy")
        .order_by(FhirResource.created_at.asc(),
                  FhirResource.id.asc())).all()
    items = []
    for row in rows:
        entry = _entry_for(session, row)
        item = _entry_out(session, row, entry)
        if state and item["state"] != state:
            continue
        if priority and item["priority"] != priority:
            continue
        if modality and item["modality"] != modality:
            continue
        items.append(item)
    return {"items": items, "total": len(items)}


def _missing_screenings(session: Session, study: FhirResource,
                        entry: RadWorklistEntry) -> list[str]:
    """The protocol's required screening forms with no completed
    response attached to this study."""
    if not entry.protocol_code:
        return []
    concept = _protocol_concept(session, entry.protocol_code) or {}
    required = [f for f in (concept.get("screeningForms") or [])
                if isinstance(f, str)]
    done = _screening_state(study.body or {})
    return [f for f in required if f not in done]


@router.post("/worklist/{study_id}/transition")
async def transition_worklist(study_id: str, request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(
                                  require_capability("radiology.work"))):
    """Advance one study through the published radiology-flow workflow
    config. Illegal transitions 409; unknown states 422. Gates, both
    from the published radiology-ops config and flip-proven in tests:
    entering in-progress requires a protocol (protocolRequired) and
    completed protocol screenings (screeningRequired); entering
    reported requires a FINAL DiagnosticReport (boards' verify flow)."""
    payload = await _json_body(request)
    target = payload.get("to_status") or payload.get("status")
    if not target:
        raise HTTPException(status_code=422,
                            detail="Transition requires 'to_status'")
    study = _study_row(session, study_id)
    entry = _entry_for(session, study, create=True)
    transitions, known_states, roles = load_workflow_machine(
        session, WORKFLOW_KEY, DEFAULT_RAD_TRANSITIONS)
    if target not in known_states:
        raise HTTPException(status_code=422,
                            detail=f"Unknown radiology worklist status: "
                                   f"{target}")
    current = entry.state
    if target not in transitions.get(current, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Illegal radiology worklist transition "
                   f"'{current}' -> '{target}'")
    boards._check_transition_role(roles, current, target, user,
                                  "radiology worklist")
    ops = _ops(session)
    if target == "in-progress":
        if ops.get("protocolRequired") is True \
                and not entry.protocol_code:
            raise HTTPException(
                status_code=409,
                detail=(f"Protocol gate: study {study_id} cannot move "
                        "to 'in-progress' until a protocol from "
                        "terminology/imaging-protocols is assigned "
                        "(radiology-ops protocolRequired is on)"))
        if ops.get("screeningRequired") is True:
            missing = _missing_screenings(session, study, entry)
            if missing:
                raise HTTPException(
                    status_code=409,
                    detail=(f"Screening gate: study {study_id} cannot "
                            "move to 'in-progress' until pre-exam "
                            "screening is complete; missing: "
                            f"{', '.join(missing)} "
                            "(radiology-ops screeningRequired is on)"))
    if target == "reported" \
            and _first_final_report(session, study_id) is None:
        raise HTTPException(
            status_code=409,
            detail=(f"Study {study_id} cannot move to 'reported': it "
                    "has no final DiagnosticReport — draft the report "
                    "and verify it through the radiology report flow "
                    "first"))
    if payload.get("priority") is not None:
        new_priority = str(payload["priority"])
        if new_priority not in _priority_concepts(ops):
            raise HTTPException(
                status_code=422,
                detail=f"Unknown priority '{new_priority}': not in the "
                       "published radiology-ops priorities")
        entry.priority = new_priority
    if target == "scheduled":
        entry.scheduled_at = _parse_dt(payload.get("scheduled_at")) \
            or _now()
        station = payload.get("station_ae") or payload.get("stationAe")
        if not station:
            station = (ops.get("stations") or {}).get(entry.modality) \
                or ops.get("defaultStationAe")
        entry.station_ae = str(station) if station else None
    entry.state = target
    history = list(entry.state_history or [])
    history.append({"state": target, "at": _now_iso(),
                    "by": user.username})
    entry.state_history = history
    session.flush()
    append_audit(session, actor=user.username,
                 action="radiology.worklist.transition",
                 entity=f"ImagingStudy/{study_id}",
                 payload={"from_status": current, "to_status": target,
                          "priority": entry.priority,
                          "protocol_code": entry.protocol_code})
    session.commit()
    return _entry_out(session, _study_row(session, study_id),
                      _entry_for(session, _study_row(session, study_id)))


@router.post("/worklist/{study_id}/protocol")
async def assign_protocol(study_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(
                              require_capability("radiology.work"))):
    """Assign a protocol from the published imaging-protocols
    terminology. The protocol's modality must match the study's (422),
    and inactive/unknown protocols are refused (422)."""
    payload = await _json_body(request)
    code = payload.get("protocol_code") or payload.get("protocol")
    if not isinstance(code, str) or not code.strip():
        raise HTTPException(status_code=422,
                            detail="Protocol assignment requires "
                                   "'protocol_code'")
    study = _study_row(session, study_id)
    concept = _protocol_concept(session, code)
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown protocol '{code}': not in the published "
                   "imaging-protocols config")
    if concept.get("active") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Protocol '{code}' is inactive in the published "
                   "imaging-protocols config")
    modality = _study_modality(study.body or {})
    if concept.get("modality") and concept["modality"] != modality:
        raise HTTPException(
            status_code=422,
            detail=f"Protocol '{code}' is a {concept['modality']} "
                   f"protocol; study {study_id} is modality {modality}")
    entry = _entry_for(session, study, create=True)
    entry.protocol_code = code
    entry.protocolled_by = user.username
    entry.protocolled_at = _now()
    session.flush()
    append_audit(session, actor=user.username,
                 action="radiology.protocol.assign",
                 entity=f"ImagingStudy/{study_id}",
                 payload={"protocol_code": code,
                          "screening_forms":
                              concept.get("screeningForms") or []})
    session.commit()
    return _entry_out(session, _study_row(session, study_id),
                      _entry_for(session, _study_row(session, study_id)))


@router.post("/worklist/{study_id}/screening/{form_key}")
async def submit_screening(study_id: str, form_key: str,
                           request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(
                               require_capability("radiology.work"))):
    """Submit one governed pre-exam screening form for a study through
    the Phase 5 documentation machinery (documentation._store_response:
    published-form + required-answer validation) and attach the
    response to the study — the _attach_form_response pattern."""
    if form_key not in SCREENING_FORM_KEYS:
        raise HTTPException(
            status_code=422,
            detail=f"'{form_key}' is not a radiology screening form; "
                   f"use one of {list(SCREENING_FORM_KEYS)}")
    payload = await _json_body(request)
    study = _study_row(session, study_id)
    cfg = published_config(session, "form", form_key)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=f"No published screening form (form/{form_key}); "
                   "publish one in Studio before recording screening")
    patient_ref = _study_patient(study.body or {})
    created = documentation._store_response(
        session, user, form_key, patient_ref, payload.get("answers"))
    screening = _screening_state(study.body or {})
    screening[form_key] = {"responseId": created["id"],
                           "formVersion": cfg.version,
                           "completedBy": user.username,
                           "completedAt": _now_iso()}
    _set_study_extension(study, SCREENING_EXTENSION,
                         json.dumps(screening))
    session.flush()
    append_audit(session, actor=user.username,
                 action="radiology.screening.submit",
                 entity=f"ImagingStudy/{study_id}",
                 payload={"form_key": form_key,
                          "response_id": created["id"],
                          "form_version": cfg.version,
                          "patient": patient_ref})
    session.commit()
    return {"study_id": study_id, "response": created,
            "screening": screening}


# ---------------------------------------------------------------------------
# Radiation dose
# ---------------------------------------------------------------------------

def _dose_quantities(ops: dict, modality: str) -> list[dict]:
    quantities = (ops.get("doseQuantities") or {}).get(modality) or []
    return [q for q in quantities
            if isinstance(q, dict) and q.get("code")]


@router.post("/worklist/{study_id}/dose", status_code=201)
async def record_dose(study_id: str, request: Request,
                      session: Session = Depends(get_db),
                      user: User = Depends(
                          require_capability("radiology.work"))):
    """Record one radiation-dose quantity for a study. The quantity
    must be configured for the study's modality in radiology-ops
    (CTDIvol/DLP for CT, DAP for X-ray...) — anything else is a 422,
    including any quantity for a modality with none configured."""
    payload = await _json_body(request)
    study = _study_row(session, study_id)
    modality = _study_modality(study.body or {})
    ops = _ops(session)
    allowed = {q["code"]: q for q in _dose_quantities(ops, modality)}
    quantity = payload.get("quantity") or payload.get("code")
    if not isinstance(quantity, str) or quantity not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Quantity '{quantity}' is not a configured dose "
                   f"quantity for modality {modality or '?'} "
                   f"(radiology-ops allows: "
                   f"{sorted(allowed) or 'none'})")
    value = payload.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or value <= 0:
        raise HTTPException(status_code=422,
                            detail="Dose requires a positive numeric "
                                   "'value'")
    entry = _entry_for(session, study, create=True)
    record = RadDoseRecord(
        study_id=study_id, patient_ref=_study_patient(study.body or {}),
        modality=modality, quantity=quantity, value=float(value),
        unit=str(allowed[quantity].get("unit") or ""),
        recorded_by=user.username)
    session.add(record)
    session.flush()
    append_audit(session, actor=user.username,
                 action="radiology.dose.record",
                 entity=f"ImagingStudy/{study_id}",
                 payload={"quantity": quantity, "value": float(value),
                          "unit": record.unit, "modality": modality,
                          "patient": record.patient_ref})
    session.commit()
    return {"id": record.id, "study_id": study_id,
            "patient_ref": record.patient_ref, "modality": modality,
            "quantity": quantity, "value": float(value),
            "unit": record.unit, "recorded_by": user.username,
            "recorded_at": _ensure_aware(record.created_at).isoformat()
            if record.created_at else None}


@router.get("/patients/{patient_id}/dose-history")
def patient_dose_history(patient_id: str,
                         session: Session = Depends(get_db),
                         user: User = Depends(
                             require_capability("boards.read"))):
    """Per-patient cumulative dose history: every recorded quantity
    across the patient's studies, with per-quantity/unit totals."""
    patient_ref = f"Patient/{patient_id}"
    rows = session.scalars(select(RadDoseRecord).where(
        RadDoseRecord.patient_ref == patient_ref)
        .order_by(RadDoseRecord.created_at.asc(),
                  RadDoseRecord.id.asc())).all()
    items = [{"id": r.id, "study_id": r.study_id,
              "modality": r.modality, "quantity": r.quantity,
              "value": r.value, "unit": r.unit,
              "recorded_by": r.recorded_by,
              "recorded_at": _ensure_aware(r.created_at).isoformat()
              if r.created_at else None} for r in rows]
    totals: dict[tuple[str, str], dict] = {}
    for r in rows:
        bucket = totals.setdefault(
            (r.quantity, r.unit),
            {"quantity": r.quantity, "unit": r.unit, "total": 0.0,
             "count": 0})
        bucket["total"] += r.value
        bucket["count"] += 1
    return {"patient_ref": patient_ref, "items": items,
            "total": len(items),
            "totals": [totals[k] for k in sorted(totals)]}


# ---------------------------------------------------------------------------
# DICOM ingest + archive (BD-35)
# ---------------------------------------------------------------------------

def _find_study_by_uid(session: Session,
                       study_uid: str) -> FhirResource | None:
    rows = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "ImagingStudy")).all()
    for row in rows:
        if boards._study_uid(row.body or {}) == study_uid:
            return row
    return None


def _instance_out(record: RadDicomInstance) -> dict:
    return {"id": record.id, "study_id": record.study_id,
            "study_uid": record.study_uid,
            "series_uid": record.series_uid,
            "sop_uid": record.sop_uid,
            "sop_class_uid": record.sop_class_uid,
            "modality": record.modality,
            "patient_id": record.patient_id,
            "series_number": record.series_number,
            "instance_number": record.instance_number,
            "cold_ref": record.cold_ref,
            "content_hash": record.content_hash,
            "size_bytes": record.size_bytes}


@router.post("/dicom/ingest", status_code=201)
async def ingest_dicom(request: Request, session: Session = Depends(get_db),
                       user: User = Depends(
                           require_capability("radiology.work"))):
    """Ingest ONE DICOM object into the archive.

    Ingest shape (documented choice): a JSON body
    {"dicom_base64": "<base64 of the exact DICOM file bytes>"} — not
    multipart. The bytes are parsed with pydicom and validated
    (Study/Series/SOP Instance UIDs, Modality, PatientID — 422 naming
    what is missing; non-DICOM bytes 422). The Study Instance UID must
    match an existing (ordered) ImagingStudy and the DICOM PatientID
    must match that study's patient — ingest is for ordered studies,
    anything else is a 422 with the reason. The EXACT received bytes
    are stored through the tiering ColdStore, content-addressed by
    SHA-256; the instance row records ref + hash + size. Re-ingesting
    the same SOP Instance UID with identical bytes is idempotent; the
    same UID with different bytes is a 409."""
    if pydicom is None:
        raise HTTPException(
            status_code=503,
            detail="DICOM ingest is unavailable: pydicom is not "
                   "installed on this runtime")
    payload = await _json_body(request)
    encoded = payload.get("dicom_base64") or payload.get("dicomBase64")
    if not isinstance(encoded, str) or not encoded.strip():
        raise HTTPException(
            status_code=422,
            detail="DICOM ingest requires 'dicom_base64' (base64 of "
                   "the DICOM object bytes)")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422,
                            detail="'dicom_base64' is not valid base64")
    try:
        dataset = pydicom.dcmread(io.BytesIO(raw))
    except Exception:
        raise HTTPException(
            status_code=422,
            detail="The supplied bytes are not a DICOM object "
                   "(pydicom could not parse them)")
    values = {}
    missing = []
    for element in ("StudyInstanceUID", "SeriesInstanceUID",
                    "SOPInstanceUID", "Modality", "PatientID"):
        value = getattr(dataset, element, None)
        if value is None or not str(value).strip():
            missing.append(element)
        else:
            values[element] = str(value)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"DICOM object is missing required elements: "
                   f"{', '.join(missing)}")
    study = _find_study_by_uid(session, values["StudyInstanceUID"])
    if study is None:
        raise HTTPException(
            status_code=422,
            detail=f"No ImagingStudy with Study Instance UID "
                   f"'{values['StudyInstanceUID']}': DICOM ingest is "
                   "for ordered studies — create the study from its "
                   "order first")
    patient_ref = _study_patient(study.body or {})
    expected_patient = patient_ref.split("/", 1)[1] \
        if patient_ref.startswith("Patient/") else ""
    if expected_patient and values["PatientID"] != expected_patient:
        raise HTTPException(
            status_code=422,
            detail=f"DICOM PatientID '{values['PatientID']}' does not "
                   f"match the study's patient '{expected_patient}'")
    digest = hashlib.sha256(raw).hexdigest()
    existing = session.scalars(select(RadDicomInstance).where(
        RadDicomInstance.sop_uid == values["SOPInstanceUID"])).first()
    if existing is not None:
        if existing.content_hash == digest:
            return {**_instance_out(existing), "already_archived": True}
        raise HTTPException(
            status_code=409,
            detail=f"SOP Instance UID '{values['SOPInstanceUID']}' is "
                   "already archived with different bytes (hash "
                   "mismatch); refusing to overwrite an archived "
                   "instance")
    store = default_cold_store()
    ref = store.put(digest, raw, metadata={
        "study_uid": values["StudyInstanceUID"],
        "series_uid": values["SeriesInstanceUID"],
        "sop_uid": values["SOPInstanceUID"],
        "modality": values["Modality"], "ingested_by": user.username})
    record = RadDicomInstance(
        study_id=study.resource_id, study_uid=values["StudyInstanceUID"],
        series_uid=values["SeriesInstanceUID"],
        sop_uid=values["SOPInstanceUID"],
        sop_class_uid=str(getattr(dataset, "SOPClassUID", "") or ""),
        modality=values["Modality"], patient_id=values["PatientID"],
        series_number=int(dataset.SeriesNumber)
        if getattr(dataset, "SeriesNumber", None) is not None else None,
        instance_number=int(dataset.InstanceNumber)
        if getattr(dataset, "InstanceNumber", None) is not None else None,
        cold_ref=ref, content_hash=digest, size_bytes=len(raw))
    session.add(record)
    session.flush()
    # Keep the ImagingStudy's counts honest (boards' DICOMweb study
    # JSON reads numberOfSeries / numberOfInstances from the body).
    siblings = session.scalars(select(RadDicomInstance).where(
        RadDicomInstance.study_id == study.resource_id)).all()
    body = dict(study.body or {})
    body["numberOfInstances"] = len(siblings)
    body["numberOfSeries"] = len({s.series_uid for s in siblings})
    study.body = body
    study.version += 1
    session.flush()
    append_audit(session, actor=user.username,
                 action="radiology.dicom.ingest",
                 entity=f"ImagingStudy/{study.resource_id}",
                 payload={"sop_uid": record.sop_uid,
                          "series_uid": record.series_uid,
                          "study_uid": record.study_uid,
                          "content_hash": digest,
                          "size_bytes": len(raw), "cold_ref": ref})
    session.commit()
    return {**_instance_out(record), "already_archived": False}


def _study_instances(session: Session,
                     study_uid: str) -> list[RadDicomInstance]:
    return list(session.scalars(select(RadDicomInstance).where(
        RadDicomInstance.study_uid == study_uid)
        .order_by(RadDicomInstance.series_number.asc(),
                  RadDicomInstance.instance_number.asc(),
                  RadDicomInstance.id.asc())).all())


@router.get("/dicomweb/studies/{study_uid}/series")
def qido_series(study_uid: str, session: Session = Depends(get_db),
                user: User = Depends(require_capability("boards.read"))):
    """QIDO-RS series-level metadata (DICOM JSON) for one archived
    study — under /radiology/dicomweb so it never collides with
    boards' study-level /dicomweb routes."""
    if _find_study_by_uid(session, study_uid) is None:
        raise HTTPException(
            status_code=404,
            detail=f"No study with Study Instance UID '{study_uid}'")
    series: dict[str, list[RadDicomInstance]] = {}
    for record in _study_instances(session, study_uid):
        series.setdefault(record.series_uid, []).append(record)
    out = []
    for series_uid, records in series.items():
        first = records[0]
        item: dict[str, dict] = {
            "0020000D": {"vr": "UI", "Value": [study_uid]},
            "0020000E": {"vr": "UI", "Value": [series_uid]},
            "00080060": {"vr": "CS", "Value": [first.modality]},
            "00201209": {"vr": "IS", "Value": [len(records)]},
        }
        if first.series_number is not None:
            item["00200011"] = {"vr": "IS",
                                "Value": [first.series_number]}
        out.append(item)
    return out


@router.get("/dicomweb/studies/{study_uid}/series/{series_uid}/instances")
def qido_instances(study_uid: str, series_uid: str,
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("boards.read"))):
    """QIDO-RS instance-level metadata (DICOM JSON) for one series."""
    records = [r for r in _study_instances(session, study_uid)
               if r.series_uid == series_uid]
    if not records:
        raise HTTPException(
            status_code=404,
            detail=f"No series '{series_uid}' in study '{study_uid}'")
    out = []
    for record in records:
        item: dict[str, dict] = {
            "0020000D": {"vr": "UI", "Value": [study_uid]},
            "0020000E": {"vr": "UI", "Value": [series_uid]},
            "00080018": {"vr": "UI", "Value": [record.sop_uid]},
            "00080060": {"vr": "CS", "Value": [record.modality]},
        }
        if record.sop_class_uid:
            item["00080016"] = {"vr": "UI",
                                "Value": [record.sop_class_uid]}
        if record.instance_number is not None:
            item["00200013"] = {"vr": "IS",
                                "Value": [record.instance_number]}
        out.append(item)
    return out


@router.get("/dicomweb/studies/{study_uid}/series/{series_uid}"
            "/instances/{sop_uid}")
def wado_instance(study_uid: str, series_uid: str, sop_uid: str,
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability("boards.read"))):
    """WADO-RS retrieve instance: the archived DICOM bytes as
    application/dicom. The bytes are re-hashed against the instance
    row before serving — a mismatch (or unreadable object) is a 500
    integrity error; unverified bytes are never served (the tiering
    precedent)."""
    record = session.scalars(select(RadDicomInstance).where(
        RadDicomInstance.study_uid == study_uid,
        RadDicomInstance.series_uid == series_uid,
        RadDicomInstance.sop_uid == sop_uid)).first()
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"No archived instance '{sop_uid}' in series "
                   f"'{series_uid}' of study '{study_uid}'")
    store = default_cold_store()
    try:
        data = store.get(record.cold_ref)
    except OSError:
        raise HTTPException(
            status_code=500,
            detail=f"Integrity error: archived object "
                   f"{record.cold_ref} for SOP Instance UID {sop_uid} "
                   "could not be read")
    digest = hashlib.sha256(data).hexdigest()
    if digest != record.content_hash:
        raise HTTPException(
            status_code=500,
            detail=f"Integrity error: archived object for SOP Instance "
                   f"UID {sop_uid} hashes to {digest}, which does not "
                   f"match the recorded content_hash "
                   f"{record.content_hash}; refusing to serve "
                   "unverified bytes")
    return Response(content=data, media_type="application/dicom")


# ---------------------------------------------------------------------------
# DICOM Modality Worklist
# ---------------------------------------------------------------------------

@router.get("/mwl")
def modality_worklist(session: Session = Depends(get_db),
                      user: User = Depends(require_capability("boards.read"))):
    """DICOM Modality Worklist (DICOM JSON, PS3.18): one entry per
    study in the scheduled/arrived worklist states, with the Scheduled
    Procedure Step sequence carrying the station AE title and the
    scheduled date/time (from the worklist entry, seeded from the
    radiology-ops station config at scheduling time)."""
    entries = session.scalars(select(RadWorklistEntry).where(
        RadWorklistEntry.state.in_(("scheduled", "arrived")))
        .order_by(RadWorklistEntry.scheduled_at.asc(),
                  RadWorklistEntry.id.asc())).all()
    out = []
    for entry in entries:
        study = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "ImagingStudy",
            FhirResource.resource_id == entry.study_id)).first()
        if study is None:
            continue
        body = study.body or {}
        pn, patient_id = boards._patient_name_pn(
            session, _study_patient(body))
        when = _ensure_aware(entry.scheduled_at) \
            or _ensure_aware(entry.ordered_at) or _now()
        step: dict[str, dict] = {
            "00080060": {"vr": "CS", "Value": [entry.modality]},
            "00400001": {"vr": "AE",
                         "Value": [entry.station_ae or ""]},
            "00400002": {"vr": "DA",
                         "Value": [when.strftime("%Y%m%d")]},
            "00400003": {"vr": "TM",
                         "Value": [when.strftime("%H%M%S")]},
            "00400009": {"vr": "SH", "Value": [entry.study_id]},
        }
        out.append({
            "00100010": {"vr": "PN", "Value": [{"Alphabetic": pn}]},
            "00100020": {"vr": "LO", "Value": [patient_id]},
            "00080050": {"vr": "SH", "Value": [entry.study_id]},
            "0020000D": {"vr": "UI", "Value": [entry.study_uid]},
            "00080060": {"vr": "CS", "Value": [entry.modality]},
            "00400100": {"vr": "SQ", "Value": [step]},
        })
    return out


# ---------------------------------------------------------------------------
# Structured reporting + addenda (boards' report flow, never forked)
# ---------------------------------------------------------------------------

def _report_code_for(session: Session, study_body: dict) -> dict:
    """The report code from the study's order, exactly as boards'
    report creation derives it."""
    code = {"text": "Imaging study report"}
    based_on = study_body.get("basedOn") or []
    if based_on and isinstance(based_on[0], dict) \
            and str(based_on[0].get("reference", "")).startswith(
                "ServiceRequest/"):
        sr_id = based_on[0]["reference"].split("/", 1)[1]
        try:
            sr_body = fhir._get_row("ServiceRequest", sr_id,
                                    session).body or {}
        except HTTPException:
            sr_body = {}
        if isinstance(sr_body.get("code"), dict) and sr_body.get("code"):
            code = sr_body["code"]
    return code


@router.post("/studies/{study_id}/structured-report", status_code=201)
async def structured_report(study_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(
                                require_capability("radiology.work"))):
    """File a structured report for a study: `template` names a
    governed form (seeded: radiology-report-ct-chest,
    radiology-report-xr-chest); `answers` are stored as a
    QuestionnaireResponse through the documentation machinery and
    composed, in template order, into the conclusion of a Diagnostic
    Report built exactly the way boards builds one (preliminary; one
    open preliminary per study, 409). Verification stays boards'
    /radiology/reports/{id}/verify — this endpoint never verifies."""
    payload = await _json_body(request)
    template = payload.get("template") or payload.get("form_key")
    if not isinstance(template, str) or not template.strip():
        raise HTTPException(
            status_code=422,
            detail="Structured reporting requires 'template' (a "
                   "published report-template form key)")
    cfg = published_config(session, "form", template)
    if cfg is None:
        raise HTTPException(
            status_code=422,
            detail=f"Report template '{template}' is not a published "
                   "form in Studio")
    study = _study_row(session, study_id)
    study_body = study.body or {}
    for existing in boards._study_reports(session, study_id):
        if (existing.body or {}).get("status") == "preliminary":
            raise HTTPException(
                status_code=409,
                detail=f"ImagingStudy/{study_id} already has a "
                       f"preliminary report "
                       f"(DiagnosticReport/{existing.resource_id}); "
                       "verify it before drafting another")
    patient_ref = _study_patient(study_body)
    answers_by_id: dict[str, object] = {}
    for item in payload.get("answers") or []:
        if isinstance(item, dict) and item.get("linkId"):
            answer_list = item.get("answer") or []
            if answer_list and isinstance(answer_list[0], dict):
                value, _unit = documentation._answer_value(answer_list[0])
                answers_by_id[item["linkId"]] = value
    lines = []
    for form_item in (cfg.payload or {}).get("item") or []:
        if not isinstance(form_item, dict):
            continue
        if form_item.get("type") in ("group", "display"):
            continue
        value = answers_by_id.get(form_item.get("linkId"))
        if value is None or value == "":
            continue
        lines.append(f"{form_item.get('text')}: {value}")
    conclusion = "\n".join(lines)
    if not conclusion:
        raise HTTPException(
            status_code=422,
            detail="The structured answers produced an empty report "
                   "conclusion; answer at least one template item")
    created = documentation._store_response(
        session, user, template, patient_ref, payload.get("answers"))
    qr_row = fhir._get_row("QuestionnaireResponse", created["id"],
                           session)
    qr_body = dict(qr_row.body or {})
    qr_body["extension"] = (qr_body.get("extension") or []) + [{
        "url": STRUCTURED_EXTENSION,
        "valueReference": {"reference": f"ImagingStudy/{study_id}"}}]
    qr_row.body = qr_body
    qr_row.version += 1
    session.flush()
    report_body = {
        "resourceType": "DiagnosticReport",
        "id": str(uuid.uuid4()),
        "status": "preliminary",
        "category": RAD_CATEGORY,
        "code": _report_code_for(session, study_body),
        "subject": {"reference": patient_ref},
        "basedOn": study_body.get("basedOn") or [],
        "imagingStudy": [{"reference": f"ImagingStudy/{study_id}"}],
        "effectiveDateTime": _now_iso(),
        "conclusion": conclusion,
        "note": [{"text": f"Structured report from governed form "
                          f"{template} v{cfg.version}; answers in "
                          f"QuestionnaireResponse/{created['id']}"}],
    }
    report = fhir.create_resource("DiagnosticReport", report_body,
                                  session, user)
    append_audit(session, actor=user.username,
                 action="radiology.report.structured",
                 entity=f"DiagnosticReport/{report['id']}",
                 payload={"study_id": study_id, "template": template,
                          "template_version": cfg.version,
                          "response_id": created["id"]})
    session.commit()
    return {**report, "questionnaireResponseId": created["id"],
            "template": template}


@router.post("/reports/{report_id}/addendum", status_code=201)
async def addendum(report_id: str, request: Request,
                   session: Session = Depends(get_db),
                   user: User = Depends(
                       require_capability("radiology.work"))):
    """Addendum: create a NEW final DiagnosticReport carrying the
    corrected conclusion, linked to the original via the addendum-of
    extension, and mark the ORIGINAL `corrected` (FHIR status). The
    original's content is otherwise preserved. Only a final report
    can be addended (preliminary -> 409: verify it first; corrected ->
    409: addend the current final report instead)."""
    payload = await _json_body(request)
    conclusion = payload.get("conclusion")
    if not isinstance(conclusion, str) or not conclusion.strip():
        raise HTTPException(
            status_code=422,
            detail="An addendum requires 'conclusion' (the corrected "
                   "report text)")
    original = fhir._get_row("DiagnosticReport", report_id, session)
    original_body = original.body or {}
    if not original_body.get("imagingStudy"):
        raise HTTPException(
            status_code=422,
            detail=f"DiagnosticReport/{report_id} is not an imaging "
                   "report (no imagingStudy link)")
    if original_body.get("status") != "final":
        raise HTTPException(
            status_code=409,
            detail=f"Report {report_id} cannot be addended from "
                   f"status '{original_body.get('status')}': only a "
                   "final report is addended (a corrected report is "
                   "superseded — addend its successor instead)")
    new_body = {
        "resourceType": "DiagnosticReport",
        "id": str(uuid.uuid4()),
        "status": "final",
        "category": original_body.get("category") or RAD_CATEGORY,
        "code": original_body.get("code") or {"text": "Imaging study "
                                                      "report"},
        "subject": original_body.get("subject") or {},
        "basedOn": original_body.get("basedOn") or [],
        "imagingStudy": original_body.get("imagingStudy") or [],
        "effectiveDateTime": _now_iso(),
        "issued": _now_iso(),
        "conclusion": conclusion,
        "extension": [{"url": ADDENDUM_OF_EXTENSION,
                       "valueReference": {
                           "reference": f"DiagnosticReport/{report_id}"}}],
        "note": [{"text": f"Addendum to DiagnosticReport/{report_id} "
                          f"by {user.username}"}],
    }
    created = fhir.create_resource("DiagnosticReport", new_body,
                                   session, user)
    body = dict(original.body or {})
    body["status"] = "corrected"
    extensions = [e for e in (body.get("extension") or [])
                  if not (isinstance(e, dict)
                          and e.get("url") == ADDENDUM_EXTENSION)]
    extensions.append({"url": ADDENDUM_EXTENSION,
                       "valueReference": {
                           "reference":
                               f"DiagnosticReport/{created['id']}"}})
    body["extension"] = extensions
    original.body = body
    original.status = "corrected"
    original.version += 1
    session.flush()
    append_audit(session, actor=user.username,
                 action="radiology.report.addendum",
                 entity=f"DiagnosticReport/{created['id']}",
                 payload={"addendum_of": report_id,
                          "study": (original_body.get("imagingStudy")
                                    or [{}])[0].get("reference")})
    append_audit(session, actor=user.username,
                 action="radiology.report.corrected",
                 entity=f"DiagnosticReport/{report_id}",
                 payload={"superseded_by": created["id"]})
    session.commit()
    return created


# ---------------------------------------------------------------------------
# Critical results — closed loop
# ---------------------------------------------------------------------------

def _create_task(session: Session, *, title: str, owner_display: str,
                 patient_ref: str, focus_ref: str) -> str:
    """A FHIR Task the way the rules engine creates them (app.rules
    _act_create_task pattern), linked to the patient + the report."""
    task_id = str(uuid.uuid4())
    body = {"resourceType": "Task", "id": task_id,
            "status": "requested", "intent": "order",
            "description": title, "authoredOn": _now_iso(),
            "requester": {"display": "radiology"},
            "focus": {"reference": focus_ref}}
    if owner_display:
        body["owner"] = {"display": owner_display}
    if patient_ref:
        body["for"] = {"reference": patient_ref}
    row = FhirResource(resource_type="Task", resource_id=task_id,
                       version=1, body=body)
    fhir._apply_projections(row, "Task", body, task_id)
    session.add(row)
    session.flush()
    return task_id


def _ordering_clinician(session: Session, report_body: dict) -> str:
    for ref in report_body.get("basedOn") or []:
        reference = str((ref or {}).get("reference") or "")
        if reference.startswith("ServiceRequest/"):
            try:
                sr_body = fhir._get_row(
                    "ServiceRequest", reference.split("/", 1)[1],
                    session).body or {}
            except HTTPException:
                continue
            requester = sr_body.get("requester") or {}
            if isinstance(requester, dict):
                if requester.get("display"):
                    return str(requester["display"])
                if requester.get("reference"):
                    return str(requester["reference"])
    return ""


def _critical_out(record: RadCriticalResult, ops: dict) -> dict:
    sla = ops.get("criticalSlaMinutes")
    sla = sla if isinstance(sla, (int, float)) \
        and not isinstance(sla, bool) else 30
    flagged = _ensure_aware(record.flagged_at)
    overdue = False
    minutes_overdue = None
    if record.state != "closed" and flagged is not None:
        deadline = flagged + timedelta(minutes=float(sla))
        if _now() > deadline:
            overdue = True
            minutes_overdue = round(
                (_now() - deadline).total_seconds() / 60.0, 2)
    return {
        "report_id": record.report_id, "study_id": record.study_id,
        "patient_ref": record.patient_ref,
        "ordering_clinician": record.ordering_clinician,
        "state": record.state, "task_id": record.task_id,
        "flagged_by": record.flagged_by,
        "flagged_at": flagged.isoformat() if flagged else None,
        "sla_minutes": sla, "overdue": overdue,
        "minutes_overdue": minutes_overdue,
        "readback_by": record.readback_by,
        "readback_text": record.readback_text,
        "readback_at": _ensure_aware(record.readback_at).isoformat()
        if record.readback_at else None,
        "closed_by": record.closed_by,
        "closed_at": _ensure_aware(record.closed_at).isoformat()
        if record.closed_at else None,
    }


def _critical_record(session: Session,
                     report_id: str) -> RadCriticalResult | None:
    return session.scalars(select(RadCriticalResult).where(
        RadCriticalResult.report_id == report_id)).first()


@router.post("/reports/{report_id}/critical", status_code=201)
async def flag_critical(report_id: str, request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(
                            require_capability("radiology.work"))):
    """Flag a FINAL imaging report as a critical result: opens the
    closed-loop record and a notification Task to the ordering
    clinician (from the order's requester, or the payload's
    `ordering_clinician`). Preliminary reports 409 — verify first."""
    payload = await _json_body(request)
    report = fhir._get_row("DiagnosticReport", report_id, session)
    body = report.body or {}
    if not body.get("imagingStudy"):
        raise HTTPException(
            status_code=422,
            detail=f"DiagnosticReport/{report_id} is not an imaging "
                   "report (no imagingStudy link)")
    if body.get("status") != "final":
        raise HTTPException(
            status_code=409,
            detail=f"Report {report_id} is not final (status "
                   f"'{body.get('status')}'); critical-result tracking "
                   "starts once the report is verified final")
    record = _critical_record(session, report_id)
    if record is not None and record.state != "closed":
        raise HTTPException(
            status_code=409,
            detail=f"Report {report_id} already has an open critical-"
                   f"result record (state '{record.state}')")
    study_ref = (body.get("imagingStudy") or [{}])[0].get("reference") \
        or ""
    study_id = study_ref.split("/", 1)[1] if "/" in study_ref else ""
    patient_ref = (body.get("subject") or {}).get("reference") or ""
    clinician = payload.get("ordering_clinician")
    if not isinstance(clinician, str) or not clinician.strip():
        clinician = _ordering_clinician(session, body) \
            or "ordering clinician"
    task_id = _create_task(
        session,
        title=f"Critical radiology result — read-back required "
              f"(DiagnosticReport/{report_id})",
        owner_display=clinician, patient_ref=patient_ref,
        focus_ref=f"DiagnosticReport/{report_id}")
    if record is None:
        record = RadCriticalResult(report_id=report_id)
        session.add(record)
    record.study_id = study_id
    record.patient_ref = patient_ref
    record.ordering_clinician = clinician
    record.state = "notified"
    record.task_id = task_id
    record.flagged_by = user.username
    record.flagged_at = _now()
    record.readback_by = None
    record.readback_text = None
    record.readback_at = None
    record.closed_by = None
    record.closed_at = None
    record.escalated_for = None
    session.flush()
    ops = _ops(session)
    append_audit(session, actor=user.username,
                 action="radiology.critical.flag",
                 entity=f"DiagnosticReport/{report_id}",
                 payload={"task_id": task_id,
                          "ordering_clinician": clinician,
                          "sla_minutes": ops.get("criticalSlaMinutes"),
                          "study_id": study_id})
    session.commit()
    return _critical_out(record, ops)


@router.post("/reports/{report_id}/critical/read-back")
async def critical_read_back(report_id: str, request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(
                                 require_capability("radiology.work"))):
    """Record the read-back: the receiving clinician (`reader`) reads
    the result back (`read_back_text`). Both are required — without a
    named reader and the read-back text the loop cannot close (422)."""
    payload = await _json_body(request)
    reader = payload.get("reader")
    if not isinstance(reader, str) or not reader.strip():
        raise HTTPException(
            status_code=422,
            detail="Read-back requires 'reader' (the identity of the "
                   "clinician who received the result)")
    text = payload.get("read_back_text") or payload.get("readBackText")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(
            status_code=422,
            detail="Read-back requires 'read_back_text' (what the "
                   "reader read back)")
    record = _critical_record(session, report_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Report {report_id} has no critical-result record")
    if record.state == "closed":
        raise HTTPException(
            status_code=409,
            detail=f"Critical result for report {report_id} is already "
                   "closed")
    if record.state == "read-back":
        raise HTTPException(
            status_code=409,
            detail=f"Critical result for report {report_id} already "
                   "has a recorded read-back")
    record.state = "read-back"
    record.readback_by = reader.strip()
    record.readback_text = text.strip()
    record.readback_at = _now()
    session.flush()
    append_audit(session, actor=user.username,
                 action="radiology.critical.read-back",
                 entity=f"DiagnosticReport/{report_id}",
                 payload={"reader": record.readback_by,
                          "task_id": record.task_id})
    session.commit()
    return _critical_out(record, _ops(session))


@router.post("/reports/{report_id}/critical/close")
def critical_close(report_id: str, session: Session = Depends(get_db),
                   user: User = Depends(
                       require_capability("radiology.work"))):
    """Close the critical-result loop — ONLY after a read-back exists
    (409 otherwise). Closure completes the notification Task."""
    record = _critical_record(session, report_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Report {report_id} has no critical-result record")
    if record.state == "closed":
        raise HTTPException(
            status_code=409,
            detail=f"Critical result for report {report_id} is already "
                   "closed")
    if record.state != "read-back":
        raise HTTPException(
            status_code=409,
            detail=f"Critical result for report {report_id} cannot "
                   f"close from state '{record.state}': the read-back "
                   "must be recorded first")
    record.state = "closed"
    record.closed_by = user.username
    record.closed_at = _now()
    if record.task_id:
        task = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Task",
            FhirResource.resource_id == record.task_id)).first()
        if task is not None:
            task_body = dict(task.body or {})
            task_body["status"] = "completed"
            task.body = task_body
            task.status = "completed"
            task.version += 1
    session.flush()
    append_audit(session, actor=user.username,
                 action="radiology.critical.close",
                 entity=f"DiagnosticReport/{report_id}",
                 payload={"task_id": record.task_id,
                          "readback_by": record.readback_by})
    session.commit()
    return _critical_out(record, _ops(session))


@router.get("/critical")
def list_critical(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("boards.read"))):
    """All critical-result records, oldest first, each with its live
    overdue computation against the radiology-ops SLA."""
    ops = _ops(session)
    rows = session.scalars(select(RadCriticalResult)
                           .order_by(RadCriticalResult.flagged_at.asc(),
                                     RadCriticalResult.id.asc())).all()
    items = [_critical_out(r, ops) for r in rows]
    return {"items": items, "total": len(items)}


@router.post("/critical/scan")
def scan_critical(session: Session = Depends(get_db),
                  user: User = Depends(
                      require_capability("radiology.work"))):
    """Overdue scan: for every open critical result past the
    radiology-ops SLA that has not already been escalated for its
    current flag, emit `radiology.critical.overdue` into the rules
    engine — the seeded published rule turns it into an escalation
    Task (the workforce credential-scan pattern). Idempotent per
    flag: a second scan escalates nothing new."""
    ops = _ops(session)
    sla = ops.get("criticalSlaMinutes")
    sla = float(sla) if isinstance(sla, (int, float)) \
        and not isinstance(sla, bool) else 30.0
    now = _now()
    rows = session.scalars(select(RadCriticalResult).where(
        RadCriticalResult.state != "closed")
        .order_by(RadCriticalResult.flagged_at.asc())).all()
    escalated = []
    overdue_count = 0
    for record in rows:
        flagged = _ensure_aware(record.flagged_at)
        if flagged is None:
            continue
        deadline = flagged + timedelta(minutes=sla)
        if now <= deadline:
            continue
        overdue_count += 1
        if record.escalated_for is not None \
                and _ensure_aware(record.escalated_for) == flagged:
            continue
        minutes_overdue = round(
            (now - deadline).total_seconds() / 60.0, 2)
        firings = evaluate_rules(
            session, CRITICAL_EVENT,
            {"overdue": True, "report_id": record.report_id,
             "study_id": record.study_id,
             "patient_ref": record.patient_ref,
             "ordering_clinician": record.ordering_clinician,
             "minutes_overdue": minutes_overdue},
            actor=user.username)
        record.escalated_for = record.flagged_at
        escalated.append({"report_id": record.report_id,
                          "minutes_overdue": minutes_overdue,
                          "firings": len(firings)})
    session.flush()
    append_audit(session, actor=user.username,
                 action="radiology.critical.scan", entity="radiology",
                 payload={"scanned": len(rows), "overdue": overdue_count,
                          "escalated": len(escalated),
                          "sla_minutes": sla})
    session.commit()
    return {"scanned": len(rows), "overdue": overdue_count,
            "escalated": escalated, "total": len(escalated)}


# ---------------------------------------------------------------------------
# Turnaround-time stats (dashboard source)
# ---------------------------------------------------------------------------

@router.get("/stats/tat")
def tat_stats(session: Session = Depends(get_db),
              user: User = Depends(require_capability("boards.read"))):
    """Turnaround time, order -> final report, per priority, against
    the radiology-ops TAT targets: study counts, breach counts and
    exact minute totals/averages. Also the count of STAT studies with
    no final report yet (the dashboard's unreported-STAT widget)."""
    ops = _ops(session)
    buckets: dict[str, dict] = {}
    for code in _priority_concepts(ops):
        buckets[code] = {"target_minutes": _tat_target(ops, code),
                         "studies": 0, "breaches": 0,
                         "total_minutes": 0.0}
    studies = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "ImagingStudy")
        .order_by(FhirResource.created_at.asc(),
                  FhirResource.id.asc())).all()
    unreported_stat = 0
    for study in studies:
        entry = _entry_for(session, study)
        if entry is not None:
            priority = entry.priority
            ordered_at = _ensure_aware(entry.ordered_at)
        else:
            priority = _derive_priority(session, study.body or {}, ops)
            ordered_at = _ensure_aware(study.created_at)
        final = _first_final_report(session, study.resource_id)
        if final is None:
            if priority == "STAT":
                unreported_stat += 1
            continue
        if ordered_at is None:
            continue
        issued = _parse_dt((final.body or {}).get("issued")) \
            or _ensure_aware(final.created_at)
        if issued is None:
            continue
        minutes = (issued - ordered_at).total_seconds() / 60.0
        bucket = buckets.setdefault(
            priority, {"target_minutes": _tat_target(ops, priority),
                       "studies": 0, "breaches": 0, "total_minutes": 0.0})
        bucket["studies"] += 1
        bucket["total_minutes"] += minutes
        if bucket["target_minutes"] is not None \
                and minutes > bucket["target_minutes"]:
            bucket["breaches"] += 1
    by_priority = {}
    for code, bucket in buckets.items():
        count = bucket["studies"]
        by_priority[code] = {
            "target_minutes": bucket["target_minutes"],
            "studies": count, "breaches": bucket["breaches"],
            "total_minutes": bucket["total_minutes"],
            "avg_minutes": (bucket["total_minutes"] / count
                            if count else None)}
    return {"generated_at": _now_iso(),
            "total": sum(b["studies"] for b in buckets.values()),
            "breaches": sum(b["breaches"] for b in buckets.values()),
            "unreported_stat": unreported_stat,
            "by_priority": by_priority}
