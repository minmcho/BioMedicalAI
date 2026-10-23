"""Wave C-AI — Clinical AI governance core (framework AI-G1/G2/G5/G6/
G7/G9/G10 backend).

The human-authored half of agent governance, beside the machine
evidence in app.agent_validation:

- **Validation statements** (AI-G2) — the controlled artefact of
  framework §4: one statement lineage per agent key, versioned,
  supersede-never-edit, stating intended use, claims, evidence,
  thresholds, limitations and prohibited uses for exactly one
  published agent-config version + one model route. Drafts may be
  incomplete (designer data); SUBMIT runs the full §4 completeness +
  evidence + threshold-discipline validation; APPROVE is four-eyes
  (approver != statement author AND approver != the covered agent
  config version's author — the shared service in app.four_eyes
  generalises this rule, this module enforces the same rule locally
  for statement approvals) with the tier's required approval kinds
  (AI-G1 tier matrix, framework §3.2).
- **Runtime gate** (AI-G4's shared verdict) — `runtime_gate()` is the
  function the run surfaces consult: kill switch, validation-record
  currency and statement currency, each refusal naming its real
  reason. It does NOT check enablement (callers do).
- **Kill switch** (AI-G5, framework §7.4) — one audited act flipping
  every AgentEnablement off; release clears only the state, never
  re-enables anything.
- **Safety linkage** (AI-G6/G9) — drift breach tracking with T3
  auto-suspension, zero-tolerance suspension, and per-run incident
  reports landing as SafetyEvents in the "ai-agent" category.
- **Disclosure** (AI-G7, framework §13.2) — the per-agent HTI-1
  source-attribute set generated from statement + published config +
  validation evidence; statement-sourced attributes are honestly
  marked "not recorded" when no approved statement exists, never
  invented.
- **Expiry worklist** (AI-G10) — statements + validation records by
  days-to-expiry, owner-attributed, overdue first.

Tier requirements and governance policy ride the published
`integration/agent-validation` config (key `tierRequirements`,
`requireStatementForEnable`, `driftBreach`); the framework §3.2
defaults below apply per-tier per-key wherever the published config
is silent, so the module works before and after a site publishes
overrides.

Capability gates mirror app.agent_validation: writes ride
`agents.validate` (or `studio.manage`), reads ride `agents.validate`
or `agents.view`; run reports ride `agents.run`.
"""
import calendar
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (Boolean, DateTime, Float, Integer, JSON, String,
                        Text, select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import agent_validation as av
from .agent_validation import (EvaluationDataset, EvaluationRun,
                               ValidationRecord, get_drift,
                               validation_status)
from .audit import append_audit
from .configstore import published_config, published_configs, role_allowed
from .db import Base
from .models import (AgentEnablement, AgentRun, SafetyEvent, StudioConfig,
                     User, utcnow)
from .security import get_current_user, get_db

router = APIRouter(prefix="/ai-governance", tags=["ai-governance"])

TIERS = ("T1", "T2", "T3")
APPROVAL_KINDS = ("clinical_reviewer", "governance_approver",
                  "clinical_owner")
PROHIBITED_SEED_CODES = (
    "no-autonomous-diagnosis",
    "no-unreviewed-order",
    "no-unreviewed-patient-communication",
    "no-excluded-population",
    "no-unlisted-route-tool-modality",
)
NOT_RECORDED = "not recorded — no approved validation statement"
_OWNER_ALIAS_WORDS = {"team", "group", "dept", "department", "service"}

# Framework §3.2 matrix — the local defaults the governed
# `tierRequirements` config key merges over, per tier per key.
DEFAULT_TIER_REQUIREMENTS: dict = {
    "T1": {"minOverallPass": 0.90, "minSliceFloor": 0.85,
           "maxSliceDisparity": 0.20, "statementValidityMonths": 12,
           "approvals": ["governance_approver"],
           "monitoringCadence": "monthly drift report"},
    "T2": {"minOverallPass": 0.95, "minSliceFloor": 0.90,
           "maxSliceDisparity": 0.10, "statementValidityMonths": 6,
           "approvals": ["clinical_reviewer", "governance_approver"],
           "monitoringCadence": "monthly drift + disposition review"},
    "T3": {"minOverallPass": 0.98, "minSliceFloor": 0.95,
           "maxSliceDisparity": 0.05, "statementValidityMonths": 6,
           "approvals": ["clinical_reviewer", "governance_approver",
                         "clinical_owner"],
           "monitoringCadence": ("continuous drift watch; monthly "
                                 "disposition + sampled-output review")},
}
DEFAULT_DRIFT_BREACH = {"consecutiveChecks": 2, "autoSuspendTiers": ["T3"]}


# ---------------------------------------------------------------------------
# This module's own tables
# ---------------------------------------------------------------------------

class AiValidationStatement(Base):
    """One VERSION of one agent's validation statement (framework §4).
    The lineage is (agent_key, statement_id); a new version is a new
    row, superseded versions are archived never edited. `fields_json`
    carries the §4.1 authored content (fields 5-21 + fundingSource);
    fields 1-4/22/23 are the columns, the approvals table and the
    generated disclosure respectively."""
    __tablename__ = "ai_validation_statements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    statement_id: Mapped[str] = mapped_column(String(200), nullable=False,
                                              index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    agent_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                           index=True)
    agent_config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_route: Mapped[str] = mapped_column(String(200), nullable=False,
                                             default="")
    risk_tier: Mapped[str] = mapped_column(String(4), nullable=False,
                                           default="T2")
    owner: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    fields_json: Mapped[dict] = mapped_column(JSON, nullable=False,
                                              default=dict)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="draft", index=True)
    author: Mapped[str] = mapped_column(String(120), nullable=False,
                                        default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    review_by: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    suspension_reason: Mapped[str] = mapped_column(Text, nullable=False,
                                                   default="")


class AiStatementApproval(Base):
    """One approval act on one statement version (framework §4 field
    22 / §5): who, in which kind, with what conditions, and — for a
    governance approver ratifying loosened thresholds — the re-stated
    loosening reason (§4.4)."""
    __tablename__ = "ai_statement_approvals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    statement_row_id: Mapped[int] = mapped_column(Integer, nullable=False,
                                                  index=True)
    approver: Mapped[str] = mapped_column(String(120), nullable=False,
                                          default="")
    approval_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    conditions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    loosening_reason: Mapped[str] = mapped_column(Text, nullable=False,
                                                  default="")
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class AiGovernanceState(Base):
    """The kill-switch singleton (id=1, framework §7.4 / AI-G5)."""
    __tablename__ = "ai_governance_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                     default=False)
    kill_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kill_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                         default="")
    kill_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    released_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class AiBreachTracker(Base):
    """Per-agent consecutive drift-breach counter (framework §9.4 /
    AI-G6): the breach rules count CONSECUTIVE flagged checks, so the
    count is state, not a recomputation."""
    __tablename__ = "ai_breach_trackers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_key: Mapped[str] = mapped_column(String(120), unique=True,
                                           nullable=False, index=True)
    consecutive_breaches: Mapped[int] = mapped_column(Integer, nullable=False,
                                                      default=0)
    last_status: Mapped[str] = mapped_column(String(60), nullable=False,
                                             default="")
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    last_flags_json: Mapped[list] = mapped_column(JSON, nullable=False,
                                                  default=list)


# ---------------------------------------------------------------------------
# Capability gates (mirror app.agent_validation)
# ---------------------------------------------------------------------------

def _require_validate(session: Session = Depends(get_db),
                      user: User = Depends(get_current_user)) -> User:
    if role_allowed(session, user.role, "agents.validate") \
            or role_allowed(session, user.role, "studio.manage"):
        return user
    raise HTTPException(
        status_code=403,
        detail=f"Role {user.role.value} lacks capability 'agents.validate'")


def _require_read(session: Session = Depends(get_db),
                  user: User = Depends(get_current_user)) -> User:
    if role_allowed(session, user.role, "agents.validate") \
            or role_allowed(session, user.role, "agents.view"):
        return user
    raise HTTPException(
        status_code=403,
        detail=f"Role {user.role.value} lacks capability 'agents.view'")


def _require_run(session: Session = Depends(get_db),
                 user: User = Depends(get_current_user)) -> User:
    if role_allowed(session, user.role, "agents.run"):
        return user
    raise HTTPException(
        status_code=403,
        detail=f"Role {user.role.value} lacks capability 'agents.run'")


# ---------------------------------------------------------------------------
# Small helpers (tree idioms)
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: datetime | None) -> str | None:
    aware = _aware(dt)
    return aware.isoformat() if aware else None


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


def _add_months(dt: datetime, months: int) -> datetime:
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _parse_date(raw) -> datetime | None:
    if isinstance(raw, datetime):
        return _aware(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        return _aware(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _owner_alias_problem(owner: str) -> str | None:
    """Field 4 names an INDIVIDUAL, never a team alias. Simple whole-
    word check over the obvious alias words — documented as simple;
    a name that trips it can be written in full (e.g. a surname)."""
    words = set(re.findall(r"[a-z]+", owner.lower()))
    hit = sorted(words & _OWNER_ALIAS_WORDS)
    if hit:
        return (f"Owner '{owner}' looks like a team alias ({', '.join(hit)}); "
                "field 4 requires a named individual as the statement owner")
    return None


def _published_agent(session: Session, agent_key: str) -> StudioConfig | None:
    return published_config(session, "agent", agent_key)


def _agent_version_row(session: Session, agent_key: str,
                       version: int) -> StudioConfig | None:
    return session.scalars(select(StudioConfig).where(
        StudioConfig.kind == "agent", StudioConfig.key == agent_key,
        StudioConfig.version == version)).first()


def _enablement(session: Session, agent_key: str) -> AgentEnablement | None:
    return session.scalars(select(AgentEnablement).where(
        AgentEnablement.agent_key == agent_key)).first()


def _is_enabled(session: Session, agent_key: str) -> bool:
    row = _enablement(session, agent_key)
    return bool(row and row.enabled)


# ---------------------------------------------------------------------------
# Governed configuration readers
# ---------------------------------------------------------------------------

def tier_requirements(session: Session) -> tuple[dict, str]:
    """The §3.2 tier matrix: local framework defaults merged with the
    published `integration/agent-validation` config's `tierRequirements`
    key, per tier per key. Returns (tiers, source)."""
    cfg = av._validation_config(session)
    merged: dict = {}
    for tier, req in DEFAULT_TIER_REQUIREMENTS.items():
        row = dict(req)
        row["approvals"] = list(req["approvals"])
        merged[tier] = row
    overrides = cfg.get("tierRequirements")
    if isinstance(overrides, dict):
        for tier, vals in overrides.items():
            if tier in merged and isinstance(vals, dict):
                merged[tier].update(vals)
    published = published_config(session, "integration", av.VALIDATION_KEY)
    return merged, ("published" if published is not None else "defaults")


def _tier_req(session: Session, tier: str) -> dict:
    tiers, _ = tier_requirements(session)
    return tiers.get(tier) or tiers["T2"]


def _require_statement(session: Session) -> bool:
    cfg = av._validation_config(session)
    value = cfg.get("requireStatementForEnable", True)
    return True if value is None else bool(value)


def _drift_breach_policy(session: Session) -> dict:
    cfg = av._validation_config(session)
    policy = dict(DEFAULT_DRIFT_BREACH)
    raw = cfg.get("driftBreach")
    if isinstance(raw, dict):
        policy.update(raw)
    try:
        policy["consecutiveChecks"] = int(policy.get("consecutiveChecks") or 2)
    except (TypeError, ValueError):
        policy["consecutiveChecks"] = 2
    if not isinstance(policy.get("autoSuspendTiers"), list):
        policy["autoSuspendTiers"] = ["T3"]
    return policy


def _ops_payload(session: Session) -> dict:
    cfg = published_config(session, "terminology", "continuum-ops")
    if cfg is not None and isinstance(cfg.payload, dict) and cfg.payload:
        return cfg.payload
    from .continuum import default_ops_payload
    return default_ops_payload()


def _harm_codes(session: Session) -> set:
    ops = _ops_payload(session)
    return {h.get("code") for h in ops.get("harmTaxonomy") or []
            if isinstance(h, dict) and h.get("code")}


def _valid_harm(session: Session, raw) -> str:
    return raw if isinstance(raw, str) and raw in _harm_codes(session) \
        else "none"


# ---------------------------------------------------------------------------
# Exported governance verdicts (CONTRACT — other modules import these)
# ---------------------------------------------------------------------------

def agent_tier(session: Session, agent_key: str) -> str:
    """The agent's risk tier: the published payload's `riskTier` when
    it positively classifies the agent (framework: an absent tier is
    NOT T1 — T1 must be positively classified), else the current
    approved/pending statement's tier, else T2."""
    cfg = _published_agent(session, agent_key)
    if cfg is not None:
        tier = (cfg.payload or {}).get("riskTier")
        if tier in TIERS:
            return tier
    rows = session.scalars(
        select(AiValidationStatement)
        .where(AiValidationStatement.agent_key == agent_key)
        .order_by(AiValidationStatement.version.desc(),
                  AiValidationStatement.id.desc())).all()
    for row in rows:
        if row.status in ("approved", "pending_approval") \
                and row.risk_tier in TIERS:
            return row.risk_tier
    return "T2"


def _statements_for(session: Session, agent_key: str) -> list:
    return session.scalars(
        select(AiValidationStatement)
        .where(AiValidationStatement.agent_key == agent_key)
        .order_by(AiValidationStatement.version.desc(),
                  AiValidationStatement.id.desc())).all()


def _currency_of(session: Session, row: AiValidationStatement) -> str:
    if row.status != "approved":
        return row.status
    cfg = _published_agent(session, row.agent_key)
    if cfg is None:
        return "stale"
    route = str((cfg.payload or {}).get("model") or "")
    if row.agent_config_version != cfg.version or row.model_route != route:
        return "stale"
    if _aware(row.review_by) is not None and _aware(row.review_by) <= _now():
        return "expired"
    return "current"


def statement_status(session: Session, agent_key: str) -> dict:
    """Current statement posture for one agent vs its PUBLISHED config
    version + model route:

    - "current"  — an approved statement covers the published
      version + route and its review-by date is in the future;
    - "stale"    — an approved statement exists but covers another
      version or route (a republication re-opens the statement, §10);
    - "expired"  — an approved statement matches but its review-by
      date has passed;
    - "suspended"— the latest statement version is suspended;
    - "not-required" — governed config does not require a statement
      for enablement and none is current;
    - "missing"  — no approved statement (none drafted, or the latest
      version is still draft/pending/retired)."""
    cfg = _published_agent(session, agent_key)
    published_version = cfg.version if cfg is not None else None
    route = str((cfg.payload or {}).get("model") or "") \
        if cfg is not None else None
    rows = _statements_for(session, agent_key)
    base = {"agentKey": agent_key, "statementId": None, "version": None,
            "author": None,
            "reviewBy": None, "publishedVersion": published_version,
            "modelRoute": route}
    approved = [r for r in rows if r.status == "approved"]
    for row in approved:
        if published_version is not None \
                and row.agent_config_version == published_version \
                and row.model_route == route:
            base.update({"statementId": row.statement_id,
                         "version": row.version, "author": row.author,
                         "reviewBy": _iso(row.review_by)})
            if _aware(row.review_by) is not None \
                    and _aware(row.review_by) <= _now():
                return {**base, "status": "expired",
                        "reason": (f"Statement {row.statement_id} "
                                   f"v{row.version} review-by date "
                                   f"{_iso(row.review_by)} has passed; "
                                   "renewal is a re-approval (§5.3)")}
            return {**base, "status": "current",
                    "reason": (f"Statement {row.statement_id} "
                               f"v{row.version} is approved and current "
                               f"until {_iso(row.review_by)}")}
    if approved:
        row = approved[0]
        base.update({"statementId": row.statement_id,
                     "version": row.version, "author": row.author,
                     "reviewBy": _iso(row.review_by)})
        return {**base, "status": "stale",
                "reason": (f"Approved statement {row.statement_id} "
                           f"v{row.version} covers config "
                           f"v{row.agent_config_version} with model route "
                           f"'{row.model_route or '(none)'}', but the "
                           f"published config is v{published_version} with "
                           f"route '{route or '(none)'}'; a version/route "
                           "change re-opens the statement (§10)")}
    latest = rows[0] if rows else None
    if latest is not None:
        base.update({"statementId": latest.statement_id,
                     "version": latest.version, "author": latest.author,
                     "reviewBy": _iso(latest.review_by)})
    if not _require_statement(session):
        return {**base, "status": "not-required",
                "reason": ("The published agent-validation config does "
                           "not require a validation statement for "
                           "enablement (requireStatementForEnable is off)")}
    if latest is not None and latest.status == "suspended":
        reason_text = latest.suspension_reason or "no reason recorded"
        return {**base, "status": "suspended",
                "reason": (f"Statement {latest.statement_id} "
                           f"v{latest.version} is suspended: "
                           f"{reason_text}")}
    if latest is None:
        return {**base, "status": "missing",
                "reason": (f"No validation statement exists for agent "
                           f"'{agent_key}'; no statement, no enablement "
                           "(framework §2.2)")}
    return {**base, "status": "missing",
            "reason": (f"Statement {latest.statement_id} v{latest.version} "
                       f"is '{latest.status}', not approved; no approved "
                       "statement covers this agent")}


def kill_switch_active(session: Session) -> bool:
    row = session.get(AiGovernanceState, 1)
    return bool(row and row.kill_switch_active)


def _gov_state(session: Session) -> AiGovernanceState:
    row = session.get(AiGovernanceState, 1)
    if row is None:
        row = AiGovernanceState(id=1, kill_switch_active=False)
        session.add(row)
        session.flush()
    return row


def runtime_gate(session: Session, agent_key: str) -> dict:
    """The shared runtime verdict (AI-G4): may this agent run, on
    governance grounds alone? Refuses, with the real reason named,
    when the kill switch is active, when a required validation record
    is not current, or when a required validation statement is not
    current. Enablement is NOT checked here — callers own that flip."""
    checks: dict = {}
    if kill_switch_active(session):
        state = _gov_state(session)
        checks["killSwitch"] = {"active": True,
                                "reason": state.kill_reason}
        return {"allowed": False,
                "reason": (f"Agent '{agent_key}' cannot run: the AI "
                           f"governance kill switch is active"
                           + (f" ({state.kill_reason})"
                              if state.kill_reason else "")),
                "checks": checks}
    checks["killSwitch"] = {"active": False}
    vs = validation_status(session, agent_key)
    checks["validation"] = {"required": vs["required"],
                            "status": vs["status"]}
    if vs["required"] and vs["status"] != "current":
        return {"allowed": False,
                "reason": (f"Agent '{agent_key}' cannot run: its "
                           f"validation record is {vs['status']} — "
                           f"{vs['reason']}"),
                "checks": checks}
    ss = statement_status(session, agent_key)
    required = _require_statement(session)
    checks["statement"] = {"required": required, "status": ss["status"]}
    if required and ss["status"] != "current":
        return {"allowed": False,
                "reason": (f"Agent '{agent_key}' cannot run: its "
                           f"validation statement is {ss['status']} — "
                           f"{ss['reason']}"),
                "checks": checks}
    return {"allowed": True,
            "reason": (f"Agent '{agent_key}' has a current validation "
                       "record and statement and the kill switch is off"),
            "checks": checks}


def governance_status(session: Session, agent_key: str) -> dict:
    """The combined governance posture for one agent: tier, kill
    switch, validation-record status, statement status, runtime gate,
    and a one-line verdict string."""
    tier = agent_tier(session, agent_key)
    vs = validation_status(session, agent_key)
    ss = statement_status(session, agent_key)
    gate = runtime_gate(session, agent_key)
    ks = kill_switch_active(session)
    if ks:
        verdict = f"kill switch active — agent '{agent_key}' is stopped"
    elif gate["allowed"]:
        verdict = (f"governed: {tier} statement {ss['status']}, "
                   f"validation record {vs['status']}")
    else:
        verdict = f"not runnable: {gate['reason']}"
    return {"agentKey": agent_key, "tier": tier, "killSwitchActive": ks,
            "validation": vs, "statement": ss, "runtimeGate": gate,
            "verdict": verdict}


# ---------------------------------------------------------------------------
# Safety linkage (AI-G6/G9)
# ---------------------------------------------------------------------------

def raise_agent_safety_event(session: Session, *, reporter: str,
                             description: str, harm_level: str = "none",
                             patient_ref: str | None = None) -> SafetyEvent:
    """Raise one SafetyEvent in the "ai-agent" category for an agent
    governance incident. The category is validated against the
    PUBLISHED continuum-ops taxonomy (terminology/continuum-ops,
    `safetyEventCategories`); if a site removed "ai-agent" from its
    taxonomy the event falls back to "other" rather than failing.
    Writes the row + the ai_governance.safety_event.raise audit row.
    Does NOT commit — the caller owns the transaction."""
    ops = _ops_payload(session)
    categories = ops.get("safetyEventCategories") or []
    category = "ai-agent" if "ai-agent" in categories else "other"
    harm = harm_level if harm_level in _harm_codes(session) else "none"
    rca = harm in (ops.get("rcaHarmLevels") or [])
    event = SafetyEvent(
        event_id=str(uuid.uuid4()), reporter=reporter,
        patient_ref=patient_ref, category=category, harm_level=harm,
        description=description, confidential=False,
        status="reported", rca_required=rca,
        rca_status="open" if rca else None)
    session.add(event)
    session.flush()
    append_audit(session, actor=reporter,
                 action="ai_governance.safety_event.raise",
                 entity=f"safety-event/{event.event_id}",
                 payload={"category": category, "harm_level": harm,
                          "description": description[:500]})
    return event


def _disable_agent(session: Session, agent_key: str, actor: str,
                   action: str, payload: dict) -> bool:
    """Flip one agent's enablement off (audited under `action`).
    Returns True when the agent was enabled and is now disabled."""
    row = _enablement(session, agent_key)
    if row is None or not row.enabled:
        return False
    row.enabled = False
    row.updated_by = actor
    session.flush()
    cfg = _published_agent(session, agent_key)
    version = cfg.version if cfg is not None else None
    append_audit(session, actor=actor, action=action,
                 entity=f"agent:{agent_key}@v{version}",
                 payload={"agent": agent_key, "version": version,
                          **payload})
    return True


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------

def _approval_out(row: AiStatementApproval) -> dict:
    return {"id": row.id, "approver": row.approver,
            "kind": row.approval_kind, "conditions": row.conditions,
            "looseningReason": row.loosening_reason,
            "decidedAt": _iso(row.decided_at)}


def _approvals_for(session: Session, row_id: int) -> list:
    rows = session.scalars(select(AiStatementApproval).where(
        AiStatementApproval.statement_row_id == row_id)
        .order_by(AiStatementApproval.id.asc())).all()
    return [_approval_out(r) for r in rows]


def _statement_out(session: Session, row: AiValidationStatement,
                   include_approvals: bool = False) -> dict:
    out = {"id": row.id, "statementId": row.statement_id,
           "version": row.version, "agentKey": row.agent_key,
           "agentConfigVersion": row.agent_config_version,
           "modelRoute": row.model_route, "riskTier": row.risk_tier,
           "owner": row.owner, "fields": row.fields_json or {},
           "status": row.status, "author": row.author,
           "currency": _currency_of(session, row),
           "suspensionReason": row.suspension_reason,
           "createdAt": _iso(row.created_at),
           "updatedAt": _iso(row.updated_at),
           "submittedAt": _iso(row.submitted_at),
           "approvedAt": _iso(row.approved_at),
           "reviewBy": _iso(row.review_by)}
    if include_approvals:
        out["approvals"] = _approvals_for(session, row.id)
    return out


def _latest_row(session: Session,
                statement_id: str) -> AiValidationStatement:
    row = session.scalars(
        select(AiValidationStatement)
        .where(AiValidationStatement.statement_id == statement_id)
        .order_by(AiValidationStatement.version.desc(),
                  AiValidationStatement.id.desc())).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Validation statement "
                                   f"'{statement_id}' not found")
    return row


# ---------------------------------------------------------------------------
# Tier requirements endpoint
# ---------------------------------------------------------------------------

@router.get("/tier-requirements")
def get_tier_requirements(session: Session = Depends(get_db),
                          user: User = Depends(_require_read)):
    tiers, source = tier_requirements(session)
    return {"tiers": tiers, "source": source}


# ---------------------------------------------------------------------------
# Statement lifecycle
# ---------------------------------------------------------------------------

def _fields_with_defaults(fields) -> dict:
    out = dict(fields) if isinstance(fields, dict) else {}
    if not str(out.get("fundingSource") or "").strip():
        out["fundingSource"] = "internal R&D"
    return out


@router.post("/statements", status_code=201)
async def create_statement(request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(_require_validate)):
    """Open a statement draft for one agent's CURRENT published
    version + model route (server-stamped from the published config;
    409 when the agent has no published config). Content may be
    incomplete in draft — completeness is a submit-time rule. When a
    lineage exists whose latest version is not draft/pending, this
    creates the next version of that lineage; the previous approved
    version stays approved until the new one is approved."""
    body = await _json_body(request)
    agent_key = body.get("agentKey") or body.get("agent_key")
    if not isinstance(agent_key, str) or not agent_key.strip():
        raise HTTPException(status_code=422, detail="'agentKey' is required")
    agent_key = agent_key.strip()
    owner = body.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        raise HTTPException(status_code=422,
                            detail="'owner' is required (field 4: a named "
                                   "individual)")
    owner = owner.strip()
    problem = _owner_alias_problem(owner)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    tier = body.get("riskTier") or body.get("risk_tier")
    if tier not in TIERS:
        raise HTTPException(status_code=422,
                            detail=f"'riskTier' must be one of {list(TIERS)}")
    cfg = _published_agent(session, agent_key)
    if cfg is None:
        raise HTTPException(
            status_code=409,
            detail=f"Agent '{agent_key}' has no published config; a "
                   "statement covers a published version, so publish the "
                   "agent before drafting its statement")
    payload = cfg.payload or {}
    declared_tier = payload.get("riskTier")
    if declared_tier in TIERS and declared_tier != tier:
        raise HTTPException(
            status_code=422,
            detail=f"Agent '{agent_key}' published config declares "
                   f"riskTier {declared_tier}; the statement's riskTier "
                   f"({tier}) must match it")
    route = str(payload.get("model") or "")
    rows = _statements_for(session, agent_key)
    live = [r for r in rows if r.status != "superseded"]
    if live:
        latest = live[0]
        if latest.status in ("draft", "pending_approval"):
            raise HTTPException(
                status_code=409,
                detail=f"Statement {latest.statement_id} v{latest.version} "
                       f"is '{latest.status}'; edit the existing draft "
                       "instead of opening a new version")
        statement_id = latest.statement_id
        version = max(r.version for r in rows) + 1
    else:
        seq = len({r.statement_id for r in rows}) + 1
        statement_id = f"VS-{agent_key}-{seq}"
        version = 1
    row = AiValidationStatement(
        statement_id=statement_id, version=version, agent_key=agent_key,
        agent_config_version=cfg.version, model_route=route,
        risk_tier=tier, owner=owner,
        fields_json=_fields_with_defaults(body.get("fields")),
        status="draft", author=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="ai_governance.statement.create",
                 entity=f"statement:{statement_id}",
                 payload={"statement_id": statement_id, "version": version,
                          "agent": agent_key,
                          "agent_config_version": cfg.version,
                          "model_route": route, "risk_tier": tier,
                          "owner": owner})
    session.commit()
    return _statement_out(session, row, include_approvals=True)


@router.get("/statements")
def list_statements(agent: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(_require_read)):
    stmt = select(AiValidationStatement).order_by(
        AiValidationStatement.agent_key.asc(),
        AiValidationStatement.version.desc(),
        AiValidationStatement.id.desc())
    if agent:
        stmt = stmt.where(AiValidationStatement.agent_key == agent)
    rows = session.scalars(stmt).all()
    return {"items": [_statement_out(session, r) for r in rows],
            "total": len(rows)}


@router.get("/statements/{statement_id}")
def get_statement(statement_id: str, session: Session = Depends(get_db),
                  user: User = Depends(_require_read)):
    row = _latest_row(session, statement_id)
    versions = session.scalars(
        select(AiValidationStatement)
        .where(AiValidationStatement.statement_id == statement_id)
        .order_by(AiValidationStatement.version.desc())).all()
    out = _statement_out(session, row, include_approvals=True)
    out["versions"] = [{"version": v.version, "status": v.status,
                        "currency": _currency_of(session, v),
                        "reviewBy": _iso(v.review_by)} for v in versions]
    return out


@router.put("/statements/{statement_id}")
async def update_statement(statement_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(_require_validate)):
    """Replace the fields/owner of the DRAFT version only — a
    submitted or decided version is a controlled artefact and is
    never edited in place (supersede-never-edit)."""
    body = await _json_body(request)
    row = _latest_row(session, statement_id)
    if row.status != "draft":
        raise HTTPException(
            status_code=409,
            detail=f"Statement {statement_id} v{row.version} is "
                   f"'{row.status}'; only a draft version may be edited")
    if "owner" in body:
        owner = body.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            raise HTTPException(status_code=422,
                                detail="'owner' must be a non-empty string "
                                       "(a named individual)")
        problem = _owner_alias_problem(owner.strip())
        if problem:
            raise HTTPException(status_code=422, detail=problem)
        row.owner = owner.strip()
    if "fields" in body:
        if not isinstance(body.get("fields"), dict):
            raise HTTPException(status_code=422,
                                detail="'fields' must be a JSON object")
        row.fields_json = _fields_with_defaults(body["fields"])
    row.updated_at = _now()
    session.flush()
    append_audit(session, actor=user.username,
                 action="ai_governance.statement.update",
                 entity=f"statement:{statement_id}",
                 payload={"statement_id": statement_id,
                          "version": row.version, "agent": row.agent_key})
    session.commit()
    return _statement_out(session, row, include_approvals=True)


# --- Submit-time completeness + evidence validation (framework §4) --------

def _nonempty(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return value is not None


def _thresholds_loosened(fields: dict, tier_req: dict) -> list:
    """Axes on which the statement's thresholds are LOOSER than the
    tier defaults (§4.4: tightening is free, loosening is governed)."""
    thresholds = fields.get("thresholds")
    if not isinstance(thresholds, dict):
        return []
    loosened = []
    overall = thresholds.get("overallPass")
    if isinstance(overall, (int, float)) and not isinstance(overall, bool) \
            and overall < float(tier_req["minOverallPass"]):
        loosened.append("overallPass")
    floor = thresholds.get("sliceFloor")
    if isinstance(floor, (int, float)) and not isinstance(floor, bool) \
            and floor < float(tier_req["minSliceFloor"]):
        loosened.append("sliceFloor")
    ceiling = thresholds.get("disparityCeiling")
    if isinstance(ceiling, (int, float)) and not isinstance(ceiling, bool) \
            and ceiling > float(tier_req["maxSliceDisparity"]):
        loosened.append("disparityCeiling")
    return loosened


def _validate_submission(session: Session,
                         row: AiValidationStatement) -> tuple[list, list]:
    """Full §4.1 completeness + evidence validation. Returns
    (missing, invalid) — every problem found, named, so the author
    can fix the draft in one pass."""
    fields = row.fields_json or {}
    tier_req = _tier_req(session, row.risk_tier)
    missing: list[str] = []
    invalid: list[str] = []

    for key, label in (("intendedUse", "intendedUse (field 5)"),
                       ("clinicalDecision", "clinicalDecision (field 6)"),
                       ("riskTierRationale",
                        "riskTierRationale (field 7)"),
                       ("populationScope", "populationScope (field 8)"),
                       ("failureModes", "failureModes (field 13)"),
                       ("knownLimitations",
                        "knownLimitations (field 14)"),
                       ("dispositionRule", "dispositionRule (field 16)"),
                       ("toolsDataAccess", "toolsDataAccess (field 17)"),
                       ("monitoringPlan", "monitoringPlan (field 18)"),
                       ("changeControlPlan",
                        "changeControlPlan (field 19)"),
                       ("incidentSuspension",
                        "incidentSuspension (field 20)")):
        if not _nonempty(fields.get(key)):
            missing.append(label)

    claims = fields.get("claims")
    if not isinstance(claims, list) or not claims:
        missing.append("claims (field 9): at least one claim is required")
    else:
        for idx, claim in enumerate(claims):
            if not isinstance(claim, dict) \
                    or not str(claim.get("text") or "").strip():
                invalid.append(f"claims[{idx}] (field 9): every claim "
                               "needs a non-empty 'text'")

    thresholds = fields.get("thresholds")
    if not isinstance(thresholds, dict):
        missing.append("thresholds (field 11): {overallPass, sliceFloor, "
                       "disparityCeiling} is required")
        thresholds = {}
    else:
        for key in ("overallPass", "sliceFloor", "disparityCeiling"):
            value = thresholds.get(key)
            if not isinstance(value, (int, float)) \
                    or isinstance(value, bool):
                invalid.append(f"thresholds.{key} (field 11) must be a "
                               "number")

    zero = fields.get("zeroToleranceClasses")
    if not isinstance(zero, list) or not zero:
        missing.append("zeroToleranceClasses (field 12) is required")
    else:
        bad = [c for c in zero if c not in (1, 2, 3, 4, 5)]
        if bad:
            invalid.append(f"zeroToleranceClasses (field 12) must be a "
                           f"subset of 1..5; got {bad}")
        for required_class in (1, 2):
            if required_class not in zero:
                invalid.append(f"zeroToleranceClasses (field 12) must "
                               f"include class {required_class} "
                               "(§4.3: fabricated clinical fact / "
                               "cross-patient contamination are never "
                               "optional)")

    prohibited = fields.get("prohibitedUses")
    if not isinstance(prohibited, list) or not prohibited:
        missing.append("prohibitedUses (field 15) is required")
    else:
        codes = set()
        for idx, entry in enumerate(prohibited):
            if not isinstance(entry, dict) \
                    or not str(entry.get("code") or "").strip():
                invalid.append(f"prohibitedUses[{idx}] (field 15): every "
                               "entry needs a 'code'")
            else:
                codes.add(str(entry["code"]).strip())
        for seed_code in PROHIBITED_SEED_CODES:
            if seed_code not in codes:
                invalid.append(f"prohibitedUses (field 15) must include "
                               f"the seed code '{seed_code}' (§4.5)")

    requested_raw = fields.get("reviewByRequested")
    if requested_raw not in (None, ""):
        requested = _parse_date(requested_raw)
        if requested is None:
            invalid.append("reviewByRequested (field 21) must be an ISO "
                           "date/datetime")
        else:
            window_end = _add_months(
                _now(), int(tier_req["statementValidityMonths"]))
            if requested > window_end:
                invalid.append(
                    f"reviewByRequested (field 21) {requested.date()} "
                    f"exceeds the {row.risk_tier} tier window of "
                    f"{tier_req['statementValidityMonths']} months "
                    f"(latest allowed {window_end.date()})")

    loosened = _thresholds_loosened(fields, tier_req)
    loosening_reason = str(fields.get("thresholdLooseningReason")
                           or "").strip()
    if loosened and not loosening_reason:
        invalid.append(
            f"thresholds loosen the {row.risk_tier} tier defaults on "
            f"{', '.join(loosened)} (field 11 / §4.4); loosening "
            "requires a 'thresholdLooseningReason' recorded on the "
            "statement for the governance approver to re-state at "
            "approval")

    evidence = fields.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        missing.append("evidence (field 10): at least one evaluation "
                       "run citation is required")
    else:
        for idx, item in enumerate(evidence):
            if not isinstance(item, dict):
                invalid.append(f"evidence[{idx}] (field 10) must be an "
                               "object {datasetKey, runKey, mode}")
                continue
            dataset_key = str(item.get("datasetKey") or "").strip()
            run_key = str(item.get("runKey") or "").strip()
            mode = str(item.get("mode") or "").strip()
            if not dataset_key or not run_key or not mode:
                invalid.append(f"evidence[{idx}] (field 10) requires "
                               "datasetKey, runKey and mode")
                continue
            if mode not in av.RUN_MODES:
                invalid.append(f"evidence[{idx}] (field 10): mode "
                               f"'{mode}' must be one of "
                               f"{list(av.RUN_MODES)}")
                continue
            run = session.scalars(select(EvaluationRun).where(
                EvaluationRun.run_key == run_key)).first()
            if run is None:
                invalid.append(f"evidence[{idx}] (field 10): evaluation "
                               f"run '{run_key}' does not exist")
                continue
            if run.agent_key != row.agent_key:
                invalid.append(f"evidence[{idx}]: run '{run_key}' is for "
                               f"agent '{run.agent_key}', not "
                               f"'{row.agent_key}'")
                continue
            if run.agent_config_version != row.agent_config_version:
                invalid.append(
                    f"evidence[{idx}]: run '{run_key}' evaluated config "
                    f"v{run.agent_config_version}, but this statement "
                    f"covers v{row.agent_config_version}")
                continue
            if run.model_route != row.model_route:
                invalid.append(
                    f"evidence[{idx}]: run '{run_key}' used model route "
                    f"'{run.model_route or '(none)'}', but this "
                    f"statement covers '{row.model_route or '(none)'}'")
                continue
            if isinstance(thresholds.get("overallPass"), (int, float)) \
                    and run.pass_rate < float(thresholds["overallPass"]):
                invalid.append(
                    f"evidence[{idx}]: run '{run_key}' overall pass "
                    f"rate {run.pass_rate:.4f} is below the statement "
                    f"threshold overallPass "
                    f"{float(thresholds['overallPass']):.4f}")
            slice_metrics = run.slice_metrics or {}
            if isinstance(thresholds.get("sliceFloor"), (int, float)):
                for label, metrics in slice_metrics.items():
                    rate = (metrics or {}).get("passRate")
                    if isinstance(rate, (int, float)) \
                            and rate < float(thresholds["sliceFloor"]):
                        invalid.append(
                            f"evidence[{idx}]: run '{run_key}' slice "
                            f"'{label}' pass rate {rate:.4f} is below "
                            f"the statement sliceFloor "
                            f"{float(thresholds['sliceFloor']):.4f}")
            if len(slice_metrics) >= 2 and isinstance(
                    thresholds.get("disparityCeiling"), (int, float)):
                rates = [m.get("passRate") for m in slice_metrics.values()
                         if isinstance((m or {}).get("passRate"),
                                       (int, float))]
                if rates:
                    disparity = round(max(rates) - min(rates), 4)
                    if disparity > float(thresholds["disparityCeiling"]):
                        invalid.append(
                            f"evidence[{idx}]: run '{run_key}' slice "
                            f"disparity {disparity:.4f} exceeds the "
                            f"statement disparityCeiling "
                            f"{float(thresholds['disparityCeiling']):.4f}")
    return missing, invalid


@router.post("/statements/{statement_id}/submit")
async def submit_statement(statement_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(_require_validate)):
    row = _latest_row(session, statement_id)
    if row.status != "draft":
        raise HTTPException(
            status_code=409,
            detail=f"Statement {statement_id} v{row.version} is "
                   f"'{row.status}'; only a draft may be submitted")
    try:
        body = await _json_body(request)
    except HTTPException:
        body = {}
    fields = dict(row.fields_json or {})
    # A loosening reason may ride the submit body as well as the
    # fields; either way it is stored ON the statement (§4.4).
    if isinstance(body.get("thresholdLooseningReason"), str) \
            and body["thresholdLooseningReason"].strip():
        fields["thresholdLooseningReason"] = \
            body["thresholdLooseningReason"].strip()
        row.fields_json = fields
    missing, invalid = _validate_submission(session, row)
    if missing or invalid:
        raise HTTPException(status_code=422,
                            detail={"missing": missing, "invalid": invalid})
    row.status = "pending_approval"
    row.submitted_at = _now()
    row.updated_at = _now()
    session.flush()
    append_audit(session, actor=user.username,
                 action="ai_governance.statement.submit",
                 entity=f"statement:{statement_id}",
                 payload={"statement_id": statement_id,
                          "version": row.version, "agent": row.agent_key,
                          "risk_tier": row.risk_tier})
    session.commit()
    return _statement_out(session, row, include_approvals=True)


# --- Approval (four-eyes, framework §5.2) ----------------------------------

def _four_eyes_refusal(session: Session, *, actor: str, statement_id: str,
                       row: AiValidationStatement, rule: str) -> None:
    """Record the refused self-approval on the hash chain, commit it
    (so the refusal survives the 403), then refuse. The rule is the
    framework's: approver != statement author AND approver != the
    covered agent config version's author."""
    append_audit(session, actor=actor,
                 action="governance.four_eyes.refused",
                 entity=f"statement:{statement_id}",
                 payload={"statement_id": statement_id,
                          "version": row.version, "agent": row.agent_key,
                          "rule": rule})
    session.commit()
    raise HTTPException(status_code=403, detail=rule)


def _check_four_eyes(session: Session, *, actor: str, statement_id: str,
                     row: AiValidationStatement) -> None:
    if actor == row.author:
        _four_eyes_refusal(
            session, actor=actor, statement_id=statement_id, row=row,
            rule=(f"Four-eyes rule (framework §5.2): '{actor}' authored "
                  f"statement {statement_id} v{row.version} and may not "
                  "approve it"))
    cfg_row = _agent_version_row(session, row.agent_key,
                                 row.agent_config_version) \
        or _published_agent(session, row.agent_key)
    if cfg_row is not None and cfg_row.created_by \
            and actor == cfg_row.created_by:
        _four_eyes_refusal(
            session, actor=actor, statement_id=statement_id, row=row,
            rule=(f"Four-eyes rule (framework §5.2): '{actor}' authored "
                  f"agent '{row.agent_key}' config "
                  f"v{row.agent_config_version} and may not approve its "
                  "validation statement"))


@router.post("/statements/{statement_id}/approve")
async def approve_statement(statement_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(_require_validate)):
    body = await _json_body(request)
    row = _latest_row(session, statement_id)
    if row.status != "pending_approval":
        raise HTTPException(
            status_code=409,
            detail=f"Statement {statement_id} v{row.version} is "
                   f"'{row.status}'; approvals need a submitted "
                   "(pending_approval) statement")
    kind = body.get("kind")
    if kind not in APPROVAL_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"'kind' must be one of {list(APPROVAL_KINDS)}")
    tier_req = _tier_req(session, row.risk_tier)
    required_kinds = list(tier_req.get("approvals") or [])
    if kind not in required_kinds:
        raise HTTPException(
            status_code=422,
            detail=f"Approval kind '{kind}' is not required for tier "
                   f"{row.risk_tier} (required: {required_kinds})")
    _check_four_eyes(session, actor=user.username,
                     statement_id=statement_id, row=row)
    existing = session.scalars(select(AiStatementApproval).where(
        AiStatementApproval.statement_row_id == row.id)).all()
    if any(a.approver == user.username for a in existing):
        raise HTTPException(
            status_code=409,
            detail=f"'{user.username}' already holds an approval on "
                   f"statement {statement_id} v{row.version}; one "
                   "actor may not hold two approval kinds on one "
                   "statement")
    if any(a.approval_kind == kind for a in existing):
        raise HTTPException(
            status_code=409,
            detail=f"Approval kind '{kind}' is already recorded on "
                   f"statement {statement_id} v{row.version}")
    fields = row.fields_json or {}
    loosening_reason = str(body.get("looseningReason") or "").strip()
    if kind == "governance_approver" \
            and _thresholds_loosened(fields, tier_req) \
            and not loosening_reason:
        raise HTTPException(
            status_code=422,
            detail="This statement's thresholds loosen the tier "
                   "defaults (§4.4); the governance approver must "
                   "re-state the loosening reason ('looseningReason') "
                   "in the approval")
    approval = AiStatementApproval(
        statement_row_id=row.id, approver=user.username,
        approval_kind=kind,
        conditions=str(body.get("conditions") or ""),
        loosening_reason=loosening_reason
        or str(fields.get("thresholdLooseningReason") or "")
        if kind == "governance_approver" else loosening_reason)
    session.add(approval)
    session.flush()
    append_audit(session, actor=user.username,
                 action="ai_governance.statement.approve",
                 entity=f"statement:{statement_id}",
                 payload={"statement_id": statement_id,
                          "version": row.version, "agent": row.agent_key,
                          "kind": kind, "approver": user.username,
                          "conditions": approval.conditions,
                          "loosening_reason": approval.loosening_reason})
    present = {a.approval_kind for a in existing} | {kind}
    completed = all(k in present for k in required_kinds)
    if completed:
        now = _now()
        row.status = "approved"
        row.approved_at = now
        months = int(tier_req["statementValidityMonths"])
        expiry = _add_months(now, months)
        requested = _parse_date(fields.get("reviewByRequested"))
        row.review_by = min(requested, expiry) if requested else expiry
        row.updated_at = now
        # The previous approved version of this agent is superseded
        # by this approval — archived, never edited (§4.1 field 1).
        for other in _statements_for(session, row.agent_key):
            if other.id != row.id and other.status == "approved":
                other.status = "superseded"
        session.flush()
        append_audit(session, actor=user.username,
                     action="ai_governance.statement.approved",
                     entity=f"statement:{statement_id}",
                     payload={"statement_id": statement_id,
                              "version": row.version,
                              "agent": row.agent_key,
                              "review_by": _iso(row.review_by)})
    session.commit()
    return _statement_out(session, row, include_approvals=True)


@router.post("/statements/{statement_id}/suspend")
async def suspend_statement(statement_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(_require_validate)):
    body = await _json_body(request)
    row = _latest_row(session, statement_id)
    if row.status != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"Statement {statement_id} v{row.version} is "
                   f"'{row.status}'; only an approved statement may be "
                   "suspended")
    row.status = "suspended"
    row.suspension_reason = str(body.get("reason") or "")
    row.updated_at = _now()
    session.flush()
    append_audit(session, actor=user.username,
                 action="ai_governance.statement.suspend",
                 entity=f"statement:{statement_id}",
                 payload={"statement_id": statement_id,
                          "version": row.version, "agent": row.agent_key,
                          "reason": row.suspension_reason})
    session.commit()
    return _statement_out(session, row, include_approvals=True)


@router.post("/statements/{statement_id}/reinstate")
async def reinstate_statement(statement_id: str, request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(_require_validate)):
    """Suspended -> approved, ONLY through a fresh governance-
    approver approval record written by this call (four-eyes against
    the statement author still applies)."""
    try:
        body = await _json_body(request)
    except HTTPException:
        body = {}
    row = _latest_row(session, statement_id)
    if row.status != "suspended":
        raise HTTPException(
            status_code=409,
            detail=f"Statement {statement_id} v{row.version} is "
                   f"'{row.status}'; only a suspended statement may be "
                   "reinstated")
    _check_four_eyes(session, actor=user.username,
                     statement_id=statement_id, row=row)
    approval = AiStatementApproval(
        statement_row_id=row.id, approver=user.username,
        approval_kind="governance_approver",
        conditions=str(body.get("conditions") or ""),
        loosening_reason=str(body.get("looseningReason") or ""))
    session.add(approval)
    row.status = "approved"
    row.suspension_reason = ""
    row.updated_at = _now()
    session.flush()
    append_audit(session, actor=user.username,
                 action="ai_governance.statement.approve",
                 entity=f"statement:{statement_id}",
                 payload={"statement_id": statement_id,
                          "version": row.version, "agent": row.agent_key,
                          "kind": "governance_approver",
                          "approver": user.username,
                          "reinstatement": True})
    append_audit(session, actor=user.username,
                 action="ai_governance.statement.reinstate",
                 entity=f"statement:{statement_id}",
                 payload={"statement_id": statement_id,
                          "version": row.version, "agent": row.agent_key})
    session.commit()
    return _statement_out(session, row, include_approvals=True)


@router.post("/statements/{statement_id}/retire")
async def retire_statement(statement_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(_require_validate)):
    try:
        body = await _json_body(request)
    except HTTPException:
        body = {}
    row = _latest_row(session, statement_id)
    if row.status in ("retired", "superseded"):
        raise HTTPException(
            status_code=409,
            detail=f"Statement {statement_id} v{row.version} is "
                   f"already '{row.status}' (terminal)")
    row.status = "retired"
    row.updated_at = _now()
    session.flush()
    append_audit(session, actor=user.username,
                 action="ai_governance.statement.retire",
                 entity=f"statement:{statement_id}",
                 payload={"statement_id": statement_id,
                          "version": row.version, "agent": row.agent_key,
                          "note": str(body.get("note") or "")})
    session.commit()
    return _statement_out(session, row, include_approvals=True)


# ---------------------------------------------------------------------------
# Per-agent status + disclosure
# ---------------------------------------------------------------------------

@router.get("/agents/{key}/status")
def get_agent_status(key: str, session: Session = Depends(get_db),
                     user: User = Depends(_require_read)):
    gov = governance_status(session, key)
    return {"agentKey": key, "tier": gov["tier"],
            "killSwitchActive": gov["killSwitchActive"],
            "enabled": _is_enabled(session, key),
            "validation": gov["validation"],
            "statement": gov["statement"],
            "governance": gov["verdict"],
            "runtimeGate": gov["runtimeGate"]}


def _approved_statement(session: Session,
                        agent_key: str) -> AiValidationStatement | None:
    for row in _statements_for(session, agent_key):
        if row.status == "approved":
            return row
    return None


@router.get("/agents/{key}/disclosure")
def get_disclosure(key: str, session: Session = Depends(get_db),
                   user: User = Depends(_require_read)):
    """The HTI-1 source-attribute set for one agent (framework
    §13.2, AI-G7), generated from the approved statement + published
    config + validation evidence. Statement-sourced attributes are
    marked NOT_RECORDED when no approved statement exists — values
    are never invented."""
    cfg = _published_agent(session, key)
    payload = (cfg.payload or {}) if cfg is not None else {}
    route = str(payload.get("model") or "")
    provider = str(payload.get("provider")
                   or (route.split("/")[0] if "/" in route else "")
                   or "(none recorded)")
    stmt = _approved_statement(session, key)
    fields = (stmt.fields_json or {}) if stmt is not None else {}
    vs = validation_status(session, key)
    record = vs.get("record")

    def stmt_value(value, fallback=NOT_RECORDED):
        if stmt is not None and _nonempty(value):
            return str(value) if not isinstance(value, (list, dict)) \
                else value
        return fallback

    # Evidence-derived attributes: cited runs of the approved
    # statement, else the current validation record.
    data_summary = NOT_RECORDED
    performance = NOT_RECORDED
    fairness = NOT_RECORDED
    cited_runs: list[EvaluationRun] = []
    if stmt is not None:
        for item in fields.get("evidence") or []:
            if isinstance(item, dict) and item.get("runKey"):
                run = session.scalars(select(EvaluationRun).where(
                    EvaluationRun.run_key == str(item["runKey"]))).first()
                if run is not None:
                    cited_runs.append(run)
    if cited_runs:
        parts = []
        for run in cited_runs:
            dataset = session.scalars(select(EvaluationDataset).where(
                EvaluationDataset.dataset_key == run.dataset_key)).first()
            digest = dataset.cases_hash if dataset is not None else "?"
            parts.append(f"dataset {run.dataset_key} (sha256 {digest}), "
                         f"run {run.run_key} mode {run.mode}")
        data_summary = "; ".join(parts)
        perf_parts = []
        disparities = []
        for run in cited_runs:
            slices = {label: m.get("passRate")
                      for label, m in (run.slice_metrics or {}).items()}
            perf_parts.append(
                f"run {run.run_key}: overall pass {run.pass_rate:.4f}; "
                f"per-slice {slices}")
            rates = [v for v in slices.values()
                     if isinstance(v, (int, float))]
            if len(rates) >= 2:
                disparities.append(round(max(rates) - min(rates), 4))
        performance = "; ".join(perf_parts)
        ceiling = (fields.get("thresholds") or {}).get("disparityCeiling")
        fairness = (f"observed slice disparity "
                    f"{max(disparities) if disparities else 0.0:.4f} "
                    f"across cited runs; statement disparity ceiling "
                    f"{ceiling}")
    elif record is not None:
        data_summary = (f"dataset {record['datasetKey']}, run "
                        f"{record['runKey']} (validation record "
                        f"{record['recordKey']})")
        performance = (f"validation record {record['recordKey']}: "
                       f"overall pass {record['passRate']:.4f}")
        fairness = ("no per-slice fairness summary recorded outside a "
                    "validation statement")
    attributes = [
        {"attribute": "Intended use",
         "value": stmt_value(fields.get("intendedUse")),
         "source": "validation statement field 5"},
        {"attribute": "Intended user / clinical decision touched",
         "value": stmt_value(fields.get("clinicalDecision")),
         "source": "validation statement field 6"},
        {"attribute": "Developer / owner",
         "value": (stmt.owner if stmt is not None
                   else (cfg.updated_by if cfg is not None
                         else NOT_RECORDED)),
         "source": ("validation statement field 4" if stmt is not None
                    else "published agent config")},
        {"attribute": "Model / algorithm description",
         "value": (f"model route '{route or '(none)'}' via provider "
                   f"'{provider}'; tools "
                   f"{payload.get('tools') or []}; api allow-list "
                   f"{payload.get('apiAllowlist') or []}"),
         "source": "published agent config + statement fields 3/17"},
        {"attribute": "Training & validation data summary",
         "value": data_summary,
         "source": "validation statement field 10 + evaluation datasets"},
        {"attribute": "Performance measures (overall + by subgroup)",
         "value": performance,
         "source": "validation statement field 11 + evaluation runs"},
        {"attribute": "Fairness / disparity measures",
         "value": fairness,
         "source": "validation statement field 11 + evaluation runs"},
        {"attribute": "Validation process & external validation status",
         "value": ("governed evaluation datasets + four-eyes approved "
                   "validation statement; external validation status: "
                   "internal governed datasets only; no external "
                   "validation exists"),
         "source": "validation statement fields 10/22; framework §4.2"},
        {"attribute": "Maintenance & update process",
         "value": stmt_value(
             fields.get("monitoringPlan")
             if not _nonempty(fields.get("changeControlPlan"))
             else (f"{fields.get('monitoringPlan')} | change control: "
                   f"{fields.get('changeControlPlan')}")),
         "source": "validation statement fields 18/19"},
        {"attribute": "Known limitations & risk mitigations",
         "value": stmt_value(
             fields.get("knownLimitations")
             if not _nonempty(fields.get("failureModes"))
             else (f"limitations: {fields.get('knownLimitations')} | "
                   f"failure modes: {fields.get('failureModes')} | "
                   f"prohibited uses: {fields.get('prohibitedUses')}")),
         "source": "validation statement fields 13/14/15"},
        {"attribute": "Funding source",
         "value": stmt_value(fields.get("fundingSource")),
         "source": "validation statement (administrative field)"},
    ]
    return {"agentKey": key, "tier": agent_tier(session, key),
            "statementId": stmt.statement_id if stmt is not None else None,
            "statementStatus": statement_status(session, key)["status"],
            "validationStatus": vs["status"],
            "attributes": attributes}


# ---------------------------------------------------------------------------
# Expiry worklist (AI-G10)
# ---------------------------------------------------------------------------

def _days_to(dt: datetime | None) -> int | None:
    aware = _aware(dt)
    if aware is None:
        return None
    return int((aware - _now()).total_seconds() // 86400)


@router.get("/worklist")
def get_worklist(session: Session = Depends(get_db),
                 user: User = Depends(_require_read)):
    agent_keys = set(session.scalars(
        select(AiValidationStatement.agent_key).distinct()).all())
    agent_keys |= set(session.scalars(
        select(ValidationRecord.agent_key).distinct()).all())
    rows = []
    for key in sorted(agent_keys):
        ss = statement_status(session, key)
        vs = validation_status(session, key)
        stmt_days = None
        if ss.get("reviewBy"):
            stmt_days = _days_to(_parse_date(ss["reviewBy"]))
        record_days = None
        record = vs.get("record")
        if record is not None and record.get("expiresAt"):
            record_days = _days_to(_parse_date(record["expiresAt"]))
        stmt_rows = _statements_for(session, key)
        cfg = _published_agent(session, key)
        owner = ""
        if stmt_rows:
            owner = stmt_rows[0].owner
        elif cfg is not None:
            owner = cfg.updated_by or cfg.created_by or ""
        if ss["status"] == "stale":
            due_state = "stale"
        else:
            day_values = [d for d in (stmt_days, record_days)
                          if d is not None]
            if not day_values:
                due_state = "none"
            elif min(day_values) < 0:
                due_state = "overdue"
            elif min(day_values) <= 30:
                due_state = "due-soon"
            else:
                due_state = "current"
        rows.append({"agentKey": key, "owner": owner,
                     "tier": agent_tier(session, key),
                     "statementStatus": ss["status"],
                     "statementDaysToExpiry": stmt_days,
                     "recordStatus": vs["status"],
                     "recordDaysToExpiry": record_days,
                     "dueState": due_state})
    rank = {"overdue": 0, "stale": 1, "due-soon": 2, "current": 3,
            "none": 4}
    rows.sort(key=lambda r: (
        rank.get(r["dueState"], 5),
        min([d for d in (r["statementDaysToExpiry"],
                         r["recordDaysToExpiry"]) if d is not None],
            default=10 ** 9),
        r["agentKey"]))
    return {"items": rows, "total": len(rows)}


# ---------------------------------------------------------------------------
# Kill switch (AI-G5, framework §7.4)
# ---------------------------------------------------------------------------

def _kill_switch_out(state: AiGovernanceState) -> dict:
    return {"active": state.kill_switch_active,
            "reason": state.kill_reason, "engagedBy": state.kill_by,
            "engagedAt": _iso(state.kill_at),
            "releasedBy": state.released_by,
            "releasedAt": _iso(state.released_at)}


@router.get("/kill-switch")
def get_kill_switch(session: Session = Depends(get_db),
                    user: User = Depends(_require_read)):
    return _kill_switch_out(_gov_state(session))


@router.post("/kill-switch")
async def engage_kill_switch(request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(_require_validate)):
    body = await _json_body(request)
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise HTTPException(status_code=422,
                            detail="'reason' is required to engage the "
                                   "kill switch (§7.4: a named reason is "
                                   "recorded)")
    state = _gov_state(session)
    if state.kill_switch_active:
        raise HTTPException(status_code=409,
                            detail="The kill switch is already active")
    state.kill_switch_active = True
    state.kill_reason = reason.strip()
    state.kill_by = user.username
    state.kill_at = _now()
    session.flush()
    disabled = 0
    for enablement in session.scalars(select(AgentEnablement)).all():
        if enablement.enabled:
            enablement.enabled = False
            enablement.updated_by = user.username
            session.flush()
            cfg = _published_agent(session, enablement.agent_key)
            version = cfg.version if cfg is not None else None
            append_audit(
                session, actor=user.username, action="agents.disable",
                entity=f"agent:{enablement.agent_key}@v{version}",
                payload={"agent": enablement.agent_key,
                         "version": version, "kill_switch": True})
            disabled += 1
    append_audit(session, actor=user.username,
                 action="ai_governance.kill_switch.engage",
                 entity="ai-governance/kill-switch",
                 payload={"reason": state.kill_reason,
                          "agents_disabled": disabled})
    session.commit()
    out = _kill_switch_out(state)
    out["agentsDisabled"] = disabled
    return out


@router.post("/kill-switch/release")
async def release_kill_switch(request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(_require_validate)):
    """Clear the kill-switch state ONLY. Re-enablement after a
    kill-switch event is per agent, through the normal gate, against
    current statements (§7.4) — never a bulk restore."""
    state = _gov_state(session)
    if not state.kill_switch_active:
        raise HTTPException(status_code=409,
                            detail="The kill switch is not active")
    state.kill_switch_active = False
    state.released_by = user.username
    state.released_at = _now()
    session.flush()
    append_audit(session, actor=user.username,
                 action="ai_governance.kill_switch.release",
                 entity="ai-governance/kill-switch",
                 payload={"released_by": user.username,
                          "agents_reenabled": 0})
    session.commit()
    return _kill_switch_out(state)


# ---------------------------------------------------------------------------
# Breach checks + zero tolerance (AI-G6, framework §9.4)
# ---------------------------------------------------------------------------

@router.post("/agents/{key}/breach-check")
def breach_check(key: str, session: Session = Depends(get_db),
                 user: User = Depends(_require_validate)):
    """One drift check against the agent's validation baseline
    (app.agent_validation.get_drift). A flagged check increments the
    consecutive-breach counter and raises a SafetyEvent; a clean
    check resets it. At the governed consecutive threshold, a
    T3-tier agent (per the governed auto-suspend tier list) is
    auto-disabled pending governance review (§9.4 rules 1-2)."""
    drift = get_drift(key, session, user)
    flags = drift.get("flags") or []
    tracker = session.scalars(select(AiBreachTracker).where(
        AiBreachTracker.agent_key == key)).first()
    if tracker is None:
        tracker = AiBreachTracker(agent_key=key, consecutive_breaches=0)
        session.add(tracker)
    actions: list[str] = []
    if not flags:
        tracker.consecutive_breaches = 0
        tracker.last_status = drift.get("status") or "within-threshold"
    else:
        tracker.consecutive_breaches += 1
        tracker.last_status = drift.get("status") or "drift-flagged"
        if tracker.consecutive_breaches == 1:
            event = raise_agent_safety_event(
                session, reporter=user.username,
                description=(
                    f"Drift breach for agent '{key}': "
                    + "; ".join(str(f.get("detail") or f)
                                for f in flags)))
            actions.append(f"safety-event:{event.event_id}")
        policy = _drift_breach_policy(session)
        tier = agent_tier(session, key)
        if tracker.consecutive_breaches >= policy["consecutiveChecks"] \
                and tier in policy["autoSuspendTiers"]:
            if _disable_agent(
                    session, key, user.username,
                    "ai_governance.auto_suspend",
                    {"reason": "drift breach auto-suspension (§9.4)",
                     "consecutive_breaches": tracker.consecutive_breaches,
                     "tier": tier}):
                actions.append("agent-auto-suspended")
            event = raise_agent_safety_event(
                session, reporter=user.username,
                description=(
                    f"Agent '{key}' auto-suspended after "
                    f"{tracker.consecutive_breaches} consecutive drift "
                    f"breach checks (tier {tier}); flags: "
                    + "; ".join(str(f.get("detail") or f) for f in flags)))
            actions.append(f"safety-event:{event.event_id}")
    tracker.last_checked_at = _now()
    tracker.last_flags_json = flags
    session.flush()
    append_audit(session, actor=user.username,
                 action="ai_governance.breach_check",
                 entity=f"agent:{key}",
                 payload={"agent": key, "drift_status": tracker.last_status,
                          "flags": len(flags),
                          "consecutive_breaches":
                              tracker.consecutive_breaches,
                          "actions": actions})
    session.commit()
    return {"agentKey": key, "driftStatus": tracker.last_status,
            "flags": flags,
            "consecutiveBreaches": tracker.consecutive_breaches,
            "actionsTaken": actions}


@router.post("/agents/{key}/zero-tolerance")
async def zero_tolerance(key: str, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(_require_validate)):
    """A confirmed zero-tolerance occurrence (§4.3 / §9.4 rule 3):
    immediate per-agent suspension — the agent is disabled, its
    current approved statement is suspended, and a SafetyEvent is
    raised, all in one audited act."""
    body = await _json_body(request)
    description = body.get("description")
    if not isinstance(description, str) or not description.strip():
        raise HTTPException(status_code=422,
                            detail="'description' is required")
    actions: list[str] = []
    if _disable_agent(session, key, user.username,
                      "ai_governance.zero_tolerance_suspend",
                      {"reason": description.strip(),
                       "run_id": body.get("runId")}):
        actions.append("agent-disabled")
    suspended_id = None
    for row in _statements_for(session, key):
        if row.status == "approved":
            row.status = "suspended"
            row.suspension_reason = \
                f"zero-tolerance occurrence: {description.strip()}"
            row.updated_at = _now()
            suspended_id = row.statement_id
            session.flush()
            append_audit(session, actor=user.username,
                         action="ai_governance.statement.suspend",
                         entity=f"statement:{row.statement_id}",
                         payload={"statement_id": row.statement_id,
                                  "version": row.version, "agent": key,
                                  "reason": row.suspension_reason,
                                  "zero_tolerance": True})
            actions.append(f"statement-suspended:{row.statement_id}")
            break
    run_ref = f" Run: {body['runId']}." if body.get("runId") else ""
    event = raise_agent_safety_event(
        session, reporter=user.username,
        harm_level=_valid_harm(session, body.get("harmLevel")),
        description=(f"Zero-tolerance occurrence for agent '{key}': "
                     f"{description.strip()}.{run_ref}"))
    actions.append(f"safety-event:{event.event_id}")
    session.commit()
    return {"agentKey": key, "eventId": event.event_id,
            "statementSuspended": suspended_id, "actionsTaken": actions}


# ---------------------------------------------------------------------------
# Per-run incident reports (AI-G9) + incident listing
# ---------------------------------------------------------------------------

@router.post("/runs/{run_id}/report", status_code=201)
async def report_run(run_id: str, request: Request,
                     session: Session = Depends(get_db),
                     user: User = Depends(_require_run)):
    """Report one agent run's output as a safety incident (AI-G9):
    a SafetyEvent in the "ai-agent" category carrying the reporter's
    text plus the run linkage block (run, agent, config version,
    model route, covering statement). Extra categories from
    `alsoCategories` that exist in the published taxonomy each get a
    linked sibling event (multi-category routing, framework §11)."""
    body = await _json_body(request)
    description = body.get("description")
    if not isinstance(description, str) or not description.strip():
        raise HTTPException(status_code=422,
                            detail="'description' is required")
    run = session.scalars(select(AgentRun).where(
        AgentRun.run_id == run_id)).first()
    if run is None:
        raise HTTPException(status_code=404,
                            detail=f"Agent run '{run_id}' not found")
    covering = None
    for row in _statements_for(session, run.agent_key):
        if row.agent_config_version == run.agent_version:
            covering = row
            break
    covering_id = covering.statement_id if covering is not None else "none"
    linkage = (f"[Agent run linkage] run: {run.run_id}; agent: "
               f"{run.agent_key}; config version: {run.agent_version}; "
               f"model route: {run.model or '(none)'}; statement: "
               f"{covering_id}")
    harm = _valid_harm(session, body.get("harmLevel"))
    event = raise_agent_safety_event(
        session, reporter=user.username, harm_level=harm,
        patient_ref=run.patient_ref,
        description=f"{description.strip()}\n\n{linkage}")
    linked: list[str] = []
    ops = _ops_payload(session)
    taxonomy = ops.get("safetyEventCategories") or []
    rca = harm in (ops.get("rcaHarmLevels") or [])
    also = body.get("alsoCategories")
    if isinstance(also, list):
        for category in also:
            if not isinstance(category, str) \
                    or category not in taxonomy \
                    or category == event.category:
                continue
            sibling = SafetyEvent(
                event_id=str(uuid.uuid4()), reporter=user.username,
                patient_ref=run.patient_ref, category=category,
                harm_level=harm,
                description=(f"{description.strip()}\n\nLinked to "
                             f"primary ai-agent event {event.event_id}. "
                             f"{linkage}"),
                confidential=False, status="reported",
                rca_required=rca, rca_status="open" if rca else None)
            session.add(sibling)
            session.flush()
            append_audit(session, actor=user.username,
                         action="ai_governance.safety_event.raise",
                         entity=f"safety-event/{sibling.event_id}",
                         payload={"category": category,
                                  "harm_level": harm,
                                  "linked_to": event.event_id})
            linked.append(sibling.event_id)
    session.commit()
    return {"eventId": event.event_id, "linkedEventIds": linked}


@router.get("/incidents")
def list_incidents(session: Session = Depends(get_db),
                   user: User = Depends(_require_read)):
    rows = session.scalars(
        select(SafetyEvent).where(SafetyEvent.category == "ai-agent")
        .order_by(SafetyEvent.id.desc()).limit(100)).all()
    items = [{"eventId": r.event_id, "id": r.event_id,
              "reporter": r.reporter, "patientRef": r.patient_ref,
              "category": r.category, "harmLevel": r.harm_level,
              "description": r.description, "status": r.status,
              "rcaRequired": r.rca_required, "rcaStatus": r.rca_status,
              "createdAt": _iso(r.created_at)} for r in rows]
    return {"items": items, "total": len(items)}
