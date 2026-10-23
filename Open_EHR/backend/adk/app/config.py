"""Agent config parsing for the OpenCare ADK runtime.

Agent definitions are OpenCare Studio configuration (kind=agent): the
backend owns, versions and publishes them and passes the full config in
each run request. Field names mirror app.configstore.validate_agent:
{description, model, instructions, tools, apiAllowlist, modalities,
temperature, maxSteps} plus the config `key` as the agent's name.

Model resolution: the config's `model` (a LiteLLM model string) wins;
the runtime-wide AGENT_MODEL env var is the fallback; empty means NO
model — runs are then refused with an honest 503, never faked.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

KNOWN_TOOLS = ("opencare_api_call",)


@dataclass
class AgentConfig:
    name: str
    description: str = ""
    model: str = ""
    instructions: str = ""
    tools: list = field(default_factory=list)
    api_allowlist: list = field(default_factory=list)
    modalities: dict = field(default_factory=lambda: {"image": False, "audio": False})
    temperature: float | None = None
    max_steps: int | None = None


def parse_agent_config(raw) -> AgentConfig:
    if not isinstance(raw, dict):
        raise ValueError("agent config must be an object")
    name = raw.get("key") or raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("agent config requires a 'key' (the Studio config key)")
    instructions = raw.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("agent config requires non-empty 'instructions'")
    model = raw.get("model") or ""
    if not isinstance(model, str):
        raise ValueError("'model' must be a string")
    if any(s in model for s in ("sk-", "api_key", "secret")):
        raise ValueError("'model' looks like a credential; model routes never carry secrets")
    tools = raw.get("tools") or []
    if not isinstance(tools, list) or any(t not in KNOWN_TOOLS for t in tools):
        raise ValueError(f"'tools' must be drawn from {list(KNOWN_TOOLS)}")
    allowlist = raw.get("apiAllowlist") or []
    if not isinstance(allowlist, list) or any(
            not isinstance(p, str) or not p.startswith("/") for p in allowlist):
        raise ValueError("'apiAllowlist' must be a list of path prefixes starting with '/'")
    raw_modalities = raw.get("modalities") or {}
    if not isinstance(raw_modalities, dict):
        raise ValueError("'modalities' must be an object")
    modalities = {"image": bool(raw_modalities.get("image")),
                  "audio": bool(raw_modalities.get("audio"))}
    temperature = raw.get("temperature")
    if temperature is not None and not isinstance(temperature, (int, float)):
        raise ValueError("'temperature' must be a number")
    max_steps = raw.get("maxSteps")
    if max_steps is not None and not isinstance(max_steps, int):
        raise ValueError("'maxSteps' must be an integer")
    return AgentConfig(
        name=name.strip(),
        description=raw.get("description") or "",
        model=model,
        instructions=instructions,
        tools=list(tools),
        api_allowlist=list(allowlist),
        modalities=modalities,
        temperature=temperature,
        max_steps=max_steps,
    )


def resolve_model(config_model: str) -> str:
    """Config model wins; AGENT_MODEL is the runtime fallback; '' = none."""
    if config_model and config_model.strip():
        return config_model.strip()
    return os.environ.get("AGENT_MODEL", "").strip()


def is_path_allowed(path: str, method: str, allowlist: list) -> bool:
    """The tool-call gate, checked BEFORE any HTTP happens. A path is
    allowed only when it is a plain API path (no absolute URLs, no
    traversal) under one of the agent's allow-listed prefixes. Unlike a
    general assistant, a clinical agent gets NO implicit GET-anywhere:
    every reachable prefix is named in its governed config."""
    if not isinstance(path, str) or not path.startswith("/"):
        return False
    if "://" in path or ".." in path.split("/"):
        return False
    return any(path == prefix or path.startswith(prefix.rstrip("/") + "/")
               or path.startswith(prefix)
               for prefix in allowlist)
