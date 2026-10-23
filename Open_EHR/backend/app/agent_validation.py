"""Phase 16, WS-C3 / BD-41 — AI model validation harness.

The gap this closes: before this module, an agent could be published
and ENABLED with no validation evidence at all. BD-41 requires, for
every agent/model version:

- **Governed evaluation datasets** — case sets are DATA (this module's
  tables), content-hashed and hash-chain audited at creation.
- **Performance + fairness measurement** — every run scores per-case
  pass/fail, an overall pass rate and PER-SLICE pass rates, and flags
  slice disparity beyond the threshold in the published
  `integration/agent-validation` config.
- **A ValidationRecord per (agent, published config version, model
  route)** — minted by a completed evaluation run, expiring after
  `validationValidityDays` from the same published config.
  `validation_status(session, agent_key)` is the function the
  coordinator calls from `agents.enable_agent` so that NO agent may
  be enabled without a current validation record when the published
  config's `requireValidationForEnable` is true.
- **A PCCP-pattern change-control record** for model/agent version
  changes (agent, from/to version, intended change, validation plan,
  result ref, approver). `GET /agent-validation/exceptions` lists
  published agent versions with NO change-control record.
- **Drift monitoring** — recent `AgentRun` statistics (count, mean
  output length, disposition reject rate) compared against the
  baseline captured on the agent's ValidationRecord, flagging
  beyond-threshold movement (thresholds from the same published
  config).

Run modes:

- `recorded` — cases carry `recordedOutput`; the harness scores those
  outputs against each case's checks. This proves the harness
  mechanics (scoring, slices, minting, expiry) with NO model — the
  recorded outputs are synthetic fixture text, never presented as
  live model performance.
- `live` — executes the agent through `app.agents`' runtime client
  (`agents._post_run`) once per case, using the PUBLISHED agent
  config. Live validation deliberately does NOT require operational
  enablement (validation precedes enablement — requiring enablement
  would deadlock the gate) and does NOT write `AgentRun` rows: the
  executions are evaluation traffic, recorded on the EvaluationRun,
  not clinical draft traffic for human disposition. Honest-off: no
  model / runtime unreachable -> the runtime's 503 is surfaced
  verbatim and NOTHING is recorded (no run, no record).

Seeded baselines: the two Phase 10 seeded agents
(`note-coding-assistant`, `lab-result-summariser`) each get — in
`ensure_agent_validation_defaults` — a baseline change-control
record (v0 -> v1, grandfathering the original seed) and a
recorded-mode ValidationRecord minted from a small baseline dataset,
so their existing publish -> enable flows keep working with the
enablement gate ON. These baselines validate the HARNESS against
recorded outputs for the agents' v1 (empty) model route; they are
not evidence of live model performance, and they expire like any
other record.

This module is the evidence source for the §170.315(b)(11) DSI
register entry in app.compliance (decision-support transparency):
validation, change-control and drift actions are all hash-chain
audited as `agent_validation.*`.

Capability gates: writes ride the new `agents.validate` capability
(coordinator wires its role defaults centrally); until those defaults
land, `studio.manage` holders may also write (the governance role
that owns agent definitions). Reads ride `agents.view` /
`agents.validate`.
"""
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (DateTime, Float, Integer, JSON, String, Text,
                        select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import agents as agents_module
from .audit import append_audit
from .configstore import (list_config_versions, published_config,
                          published_configs, role_allowed)
from .db import Base
from .models import AgentRun, ConfigStatus, StudioConfig, User, utcnow
from .security import get_current_user, get_db

router = APIRouter(prefix="/agent-validation", tags=["agent-validation"])

VALIDATION_KEY = "agent-validation"   # integration/agent-validation
CHECK_TYPES = ("contains", "not-contains", "exact", "tool-used")
RUN_MODES = ("recorded", "live")
BASELINE_DATASET_KEY = "baseline-seeded-agents"
BASELINE_AGENT_KEYS = ("note-coding-assistant", "lab-result-summariser")
RECENT_RUN_WINDOW = 50


# ---------------------------------------------------------------------------
# This module's own tables
# ---------------------------------------------------------------------------

class EvaluationDataset(Base):
    """One governed evaluation dataset: an ordered list of JSON cases
    {id, prompt, parts?, recordedOutput?, checks, slices}. Datasets
    are data — the canonical cases JSON is sha256-hashed at creation
    (`cases_hash`) so a run's evidence names exactly what was scored."""
    __tablename__ = "agent_validation_datasets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_key: Mapped[str] = mapped_column(String(120), unique=True,
                                             nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cases: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    cases_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


class EvaluationRun(Base):
    """One completed evaluation run of one dataset against one agent
    (published version + model route): per-case results, overall and
    per-slice pass rates, and disparity flags vs the published
    thresholds. Failed/honest-off live attempts are NEVER recorded
    here (no run exists for an execution that never happened)."""
    __tablename__ = "agent_validation_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_key: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    dataset_id: Mapped[int] = mapped_column(Integer, nullable=False,
                                            index=True)
    dataset_key: Mapped[str] = mapped_column(String(120), nullable=False)
    agent_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                           index=True)
    agent_config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_route: Mapped[str] = mapped_column(String(200), nullable=False,
                                             default="")
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed_cases: Mapped[int] = mapped_column(Integer, nullable=False,
                                              default=0)
    pass_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    slice_metrics: Mapped[dict] = mapped_column(JSON, nullable=False,
                                                default=dict)
    disparity_flags: Mapped[list] = mapped_column(JSON, nullable=False,
                                                  default=list)
    case_results: Mapped[list] = mapped_column(JSON, nullable=False,
                                               default=list)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class ValidationRecord(Base):
    """The enablement evidence for one (agent, config version, model
    route): minted by a completed EvaluationRun, expiring
    `validationValidityDays` after issue. `baseline_json` captures the
    agent's live AgentRun statistics at mint time — the reference the
    drift report compares later live behaviour against."""
    __tablename__ = "agent_validation_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_key: Mapped[str] = mapped_column(String(120), unique=True,
                                            nullable=False, index=True)
    agent_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                           index=True)
    agent_config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_route: Mapped[str] = mapped_column(String(200), nullable=False,
                                             default="")
    run_key: Mapped[str] = mapped_column(String(120), nullable=False)
    dataset_key: Mapped[str] = mapped_column(String(120), nullable=False)
    pass_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    baseline_json: Mapped[dict] = mapped_column(JSON, nullable=False,
                                                default=dict)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)
    issued_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                           default="")


class ChangeControlRecord(Base):
    """One PCCP-pattern change-control record: a planned agent/model
    version change, its intended change, the validation plan, the
    result reference once executed, and its approver. A published
    agent version with no record naming it as `to_version` is an
    exception (see GET /exceptions)."""
    __tablename__ = "agent_validation_change_control"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_key: Mapped[str] = mapped_column(String(120), unique=True,
                                            nullable=False, index=True)
    agent_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                           index=True)
    from_version: Mapped[int] = mapped_column(Integer, nullable=False)
    to_version: Mapped[int] = mapped_column(Integer, nullable=False)
    intended_change: Mapped[str] = mapped_column(Text, nullable=False,
                                                 default="")
    validation_plan: Mapped[str] = mapped_column(Text, nullable=False,
                                                  default="")
    result_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                            default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="proposed", index=True)
    approver: Mapped[str] = mapped_column(String(120), nullable=False,
                                          default="")
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Capability gates (agents.validate; studio.manage until its defaults land)
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


# ---------------------------------------------------------------------------
# Published config: integration/agent-validation
# ---------------------------------------------------------------------------

def default_agent_validation_payload() -> dict:
    return {
        "requireValidationForEnable": True,
        "validationValidityDays": 180,
        "sliceDisparityThreshold": 0.2,
        "driftRejectRateThreshold": 0.2,
        "driftOutputLengthChangeThreshold": 0.5,
        # C-AI keys (AI-G1/G2/G6): statement gate at enable, per-tier
        # evidence thresholds (framework §3.2), and the drift-breach
        # auto-suspension policy (§9.4).
        "requireStatementForEnable": True,
        "tierRequirements": {
            "T1": {"minOverallPass": 0.90, "minSliceFloor": 0.85,
                   "maxSliceDisparity": 0.20, "statementValidityMonths": 12,
                   "approvals": ["governance_approver"],
                   "monitoringCadence": "monthly"},
            "T2": {"minOverallPass": 0.95, "minSliceFloor": 0.90,
                   "maxSliceDisparity": 0.10, "statementValidityMonths": 6,
                   "approvals": ["clinical_reviewer", "governance_approver"],
                   "monitoringCadence": "monthly"},
            "T3": {"minOverallPass": 0.98, "minSliceFloor": 0.95,
                   "maxSliceDisparity": 0.05, "statementValidityMonths": 6,
                   "approvals": ["clinical_reviewer", "governance_approver",
                                 "clinical_owner"],
                   "monitoringCadence": "continuous"},
        },
        "driftBreach": {"consecutiveChecks": 2, "autoSuspendTiers": ["T3"]},
        "notes": ("BD-41 validation governance. requireValidationForEnable: "
                  "agents.enable_agent refuses enablement without a current "
                  "ValidationRecord for the published version + model route. "
                  "validationValidityDays: a record expires this many days "
                  "after its evaluation run. sliceDisparityThreshold: a run "
                  "flags slice disparity when max-min per-slice pass rate "
                  "exceeds it. Drift thresholds compare recent AgentRun "
                  "reject rate (absolute delta) and mean output length "
                  "(relative change) against the validation baseline. "
                  "C-AI keys: requireStatementForEnable (enablement also "
                  "requires a current approved validation statement), "
                  "tierRequirements (per-tier pass/floor/disparity "
                  "thresholds, statement validity and approval kinds from "
                  "the framework §3.2 matrix), driftBreach (consecutive "
                  "breaching drift checks before action; tiers in "
                  "autoSuspendTiers are auto-suspended on breach)."),
    }


def _validation_config(session: Session) -> dict:
    cfg = published_config(session, "integration", VALIDATION_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    merged = default_agent_validation_payload()
    if isinstance(payload, dict):
        merged.update(payload)
    return merged


def _validity_days(session: Session) -> int:
    try:
        days = int(_validation_config(session).get("validationValidityDays"))
    except (TypeError, ValueError):
        days = 180
    return days if days > 0 else 180


def _threshold(session: Session, key: str, fallback: float) -> float:
    try:
        return float(_validation_config(session).get(key))
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------------------
# Helpers
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


def _cases_hash(cases: list) -> str:
    canonical = json.dumps(cases, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_cases(cases) -> list:
    if not isinstance(cases, list) or not cases:
        raise HTTPException(status_code=422,
                            detail="'cases' must be a non-empty list")
    seen: set[str] = set()
    normalised = []
    for idx, case in enumerate(cases):
        if not isinstance(case, dict):
            raise HTTPException(status_code=422,
                                detail=f"Case #{idx} must be a JSON object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise HTTPException(
                status_code=422,
                detail=f"Case #{idx} requires a non-empty string 'id'")
        case_id = case_id.strip()
        if case_id in seen:
            raise HTTPException(status_code=422,
                                detail=f"Duplicate case id '{case_id}'")
        seen.add(case_id)
        prompt = case.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(
                status_code=422,
                detail=f"Case '{case_id}' requires a non-empty 'prompt'")
        checks = case.get("checks") or []
        if not isinstance(checks, list):
            raise HTTPException(
                status_code=422,
                detail=f"Case '{case_id}': 'checks' must be a list")
        for check in checks:
            if not isinstance(check, dict) \
                    or check.get("type") not in CHECK_TYPES:
                raise HTTPException(
                    status_code=422,
                    detail=f"Case '{case_id}': each check 'type' must be "
                           f"one of {list(CHECK_TYPES)}")
            if not isinstance(check.get("value"), str):
                raise HTTPException(
                    status_code=422,
                    detail=f"Case '{case_id}': each check requires a "
                           "string 'value'")
        slices = case.get("slices") or []
        if not isinstance(slices, list) \
                or any(not isinstance(s, str) for s in slices):
            raise HTTPException(
                status_code=422,
                detail=f"Case '{case_id}': 'slices' must be a list of "
                       "strings")
        recorded = case.get("recordedOutput")
        if recorded is not None and not isinstance(recorded, (str, dict)):
            raise HTTPException(
                status_code=422,
                detail=f"Case '{case_id}': 'recordedOutput' must be a "
                       "string or an object {output, tools}")
        normalised.append(case)
    return normalised


def _output_text_and_tools(case: dict,
                           live_output: str | None = None,
                           live_tools: list | None = None) -> tuple[str, list]:
    tools: list = []
    for field in ("recordedTools", "toolsUsed"):
        value = case.get(field)
        if isinstance(value, list):
            tools.extend(str(t) for t in value)
    if live_output is not None:
        return live_output, list(live_tools or tools)
    recorded = case.get("recordedOutput")
    if isinstance(recorded, dict):
        for field in ("tools", "toolsUsed"):
            value = recorded.get(field)
            if isinstance(value, list):
                tools.extend(str(t) for t in value)
        text = recorded.get("output") or recorded.get("text") or ""
        return str(text), tools
    if isinstance(recorded, str):
        return recorded, tools
    return "", tools


def _check_passes(check: dict, text: str, tools: list) -> bool:
    ctype, value = check["type"], check["value"]
    if ctype == "contains":
        return value in text
    if ctype == "not-contains":
        return value not in text
    if ctype == "exact":
        return text.strip() == value.strip()
    if ctype == "tool-used":
        return value in tools
    return False


def _score_cases(cases: list, live_outputs: dict | None = None) -> tuple[list, dict, list, float]:
    """Score cases -> (case_results, slice_metrics, disparity_flags,
    pass_rate). live_outputs maps case id -> (output, tools); without
    it, each case's recordedOutput is scored."""
    cfg_threshold = None  # filled by caller via flags recompute if needed
    results = []
    slices: dict[str, dict] = {}
    passed = 0
    for case in cases:
        if live_outputs is not None:
            output, tools = live_outputs[case["id"].strip()]
            text, tools = _output_text_and_tools(case, output, tools)
            scored_against = "live-output"
        else:
            text, tools = _output_text_and_tools(case)
            scored_against = "recorded-output"
        check_results = [{"type": c["type"], "value": c["value"],
                          "passed": _check_passes(c, text, tools)}
                         for c in case.get("checks") or []]
        ok = all(c["passed"] for c in check_results)
        if case.get("recordedOutput") is None and live_outputs is None:
            ok = False
            scored_against = "missing-recorded-output"
        passed += 1 if ok else 0
        results.append({"caseId": case["id"].strip(), "passed": ok,
                        "checks": check_results,
                        "slices": case.get("slices") or [],
                        "scoredAgainst": scored_against,
                        "output": text})
        for label in case.get("slices") or []:
            bucket = slices.setdefault(label, {"total": 0, "passed": 0})
            bucket["total"] += 1
            bucket["passed"] += 1 if ok else 0
    slice_metrics = {}
    for label, bucket in sorted(slices.items()):
        rate = bucket["passed"] / bucket["total"] if bucket["total"] else 0.0
        slice_metrics[label] = {"total": bucket["total"],
                                "passed": bucket["passed"],
                                "passRate": round(rate, 4)}
    total = len(cases)
    pass_rate = round(passed / total, 4) if total else 0.0
    return results, slice_metrics, [], pass_rate


def _disparity_flags(slice_metrics: dict, overall_rate: float,
                     threshold: float) -> list:
    flags = []
    if len(slice_metrics) >= 2:
        rates = {k: v["passRate"] for k, v in slice_metrics.items()}
        hi = max(rates, key=rates.get)
        lo = min(rates, key=rates.get)
        disparity = round(rates[hi] - rates[lo], 4)
        if disparity > threshold:
            flags.append({"type": "slice-disparity", "disparity": disparity,
                          "threshold": threshold, "highestSlice": hi,
                          "lowestSlice": lo,
                          "detail": (f"Slice pass rates span {rates[lo]:.2f} "
                                     f"({lo}) to {rates[hi]:.2f} ({hi}); "
                                     f"disparity {disparity:.2f} exceeds "
                                     f"the {threshold:.2f} threshold")})
    for label, metrics in slice_metrics.items():
        gap = round(overall_rate - metrics["passRate"], 4)
        if gap > threshold:
            flags.append({"type": "slice-below-overall", "slice": label,
                          "slicePassRate": metrics["passRate"],
                          "overallPassRate": overall_rate, "gap": gap,
                          "threshold": threshold,
                          "detail": (f"Slice '{label}' passes at "
                                     f"{metrics['passRate']:.2f}, {gap:.2f} "
                                     f"below the overall rate")})
    return flags


def _agent_run_stats(session: Session, agent_key: str,
                     limit: int | None = None) -> dict:
    stmt = select(AgentRun).where(AgentRun.agent_key == agent_key) \
        .order_by(AgentRun.id.desc())
    if limit:
        stmt = stmt.limit(limit)
    rows = session.scalars(stmt).all()
    count = len(rows)
    mean_len = round(sum(len(r.output or "") for r in rows) / count, 2) \
        if count else 0.0
    dispositioned = [r for r in rows
                     if r.disposition in ("accepted", "rejected")]
    rejected = sum(1 for r in dispositioned if r.disposition == "rejected")
    reject_rate = round(rejected / len(dispositioned), 4) \
        if dispositioned else 0.0
    return {"count": count, "meanOutputLength": mean_len,
            "dispositionedCount": len(dispositioned),
            "rejectedCount": rejected, "rejectRate": reject_rate}


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------

def _dataset_out(row: EvaluationDataset, include_cases: bool = True) -> dict:
    out = {"id": row.id, "key": row.dataset_key, "datasetKey": row.dataset_key,
           "name": row.name, "description": row.description,
           "caseCount": row.case_count, "casesHash": row.cases_hash,
           "createdBy": row.created_by,
           "createdAt": _iso(row.created_at)}
    if include_cases:
        out["cases"] = row.cases or []
    return out


def _tier_assessment(session: Session, agent_key: str, pass_rate: float,
                     slice_metrics: dict) -> dict:
    """C-AI (AI-G1): score one run against its agent's risk-tier
    thresholds from the governed tierRequirements config. The tier
    comes from the agent's PUBLISHED payload riskTier (absent/invalid
    -> T2, per four_eyes.agent_tier_of)."""
    from .four_eyes import agent_tier_of
    cfg = published_config(session, "agent", agent_key)
    tier = agent_tier_of(cfg.payload if cfg is not None else None)
    requirements = (_validation_config(session).get("tierRequirements")
                    or {})
    req = requirements.get(tier) or {}
    defaults = default_agent_validation_payload()["tierRequirements"][tier]
    thresholds = {
        "minOverallPass": req.get("minOverallPass",
                                  defaults["minOverallPass"]),
        "minSliceFloor": req.get("minSliceFloor", defaults["minSliceFloor"]),
        "maxSliceDisparity": req.get("maxSliceDisparity",
                                     defaults["maxSliceDisparity"]),
    }
    rates = [float(m.get("passRate") or 0.0)
             for m in (slice_metrics or {}).values()]
    slice_floor = min(rates) if rates else float(pass_rate)
    disparity = round(max(rates) - min(rates), 4) if len(rates) >= 2 else 0.0
    meets = (float(pass_rate) >= float(thresholds["minOverallPass"])
             and slice_floor >= float(thresholds["minSliceFloor"])
             and disparity <= float(thresholds["maxSliceDisparity"]))
    detail = (f"Tier {tier}: overall pass {float(pass_rate):.2f} vs "
              f">= {float(thresholds['minOverallPass']):.2f}; slice floor "
              f"{slice_floor:.2f} vs >= {float(thresholds['minSliceFloor']):.2f}; "
              f"slice disparity {disparity:.2f} vs "
              f"<= {float(thresholds['maxSliceDisparity']):.2f} — "
              f"{'meets' if meets else 'does NOT meet'} the {tier} thresholds")
    return {"tier": tier, "thresholds": thresholds, "meets": meets,
            "detail": detail}


def _run_out(row: EvaluationRun, session: Session | None = None) -> dict:
    assessment = getattr(row, "tier_assessment", None)
    if assessment is None and session is not None:
        assessment = _tier_assessment(session, row.agent_key, row.pass_rate,
                                      row.slice_metrics or {})
    return {"id": row.id, "runKey": row.run_key, "runId": row.run_key,
            "datasetId": row.dataset_id, "datasetKey": row.dataset_key,
            "agentKey": row.agent_key,
            "agentConfigVersion": row.agent_config_version,
            "modelRoute": row.model_route, "mode": row.mode,
            "totalCases": row.total_cases, "passedCases": row.passed_cases,
            "passRate": row.pass_rate, "sliceMetrics": row.slice_metrics or {},
            "disparityFlags": row.disparity_flags or [],
            "caseResults": row.case_results or [], "actor": row.actor,
            "tierAssessment": assessment,
            "createdAt": _iso(row.created_at)}


def _record_out(row: ValidationRecord) -> dict:
    return {"id": row.id, "recordKey": row.record_key,
            "agentKey": row.agent_key,
            "agentConfigVersion": row.agent_config_version,
            "modelRoute": row.model_route, "runId": row.run_key,
            "runKey": row.run_key, "datasetKey": row.dataset_key,
            "passRate": row.pass_rate, "baseline": row.baseline_json or {},
            "issuedAt": _iso(row.issued_at),
            "expiresAt": _iso(row.expires_at), "issuedBy": row.issued_by}


def _cc_out(row: ChangeControlRecord) -> dict:
    return {"id": row.id, "recordKey": row.record_key,
            "agentKey": row.agent_key, "fromVersion": row.from_version,
            "toVersion": row.to_version,
            "intendedChange": row.intended_change,
            "validationPlan": row.validation_plan,
            "validationPlanId": row.validation_plan,
            "resultRef": row.result_ref, "status": row.status,
            "approver": row.approver, "createdBy": row.created_by,
            "createdAt": _iso(row.created_at),
            "approvedAt": _iso(row.approved_at)}


# ---------------------------------------------------------------------------
# Validation status — THE enablement-gate function (coordinator hook)
# ---------------------------------------------------------------------------

def validation_status(session: Session, agent_key: str) -> dict:
    """Current validation posture for one agent vs its PUBLISHED config
    version + model route:

    - "current"  — a ValidationRecord exists for the published
      version + model route and has not expired;
    - "expired"  — such a record exists but its validity window
      (`validationValidityDays`) has passed;
    - "missing"  — no record for the published version + model route
      (never validated, or validated for another version/model), or
      the agent has no published version at all.

    The record matching is version- AND model-exact: a model-route
    change invalidates prior evidence, which is precisely the PCCP
    change the change-control records govern."""
    config = _validation_config(session)
    required = bool(config.get("requireValidationForEnable", True))
    cfg = published_config(session, "agent", agent_key)
    published_version = cfg.version if cfg is not None else None
    model_route = str((cfg.payload or {}).get("model") or "") \
        if cfg is not None else None
    base = {"agentKey": agent_key, "required": required,
            "publishedVersion": published_version,
            "modelRoute": model_route, "record": None}
    if cfg is None:
        return {**base, "status": "missing",
                "reason": f"Agent '{agent_key}' has no published version; "
                          "there is nothing validated to enable"}
    records = session.scalars(
        select(ValidationRecord)
        .where(ValidationRecord.agent_key == agent_key)
        .order_by(ValidationRecord.id.desc())).all()
    matching = [r for r in records
                if r.agent_config_version == published_version
                and r.model_route == model_route]
    if not matching:
        return {**base, "status": "missing",
                "reason": (f"No validation record for agent '{agent_key}' "
                           f"published v{published_version} with model "
                           f"route '{model_route or '(none)'}'")}
    latest = matching[0]
    base["record"] = _record_out(latest)
    if _aware(latest.expires_at) and _aware(latest.expires_at) > _now():
        return {**base, "status": "current",
                "reason": (f"Validation record {latest.record_key} is "
                           f"current until {_iso(latest.expires_at)}")}
    return {**base, "status": "expired",
            "reason": (f"Validation record {latest.record_key} expired at "
                       f"{_iso(latest.expires_at)}; re-run an evaluation "
                       "dataset to renew it")}


# ---------------------------------------------------------------------------
# Run execution (shared by the endpoint and the baseline seeder)
# ---------------------------------------------------------------------------

def _execute_run(session: Session, dataset: EvaluationDataset,
                 agent_key: str, version: int, model_route: str,
                 mode: str, actor: str,
                 live_outputs: dict | None = None) -> tuple[EvaluationRun, ValidationRecord]:
    cases = list(dataset.cases or [])
    results, slice_metrics, _flags, pass_rate = _score_cases(
        cases, live_outputs=live_outputs)
    threshold = _threshold(session, "sliceDisparityThreshold", 0.2)
    flags = _disparity_flags(slice_metrics, pass_rate, threshold)
    passed = sum(1 for r in results if r["passed"])
    run = EvaluationRun(
        run_key=str(uuid.uuid4()), dataset_id=dataset.id,
        dataset_key=dataset.dataset_key, agent_key=agent_key,
        agent_config_version=version, model_route=model_route, mode=mode,
        total_cases=len(cases), passed_cases=passed, pass_rate=pass_rate,
        slice_metrics=slice_metrics, disparity_flags=flags,
        case_results=results, actor=actor)
    session.add(run)
    session.flush()
    # C-AI: the run's tier assessment (transient — recomputed by
    # _run_out from the stored metrics whenever a session is at hand).
    run.tier_assessment = _tier_assessment(session, agent_key, pass_rate,
                                           slice_metrics)
    issued = _now()
    record = ValidationRecord(
        record_key=str(uuid.uuid4()), agent_key=agent_key,
        agent_config_version=version, model_route=model_route,
        run_key=run.run_key, dataset_key=dataset.dataset_key,
        pass_rate=pass_rate,
        baseline_json=_agent_run_stats(session, agent_key),
        issued_at=issued,
        expires_at=issued + timedelta(days=_validity_days(session)),
        issued_by=actor)
    session.add(record)
    session.flush()
    append_audit(session, actor=actor, action="agent_validation.run",
                 entity=f"agent:{agent_key}@v{version}",
                 payload={"run_key": run.run_key, "dataset": dataset.dataset_key,
                          "agent": agent_key, "version": version,
                          "model_route": model_route, "mode": mode,
                          "pass_rate": pass_rate,
                          "total_cases": len(cases),
                          "disparity_flags": flags})
    append_audit(session, actor=actor,
                 action="agent_validation.validation.issue",
                 entity=f"agent:{agent_key}@v{version}",
                 payload={"record_key": record.record_key,
                          "run_key": run.run_key, "pass_rate": pass_rate,
                          "expires_at": _iso(record.expires_at)})
    return run, record


# ---------------------------------------------------------------------------
# Config + dataset endpoints
# ---------------------------------------------------------------------------

@router.get("/config")
def get_config(session: Session = Depends(get_db),
               user: User = Depends(_require_read)):
    cfg = published_config(session, "integration", VALIDATION_KEY)
    return {"config": _validation_config(session),
            "publishedVersion": cfg.version if cfg is not None else None,
            "source": "published" if cfg is not None else "defaults"}


@router.post("/datasets", status_code=201)
async def create_dataset(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(_require_validate)):
    """Create one governed evaluation dataset. The cases are stored
    as data, sha256-hashed (canonical JSON) and hash-chain audited —
    a later run's evidence names this exact case set by hash."""
    body = await _json_body(request)
    cases = _validate_cases(body.get("cases"))
    key = body.get("key") or body.get("datasetKey") or body.get("dataset_key")
    name = str(body.get("name") or body.get("title") or "")
    if not isinstance(key, str) or not key.strip():
        slug = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
        key = slug or f"dataset-{_cases_hash(cases)[:8]}"
    key = key.strip()
    if session.scalars(select(EvaluationDataset).where(
            EvaluationDataset.dataset_key == key)).first() is not None:
        raise HTTPException(status_code=409,
                            detail=f"Evaluation dataset '{key}' already exists")
    row = EvaluationDataset(
        dataset_key=key, name=name,
        description=str(body.get("description") or ""), cases=cases,
        cases_hash=_cases_hash(cases), case_count=len(cases),
        created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="agent_validation.dataset.create",
                 entity=f"EvaluationDataset/{key}",
                 payload={"dataset_key": key, "case_count": len(cases),
                          "cases_hash": row.cases_hash})
    session.commit()
    return _dataset_out(row)


def _dataset_row(session: Session, dataset_id: str) -> EvaluationDataset:
    row = None
    if dataset_id.isdigit():
        row = session.get(EvaluationDataset, int(dataset_id))
    if row is None:
        row = session.scalars(select(EvaluationDataset).where(
            EvaluationDataset.dataset_key == dataset_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Evaluation dataset '{dataset_id}' not found")
    return row


@router.get("/datasets")
def list_datasets(session: Session = Depends(get_db),
                  user: User = Depends(_require_read)):
    rows = session.scalars(select(EvaluationDataset)
                           .order_by(EvaluationDataset.id.asc())).all()
    return {"items": [_dataset_out(r, include_cases=False) for r in rows],
            "total": len(rows)}


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: str, session: Session = Depends(get_db),
                user: User = Depends(_require_read)):
    return _dataset_out(_dataset_row(session, dataset_id))


@router.post("/datasets/{dataset_id}/run", status_code=201)
async def run_dataset(dataset_id: str, request: Request,
                      session: Session = Depends(get_db),
                      user: User = Depends(_require_validate)):
    """Run one dataset against one agent's PUBLISHED version.

    recorded: scores each case's recordedOutput (harness mechanics;
    no model involved). live: executes each case through app.agents'
    runtime client; a no-model/unreachable runtime surfaces its 503
    verbatim and records NOTHING. A completed run mints the
    ValidationRecord that the enablement gate reads."""
    body = await _json_body(request)
    dataset = _dataset_row(session, dataset_id)
    agent_key = body.get("agentKey") or body.get("agent_key")
    if not isinstance(agent_key, str) or not agent_key.strip():
        raise HTTPException(status_code=422, detail="'agentKey' is required")
    agent_key = agent_key.strip()
    mode = body.get("mode") or "recorded"
    if mode not in RUN_MODES:
        raise HTTPException(status_code=422,
                            detail=f"'mode' must be one of {list(RUN_MODES)}")
    cfg = published_config(session, "agent", agent_key)
    if cfg is None:
        raise HTTPException(
            status_code=404,
            detail=f"No published agent config for '{agent_key}'; publish "
                   "a version before validating it")
    payload = dict(cfg.payload or {})
    model_route = str(payload.get("model") or "")
    live_outputs: dict | None = None
    if mode == "live":
        live_outputs = {}
        token = request.headers.get("Authorization", "") \
            .removeprefix("Bearer ").strip()
        for case in dataset.cases or []:
            runtime_payload = {"agent": {**payload, "key": agent_key},
                               "prompt": case["prompt"],
                               "apiBaseUrl": agents_module.api_base_url(),
                               "apiToken": token}
            parts = case.get("parts")
            if isinstance(parts, dict):
                for field in ("imageBase64", "imageMime",
                              "audioBase64", "audioMime"):
                    if parts.get(field):
                        runtime_payload[field] = parts[field]
            elif parts is not None:
                runtime_payload["parts"] = parts
            try:
                status, result = agents_module._post_run(runtime_payload)
            except httpx.HTTPError:
                raise HTTPException(
                    status_code=503,
                    detail=(f"Agent runtime unavailable at "
                            f"{agents_module.runtime_url()} — start the "
                            "ADK service (backend/adk) or set "
                            "ADK_RUNTIME_URL. No answer was generated."))
            if status == 503:
                raise HTTPException(
                    status_code=503,
                    detail=result.get("error") or "Agent runtime unavailable")
            if status != 200:
                raise HTTPException(
                    status_code=status if status in (400, 422) else 502,
                    detail=result.get("error")
                    or f"Agent runtime returned {status}")
            tools = result.get("tools") or result.get("toolsUsed") or []
            live_outputs[case["id"].strip()] = (
                str(result.get("output") or ""),
                [str(t) for t in tools] if isinstance(tools, list) else [])
    run, record = _execute_run(session, dataset, agent_key, cfg.version,
                               model_route, mode, user.username,
                               live_outputs=live_outputs)
    session.commit()
    out = _run_out(run, session)
    out["validationRecord"] = _record_out(record)
    return out


@router.get("/runs")
def list_runs(agent: str | None = Query(default=None),
              session: Session = Depends(get_db),
              user: User = Depends(_require_read)):
    stmt = select(EvaluationRun).order_by(EvaluationRun.id.desc()).limit(200)
    if agent:
        stmt = stmt.where(EvaluationRun.agent_key == agent)
    rows = session.scalars(stmt).all()
    return {"items": [_run_out(r, session) for r in rows], "total": len(rows)}


@router.get("/validations")
def list_validations(agent: str | None = Query(default=None),
                     session: Session = Depends(get_db),
                     user: User = Depends(_require_read)):
    stmt = select(ValidationRecord).order_by(ValidationRecord.id.desc()) \
        .limit(200)
    if agent:
        stmt = stmt.where(ValidationRecord.agent_key == agent)
    rows = session.scalars(stmt).all()
    return {"items": [_record_out(r) for r in rows], "total": len(rows)}


@router.get("/agents/{key}/status")
def get_status(key: str, session: Session = Depends(get_db),
               user: User = Depends(_require_read)):
    return validation_status(session, key)


@router.get("/agents/{key}/drift")
def get_drift(key: str, session: Session = Depends(get_db),
              user: User = Depends(_require_read)):
    """Drift report: recent AgentRun behaviour (last
    RECENT_RUN_WINDOW runs) vs the baseline captured on the agent's
    latest ValidationRecord. Flags reject-rate movement beyond
    `driftRejectRateThreshold` (absolute) and mean-output-length
    movement beyond `driftOutputLengthChangeThreshold` (relative)."""
    record = session.scalars(
        select(ValidationRecord).where(ValidationRecord.agent_key == key)
        .order_by(ValidationRecord.id.desc())).first()
    recent = _agent_run_stats(session, key, limit=RECENT_RUN_WINDOW)
    reject_threshold = _threshold(session, "driftRejectRateThreshold", 0.2)
    length_threshold = _threshold(
        session, "driftOutputLengthChangeThreshold", 0.5)
    thresholds = {"rejectRate": reject_threshold,
                  "meanOutputLengthRelative": length_threshold}
    if record is None:
        return {"agentKey": key, "status": "no-baseline",
                "baseline": None, "recent": recent, "flags": [],
                "thresholds": thresholds,
                "reason": (f"No validation record for '{key}'; there is "
                           "no baseline to drift from")}
    baseline = record.baseline_json or {}
    flags = []
    base_reject = float(baseline.get("rejectRate") or 0.0)
    delta = round(abs(recent["rejectRate"] - base_reject), 4)
    if delta > reject_threshold:
        flags.append({"metric": "rejectRate", "baseline": base_reject,
                      "recent": recent["rejectRate"], "delta": delta,
                      "threshold": reject_threshold,
                      "detail": (f"Disposition reject rate moved from "
                                 f"{base_reject:.2f} at validation to "
                                 f"{recent['rejectRate']:.2f} on recent "
                                 f"runs (delta {delta:.2f} > "
                                 f"{reject_threshold:.2f})")})
    base_len = float(baseline.get("meanOutputLength") or 0.0)
    recent_len = float(recent["meanOutputLength"])
    if base_len > 0:
        rel = round(abs(recent_len - base_len) / base_len, 4)
        if rel > length_threshold:
            flags.append({"metric": "meanOutputLength", "baseline": base_len,
                          "recent": recent_len, "relativeChange": rel,
                          "threshold": length_threshold,
                          "detail": (f"Mean output length moved from "
                                     f"{base_len:.1f} to {recent_len:.1f} "
                                     f"chars ({rel:.0%} vs the "
                                     f"{length_threshold:.0%} threshold)")})
    elif recent_len > 0:
        flags.append({"metric": "meanOutputLength", "baseline": 0.0,
                      "recent": recent_len, "relativeChange": None,
                      "threshold": length_threshold,
                      "detail": ("Validation baseline had no live runs "
                                 "(mean output length 0); recent runs "
                                 f"average {recent_len:.1f} chars — no "
                                 "length baseline exists to compare "
                                 "against")})
    return {"agentKey": key,
            "status": "drift-flagged" if flags else "within-threshold",
            "baseline": baseline, "recent": recent, "flags": flags,
            "thresholds": thresholds,
            "validationRecord": {"recordKey": record.record_key,
                                 "runId": record.run_key,
                                 "issuedAt": _iso(record.issued_at),
                                 "expiresAt": _iso(record.expires_at)}}


# ---------------------------------------------------------------------------
# Change control (PCCP pattern) + exceptions
# ---------------------------------------------------------------------------

@router.post("/change-control", status_code=201)
async def create_change_control(request: Request,
                                session: Session = Depends(get_db),
                                user: User = Depends(_require_validate)):
    body = await _json_body(request)
    agent_key = body.get("agentKey") or body.get("agent_key")
    if not isinstance(agent_key, str) or not agent_key.strip():
        raise HTTPException(status_code=422, detail="'agentKey' is required")
    intended = body.get("intendedChange") or body.get("intended_change")
    if not isinstance(intended, str) or not intended.strip():
        raise HTTPException(status_code=422,
                            detail="'intendedChange' is required")
    versions = {}
    for field, out in (("fromVersion", "from_version"),
                       ("toVersion", "to_version")):
        value = body.get(field, body.get(out))
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(status_code=422,
                                detail=f"'{field}' must be an integer")
        versions[out] = value
    plan = body.get("validationPlanId") or body.get("validationPlan") \
        or body.get("validation_plan") or ""
    row = ChangeControlRecord(
        record_key=str(uuid.uuid4()), agent_key=agent_key.strip(),
        from_version=versions["from_version"],
        to_version=versions["to_version"],
        intended_change=intended.strip(),
        validation_plan=str(plan),
        result_ref=str(body.get("resultRef") or ""),
        status="proposed", created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="agent_validation.change_control.create",
                 entity=f"agent:{row.agent_key}@v{row.to_version}",
                 payload={"record_key": row.record_key,
                          "agent": row.agent_key,
                          "from_version": row.from_version,
                          "to_version": row.to_version,
                          "intended_change": row.intended_change,
                          "validation_plan": row.validation_plan})
    session.commit()
    return _cc_out(row)


@router.get("/change-control")
def list_change_control(agent: str | None = Query(default=None),
                        session: Session = Depends(get_db),
                        user: User = Depends(_require_read)):
    stmt = select(ChangeControlRecord) \
        .order_by(ChangeControlRecord.id.desc()).limit(200)
    if agent:
        stmt = stmt.where(ChangeControlRecord.agent_key == agent)
    rows = session.scalars(stmt).all()
    return {"items": [_cc_out(r) for r in rows], "total": len(rows)}


@router.post("/change-control/{record_id}/approve")
async def approve_change_control(record_id: int, request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(_require_validate)):
    body = {}
    try:
        parsed = await request.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:
        body = {}
    row = session.get(ChangeControlRecord, record_id)
    if row is None:
        # Also accept the record_key as the identifier.
        row = session.scalars(select(ChangeControlRecord).where(
            ChangeControlRecord.record_key == str(record_id))).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Change-control record {record_id} not found")
    if row.status == "approved":
        raise HTTPException(
            status_code=409,
            detail=f"Change-control record {row.record_key} is already approved")
    # C-AI: the approver is the AUTHENTICATED user — a body-supplied
    # "approver" is ignored for identity (only resultRef is honoured
    # from the body) — and four-eyes applies against the record's
    # creator: self-approval is an audited refusal + 403.
    from . import four_eyes
    four_eyes.enforce(
        session, actor=user.username, authors={row.created_by},
        action="agent_validation.change_control.approve",
        entity=f"agent:{row.agent_key}@v{row.to_version}",
        detail=f"approve change-control record {row.record_key} "
               f"for agent '{row.agent_key}'")
    row.status = "approved"
    row.approver = user.username
    row.approved_at = _now()
    if body.get("resultRef"):
        row.result_ref = str(body["resultRef"])
    session.flush()
    append_audit(session, actor=user.username,
                 action="agent_validation.change_control.approve",
                 entity=f"agent:{row.agent_key}@v{row.to_version}",
                 payload={"record_key": row.record_key,
                          "agent": row.agent_key,
                          "to_version": row.to_version,
                          "approver": row.approver,
                          "result_ref": row.result_ref})
    session.commit()
    return _cc_out(row)


@router.get("/exceptions")
def list_exceptions(session: Session = Depends(get_db),
                    user: User = Depends(_require_read)):
    """Every PUBLISHED agent whose current published version has no
    change-control record naming it as to_version — the PCCP gap
    list. Draft-only agents are not exceptions (nothing is live);
    the seeded agents' v1s are covered by their baseline records."""
    items = []
    for cfg in published_configs(session, "agent"):
        covered = session.scalars(select(ChangeControlRecord).where(
            ChangeControlRecord.agent_key == cfg.key,
            ChangeControlRecord.to_version == cfg.version)).first()
        if covered is None:
            items.append({
                "agentKey": cfg.key, "version": cfg.version,
                "modelRoute": str((cfg.payload or {}).get("model") or ""),
                "reason": (f"Published agent '{cfg.key}' v{cfg.version} "
                           "has no change-control record; a version "
                           "change without PCCP change control is an "
                           "exception under BD-41")})
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Seeding — published governance config + grandfathered baselines
# ---------------------------------------------------------------------------

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


def default_baseline_dataset_payload() -> dict:
    return {
        "key": BASELINE_DATASET_KEY,
        "name": "Baseline harness dataset for the seeded agents",
        "description": ("Recorded-mode baseline cases proving the BD-41 "
                        "harness mechanics (scoring, slices, minting) for "
                        "the two Phase 10 seeded agents. The recorded "
                        "outputs are synthetic fixture text — this "
                        "dataset is NOT live model evidence."),
        "cases": [
            {"id": "baseline-draft-summary",
             "prompt": "Summarise the synthetic encounter context as a draft.",
             "recordedOutput": "Draft summary of the synthetic input, "
                               "prepared for human review.",
             "checks": [{"type": "contains", "value": "Draft"},
                        {"type": "not-contains", "value": "signed"}],
             "slices": ["baseline"]},
            {"id": "baseline-follow-up",
             "prompt": "List the follow-up items from the synthetic results.",
             "recordedOutput": "Draft follow-up: verify each value "
                               "against the chart before acting.",
             "checks": [{"type": "contains", "value": "verify"}],
             "slices": ["baseline"]},
        ],
    }


def _ensure_baseline_dataset(session: Session) -> EvaluationDataset:
    row = session.scalars(select(EvaluationDataset).where(
        EvaluationDataset.dataset_key == BASELINE_DATASET_KEY)).first()
    if row is not None:
        return row
    payload = default_baseline_dataset_payload()
    cases = _validate_cases(payload["cases"])
    row = EvaluationDataset(
        dataset_key=BASELINE_DATASET_KEY, name=payload["name"],
        description=payload["description"], cases=cases,
        cases_hash=_cases_hash(cases), case_count=len(cases),
        created_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system",
                 action="agent_validation.dataset.create",
                 entity=f"EvaluationDataset/{BASELINE_DATASET_KEY}",
                 payload={"dataset_key": BASELINE_DATASET_KEY,
                          "case_count": len(cases),
                          "cases_hash": row.cases_hash,
                          "seeded_default": True})
    return row


def ensure_agent_validation_defaults(session: Session) -> None:
    """Seed the published `integration/agent-validation` governance
    config, the baseline dataset, and — for each of the two Phase 10
    seeded agents — a grandfathered baseline change-control record
    (v0 -> its seeded v1) plus a recorded-mode ValidationRecord for
    that version + model route, so publish -> enable keeps working
    with the BD-41 gate ON. Idempotent: existing rows are the truth.

    Ordering: call AFTER app.agents.ensure_agent_defaults (the
    coordinator owns that ordering in main.py)."""
    _seed_published(session, "integration", VALIDATION_KEY,
                    default_agent_validation_payload())
    dataset = _ensure_baseline_dataset(session)
    for agent_key in BASELINE_AGENT_KEYS:
        versions = list_config_versions(session, "agent", agent_key)
        if not versions:
            continue
        v1 = versions[0]
        model_route = str((v1.payload or {}).get("model") or "")
        covered = session.scalars(select(ChangeControlRecord).where(
            ChangeControlRecord.agent_key == agent_key,
            ChangeControlRecord.to_version == v1.version)).first()
        run = None
        record = None
        existing = session.scalars(select(ValidationRecord).where(
            ValidationRecord.agent_key == agent_key,
            ValidationRecord.agent_config_version == v1.version,
            ValidationRecord.model_route == model_route)).first()
        if existing is None:
            run, record = _execute_run(
                session, dataset, agent_key, v1.version, model_route,
                "recorded", "system")
        if covered is None:
            cc = ChangeControlRecord(
                record_key=str(uuid.uuid4()), agent_key=agent_key,
                from_version=0, to_version=v1.version,
                intended_change=(
                    "Grandfathered baseline: the agent's original seeded "
                    "definition (Phase 10 seed). No prior version existed "
                    "to change from; BD-41 change control applies to every "
                    "subsequent version."),
                validation_plan=BASELINE_DATASET_KEY,
                result_ref=(record.record_key if record is not None
                            else (existing.record_key if existing else "")),
                status="approved", approver="system",
                created_by="system", approved_at=_now())
            session.add(cc)
            session.flush()
            append_audit(session, actor="system",
                         action="agent_validation.change_control.create",
                         entity=f"agent:{agent_key}@v{v1.version}",
                         payload={"record_key": cc.record_key,
                                  "agent": agent_key, "from_version": 0,
                                  "to_version": v1.version,
                                  "baseline": True,
                                  "intended_change": cc.intended_change})
            append_audit(session, actor="system",
                         action="agent_validation.change_control.approve",
                         entity=f"agent:{agent_key}@v{v1.version}",
                         payload={"record_key": cc.record_key,
                                  "agent": agent_key,
                                  "to_version": v1.version,
                                  "approver": "system", "baseline": True})
    session.commit()
