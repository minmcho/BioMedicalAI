"""AI providers registry — PLATFORM PASS Workstream 10 (Studio Full
Parity, STUDIO_PARITY row 4): app.ai_providers.

This is the governed registry the ADK resolves providers from: the
OpenCare adaptation of Agentic_App's StudioAiProviders surface.
Provider definitions live in the shared StudioConfig store under
kind ``ai-provider`` with in-module draft -> publish governance
mirroring app.report_engine / app.studio_config (drafts are designer
data gated by ``studio.manage``; published versions are immutable;
publishing supersedes; every governance transition is written to the
hash-chained audit log).

THE CREDENTIAL LAW (the same law as the DB platform's vault-key-name
rule): NO secret is ever stored in a config payload. A provider that
needs an API key references it by ENV-VAR NAME ONLY (``apiKeyRef``,
e.g. ``"OPENAI_API_KEY"``) — the value lives in the deployment
environment, never in configuration. Any payload carrying a
credential-shaped field (``apiKey``, ``api_key``, ``secret``,
``password``, ``token``, ``key``, ``credentials``) at top level is
refused 422 by the validator, before any other check.

What ``GET /ai-providers/effective`` reports per published provider:
the provider's public shape plus ``keyConfigured`` — a BOOLEAN that
is true only when ``apiKeyRef`` is set AND that env var is present
in ``os.environ`` (providers without an ``apiKeyRef`` report
``keyConfigured: null`` — e.g. a local Ollama needs no key). The env
var's VALUE is never read into a response, a log, or an audit row.

HONEST SCOPE: wiring the ADK service to consume
``/ai-providers/effective`` lands in Phase 16. This module makes NO
live model calls, and there is no model endpoint in this
environment, so nothing here can be live-verified against a real
provider — the registry, its governance, and the ``keyConfigured``
presence check are what this module delivers and what its tests
exercise.

Capabilities (existing only): definition governance rides
``studio.manage``; definition reads and the effective view ride
``studio.read``.

INTEGRATION NOTE: the ``ai-provider`` kind IS registered in the
generic Studio API — it appears in app.configstore's CONFIG_KINDS
with this module's validator wired into configstore's VALIDATORS
(``validate_ai_provider``), so provider definitions ride the generic
draft -> publish lifecycle (app.studio_config) as well as this
module's own endpoints, and the C-AI four-eyes publish gate
(app.four_eyes) applies on both paths.
"""
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import append_audit
from .configstore import published_config, published_configs, role_allowed
from .models import ConfigStatus, StudioConfig, User
from .security import get_db, require_capability

router = APIRouter(prefix="/ai-providers", tags=["ai-providers"])

KIND = "ai-provider"

PROVIDER_TYPES = ("ollama", "openai-compatible", "anthropic", "google",
                  "azure-openai")

# Credential-shaped top-level fields, refused by NAME. A provider
# payload may reference a key via `apiKeyRef` (an env-var name) and
# nothing else — see the module docstring's credential law.
FORBIDDEN_CREDENTIAL_FIELDS = ("apiKey", "api_key", "secret", "password",
                               "token", "key", "credentials")
_FORBIDDEN_NORMALISED = {f.lower() for f in FORBIDDEN_CREDENTIAL_FIELDS}

# An env-var NAME: uppercase start, then uppercase / digits / underscore.
API_KEY_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

EFFECTIVE_NOTE = ("No live model call is made by this module. "
                  "keyConfigured reports only whether the environment "
                  "variable named by apiKeyRef is present — never its "
                  "value. ADK consumption of this registry lands in "
                  "Phase 16.")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _scan_forbidden_credentials(payload: dict) -> None:
    """Refuse 422 when any credential-shaped field is present at the
    payload's top level, naming every offending field."""
    found = [key for key in payload
             if str(key).lower() in _FORBIDDEN_NORMALISED]
    if found:
        raise HTTPException(
            status_code=422,
            detail=(f"Credential field(s) {found} are forbidden in an "
                    "AI provider definition: secrets are never stored "
                    "in configuration. Reference an API key by "
                    "env-var NAME only, via 'apiKeyRef' (e.g. "
                    "\"OPENAI_API_KEY\")."))


def _require_non_empty_str(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=422,
            detail=f"'{field}' is required (non-empty string)")
    return value


def _optional_str(payload: dict, field: str) -> None:
    value = payload.get(field)
    if value is not None and not isinstance(value, str):
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be a string")


def validate_ai_provider_payload(payload) -> dict:
    """Validate one ai-provider payload; returns it normalised
    (defaults applied). Raises HTTPException 422 on any problem —
    the credential scan runs FIRST, before structural checks.
    Configstore-compatible: the coordinator registers this as the
    ``ai-provider`` kind validator."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422,
                            detail="AI provider definition must be a "
                                   "JSON object")
    _scan_forbidden_credentials(payload)
    _require_non_empty_str(payload, "name")
    provider_type = payload.get("type")
    if provider_type not in PROVIDER_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(f"'type' must be one of {list(PROVIDER_TYPES)}; "
                    f"got '{provider_type}'"))
    _require_non_empty_str(payload, "model")
    _optional_str(payload, "baseUrl")
    _optional_str(payload, "department")
    _optional_str(payload, "notes")
    enabled = payload.get("enabled", False)
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422,
                            detail="'enabled' must be a boolean")
    api_key_ref = payload.get("apiKeyRef")
    if api_key_ref is not None:
        if not isinstance(api_key_ref, str) \
                or not API_KEY_REF_RE.match(api_key_ref):
            raise HTTPException(
                status_code=422,
                detail=("'apiKeyRef' must be an environment-variable "
                        "NAME matching ^[A-Z][A-Z0-9_]*$ (the name of "
                        "the variable holding the key — never the "
                        "key itself)"))
    return {**payload, "enabled": enabled}


def default_ai_provider_payloads() -> dict:
    """Built-in provider payloads, keyed by definition key: one
    local-first default (local Ollama, no key needed).

    These are publishable via the governance API above — they are
    NOT auto-seeded: nothing is written to the config store until a
    studio manager creates and publishes the definition, exactly
    like every other governed kind. A fresh deployment therefore
    has NO effective providers until a human publishes one."""
    return {
        "local-ollama": {
            "name": "Local Ollama (local-first)",
            "type": "ollama",
            "baseUrl": "http://127.0.0.1:11434",
            "model": "llama3.2:3b",
            "enabled": False,
        },
    }


# ---------------------------------------------------------------------------
# In-module governance (mirrors app.report_engine / app.studio_config)
# ---------------------------------------------------------------------------

def _versions(session: Session, key: str) -> list[StudioConfig]:
    return list(session.scalars(
        select(StudioConfig).where(StudioConfig.kind == KIND,
                                   StudioConfig.key == key)
        .order_by(StudioConfig.version.asc())).all())


def _published(session: Session, key: str) -> StudioConfig | None:
    return published_config(session, KIND, key)


def _out(row: StudioConfig) -> dict:
    return {"kind": row.kind, "key": row.key, "version": row.version,
            "status": row.status.value, "payload": row.payload,
            "created_by": row.created_by, "updated_by": row.updated_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def _can_see(session: Session, row: StudioConfig, user: User) -> bool:
    if role_allowed(session, user.role, "studio.manage"):
        return True
    return row.status == ConfigStatus.published


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


@router.post("/definitions/{key}", status_code=201)
async def create_definition(key: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("studio.manage"))):
    payload = validate_ai_provider_payload(await _json_body(request))
    versions = _versions(session, key)
    if any(v.status == ConfigStatus.draft for v in versions):
        raise HTTPException(
            status_code=409,
            detail=f"A draft already exists for {KIND}/{key}; publish it first")
    next_version = (versions[-1].version + 1) if versions else 1
    row = StudioConfig(kind=KIND, key=key, version=next_version,
                       status=ConfigStatus.draft, payload=payload,
                       created_by=user.username, updated_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="ai-providers.definition.create",
                 entity=f"config:{KIND}/{key}@v{next_version}",
                 payload={"kind": KIND, "key": key, "version": next_version})
    session.commit()
    return _out(row)


@router.put("/definitions/{key}/versions/{version}")
async def update_definition(key: str, version: int, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("studio.manage"))):
    row = next((v for v in _versions(session, key) if v.version == version),
               None)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Config {KIND}/{key} v{version} not found")
    if row.status != ConfigStatus.draft:
        raise HTTPException(
            status_code=409,
            detail=f"Config {KIND}/{key} v{version} is '{row.status.value}' "
                   "and immutable; create a new draft version")
    row.payload = validate_ai_provider_payload(await _json_body(request))
    row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username,
                 action="ai-providers.definition.update",
                 entity=f"config:{KIND}/{key}@v{version}",
                 payload={"kind": KIND, "key": key, "version": version})
    session.commit()
    return _out(row)


@router.post("/definitions/{key}/versions/{version}/publish")
def publish_definition(key: str, version: int,
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("studio.manage"))):
    row = next((v for v in _versions(session, key) if v.version == version),
               None)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Config {KIND}/{key} v{version} not found")
    if row.status == ConfigStatus.published:
        raise HTTPException(status_code=409,
                            detail=f"Config {KIND}/{key} v{version} is already published")
    if row.status == ConfigStatus.superseded:
        raise HTTPException(status_code=409,
                            detail="Superseded versions cannot be republished directly")
    # C-AI four-eyes (AI-G3, app.four_eyes): an ai-provider definition
    # may never be published by its own author — on this module's
    # endpoint exactly as on the generic Studio API path.
    from . import four_eyes
    four_eyes.enforce(
        session, actor=user.username, authors={row.created_by},
        action="ai-providers.definition.publish",
        entity=f"config:{KIND}/{key}@v{version}",
        detail=f"publish ai-provider '{key}' v{version}")
    current = _published(session, key)
    superseded = None
    if current is not None and current.id != row.id:
        current.status = ConfigStatus.superseded
        superseded = current.version
        session.flush()
    row.status = ConfigStatus.published
    row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username,
                 action="ai-providers.definition.publish",
                 entity=f"config:{KIND}/{key}@v{version}",
                 payload={"kind": KIND, "key": key, "version": version,
                          "superseded_version": superseded})
    session.commit()
    return _out(row)


@router.get("/definitions")
def list_definitions(session: Session = Depends(get_db),
                     user: User = Depends(require_capability("studio.read"))):
    rows = session.scalars(
        select(StudioConfig).where(StudioConfig.kind == KIND)
        .order_by(StudioConfig.key.asc(), StudioConfig.version.asc())).all()
    items = [_out(r) for r in rows if _can_see(session, r, user)]
    return {"items": items, "total": len(items)}


@router.get("/definitions/{key}")
def get_definition(key: str, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("studio.read"))):
    rows = [r for r in _versions(session, key) if _can_see(session, r, user)]
    if not rows:
        raise HTTPException(status_code=404,
                            detail=f"No config found for {KIND}/{key}")
    return {"key": key, "versions": [_out(r) for r in rows],
            "published": (_out(_published(session, key))
                          if _published(session, key) else None)}


# ---------------------------------------------------------------------------
# Effective (published) providers — presence only, never a key value
# ---------------------------------------------------------------------------

def _effective_item(row: StudioConfig) -> dict:
    payload = row.payload or {}
    api_key_ref = payload.get("apiKeyRef")
    if isinstance(api_key_ref, str) and api_key_ref:
        # Presence ONLY: the env var's value is never read out.
        key_configured: bool | None = api_key_ref in os.environ
    else:
        key_configured = None
    return {"key": row.key,
            "name": payload.get("name"),
            "type": payload.get("type"),
            "model": payload.get("model"),
            "baseUrl": payload.get("baseUrl"),
            "department": payload.get("department"),
            "enabled": payload.get("enabled", False),
            "keyConfigured": key_configured}


@router.get("/effective")
def get_effective(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("studio.read"))):
    """The published providers — the set the ADK will resolve from
    (Phase 16 wiring). Drafts and superseded versions never appear
    here. No live model call is made by this module."""
    items = [_effective_item(row)
             for row in published_configs(session, KIND)]
    return {"items": items, "total": len(items), "note": EFFECTIVE_NOTE}
