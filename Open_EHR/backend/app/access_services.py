"""Phase 15 (BD-21/22/23/28/31) — utilisation review, online bill
pay, portal proxy access, telehealth at scale, credential-gated
scheduling.

BD-21 UTILISATION REVIEW: UR cases are operational records in this
module's OWN table, opened for admissions (patient + encounter
refs). Review states: pending -> in-review -> approved |
denial-risk | escalated. Criteria are BRING-YOUR-OWN governed
config: terminology/ur-criteria is seeded with a clearly-labelled
SITE-FIXTURE example set ONLY. Licensed criteria content
(InterQual / MCG class) is NEVER shipped or invented by this
system — a site licences its own criteria and publishes them as a
new governed version of that config. A review records which
criteria set (code) and which evidence fields were checked. When
the first reviewer marks a case denial-risk, a physician-advisor
second review is REQUIRED and the advisor must differ from the
first reviewer (409 otherwise — the app.privacy break-glass
opener != reviewer split pattern). GET /ur/denial-risk is the JOIN
FEED for the revenue denials workqueue (app.revenue
GET /billing/denials): records carry the claim / encounter /
patient refs the revenue surface joins on. This module does not
fork revenue.py.

BD-22 ONLINE BILL PAY: patient-facing endpoints under the portal
pattern (the PATIENT token's own data only). The balance is
computed from app.revenue's statement / ledger machinery (imported
and reused — there is no second ledger). Payments go through a
governed payment-provider interface: the published config
integration/payment-provider is {mode: "test", provider:
"test-mode"} and the in-module test-mode provider validates the
amount, records the payment via revenue's own patient-payment
ledger (the PatientPayment table + revenue.claim_financials due
check), and returns a receipt carrying provider_mode: "test". A
live processor is NOT integrated and no payload ever claims
otherwise.

BD-23 PORTAL PROXY ACCESS: grant records in this module's own
table (patient, proxy username — an existing account, scopes
validated against published terminology/proxy-scopes, granted-by,
optional expiry, status active | revoked). Scoped proxy reads ride
the portal prefix; EVERY successful proxy read is hash-audit-
chained with actor = the proxy username and payload
on_behalf_of = the patient ref.

BD-28 TELEHEALTH AT SCALE: per-service configuration is published
config (integration/telehealth-services) layered over Phase 9's
single template (app.engagement integration/telehealth): a service
entry wins for join-template / waiting-room message / intake form,
otherwise the Phase 9 template applies. Virtual-visit lifecycle
records carry states scheduled -> checked-in -> waiting ->
in-progress -> completed | virtual-no-show (a terminal state
distinct from a clinic no-show). An interpreter-on-video is a
REFERENCE (interpreter_request_id, owned by the parallel
interpreter workstream) plus a display field — no import of that
module. After a completed visit the patient submits the governed
outcome form (form/telehealth-outcome) through the portal — this
is the BD-28 acceptance surface converting Phase 9's
outcome-capture NOT RUN.

BD-31 CREDENTIAL-GATED SCHEDULING: a real checker service in this
module (POST /scheduling/credential-check, also callable as
:func:`evaluate_credential_check`) evaluating the staff member's
credential records in app.people (StaffMember / StaffCredential):
allowed only when a credential covering the privilege (credential
type == privilege) is present and unexpired at the booking
instant (UTC). The response names the deciding credential or the
refusal reason (absent / expired / unknown-staff). Wiring the
checker into the booking endpoints that assign a named clinician
is listed as INTEGRATION-NEEDED in the workstream report — those
files are not edited here.
"""
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import DateTime, Float, Integer, JSON, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import engagement, fhir, people, revenue
from .audit import append_audit
from .configstore import published_config
from .db import Base
from .models import (ConfigStatus, FhirResource, PatientPayment, Role,
                     StaffCredential, StaffMember, StudioConfig, User,
                     UserAccount, utcnow)
from .security import get_current_user, get_db, require_capability, require_roles

router = APIRouter(tags=["access_services"])

UR_CRITERIA_KEY = "ur-criteria"
PROXY_SCOPES_KEY = "proxy-scopes"
PAYMENT_PROVIDER_KEY = "payment-provider"
TELEHEALTH_SERVICES_KEY = "telehealth-services"
TELEHEALTH_OUTCOME_FORM = "telehealth-outcome"

UR_STATES = ("pending", "in-review", "approved", "denial-risk", "escalated")
UR_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"in-review"},
    "in-review": {"approved", "denial-risk", "escalated"},
    "denial-risk": set(),  # moved only by the advisor review
    "approved": set(),
    "escalated": {"in-review"},
}
ADVISOR_DECISIONS = ("approved", "denial-risk", "escalated")

VIRTUAL_VISIT_STATES = ("scheduled", "checked-in", "waiting", "in-progress",
                        "completed", "virtual-no-show")
VIRTUAL_VISIT_TRANSITIONS: dict[str, set[str]] = {
    "scheduled": {"checked-in", "virtual-no-show"},
    "checked-in": {"waiting", "virtual-no-show"},
    "waiting": {"in-progress", "virtual-no-show"},
    "in-progress": {"completed", "virtual-no-show"},
    "completed": set(),
    "virtual-no-show": set(),
}

PAYMENT_PROVIDER_DISCLAIMER = (
    "Test-mode payment provider only: this records the payment in "
    "the revenue ledger (app.revenue PatientPayment). No live card "
    "processor or payment gateway is integrated.")


# ---------------------------------------------------------------------------
# Own tables
# ---------------------------------------------------------------------------

class URCase(Base):
    __tablename__ = "ur_cases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    encounter_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    claim_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    claim_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    criteria_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    evidence_checked: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    first_reviewer: Mapped[str | None] = mapped_column(String(120), nullable=True)
    advisor_reviewer: Mapped[str | None] = mapped_column(String(120), nullable=True)
    advisor_decision: Mapped[str | None] = mapped_column(String(40), nullable=True)
    history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class ProxyGrant(Base):
    __tablename__ = "portal_proxy_grants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    grant_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    proxy_username: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    granted_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    grant_source: Mapped[str] = mapped_column(String(40), nullable=False,
                                              default="patient")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class TelehealthVisit(Base):
    __tablename__ = "telehealth_visits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    visit_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    appointment_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    appointment_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    service_line: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="scheduled")
    interpreter_request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    interpreter_display: Mapped[str | None] = mapped_column(String(300), nullable=True)
    timestamps: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    state_history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    outcome_response_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _parse_instant(raw, field: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=422, detail=f"'{field}' must be an ISO datetime/date string")
    text = raw.strip()
    try:
        if len(text) == 10:
            parsed = datetime.combine(date.fromisoformat(text),
                                      datetime.max.time().replace(microsecond=0),
                                      tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"'{field}' must be an ISO datetime/date string")
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


def _concepts(session: Session, kind: str, key: str) -> list[dict]:
    cfg = published_config(session, kind, key)
    if cfg is None:
        return []
    return [c for c in (cfg.payload or {}).get("concepts") or []
            if isinstance(c, dict)]


# ---------------------------------------------------------------------------
# Default Studio configuration
# ---------------------------------------------------------------------------

def default_ur_criteria_payload() -> dict:
    def crit(code, display, description, evidence):
        return {"code": code, "display": display, "active": True,
                "description": description, "requiredEvidence": evidence,
                "fixture": True}
    return {
        "name": "Utilisation review criteria (SITE-FIXTURE example set only)",
        "system": "urn:opencare:ur-criteria",
        "notice": ("SITE-FIXTURE EXAMPLE ONLY — these are locally invented "
                   "example criteria for testing the review workflow. "
                   "Licensed criteria content (InterQual / MCG class) is "
                   "never shipped or invented by this system; a site "
                   "licences its own criteria and publishes them here as a "
                   "new governed version."),
        "concepts": [
            crit("site-fixture-med-nec-01",
                 "SITE-FIXTURE example: medical-necessity review",
                 "SITE-FIXTURE example criteria set for local testing only — "
                 "not licensed InterQual/MCG content.",
                 ["diagnosis-documented", "clinical-notes",
                  "conservative-therapy-noted"]),
            crit("site-fixture-continued-stay-01",
                 "SITE-FIXTURE example: continued-stay review",
                 "SITE-FIXTURE example criteria set for local testing only — "
                 "not licensed InterQual/MCG content.",
                 ["daily-progress-note", "discharge-plan-started",
                  "vitals-stable"]),
        ]}


def default_proxy_scopes_payload() -> dict:
    def scope(code, display):
        return {"code": code, "display": display, "active": True}
    return {"name": "Portal proxy access scopes",
            "system": "urn:opencare:proxy-scope",
            "concepts": [scope("schedule", "View appointments"),
                         scope("messages", "View message threads"),
                         scope("results", "View released results")]}


def default_payment_provider_payload() -> dict:
    return {"mode": "test", "provider": "test-mode", "live": False,
            "disclaimer": PAYMENT_PROVIDER_DISCLAIMER}


def default_telehealth_services_payload() -> dict:
    return {
        "disclaimer": ("Per-service telehealth configuration layered over "
                       "the Phase 9 integration/telehealth template: a "
                       "service entry wins for joinUrlTemplate / "
                       "waitingRoomMessage / intakeFormKey; anything a "
                       "service does not name falls back to the Phase 9 "
                       "template. Join links are provider DATA — OpenCare "
                       "does not provide video."),
        "services": [
            {"code": "cardiology", "serviceLine": "cardiology",
             "display": "Cardiology telehealth",
             "joinUrlTemplate": "https://video.example.invalid/cardiology/{appointmentId}",
             "waitingRoomMessage": "Welcome to cardiology telehealth — your clinician will join shortly.",
             "intakeFormKey": "telehealth-intake"},
            {"code": "general", "serviceLine": "general",
             "display": "General telehealth",
             "waitingRoomMessage": "Welcome — your clinician will join shortly.",
             "intakeFormKey": "telehealth-intake"},
        ]}


def default_telehealth_outcome_payload() -> dict:
    return {"resourceType": "Questionnaire",
            "title": "Telehealth visit outcome (patient-reported)",
            "item": [
                {"linkId": "outcome-summary",
                 "text": "How did the visit go? Summarise the outcome.",
                 "type": "text", "required": True},
                {"linkId": "goals-met",
                 "text": "Were the goals of the visit met?",
                 "type": "boolean", "required": True},
                {"linkId": "follow-up-needed",
                 "text": "Do you need a follow-up appointment?",
                 "type": "boolean", "required": False},
            ]}


def ensure_access_services_defaults(session: Session) -> None:
    """Seed this module's published Studio configuration defaults
    (idempotent)."""
    _seed_published(session, "terminology", UR_CRITERIA_KEY,
                    default_ur_criteria_payload())
    _seed_published(session, "terminology", PROXY_SCOPES_KEY,
                    default_proxy_scopes_payload())
    _seed_published(session, "integration", PAYMENT_PROVIDER_KEY,
                    default_payment_provider_payload())
    _seed_published(session, "integration", TELEHEALTH_SERVICES_KEY,
                    default_telehealth_services_payload())
    _seed_published(session, "form", TELEHEALTH_OUTCOME_FORM,
                    default_telehealth_outcome_payload())
    session.commit()


# ---------------------------------------------------------------------------
# BD-21 — Utilisation review
# ---------------------------------------------------------------------------

def _ur_out(row: URCase) -> dict:
    return {"id": row.case_id, "case_id": row.case_id,
            "patient": row.patient_ref, "patient_ref": row.patient_ref,
            "encounter_ref": row.encounter_ref,
            "encounter_id": (row.encounter_ref.split("/", 1)[1]
                             if row.encounter_ref and "/" in row.encounter_ref
                             else None),
            "claim_ref": row.claim_ref, "claim_id": row.claim_id,
            "status": row.status, "criteria_code": row.criteria_code,
            "criteria_set": row.criteria_code,
            "evidence_checked": row.evidence_checked or [],
            "first_reviewer": row.first_reviewer,
            "advisor_reviewer": row.advisor_reviewer,
            "advisor_decision": row.advisor_decision,
            "advisor_review_required": row.status == "denial-risk",
            "history": row.history or [], "note": row.note,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def _ur_row(session: Session, case_id: str) -> URCase:
    row = session.scalars(select(URCase).where(URCase.case_id == case_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"UR case {case_id} not found")
    return row


def _criteria_concept(session: Session, code: str) -> dict:
    for concept in _concepts(session, "terminology", UR_CRITERIA_KEY):
        if concept.get("code") == code:
            if concept.get("active") is False:
                raise HTTPException(
                    status_code=422,
                    detail=f"UR criteria '{code}' is inactive in the published "
                           "terminology/ur-criteria config")
            return concept
    raise HTTPException(
        status_code=422,
        detail=f"Unknown UR criteria '{code}': not in the published "
               "terminology/ur-criteria config (criteria are bring-your-own "
               "governed config; licensed InterQual/MCG content is not shipped)")


def _validate_evidence(concept: dict, evidence) -> list[str]:
    if evidence is None:
        return []
    if not isinstance(evidence, list) or any(
            not isinstance(e, str) or not e for e in evidence):
        raise HTTPException(status_code=422,
                            detail="'evidence_checked' must be a list of field names")
    required = concept.get("requiredEvidence") or []
    unknown = [e for e in evidence if required and e not in required]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Evidence field(s) {unknown} are not in the required-evidence "
                   f"list for criteria '{concept.get('code')}' ({required})")
    return list(evidence)


def _patient_ref_from(session: Session, payload: dict) -> str:
    ref = payload.get("patient_ref") or payload.get("patient")
    subject = payload.get("subject")
    if not ref and isinstance(subject, dict):
        ref = subject.get("reference")
    if not ref or not isinstance(ref, str):
        raise HTTPException(status_code=422, detail="Requires 'patient' naming a Patient")
    ref = fhir._normalize_patient_ref(ref)
    fhir._get_row("Patient", ref.split("/", 1)[1], session)
    return ref


@router.post("/ur/cases", status_code=201)
async def create_ur_case(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(require_capability("ur.work"))):
    payload = await _json_body(request)
    patient_ref = _patient_ref_from(session, payload)
    encounter_ref = None
    raw_enc = payload.get("encounter_ref") or payload.get("encounter_id") \
        or payload.get("encounter")
    if isinstance(raw_enc, dict):
        raw_enc = raw_enc.get("reference")
    if raw_enc:
        encounter_ref = raw_enc if "/" in str(raw_enc) else f"Encounter/{raw_enc}"
        enc_row = fhir._get_row("Encounter", encounter_ref.split("/", 1)[1], session)
        if enc_row.patient_ref and enc_row.patient_ref != patient_ref:
            raise HTTPException(
                status_code=422,
                detail=f"Encounter {encounter_ref} belongs to {enc_row.patient_ref}, "
                       f"not {patient_ref}")
    claim_id = payload.get("claim_id")
    claim_ref = payload.get("claim_ref")
    if claim_ref and not claim_id:
        claim_id = str(claim_ref).split("/", 1)[1] if "/" in str(claim_ref) else str(claim_ref)
    if claim_id:
        fhir._get_row("Claim", str(claim_id), session)  # 404 if unknown
        claim_ref = f"Claim/{claim_id}"
    criteria_code = payload.get("criteria_code") or payload.get("criteria_set")
    if criteria_code:
        _criteria_concept(session, str(criteria_code))
    row = URCase(case_id=str(uuid.uuid4()), patient_ref=patient_ref,
                 encounter_ref=encounter_ref, claim_ref=claim_ref,
                 claim_id=str(claim_id) if claim_id else None,
                 status="pending",
                 criteria_code=str(criteria_code) if criteria_code else None,
                 evidence_checked=[], first_reviewer=None,
                 history=[{"from": None, "to": "pending", "by": user.username,
                           "at": _now_iso()}],
                 note=payload.get("note") if isinstance(payload.get("note"), str) else None,
                 created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="ur.case.create",
                 entity=f"URCase/{row.case_id}",
                 payload={"patient": patient_ref, "encounter_ref": encounter_ref,
                          "claim_ref": claim_ref, "criteria_code": row.criteria_code})
    session.commit()
    return _ur_out(row)


@router.get("/ur/cases")
def list_ur_cases(status: str | None = Query(default=None),
                  patient: str | None = Query(default=None),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability("ur.read"))):
    stmt = select(URCase)
    if status:
        stmt = stmt.where(URCase.status == status)
    if patient:
        stmt = stmt.where(URCase.patient_ref == fhir._normalize_patient_ref(patient))
    rows = session.scalars(stmt.order_by(URCase.created_at.asc(), URCase.id.asc())).all()
    return {"items": [_ur_out(r) for r in rows], "total": len(rows)}


@router.get("/ur/denial-risk")
def ur_denial_risk(session: Session = Depends(get_db),
                   user: User = Depends(require_capability("ur.read"))):
    """The join feed for the revenue denials workqueue: every UR case
    currently in denial-risk, carrying the claim / encounter /
    patient refs app.revenue's workqueue (GET /billing/denials)
    joins on. This is a feed, not a fork of revenue.py."""
    rows = session.scalars(
        select(URCase).where(URCase.status == "denial-risk")
        .order_by(URCase.created_at.asc(), URCase.id.asc())).all()
    items = [_ur_out(r) for r in rows]
    return {"items": items, "total": len(items),
            "join_keys": ["claim_id", "claim_ref", "encounter_ref", "patient_ref"],
            "joins_with": "GET /billing/denials (app.revenue denials workqueue)",
            "note": ("Join feed only — denial workqueue state (assignee, "
                     "notes, outcomes) lives in app.revenue and is not "
                     "duplicated here.")}


@router.get("/ur/cases/{case_id}")
def get_ur_case(case_id: str, session: Session = Depends(get_db),
                user: User = Depends(require_capability("ur.read"))):
    return _ur_out(_ur_row(session, case_id))


@router.post("/ur/cases/{case_id}/review")
async def review_ur_case(case_id: str, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("ur.work"))):
    payload = await _json_body(request)
    target = payload.get("to_status") or payload.get("decision") or payload.get("status")
    if not target:
        raise HTTPException(status_code=422, detail="Review requires 'to_status'")
    if target not in UR_STATES:
        raise HTTPException(status_code=422, detail=f"Unknown UR status: {target}")
    row = _ur_row(session, case_id)
    if target not in UR_TRANSITIONS.get(row.status, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Illegal UR transition '{row.status}' -> '{target}'")
    criteria_code = payload.get("criteria_code") or payload.get("criteria_set") \
        or row.criteria_code
    evidence = payload.get("evidence_checked")
    if evidence is None:
        evidence = payload.get("evidence_fields")
    checked: list[str] = list(row.evidence_checked or [])
    if target in ("approved", "denial-risk", "escalated"):
        if not criteria_code:
            raise HTTPException(
                status_code=422,
                detail="A deciding review requires 'criteria_code' (which "
                       "governed criteria set was applied)")
        concept = _criteria_concept(session, str(criteria_code))
        checked = _validate_evidence(concept, evidence) if evidence is not None \
            else checked
        row.criteria_code = str(criteria_code)
        row.evidence_checked = checked
    elif criteria_code:
        concept = _criteria_concept(session, str(criteria_code))
        if evidence is not None:
            checked = _validate_evidence(concept, evidence)
            row.evidence_checked = checked
        row.criteria_code = str(criteria_code)
    previous = row.status
    row.status = target
    if target in ("approved", "denial-risk", "escalated") \
            and row.first_reviewer is None:
        row.first_reviewer = user.username
    history = list(row.history or [])
    history.append({"from": previous, "to": target, "by": user.username,
                    "at": _now_iso(), "criteria_code": row.criteria_code,
                    "evidence_checked": list(row.evidence_checked or []),
                    "advisor": False})
    row.history = history
    if isinstance(payload.get("note"), str):
        row.note = payload["note"]
    session.flush()
    append_audit(session, actor=user.username, action="ur.case.review",
                 entity=f"URCase/{case_id}",
                 payload={"from_status": previous, "to_status": target,
                          "criteria_code": row.criteria_code,
                          "evidence_checked": list(row.evidence_checked or []),
                          "patient": row.patient_ref,
                          "encounter_ref": row.encounter_ref,
                          "claim_ref": row.claim_ref})
    session.commit()
    return _ur_out(row)


@router.post("/ur/cases/{case_id}/advisor-review")
async def advisor_review_ur_case(case_id: str, request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_capability("ur.work"))):
    """Physician-advisor second review — REQUIRED for denial-risk
    cases, and the advisor must differ from the first reviewer
    (409 otherwise; the break-glass opener != reviewer pattern)."""
    payload = await _json_body(request)
    decision = payload.get("decision") or payload.get("to_status")
    if decision not in ADVISOR_DECISIONS:
        raise HTTPException(
            status_code=422,
            detail=f"Advisor 'decision' must be one of {list(ADVISOR_DECISIONS)}")
    row = _ur_row(session, case_id)
    if row.status != "denial-risk":
        raise HTTPException(
            status_code=409,
            detail=f"UR case {case_id} is in status '{row.status}'; advisor "
                   "review is required (and only available) for denial-risk cases")
    if row.first_reviewer is not None and user.username == row.first_reviewer:
        raise HTTPException(
            status_code=409,
            detail="The physician advisor must differ from the first reviewer "
                   f"('{row.first_reviewer}') — a denial-risk case cannot be "
                   "advisor-reviewed by the person who marked it")
    criteria_code = payload.get("criteria_code") or row.criteria_code
    evidence = payload.get("evidence_checked")
    if evidence is None:
        evidence = payload.get("evidence_fields")
    if criteria_code:
        concept = _criteria_concept(session, str(criteria_code))
        if evidence is not None:
            row.evidence_checked = _validate_evidence(concept, evidence)
        row.criteria_code = str(criteria_code)
    previous = row.status
    row.advisor_reviewer = user.username
    row.advisor_decision = decision
    row.status = decision
    history = list(row.history or [])
    history.append({"from": previous, "to": decision, "by": user.username,
                    "at": _now_iso(), "criteria_code": row.criteria_code,
                    "evidence_checked": list(row.evidence_checked or []),
                    "advisor": True})
    row.history = history
    session.flush()
    append_audit(session, actor=user.username, action="ur.case.advisor_review",
                 entity=f"URCase/{case_id}",
                 payload={"from_status": previous, "to_status": decision,
                          "first_reviewer": row.first_reviewer,
                          "advisor_reviewer": user.username,
                          "criteria_code": row.criteria_code,
                          "patient": row.patient_ref,
                          "claim_ref": row.claim_ref})
    session.commit()
    return _ur_out(row)


# ---------------------------------------------------------------------------
# BD-22 — Online bill pay (portal, test-mode provider, revenue ledger)
# ---------------------------------------------------------------------------

def _payment_provider_config(session: Session) -> dict:
    cfg = published_config(session, "integration", PAYMENT_PROVIDER_KEY)
    return (cfg.payload or {}) if cfg is not None else {}


class TestModePaymentProvider:
    """The governed test-mode provider: validates and records a
    payment through revenue's ledger machinery. It is NOT a live
    processor and never claims to be one."""
    mode = "test"
    provider = "test-mode"

    def process(self, session: Session, user: User, patient_ref: str,
                claim_id: str, amount, method: str) -> dict:
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) \
                or float(amount) <= 0:
            raise HTTPException(status_code=422,
                                detail="'amount' must be a positive number")
        amount = revenue._money(amount)
        methods = revenue._concept_map(session, revenue.PAYMENT_METHODS_KEY)
        if not isinstance(method, str) or method not in methods \
                or methods[method].get("active") is False:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown payment method '{method}': not in the "
                       "published payment-methods terminology")
        # Patient-scoped claim read (another patient's claim is a 404).
        claim = fhir.get_resource("Claim", claim_id, session, user)
        row = fhir._get_row("Claim", claim_id, session)
        financials = revenue.claim_financials(session, claim_id, row.body or {})
        if amount > financials["patient_due"]:
            raise HTTPException(
                status_code=422,
                detail=f"Payment {amount:.2f} exceeds the patient's "
                       f"outstanding responsibility on claim {claim_id} "
                       f"({financials['patient_due']:.2f})")
        payment = PatientPayment(
            payment_id=str(uuid.uuid4()), claim_id=claim_id,
            patient_ref=(claim.get("patient") or {}).get("reference") or patient_ref,
            amount=amount, method=method, recorded_by=user.username)
        session.add(payment)
        session.flush()
        return {"payment_id": payment.payment_id, "claim_id": claim_id,
                "patient": payment.patient_ref, "amount": amount,
                "method": method,
                "patient_due_after": revenue._money(
                    financials["patient_due"] - amount)}


def _own_statement(session: Session, user: User) -> dict:
    return revenue.patient_statement(user.patient_ref, session=session, user=user)


@router.get("/portal/billing/balance")
def portal_billing_balance(patient: str | None = Query(default=None),
                           session: Session = Depends(get_db),
                           user: User = Depends(require_roles(Role.patient))):
    if not user.patient_ref:
        raise HTTPException(status_code=403,
                            detail="This patient account is not linked to a patient record")
    if patient is not None \
            and fhir._normalize_patient_ref(patient) != user.patient_ref:
        # Another patient's balance is invisible to this caller.
        raise HTTPException(status_code=404, detail="Statement not found")
    statement = _own_statement(session, user)
    totals = statement["totals"]
    return {"patient": user.patient_ref, "balance": totals["patient_due"],
            "patient_due": totals["patient_due"],
            "patient_paid": totals["patient_paid"],
            "patient_responsibility": totals["patient_responsibility"],
            "totals": totals, "claims": statement["claims"],
            "provider_mode": _payment_provider_config(session).get("mode", "test"),
            "disclaimer": PAYMENT_PROVIDER_DISCLAIMER,
            "revenue_disclaimer": statement.get("disclaimer")}


@router.post("/portal/billing/payments", status_code=201)
async def portal_billing_payment(request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_roles(Role.patient))):
    if not user.patient_ref:
        raise HTTPException(status_code=403,
                            detail="This patient account is not linked to a patient record")
    config = _payment_provider_config(session)
    if config.get("mode") != "test" or config.get("provider") != "test-mode":
        raise HTTPException(
            status_code=409,
            detail="The published payment-provider config is not test-mode; "
                   "no live payment processor is integrated in this system, "
                   "so the payment cannot be taken")
    payload = await _json_body(request)
    amount = payload.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) \
            or float(amount) <= 0:
        raise HTTPException(status_code=422,
                            detail="'amount' must be a positive number")
    statement = _own_statement(session, user)
    claim_id = payload.get("claim_id") or payload.get("claimId")
    if not claim_id:
        candidates = [c for c in statement["claims"]
                      if c.get("patient_due", 0) > 0]
        if not candidates:
            raise HTTPException(
                status_code=422,
                detail="No claim with an outstanding patient balance to pay")
        claim_id = candidates[0]["claim_id"]
    else:
        claim_id = str(claim_id)
    method = payload.get("method") or "card"
    provider = TestModePaymentProvider()
    receipt_core = provider.process(session, user, user.patient_ref,
                                    claim_id, amount, method)
    session.flush()
    append_audit(session, actor=user.username, action="portal.billing.payment",
                 entity=f"Claim/{claim_id}",
                 payload={"payment_id": receipt_core["payment_id"],
                          "amount": receipt_core["amount"],
                          "method": method, "provider": provider.provider,
                          "provider_mode": provider.mode,
                          "patient_due_after": receipt_core["patient_due_after"]})
    session.commit()
    balance_after = _own_statement(session, user)["totals"]["patient_due"]
    return {"receipt_id": receipt_core["payment_id"],
            "payment_id": receipt_core["payment_id"],
            "claim_id": claim_id, "patient": user.patient_ref,
            "amount": receipt_core["amount"], "method": method,
            "provider": provider.provider, "provider_mode": "test",
            "mode": "test", "live": False,
            "patient_due_after": receipt_core["patient_due_after"],
            "balance_after": balance_after,
            "recorded_by": user.username,
            "disclaimer": PAYMENT_PROVIDER_DISCLAIMER}


# ---------------------------------------------------------------------------
# BD-23 — Portal proxy access
# ---------------------------------------------------------------------------

def _grant_out(row: ProxyGrant) -> dict:
    expires = _aware(row.expires_at)
    expired = bool(expires and _now() > expires)
    return {"id": row.grant_id, "grant_id": row.grant_id,
            "patient": row.patient_ref, "patient_ref": row.patient_ref,
            "proxy_username": row.proxy_username,
            "scopes": row.scopes or [], "granted_by": row.granted_by,
            "grant_source": row.grant_source, "status": row.status,
            "expired": expired,
            "effective_status": ("expired" if expired and row.status == "active"
                                 else row.status),
            "expires_at": expires.isoformat() if expires else None,
            "created_at": row.created_at.isoformat() if row.created_at else None}


def _grant_row(session: Session, grant_id: str) -> ProxyGrant:
    row = session.scalars(
        select(ProxyGrant).where(ProxyGrant.grant_id == grant_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Proxy grant {grant_id} not found")
    return row


def _account_exists(session: Session, username: str) -> bool:
    if session.scalars(select(User).where(User.username == username)).first():
        return True
    return session.scalars(select(UserAccount).where(
        UserAccount.username == username)).first() is not None


def _validate_scopes(session: Session, scopes) -> list[str]:
    if not isinstance(scopes, list) or not scopes or any(
            not isinstance(s, str) or not s for s in scopes):
        raise HTTPException(status_code=422,
                            detail="'scopes' must be a non-empty list of scope codes")
    allowed = {c.get("code") for c in _concepts(session, "terminology", PROXY_SCOPES_KEY)
               if c.get("active") is not False}
    unknown = [s for s in scopes if s not in allowed]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown proxy scope(s) {unknown}: the published "
                   f"terminology/proxy-scopes allows {sorted(allowed)}")
    return list(dict.fromkeys(scopes))


def _create_grant(session: Session, user: User, patient_ref: str,
                  payload: dict, source: str) -> ProxyGrant:
    proxy_username = payload.get("proxy_username") or payload.get("proxy")
    if not isinstance(proxy_username, str) or not proxy_username.strip():
        raise HTTPException(status_code=422, detail="Grant requires 'proxy_username'")
    proxy_username = proxy_username.strip()
    if not _account_exists(session, proxy_username):
        raise HTTPException(
            status_code=422,
            detail=f"Proxy '{proxy_username}' is not an existing account")
    scopes = _validate_scopes(session, payload.get("scopes"))
    expires_at = None
    if payload.get("expires_at"):
        expires_at = _parse_instant(payload["expires_at"], "expires_at")
    elif payload.get("expiry"):
        expires_at = _parse_instant(payload["expiry"], "expiry")
    row = ProxyGrant(grant_id=str(uuid.uuid4()), patient_ref=patient_ref,
                     proxy_username=proxy_username, scopes=scopes,
                     granted_by=user.username, grant_source=source,
                     status="active", expires_at=expires_at)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="access.proxy.grant",
                 entity=patient_ref,
                 payload={"grant_id": row.grant_id, "on_behalf_of": patient_ref,
                          "proxy_username": proxy_username, "scopes": scopes,
                          "grant_source": source,
                          "expires_at": expires_at.isoformat() if expires_at else None})
    session.commit()
    return row


@router.post("/portal/proxy/grants", status_code=201)
async def create_proxy_grant_patient(request: Request,
                                     session: Session = Depends(get_db),
                                     user: User = Depends(require_roles(Role.patient))):
    """The patient themself grants proxy access (their portal token;
    the patient is always the caller's own record)."""
    if not user.patient_ref:
        raise HTTPException(status_code=403,
                            detail="This patient account is not linked to a patient record")
    payload = await _json_body(request)
    named = payload.get("patient") or payload.get("patient_ref")
    if named and fhir._normalize_patient_ref(str(named)) != user.patient_ref:
        raise HTTPException(status_code=403,
                            detail="A patient may only grant proxy access to their own record")
    return _grant_out(_create_grant(session, user, user.patient_ref,
                                     payload, "patient"))


@router.post("/proxy/grants", status_code=201)
async def create_proxy_grant_admin(request: Request,
                                   session: Session = Depends(get_db),
                                   user: User = Depends(get_current_user)):
    """Admin-assisted grant: staff (admin / configurator) grant a
    proxy access to a named patient's record on their behalf."""
    if user.role not in (Role.admin, Role.configurator):
        raise HTTPException(
            status_code=403,
            detail="Admin-assisted proxy grants require an admin or configurator account")
    payload = await _json_body(request)
    patient_ref = _patient_ref_from(session, payload)
    return _grant_out(_create_grant(session, user, patient_ref,
                                     payload, "admin-assisted"))


def _revoke_grant(session: Session, user: User, grant_id: str,
                  *, admin_route: bool) -> dict:
    row = _grant_row(session, grant_id)
    if admin_route:
        if user.role not in (Role.admin, Role.configurator):
            raise HTTPException(status_code=403,
                                detail="Revoking via this route requires admin/configurator")
    else:
        if user.role == Role.patient and row.patient_ref != user.patient_ref:
            raise HTTPException(status_code=404,
                                detail=f"Proxy grant {grant_id} not found")
        if user.role not in (Role.patient, Role.admin, Role.configurator) \
                and user.username != row.proxy_username:
            raise HTTPException(status_code=403,
                                detail="Only the patient, an admin, or the proxy may revoke this grant")
    if row.status == "revoked":
        raise HTTPException(status_code=409,
                            detail=f"Proxy grant {grant_id} is already revoked")
    row.status = "revoked"
    session.flush()
    append_audit(session, actor=user.username, action="access.proxy.revoke",
                 entity=row.patient_ref,
                 payload={"grant_id": grant_id, "on_behalf_of": row.patient_ref,
                          "proxy_username": row.proxy_username})
    session.commit()
    return _grant_out(row)


@router.post("/portal/proxy/grants/{grant_id}/revoke")
async def revoke_proxy_grant_patient(grant_id: str,
                                     session: Session = Depends(get_db),
                                     user: User = Depends(get_current_user)):
    return _revoke_grant(session, user, grant_id, admin_route=False)


@router.post("/proxy/grants/{grant_id}/revoke")
async def revoke_proxy_grant_admin(grant_id: str,
                                   session: Session = Depends(get_db),
                                   user: User = Depends(get_current_user)):
    return _revoke_grant(session, user, grant_id, admin_route=True)


@router.get("/portal/proxy/grants")
def list_my_grants(session: Session = Depends(get_db),
                   user: User = Depends(require_roles(Role.patient))):
    """Grants the calling patient has given (their own record only)."""
    rows = session.scalars(
        select(ProxyGrant).where(ProxyGrant.patient_ref == user.patient_ref)
        .order_by(ProxyGrant.created_at.asc(), ProxyGrant.id.asc())).all()
    return {"items": [_grant_out(r) for r in rows], "total": len(rows)}


def _active_grant_for(session: Session, user: User, patient_ref: str,
                      scope: str) -> ProxyGrant:
    """The caller's effective grant for (patient, scope) — every
    failure mode is a 403: no grant, revoked grant, expired grant,
    or a grant that does not carry the scope."""
    fhir._get_row("Patient", patient_ref.split("/", 1)[1], session)  # 404
    rows = session.scalars(
        select(ProxyGrant).where(ProxyGrant.patient_ref == patient_ref,
                                 ProxyGrant.proxy_username == user.username)
        .order_by(ProxyGrant.created_at.desc(), ProxyGrant.id.desc())).all()
    if not rows:
        raise HTTPException(
            status_code=403,
            detail=f"No proxy grant allows '{user.username}' to act for {patient_ref}")
    row = rows[0]
    if row.status == "revoked":
        raise HTTPException(status_code=403,
                            detail="This proxy grant has been revoked")
    expires = _aware(row.expires_at)
    if expires and _now() > expires:
        raise HTTPException(status_code=403,
                            detail="This proxy grant has expired")
    if scope not in (row.scopes or []):
        raise HTTPException(
            status_code=403,
            detail=f"Proxy grant does not carry the '{scope}' scope "
                   f"(granted scopes: {row.scopes or []})")
    return row


def _audit_proxy_read(session: Session, user: User, grant: ProxyGrant,
                      scope: str, resource: str) -> None:
    append_audit(session, actor=user.username, action="access.proxy.read",
                 entity=grant.patient_ref,
                 payload={"on_behalf_of": grant.patient_ref,
                          "proxy": user.username,
                          "grant_id": grant.grant_id, "scope": scope,
                          "resource": resource})
    session.commit()


@router.get("/portal/proxy/patients")
def proxy_patients(session: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Patients for whom the caller is a proxy (active grants)."""
    rows = session.scalars(
        select(ProxyGrant).where(ProxyGrant.proxy_username == user.username)
        .order_by(ProxyGrant.created_at.asc(), ProxyGrant.id.asc())).all()
    items = []
    for row in rows:
        out = _grant_out(row)
        if out["effective_status"] != "active":
            continue
        items.append(out)
    return {"items": items, "total": len(items), "proxy": user.username}


@router.get("/portal/proxy/patients/{patient}/appointments")
def proxy_appointments(patient: str, session: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    patient_ref = fhir._normalize_patient_ref(patient)
    grant = _active_grant_for(session, user, patient_ref, "schedule")
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Appointment",
                                   FhirResource.patient_ref == patient_ref)
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    items = [fhir._render(r) for r in rows]
    _audit_proxy_read(session, user, grant, "schedule", "appointments")
    return {"items": items, "total": len(items), "patient": patient_ref,
            "on_behalf_of": patient_ref, "proxy": user.username}


@router.get("/portal/proxy/patients/{patient}/messages")
def proxy_messages(patient: str, session: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    patient_ref = fhir._normalize_patient_ref(patient)
    grant = _active_grant_for(session, user, patient_ref, "messages")
    # Reuse engagement's thread reads by patient (thread summaries).
    items = engagement._summaries(session, user, patient_ref)
    _audit_proxy_read(session, user, grant, "messages", "message-threads")
    return {"items": items, "total": len(items), "patient": patient_ref,
            "on_behalf_of": patient_ref, "proxy": user.username}


@router.get("/portal/proxy/patients/{patient}/results")
def proxy_results(patient: str, session: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    patient_ref = fhir._normalize_patient_ref(patient)
    grant = _active_grant_for(session, user, patient_ref, "results")
    # The FHIR inbox read by patient (released results only).
    bundle = fhir.results_bundle(session, patient_ref)
    _audit_proxy_read(session, user, grant, "results", "results-inbox")
    return {**bundle, "patient": patient_ref,
            "on_behalf_of": patient_ref, "proxy": user.username}


# ---------------------------------------------------------------------------
# BD-28 — Telehealth at scale
# ---------------------------------------------------------------------------

def _telehealth_services_config(session: Session) -> dict:
    cfg = published_config(session, "integration", TELEHEALTH_SERVICES_KEY)
    return (cfg.payload or {}) if cfg is not None else {}


def _service_entry(session: Session, service_line: str | None) -> dict | None:
    if not service_line:
        return None
    for entry in _telehealth_services_config(session).get("services") or []:
        if isinstance(entry, dict) and (entry.get("code") == service_line
                                        or entry.get("serviceLine") == service_line):
            return entry
    return None


def resolve_telehealth_config(session: Session, service_line: str | None) -> dict:
    """Layered telehealth resolution: the per-service entry (published
    integration/telehealth-services) wins; anything it does not name
    falls back to Phase 9's single template (app.engagement
    integration/telehealth)."""
    base = engagement._telehealth_config(session)
    provider = base.get("provider") or {}
    base_template = provider.get("joinUrlTemplate")
    base_intake = base.get("intakeForm") or engagement.TELEHEALTH_INTAKE_FORM
    entry = _service_entry(session, service_line)
    template = (entry or {}).get("joinUrlTemplate") or base_template
    intake = (entry or {}).get("intakeFormKey") or (entry or {}).get("intakeForm") \
        or base_intake
    return {
        "service_line": service_line,
        "source": "service-override" if entry and entry.get("joinUrlTemplate")
                  else ("service-entry" if entry else "phase9-template"),
        "provider_name": provider.get("name"),
        "join_url_template": template,
        "waiting_room_message": (entry or {}).get("waitingRoomMessage"),
        "intake_form": intake,
        "intake_form_key": intake,
        "disclaimer": base.get("disclaimer"),
        "service_entry": entry,
    }


def _render_join_url(template, appointment_id: str):
    if isinstance(template, str) and template:
        return template.replace("{appointmentId}", appointment_id)
    return None


def _visit_out(row: TelehealthVisit) -> dict:
    return {"id": row.visit_id, "visit_id": row.visit_id,
            "appointment_id": row.appointment_id,
            "appointment_ref": row.appointment_ref,
            "patient": row.patient_ref, "patient_ref": row.patient_ref,
            "service_line": row.service_line, "status": row.status,
            "state": row.status,
            "interpreter_request_id": row.interpreter_request_id,
            "interpreter_display": row.interpreter_display,
            "timestamps": row.timestamps or {},
            "state_history": row.state_history or [],
            "outcome_response_id": row.outcome_response_id,
            "outcome_submitted": row.outcome_response_id is not None,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def _visit_row(session: Session, visit_id: str) -> TelehealthVisit:
    row = session.scalars(
        select(TelehealthVisit).where(TelehealthVisit.visit_id == visit_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Telehealth visit {visit_id} not found")
    return row


def _visit_workspace(session: Session, row: TelehealthVisit) -> dict:
    resolved = resolve_telehealth_config(session, row.service_line)
    form_cfg = published_config(session, "form", resolved["intake_form"])
    return {"visit": _visit_out(row),
            "appointment_id": row.appointment_id,
            "patient": row.patient_ref,
            "service_line": row.service_line,
            "resolved_config": resolved,
            "provider_name": resolved["provider_name"],
            "join_url": _render_join_url(resolved["join_url_template"],
                                          row.appointment_id),
            "waiting_room_message": resolved["waiting_room_message"],
            "intake_form": resolved["intake_form"],
            "intake_form_definition": form_cfg.payload if form_cfg else None,
            "disclaimer": resolved["disclaimer"]}


@router.get("/telehealth/services")
def telehealth_services(session: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """The published per-service telehealth configuration, resolved
    against the Phase 9 base template per service line."""
    config = _telehealth_services_config(session)
    services = []
    for entry in config.get("services") or []:
        if isinstance(entry, dict):
            code = entry.get("code") or entry.get("serviceLine")
            services.append(resolve_telehealth_config(session, code))
    return {"services": services, "total": len(services),
            "base": resolve_telehealth_config(session, None)}


@router.post("/telehealth/visits", status_code=201)
async def create_telehealth_visit(request: Request,
                                  session: Session = Depends(get_db),
                                  user: User = Depends(require_capability("messages.use"))):
    payload = await _json_body(request)
    appointment_id = payload.get("appointment_id") or payload.get("appointmentId")
    if not appointment_id and payload.get("appointment_ref"):
        appointment_id = str(payload["appointment_ref"]).split("/", 1)[-1]
    if not appointment_id:
        raise HTTPException(status_code=422, detail="Visit requires 'appointment_id'")
    appointment_id = str(appointment_id)
    appt = fhir._get_row("Appointment", appointment_id, session)
    appt_body = appt.body or {}
    if engagement._appointment_type_code(appt_body) != "telehealth":
        raise HTTPException(
            status_code=422,
            detail=f"Appointment/{appointment_id} is not a telehealth appointment")
    service_line = payload.get("service_line") or payload.get("serviceLine") \
        or "general"
    patient_ref = engagement._appointment_patient_ref(appt_body) or appt.patient_ref
    now_iso = _now_iso()
    row = TelehealthVisit(
        visit_id=str(uuid.uuid4()), appointment_id=appointment_id,
        appointment_ref=f"Appointment/{appointment_id}",
        patient_ref=patient_ref, service_line=str(service_line),
        status="scheduled",
        interpreter_request_id=payload.get("interpreter_request_id")
        if isinstance(payload.get("interpreter_request_id"), str) else None,
        interpreter_display=payload.get("interpreter_display")
        if isinstance(payload.get("interpreter_display"), str) else None,
        timestamps={"scheduled": now_iso},
        state_history=[{"state": "scheduled", "at": now_iso, "by": user.username}],
        created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="telehealth.visit.create",
                 entity=f"TelehealthVisit/{row.visit_id}",
                 payload={"appointment_id": appointment_id,
                          "patient": patient_ref,
                          "service_line": row.service_line})
    session.commit()
    return _visit_out(row)


@router.get("/telehealth/visits")
def list_telehealth_visits(status: str | None = Query(default=None),
                           service_line: str | None = Query(default=None),
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("messages.use"))):
    stmt = select(TelehealthVisit)
    if status:
        stmt = stmt.where(TelehealthVisit.status == status)
    if service_line:
        stmt = stmt.where(TelehealthVisit.service_line == service_line)
    rows = session.scalars(stmt.order_by(TelehealthVisit.created_at.asc(),
                                         TelehealthVisit.id.asc())).all()
    return {"items": [_visit_out(r) for r in rows], "total": len(rows)}


@router.get("/telehealth/visits/{visit_id}")
def get_telehealth_visit(visit_id: str, session: Session = Depends(get_db),
                         user: User = Depends(require_capability("messages.use"))):
    return _visit_out(_visit_row(session, visit_id))


@router.get("/telehealth/visits/{visit_id}/workspace")
def telehealth_visit_workspace(visit_id: str, session: Session = Depends(get_db),
                               user: User = Depends(require_capability("messages.use"))):
    return _visit_workspace(session, _visit_row(session, visit_id))


@router.post("/telehealth/visits/{visit_id}/transition")
async def transition_telehealth_visit(visit_id: str, request: Request,
                                      session: Session = Depends(get_db),
                                      user: User = Depends(require_capability("messages.use"))):
    payload = await _json_body(request)
    target = payload.get("to_status") or payload.get("status") or payload.get("to_state")
    if not target:
        raise HTTPException(status_code=422, detail="Transition requires 'to_status'")
    if target not in VIRTUAL_VISIT_STATES:
        raise HTTPException(status_code=422,
                            detail=f"Unknown virtual-visit status: {target}")
    row = _visit_row(session, visit_id)
    if target not in VIRTUAL_VISIT_TRANSITIONS.get(row.status, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Illegal virtual-visit transition '{row.status}' -> '{target}'"
                   + (" (virtual-no-show is terminal and distinct from a "
                      "clinic no-show)" if row.status == "virtual-no-show" else ""))
    previous = row.status
    now_iso = _now_iso()
    row.status = target
    timestamps = dict(row.timestamps or {})
    timestamps[target.replace("-", "_")] = now_iso
    row.timestamps = timestamps
    history = list(row.state_history or [])
    history.append({"state": target, "from": previous, "at": now_iso,
                    "by": user.username})
    row.state_history = history
    if isinstance(payload.get("interpreter_request_id"), str):
        row.interpreter_request_id = payload["interpreter_request_id"]
    if isinstance(payload.get("interpreter_display"), str):
        row.interpreter_display = payload["interpreter_display"]
    session.flush()
    append_audit(session, actor=user.username, action="telehealth.visit.transition",
                 entity=f"TelehealthVisit/{visit_id}",
                 payload={"from_status": previous, "to_status": target,
                          "appointment_id": row.appointment_id,
                          "patient": row.patient_ref})
    session.commit()
    return _visit_out(row)


@router.post("/telehealth/visits/{visit_id}/interpreter-request")
async def set_interpreter_request(visit_id: str, request: Request,
                                  session: Session = Depends(get_db),
                                  user: User = Depends(require_capability("messages.use"))):
    """Attach an interpreter-on-video REFERENCE to a visit. The
    interpreter request itself is owned by the parallel interpreter
    workstream — this stores the reference + display only."""
    payload = await _json_body(request)
    request_id = payload.get("interpreter_request_id") or payload.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise HTTPException(status_code=422,
                            detail="Requires 'interpreter_request_id' (a reference to the interpreter module's request)")
    row = _visit_row(session, visit_id)
    row.interpreter_request_id = request_id.strip()
    if isinstance(payload.get("interpreter_display"), str):
        row.interpreter_display = payload["interpreter_display"]
    elif isinstance(payload.get("language"), str):
        row.interpreter_display = payload["language"]
    session.flush()
    append_audit(session, actor=user.username,
                 action="telehealth.visit.interpreter_request",
                 entity=f"TelehealthVisit/{visit_id}",
                 payload={"interpreter_request_id": row.interpreter_request_id,
                          "interpreter_display": row.interpreter_display})
    session.commit()
    return _visit_out(row)


@router.get("/portal/telehealth/visits")
def portal_telehealth_visits(session: Session = Depends(get_db),
                             user: User = Depends(require_roles(Role.patient))):
    """The signed-in patient's OWN virtual visits (Phase 15 closure:
    the portal outcome action needs a patient-scoped list — the by-id
    route below stays the detail/workspace read). Ownership is the
    visit row's own patient_ref, the same check the by-id and outcome
    routes enforce; other patients' visits are never listed."""
    rows = session.scalars(
        select(TelehealthVisit)
        .where(TelehealthVisit.patient_ref == user.patient_ref)
        .order_by(TelehealthVisit.created_at.desc())).all()
    return {"items": [_visit_out(r) for r in rows], "total": len(rows),
            "patient": user.patient_ref}


@router.get("/portal/telehealth/visits/{visit_id}")
def portal_telehealth_visit(visit_id: str, session: Session = Depends(get_db),
                            user: User = Depends(require_roles(Role.patient))):
    row = _visit_row(session, visit_id)
    if row.patient_ref != user.patient_ref:
        raise HTTPException(status_code=404,
                            detail=f"Telehealth visit {visit_id} not found")
    return _visit_workspace(session, row)


@router.post("/portal/telehealth/visits/{visit_id}/outcome", status_code=201)
async def portal_telehealth_outcome(visit_id: str, request: Request,
                                    session: Session = Depends(get_db),
                                    user: User = Depends(require_roles(Role.patient))):
    """Portal outcome capture: after a COMPLETED visit, the patient
    submits the governed outcome form (form/telehealth-outcome) for
    their OWN visit only."""
    row = _visit_row(session, visit_id)
    if row.patient_ref != user.patient_ref:
        raise HTTPException(status_code=404,
                            detail=f"Telehealth visit {visit_id} not found")
    if row.status != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Telehealth visit {visit_id} is in status '{row.status}'; "
                   "an outcome can only be captured after a completed visit")
    if row.outcome_response_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Telehealth visit {visit_id} already has an outcome response")
    payload = await _json_body(request)
    answers = payload.get("answers")
    if not isinstance(answers, list):
        raise HTTPException(status_code=422,
                            detail="'answers' must be a list of QuestionnaireResponse items")
    body = {
        "resourceType": "QuestionnaireResponse",
        "id": str(uuid.uuid4()),
        "status": "completed",
        "questionnaire": TELEHEALTH_OUTCOME_FORM,
        "subject": {"reference": user.patient_ref},
        "authored": _now_iso(),
        "author": {"display": user.username},
        "item": answers,
        "extension": [{"url": engagement.APPOINTMENT_EXTENSION,
                       "valueReference": {
                           "reference": f"Appointment/{row.appointment_id}"}}],
    }
    fhir._validate_questionnaire_response(session, body)
    rendered = fhir.create_resource("QuestionnaireResponse", body, session, user)
    row.outcome_response_id = rendered["id"]
    timestamps = dict(row.timestamps or {})
    timestamps["outcome_submitted"] = _now_iso()
    row.timestamps = timestamps
    session.flush()
    append_audit(session, actor=user.username, action="telehealth.visit.outcome",
                 entity=f"TelehealthVisit/{visit_id}",
                 payload={"appointment_id": row.appointment_id,
                          "form": TELEHEALTH_OUTCOME_FORM,
                          "response_id": rendered["id"]})
    session.commit()
    return {"visit": _visit_out(row), "outcome": rendered,
            "form": TELEHEALTH_OUTCOME_FORM}


# ---------------------------------------------------------------------------
# BD-31 — Credential-gated scheduling checker
# ---------------------------------------------------------------------------

def evaluate_credential_check(session: Session, staff_username: str,
                              privilege: str, at: datetime | None = None) -> dict:
    """The credential checker service: evaluate one staff member's
    app.people credential records for one privilege at one instant
    (UTC; default now). A credential COVERS the privilege when its
    type equals the privilege code (credential types are the
    published workforce config's credentialTypes). Allowed only
    when a covering credential is present and unexpired at the
    instant — expiry follows app.people's convention (a credential
    is expired when the booking DATE is after its expires_on date;
    expiring on the day itself is still valid)."""
    instant = at or _now()
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    instant = instant.astimezone(timezone.utc)
    booking_date = instant.date()
    base = {"staff_username": staff_username, "privilege": privilege,
            "checked_at": instant.isoformat(),
            "booking_instant": instant.isoformat()}
    staff = session.scalars(select(StaffMember).where(
        StaffMember.username == staff_username)).first()
    if staff is None:
        return {**base, "allowed": False, "staff_id": None,
                "reason": "unknown-staff",
                "detail": f"No staff directory entry for username '{staff_username}'",
                "credential": None, "deciding_credential": None}
    credentials = session.scalars(
        select(StaffCredential).where(StaffCredential.staff_id == staff.staff_id)
        .order_by(StaffCredential.expires_on.asc())).all()
    covering = [c for c in credentials if c.type == privilege]
    if not covering:
        return {**base, "allowed": False, "staff_id": staff.staff_id,
                "reason": "absent",
                "detail": f"No credential covering privilege '{privilege}' "
                          f"for staff '{staff_username}'",
                "credential": None, "deciding_credential": None}

    def _cred_out(c: StaffCredential) -> dict:
        # Reuse app.people's expiry arithmetic for the days figure.
        return {"credential_id": c.credential_id, "type": c.type,
                "number": c.number, "issued_on": c.issued_on,
                "expires_on": c.expires_on, "staff_id": c.staff_id}

    valid = []
    expired = []
    for cred in covering:
        try:
            expiry = date.fromisoformat(cred.expires_on)
        except (TypeError, ValueError):
            expired.append(cred)
            continue
        (valid if booking_date <= expiry else expired).append(cred)
    if valid:
        deciding = sorted(valid, key=lambda c: c.expires_on)[-1]
        out = _cred_out(deciding)
        return {**base, "allowed": True, "staff_id": staff.staff_id,
                "reason": None, "credential": out, "deciding_credential": out,
                "detail": f"Credential {deciding.credential_id} "
                          f"({deciding.type}) is unexpired at the booking instant"}
    deciding = sorted(expired, key=lambda c: c.expires_on)[-1]
    out = _cred_out(deciding)
    return {**base, "allowed": False, "staff_id": staff.staff_id,
            "reason": "expired",
            "detail": f"Credential covering '{privilege}' expired on "
                      f"{deciding.expires_on} (before the booking instant)",
            "credential": out, "deciding_credential": out,
            "expired_credential": out}


# Public alias for the coordinator's booking-hook wiring.
check_staff_credential = evaluate_credential_check


@router.post("/scheduling/credential-check")
async def credential_check(request: Request, session: Session = Depends(get_db),
                           user: User = Depends(require_capability("workforce.read"))):
    payload = await _json_body(request)
    staff_username = payload.get("staff_username") or payload.get("staffUsername") \
        or payload.get("username")
    privilege = payload.get("privilege")
    if not isinstance(staff_username, str) or not staff_username.strip():
        raise HTTPException(status_code=422, detail="Check requires 'staff_username'")
    if not isinstance(privilege, str) or not privilege.strip():
        raise HTTPException(status_code=422, detail="Check requires 'privilege'")
    at = None
    raw_at = payload.get("at") or payload.get("booking_at") \
        or payload.get("booking_instant") or payload.get("instant")
    if raw_at:
        at = _parse_instant(raw_at, "at")
    result = evaluate_credential_check(session, staff_username.strip(),
                                       privilege.strip(), at)
    append_audit(session, actor=user.username,
                 action="scheduling.credential_check",
                 entity=f"staff:{result.get('staff_id') or staff_username}",
                 payload={"staff_username": staff_username,
                          "privilege": privilege,
                          "allowed": result["allowed"],
                          "reason": result["reason"],
                          "credential_id": (result.get("deciding_credential")
                                            or {}).get("credential_id")})
    session.commit()
    return result
