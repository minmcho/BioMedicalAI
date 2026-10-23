"""Studio Ops — PLATFORM PASS Workstream 10 (Studio Full Parity).

Three read surfaces plus two declaration validators, all built on the
existing governance (docs/STUDIO_PARITY.md rows 5, 15, 18, 30, 37):

- ``GET /studio-ops/inbox`` — one approvals inbox aggregating REAL
  pending human-action items from their owning modules (pack
  lifecycle, Studio config drafts, break-glass review, pipeline DQ
  quarantine, MPI dedup candidates). Each item deep-links by hint
  (``action``) to its owning surface; this module decides nothing.
- ``GET /studio-ops/organisation`` — a read model over the real org
  structure: the published locations tree, the staff directory and
  the managed accounts. Edits stay in the owning governed surfaces.
- ``GET /studio-ops/data-views`` — a read-only registry of governed
  data sources (report-engine/export datasets, OMOP warehouse
  tables, lake zones) with live counts. NO SQL anywhere: there is
  no query editor and no ad-hoc query path in this module.

Declarations are governance artefacts ONLY: ``validate_endpoint_declaration``
and ``validate_tool_declaration`` validate endpoint/tool DECLARATIONS
for the Studio config kinds ``endpoint`` and ``tool``. Nothing in
this module creates a route, runs a tool, or executes anything.
Endpoint/tool declarations are born disabled; enablement is a
governance marker, not a runtime effect. MCP execution lands in
Phase 16 (Min's placement directive).

Governance law: existing capabilities only — every route here is a
read, so every route is gated ``studio.read``. No new capability is
introduced.

Inbox sources: all five specified sources are included (pack
installations, config drafts, break-glass reviews, DQ quarantine,
dedup candidates) — each is honestly queryable from a real model,
so none is omitted.
"""
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .configstore import CAPABILITY_DEFAULTS, published_config
from .models import (BreakGlassAccess, ConfigStatus, DataQuarantine,
                     DedupCandidate, InstallationStatus, LakeLineageLink,
                     LakeRawEntry, ModulePack, PackInstallation,
                     StaffMember, StudioConfig, User, UserAccount)
from .reports import EXPORT_DATASETS
from .security import get_db, require_capability
from .warehouse import WarehouseBase, warehouse_session_factory

router = APIRouter(prefix="/studio-ops", tags=["studio-ops"])

INBOX_CAP = 200
# The published locations config: Phase 6 lives at
# terminology/locations (app.inpatient.LOCATIONS_KEY), payload
# {name, system, bedStatuses, concepts: [{code, display, type:
# facility|unit|room|bed, parent, room?, bed?, active}]} — facilities
# are concepts of type "facility", units type "unit" with parent =
# facility code, beds type "bed" with parent = unit code.
LOCATIONS_KIND = "terminology"
LOCATIONS_KEY = "locations"


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _sort_key(value) -> float:
    """Epoch seconds for newest-first sorting; naive (SQLite) and
    aware datetimes are normalised to UTC so they never compare
    against each other directly."""
    if value is None:
        return 0.0
    from datetime import timezone
    aware = value if value.tzinfo is not None else value.replace(
        tzinfo=timezone.utc)
    return aware.timestamp()


# ---------------------------------------------------------------------------
# 1. Approvals inbox
# ---------------------------------------------------------------------------

# Pack installation status -> the human step it is waiting for.
_PACK_STEPS = {
    InstallationStatus.installed_draft: ("approve", "awaiting approval"),
    InstallationStatus.approved: ("attach", "awaiting attach"),
    InstallationStatus.attached: ("enable", "awaiting enablement"),
}


def _inbox_items(session: Session) -> list[dict]:
    items: list[dict] = []

    # Pack installations awaiting approve / attach / enable.
    for inst in session.scalars(select(PackInstallation).where(
            PackInstallation.status.in_(
                list(_PACK_STEPS)))).all():
        step, label = _PACK_STEPS[inst.status]
        pack = session.get(ModulePack, inst.pack_id)
        pack_name = pack.name if pack is not None else f"pack #{inst.pack_id}"
        version = pack.version if pack is not None else ""
        items.append({
            "source": "pack-installation",
            "id": inst.id,
            "title": f"Pack {pack_name}@{version} {label}",
            "detail": (f"Installation {inst.id} is in status "
                       f"'{inst.status.value}' (installed by "
                       f"{inst.installed_by or 'unknown'}); the next "
                       f"governed step is '{step}'."),
            "created_at": _iso(inst.created_at),
            "action": "Studio → Module packs",
            "_sort": _sort_key(inst.created_at),
        })

    # Config drafts awaiting publish — one item per (kind, key),
    # the latest draft version.
    draft_rows = session.scalars(
        select(StudioConfig).where(
            StudioConfig.status == ConfigStatus.draft)
        .order_by(StudioConfig.kind, StudioConfig.key,
                  StudioConfig.version)).all()
    latest_draft: dict[tuple[str, str], StudioConfig] = {}
    for row in draft_rows:
        latest_draft[(row.kind, row.key)] = row
    for row in latest_draft.values():
        items.append({
            "source": "config-draft",
            "id": row.id,
            "title": f"Config draft {row.kind}/{row.key} v{row.version} "
                     "awaiting publish",
            "detail": (f"Draft v{row.version} of {row.kind}/{row.key} "
                       f"by {row.updated_by or row.created_by or 'unknown'} "
                       "is unpublished; publishing supersedes the current "
                       "published version."),
            "created_at": _iso(row.created_at),
            "action": f"Studio → Config ({row.kind})",
            "_sort": _sort_key(row.created_at),
        })

    # Break-glass openings awaiting their mandatory review
    # (reviewed_by IS NULL — the 100%-review queue in app.privacy).
    for row in session.scalars(select(BreakGlassAccess).where(
            BreakGlassAccess.reviewed_by.is_(None))).all():
        items.append({
            "source": "break-glass-review",
            "id": row.access_id,
            "title": f"Break-glass access to {row.patient_ref} "
                     "awaiting review",
            "detail": (f"Opened by {row.actor} at "
                       f"{_iso(row.opened_at)}: {row.reason} "
                       f"Justification due {_iso(row.justification_due_at)}."),
            "created_at": _iso(row.opened_at or row.created_at),
            "action": "Privacy → Review queue",
            "_sort": _sort_key(row.opened_at or row.created_at),
        })

    # Pipeline / device DQ quarantine awaiting disposition
    # (DataQuarantine rows still held — app.pipelines / app.devices
    # share this one workqueue).
    for row in session.scalars(select(DataQuarantine).where(
            DataQuarantine.status == "held")).all():
        items.append({
            "source": "pipeline-quarantine",
            "id": row.id,
            "title": f"Quarantined data ({row.source}) awaiting disposition",
            "detail": row.reason,
            "created_at": _iso(row.created_at),
            "action": "Data platform → Quarantine",
            "_sort": _sort_key(row.created_at),
        })

    # MPI dedup candidates awaiting human review (never auto-merged).
    for row in session.scalars(select(DedupCandidate).where(
            DedupCandidate.status == "pending")).all():
        items.append({
            "source": "dedup-candidate",
            "id": row.id,
            "title": (f"Possible duplicate patients {row.patient_a} / "
                      f"{row.patient_b} awaiting review"),
            "detail": (f"MPI dedup candidate with score {row.score}; "
                       "a human records the disposition — the system "
                       "never merges on its own."),
            "created_at": _iso(row.created_at),
            "action": "Data platform → Dedup candidates",
            "_sort": _sort_key(row.created_at),
        })

    items.sort(key=lambda item: item["_sort"], reverse=True)
    for item in items:
        del item["_sort"]
    return items


@router.get("/inbox")
def inbox(session: Session = Depends(get_db),
          user: User = Depends(require_capability("studio.read"))):
    """Every real pending human-action item, newest first (cap 200)."""
    all_items = _inbox_items(session)
    by_source = dict(Counter(item["source"] for item in all_items))
    items = all_items[:INBOX_CAP]
    return {"items": items, "total": len(all_items),
            "by_source": by_source}


# ---------------------------------------------------------------------------
# 2. Organisation read model
# ---------------------------------------------------------------------------

def _counts(values) -> dict:
    return dict(sorted(Counter(
        value if value else "unassigned" for value in values).items()))


@router.get("/organisation")
def organisation(session: Session = Depends(get_db),
                 user: User = Depends(require_capability("studio.read"))):
    """The organisation as governed data actually shapes it.

    The four-level hierarchy (region -> facility/location ->
    department -> unit -> bed, Organisation & Registration Placement
    pass) comes from the published terminology/locations concepts
    (retired concepts, active=false, are excluded — the same rule
    the bed board applies). Staff and account summaries are counted
    from their real tables. Anything not honestly queryable is null
    with a ``notes`` entry saying why. Edits happen in the owning
    surfaces, never here."""
    notes: list[str] = []
    facilities: list[dict] = []
    regions: list[dict] = []
    total_units = 0
    total_beds = 0
    total_regions = 0
    total_departments = 0

    total_facilities = 0
    cfg = published_config(session, LOCATIONS_KIND, LOCATIONS_KEY)
    if cfg is None:
        notes.append(
            "No published terminology/locations config exists, so the "
            "facility/unit/bed structure cannot be read; publish one "
            "in Studio (Units & Beds manager) first.")
    else:
        concepts = [c for c in (cfg.payload or {}).get("concepts") or []
                    if isinstance(c, dict)
                    and c.get("active") is not False and c.get("code")]
        region_rows = [c for c in concepts if c.get("type") == "region"]
        facility_rows = [c for c in concepts if c.get("type") == "facility"]
        department_rows = [c for c in concepts
                           if c.get("type") == "department"]
        unit_rows = [c for c in concepts if c.get("type") == "unit"]
        bed_rows = [c for c in concepts if c.get("type") == "bed"]
        total_regions = len(region_rows)
        total_facilities = len(facility_rows)
        total_departments = len(department_rows)
        total_units = len(unit_rows)
        total_beds = len(bed_rows)

        units_by_code: dict[str, dict] = {}
        for concept in unit_rows:
            units_by_code[concept["code"]] = {
                "code": concept["code"],
                "display": concept.get("display") or concept["code"],
                "parent": concept.get("parent"),
                "beds": [],
                "bed_count": 0,
            }
        for concept in bed_rows:
            bed = {"code": concept["code"],
                   "display": concept.get("display") or concept["code"],
                   "parent": concept.get("parent")}
            for field in ("room", "bed"):
                if concept.get(field) is not None:
                    bed[field] = concept[field]
            unit = units_by_code.get(concept.get("parent") or "")
            if unit is not None:
                unit["beds"].append(bed)
            else:
                notes.append(
                    f"Bed {concept['code']} names parent "
                    f"'{concept.get('parent')}' which is not a unit in "
                    "the published locations config; it is counted in "
                    "the totals but sits under no unit.")
        for unit in units_by_code.values():
            unit["bed_count"] = len(unit["beds"])

        # Placement pass: units group under DEPARTMENTS; a facility's
        # flat "units" list (kept for existing consumers) is its
        # direct units plus every department's units. A unit still
        # parented directly to a facility (un-migrated config) is a
        # direct unit; a unit parented to neither is an orphan.
        departments_by_code: dict[str, dict] = {}
        for concept in department_rows:
            departments_by_code[concept["code"]] = {
                "code": concept["code"],
                "display": concept.get("display") or concept["code"],
                "parent": concept.get("parent"),
                "units": [],
                "unit_count": 0,
                "bed_count": 0,
            }
        facility_units: dict[str, list[dict]] = {
            c["code"]: [] for c in facility_rows}
        orphan_units: list[dict] = []
        for unit in units_by_code.values():
            if unit["parent"] in departments_by_code:
                departments_by_code[unit["parent"]]["units"].append(unit)
            elif unit["parent"] in facility_units:
                facility_units[unit["parent"]].append(unit)
            else:
                orphan_units.append(unit)
        for dept in departments_by_code.values():
            dept["unit_count"] = len(dept["units"])
            dept["bed_count"] = sum(u["bed_count"] for u in dept["units"])
        facility_departments: dict[str, list[dict]] = {
            c["code"]: [] for c in facility_rows}
        orphan_departments = 0
        for dept in departments_by_code.values():
            if dept["parent"] in facility_departments:
                facility_departments[dept["parent"]].append(dept)
                facility_units[dept["parent"]].extend(dept["units"])
            else:
                orphan_departments += 1
        if orphan_departments:
            notes.append(
                f"{orphan_departments} department(s) name a parent "
                "that is not a facility in the published locations "
                "config; they are counted in the totals but listed "
                "under no facility.")
        for concept in facility_rows:
            facilities.append({
                "code": concept["code"],
                "display": concept.get("display") or concept["code"],
                "parent": concept.get("parent"),
                "departments": facility_departments[concept["code"]],
                "units": facility_units[concept["code"]],
                "unit_count": len(facility_units[concept["code"]]),
                "bed_count": sum(u["bed_count"]
                                 for u in facility_units[concept["code"]]),
            })
        if orphan_units:
            notes.append(
                f"{len(orphan_units)} unit(s) name a parent that is "
                "neither a facility nor a department in the published "
                "locations config; they are counted in the totals but "
                "listed under no facility.")
            facilities.extend(
                {"code": unit["code"], "display": unit["display"],
                 "departments": [], "units": [unit], "unit_count": 1,
                 "bed_count": unit["bed_count"]} for unit in orphan_units)
        region_facilities: dict[str, list[str]] = {
            c["code"]: [] for c in region_rows}
        for concept in facility_rows:
            if concept.get("parent") in region_facilities:
                region_facilities[concept["parent"]].append(
                    concept["code"])
        for concept in region_rows:
            regions.append({
                "code": concept["code"],
                "display": concept.get("display") or concept["code"],
                "facilities": region_facilities[concept["code"]],
                "facility_count": len(region_facilities[concept["code"]]),
            })

    staff_rows = session.scalars(select(StaffMember)).all()
    staff = {
        "total": len(staff_rows),
        "by_role": _counts(r.job_role for r in staff_rows),
        # Placement pass: the directory now carries the governed
        # four-level placement, so staff roll up by every level.
        "by_region": _counts(r.region for r in staff_rows),
        "by_location": _counts(r.location for r in staff_rows),
        "by_department": _counts(r.department for r in staff_rows),
        "by_unit": _counts(r.unit for r in staff_rows),
        "by_service_line": _counts(r.service_line for r in staff_rows),
        "by_status": _counts(r.status for r in staff_rows),
    }

    account_rows = session.scalars(select(UserAccount)).all()
    accounts = {
        "total": len(account_rows),
        "by_status": _counts(r.status for r in account_rows),
        "by_role": _counts(r.role.value if r.role is not None else None
                            for r in account_rows),
    }

    return {
        "regions": regions,
        "facilities": facilities,
        "totals": {"regions": total_regions,
                   "facilities": total_facilities,
                   "departments": total_departments,
                   "units": total_units, "beds": total_beds},
        "staff": staff,
        "accounts": accounts,
        "notes": notes,
        "edit_surfaces": ["Units & Beds manager", "People directory",
                          "Accounts"],
        "edit_note": ("This is a read model. Edits happen in the "
                      "owning governed surfaces listed in "
                      "edit_surfaces — never here."),
    }


# ---------------------------------------------------------------------------
# 3. Data-views registry
# ---------------------------------------------------------------------------

def _dataset_columns(session: Session, key: str, builder) -> list[str]:
    """Columns for one export dataset: the report engine's catalogue
    helper when importable (declared keys unioned with live builder
    output), else the keys of the builder's live output rows."""
    try:
        from .report_engine import dataset_fields
        return sorted(dataset_fields(session, key))
    except Exception:
        columns: set[str] = set()
        for row in builder(session):
            columns.update(row.keys())
        return sorted(columns)


@router.get("/data-views")
def data_views(session: Session = Depends(get_db),
               user: User = Depends(require_capability("studio.read"))):
    """Registry of governed data sources with live counts. Kinds:
    dataset (app.reports EXPORT_DATASETS), warehouse-table (OMOP
    WarehouseBase metadata), lake-zone (raw entries + lineage links).
    There is NO SQL anywhere in this registry."""
    items: list[dict] = []
    notes: list[str] = []

    for key, (title, description, builder) in EXPORT_DATASETS.items():
        rows = builder(session)
        items.append({
            "name": key,
            "kind": "dataset",
            "description": f"{title} — {description}",
            "columns": _dataset_columns(session, key, builder),
            "row_count": len(rows),
            "source": "app.reports.EXPORT_DATASETS",
        })

    # Warehouse tables: columns always from metadata; row counts via
    # the warehouse session factory, honestly null when the warehouse
    # database is unavailable.
    warehouse_factory = None
    try:
        warehouse_factory = warehouse_session_factory()
    except Exception as exc:  # noqa: BLE001 — reported, never hidden
        notes.append(f"Warehouse database unavailable: "
                     f"{type(exc).__name__}: {exc}")
    for table_name in sorted(WarehouseBase.metadata.tables):
        table = WarehouseBase.metadata.tables[table_name]
        row_count = None
        if warehouse_factory is not None:
            try:
                with warehouse_factory() as wh_session:
                    row_count = wh_session.scalar(
                        select(func.count()).select_from(table))
            except Exception as exc:  # noqa: BLE001
                notes.append(
                    f"Warehouse table {table_name}: row count "
                    f"unavailable ({type(exc).__name__}: {exc})")
        items.append({
            "name": table_name,
            "kind": "warehouse-table",
            "description": f"OMOP warehouse table {table_name}.",
            "columns": [c.name for c in table.columns],
            "row_count": row_count,
            "source": "app.warehouse.WarehouseBase",
        })

    raw_count = session.scalar(
        select(func.count()).select_from(LakeRawEntry)) or 0
    items.append({
        "name": "lake-raw",
        "kind": "lake-zone",
        "description": ("Data lake raw zone: inbound payloads as "
                        "received (immutable LakeRawEntry rows)."),
        "columns": [c.name for c in LakeRawEntry.__table__.columns],
        "row_count": int(raw_count),
        "source": "app.lake (LakeRawEntry)",
    })
    link_count = session.scalar(
        select(func.count()).select_from(LakeLineageLink)) or 0
    items.append({
        "name": "lake-curated",
        "kind": "lake-zone",
        "description": ("Data lake curated zone, counted by its "
                        "raw → curated lineage links "
                        "(LakeLineageLink rows)."),
        "columns": [c.name for c in LakeLineageLink.__table__.columns],
        "row_count": int(link_count),
        "source": "app.lake (LakeLineageLink)",
    })

    return {"items": items, "total": len(items), "notes": notes}


# ---------------------------------------------------------------------------
# 4. Declaration validators (kinds "endpoint" and "tool")
#
# Configstore-compatible: one payload argument, HTTPException 422 on
# any problem. The coordinator registers these for the config kinds
# "endpoint" and "tool". They validate DECLARATIONS only — see the
# module docstring: nothing here creates a route or a runnable tool.
# ---------------------------------------------------------------------------

ENDPOINT_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
ENDPOINT_BACKINGS = ("config", "pack", "external")
TOOL_KINDS = ("rest", "webhook", "mcp", "pipeline", "rule")

ENDPOINT_BORN_DISABLED = (
    "endpoint declarations are born disabled; enablement is a "
    "governance marker and does not create a live route")
TOOL_BORN_DISABLED = (
    "tool declarations are born disabled; enablement is a governance "
    "marker and does not make a tool runnable")


def _fail(detail: str):
    raise HTTPException(status_code=422, detail=detail)


def _require_title(payload: dict) -> None:
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        _fail("'title' is required and must be a non-empty string")


def _check_description(payload: dict) -> None:
    if "description" in payload and not isinstance(payload["description"], str):
        _fail("'description' must be a string")


def _check_born_disabled(payload: dict, message: str) -> None:
    if "enabled" in payload and payload["enabled"] is not False:
        _fail(message)


def validate_endpoint_declaration(payload) -> None:
    """Validate one endpoint DECLARATION (config kind ``endpoint``).

    Required: title, method, path, capability, backing. The
    capability must be an EXISTING capability (a key of
    CAPABILITY_DEFAULTS in app.configstore — the registry
    app.security.require_capability enforces); declaring a gate on a
    capability that does not exist would govern nothing. The
    declaration is born disabled: ``enabled`` must be absent or
    False."""
    if not isinstance(payload, dict):
        _fail("Endpoint declaration must be a JSON object")
    _require_title(payload)
    method = payload.get("method")
    if method not in ENDPOINT_METHODS:
        _fail(f"'method' must be one of {list(ENDPOINT_METHODS)}")
    path = payload.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        _fail("'path' is required and must be a string starting with '/'")
    capability = payload.get("capability")
    if not isinstance(capability, str) or not capability:
        _fail("'capability' is required and must be a string")
    if capability not in CAPABILITY_DEFAULTS:
        _fail(f"Unknown capability '{capability}': endpoint declarations "
              f"must name an existing capability "
              f"({sorted(CAPABILITY_DEFAULTS)})")
    backing = payload.get("backing")
    if backing not in ENDPOINT_BACKINGS:
        _fail(f"'backing' must be one of {list(ENDPOINT_BACKINGS)}")
    _check_description(payload)
    _check_born_disabled(payload, ENDPOINT_BORN_DISABLED)


def validate_tool_declaration(payload) -> None:
    """Validate one tool DECLARATION (config kind ``tool``).

    Required: title, kind ∈ rest/webhook/mcp/pipeline/rule. The
    executable kinds of the reference product (javascript, python,
    sql) do NOT exist here — Rule 1: no arbitrary code execution in
    the clinical runtime. Optional: description, configRef (a
    governed config key reference), inputSchema (an object). Born
    disabled, exactly like endpoint declarations."""
    if not isinstance(payload, dict):
        _fail("Tool declaration must be a JSON object")
    _require_title(payload)
    kind = payload.get("kind")
    if kind not in TOOL_KINDS:
        _fail(f"'kind' must be one of {list(TOOL_KINDS)} "
              "(executable kinds such as javascript/python/sql do not "
              "exist in the clinical runtime)")
    _check_description(payload)
    if "configRef" in payload and not isinstance(payload["configRef"], str):
        _fail("'configRef' must be a string (a governed config key "
              "reference)")
    if "inputSchema" in payload and not isinstance(payload["inputSchema"], dict):
        _fail("'inputSchema' must be an object")
    _check_born_disabled(payload, TOOL_BORN_DISABLED)
