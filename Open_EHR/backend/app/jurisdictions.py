"""Jurisdiction packs — Phase 17, WS-COMP (BD-57/58/59).

Three governed regulatory PACKS, each seeded as a DRAFT Studio
config (kind=regulatory_pack) exactly like the Phase 10 HIPAA/ONC
packs and the Phase 16 GCP/GLP/Part 11 packs (the compliance
insert-if-absent pattern): a pack binds NOTHING while it is a draft;
a human reviews, approves and publishes it through Studio
governance before its parameters drive enforcement. LEGAL
ATTESTATION IS EXTERNAL for all three packs — this module records
that as a waiver candidate in its pack notes and never claims it.

- **BD-57 / CMP-37 — 42 CFR Part 2** (key `part2-42cfr`):
  single-consent model per the 2024 final rule (one consent covers
  treatment/payment/operations uses), SUD counselling notes
  segmented SEPARATELY (a general SUD consent never releases them),
  and the prohibition on using Part 2 records in legal proceedings
  enforced in the disclosure path below (refused even WITH a
  consent on file unless a court order is asserted). Consent
  directives are governed data: ConsentDirective rows whose shape
  the pack parameterises, enforced by POST /jurisdictions/disclose —
  the disclosure gate the export centre / record-release paths call.
- **BD-58 / CMP-38 — GDPR** (key `gdpr-eu`): Art. 9(2)(h) basis
  recording (lawful-basis directives), rights workflows — access
  fulfilled inside the configured window (workflow states + timers,
  never real waiting), rectification BY SUPPLEMENTARY STATEMENT
  (the original record is never mutated), erasure REFUSED WITH A
  DOCUMENTED REASON where retention law binds (the refusal is the
  correct outcome, recorded as such) — DPIA + records-of-processing
  artefacts GENERATED from the live module/config inventory, and
  the 72-hour breach clock that feeds app.breach_ops (the gdpr-eu
  pack's breachClockHours is the parameter breach_ops reads).
- **BD-59 / CMP-39 — PHIPA** (key `phipa-ontario`): lock-box
  consent directives as configuration — circle-of-care implied
  consent is the default; a patient's withhold/withdraw directive
  names providers/organisations that may NOT read the chart.
  Enforcement is on the chart-access path below: a withheld reader
  is refused 403; an OVERRIDE requires a reason, is allowed, and is
  logged + producible as a report (GET /jurisdictions/overrides).
  Snooping-inclusive breach notification rides the BD-55 register:
  a PHIPA incident is a breach_ops incident with jurisdiction
  CA-ON; BD-52 snooping DETECTION is a separate workstream — this
  module's interface to it is the register (detections land as
  incidents there), stated here, not built twice.

Studio config surfaces: regulatory_pack/part2-42cfr,
regulatory_pack/gdpr-eu, regulatory_pack/phipa-ontario (the packs
themselves), permissions/role-matrix (capabilities
jurisdictions.read / jurisdictions.work).
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import fhir
from .audit import append_audit
from .configstore import (CAPABILITY_DEFAULTS, list_config_versions,
                          published_config, published_configs, role_allowed)
from .db import Base
from .models import AuditLog, ConfigStatus, FhirResource, StudioConfig, User, utcnow
from .security import get_current_user, get_db

router = APIRouter(prefix="/jurisdictions", tags=["jurisdictions"])

PART2_KEY = "part2-42cfr"
GDPR_KEY = "gdpr-eu"
PHIPA_KEY = "phipa-ontario"

_CAP_FALLBACKS = {
    "jurisdictions.read": ("admin", "configurator", "clinician", "viewer"),
    "jurisdictions.work": ("admin", "configurator", "clinician"),
}

WAIVER_LINE = ("WAIVER CANDIDATE: legal attestation for this pack is "
               "EXTERNAL — a qualified lawyer/privacy officer must attest "
               "the pack's parameters before publication; the system "
               "records the pack as a DRAFT and never claims attestation.")


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


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

class ConsentDirective(Base):
    """One patient consent / lock-box / lawful-basis directive.
    regime: part2 | gdpr | phipa. kind: part2-consent |
    part2-counselling-consent | gdpr-lawful-basis | phipa-withhold |
    phipa-withdraw. segment: sud | sud-counselling | general."""
    __tablename__ = "jurisdiction_consent_directives"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    directive_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    regime: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    segment: Mapped[str] = mapped_column(String(40), nullable=False, default="general")
    provider_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    organisation: Mapped[str | None] = mapped_column(String(300), nullable=True)
    basis: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ConsentOverride(Base):
    """One lock-box override: a withheld reader proceeded with a
    stated reason. Append-only; producible as a report."""
    __tablename__ = "jurisdiction_consent_overrides"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    directive_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    provider_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    organisation: Mapped[str | None] = mapped_column(String(300), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RightsRequest(Base):
    """One GDPR data-subject rights request with its workflow state
    and timer (due_at from the configured window)."""
    __tablename__ = "jurisdiction_rights_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # access|rectification|erasure
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="received")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    supplementary_statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    refusal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    fulfilled_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Pack payloads (DRAFTS)
# ---------------------------------------------------------------------------

def default_part2_pack_payload() -> dict:
    return {
        "body": "42 CFR Part 2",
        "jurisdiction": "US",
        "parameters": {
            "consentModel": "single-consent-2024-final-rule",
            "counsellingNotesSegmented": True,
            "proceedingsUseProhibited": True,
            "sudConditionCodes": "F10,F11,F12,F13,F14,F15,F16,F18,F19",
        },
        "requirements": ["42 CFR 2.31", "42 CFR 2.32", "42 CFR 2.33",
                         "42 CFR 2.61-2.67 (proceedings)"],
        "notes": ("Substance-use-disorder confidentiality (2024 final "
                  "rule alignment with HIPAA): ONE consent covers TPO "
                  "uses/disclosures; SUD counselling notes are segmented "
                  "and need their own consent; Part 2 records may not be "
                  "used in legal proceedings against the patient without "
                  "consent or a Part 2 court order. Enforcement: "
                  "app.jurisdictions disclosure gate. DRAFT — "
                  + WAIVER_LINE),
    }


def default_gdpr_pack_payload() -> dict:
    return {
        "body": "GDPR",
        "jurisdiction": "EU",
        "parameters": {
            "lawfulBasisArticle": "Art. 9(2)(h)",
            "lawfulBasisLabel": "health or social care provision/management",
            "accessWindowDays": 30,
            "breachClockHours": 72,
            "individualNotificationDays": 30,
            "retentionBoundErasure": True,
            "erasureRefusalReason": ("Erasure refused: the record is subject "
                                     "to a legal retention obligation "
                                     "(medical-record retention law / HIPAA "
                                     "documentation retention) that overrides "
                                     "Art. 17 for the retention period."),
        },
        "requirements": ["GDPR Art. 9(2)(h)", "GDPR Art. 15", "GDPR Art. 16",
                         "GDPR Art. 17", "GDPR Art. 30", "GDPR Art. 33",
                         "GDPR Art. 35"],
        "notes": ("EU General Data Protection Regulation baseline for "
                  "health data. The 72-hour breachClockHours parameter is "
                  "read by app.breach_ops for EU incidents. Rights "
                  "workflows: app.jurisdictions. DRAFT — " + WAIVER_LINE),
    }


def default_phipa_pack_payload() -> dict:
    return {
        "body": "PHIPA",
        "jurisdiction": "CA-ON",
        "parameters": {
            "impliedConsentCircleOfCare": True,
            "lockBoxEnabled": True,
            "breachNotifyHours": 72,
            "breachNotifyStandard": "first reasonable opportunity",
        },
        "requirements": ["PHIPA s.18", "PHIPA s.29", "PHIPA s.55.8 (lock-box)",
                         "PHIPA s.12 (safeguards/breach)"],
        "notes": ("Ontario Personal Health Information Protection Act: "
                  "implied consent within the circle of care; the patient "
                  "may withhold/withdraw consent for a named provider or "
                  "organisation (the lock-box); overrides require a reason "
                  "and are logged + producible; breach (including "
                  "snooping) notification at the first reasonable "
                  "opportunity rides the BD-55 register (jurisdiction "
                  "CA-ON). DRAFT — " + WAIVER_LINE),
    }


_PACKS = {PART2_KEY: default_part2_pack_payload,
          GDPR_KEY: default_gdpr_pack_payload,
          PHIPA_KEY: default_phipa_pack_payload}


def _seed_pack_draft(session: Session, key: str, payload: dict) -> None:
    if list_config_versions(session, "regulatory_pack", key):
        return
    row = StudioConfig(kind="regulatory_pack", key=key, version=1,
                       status=ConfigStatus.draft, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:regulatory_pack/{key}@v1",
                 payload={"kind": "regulatory_pack", "key": key, "version": 1,
                          "seeded_default": True})


def ensure_jurisdiction_defaults(session: Session) -> None:
    for key, factory in _PACKS.items():
        _seed_pack_draft(session, key, factory())
    session.commit()


def _ensure(session: Session) -> None:
    if not list_config_versions(session, "regulatory_pack", PART2_KEY):
        ensure_jurisdiction_defaults(session)


def _pack_state(session: Session, key: str) -> dict:
    versions = list_config_versions(session, "regulatory_pack", key)
    published = published_config(session, "regulatory_pack", key)
    latest = versions[-1] if versions else None
    return {"key": key, "exists": latest is not None,
            "status": (published.status.value if published else
                       (latest.status.value if latest else None)),
            "published_version": published.version if published else None,
            "latest_version": latest.version if latest else None,
            "payload": (published.payload if published else
                        (latest.payload if latest else None)),
            "studio_surface": f"Studio > Config > regulatory_pack/{key}",
            "waiver_candidate": WAIVER_LINE}


def _params(session: Session, key: str) -> tuple[dict, str]:
    """(parameters, source): PUBLISHED pack parameters bind; a draft's
    parameters are returned labelled as draft defaults so workflows
    can run pre-publication without pretending the draft binds."""
    published = published_config(session, "regulatory_pack", key)
    if published is not None:
        params = (published.payload or {}).get("parameters") or {}
        return (params if isinstance(params, dict) else {},
                f"regulatory_pack/{key}@v{published.version} (published)")
    versions = list_config_versions(session, "regulatory_pack", key)
    if versions:
        params = (versions[-1].payload or {}).get("parameters") or {}
        return (params if isinstance(params, dict) else {},
                f"regulatory_pack/{key} DRAFT defaults (not yet binding)")
    return {}, "built-in defaults"


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


def _patient_ref(body: dict) -> str:
    raw = body.get("patient_ref") or body.get("patient") or body.get("patientRef")
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=422, detail="'patient_ref' is required")
    return fhir._normalize_patient_ref(raw.strip())


def _check_patient(session: Session, ref: str) -> str:
    fhir._get_row("Patient", ref.split("/", 1)[1], session)  # 404 if unknown
    return ref


def _directive_out(row: ConsentDirective) -> dict:
    return {"directive_id": row.directive_id, "patient_ref": row.patient_ref,
            "regime": row.regime, "kind": row.kind, "segment": row.segment,
            "provider_ref": row.provider_ref, "organisation": row.organisation,
            "basis": row.basis, "status": row.status, "created_by": row.created_by,
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None}


def _active_directives(session: Session, patient_ref: str,
                       regime: str | None = None) -> list[ConsentDirective]:
    stmt = select(ConsentDirective).where(
        ConsentDirective.patient_ref == patient_ref,
        ConsentDirective.status == "active")
    if regime:
        stmt = stmt.where(ConsentDirective.regime == regime)
    return list(session.scalars(stmt.order_by(ConsentDirective.id.asc())).all())


# ---------------------------------------------------------------------------
# Packs
# ---------------------------------------------------------------------------

@router.get("/packs")
def packs(session: Session = Depends(get_db),
          user: User = Depends(_require("jurisdictions.read"))):
    _ensure(session)
    return {"items": [_pack_state(session, k) for k in (PART2_KEY, GDPR_KEY, PHIPA_KEY)],
            "total": 3,
            "governance": ("Packs are Studio regulatory_pack configs seeded "
                           "as DRAFTS: draft -> publish by a human in Studio "
                           "before parameters bind. Legal attestation is "
                           "EXTERNAL (waiver candidate) for all three.")}


# ---------------------------------------------------------------------------
# Consent directives (all regimes)
# ---------------------------------------------------------------------------

_KINDS = {"part2": ("part2-consent", "part2-counselling-consent"),
          "gdpr": ("gdpr-lawful-basis",),
          "phipa": ("phipa-withhold", "phipa-withdraw")}


@router.post("/consents", status_code=201)
async def create_consent(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(_require("jurisdictions.work"))):
    _ensure(session)
    body = await _json_body(request)
    ref = _check_patient(session, _patient_ref(body))
    regime = str(body.get("regime") or "").lower()
    kind = str(body.get("kind") or "")
    if regime not in _KINDS or kind not in _KINDS[regime]:
        raise HTTPException(status_code=422,
                            detail=f"'regime'/'kind' must be one of "
                                   f"{ {k: list(v) for k, v in _KINDS.items()} }")
    segment = str(body.get("segment") or "general")
    if kind == "part2-counselling-consent":
        segment = "sud-counselling"
    elif kind == "part2-consent" and segment == "general":
        segment = "sud"
    import uuid as _uuid
    row = ConsentDirective(
        directive_id=f"cons-{_uuid.uuid4().hex[:12]}", patient_ref=ref,
        regime=regime, kind=kind, segment=segment,
        provider_ref=(str(body["provider_ref"]) if body.get("provider_ref") else None),
        organisation=(str(body["organisation"]) if body.get("organisation") else None),
        basis=(str(body["basis"]) if body.get("basis") else
               ("Art. 9(2)(h)" if kind == "gdpr-lawful-basis" else None)),
        status="active", created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="jurisdictions.consent.create",
                 entity=ref,
                 payload={"directive_id": row.directive_id, "regime": regime,
                          "kind": kind, "segment": segment, "patient": ref})
    session.commit()
    return _directive_out(row)


@router.get("/consents")
def list_consents(patient_ref: str = Query(...), session: Session = Depends(get_db),
                  user: User = Depends(_require("jurisdictions.read"))):
    _ensure(session)
    ref = fhir._normalize_patient_ref(patient_ref)
    rows = session.scalars(select(ConsentDirective).where(
        ConsentDirective.patient_ref == ref).order_by(ConsentDirective.id.asc())).all()
    return {"items": [_directive_out(r) for r in rows], "total": len(rows)}


@router.post("/consents/{directive_id}/revoke")
def revoke_consent(directive_id: str, session: Session = Depends(get_db),
                   user: User = Depends(_require("jurisdictions.work"))):
    _ensure(session)
    row = session.scalars(select(ConsentDirective).where(
        ConsentDirective.directive_id == directive_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Directive '{directive_id}' not found")
    row.status = "revoked"
    session.flush()
    append_audit(session, actor=user.username, action="jurisdictions.consent.revoke",
                 entity=row.patient_ref,
                 payload={"directive_id": directive_id, "patient": row.patient_ref})
    session.commit()
    return _directive_out(row)


# ---------------------------------------------------------------------------
# BD-57 — Part 2 disclosure gate (export centre / record release call this)
# ---------------------------------------------------------------------------

def _patient_has_sud(session: Session, patient_ref: str, pack_params: dict) -> bool:
    codes_raw = pack_params.get("sudConditionCodes") or ""
    codes = {c.strip() for c in str(codes_raw).split(",") if c.strip()}
    if not codes:
        return False
    for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Condition",
            FhirResource.patient_ref == patient_ref)).all():
        for coding in ((row.body or {}).get("code") or {}).get("coding") or []:
            if isinstance(coding, dict) and coding.get("code") in codes:
                return True
    return False


@router.post("/disclose")
async def disclose(request: Request, session: Session = Depends(get_db),
                   user: User = Depends(_require("jurisdictions.work"))):
    """The disclosure decision for SUD-segmented data. Returns
    {allowed, reasons} — allowed only when the Part 2 consents the
    segment requires are ACTIVE on file. Refusals: no consent for the
    SUD segment; counselling notes without their OWN consent (a
    general consent never suffices); ANY proceedings use without a
    court order (prohibited even with consent). The decision and its
    reasons are audited either way."""
    _ensure(session)
    body = await _json_body(request)
    ref = _check_patient(session, _patient_ref(body))
    params, source = _params(session, PART2_KEY)
    segment = str(body.get("segment") or "")
    if segment not in ("sud", "sud-counselling"):
        # Infer from the chart: a patient with a SUD condition on file
        # makes an unsegmented disclosure a SUD disclosure.
        segment = "sud" if _patient_has_sud(session, ref, params) else "general"
    reasons: list[str] = []
    allowed = True
    if segment in ("sud", "sud-counselling"):
        directives = _active_directives(session, ref, regime="part2")
        has_general = any(d.kind == "part2-consent" for d in directives)
        has_counselling = any(d.kind == "part2-counselling-consent" for d in directives)
        if not has_general:
            allowed = False
            reasons.append("No active 42 CFR Part 2 consent on file for this patient")
        if segment == "sud-counselling" and not has_counselling:
            allowed = False
            reasons.append("SUD counselling notes are segmented separately: they "
                           "require their own part2-counselling-consent, which is "
                           "not on file (a general Part 2 consent never suffices)")
        if body.get("for_proceedings") is True and body.get("court_order") is not True:
            allowed = False
            reasons.append("Use of Part 2 records in legal proceedings against the "
                           "patient is prohibited without the patient's consent for "
                           "that use or a Part 2 court order — neither is present")
    append_audit(session, actor=user.username,
                 action="jurisdictions.disclose.allow" if allowed else "jurisdictions.disclose.refuse",
                 entity=ref,
                 payload={"patient": ref, "segment": segment, "allowed": allowed,
                          "reasons": reasons, "pack_source": source,
                          "purpose": str(body.get("purpose") or "")})
    session.commit()
    return {"allowed": allowed, "patient_ref": ref, "segment": segment,
            "reasons": reasons, "pack_source": source}


# ---------------------------------------------------------------------------
# BD-59 — PHIPA lock-box chart access + overrides
# ---------------------------------------------------------------------------

@router.post("/chart-access")
async def chart_access(request: Request, session: Session = Depends(get_db),
                       user: User = Depends(_require("jurisdictions.work"))):
    """Circle-of-care implied consent is the default (allowed). A
    patient's active phipa-withhold/withdraw directive naming the
    reader's provider_ref or organisation BLOCKS the read (403)
    unless the caller supplies an override_reason: the override is
    allowed, logged as a ConsentOverride, audited, and producible
    via GET /jurisdictions/overrides."""
    _ensure(session)
    body = await _json_body(request)
    ref = _check_patient(session, _patient_ref(body))
    provider_ref = str(body.get("provider_ref") or "") or None
    organisation = str(body.get("organisation") or "") or None
    override_reason = body.get("override_reason")
    blockers = []
    for d in _active_directives(session, ref, regime="phipa"):
        if d.kind not in ("phipa-withhold", "phipa-withdraw"):
            continue
        if (d.provider_ref and provider_ref and d.provider_ref == provider_ref) or \
           (d.organisation and organisation and d.organisation == organisation) or \
           (not d.provider_ref and not d.organisation):
            blockers.append(d)
    if not blockers:
        append_audit(session, actor=user.username, action="jurisdictions.chart_access.allow",
                     entity=ref, payload={"patient": ref, "basis": "circle-of-care implied consent"})
        session.commit()
        return {"allowed": True, "patient_ref": ref, "basis": "circle-of-care implied consent",
                "overridden": False}
    if not (isinstance(override_reason, str) and override_reason.strip()):
        append_audit(session, actor=user.username, action="jurisdictions.chart_access.refuse",
                     entity=ref, payload={"patient": ref,
                                          "blocked_by": [b.directive_id for b in blockers]})
        session.commit()
        raise HTTPException(status_code=403,
                            detail="Lock-box: the patient has withheld consent for "
                                   "this provider/organisation. Access requires an "
                                   "override with a stated reason (it will be logged "
                                   "and is producible).")
    for blocker in blockers:
        session.add(ConsentOverride(
            patient_ref=ref, directive_id=blocker.directive_id, actor=user.username,
            provider_ref=provider_ref, organisation=organisation,
            reason=override_reason.strip()))
    session.flush()
    append_audit(session, actor=user.username, action="jurisdictions.chart_access.override",
                 entity=ref, payload={"patient": ref, "reason": override_reason.strip(),
                                      "blocked_by": [b.directive_id for b in blockers]})
    session.commit()
    return {"allowed": True, "patient_ref": ref, "basis": "lock-box override (logged)",
            "overridden": True, "override_reason": override_reason.strip()}


@router.get("/overrides")
def overrides(patient_ref: str | None = Query(default=None),
              session: Session = Depends(get_db),
              user: User = Depends(_require("jurisdictions.read"))):
    """The producible lock-box override report."""
    _ensure(session)
    stmt = select(ConsentOverride).order_by(ConsentOverride.id.asc())
    if patient_ref:
        stmt = stmt.where(ConsentOverride.patient_ref == fhir._normalize_patient_ref(patient_ref))
    rows = session.scalars(stmt).all()
    items = [{"id": r.id, "patient_ref": r.patient_ref, "directive_id": r.directive_id,
              "actor": r.actor, "provider_ref": r.provider_ref,
              "organisation": r.organisation, "reason": r.reason,
              "created_at": _aware(r.created_at).isoformat() if r.created_at else None}
             for r in rows]
    return {"items": items, "total": len(items),
            "note": "Every lock-box override, with its stated reason — the PHIPA-producible record."}


# ---------------------------------------------------------------------------
# BD-58 — GDPR rights workflows + DPIA / RoPA
# ---------------------------------------------------------------------------

def _rights_out(row: RightsRequest, now: datetime | None = None) -> dict:
    now = now or _now()
    return {"request_id": row.request_id, "patient_ref": row.patient_ref,
            "kind": row.kind, "status": row.status, "note": row.note,
            "supplementary_statement": row.supplementary_statement,
            "refusal_reason": row.refusal_reason,
            "due_at": _aware(row.due_at).isoformat() if row.due_at else None,
            "overdue": bool(row.status in ("received", "in-progress")
                            and row.due_at and now > _aware(row.due_at)),
            "created_by": row.created_by, "fulfilled_by": row.fulfilled_by,
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None}


@router.post("/rights-requests", status_code=201)
async def create_rights_request(request: Request, session: Session = Depends(get_db),
                                user: User = Depends(_require("jurisdictions.work"))):
    _ensure(session)
    body = await _json_body(request)
    ref = _check_patient(session, _patient_ref(body))
    kind = str(body.get("kind") or "")
    if kind not in ("access", "rectification", "erasure"):
        raise HTTPException(status_code=422, detail="'kind' must be access, rectification or erasure")
    params, source = _params(session, GDPR_KEY)
    window = params.get("accessWindowDays")
    window_days = window if isinstance(window, int) and not isinstance(window, bool) and window > 0 else 30
    # Record the Art. 9(2)(h) basis the processing rests on, if the
    # caller asserts one / the pack names it.
    import uuid as _uuid
    row = RightsRequest(
        request_id=f"gdpr-{_uuid.uuid4().hex[:12]}", patient_ref=ref, kind=kind,
        status="received", note=str(body.get("note") or ""),
        due_at=_now() + timedelta(days=window_days), created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="jurisdictions.rights.create",
                 entity=ref, payload={"request_id": row.request_id, "kind": kind,
                                      "patient": ref, "window_days": window_days,
                                      "window_source": source,
                                      "lawful_basis": params.get("lawfulBasisArticle", "Art. 9(2)(h)")})
    session.commit()
    return _rights_out(row)


@router.get("/rights-requests")
def list_rights_requests(patient_ref: str | None = Query(default=None),
                         session: Session = Depends(get_db),
                         user: User = Depends(_require("jurisdictions.read"))):
    _ensure(session)
    stmt = select(RightsRequest).order_by(RightsRequest.id.asc())
    if patient_ref:
        stmt = stmt.where(RightsRequest.patient_ref == fhir._normalize_patient_ref(patient_ref))
    rows = session.scalars(stmt).all()
    now = _now()
    return {"items": [_rights_out(r, now) for r in rows], "total": len(rows)}


@router.post("/rights-requests/{request_id}/fulfil")
async def fulfil_rights_request(request_id: str, request: Request,
                                session: Session = Depends(get_db),
                                user: User = Depends(_require("jurisdictions.work"))):
    """Disposition one rights request. Rules:
    - rectification is fulfilled ONLY by supplementary statement
      (Art. 16): the statement is stored; the underlying record is
      never mutated here.
    - erasure where retention law binds (the pack's
      retentionBoundErasure) may NOT be fulfilled — it must be
      REFUSED with the documented reason; attempting fulfilment is
      refused 409 naming that. A refusal requires a reason (the
      documented refusal IS the correct outcome)."""
    _ensure(session)
    row = session.scalars(select(RightsRequest).where(
        RightsRequest.request_id == request_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Rights request '{request_id}' not found")
    if row.status in ("fulfilled", "refused"):
        raise HTTPException(status_code=409, detail=f"Request {request_id} is already {row.status}")
    body = await _json_body(request)
    outcome = str(body.get("outcome") or "")
    if outcome not in ("fulfilled", "refused"):
        raise HTTPException(status_code=422, detail="'outcome' must be fulfilled or refused")
    params, source = _params(session, GDPR_KEY)
    if outcome == "fulfilled" and row.kind == "erasure" \
            and params.get("retentionBoundErasure") is True:
        raise HTTPException(
            status_code=409,
            detail="Erasure cannot be fulfilled for this record: retention law "
                   "binds (pack parameter retentionBoundErasure). Record a "
                   "REFUSAL with the documented reason instead — that refusal "
                   "is the correct GDPR outcome.")
    if outcome == "fulfilled" and row.kind == "rectification":
        statement = body.get("supplementary_statement") or body.get("supplementaryStatement")
        if not (isinstance(statement, str) and statement.strip()):
            raise HTTPException(
                status_code=422,
                detail="Rectification is fulfilled by supplementary statement "
                       "(Art. 16): supply 'supplementary_statement'. The "
                       "original record is not mutated.")
        row.supplementary_statement = statement.strip()
    if outcome == "refused":
        reason = body.get("reason") or body.get("refusal_reason") or \
            (params.get("erasureRefusalReason") if row.kind == "erasure" else None)
        if not (isinstance(reason, str) and reason.strip()):
            raise HTTPException(status_code=422,
                                detail="A refusal requires a documented 'reason'")
        row.refusal_reason = str(reason).strip()
    row.status = outcome
    row.fulfilled_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="jurisdictions.rights.fulfil",
                 entity=row.patient_ref,
                 payload={"request_id": request_id, "kind": row.kind, "outcome": outcome,
                          "refusal_reason": row.refusal_reason,
                          "supplementary_statement_present": bool(row.supplementary_statement)})
    session.commit()
    return _rights_out(row)


def _inventory(session: Session) -> dict:
    """The LIVE system inventory the DPIA / RoPA are generated from:
    published navigation modules, config kinds in use, FHIR resource
    types held, and the audit posture. Nothing here is hand-written
    prose about a hypothetical system."""
    nav = published_config(session, "navigation", "main")
    modules = []
    if nav is not None:
        for item in (nav.payload or {}).get("items") or []:
            if isinstance(item, dict) and item.get("module"):
                modules.append({"module": item["module"], "label": item.get("label"),
                                "group": item.get("group")})
    kinds = {}
    for cfg in session.scalars(select(StudioConfig).where(
            StudioConfig.status == ConfigStatus.published)).all():
        kinds[cfg.kind] = kinds.get(cfg.kind, 0) + 1
    resource_types: dict[str, int] = {}
    for row in session.scalars(select(FhirResource)).all():
        resource_types[row.resource_type] = resource_types.get(row.resource_type, 0) + 1
    audit_total = len(session.scalars(select(AuditLog)).all())
    return {"modules": modules, "published_config_kinds": kinds,
            "fhir_resource_types": resource_types, "audit_entries": audit_total}


@router.get("/dpia")
def dpia(session: Session = Depends(get_db),
         user: User = Depends(_require("jurisdictions.read"))):
    _ensure(session)
    params, source = _params(session, GDPR_KEY)
    inv = _inventory(session)
    append_audit(session, actor=user.username, action="jurisdictions.dpia.generate",
                 entity="gdpr:dpia", payload={"modules": len(inv["modules"]),
                                               "pack_source": source})
    session.commit()
    return {"artefact": "Data Protection Impact Assessment (GDPR Art. 35)",
            "generated_at": _now().isoformat(), "generated_from": "live system inventory",
            "lawful_basis": {"article": params.get("lawfulBasisArticle", "Art. 9(2)(h)"),
                             "label": params.get("lawfulBasisLabel",
                                                 "health or social care provision/management"),
                             "source": source},
            "processing_scope": inv,
            "necessity_and_risks": [
                "Processing is limited to care provision/management under the recorded Art. 9(2)(h) basis.",
                "Special-category data is access-controlled by role/capability and every access is hash-chain audited.",
                "Breach notification runs on the BD-55 register with the pack's 72-hour supervisory clock.",
            ],
            "note": "Generated artefact — a human DPO reviews and signs the DPIA; the system does not attest it."}


@router.get("/ropa")
def ropa(session: Session = Depends(get_db),
         user: User = Depends(_require("jurisdictions.read"))):
    _ensure(session)
    params, source = _params(session, GDPR_KEY)
    inv = _inventory(session)
    append_audit(session, actor=user.username, action="jurisdictions.ropa.generate",
                 entity="gdpr:ropa", payload={"modules": len(inv["modules"]),
                                              "pack_source": source})
    session.commit()
    return {"artefact": "Records of Processing Activities (GDPR Art. 30)",
            "generated_at": _now().isoformat(), "generated_from": "live system inventory",
            "controller_processing": [
                {"purpose": "Provision and management of health care",
                 "lawful_basis": params.get("lawfulBasisArticle", "Art. 9(2)(h)"),
                 "data_categories": sorted(inv["fhir_resource_types"].keys()),
                 "record_counts_by_type": inv["fhir_resource_types"],
                 "systems": [m["module"] for m in inv["modules"]]},
            ],
            "published_config_kinds": inv["published_config_kinds"],
            "note": "Generated from the live inventory at request time; counts are this deployment's own."}
