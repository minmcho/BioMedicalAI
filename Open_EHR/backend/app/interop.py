"""Phase 8 — Live interoperability (app.interop).

HL7 v2 feeds over MLLP, FHIR bulk export and public-health (ELR)
reporting, built on the Phase 2–7 machinery rather than beside it:

- ONE HL7 implementation: app.hl7 (the Phase 2 ORU reader moved there
  in this phase; builders + ACK + MLLP framing live there too).
- Outbound messages are generated FROM LIVE EVENTS by hooks at the
  event sites — app.inpatient._record_event (ADT A01/A02/A03/A08),
  app.orders sign transition (ORM^O01), app.lab._release_now and the
  bench finalise/sign-out paths (ORU^R01) — and each hook writes its
  queue rows in the SAME transaction as the event (interface outbox),
  one row per enabled, subscribed endpoint in the published
  `integration/hl7-endpoints` Studio config. No enabled endpoint ->
  nothing queued: a site with no interfaces configured behaves
  exactly as before this phase.
- The dispatcher drains the queue over real MLLP sockets with the
  published retry policy; every attempt is logged; exhausted messages
  dead-letter and can be replayed from the interface admin API (the
  attempt history is never rewritten).
- Inbound MLLP messages are processed by app.mllp_server through
  :func:`process_inbound`: ORU^R01 routes into the EXISTING lab ingest
  (app.lab.ingest_hl7_groups — drafts only, never auto-released);
  ADT is recorded as an external event and NEVER mutates the census —
  a conflicting ADT (A01 for an already-admitted patient, A03 with no
  active admission, unknown patient) is refused with a NAK + ERR.
- Public health: a released report whose analyte is on the published
  `terminology/reportable-conditions` list additionally queues an ELR
  ORU^R01 addressed to the endpoint NAMED `public-health`.
  Immunisation (VXU) reporting is NOT built: this system has no
  Immunization resource or vaccine administration model anywhere in
  its data (BCMA MedicationAdministration is medication, not
  vaccination), and fabricating one is out of scope — see the SQA
  report.
- FHIR bulk export is $export-shaped: POST /interop/$export -> 202 +
  job; NDJSON per resource type; the job/status surface carries
  counts only, never patient data; the files are served only through
  the `interop.export` capability gate and every job + download is
  audited.
"""
import json
import socket
import threading
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir, hl7, lab
from .audit import append_audit
from .configstore import published_config
from .models import (BulkExportFile, BulkExportJob, ConfigStatus,
                     FhirResource, Hl7InboundMessage, Hl7OutboundMessage,
                     Hl7SendAttempt, Role, StudioConfig, User)
from .security import get_db, require_capability

router = APIRouter(prefix="/interop", tags=["interop"])

ENDPOINTS_KEY = "hl7-endpoints"
REPORTABLE_KEY = "reportable-conditions"
LOCATIONS_KEY = "locations"
PUBLIC_HEALTH_ENDPOINT = "public-health"
INTERFACE_USERNAME = "interface-engine"

EXPORTABLE_TYPES = ("Patient", "Encounter", "Observation", "Condition",
                    "MedicationRequest")

_ADT_TYPES = ("ADT^A01", "ADT^A02", "ADT^A03", "ADT^A08")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns timezone-NAIVE datetimes for DateTime(timezone=True)
    columns; normalise to UTC-aware before comparing (the same
    convention as app.inpatient._aware)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _now_iso() -> str:
    return _now().isoformat()


# ---------------------------------------------------------------------------
# Default (seeded) configuration
# ---------------------------------------------------------------------------

def default_endpoints_payload() -> dict:
    """Interface-engine config (integration/hl7-endpoints). Both seeded
    endpoints are DISABLED: no message leaves or is even queued until
    a configurator publishes an enabled endpoint through Studio."""
    return {
        "facility": {"sendingApplication": "OPENCARE",
                     "sendingFacility": "OPENCARE HOSPITAL"},
        "retry": {"maxAttempts": 3, "backoffSeconds": 30},
        "endpoints": [
            {"name": "downstream-ehr", "host": "127.0.0.1", "port": 2575,
             "messageTypes": ["ADT", "ORM", "ORU"], "enabled": False,
             "receivingApplication": "DOWNSTREAM-EHR",
             "receivingFacility": "DOWNSTREAM"},
            {"name": PUBLIC_HEALTH_ENDPOINT, "host": "127.0.0.1",
             "port": 2576, "messageTypes": ["ORU"], "enabled": False,
             "receivingApplication": "PH-REPORTING",
             "receivingFacility": "PUBLIC HEALTH"},
        ],
    }


def default_reportable_payload() -> dict:
    """ELR trigger list (terminology/reportable-conditions): a released
    result whose LOINC code matches a concept AND whose value satisfies
    the concept's valueCondition queues an ELR to public health.
    valueCondition operators: "any" (code alone), "above" (numeric
    value > value), "positive" (qualitative positive/detected/reactive).
    Synthetic example set — sites govern the real list in Studio."""
    def cond(code, display, condition, vc):
        return {"code": code, "display": display, "condition": condition,
                "active": True, "valueCondition": vc}

    return {
        "name": "Reportable conditions (public-health ELR triggers)",
        "system": "http://loinc.org",
        "concepts": [
            cond("94500-6", "SARS-CoV-2 RNA Resp Ql NAA+probe", "COVID-19",
                 {"operator": "positive"}),
            cond("5671-3", "Lead [Mass/volume] in Blood", "Lead poisoning",
                 {"operator": "above", "value": 3.5}),
            cond("5196-1", "Hepatitis B virus surface Ag [Presence] in Serum",
                 "Hepatitis B", {"operator": "positive"}),
        ],
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


def ensure_interop_defaults(session: Session) -> None:
    """Seed the interface-engine + reportable-conditions configs as
    published v1 (idempotent), exactly like the other modules."""
    _seed_published(session, "integration", ENDPOINTS_KEY,
                    default_endpoints_payload())
    _seed_published(session, "terminology", REPORTABLE_KEY,
                    default_reportable_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Config accessors
# ---------------------------------------------------------------------------

def _endpoints_payload(session: Session) -> dict:
    cfg = published_config(session, "integration", ENDPOINTS_KEY)
    return (cfg.payload or {}) if cfg is not None else {}


def _facility(session: Session) -> dict:
    facility = _endpoints_payload(session).get("facility") or {}
    return {"sendingApplication": facility.get("sendingApplication") or "OPENCARE",
            "sendingFacility": facility.get("sendingFacility") or "OPENCARE"}


def _retry_policy(session: Session) -> tuple[int, float]:
    retry = _endpoints_payload(session).get("retry") or {}
    max_attempts = retry.get("maxAttempts")
    backoff = retry.get("backoffSeconds")
    return (max_attempts if isinstance(max_attempts, int) and max_attempts >= 1 else 3,
            float(backoff) if isinstance(backoff, (int, float)) and backoff >= 0 else 30.0)


def _endpoints(session: Session) -> list[dict]:
    return [e for e in (_endpoints_payload(session).get("endpoints") or [])
            if isinstance(e, dict) and e.get("name")]


def _subscribed(endpoint: dict, message_type: str) -> bool:
    types = endpoint.get("messageTypes") or []
    family = message_type.split("^")[0]
    return message_type in types or family in types


def _active_endpoints(session: Session, message_type: str,
                      only_name: str | None = None,
                      exclude_name: str | None = None) -> list[dict]:
    out = []
    for endpoint in _endpoints(session):
        if only_name is not None and endpoint["name"] != only_name:
            continue
        if exclude_name is not None and endpoint["name"] == exclude_name:
            continue
        if not endpoint.get("enabled"):
            continue
        if _subscribed(endpoint, message_type):
            out.append(endpoint)
    return out


def _reportable_concepts(session: Session) -> list[dict]:
    cfg = published_config(session, "terminology", REPORTABLE_KEY)
    if cfg is None:
        return []
    return [c for c in (cfg.payload or {}).get("concepts") or []
            if isinstance(c, dict) and c.get("active", True)]


# ---------------------------------------------------------------------------
# Outbound queue (the interface outbox)
# ---------------------------------------------------------------------------

def _queue_row(session: Session, endpoint: dict, message_type: str, raw: str,
               patient_ref: str | None, source_kind: str,
               source_id: str) -> Hl7OutboundMessage:
    segments = hl7.parse_segments(raw)
    row = Hl7OutboundMessage(
        message_id=f"hl7-{uuid.uuid4().hex[:16]}",
        control_id=hl7.control_id_of(segments),
        message_type=message_type, endpoint_name=endpoint["name"],
        patient_ref=patient_ref, source_kind=source_kind,
        source_id=source_id, raw=raw, status="pending")
    session.add(row)
    session.flush()
    return row


def _fanout(session: Session, message_type: str, patient_ref: str | None,
            source_kind: str, source_id: str, build,
            only_name: str | None = None,
            exclude_name: str | None = None) -> list[Hl7OutboundMessage]:
    """Build + queue one message per active endpoint. `build(endpoint)`
    returns the raw HL7 text for that endpoint (MSH-5/6 differ). Called
    from the event hooks, inside the event's own transaction."""
    rows = []
    for endpoint in _active_endpoints(session, message_type,
                                      only_name=only_name,
                                      exclude_name=exclude_name):
        raw = build(endpoint)
        if raw:
            rows.append(_queue_row(session, endpoint, message_type, raw,
                                   patient_ref, source_kind, source_id))
    return rows


def _patient_row(session: Session, patient_ref: str | None) -> FhirResource | None:
    if not patient_ref:
        return None
    patient_id = patient_ref.split("/")[-1]
    return session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Patient",
                                   FhirResource.resource_id == patient_id)).first()


def _rendered(session: Session, resource_type: str,
              resource_id: str | None) -> dict | None:
    if not resource_id:
        return None
    row = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == resource_type,
                                   FhirResource.resource_id == resource_id)).first()
    return fhir._render(row) if row is not None else None


def _location_field(session: Session, bed_code: str | None) -> str:
    """PV1-3 style point-of-care^room^bed for a bed code, resolved
    through the published locations terminology."""
    if not bed_code:
        return ""
    cfg = published_config(session, "terminology", LOCATIONS_KEY)
    concepts = (cfg.payload or {}).get("concepts") if cfg is not None else []
    for concept in concepts or []:
        if concept.get("code") == bed_code:
            unit = str(concept.get("parent") or "")
            room = str(concept.get("room") or "")
            bed = str(concept.get("bed") or "")
            return "^".join(hl7.esc(p) for p in (unit, room, bed) if p) \
                or hl7.esc(bed_code)
    return hl7.esc(bed_code)


def enqueue_adt_event(session: Session, event) -> list[Hl7OutboundMessage]:
    """ADT^A01/A02/A03/A08 for one AdtEvent row — called by
    app.inpatient._record_event in the event's transaction."""
    if event.event_type not in ("A01", "A02", "A03", "A08"):
        return []
    patient = _patient_row(session, event.patient_ref)
    if patient is None:
        return []
    encounter = _rendered(session, "Encounter", event.encounter_id) or {}
    patient_body = fhir._render(patient)
    facility = _facility(session)
    assigned = _location_field(session, event.to_location or event.from_location)
    prior = _location_field(session, event.from_location) \
        if event.event_type == "A02" else ""
    if event.event_type == "A03":
        assigned = _location_field(session, event.from_location)
    message_type = f"ADT^{event.event_type}"

    def build(endpoint):
        return hl7.build_adt(
            patient_body, encounter, event.event_type,
            control_id=hl7.new_control_id("ADT"),
            sending_application=facility["sendingApplication"],
            sending_facility=facility["sendingFacility"],
            receiving_application=endpoint.get("receivingApplication") or "",
            receiving_facility=endpoint.get("receivingFacility") or "",
            assigned_location=assigned, prior_location=prior,
            discharge_datetime=event.created_at.isoformat()
            if event.event_type == "A03" and event.created_at else "")

    return _fanout(session, message_type, event.patient_ref, "adt_event",
                   f"adt-event/{event.id}", build)


def enqueue_signed_order(session: Session, order_row: FhirResource) -> list[Hl7OutboundMessage]:
    """ORM^O01 for one freshly signed CPOE order — called by
    app.orders' sign transition in the signing transaction."""
    patient = _patient_row(session, order_row.patient_ref)
    if patient is None:
        return []
    order_body = fhir._render(order_row)
    patient_body = fhir._render(patient)
    facility = _facility(session)

    def build(endpoint):
        return hl7.build_orm(
            order_body, patient_body, control_id=hl7.new_control_id("ORM"),
            sending_application=facility["sendingApplication"],
            sending_facility=facility["sendingFacility"],
            receiving_application=endpoint.get("receivingApplication") or "",
            receiving_facility=endpoint.get("receivingFacility") or "")

    return _fanout(session, "ORM^O01", order_row.patient_ref, "order",
                   f"{order_row.resource_type}/{order_row.resource_id}", build)


def _report_patient_ref(report_row: FhirResource,
                        order_row: FhirResource) -> str | None:
    for body in (report_row.body or {}, order_row.body or {}):
        ref = ((body.get("subject") or {}).get("reference")
               or (body.get("patient") or {}).get("reference"))
        if ref:
            return ref
    return report_row.patient_ref or order_row.patient_ref


def _value_matches(concept: dict, observation_body: dict) -> bool:
    condition = concept.get("valueCondition") or {}
    operator = str(condition.get("operator") or "any").lower()
    if operator == "any":
        return True
    if operator == "above":
        qty = observation_body.get("valueQuantity") or {}
        value = qty.get("value")
        threshold = condition.get("value")
        return isinstance(value, (int, float)) and isinstance(threshold, (int, float)) \
            and value > threshold
    if operator == "positive":
        text = ""
        if "valueString" in observation_body:
            text = str(observation_body.get("valueString") or "")
        elif "valueCodeableConcept" in observation_body:
            coding = ((observation_body.get("valueCodeableConcept") or {})
                      .get("coding") or [])
            if coding:
                text = str(coding[0].get("code") or coding[0].get("display") or "")
        elif "valueBoolean" in observation_body:
            return observation_body.get("valueBoolean") is True
        return text.strip().lower() in ("positive", "detected", "reactive",
                                        "present", "pos")
    return False


def _reportable_matches(session: Session,
                        observation_bodies: list[dict]) -> list[dict]:
    concepts = _reportable_concepts(session)
    matched = []
    for obs in observation_bodies:
        coding = ((obs.get("code") or {}).get("coding") or [])
        code = str(coding[0].get("code") or "") if coding else ""
        if not code:
            continue
        for concept in concepts:
            if concept.get("code") == code and _value_matches(concept, obs):
                matched.append(concept)
                break
    return matched


def enqueue_released_report(session: Session, report_row: FhirResource,
                            order_row: FhirResource) -> list[Hl7OutboundMessage]:
    """ORU^R01 for one released lab report (+ the ELR variant to the
    public-health endpoint when a resulted analyte is reportable) —
    called by app.lab._release_now and the bench finalise/sign-out
    paths, in the release transaction."""
    patient_ref = _report_patient_ref(report_row, order_row)
    patient = _patient_row(session, patient_ref)
    if patient is None:
        return []
    report_body = fhir._render(report_row)
    order_body = fhir._render(order_row)
    patient_body = fhir._render(patient)
    observation_bodies = [fhir._render(o)
                          for o in lab._result_observations(session, report_row)]
    facility = _facility(session)
    source_id = f"DiagnosticReport/{report_row.resource_id}"

    def build(endpoint):
        return hl7.build_oru(
            report_body, observation_bodies, order_body, patient_body,
            control_id=hl7.new_control_id("ORU"),
            sending_application=facility["sendingApplication"],
            sending_facility=facility["sendingFacility"],
            receiving_application=endpoint.get("receivingApplication") or "",
            receiving_facility=endpoint.get("receivingFacility") or "")

    # The general results feed goes to every subscribed endpoint EXCEPT
    # public health — that endpoint receives the ELR variant only, so
    # a reportable result is never double-sent in two shapes.
    rows = _fanout(session, "ORU^R01", patient_ref, "lab_report", source_id,
                   build, exclude_name=PUBLIC_HEALTH_ENDPOINT)

    matched = _reportable_matches(session, observation_bodies)
    if matched:
        condition_names = ", ".join(
            str(c.get("condition") or c.get("display") or c.get("code"))
            for c in matched)

        def build_elr(endpoint):
            return hl7.build_oru(
                report_body, observation_bodies, order_body, patient_body,
                control_id=hl7.new_control_id("ELR"),
                sending_application=facility["sendingApplication"],
                sending_facility=facility["sendingFacility"],
                receiving_application=endpoint.get("receivingApplication") or "",
                receiving_facility=endpoint.get("receivingFacility") or "",
                elr_condition=condition_names)

        rows += _fanout(session, "ORU^R01", patient_ref, "elr", source_id,
                        build_elr, only_name=PUBLIC_HEALTH_ENDPOINT)
    return rows


# ---------------------------------------------------------------------------
# MLLP transport + dispatcher
# ---------------------------------------------------------------------------

def send_mllp(host: str, port: int, raw: str, timeout: float = 10.0) -> tuple[str, str]:
    """Send one framed message over a real socket and read the ACK.
    Returns (ack_code, detail): ack_code is AA/AE/AR from the peer's
    MSA, or '' with the transport problem in detail."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(hl7.wrap_mllp(raw))
            buffer = b""
            while hl7.MLLP_END not in buffer:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
    except OSError as exc:
        return "", f"{type(exc).__name__}: {exc}"
    if not buffer:
        return "", "connection closed without an ACK"
    ack = hl7.parse_ack(hl7.unwrap_mllp(buffer))
    if not ack["ack_code"]:
        return "", "response carried no MSA segment (not an HL7 ACK)"
    detail = ack["text"] or f"MSA-1={ack['ack_code']}"
    return ack["ack_code"], detail


def dispatch_pending(session: Session, now: datetime | None = None) -> dict:
    """Drain due outbound rows to their endpoints. One pass: every
    pending row whose backoff has elapsed gets exactly one attempt.
    Returns attempt statistics. The caller owns the transaction."""
    now = now or _now()
    max_attempts, backoff = _retry_policy(session)
    endpoints = {e["name"]: e for e in _endpoints(session)}
    stats = {"attempted": 0, "sent": 0, "failed": 0, "dead_lettered": 0,
             "skipped": 0}
    rows = session.scalars(
        select(Hl7OutboundMessage)
        .where(Hl7OutboundMessage.status == "pending")
        .order_by(Hl7OutboundMessage.id.asc())).all()
    for row in rows:
        if row.next_attempt_at is not None and _aware(row.next_attempt_at) > now:
            stats["skipped"] += 1
            continue
        endpoint = endpoints.get(row.endpoint_name)
        if endpoint is None or not endpoint.get("enabled") \
                or not _subscribed(endpoint, row.message_type):
            # The endpoint vanished (or was unsubscribed/disabled after
            # queueing): nothing can ever deliver this row as addressed.
            # A disabled endpoint is left pending (a re-enable revives
            # it); a removed/unsubscribed one dead-letters loudly.
            if endpoint is not None and not endpoint.get("enabled"):
                stats["skipped"] += 1
                continue
            row.status = "dead_letter"
            row.last_error = ("endpoint no longer in the published config "
                              "or no longer subscribed to this message type")
            stats["dead_lettered"] += 1
            continue
        stats["attempted"] += 1
        row.attempts += 1
        ack_code, detail = send_mllp(str(endpoint["host"]), int(endpoint["port"]),
                                     row.raw)
        outcome = "ack" if ack_code == "AA" else ("nak" if ack_code else "error")
        session.add(Hl7SendAttempt(outbound_id=row.id, attempt_no=row.attempts,
                                   outcome=outcome,
                                   detail=(f"MSA {ack_code}: " if ack_code else "")
                                   + detail))
        if ack_code == "AA":
            row.status = "sent"
            row.ack_code = "AA"
            row.sent_at = now
            row.last_error = None
            row.next_attempt_at = None
            stats["sent"] += 1
        else:
            row.ack_code = ack_code or None
            row.last_error = detail
            stats["failed"] += 1
            if row.attempts - row.attempt_base >= max_attempts:
                row.status = "dead_letter"
                row.next_attempt_at = None
                stats["dead_lettered"] += 1
            else:
                wait = backoff * (2 ** (row.attempts - row.attempt_base - 1))
                row.next_attempt_at = now + timedelta(seconds=wait)
        session.flush()
    append_audit(session, actor="system:interop-dispatcher",
                 action="interop.dispatch",
                 entity="hl7-outbound",
                 payload={k: v for k, v in stats.items()})
    return stats


# ---------------------------------------------------------------------------
# Inbound processing (used by app.mllp_server)
# ---------------------------------------------------------------------------

def _interface_user(session: Session) -> User:
    user = session.scalars(
        select(User).where(User.username == INTERFACE_USERNAME)).first()
    if user is None:
        user = User(username=INTERFACE_USERNAME, role=Role.clinician)
        session.add(user)
        session.flush()
    return user


def _resolve_patient(session: Session, pid3: str) -> FhirResource | None:
    """Resolve an inbound PID-3 to a Patient row: by resource id, or by
    any identifier value (external systems address patients by MRN)."""
    if not pid3:
        return None
    row = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Patient",
                                   FhirResource.resource_id == pid3)).first()
    if row is not None:
        return row
    for candidate in session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "Patient")).all():
        for ident in (candidate.body or {}).get("identifier") or []:
            if isinstance(ident, dict) and str(ident.get("value") or "") == pid3:
                return candidate
    return None


def _encounter_class_code(body: dict) -> str:
    """Encounter.class as stored in this system is a bare Coding
    ({system, code}) — FHIR R4 style — but tolerate the CodeableConcept
    shape too (same tolerance as app.inpatient._class_code)."""
    cls = body.get("class") or {}
    if isinstance(cls, dict):
        if cls.get("code"):
            return str(cls["code"])
        coding = cls.get("coding") or []
        if coding:
            return str(coding[0].get("code") or "")
    return ""


def _active_admission(session: Session, patient_ref: str) -> FhirResource | None:
    """Mirror of app.inpatient's rule: the newest in-progress IMP
    encounter for the patient, or None."""
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Encounter",
            FhirResource.patient_ref == patient_ref,
            FhirResource.status == "in-progress")
        .order_by(FhirResource.created_at.desc(), FhirResource.id.desc())).all()
    for row in rows:
        if _encounter_class_code(row.body or {}) == "IMP":
            return row
    return None


def process_inbound(session: Session, raw_text: str, peer: str = "") -> str:
    """Process ONE inbound HL7 message and return the ACK text.

    Every message is recorded (Hl7InboundMessage) with its disposition
    and audited, whatever happens. The caller (the MLLP server) owns
    the commit. ORU^R01 lands as lab DRAFTS through the existing ingest
    path; ADT is recorded as an external event and never mutates the
    census; conflicts are refused with AR + ERR."""
    text = hl7.unwrap_mllp(raw_text.encode("utf-8")) \
        if isinstance(raw_text, bytes) else raw_text
    segments = hl7.parse_segments(text)
    msh = hl7.segment_fields(segments, "MSH")
    ack_code, status, detail = "AR", "rejected", ""
    patient_ref = None
    message_type = ""
    control_id = ""
    if msh is None:
        detail = "Message does not start with an MSH segment"
    else:
        message_type = hl7.message_type_of(segments)
        control_id = hl7.control_id_of(segments)
        pid3 = hl7.patient_id_of(segments)
        try:
            if message_type == "ORU^R01":
                group = lab.parse_hl7_oru(text)
                user = _interface_user(session)
                created = lab.ingest_hl7_groups(session, user, [group],
                                                source_format="hl7")
                patient_ref = f"Patient/{group[1]}"
                ack_code, status = "AA", "accepted"
                detail = (f"Ingested as DRAFT lab report "
                          f"{created[0]['report_id']} "
                          f"({created[0]['result_count']} results, unreleased)")
            elif message_type in _ADT_TYPES:
                patient = _resolve_patient(session, pid3)
                if patient is None:
                    detail = f"Unknown patient in PID-3: '{pid3}'"
                else:
                    patient_ref = f"Patient/{patient.resource_id}"
                    active = _active_admission(session, patient_ref)
                    event = message_type.split("^")[1]
                    if event == "A01" and active is not None:
                        detail = (f"Conflicting ADT^A01: patient already has an "
                                  f"active inpatient admission "
                                  f"(Encounter/{active.resource_id})")
                    elif event == "A03" and active is None:
                        detail = ("Conflicting ADT^A03: patient has no active "
                                  "inpatient admission to discharge")
                    else:
                        ack_code, status = "AA", "accepted"
                        detail = ("External ADT event recorded; census NOT "
                                  "modified (inbound ADT never mutates bed "
                                  "state — admissions run through the "
                                  "inpatient workflows)")
            else:
                ack_code = "AE"
                detail = f"Unsupported message type: '{message_type or '(none)'}'"
        except HTTPException as exc:
            ack_code = "AE"
            status = "rejected"
            detail = str(exc.detail)
        except Exception as exc:  # a poison message must still get an ACK
            session.rollback()
            ack_code = "AE"
            status = "rejected"
            detail = f"Internal error processing message: {type(exc).__name__}"
    session.add(Hl7InboundMessage(
        message_type=message_type, control_id=control_id,
        patient_ref=patient_ref, peer=peer, raw=text, status=status,
        ack_code=ack_code, detail=detail))
    session.flush()
    append_audit(session, actor=INTERFACE_USERNAME,
                 action="interop.inbound",
                 entity=f"hl7-inbound/{message_type or 'unparsed'}",
                 payload={"status": status, "ack_code": ack_code,
                          "patient": patient_ref, "peer": peer,
                          "detail": detail})
    facility = _facility(session)
    return hl7.build_ack(text, ack_code, detail,
                         sending_application=facility["sendingApplication"],
                         sending_facility=facility["sendingFacility"])


# ---------------------------------------------------------------------------
# FHIR bulk export
# ---------------------------------------------------------------------------

def run_export_job(session: Session, job: BulkExportJob) -> BulkExportJob:
    """Generate the NDJSON files for one job (in the caller's
    transaction). Each line is one resource exactly as the FHIR API
    renders it. The job row records counts only."""
    job.status = "processing"
    session.flush()
    try:
        files_meta = []
        for resource_type in job.resource_types:
            rows = session.scalars(
                select(FhirResource)
                .where(FhirResource.resource_type == resource_type)
                .order_by(FhirResource.resource_id.asc())).all()
            lines = [json.dumps(fhir._render(row)) for row in rows]
            session.add(BulkExportFile(
                job_id=job.job_id, resource_type=resource_type,
                count=len(lines), content="\n".join(lines)))
            files_meta.append({
                "resource_type": resource_type, "count": len(lines),
                "url": f"/interop/export-jobs/{job.job_id}/files/{resource_type}"})
        job.files = files_meta
        job.status = "complete"
        job.error = None
    except Exception as exc:  # recorded on the job, never a 500 ghost
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
    job.completed_at = _now()
    session.flush()
    append_audit(session, actor=job.created_by or "system",
                 action=f"interop.export.{job.status}",
                 entity=f"bulk-export/{job.job_id}",
                 payload={"files": job.files, "error": job.error})
    return job


def _run_export_thread(session_factory, job_id: str) -> None:
    with session_factory() as session:
        job = session.scalars(
            select(BulkExportJob).where(BulkExportJob.job_id == job_id)).first()
        if job is not None and job.status == "requested":
            run_export_job(session, job)
            session.commit()


# ---------------------------------------------------------------------------
# Interface admin + export API
# ---------------------------------------------------------------------------

def _outbound_out(session: Session, row: Hl7OutboundMessage,
                  detail: bool = False) -> dict:
    out = {"id": row.id, "message_id": row.message_id,
           "control_id": row.control_id, "message_type": row.message_type,
           "endpoint": row.endpoint_name, "patient_ref": row.patient_ref,
           "source": {"kind": row.source_kind, "id": row.source_id},
           "status": row.status, "attempts": row.attempts,
           "replay_count": row.replay_count, "ack_code": row.ack_code,
           "last_error": row.last_error,
           "created_at": row.created_at.isoformat() if row.created_at else None,
           "sent_at": row.sent_at.isoformat() if row.sent_at else None}
    if detail:
        out["raw"] = row.raw
        attempts = session.scalars(
            select(Hl7SendAttempt).where(Hl7SendAttempt.outbound_id == row.id)
            .order_by(Hl7SendAttempt.id.asc())).all()
        out["attempt_history"] = [
            {"attempt_no": a.attempt_no, "outcome": a.outcome,
             "detail": a.detail,
             "at": a.created_at.isoformat() if a.created_at else None}
            for a in attempts]
    return out


def _inbound_out(row: Hl7InboundMessage, detail: bool = False) -> dict:
    out = {"id": row.id, "message_type": row.message_type,
           "control_id": row.control_id, "patient_ref": row.patient_ref,
           "peer": row.peer, "status": row.status, "ack_code": row.ack_code,
           "detail": row.detail,
           "created_at": row.created_at.isoformat() if row.created_at else None}
    if detail:
        out["raw"] = row.raw
    return out


@router.get("/endpoints")
def list_endpoints(session: Session = Depends(get_db),
                   user: User = Depends(require_capability("interop.read"))):
    """Effective interface config (the published Studio payload) with
    live per-endpoint queue tallies. Endpoint edits go through the
    Studio draft -> publish flow — there is deliberately no write
    endpoint here."""
    cfg = published_config(session, "integration", ENDPOINTS_KEY)
    endpoints = []
    for endpoint in _endpoints(session):
        rows = session.scalars(
            select(Hl7OutboundMessage).where(
                Hl7OutboundMessage.endpoint_name == endpoint["name"])).all()
        tallies = {"pending": 0, "sent": 0, "dead_letter": 0}
        for row in rows:
            if row.status in tallies:
                tallies[row.status] += 1
        endpoints.append({**endpoint, "queue": tallies})
    return {"config": {"kind": "integration", "key": ENDPOINTS_KEY,
                       "version": cfg.version if cfg is not None else None},
            "facility": _facility(session),
            "retry": {"maxAttempts": _retry_policy(session)[0],
                      "backoffSeconds": _retry_policy(session)[1]},
            "endpoints": endpoints}


@router.get("/messages")
def list_messages(direction: str | None = Query(default=None),
                  type: str | None = Query(default=None),
                  status: str | None = Query(default=None),
                  patient: str | None = Query(default=None),
                  limit: int = Query(default=50, ge=1, le=500),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability("interop.read"))):
    """The unified interface log (inbound + outbound), newest first,
    filterable by direction / message type / status / patient."""
    items = []
    patient_ref = fhir._normalize_patient_ref(patient) if patient else None
    if direction in (None, "out", "outbound"):
        stmt = select(Hl7OutboundMessage).order_by(Hl7OutboundMessage.id.desc())
        if type:
            stmt = stmt.where(Hl7OutboundMessage.message_type == type)
        if status:
            stmt = stmt.where(Hl7OutboundMessage.status == status)
        if patient_ref:
            stmt = stmt.where(Hl7OutboundMessage.patient_ref == patient_ref)
        for row in session.scalars(stmt.limit(limit)).all():
            items.append({"direction": "outbound", **_outbound_out(session, row)})
    if direction in (None, "in", "inbound"):
        stmt = select(Hl7InboundMessage).order_by(Hl7InboundMessage.id.desc())
        if type:
            stmt = stmt.where(Hl7InboundMessage.message_type == type)
        if status:
            stmt = stmt.where(Hl7InboundMessage.status == status)
        if patient_ref:
            stmt = stmt.where(Hl7InboundMessage.patient_ref == patient_ref)
        for row in session.scalars(stmt.limit(limit)).all():
            items.append({"direction": "inbound", **_inbound_out(row)})
    items.sort(key=lambda i: i["created_at"] or "", reverse=True)
    return {"items": items[:limit], "count": min(len(items), limit)}


@router.get("/messages/outbound/{message_pk}")
def get_outbound(message_pk: int, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("interop.read"))):
    row = session.get(Hl7OutboundMessage, message_pk)
    if row is None:
        raise HTTPException(status_code=404, detail="Outbound message not found")
    return _outbound_out(session, row, detail=True)


@router.get("/messages/inbound/{message_pk}")
def get_inbound(message_pk: int, session: Session = Depends(get_db),
                user: User = Depends(require_capability("interop.read"))):
    row = session.get(Hl7InboundMessage, message_pk)
    if row is None:
        raise HTTPException(status_code=404, detail="Inbound message not found")
    return _inbound_out(row, detail=True)


@router.get("/dead-letters")
def list_dead_letters(session: Session = Depends(get_db),
                      user: User = Depends(require_capability("interop.read"))):
    rows = session.scalars(
        select(Hl7OutboundMessage)
        .where(Hl7OutboundMessage.status == "dead_letter")
        .order_by(Hl7OutboundMessage.id.desc())).all()
    return {"items": [_outbound_out(session, r, detail=True) for r in rows],
            "count": len(rows)}


@router.post("/dead-letters/{message_pk}/replay")
def replay_dead_letter(message_pk: int, session: Session = Depends(get_db),
                       user: User = Depends(require_capability("interop.manage"))):
    """Re-queue one dead letter. The attempt history is preserved
    verbatim; the row's attempt BASE moves to the current count, so
    the message gets a fresh max-attempts budget on top of its
    history."""
    row = session.get(Hl7OutboundMessage, message_pk)
    if row is None:
        raise HTTPException(status_code=404, detail="Outbound message not found")
    if row.status != "dead_letter":
        raise HTTPException(
            status_code=409,
            detail=f"Only dead-lettered messages can be replayed "
                   f"(this one is '{row.status}')")
    row.status = "pending"
    row.attempt_base = row.attempts
    row.replay_count += 1
    row.next_attempt_at = None
    row.last_error = None
    session.flush()
    append_audit(session, actor=user.username, action="interop.dead_letter.replay",
                 entity=f"hl7-outbound/{row.message_id}",
                 payload={"endpoint": row.endpoint_name,
                          "message_type": row.message_type,
                          "preserved_attempts": row.attempts,
                          "replay_count": row.replay_count})
    session.commit()
    return _outbound_out(session, row, detail=True)


@router.post("/dispatch")
def dispatch(session: Session = Depends(get_db),
             user: User = Depends(require_capability("interop.manage"))):
    """Run one dispatcher pass now (the same pass a scheduler/cron
    would invoke in production)."""
    stats = dispatch_pending(session)
    session.commit()
    return {"stats": stats}


@router.post("/$export", status_code=202)
async def start_export(request: Request,
                       wait: bool = Query(default=False),
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("interop.export"))):
    """FHIR Bulk Data $export (system level). Creates a job and
    returns 202 + the status URL. Generation runs on a background
    thread; `?wait=true` is the synchronous small-mode for small
    deployments and tests — the job shape is identical either way."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    requested = payload.get("resourceTypes") or list(EXPORTABLE_TYPES)
    unknown = [t for t in requested if t not in EXPORTABLE_TYPES]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(f"Unsupported export resource type(s): "
                    f"{', '.join(unknown)} (exportable: "
                    f"{', '.join(EXPORTABLE_TYPES)})"))
    job = BulkExportJob(job_id=f"export-{uuid.uuid4().hex[:12]}",
                        status="requested",
                        resource_types=list(requested),
                        created_by=user.username)
    session.add(job)
    session.flush()
    append_audit(session, actor=user.username, action="interop.export.create",
                 entity=f"bulk-export/{job.job_id}",
                 payload={"resource_types": job.resource_types})
    session.commit()
    if wait:
        with request.app.state.session_factory() as job_session:
            job_row = job_session.scalars(
                select(BulkExportJob).where(
                    BulkExportJob.job_id == job.job_id)).first()
            run_export_job(job_session, job_row)
            job_session.commit()
    else:
        threading.Thread(target=_run_export_thread,
                         args=(request.app.state.session_factory, job.job_id),
                         daemon=True).start()
    return {"job_id": job.job_id, "status": "requested" if not wait else "complete",
            "status_url": f"/interop/export-jobs/{job.job_id}",
            "resource_types": job.resource_types}


def _job_out(job: BulkExportJob) -> dict:
    """Job metadata ONLY — counts and file links, never patient data."""
    return {"job_id": job.job_id, "status": job.status,
            "resource_types": job.resource_types, "files": job.files,
            "error": job.error, "created_by": job.created_by,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "completed_at": job.completed_at.isoformat()
            if job.completed_at else None}


@router.get("/export-jobs/{job_id}")
def export_job_status(job_id: str, session: Session = Depends(get_db),
                      user: User = Depends(require_capability("interop.export"))):
    job = session.scalars(
        select(BulkExportJob).where(BulkExportJob.job_id == job_id)).first()
    if job is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    return _job_out(job)


@router.get("/export-jobs/{job_id}/files/{resource_type}")
def export_job_file(job_id: str, resource_type: str,
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability("interop.export"))):
    job = session.scalars(
        select(BulkExportJob).where(BulkExportJob.job_id == job_id)).first()
    if job is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    file_row = session.scalars(
        select(BulkExportFile).where(BulkExportFile.job_id == job_id,
                                     BulkExportFile.resource_type == resource_type)
    ).first()
    if file_row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Export job {job_id} has no {resource_type} file")
    append_audit(session, actor=user.username, action="interop.export.download",
                 entity=f"bulk-export/{job_id}",
                 payload={"resource_type": resource_type,
                          "count": file_row.count})
    session.commit()
    return PlainTextResponse(file_row.content,
                             media_type="application/fhir+ndjson")
