"""Phase 16, Workstream D — MCP + full A2A as governed Studio
configuration (app.mcp_a2a).

Two real protocol surfaces, both driven by governed configuration —
no stubs, no hardcoded peers:

MCP CLIENT (this system calls external MCP servers)
  External servers are published Studio config, kind ``mcp_server``
  (registered centrally by the coordinator; payload shape validated
  by :func:`validate_mcp_server_payload` in this module):
  ``{name, transport: "http", url, toolAllowlist, tools?}`` where the
  optional ``tools`` list may declare a per-tool
  ``requiredCapability``. Publishing a server does NOT turn it on:
  enable/disable are separate governed actions on this module's
  endpoints (``POST /mcp/servers/{key}/enable|disable``,
  ``studio.manage``), audited exactly like agent enablement in
  app.agents. The client speaks JSON-RPC over httpx
  (``trust_env=False``): an ``initialize`` handshake, then
  ``tools/list`` / ``tools/call``.

  Patient-scope guardrail (checked BEFORE any HTTP leaves this
  process): a call whose arguments carry a patientId naming a patient
  the caller cannot access is refused 403 — a patient-role caller may
  only name their own linked patient (the engagement.py portal
  pattern), and a staff caller must hold the tool's declared
  ``requiredCapability`` when the server config declares one. Every
  call — allowed, refused, or failed upstream — is audited
  ``mcp.tool.call`` with {server, tool, patient ref, status} and
  NEVER the raw arguments or result payloads.

  Credential law (mirrors app.ai_providers): a server config never
  carries secrets. Credential-shaped top-level fields are refused
  422 by the validator, URLs with embedded userinfo are refused, and
  this client never forwards config-supplied headers — only
  Content-Type/Accept.

MCP SERVER (this system exposes governed read tools at ``POST /mcp``)
  Tool declarations live in published ``integration/mcp-tools``:
  ``{tools: [{name, description, requiredCapability, patientScoped,
  enabled}]}`` — seeded published with ALL tools DISABLED
  (patient_summary, lab_results_recent, schedule_day; read-only).
  A ``tools/call`` executes only when the tool is enabled in the
  published doc AND the caller holds its requiredCapability (a
  patient caller is the exception for patientScoped tools: they may
  call for their OWN record only) AND patient-scope passes. Every
  call is audited ``mcp.tool.call`` with surface="server".

A2A (agent-to-agent, HTTP + JSON-RPC binding)
  Node metadata is published ``integration/a2a`` (seeded).
  ``GET /.well-known/agent-card.json`` builds the node AgentCard from
  that metadata plus the published+ENABLED agent configs (skills =
  enabled agents — see the integration note for the mapping).
  ``POST /a2a/rpc`` implements ``message/send``, ``tasks/get``,
  ``tasks/cancel`` and ``message/stream``. STREAMING BINDING CHOICE:
  ``message/stream`` creates the task exactly like ``message/send``
  and returns it with a ``streamUrl``; the SSE stream itself is the
  companion ``GET /a2a/tasks/{id}/stream`` (text/event-stream
  replaying the task's recorded status transitions, then closing).
  Task states: submitted -> working -> completed | failed, plus
  canceled for a non-terminal task cancelled via tasks/cancel.

  A task executes its agent THROUGH app.agents (its runtime client
  ``_post_run`` + enablement checks). Honest-off: no model / runtime
  down ⇒ the task ends ``failed`` with the runtime's honest reason in
  its status message — never a fabricated completion, and (mirroring
  app.agents) no AgentRun is recorded for a failed execution. A
  successful execution records the AgentRun + ``agents.run`` audit
  exactly like a direct run, so the disposition flow applies.

  Delegation: an ``X-OpenCare-On-Behalf-Of`` header is accepted ONLY
  from admin/configurator callers and is then recorded in the audit
  entry (and on the task). From any other role it is ignored, with
  the refusal recorded in the audit entry — never silently applied.

Capabilities: ``mcp.use`` / ``a2a.use`` are NEW capabilities the
coordinator wires into CAPABILITY_DEFAULTS centrally (proposed
defaults in the integration note). Until that wiring lands,
:func:`_cap_allowed` falls back to this module's proposed defaults
for those two names only; every other capability resolves through
app.configstore.role_allowed exactly as elsewhere, and an unknown
capability fails closed.
"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import (Boolean, DateTime, Integer, JSON, String, Text,
                        select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import agents as agents_module
from .audit import append_audit
from .configstore import (CAPABILITY_DEFAULTS, list_config_versions,
                          published_config, published_configs,
                          role_allowed)
from .db import Base
from .models import (AgentRun, ConfigStatus, FhirResource, Role,
                     StudioConfig, User, utcnow)
from .security import get_current_user, get_db, require_capability

# ---------------------------------------------------------------------------
# Routers: one combined `router` for the coordinator to mount, built
# from the three surfaces.
# ---------------------------------------------------------------------------

mcp_router = APIRouter(prefix="/mcp", tags=["mcp"])
a2a_router = APIRouter(prefix="/a2a", tags=["a2a"])
wellknown_router = APIRouter(tags=["a2a-discovery"])

MCP_TOOLS_KEY = "mcp-tools"          # integration/mcp-tools
A2A_KEY = "a2a"                      # integration/a2a
MCP_SERVER_KIND = "mcp_server"
MCP_PROTOCOL_VERSION = "2024-11-05"
A2A_PROTOCOL_VERSION = "0.3.0"

# Proposed defaults for the two NEW capabilities (coordinator wires
# these into CAPABILITY_DEFAULTS; used here only as a fallback until
# then). mcp.use includes patient because the patient-scope guardrail
# — not the capability — is what limits a patient caller to their own
# record. a2a.use mirrors agents.run (a task executes an agent).
_FALLBACK_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "mcp.use": ("admin", "configurator", "clinician", "viewer",
                "patient"),
    "a2a.use": ("admin", "configurator", "clinician"),
}

TERMINAL_TASK_STATES = ("completed", "failed", "canceled")


def _cap_allowed(session: Session, user: User, capability: str) -> bool:
    """role_allowed, with a module-local fallback ONLY for the new
    Phase 16 capabilities that are not yet in CAPABILITY_DEFAULTS
    and have no published permissions override. Anything else
    unknown fails closed via role_allowed's empty default."""
    if capability in CAPABILITY_DEFAULTS:
        return role_allowed(session, user.role, capability)
    cfg = published_config(session, "permissions", "role-matrix")
    if cfg is not None:
        caps = (cfg.payload or {}).get("capabilities", {})
        if isinstance(caps.get(capability), dict) \
                and user.role.value in caps[capability]:
            return role_allowed(session, user.role, capability)
    return user.role.value in _FALLBACK_CAPABILITIES.get(capability, ())


def _require_mcp_use(session: Session, user: User) -> None:
    if not _cap_allowed(session, user, "mcp.use"):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.value} lacks capability 'mcp.use'")


# ---------------------------------------------------------------------------
# This module's own tables
# ---------------------------------------------------------------------------

class McpServerEnablement(Base):
    """Operational enablement for one published mcp_server config —
    the AgentEnablement pattern: the DEFINITION is Studio config
    (draft -> publish, immutable); whether it may be CALLED is this
    separate, audited flip."""
    __tablename__ = "mcp_server_enablements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_key: Mapped[str] = mapped_column(String(120), unique=True,
                                            nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                          default=False)
    updated_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class A2aTask(Base):
    """One A2A task: a message sent to one enabled agent, executed
    through app.agents. `history` is the A2A message history (the
    sender's message plus, on success, the agent's reply message);
    `artifacts` carries the agent's draft output on completion;
    `status_history` records every state transition for the SSE
    stream replay. The raw prompt is kept ONLY here (the task is the
    sender's own artefact, returned to authorised callers); audits
    carry its sha256, never the text."""
    __tablename__ = "a2a_tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    agent_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                           index=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False,
                                       default="")
    on_behalf_of: Mapped[str | None] = mapped_column(String(200),
                                                     nullable=True)
    patient_ref: Mapped[str | None] = mapped_column(String(200),
                                                    nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="submitted", index=True)
    status_message: Mapped[str | None] = mapped_column(Text,
                                                       nullable=True)
    message_hash: Mapped[str] = mapped_column(String(64), nullable=False,
                                              default="")
    history: Mapped[list] = mapped_column(JSON, nullable=False,
                                          default=list)
    artifacts: Mapped[list] = mapped_column(JSON, nullable=False,
                                            default=list)
    status_history: Mapped[list] = mapped_column(JSON, nullable=False,
                                                 default=list)
    agent_run_id: Mapped[str | None] = mapped_column(String(120),
                                                     nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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


def _normalize_patient_ref(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    return value if value.startswith("Patient/") else f"Patient/{value}"


_PATIENT_ARG_KEYS = ("patientId", "patient_id", "patientRef",
                     "patient_ref", "patient")


def _extract_patient_ref(arguments) -> str | None:
    """The patient a tool call names, from its TOP-LEVEL arguments
    only (patientId is the canonical MCP argument name here)."""
    if not isinstance(arguments, dict):
        return None
    for key in _PATIENT_ARG_KEYS:
        ref = _normalize_patient_ref(arguments.get(key))
        if ref:
            return ref
    return None


def _patient_scope_ok(user: User, patient_ref: str) -> bool:
    """The engagement.py portal pattern: a patient caller may name
    exactly their own linked record, nobody else's."""
    return bool(user.patient_ref) and patient_ref == user.patient_ref


def _delegation(request: Request, user: User) -> dict:
    """Resolve X-OpenCare-On-Behalf-Of for one request. Accepted only
    from admin/configurator; otherwise recorded as refused and NOT
    applied. The returned dict is written into audit payloads."""
    header = request.headers.get("X-OpenCare-On-Behalf-Of")
    if not header or not header.strip():
        return {"header_present": False, "accepted": False,
                "on_behalf_of": None}
    if user.role in (Role.admin, Role.configurator):
        return {"header_present": True, "accepted": True,
                "on_behalf_of": header.strip()}
    return {"header_present": True, "accepted": False,
            "on_behalf_of": None,
            "refused_reason": (f"role {user.role.value} may not "
                               "delegate; header ignored")}


# JSON-RPC envelopes -----------------------------------------------------------

def _rpc_result(req_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id, code: int, message: str, data: dict | None = None) -> dict:
    error: dict = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INVALID_REQUEST = -32600
ERR_TOOL_DISABLED = -32001
ERR_FORBIDDEN = -32003
ERR_TASK_TERMINAL = -32002


# ---------------------------------------------------------------------------
# mcp_server payload validation (coordinator registers this as the
# `mcp_server` kind validator in app.configstore)
# ---------------------------------------------------------------------------

_FORBIDDEN_CREDENTIAL_FIELDS = {"apikey", "api_key", "secret",
                                "password", "token", "key",
                                "credentials", "authorization"}


def validate_mcp_server_payload(payload) -> dict:
    """Validate one mcp_server config payload; returns it unchanged.
    Raises HTTPException 422. The credential scan runs FIRST: secrets
    are never stored in configuration (the ai_providers law) — a
    server needing auth references it outside config, never here."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422,
                            detail="mcp_server definition must be a "
                                   "JSON object")
    found = [k for k in payload
             if str(k).lower() in _FORBIDDEN_CREDENTIAL_FIELDS]
    if found:
        raise HTTPException(
            status_code=422,
            detail=(f"Credential field(s) {found} are forbidden in an "
                    "mcp_server definition: secrets are never stored "
                    "in configuration."))
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=422,
                            detail="'name' is required (non-empty string)")
    url = payload.get("url")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(status_code=422,
                            detail="'url' is required (non-empty string)")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=422,
                            detail="'url' must be an http(s) URL")
    if parsed.username or parsed.password:
        raise HTTPException(
            status_code=422,
            detail="'url' must not embed credentials (userinfo); "
                   "secrets are never stored in configuration")
    transport = payload.get("transport", "http")
    if transport != "http":
        raise HTTPException(
            status_code=422,
            detail=f"'transport' must be 'http'; got '{transport}'")
    allowlist = payload.get("toolAllowlist")
    if not isinstance(allowlist, list) or any(
            not isinstance(t, str) or not t for t in allowlist):
        raise HTTPException(
            status_code=422,
            detail="'toolAllowlist' is required (a list of tool names; "
                   "an empty list allows no tools)")
    tools = payload.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise HTTPException(status_code=422,
                                detail="'tools' must be a list")
        for i, tool in enumerate(tools):
            if not isinstance(tool, dict) \
                    or not isinstance(tool.get("name"), str) \
                    or not tool["name"]:
                raise HTTPException(
                    status_code=422,
                    detail=f"tools[{i}] must be an object with a "
                           "non-empty 'name'")
            cap = tool.get("requiredCapability")
            if cap is not None and not isinstance(cap, str):
                raise HTTPException(
                    status_code=422,
                    detail=f"tools[{i}].requiredCapability must be a string")
    caps = payload.get("toolCapabilities")
    if caps is not None and (not isinstance(caps, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in caps.items())):
        raise HTTPException(
            status_code=422,
            detail="'toolCapabilities' must be an object mapping "
                   "tool name -> capability string")
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        raise HTTPException(status_code=422,
                            detail="'enabled' must be a boolean")
    return payload


def _declared_tool_capability(payload: dict, tool: str) -> str | None:
    caps = payload.get("toolCapabilities")
    if isinstance(caps, dict) and isinstance(caps.get(tool), str):
        return caps[tool]
    for entry in payload.get("tools") or []:
        if isinstance(entry, dict) and entry.get("name") == tool:
            cap = entry.get("requiredCapability")
            return cap if isinstance(cap, str) and cap else None
    return None


def _public_server(key: str, cfg, enabled: bool) -> dict:
    """The client-facing shape of one mcp_server config — whitelisted
    fields only, so nothing credential-shaped stored by any other
    path can ever leak through this surface."""
    payload = cfg.payload or {}
    return {"key": key, "version": cfg.version,
            "name": payload.get("name"),
            "transport": payload.get("transport", "http"),
            "url": payload.get("url"),
            "toolAllowlist": payload.get("toolAllowlist") or [],
            "tools": [{"name": t.get("name"),
                       "requiredCapability": t.get("requiredCapability")}
                      for t in payload.get("tools") or []
                      if isinstance(t, dict)],
            "enabled": enabled}


# ---------------------------------------------------------------------------
# Seeded published integration docs: integration/mcp-tools +
# integration/a2a
# ---------------------------------------------------------------------------

def default_mcp_tools_payload() -> dict:
    return {
        "name": "OpenCare MCP server tools (governed declarations)",
        "tools": [
            {"name": "patient_summary",
             "description": ("Read-only summary of one patient: "
                             "demographics plus condition/encounter "
                             "counts from the chart."),
             "requiredCapability": "notes.read",
             "patientScoped": True, "enabled": False},
            {"name": "lab_results_recent",
             "description": ("Read-only: a patient's most recent lab "
                             "observations and diagnostic reports."),
             "requiredCapability": "lab.read",
             "patientScoped": True, "enabled": False},
            {"name": "schedule_day",
             "description": ("Read-only: appointments on one day, "
                             "optionally for one patient."),
             "requiredCapability": "inpatient.read",
             "patientScoped": True, "enabled": False},
        ],
    }


def default_a2a_payload() -> dict:
    return {
        "name": "OpenCare",
        "description": ("OpenCare EHR agent node: governed Studio "
                        "agents exposed over A2A. Agents draft; "
                        "humans dispose."),
        "url": "http://127.0.0.1:8000/a2a/rpc",
        "version": "1.0.0",
    }


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


def ensure_mcp_a2a_defaults(session: Session) -> None:
    """Seed published integration/mcp-tools (all tools DISABLED) and
    integration/a2a node metadata (idempotent)."""
    _seed_published(session, "integration", MCP_TOOLS_KEY,
                    default_mcp_tools_payload())
    _seed_published(session, "integration", A2A_KEY,
                    default_a2a_payload())
    session.commit()


def _mcp_tools(session: Session) -> list[dict]:
    cfg = published_config(session, "integration", MCP_TOOLS_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    tools = payload.get("tools")
    if not isinstance(tools, list):
        tools = default_mcp_tools_payload()["tools"]
    return [t for t in tools if isinstance(t, dict) and t.get("name")]


def _a2a_node(session: Session) -> dict:
    cfg = published_config(session, "integration", A2A_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    node = dict(default_a2a_payload())
    for field in ("name", "description", "url", "version"):
        if isinstance(payload.get(field), str) and payload[field]:
            node[field] = payload[field]
    return node


# ===========================================================================
# MCP CLIENT
# ===========================================================================

def _server_enablement(session: Session, key: str) \
        -> McpServerEnablement | None:
    return session.scalars(select(McpServerEnablement).where(
        McpServerEnablement.server_key == key)).first()


def _server_enabled(session: Session, key: str) -> bool:
    row = _server_enablement(session, key)
    return bool(row and row.enabled)


def _audit_mcp_call(session: Session, actor: str, surface: str,
                    server: str | None, tool: str | None,
                    patient_ref: str | None, status: str,
                    extra: dict | None = None) -> None:
    payload = {"surface": surface, "server": server, "tool": tool,
               "patient": patient_ref, "status": status}
    if extra:
        payload.update(extra)
    append_audit(session, actor=actor, action="mcp.tool.call",
                 entity=(f"mcp_server:{server}" if server
                         else "mcp:server"),
                 payload=payload)
    session.commit()


def _mcp_rpc_sequence(url: str, method: str, params: dict) -> dict:
    """One MCP exchange against an external server: initialize
    handshake, then the requested method, over httpx with
    trust_env=False (operator-configured local-network URLs must
    never be rerouted by ambient proxy env). Returns the JSON-RPC
    response body of the requested method. Raises httpx.HTTPError on
    transport failure. No headers from config are ever sent."""
    headers = {"Content-Type": "application/json",
               "Accept": "application/json"}
    with httpx.Client(trust_env=False, timeout=10.0) as client:
        client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": MCP_PROTOCOL_VERSION,
                       "capabilities": {},
                       "clientInfo": {"name": "OpenCare MCP client",
                                      "version": "1.0"}}})
        response = client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": method,
            "params": params})
    try:
        body = response.json()
    except ValueError:
        body = {"error": {"code": -32000,
                          "message": response.text[:300]}}
    return body if isinstance(body, dict) else {"result": body}


@mcp_router.get("/servers")
def list_mcp_servers(session: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    _require_mcp_use(session, user)
    servers = [_public_server(cfg.key, cfg,
                              _server_enabled(session, cfg.key))
               for cfg in published_configs(session, MCP_SERVER_KIND)]
    return {"servers": servers, "total": len(servers)}


def _set_server_enabled(key: str, enabled: bool, session: Session,
                        user: User) -> dict:
    cfg = published_config(session, MCP_SERVER_KIND, key)
    if cfg is None:
        raise HTTPException(
            status_code=409,
            detail=f"MCP server '{key}' has no published "
                   f"{MCP_SERVER_KIND} config; publish a definition "
                   "before enabling it")
    row = _server_enablement(session, key)
    if row is None:
        row = McpServerEnablement(server_key=key, enabled=False)
        session.add(row)
    row.enabled = enabled
    row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username,
                 action=("mcp.server.enable" if enabled
                         else "mcp.server.disable"),
                 entity=f"mcp_server:{key}@v{cfg.version}",
                 payload={"server": key, "version": cfg.version,
                          "enabled": enabled})
    session.commit()
    return _public_server(key, cfg, enabled)


@mcp_router.post("/servers/{key}/enable")
def enable_mcp_server(key: str, session: Session = Depends(get_db),
                      user: User = Depends(
                          require_capability("studio.manage"))):
    return _set_server_enabled(key, True, session, user)


@mcp_router.post("/servers/{key}/disable")
def disable_mcp_server(key: str, session: Session = Depends(get_db),
                       user: User = Depends(
                           require_capability("studio.manage"))):
    return _set_server_enabled(key, False, session, user)


def _load_enabled_server(session: Session, user: User, key: str,
                         tool: str | None):
    """Published + enabled gate shared by tools/list and tools/call.
    Audits the refusal as an mcp.tool.call status and raises."""
    cfg = published_config(session, MCP_SERVER_KIND, key)
    if cfg is None:
        _audit_mcp_call(session, user.username, "client", key, tool,
                        None, "refused-unknown-server")
        raise HTTPException(status_code=404,
                            detail=f"No published mcp_server config "
                                   f"for '{key}'")
    if not _server_enabled(session, key):
        _audit_mcp_call(session, user.username, "client", key, tool,
                        None, "refused-not-enabled")
        raise HTTPException(
            status_code=409,
            detail=f"MCP server '{key}' is published but not enabled; "
                   "an operator enables it (POST /mcp/servers/"
                   f"{key}/enable) before it can be called")
    return cfg


@mcp_router.get("/servers/{key}/tools")
def list_mcp_server_tools(key: str, session: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    _require_mcp_use(session, user)
    cfg = _load_enabled_server(session, user, key, None)
    try:
        body = _mcp_rpc_sequence(str((cfg.payload or {}).get("url")),
                                 "tools/list", {})
    except httpx.HTTPError:
        _audit_mcp_call(session, user.username, "client", key, None,
                        None, "upstream-unavailable")
        raise HTTPException(status_code=502,
                            detail=f"MCP server '{key}' is unreachable")
    if "error" in body:
        _audit_mcp_call(session, user.username, "client", key, None,
                        None, "upstream-error")
        raise HTTPException(status_code=502,
                            detail=f"MCP server '{key}' returned an "
                                   f"error for tools/list")
    append_audit(session, actor=user.username, action="mcp.tools.list",
                 entity=f"mcp_server:{key}",
                 payload={"server": key, "status": "ok"})
    session.commit()
    tools = (body.get("result") or {}).get("tools") or []
    return {"server": key, "tools": tools}


@mcp_router.post("/servers/{key}/call")
async def call_mcp_server_tool(key: str, request: Request,
                               session: Session = Depends(get_db),
                               user: User = Depends(get_current_user)):
    """Call one tool on one published+ENABLED external MCP server.
    The guardrails (allowlist, declared capability, patient scope)
    all run BEFORE any HTTP; every outcome is audited without raw
    payloads."""
    _require_mcp_use(session, user)
    body = await _json_body(request)
    tool = body.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise HTTPException(status_code=422,
                            detail="Call requires a non-empty 'tool'")
    tool = tool.strip()
    arguments = body.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise HTTPException(status_code=422,
                            detail="'arguments' must be a JSON object")
    cfg = _load_enabled_server(session, user, key, tool)
    payload = cfg.payload or {}
    patient_ref = _extract_patient_ref(arguments)

    allowlist = payload.get("toolAllowlist") or []
    if tool not in allowlist:
        _audit_mcp_call(session, user.username, "client", key, tool,
                        patient_ref, "refused-allowlist")
        raise HTTPException(
            status_code=403,
            detail=f"Tool '{tool}' is not in the published "
                   f"toolAllowlist for MCP server '{key}'")

    # Patient-scope + declared-capability guardrail — BEFORE HTTP.
    if user.role == Role.patient:
        if patient_ref is not None \
                and not _patient_scope_ok(user, patient_ref):
            _audit_mcp_call(session, user.username, "client", key,
                            tool, patient_ref,
                            "refused-patient-scope")
            raise HTTPException(
                status_code=403,
                detail="Patient callers may only call tools for "
                       "their own record")
    else:
        required = _declared_tool_capability(payload, tool)
        if required and not _cap_allowed(session, user, required):
            _audit_mcp_call(session, user.username, "client", key,
                            tool, patient_ref, "refused-capability",
                            {"required_capability": required})
            raise HTTPException(
                status_code=403,
                detail=f"Role {user.role.value} lacks capability "
                       f"'{required}' required by tool '{tool}' on "
                       f"MCP server '{key}'")

    try:
        result_body = _mcp_rpc_sequence(
            str(payload.get("url")), "tools/call",
            {"name": tool, "arguments": arguments})
    except httpx.HTTPError:
        _audit_mcp_call(session, user.username, "client", key, tool,
                        patient_ref, "upstream-unavailable")
        raise HTTPException(status_code=502,
                            detail=f"MCP server '{key}' is unreachable")
    status = "upstream-error" if "error" in result_body else "ok"
    _audit_mcp_call(session, user.username, "client", key, tool,
                    patient_ref, status)
    return result_body


# ===========================================================================
# MCP SERVER — governed read tools at POST /mcp
# ===========================================================================

def _tool_input_schema() -> dict:
    return {"type": "object",
            "properties": {
                "patientId": {"type": "string"},
                "date": {"type": "string"},
                "limit": {"type": "integer"}},
            "required": []}


def _patient_summary(session: Session, patient_ref: str) -> dict:
    patient_id = patient_ref.split("/", 1)[1]
    row = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Patient",
        FhirResource.resource_id == patient_id)).first()
    if row is None:
        raise _ToolError(ERR_INVALID_PARAMS,
                         f"Patient '{patient_ref}' not found")
    body = row.body or {}
    counts: dict[str, int] = {}
    for rtype in ("Condition", "Encounter", "Observation",
                  "DiagnosticReport", "Appointment"):
        counts[rtype] = len(session.scalars(select(FhirResource).where(
            FhirResource.resource_type == rtype,
            FhirResource.patient_ref == patient_ref)).all())
    return {"patient": patient_ref, "name": body.get("name") or [],
            "gender": body.get("gender"),
            "birthDate": body.get("birthDate"),
            "active": body.get("active"), "counts": counts}


def _lab_results_recent(session: Session, patient_ref: str,
                        arguments: dict) -> dict:
    limit = arguments.get("limit", 5)
    if isinstance(limit, bool) or not isinstance(limit, int) \
            or limit < 1 or limit > 50:
        raise _ToolError(ERR_INVALID_PARAMS,
                         "'limit' must be an integer in 1..50")
    rows = session.scalars(
        select(FhirResource)
        .where(FhirResource.resource_type.in_(
            ("Observation", "DiagnosticReport")),
            FhirResource.patient_ref == patient_ref)
        .order_by(FhirResource.id.desc()).limit(limit)).all()
    items = []
    for row in rows:
        body = row.body or {}
        coding = ((body.get("code") or {}).get("coding") or [{}])
        items.append({
            "resourceType": row.resource_type, "id": row.resource_id,
            "status": body.get("status"),
            "code": coding[0].get("code") if coding else None,
            "display": coding[0].get("display") if coding else None,
            "valueQuantity": body.get("valueQuantity"),
            "effectiveDateTime": body.get("effectiveDateTime")})
    return {"patient": patient_ref, "results": items,
            "total": len(items)}


def _schedule_day(session: Session, arguments: dict) -> dict:
    date_str = arguments.get("date")
    if date_str is None:
        date_str = _now().date().isoformat()
    if not isinstance(date_str, str):
        raise _ToolError(ERR_INVALID_PARAMS, "'date' must be a string")
    try:
        day = datetime.fromisoformat(date_str).date()
    except ValueError:
        raise _ToolError(ERR_INVALID_PARAMS,
                         f"'date' is not a valid date: '{date_str}'")
    patient_ref = _extract_patient_ref(arguments)
    stmt = select(FhirResource).where(
        FhirResource.resource_type == "Appointment")
    if patient_ref:
        stmt = stmt.where(FhirResource.patient_ref == patient_ref)
    items = []
    for row in session.scalars(stmt).all():
        if row.start_at is None or row.start_at.date() != day:
            continue
        items.append({"id": row.resource_id,
                      "status": (row.body or {}).get("status"),
                      "start": (row.body or {}).get("start"),
                      "patient": row.patient_ref})
    items.sort(key=lambda i: str(i.get("start") or ""))
    return {"date": day.isoformat(), "patient": patient_ref,
            "appointments": items, "total": len(items)}


class _ToolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _execute_mcp_tool(session: Session, name: str,
                      arguments: dict) -> dict:
    if name == "patient_summary":
        ref = _extract_patient_ref(arguments)
        if not ref:
            raise _ToolError(ERR_INVALID_PARAMS,
                             "patient_summary requires 'patientId'")
        return _patient_summary(session, ref)
    if name == "lab_results_recent":
        ref = _extract_patient_ref(arguments)
        if not ref:
            raise _ToolError(ERR_INVALID_PARAMS,
                             "lab_results_recent requires 'patientId'")
        return _lab_results_recent(session, ref, arguments)
    if name == "schedule_day":
        return _schedule_day(session, arguments)
    raise _ToolError(ERR_INVALID_PARAMS, f"Unknown tool '{name}'")


@mcp_router.post("")
async def mcp_server_endpoint(request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """The OpenCare MCP server: one JSON-RPC endpoint. Bearer auth
    (401 via the dependency when absent). Method errors are JSON-RPC
    error envelopes, never HTTP errors, once the caller is
    authenticated."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content=_rpc_error(
            None, ERR_INVALID_REQUEST, "Request body must be valid JSON"))
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content=_rpc_error(
            None, ERR_INVALID_REQUEST, "JSON-RPC request must be an object"))
    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    if not isinstance(method, str) or not method:
        return _rpc_error(req_id, ERR_INVALID_REQUEST,
                          "JSON-RPC request requires a 'method'")
    if not isinstance(params, dict):
        return _rpc_error(req_id, ERR_INVALID_PARAMS,
                          "'params' must be an object")
    if method.startswith("notifications/"):
        return JSONResponse(status_code=202, content=None)

    if method == "initialize":
        requested = params.get("protocolVersion")
        return _rpc_result(req_id, {
            "protocolVersion": (requested if isinstance(requested, str)
                                and requested else MCP_PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "OpenCare MCP", "version": "1.0.0"}})
    if method == "ping":
        return _rpc_result(req_id, {})
    if method == "tools/list":
        if not _cap_allowed(session, user, "mcp.use"):
            return _rpc_error(req_id, ERR_FORBIDDEN,
                              f"Role {user.role.value} lacks "
                              "capability 'mcp.use'")
        tools = [{"name": t["name"],
                  "description": t.get("description") or "",
                  "inputSchema": _tool_input_schema()}
                 for t in _mcp_tools(session)
                 if t.get("enabled") is True]
        return _rpc_result(req_id, {"tools": tools})
    if method == "tools/call":
        return _mcp_server_tool_call(session, user, req_id, params)
    return _rpc_error(req_id, ERR_METHOD_NOT_FOUND,
                      f"Method not found: {method}")


def _mcp_server_tool_call(session: Session, user: User, req_id,
                          params: dict):
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(name, str) or not name:
        return _rpc_error(req_id, ERR_INVALID_PARAMS,
                          "tools/call requires a tool 'name'")
    if not isinstance(arguments, dict):
        return _rpc_error(req_id, ERR_INVALID_PARAMS,
                          "tools/call 'arguments' must be an object")
    patient_ref = _extract_patient_ref(arguments)

    def _refuse(code, status, message, extra=None):
        _audit_mcp_call(session, user.username, "server", None, name,
                        patient_ref, status, extra)
        return _rpc_error(req_id, code, message)

    if not _cap_allowed(session, user, "mcp.use"):
        return _refuse(ERR_FORBIDDEN, "refused-capability",
                       f"Role {user.role.value} lacks capability "
                       "'mcp.use'")
    declared = {t["name"]: t for t in _mcp_tools(session)}
    tool = declared.get(name)
    if tool is None:
        return _refuse(ERR_INVALID_PARAMS, "refused-unknown-tool",
                       f"Tool '{name}' is not declared in the "
                       "published integration/mcp-tools config")
    if tool.get("enabled") is not True:
        return _refuse(ERR_TOOL_DISABLED, "refused-tool-disabled",
                       f"Tool '{name}' is disabled in the published "
                       "integration/mcp-tools config")
    # C-AI kill switch (AI-G5): while the AI governance kill switch
    # is active, EVERY agent-reachable tool refuses, whatever its
    # per-tool enablement says.
    from . import ai_governance as _gov
    if _gov.kill_switch_active(session):
        _ks_reason = ""
        try:
            from .ai_governance import AiGovernanceState
            _state = session.scalars(
                select(AiGovernanceState)).first()
            if _state is not None:
                _ks_reason = str(getattr(_state, "kill_reason", "")
                                 or getattr(_state, "kill_switch_reason", "")
                                 or getattr(_state, "reason", "") or "")
        except Exception:
            _ks_reason = ""
        _ks_msg = ("All agent-reachable tools are disabled: the AI "
                   "governance kill switch is active")
        if _ks_reason:
            _ks_msg += f" ({_ks_reason})"
        return _refuse(ERR_TOOL_DISABLED, "refused-kill-switch", _ks_msg)
    # Patient-scope before capability: a patient caller may use a
    # patientScoped tool for their OWN record only (portal pattern);
    # every other caller must hold the tool's requiredCapability.
    if user.role == Role.patient:
        if not tool.get("patientScoped"):
            return _refuse(ERR_FORBIDDEN, "refused-capability",
                           f"Tool '{name}' is not patient-scoped; "
                           "patient callers may not use it")
        if patient_ref is None:
            return _rpc_error(req_id, ERR_INVALID_PARAMS,
                              f"Tool '{name}' requires 'patientId'")
        if not _patient_scope_ok(user, patient_ref):
            return _refuse(ERR_FORBIDDEN, "refused-patient-scope",
                           "Patient callers may only access their "
                           "own record")
    else:
        required = tool.get("requiredCapability")
        if isinstance(required, str) and required \
                and not _cap_allowed(session, user, required):
            return _refuse(ERR_FORBIDDEN, "refused-capability",
                           f"Role {user.role.value} lacks capability "
                           f"'{required}' required by tool '{name}'",
                           {"required_capability": required})
    try:
        data = _execute_mcp_tool(session, name, arguments)
    except _ToolError as exc:
        _audit_mcp_call(session, user.username, "server", None, name,
                        patient_ref, "error",
                        {"error_code": exc.code})
        return _rpc_error(req_id, exc.code, exc.message)
    _audit_mcp_call(session, user.username, "server", None, name,
                    patient_ref, "ok")
    return _rpc_result(req_id, {
        "content": [{"type": "text", "text": json.dumps(data)}],
        "structuredContent": data, "isError": False})


# ===========================================================================
# A2A — AgentCard + JSON-RPC tasks
# ===========================================================================

def build_agent_card(session: Session) -> dict:
    """The node AgentCard: node metadata from published
    integration/a2a; skills from published+ENABLED agent configs
    (one skill per enabled agent: id/name = agent key, description
    from the agent payload, input/output modes from its declared
    modalities)."""
    node = _a2a_node(session)
    skills = []
    for cfg in published_configs(session, "agent"):
        if not agents_module._is_enabled(session, cfg.key):
            continue
        payload = cfg.payload or {}
        modalities = payload.get("modalities") or {}
        input_modes = [m for m, on in
                       (("text", True), ("image", modalities.get("image")),
                        ("audio", modalities.get("audio"))) if on]
        skills.append({
            "id": cfg.key, "name": cfg.key,
            "description": payload.get("description") or "",
            "tags": ["agent"],
            "inputModes": input_modes or ["text"],
            "outputModes": ["text"]})
    return {
        "name": node["name"], "description": node["description"],
        "url": node["url"], "version": node["version"],
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "capabilities": {"streaming": True, "pushNotifications": False},
        "defaultInputModes": ["text"], "defaultOutputModes": ["text"],
        "securitySchemes": {"bearer": {"type": "http",
                                       "scheme": "bearer"}},
        "security": [{"bearer": []}],
        "skills": skills,
    }


@wellknown_router.get("/.well-known/agent-card.json")
def agent_card(session: Session = Depends(get_db)):
    """Public discovery document (the A2A well-known location). It
    carries agent metadata only — no patient data, no secrets."""
    return build_agent_card(session)


def _task_out(task: A2aTask) -> dict:
    return {"id": task.task_id, "contextId": task.task_id,
            "kind": "task", "agentKey": task.agent_key,
            "status": task.status, "statusMessage": task.status_message,
            "patientRef": task.patient_ref, "actor": task.actor,
            "onBehalfOf": task.on_behalf_of,
            "history": task.history or [],
            "artifacts": task.artifacts or [],
            "agentRunId": task.agent_run_id,
            "createdAt": (_aware(task.created_at).isoformat()
                          if task.created_at else None),
            "updatedAt": (_aware(task.updated_at).isoformat()
                          if task.updated_at else None)}


def _transition(session: Session, task: A2aTask, status: str,
                message: str | None = None) -> None:
    task.status = status
    if message is not None:
        task.status_message = message
    history = list(task.status_history or [])
    history.append({"status": status,
                    "at": _now().isoformat(),
                    "message": message})
    task.status_history = history
    task.updated_at = _now()
    session.flush()


def _message_text(params: dict) -> str | None:
    message = params.get("message")
    if isinstance(message, str) and message.strip():
        return message
    if isinstance(message, dict):
        parts = message.get("parts") or []
        texts = [p.get("text") for p in parts
                 if isinstance(p, dict)
                 and isinstance(p.get("text"), str)]
        joined = "".join(texts).strip()
        if joined:
            return joined
    return None


def _execute_task(session: Session, task: A2aTask, user: User,
                  request: Request, delegation: dict) -> None:
    """Run the task's agent through app.agents and settle the task.
    The task is already committed in `working`; every path here ends
    it completed or failed-honest and audits the outcome."""
    cfg = published_config(session, "agent", task.agent_key)
    payload = dict(cfg.payload or {})
    message = (task.history or [{}])[0].get("parts", [{}])[0].get("text", "")
    runtime_payload = {
        "agent": {**payload, "key": task.agent_key},
        "prompt": message,
        "apiBaseUrl": agents_module.api_base_url(),
        "apiToken": request.headers.get("Authorization", "")
        .removeprefix("Bearer ").strip(),
    }
    try:
        status, result = agents_module._post_run(runtime_payload)
    except httpx.HTTPError:
        reason = (f"Agent runtime unavailable at "
                  f"{agents_module.runtime_url()} — start the ADK "
                  "service (backend/adk) or set ADK_RUNTIME_URL. "
                  "No answer was generated.")
        _transition(session, task, "failed", reason)
        session.flush()
        append_audit(session, actor=user.username,
                     action="a2a.task.failed",
                     entity=f"a2a_task:{task.task_id}",
                     payload={"task_id": task.task_id,
                              "agent": task.agent_key,
                              "patient": task.patient_ref,
                              "reason": reason,
                              "delegation": delegation})
        session.commit()
        return
    if status != 200:
        reason = (result.get("error")
                  or f"Agent runtime returned {status}")
        _transition(session, task, "failed", str(reason))
        session.flush()
        append_audit(session, actor=user.username,
                     action="a2a.task.failed",
                     entity=f"a2a_task:{task.task_id}",
                     payload={"task_id": task.task_id,
                              "agent": task.agent_key,
                              "patient": task.patient_ref,
                              "reason": str(reason),
                              "delegation": delegation})
        session.commit()
        return
    output = str(result.get("output") or "")
    input_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
    run = AgentRun(run_id=str(uuid.uuid4()), agent_key=task.agent_key,
                   agent_version=cfg.version, actor=user.username,
                   patient_ref=task.patient_ref, input_hash=input_hash,
                   output=output, model=result.get("model"))
    session.add(run)
    session.flush()
    append_audit(session, actor=user.username, action="agents.run",
                 entity=f"agent:{task.agent_key}@v{cfg.version}",
                 payload={"agent": task.agent_key,
                          "version": cfg.version, "run_id": run.run_id,
                          "input_hash": input_hash,
                          "patient": task.patient_ref,
                          "model": result.get("model"),
                          "output": output, "disposition": "pending",
                          "via": "a2a", "task_id": task.task_id})
    task.agent_run_id = run.run_id
    task.artifacts = [{"artifactId": str(uuid.uuid4()),
                       "name": task.agent_key,
                       "parts": [{"kind": "text", "text": output}]}]
    history = list(task.history or [])
    history.append({"role": "agent",
                    "parts": [{"kind": "text", "text": output}],
                    "messageId": str(uuid.uuid4())})
    task.history = history
    _transition(session, task, "completed", None)
    session.flush()
    append_audit(session, actor=user.username,
                 action="a2a.task.completed",
                 entity=f"a2a_task:{task.task_id}",
                 payload={"task_id": task.task_id,
                          "agent": task.agent_key,
                          "patient": task.patient_ref,
                          "run_id": run.run_id,
                          "delegation": delegation})
    session.commit()


def _a2a_send(session: Session, user: User, request: Request,
              req_id, params: dict, stream: bool):
    agent_key = params.get("agentKey")
    message = _message_text(params)
    if not isinstance(agent_key, str) or not agent_key.strip():
        return _rpc_error(req_id, ERR_INVALID_PARAMS,
                          "message/send requires 'agentKey'")
    if message is None:
        return _rpc_error(req_id, ERR_INVALID_PARAMS,
                          "message/send requires a non-empty 'message'")
    agent_key = agent_key.strip()
    patient_ref = _normalize_patient_ref(
        params.get("patientRef") or params.get("patient_ref"))
    delegation = _delegation(request, user)
    cfg = published_config(session, "agent", agent_key)
    if cfg is None:
        return _rpc_error(req_id, ERR_INVALID_PARAMS,
                          f"No published agent config for '{agent_key}'")
    if not agents_module._is_enabled(session, agent_key):
        return _rpc_error(
            req_id, ERR_TOOL_DISABLED,
            f"Agent '{agent_key}' is published but not enabled; an "
            "operator enables it in System Studio before it can run")
    # C-AI runtime gate (AI-G4/AI-G5): expired record/statement,
    # suspension or an active kill switch refuses the delegation with
    # the gate's reason.
    _refusal = agents_module.runtime_refusal(session, agent_key)
    if _refusal:
        return _rpc_error(req_id, ERR_TOOL_DISABLED, _refusal)
    task = A2aTask(
        task_id=str(uuid.uuid4()), agent_key=agent_key,
        actor=user.username,
        on_behalf_of=delegation["on_behalf_of"],
        patient_ref=patient_ref, status="submitted",
        message_hash=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        history=[{"role": "user",
                  "parts": [{"kind": "text", "text": message}],
                  "messageId": str(uuid.uuid4())}],
        artifacts=[], status_history=[])
    session.add(task)
    session.flush()
    _transition(session, task, "submitted", None)
    session.flush()
    append_audit(session, actor=user.username, action="a2a.task.create",
                 entity=f"a2a_task:{task.task_id}",
                 payload={"task_id": task.task_id, "agent": agent_key,
                          "patient": patient_ref,
                          "input_hash": task.message_hash,
                          "delegation": delegation})
    session.commit()
    _transition(session, task, "working", None)
    session.commit()
    _execute_task(session, task, user, request, delegation)
    out = _task_out(task)
    if stream:
        out["streamUrl"] = f"/a2a/tasks/{task.task_id}/stream"
    return _rpc_result(req_id, out)


def _find_task(session: Session, params: dict) -> A2aTask | None:
    task_id = params.get("id") or params.get("taskId")
    if not isinstance(task_id, str) or not task_id:
        return None
    return session.scalars(select(A2aTask).where(
        A2aTask.task_id == task_id)).first()


@a2a_router.post("/rpc")
async def a2a_rpc(request: Request, session: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """A2A JSON-RPC endpoint. Bearer required (401 via the
    dependency when absent); a caller without a2a.use gets a
    JSON-RPC forbidden error, in-envelope."""
    body = await _json_body(request)
    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    if not isinstance(method, str) or not method:
        return _rpc_error(req_id, ERR_INVALID_REQUEST,
                          "JSON-RPC request requires a 'method'")
    if not isinstance(params, dict):
        return _rpc_error(req_id, ERR_INVALID_PARAMS,
                          "'params' must be an object")
    if method not in ("message/send", "message/stream", "tasks/get",
                      "tasks/cancel"):
        return _rpc_error(req_id, ERR_METHOD_NOT_FOUND,
                          f"Method not found: {method}")
    if not _cap_allowed(session, user, "a2a.use"):
        return _rpc_error(req_id, ERR_FORBIDDEN,
                          f"Role {user.role.value} lacks capability "
                          "'a2a.use'")
    if method in ("message/send", "message/stream"):
        return _a2a_send(session, user, request, req_id, params,
                         stream=(method == "message/stream"))
    if method == "tasks/get":
        task = _find_task(session, params)
        if task is None:
            return _rpc_error(req_id, ERR_INVALID_PARAMS,
                              "tasks/get requires a known task 'id'")
        return _rpc_result(req_id, _task_out(task))
    # tasks/cancel
    task = _find_task(session, params)
    if task is None:
        return _rpc_error(req_id, ERR_INVALID_PARAMS,
                          "tasks/cancel requires a known task 'id'")
    if task.status in TERMINAL_TASK_STATES:
        return _rpc_error(
            req_id, ERR_TASK_TERMINAL,
            f"Task {task.task_id} is already '{task.status}' and "
            "cannot be canceled")
    delegation = _delegation(request, user)
    _transition(session, task, "canceled", "Canceled by caller")
    session.flush()
    append_audit(session, actor=user.username, action="a2a.task.cancel",
                 entity=f"a2a_task:{task.task_id}",
                 payload={"task_id": task.task_id,
                          "agent": task.agent_key,
                          "patient": task.patient_ref,
                          "delegation": delegation})
    session.commit()
    return _rpc_result(req_id, _task_out(task))


@a2a_router.get("/tasks/{task_id}")
def get_a2a_task(task_id: str, session: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    if not _cap_allowed(session, user, "a2a.use"):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.value} lacks capability 'a2a.use'")
    task = session.scalars(select(A2aTask).where(
        A2aTask.task_id == task_id)).first()
    if task is None:
        raise HTTPException(status_code=404,
                            detail=f"A2A task {task_id} not found")
    return _task_out(task)


@a2a_router.get("/tasks/{task_id}/stream")
def stream_a2a_task(task_id: str, session: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """The companion SSE binding for message/stream: replays the
    task's recorded status transitions as server-sent events, then
    closes. (A task created via message/send executes synchronously,
    so by stream time its transitions are complete; a non-terminal
    task streams its transitions-so-far and closes — the binding is
    replay, stated in the integration note.)"""
    if not _cap_allowed(session, user, "a2a.use"):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.value} lacks capability 'a2a.use'")
    task = session.scalars(select(A2aTask).where(
        A2aTask.task_id == task_id)).first()
    if task is None:
        raise HTTPException(status_code=404,
                            detail=f"A2A task {task_id} not found")
    events = list(task.status_history or [])
    snapshot = _task_out(task)

    def _gen():
        yield ": opencare a2a task stream\n\n"
        for entry in events:
            data = {"id": task_id, "status": entry.get("status"),
                    "at": entry.get("at"),
                    "message": entry.get("message")}
            yield (f"event: task-status\n"
                   f"data: {json.dumps(data)}\n\n")
        yield (f"event: task\n"
               f"data: {json.dumps(snapshot)}\n\n")

    return StreamingResponse(_gen(), media_type="text/event-stream")


# Combined router for the coordinator to mount.
router = APIRouter(tags=["mcp-a2a"])
router.include_router(mcp_router)
router.include_router(a2a_router)
router.include_router(wellknown_router)
