"""Pack-enablement route gating — Platform pass, RTM STU-04.

The design (docs v2.0 §6.1, and the pack manifests themselves) has
always promised that a pack's declared backend surface ships DISABLED
until a human enables the pack: manifests declare `api_endpoints`
with `enabled: false`, and `app.studio` refuses any manifest that
tries to self-enable one. Until this module, nothing at REQUEST time
honoured that state — the routes served regardless of pack state
(the documented "pack-enablement route gating" gap carried since
the realignment SQA).

What this module does
---------------------
A governed enforcement gate, wired as middleware in `app.main`
next to the Phase 10 downtime guard (whose per-request
published-config pattern it mirrors):

- Phase 17 (WS-PACKGATE, BD-01 completion): the gate is ON by
  default. The posture is delivered through GOVERNANCE, not a code
  constant: `app.studio_config.ensure_default_configs` seeds the
  PUBLISHED Studio config kind `integration`, key `pack-gating`,
  as `{"enabled": true}` on a fresh deployment, and
  `app.studio.ensure_core_pack_installations` seeds the core
  packs (clinical-core, interop, lab — the packs whose manifests
  declare the core application's routes) through the governed
  install -> approve -> attach -> enable lifecycle, so a fresh
  deployment gates AND serves. Two rules complete the posture:
  (1) ABSENT config means ON — `gating_enabled` returns True when
  no published `pack-gating` config exists at all, so a deployment
  whose seed was removed, or which predates the seed, still gates
  rather than silently serving disabled packs; (2) the ESCAPE
  HATCH is an explicit published `{"enabled": false}` — a governed,
  audited operator kill-switch (Studio -> config, draft -> publish,
  exactly like every other config change) that restores the
  pre-Phase-17 open behaviour. That is the ONLY way the gate turns
  off, and because config seeding is insert-if-absent, an operator's
  published `enabled: false` is never overwritten by a restart.
- When ON: a request whose (method, path) matches an endpoint
  declared in ANY pack manifest (paths are templates; `{param}`
  segments match one path segment) is refused **403** unless at
  least one declaring pack has an installation in status `enabled`.
  The ANY rule matters: core endpoints (e.g. `POST /fhir/Patient`)
  are declared by `clinical-core` and may also be declared by
  specialty packs — one enabled declaring pack is enough to serve.
- Exempt by construction: `/health` and everything under `/studio`
  — the surfaces an operator needs to install/approve/attach/enable
  a pack, or to publish `pack-gating` back off, can never be gated.
  (The downtime guard uses the same exemption logic.)
- The gate is a governance enforcement, not a security boundary:
  authentication and capability checks still run inside the routes
  whenever the gate passes a request through, and — mirroring the
  downtime guard's precedent that a guard failure must never take
  the API down — a failure inside the gate itself fails OPEN and
  serving continues. Sites that need hard denial should also use
  the network tier; what the gate guarantees is that the governed
  pack state is honoured in normal operation.

Cost: one published-config read per non-exempt request — the
same pattern (and cost class) as the downtime guard's per-write
read.
"""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .configstore import published_config
from .models import InstallationStatus, ModulePack, PackInstallation

GATING_KIND = "integration"
GATING_KEY = "pack-gating"

EXEMPT_PREFIXES = ("/studio",)


def gating_enabled(session: Session) -> bool:
    """Phase 17 default-ON: the gate is ON unless an operator has
    published the escape hatch — an explicit `{"enabled": false}`.

    Absent config = ON (see the module docstring for the rationale);
    a published payload without an `enabled` key is likewise ON.
    Only the literal boolean False disables the gate, so a malformed
    or partial payload can never silently open disabled packs."""
    cfg = published_config(session, GATING_KIND, GATING_KEY)
    if cfg is None:
        return True
    payload = cfg.payload or {}
    return payload.get("enabled") is not False


def _template_regex(path: str) -> re.Pattern:
    parts = [p for p in path.split("/") if p != ""]
    pattern = "^"
    for part in parts:
        pattern += "/"
        if part.startswith("{") and part.endswith("}"):
            pattern += "[^/]+"
        else:
            pattern += re.escape(part)
    pattern += "/?$"
    return re.compile(pattern)


def declared_endpoint_block(session: Session, method: str,
                            path: str) -> tuple[bool, list[str]]:
    """Decide whether (method, path) is pack-gated right now.

    Returns (blocked, declaring_pack_names). Blocked is True only
    when at least one pack declares the endpoint AND no declaring
    pack has an enabled installation.
    """
    packs = session.scalars(select(ModulePack)).all()
    if not packs:
        return (False, [])
    enabled_pack_ids = set(session.scalars(
        select(PackInstallation.pack_id)
        .where(PackInstallation.status == InstallationStatus.enabled)
    ).all())
    declaring: list[ModulePack] = []
    for pack in packs:
        for endpoint in (pack.manifest or {}).get("api_endpoints") or []:
            if not isinstance(endpoint, dict):
                continue
            if str(endpoint.get("method") or "").upper() != method.upper():
                continue
            declared_path = endpoint.get("path") or ""
            if declared_path and _template_regex(declared_path).match(path):
                declaring.append(pack)
                break
    if not declaring:
        return (False, [])
    if any(pack.id in enabled_pack_ids for pack in declaring):
        return (False, sorted({pack.name for pack in declaring}))
    return (True, sorted({pack.name for pack in declaring}))
