"""Phase 16, WS-C1 — supervised agent workflows (Scopes B + D).

An agentflow (kind=agentflow Studio config) is an ordered chain of
ONLY two step types: `agent` (run a published + enabled agent through
the app.agents substrate) and `review` (a human gate). Any other step
type — above all any step that would write clinical data — is refused
422 at validation and at run start: the engine has no autonomous
clinical action. Runs park at review steps; approve resumes, edit
substitutes the reviewed text, reject TERMINATES (downstream agents
never invoked, proven by transport call counts). Honest-off: no model
/ runtime down => the run is recorded `failed` with the honest
reason, no AgentRun, no fabricated output. The ADK transport is
substituted (monkeypatch of app.agents._post_run, the Phase 10
pattern); a live model run is NOT RUN.

Synthetic data only.

C-AI registered adjustment: agent drafts authored by ADMIN are
published by CONFIG (four-eyes publish gate), and enablement runs
the shared tests.conftest.governance_for_enable flow (validation
record + approved statement) with the enable performed by CONFIG.
Workflow agent steps also consult the C-AI runtime gate. Assertions
are unchanged in strength.
"""
import hashlib

import httpx
import pytest
from sqlalchemy import select

from app import agents as agents_module
from app.models import AuditLog, ConfigStatus, StudioConfig
from tests.conftest import (ADMIN, CLINICIAN, CONFIG, VIEWER,
                            governance_for_enable)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _publish_agent(client, key, **overrides):
    payload = {
        "description": "Test agent",
        "model": "ollama_chat/llama3.2:3b",
        "instructions": "Do the task briefly.",
        "tools": ["opencare_api_call"],
        "apiAllowlist": ["/fhir"],
        "modalities": {"text": True, "image": False, "audio": False},
    }
    payload.update(overrides)
    r = client.post(f"/studio/config/agent/{key}", json=payload,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/agent/{key}/versions/{version}/publish",
        headers=CONFIG)  # C-AI four-eyes: author ADMIN may not publish
    assert r.status_code == 200, r.text
    return version


def _enable(client, key):
    """BD-41 + C-AI gates: governance flow, then enable by CONFIG."""
    governance_for_enable(client, key)
    r = client.post(f"/agents/{key}/enable", headers=CONFIG)
    assert r.status_code == 200, r.text


def _publish_flow(client, key, payload):
    """Insert a PUBLISHED agentflow config directly (the kind is
    registered centrally by the coordinator; this module reads
    published Studio config exactly like every other module)."""
    factory = client.app.state.session_factory
    with factory() as session:
        session.add(StudioConfig(
            kind="agentflow", key=key, version=1,
            status=ConfigStatus.published, payload=payload,
            created_by="test", updated_by="test"))
        session.commit()


def _flow(*steps):
    return {"description": "Test flow", "steps": list(steps)}


def _agent_step(key):
    return {"type": "agent", "agentKey": key}


def _review_step(role="clinician"):
    return {"type": "review", "role": role,
            "instructions": "Check the draft before it moves on."}


def _audit_actions(client):
    factory = client.app.state.session_factory
    with factory() as session:
        return [row.action for row in session.scalars(select(AuditLog)).all()]


class _Transport:
    """Substituted ADK transport: records calls, replays outputs."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        output = self.outputs[len(self.calls) - 1]
        return 200, {"output": output,
                     "model": "ollama_chat/llama3.2:3b",
                     "agent": payload["agent"]["key"]}


def _two_agent_chain(client, monkeypatch, outputs=("STEP-ONE-OUTPUT",
                                                   "STEP-TWO-OUTPUT")):
    _publish_agent(client, "chain-a")
    _publish_agent(client, "chain-b")
    _enable(client, "chain-a")  # BD-41 + C-AI gates (see helper)
    _enable(client, "chain-b")  # BD-41 + C-AI gates (see helper)
    transport = _Transport(outputs)
    monkeypatch.setattr(agents_module, "_post_run", transport)
    return transport


# ---------------------------------------------------------------------------
# Step-type refusal — no autonomous clinical action exists
# ---------------------------------------------------------------------------

def test_validate_refuses_write_step(client):
    r = client.post("/agent-workflows/validate", headers=CLINICIAN, json={
        "steps": [{"type": "agent", "agentKey": "chain-a"},
                  {"type": "write", "target": "Observation"}]})
    assert r.status_code == 422
    assert "write" in r.json()["detail"]


def test_validate_accepts_agent_and_review_steps(client):
    r = client.post("/agent-workflows/validate", headers=CLINICIAN,
                    json=_flow(_agent_step("chain-a"), _review_step(),
                               _agent_step("chain-b")))
    assert r.status_code == 200, r.text
    assert r.json() == {"valid": True, "steps": 3,
                        "step_types": ["agent", "review", "agent"]}


def test_run_start_refuses_write_step_and_records_nothing(client):
    _publish_flow(client, "evil-flow", _flow(
        _agent_step("chain-a"),
        {"type": "write", "target": "MedicationRequest"}))
    r = client.post("/agent-workflows/evil-flow/run",
                    json={"input": "go"}, headers=CLINICIAN)
    assert r.status_code == 422
    # Refused BEFORE any run record: no run, no audit trail of a run.
    assert client.get("/agent-workflows/runs", headers=CLINICIAN).json() == []
    assert "agentflow.run" not in _audit_actions(client)


def test_run_unknown_flow_is_404(client):
    r = client.post("/agent-workflows/nope/run",
                    json={"input": "go"}, headers=CLINICIAN)
    assert r.status_code == 404


def test_run_requires_agents_run_capability(client):
    _publish_flow(client, "caps-flow", _flow(_agent_step("chain-a")))
    r = client.post("/agent-workflows/caps-flow/run",
                    json={"input": "go"}, headers=VIEWER)
    assert r.status_code == 403
    r = client.get("/agent-workflows/runs", headers=VIEWER)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Chain execution through the substituted transport
# ---------------------------------------------------------------------------

def test_chain_executes_in_order_and_completes(client, monkeypatch):
    transport = _two_agent_chain(client, monkeypatch)
    _publish_flow(client, "chain-flow",
                  _flow(_agent_step("chain-a"), _agent_step("chain-b")))
    r = client.post("/agent-workflows/chain-flow/run", headers=CLINICIAN,
                    json={"input": "Draft please",
                          "patientRef": "Patient/pat-1"})
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["status"] == "completed"
    assert run["output"] == "STEP-TWO-OUTPUT"
    assert run["patient_ref"] == "Patient/pat-1"
    assert run["input_hash"] == hashlib.sha256(b"Draft please").hexdigest()
    # Order + chaining: the second agent's prompt IS the first output.
    assert [c["agent"]["key"] for c in transport.calls] == ["chain-a",
                                                            "chain-b"]
    assert transport.calls[0]["prompt"] == "Draft please"
    assert transport.calls[1]["prompt"] == "STEP-ONE-OUTPUT"
    # Each agent step is ALSO an AgentRun under the existing
    # disposition flow.
    agent_runs = client.get("/agents/runs", headers=CLINICIAN).json()
    assert len(agent_runs) == 2
    assert {x["agent"] for x in agent_runs} == {"chain-a", "chain-b"}
    assert all(x["disposition"] == "pending" for x in agent_runs)
    step_rows = run["steps"]
    assert [s["status"] for s in step_rows] == ["completed", "completed"]
    assert step_rows[0]["agent_run_id"] == next(
        x["run_id"] for x in agent_runs if x["agent"] == "chain-a")
    actions = _audit_actions(client)
    assert "agentflow.run" in actions
    assert "agentflow.step" in actions
    assert "agents.run" in actions
    # GET run returns the same record + steps.
    got = client.get(f"/agent-workflows/runs/{run['run_id']}",
                     headers=CLINICIAN)
    assert got.status_code == 200
    assert got.json()["status"] == "completed"


def test_review_parks_then_approve_resumes(client, monkeypatch):
    transport = _two_agent_chain(client, monkeypatch)
    _publish_flow(client, "supervised-flow", _flow(
        _agent_step("chain-a"), _review_step(), _agent_step("chain-b")))
    r = client.post("/agent-workflows/supervised-flow/run",
                    json={"input": "Draft please"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["status"] == "awaiting-review"
    assert run["output"] is None
    assert len(transport.calls) == 1  # parked BEFORE the second agent
    assert [s["status"] for s in run["steps"]] == [
        "completed", "awaiting-review", "pending"]
    # Approve: the reviewed text passes through to the next agent.
    r = client.post(f"/agent-workflows/runs/{run['run_id']}/review",
                    json={"decision": "approve", "note": "looks right"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "completed"
    assert done["output"] == "STEP-TWO-OUTPUT"
    assert len(transport.calls) == 2
    assert transport.calls[1]["prompt"] == "STEP-ONE-OUTPUT"
    review_step = done["steps"][1]
    assert review_step["status"] == "approved"
    assert review_step["reviewed_by"] == "dev-clinician"
    assert review_step["review_note"] == "looks right"
    assert "agentflow.review" in _audit_actions(client)


def test_review_reject_terminates_and_downstream_never_runs(
        client, monkeypatch):
    transport = _two_agent_chain(client, monkeypatch)
    _publish_flow(client, "supervised-flow", _flow(
        _agent_step("chain-a"), _review_step(), _agent_step("chain-b")))
    r = client.post("/agent-workflows/supervised-flow/run",
                    json={"input": "Draft please"}, headers=CLINICIAN)
    run = r.json()
    assert run["status"] == "awaiting-review"
    r = client.post(f"/agent-workflows/runs/{run['run_id']}/review",
                    json={"decision": "reject",
                          "note": "facts not in the recording"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "terminated"
    assert done["output"] is None
    # THE assertion: the downstream agent was never invoked.
    assert len(transport.calls) == 1
    assert [s["status"] for s in done["steps"]] == [
        "completed", "rejected", "skipped"]
    agent_runs = client.get("/agents/runs", headers=CLINICIAN).json()
    assert len(agent_runs) == 1
    assert agent_runs[0]["agent"] == "chain-a"


def test_review_edit_substitutes_output_for_downstream(client, monkeypatch):
    transport = _two_agent_chain(client, monkeypatch)
    _publish_flow(client, "supervised-flow", _flow(
        _agent_step("chain-a"), _review_step(), _agent_step("chain-b")))
    r = client.post("/agent-workflows/supervised-flow/run",
                    json={"input": "Draft please"}, headers=CLINICIAN)
    run = r.json()
    r = client.post(f"/agent-workflows/runs/{run['run_id']}/review",
                    json={"decision": "edit",
                          "editedOutput": "EDITED-BY-REVIEWER"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "completed"
    assert done["steps"][1]["status"] == "edited"
    assert done["steps"][1]["output"] == "EDITED-BY-REVIEWER"
    # The downstream agent received the EDITED text, not the draft.
    assert transport.calls[1]["prompt"] == "EDITED-BY-REVIEWER"
    downstream = next(x for x in client.get(
        "/agents/runs", headers=CLINICIAN).json() if x["agent"] == "chain-b")
    assert downstream["input_hash"] == hashlib.sha256(
        b"EDITED-BY-REVIEWER").hexdigest()


def test_review_state_and_decision_guards(client, monkeypatch):
    _two_agent_chain(client, monkeypatch)
    _publish_flow(client, "supervised-flow", _flow(
        _agent_step("chain-a"), _review_step(), _agent_step("chain-b")))
    run = client.post("/agent-workflows/supervised-flow/run",
                      json={"input": "x"}, headers=CLINICIAN).json()
    # Bad decision -> 422; edit without editedOutput -> 422.
    r = client.post(f"/agent-workflows/runs/{run['run_id']}/review",
                    json={"decision": "shrug"}, headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post(f"/agent-workflows/runs/{run['run_id']}/review",
                    json={"decision": "edit"}, headers=CLINICIAN)
    assert r.status_code == 422
    # A configurator does not hold the step's declared clinician role.
    r = client.post(f"/agent-workflows/runs/{run['run_id']}/review",
                    json={"decision": "approve"}, headers=CONFIG)
    assert r.status_code == 403
    # Approve once; a second review on a finished run -> 409.
    r = client.post(f"/agent-workflows/runs/{run['run_id']}/review",
                    json={"decision": "approve"}, headers=CLINICIAN)
    assert r.status_code == 200
    r = client.post(f"/agent-workflows/runs/{run['run_id']}/review",
                    json={"decision": "approve"}, headers=CLINICIAN)
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Honest-off — failures are recorded, nothing is fabricated
# ---------------------------------------------------------------------------

def test_honest_off_no_model_fails_run_without_output(client, monkeypatch):
    _publish_agent(client, "modelless-a", model="")
    _enable(client, "modelless-a")  # BD-41 + C-AI gates (see helper)
    _publish_flow(client, "modelless-flow", _flow(_agent_step("modelless-a")))

    def _no_model(payload):
        return 503, {"error": "No model configured: set a model on the "
                              "agent config or AGENT_MODEL on the ADK "
                              "runtime. No answer was generated."}

    monkeypatch.setattr(agents_module, "_post_run", _no_model)
    r = client.post("/agent-workflows/modelless-flow/run",
                    json={"input": "Draft please"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["status"] == "failed"
    assert "No model configured" in run["failure_reason"]
    assert run["output"] is None
    assert run["steps"][0]["status"] == "failed"
    # No AgentRun was created and no output exists anywhere.
    assert client.get("/agents/runs", headers=CLINICIAN).json() == []


def test_honest_off_runtime_unreachable_fails_run(client, monkeypatch):
    _publish_agent(client, "down-a")
    _enable(client, "down-a")  # BD-41 + C-AI gates (see helper)
    _publish_flow(client, "down-flow", _flow(_agent_step("down-a")))

    def _boom(payload):
        raise httpx.ConnectError("connection refused", request=None)

    monkeypatch.setattr(agents_module, "_post_run", _boom)
    r = client.post("/agent-workflows/down-flow/run",
                    json={"input": "Draft please"}, headers=CLINICIAN)
    run = r.json()
    assert run["status"] == "failed"
    assert "unavailable" in run["failure_reason"]
    assert "No answer was generated" in run["failure_reason"]
    assert run["output"] is None
    assert client.get("/agents/runs", headers=CLINICIAN).json() == []


def test_unenabled_agent_fails_run_and_transport_never_called(
        client, monkeypatch):
    _publish_agent(client, "sleepy-a")  # published, never enabled
    _publish_flow(client, "sleepy-flow", _flow(_agent_step("sleepy-a")))
    transport = _Transport(["SHOULD-NEVER-APPEAR"])
    monkeypatch.setattr(agents_module, "_post_run", transport)
    r = client.post("/agent-workflows/sleepy-flow/run",
                    json={"input": "Draft please"}, headers=CLINICIAN)
    run = r.json()
    assert run["status"] == "failed"
    assert "not enabled" in run["failure_reason"]
    assert run["output"] is None
    assert transport.calls == []


# ---------------------------------------------------------------------------
# Scope D seeds — drafts, not runnable until published + enabled
# ---------------------------------------------------------------------------

def test_seeded_agents_exist_as_drafts_and_cannot_run(client):
    drafts = client.get("/studio/config?kind=agent", headers=ADMIN).json()
    by_key = {d["key"]: d for d in drafts}
    for key in ("ambient-scribe", "chart-search", "deep-research"):
        assert by_key[key]["status"] == "draft", key
    # The runnable roster shows only PUBLISHED agents: none of the
    # seeded three appears, and running one is a 404.
    roster = client.get("/agents", headers=CLINICIAN).json()
    assert {a["key"] for a in roster["agents"]}.isdisjoint(
        {"ambient-scribe", "chart-search", "deep-research"})
    for key in ("ambient-scribe", "chart-search", "deep-research"):
        r = client.post(f"/agents/{key}/run", json={"prompt": "hi"},
                        headers=CLINICIAN)
        assert r.status_code == 404, key


def test_seeded_agent_payload_shapes(client):
    factory = client.app.state.session_factory
    with factory() as session:
        rows = {r.key: r for r in session.scalars(select(StudioConfig).where(
            StudioConfig.kind == "agent")).all()}
    scribe = rows["ambient-scribe"].payload
    assert scribe["modalities"]["audio"] is True
    assert scribe["model"] == ""
    search = rows["chart-search"].payload
    assert search["modalities"] == {"text": True, "image": False,
                                    "audio": False}
    assert "citation" in search["instructions"].lower() or \
        "cite" in search["instructions"].lower()
    research = rows["deep-research"].payload
    assert research["maxSteps"] > 6  # raised vs the Phase 10 seeds' 6
    assert research["model"] == ""


def test_seeded_agentflow_is_a_draft_and_cannot_run(client):
    factory = client.app.state.session_factory
    with factory() as session:
        seeded = session.scalars(select(StudioConfig).where(
            StudioConfig.kind == "agentflow",
            StudioConfig.key == "supervised-note-drafting")).first()
    assert seeded is not None
    assert seeded.status == ConfigStatus.draft
    # Only PUBLISHED flows are listed / runnable.
    listed = client.get("/agent-workflows", headers=CLINICIAN).json()
    assert listed["workflows"] == []
    r = client.post("/agent-workflows/supervised-note-drafting/run",
                    json={"input": "Draft please"}, headers=CLINICIAN)
    assert r.status_code == 404
    # The seeded chain shape: agent -> review (clinician) -> agent.
    factory = client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(StudioConfig).where(
            StudioConfig.kind == "agentflow",
            StudioConfig.key == "supervised-note-drafting")).first()
    steps = row.payload["steps"]
    assert [s["type"] for s in steps] == ["agent", "review", "agent"]
    assert steps[0]["agentKey"] == "ambient-scribe"
    assert steps[1]["role"] == "clinician"
    assert steps[2]["agentKey"] == "note-coding-assistant"
