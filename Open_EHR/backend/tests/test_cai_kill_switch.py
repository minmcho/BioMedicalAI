"""C-AI — kill-switch drill (Worker W2, framework §7.4 / AI-G5).

One governed act stops everything: agent runs refuse naming the
kill switch, MCP tools/call refuses with status refused-kill-switch,
A2A sends refuse — and releasing the switch re-enables NOTHING:
per-agent re-enablement through the normal gate is the only way
back. Synthetic data only; the ADK transport is substituted.
"""
import pytest
from sqlalchemy import select

from app import agents as agents_module
from app import mcp_a2a
from app.models import AuditLog, ConfigStatus, StudioConfig
from tests.conftest import (ADMIN, CLINICIAN, CONFIG,
                            governance_for_enable)

PATIENT1 = {"resourceType": "Patient", "id": "pat-1",
            "name": [{"family": "Synthetic", "given": ["Pat"]}],
            "gender": "unknown", "birthDate": "1990-01-01"}


def _agent_payload():
    return {"description": "Kill-switch drill agent",
            "model": "ollama_chat/llama3.2:3b",
            "instructions": "Summarise the input briefly.",
            "tools": ["opencare_api_call"], "apiAllowlist": ["/fhir"],
            "modalities": {"text": True, "image": False, "audio": False}}


def _publish_and_enable(client, key):
    r = client.post(f"/studio/config/agent/{key}",
                    json=_agent_payload(), headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/agent/{key}/versions/{version}/publish",
        headers=CONFIG)
    assert r.status_code == 200, r.text
    governance_for_enable(client, key)
    r = client.post(f"/agents/{key}/enable", headers=CONFIG)
    assert r.status_code == 200, r.text


def _ok_transport(payload):
    return 200, {"output": "DRAFT: brief summary.",
                 "model": "ollama_chat/llama3.2:3b"}


def _roster(client):
    body = client.get("/agents", headers=CLINICIAN).json()
    return {a["key"]: a for a in body["agents"]}


def _publish_config_db(client, kind, key, payload):
    factory = client.app.state.session_factory
    with factory() as session:
        versions = session.scalars(select(StudioConfig).where(
            StudioConfig.kind == kind, StudioConfig.key == key)
            .order_by(StudioConfig.version.asc())).all()
        for v in versions:
            if v.status == ConfigStatus.published:
                v.status = ConfigStatus.superseded
        row = StudioConfig(
            kind=kind, key=key,
            version=(versions[-1].version + 1) if versions else 1,
            status=ConfigStatus.published, payload=payload,
            created_by="test", updated_by="test")
        session.add(row)
        session.commit()


def _enable_mcp_tool(client, name):
    factory = client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(StudioConfig).where(
            StudioConfig.kind == "integration",
            StudioConfig.key == "mcp-tools",
            StudioConfig.status == ConfigStatus.published)).first()
        assert row is not None
        payload = dict(row.payload or {})
    tools = []
    for tool in payload.get("tools") or []:
        tool = dict(tool)
        if tool.get("name") == name:
            tool["enabled"] = True
        tools.append(tool)
    _publish_config_db(client, "integration", "mcp-tools",
                       {**payload, "tools": tools})


def _audit_payloads(client, action):
    factory = client.app.state.session_factory
    with factory() as session:
        return [dict(row.payload or {}) for row in session.scalars(
            select(AuditLog).where(AuditLog.action == action)).all()]


def _mcp_call(client, name, arguments):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments}},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def _a2a_send(client, agent_key):
    r = client.post("/a2a/rpc", json={
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"agentKey": agent_key, "message": "Summarise please"}},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def test_kill_switch_drill(client, monkeypatch):
    _publish_and_enable(client, "ks-agent-a")
    _publish_and_enable(client, "ks-agent-b")
    monkeypatch.setattr(agents_module, "_post_run", _ok_transport)
    # Sanity: agent A runs before the drill.
    r = client.post("/agents/ks-agent-a/run", json={"prompt": "hi"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # An agent-reachable MCP tool is enabled and callable.
    assert client.post("/fhir/Patient", json=PATIENT1,
                       headers=CLINICIAN).status_code == 201
    _enable_mcp_tool(client, "patient_summary")
    body = _mcp_call(client, "patient_summary", {"patientId": "pat-1"})
    assert "result" in body, body

    # --- Engage ------------------------------------------------------------
    r = client.post("/ai-governance/kill-switch",
                    json={"reason": "C-AI drill: suspected fabricated "
                                     "output class"},
                    headers=CONFIG)
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True
    assert r.json()["agentsDisabled"] == 2
    # Every enablement was flipped off by the one act.
    roster = _roster(client)
    assert roster["ks-agent-a"]["enabled"] is False
    assert roster["ks-agent-b"]["enabled"] is False

    # MCP tools/call refuses with the kill-switch status + reason.
    body = _mcp_call(client, "patient_summary", {"patientId": "pat-1"})
    assert body["error"]["code"] == mcp_a2a.ERR_TOOL_DISABLED
    assert "kill switch" in body["error"]["message"]
    statuses = [p.get("status") for p in _audit_payloads(
        client, "mcp.tool.call")]
    assert "refused-kill-switch" in statuses

    # A2A send refuses (agent disabled by the kill-switch flip).
    body = _a2a_send(client, "ks-agent-a")
    assert body["error"]["code"] == mcp_a2a.ERR_TOOL_DISABLED

    # Re-enabling through the normal gate is possible, but runs
    # still refuse — naming the kill switch — while it is active.
    r = client.post("/agents/ks-agent-a/enable", headers=CONFIG)
    assert r.status_code == 200, r.text
    r = client.post("/agents/ks-agent-a/run", json={"prompt": "hi"},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "kill switch" in r.json()["detail"]
    body = _a2a_send(client, "ks-agent-a")
    assert body["error"]["code"] == mcp_a2a.ERR_TOOL_DISABLED
    assert "kill switch" in body["error"]["message"]

    # --- Release -----------------------------------------------------------
    r = client.post("/ai-governance/kill-switch/release", json={},
                    headers=CONFIG)
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False
    # Release restored NOTHING: agent B (never re-enabled) is still off.
    roster = _roster(client)
    assert roster["ks-agent-b"]["enabled"] is False
    # Agent A runs again (it was re-enabled per-agent during the drill).
    r = client.post("/agents/ks-agent-a/run", json={"prompt": "hi"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # Per-agent re-enablement of B through the normal gate works.
    r = client.post("/agents/ks-agent-b/enable", headers=CONFIG)
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True
    r = client.post("/agents/ks-agent-b/run", json={"prompt": "hi"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # The MCP tool is callable again too.
    body = _mcp_call(client, "patient_summary", {"patientId": "pat-1"})
    assert "result" in body, body
