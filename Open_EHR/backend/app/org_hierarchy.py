"""Organisation hierarchy & registration placement — Organisation &
Registration Placement pass (app.org_hierarchy).

Min's directive (2026-10-19): "registration user with department,
region, unit, location" — every registered user/employee carries an
organisational placement at FOUR governed levels:

    REGION -> LOCATION (facility/site) -> DEPARTMENT -> UNIT

The hierarchy itself is governed configuration data: it lives in the
published `terminology/locations` config as concepts, extended by
this pass with two new concept types alongside the existing ones:

    type "region"      (no parent)
    type "facility"    (parent = region code)      <- the LOCATION level
    type "department"  (parent = facility code)
    type "unit"        (parent = department code)
    type "bed"         (parent = unit code; unchanged from Phase 6)

This module is the ONE resolver every placement read/write goes
through — no parallel validation anywhere else in the system:

- ``load_hierarchy`` / ``hierarchy_tree`` — the read model (the tree
  feeds cascading selects and the future Home pass).
- ``validate_placement`` — the chain validator. Every error is a 422
  NAMING the problem: the missing level, the unknown code, or the
  broken link.
- ``placement_for_unit`` / ``placement_view`` — derivation + display.
- ``resolve_scope`` — turns any placement level into the set of unit
  codes under it, for filters and rollups.
- ``extend_locations_hierarchy`` — the governed migration for a
  locations config published before this pass (the
  inpatient._extend_note_types pattern: a new published version,
  system actor, audited; existing concepts preserved).
- ``ensure_org_columns`` / ``ensure_org_placement`` — startup hooks
  wired in app.main: additive columns for pre-existing databases
  (this codebase has no migration framework; create_all never alters
  a table), then hierarchy extension + placement backfill.

The read endpoint is gated on `workforce.read` (admin / configurator
/ clinician / viewer in the default matrix): the hierarchy is
operational reference data every signed-in role legitimately reads
(the onboarding form, workforce screens and dashboards all consume
it), while every WRITE still goes through the governed locations
draft -> publish lifecycle in Studio / the Units & Beds manager.
"""
import copy

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from .audit import append_audit
from .configstore import list_config_versions, published_config
from .models import ConfigStatus, StaffMember, StudioConfig, User, UserAccount
from .security import get_db, require_capability

router = APIRouter(prefix="/org-hierarchy", tags=["org-hierarchy"])

LOCATIONS_KIND = "terminology"
LOCATIONS_KEY = "locations"

# Placement level names <-> locations concept types. The LOCATION
# level is the pre-existing "facility" concept type: renaming it
# would break every Phase 6+ consumer, so the level is named
# "location" in placement dicts and "facility" in concepts.
LEVELS = ("region", "location", "department", "unit")
TYPE_OF_LEVEL = {"region": "region", "location": "facility",
                 "department": "department", "unit": "unit"}
LEVEL_OF_TYPE = {v: k for k, v in TYPE_OF_LEVEL.items()}
# The level directly above each level (chain links).
PARENT_LEVEL = {"location": "region", "department": "location",
                "unit": "department"}

# The demo-org migration mapping (seed data evolution, recorded in
# docs/sqa/ORG_PLACEMENT_SQA_REPORT.md): departments created under
# the demo facility, and the department each pre-existing demo unit
# belongs to. Unknown units fall back to a "General" department
# under their own facility.
DEMO_REGION = {"code": "region-main", "display": "OpenCare Region"}
DEMO_DEPARTMENTS = [
    ("dept-medicine", "Medicine"),
    ("dept-critical-care", "Critical Care"),
    ("dept-emergency", "Emergency Medicine"),
    ("dept-ambulatory", "Ambulatory Care"),
]
DEMO_UNIT_DEPARTMENT = {
    "unit-medsurg": "dept-medicine",
    "unit-icu": "dept-critical-care",
    "ed": "dept-emergency",
    "clinic": "dept-ambulatory",
}

PLACEMENT_COLUMNS = ("region", "location", "department")
PLACEMENT_TABLES = ("staff_members", "user_accounts")


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------

def load_hierarchy(session: Session) -> dict:
    """The published locations hierarchy as lookup maps.

    Returns {"by_code": {code: {"code", "display", "level", "type",
    "parent"}}, "children": {parent_code: [child codes in payload
    order]}, "version": int|None}. Retired concepts (active=false)
    are excluded — the same rule the bed board applies. 503 when no
    locations config is published (mirrors app.inpatient)."""
    cfg = published_config(session, LOCATIONS_KIND, LOCATIONS_KEY)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=("No published locations (terminology/locations); "
                    "publish one in Studio before using organisational "
                    "placement"))
    by_code: dict[str, dict] = {}
    children: dict[str, list[str]] = {}
    for concept in (cfg.payload or {}).get("concepts") or []:
        if not isinstance(concept, dict) or not concept.get("code"):
            continue
        if concept.get("active") is False:
            continue
        ctype = concept.get("type") or ""
        level = LEVEL_OF_TYPE.get(ctype)
        if level is None:
            # Beds, rooms and any site-specific concept types are not
            # placement levels; beds are counted separately below.
            continue
        entry = {"code": concept["code"],
                 "display": concept.get("display") or concept["code"],
                 "level": level, "type": ctype,
                 "parent": concept.get("parent")}
        by_code[entry["code"]] = entry
        if entry["parent"]:
            children.setdefault(entry["parent"], []).append(entry["code"])
    # Bed counts ride along as a side map: unit code -> active beds.
    bed_counts: dict[str, int] = {}
    for concept in (cfg.payload or {}).get("concepts") or []:
        if isinstance(concept, dict) and concept.get("type") == "bed" \
                and concept.get("active") is not False \
                and concept.get("parent"):
            bed_counts[concept["parent"]] = \
                bed_counts.get(concept["parent"], 0) + 1
    return {"by_code": by_code, "children": children,
            "bed_counts": bed_counts,
            "version": cfg.version if cfg else None}


def hierarchy_tree(session: Session) -> dict:
    """The nested four-level tree: regions -> locations (facilities)
    -> departments -> units (with bed counts). Concepts whose chain
    cannot be resolved upward (a unit still parented to a facility in
    an un-migrated config, an orphan department) are NOT dropped:
    they are listed under "orphans" by level so the gap is visible."""
    h = load_hierarchy(session)
    by_code, children, bed_counts = h["by_code"], h["children"], \
        h["bed_counts"]

    def unit_node(code: str) -> dict:
        return {"code": code, "display": by_code[code]["display"],
                "bed_count": bed_counts.get(code, 0)}

    placed: set[str] = set()
    regions: list[dict] = []
    for code, entry in by_code.items():
        if entry["level"] != "region":
            continue
        region_node = {"code": code, "display": entry["display"],
                       "locations": []}
        for loc_code in children.get(code, []):
            if by_code[loc_code]["level"] != "location":
                continue
            placed.add(loc_code)
            loc_node = {"code": loc_code,
                        "display": by_code[loc_code]["display"],
                        "departments": [], "units": []}
            for dept_code in children.get(loc_code, []):
                if by_code[dept_code]["level"] != "department":
                    continue
                placed.add(dept_code)
                dept_node = {"code": dept_code,
                             "display": by_code[dept_code]["display"],
                             "units": []}
                for unit_code in children.get(dept_code, []):
                    if by_code[unit_code]["level"] != "unit":
                        continue
                    placed.add(unit_code)
                    dept_node["units"].append(unit_node(unit_code))
                loc_node["departments"].append(dept_node)
            # Legacy shape tolerated in the read model: units parented
            # directly to the facility ride on the location node.
            for unit_code in children.get(loc_code, []):
                if by_code[unit_code]["level"] == "unit":
                    placed.add(unit_code)
                    loc_node["units"].append(unit_node(unit_code))
            region_node["locations"].append(loc_node)
        placed.add(code)
        regions.append(region_node)
    orphans = {
        level: [{"code": e["code"], "display": e["display"],
                 "parent": e["parent"]}
                for e in by_code.values()
                if e["level"] == level and e["code"] not in placed]
        for level in ("location", "department", "unit")
    }
    return {"version": h["version"], "regions": regions,
            "orphans": orphans}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _ancestors(by_code: dict, code: str) -> list[dict]:
    """The chain above a concept, nearest first, cycle-safe."""
    chain: list[dict] = []
    seen = {code}
    current = by_code.get(code)
    while current is not None and current.get("parent"):
        parent = by_code.get(current["parent"])
        if parent is None or parent["code"] in seen:
            break
        chain.append(parent)
        seen.add(parent["code"])
        current = parent
    return chain


def validate_placement(session: Session, placement: dict, *,
                       require_all: bool = True) -> dict:
    """Validate a placement chain against the published hierarchy.

    `placement` maps level names (region / location / department /
    unit) to concept codes. With require_all=True every level must
    be present; with False, only the provided levels are checked.
    Every provided code must exist at its level, and every provided
    pair of levels (adjacent or not) must chain: the higher level
    must be the corresponding ancestor of the lower one. Failures are
    422s naming the missing level, the unknown code, or the broken
    link (with the actual ancestor named when there is one).

    Returns the normalised placement {level: code|None}."""
    h = load_hierarchy(session)
    by_code = h["by_code"]
    provided = {level: placement.get(level) for level in LEVELS}
    missing = [level for level in LEVELS if not provided[level]]
    if require_all and missing:
        raise HTTPException(
            status_code=422,
            detail=("Placement is incomplete: missing "
                    + ", ".join(missing)
                    + " (a placement needs region, location, "
                      "department and unit)"))
    for level in LEVELS:
        code = provided[level]
        if code is None:
            continue
        entry = by_code.get(code)
        if entry is None or entry["level"] != level:
            raise HTTPException(
                status_code=422,
                detail=(f"Unknown {level} '{code}': not a {level} in "
                        "the published locations hierarchy"))
    # Chain consistency for every provided pair (lower, higher).
    for idx, lower_level in enumerate(LEVELS):
        lower_code = provided[lower_level]
        if lower_code is None:
            continue
        ancestors = _ancestors(by_code, lower_code)
        ancestor_by_level = {a["level"]: a for a in ancestors}
        for higher_level in LEVELS[:idx]:
            higher_code = provided[higher_level]
            if higher_code is None:
                continue
            actual = ancestor_by_level.get(higher_level)
            if actual is None:
                raise HTTPException(
                    status_code=422,
                    detail=(f"Placement chain broken: {lower_level} "
                            f"'{lower_code}' has no {higher_level} in "
                            "the published locations hierarchy"))
            if actual["code"] != higher_code:
                raise HTTPException(
                    status_code=422,
                    detail=(f"Placement chain broken: {lower_level} "
                            f"'{lower_code}' does not belong to "
                            f"{higher_level} '{higher_code}' (it "
                            f"belongs to {higher_level} "
                            f"'{actual['code']}')"))
    return provided


def placement_for_unit(session: Session, unit_code: str | None) -> dict | None:
    """The full placement chain for a unit code, derived by walking
    parents in the published hierarchy. Unresolvable levels come
    back None (a legacy unit parented directly to a facility yields
    department=None). None when the unit itself is unknown."""
    if not unit_code:
        return None
    h = load_hierarchy(session)
    by_code = h["by_code"]
    entry = by_code.get(unit_code)
    if entry is None or entry["level"] != "unit":
        return None
    placement: dict = {level: None for level in LEVELS}
    placement["unit"] = unit_code
    for ancestor in _ancestors(by_code, unit_code):
        if placement[ancestor["level"]] is None:
            placement[ancestor["level"]] = ancestor["code"]
    return placement


def placement_view(session: Session, placement: dict | None) -> dict:
    """A placement dict of codes -> the display shape
    {level: {"code", "display"} | None} for API responses and UI.
    Tolerates a None placement and unknown codes (display falls
    back to the code)."""
    view: dict = {}
    if not placement:
        return {level: None for level in LEVELS}
    try:
        by_code = load_hierarchy(session)["by_code"]
    except HTTPException:
        by_code = {}
    for level in LEVELS:
        code = placement.get(level)
        if not code:
            view[level] = None
            continue
        entry = by_code.get(code)
        view[level] = {"code": code,
                       "display": entry["display"] if entry else code}
    return view


def resolve_scope(session: Session, region: str | None = None,
                  location: str | None = None,
                  department: str | None = None,
                  unit: str | None = None) -> set[str] | None:
    """Resolve a placement filter to the set of unit codes in scope.

    None when no filter is given (unscoped). Provided ids are
    validated through validate_placement(require_all=False), so an
    unknown id or an inconsistent combination is a 422 naming it.
    A region scope returns the aggregate of its locations'
    departments' units (legacy facility-parented units included)."""
    placement = {"region": region, "location": location,
                 "department": department, "unit": unit}
    if all(value is None for value in placement.values()):
        return None
    validate_placement(session, placement, require_all=False)
    h = load_hierarchy(session)
    by_code, children = h["by_code"], h["children"]
    if unit is not None:
        return {unit}
    if department is not None:
        return {code for code in children.get(department, [])
                if by_code[code]["level"] == "unit"}
    if location is not None:
        units: set[str] = set()
        for child in children.get(location, []):
            if by_code[child]["level"] == "unit":
                units.add(child)
            elif by_code[child]["level"] == "department":
                units.update(
                    code for code in children.get(child, [])
                    if by_code[code]["level"] == "unit")
        return units
    # Region scope: aggregate over its locations.
    units = set()
    for loc_code in children.get(region, []):
        if by_code[loc_code]["level"] != "location":
            continue
        for child in children.get(loc_code, []):
            if by_code[child]["level"] == "unit":
                units.add(child)
            elif by_code[child]["level"] == "department":
                units.update(
                    code for code in children.get(child, [])
                    if by_code[code]["level"] == "unit")
    return units


# ---------------------------------------------------------------------------
# Governed hierarchy migration (for configs published before this pass)
# ---------------------------------------------------------------------------

def _hierarchy_complete(concepts: list[dict]) -> bool:
    by_code = {c.get("code"): c for c in concepts
               if isinstance(c, dict) and c.get("code")}
    if not any(c.get("type") == "region" for c in by_code.values()):
        return False
    for concept in by_code.values():
        ctype = concept.get("type")
        parent = by_code.get(concept.get("parent") or "")
        if ctype == "facility" and (parent is None
                                   or parent.get("type") != "region"):
            return False
        if ctype == "unit" and (parent is None
                                or parent.get("type") != "department"):
            return False
    return True


def extend_locations_hierarchy(session: Session) -> bool:
    """Republish a legacy locations config in the four-level shape.

    The governed evolution pattern (cf. inpatient._extend_note_types):
    the CURRENT published payload is preserved verbatim except for
    the hierarchy completion, and the result is published as a NEW
    version by the system actor with audit rows. Completion means:
    a region exists; every facility is parented to a region; the
    demo departments exist under the demo facility; every unit is
    parented to a department (the recorded demo mapping for known
    units, a per-facility "General" department otherwise). Returns
    True when a new version was published. Idempotent: a complete
    hierarchy is left untouched (returns False)."""
    cfg = published_config(session, LOCATIONS_KIND, LOCATIONS_KEY)
    if cfg is None:
        return False
    concepts = [c for c in (cfg.payload or {}).get("concepts") or []
                if isinstance(c, dict)]
    if _hierarchy_complete(concepts):
        return False
    payload = copy.deepcopy(cfg.payload or {})
    concepts = [c for c in payload.get("concepts") or []
                if isinstance(c, dict)]
    payload["concepts"] = concepts
    by_code = {c["code"]: c for c in concepts if c.get("code")}

    # 1. Region.
    region = next((c for c in concepts if c.get("type") == "region"),
                  None)
    if region is None:
        region = {"code": DEMO_REGION["code"],
                  "display": DEMO_REGION["display"],
                  "type": "region", "active": True}
        concepts.insert(0, region)
        by_code[region["code"]] = region
    # 2. Facilities parented to the region.
    facilities = [c for c in concepts if c.get("type") == "facility"]
    for facility in facilities:
        parent = by_code.get(facility.get("parent") or "")
        if parent is None or parent.get("type") != "region":
            facility["parent"] = region["code"]
    # 3. Demo departments under the demo facility.
    demo_facility = by_code.get("facility-main") or (
        facilities[0] if facilities else None)
    if demo_facility is not None:
        for code, display in DEMO_DEPARTMENTS:
            if code not in by_code:
                dept = {"code": code, "display": display,
                        "type": "department",
                        "parent": demo_facility["code"], "active": True}
                concepts.append(dept)
                by_code[code] = dept

    def department_for(unit: dict) -> str:
        facility_code = unit.get("parent")
        mapped = DEMO_UNIT_DEPARTMENT.get(unit.get("code") or "")
        if mapped and mapped in by_code:
            return mapped
        # Contract mapping: an UNKNOWN unit belongs to a "General"
        # department under its own facility — dept-general, created
        # if absent (a facility-suffixed code only when dept-general
        # is already another facility's department).
        existing = by_code.get("dept-general")
        if existing is not None \
                and existing.get("parent") == facility_code:
            return "dept-general"
        code = ("dept-general" if existing is None
                else f"dept-general-{facility_code}")
        if code not in by_code:
            general = {"code": code, "display": "General",
                       "type": "department", "parent": facility_code,
                       "active": True}
            concepts.append(general)
            by_code[code] = general
        return code

    # 4. Units parented to departments.
    for concept in concepts:
        if concept.get("type") != "unit":
            continue
        parent = by_code.get(concept.get("parent") or "")
        if parent is not None and parent.get("type") == "department":
            continue
        concept["parent"] = department_for(concept)

    cfg.status = ConfigStatus.superseded
    session.flush()
    versions = list_config_versions(session, LOCATIONS_KIND,
                                    LOCATIONS_KEY)
    row = StudioConfig(kind=LOCATIONS_KIND, key=LOCATIONS_KEY,
                       version=versions[-1].version + 1,
                       status=ConfigStatus.published, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    for action in ("studio.config.create", "studio.config.publish"):
        append_audit(session, actor="system", action=action,
                     entity=(f"config:{LOCATIONS_KIND}/{LOCATIONS_KEY}"
                             f"@v{row.version}"),
                     payload={"kind": LOCATIONS_KIND,
                              "key": LOCATIONS_KEY,
                              "version": row.version,
                              "org_hierarchy_extension": True})
    return True


# ---------------------------------------------------------------------------
# Startup hooks (wired in app.main)
# ---------------------------------------------------------------------------

def ensure_org_columns(engine) -> None:
    """Additive placement columns for pre-existing databases.

    This codebase creates schema with Base.metadata.create_all,
    which never alters an existing table — without this hook a
    database created before this pass would fail every staff /
    account query. Inspector-guarded ALTER TABLE ADD COLUMN for
    exactly the three placement columns on the two tables; a no-op
    wherever the columns already exist (every fresh database)."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in PLACEMENT_TABLES:
            if table not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table)}
            for column in PLACEMENT_COLUMNS:
                if column not in present:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN {column} "
                        "VARCHAR(120)"))


def _backfill_rows(session: Session, model, counts: dict, key: str) -> None:
    rows = session.scalars(
        select(model).where(model.region.is_(None),
                            model.unit.is_not(None))).all()
    for row in rows:
        chain = placement_for_unit(session, row.unit)
        if chain is None or not chain.get("region"):
            counts[f"{key}_unplaced"] += 1
            continue
        row.region = chain.get("region")
        row.location = chain.get("location")
        row.department = chain.get("department")
        counts[f"{key}_filled"] += 1


def ensure_org_placement(session: Session) -> None:
    """Startup: extend the hierarchy if the published locations
    config predates this pass, then backfill staff/account rows that
    carry a unit but no placement. Writes ONE audit row when (and
    only when) something changed, so restarts are silent no-ops.
    Rows whose unit does not resolve are left NULL and counted in
    that audit row when one is written."""
    extended = extend_locations_hierarchy(session)
    counts = {"staff_filled": 0, "accounts_filled": 0,
              "staff_unplaced": 0, "accounts_unplaced": 0}
    try:
        _backfill_rows(session, StaffMember, counts, "staff")
        _backfill_rows(session, UserAccount, counts, "accounts")
    except Exception:
        # A hierarchy that cannot be read (e.g. locations config not
        # yet seeded in an unusual boot order) must not take the
        # application down at startup; placement validation will
        # surface the 503 at request time instead.
        session.rollback()
        if extended:
            session.commit()
        return
    if extended or counts["staff_filled"] or counts["accounts_filled"]:
        session.flush()
        append_audit(session, actor="system",
                     action="org.placement.backfill",
                     entity="org-hierarchy",
                     payload={"hierarchy_extended": extended, **counts})
    session.commit()


# ---------------------------------------------------------------------------
# Read endpoint
# ---------------------------------------------------------------------------

@router.get("")
def org_hierarchy(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("workforce.read"))):
    """The published four-level org hierarchy tree (regions ->
    locations -> departments -> units with bed counts) plus any
    orphan concepts whose chain does not resolve. Feeds cascading
    placement selects and the Home dashboard."""
    return hierarchy_tree(session)
