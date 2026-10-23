"""Phase 10, Track A — Studio-managed agents (ADK pattern).

Agent definitions are governed Studio config (kind=agent): seeded as
DRAFTS, published immutable, and a published agent still cannot run
until it is separately ENABLED (the pack lifecycle pattern). Runs go
to the ADK runtime service; with the runtime down or modelless the
answer is an honest 503 — never a fabricated one. The runtime
transport is substituted in these tests (monkeypatch of
app.agents._post_run); the live-runtime honesty path is covered by
the ADK service's own suite (backend/adk/tests) and by the
unreachable-runtime test here.

Synthetic data only.

C-AI registered adjustment: agent publish/enable is now four-eyes +
statement gated (app.four_eyes / app.ai_governance). Choreography in
this file: agent drafts are authored by ADMIN, published by CONFIG
(the author may not publish a T2/T3 agent), statements are authored
by ADMIN and approved by CONFIG/CLINICIAN via the shared
tests.conftest.governance_for_enable helper, and agents are enabled
by CONFIG. Assertions are unchanged in strength.
"""
import hashlib

import httpx
import pytest
from sqlalchemy import select

from app import agents as agents_module
from app.models import AuditLog
from tests.conftest import ADMIN, CLINICIAN, CONFIG, VIEWER, governance_for_enable


def _publish(client, kind, key, payload, headers=ADMIN, publish_headers=CONFIG):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=publish_headers)
    assert r.status_code == 200, r.text
    return version


def _enable(client, key):
    """C-AI choreography: governance flow (record + approved
    statement) then enable by CONFIG — never by the author ADMIN."""
    governance_for_enable(client, key)
    r = client.post(f"/agents/{key}/enable", headers=CONFIG)
    assert r.status_code == 200, r.text
    return r


def _agent_payload(**overrides):
    payload = {
        "description": "Test agent",
        "model": "ollama_chat/llama3.2:3b",
        "instructions": "Summarise the input briefly.",
        "tools": ["opencare_api_call"],
        "apiAllowlist": ["/fhir"],
        "modalities": {"text": True, "image": False, "audio": False},
    }
    payload.update(overrides)
    return payload


def _audit_actions(client):
    factory = client.app.state.session_factory
    with factory() as session:
        return [row.action for row in session.scalars(select(AuditLog)).all()]


# ---------------------------------------------------------------------------
# Seeds + lifecycle
# ---------------------------------------------------------------------------

def test_seeded_agents_are_drafts_and_cannot_run(client):
    drafts = client.get("/studio/config?kind=agent", headers=ADMIN).json()
    by_key = {d["key"]: d for d in drafts}
    assert by_key["note-coding-assistant"]["status"] == "draft"
    assert by_key["lab-result-summariser"]["status"] == "draft"
    # The runnable roster shows only PUBLISHED agents: empty.
    roster = client.get("/agents", headers=CLINICIAN).json()
    assert roster["agents"] == []
    assert roster["runtime"]["reachable"] is False
    # A draft-only agent does not exist for the run endpoint.
    r = client.post("/agents/note-coding-assistant/run",
                    json={"prompt": "hello"}, headers=CLINICIAN)
    assert r.status_code == 404


def test_agent_config_validation(client):
    # Unknown tool -> 422 (tools are a server registry, not free text).
    r = client.post("/studio/config/agent/bad-tools",
                    json=_agent_payload(tools=["shell"]), headers=ADMIN)
    assert r.status_code == 422
    # A credential-looking model string -> 422.
    r = client.post("/studio/config/agent/bad-model",
                    json=_agent_payload(model="sk-secret-key"), headers=ADMIN)
    assert r.status_code == 422
    # Missing instructions -> 422.
    payload = _agent_payload()
    del payload["instructions"]
    r = client.post("/studio/config/agent/no-instructions",
                    json=payload, headers=ADMIN)
    assert r.status_code == 422
    # An empty model is VALID config: it means "no model route".
    r = client.post("/studio/config/agent/no-model",
                    json=_agent_payload(model=""), headers=ADMIN)
    assert r.status_code == 201, r.text


def test_enable_requires_a_published_version(client):
    r = client.post("/agents/note-coding-assistant/enable", headers=ADMIN)
    assert r.status_code == 409  # still only a seeded draft
    # Publish the SEEDED draft v1 (a second draft cannot be created
    # while one is in progress — that is the governance working).
    r = client.post(
        "/studio/config/agent/note-coding-assistant/versions/1/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    # C-AI: the seeded draft's author is "system" (no human builder),
    # so ADMIN may publish it; enablement still needs the statement
    # flow + a non-author enabler (CONFIG).
    r = _enable(client, "note-coding-assistant")
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True
    assert "agents.enable" in _audit_actions(client)


def test_published_agent_version_is_immutable(client):
    _publish(client, "agent", "immutable-agent", _agent_payload())
    r = client.put("/studio/config/agent/immutable-agent/versions/1",
                   json=_agent_payload(description="changed"), headers=ADMIN)
    assert r.status_code == 409


def test_run_blocked_until_enabled(client):
    _publish(client, "agent", "gated-agent", _agent_payload())
    r = client.post("/agents/gated-agent/run",
                    json={"prompt": "hi"}, headers=CLINICIAN)
    assert r.status_code == 409
    assert "not enabled" in r.json()["detail"]


def test_run_requires_agents_run_capability(client):
    _publish(client, "agent", "caps-agent", _agent_payload())
    _enable(client, "caps-agent")
    r = client.post("/agents/caps-agent/run",
                    json={"prompt": "hi"}, headers=VIEWER)
    assert r.status_code == 403
    r = client.get("/agents", headers=VIEWER)
    assert r.status_code == 403


def test_run_modality_gate(client):
    _publish(client, "agent", "text-only", _agent_payload())
    _enable(client, "text-only")  # BD-41 + C-AI gates (see helper)
    r = client.post("/agents/text-only/run",
                    json={"prompt": "hi", "imageBase64": "aGVsbG8="},
                    headers=CLINICIAN)
    assert r.status_code == 422
    assert "image" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Honest-off behaviour
# ---------------------------------------------------------------------------

def test_run_runtime_unreachable_is_honest_503(client, monkeypatch):
    _publish(client, "agent", "down-agent", _agent_payload())
    _enable(client, "down-agent")  # BD-41 + C-AI gates (see helper)

    def _boom(payload):
        raise httpx.ConnectError("connection refused", request=None)

    monkeypatch.setattr(agents_module, "_post_run", _boom)
    r = client.post("/agents/down-agent/run",
                    json={"prompt": "hi"}, headers=CLINICIAN)
    assert r.status_code == 503
    assert "unavailable" in r.json()["detail"]
    assert "No answer was generated" in r.json()["detail"]
    # Nothing was recorded: no run row, no agents.run audit entry.
    assert client.get("/agents/runs", headers=CLINICIAN).json() == []
    assert "agents.run" not in _audit_actions(client)


def test_run_no_model_is_honest_503(client, monkeypatch):
    _publish(client, "agent", "modelless", _agent_payload(model=""))
    _enable(client, "modelless")  # BD-41 + C-AI gates (see helper)

    def _no_model(payload):
        return 503, {"error": "No model configured: set a model on the agent "
                              "config or AGENT_MODEL on the ADK runtime. "
                              "No answer was generated."}

    monkeypatch.setattr(agents_module, "_post_run", _no_model)
    r = client.post("/agents/modelless/run",
                    json={"prompt": "hi"}, headers=CLINICIAN)
    assert r.status_code == 503
    assert "No model configured" in r.json()["detail"]
    assert client.get("/agents/runs", headers=CLINICIAN).json() == []


# ---------------------------------------------------------------------------
# A successful (transport-substituted) run + human disposition
# ---------------------------------------------------------------------------

def test_run_records_draft_and_disposition(client, monkeypatch):
    _publish(client, "agent", "worker", _agent_payload())
    _enable(client, "worker")  # BD-41 + C-AI gates (see helper)
    seen = {}

    def _ok(payload):
        seen.update(payload)
        return 200, {"output": "DRAFT: brief summary.",
                     "model": "ollama_chat/llama3.2:3b", "agent": "worker"}

    monkeypatch.setattr(agents_module, "_post_run", _ok)
    r = client.post("/agents/worker/run",
                    json={"prompt": "Summarise pat-1", "patient_ref": "Patient/pat-1"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["output"] == "DRAFT: brief summary."
    assert run["disposition"] == "pending"
    assert run["version"] == 1
    # The input hash is the sha256 of the prompt (auditable, reproducible).
    assert run["input_hash"] == hashlib.sha256(b"Summarise pat-1").hexdigest()
    # The runtime received the published config + the CALLER's token.
    assert seen["agent"]["instructions"] == "Summarise the input briefly."
    assert seen["apiToken"] == "clinician-token"
    # Audit carries agent, version, input hash, output, disposition.
    assert "agents.run" in _audit_actions(client)
    # The run is listed; a human dispositions it — once.
    runs = client.get("/agents/runs", headers=CLINICIAN).json()
    assert [x["run_id"] for x in runs] == [run["run_id"]]
    d = client.post(f"/agents/runs/{run['run_id']}/disposition",
                    json={"disposition": "accepted"}, headers=CLINICIAN)
    assert d.status_code == 200 and d.json()["disposition"] == "accepted"
    assert d.json()["disposition_by"] == "dev-clinician"
    again = client.post(f"/agents/runs/{run['run_id']}/disposition",
                        json={"disposition": "rejected"}, headers=CLINICIAN)
    assert again.status_code == 409
    assert "agents.disposition" in _audit_actions(client)


def test_disable_stops_runs(client, monkeypatch):
    _publish(client, "agent", "toggle", _agent_payload())
    _enable(client, "toggle")
    client.post("/agents/toggle/disable", headers=ADMIN)
    r = client.post("/agents/toggle/run",
                    json={"prompt": "hi"}, headers=CLINICIAN)
    assert r.status_code == 409
    assert "agents.disable" in _audit_actions(client)
