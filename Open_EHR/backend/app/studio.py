"""System Studio (adapted concept): module-pack lifecycle.

A pack is installed as a DESIGNER DRAFT only. It becomes usable only through
separate human steps, in order: approve -> attach -> enable. Backend surface
declared in a pack manifest ships with enabled=false and is never flipped by
installation itself.
"""
import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import append_audit
from .configstore import (ConfigPayloadError, list_config_versions,
                          published_config, validate_payload)
from .models import (AuditLog, ConfigStatus, InstallationStatus, ModulePack,
                     PackInstallation, StudioConfig, User)
from .security import get_db, require_capability

router = APIRouter(prefix="/studio", tags=["studio"])

PACKS_DIR = Path(__file__).resolve().parent.parent / "packs"

# Allowed transitions: action -> (required current status, new status)
TRANSITIONS = {
    "approve": (InstallationStatus.installed_draft, InstallationStatus.approved),
    "attach": (InstallationStatus.approved, InstallationStatus.attached),
    "enable": (InstallationStatus.attached, InstallationStatus.enabled),
    "disable": (InstallationStatus.enabled, InstallationStatus.disabled),
}


def load_pack_manifests(session: Session) -> list[ModulePack]:
    packs: list[ModulePack] = []
    for path in sorted(PACKS_DIR.glob("*.pack.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        pack = session.scalars(
            select(ModulePack).where(ModulePack.name == manifest["name"],
                                     ModulePack.version == manifest["version"])
        ).first()
        if pack is None:
            pack = ModulePack(name=manifest["name"], version=manifest["version"], manifest=manifest)
            session.add(pack)
            session.flush()
        packs.append(pack)
    session.commit()
    return packs


def _pack_out(pack: ModulePack) -> dict:
    return {"id": pack.id, "name": pack.name, "version": pack.version, "manifest": pack.manifest}


def _installation_out(inst: PackInstallation, pack: ModulePack) -> dict:
    return {"id": inst.id, "pack_id": pack.id, "pack_name": pack.name,
            "version": pack.version, "status": inst.status.value, "installed_by": inst.installed_by}


@router.get("/packs")
def list_packs(session: Session = Depends(get_db),
               user: User = Depends(require_capability("studio.read"))):
    return [_pack_out(p) for p in load_pack_manifests(session)]


@router.post("/packs/{name}/install", status_code=201)
def install_pack(name: str, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("studio.manage"))):
    load_pack_manifests(session)
    pack = session.scalars(select(ModulePack).where(ModulePack.name == name)).first()
    if pack is None:
        raise HTTPException(status_code=404, detail=f"Pack not found: {name}")
    existing = session.scalars(select(PackInstallation).where(PackInstallation.pack_id == pack.id)).first()
    if existing is not None:
        return _installation_out(existing, pack)  # idempotent: no duplicate installation
    inst = PackInstallation(pack_id=pack.id, status=InstallationStatus.installed_draft,
                            installed_by=user.username)
    session.add(inst)
    session.flush()
    append_audit(session, actor=user.username, action="studio.install",
                 entity=f"pack:{pack.name}@{pack.version}", payload={"installation_id": inst.id})
    session.commit()
    return _installation_out(inst, pack)


def _transition(action: str, installation_id: int, session: Session, user: User):
    inst = session.get(PackInstallation, installation_id)
    if inst is None:
        raise HTTPException(status_code=404, detail="Installation not found")
    required, target = TRANSITIONS[action]
    if inst.status != required:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot {action} an installation in status '{inst.status.value}'; requires '{required.value}'")
    inst.status = target
    session.flush()
    append_audit(session, actor=user.username, action=f"studio.{action}",
                 entity=f"installation:{inst.id}", payload={"from_status": required.value, "to_status": target.value})
    session.commit()
    pack = session.get(ModulePack, inst.pack_id)
    return _installation_out(inst, pack)


@router.post("/installations/{installation_id}/approve")
def approve(installation_id: int, session: Session = Depends(get_db),
            user: User = Depends(require_capability("studio.manage"))):
    return _transition("approve", installation_id, session, user)


@router.post("/installations/{installation_id}/attach")
def attach(installation_id: int, session: Session = Depends(get_db),
           user: User = Depends(require_capability("studio.manage"))):
    return _transition("attach", installation_id, session, user)


# ---------------------------------------------------------------------------
# Profile packs: configuration applied at ENABLE time (Phase 5,
# roadmap §5). A pack manifest may carry a `configuration` list of
# {kind, key, payload} entries — deployment profiles are packs of this
# shape. Enabling such a pack publishes each entry as a NEW version
# (superseding whatever is currently published, hash-chain audited
# with via_pack attribution). Disabling the pack does NOT unpublish:
# configuration, once applied, is governed Studio data like any
# other — a site rolls it back through the normal config rollback,
# not by toggling the pack. Packs without a `configuration` list
# behave exactly as before (no config is touched).
#
# Payload strings may reference pack parameters as "{{name}}",
# supplied in the enable request body {"parameters": {...}}. The one
# parameter with a built-in fallback is orgName: the currently
# published branding org name (or "OpenCare"), so a profile can re-skin
# a deployment without renaming the organisation unless asked to.
# ---------------------------------------------------------------------------

_PARAM_RE = re.compile(r"\{\{(\w+)\}\}")


def _resolve_parameters(value, parameters: dict, session: Session,
                        pack_name: str):
    if isinstance(value, str):
        def replace(match: re.Match) -> str:
            name = match.group(1)
            if name in parameters:
                return str(parameters[name])
            if name == "orgName":
                branding = published_config(session, "branding", "default")
                org = (branding.payload or {}).get("orgName") \
                    if branding is not None else None
                return org if isinstance(org, str) and org else "OpenCare"
            raise HTTPException(
                status_code=422,
                detail=(f"Pack '{pack_name}' configuration requires parameter "
                        f"'{name}'; supply it in the enable request body as "
                        '{"parameters": {...}}'))
        return _PARAM_RE.sub(replace, value)
    if isinstance(value, dict):
        return {k: _resolve_parameters(v, parameters, session, pack_name)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_parameters(v, parameters, session, pack_name)
                for v in value]
    return value


def _apply_pack_configuration(session: Session, pack: ModulePack,
                              user: User, parameters: dict) -> list[dict]:
    """Publish a pack's `configuration` entries as new versions.
    Two phases: validate EVERYTHING first (payload shape, parameter
    resolution, no in-progress draft in the way) and only then write,
    so a failed apply changes nothing — the enable that called us
    rolls back as one transaction."""
    entries = (pack.manifest or {}).get("configuration") or []
    if not isinstance(entries, list) or not entries:
        return []
    prepared = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or not entry.get("kind") \
                or not entry.get("key") or "payload" not in entry:
            raise HTTPException(
                status_code=422,
                detail=(f"Pack '{pack.name}' configuration[{i}] must be an "
                        "object with kind, key and payload"))
        kind, key = str(entry["kind"]), str(entry["key"])
        payload = _resolve_parameters(entry["payload"], parameters,
                                      session, pack.name)
        try:
            validate_payload(kind, payload)
        except ConfigPayloadError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=(f"Pack '{pack.name}' configuration {kind}/{key} is "
                        f"invalid: {exc.detail}"))
        except KeyError:
            raise HTTPException(
                status_code=422,
                detail=(f"Pack '{pack.name}' configuration[{i}] names unknown "
                        f"config kind '{kind}'"))
        versions = list_config_versions(session, kind, key)
        if any(v.status == ConfigStatus.draft for v in versions):
            raise HTTPException(
                status_code=409,
                detail=(f"Cannot apply pack '{pack.name}': a draft already "
                        f"exists for {kind}/{key}; publish or discard it "
                        "before enabling a profile over it"))
        prepared.append((kind, key, payload, versions))

    applied = []
    for kind, key, payload, versions in prepared:
        current = published_config(session, kind, key)
        superseded_version = None
        if current is not None:
            current.status = ConfigStatus.superseded
            superseded_version = current.version
            session.flush()
        next_version = (versions[-1].version + 1) if versions else 1
        row = StudioConfig(kind=kind, key=key, version=next_version,
                           status=ConfigStatus.published, payload=payload,
                           created_by=user.username, updated_by=user.username)
        session.add(row)
        session.flush()
        for action in ("studio.config.create", "studio.config.publish"):
            append_audit(session, actor=user.username, action=action,
                         entity=f"config:{kind}/{key}@v{next_version}",
                         payload={"kind": kind, "key": key,
                                  "version": next_version,
                                  "superseded_version": superseded_version
                                  if action.endswith("publish") else None,
                                  "via_pack": pack.name,
                                  "profile_apply": True})
        applied.append({"kind": kind, "key": key, "version": next_version})
    return applied


@router.post("/installations/{installation_id}/enable")
async def enable(installation_id: int, request: Request,
                 session: Session = Depends(get_db),
                 user: User = Depends(require_capability("studio.manage"))):
    """Enable an attached installation. For packs carrying a
    `configuration` list (deployment profiles), enabling also APPLIES
    that configuration (see _apply_pack_configuration); an optional
    JSON body {"parameters": {...}} supplies pack parameters. A failed
    apply aborts the enable — the installation stays `attached`."""
    inst = session.get(PackInstallation, installation_id)
    if inst is None:
        raise HTTPException(status_code=404, detail="Installation not found")
    required, target = TRANSITIONS["enable"]
    if inst.status != required:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot enable an installation in status '{inst.status.value}'; requires '{required.value}'")
    parameters: dict = {}
    try:
        body = await request.json()
    except Exception:
        body = None
    if isinstance(body, dict) and isinstance(body.get("parameters"), dict):
        parameters = body["parameters"]
    pack = session.get(ModulePack, inst.pack_id)
    applied = _apply_pack_configuration(session, pack, user, parameters) \
        if pack is not None else []
    inst.status = target
    session.flush()
    append_audit(session, actor=user.username, action="studio.enable",
                 entity=f"installation:{inst.id}",
                 payload={"from_status": required.value,
                          "to_status": target.value,
                          "applied_configuration": applied})
    session.commit()
    out = _installation_out(inst, pack)
    out["applied_configuration"] = applied
    return out


@router.post("/installations/{installation_id}/disable")
def disable(installation_id: int, session: Session = Depends(get_db),
            user: User = Depends(require_capability("studio.manage"))):
    return _transition("disable", installation_id, session, user)


@router.get("/installations")
def list_installations(session: Session = Depends(get_db),
                       user: User = Depends(require_capability("studio.read"))):
    out = []
    for inst in session.scalars(select(PackInstallation)).all():
        out.append(_installation_out(inst, session.get(ModulePack, inst.pack_id)))
    return out


@router.get("/audit")
def list_audit(limit: int = Query(default=50, ge=1, le=200),
               session: Session = Depends(get_db),
               user: User = Depends(require_capability("studio.manage"))):
    """Most recent entries of the hash-chained audit log, newest first.

    One chain covers pack lifecycle actions, Studio config writes and
    clinical writes (app.audit.append_audit), so this single read surface
    serves the Studio audit tab. Read-only; admin/configurator only —
    the chain is governance data, not clinical data."""
    rows = session.scalars(
        select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).all()
    return [{
        "id": r.id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "actor": r.actor, "action": r.action, "entity": r.entity,
        "entry_hash": r.entry_hash, "prev_hash": r.prev_hash,
    } for r in rows]
