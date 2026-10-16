"""System Studio (adapted concept): module-pack lifecycle.

A pack is installed as a DESIGNER DRAFT only. It becomes usable only through
separate human steps, in order: approve -> attach -> enable. Backend surface
declared in a pack manifest ships with enabled=false and is never flipped by
installation itself.
"""
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import append_audit
from .models import (AuditLog, InstallationStatus, ModulePack, PackInstallation,
                     Role, User)
from .security import get_db, require_roles

router = APIRouter(prefix="/studio", tags=["studio"])

PACKS_DIR = Path(__file__).resolve().parent.parent / "packs"

STUDIO_ROLES = (Role.admin, Role.configurator)

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
def list_packs(session: Session = Depends(get_db), user: User = Depends(require_roles(
        Role.admin, Role.configurator, Role.clinician, Role.viewer))):
    return [_pack_out(p) for p in load_pack_manifests(session)]


@router.post("/packs/{name}/install", status_code=201)
def install_pack(name: str, session: Session = Depends(get_db),
                 user: User = Depends(require_roles(*STUDIO_ROLES))):
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
            user: User = Depends(require_roles(*STUDIO_ROLES))):
    return _transition("approve", installation_id, session, user)


@router.post("/installations/{installation_id}/attach")
def attach(installation_id: int, session: Session = Depends(get_db),
           user: User = Depends(require_roles(*STUDIO_ROLES))):
    return _transition("attach", installation_id, session, user)


@router.post("/installations/{installation_id}/enable")
def enable(installation_id: int, session: Session = Depends(get_db),
           user: User = Depends(require_roles(*STUDIO_ROLES))):
    return _transition("enable", installation_id, session, user)


@router.post("/installations/{installation_id}/disable")
def disable(installation_id: int, session: Session = Depends(get_db),
            user: User = Depends(require_roles(*STUDIO_ROLES))):
    return _transition("disable", installation_id, session, user)


@router.get("/installations")
def list_installations(session: Session = Depends(get_db), user: User = Depends(require_roles(
        Role.admin, Role.configurator, Role.clinician, Role.viewer))):
    out = []
    for inst in session.scalars(select(PackInstallation)).all():
        out.append(_installation_out(inst, session.get(ModulePack, inst.pack_id)))
    return out


@router.get("/audit")
def list_audit(limit: int = Query(default=50, ge=1, le=200),
               session: Session = Depends(get_db),
               user: User = Depends(require_roles(*STUDIO_ROLES))):
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
