"""Phase 16, Workstream D — MCP + A2A as governed Studio
configuration (app.mcp_a2a).

Conformance coverage:
- MCP client round-trip against an in-suite fixture MCP server (a
  threaded stdlib http.server speaking JSON-RPC), including the
  initialize handshake the client performs before tools/call;
- guardrails BEFORE any HTTP: patient naming another patient (403,
  fixture records NO call), staff missing the tool's declared
  requiredCapability (403, no call), allowlist refusal, not-enabled
  refusal (409);
- enable/disable as separate governed actions (studio.manage);
- audit: every call writes mcp.tool.call with {server, tool, patient,
  status} and NEVER raw argument payloads;
- MCP server: JSON-RPC shapes (initialize / ping / tools/list /
  tools/call, unknown method -32601, invalid params -32602), tools
  governed by published integration/mcp-tools (all disabled at seed:
  list empty, disabled call refused), capability + patient-scope
  refusals, real read-tool execution against seeded FHIR data;
- A2A: AgentCard schema fields + skills = published+ENABLED agents
  only; JSON-RPC error shapes; task lifecycle submitted->working->
  failed (honest, no model — the runtime's 503 reason verbatim, no
  AgentRun recorded) and ->completed with the agent transport
  substituted (AgentRun recorded, artifacts carry the draft);
  tasks/cancel on a non-terminal task + refusal on a terminal one;
  SSE stream binding (GET /a2a/tasks/{id}/stream);
- delegation: X-OpenCare-On-Behalf-Of accepted + audited for admin,
  refused (ignored-with-audit-note) for clinician.

A live external MCP/A2A peer is NOT RUN — the fixture server and the
substituted agent transport prove the plumbing only.

Synthetic data only.

C-AI registered adjustment: agent drafts authored by ADMIN are
published by CONFIG (four-eyes publish gate), and A2A worker
enablement runs the shared tests.conftest.governance_for_enable
flow (validation record + approved statement) with the enable
performed by CONFIG. A2A sends also consult the C-AI runtime gate.
Assertions are unchanged in strength.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from sqlalchemy import select

from app import agents as agents_module
from app import mcp_a2a
from app.models import AuditLog, ConfigStatus, StudioConfig
from tests.conftest import (ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER,
                            governance_for_enable)

PATIENT1 = {"resourceType": "Patient", "id": "pat-1",
            "name": [{"family": "Synthetic", "given": ["Pat"]}],
            "gender": "unknown", "birthDate": "1990-01-01"}
PATIENT2 = {"resourceType": "Patient", "id": "pat-2",
            "name": [{"family": "Other", "given": ["Sam"]}],
            "gender": "unknown"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _audit_rows(client):
    factory = client.app.state.session_factory
    with factory() as session:
        return [(r.actor, r.action, r.entity, dict(r.payload or {}))
                for r in session.scalars(select(AuditLog)).all()]


def _publish_config_db(client, kind, key, payload):
    """Insert a new PUBLISHED version of a config object directly in
    the store (the mcp_server kind is registered centrally by the
    coordinator; integration docs under test need republishing with a
    tool flipped). Mirrors publish semantics: the previous published
    version is superseded."""
    factory = client.app.state.session_factory
    with factory() as session:
        versions = session.scalars(select(StudioConfig).where(
            StudioConfig.kind == kind, StudioConfig.key == key)
            .order_by(StudioConfig.version.asc())).all()
        for v in versions:
            if v.status == ConfigStatus.published:
                v.status = ConfigStatus.superseded
        next_version = (versions[-1].version + 1) if versions else 1
        row = StudioConfig(kind=kind, key=key, version=next_version,
                           status=ConfigStatus.published, payload=payload,
                           created_by="test", updated_by="test")
        session.add(row)
        session.commit()
        return next_version


def _published_payload(client, kind, key):
    factory = client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(StudioConfig).where(
            StudioConfig.kind == kind, StudioConfig.key == key,
            StudioConfig.status == ConfigStatus.published)).first()
        assert row is not None, f"no published {kind}/{key}"
        return dict(row.payload or {})


def _set_mcp_tool(client, name, **changes):
    payload = _published_payload(client, "integration", "mcp-tools")
    tools = []
    for tool in payload.get("tools") or []:
        tool = dict(tool)
        if tool.get("name") == name:
            tool.update(changes)
        tools.append(tool)
    _publish_config_db(client, "integration", "mcp-tools",
                       {**payload, "tools": tools})


def _enable(client, key):
    """BD-41 + C-AI gates: governance flow (validation record +
    approved statement via tests.conftest.governance_for_enable),
    then enable by CONFIG — never by the author ADMIN."""
    governance_for_enable(client, key)
    r = client.post(f"/agents/{key}/enable", headers=CONFIG)
    assert r.status_code == 200, r.text


def _publish_agent(client, key, model=""):
    payload = {"description": f"A2A test agent {key}", "model": model,
               "instructions": "Summarise the input briefly.",
               "tools": ["opencare_api_call"], "apiAllowlist": ["/fhir"],
               "modalities": {"text": True, "image": False,
                              "audio": False}}
    r = client.post(f"/studio/config/agent/{key}", json=payload,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/agent/{key}/versions/{version}/publish",
        headers=CONFIG)  # C-AI four-eyes: author ADMIN may not publish
    assert r.status_code == 200, r.text


def _rpc(client, payload, headers=CLINICIAN, path="/mcp"):
    return client.post(path, json=payload, headers=headers)


def _mcp_result(response):
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert "result" in body, body
    return body["result"]


def _mcp_error(response):
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" in body, body
    return body["error"]


# ---------------------------------------------------------------------------
# Fixture MCP server (threaded stdlib HTTP, real httpx round-trip)
# ---------------------------------------------------------------------------

class _FixtureMcp:
    def __init__(self):
        self.methods: list[str] = []
        self.calls: list[dict] = []
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the suite quiet
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                method = body.get("method")
                state.methods.append(method)
                params = body.get("params") or {}
                if method == "initialize":
                    result = {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fixture-mcp",
                                       "version": "0.1"}}
                    return self._reply(body.get("id"), result=result)
                if method == "tools/list":
                    return self._reply(body.get("id"), result={"tools": [
                        {"name": "echo_patient",
                         "description": "Echoes its arguments"},
                        {"name": "patient_vitals",
                         "description": "Synthetic vitals"}]})
                if method == "tools/call":
                    state.calls.append(params)
                    name = params.get("name")
                    args = params.get("arguments") or {}
                    if name == "echo_patient":
                        return self._reply(body.get("id"), result={
                            "content": [{"type": "text",
                                         "text": json.dumps(args)}],
                            "isError": False})
                    if name == "patient_vitals":
                        return self._reply(body.get("id"), result={
                            "content": [{"type": "text",
                                         "text": json.dumps(
                                             {"hr": 72,
                                              "patient": args.get(
                                                  "patientId")})}],
                            "isError": False})
                    return self._reply(body.get("id"), error={
                        "code": -32602,
                        "message": f"unknown tool {name}"})
                return self._reply(body.get("id"), error={
                    "code": -32601, "message": "method not found"})

            def _reply(self, req_id, result=None, error=None):
                payload = {"jsonrpc": "2.0", "id": req_id}
                if error is not None:
                    payload["error"] = error
                else:
                    payload["result"] = result
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/mcp"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def fixture_mcp():
    fixture = _FixtureMcp()
    yield fixture
    fixture.stop()


def _publish_mcp_server(client, fixture, key="fixture",
                        allowlist=("echo_patient", "patient_vitals"),
                        tools=None):
    payload = {"name": "Fixture MCP (synthetic)", "transport": "http",
               "url": fixture.url, "toolAllowlist": list(allowlist)}
    if tools is not None:
        payload["tools"] = tools
    _publish_config_db(client, "mcp_server", key, payload)
    return payload


def _seed_patients(client):
    assert client.post("/fhir/Patient", json=PATIENT1,
                       headers=CLINICIAN).status_code == 201
    assert client.post("/fhir/Patient", json=PATIENT2,
                       headers=CLINICIAN).status_code == 201


# ---------------------------------------------------------------------------
# mcp_server payload validation (the coordinator-registered validator)
# ---------------------------------------------------------------------------

def test_mcp_server_validator_credential_law_and_shape(client):
    good = {"name": "X", "transport": "http",
            "url": "http://127.0.0.1:9999/mcp", "toolAllowlist": []}
    assert mcp_a2a.validate_mcp_server_payload(good) == good
    for field in ("apiKey", "password", "token", "secret"):
        with pytest.raises(Exception) as exc:
            mcp_a2a.validate_mcp_server_payload(
                {**good, field: "sk-synthetic"})
        assert exc.value.status_code == 422
        assert field in exc.value.detail
    # Credentials embedded in the URL are refused too.
    with pytest.raises(Exception) as exc:
        mcp_a2a.validate_mcp_server_payload(
            {**good, "url": "http://user:pass@127.0.0.1:9999/mcp"})
    assert exc.value.status_code == 422
    with pytest.raises(Exception):
        mcp_a2a.validate_mcp_server_payload({**good, "transport": "stdio"})
    with pytest.raises(Exception):
        mcp_a2a.validate_mcp_server_payload(
            {k: v for k, v in good.items() if k != "toolAllowlist"})


# ---------------------------------------------------------------------------
# MCP client
# ---------------------------------------------------------------------------

def test_mcp_client_enable_governance_and_listing(client, fixture_mcp):
    _publish_mcp_server(client, fixture_mcp)
    listing = client.get("/mcp/servers", headers=CLINICIAN)
    assert listing.status_code == 200, listing.text
    servers = listing.json()["servers"]
    assert len(servers) == 1
    server = servers[0]
    assert server["key"] == "fixture" and server["enabled"] is False
    assert server["url"] == fixture_mcp.url
    # Published but not enabled: a call is refused 409 before any HTTP.
    r = client.post("/mcp/servers/fixture/call",
                    json={"tool": "echo_patient",
                          "arguments": {"patientId": "pat-1"}},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert fixture_mcp.calls == []
    # Enable is a governed studio.manage action: clinician refused.
    r = client.post("/mcp/servers/fixture/enable", headers=CLINICIAN)
    assert r.status_code == 403
    r = client.post("/mcp/servers/fixture/enable", headers=ADMIN)
    assert r.status_code == 200 and r.json()["enabled"] is True
    actions = [a for _, a, _, _ in _audit_rows(client)]
    assert "mcp.server.enable" in actions
    # Disable flips it back; calls are refused again.
    r = client.post("/mcp/servers/fixture/disable", headers=CONFIG)
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert "mcp.server.disable" in [a for _, a, _, _ in _audit_rows(client)]
    # Enabling an unpublished server is a 409.
    r = client.post("/mcp/servers/ghost/enable", headers=ADMIN)
    assert r.status_code == 409


def test_mcp_client_round_trip_and_tools_list(client, fixture_mcp):
    _publish_mcp_server(client, fixture_mcp)
    client.post("/mcp/servers/fixture/enable", headers=ADMIN)
    r = client.get("/mcp/servers/fixture/tools", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()["tools"]}
    assert names == {"echo_patient", "patient_vitals"}
    assert "initialize" in fixture_mcp.methods
    assert "tools/list" in fixture_mcp.methods

    fixture_mcp.methods.clear()
    fixture_mcp.calls.clear()
    r = client.post("/mcp/servers/fixture/call",
                    json={"tool": "echo_patient",
                          "arguments": {"patientId": "pat-1",
                                        "note": "synthetic"}},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["isError"] is False
    echoed = json.loads(body["result"]["content"][0]["text"])
    assert echoed["patientId"] == "pat-1"
    # The client performed the initialize handshake before tools/call.
    assert fixture_mcp.methods[0] == "initialize"
    assert fixture_mcp.methods[-1] == "tools/call"
    assert fixture_mcp.calls[0]["name"] == "echo_patient"
    # Audit: mcp.tool.call with patient ref + status, never raw payloads.
    calls = [p for _, a, _, p in _audit_rows(client)
             if a == "mcp.tool.call" and p.get("surface") == "client"]
    assert calls and calls[-1]["server"] == "fixture"
    assert calls[-1]["tool"] == "echo_patient"
    assert calls[-1]["patient"] == "Patient/pat-1"
    assert calls[-1]["status"] == "ok"
    assert "synthetic" not in json.dumps(calls)


def test_mcp_client_allowlist_refusal_before_http(client, fixture_mcp):
    _publish_mcp_server(client, fixture_mcp, allowlist=("echo_patient",))
    client.post("/mcp/servers/fixture/enable", headers=ADMIN)
    r = client.post("/mcp/servers/fixture/call",
                    json={"tool": "patient_vitals",
                          "arguments": {"patientId": "pat-1"}},
                    headers=CLINICIAN)
    assert r.status_code == 403, r.text
    assert fixture_mcp.calls == []
    statuses = [p.get("status") for _, a, _, p in _audit_rows(client)
                if a == "mcp.tool.call"]
    assert "refused-allowlist" in statuses


def test_mcp_client_patient_scope_guardrail(client, fixture_mcp):
    _seed_patients(client)
    _publish_mcp_server(client, fixture_mcp)
    client.post("/mcp/servers/fixture/enable", headers=ADMIN)
    # The dev patient is linked to Patient/pat-1: naming pat-2 is a
    # 403 BEFORE any HTTP reaches the fixture.
    r = client.post("/mcp/servers/fixture/call",
                    json={"tool": "echo_patient",
                          "arguments": {"patientId": "pat-2"}},
                    headers=PATIENT)
    assert r.status_code == 403, r.text
    assert fixture_mcp.calls == []
    statuses = [p.get("status") for _, a, _, p in _audit_rows(client)
                if a == "mcp.tool.call"]
    assert "refused-patient-scope" in statuses
    # Their own record passes the guardrail and round-trips.
    r = client.post("/mcp/servers/fixture/call",
                    json={"tool": "echo_patient",
                          "arguments": {"patientId": "pat-1"}},
                    headers=PATIENT)
    assert r.status_code == 200, r.text
    assert len(fixture_mcp.calls) == 1


def test_mcp_client_declared_capability_guardrail(client, fixture_mcp):
    # patient_vitals declares lab.verify (admin + clinician only).
    _publish_mcp_server(
        client, fixture_mcp, key="gated",
        tools=[{"name": "patient_vitals",
                "requiredCapability": "lab.verify"}])
    client.post("/mcp/servers/gated/enable", headers=ADMIN)
    # Configurator lacks lab.verify: 403 before any HTTP.
    r = client.post("/mcp/servers/gated/call",
                    json={"tool": "patient_vitals",
                          "arguments": {"patientId": "pat-1"}},
                    headers=CONFIG)
    assert r.status_code == 403, r.text
    assert fixture_mcp.calls == []
    statuses = [p.get("status") for _, a, _, p in _audit_rows(client)
                if a == "mcp.tool.call"]
    assert "refused-capability" in statuses
    # A clinician holds lab.verify and the call round-trips.
    r = client.post("/mcp/servers/gated/call",
                    json={"tool": "patient_vitals",
                          "arguments": {"patientId": "pat-1"}},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    vitals = json.loads(r.json()["result"]["content"][0]["text"])
    assert vitals == {"hr": 72, "patient": "pat-1"}


# ---------------------------------------------------------------------------
# MCP server (POST /mcp)
# ---------------------------------------------------------------------------

def test_mcp_server_jsonrpc_shapes(client):
    result = _mcp_result(_rpc(client, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05"}}))
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "OpenCare MCP"
    assert result["capabilities"] == {"tools": {}}
    assert _mcp_result(_rpc(client, {
        "jsonrpc": "2.0", "id": 2, "method": "ping"})) == {}
    error = _mcp_error(_rpc(client, {
        "jsonrpc": "2.0", "id": 3, "method": "resources/list"}))
    assert error["code"] == -32601
    # Invalid params: tools/call with no tool name.
    error = _mcp_error(_rpc(client, {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"arguments": {}}}))
    assert error["code"] == -32602
    # Bearer is required.
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 5,
                                  "method": "ping"})
    assert r.status_code == 401


def test_mcp_server_tools_governed_disabled_by_default(client):
    _seed_patients(client)
    # Seeded published with ALL tools disabled: tools/list is empty
    # and a call to a declared-but-disabled tool is refused.
    result = _mcp_result(_rpc(client, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    assert result["tools"] == []
    error = _mcp_error(_rpc(client, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "patient_summary",
                   "arguments": {"patientId": "pat-1"}}}))
    assert error["code"] == mcp_a2a.ERR_TOOL_DISABLED
    # An undeclared tool is invalid params.
    error = _mcp_error(_rpc(client, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "drop_tables", "arguments": {}}}))
    assert error["code"] == -32602
    # Enabling one tool in the published doc makes exactly it listed.
    _set_mcp_tool(client, "patient_summary", enabled=True)
    result = _mcp_result(_rpc(client, {
        "jsonrpc": "2.0", "id": 4, "method": "tools/list"}))
    assert [t["name"] for t in result["tools"]] == ["patient_summary"]
    assert result["tools"][0]["inputSchema"]["type"] == "object"


def test_mcp_server_patient_summary_and_patient_scope(client):
    _seed_patients(client)
    _set_mcp_tool(client, "patient_summary", enabled=True)
    result = _mcp_result(_rpc(client, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "patient_summary",
                   "arguments": {"patientId": "pat-1"}}}))
    assert result["isError"] is False
    data = result["structuredContent"]
    assert data["patient"] == "Patient/pat-1"
    assert data["name"][0]["family"] == "Synthetic"
    assert data["birthDate"] == "1990-01-01"
    text = json.loads(result["content"][0]["text"])
    assert text["patient"] == "Patient/pat-1"
    # Patient caller: own record ok, another patient's refused.
    own = _rpc(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "patient_summary",
                                   "arguments": {"patientId": "pat-1"}}},
               headers=PATIENT)
    assert "result" in own.json(), own.text
    other = _rpc(client, {"jsonrpc": "2.0", "id": 3,
                          "method": "tools/call",
                          "params": {"name": "patient_summary",
                                     "arguments": {"patientId": "pat-2"}}},
                 headers=PATIENT)
    assert _mcp_error(other)["code"] == mcp_a2a.ERR_FORBIDDEN
    # Missing patientId is invalid params for a patient-scoped tool.
    error = _mcp_error(_rpc(client, {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "patient_summary", "arguments": {}}}))
    assert error["code"] == -32602
    # Audited per call, server surface, no raw payloads.
    server_calls = [p for _, a, _, p in _audit_rows(client)
                    if a == "mcp.tool.call" and p.get("surface") == "server"]
    assert {p["status"] for p in server_calls} >= {"ok",
                                                   "refused-patient-scope"}


def test_mcp_server_capability_refusal(client):
    _seed_patients(client)
    # Republish the tool requiring lab.verify: viewer (lab.read only)
    # is refused; clinician holds it.
    _set_mcp_tool(client, "patient_summary", enabled=True,
                  requiredCapability="lab.verify")
    error = _mcp_error(_rpc(client, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "patient_summary",
                   "arguments": {"patientId": "pat-1"}}}, headers=VIEWER))
    assert error["code"] == mcp_a2a.ERR_FORBIDDEN
    result = _mcp_result(_rpc(client, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "patient_summary",
                   "arguments": {"patientId": "pat-1"}}}))
    assert result["structuredContent"]["patient"] == "Patient/pat-1"


def test_mcp_server_lab_results_and_schedule_day(client):
    _seed_patients(client)
    obs = {"resourceType": "Observation", "id": "obs-mcp-1",
           "status": "final",
           "code": {"coding": [{"system": "http://loinc.org",
                                "code": "8867-4",
                                "display": "Heart rate"}]},
           "subject": {"reference": "Patient/pat-1"},
           "valueQuantity": {"value": 72, "unit": "/min"}}
    assert client.post("/fhir/Observation", json=obs,
                       headers=CLINICIAN).status_code == 201
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    appt = {"resourceType": "Appointment", "id": "appt-mcp-1",
            "status": "booked", "start": f"{today}T09:00:00Z",
            "end": f"{today}T09:30:00Z",
            "participant": [
                {"actor": {"reference": "Patient/pat-1"},
                 "status": "accepted"},
                {"actor": {"reference": "Practitioner/prac-1"},
                 "status": "accepted"}]}
    assert client.post("/fhir/Appointment", json=appt,
                       headers=CLINICIAN).status_code == 201

    _set_mcp_tool(client, "lab_results_recent", enabled=True)
    _set_mcp_tool(client, "schedule_day", enabled=True)
    result = _mcp_result(_rpc(client, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "lab_results_recent",
                   "arguments": {"patientId": "pat-1"}}}))
    data = result["structuredContent"]
    assert data["total"] == 1
    assert data["results"][0]["id"] == "obs-mcp-1"
    assert data["results"][0]["display"] == "Heart rate"
    assert data["results"][0]["valueQuantity"]["value"] == 72

    result = _mcp_result(_rpc(client, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "schedule_day",
                   "arguments": {"date": today,
                                 "patientId": "pat-1"}}}))
    data = result["structuredContent"]
    assert data["date"] == today and data["total"] == 1
    assert data["appointments"][0]["id"] == "appt-mcp-1"
    # A day with nothing scheduled is an honest empty result.
    result = _mcp_result(_rpc(client, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "schedule_day",
                   "arguments": {"date": "2030-01-01"}}}))
    assert result["structuredContent"]["total"] == 0


# ---------------------------------------------------------------------------
# A2A
# ---------------------------------------------------------------------------

def _a2a_rpc(client, payload, headers=CLINICIAN, extra_headers=None):
    merged = dict(headers)
    if extra_headers:
        merged.update(extra_headers)
    return client.post("/a2a/rpc", json=payload, headers=merged)


def _a2a_result(response):
    assert response.status_code == 200, response.text
    body = response.json()
    assert "result" in body, body
    return body["result"]


def test_a2a_agent_card_schema_and_enabled_skills_only(client):
    _publish_agent(client, "a2a-worker")
    _publish_agent(client, "a2a-idle")
    _enable(client, "a2a-worker")  # BD-41 + C-AI gates
    # The well-known card is public discovery (no bearer needed).
    r = client.get("/.well-known/agent-card.json")
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["name"] == "OpenCare"
    assert card["description"]
    assert card["url"].endswith("/a2a/rpc")
    assert card["version"] == "1.0.0"
    assert card["protocolVersion"] == mcp_a2a.A2A_PROTOCOL_VERSION
    assert card["capabilities"]["streaming"] is True
    assert card["securitySchemes"]["bearer"]["scheme"] == "bearer"
    # Skills = published+ENABLED agents only: the idle one is absent.
    assert [s["id"] for s in card["skills"]] == ["a2a-worker"]
    skill = card["skills"][0]
    assert skill["description"] == "A2A test agent a2a-worker"
    assert skill["inputModes"] == ["text"]


def test_a2a_rpc_error_shapes_and_auth(client):
    error = _a2a_rpc(client, {"jsonrpc": "2.0", "id": 1,
                              "method": "agent/poem"}).json()["error"]
    assert error["code"] == -32601
    # Invalid params: message/send without a message.
    error = _a2a_rpc(client, {"jsonrpc": "2.0", "id": 2,
                              "method": "message/send",
                              "params": {"agentKey": "x"}}).json()["error"]
    assert error["code"] == -32602
    # Unknown agent.
    _publish_agent(client, "a2a-known")
    error = _a2a_rpc(client, {"jsonrpc": "2.0", "id": 3,
                              "method": "message/send",
                              "params": {"agentKey": "ghost",
                                         "message": "hi"}}).json()["error"]
    assert error["code"] == -32602
    # Published but not enabled.
    error = _a2a_rpc(client, {"jsonrpc": "2.0", "id": 4,
                              "method": "message/send",
                              "params": {"agentKey": "a2a-known",
                                         "message": "hi"}}).json()["error"]
    assert error["code"] == mcp_a2a.ERR_TOOL_DISABLED
    # Bearer required on rpc; viewer lacks a2a.use.
    r = client.post("/a2a/rpc", json={"jsonrpc": "2.0", "id": 5,
                                      "method": "tasks/get",
                                      "params": {"id": "x"}})
    assert r.status_code == 401
    error = _a2a_rpc(client, {"jsonrpc": "2.0", "id": 6,
                              "method": "tasks/get",
                              "params": {"id": "x"}},
                     headers=VIEWER).json()["error"]
    assert error["code"] == mcp_a2a.ERR_FORBIDDEN


def test_a2a_task_lifecycle_failed_honest_without_model(client,
                                                        monkeypatch):
    _publish_agent(client, "a2a-worker", model="")
    _enable(client, "a2a-worker")  # BD-41 + C-AI gates

    def _no_model(payload):
        return 503, {"error": "No model configured: set a model on "
                              "the agent config or AGENT_MODEL on the "
                              "ADK runtime. No answer was generated."}

    monkeypatch.setattr(agents_module, "_post_run", _no_model)
    task = _a2a_result(_a2a_rpc(client, {
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"agentKey": "a2a-worker",
                   "message": "Summarise pat-1",
                   "patientRef": "Patient/pat-1"}}))
    assert task["status"] == "failed"
    assert "No model configured" in task["statusMessage"]
    assert task["artifacts"] == []
    assert task["agentRunId"] is None
    assert task["patientRef"] == "Patient/pat-1"
    # History holds only the sender's message — nothing fabricated.
    assert len(task["history"]) == 1
    assert task["history"][0]["role"] == "user"
    # tasks/get returns the same settled task.
    got = _a2a_result(_a2a_rpc(client, {
        "jsonrpc": "2.0", "id": 2, "method": "tasks/get",
        "params": {"id": task["id"]}}))
    assert got["status"] == "failed" and got["id"] == task["id"]
    # No AgentRun was recorded for the failed execution.
    assert client.get("/agents/runs", headers=CLINICIAN).json() == []
    actions = [a for _, a, _, _ in _audit_rows(client)]
    assert "a2a.task.create" in actions and "a2a.task.failed" in actions
    assert "agents.run" not in actions
    # The SSE stream replays the transitions incl. the failed state.
    r = client.get(f"/a2a/tasks/{task['id']}/stream", headers=CLINICIAN)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert '"submitted"' in r.text and '"working"' in r.text
    assert '"failed"' in r.text


def test_a2a_task_completed_with_substituted_transport(client,
                                                       monkeypatch):
    _publish_agent(client, "a2a-worker",
                   model="ollama_chat/llama3.2:3b")
    _enable(client, "a2a-worker")  # BD-41 + C-AI gates
    seen = {}

    def _ok(payload):
        seen.update(payload)
        return 200, {"output": "DRAFT: brief A2A summary.",
                     "model": "ollama_chat/llama3.2:3b"}

    monkeypatch.setattr(agents_module, "_post_run", _ok)
    task = _a2a_result(_a2a_rpc(client, {
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"agentKey": "a2a-worker", "message": "Summarise",
                   "patientRef": "Patient/pat-1"}}))
    assert task["status"] == "completed"
    assert task["statusMessage"] is None
    assert task["artifacts"][0]["parts"][0]["text"] == \
        "DRAFT: brief A2A summary."
    assert [h["role"] for h in task["history"]] == ["user", "agent"]
    # The runtime received the published agent config + caller token.
    assert seen["agent"]["key"] == "a2a-worker"
    assert seen["apiToken"] == "clinician-token"
    # An AgentRun was recorded through app.agents' machinery.
    assert task["agentRunId"]
    runs = client.get("/agents/runs", headers=CLINICIAN).json()
    assert [r["run_id"] for r in runs] == [task["agentRunId"]]
    assert runs[0]["output"] == "DRAFT: brief A2A summary."
    actions = [a for _, a, _, _ in _audit_rows(client)]
    assert "a2a.task.completed" in actions and "agents.run" in actions


def test_a2a_message_stream_returns_task_and_stream_url(client,
                                                        monkeypatch):
    _publish_agent(client, "a2a-worker", model="")
    _enable(client, "a2a-worker")  # BD-41 + C-AI gates
    monkeypatch.setattr(agents_module, "_post_run",
                        lambda payload: (503, {"error": "No model "
                                                       "configured."}))
    task = _a2a_result(_a2a_rpc(client, {
        "jsonrpc": "2.0", "id": 1, "method": "message/stream",
        "params": {"agentKey": "a2a-worker", "message": "hi"}}))
    assert task["status"] == "failed"
    assert task["streamUrl"] == f"/a2a/tasks/{task['id']}/stream"
    r = client.get(task["streamUrl"], headers=CLINICIAN)
    assert r.status_code == 200
    assert "event: task-status" in r.text


def test_a2a_tasks_cancel_non_terminal_and_terminal_refusal(client):
    _publish_agent(client, "a2a-worker")
    _enable(client, "a2a-worker")  # BD-41 + C-AI gates
    # A non-terminal task (inserted in `submitted`, as a long-running
    # task would sit) can be canceled; a terminal one cannot.
    factory = client.app.state.session_factory
    with factory() as session:
        session.add(mcp_a2a.A2aTask(
            task_id="task-cancel-1", agent_key="a2a-worker",
            actor="dev-clinician", status="submitted",
            message_hash="0" * 64, history=[], artifacts=[],
            status_history=[{"status": "submitted", "at": "2026-10-19"
                             "T00:00:00+00:00", "message": None}]))
        session.commit()
    task = _a2a_result(_a2a_rpc(client, {
        "jsonrpc": "2.0", "id": 1, "method": "tasks/cancel",
        "params": {"id": "task-cancel-1"}}))
    assert task["status"] == "canceled"
    assert "a2a.task.cancel" in [a for _, a, _, _ in _audit_rows(client)]
    error = _a2a_rpc(client, {
        "jsonrpc": "2.0", "id": 2, "method": "tasks/cancel",
        "params": {"id": "task-cancel-1"}}).json()["error"]
    assert error["code"] == mcp_a2a.ERR_TASK_TERMINAL
    # Unknown task id is invalid params for both get and cancel.
    for method in ("tasks/get", "tasks/cancel"):
        error = _a2a_rpc(client, {
            "jsonrpc": "2.0", "id": 3, "method": method,
            "params": {"id": "nope"}}).json()["error"]
        assert error["code"] == -32602
    # REST read + stream 404 for unknown tasks.
    assert client.get("/a2a/tasks/nope", headers=CLINICIAN).status_code \
        == 404
    assert client.get("/a2a/tasks/nope/stream",
                      headers=CLINICIAN).status_code == 404


def test_a2a_delegation_header_admin_accepted_clinician_refused(
        client, monkeypatch):
    _publish_agent(client, "a2a-worker", model="")
    _enable(client, "a2a-worker")  # BD-41 + C-AI gates
    monkeypatch.setattr(agents_module, "_post_run",
                        lambda payload: (503, {"error": "No model "
                                                       "configured."}))
    # Admin: the header is accepted and recorded.
    task = _a2a_result(_a2a_rpc(
        client, {"jsonrpc": "2.0", "id": 1, "method": "message/send",
                 "params": {"agentKey": "a2a-worker", "message": "hi"}},
        headers=ADMIN,
        extra_headers={"X-OpenCare-On-Behalf-Of":
                       "Practitioner/dr-smith"}))
    assert task["onBehalfOf"] == "Practitioner/dr-smith"
    creates = [p for _, a, _, p in _audit_rows(client)
               if a == "a2a.task.create"]
    delegation = creates[-1]["delegation"]
    assert delegation["accepted"] is True
    assert delegation["on_behalf_of"] == "Practitioner/dr-smith"

    # Clinician: the header is refused — ignored, with the refusal
    # recorded in the audit entry; the task carries no delegation.
    task = _a2a_result(_a2a_rpc(
        client, {"jsonrpc": "2.0", "id": 2, "method": "message/send",
                 "params": {"agentKey": "a2a-worker", "message": "hi"}},
        headers=CLINICIAN,
        extra_headers={"X-OpenCare-On-Behalf-Of":
                       "Practitioner/dr-smith"}))
    assert task["onBehalfOf"] is None
    creates = [p for _, a, _, p in _audit_rows(client)
               if a == "a2a.task.create"]
    delegation = creates[-1]["delegation"]
    assert delegation["accepted"] is False
    assert delegation["on_behalf_of"] is None
    assert "may not delegate" in delegation["refused_reason"]
