"""Phase 16 (WS-A1, BD-49) — FHIR directory sync.

Materialises the FHIR directory resources from the operational
sources of truth, so other modules (notably the Phase 16 federation
layer, BD-36, which reads Consent + directory references) can work
against ordinary FHIR resources instead of private tables:

- **Practitioner** — one per `people.StaffMember` row. The resource
  id is the staff_id and the resource also carries the staff_id as an
  identifier (`urn:opencare:staff-id`), which is the sync key: a
  re-sync finds the existing resource BY IDENTIFIER and updates it
  in place (version bump), never duplicates it.
- **Organization** — one per region / facility / department / unit
  concept in the PUBLISHED `terminology/locations` config (the same
  config app.inpatient materialises Location resources from;
  beds/rooms are Locations, not Organizations, and are excluded).
  The resource id is the location code and the code is also carried
  as an identifier (`urn:opencare:location-code`), the sync key, so
  a locations republication that adds a unit adds exactly one
  Organization on the next sync. `partOf` follows the concept's
  parent, so the Organization tree mirrors the governed hierarchy.

Sync (`POST /directories/sync`):

- Idempotent create-or-update: second sync with unchanged sources
  reports every resource `unchanged` and creates nothing.
- `dryRun: true` returns the full plan (per-resource action) and
  writes NO FHIR resources and NO sync-run row. The dry run itself
  is still audited — "every sync audited" includes dry runs — with
  `dry_run: true` in the audit payload.
- Every real sync writes one `DirectorySyncRun` row (this module's
  own table) and one hash-chained audit entry
  (`directories.sync`) in the SAME transaction as the FHIR writes;
  the run row stores that audit entry's hash as its `audit_ref`,
  which `GET /directories/status` surfaces as the last-sync ref.
- Consent resources are NOT materialised here: they are plain FHIR
  Consent in the store (added to fhir.SUPPORTED by this WS, read by
  BD-36). There is no consent workflow in this module.

Capability gates (proposal for the coordinator — see the WS-A1
integration note): the sync is a directory-administration act.
Until dedicated `directories.read` / `directories.sync` capabilities
are registered in CAPABILITY_DEFAULTS, this module gates sync on
`studio.manage` (admin/configurator — the directories-appropriate
default named in the task) and reads on `workforce.read`
(admin/configurator/clinician/viewer). The gate helpers ALSO honour
the proposed `directories.*` capabilities when a site publishes
them in the permissions config, so registering the defaults later
needs no code change here.

Studio config surface: published `integration/directories`
(identifier systems + which location types become Organizations),
seeded published v1 by `ensure_directories_defaults`. Changing the
identifier systems in config changes the sync keys on the next sync
— configuration, not code (configure, don't fork).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import DateTime, Integer, JSON, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import fhir
from .audit import append_audit
from .configstore import published_config, role_allowed
from .db import Base
from .models import (AuditLog, ConfigStatus, FhirResource, StaffMember,
                     StudioConfig, User, utcnow)
from .security import get_current_user, get_db

router = APIRouter(prefix="/directories", tags=["directories"])

DIRECTORIES_KEY = "directories"  # integration/directories
LOCATIONS_KEY = "locations"      # terminology/locations (app.inpatient)

STAFF_ID_SYSTEM = "urn:opencare:staff-id"
LOCATION_CODE_SYSTEM = "urn:opencare:location-code"
SOURCE_SYSTEM = "urn:opencare:source"
SOURCE_CODE = "directory-sync"

# Location concept types that become Organizations: the whole
# governed org hierarchy (region -> facility -> department -> unit),
# so a unit's Organization partOf names its department, the
# department's the facility, and the facility's the region — every
# partOf reference resolves. Beds and rooms stay Locations only.
ORGANIZATION_LOCATION_TYPES = ("region", "facility", "department",
                               "unit")


# ---------------------------------------------------------------------------
# This module's own table
# ---------------------------------------------------------------------------

class DirectorySyncRun(Base):
    """One completed (non-dry-run) directory sync — Phase 16 (WS-A1).

    Counts only, plus the hash of the `directories.sync` audit entry
    written in the same transaction (`audit_ref`): the status
    endpoint reports the latest row, so "when did we last sync, who
    ran it, what changed, and where is it in the audit chain" is one
    read. Dry runs write no row here (they write nothing but their
    audit entry)."""
    __tablename__ = "directory_sync_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sync_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False,
                                       default="")
    practitioner_created: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0)
    practitioner_updated: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0)
    practitioner_unchanged: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0)
    organization_created: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0)
    organization_updated: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0)
    organization_unchanged: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    audit_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Studio config: integration/directories
# ---------------------------------------------------------------------------

def default_directories_payload() -> dict:
    return {
        "name": "FHIR directory sync (synthetic defaults)",
        "practitionerIdentifierSystem": STAFF_ID_SYSTEM,
        "organizationIdentifierSystem": LOCATION_CODE_SYSTEM,
        "organizationLocationTypes": list(ORGANIZATION_LOCATION_TYPES),
    }


def _directories_config(session: Session) -> dict:
    cfg = published_config(session, "integration", DIRECTORIES_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    defaults = default_directories_payload()
    return {**defaults, **{k: v for k, v in payload.items() if v is not None}}


def _seed_published(session: Session, kind: str, key: str,
                    payload: dict) -> None:
    existing = session.scalars(
        select(StudioConfig).where(StudioConfig.kind == kind,
                                   StudioConfig.key == key)).first()
    if existing is not None:
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


def ensure_directories_defaults(session: Session) -> None:
    """Seed published integration/directories v1 (idempotent). No
    resources are materialised here — materialisation is the explicit,
    audited POST /directories/sync act."""
    _seed_published(session, "integration", DIRECTORIES_KEY,
                    default_directories_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Capability gates (see module docstring for the proposal)
# ---------------------------------------------------------------------------

def _allowed(session: Session, user: User, *capabilities: str) -> bool:
    return any(role_allowed(session, user.role, cap)
               for cap in capabilities)


def _require_sync(session: Session = Depends(get_db),
                  user: User = Depends(get_current_user)) -> User:
    if _allowed(session, user, "directories.sync", "studio.manage"):
        return user
    raise HTTPException(
        status_code=403,
        detail=(f"Role {user.role.value} lacks capability "
                "'directories.sync' (fallback: 'studio.manage')"))


def _require_read(session: Session = Depends(get_db),
                  user: User = Depends(get_current_user)) -> User:
    if _allowed(session, user, "directories.read", "workforce.read",
                "studio.read"):
        return user
    raise HTTPException(
        status_code=403,
        detail=(f"Role {user.role.value} lacks capability "
                "'directories.read' (fallback: 'workforce.read')"))


# ---------------------------------------------------------------------------
# Desired-resource builders (pure: source row -> FHIR body)
# ---------------------------------------------------------------------------

def _split_name(full_name: str) -> dict:
    """FHIR HumanName from the directory's display string. The seed
    format is "Given Family, Credential" — the credential suffix is
    dropped from family/given but the full string is kept in `text`,
    so nothing is lost."""
    text = (full_name or "").strip()
    base = text.split(",", 1)[0].strip()
    parts = base.split()
    name: dict = {"text": text}
    if len(parts) >= 2:
        name["family"] = parts[-1]
        name["given"] = parts[:-1]
    elif parts:
        name["family"] = parts[0]
    return name


def _practitioner_body(staff: StaffMember, config: dict) -> dict:
    system = str(config.get("practitionerIdentifierSystem")
                 or STAFF_ID_SYSTEM)
    body: dict = {
        "resourceType": "Practitioner",
        "id": staff.staff_id,
        "identifier": [{"system": system, "value": staff.staff_id}],
        "active": staff.status == "active",
        "name": [_split_name(staff.full_name)],
        "meta": {"tag": [{"system": SOURCE_SYSTEM, "code": SOURCE_CODE}]},
    }
    telecom = []
    if staff.email:
        telecom.append({"system": "email", "value": staff.email,
                        "use": "work"})
    if staff.phone:
        telecom.append({"system": "phone", "value": staff.phone,
                        "use": "work"})
    if telecom:
        body["telecom"] = telecom
    if staff.job_role:
        body["qualification"] = [{
            "code": {"coding": [{
                "system": "urn:opencare:job-role",
                "code": staff.job_role}]}}]
    extensions = []
    for url_suffix, value in (("job-role", staff.job_role),
                              ("staff-unit", staff.unit),
                              ("service-line", staff.service_line),
                              ("username", staff.username),
                              ("staff-status", staff.status)):
        if value:
            extensions.append({
                "url": f"urn:opencare:extension/{url_suffix}",
                "valueString": value})
    if extensions:
        body["extension"] = extensions
    return body


def _organization_body(concept: dict, config: dict) -> dict:
    system = str(config.get("organizationIdentifierSystem")
                 or LOCATION_CODE_SYSTEM)
    code = str(concept.get("code"))
    level = str(concept.get("type") or "")
    type_code = "prov" if level in ("facility", "region") else "dept"
    type_display = ("Healthcare Provider"
                    if level in ("facility", "region")
                    else "Hospital Department")
    body: dict = {
        "resourceType": "Organization",
        "id": code,
        "identifier": [{"system": system, "value": code}],
        "active": concept.get("active") is not False,
        "type": [{"coding": [{
            "system": ("http://terminology.hl7.org/CodeSystem/"
                       "organization-type"),
            "code": type_code, "display": type_display}]}],
        "name": concept.get("display") or code,
        "meta": {"tag": [{"system": SOURCE_SYSTEM, "code": SOURCE_CODE}]},
        "extension": [{
            "url": "urn:opencare:extension/location-type",
            "valueString": level}],
    }
    parent = concept.get("parent")
    if isinstance(parent, str) and parent:
        body["partOf"] = {"reference": f"Organization/{parent}"}
    return body


def _location_org_concepts(session: Session, config: dict) -> list[dict]:
    """Region + facility + department + unit concepts from the
    PUBLISHED locations config, in payload order. No published
    config -> no Organizations (the plan honestly reports zero, it
    does not invent a facility)."""
    cfg = published_config(session, "terminology", LOCATIONS_KEY)
    if cfg is None:
        return []
    wanted = config.get("organizationLocationTypes")
    if not isinstance(wanted, list) or not wanted:
        wanted = list(ORGANIZATION_LOCATION_TYPES)
    return [c for c in (cfg.payload or {}).get("concepts") or []
            if isinstance(c, dict) and c.get("code")
            and c.get("type") in wanted]


# ---------------------------------------------------------------------------
# Sync planning / application
# ---------------------------------------------------------------------------

def _find_by_identifier(session: Session, resource_type: str,
                        system: str, value: str) -> FhirResource | None:
    """The stored resource carrying identifier system+value, or None.
    Falls back to the deterministic id (resource id == key value for
    everything this sync creates) so a resource whose identifier was
    edited still resolves instead of duplicating."""
    rows = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == resource_type)).all()
    for row in rows:
        for ident in (row.body or {}).get("identifier") or []:
            if isinstance(ident, dict) and ident.get("system") == system \
                    and str(ident.get("value")) == value:
                return row
    return session.scalars(select(FhirResource).where(
        FhirResource.resource_type == resource_type,
        FhirResource.resource_id == value)).first()


def _plan(session: Session, config: dict) -> dict:
    """Compute the create/update/unchanged plan for both resource
    types WITHOUT writing. The same plan drives dry-run responses
    and the real apply, so a dry run can never promise a different
    outcome than the sync that follows it."""
    prac_system = str(config.get("practitionerIdentifierSystem")
                      or STAFF_ID_SYSTEM)
    org_system = str(config.get("organizationIdentifierSystem")
                     or LOCATION_CODE_SYSTEM)
    plan: dict = {"practitioners": [], "organizations": []}
    staff_rows = session.scalars(
        select(StaffMember).order_by(StaffMember.staff_id.asc())).all()
    for staff in staff_rows:
        desired = _practitioner_body(staff, config)
        existing = _find_by_identifier(session, "Practitioner",
                                       prac_system, staff.staff_id)
        if existing is None:
            action = "create"
        elif (existing.body or {}) == desired:
            action = "unchanged"
        else:
            action = "update"
        plan["practitioners"].append({
            "key": staff.staff_id, "action": action,
            "resource_id": (existing.resource_id if existing is not None
                            else staff.staff_id),
            "_desired": desired, "_existing": existing})
    for concept in _location_org_concepts(session, config):
        code = str(concept["code"])
        desired = _organization_body(concept, config)
        existing = _find_by_identifier(session, "Organization",
                                       org_system, code)
        if existing is None:
            action = "create"
        elif (existing.body or {}) == desired:
            action = "unchanged"
        else:
            action = "update"
        plan["organizations"].append({
            "key": code, "action": action,
            "resource_id": (existing.resource_id if existing is not None
                            else code),
            "_desired": desired, "_existing": existing})
    return plan


def _counts(items: list[dict]) -> dict:
    return {
        "total": len(items),
        "create": sum(1 for i in items if i["action"] == "create"),
        "update": sum(1 for i in items if i["action"] == "update"),
        "unchanged": sum(1 for i in items if i["action"] == "unchanged"),
    }


def _apply(session: Session, resource_type: str, items: list[dict]) -> None:
    for item in items:
        if item["action"] == "unchanged":
            continue
        desired = item["_desired"]
        existing = item["_existing"]
        if existing is None:
            row = FhirResource(resource_type=resource_type,
                               resource_id=item["resource_id"], version=1,
                               body=desired)
            fhir._apply_projections(row, resource_type, desired,
                                    item["resource_id"])
            session.add(row)
        else:
            existing.body = desired
            existing.version += 1
            fhir._apply_projections(existing, resource_type, desired,
                                    existing.resource_id)
    session.flush()


async def _json_body(request: Request) -> dict:
    if request.headers.get("content-length") in (None, "0"):
        return {}
    try:
        body = await request.json()
    except Exception:
        return {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=422,
                            detail="Request body must be a JSON object")
    return body


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/sync")
async def sync_directories(request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(_require_sync)):
    """Materialise Practitioner + Organization resources from the
    staff directory and the published locations config. Body:
    {"dryRun": true} (or "dry_run") returns the plan and writes no
    FHIR resources; anything else performs the sync. Every call —
    dry or real — writes a hash-chained audit entry in the same
    transaction as any writes it makes."""
    body = await _json_body(request)
    dry_run = bool(body.get("dryRun", body.get("dry_run", False)))
    config = _directories_config(session)
    plan = _plan(session, config)
    prac_counts = _counts(plan["practitioners"])
    org_counts = _counts(plan["organizations"])
    payload = {
        "dry_run": dry_run,
        "practitioners": prac_counts,
        "organizations": org_counts,
    }
    if dry_run:
        entry = append_audit(session, actor=user.username,
                             action="directories.sync",
                             entity="directories", payload=payload)
        session.commit()
        return {
            "dry_run": True,
            "practitioners": {**prac_counts, "items": [
                {"key": i["key"], "action": i["action"],
                 "reference": f"Practitioner/{i['resource_id']}"}
                for i in plan["practitioners"]]},
            "organizations": {**org_counts, "items": [
                {"key": i["key"], "action": i["action"],
                 "reference": f"Organization/{i['resource_id']}"}
                for i in plan["organizations"]]},
            "audit_ref": entry.entry_hash,
            "note": "Dry run — no FHIR resources were written.",
        }
    _apply(session, "Practitioner", plan["practitioners"])
    _apply(session, "Organization", plan["organizations"])
    entry = append_audit(session, actor=user.username,
                         action="directories.sync",
                         entity="directories", payload=payload)
    session.flush()
    run = DirectorySyncRun(
        sync_id=f"dirsync-{entry.id}",
        actor=user.username,
        practitioner_created=prac_counts["create"],
        practitioner_updated=prac_counts["update"],
        practitioner_unchanged=prac_counts["unchanged"],
        organization_created=org_counts["create"],
        organization_updated=org_counts["update"],
        organization_unchanged=org_counts["unchanged"],
        detail=payload, audit_ref=entry.entry_hash)
    session.add(run)
    session.commit()
    return {
        "dry_run": False,
        "sync_id": run.sync_id,
        "practitioners": {**prac_counts, "items": [
            {"key": i["key"], "action": i["action"],
             "reference": f"Practitioner/{i['resource_id']}"}
            for i in plan["practitioners"]]},
        "organizations": {**org_counts, "items": [
            {"key": i["key"], "action": i["action"],
             "reference": f"Organization/{i['resource_id']}"}
            for i in plan["organizations"]]},
        "audit_ref": entry.entry_hash,
    }


def _last_sync(session: Session) -> dict | None:
    run = session.scalars(
        select(DirectorySyncRun).order_by(DirectorySyncRun.id.desc())
    ).first()
    if run is not None:
        at = run.created_at
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return {
            "sync_id": run.sync_id, "actor": run.actor,
            "at": at.isoformat() if at else None,
            "audit_ref": run.audit_ref,
            "practitioners": {
                "created": run.practitioner_created,
                "updated": run.practitioner_updated,
                "unchanged": run.practitioner_unchanged},
            "organizations": {
                "created": run.organization_created,
                "updated": run.organization_updated,
                "unchanged": run.organization_unchanged},
        }
    # Fallback for deployments synced before the run table existed
    # (and for a last action that was a dry run): the audit chain is
    # the record either way.
    entry = session.scalars(
        select(AuditLog).where(AuditLog.action == "directories.sync")
        .order_by(AuditLog.id.desc())).first()
    if entry is None:
        return None
    at = entry.created_at
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return {"sync_id": None, "actor": entry.actor,
            "at": at.isoformat() if at else None,
            "audit_ref": entry.entry_hash,
            "detail": entry.payload or {}}


@router.get("/status")
def sync_status(session: Session = Depends(get_db),
                user: User = Depends(_require_read)):
    """Sync status: live FHIR counts vs source counts, plus the last
    sync (with its audit-chain ref). Counts are computed from the
    store at call time — never cached from the last run."""
    config = _directories_config(session)
    staff_total = len(session.scalars(select(StaffMember)).all())
    org_source_total = len(_location_org_concepts(session, config))
    prac_total = len(session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Practitioner")).all())
    org_total = len(session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Organization")).all())
    consent_total = len(session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Consent")).all())
    runs = len(session.scalars(select(DirectorySyncRun)).all())
    return {
        "practitioners": {"fhir_total": prac_total,
                          "source_total": staff_total},
        "organizations": {"fhir_total": org_total,
                          "source_total": org_source_total},
        "consents": {"fhir_total": consent_total},
        "sync_runs": runs,
        "last_sync": _last_sync(session),
    }


@router.get("/practitioners")
def list_practitioners(session: Session = Depends(get_db),
                       user: User = Depends(_require_read)):
    rows = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Practitioner")
        .order_by(FhirResource.resource_id.asc())).all()
    return {"items": [fhir._render(r) for r in rows], "total": len(rows)}


@router.get("/organizations")
def list_organizations(session: Session = Depends(get_db),
                       user: User = Depends(_require_read)):
    rows = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Organization")
        .order_by(FhirResource.resource_id.asc())).all()
    return {"items": [fhir._render(r) for r in rows], "total": len(rows)}
