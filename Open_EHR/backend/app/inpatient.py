"""Phase 6 — Inpatient operations: locations & beds, the ADT event
system, census + bed board, case management + discharge planning, and
anaesthesia / perioperative depth on the Phase 4B surgery board.

Built ON the Studio customization engine like every other module
(roadmap §6: configure, don't fork) — everything site-specific is
published Studio configuration, never hardcoded:

- the location hierarchy (facility -> unit -> room -> bed) is
  terminology config, key ``locations``; FHIR Location resources are
  MATERIALISED from it (a config publish that adds a unit or bed makes
  it appear on the board with no code change);
- bed statuses are data on the same payload (``bedStatuses``);
- discharge dispositions are terminology config, key
  ``discharge-dispositions``;
- ADT event-type labels (A01/A02/A03/A08) are terminology config, key
  ``adt-event-types``;
- the inpatient Encounter lifecycle is workflow config, key
  ``inpatient-flow``, loaded through the SAME shared machine loader
  (app.configstore.load_workflow_machine) that drives the fhir.py
  Appointment ADT machine — one engine, no fork;
- the discharge checklist, the pre-op checklist, the anaesthesia
  record + anaesthesia vitals forms, and the pre-op / PACU note
  templates are form configs; the ``preop`` / ``pacu`` note types are
  appended to the published note-types catalogue as a new governed
  version at startup (existing concepts preserved).

State that is NOT configuration: a bed's current status and occupant
live on its Location resource (extensions), an admission's discharge
plan lives on its Encounter (extension), and every movement writes
one row to the ADT event log (app.models.AdtEvent) in the same
transaction as the hash-chained AuditLog entry — bed status changes
are audited events, never silent edits.

Admission paths: POST /inpatient/admissions creates the inpatient
Encounter (class IMP) and assigns a bed in one act; an inpatient
Encounter created elsewhere (the ED board's admit endpoint, app.boards)
gets its bed through POST /inpatient/encounters/{id}/assign-bed.
Transfers release the old bed to `cleaning`; discharge closes the
encounter through the published inpatient-flow machine and releases
the bed to `cleaning`; housekeeping flips cleaning -> available.
"""
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import boards, documentation, fhir
from .audit import append_audit
from .configstore import (list_config_versions, load_workflow_machine,
                          published_config, transition_role_allowed)
from .models import (AdtEvent, AuditLog, ConfigStatus, FhirResource,
                     StudioConfig, User)
from .orders import PERFORMER_ROLE_EXTENSION
from .security import get_db, require_capability

router = APIRouter(prefix="/inpatient", tags=["inpatient"])

LOCATIONS_KEY = "locations"
DISPOSITIONS_KEY = "discharge-dispositions"
ADT_EVENT_TYPES_KEY = "adt-event-types"
INPATIENT_WORKFLOW_KEY = "inpatient-flow"

LOCATIONS_SYSTEM = "urn:opencare:location"
ACT_CODE_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
LOCATION_TYPE_SYSTEM = ("http://terminology.hl7.org/"
                        "CodeSystem/location-physical-type")

BED_STATUS_EXTENSION = "urn:opencare:extension/bed-status"
BED_OCCUPANT_EXTENSION = "urn:opencare:extension/bed-occupant"
DISCHARGE_PLAN_EXTENSION = "urn:opencare:extension/discharge-plan"
DISCHARGE_DISPOSITION_EXTENSION = "urn:opencare:extension/discharge-disposition"
PREOP_EXTENSION = "urn:opencare:extension/preop-checklist"
ANAESTHESIA_EXTENSION = "urn:opencare:extension/anaesthesia-record"
ANAESTHESIA_CASE_EXTENSION = "urn:opencare:extension/anaesthesia-case"

BED_STATUSES = ("available", "occupied", "cleaning", "blocked")
# Fallback lifecycle for the inpatient Encounter — the live machine is
# the published workflow config (seeded v1 with exactly these
# transitions); this constant is used only if that config is disabled.
DEFAULT_INPATIENT_TRANSITIONS: dict[str, set[str]] = {
    "in-progress": {"finished"},
    "finished": set(),
}
TASK_STATUSES = ("requested", "in-progress", "completed", "cancelled")
TASK_TERMINAL = ("completed", "cancelled")

# FHIR location physical-type codes per hierarchy level.
_PHYSICAL_TYPES = {"facility": ("si", "Site"), "unit": ("wa", "Ward"),
                   "room": ("ro", "Room"), "bed": ("bd", "Bed")}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


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


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


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

def default_locations_payload() -> dict:
    concepts: list[dict] = [
        {"code": "facility-main", "display": "OpenCare Hospital",
         "type": "facility", "active": True},
        {"code": "unit-medsurg", "display": "Medical-Surgical Unit",
         "type": "unit", "parent": "facility-main", "active": True},
        {"code": "unit-icu", "display": "Intensive Care Unit",
         "type": "unit", "parent": "facility-main", "active": True},
    ]
    for room in range(101, 107):
        for bed in ("A", "B"):
            concepts.append({
                "code": f"MS-{room}{bed}",
                "display": f"Med-Surg Room {room} Bed {bed}",
                "type": "bed", "parent": "unit-medsurg",
                "room": str(room), "bed": bed, "active": True})
    for room in range(201, 207):
        concepts.append({
            "code": f"ICU-{room}",
            "display": f"ICU Room {room}",
            "type": "bed", "parent": "unit-icu",
            "room": str(room), "bed": "1", "active": True})
    return {"name": "OpenCare hospital locations",
            "system": LOCATIONS_SYSTEM,
            "bedStatuses": list(BED_STATUSES),
            "concepts": concepts}


def default_dispositions_payload() -> dict:
    def disp(code, display):
        return {"code": code, "display": display, "active": True}

    return {"name": "OpenCare discharge dispositions",
            "system": "urn:opencare:discharge-disposition",
            "concepts": [
                disp("home", "Home"),
                disp("home-health", "Home with home health"),
                disp("snf", "Skilled nursing facility"),
                disp("rehab", "Inpatient rehabilitation"),
                disp("ltach", "Long-term acute care hospital"),
                disp("hospice", "Hospice"),
            ]}


def default_adt_event_types_payload() -> dict:
    def evt(code, display):
        return {"code": code, "display": display, "active": True}

    return {"name": "HL7 v2 ADT event types (subset used by OpenCare)",
            "system": "urn:opencare:adt-event",
            "concepts": [
                evt("A01", "Admit / visit notification"),
                evt("A02", "Transfer a patient"),
                evt("A03", "Discharge / end visit"),
                evt("A08", "Update patient information"),
            ]}


def default_inpatient_workflow_payload() -> dict:
    states = sorted(DEFAULT_INPATIENT_TRANSITIONS.keys())
    transitions = [
        {"from": src, "to": dst}
        for src in sorted(DEFAULT_INPATIENT_TRANSITIONS)
        for dst in sorted(DEFAULT_INPATIENT_TRANSITIONS[src])
    ]
    return {"states": states, "transitions": transitions}


def _form(title: str, items: list[dict]) -> dict:
    return {"resourceType": "Questionnaire", "title": title, "item": items}


def _item(link_id, text, type_, required=False):
    return {"linkId": link_id, "text": text, "type": type_, "required": required}


def _choice(link_id, text, options, required=False):
    item = _item(link_id, text, "choice", required)
    item["answerOption"] = [
        {"valueCoding": {"code": code, "display": display}}
        for code, display in options]
    return item


def default_form_payloads() -> dict[str, dict]:
    return {
        "discharge-checklist": _form("Discharge checklist", [
            _choice("disposition", "Discharge disposition",
                    [("home", "Home"),
                     ("home-health", "Home with home health"),
                     ("snf", "Skilled nursing facility"),
                     ("rehab", "Inpatient rehabilitation"),
                     ("ltach", "Long-term acute care hospital"),
                     ("hospice", "Hospice")], True),
            _item("target-discharge-date", "Target discharge date", "date", True),
            _item("meds-reconciled", "Medications reconciled", "boolean", True),
            _item("follow-up-scheduled", "Follow-up appointment scheduled",
                  "boolean"),
            _item("transport-arranged", "Transport arranged", "boolean"),
            _item("barriers", "Barriers to discharge", "text"),
            _item("notes", "Discharge planning notes", "text"),
        ]),
        "preop-checklist": _form("Pre-operative checklist", [
            _item("consent-signed", "Consent signed", "boolean", True),
            _item("site-marked", "Surgical site marked", "boolean", True),
            _item("allergies-reviewed", "Allergies reviewed", "boolean", True),
            _item("npo-confirmed", "NPO status confirmed", "boolean", True),
            _choice("antibiotics", "Prophylactic antibiotics",
                    [("given", "Given"), ("not-indicated", "Not indicated"),
                     ("pending", "Pending")]),
            _item("notes", "Pre-op notes", "text"),
        ]),
        "anaesthesia-record": _form("Anaesthesia record", [
            _choice("anaesthesia-type", "Anaesthesia type",
                    [("general", "General"), ("regional", "Regional"),
                     ("monitored-care", "Monitored anaesthesia care"),
                     ("local", "Local")], True),
            _choice("airway", "Airway",
                    [("natural", "Natural airway"), ("mask", "Face mask"),
                     ("lma", "Laryngeal mask airway"),
                     ("ett", "Endotracheal tube")], True),
            _item("agents", "Agents administered", "text", True),
            _item("fluids", "Fluids administered", "text"),
            _item("estimated-blood-loss-ml", "Estimated blood loss (mL)",
                  "integer"),
            _item("urine-output-ml", "Urine output (mL)", "integer"),
            _item("complications", "Complications", "text"),
        ]),
        "anaesthesia-vitals": _form("Anaesthesia vitals", [
            _item("systolic", "Systolic BP (mmHg)", "integer", True),
            _item("diastolic", "Diastolic BP (mmHg)", "integer"),
            _item("heart-rate", "Heart rate (bpm)", "integer", True),
            _item("spo2", "SpO2 (%)", "integer", True),
            _item("etco2", "EtCO2 (mmHg)", "integer"),
            _item("temperature", "Temperature (°C)", "decimal"),
            _item("notes", "Notes", "text"),
        ]),
        "note-preop": _form("Pre-operative assessment", [
            _item("assessment", "Assessment", "text", True),
            _item("plan", "Anaesthetic / surgical plan", "text", True),
            _item("risks-discussed", "Risks discussed with patient",
                  "boolean"),
        ]),
        "note-pacu": _form("Post-anaesthesia (PACU) note", [
            _item("recovery-status", "Recovery status", "text", True),
            _item("pain-score", "Pain score (0-10)", "integer"),
            _choice("nausea", "Nausea",
                    [("none", "None"), ("mild", "Mild"),
                     ("moderate", "Moderate"), ("severe", "Severe")]),
            _item("discharge-ready", "Ready for PACU discharge", "boolean"),
            _item("notes", "Notes", "text"),
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


def _extend_note_types(session: Session) -> None:
    """Append the Phase 6 note types (preop, pacu) to the PUBLISHED
    note-types catalogue as a new governed version — the governed way
    for config to evolve. Existing concepts (including any a site has
    customised) are preserved verbatim; nothing happens when both
    types are already present or the catalogue is unpublished."""
    cfg = published_config(session, "terminology", documentation.NOTE_TYPES_KEY)
    if cfg is None:
        return
    concepts = [c for c in (cfg.payload or {}).get("concepts") or []
                if isinstance(c, dict)]
    have = {c.get("code") for c in concepts}
    additions = [
        {"code": "preop", "display": "Pre-operative assessment",
         "active": True, "templateForm": "note-preop"},
        {"code": "pacu", "display": "Post-anaesthesia (PACU) note",
         "active": True, "templateForm": "note-pacu"},
    ]
    additions = [a for a in additions if a["code"] not in have]
    if not additions:
        return
    payload = dict(cfg.payload or {})
    payload["concepts"] = concepts + additions
    cfg.status = ConfigStatus.superseded
    session.flush()
    versions = list_config_versions(session, "terminology",
                                    documentation.NOTE_TYPES_KEY)
    row = StudioConfig(kind="terminology", key=documentation.NOTE_TYPES_KEY,
                       version=versions[-1].version + 1,
                       status=ConfigStatus.published, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    for action in ("studio.config.create", "studio.config.publish"):
        append_audit(session, actor="system", action=action,
                     entity=f"config:terminology/{documentation.NOTE_TYPES_KEY}@v{row.version}",
                     payload={"kind": "terminology",
                              "key": documentation.NOTE_TYPES_KEY,
                              "version": row.version,
                              "phase6_extension": [a["code"] for a in additions]})


def ensure_inpatient_defaults(session: Session) -> None:
    """Seed the Phase 6 Studio config as published v1 (idempotent) and
    materialise the Location resources from the locations config."""
    _seed_published(session, "terminology", LOCATIONS_KEY,
                    default_locations_payload())
    _seed_published(session, "terminology", DISPOSITIONS_KEY,
                    default_dispositions_payload())
    _seed_published(session, "terminology", ADT_EVENT_TYPES_KEY,
                    default_adt_event_types_payload())
    _seed_published(session, "workflow", INPATIENT_WORKFLOW_KEY,
                    default_inpatient_workflow_payload())
    for key, payload in default_form_payloads().items():
        _seed_published(session, "form", key, payload)
    _extend_note_types(session)
    _materialise_locations(session)
    session.commit()


# ---------------------------------------------------------------------------
# Config accessors
# ---------------------------------------------------------------------------

def _concepts(session: Session, key: str, label: str) -> list[dict]:
    cfg = published_config(session, "terminology", key)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=(f"No published {label} (terminology/{key}); publish one "
                    "in Studio before using inpatient operations"))
    return [c for c in (cfg.payload or {}).get("concepts") or []
            if isinstance(c, dict)]


def _concept(session: Session, key: str, label: str, code: str) -> dict | None:
    for concept in _concepts(session, key, label):
        if concept.get("code") == code:
            return concept
    return None


def _location_concepts(session: Session) -> tuple[list[dict], list[dict]]:
    """(units, beds) from the published locations config, in payload
    order. Facility concepts are structure only (not board sections)."""
    concepts = _concepts(session, LOCATIONS_KEY, "locations")
    units = [c for c in concepts if c.get("type") == "unit"]
    beds = [c for c in concepts if c.get("type") == "bed"]
    return units, beds


def _bed_statuses(session: Session) -> tuple[str, ...]:
    cfg = published_config(session, "terminology", LOCATIONS_KEY)
    values = (cfg.payload or {}).get("bedStatuses") if cfg else None
    if isinstance(values, list) and values and all(
            isinstance(v, str) and v for v in values):
        return tuple(values)
    return BED_STATUSES


def _event_display(session: Session, event_type: str) -> str | None:
    cfg = published_config(session, "terminology", ADT_EVENT_TYPES_KEY)
    if cfg is None:
        return None
    for concept in (cfg.payload or {}).get("concepts") or []:
        if isinstance(concept, dict) and concept.get("code") == event_type:
            return concept.get("display")
    return None


# ---------------------------------------------------------------------------
# Location materialisation + bed state
# ---------------------------------------------------------------------------

def _materialise_locations(session: Session) -> list[str]:
    """Create any Location resources the published locations config
    declares but the store lacks (idempotent). Existing rows are live
    operational state and are never touched. Returns created codes."""
    cfg = published_config(session, "terminology", LOCATIONS_KEY)
    if cfg is None:
        return []
    existing = {r.resource_id for r in session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Location")).all()}
    created: list[str] = []
    for concept in (cfg.payload or {}).get("concepts") or []:
        if not isinstance(concept, dict):
            continue
        code = concept.get("code")
        if not code or code in existing or concept.get("active") is False:
            continue
        level = concept.get("type") or "bed"
        body: dict = {
            "resourceType": "Location",
            "id": code,
            "status": "active",
            "name": concept.get("display") or code,
        }
        phys = _PHYSICAL_TYPES.get(level)
        if phys:
            body["physicalType"] = {"coding": [{
                "system": LOCATION_TYPE_SYSTEM,
                "code": phys[0], "display": phys[1]}]}
        if concept.get("parent"):
            body["partOf"] = {"reference": f"Location/{concept['parent']}"}
        if level == "bed":
            body["extension"] = [{"url": BED_STATUS_EXTENSION,
                                  "valueCode": "available"}]
        row = FhirResource(resource_type="Location", resource_id=code,
                           version=1, body=body)
        fhir._apply_projections(row, "Location", body, code)
        session.add(row)
        created.append(code)
    if created:
        session.flush()
        append_audit(session, actor="system",
                     action="inpatient.locations.materialise",
                     entity="Location",
                     payload={"created": created})
    return created


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


def _drop_extension(body: dict, url: str) -> None:
    body["extension"] = [e for e in (body.get("extension") or [])
                         if not (isinstance(e, dict) and e.get("url") == url)]


def _bed_row(session: Session, bed_code: str) -> FhirResource:
    _materialise_locations(session)
    row = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Location",
                                   FhirResource.resource_id == bed_code)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Location/{bed_code} not found")
    return row


def _bed_status(body: dict) -> str:
    ext = _get_extension(body or {}, BED_STATUS_EXTENSION)
    if ext is not None and isinstance(ext.get("valueCode"), str):
        return ext["valueCode"]
    return "available"


def _bed_occupant(body: dict) -> str | None:
    ext = _get_extension(body or {}, BED_OCCUPANT_EXTENSION)
    if ext is not None:
        ref = ((ext.get("valueReference") or {}).get("reference"))
        return ref if isinstance(ref, str) else None
    return None


def _set_bed_status(session: Session, user: User | None, row: FhirResource,
                    status: str, occupant_ref: str | None = None,
                    reason: str = "") -> None:
    """The ONLY path that changes a bed's status: mutation + version
    bump + hash-chained audit row, in the caller's transaction."""
    body = dict(row.body or {})
    _set_extension(body, BED_STATUS_EXTENSION, valueCode=status)
    if occupant_ref:
        _set_extension(body, BED_OCCUPANT_EXTENSION,
                       valueReference={"reference": occupant_ref})
    else:
        _drop_extension(body, BED_OCCUPANT_EXTENSION)
    row.body = body
    row.version += 1
    session.flush()
    append_audit(session, actor=user.username if user else "system",
                 action="inpatient.bed.status",
                 entity=f"Location/{row.resource_id}",
                 payload={"status": status, "occupant": occupant_ref,
                          "reason": reason, "version": row.version})


# ---------------------------------------------------------------------------
# ADT event log
# ---------------------------------------------------------------------------

def _record_event(session: Session, user: User, event_type: str,
                  encounter_id: str | None, patient_ref: str | None,
                  from_location: str | None = None,
                  to_location: str | None = None,
                  detail: dict | None = None) -> AdtEvent:
    row = AdtEvent(event_type=event_type, encounter_id=encounter_id,
                   patient_ref=patient_ref, from_location=from_location,
                   to_location=to_location, actor=user.username,
                   detail=detail or {})
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action=f"inpatient.adt.{event_type}",
                 entity=f"Encounter/{encounter_id}" if encounter_id else "ADT",
                 payload={"event_type": event_type,
                          "patient": patient_ref,
                          "from_location": from_location,
                          "to_location": to_location,
                          **(detail or {})})
    # Phase 8: queue the outbound ADT^A0x in THIS transaction (the
    # interface outbox — a committed event can never lack its feed
    # messages). Lazy import: app.interop imports app.fhir/app.lab,
    # not this module, but the hook keeps the dependency one-way and
    # obvious. No enabled endpoint configured -> queues nothing.
    from .interop import enqueue_adt_event
    enqueue_adt_event(session, row)
    return row


def _event_out(session: Session, row: AdtEvent) -> dict:
    return {
        "id": row.id,
        "event_type": row.event_type,
        "event_display": _event_display(session, row.event_type),
        "encounter_id": row.encounter_id,
        "patient_ref": row.patient_ref,
        "from_location": row.from_location,
        "to_location": row.to_location,
        "actor": row.actor,
        "detail": row.detail or {},
        "at": _aware(row.created_at).isoformat() if row.created_at else None,
    }


# ---------------------------------------------------------------------------
# Encounter helpers
# ---------------------------------------------------------------------------

def _class_code(body: dict) -> str | None:
    cls = (body or {}).get("class")
    if isinstance(cls, dict):
        return cls.get("code")
    if isinstance(cls, list):
        for coding in cls:
            if isinstance(coding, dict) and coding.get("code"):
                return coding["code"]
    return None


def _inpatient_row(session: Session, encounter_id: str) -> FhirResource:
    row = fhir._get_row("Encounter", encounter_id, session)
    if _class_code(row.body or {}) != "IMP":
        raise HTTPException(
            status_code=422,
            detail=f"Encounter/{encounter_id} is not an inpatient (IMP) encounter")
    return row


def _body_encounter_ref(body: dict) -> str | None:
    encounter = (body or {}).get("encounter")
    if isinstance(encounter, dict):
        ref = encounter.get("reference")
        return ref if isinstance(ref, str) else None
    return None


def _subject_ref(body: dict) -> str | None:
    subject = (body or {}).get("subject") or {}
    return subject.get("reference") if isinstance(subject, dict) else None


def _current_bed(body: dict) -> str | None:
    """The bed code of the encounter's ACTIVE location entry, if any."""
    for entry in (body or {}).get("location") or []:
        if not isinstance(entry, dict) or entry.get("status") != "active":
            continue
        ref = ((entry.get("location") or {}).get("reference") or "")
        if ref.startswith("Location/"):
            return ref.split("/", 1)[1]
    return None


def _add_location_entry(body: dict, bed_code: str, start_iso: str) -> None:
    entries = [e for e in (body.get("location") or []) if isinstance(e, dict)]
    entries.append({"location": {"reference": f"Location/{bed_code}"},
                    "status": "active",
                    "period": {"start": start_iso}})
    body["location"] = entries


def _close_location_entry(body: dict, bed_code: str, end_iso: str) -> None:
    entries = []
    for entry in (body.get("location") or []):
        if isinstance(entry, dict):
            ref = ((entry.get("location") or {}).get("reference") or "")
            if ref == f"Location/{bed_code}" and entry.get("status") == "active":
                entry = dict(entry)
                entry["status"] = "completed"
                period = dict(entry.get("period") or {})
                period["end"] = end_iso
                entry["period"] = period
        entries.append(entry)
    body["location"] = entries


def _los(row: FhirResource, now: datetime) -> dict:
    """Length of stay for an encounter: admitted_at + LOS in days
    (1 decimal) computed from the period start — never stored."""
    start = _parse_dt(((row.body or {}).get("period") or {}).get("start"))
    if start is None:
        return {"admitted_at": None, "los_days": None}
    return {"admitted_at": start.isoformat(),
            "los_days": round((now - start).total_seconds() / 86400, 1)}


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


def _require_available_bed(session: Session, bed_code) -> tuple[dict, FhirResource]:
    """(concept, row) for a bed that may take a patient: the code must
    be a bed in the published locations config (422) and the bed's
    live status must be `available` (409 naming the current status)."""
    if not isinstance(bed_code, str) or not bed_code.strip():
        raise HTTPException(status_code=422, detail="A bed code is required ('bed')")
    concept = _concept(session, LOCATIONS_KEY, "locations", bed_code)
    if concept is None or concept.get("type") != "bed":
        raise HTTPException(
            status_code=422,
            detail=f"Unknown bed '{bed_code}': not a bed in the published "
                   "locations config")
    if concept.get("active") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Bed '{bed_code}' is inactive in the published "
                   "locations config")
    row = _bed_row(session, bed_code)
    status = _bed_status(row.body or {})
    if status != "available":
        raise HTTPException(
            status_code=409,
            detail=f"Bed {bed_code} is '{status}' and cannot take a patient")
    return concept, row


def _bed_entry(session: Session, concept: dict, now: datetime) -> dict:
    """One bed as the board/census render it: config identity + live
    status + occupant facts resolved from the occupant Encounter."""
    code = concept["code"]
    row = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Location",
                                   FhirResource.resource_id == code)).first()
    status = _bed_status(row.body or {}) if row is not None else "available"
    occupant_ref = _bed_occupant(row.body or {}) if row is not None else None
    entry = {
        "code": code,
        "display": concept.get("display"),
        "room": concept.get("room"),
        "bed": concept.get("bed"),
        "unit": concept.get("parent"),
        "status": status,
        "patient_ref": None,
        "encounter_id": None,
        "admitted_at": None,
        "los_days": None,
    }
    if occupant_ref and occupant_ref.startswith("Encounter/"):
        encounter_id = occupant_ref.split("/", 1)[1]
        enc = session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "Encounter",
                FhirResource.resource_id == encounter_id)).first()
        if enc is not None and (enc.body or {}).get("status") == "in-progress":
            entry["patient_ref"] = _subject_ref(enc.body or {})
            entry["encounter_id"] = encounter_id
            entry.update(_los(enc, now))
    return entry


def _plan_of(body: dict) -> dict | None:
    ext = _get_extension(body or {}, DISCHARGE_PLAN_EXTENSION)
    if ext is not None and isinstance(ext.get("valueString"), str):
        try:
            parsed = json.loads(ext["valueString"])
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _set_plan(session: Session, user: User, row: FhirResource, plan: dict) -> None:
    body = dict(row.body or {})
    _set_extension(body, DISCHARGE_PLAN_EXTENSION,
                   valueString=json.dumps(plan))
    row.body = body
    row.version += 1
    session.flush()
    append_audit(session, actor=user.username, action="inpatient.discharge.plan",
                 entity=f"Encounter/{row.resource_id}",
                 payload={"target_date": plan.get("targetDate"),
                          "disposition": plan.get("disposition"),
                          "checklist_response_id": plan.get("checklistResponseId"),
                          "version": row.version})


def _active_admission_row(session: Session,
                          patient_ref: str) -> FhirResource | None:
    """The patient's current admission: the newest in-progress IMP
    encounter, or None."""
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Encounter",
            FhirResource.patient_ref == patient_ref,
            FhirResource.status == "in-progress")
        .order_by(FhirResource.created_at.desc(), FhirResource.id.desc())).all()
    for row in rows:
        if _class_code(row.body or {}) == "IMP":
            return row
    return None


def _admission_detail(session: Session, row: FhirResource,
                      now: datetime | None = None) -> dict:
    now = now or _now()
    bed_code = _current_bed(row.body or {})
    bed = None
    if bed_code:
        concept = _concept(session, LOCATIONS_KEY, "locations", bed_code)
        if concept is not None:
            bed = _bed_entry(session, concept, now)
    return {
        "encounter": fhir._render(row),
        "bed_code": bed_code,
        "bed": bed,
        **_los(row, now),
        "discharge_plan": _plan_of(row.body or {}),
    }


# ---------------------------------------------------------------------------
# Locations, census, bed board
# ---------------------------------------------------------------------------

@router.get("/locations")
def list_locations(session: Session = Depends(get_db),
                   user: User = Depends(require_capability("inpatient.read"))):
    """The location hierarchy: config identity (facility/unit/bed,
    parent links) joined with each bed's live status. Locations added
    by a config publish are materialised here on first read."""
    _materialise_locations(session)
    session.commit()
    cfg = published_config(session, "terminology", LOCATIONS_KEY)
    now = _now()
    items = []
    for concept in _concepts(session, LOCATIONS_KEY, "locations"):
        item = {"code": concept.get("code"),
                "display": concept.get("display"),
                "type": concept.get("type"),
                "parent": concept.get("parent"),
                "active": concept.get("active") is not False}
        if concept.get("type") == "bed":
            entry = _bed_entry(session, concept, now)
            item.update({"status": entry["status"],
                         "patient_ref": entry["patient_ref"],
                         "encounter_id": entry["encounter_id"]})
        items.append(item)
    return {"locations": items, "version": cfg.version if cfg else None,
            "bed_statuses": list(_bed_statuses(session))}


@router.get("/census")
def census(session: Session = Depends(get_db),
           user: User = Depends(require_capability("inpatient.read"))):
    """Current census: per unit, the bed counts by live status and the
    occupancy percentage (computed here, from stored state + clock),
    plus the hospital totals and today's expected discharges."""
    _materialise_locations(session)
    session.commit()
    now = _now()
    units, beds = _location_concepts(session)
    beds_by_unit: dict[str, list[dict]] = {}
    for concept in beds:
        beds_by_unit.setdefault(concept.get("parent") or "", []).append(concept)

    def summarise(entries: list[dict]) -> dict:
        counts = {status: 0 for status in _bed_statuses(session)}
        for entry in entries:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        total = len(entries)
        occupied = counts.get("occupied", 0)
        return {"beds_total": total, **counts,
                "occupancy_pct": round(100.0 * occupied / total, 1) if total else 0.0}

    unit_views = []
    all_entries: list[dict] = []
    for unit in units:
        entries = [_bed_entry(session, c, now)
                   for c in beds_by_unit.get(unit["code"], [])]
        all_entries.extend(entries)
        unit_views.append({"code": unit["code"],
                           "display": unit.get("display"),
                           **summarise(entries), "beds": entries})
    expected = _expected_discharges(session, now.date().isoformat())
    return {"generated_at": now.isoformat(),
            "units": unit_views,
            "totals": summarise(all_entries),
            "expected_discharges_today": {"date": now.date().isoformat(),
                                          "discharges": expected,
                                          "total": len(expected)}}


@router.get("/beds")
def bed_listing(session: Session = Depends(get_db),
                user: User = Depends(require_capability("inpatient.read"))):
    """Patient-per-bed listing: every configured bed with its live
    status and, when occupied, the patient + admission facts."""
    _materialise_locations(session)
    session.commit()
    now = _now()
    units, beds = _location_concepts(session)
    unit_display = {u["code"]: u.get("display") for u in units}
    entries = []
    for concept in beds:
        entry = _bed_entry(session, concept, now)
        entry["unit_display"] = unit_display.get(concept.get("parent"))
        entries.append(entry)
    return {"beds": entries, "total": len(entries)}


@router.post("/beds/{bed_code}/status")
async def set_bed_status(bed_code: str, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("inpatient.work"))):
    """Housekeeping / bed-management flips. Occupancy is NEVER set
    here — a bed becomes occupied only through admit / assign-bed /
    transfer, and an occupied bed must be discharged or transferred
    first (409). Manual flips: cleaning -> available (housekeeping
    done), available <-> blocked, available -> cleaning."""
    payload = await _json_body(request)
    target = payload.get("status") or payload.get("to_status")
    if not isinstance(target, str) or not target:
        raise HTTPException(status_code=422, detail="Requires 'status'")
    if target not in _bed_statuses(session):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown bed status '{target}': not in the published "
                   "locations config's bedStatuses")
    if target == "occupied":
        raise HTTPException(
            status_code=422,
            detail="A bed becomes 'occupied' only through admission or "
                   "transfer, never a bare status flip")
    concept = _concept(session, LOCATIONS_KEY, "locations", bed_code)
    if concept is None or concept.get("type") != "bed":
        raise HTTPException(
            status_code=422,
            detail=f"Unknown bed '{bed_code}': not a bed in the published "
                   "locations config")
    row = _bed_row(session, bed_code)
    current = _bed_status(row.body or {})
    if current == "occupied":
        raise HTTPException(
            status_code=409,
            detail=f"Bed {bed_code} is occupied; discharge or transfer "
                   "the patient before changing its status")
    if target == current:
        raise HTTPException(
            status_code=409, detail=f"Bed {bed_code} is already '{current}'")
    _set_bed_status(session, user, row, target,
                    reason=str(payload.get("reason") or "manual flip"))
    session.commit()
    entry = _bed_entry(session, concept, _now())
    return {"bed": entry}


# ---------------------------------------------------------------------------
# Admissions & ADT: admit / assign-bed / transfer / discharge
# ---------------------------------------------------------------------------

def _occupy_bed(session: Session, user: User, bed_row: FhirResource,
                encounter_id: str, reason: str) -> None:
    _set_bed_status(session, user, bed_row, "occupied",
                    occupant_ref=f"Encounter/{encounter_id}", reason=reason)


@router.post("/admissions", status_code=201)
async def admit(request: Request, session: Session = Depends(get_db),
                user: User = Depends(require_capability("inpatient.work"))):
    """Admit a patient to a bed: creates the inpatient Encounter
    (class IMP, in-progress) with the bed as its active location,
    occupies the bed, and writes the A01 ADT event — one act, audited
    at every step. The bed must be `available` (409 otherwise)."""
    payload = await _json_body(request)
    patient_ref = _patient_ref_of(session, payload)
    existing = _active_admission_row(session, patient_ref)
    if existing is not None:
        current_bed = _current_bed(existing.body or {})
        hint = (f"they already occupy bed {current_bed} — transfer them "
                "instead" if current_bed else
                "assign that admission a bed instead")
        raise HTTPException(
            status_code=409,
            detail=(f"{patient_ref} already has an active inpatient "
                    f"admission (Encounter/{existing.resource_id}); {hint}"))
    concept, bed_row = _require_available_bed(session, payload.get("bed"))
    bed_code = concept["code"]
    now_iso = _now_iso()
    body = {
        "resourceType": "Encounter",
        "id": str(uuid.uuid4()),
        "status": "in-progress",
        "class": {"system": ACT_CODE_SYSTEM, "code": "IMP",
                  "display": "inpatient encounter"},
        "subject": {"reference": patient_ref},
        "period": {"start": now_iso},
        "location": [{"location": {"reference": f"Location/{bed_code}"},
                      "status": "active",
                      "period": {"start": now_iso}}],
    }
    reason = payload.get("reason") or payload.get("diagnosis")
    if isinstance(reason, str) and reason.strip():
        body["reasonCode"] = [{"text": reason}]
    encounter = fhir.create_resource("Encounter", body, session, user)
    _occupy_bed(session, user, bed_row, encounter["id"], "admission")
    event = _record_event(session, user, "A01", encounter["id"], patient_ref,
                          to_location=bed_code,
                          detail={"bed": bed_code,
                                  "unit": concept.get("parent")})
    session.commit()
    row = _inpatient_row(session, encounter["id"])
    return {"encounter": fhir._render(row),
            "bed": _bed_entry(session, concept, _now()),
            "event": _event_out(session, event)}


@router.post("/encounters/{encounter_id}/assign-bed")
async def assign_bed(encounter_id: str, request: Request,
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability("inpatient.work"))):
    """Give an existing inpatient Encounter its bed — the completion
    of an admission begun elsewhere (e.g. the ED board's admit, which
    creates the Encounter without a bed). Writes the A01 event. An
    encounter that already has a bed must be TRANSFERRED instead
    (409)."""
    payload = await _json_body(request)
    row = _inpatient_row(session, encounter_id)
    body = dict(row.body or {})
    if body.get("status") != "in-progress":
        raise HTTPException(
            status_code=409,
            detail=f"Encounter {encounter_id} is '{body.get('status')}'; "
                   "bed assignment requires an in-progress admission")
    if _current_bed(body) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Encounter {encounter_id} already has bed "
                   f"{_current_bed(body)}; use the transfer endpoint to move it")
    concept, bed_row = _require_available_bed(session, payload.get("bed"))
    bed_code = concept["code"]
    _add_location_entry(body, bed_code, _now_iso())
    row.body = body
    row.version += 1
    session.flush()
    _occupy_bed(session, user, bed_row, encounter_id, "bed assignment")
    patient_ref = _subject_ref(body)
    event = _record_event(session, user, "A01", encounter_id, patient_ref,
                          to_location=bed_code,
                          detail={"bed": bed_code,
                                  "unit": concept.get("parent"),
                                  "via": "assign-bed"})
    session.commit()
    return {"encounter": fhir._render(row),
            "bed": _bed_entry(session, concept, _now()),
            "event": _event_out(session, event)}


@router.post("/encounters/{encounter_id}/transfer")
async def transfer(encounter_id: str, request: Request,
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("inpatient.work"))):
    """Move an admitted patient to another bed: the old bed is
    released to `cleaning`, the new bed is occupied, the encounter's
    location history gains a completed + an active entry, and an A02
    ADT event records from -> to."""
    payload = await _json_body(request)
    row = _inpatient_row(session, encounter_id)
    body = dict(row.body or {})
    if body.get("status") != "in-progress":
        raise HTTPException(
            status_code=409,
            detail=f"Encounter {encounter_id} is '{body.get('status')}'; "
                   "only an in-progress admission can transfer")
    current_bed = _current_bed(body)
    if current_bed is None:
        raise HTTPException(
            status_code=422,
            detail=f"Encounter {encounter_id} has no current bed; use "
                   "assign-bed before transferring")
    if payload.get("bed") == current_bed:
        raise HTTPException(
            status_code=422,
            detail=f"Transfer target {current_bed} is the patient's current bed")
    concept, bed_row = _require_available_bed(session, payload.get("bed"))
    new_bed = concept["code"]
    now_iso = _now_iso()
    old_row = _bed_row(session, current_bed)
    _set_bed_status(session, user, old_row, "cleaning",
                    reason=f"transfer out (Encounter/{encounter_id})")
    _close_location_entry(body, current_bed, now_iso)
    _add_location_entry(body, new_bed, now_iso)
    row.body = body
    row.version += 1
    session.flush()
    _occupy_bed(session, user, bed_row, encounter_id, "transfer in")
    patient_ref = _subject_ref(body)
    event = _record_event(session, user, "A02", encounter_id, patient_ref,
                          from_location=current_bed, to_location=new_bed,
                          detail={"from_bed": current_bed, "to_bed": new_bed,
                                  "unit": concept.get("parent")})
    session.commit()
    return {"encounter": fhir._render(row),
            "bed": _bed_entry(session, concept, _now()),
            "event": _event_out(session, event)}


def _disposition_concept(session: Session, code) -> dict:
    concept = _concept(session, DISPOSITIONS_KEY, "discharge dispositions",
                       code) if isinstance(code, str) else None
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown discharge disposition '{code}': not in the "
                   "published discharge-dispositions config")
    if concept.get("active") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Discharge disposition '{code}' is inactive in the "
                   "published discharge-dispositions config")
    return concept


@router.post("/encounters/{encounter_id}/discharge")
async def discharge(encounter_id: str, request: Request,
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability("inpatient.work"))):
    """Discharge: closes the encounter through the published
    inpatient-flow workflow machine (in-progress -> finished, with
    per-transition roles honoured), ends its period, completes its
    location entry, releases the bed to `cleaning`, and writes the
    A03 ADT event. The disposition comes from the payload when given
    (validated against the published terminology), else from the
    discharge plan."""
    payload = await _json_body(request)
    row = _inpatient_row(session, encounter_id)
    body = dict(row.body or {})
    current = body.get("status")
    transitions, known_states, roles = load_workflow_machine(
        session, INPATIENT_WORKFLOW_KEY, DEFAULT_INPATIENT_TRANSITIONS)
    target = "finished"
    if target not in known_states or current not in known_states:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown inpatient encounter status: {current}")
    if target not in transitions.get(current, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Illegal inpatient transition '{current}' -> '{target}'")
    if not transition_role_allowed(roles, current, target, user.role.value):
        raise HTTPException(
            status_code=403,
            detail=(f"Role {user.role.value} may not perform the inpatient "
                    f"transition '{current}' -> '{target}': the published "
                    "workflow config restricts it to "
                    f"{', '.join(roles.get((current, target), []))}"))

    plan = _plan_of(body)
    disposition = payload.get("disposition")
    if disposition is None and plan:
        disposition = plan.get("disposition")
    if disposition is not None:
        _disposition_concept(session, disposition)

    bed_code = _current_bed(body)
    now_iso = _now_iso()
    body["status"] = "finished"
    period = dict(body.get("period") or {})
    period["end"] = now_iso
    body["period"] = period
    if bed_code:
        _close_location_entry(body, bed_code, now_iso)
    if disposition is not None:
        _set_extension(body, DISCHARGE_DISPOSITION_EXTENSION,
                       valueString=str(disposition))
    row.body = body
    row.status = "finished"
    row.version += 1
    session.flush()
    append_audit(session, actor=user.username, action="inpatient.encounter.discharge",
                 entity=f"Encounter/{encounter_id}",
                 payload={"disposition": disposition, "bed": bed_code,
                          "version": row.version})
    if bed_code:
        bed_row = _bed_row(session, bed_code)
        _set_bed_status(session, user, bed_row, "cleaning",
                        reason=f"discharge (Encounter/{encounter_id})")
    patient_ref = _subject_ref(body)
    event = _record_event(session, user, "A03", encounter_id, patient_ref,
                          from_location=bed_code,
                          detail={"bed": bed_code, "disposition": disposition})
    session.commit()
    return {"encounter": fhir._render(row),
            "bed_code_released": bed_code,
            "disposition": disposition,
            "event": _event_out(session, event)}


@router.get("/encounters/{encounter_id}")
def admission_detail(encounter_id: str, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("inpatient.read"))):
    row = _inpatient_row(session, encounter_id)
    return _admission_detail(session, row)


@router.get("/patients/{patient_id}/admission")
def patient_admission(patient_id: str, session: Session = Depends(get_db),
                      user: User = Depends(require_capability("inpatient.read"))):
    """The patient's current admission (in-progress IMP encounter),
    or {admission: null} — the chart's admission tab data source."""
    patient_ref = fhir._normalize_patient_ref(patient_id)
    row = _active_admission_row(session, patient_ref)
    if row is not None:
        return {"admission": _admission_detail(session, row)}
    return {"admission": None}


@router.get("/adt-events")
def adt_events(encounter_id: str | None = Query(default=None),
               patient: str | None = Query(default=None),
               limit: int = Query(default=100),
               session: Session = Depends(get_db),
               user: User = Depends(require_capability("inpatient.read"))):
    """The ADT event log, newest first — the movement history the bed
    board stands on (A01/A02/A03/A08 with config-driven labels)."""
    stmt = select(AdtEvent).order_by(AdtEvent.id.desc())
    if encounter_id:
        stmt = stmt.where(AdtEvent.encounter_id == encounter_id)
    if patient:
        stmt = stmt.where(
            AdtEvent.patient_ref == fhir._normalize_patient_ref(patient))
    rows = session.scalars(stmt.limit(max(1, min(limit, 500)))).all()
    return {"events": [_event_out(session, r) for r in rows],
            "total": len(rows)}


# ---------------------------------------------------------------------------
# Discharge planning + case management
# ---------------------------------------------------------------------------

@router.get("/discharge-dispositions")
def discharge_dispositions(session: Session = Depends(get_db),
                           user: User = Depends(require_capability("inpatient.read"))):
    """The published disposition value set (home / SNF / rehab / …) —
    Studio terminology, served so clients render exactly what is
    published, never a hardcoded list."""
    cfg = published_config(session, "terminology", DISPOSITIONS_KEY)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail="No published discharge dispositions "
                   "(terminology/discharge-dispositions)")
    return {"dispositions": [c for c in (cfg.payload or {}).get("concepts") or []
                             if isinstance(c, dict)],
            "version": cfg.version}


@router.post("/encounters/{encounter_id}/discharge-plan")
async def set_discharge_plan(encounter_id: str, request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability("inpatient.work"))):
    """Record / revise the discharge plan on the encounter (extension
    data: target date, disposition, barriers, checklist linkage).
    Plan changes are A08 ADT events — the update label of the HL7
    vocabulary — and are hash-audited."""
    payload = await _json_body(request)
    row = _inpatient_row(session, encounter_id)
    body = row.body or {}
    if body.get("status") != "in-progress":
        raise HTTPException(
            status_code=409,
            detail=f"Encounter {encounter_id} is '{body.get('status')}'; "
                   "discharge planning requires an in-progress admission")
    plan = dict(_plan_of(body) or {})
    if payload.get("disposition") is not None:
        _disposition_concept(session, payload["disposition"])
        plan["disposition"] = payload["disposition"]
    if payload.get("targetDate") is not None:
        target = payload["targetDate"]
        if not isinstance(target, str) or len(target) < 10:
            raise HTTPException(status_code=422,
                                detail="targetDate must be an ISO date string")
        plan["targetDate"] = target[:10]
    if payload.get("barriers") is not None:
        plan["barriers"] = str(payload["barriers"])
    if payload.get("checklistResponseId") is not None:
        plan["checklistResponseId"] = str(payload["checklistResponseId"])
    plan["updatedBy"] = user.username
    plan["updatedAt"] = _now_iso()
    _set_plan(session, user, row, plan)
    patient_ref = _subject_ref(body)
    event = _record_event(session, user, "A08", encounter_id, patient_ref,
                          detail={"discharge_plan": {
                              "target_date": plan.get("targetDate"),
                              "disposition": plan.get("disposition")}})
    session.commit()
    return {"encounter_id": encounter_id, "discharge_plan": plan,
            "event": _event_out(session, event)}


def _expected_discharges(session: Session, date_str: str) -> list[dict]:
    """In-progress inpatient encounters whose discharge plan targets
    the given date (default-resolution happens at the endpoint)."""
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Encounter",
            FhirResource.status == "in-progress")
        .order_by(FhirResource.created_at)).all()
    now = _now()
    out = []
    for row in rows:
        if _class_code(row.body or {}) != "IMP":
            continue
        plan = _plan_of(row.body or {})
        if not plan or plan.get("targetDate") != date_str:
            continue
        out.append({
            "encounter_id": row.resource_id,
            "patient_ref": _subject_ref(row.body or {}),
            "bed_code": _current_bed(row.body or {}),
            "target_date": plan.get("targetDate"),
            "disposition": plan.get("disposition"),
            "barriers": plan.get("barriers"),
            **_los(row, now),
        })
    return out


@router.get("/expected-discharges")
def expected_discharges(date: str | None = Query(default=None),
                        session: Session = Depends(get_db),
                        user: User = Depends(require_capability("inpatient.read"))):
    """Expected discharges for a date (defaults to today, UTC) — the
    feed the census / bed board shows for discharge planning."""
    date_str = date or _now().date().isoformat()
    discharges = _expected_discharges(session, date_str)
    return {"date": date_str, "discharges": discharges,
            "total": len(discharges)}


@router.post("/encounters/{encounter_id}/care-plan", status_code=201)
async def add_care_plan_item(encounter_id: str, request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability("inpatient.work"))):
    """Add one case-management item to the admission's care plan: a
    FHIR Task linked to the encounter, using the same Task patterns
    as the Phase 5 consult machinery."""
    payload = await _json_body(request)
    row = _inpatient_row(session, encounter_id)
    body = row.body or {}
    if body.get("status") != "in-progress":
        raise HTTPException(
            status_code=409,
            detail=f"Encounter {encounter_id} is '{body.get('status')}'; "
                   "care-plan items require an in-progress admission")
    description = payload.get("description")
    if not isinstance(description, str) or not description.strip():
        raise HTTPException(status_code=422,
                            detail="A care-plan item requires 'description'")
    patient_ref = _subject_ref(body)
    task_body = {
        "resourceType": "Task",
        "id": str(uuid.uuid4()),
        "status": "requested",
        "intent": "plan",
        "description": description,
        "for": {"reference": patient_ref} if patient_ref else None,
        "encounter": {"reference": f"Encounter/{encounter_id}"},
        "authoredOn": _now_iso(),
        "requester": {"reference": f"Practitioner/{user.username}"},
    }
    task_body = {k: v for k, v in task_body.items() if v is not None}
    created = fhir.create_resource("Task", task_body, session, user)
    session.commit()
    return {"task": created}


@router.get("/encounters/{encounter_id}/care-plan")
def list_care_plan(encounter_id: str, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("inpatient.read"))):
    row = _inpatient_row(session, encounter_id)
    wanted = f"Encounter/{encounter_id}"
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Task",
            FhirResource.patient_ref == _subject_ref(row.body or {}))
        .order_by(FhirResource.created_at, FhirResource.id)).all()
    rows = [r for r in rows
            if _body_encounter_ref(r.body or {}) == wanted]
    return {"tasks": [fhir._render(r) for r in rows], "total": len(rows)}


@router.post("/care-plan/{task_id}/transition")
async def transition_care_plan_item(task_id: str, request: Request,
                                    session: Session = Depends(get_db),
                                    user: User = Depends(require_capability("inpatient.work"))):
    """Advance a care-plan item (requested -> in-progress -> completed,
    or cancel). Completion stamps the performing role via the same
    extension the BCMA MAR uses."""
    payload = await _json_body(request)
    target = payload.get("to") or payload.get("status")
    if not isinstance(target, str) or not target:
        raise HTTPException(status_code=422, detail="Requires target status ('to')")
    if target not in TASK_STATUSES:
        raise HTTPException(status_code=422,
                            detail=f"Unknown Task status '{target}'")
    task_row = fhir._get_row("Task", task_id, session)
    encounter_full = _body_encounter_ref(task_row.body or {}) or ""
    encounter_ref = encounter_full.split("/", 1)[1] if "/" in encounter_full else encounter_full
    if encounter_ref:
        _inpatient_row(session, encounter_ref)
    else:
        raise HTTPException(status_code=422,
                            detail=f"Task {task_id} is not linked to an admission")
    current = (task_row.body or {}).get("status")
    allowed = {"requested": {"in-progress", "cancelled"},
               "in-progress": {"completed", "cancelled"}}
    if target not in allowed.get(current, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Illegal care-plan transition '{current}' -> '{target}'")
    body = dict(task_row.body or {})
    body["status"] = target
    if target == "in-progress":
        _set_extension(body, PERFORMER_ROLE_EXTENSION,
                       valueString=user.role.value)
    if target in TASK_TERMINAL:
        body["lastModified"] = _now_iso()
    task_row.body = body
    task_row.status = target
    task_row.version += 1
    session.flush()
    append_audit(session, actor=user.username,
                 action="inpatient.careplan.transition",
                 entity=f"Task/{task_id}",
                 payload={"from": current, "to": target,
                          "encounter": encounter_ref})
    session.commit()
    return {"task": fhir._render(task_row)}


# ---------------------------------------------------------------------------
# Perioperative depth: pre-op checklist + anaesthesia on surgery cases
# ---------------------------------------------------------------------------

def _case_row_or_404(session: Session, case_id: str) -> FhirResource:
    row = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Procedure",
                                   FhirResource.resource_id == case_id)).first()
    if row is None or not boards._is_surgery_case(row.body or {}):
        raise HTTPException(status_code=404,
                            detail=f"Surgery case {case_id} not found")
    return row


def _full_case_entry(session: Session, row: FhirResource) -> dict:
    """The boards case entry (case + appointment + room + timeline)
    for one case row, resolving its linked scheduling Appointment."""
    appointment = boards._case_appointment(session, row)
    if appointment is None:
        return {"case": fhir._render(row), "appointment": None,
                "room": None, "state": (row.body or {}).get("status"),
                "start": None, "end": None,
                "patient_ref": row.patient_ref,
                "timeline": boards.case_timeline(row.body or {})}
    return boards._case_entry(row, appointment)


def _answer_map(answers) -> dict:
    """Submitted answers -> {linkId: scalar value}, normalised with
    the Phase 5 documentation machinery (documentation._answer_value)."""
    out: dict[str, object] = {}
    if not isinstance(answers, list):
        return out
    for entry in answers:
        if not isinstance(entry, dict) or not entry.get("linkId"):
            continue
        answer_list = entry.get("answer") or []
        if answer_list and isinstance(answer_list[0], dict):
            value, _unit = documentation._answer_value(answer_list[0])
            out[entry["linkId"]] = value
    return out


def _published_form(session: Session, form_key: str, form_label: str):
    """The published form config row, or 503 (the form is Studio data;
    inpatient operations cannot run a form nobody published)."""
    cfg = published_config(session, "form", form_key)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=(f"No published {form_label} (form/{form_key}); publish "
                    "one in Studio before recording it"))
    return cfg


def _attach_form_response(session: Session, user: User, row: FhirResource,
                          extension_url: str, meta: dict,
                          audit_action: str) -> None:
    """Record a form-response linkage on the case (extension data) +
    audit, in the caller's transaction (flush only)."""
    body = dict(row.body or {})
    _set_extension(body, extension_url, valueString=json.dumps(meta))
    row.body = body
    row.version += 1
    session.flush()
    append_audit(session, actor=user.username, action=audit_action,
                 entity=f"Procedure/{row.resource_id}",
                 payload={"response_id": meta.get("responseId"),
                          "form_version": meta.get("formVersion")})


@router.post("/surgery-cases/{case_id}/preop-checklist")
async def submit_preop_checklist(case_id: str, request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_capability("inpatient.work"))):
    """Submit the published pre-op checklist form for a surgery case;
    the response (stored by the Phase 5 documentation machinery) is
    linked from the case so the Surgery board can show it."""
    payload = await _json_body(request)
    row = _case_row_or_404(session, case_id)
    cfg = _published_form(session, "preop-checklist", "pre-op checklist form")
    created = documentation._store_response(
        session, user, "preop-checklist", row.patient_ref,
        payload.get("answers"), _body_encounter_ref(row.body or {}))
    _attach_form_response(
        session, user, row, PREOP_EXTENSION,
        {"responseId": created["id"], "formVersion": cfg.version,
         "completedBy": user.username, "completedAt": _now_iso()},
        "inpatient.surgery.preop")
    session.commit()
    return {"case": _full_case_entry(session, _case_row_or_404(session, case_id)),
            "response": created}


@router.post("/surgery-cases/{case_id}/anaesthesia")
async def submit_anaesthesia(case_id: str, request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability("inpatient.work"))):
    """Submit the published anaesthesia record form for a surgery
    case: a validated QuestionnaireResponse (Phase 5 machinery),
    linked to the case via the case's anaesthesia extension."""
    payload = await _json_body(request)
    row = _case_row_or_404(session, case_id)
    cfg = _published_form(session, "anaesthesia-record",
                          "anaesthesia record form")
    created = documentation._store_response(
        session, user, "anaesthesia-record", row.patient_ref,
        payload.get("answers"), _body_encounter_ref(row.body or {}))
    _attach_form_response(
        session, user, row, ANAESTHESIA_EXTENSION,
        {"responseId": created["id"], "formVersion": cfg.version,
         "recordedBy": user.username, "recordedAt": _now_iso(),
         "summary": _answer_map(payload.get("answers"))},
        "inpatient.surgery.anaesthesia")
    session.commit()
    return {"case": _full_case_entry(session, _case_row_or_404(session, case_id)),
            "response": created}


@router.post("/surgery-cases/{case_id}/anaesthesia-vitals", status_code=201)
async def submit_anaesthesia_vitals(case_id: str, request: Request,
                                    session: Session = Depends(get_db),
                                    user: User = Depends(require_capability("inpatient.work"))):
    """One vitals reading on the case's anaesthesia flowsheet: a
    QuestionnaireResponse against the published anaesthesia-vitals
    form (the Phase 5 flowsheet pattern), server-timestamped and
    tagged with the case id so the case record can scope its series."""
    payload = await _json_body(request)
    row = _case_row_or_404(session, case_id)
    _published_form(session, "anaesthesia-vitals", "anaesthesia vitals form")
    created = documentation._store_response(
        session, user, "anaesthesia-vitals", row.patient_ref,
        payload.get("answers"), _body_encounter_ref(row.body or {}))
    qr_row = fhir._get_row("QuestionnaireResponse", created["id"], session)
    qr_body = dict(qr_row.body or {})
    _set_extension(qr_body, ANAESTHESIA_CASE_EXTENSION, valueString=case_id)
    qr_row.body = qr_body
    qr_row.version += 1
    session.flush()
    append_audit(session, actor=user.username,
                 action="inpatient.surgery.anaesthesia-vitals",
                 entity=f"Procedure/{case_id}",
                 payload={"response_id": created["id"]})
    session.commit()
    return {"case_id": case_id, "response": created,
            "recorded_at": created.get("authored")}


def _case_vitals_series(session: Session, patient_ref: str | None,
                        case_id: str) -> dict:
    """The anaesthesia vitals series for ONE case, in the Phase 5
    flowsheet view shape (form items + one entry per reading)."""
    form = published_config(session, "form", "anaesthesia-vitals")
    items = documentation._flatten_items((form.payload or {}).get("item"))         if form is not None else []
    entries = []
    if patient_ref:
        rows = session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "QuestionnaireResponse",
                FhirResource.patient_ref == patient_ref)
            .order_by(FhirResource.created_at.asc(),
                      FhirResource.id.asc())).all()
        for r in rows:
            body = r.body or {}
            ref = str(body.get("questionnaire") or "")
            if ref.rstrip("/").rsplit("/", 1)[-1] != "anaesthesia-vitals":
                continue
            ext = _get_extension(body, ANAESTHESIA_CASE_EXTENSION)
            if ext is None or ext.get("valueString") != case_id:
                continue
            answers: dict[str, dict] = {}
            for item in body.get("item") or []:
                if not isinstance(item, dict) or not item.get("linkId"):
                    continue
                answer_list = item.get("answer") or []
                if answer_list and isinstance(answer_list[0], dict):
                    value, unit = documentation._answer_value(answer_list[0])
                    answers[item["linkId"]] = {"value": value, "unit": unit}
            entries.append({"id": r.resource_id,
                            "authored": body.get("authored"),
                            "answers": answers})
    return {"form_key": "anaesthesia-vitals",
            "form": {"title": (form.payload or {}).get("title")
                     if form is not None else None,
                     "items": [{"linkId": i.get("linkId"),
                                "text": i.get("text"),
                                "type": i.get("type"),
                                "required": bool(i.get("required"))}
                               for i in items]},
            "entries": entries, "total": len(entries)}


@router.get("/surgery-cases/{case_id}/record")
def surgery_case_record(case_id: str, session: Session = Depends(get_db),
                        user: User = Depends(require_capability("inpatient.read"))):
    """The full perioperative record for one case: board entry +
    timeline, the pre-op checklist, the anaesthesia record, and the
    anaesthesia vitals series (Phase 5 flowsheet view shape)."""
    row = _case_row_or_404(session, case_id)
    body = row.body or {}

    def response_of(extension_url: str):
        ext = _get_extension(body, extension_url)
        if ext is None or not isinstance(ext.get("valueString"), str):
            return None
        try:
            meta = json.loads(ext["valueString"])
        except ValueError:
            return None
        response_id = meta.get("responseId")
        if not response_id:
            return None
        resp = session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "QuestionnaireResponse",
                FhirResource.resource_id == response_id)).first()
        return {"meta": meta,
                "response": fhir._render(resp) if resp is not None else None}

    return {"case": _full_case_entry(session, row),
            "preop_checklist": response_of(PREOP_EXTENSION),
            "anaesthesia": response_of(ANAESTHESIA_EXTENSION),
            "anaesthesia_vitals": _case_vitals_series(
                session, row.patient_ref, case_id)}
