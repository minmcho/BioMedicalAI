"""Studio customization engine — versioned configuration API.

Implements docs/STUDIO_CUSTOMIZATION.md: everything site-specific (forms,
workflows, terminology, navigation, branding, permissions) is configuration
DATA with a draft -> publish lifecycle, never hardcoded per site.

Governance rules enforced here:
- Writes require admin/configurator (the same studio gate as module packs);
  clinician/viewer can only ever read PUBLISHED (effective) config.
- Published versions are immutable. Editing one means creating a new draft
  version (POST) and publishing it; publishing supersedes the previously
  published version, which is kept for rollback.
- Rollback republishes an earlier version's payload as a NEW version.
- Every create/update/publish/disable/rollback writes a hash-chained
  AuditLog row.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import append_audit
from .configstore import (ADT_WORKFLOW_KEY, CONFIG_KINDS, ConfigPayloadError,
                          default_adt_workflow_payload, diff_payloads,
                          get_config_version, list_config_versions,
                          published_config, published_configs, validate_payload)
from .models import ConfigStatus, Role, StudioConfig, User
from .security import get_db, require_roles

router = APIRouter(prefix="/studio/config", tags=["studio-config"])

STUDIO_ROLES = (Role.admin, Role.configurator)
READ_ROLES = (Role.admin, Role.configurator, Role.clinician, Role.viewer)


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


def _can_see(row: StudioConfig, user: User) -> bool:
    """Clinician/viewer see published config only; drafts are designer data."""
    if user.role in STUDIO_ROLES:
        return True
    return row.status == ConfigStatus.published


def _get_visible(kind: str, key: str, version: int, session: Session, user: User) -> StudioConfig:
    row = get_config_version(session, kind, key, version)
    if row is None or not _can_see(row, user):
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
                       user: User = Depends(require_roles(*READ_ROLES))):
    _require_kind(kind)
    return [_config_out(r) for r in published_configs(session, kind)]


@router.get("/{kind}/effective/{key}")
def effective_for_key(kind: str, key: str, session: Session = Depends(get_db),
                      user: User = Depends(require_roles(*READ_ROLES))):
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
                       user: User = Depends(require_roles(*STUDIO_ROLES))):
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
                       user: User = Depends(require_roles(*STUDIO_ROLES))):
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
            user: User = Depends(require_roles(*STUDIO_ROLES))):
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
    return _config_out(_publish_row(row, session, user, "studio.config.publish", {}))


@router.post("/{kind}/{key}/versions/{version}/disable")
def disable(kind: str, key: str, version: int, session: Session = Depends(get_db),
            user: User = Depends(require_roles(*STUDIO_ROLES))):
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
                   user: User = Depends(require_roles(*STUDIO_ROLES))):
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
                 user: User = Depends(require_roles(*READ_ROLES))):
    if kind is not None:
        _require_kind(kind)
    stmt = select(StudioConfig).order_by(StudioConfig.kind, StudioConfig.key,
                                         StudioConfig.version)
    if kind is not None:
        stmt = stmt.where(StudioConfig.kind == kind)
    rows = session.scalars(stmt).all()
    return [_config_out(r) for r in rows if _can_see(r, user)]


@router.get("/{kind}/{key}")
def config_history(kind: str, key: str, session: Session = Depends(get_db),
                   user: User = Depends(require_roles(*READ_ROLES))):
    _require_kind(kind)
    all_rows = list_config_versions(session, kind, key)
    if not all_rows:
        raise HTTPException(status_code=404, detail=f"No config found for {kind}/{key}")
    return [_config_out(r) for r in all_rows if _can_see(r, user)]


@router.get("/{kind}/{key}/diff")
def config_diff(kind: str, key: str, from_version: int = Query(..., alias="from"),
                to_version: int = Query(..., alias="to"),
                session: Session = Depends(get_db),
                user: User = Depends(require_roles(*READ_ROLES))):
    _require_kind(kind)
    old = _get_visible(kind, key, from_version, session, user)
    new = _get_visible(kind, key, to_version, session, user)
    return {"kind": kind, "key": key, "from_version": from_version,
            "to_version": to_version, "changes": diff_payloads(old.payload, new.payload)}


@router.get("/{kind}/{key}/versions/{version}")
def get_version(kind: str, key: str, version: int, session: Session = Depends(get_db),
                user: User = Depends(require_roles(*READ_ROLES))):
    _require_kind(kind)
    return _config_out(_get_visible(kind, key, version, session, user))


# ---------------------------------------------------------------------------
# Defaults seeded at startup
# ---------------------------------------------------------------------------

def ensure_default_configs(session: Session) -> None:
    """Seed the Appointment ADT state machine as published workflow config v1
    (idempotent). From then on the ADT endpoint reads its transitions from
    Studio config; the hardcoded map in fhir.py is only a fallback for the
    case where this config has been disabled."""
    existing = list_config_versions(session, "workflow", ADT_WORKFLOW_KEY)
    if existing:
        return
    row = StudioConfig(kind="workflow", key=ADT_WORKFLOW_KEY, version=1,
                       status=ConfigStatus.published,
                       payload=default_adt_workflow_payload(),
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:workflow/{ADT_WORKFLOW_KEY}@v1",
                 payload={"kind": "workflow", "key": ADT_WORKFLOW_KEY, "version": 1,
                          "seeded_default": True})
    append_audit(session, actor="system", action="studio.config.publish",
                 entity=f"config:workflow/{ADT_WORKFLOW_KEY}@v1",
                 payload={"kind": "workflow", "key": ADT_WORKFLOW_KEY, "version": 1,
                          "superseded_version": None, "seeded_default": True})
    session.commit()
