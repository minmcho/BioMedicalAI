"""Studio customization engine — versioned configuration API.

Implements docs/STUDIO_CUSTOMIZATION.md: everything site-specific (forms,
workflows, terminology, navigation, branding, permissions) is configuration
DATA with a draft -> publish lifecycle, never hardcoded per site.

Governance rules enforced here:
- Writes require the `studio.manage` capability (default admin/
  configurator, overridable via the published permissions matrix);
  reads require `studio.read` (default admin/configurator/clinician/
  viewer). Clinician/viewer can only ever read PUBLISHED (effective)
  config; drafts are designer data gated by studio.manage.
- Published versions are immutable. Editing one means creating a new draft
  version (POST) and publishing it; publishing supersedes the previously
  published version, which is kept for rollback.
- Rollback republishes an earlier version's payload as a NEW version.
- Every create/update/publish/disable/rollback writes a hash-chained
  AuditLog row.
"""
import copy
import hashlib
import json
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import append_audit
from .configstore import (ADT_WORKFLOW_KEY, BRANDING_KEY, CONFIG_KINDS,
                          ConfigPayloadError, NAVIGATION_KEY,
                          default_adt_workflow_payload,
                          default_branding_payload, default_navigation_payload,
                          diff_payloads, get_config_version,
                          list_config_versions, published_config,
                          published_configs, role_allowed, validate_payload)
from .models import AuditLog, ConfigStatus, StudioConfig, User
from .security import get_db, require_capability

router = APIRouter(prefix="/studio/config", tags=["studio-config"])


def _require_kind(kind: str) -> None:
    if kind not in CONFIG_KINDS:
        raise HTTPException(status_code=404, detail=f"Unknown config kind: {kind}")


def _validate(kind: str, payload) -> None:
    try:
        validate_payload(kind, payload)
    except ConfigPayloadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


def _config_out(row: StudioConfig) -> dict:
    return {
        "id": row.id, "kind": row.kind, "key": row.key, "version": row.version,
        "status": row.status.value, "payload": row.payload,
        "created_by": row.created_by, "updated_by": row.updated_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _can_see(session: Session, row: StudioConfig, user: User) -> bool:
    """Drafts are designer data: only studio.manage holders see them;
    everyone else with studio.read sees published config only."""
    if role_allowed(session, user.role, "studio.manage"):
        return True
    return row.status == ConfigStatus.published


def _get_visible(kind: str, key: str, version: int, session: Session, user: User) -> StudioConfig:
    row = get_config_version(session, kind, key, version)
    if row is None or not _can_see(session, row, user):
        raise HTTPException(status_code=404,
                            detail=f"Config {kind}/{key} v{version} not found")
    return row


async def _json_body(request: Request):
    try:
        return await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")


# ---------------------------------------------------------------------------
# Effective (published-only) reads — registered before /{kind}/{key} so the
# literal "effective" segment wins over a config key of the same name.
# ---------------------------------------------------------------------------

@router.get("/{kind}/effective")
def effective_for_kind(kind: str, session: Session = Depends(get_db),
                       user: User = Depends(require_capability("studio.read"))):
    _require_kind(kind)
    return [_config_out(r) for r in published_configs(session, kind)]


@router.get("/{kind}/effective/{key}")
def effective_for_key(kind: str, key: str, session: Session = Depends(get_db),
                      user: User = Depends(require_capability("studio.read"))):
    _require_kind(kind)
    row = published_config(session, kind, key)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"No published config for {kind}/{key}")
    return _config_out(row)


# ---------------------------------------------------------------------------
# Create / update drafts
# ---------------------------------------------------------------------------

@router.post("/{kind}/{key}", status_code=201)
async def create_draft(kind: str, key: str, request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("studio.manage"))):
    """Create the next version of {kind}/{key} as a draft. If the latest
    version is published, this is how it is 'edited': the published row is
    never mutated — a new draft version spawns instead."""
    _require_kind(kind)
    payload = await _json_body(request)
    _validate(kind, payload)
    versions = list_config_versions(session, kind, key)
    drafts = [v for v in versions if v.status == ConfigStatus.draft]
    if drafts:
        raise HTTPException(
            status_code=409,
            detail=(f"A draft already exists for {kind}/{key} "
                    f"(v{drafts[-1].version}); edit that draft or publish it first"))
    next_version = (versions[-1].version + 1) if versions else 1
    row = StudioConfig(kind=kind, key=key, version=next_version,
                       status=ConfigStatus.draft, payload=payload,
                       created_by=user.username, updated_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="studio.config.create",
                 entity=f"config:{kind}/{key}@v{next_version}",
                 payload={"kind": kind, "key": key, "version": next_version})
    session.commit()
    return _config_out(row)


@router.put("/{kind}/{key}/versions/{version}")
async def update_draft(kind: str, key: str, version: int, request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("studio.manage"))):
    """Replace a DRAFT version's payload. Any other status -> 409: published
    config is immutable and changes only via a new draft version."""
    _require_kind(kind)
    row = get_config_version(session, kind, key, version)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Config {kind}/{key} v{version} not found")
    if row.status != ConfigStatus.draft:
        raise HTTPException(
            status_code=409,
            detail=(f"Config {kind}/{key} v{version} is '{row.status.value}' and "
                    "immutable; create a new draft version to change it"))
    payload = await _json_body(request)
    _validate(kind, payload)
    row.payload = payload
    row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="studio.config.update",
                 entity=f"config:{kind}/{key}@v{version}",
                 payload={"kind": kind, "key": key, "version": version})
    session.commit()
    return _config_out(row)


# ---------------------------------------------------------------------------
# Lifecycle: publish / disable / rollback
# ---------------------------------------------------------------------------

def _publish_row(row: StudioConfig, session: Session, user: User, action: str,
                 extra: dict) -> StudioConfig:
    current = published_config(session, row.kind, row.key)
    superseded_version = None
    if current is not None and current.id != row.id:
        current.status = ConfigStatus.superseded
        superseded_version = current.version
        session.flush()
    row.status = ConfigStatus.published
    row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action=action,
                 entity=f"config:{row.kind}/{row.key}@v{row.version}",
                 payload={"kind": row.kind, "key": row.key, "version": row.version,
                          "superseded_version": superseded_version, **extra})
    session.commit()
    return row


@router.post("/{kind}/{key}/versions/{version}/publish")
def publish(kind: str, key: str, version: int, session: Session = Depends(get_db),
            user: User = Depends(require_capability("studio.manage"))):
    _require_kind(kind)
    row = get_config_version(session, kind, key, version)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Config {kind}/{key} v{version} not found")
    if row.status == ConfigStatus.published:
        raise HTTPException(status_code=409,
                            detail=f"Config {kind}/{key} v{version} is already published")
    if row.status == ConfigStatus.superseded:
        raise HTTPException(
            status_code=409,
            detail=(f"Config {kind}/{key} v{version} was superseded; use rollback "
                    "to bring its payload back"))
    # C-AI four-eyes (AI-G3, app.four_eyes): publishing an ai-provider
    # definition always requires a second person; publishing an agent
    # requires one when the agent's tier is T2/T3. The rollback path
    # (_publish_row via rollback) is EXEMPT: rollback restores a
    # previously approved payload — it is not a new approval, and the
    # payload it restores already passed this gate when first
    # published.
    from . import four_eyes
    if kind == "ai-provider" or (
            kind == "agent"
            and four_eyes.agent_tier_of(row.payload)
            in four_eyes.TIER_GATED_KINDS["agent"]):
        four_eyes.enforce(
            session, actor=user.username, authors={row.created_by},
            action="studio.config.publish",
            entity=f"config:{kind}/{key}@v{version}",
            detail=f"publish {kind} '{key}' v{version}")
    return _config_out(_publish_row(row, session, user, "studio.config.publish", {}))


@router.post("/{kind}/{key}/versions/{version}/disable")
def disable(kind: str, key: str, version: int, session: Session = Depends(get_db),
            user: User = Depends(require_capability("studio.manage"))):
    _require_kind(kind)
    row = get_config_version(session, kind, key, version)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Config {kind}/{key} v{version} not found")
    if row.status != ConfigStatus.published:
        raise HTTPException(
            status_code=409,
            detail=f"Only a published config can be disabled; v{version} is '{row.status.value}'")
    row.status = ConfigStatus.disabled
    row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="studio.config.disable",
                 entity=f"config:{kind}/{key}@v{version}",
                 payload={"kind": kind, "key": key, "version": version})
    session.commit()
    return _config_out(row)


@router.post("/{kind}/{key}/rollback", status_code=201)
async def rollback(kind: str, key: str, request: Request,
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("studio.manage"))):
    """Republish an earlier version's payload as a NEW published version.
    The target must have been published at some point (never a draft)."""
    _require_kind(kind)
    body = await _json_body(request)
    target_version = body.get("version") if isinstance(body, dict) else None
    if not isinstance(target_version, int):
        raise HTTPException(status_code=422,
                            detail="Rollback requires a JSON body {\"version\": <int>}")
    target = get_config_version(session, kind, key, target_version)
    if target is None:
        raise HTTPException(status_code=404,
                            detail=f"Config {kind}/{key} v{target_version} not found")
    if target.status == ConfigStatus.draft:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot roll back to v{target_version}: it is a draft and was never published")
    versions = list_config_versions(session, kind, key)
    new_row = StudioConfig(kind=kind, key=key, version=versions[-1].version + 1,
                           status=ConfigStatus.draft, payload=dict(target.payload),
                           created_by=user.username, updated_by=user.username)
    session.add(new_row)
    session.flush()
    return _config_out(_publish_row(new_row, session, user, "studio.config.rollback",
                                    {"source_version": target_version}))


# ---------------------------------------------------------------------------
# Reads: list / history / single version / diff
# ---------------------------------------------------------------------------

@router.get("")
def list_configs(kind: str | None = Query(default=None),
                 session: Session = Depends(get_db),
                 user: User = Depends(require_capability("studio.read"))):
    if kind is not None:
        _require_kind(kind)
    stmt = select(StudioConfig).order_by(StudioConfig.kind, StudioConfig.key,
                                         StudioConfig.version)
    if kind is not None:
        stmt = stmt.where(StudioConfig.kind == kind)
    rows = session.scalars(stmt).all()
    return [_config_out(r) for r in rows if _can_see(session, r, user)]


@router.get("/{kind}/{key}")
def config_history(kind: str, key: str, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("studio.read"))):
    _require_kind(kind)
    all_rows = list_config_versions(session, kind, key)
    if not all_rows:
        raise HTTPException(status_code=404, detail=f"No config found for {kind}/{key}")
    return [_config_out(r) for r in all_rows if _can_see(session, r, user)]


@router.get("/{kind}/{key}/diff")
def config_diff(kind: str, key: str, from_version: int = Query(..., alias="from"),
                to_version: int = Query(..., alias="to"),
                session: Session = Depends(get_db),
                user: User = Depends(require_capability("studio.read"))):
    _require_kind(kind)
    old = _get_visible(kind, key, from_version, session, user)
    new = _get_visible(kind, key, to_version, session, user)
    return {"kind": kind, "key": key, "from_version": from_version,
            "to_version": to_version, "changes": diff_payloads(old.payload, new.payload)}


@router.get("/{kind}/{key}/versions/{version}")
def get_version(kind: str, key: str, version: int, session: Session = Depends(get_db),
                user: User = Depends(require_capability("studio.read"))):
    _require_kind(kind)
    return _config_out(_get_visible(kind, key, version, session, user))


# ---------------------------------------------------------------------------
# Defaults seeded at startup
# ---------------------------------------------------------------------------

def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
    """Insert one config object as published v1 (idempotent: any existing
    version of the object means the seed already ran — never overwrite)."""
    if list_config_versions(session, kind, key):
        return
    row = StudioConfig(kind=kind, key=key, version=1,
                       status=ConfigStatus.published, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:{kind}/{key}@v1",
                 payload={"kind": kind, "key": key, "version": 1,
                          "seeded_default": True})
    append_audit(session, actor="system", action="studio.config.publish",
                 entity=f"config:{kind}/{key}@v1",
                 payload={"kind": kind, "key": key, "version": 1,
                          "superseded_version": None, "seeded_default": True})


# ---------------------------------------------------------------------------
# Seeded-default upgrade path (Home & Module UX pass, WS1c)
#
# _seed_published is insert-if-absent and never upgrades: a database
# seeded by an older build keeps that era's published navigation/main
# (and branding/default) forever. This machinery recognises an
# UNTOUCHED seeded default and publishes the current code default as a
# NEW version (superseding, never mutating, the old row); a customised
# payload is left alone and noted once per distinct payload hash as
# drift. Recognition is exactly per the coordinator ledger:
#   payload != current code default AND either
#     (P) provenance-clean, or
#     (F) fingerprint match against a known legacy-default projection.
# ---------------------------------------------------------------------------

# _publish_row only reads ``user.username`` (audit actor + updated_by);
# the upgrade acts as the system actor, matching the
# app.studio._SYSTEM_ACTOR idiom for system publishes.
_SYSTEM_ACTOR = SimpleNamespace(username="system")

# Pre-UX group for every module (Era D) — the groups the navigation
# carried before the Home & Module UX regroup. Built into legacy
# projections by transformation of the CURRENT default (see
# _legacy_nav_projections), never as a frozen payload copy.
_LEGACY_GROUP_BY_MODULE: dict[str, str] = {
    "patients": "Patient Care",
    "inbox": "Patient Care",
    "scheduling": "Patient Care",
    "portal": "Patient Care",
    "billing": "Revenue",
    "ed": "Clinical",
    "radiology": "Clinical",
    "surgery": "Clinical",
    "lab": "Laboratory",
    "pharmacy": "Pharmacy",
    "beds": "Operations",
    "command": "Operations",
    "er-ops": "Operations",
    "unit-board": "Operations",
    "clinic": "Operations",
    "aftercare": "Operations",
    "safety": "Operations",
    "support": "Operations",
    "workforce": "Operations",
    "teams": "Operations",
    "pophealth": "Data & Platform",
    "reports": "Data & Platform",
    "imports": "Data & Platform",
    "devices": "Data & Platform",
    "data-platform": "Data & Platform",
    "federation": "Data & Platform",
    "research": "Data & Platform",
    "analytics": "Data & Platform",
    "documents": "Data & Platform",
    "studio": "System Studio",
}


def _nav_projection(payload) -> tuple:
    """Canonical navigation projection: a sorted tuple of
    (module, label, tuple(sorted(roles)), group) per item.

    Deliberately ignores item order, ``order``, ``purpose``, ``icon``
    and ``sections``: order is presentation, purpose/icon are the UX
    pass's additive fields, and sections are deep-links inside a
    module. Publishing anything at all is a human publish, which the
    provenance arm (P) catches — the fingerprint (F) only needs to
    recognise the structural identity (which modules, with what
    labels/roles, in which groups) of a legacy seeded default.
    """
    if not isinstance(payload, dict):
        return ()
    items = payload.get("items")
    if not isinstance(items, list):
        return ()
    tuples = []
    for item in items:
        if not isinstance(item, dict):
            continue
        module = item.get("module") if isinstance(item.get("module"), str) else ""
        label = item.get("label") if isinstance(item.get("label"), str) else ""
        group = item.get("group") if isinstance(item.get("group"), str) else ""
        roles = item.get("roles")
        roles_tuple = tuple(sorted(roles)) if isinstance(roles, list) and all(
            isinstance(r, str) for r in roles) else ()
        tuples.append((module, label, roles_tuple, group))
    return tuple(sorted(tuples))


def _legacy_nav_projections() -> set:
    """Known legacy-default projections, built from the CURRENT
    default by transformation:

    - Era D: the full 30-module set with purpose/icon stripped (the
      projection ignores them anyway) and the pre-UX groups restored
      via _LEGACY_GROUP_BY_MODULE.
    - Era C: Era D minus {teams}.
    - Era B: Era C minus {federation, research, analytics, documents}.
    - Era A: Era B minus {support}.

    Modules introduced after every legacy era (Inventory & Materials
    pass: "inventory") belong to NO era — they are excluded from all
    four projections, so a genuine legacy payload keeps matching its
    era exactly and still upgrades to the current default (which is
    how those databases gain the new module's card).
    """
    base = []
    for item in default_navigation_payload().get("items") or []:
        module = item.get("module")
        roles = item.get("roles")
        roles_tuple = tuple(sorted(roles)) if isinstance(roles, list) else ()
        base.append((module, item.get("label"), roles_tuple,
                     _LEGACY_GROUP_BY_MODULE.get(module, item.get("group"))))

    def _proj(exclude: set) -> tuple:
        return tuple(sorted(t for t in base if t[0] not in exclude))

    era_b_exclude = {"teams", "federation", "research", "analytics", "documents"}
    post_era = {"inventory"}  # postdates every legacy era (see above)
    return {
        _proj(post_era),
        _proj(post_era | {"teams"}),
        _proj(post_era | era_b_exclude),
        _proj(post_era | era_b_exclude | {"support"}),
    }


def _is_provenance_clean(session: Session, kind: str, key: str,
                         versions: list) -> bool:
    """(P) Provenance arm: every StudioConfig row for the key was
    created_by "system" AND no studio.config.publish audit row for
    ``config:{kind}/{key}@*`` lacks seeded_default=True or carries a
    non-"system" actor.

    At least one such publish row must exist: provenance is positive
    evidence the payload was seeded. A payload with no audit history
    at all carries no such evidence and is not provenance-clean (it
    may still match the fingerprint arm, or be noted as drift).
    """
    if not versions:
        return False
    if any(row.created_by != "system" for row in versions):
        return False
    prefix = f"config:{kind}/{key}@"
    publish_rows = list(session.scalars(
        select(AuditLog).where(
            AuditLog.action == "studio.config.publish",
            AuditLog.entity.like(prefix + "%"))).all())
    # Defensive: LIKE treats %/_ in the prefix literally enough for
    # our keys, but confirm the prefix in Python as well.
    publish_rows = [r for r in publish_rows if r.entity.startswith(prefix)]
    if not publish_rows:
        return False
    for row in publish_rows:
        if row.actor != "system":
            return False
        payload = row.payload or {}
        if not isinstance(payload, dict) or payload.get("seeded_default") is not True:
            return False
    return True


def _matches_legacy_fingerprint(kind: str, key: str, payload,
                                current_default) -> bool:
    """(F) Fingerprint arm."""
    if kind == "navigation" and key == NAVIGATION_KEY:
        return _nav_projection(payload) in _legacy_nav_projections()
    if kind == "branding" and key == BRANDING_KEY:
        # Branding's fingerprint is its full current default payload:
        # there is a single historical branding default, so an
        # untouched branding payload EQUALS the current default and
        # the caller has already no-oped. A differing payload can
        # therefore only upgrade via the provenance arm. The check is
        # kept explicit so the machinery is wired for branding exactly
        # as specified.
        return payload == current_default
    return False


def _payload_hash(payload) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")).hexdigest()


def upgrade_seeded_default(session: Session, kind: str, key: str,
                           current_default: dict) -> None:
    """Upgrade one seeded config (navigation/main or branding/default)
    when its published payload is an untouched legacy default; note
    drift once per distinct payload hash when it is customised.

    Idempotent: when the published payload already equals the current
    default this is a no-op, so a second startup run changes nothing.
    """
    published = published_config(session, kind, key)
    if published is None:
        return
    if published.payload == current_default:
        return
    versions = list_config_versions(session, kind, key)
    if _is_provenance_clean(session, kind, key, versions) or \
            _matches_legacy_fingerprint(kind, key, published.payload,
                                        current_default):
        old_version = published.version
        next_version = (versions[-1].version + 1) if versions else 1
        new_row = StudioConfig(
            kind=kind, key=key, version=next_version,
            status=ConfigStatus.draft,
            payload=copy.deepcopy(current_default),
            created_by="system", updated_by="system")
        session.add(new_row)
        session.flush()
        append_audit(session, actor="system", action="studio.config.create",
                     entity=f"config:{kind}/{key}@v{next_version}",
                     payload={"kind": kind, "key": key,
                              "version": next_version,
                              "seeded_default": True,
                              "seed_upgrade": True,
                              "from_version": old_version})
        # _publish_row supersedes the old published row (never mutates
        # its payload), marks the new row published, writes the publish
        # audit with the extended payload, and commits.
        _publish_row(new_row, session, _SYSTEM_ACTOR,
                     "studio.config.publish",
                     {"seeded_default": True, "seed_upgrade": True,
                      "from_version": old_version})
        return
    # Customised (neither P nor F) and != current default: leave the
    # payload alone; write ONE drift note per distinct payload hash.
    payload_hash = _payload_hash(published.payload)
    existing_drift = session.scalars(
        select(AuditLog).where(
            AuditLog.action == "studio.config.seed_drift")).all()
    for row in existing_drift:
        drift_payload = row.payload or {}
        if isinstance(drift_payload, dict) \
                and drift_payload.get("kind") == kind \
                and drift_payload.get("key") == key \
                and drift_payload.get("payload_hash") == payload_hash:
            return
    append_audit(session, actor="system",
                 action="studio.config.seed_drift",
                 entity=f"config:{kind}/{key}@v{published.version}",
                 payload={"kind": kind, "key": key,
                          "version": published.version,
                          "payload_hash": payload_hash,
                          "hash": payload_hash})
    session.commit()


# Backwards-friendly private alias (internal callers use the public name).
_upgrade_seeded_default = upgrade_seeded_default


def ensure_default_configs(session: Session) -> None:
    """Seed the built-in published v1 configs at startup (idempotent):

    - the Appointment ADT state machine (workflow/appointment-adt) — from
      then on the ADT endpoint reads its transitions from Studio config;
      the hardcoded map in fhir.py is only a fallback for the case where
      this config has been disabled;
    - the shell navigation (navigation/main) and branding
      (branding/default) — payload-for-payload mirrors of the web shell's
      built-in defaults, so the shell consumes published config from first
      boot with zero visible change until a site publishes over them."""
    _seed_published(session, "workflow", ADT_WORKFLOW_KEY,
                    default_adt_workflow_payload())
    _seed_published(session, "navigation", NAVIGATION_KEY,
                    default_navigation_payload())
    _seed_published(session, "branding", BRANDING_KEY,
                    default_branding_payload())
    # WS1c seeded-default upgrade path: after the navigation and
    # branding seeds above, upgrade an untouched legacy seeded default
    # to the current code default (or note drift once if customised).
    # Fresh databases seeded just above carry the current default, so
    # both calls no-op there. No other seeded config is wired.
    upgrade_seeded_default(session, "navigation", NAVIGATION_KEY,
                           default_navigation_payload())
    upgrade_seeded_default(session, "branding", BRANDING_KEY,
                           default_branding_payload())
    # Phase 10 (Track K): the downtime read-only flag — published from
    # first boot in the OFF state, so the downtime middleware's config
    # is governed data an operator flips, never a code path.
    _seed_published(session, "integration", "downtime",
                    {"readOnly": False, "message": ""})
    # Phase 17 (WS-PACKGATE, BD-01 completion): pack-enablement route
    # gating ships ON. The published pack-gating config is seeded
    # enabled — the default lives in governed config data, not in a
    # code constant — and app.pack_gate treats absent config as ON
    # as the backstop. An operator turns the gate off only by
    # publishing {"enabled": false} (the audited escape hatch);
    # insert-if-absent seeding never overwrites that choice. The
    # core packs whose routes this gates are seeded enabled through
    # the pack lifecycle by app.studio (see main.create_app).
    _seed_published(session, "integration", "pack-gating",
                    {"enabled": True})
    # Phase 17: the Studio OIDC registration — published DISABLED with no
    # issuer, so a fresh deployment behaves exactly as before until a
    # site publishes an enabled version pointing at its IdP (e.g. the
    # live Keycloak realm). No secrets ever live in config.
    _seed_published(session, "integration", "oidc",
                    {"enabled": False, "issuer": "", "jwksUrl": "",
                     "clientId": "opencare-backend", "audience": "opencare-backend",
                     "rolesClaim": "realm_access.roles"})
    # Phase 17 (BD-54 / CMP-33): session idle-lock + tap-and-go re-entry
    # policy, consumed by the web shell (app.session_security).
    _seed_published(session, "integration", "session-security",
                    {"enabled": True, "idleLockMinutes": 15,
                     "reentryGraceMinutes": 5})
    # Phase 10 (Track B): dashboards are config. One published
    # operations overview ships as the worked example — sites publish
    # their own `dashboard` configs for their own views; the generic
    # Studio Dashboards view renders any of them.
    _seed_published(session, "dashboard", "operations-overview", {
        "title": "Operations overview",
        "widgets": [
            {"id": "census", "title": "Patients in beds",
             "endpoint": "/inpatient/census", "display": "total"},
            {"id": "critical-open", "title": "Critical reports awaiting closure",
             "endpoint": "/lab/critical-reports", "display": "total"},
            {"id": "verification-queue", "title": "Lab reports in verification",
             "endpoint": "/lab/verification-queue", "display": "total"},
        ]})
    session.commit()
