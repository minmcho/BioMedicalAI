"""Breach operations — Phase 17, WS-COMP (BD-55 / CMP-04 remainder).

The incident register an OCR audit asks for, operable end-to-end:

- **Register** (`BreachIncident`): every suspected/confirmed breach of
  unsecured PHI with DISCOVERED and AWARENESS timestamps. Those two
  timestamps are IMMUTABLE once recorded (an update naming either is
  refused 422) — the notification clocks run from awareness, and a
  clock that can be quietly re-based is not a clock.
- **4-factor assessment as a GOVERNED FORM**: the HIPAA
  164.402 risk assessment (nature/extent, nature of PHI, the
  unauthorised person, mitigation) is a PUBLISHED Studio form
  (kind=form, key=breach-4factor). An assessment is accepted only
  against the published form: every required form item must carry an
  answer, and the stored assessment records the form key + version it
  was taken under. Editing the form is a Studio publish, never code.
- **Notification clocks from configuration**: the clock SET is
  selected by the incident's jurisdiction — US: HIPAA individual +
  HHS at 60 days (the published hipaa-baseline regulatory pack's
  `breachNotificationDays`, built-in 60); EU: GDPR supervisory
  authority at 72 hours + individual notification (the gdpr-eu pack,
  see app.jurisdictions); CA-ON: PHIPA notification at the first
  reasonable opportunity, operationalised by the phipa-ontario pack.
  The operational parameters (warning window) live in the published
  integration/breach-ops config. A regulation change is a config
  publish, never a code branch.
- **Escalation**: `POST /breach/incidents/{id}/check-clocks` evaluates
  the clocks; a clock inside its warning window (or breached) with no
  matching notification on file raises a FHIR Task (the same Task
  surface the rules engine's create_task action writes) and audits
  the escalation. NOTE: the rules engine's event registry
  (configstore.RULE_EVENTS) is a closed server registry and carries
  no breach event, so no published *rule config* can bind this —
  escalation is direct Task creation here, stated plainly rather
  than dressed up as a rule firing.
- **Evidence pack**: register entry + assessment + notifications +
  clocks + the hash-chained audit extract naming the incident — the
  artefact an OCR audit / supervisory-authority inquiry asks for.

Studio config surfaces: form/breach-4factor (the assessment form),
regulatory_pack/hipaa-baseline + regulatory_pack/gdpr-eu +
regulatory_pack/phipa-ontario (clock parameters, via
app.compliance / app.jurisdictions), integration/breach-ops
(escalation warning window), permissions/role-matrix (capabilities
breach.read / breach.manage).
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import fhir
from .audit import append_audit
from .configstore import (CAPABILITY_DEFAULTS, list_config_versions,
                          published_config, role_allowed)
from .db import Base
from .models import AuditLog, ConfigStatus, FhirResource, StudioConfig, User, utcnow
from .security import get_current_user, get_db

router = APIRouter(prefix="/breach", tags=["breach"])

FORM_KEY = "breach-4factor"
OPS_KEY = "breach-ops"

_CAP_FALLBACKS = {
    "breach.read": ("admin", "configurator", "clinician", "viewer"),
    "breach.manage": ("admin", "configurator"),
}

# The four HIPAA factors, by form linkId (the form definition below
# and the assessment validator agree on these ids — the form is the
# governed artefact, this tuple is only the server-side checklist).
FACTORS = ("factor1-nature-extent", "factor2-nature-of-phi",
           "factor3-unauthorised-person", "factor4-mitigation")


def _cap_allowed(session: Session, user: User, capability: str) -> bool:
    cfg = published_config(session, "permissions", "role-matrix")
    if cfg is not None:
        overrides = ((cfg.payload or {}).get("capabilities") or {}).get(capability)
        if isinstance(overrides, dict) and user.role.value in overrides:
            return bool(overrides[user.role.value])
    if capability in CAPABILITY_DEFAULTS:
        return role_allowed(session, user.role, capability)
    return user.role.value in _CAP_FALLBACKS.get(capability, ())


def _require(capability: str):
    def dep(session: Session = Depends(get_db),
            user: User = Depends(get_current_user)) -> User:
        if not _cap_allowed(session, user, capability):
            raise HTTPException(
                status_code=403,
                detail=f"Role {user.role.value} lacks capability '{capability}'")
        return user
    return dep


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _parse_dt(raw, field: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=422, detail=f"'{field}' must be an ISO datetime")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"'{field}' must be an ISO datetime")
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Tables (module-local, the Phase 16 research_ops pattern)
# ---------------------------------------------------------------------------

class BreachIncident(Base):
    __tablename__ = "breach_incidents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    jurisdiction: Mapped[str] = mapped_column(String(20), nullable=False, default="US")
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    affected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    awareness_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="open")
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class BreachAssessment(Base):
    __tablename__ = "breach_assessments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    form_key: Mapped[str] = mapped_column(String(120), nullable=False, default=FORM_KEY)
    form_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    factors: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    risk_level: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    is_breach: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    assessed_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BreachNotification(Base):
    __tablename__ = "breach_notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    recipient: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    channel: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Governed defaults: the 4-factor form (published) + ops config (published)
# ---------------------------------------------------------------------------

def default_form_payload() -> dict:
    def item(link_id, text, type_, required=True):
        return {"linkId": link_id, "text": text, "type": type_, "required": required}
    return {"resourceType": "Questionnaire",
            "title": "Breach risk assessment — HIPAA 4-factor (45 CFR 164.402)",
            "item": [
                item("factor1-nature-extent",
                     "Factor 1 — nature and extent of PHI involved, "
                     "including identifiers and likelihood of re-identification", "text"),
                item("factor2-nature-of-phi",
                     "Factor 2 — nature of the PHI / sensitivity of the "
                     "information involved", "text"),
                item("factor3-unauthorised-person",
                     "Factor 3 — the unauthorised person who used the PHI "
                     "or to whom the disclosure was made", "text"),
                item("factor4-mitigation",
                     "Factor 4 — whether PHI was actually acquired or viewed, "
                     "and the extent to which the risk has been mitigated", "text"),
                item("risk-level", "Overall probability that PHI has been "
                     "compromised (low / not-low)", "string"),
                item("conclusion", "Conclusion: breach / not a breach, with "
                     "reasoning", "text"),
            ]}


def default_ops_payload() -> dict:
    return {"escalationWarningHours": 72,
            "notes": ("Breach-operations parameters. Clock DURATIONS come "
                      "from the published regulatory packs (hipaa-baseline "
                      "breachNotificationDays; gdpr-eu breachClockHours; "
                      "phipa-ontario breachNotifyHours); this config carries "
                      "only the escalation warning window.")}


def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
    if list_config_versions(session, kind, key):
        return
    row = StudioConfig(kind=kind, key=key, version=1, status=ConfigStatus.published,
                       payload=payload, created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:{kind}/{key}@v1",
                 payload={"kind": kind, "key": key, "version": 1, "seeded_default": True})


def ensure_breach_defaults(session: Session) -> None:
    _seed_published(session, "form", FORM_KEY, default_form_payload())
    _seed_published(session, "integration", OPS_KEY, default_ops_payload())
    session.commit()


def _ensure(session: Session) -> None:
    if published_config(session, "form", FORM_KEY) is None \
            and not list_config_versions(session, "form", FORM_KEY):
        ensure_breach_defaults(session)


# ---------------------------------------------------------------------------
# Clocks — computed from published regulatory-pack parameters
# ---------------------------------------------------------------------------

def _pack_params(session: Session, key: str) -> dict:
    cfg = published_config(session, "regulatory_pack", key)
    params = (cfg.payload or {}).get("parameters") if cfg is not None else {}
    return params if isinstance(params, dict) else {}


def _int_param(params: dict, name: str, default: int) -> int:
    value = params.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default


def clock_set(session: Session, incident: BreachIncident) -> list[dict]:
    """The notification clocks for this incident's jurisdiction.
    Each clock: kind, label, due_at (from AWARENESS), source."""
    aware = _aware(incident.awareness_at)
    clocks = []
    jur = (incident.jurisdiction or "US").upper()
    if jur == "EU":
        params = _pack_params(session, "gdpr-eu")
        sa_hours = _int_param(params, "breachClockHours", 72)
        ind_days = _int_param(params, "individualNotificationDays", 30)
        clocks.append({"kind": "gdpr_supervisory_authority",
                       "label": "GDPR Art. 33 supervisory-authority notification",
                       "due_at": aware + timedelta(hours=sa_hours),
                       "source": "regulatory_pack/gdpr-eu breachClockHours (default 72)"})
        clocks.append({"kind": "gdpr_individual",
                       "label": "GDPR Art. 34 individual notification (without undue delay)",
                       "due_at": aware + timedelta(days=ind_days),
                       "source": "regulatory_pack/gdpr-eu individualNotificationDays (default 30)"})
    elif jur in ("CA-ON", "PHIPA", "CA"):
        params = _pack_params(session, "phipa-ontario")
        hours = _int_param(params, "breachNotifyHours", 72)
        clocks.append({"kind": "phipa_commissioner",
                       "label": "PHIPA notification to the Commissioner (first reasonable opportunity)",
                       "due_at": aware + timedelta(hours=hours),
                       "source": "regulatory_pack/phipa-ontario breachNotifyHours (default 72)"})
        clocks.append({"kind": "phipa_individual",
                       "label": "PHIPA notification to affected individuals (first reasonable opportunity)",
                       "due_at": aware + timedelta(hours=hours),
                       "source": "regulatory_pack/phipa-ontario breachNotifyHours (default 72)"})
    else:  # US / HIPAA default
        params = _pack_params(session, "hipaa-baseline")
        days = _int_param(params, "breachNotificationDays", 60)
        clocks.append({"kind": "hipaa_individual",
                       "label": "HIPAA 164.404 individual notification (60 days)",
                       "due_at": aware + timedelta(days=days),
                       "source": "regulatory_pack/hipaa-baseline breachNotificationDays (default 60)"})
        clocks.append({"kind": "hipaa_hhs",
                       "label": "HIPAA 164.408 HHS notification (60 days)",
                       "due_at": aware + timedelta(days=days),
                       "source": "regulatory_pack/hipaa-baseline breachNotificationDays (default 60)"})
    return clocks


def _warning_hours(session: Session) -> int:
    cfg = published_config(session, "integration", OPS_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    value = payload.get("escalationWarningHours")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 72


def _clocks_out(session: Session, incident: BreachIncident, now: datetime) -> list[dict]:
    sent = {n.kind for n in session.scalars(select(BreachNotification).where(
        BreachNotification.incident_id == incident.incident_id)).all()}
    warning = timedelta(hours=_warning_hours(session))
    out = []
    for clock in clock_set(session, incident):
        due = clock["due_at"]
        satisfied = clock["kind"] in sent
        if satisfied:
            state = "satisfied"
        elif now > due:
            state = "breached"
        elif due - now <= warning:
            state = "due_soon"
        else:
            state = "pending"
        out.append({"kind": clock["kind"], "label": clock["label"],
                    "due_at": due.isoformat(), "source": clock["source"],
                    "satisfied": satisfied, "state": state})
    return out


def _incident_out(session: Session, row: BreachIncident, now: datetime | None = None) -> dict:
    now = now or _now()
    return {"incident_id": row.incident_id, "title": row.title,
            "description": row.description, "jurisdiction": row.jurisdiction,
            "patient_ref": row.patient_ref, "affected_count": row.affected_count,
            "discovered_at": _aware(row.discovered_at).isoformat() if row.discovered_at else None,
            "awareness_at": _aware(row.awareness_at).isoformat() if row.awareness_at else None,
            "status": row.status, "created_by": row.created_by,
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None,
            "clocks": _clocks_out(session, row, now)}


def _assessment_out(row: BreachAssessment) -> dict:
    return {"id": row.id, "incident_id": row.incident_id, "form_key": row.form_key,
            "form_version": row.form_version, "factors": row.factors,
            "risk_level": row.risk_level, "is_breach": row.is_breach,
            "assessed_by": row.assessed_by,
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None}


def _notification_out(row: BreachNotification) -> dict:
    return {"id": row.id, "incident_id": row.incident_id, "kind": row.kind,
            "recipient": row.recipient, "channel": row.channel,
            "sent_at": _aware(row.sent_at).isoformat() if row.sent_at else None,
            "recorded_by": row.recorded_by}


def _row(session: Session, incident_id: str) -> BreachIncident:
    row = session.scalars(select(BreachIncident).where(
        BreachIncident.incident_id == incident_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Breach incident '{incident_id}' not found")
    return row


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

@router.post("/incidents", status_code=201)
async def create_incident(request: Request, session: Session = Depends(get_db),
                          user: User = Depends(_require("breach.manage"))):
    _ensure(session)
    body = await _json_body(request)
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise HTTPException(status_code=422, detail="'title' is required")
    discovered = _parse_dt(body.get("discovered_at") or body.get("discoveredAt"), "discovered_at")
    awareness_raw = body.get("awareness_at") or body.get("awarenessAt")
    awareness = _parse_dt(awareness_raw, "awareness_at") if awareness_raw else discovered
    if awareness < discovered:
        raise HTTPException(status_code=422, detail="'awareness_at' cannot precede 'discovered_at'")
    jurisdiction = str(body.get("jurisdiction") or "US").upper()
    if jurisdiction not in ("US", "EU", "CA-ON", "PHIPA", "CA"):
        raise HTTPException(status_code=422, detail="'jurisdiction' must be US, EU or CA-ON")
    if jurisdiction == "PHIPA":
        jurisdiction = "CA-ON"
    affected = body.get("affected_count", 0)
    if isinstance(affected, bool) or not isinstance(affected, int) or affected < 0:
        raise HTTPException(status_code=422, detail="'affected_count' must be a non-negative integer")
    row = BreachIncident(
        incident_id=f"brc-{uuid.uuid4().hex[:12]}", title=title.strip(),
        description=str(body.get("description") or ""),
        jurisdiction=jurisdiction,
        patient_ref=(str(body["patient_ref"]) if body.get("patient_ref") else None),
        affected_count=affected, discovered_at=discovered, awareness_at=awareness,
        status="open", created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="breach.incident.create",
                 entity=f"breach:{row.incident_id}",
                 payload={"incident_id": row.incident_id, "jurisdiction": jurisdiction,
                          "awareness_at": awareness.isoformat(),
                          "discovered_at": discovered.isoformat(),
                          "affected_count": affected})
    session.commit()
    return _incident_out(session, row)


@router.get("/incidents")
def list_incidents(session: Session = Depends(get_db),
                   user: User = Depends(_require("breach.read"))):
    _ensure(session)
    rows = session.scalars(select(BreachIncident).order_by(BreachIncident.id.asc())).all()
    now = _now()
    return {"items": [_incident_out(session, r, now) for r in rows], "total": len(rows)}


@router.get("/incidents/{incident_id}")
def get_incident(incident_id: str, session: Session = Depends(get_db),
                 user: User = Depends(_require("breach.read"))):
    _ensure(session)
    return _incident_out(session, _row(session, incident_id))


@router.put("/incidents/{incident_id}")
async def update_incident(incident_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(_require("breach.manage"))):
    """Edit mutable register fields. discovered_at / awareness_at are
    IMMUTABLE: naming either with a different value is refused 422 —
    the clocks run from awareness and must never be re-based."""
    _ensure(session)
    row = _row(session, incident_id)
    body = await _json_body(request)
    for field, current in (("discovered_at", row.discovered_at),
                           ("awareness_at", row.awareness_at)):
        for alias in (field, "discoveredAt" if field == "discovered_at" else "awarenessAt"):
            if alias in body and body[alias] is not None:
                if _parse_dt(body[alias], field) != _aware(current):
                    raise HTTPException(
                        status_code=422,
                        detail=f"'{field}' is immutable once recorded (the "
                               "notification clocks run from it)")
    if isinstance(body.get("title"), str) and body["title"].strip():
        row.title = body["title"].strip()
    if isinstance(body.get("description"), str):
        row.description = body["description"]
    if body.get("status") in ("open", "assessing", "notifying", "closed"):
        row.status = body["status"]
    if isinstance(body.get("affected_count"), int) and not isinstance(body.get("affected_count"), bool) \
            and body["affected_count"] >= 0:
        row.affected_count = body["affected_count"]
    session.flush()
    append_audit(session, actor=user.username, action="breach.incident.update",
                 entity=f"breach:{incident_id}", payload={"incident_id": incident_id})
    session.commit()
    return _incident_out(session, row)


# ---------------------------------------------------------------------------
# 4-factor assessment (governed form)
# ---------------------------------------------------------------------------

@router.post("/incidents/{incident_id}/assessment", status_code=201)
async def assess(incident_id: str, request: Request, session: Session = Depends(get_db),
                 user: User = Depends(_require("breach.manage"))):
    _ensure(session)
    row = _row(session, incident_id)
    form = published_config(session, "form", FORM_KEY)
    if form is None:
        raise HTTPException(status_code=503,
                            detail=f"No published assessment form (form/{FORM_KEY}); "
                                   "publish one in Studio before assessing")
    body = await _json_body(request)
    factors = body.get("factors") if isinstance(body.get("factors"), dict) else body
    required = [i["linkId"] for i in (form.payload or {}).get("item") or []
                if isinstance(i, dict) and i.get("required") and i.get("linkId")]
    missing = [link for link in required
               if not isinstance(factors.get(link), str) or not factors[link].strip()]
    if missing:
        raise HTTPException(status_code=422,
                            detail=f"Assessment is missing required form items: {missing} "
                                   f"(form {FORM_KEY}@v{form.version})")
    risk = str(factors.get("risk-level") or body.get("risk_level") or "")
    is_breach = body.get("is_breach")
    if not isinstance(is_breach, bool):
        is_breach = risk.strip().lower() != "low"
    record = BreachAssessment(
        incident_id=incident_id, form_key=FORM_KEY, form_version=form.version,
        factors={k: factors[k] for k in required if k in factors},
        risk_level=risk, is_breach=is_breach, assessed_by=user.username)
    session.add(record)
    if row.status == "open":
        row.status = "assessing"
    session.flush()
    append_audit(session, actor=user.username, action="breach.assessment.create",
                 entity=f"breach:{incident_id}",
                 payload={"incident_id": incident_id, "assessment_id": record.id,
                          "form": f"{FORM_KEY}@v{form.version}", "is_breach": is_breach,
                          "risk_level": risk})
    session.commit()
    return _assessment_out(record)


@router.get("/incidents/{incident_id}/assessment")
def get_assessment(incident_id: str, session: Session = Depends(get_db),
                   user: User = Depends(_require("breach.read"))):
    _ensure(session)
    _row(session, incident_id)
    record = session.scalars(select(BreachAssessment).where(
        BreachAssessment.incident_id == incident_id).order_by(
        BreachAssessment.id.desc())).first()
    if record is None:
        raise HTTPException(status_code=404, detail="No assessment recorded for this incident")
    return _assessment_out(record)


# ---------------------------------------------------------------------------
# Notifications + escalation
# ---------------------------------------------------------------------------

@router.post("/incidents/{incident_id}/notifications", status_code=201)
async def notify(incident_id: str, request: Request, session: Session = Depends(get_db),
                 user: User = Depends(_require("breach.manage"))):
    _ensure(session)
    row = _row(session, incident_id)
    body = await _json_body(request)
    kinds = {c["kind"] for c in clock_set(session, row)}
    kind = body.get("kind")
    if kind not in kinds:
        raise HTTPException(status_code=422,
                            detail=f"'kind' must be one of {sorted(kinds)} for this "
                                   "incident's jurisdiction clock set")
    sent_raw = body.get("sent_at") or body.get("sentAt")
    sent_at = _parse_dt(sent_raw, "sent_at") if sent_raw else _now()
    record = BreachNotification(
        incident_id=incident_id, kind=kind,
        recipient=str(body.get("recipient") or ""), channel=str(body.get("channel") or ""),
        sent_at=sent_at, recorded_by=user.username)
    session.add(record)
    session.flush()
    append_audit(session, actor=user.username, action="breach.notification.record",
                 entity=f"breach:{incident_id}",
                 payload={"incident_id": incident_id, "kind": kind,
                          "recipient": record.recipient, "sent_at": sent_at.isoformat()})
    session.commit()
    return _notification_out(record)


@router.get("/incidents/{incident_id}/notifications")
def list_notifications(incident_id: str, session: Session = Depends(get_db),
                       user: User = Depends(_require("breach.read"))):
    _ensure(session)
    _row(session, incident_id)
    rows = session.scalars(select(BreachNotification).where(
        BreachNotification.incident_id == incident_id).order_by(
        BreachNotification.id.asc())).all()
    return {"items": [_notification_out(r) for r in rows], "total": len(rows)}


@router.post("/incidents/{incident_id}/check-clocks")
def check_clocks(incident_id: str, session: Session = Depends(get_db),
                 user: User = Depends(_require("breach.manage"))):
    """Evaluate the clocks NOW. An unsatisfied clock that is due_soon
    or breached raises an escalation Task (FHIR Task, the rules
    engine's task surface) unless an open escalation Task for that
    clock already exists. Every check is audited."""
    _ensure(session)
    row = _row(session, incident_id)
    now = _now()
    clocks = _clocks_out(session, row, now)
    escalations = []
    for clock in clocks:
        if clock["state"] not in ("due_soon", "breached"):
            continue
        marker = f"breach-escalation:{incident_id}:{clock['kind']}"
        existing = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Task")).all()
        if any(marker in str((t.body or {}).get("description") or "") for t in existing):
            escalations.append({"clock": clock["kind"], "task": "already-open"})
            continue
        task_id = str(uuid.uuid4())
        body = {"resourceType": "Task", "id": task_id, "status": "requested",
                "intent": "order",
                "description": f"{marker} — {clock['label']} due {clock['due_at']} "
                               f"(state: {clock['state']})",
                "authoredOn": now.isoformat(),
                "requester": {"display": "breach-ops"},
                "owner": {"display": "privacy-office"}}
        if row.patient_ref:
            body["for"] = {"reference": row.patient_ref}
        task = FhirResource(resource_type="Task", resource_id=task_id, version=1, body=body)
        fhir._apply_projections(task, "Task", body, task_id)
        session.add(task)
        session.flush()
        append_audit(session, actor=user.username, action="breach.clock.escalate",
                     entity=f"breach:{incident_id}",
                     payload={"incident_id": incident_id, "clock": clock["kind"],
                              "state": clock["state"], "task_id": task_id,
                              "due_at": clock["due_at"]})
        escalations.append({"clock": clock["kind"], "task_id": task_id,
                            "state": clock["state"]})
    session.commit()
    return {"incident_id": incident_id, "clocks": clocks, "escalations": escalations}


# ---------------------------------------------------------------------------
# Evidence pack — what an OCR audit asks for
# ---------------------------------------------------------------------------

@router.get("/incidents/{incident_id}/evidence-pack")
def evidence_pack(incident_id: str, session: Session = Depends(get_db),
                  user: User = Depends(_require("breach.read"))):
    _ensure(session)
    row = _row(session, incident_id)
    assessments = session.scalars(select(BreachAssessment).where(
        BreachAssessment.incident_id == incident_id).order_by(
        BreachAssessment.id.asc())).all()
    notifications = session.scalars(select(BreachNotification).where(
        BreachNotification.incident_id == incident_id).order_by(
        BreachNotification.id.asc())).all()
    audit_entries = []
    for entry in session.scalars(select(AuditLog).order_by(AuditLog.id.asc())).all():
        if incident_id in (entry.entity or "") or incident_id in str(entry.payload or {}):
            audit_entries.append({"id": entry.id, "actor": entry.actor,
                                  "action": entry.action, "entity": entry.entity,
                                  "created_at": _aware(entry.created_at).isoformat()
                                  if entry.created_at else None,
                                  "entry_hash": entry.entry_hash})
    append_audit(session, actor=user.username, action="breach.evidence.export",
                 entity=f"breach:{incident_id}",
                 payload={"incident_id": incident_id,
                          "audit_entries_included": len(audit_entries)})
    session.commit()
    return {"incident": _incident_out(session, row),
            "assessments": [_assessment_out(a) for a in assessments],
            "notifications": [_notification_out(n) for n in notifications],
            "audit_extract": audit_entries,
            "audit_extract_total": len(audit_entries),
            "generated_by": user.username, "generated_at": _now().isoformat(),
            "note": ("Evidence pack for OCR / supervisory review: register "
                     "entry, governed 4-factor assessment(s), recorded "
                     "notifications with clock states, and the hash-chained "
                     "audit extract naming this incident.")}
