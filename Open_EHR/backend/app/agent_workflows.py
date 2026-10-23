"""Supervised agent workflows — Phase 16, WS-C1 (Scopes B + D).

An `agentflow` is governed Studio configuration (kind=agentflow,
registered centrally in app.configstore by the coordinator): an
ORDERED list of steps. Only two step types exist:

- `agent`  {agentKey} — run one published + ENABLED Studio agent
  through the app.agents runtime substrate (imported, never forked:
  enablement via `agents._is_enabled`, transport via
  `agents._post_run`). The step's output is recorded as an ordinary
  AgentRun, so the existing human disposition flow
  (POST /agents/runs/{id}/disposition) governs it exactly like a
  standalone run.
- `review` {role, instructions} — a HUMAN reviewer step. The run
  parks in `awaiting-review`; a reviewer holding the declared role
  (or an admin) approves (the reviewed text passes through), edits
  (the edited text becomes the step output), or rejects — and a
  rejection TERMINATES the workflow: no downstream step ever runs.

NO AUTONOMOUS CLINICAL ACTION: there is deliberately no step type
that writes clinical data — no `write`, `order`, `file`, `post` or
any other effect step exists in the vocabulary, and any definition
naming one is refused with 422 both by POST /agent-workflows/validate
and at run start, before any run record is created. The engine's
only effects are AgentRun drafts (which a human dispositions),
workflow run/step records, and hash-chained audit rows
(`agentflow.run` / `agentflow.step` / `agentflow.review`, plus the
substrate's own `agents.run` per agent step). Agents draft, humans
dispose — a workflow never signs, files, orders or writes anything.

Honest-off, inherited from app.agents: if an agent step's runtime is
unreachable or has no model, the workflow run is recorded as
`failed` with the runtime's honest reason verbatim in
`failure_reason`, the step is marked failed, NO AgentRun is created
and NO output is fabricated. A published-but-not-enabled agent (or
one with no published version) fails the run the same recorded,
honest way — the transport is never even called.
"""
import hashlib
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (DateTime, Integer, JSON, String, Text, select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import agents as agents_runtime
from .audit import append_audit
from .configstore import (list_config_versions, published_config,
                          published_configs)
from .db import Base
from .models import AgentRun, ConfigStatus, StudioConfig, User, utcnow
from .security import get_db, require_capability

router = APIRouter(prefix="/agent-workflows", tags=["agent-workflows"])

AGENTFLOW_KIND = "agentflow"
STEP_TYPES = ("agent", "review")
RUN_STATUSES = ("running", "awaiting-review", "completed", "failed",
                "terminated")


# ---------------------------------------------------------------------------
# This module's own tables
# ---------------------------------------------------------------------------

class AgentWorkflowRun(Base):
    """One execution of one published agentflow definition. The row
    is the reviewable record of the whole chain: status, the input
    (and its sha256), the final output when the chain completes, or
    the honest failure_reason when it does not."""
    __tablename__ = "agent_workflow_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(120), unique=True,
                                        nullable=False, index=True)
    flow_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                          index=True)
    flow_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False,
                                        default="running", index=True)
    patient_ref: Mapped[str | None] = mapped_column(String(200),
                                                    nullable=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False,
                                       default="")
    input_text: Mapped[str] = mapped_column(Text, nullable=False,
                                            default="")
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text,
                                                       nullable=True)
    current_step: Mapped[int] = mapped_column(Integer, nullable=False,
                                              default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


class AgentWorkflowStep(Base):
    """One step of one workflow run, created (pending) for EVERY step
    of the definition at run start — so a terminated run shows its
    downstream steps as `skipped`, never silently absent. An agent
    step links its AgentRun via agent_run_id; a review step records
    the decision, the reviewer and the note."""
    __tablename__ = "agent_workflow_steps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                        index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(20), nullable=False)
    agent_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                           default="")
    role: Mapped[str] = mapped_column(String(60), nullable=False,
                                      default="")
    instructions: Mapped[str] = mapped_column(Text, nullable=False,
                                              default="")
    status: Mapped[str] = mapped_column(String(30), nullable=False,
                                        default="pending", index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(120),
                                                     nullable=True)
    input_text: Mapped[str] = mapped_column(Text, nullable=False,
                                            default="")
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_decision: Mapped[str | None] = mapped_column(String(20),
                                                        nullable=True)
    review_note: Mapped[str] = mapped_column(Text, nullable=False,
                                             default="")
    reviewed_by: Mapped[str | None] = mapped_column(String(120),
                                                    nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False,
                                         default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


# ---------------------------------------------------------------------------
# Payload validation — the agentflow shape. This is ALSO the shape
# contract handed to the coordinator for the kind validator in
# app.configstore (see the integration note): steps ONLY of type
# `agent` {agentKey} or `review` {role, instructions}; anything else
# is an error. The same check runs in this module's validate endpoint
# and at run start.
# ---------------------------------------------------------------------------

def agentflow_payload_errors(payload) -> list[str]:
    """Structural errors in an agentflow payload ([] = valid)."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["agentflow payload must be a JSON object"]
    if "description" in payload and not isinstance(payload["description"], str):
        errors.append("'description' must be a string")
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("agentflow config requires a non-empty 'steps' list")
        return errors
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"steps[{i}] must be an object")
            continue
        step_type = step.get("type")
        if step_type not in STEP_TYPES:
            errors.append(
                f"steps[{i}].type must be one of {list(STEP_TYPES)} — "
                f"got '{step_type}'; no other step type exists (a "
                "workflow can never write clinical data)")
            continue
        if step_type == "agent":
            agent_key = step.get("agentKey") or step.get("agent_key")
            if not isinstance(agent_key, str) or not agent_key.strip():
                errors.append(f"steps[{i}].agentKey is required for an "
                              "'agent' step")
        else:  # review
            role = step.get("role")
            if not isinstance(role, str) or not role.strip():
                errors.append(f"steps[{i}].role is required for a "
                              "'review' step")
            instructions = step.get("instructions")
            if not isinstance(instructions, str) or not instructions.strip():
                errors.append(f"steps[{i}].instructions is required for "
                              "a 'review' step")
    return errors


def validate_agentflow_payload(payload) -> None:
    """Raise 422 with every structural error found (endpoint use)."""
    errors = agentflow_payload_errors(payload)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))


def _step_agent_key(step: dict) -> str:
    return str(step.get("agentKey") or step.get("agent_key") or "")


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------

def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _step_out(step: AgentWorkflowStep) -> dict:
    return {"index": step.step_index, "type": step.step_type,
            "agent_key": step.agent_key or None, "role": step.role or None,
            "instructions": step.instructions or None,
            "status": step.status, "agent_run_id": step.agent_run_id,
            "input": step.input_text, "output": step.output,
            "review_decision": step.review_decision,
            "review_note": step.review_note or None,
            "reviewed_by": step.reviewed_by,
            "reviewed_at": (_aware(step.reviewed_at).isoformat()
                            if step.reviewed_at else None)}


def _run_out(session: Session, run: AgentWorkflowRun) -> dict:
    steps = session.scalars(
        select(AgentWorkflowStep).where(AgentWorkflowStep.run_id == run.run_id)
        .order_by(AgentWorkflowStep.step_index.asc())).all()
    return {"run_id": run.run_id, "key": run.flow_key,
            "version": run.flow_version, "status": run.status,
            "patient_ref": run.patient_ref, "actor": run.actor,
            "input": run.input_text, "input_hash": run.input_hash,
            "output": run.output, "failure_reason": run.failure_reason,
            "current_step": run.current_step,
            "steps": [_step_out(s) for s in steps],
            "created_at": (_aware(run.created_at).isoformat()
                           if run.created_at else None),
            "updated_at": (_aware(run.updated_at).isoformat()
                           if run.updated_at else None)}


def _flow_out(cfg: StudioConfig) -> dict:
    payload = cfg.payload or {}
    return {"key": cfg.key, "version": cfg.version,
            "status": cfg.status.value,
            "description": payload.get("description"),
            "steps": payload.get("steps") or [],
            "valid": not agentflow_payload_errors(payload)}


def _run_row(session: Session, run_id: str) -> AgentWorkflowRun:
    run = session.scalars(select(AgentWorkflowRun).where(
        AgentWorkflowRun.run_id == run_id)).first()
    if run is None:
        raise HTTPException(status_code=404,
                            detail=f"Agent workflow run {run_id} not found")
    return run


def _steps(session: Session, run_id: str) -> list[AgentWorkflowStep]:
    return list(session.scalars(
        select(AgentWorkflowStep).where(AgentWorkflowStep.run_id == run_id)
        .order_by(AgentWorkflowStep.step_index.asc())).all())


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


# ---------------------------------------------------------------------------
# The run engine. Advances a run from `start_index` with `current_text`
# as the working text until it completes, parks at a review step, or
# fails honestly. Mutates + audits in the caller's transaction.
# ---------------------------------------------------------------------------

def _fail_run(session: Session, run: AgentWorkflowRun,
              step: AgentWorkflowStep, reason: str, actor: str) -> None:
    step.status = "failed"
    step.detail = {**(step.detail or {}), "failure_reason": reason}
    for later in _steps(session, run.run_id):
        if later.step_index > step.step_index and later.status == "pending":
            later.status = "skipped"
    run.status = "failed"
    run.failure_reason = reason
    run.output = None
    session.flush()
    append_audit(session, actor=actor, action="agentflow.step",
                 entity=f"agentflow:{run.flow_key}@v{run.flow_version}",
                 payload={"run_id": run.run_id, "step": step.step_index,
                          "type": step.step_type,
                          "agent": step.agent_key or None,
                          "status": "failed", "reason": reason})
    append_audit(session, actor=actor, action="agentflow.run",
                 entity=f"agentflow:{run.flow_key}@v{run.flow_version}",
                 payload={"run_id": run.run_id, "status": "failed",
                          "reason": reason,
                          "patient": run.patient_ref})


def _run_agent_step(session: Session, run: AgentWorkflowRun,
                    step: AgentWorkflowStep, prompt: str, actor: str,
                    api_token: str) -> str | None:
    """Execute ONE agent step through the app.agents substrate.
    Returns the agent's draft output, or None when the run has been
    failed honestly (reason recorded on the run by _fail_run)."""
    agent_key = step.agent_key
    cfg = published_config(session, "agent", agent_key)
    if cfg is None:
        _fail_run(session, run, step,
                  f"Agent '{agent_key}' has no published version; "
                  "the workflow cannot run it", actor)
        return None
    if not agents_runtime._is_enabled(session, agent_key):
        _fail_run(session, run, step,
                  f"Agent '{agent_key}' is published but not enabled; "
                  "an operator enables it in System Studio before a "
                  "workflow can run it", actor)
        return None
    # C-AI runtime gate (AI-G4): expired record/statement, suspension
    # or an active kill switch fails the step honestly, naming the
    # governance reason.
    _refusal = agents_runtime.runtime_refusal(session, agent_key)
    if _refusal:
        _fail_run(session, run, step, _refusal, actor)
        return None
    payload = dict(cfg.payload or {})
    runtime_payload = {
        "agent": {**payload, "key": agent_key},
        "prompt": prompt,
        "apiBaseUrl": agents_runtime.api_base_url(),
        "apiToken": api_token,
    }
    try:
        status, result = agents_runtime._post_run(runtime_payload)
    except httpx.HTTPError:
        _fail_run(session, run, step,
                  f"Agent runtime unavailable at "
                  f"{agents_runtime.runtime_url()} — start the ADK "
                  "service (backend/adk) or set ADK_RUNTIME_URL. "
                  "No answer was generated.", actor)
        return None
    if status == 503:
        _fail_run(session, run, step,
                  str(result.get("error") or "Agent runtime unavailable"),
                  actor)
        return None
    if status != 200:
        _fail_run(session, run, step,
                  str(result.get("error")
                      or f"Agent runtime returned {status}"), actor)
        return None
    output = str(result.get("output") or "")
    input_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    agent_run = AgentRun(run_id=str(uuid.uuid4()), agent_key=agent_key,
                         agent_version=cfg.version, actor=actor,
                         patient_ref=run.patient_ref,
                         input_hash=input_hash, output=output,
                         model=result.get("model"))
    session.add(agent_run)
    session.flush()
    # The substrate's own audit action, so the run shows in the same
    # chain + disposition worklist as a standalone agent run.
    append_audit(session, actor=actor, action="agents.run",
                 entity=f"agent:{agent_key}@v{cfg.version}",
                 payload={"agent": agent_key, "version": cfg.version,
                          "run_id": agent_run.run_id,
                          "input_hash": input_hash,
                          "patient": run.patient_ref,
                          "model": result.get("model"),
                          "output": output, "disposition": "pending",
                          "via": "agentflow",
                          "workflow_run_id": run.run_id})
    step.status = "completed"
    step.agent_run_id = agent_run.run_id
    step.output = output
    session.flush()
    append_audit(session, actor=actor, action="agentflow.step",
                 entity=f"agentflow:{run.flow_key}@v{run.flow_version}",
                 payload={"run_id": run.run_id, "step": step.step_index,
                          "type": "agent", "agent": agent_key,
                          "agent_run_id": agent_run.run_id,
                          "status": "completed"})
    return output


def _advance(session: Session, run: AgentWorkflowRun, actor: str,
             api_token: str, start_index: int, current_text: str) -> None:
    steps = _steps(session, run.run_id)
    for step in steps[start_index:]:
        run.current_step = step.step_index
        step.input_text = current_text
        if step.step_type == "review":
            step.status = "awaiting-review"
            run.status = "awaiting-review"
            session.flush()
            append_audit(session, actor=actor, action="agentflow.step",
                         entity=f"agentflow:{run.flow_key}"
                                f"@v{run.flow_version}",
                         payload={"run_id": run.run_id,
                                  "step": step.step_index,
                                  "type": "review", "role": step.role,
                                  "status": "awaiting-review"})
            return
        step.status = "running"
        session.flush()
        output = _run_agent_step(session, run, step, current_text,
                                 actor, api_token)
        if output is None:
            return  # run failed honestly; reason already recorded
        current_text = output
    run.status = "completed"
    run.output = current_text
    run.current_step = len(steps)
    session.flush()
    append_audit(session, actor=actor, action="agentflow.run",
                 entity=f"agentflow:{run.flow_key}@v{run.flow_version}",
                 payload={"run_id": run.run_id, "status": "completed",
                          "patient": run.patient_ref,
                          "output": current_text})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.post("/validate")
async def validate_flow(request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(require_capability("agents.run"))):
    """Publish-validation for an agentflow payload (the same check
    the run path applies to a published definition): steps ONLY of
    type `agent` {agentKey} or `review` {role, instructions} —
    anything else is 422, including any step type that would write
    clinical data, because no such step type exists."""
    payload = await _json_body(request)
    validate_agentflow_payload(payload)
    steps = payload.get("steps") or []
    return {"valid": True, "steps": len(steps),
            "step_types": [s.get("type") for s in steps]}


@router.get("/runs")
def list_runs(flow: str | None = Query(default=None),
              limit: int = Query(default=50, ge=1, le=200),
              session: Session = Depends(get_db),
              user: User = Depends(require_capability("agents.view"))):
    stmt = select(AgentWorkflowRun).order_by(
        AgentWorkflowRun.id.desc()).limit(limit)
    if flow:
        stmt = stmt.where(AgentWorkflowRun.flow_key == flow)
    return [_run_out(session, r) for r in session.scalars(stmt).all()]


@router.get("/runs/{run_id}")
def get_run(run_id: str, session: Session = Depends(get_db),
            user: User = Depends(require_capability("agents.view"))):
    return _run_out(session, _run_row(session, run_id))


@router.post("/runs/{run_id}/review")
async def review_run(run_id: str, request: Request,
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability("agents.run"))):
    """The human review gate. approve: the reviewed text passes
    through and the chain resumes. edit: `editedOutput` replaces the
    reviewed text as the step output and the chain resumes with it.
    reject: the workflow TERMINATES — downstream steps are marked
    skipped and never run. The reviewer must hold the role the step
    declares (admins may review any step)."""
    body = await _json_body(request)
    decision = body.get("decision")
    if decision not in ("approve", "reject", "edit"):
        raise HTTPException(
            status_code=422,
            detail="'decision' must be 'approve', 'reject' or 'edit'")
    edited = body.get("editedOutput")
    if edited is None:
        edited = body.get("edited_output")
    if decision == "edit" and (not isinstance(edited, str)
                               or not edited.strip()):
        raise HTTPException(
            status_code=422,
            detail="'editedOutput' is required (non-empty) when "
                   "decision is 'edit'")
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise HTTPException(status_code=422, detail="'note' must be a string")
    run = _run_row(session, run_id)
    if run.status != "awaiting-review":
        raise HTTPException(
            status_code=409,
            detail=f"Run {run_id} is '{run.status}', not awaiting review")
    steps = _steps(session, run.run_id)
    step = next((s for s in steps if s.status == "awaiting-review"), None)
    if step is None:  # pragma: no cover - status/step invariant
        raise HTTPException(status_code=409,
                            detail=f"Run {run_id} has no step awaiting review")
    if user.role.value != step.role and user.role.value != "admin":
        raise HTTPException(
            status_code=403,
            detail=f"This review step requires role '{step.role}'")
    step.review_decision = decision
    step.review_note = note or ""
    step.reviewed_by = user.username
    step.reviewed_at = datetime.now(timezone.utc)
    token = request.headers.get("Authorization", "") \
        .removeprefix("Bearer ").strip()
    if decision == "reject":
        step.status = "rejected"
        step.output = None
        for later in steps:
            if later.step_index > step.step_index and later.status == "pending":
                later.status = "skipped"
        run.status = "terminated"
        run.output = None
        session.flush()
        append_audit(session, actor=user.username, action="agentflow.review",
                     entity=f"agentflow:{run.flow_key}@v{run.flow_version}",
                     payload={"run_id": run.run_id, "step": step.step_index,
                              "decision": "reject", "role": step.role,
                              "note": step.review_note})
        append_audit(session, actor=user.username, action="agentflow.run",
                     entity=f"agentflow:{run.flow_key}@v{run.flow_version}",
                     payload={"run_id": run.run_id, "status": "terminated",
                              "reason": "reviewer rejected the step output",
                              "patient": run.patient_ref})
        session.commit()
        return _run_out(session, run)
    step.output = edited if decision == "edit" else step.input_text
    step.status = "edited" if decision == "edit" else "approved"
    session.flush()
    append_audit(session, actor=user.username, action="agentflow.review",
                 entity=f"agentflow:{run.flow_key}@v{run.flow_version}",
                 payload={"run_id": run.run_id, "step": step.step_index,
                          "decision": decision, "role": step.role,
                          "note": step.review_note,
                          "output": step.output})
    run.status = "running"
    session.flush()
    _advance(session, run, user.username, token,
             step.step_index + 1, step.output)
    session.commit()
    return _run_out(session, run)


@router.get("")
def list_flows(session: Session = Depends(get_db),
               user: User = Depends(require_capability("agents.view"))):
    """Published agentflow definitions (drafts are designer data,
    served by the Studio config API — they never appear here and can
    never run)."""
    return {"workflows": [_flow_out(cfg)
                          for cfg in published_configs(session, AGENTFLOW_KIND)]}


@router.get("/{key}")
def get_flow(key: str, session: Session = Depends(get_db),
             user: User = Depends(require_capability("agents.view"))):
    cfg = published_config(session, AGENTFLOW_KIND, key)
    if cfg is None:
        raise HTTPException(status_code=404,
                            detail=f"No published agentflow config for '{key}'")
    return _flow_out(cfg)


@router.post("/{key}/run", status_code=201)
async def run_flow(key: str, request: Request,
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("agents.run"))):
    """Start one run of a PUBLISHED agentflow. The definition is
    re-validated at run start — a step type outside {agent, review}
    is refused 422 before any run record exists. Agent steps execute
    in order; the run parks at the first review step, completes with
    the final step's output, or fails honestly (status recorded on
    the run, no output fabricated)."""
    body = await _json_body(request)
    input_text = body.get("input")
    if input_text is None:
        input_text = body.get("prompt")
    if not isinstance(input_text, str) or not input_text.strip():
        raise HTTPException(status_code=422,
                            detail="Run requires a non-empty 'input'")
    patient_ref = body.get("patientRef") or body.get("patient_ref")
    if patient_ref is not None and not isinstance(patient_ref, str):
        raise HTTPException(status_code=422,
                            detail="'patientRef' must be a string")
    cfg = published_config(session, AGENTFLOW_KIND, key)
    if cfg is None:
        raise HTTPException(status_code=404,
                            detail=f"No published agentflow config for '{key}'")
    payload = cfg.payload or {}
    validate_agentflow_payload(payload)  # 422 — refused at run start

    run = AgentWorkflowRun(
        run_id=str(uuid.uuid4()), flow_key=key, flow_version=cfg.version,
        status="running", patient_ref=patient_ref,
        actor=user.username, input_text=input_text,
        input_hash=hashlib.sha256(input_text.encode("utf-8")).hexdigest())
    session.add(run)
    session.flush()
    for i, step_def in enumerate(payload.get("steps") or []):
        session.add(AgentWorkflowStep(
            run_id=run.run_id, step_index=i,
            step_type=str(step_def.get("type")),
            agent_key=_step_agent_key(step_def)
            if step_def.get("type") == "agent" else "",
            role=str(step_def.get("role") or "")
            if step_def.get("type") == "review" else "",
            instructions=str(step_def.get("instructions") or "")
            if step_def.get("type") == "review" else "",
            status="pending"))
    session.flush()
    append_audit(session, actor=user.username, action="agentflow.run",
                 entity=f"agentflow:{key}@v{cfg.version}",
                 payload={"run_id": run.run_id, "status": "started",
                          "patient": patient_ref,
                          "input_hash": run.input_hash,
                          "steps": len(payload.get("steps") or [])})
    token = request.headers.get("Authorization", "") \
        .removeprefix("Bearer ").strip()
    _advance(session, run, user.username, token, 0, input_text)
    session.commit()
    return _run_out(session, run)


# ---------------------------------------------------------------------------
# Seeds — Scope D agent drafts + the supervised-note-drafting flow.
# Agents ride app.agents._seed_draft (kind=agent, DRAFT v1, empty model
# route — never self-published, never enabled); the agentflow seed is
# a DRAFT StudioConfig of the new kind, seeded the same insert-if-
# absent way. A deploying site reviews, sets model routes, publishes
# and enables through the ordinary governance, or ignores them.
# ---------------------------------------------------------------------------

def default_agent_workflow_agent_payloads() -> dict[str, dict]:
    return {
        "ambient-scribe": {
            "description": ("Phase 16 (Scope D): drafts an encounter note "
                            "from ambient encounter audio (transcribed by "
                            "the runtime) and the encounter context. "
                            "Draft only — the clinician reviews, edits "
                            "and signs the real note; designed to run "
                            "inside supervised agentflows behind a human "
                            "review step."),
            "model": "",
            "instructions": (
                "You are the OpenCare ambient scribe. Given the encounter "
                "audio transcript and context in the prompt, draft a "
                "concise encounter note (subjective, objective, "
                "assessment, plan) using ONLY facts actually stated in "
                "the transcript or context — never invent findings, "
                "values, speakers or history, and attribute statements "
                "to the speaker the transcript names. Where the audio is "
                "unclear or a section has no supporting facts, write "
                "'not captured in the recording' instead of filling it "
                "in. This is a draft for the treating clinician to "
                "review and edit before signing."),
            "tools": ["opencare_api_call"],
            "apiAllowlist": ["/fhir", "/notes"],
            "modalities": {"text": True, "image": False, "audio": True},
            "temperature": 0.2,
            "maxSteps": 6,
        },
        "chart-search": {
            "description": ("Phase 16 (Scope D): answers questions about "
                            "one patient's chart in natural language, "
                            "with a source citation for every claim. "
                            "Draft only — answers are reading aids for a "
                            "human who verifies against the chart."),
            "model": "",
            "instructions": (
                "You are the OpenCare chart search assistant. Answer "
                "the question in the prompt using ONLY the chart facts "
                "provided or retrievable through your tools. Cite the "
                "source of every claim inline as the resource reference "
                "it came from (for example Patient/pat-1, "
                "Observation/<id>, MedicationRequest/<id>) — a claim "
                "without a citation is not allowed. If the chart does "
                "not contain the answer, say exactly that; never infer, "
                "extrapolate or invent values, dates or history. End "
                "with a 'Sources' list of the references you cited."),
            "tools": ["opencare_api_call"],
            "apiAllowlist": ["/fhir", "/notes", "/lab"],
            "modalities": {"text": True, "image": False, "audio": False},
            "temperature": 0.2,
            "maxSteps": 6,
        },
        "deep-research": {
            "description": ("Phase 16 (Scope D): drafts a literature-"
                            "synthesis brief on a clinical question — "
                            "longer multi-step research runs (maxSteps "
                            "raised) that gather, compare and summarise "
                            "sources. Draft only — a clinician judges the "
                            "evidence; nothing here is a recommendation "
                            "to act."),
            "model": "",
            "instructions": (
                "You are the OpenCare deep research assistant. Given "
                "the clinical question in the prompt, draft a structured "
                "literature-synthesis brief: restate the question, "
                "summarise what each source in the input actually "
                "found (with its citation), note where sources agree "
                "or conflict, grade the overall evidence quality "
                "honestly, and list the open questions the sources do "
                "not settle. Use ONLY the sources provided or "
                "retrievable through your tools — never invent "
                "studies, effect sizes or citations; if the input has "
                "no usable sources, say so instead of synthesising. "
                "This is a draft brief for human review, not clinical "
                "guidance."),
            "tools": ["opencare_api_call"],
            "apiAllowlist": ["/fhir", "/reports"],
            "modalities": {"text": True, "image": False, "audio": False},
            "temperature": 0.2,
            "maxSteps": 16,
        },
    }


def default_agentflow_payloads() -> dict[str, dict]:
    return {
        "supervised-note-drafting": {
            "description": ("Phase 16 (Scope B/D seed): the ambient "
                            "scribe drafts an encounter note, a "
                            "clinician reviews/edits it, and the note "
                            "coding assistant then drafts coding "
                            "suggestions from the REVIEWED text. Every "
                            "output stays a draft for human "
                            "disposition — the chain writes nothing to "
                            "the chart."),
            "steps": [
                {"type": "agent", "agentKey": "ambient-scribe"},
                {"type": "review", "role": "clinician",
                 "instructions": ("Review the scribe's draft note "
                                  "against the encounter: correct any "
                                  "fact the recording does not support, "
                                  "then approve, edit, or reject the "
                                  "draft.")},
                {"type": "agent", "agentKey": "note-coding-assistant"},
            ],
        },
    }


def _seed_agentflow_draft(session: Session, key: str,
                          payload: dict) -> None:
    if list_config_versions(session, AGENTFLOW_KIND, key):
        return
    row = StudioConfig(kind=AGENTFLOW_KIND, key=key, version=1,
                       status=ConfigStatus.draft, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:{AGENTFLOW_KIND}/{key}@v1",
                 payload={"kind": AGENTFLOW_KIND, "key": key, "version": 1,
                          "seeded_default": True})


def ensure_agent_workflows_defaults(session: Session) -> None:
    """Seed the three Scope D agent DRAFTS (via app.agents._seed_draft)
    and the DRAFT supervised-note-drafting agentflow. Idempotent —
    existing versions are the truth and are never duplicated."""
    for key, payload in default_agent_workflow_agent_payloads().items():
        agents_runtime._seed_draft(session, key, payload)
    for key, payload in default_agentflow_payloads().items():
        _seed_agentflow_draft(session, key, payload)
    session.commit()
