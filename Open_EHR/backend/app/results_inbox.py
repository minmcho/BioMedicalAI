"""Phase 15 (G) — the real clinician Results Inbox.

Today's /fhir/inbox is a single-patient read-only lookup; this module
is the cross-patient worklist a clinician actually starts a shift
with, under /inbox, gated by the SAME `fhir.inbox` capability for
reads AND actions:

- **Worklist** (GET /inbox/results): RELEASED DiagnosticReports only —
  lab (+ their Observations), imaging (app.boards) and pathology
  (app.lab_benches) — honouring the Phase 2 visibility rules EXACTLY
  by reusing app.fhir.has_unreleased_tag (there is deliberately no
  second visibility implementation here: a report carrying the
  lab-unreleased tag, or in a non-released status, does not exist as
  far as this inbox is concerned). A result's flag is derived ONLY
  from stored data: the lab critical-result extension
  (app.lab.CRITICAL_EXTENSION_URL) or an Observation interpretation
  of LL/HH makes it `critical`; L/H (or A) makes it `abnormal`; a
  source carrying no flag (a typical imaging/pathology report) yields
  flag null — a flag is never invented. Order: critical first, then
  abnormal, then newest. Filters: kind, flag, ack, patient; personal
  scoping (?scope=mine / ?scope=unit). Filter defaults, the page size
  and the critical SLA live in the published `integration/inbox`
  config, seeded by ensure_results_inbox_defaults.

- **Acknowledgement** is this module's own table (result ref +
  clinician username + timestamp + note), per-clinician and
  idempotent: a repeat POST returns the original record with
  already_acknowledged: true and writes no second row or audit entry.

- **Detail** (GET /inbox/results/{kind}/{id}): the report, its
  Observations WITH reference ranges as stored, the conclusion,
  patient summary + encounter context, and a trend of prior results
  of the same code(s) for the patient, oldest -> newest.

- **Actions**: follow-up-task (a real FHIR Task), message-patient
  (through app.engagement's messaging machinery — the message lands
  in the patient's thread), escalate (a Task to the escalation
  audience named in the inbox config, marked with the escalation
  extension).

- **SLA escalation**: POST /inbox/scan finds CRITICAL results that
  NOBODY has acknowledged and whose effective time is older than the
  configured slaHours, and emits `inbox.critical.unacknowledged`
  into the rules engine; the seeded PUBLISHED rule
  `inbox-critical-sla-escalation` (kind `rule`) turns that event into
  an escalation Task. NOTE: the event name is not yet in
  app.configstore.RULE_EVENTS, so the seeded rule is written directly
  as a published StudioConfig (the app.rules._seed_published pattern,
  the Phase 15 app.rfid precedent) — flagged INTEGRATION-NEEDED for
  the coordinator so the rule becomes Studio-editable. A scan is
  idempotent per result (an `inbox.sla.escalated` audit marker): a
  result escalates once, however often the scan runs.

- **Cross-link summary** (GET /inbox/summary): the results counts
  beside the continuum async inbox's open-task / breach counts — the
  continuum half computed by calling app.continuum.async_inbox
  itself (its own summary path), each half carrying the other's nav
  hint (module key).

- **Living seed**: when the demo patients (Patient/pat-1, pat-2 from
  backend/seed/synthea_bundle.json) exist in the store, ensure adds a
  few RELEASED results for them through the real FHIR resource shapes
  (fixed ids = idempotent; unit-test databases have no demo patients,
  so tests are unaffected).
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (DateTime, Integer, JSON, String, Text,
                        UniqueConstraint, select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import continuum, engagement, fhir, inpatient
from .audit import append_audit
from .configstore import published_config
from .db import Base
from .models import (AuditLog, ConfigStatus, FhirResource, StaffMember,
                     StudioConfig, User, utcnow)
from .rules import evaluate_rules
from .security import get_db, require_capability

router = APIRouter(prefix="/inbox", tags=["results_inbox"])

INBOX_KEY = "inbox"  # integration/inbox
SLA_EVENT = "inbox.critical.unacknowledged"
SLA_RULE_KEY = "inbox-critical-sla-escalation"
ESCALATION_EXTENSION = "urn:opencare:extension/escalation"
RESULT_EXTENSION = "urn:opencare:extension/source-result"
CRITICAL_EXTENSION_URL = "urn:opencare:extension/critical-result"

RELEASED_STATUSES = ("final", "amended", "corrected")
KINDS = ("lab", "imaging", "pathology")
# Category codes (HL7 v2-0074) that make a DiagnosticReport pathology
# or imaging; everything else released is lab (LAB, MB, ...).
_PATHOLOGY_CATEGORIES = ("SP", "CP")
_IMAGING_CATEGORIES = ("RAD",)
_CRITICAL_INTERP = ("LL", "HH")
_ABNORMAL_INTERP = ("L", "H", "A", "AA")

DEFAULT_INBOX_PAYLOAD = {
    "slaHours": 4,
    "pageSize": 50,
    "defaultAckFilter": "all",
    "escalationAudience": "charge-nurse",
    "escalationTaskTitle": "Critical result unacknowledged past SLA — escalate",
}


# ---------------------------------------------------------------------------
# This module's own table
# ---------------------------------------------------------------------------

class ResultAcknowledgement(Base):
    """One clinician's acknowledgement of one result — Phase 15 (G).

    The acknowledgement is operational work state, not part of the
    FHIR report body (the app.lab LabCriticalNotification separation):
    one row per (kind, result_id, clinician username); the unique
    constraint is what makes the acknowledge endpoint idempotent.
    """
    __tablename__ = "result_acknowledgements"
    __table_args__ = (UniqueConstraint("kind", "result_id", "username"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    result_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    result_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    username: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


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
        return {}
    return body if isinstance(body, dict) else {}


def _inbox_config(session: Session) -> dict:
    cfg = published_config(session, "integration", INBOX_KEY)
    payload = dict(cfg.payload or {}) if cfg is not None else {}
    merged = dict(DEFAULT_INBOX_PAYLOAD)
    merged.update({k: v for k, v in payload.items() if v is not None})
    return merged


def _sla_hours(config: dict) -> float:
    value = config.get("slaHours")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return float(DEFAULT_INBOX_PAYLOAD["slaHours"])
    return float(value)


def _page_size(config: dict) -> int:
    value = config.get("pageSize")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULT_INBOX_PAYLOAD["pageSize"])
    return value


def _category_codes(body: dict) -> list[str]:
    codes = []
    for cat in (body or {}).get("category") or []:
        if isinstance(cat, dict):
            for coding in cat.get("coding") or []:
                if isinstance(coding, dict) and coding.get("code"):
                    codes.append(str(coding["code"]))
    return codes


def _kind_of(body: dict) -> str:
    if (body or {}).get("imagingStudy"):
        return "imaging"
    codes = _category_codes(body)
    if any(c in _IMAGING_CATEGORIES for c in codes):
        return "imaging"
    if any(c in _PATHOLOGY_CATEGORIES for c in codes):
        return "pathology"
    return "lab"


def _code_of(body: dict) -> tuple[str | None, str | None]:
    concept = (body or {}).get("code") or {}
    coding = concept.get("coding") or []
    if coding and isinstance(coding[0], dict):
        return coding[0].get("code"), coding[0].get("display") or concept.get("text")
    return None, concept.get("text") if isinstance(concept, dict) else None


def _result_observations(session: Session, report: FhirResource) -> list[FhirResource]:
    rows = []
    for ref in (report.body or {}).get("result") or []:
        reference = ref.get("reference") if isinstance(ref, dict) else None
        if reference and reference.startswith("Observation/"):
            try:
                rows.append(fhir._get_row(
                    "Observation", reference.split("/", 1)[1], session))
            except HTTPException:
                continue
    return rows


def _interp_of(obs_body: dict) -> str | None:
    interp = (obs_body or {}).get("interpretation") or []
    if interp and isinstance(interp[0], dict):
        coding = interp[0].get("coding") or []
        if coding and isinstance(coding[0], dict):
            return coding[0].get("code")
    return None


def _flag_of(session: Session, report: FhirResource,
             observations: list[FhirResource] | None = None) -> str | None:
    """critical | abnormal | None — ONLY from stored data: the
    critical-result extension on the report, or Observation
    interpretations (LL/HH critical; L/H/A abnormal)."""
    for ext in (report.body or {}).get("extension") or []:
        if isinstance(ext, dict) and ext.get("url") == CRITICAL_EXTENSION_URL \
                and ext.get("valueBoolean") is True:
            return "critical"
    if observations is None:
        observations = _result_observations(session, report)
    flag = None
    for obs in observations:
        interp = _interp_of(obs.body or {})
        if interp in _CRITICAL_INTERP:
            return "critical"
        if interp in _ABNORMAL_INTERP:
            flag = "abnormal"
    return flag


def _effective_at(report: FhirResource) -> datetime:
    body = report.body or {}
    parsed = _parse_dt(body.get("effectiveDateTime")) or _parse_dt(body.get("issued"))
    if parsed is not None:
        return parsed
    return _aware(report.created_at) or _now()


def _is_released(report: FhirResource) -> bool:
    """RELEASED only, honouring app.fhir's visibility rules exactly:
    the lab-unreleased tag hides a result for every caller of the
    results bundle, and a released report carries a released status."""
    if fhir.has_unreleased_tag(report.body):
        return False
    return (report.body or {}).get("status") in RELEASED_STATUSES


def _released_reports(session: Session) -> list[FhirResource]:
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "DiagnosticReport")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    return [r for r in rows if _is_released(r)]


def _ack_map(session: Session, username: str) -> dict[tuple[str, str], ResultAcknowledgement]:
    rows = session.scalars(select(ResultAcknowledgement).where(
        ResultAcknowledgement.username == username)).all()
    return {(r.kind, r.result_id): r for r in rows}


def _any_acknowledged(session: Session, kind: str, result_id: str) -> bool:
    return session.scalars(select(ResultAcknowledgement).where(
        ResultAcknowledgement.kind == kind,
        ResultAcknowledgement.result_id == result_id)).first() is not None


def _caller_staff(session: Session, user: User) -> StaffMember | None:
    return session.scalars(select(StaffMember).where(
        StaffMember.username == user.username)).first()


def _scope_patient_refs(session: Session, user: User,
                        scope: str | None) -> set[str] | None:
    """Patient refs in scope, or None for the unscoped (all) worklist.

    mine: patients whose CURRENT (in-progress inpatient) encounter
    carries the caller as attending — the attending extension holds a
    staff-directory id (the caller's directory entry) or, defensively,
    the username itself.
    unit: patients whose current admission sits on the caller's staff
    unit (app.people link via StaffMember.unit; the admission's unit
    is resolved through its bed exactly as app.continuum does)."""
    if scope is None:
        return None
    if scope not in ("mine", "unit"):
        raise HTTPException(status_code=422,
                            detail=f"Unknown scope '{scope}': use mine or unit")
    staff = _caller_staff(session, user)
    admissions = continuum._active_admissions(session)
    if scope == "mine":
        attending_ids = {user.username}
        if staff is not None:
            attending_ids.add(staff.staff_id)
        refs = set()
        for row in admissions:
            ext = inpatient._get_extension(row.body or {},
                                           inpatient.ATTENDING_EXTENSION)
            if ext is not None and ext.get("valueString") in attending_ids:
                ref = inpatient._subject_ref(row.body or {})
                if ref:
                    refs.add(ref)
        return refs
    # scope == "unit"
    if staff is None or not staff.unit:
        return set()
    bed_units = continuum._bed_unit_map(session)
    refs = set()
    for row in admissions:
        if continuum._admission_unit(session, row.body or {}, bed_units) == staff.unit:
            ref = inpatient._subject_ref(row.body or {})
            if ref:
                refs.add(ref)
    return refs


def _item(session: Session, report: FhirResource,
          ack: ResultAcknowledgement | None) -> dict:
    body = report.body or {}
    kind = _kind_of(body)
    code, display = _code_of(body)
    observations = _result_observations(session, report)
    return {
        "result_ref": f"DiagnosticReport/{report.resource_id}",
        "result_type": "DiagnosticReport",
        "result_id": report.resource_id,
        "patient_ref": report.patient_ref or (body.get("subject") or {}).get("reference"),
        "kind": kind,
        "title": display or code or "Diagnostic report",
        "code": code,
        "effective_at": _effective_at(report).isoformat(),
        "flag": _flag_of(session, report, observations),
        "status": body.get("status"),
        "observation_count": len(observations),
        "acknowledged": ack is not None,
        "acknowledged_at": (_aware(ack.acknowledged_at).isoformat()
                            if ack is not None and ack.acknowledged_at else None),
        "acknowledged_by": ack.username if ack is not None else None,
    }


_FLAG_RANK = {"critical": 0, "abnormal": 1, None: 2}


def _worklist(session: Session, user: User, *, kind=None, flag=None,
              ack=None, patient=None, scope=None) -> list[dict]:
    scoped_refs = _scope_patient_refs(session, user, scope)
    acks = _ack_map(session, user.username)
    items = []
    for report in _released_reports(session):
        item = _item(session, report, acks.get((_kind_of(report.body or {}),
                                                report.resource_id)))
        if kind and item["kind"] != kind:
            continue
        if flag:
            if flag == "none":
                if item["flag"] is not None:
                    continue
            elif item["flag"] != flag:
                continue
        if ack == "unacknowledged" and item["acknowledged"]:
            continue
        if ack == "acknowledged" and not item["acknowledged"]:
            continue
        if patient:
            wanted = fhir._normalize_patient_ref(patient)
            if item["patient_ref"] != wanted:
                continue
        if scoped_refs is not None and item["patient_ref"] not in scoped_refs:
            continue
        items.append(item)
    items.sort(key=lambda i: (_FLAG_RANK.get(i["flag"], 2),
                              -_parse_dt(i["effective_at"]).timestamp()
                              if _parse_dt(i["effective_at"]) else 0))
    return items


def _get_result(session: Session, kind: str, result_id: str) -> FhirResource:
    if kind not in KINDS:
        raise HTTPException(status_code=404,
                            detail=f"Result {kind}/{result_id} not found")
    row = fhir._get_row("DiagnosticReport", result_id, session)
    if not _is_released(row) or _kind_of(row.body or {}) != kind:
        # An unreleased (or differently-kinded) result does not exist
        # as far as the inbox is concerned.
        raise HTTPException(status_code=404,
                            detail=f"Result {kind}/{result_id} not found")
    return row


def _ack_out(row: ResultAcknowledgement, already: bool) -> dict:
    return {
        "result_ref": row.result_ref,
        "kind": row.kind,
        "result_id": row.result_id,
        "patient_ref": row.patient_ref,
        "acknowledged_by": row.username,
        "acknowledged_at": (_aware(row.acknowledged_at).isoformat()
                            if row.acknowledged_at else None),
        "note": row.note,
        "already_acknowledged": already,
    }


# ---------------------------------------------------------------------------
# Worklist + summary
# ---------------------------------------------------------------------------

@router.get("/results")
def list_results(kind: str | None = Query(default=None),
                 flag: str | None = Query(default=None),
                 ack: str | None = Query(default=None),
                 patient: str | None = Query(default=None),
                 scope: str | None = Query(default=None),
                 limit: int | None = Query(default=None),
                 session: Session = Depends(get_db),
                 user: User = Depends(require_capability("fhir.inbox"))):
    """Cross-patient results worklist: RELEASED results only, critical
    first, then abnormal, then newest. Filter defaults + page size
    come from the published integration/inbox config."""
    config = _inbox_config(session)
    if kind is not None and kind not in KINDS:
        raise HTTPException(status_code=422,
                            detail=f"'kind' must be one of {list(KINDS)}")
    if flag is not None and flag not in ("critical", "abnormal", "none"):
        raise HTTPException(status_code=422,
                            detail="'flag' must be critical, abnormal or none")
    ack_filter = ack if ack is not None else config.get("defaultAckFilter") or "all"
    if ack_filter not in ("unacknowledged", "acknowledged", "all"):
        raise HTTPException(status_code=422,
                            detail="'ack' must be unacknowledged, acknowledged or all")
    items = _worklist(session, user, kind=kind, flag=flag,
                      ack=None if ack_filter == "all" else ack_filter,
                      patient=patient, scope=scope)
    page_size = _page_size(config)
    effective_limit = limit if isinstance(limit, int) and limit > 0 else page_size
    return {"items": items[:effective_limit], "total": len(items),
            "page_size": page_size, "limit": effective_limit,
            "truncated": len(items) > effective_limit,
            "scope": scope or "all",
            "filters": {"kind": kind, "flag": flag, "ack": ack_filter,
                        "patient": patient}}


@router.get("/summary")
def summary(session: Session = Depends(get_db),
            user: User = Depends(require_capability("fhir.inbox"))):
    """One-workflow cross-link: this inbox's unacknowledged counts
    beside the continuum async inbox's open / breached Task counts
    (computed through app.continuum.async_inbox — its own summary
    path). Each half carries the other's nav hint (module key)."""
    items = _worklist(session, user)
    unack = [i for i in items if not i["acknowledged"]]
    continuum_view = continuum.async_inbox(type="task", session=session, user=user)
    return {
        "results": {
            "unacknowledged": len(unack),
            "critical_unacknowledged": sum(1 for i in unack
                                           if i["flag"] == "critical"),
            "module": "inbox",
            "nav_hint": {"module": "command", "label": "Care continuum"},
        },
        "continuum": {
            "open_tasks": continuum_view.get("total", 0),
            "breached": continuum_view.get("breached", 0),
            "module": "command",
            "nav_hint": {"module": "inbox", "label": "Results inbox"},
        },
        "generated_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Detail + trend
# ---------------------------------------------------------------------------

def _patient_summary(session: Session, patient_ref: str | None) -> dict | None:
    if not patient_ref or not patient_ref.startswith("Patient/"):
        return None
    row = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Patient",
        FhirResource.resource_id == patient_ref.split("/", 1)[1])).first()
    if row is None:
        return {"patient_ref": patient_ref, "found": False}
    body = row.body or {}
    name = ((body.get("name") or [{}])[0]) if body.get("name") else {}
    display_name = None
    if isinstance(name, dict):
        display_name = name.get("text") or " ".join(
            [*(name.get("given") or []), name.get("family") or ""]).strip() or None
    return {"patient_ref": patient_ref, "found": True,
            "id": row.resource_id, "name": display_name,
            "gender": body.get("gender"), "birth_date": body.get("birthDate"),
            "patient": fhir._render(row)}


def _encounter_context(session: Session, report: FhirResource,
                       observations: list[FhirResource]) -> dict | None:
    ref = None
    for source in [report.body or {}, *[o.body or {} for o in observations]]:
        encounter = source.get("encounter")
        if isinstance(encounter, dict) and encounter.get("reference"):
            ref = encounter["reference"]
            break
    if not ref or not ref.startswith("Encounter/"):
        return None
    row = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Encounter",
        FhirResource.resource_id == ref.split("/", 1)[1])).first()
    if row is None:
        return {"encounter_ref": ref, "found": False}
    body = row.body or {}
    attending = inpatient._get_extension(body, inpatient.ATTENDING_EXTENSION)
    return {"encounter_ref": ref, "found": True, "id": row.resource_id,
            "status": body.get("status"),
            "class_code": inpatient._class_code(body),
            "period": body.get("period"),
            "attending_staff_id": attending.get("valueString") if attending else None,
            "encounter": fhir._render(row)}


def _trend(session: Session, patient_ref: str | None,
           observations: list[FhirResource]) -> list[dict]:
    """Prior results of the same code(s) for the patient, oldest ->
    newest: every RELEASED-visibility Observation for the patient
    whose code matches one of this report's Observation codes."""
    codes = []
    for obs in observations:
        code, _ = _code_of(obs.body or {})
        if code and code not in codes:
            codes.append(code)
    if not patient_ref or not codes:
        return []
    rows = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Observation",
        FhirResource.patient_ref == patient_ref)).all()
    entries = []
    for row in rows:
        if fhir.has_unreleased_tag(row.body):
            continue
        body = row.body or {}
        code, display = _code_of(body)
        if code not in codes:
            continue
        quantity = body.get("valueQuantity") or {}
        value = quantity.get("value")
        if value is None:
            value = body.get("valueString")
        if value is None:
            value = body.get("valueCodeableConcept", {}).get("text") \
                if isinstance(body.get("valueCodeableConcept"), dict) else None
        effective = _parse_dt(body.get("effectiveDateTime")) \
            or _aware(row.created_at) or _now()
        entries.append({
            "observation_id": row.resource_id, "code": code,
            "display": display, "value": value,
            "unit": quantity.get("unit"),
            "interpretation": _interp_of(body),
            "effective_at": effective.isoformat(),
            "_sort": effective.timestamp(),
        })
    entries.sort(key=lambda e: e["_sort"])
    for entry in entries:
        entry.pop("_sort", None)
    return entries


@router.get("/results/{kind}/{result_id}")
def result_detail(kind: str, result_id: str,
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability("fhir.inbox"))):
    """Full result: report + Observations (reference ranges as
    stored), conclusion, patient summary + encounter context, and
    the same-code trend for the patient, oldest -> newest."""
    report = _get_result(session, kind, result_id)
    observations = _result_observations(session, report)
    acks = _ack_map(session, user.username)
    item = _item(session, report, acks.get((kind, result_id)))
    return {
        **item,
        "report": fhir._render(report),
        "conclusion": (report.body or {}).get("conclusion"),
        "observations": [fhir._render(o) for o in observations],
        "patient_summary": _patient_summary(session, item["patient_ref"]),
        "encounter_context": _encounter_context(session, report, observations),
        "trend": _trend(session, item["patient_ref"], observations),
    }


# ---------------------------------------------------------------------------
# Acknowledgement + actions
# ---------------------------------------------------------------------------

@router.post("/results/{kind}/{result_id}/acknowledge")
async def acknowledge(kind: str, result_id: str, request: Request,
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("fhir.inbox"))):
    """Acknowledge a result FOR THE CALLER. Idempotent: the first
    call creates the record (audited inbox.acknowledge); a repeat
    returns the ORIGINAL record with already_acknowledged: true —
    no duplicate row, no second audit entry."""
    report = _get_result(session, kind, result_id)
    payload = await _json_body(request)
    existing = session.scalars(select(ResultAcknowledgement).where(
        ResultAcknowledgement.kind == kind,
        ResultAcknowledgement.result_id == result_id,
        ResultAcknowledgement.username == user.username)).first()
    if existing is not None:
        return _ack_out(existing, already=True)
    note = payload.get("note")
    row = ResultAcknowledgement(
        kind=kind, result_id=result_id,
        result_ref=f"DiagnosticReport/{result_id}",
        patient_ref=report.patient_ref
        or (report.body or {}).get("subject", {}).get("reference"),
        username=user.username,
        note=note if isinstance(note, str) else None)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="inbox.acknowledge",
                 entity=f"DiagnosticReport/{result_id}",
                 payload={"kind": kind, "patient": row.patient_ref,
                          "note": row.note})
    session.commit()
    return _ack_out(row, already=False)


def _create_task(session: Session, user: User, report: FhirResource,
                 *, description: str, owner_display: str | None,
                 escalation: bool) -> dict:
    task_id = str(uuid.uuid4())
    body = {
        "resourceType": "Task",
        "id": task_id,
        "status": "requested",
        "intent": "order",
        "description": description,
        "authoredOn": _now_iso(),
        "requester": {"display": user.username},
        "focus": {"reference": f"DiagnosticReport/{report.resource_id}"},
        "extension": [{"url": RESULT_EXTENSION,
                       "valueReference": {
                           "reference": f"DiagnosticReport/{report.resource_id}"}}],
    }
    if report.patient_ref:
        body["for"] = {"reference": report.patient_ref}
    if owner_display:
        body["owner"] = {"display": owner_display}
    if escalation:
        body["extension"].append({"url": ESCALATION_EXTENSION,
                                  "valueBoolean": True})
    return fhir.create_resource("Task", body, session, user)


@router.post("/results/{kind}/{result_id}/follow-up-task", status_code=201)
async def follow_up_task(kind: str, result_id: str, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("fhir.inbox"))):
    """Create a real FHIR Task about this result (subject = patient,
    focus = the report), audited as inbox.follow_up_task."""
    report = _get_result(session, kind, result_id)
    payload = await _json_body(request)
    description = payload.get("description") or payload.get("title")
    if not isinstance(description, str) or not description.strip():
        raise HTTPException(status_code=422,
                            detail="Follow-up task requires 'description'")
    owner = payload.get("owner") if isinstance(payload.get("owner"), str) else None
    task = _create_task(session, user, report,
                        description=description.strip(), owner_display=owner,
                        escalation=False)
    append_audit(session, actor=user.username, action="inbox.follow_up_task",
                 entity=f"DiagnosticReport/{result_id}",
                 payload={"task_id": task["id"], "kind": kind,
                          "patient": report.patient_ref})
    session.commit()
    return {"task": task, "result_ref": f"DiagnosticReport/{result_id}"}


@router.post("/results/{kind}/{result_id}/message-patient", status_code=201)
async def message_patient(kind: str, result_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("fhir.inbox"))):
    """Message the patient about this result through app.engagement's
    messaging machinery (imported service path): the message lands in
    the patient's Communication thread (staff thread views + the
    patient's portal both read it from there)."""
    report = _get_result(session, kind, result_id)
    payload = await _json_body(request)
    text = payload.get("text") or payload.get("message")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=422, detail="Message requires 'text'")
    patient_ref = report.patient_ref \
        or (report.body or {}).get("subject", {}).get("reference")
    if not patient_ref:
        raise HTTPException(status_code=422,
                            detail="Result has no patient to message")
    _, display = _code_of(report.body or {})
    topic = payload.get("topic") if isinstance(payload.get("topic"), str) \
        else f"Your result: {display or 'Diagnostic report'}"
    row = engagement._create_message(session, user, patient_ref,
                                     text.strip(), topic=topic)
    append_audit(session, actor=user.username, action="inbox.message_patient",
                 entity=f"DiagnosticReport/{result_id}",
                 payload={"communication_id": row.resource_id, "kind": kind,
                          "patient": patient_ref,
                          "thread_id": engagement._thread_id_of(row.body or {})})
    session.commit()
    return {"message": engagement._decorate_message(row, user),
            "thread_id": engagement._thread_id_of(row.body or {}),
            "result_ref": f"DiagnosticReport/{result_id}"}


@router.post("/results/{kind}/{result_id}/escalate", status_code=201)
async def escalate(kind: str, result_id: str, request: Request,
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("fhir.inbox"))):
    """Escalate a result: a Task to the escalation audience named in
    the published inbox config, marked with the escalation extension,
    audited as inbox.escalate."""
    report = _get_result(session, kind, result_id)
    payload = await _json_body(request)
    config = _inbox_config(session)
    audience = payload.get("audience") if isinstance(payload.get("audience"), str) \
        and payload.get("audience").strip() else config.get("escalationAudience")
    _, display = _code_of(report.body or {})
    reason = payload.get("reason") if isinstance(payload.get("reason"), str) else None
    description = (f"Escalated result: {display or result_id}"
                   + (f" — {reason}" if reason else ""))
    task = _create_task(session, user, report, description=description,
                        owner_display=str(audience) if audience else None,
                        escalation=True)
    append_audit(session, actor=user.username, action="inbox.escalate",
                 entity=f"DiagnosticReport/{result_id}",
                 payload={"task_id": task["id"], "kind": kind,
                          "audience": audience, "patient": report.patient_ref})
    session.commit()
    return {"task": task, "audience": audience,
            "result_ref": f"DiagnosticReport/{result_id}"}


# ---------------------------------------------------------------------------
# SLA scan
# ---------------------------------------------------------------------------

@router.post("/scan")
def scan(session: Session = Depends(get_db),
         user: User = Depends(require_capability("fhir.inbox"))):
    """SLA scan: CRITICAL results acknowledged by NOBODY whose
    effective time is older than the configured slaHours emit
    inbox.critical.unacknowledged into the rules engine (the seeded
    published rule opens the escalation Task). Idempotent per result
    via the inbox.sla.escalated audit marker; acknowledged-in-time
    criticals and in-SLA criticals never escalate."""
    config = _inbox_config(session)
    sla = _sla_hours(config)
    now = _now()
    scanned = 0
    escalated = []
    for report in _released_reports(session):
        if _flag_of(session, report) != "critical":
            continue
        scanned += 1
        kind = _kind_of(report.body or {})
        if _any_acknowledged(session, kind, report.resource_id):
            continue
        age_hours = (now - _effective_at(report)).total_seconds() / 3600.0
        if age_hours <= sla:
            continue
        marker = session.scalars(select(AuditLog).where(
            AuditLog.action == "inbox.sla.escalated",
            AuditLog.entity == f"DiagnosticReport/{report.resource_id}")).first()
        if marker is not None:
            continue
        firings = evaluate_rules(
            session, SLA_EVENT,
            {"critical": True, "result_id": report.resource_id,
             "kind": kind, "patient_ref": report.patient_ref,
             "age_hours": round(age_hours, 2), "sla_hours": sla},
            actor=user.username)
        append_audit(session, actor=user.username, action="inbox.sla.escalated",
                     entity=f"DiagnosticReport/{report.resource_id}",
                     payload={"kind": kind, "patient": report.patient_ref,
                              "age_hours": round(age_hours, 2),
                              "sla_hours": sla, "firings": len(firings)})
        escalated.append({"result_ref": f"DiagnosticReport/{report.resource_id}",
                          "kind": kind, "patient_ref": report.patient_ref,
                          "age_hours": round(age_hours, 2),
                          "firings": firings})
    session.commit()
    return {"scanned_critical": scanned, "escalated": escalated,
            "escalated_count": len(escalated), "sla_hours": sla,
            "generated_at": now.isoformat()}


# ---------------------------------------------------------------------------
# Defaults: published inbox config + SLA rule + living demo seed
# ---------------------------------------------------------------------------

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


def _demo_observation(obs_id: str, patient_ref: str, code: str, display: str,
                      value: float, unit: str, interp: str,
                      low: float, high: float, effective: str) -> FhirResource:
    body = {
        "resourceType": "Observation", "id": obs_id, "status": "final",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
            "code": "laboratory", "display": "Laboratory"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": code,
                             "display": display}], "text": display},
        "subject": {"reference": patient_ref},
        "effectiveDateTime": effective,
        "valueQuantity": {"value": value, "unit": unit},
        "interpretation": [{
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/"
                                   "v3-ObservationInterpretation",
                        "code": interp, "display": interp}],
            "text": interp}],
        "referenceRange": [{"low": {"value": low, "unit": unit},
                            "high": {"value": high, "unit": unit}}],
    }
    row = FhirResource(resource_type="Observation", resource_id=obs_id,
                       version=1, body=body)
    fhir._apply_projections(row, "Observation", body, obs_id)
    return row


def _demo_report(report_id: str, patient_ref: str, code: str, display: str,
                 obs_ids: list[str], effective: str, *,
                 critical: bool = False, category: str = "LAB",
                 category_display: str = "Laboratory",
                 conclusion: str | None = None,
                 imaging_study: str | None = None) -> FhirResource:
    body = {
        "resourceType": "DiagnosticReport", "id": report_id, "status": "final",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
            "code": category, "display": category_display}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": code,
                             "display": display}], "text": display},
        "subject": {"reference": patient_ref},
        "effectiveDateTime": effective, "issued": effective,
        "result": [{"reference": f"Observation/{o}"} for o in obs_ids],
    }
    if critical:
        body["extension"] = [{"url": CRITICAL_EXTENSION_URL,
                              "valueBoolean": True}]
    if conclusion:
        body["conclusion"] = conclusion
    if imaging_study:
        body["imagingStudy"] = [{"reference": f"ImagingStudy/{imaging_study}"}]
    row = FhirResource(resource_type="DiagnosticReport",
                       resource_id=report_id, version=1, body=body)
    fhir._apply_projections(row, "DiagnosticReport", body, report_id)
    return row


def _seed_demo_results(session: Session) -> None:
    """The living inbox for the seeded demo deployment: a few RELEASED
    results for the demo patients, in genuine lab / imaging shapes
    (interpretations + reference ranges stored on the Observations,
    the critical extension on the critical report — flags derive from
    stored data exactly as for any other result). Guarded: only when
    the demo patients exist in the store (they arrive via
    backend/seed/seed.py, never in a unit-test database), and
    idempotent via fixed resource ids."""
    patients = {r.resource_id for r in session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Patient",
                                   FhirResource.resource_id.in_(["pat-1", "pat-2"]))).all()}
    if not {"pat-1", "pat-2"} <= patients:
        return
    existing_reports = {r.resource_id for r in session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "DiagnosticReport")).all()}
    if "inbox-demo-rep-bmp" in existing_reports:
        return
    now = _now_iso()
    rows: list[FhirResource] = [
        # pat-1: normal BMP glucose + abnormal (high) glucose panel.
        _demo_observation("inbox-demo-obs-glu-normal", "Patient/pat-1",
                          "2345-7", "Glucose [Mass/volume] in Serum or Plasma",
                          92.0, "mg/dL", "N", 70.0, 99.0, now),
        _demo_observation("inbox-demo-obs-glu-high", "Patient/pat-1",
                          "2345-7", "Glucose [Mass/volume] in Serum or Plasma",
                          152.0, "mg/dL", "H", 70.0, 99.0, now),
        # pat-2: CRITICAL potassium, left unacknowledged.
        _demo_observation("inbox-demo-obs-k-crit", "Patient/pat-2",
                          "2823-3", "Potassium [Moles/volume] in Serum or Plasma",
                          6.4, "mmol/L", "HH", 3.5, 5.1, now),
    ]
    study_body = {
        "resourceType": "ImagingStudy", "id": "inbox-demo-study-1",
        "status": "available",
        "identifier": [{"system": "urn:dicom:uid",
                        "value": "2.25.111111111111111111111111111111"}],
        "modality": [{"system": "http://dicom.nema.org/resources/ontology/DCM",
                      "code": "CT", "display": "Computed Tomography"}],
        "subject": {"reference": "Patient/pat-2"},
        "started": now, "numberOfSeries": 0, "numberOfInstances": 0,
        "description": "CT head (synthetic demo study, metadata only)",
    }
    study = FhirResource(resource_type="ImagingStudy",
                         resource_id="inbox-demo-study-1", version=1,
                         body=study_body)
    fhir._apply_projections(study, "ImagingStudy", study_body,
                            "inbox-demo-study-1")
    rows.append(study)
    rows += [
        _demo_report("inbox-demo-rep-bmp", "Patient/pat-1", "51990-0",
                     "Basic metabolic panel", ["inbox-demo-obs-glu-normal"], now),
        _demo_report("inbox-demo-rep-glu", "Patient/pat-1", "51990-0",
                     "Basic metabolic panel", ["inbox-demo-obs-glu-high"], now),
        _demo_report("inbox-demo-rep-crit", "Patient/pat-2", "51990-0",
                     "Basic metabolic panel", ["inbox-demo-obs-k-crit"], now,
                     critical=True),
        _demo_report("inbox-demo-rep-ct", "Patient/pat-2", "CT-HEAD",
                     "CT head report", [], now, category="RAD",
                     category_display="Radiology",
                     conclusion="No acute intracranial abnormality "
                                "(synthetic demo report).",
                     imaging_study="inbox-demo-study-1"),
    ]
    for row in rows:
        session.add(row)
    session.flush()
    append_audit(session, actor="system", action="inbox.demo.seed",
                 entity="DiagnosticReport",
                 payload={"reports": ["inbox-demo-rep-bmp", "inbox-demo-rep-glu",
                                      "inbox-demo-rep-crit", "inbox-demo-rep-ct"],
                          "synthetic": True})


def ensure_results_inbox_defaults(session: Session) -> None:
    """Seed the published integration/inbox config (SLA, page size,
    filter defaults, escalation audience) + the published SLA rule,
    and — only when the demo patients exist — the living demo inbox.
    Idempotent throughout."""
    _seed_published(session, "integration", INBOX_KEY,
                    dict(DEFAULT_INBOX_PAYLOAD))
    # The SLA rule is seeded through app.rules' direct seed path (the
    # rfid precedent): its event is not yet in RULE_EVENTS, so it
    # cannot go through the Studio rule validator until the
    # coordinator adds the event to the registry (INTEGRATION-NEEDED).
    from .rules import _seed_published as seed_rule
    seed_rule(session, "rule", SLA_RULE_KEY, {
        "name": "Critical result SLA escalation",
        "description": ("Phase 15 (G): the results-inbox SLA scan emits "
                        "inbox.critical.unacknowledged for a released "
                        "CRITICAL result that nobody has acknowledged "
                        "within the published integration/inbox slaHours; "
                        "this rule opens the escalation Task for the "
                        "audience named in the inbox config."),
        "event": SLA_EVENT,
        "condition": {"signal": "critical", "op": "eq", "value": True},
        "action": {"type": "create_task",
                   "params": {"title": DEFAULT_INBOX_PAYLOAD["escalationTaskTitle"],
                              "ownerRole": DEFAULT_INBOX_PAYLOAD["escalationAudience"]}},
        "enabled": True,
    })
    _seed_demo_results(session)
    session.commit()
