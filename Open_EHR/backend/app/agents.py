"""Studio-managed agents — Phase 10, Track A.

An agent is governed Studio configuration (kind=agent, app.configstore):
model route, system prompt, tool allow-list, input modalities — with the
Studio lifecycle: created as a DRAFT, published versions immutable. On
top of the config lifecycle, running is a separate operational step,
mirroring the module-pack lifecycle (approve -> attach -> enable):
publish approves the definition, and only an explicitly ENABLED agent
(AgentEnablement row, flip audited) may run.

Runs go to the ADK runtime service (backend/adk — Google ADK + LiteLLM,
its own environment) over HTTP. Honest-off at every layer:
- no published version -> the agent does not exist (404);
- published but not enabled -> 409;
- runtime unreachable -> 503 "agent runtime unavailable";
- runtime has no model for the agent -> the runtime's own 503 is
  surfaced verbatim: no model configured, NO answer fabricated.

Every successful run is recorded (AgentRun: agent, published version,
actor, sha256 of the input, the draft output) and hash-chain audited
(action `agents.run`). Agents draft, humans act: the output is a draft
and a human records its disposition (accepted / rejected) through the
disposition endpoint — an agent never writes clinical data itself; its
one tool calls this API with the INVOKING USER's token, allow-listed
per agent, so it can see only what that user could see.
"""
import hashlib
import os
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import append_audit
from .configstore import (list_config_versions, published_config,
                          published_configs)
from .models import (AgentEnablement, AgentRun, ConfigStatus, StudioConfig,
                     User)
from .security import get_db, require_capability

router = APIRouter(prefix="/agents", tags=["agents"])

RUNTIME_TIMEOUT_S = 130.0


def runtime_url() -> str:
    return os.environ.get("ADK_RUNTIME_URL", "http://127.0.0.1:8100").rstrip("/")


def api_base_url() -> str:
    return os.environ.get("OPENCARE_API_BASE", "http://127.0.0.1:8000").rstrip("/")


# ---------------------------------------------------------------------------
# Runtime transport (module-level so tests can substitute it)
# ---------------------------------------------------------------------------

def _post_run(payload: dict) -> tuple[int, dict]:
    """POST {agent, prompt, ...} to the ADK runtime. Returns
    (status, body). Raises httpx.HTTPError on transport failure.
    trust_env=False: the runtime URL is operator configuration on the
    local network — ambient proxy env vars must never reroute (or
    break) the call."""
    with httpx.Client(trust_env=False, timeout=RUNTIME_TIMEOUT_S) as client:
        response = client.post(f"{runtime_url()}/run", json=payload)
    try:
        body = response.json()
    except ValueError:
        body = {"error": response.text[:500]}
    return response.status_code, body if isinstance(body, dict) else {"error": str(body)}


def _get_runtime_health() -> dict | None:
    try:
        with httpx.Client(trust_env=False, timeout=3.0) as client:
            response = client.get(f"{runtime_url()}/health")
        if response.status_code == 200:
            return response.json()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Enablement (the operational half of the lifecycle)
# ---------------------------------------------------------------------------

def _enablement(session: Session, key: str) -> AgentEnablement | None:
    return session.scalars(select(AgentEnablement).where(
        AgentEnablement.agent_key == key)).first()


def _is_enabled(session: Session, key: str) -> bool:
    row = _enablement(session, key)
    return bool(row and row.enabled)


def runtime_refusal(session: Session, key: str) -> str | None:
    """C-AI runtime gate (AI-G4): the reason this agent may not run
    right now, or None when governance allows the run. Lazily imports
    app.ai_governance (W1's module; the tree's inline-import pattern)
    and consults its runtime_gate — expiry of the validation record
    or statement, suspension, or an active kill switch all refuse
    with the gate's own composed reason, surfaced verbatim."""
    from . import ai_governance
    gate = ai_governance.runtime_gate(session, key)
    if isinstance(gate, dict) and not gate.get("allowed", True):
        return str(gate.get("reason")
                   or f"Agent '{key}' is blocked by AI governance")
    return None


def _statement_author(ss: dict) -> str | None:
    """Best-effort extraction of the current statement's author from
    an ai_governance.statement_status result (top-level or nested
    under 'statement')."""
    if not isinstance(ss, dict):
        return None
    for source in (ss, ss.get("statement")):
        if isinstance(source, dict):
            for field in ("author", "created_by", "createdBy"):
                value = source.get(field)
                if isinstance(value, str) and value:
                    return value
    return None


def _agent_out(session: Session, cfg: StudioConfig) -> dict:
    payload = cfg.payload or {}
    model = payload.get("model") or ""
    return {
        "key": cfg.key, "version": cfg.version, "status": cfg.status.value,
        "description": payload.get("description"),
        "model": model or None,
        "model_configured": bool(model),
        "tools": payload.get("tools") or [],
        "api_allowlist": payload.get("apiAllowlist") or [],
        "modalities": payload.get("modalities") or {"text": True},
        "enabled": _is_enabled(session, cfg.key),
        "risk_tier": payload.get("riskTier")
        if payload.get("riskTier") in ("T1", "T2", "T3") else "T2",
    }


@router.get("")
def list_agents(session: Session = Depends(get_db),
                user: User = Depends(require_capability("agents.view"))):
    """The runnable agent roster: every PUBLISHED agent config with its
    enablement state and whether it carries a model route. Drafts are
    designer data (the Studio config API serves them to studio.manage
    holders); they never appear here and can never run."""
    agents = [_agent_out(session, cfg)
              for cfg in published_configs(session, "agent")]
    runtime = _get_runtime_health()
    return {"agents": agents,
            "runtime": {"url": runtime_url(),
                        "reachable": runtime is not None,
                        "model_configured": bool(runtime and runtime.get("modelConfigured")),
                        "model": runtime.get("model") if runtime else None}}


@router.post("/{key}/enable")
def enable_agent(key: str, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("studio.manage"))):
    """Enable a published agent (the step after publish in the agent
    lifecycle: draft -> publish/approve -> enable). Enabling an agent
    with no published version -> 409: there is nothing approved to turn
    on. Every flip is hash-chain audited."""
    cfg = published_config(session, "agent", key)
    if cfg is None:
        raise HTTPException(
            status_code=409,
            detail=f"Agent '{key}' has no published version; publish a "
                   "definition before enabling it")
    # Phase 16 (BD-41): the validation gate — an agent whose published
    # integration/agent-validation config requires validation cannot be
    # enabled without a CURRENT validation record for this exact
    # published version + model route (app.agent_validation). Inline
    # import: agent_validation imports app.agents (circular otherwise).
    from .agent_validation import validation_status
    _vs = validation_status(session, key)
    if _vs["required"] and _vs["status"] != "current":
        raise HTTPException(
            status_code=409,
            detail=f"Agent '{key}' cannot be enabled without a current "
                   f"validation record (status: {_vs['status']}): "
                   f"{_vs['reason']}")
    # C-AI (AI-G2/AI-G3): the statement gate + four-eyes. No current
    # approved validation statement -> no enablement (framework §2.2),
    # when the published agent-validation config requires one.
    from . import ai_governance, four_eyes
    from .agent_validation import _validation_config
    _stmt_author = None
    if _validation_config(session).get("requireStatementForEnable", True):
        _ss = ai_governance.statement_status(session, key)
        _stmt_author = _statement_author(_ss)
        if _stmt_author is None:
            # statement_status carries no author; look the statement
            # row up in ai_governance's register for the four-eyes set.
            try:
                _rows = session.scalars(
                    select(ai_governance.AiValidationStatement).where(
                        ai_governance.AiValidationStatement.agent_key == key)
                    .order_by(ai_governance.AiValidationStatement.id.desc())
                ).all()
                _approved = [r for r in _rows if r.status == "approved"]
                if _approved:
                    _stmt_author = _approved[0].author
                elif _rows:
                    _stmt_author = _rows[0].author
            except Exception:
                _stmt_author = None
        if _ss["status"] != "current":
            raise HTTPException(
                status_code=409,
                detail=f"Agent '{key}' cannot be enabled without a "
                       f"current validation statement (statement "
                       f"status: {_ss['status']}): "
                       f"{_ss.get('reason') or 'no approved statement'}")
    # Four-eyes for T2/T3: the enabler may be neither the published
    # config's author nor the statement's author.
    _tier = four_eyes.agent_tier_of(cfg.payload)
    if _tier in four_eyes.TIER_GATED_KINDS["agent"]:
        if _stmt_author is None:
            try:
                _stmt_author = _statement_author(
                    ai_governance.statement_status(session, key))
            except Exception:
                _stmt_author = None  # at minimum the config author gates
        four_eyes.enforce(
            session, actor=user.username,
            authors={cfg.created_by, _stmt_author},
            action="agents.enable", entity=f"agent:{key}@v{cfg.version}",
            detail=f"enable agent '{key}' (risk tier {_tier})")
    row = _enablement(session, key)
    if row is None:
        row = AgentEnablement(agent_key=key, enabled=False)
        session.add(row)
    row.enabled = True
    row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="agents.enable",
                 entity=f"agent:{key}@v{cfg.version}",
                 payload={"agent": key, "version": cfg.version})
    session.commit()
    return _agent_out(session, cfg)


@router.post("/{key}/disable")
def disable_agent(key: str, session: Session = Depends(get_db),
                  user: User = Depends(require_capability("studio.manage"))):
    cfg = published_config(session, "agent", key)
    if cfg is None:
        raise HTTPException(status_code=404,
                            detail=f"No published agent config for '{key}'")
    row = _enablement(session, key)
    if row is None:
        row = AgentEnablement(agent_key=key, enabled=False)
        session.add(row)
    row.enabled = False
    row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="agents.disable",
                 entity=f"agent:{key}@v{cfg.version}",
                 payload={"agent": key, "version": cfg.version})
    session.commit()
    return _agent_out(session, cfg)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def _run_out(run: AgentRun) -> dict:
    return {"run_id": run.run_id, "agent": run.agent_key,
            "version": run.agent_version, "actor": run.actor,
            "patient_ref": run.patient_ref, "input_hash": run.input_hash,
            "output": run.output, "model": run.model,
            "disposition": run.disposition,
            "disposition_by": run.disposition_by,
            "disposition_at": (run.disposition_at.isoformat()
                               if run.disposition_at else None),
            "created_at": run.created_at.isoformat() if run.created_at else None}


@router.post("/{key}/run", status_code=201)
async def run_agent(key: str, request: Request,
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability("agents.run"))):
    """Run one ENABLED agent once. The output is a DRAFT, recorded on
    an AgentRun row + the hash-chained audit (agent, version, input
    hash, output) — nothing clinical is written by the agent itself."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=422, detail="Run requires a non-empty 'prompt'")
    cfg = published_config(session, "agent", key)
    if cfg is None:
        raise HTTPException(status_code=404,
                            detail=f"No published agent config for '{key}'")
    if not _is_enabled(session, key):
        raise HTTPException(
            status_code=409,
            detail=f"Agent '{key}' is published but not enabled; an "
                   "operator enables it in System Studio before it can run")
    # C-AI runtime gate (AI-G4): an enabled agent whose validation
    # record or statement has since expired (or which is suspended /
    # under an active kill switch) refuses here, naming the reason.
    _refusal = runtime_refusal(session, key)
    if _refusal:
        raise HTTPException(status_code=409, detail=_refusal)
    payload = dict(cfg.payload or {})
    modalities = payload.get("modalities") or {}
    for field, modality in (("imageBase64", "image"), ("audioBase64", "audio")):
        if body.get(field) and not modalities.get(modality):
            raise HTTPException(
                status_code=422,
                detail=f"'{field}' was provided but agent '{key}' does not "
                       f"enable {modality} input in its published config")

    runtime_payload = {
        "agent": {**payload, "key": key},
        "prompt": prompt,
        "apiBaseUrl": api_base_url(),
        "apiToken": request.headers.get("Authorization", "").removeprefix("Bearer ").strip(),
    }
    for field in ("imageBase64", "imageMime", "audioBase64", "audioMime"):
        if body.get(field):
            runtime_payload[field] = body[field]
    try:
        status, result = _post_run(runtime_payload)
    except httpx.HTTPError:
        raise HTTPException(
            status_code=503,
            detail=(f"Agent runtime unavailable at {runtime_url()} — start "
                    "the ADK service (backend/adk) or set ADK_RUNTIME_URL. "
                    "No answer was generated."))
    if status == 503:
        raise HTTPException(status_code=503,
                            detail=result.get("error") or "Agent runtime unavailable")
    if status != 200:
        raise HTTPException(status_code=status if status in (400, 422) else 502,
                            detail=result.get("error") or f"Agent runtime returned {status}")

    output = str(result.get("output") or "")
    input_hash = hashlib.sha256(
        (prompt + str(body.get("imageBase64") or "")
         + str(body.get("audioBase64") or "")).encode("utf-8")).hexdigest()
    run = AgentRun(run_id=str(uuid.uuid4()), agent_key=key,
                   agent_version=cfg.version, actor=user.username,
                   patient_ref=body.get("patient_ref"), input_hash=input_hash,
                   output=output, model=result.get("model"))
    session.add(run)
    session.flush()
    append_audit(session, actor=user.username, action="agents.run",
                 entity=f"agent:{key}@v{cfg.version}",
                 payload={"agent": key, "version": cfg.version,
                          "run_id": run.run_id, "input_hash": input_hash,
                          "patient": body.get("patient_ref"),
                          "model": result.get("model"),
                          "output": output,
                          "disposition": "pending"})
    session.commit()
    return _run_out(run)


@router.get("/runs")
def list_runs(agent: str | None = Query(default=None),
              limit: int = Query(default=50, ge=1, le=200),
              session: Session = Depends(get_db),
              user: User = Depends(require_capability("agents.view"))):
    stmt = select(AgentRun).order_by(AgentRun.id.desc()).limit(limit)
    if agent:
        stmt = stmt.where(AgentRun.agent_key == agent)
    return [_run_out(r) for r in session.scalars(stmt).all()]


@router.post("/runs/{run_id}/disposition")
async def set_disposition(run_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("agents.run"))):
    """Record the human disposition of a run's draft output —
    accepted or rejected. The agent's part ended at the draft; this
    is the human acting, and it is audited like every other step."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    disposition = (body or {}).get("disposition")
    if disposition not in ("accepted", "rejected"):
        raise HTTPException(
            status_code=422,
            detail="'disposition' must be 'accepted' or 'rejected'")
    run = session.scalars(select(AgentRun).where(AgentRun.run_id == run_id)).first()
    if run is None:
        raise HTTPException(status_code=404, detail=f"Agent run {run_id} not found")
    if run.disposition != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Run {run_id} already has disposition '{run.disposition}'")
    run.disposition = disposition
    run.disposition_by = user.username
    run.disposition_at = datetime.now(timezone.utc)
    session.flush()
    append_audit(session, actor=user.username, action="agents.disposition",
                 entity=f"agent:{run.agent_key}@v{run.agent_version}",
                 payload={"run_id": run.run_id, "agent": run.agent_key,
                          "disposition": disposition})
    session.commit()
    return _run_out(run)


# ---------------------------------------------------------------------------
# Seeded agents — DRAFT v1 at startup (never self-published, never
# enabled): a deploying site reviews, sets a model route, publishes and
# enables them through the ordinary governance, or ignores them.
# ---------------------------------------------------------------------------

def default_agent_payloads() -> dict[str, dict]:
    return {
        "note-coding-assistant": {
            "description": ("Drafts a progress note from encounter context and "
                            "suggests ICD-10/CPT codes for HIM review. "
                            "Draft only — a clinician authors and signs "
                            "the real note; a coder assigns the real codes."),
            "riskTier": "T2",
            "model": "",
            "instructions": (
                "You are the OpenCare note and coding assistant. Given the "
                "encounter context in the prompt, draft a concise progress "
                "note (subjective, objective, assessment, plan) using ONLY "
                "facts present in the input — never invent findings, values "
                "or history. Then list suggested ICD-10 and CPT codes with "
                "a one-line justification each, marked as suggestions for "
                "human review. If the input lacks the facts for a section, "
                "say so instead of filling it in."),
            "tools": ["opencare_api_call"],
            "apiAllowlist": ["/fhir", "/notes", "/orders"],
            "modalities": {"text": True, "image": False, "audio": False},
            "temperature": 0.2,
            "maxSteps": 6,
        },
        "lab-result-summariser": {
            "description": ("Summarises a patient's released lab results "
                            "into a short clinician-facing digest: what is "
                            "abnormal, what changed, what needs follow-up. "
                            "Draft only."),
            "riskTier": "T2",
            "model": "",
            "instructions": (
                "You are the OpenCare lab result summariser. Summarise the "
                "released laboratory results in the prompt for the treating "
                "clinician: group by panel, call out abnormal and critical "
                "values with their reference ranges, note deltas the input "
                "shows, and end with a follow-up watch-list. Use ONLY the "
                "results given — never invent values, ranges or prior "
                "results. This is a draft summary for a human to verify "
                "against the chart."),
            "tools": ["opencare_api_call"],
            "apiAllowlist": ["/lab", "/fhir"],
            "modalities": {"text": True, "image": False, "audio": False},
            "temperature": 0.2,
            "maxSteps": 6,
        },
        "pharmacy-dur-reviewer": {
            "description": ("Phase 11 (Track A/C): reviews a patient's open "
                            "medication orders for drug-utilisation-review "
                            "signals — recorded-allergy conflicts, renal "
                            "function against the configured threshold, "
                            "medication-monitoring gaps, duplicate therapy "
                            "— and drafts a pharmacist-facing review note. "
                            "Draft only: the pharmacist verifies, adjusts "
                            "or rejects; the agent never verifies an "
                            "order, never dispenses, and drug-interaction "
                            "checking stays inert until a licensed "
                            "knowledge source is attached (it must say "
                            "so, never guess at pairs)."),
            "riskTier": "T2",
            "model": "",
            "instructions": (
                "You are the OpenCare pharmacy DUR reviewer. Given the "
                "medication orders and chart facts in the prompt, draft "
                "a drug-utilisation-review note for the verifying "
                "pharmacist: for each order, state the signals you can "
                "support FROM THE INPUT ONLY (allergy conflicts, renal "
                "function vs the stated threshold, monitoring results "
                "present or missing, duplicate therapy), each with the "
                "fact it rests on. Never invent values, results or "
                "history. Do NOT assess drug-drug interactions: no "
                "interaction knowledge source is attached to this "
                "system, so state that interaction checking was not "
                "performed rather than implying a pair is safe. End "
                "with a recommended action per flagged order, marked "
                "as a draft recommendation for the pharmacist's own "
                "judgement."),
            "tools": ["opencare_api_call"],
            "apiAllowlist": ["/pharmacy", "/fhir", "/orders"],
            "modalities": {"text": True, "image": False, "audio": False},
            "temperature": 0.2,
            "maxSteps": 6,
        },
    }


def _seed_draft(session: Session, key: str, payload: dict) -> None:
    if list_config_versions(session, "agent", key):
        return
    row = StudioConfig(kind="agent", key=key, version=1,
                       status=ConfigStatus.draft, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:agent/{key}@v1",
                 payload={"kind": "agent", "key": key, "version": 1,
                          "seeded_default": True})


def ensure_agent_defaults(session: Session) -> None:
    for key, payload in default_agent_payloads().items():
        _seed_draft(session, key, payload)
    session.commit()
