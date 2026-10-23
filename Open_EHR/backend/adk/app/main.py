"""OpenCare ADK runtime service.

A thin FastAPI wrapper around Google ADK (google-adk, Apache-2.0),
ported in pattern from Agentic_App's System Studio ADK service
(OpenCare's own code, written for the Studio `agent` config shape).
Agent definitions are OpenCare Studio configuration: the EHR backend
stores/versions/publishes them and passes the full config in each run
request (and may sync configs for roster listing via POST /agents/sync).
This service is deliberately generic — it executes whatever governed
config it is given.

Models go through LiteLLM (ADK's LiteLlm), so any LiteLLM model string
works: the local-first default is a local Ollama model
(AGENT_MODEL=ollama_chat/llama3.2:3b, no API key, nothing leaves the
machine). Honest failure contract: when no model is configured, /run
answers 503 — never a fabricated answer. Agents draft; humans act:
this service returns text drafts only, and its one tool calls the EHR
API with the invoking user's own token, allow-listed per agent.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import AgentConfig, parse_agent_config, resolve_model
from .logredact import install_log_redaction, redact_text
from .tools import make_api_call_tool

RUN_TIMEOUT_S = float(os.environ.get("ADK_RUN_TIMEOUT_S", "120"))

# Phase 17 (BD-53): PHI redaction at this service's logging boundary
# (app.logredact mirror, spec log-redaction-v1) — installed before
# the app serves so boot-time log lines are covered too.
install_log_redaction()
logger = logging.getLogger("opencare-adk")

app = FastAPI(title="OpenCare ADK Runtime", version="1.0.0")

_lock = threading.Lock()
_synced: dict[str, dict] = {}


def _adk_name(name: str) -> str:
    """ADK agent names must be identifiers; derive one from the config key."""
    s = re.sub(r"[^a-zA-Z0-9_]", "_", name or "agent")
    if not s or s[0].isdigit():
        s = f"agent_{s}"
    return s[:64]


@app.get("/health")
def health():
    model = os.environ.get("AGENT_MODEL", "").strip()
    return {
        "ok": True,
        "service": "opencare-adk",
        "modelConfigured": bool(model),
        "model": model or None,
        "agentsSynced": len(_synced),
    }


@app.get("/metrics")
def metrics():
    """Prometheus text exposition (Phase 17, BD-61) — hand-rolled:
    this service's venv carries no metrics library by design."""
    from fastapi.responses import PlainTextResponse

    model = os.environ.get("AGENT_MODEL", "").strip()
    body = (
        "# HELP opencare_adk_up ADK runtime liveness (always 1 when served).\n"
        "# TYPE opencare_adk_up gauge\n"
        "opencare_adk_up 1\n"
        "# HELP opencare_adk_agents_synced Agent configs synced from the EHR Studio.\n"
        "# TYPE opencare_adk_agents_synced gauge\n"
        f"opencare_adk_agents_synced {len(_synced)}\n"
        "# HELP opencare_adk_model_configured Whether a model is configured (1) or the runtime is honestly off (0).\n"
        "# TYPE opencare_adk_model_configured gauge\n"
        f"opencare_adk_model_configured {1 if model else 0}\n"
    )
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


@app.post("/agents/sync")
async def agents_sync(request: Request):
    body = await request.json()
    agents = body.get("agents")
    if not isinstance(agents, list):
        return JSONResponse(status_code=422,
                            content={"error": "agents must be an array of agent configs"})
    parsed: dict[str, dict] = {}
    for raw in agents:
        try:
            cfg = parse_agent_config(raw)
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"error": str(exc)})
        parsed[cfg.name] = raw
    with _lock:
        _synced.clear()
        _synced.update(parsed)
    return {"ok": True, "agents": sorted(parsed.keys())}


@app.get("/agents")
def agents_roster():
    roster = []
    with _lock:
        items = list(_synced.values())
    for raw in items:
        try:
            cfg = parse_agent_config(raw)
        except ValueError:
            continue
        roster.append({
            "name": cfg.name,
            "description": cfg.description,
            "model": resolve_model(cfg.model) or None,
            "tools": cfg.tools,
            "modalities": cfg.modalities,
        })
    return {"agents": roster}


def _build_agent(cfg: AgentConfig, model: str, api_base_url: str, api_token: str):
    from google.adk.agents import LlmAgent
    from google.adk.models.lite_llm import LiteLlm
    from google.genai import types as genai_types

    tools = []
    if "opencare_api_call" in cfg.tools:
        tools.append(make_api_call_tool(api_base_url, api_token, cfg.api_allowlist))
    gen_cfg = None
    if cfg.temperature is not None:
        gen_cfg = genai_types.GenerateContentConfig(temperature=cfg.temperature)
    return LlmAgent(
        name=_adk_name(cfg.name),
        description=cfg.description or cfg.name,
        model=LiteLlm(model=model),
        instruction=cfg.instructions,
        tools=tools,
        generate_content_config=gen_cfg,
    )


async def _run_agent(cfg: AgentConfig, model: str, parts,
                     api_base_url: str, api_token: str) -> str:
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as genai_types

    try:
        from google.adk.agents.run_config import RunConfig
    except Exception:
        RunConfig = None  # ADK layout variance — max_steps then stays advisory

    agent = _build_agent(cfg, model, api_base_url, api_token)
    runner = Runner(
        agent=agent,
        app_name="opencare-adk",
        session_service=InMemorySessionService(),
        auto_create_session=True,
    )
    message = genai_types.Content(role="user", parts=parts)
    run_config = RunConfig(max_llm_calls=cfg.max_steps) if (RunConfig and cfg.max_steps) else None

    final_text = ""
    collected: list[str] = []

    async def _consume() -> None:
        nonlocal final_text
        async for event in runner.run_async(
            user_id="opencare-studio",
            session_id=f"run-{id(parts)}",
            new_message=message,
            run_config=run_config,
        ):
            content = getattr(event, "content", None)
            texts = []
            if content and getattr(content, "parts", None):
                texts = [p.text for p in content.parts if getattr(p, "text", None)]
            if texts:
                collected.extend(texts)
                if event.is_final_response():
                    final_text = "".join(texts)

    await asyncio.wait_for(_consume(), timeout=RUN_TIMEOUT_S)
    return final_text or "\n".join(collected).strip()


@app.post("/run")
async def run(request: Request):
    body = await request.json()
    raw_agent = body.get("agent")
    if not isinstance(raw_agent, dict):
        return JSONResponse(status_code=422,
                            content={"error": "agent config object is required"})
    try:
        cfg = parse_agent_config(raw_agent)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})

    # Multimodal gating: a part the config did not opt into is a 422,
    # not a silent drop — the operator learns the config needs the flag.
    parts_spec = [("imageBase64", "image"), ("audioBase64", "audio")]
    for field_name, kind in parts_spec:
        if body.get(field_name) and not cfg.modalities.get(kind):
            return JSONResponse(
                status_code=422,
                content={"error": f"{field_name} was provided but this agent config "
                                  f"does not enable {kind} input"})

    prompt = str(body.get("prompt") or "")
    if not prompt.strip() and not any(body.get(f) for f, _k in parts_spec):
        return JSONResponse(status_code=422,
                            content={"error": "a prompt or a multimodal part is required"})

    model = resolve_model(cfg.model)
    if not model:
        return JSONResponse(
            status_code=503,
            content={"error": "No model configured: set a model on the agent config or "
                              "AGENT_MODEL on the ADK runtime (local-first example: "
                              "AGENT_MODEL=ollama_chat/llama3.2:3b with Ollama running). "
                              "No answer was generated."})

    import base64

    from google.genai import types as genai_types

    parts = []
    if prompt.strip():
        parts.append(genai_types.Part(text=prompt))
    for field_name, kind in parts_spec:
        b64 = body.get(field_name)
        if b64:
            mime = body.get(f"{kind}Mime") or (
                "image/png" if kind == "image" else "audio/wav")
            parts.append(genai_types.Part.from_bytes(
                data=base64.b64decode(b64), mime_type=mime))

    try:
        output = await _run_agent(cfg, model, parts,
                                  str(body.get("apiBaseUrl") or ""),
                                  str(body.get("apiToken") or ""))
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=503,
            content={"error": f"Model run timed out after {RUN_TIMEOUT_S:.0f}s. "
                              "No answer was generated."})
    except Exception as exc:  # model endpoint unreachable / provider error
        # The provider error can echo prompt text; it crosses the
        # logging boundary AND the API boundary, so it is redacted
        # (BD-53) before it is logged or returned.
        safe = redact_text(str(exc))
        logger.error("model call failed: %s", safe)
        return JSONResponse(
            status_code=503,
            content={"error": f"Model call failed: {safe}. No answer was generated."})
    return {"output": output, "model": model, "agent": cfg.name}
