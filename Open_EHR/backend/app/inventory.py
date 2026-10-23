"""Inventory & Materials — item master, lot-level stock, append-only
movement ledger, PAR levels, requisitions, cycle counts, case draw,
implant traceability and scan/RFID identification.

Governed configuration (never code):

- The item taxonomy (categories/subcategories), the adjustment
  reason codes and the expiry warning window live in the PUBLISHED
  Studio config kind "inventory", key "taxonomy"
  (app.configstore.ensure_inventory_defaults seeds it; this module
  only reads it via published_config). Item create/update validates
  the category/subcategory pair against the published payload —
  an unpublished pair is a 422 naming it.
- Stock locations are codes from the governed placement hierarchy:
  the published terminology/locations concepts, resolved through
  app.org_hierarchy.load_hierarchy. Only facility, department and
  unit concepts are stock locations (a region is not); an unknown
  code or a wrong level is a 422 naming the code. There is NO
  parallel location model here.
- Capabilities (app.configstore.CAPABILITY_DEFAULTS): inventory.read
  (admin/configurator/clinician/viewer), inventory.manage and
  inventory.move (admin/configurator/clinician).

Stock discipline (the surgery generate_pick_list / pharmacy_ops
precedents):

- FEFO allocation everywhere stock leaves a location (issue,
  transfer, requisition issue, case draw): earliest expiry first,
  expired lots NEVER allocate.
- Validate-all-then-apply: an oversell / shortfall is refused 409
  BEFORE anything is written — stock can never go negative and a
  refused movement changes nothing.
- Every stock change writes an append-only InventoryMovement row
  (the DSCSA ledger pattern, cf. models.DscsaLedgerEntry) with the
  signed delta, the lot's balance after, and the effective cost /
  signed value when the item is costed.
- Case draw: a surgeon's PreferenceCard (app.surgery_depth) is
  drawn line by line; EVERY card line's code must be an inventory
  item sku and fully coverable, or the whole draw is refused 409
  naming the problem lines — nothing is written.

Identification: POST /scan resolves, in order, a registered barcode
(app.barcodes BarcodeRecord bound to "InventoryItem/<sku>" or
"InventoryLot/<lot row id>"), an RFID tag (app.rfid RfidTag bound
to such an entity), an exact sku, an exact lot_number / serial, and
the pipe payload "SKU|LOT" / "SKU|LOT|EXPIRY". Every scan appends
an InventoryScanEvent. POST /rfid-events resolves an EPC through
the RFID registry the same way.

Synthetic data only.
"""
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (Boolean, DateTime, Integer, JSON, String, Text,
                        UniqueConstraint, inspect, select, text)
from sqlalchemy.orm import Mapped, Session, mapped_column

from .audit import append_audit
from .barcodes import BarcodeRecord
from .configstore import (INVENTORY_TAXONOMY_KEY,
                            default_inventory_taxonomy_payload,
                            published_config)
from .db import Base
from .models import Device, User, utcnow
from .org_hierarchy import load_hierarchy
from .rfid import RfidTag
from .security import get_db, require_capability
from .surgery_depth import PreferenceCard

router = APIRouter(prefix="/inventory", tags=["inventory"])

MOVEMENT_EVENTS = ("receive", "issue", "transfer", "adjust",
                   "count_adjust", "case_issue", "return", "kit_open",
                   "kit_assemble", "kit_disassemble", "stage_transition",
                   "container_open", "container_consume")
REQUISITION_STATUSES = ("requested", "partially_issued", "issued",
                        "cancelled")
COUNT_STATUSES = ("open", "completed", "cancelled")
# Placement levels that may hold stock (org_hierarchy levels:
# facility concepts report level "location").
STOCK_LOCATION_LEVELS = ("location", "department", "unit")


# ---------------------------------------------------------------------------
# This module's own tables (shared Base; surgery_depth precedent)
# ---------------------------------------------------------------------------

class InventoryItem(Base):
    """One item-master record: what the thing is, how it is tracked
    (lot / serial / expiry), its default unit cost and its optional
    links out — formulary_code into the pharmacy formulary
    (terminology/formulary) and asset_key into the device registry
    (models.Device)."""
    __tablename__ = "inventory_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(120), unique=True,
                                     nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False,
                                          index=True)
    subcategory: Mapped[str] = mapped_column(String(60), nullable=False)
    uom: Mapped[str] = mapped_column(String(40), nullable=False,
                                     default="each")
    structure: Mapped[str] = mapped_column(String(10), nullable=False,
                                           default="simple")
    base_uom: Mapped[str | None] = mapped_column(String(40), nullable=True)
    uom_ladder: Mapped[list] = mapped_column(JSON, nullable=False,
                                             default=list)
    kit_components: Mapped[list] = mapped_column(JSON, nullable=False,
                                                 default=list)
    kit_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    hazard_class: Mapped[str | None] = mapped_column(String(60),
                                                     nullable=True)
    sds_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lot_tracked: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                              default=False)
    serial_tracked: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                 default=False)
    expiry_tracked: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                 default=False)
    unit_cost_cents: Mapped[int | None] = mapped_column(Integer,
                                                        nullable=True)
    formulary_code: Mapped[str | None] = mapped_column(String(60),
                                                       nullable=True)
    asset_key: Mapped[str | None] = mapped_column(String(120),
                                                  nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                         default=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                default=utcnow,
                                                onupdate=utcnow)


class InventoryStockLot(Base):
    """One lot row: the quantity of one sku held at one placement
    location under one lot identity (lot_number / serial / expiry).
    unit_cost_cents is the LOT cost override; the effective cost is
    the override when set, else the item default."""
    __tablename__ = "inventory_stock_lots"
    __table_args__ = (UniqueConstraint("sku", "location_code",
                                       "lot_number", "serial"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    location_code: Mapped[str] = mapped_column(String(120), nullable=False,
                                               index=True)
    lot_number: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    serial: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expiry_date: Mapped[str | None] = mapped_column(String(10),
                                                    nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False,
                                          default=0)
    unit_cost_cents: Mapped[int | None] = mapped_column(Integer,
                                                        nullable=True)
    stage: Mapped[str] = mapped_column(String(20), nullable=False,
                                       default="released")
    verified_by: Mapped[str | None] = mapped_column(String(120),
                                                    nullable=True)
    verification_method: Mapped[str | None] = mapped_column(
        String(200), nullable=True)
    verification_result: Mapped[str | None] = mapped_column(
        String(20), nullable=True)
    verified_on: Mapped[str | None] = mapped_column(String(10),
                                                    nullable=True)
    verification_note: Mapped[str | None] = mapped_column(Text,
                                                          nullable=True)
    composition: Mapped[list] = mapped_column(JSON, nullable=False,
                                              default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


class InventoryMovement(Base):
    """Append-only stock ledger (DSCSA pattern): one row per lot
    movement, quantity is the SIGNED delta at location_code and
    balance_after is that lot's balance after the movement.
    value_cents is the signed quantity x the effective cost applied,
    or None when the item/lot is uncosted."""
    __tablename__ = "inventory_movements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(20), nullable=False,
                                       index=True)
    sku: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    location_code: Mapped[str] = mapped_column(String(120), nullable=False,
                                               index=True)
    to_location_code: Mapped[str | None] = mapped_column(String(120),
                                                         nullable=True)
    lot_number: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    serial: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expiry_date: Mapped[str | None] = mapped_column(String(10),
                                                    nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False,
                                          default=0)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False,
                                               default=0)
    unit_cost_cents: Mapped[int | None] = mapped_column(Integer,
                                                        nullable=True)
    value_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reference: Mapped[str] = mapped_column(String(200), nullable=False,
                                           default="")
    reason_code: Mapped[str | None] = mapped_column(String(60),
                                                    nullable=True)
    patient_ref: Mapped[str | None] = mapped_column(String(200),
                                                    nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False,
                                       default="")
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    unit_level: Mapped[str | None] = mapped_column(String(40),
                                                   nullable=True)
    container_code: Mapped[str | None] = mapped_column(String(120),
                                                       nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class InventoryContainer(Base):
    """One physical container (bottle/vial) of a lot at a location:
    the level below the lot for container-tracked classes. Its
    quantity is in the item's base units and the lot's balance is
    the sum of its live containers' quantities."""
    __tablename__ = "inventory_containers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    container_code: Mapped[str] = mapped_column(String(120), unique=True,
                                                nullable=False, index=True)
    sku: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    location_code: Mapped[str] = mapped_column(String(120), nullable=False,
                                               index=True)
    lot_number: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    serial: Mapped[str | None] = mapped_column(String(120), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False,
                                          default=0)
    stage: Mapped[str] = mapped_column(String(20), nullable=False,
                                       default="released")
    opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    opened_by: Mapped[str | None] = mapped_column(String(120),
                                                  nullable=True)
    in_use_expiry_date: Mapped[str | None] = mapped_column(String(10),
                                                          nullable=True)
    disposal_reason_code: Mapped[str | None] = mapped_column(
        String(60), nullable=True)
    disposed_by: Mapped[str | None] = mapped_column(String(120),
                                                    nullable=True)
    disposed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


class InventoryParLevel(Base):
    """One PAR (periodic automatic replenishment) level: the target
    on-hand for one sku at one location. The low-stock worklist is
    every PAR row whose on-hand is below its level."""
    __tablename__ = "inventory_par_levels"
    __table_args__ = (UniqueConstraint("sku", "location_code"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    location_code: Mapped[str] = mapped_column(String(120), nullable=False,
                                               index=True)
    par_level: Mapped[int] = mapped_column(Integer, nullable=False,
                                          default=0)
    par_unit_level: Mapped[str | None] = mapped_column(String(40),
                                                       nullable=True)
    par_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


class InventoryRequisition(Base):
    """One internal requisition: to_location_code asks,
    from_location_code fulfils. Lines carry qty_requested /
    qty_issued; the status is derived from the lines on each issue."""
    __tablename__ = "inventory_requisitions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    req_id: Mapped[str] = mapped_column(String(120), unique=True,
                                        nullable=False, index=True)
    from_location_code: Mapped[str] = mapped_column(String(120),
                                                    nullable=False)
    to_location_code: Mapped[str] = mapped_column(String(120),
                                                  nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="requested", index=True)
    lines: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    reference: Mapped[str] = mapped_column(String(200), nullable=False,
                                           default="")
    requested_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                              default="")
    issued_by: Mapped[str | None] = mapped_column(String(120),
                                                  nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


class InventoryCycleCount(Base):
    """One cycle count at one location: the snapshot lines taken at
    creation (one per lot row, expected = on-hand then) and, on
    completion, the counted quantities + variances. Variances are
    applied as count_adjust movements."""
    __tablename__ = "inventory_cycle_counts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    count_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    location_code: Mapped[str] = mapped_column(String(120), nullable=False,
                                               index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="open", index=True)
    lines: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    reason_code: Mapped[str | None] = mapped_column(String(60),
                                                    nullable=True)
    counted_by: Mapped[str | None] = mapped_column(String(120),
                                                   nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


class InventoryScanEvent(Base):
    """One identification event (barcode scan, pipe payload or RFID
    sighting routed through this module): what was presented, what
    it resolved to (nulls when unresolved), and where/who."""
    __tablename__ = "inventory_scan_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False,
                                      index=True)
    value: Mapped[str] = mapped_column(String(300), nullable=False)
    resolved_sku: Mapped[str | None] = mapped_column(String(120),
                                                     nullable=True)
    resolved_lot_number: Mapped[str | None] = mapped_column(
        String(120), nullable=True)
    context: Mapped[str | None] = mapped_column(String(120), nullable=True)
    location_code: Mapped[str | None] = mapped_column(String(120),
                                                      nullable=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False,
                                       default="")
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Startup ensure: Inventory Depth columns for pre-Depth databases
# ---------------------------------------------------------------------------

#: Additive Inventory Depth columns on the four inventory tables the
#: Depth pass extended, as (table, ((column, ddl), ...)) — the
#: org_hierarchy / pharmacy_ops precedent. create_all never alters
#: an existing table, so a database created before the Depth pass is
#: missing exactly these; the DDL defaults match the model defaults
#: (existing lots were allocatable stock, so their stage backfills
#: to "released", preserving their pre-Depth behaviour).
INVENTORY_DEPTH_ADD_COLUMNS = (
    ("inventory_items", (
        ("structure", "VARCHAR(10) NOT NULL DEFAULT 'simple'"),
        ("base_uom", "VARCHAR(40)"),
        ("uom_ladder", "JSON NOT NULL DEFAULT '[]'"),
        ("kit_components", "JSON NOT NULL DEFAULT '[]'"),
        ("kit_mode", "VARCHAR(20)"),
        ("hazard_class", "VARCHAR(60)"),
        ("sds_ref", "VARCHAR(200)"),
    )),
    ("inventory_stock_lots", (
        ("stage", "VARCHAR(20) NOT NULL DEFAULT 'released'"),
        ("verified_by", "VARCHAR(120)"),
        ("verification_method", "VARCHAR(200)"),
        ("verification_result", "VARCHAR(20)"),
        ("verified_on", "VARCHAR(10)"),
        ("verification_note", "TEXT"),
        ("composition", "JSON NOT NULL DEFAULT '[]'"),
    )),
    ("inventory_movements", (
        ("unit_level", "VARCHAR(40)"),
        ("container_code", "VARCHAR(120)"),
    )),
    ("inventory_par_levels", (
        ("par_unit_level", "VARCHAR(40)"),
        ("par_qty", "INTEGER"),
    )),
)


def ensure_inventory_columns(engine) -> None:
    """Additive Inventory Depth columns for pre-existing databases
    (the org_hierarchy.ensure_org_columns precedent: create_all
    never alters an existing table). Inspector-guarded ALTER TABLE
    ADD COLUMN for exactly the Depth columns on the four extended
    tables; a no-op wherever they already exist (every fresh
    database). Wired from app.main.create_app next to
    pharmacy_ops.ensure_pharmacy_verification_columns."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in INVENTORY_DEPTH_ADD_COLUMNS:
            if table not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table)}
            for column, ddl in columns:
                if column not in present:
                    conn.execute(text(
                        f"ALTER TABLE {table} "
                        f"ADD COLUMN {column} {ddl}"))


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _today() -> date:
    return datetime.now(timezone.utc).date()


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


def _require_str(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"'{field}' is required")
    return value.strip()


def _optional_str(payload: dict, field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be a string")
    return value.strip() or None


def _int_field(payload: dict, field: str, *, required: bool = False,
               minimum: int | None = None) -> int | None:
    value = payload.get(field)
    if value is None:
        if required:
            raise HTTPException(status_code=422,
                                detail=f"'{field}' is required")
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be an integer")
    if minimum is not None and value < minimum:
        raise HTTPException(
            status_code=422,
            detail=f"'{field}' must be >= {minimum} (got {value})")
    return value


def _bool_field(payload: dict, field: str, default: bool) -> bool:
    value = payload.get(field)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be a boolean")
    return value


def _parse_date(value, field: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be an ISO date "
                                   "(YYYY-MM-DD)")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(status_code=422,
                            detail=f"'{field}' is not a valid ISO date: "
                                   f"'{value}'")


def _lot_expired(lot: InventoryStockLot, today: date) -> bool:
    """A lot is expired when its expiry_date is before today. A lot
    with an unparseable expiry is treated as expired (never
    allocates) — the safe direction."""
    if not lot.expiry_date:
        return False
    try:
        return date.fromisoformat(lot.expiry_date) < today
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# Governed configuration: taxonomy + locations
# ---------------------------------------------------------------------------

def _taxonomy_payload(session: Session) -> dict | None:
    cfg = published_config(session, "inventory", INVENTORY_TAXONOMY_KEY)
    if cfg is None:
        return None
    return cfg.payload or {}


def _taxonomy_or_404(session: Session) -> dict:
    payload = _taxonomy_payload(session)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail="No published inventory taxonomy "
                   "(inventory/taxonomy); publish one in Studio")
    return payload


def _validate_category_pair(session: Session, category: str,
                            subcategory: str) -> None:
    """The pair must exist in the PUBLISHED taxonomy — 422 naming
    the pair otherwise (including when nothing is published)."""
    payload = _taxonomy_payload(session)
    categories = {}
    if payload:
        for entry in payload.get("categories") or []:
            if isinstance(entry, dict) and entry.get("code"):
                categories[entry["code"]] = entry
    entry = categories.get(category)
    if entry is None:
        raise HTTPException(
            status_code=422,
            detail=f"Category '{category}' with subcategory "
                   f"'{subcategory}' is not in the published "
                   "inventory taxonomy")
    subs = {s.get("code") for s in entry.get("subcategories") or []
            if isinstance(s, dict)}
    if subcategory not in subs:
        raise HTTPException(
            status_code=422,
            detail=f"Subcategory '{subcategory}' is not a subcategory "
                   f"of category '{category}' in the published "
                   "inventory taxonomy")


def _reason_codes(session: Session) -> set[str]:
    payload = _taxonomy_payload(session) or {}
    return {r.get("code") for r in payload.get("reasonCodes") or []
            if isinstance(r, dict) and r.get("code")}


def _governed_reason_codes(session: Session, field: str) -> set[str]:
    payload = _taxonomy_payload(session) or {}
    return {r.get("code") for r in payload.get(field) or []
            if isinstance(r, dict) and r.get("code")}


_POLICY_DEFAULTS = {"containerTracked": False, "initialStage": "released",
                    "verificationRequired": False,
                    "verificationValidityDays": None,
                    "openStabilityDays": None}


def _class_policy(session: Session, category: str,
                  subcategory: str) -> dict:
    """Per-class policy: published taxonomy -> category entry ->
    subcategory entry, merged per-key (the subcategory wins per
    key). No policy anywhere = the system defaults, under which
    every pre-existing class behaves exactly as before."""
    policy = dict(_POLICY_DEFAULTS)
    payload = _taxonomy_payload(session) or {}
    for entry in payload.get("categories") or []:
        if not isinstance(entry, dict) or entry.get("code") != category:
            continue
        cp = entry.get("classPolicy")
        if isinstance(cp, dict):
            for key in policy:
                if key in cp:
                    policy[key] = cp[key]
        for sub in entry.get("subcategories") or []:
            if isinstance(sub, dict) and sub.get("code") == subcategory:
                scp = sub.get("classPolicy")
                if isinstance(scp, dict):
                    for key in policy:
                        if key in scp:
                            policy[key] = scp[key]
                break
        break
    return policy


def _policy_for_item(session: Session, item: "InventoryItem") -> dict:
    return _class_policy(session, item.category, item.subcategory)


def _verification_status(lot: "InventoryStockLot",
                         policy: dict) -> tuple[bool, str]:
    """(current, why-not) for a lot's verification record against a
    class policy: a CURRENT passing verification is result 'pass'
    with verified_on within verificationValidityDays when set."""
    if not getattr(lot, "verification_result", None):
        return False, "missing verification"
    if lot.verification_result != "pass":
        return False, (f"failed verification "
                       f"(result '{lot.verification_result}')")
    if not lot.verified_on:
        return False, "missing verification date"
    validity = policy.get("verificationValidityDays")
    if validity is not None:
        try:
            verified = date.fromisoformat(lot.verified_on)
        except (TypeError, ValueError):
            return False, "invalid verification date"
        age = (_today() - verified).days
        if age > validity:
            return False, (f"expired verification (verified_on "
                           f"{lot.verified_on}, validity {validity} "
                           "days)")
    return True, "current passing verification"


def _kit_recall_block(session: Session, lot: "InventoryStockLot"):
    """A kit lot is recall-blocked when any composition entry's
    (sku, lot_number) matches ANY lot row (any location) whose
    stage is recalled. Returns (blocked, blocked_by)."""
    blocked_by = []
    for entry in getattr(lot, "composition", None) or []:
        if not isinstance(entry, dict):
            continue
        esku = entry.get("sku")
        elot = entry.get("lot_number")
        if not esku or elot is None:
            continue
        hit = session.scalars(select(InventoryStockLot).where(
            InventoryStockLot.sku == esku,
            InventoryStockLot.lot_number == elot,
            InventoryStockLot.stage == "recalled")).first()
        if hit is not None:
            blocked_by.append({"sku": esku, "lot_number": elot})
    return bool(blocked_by), blocked_by


def _expiry_window_days(session: Session) -> int:
    payload = _taxonomy_payload(session)
    if payload:
        window = payload.get("expiryWarningDays")
        if isinstance(window, int) and not isinstance(window, bool):
            return window
    return default_inventory_taxonomy_payload()["expiryWarningDays"]


def _validate_location(session: Session, code: str) -> None:
    """A stock location must be a facility, department or unit
    concept in the PUBLISHED locations hierarchy (org_hierarchy is
    the one resolver). Unknown code or wrong level -> 422 naming
    the code."""
    hierarchy = load_hierarchy(session)  # 503 when unpublished
    entry = hierarchy["by_code"].get(code)
    if entry is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown location '{code}': not in the published "
                   "locations hierarchy")
    if entry["level"] not in STOCK_LOCATION_LEVELS:
        raise HTTPException(
            status_code=422,
            detail=f"Location '{code}' is a {entry['type']} in the "
                   "published locations hierarchy; stock locations "
                   "must be a facility, department or unit")


def _validate_formulary_code(session: Session, code: str | None) -> None:
    """Validate against the pharmacy formulary source (app.pharmacy:
    the published terminology/formulary concepts). When no formulary
    is published there is no source to validate against, so the
    string is accepted as recorded."""
    if code is None:
        return
    cfg = published_config(session, "terminology", "formulary")
    if cfg is None:
        return
    codes = {c.get("code") for c in (cfg.payload or {}).get("concepts")
             or [] if isinstance(c, dict)}
    if code not in codes:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown formulary_code '{code}': not in the "
                   "published pharmacy formulary "
                   "(terminology/formulary)")


def _validate_asset_key(session: Session, key: str | None) -> None:
    """Validate against the device registry (models.Device, the
    app.devices registry): an unknown device_key is a 422."""
    if key is None:
        return
    row = session.scalars(select(Device).where(
        Device.device_key == key)).first()
    if row is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown asset_key '{key}': not in the device "
                   "registry")


# ---------------------------------------------------------------------------
# Item structure: ladder + kit validation
# ---------------------------------------------------------------------------

def _parse_ladder(raw, base_candidate: str | None) -> list:
    """Validate a uom_ladder payload: a non-empty list ordered
    largest -> base, unique codes, containsNext an int >= 2 for all
    but the last level, and the last level's code == base_uom with
    containsNext 1 (absent -> 1). Returns the normalised ladder."""
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=422,
                            detail="'uom_ladder' must be a non-empty list")
    entries = []
    seen: set[str] = set()
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=422,
                detail="Each 'uom_ladder' level must be an object "
                       "{code, containsNext, barcode?}")
        code = entry.get("code")
        if not isinstance(code, str) or not code.strip():
            raise HTTPException(
                status_code=422,
                detail="Each 'uom_ladder' level needs a non-empty 'code'")
        code = code.strip()
        if code in seen:
            raise HTTPException(
                status_code=422,
                detail=f"'uom_ladder' has duplicate level code '{code}'")
        seen.add(code)
        is_last = idx == len(raw) - 1
        contains = entry.get("containsNext")
        if contains is None:
            if is_last:
                contains = 1
            else:
                raise HTTPException(
                    status_code=422,
                    detail=f"Ladder level '{code}' requires "
                           "'containsNext' (an integer >= 2)")
        if isinstance(contains, bool) or not isinstance(contains, int):
            raise HTTPException(
                status_code=422,
                detail=f"Ladder level '{code}': 'containsNext' must "
                       "be an integer")
        if is_last and contains != 1:
            raise HTTPException(
                status_code=422,
                detail=f"Ladder level '{code}' is the base level: its "
                       f"'containsNext' must be 1 (got {contains})")
        if not is_last and contains < 2:
            raise HTTPException(
                status_code=422,
                detail=f"Ladder level '{code}': 'containsNext' must "
                       f"be >= 2 (got {contains})")
        normalised = {"code": code, "containsNext": contains}
        barcode = entry.get("barcode")
        if barcode is not None:
            if not isinstance(barcode, str):
                raise HTTPException(
                    status_code=422,
                    detail=f"Ladder level '{code}': 'barcode' must be "
                           "a string")
            if barcode.strip():
                normalised["barcode"] = barcode.strip()
        entries.append(normalised)
    if base_candidate is not None \
            and entries[-1]["code"] != base_candidate:
        raise HTTPException(
            status_code=422,
            detail=f"Last ladder level '{entries[-1]['code']}' must "
                   f"equal base_uom '{base_candidate}'")
    return entries


def _ladder_factor(item: InventoryItem, level: str) -> int:
    """The exact integer factor converting one unit of `level` into
    base units (the product of containsNext from that level down).
    An unknown level is a 422 naming it."""
    ladder = getattr(item, "uom_ladder", None) or []
    if ladder:
        codes = [e.get("code") for e in ladder if isinstance(e, dict)]
        if level not in codes:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown unit_level '{level}' for item "
                       f"'{item.sku}': ladder levels are "
                       f"{', '.join(str(c) for c in codes)}")
        idx = codes.index(level)
        factor = 1
        for entry in ladder[idx:]:
            contains = entry.get("containsNext")
            factor *= contains if isinstance(contains, int) \
                and not isinstance(contains, bool) else 1
        return factor
    base = getattr(item, "base_uom", None) or item.uom
    if level == base or level == item.uom:
        return 1
    raise HTTPException(
        status_code=422,
        detail=f"Unknown unit_level '{level}' for item '{item.sku}': "
               f"the item has no uom_ladder (base unit '{base}')")


def _convert_entered(item: InventoryItem, payload: dict,
                     quantity: int):
    """Apply the optional unit_level conversion to an entered
    quantity. Returns (base_quantity, unit_level|None)."""
    level = _optional_str(payload, "unit_level")
    if level is None:
        return quantity, None
    factor = _ladder_factor(item, level)
    return quantity * factor, level


def _resolve_item_structure(session: Session, payload: dict, *,
                            sku: str,
                            current: InventoryItem | None = None) -> dict:
    """Shared create/update validation of structure, the unit
    ladder and the kit definition. Returns the resolved values."""
    def _cur(attr, fallback=None):
        return getattr(current, attr, fallback) if current else fallback

    structure = payload.get("structure", _cur("structure", "simple"))
    if structure is None:
        structure = "simple"
    if structure not in ("simple", "kit"):
        raise HTTPException(
            status_code=422,
            detail=f"'structure' must be 'simple' or 'kit' "
                   f"(got '{structure}')")
    uom_value = payload.get("uom") if isinstance(payload.get("uom"), str) \
        and payload.get("uom").strip() else _cur("uom", "each")
    # ---- ladder + base_uom ----
    if "uom_ladder" in payload:
        raw_ladder = payload.get("uom_ladder")
    else:
        raw_ladder = _cur("uom_ladder", None)
    explicit_base = payload.get("base_uom")
    if explicit_base is not None and (not isinstance(explicit_base, str)
                                      or not explicit_base.strip()):
        raise HTTPException(status_code=422,
                            detail="'base_uom' must be a non-empty string")
    base_candidate = explicit_base.strip() if explicit_base else None
    if raw_ladder or "uom_ladder" in payload:
        if base_candidate is None:
            base_candidate = _cur("base_uom", None)
        ladder = _parse_ladder(raw_ladder, base_candidate)
        base_uom = ladder[-1]["code"]
    else:
        ladder = []
        base_uom = base_candidate or _cur("base_uom", None) or uom_value
    # ---- kit definition ----
    if "kit_components" in payload:
        raw_components = payload.get("kit_components")
    else:
        raw_components = _cur("kit_components", None)
    kit_mode = payload.get("kit_mode", _cur("kit_mode", None))
    if kit_mode is not None and not isinstance(kit_mode, str):
        raise HTTPException(status_code=422,
                            detail="'kit_mode' must be a string")
    components: list = []
    if structure == "kit":
        if kit_mode not in ("track-whole", "break-on-receipt"):
            raise HTTPException(
                status_code=422,
                detail=f"Kit item '{sku}': 'kit_mode' must be "
                       "'track-whole' or 'break-on-receipt' "
                       f"(got '{kit_mode}')")
        if not isinstance(raw_components, list) or not raw_components:
            raise HTTPException(
                status_code=422,
                detail=f"Kit item '{sku}': 'kit_components' must be "
                       "a non-empty list")
        seen_components: set[str] = set()
        for entry in raw_components:
            if not isinstance(entry, dict):
                raise HTTPException(
                    status_code=422,
                    detail=f"Kit item '{sku}': each component must be "
                           "an object {sku, qty}")
            csku = entry.get("sku")
            if not isinstance(csku, str) or not csku.strip():
                raise HTTPException(
                    status_code=422,
                    detail=f"Kit item '{sku}': each component needs "
                           "a 'sku'")
            csku = csku.strip()
            cqty = entry.get("qty")
            if isinstance(cqty, bool) or not isinstance(cqty, int) \
                    or cqty <= 0:
                raise HTTPException(
                    status_code=422,
                    detail=f"Kit component '{csku}': 'qty' must be a "
                           "positive integer")
            if csku == sku:
                raise HTTPException(
                    status_code=422,
                    detail=f"Kit component '{csku}': a kit cannot "
                           "contain itself")
            if csku in seen_components:
                raise HTTPException(
                    status_code=422,
                    detail=f"Kit component '{csku}': duplicate "
                           "component sku")
            seen_components.add(csku)
            comp = session.scalars(select(InventoryItem).where(
                InventoryItem.sku == csku)).first()
            if comp is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"Kit component '{csku}': not an existing "
                           "inventory item")
            if (getattr(comp, "structure", "simple") or "simple") \
                    != "simple":
                raise HTTPException(
                    status_code=422,
                    detail=f"Kit component '{csku}': components must "
                           "be SIMPLE items (no nested kits)")
            if comp.asset_key:
                raise HTTPException(
                    status_code=422,
                    detail=f"Kit component '{csku}': asset-linked "
                           "(instrument) items cannot be kit "
                           "components (asset_key is set)")
            components.append({"sku": csku, "qty": cqty})
    else:
        if raw_components:
            raise HTTPException(
                status_code=422,
                detail=f"Item '{sku}': 'kit_components' is only valid "
                       "for structure 'kit'")
        kit_mode = None
    hazard = payload.get("hazard_class", _cur("hazard_class", None))
    if hazard is not None and not isinstance(hazard, str):
        raise HTTPException(status_code=422,
                            detail="'hazard_class' must be a string")
    sds = payload.get("sds_ref", _cur("sds_ref", None))
    if sds is not None and not isinstance(sds, str):
        raise HTTPException(status_code=422,
                            detail="'sds_ref' must be a string")
    return {"structure": structure, "base_uom": base_uom,
            "uom_ladder": ladder, "kit_components": components,
            "kit_mode": kit_mode,
            "hazard_class": (hazard.strip() or None)
            if isinstance(hazard, str) else hazard,
            "sds_ref": (sds.strip() or None)
            if isinstance(sds, str) else sds}


# ---------------------------------------------------------------------------
# Serialisers + stock core
# ---------------------------------------------------------------------------

def _item_out(row: InventoryItem) -> dict:
    return {
        "sku": row.sku, "name": row.name, "category": row.category,
        "subcategory": row.subcategory, "uom": row.uom,
        "structure": getattr(row, "structure", None) or "simple",
        "base_uom": getattr(row, "base_uom", None) or row.uom,
        "uom_ladder": list(getattr(row, "uom_ladder", None) or []),
        "kit_components": list(getattr(row, "kit_components", None)
                               or []),
        "kit_mode": getattr(row, "kit_mode", None),
        "hazard_class": getattr(row, "hazard_class", None),
        "sds_ref": getattr(row, "sds_ref", None),
        "lot_tracked": bool(row.lot_tracked),
        "serial_tracked": bool(row.serial_tracked),
        "expiry_tracked": bool(row.expiry_tracked),
        "unit_cost_cents": row.unit_cost_cents,
        "formulary_code": row.formulary_code,
        "asset_key": row.asset_key, "active": bool(row.active),
        "created_by": row.created_by,
        "created_at": _aware(row.created_at).isoformat()
        if row.created_at else None,
        "updated_at": _aware(row.updated_at).isoformat()
        if row.updated_at else None,
    }


def _item_or_404(session: Session, sku: str) -> InventoryItem:
    row = session.scalars(select(InventoryItem).where(
        InventoryItem.sku == sku)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Inventory item '{sku}' was not found")
    return row


def _effective_cost(lot: InventoryStockLot,
                    item: InventoryItem | None) -> int | None:
    if lot.unit_cost_cents is not None:
        return lot.unit_cost_cents
    return item.unit_cost_cents if item is not None else None


def _lot_out(session: Session, lot: InventoryStockLot,
             item: InventoryItem | None = None) -> dict:
    if item is None:
        item = session.scalars(select(InventoryItem).where(
            InventoryItem.sku == lot.sku)).first()
    effective = _effective_cost(lot, item)
    is_kit = item is not None \
        and (getattr(item, "structure", "simple") or "simple") == "kit"
    if is_kit:
        blocked, blocked_by = _kit_recall_block(session, lot)
    else:
        blocked, blocked_by = False, []
    verification = {
        "verified_by": getattr(lot, "verified_by", None),
        "method": getattr(lot, "verification_method", None),
        "result": getattr(lot, "verification_result", None),
        "verified_on": getattr(lot, "verified_on", None),
        "note": getattr(lot, "verification_note", None),
    }
    return {
        "id": lot.id, "sku": lot.sku, "location_code": lot.location_code,
        "lot_number": lot.lot_number, "serial": lot.serial,
        "expiry_date": lot.expiry_date, "quantity": lot.quantity,
        "lot_unit_cost_cents": lot.unit_cost_cents,
        "unit_cost_cents": effective,
        "value_cents": (lot.quantity * effective
                        if effective is not None else None),
        "stage": getattr(lot, "stage", None) or "released",
        "verified_by": verification["verified_by"],
        "verification_method": verification["method"],
        "verification_result": verification["result"],
        "verified_on": verification["verified_on"],
        "verification_note": verification["note"],
        "verification": verification,
        "composition": list(getattr(lot, "composition", None) or []),
        "recall_blocked": blocked, "blocked_by": blocked_by,
    }


def _movement_out(row: InventoryMovement) -> dict:
    return {
        "entry_id": row.entry_id, "event": row.event, "sku": row.sku,
        "location_code": row.location_code,
        "to_location_code": row.to_location_code,
        "lot_number": row.lot_number, "serial": row.serial,
        "expiry_date": row.expiry_date, "quantity": row.quantity,
        "balance_after": row.balance_after,
        "unit_cost_cents": row.unit_cost_cents,
        "value_cents": row.value_cents, "reference": row.reference,
        "reason_code": row.reason_code, "patient_ref": row.patient_ref,
        "note": row.note, "actor": row.actor, "detail": row.detail or {},
        "unit_level": getattr(row, "unit_level", None),
        "container_code": getattr(row, "container_code", None),
        "created_at": _aware(row.created_at).isoformat()
        if row.created_at else None,
    }


def _containers_for_lot(session: Session,
                        lot: InventoryStockLot) -> list:
    stmt = select(InventoryContainer).where(
        InventoryContainer.sku == lot.sku,
        InventoryContainer.location_code == lot.location_code,
        InventoryContainer.lot_number == lot.lot_number)
    if lot.serial is None:
        stmt = stmt.where(InventoryContainer.serial.is_(None))
    else:
        stmt = stmt.where(InventoryContainer.serial == lot.serial)
    return session.scalars(stmt.order_by(InventoryContainer.id.asc())).all()


def _container_effective_expiry(container: InventoryContainer,
                                lot: InventoryStockLot | None) -> str | None:
    """Effective expiry: the earlier of the lot's shelf expiry and
    the container's in-use expiry (once opened)."""
    dates = []
    if lot is not None and lot.expiry_date:
        dates.append(lot.expiry_date)
    if container.in_use_expiry_date:
        dates.append(container.in_use_expiry_date)
    return min(dates) if dates else None


def _container_in_use_expiry(lot: InventoryStockLot, policy: dict,
                             opened_date: date | None = None) -> str | None:
    days = policy.get("openStabilityDays")
    if days is None:
        return lot.expiry_date
    opened = opened_date or _today()
    candidate = (opened + timedelta(days=days)).isoformat()
    if lot.expiry_date:
        return min(lot.expiry_date, candidate)
    return candidate


def _drawable_containers(session: Session, lot: InventoryStockLot,
                         today: date) -> list:
    """The lot's containers that may be drawn, in container-FEFO
    order: opened containers first by effective expiry, then
    unopened by shelf (effective) expiry. Expired-effective
    containers never draw."""
    out = []
    for cont in _containers_for_lot(session, lot):
        if cont.stage not in ("released", "in_use"):
            continue
        if cont.quantity <= 0:
            continue
        effective = _container_effective_expiry(cont, lot)
        if effective:
            try:
                if date.fromisoformat(effective) < today:
                    continue
            except ValueError:
                continue
        out.append(cont)
    out.sort(key=lambda c: (
        0 if (c.stage == "in_use" or c.opened_at is not None) else 1,
        _container_effective_expiry(c, lot) or "9999-12-31", c.id))
    return out


def _auto_container_code(session: Session, lot: InventoryStockLot,
                         taken: set | None = None) -> str:
    taken = taken or set()
    base = lot.lot_number or lot.sku
    seq = 1
    while True:
        code = f"{base}-{seq:03d}"
        if code not in taken and session.scalars(
                select(InventoryContainer).where(
                    InventoryContainer.container_code == code)).first() \
                is None:
            return code
        seq += 1


def _make_container(session: Session, *, lot: InventoryStockLot,
                    quantity: int, code: str | None = None,
                    taken: set | None = None) -> InventoryContainer:
    cont = InventoryContainer(
        container_code=code or _auto_container_code(session, lot, taken),
        sku=lot.sku, location_code=lot.location_code,
        lot_number=lot.lot_number, serial=lot.serial, quantity=quantity,
        stage=getattr(lot, "stage", None) or "released")
    session.add(cont)
    session.flush()
    return cont


def _write_movement(session: Session, *, event: str, item: InventoryItem,
                    lot: InventoryStockLot, delta: int, actor: str,
                    to_location_code: str | None = None,
                    reference: str = "", reason_code: str | None = None,
                    patient_ref: str | None = None,
                    note: str | None = None,
                    detail: dict | None = None,
                    unit_level: str | None = None,
                    container_code: str | None = None) -> InventoryMovement:
    """Append one ledger row for a lot whose quantity has ALREADY
    been changed by the caller (balance_after reads the lot's new
    quantity). Cost/value are stamped from the lot's effective cost."""
    effective = _effective_cost(lot, item)
    row = InventoryMovement(
        entry_id=f"mov-{uuid.uuid4()}", event=event, sku=lot.sku,
        location_code=lot.location_code, to_location_code=to_location_code,
        lot_number=lot.lot_number, serial=lot.serial,
        expiry_date=lot.expiry_date, quantity=delta,
        balance_after=lot.quantity, unit_cost_cents=effective,
        value_cents=(delta * effective if effective is not None else None),
        reference=reference or "", reason_code=reason_code,
        patient_ref=patient_ref, note=note, actor=actor,
        detail=detail or {}, unit_level=unit_level,
        container_code=container_code)
    session.add(row)
    session.flush()
    return row


def _find_lot(session: Session, sku: str, location_code: str,
              lot_number: str, serial: str | None) -> InventoryStockLot | None:
    stmt = select(InventoryStockLot).where(
        InventoryStockLot.sku == sku,
        InventoryStockLot.location_code == location_code,
        InventoryStockLot.lot_number == lot_number)
    if serial is None:
        stmt = stmt.where(InventoryStockLot.serial.is_(None))
    else:
        stmt = stmt.where(InventoryStockLot.serial == serial)
    return session.scalars(stmt).first()


def _fefo_plan(session: Session, sku: str, location_code: str,
               quantity: int, lot_number: str | None = None,
               reserved: dict[int, int] | None = None):
    """Plan a FEFO draw of `quantity` units of `sku` at
    `location_code`. Returns (plan, available) where plan is a list
    of (lot, take) in FEFO order (earliest expiry first, no-expiry
    lots last) and available is the total drawable units. Expired
    lots NEVER allocate. `reserved` maps lot id -> units already
    promised to an earlier line of the same validate-all operation.
    Nothing is written here."""
    reserved = reserved or {}
    stmt = select(InventoryStockLot).where(
        InventoryStockLot.sku == sku,
        InventoryStockLot.location_code == location_code,
        InventoryStockLot.quantity > 0)
    if lot_number is not None:
        stmt = stmt.where(InventoryStockLot.lot_number == lot_number)
    lots = session.scalars(stmt).all()
    today = _today()
    item = session.scalars(select(InventoryItem).where(
        InventoryItem.sku == sku)).first()
    policy = _policy_for_item(session, item) if item is not None else None
    container_tracked = bool(policy and policy.get("containerTracked"))
    # Stage discipline: only released / in_use lots allocate, and a
    # kit lot blocked by a recalled component never allocates.
    usable = []
    caps: dict[int, int] = {}
    for lot in lots:
        if _lot_expired(lot, today):
            continue
        if (getattr(lot, "stage", None) or "released") \
                not in ("released", "in_use"):
            continue
        if item is not None and (getattr(item, "structure", "simple")
                                 or "simple") == "kit":
            blocked, _by = _kit_recall_block(session, lot)
            if blocked:
                continue
        cap = max(0, lot.quantity - reserved.get(lot.id, 0))
        if container_tracked:
            containers = _containers_for_lot(session, lot)
            if containers:
                drawable = sum(c.quantity for c in
                               _drawable_containers(session, lot, today))
                cap = min(cap, drawable)
        caps[lot.id] = cap
        usable.append(lot)

    def _fefo_key(lot):
        if container_tracked:
            drawable = _drawable_containers(session, lot, today)
            if drawable:
                effective = _container_effective_expiry(drawable[0], lot)
                return (effective is None, effective or "", lot.id)
        return (lot.expiry_date is None, lot.expiry_date or "", lot.id)

    usable.sort(key=_fefo_key)
    available = sum(caps[lot.id] for lot in usable)
    plan: list[tuple[InventoryStockLot, int]] = []
    remaining = quantity
    for lot in usable:
        if remaining <= 0:
            break
        take = min(caps[lot.id], remaining)
        if take > 0:
            plan.append((lot, take))
            remaining -= take
    return plan, available


def _allocatable_info(session: Session, sku: str,
                      location_code: str) -> dict:
    """How much stock at a location is blocked from allocation, and
    why: stage_blocked_qty (lots whose stage is not released/in_use)
    and recall_blocked_qty (kit lots blocked by a recalled
    component). Expired lots are expiry-blocked, counted in
    neither."""
    stmt = select(InventoryStockLot).where(
        InventoryStockLot.sku == sku,
        InventoryStockLot.location_code == location_code,
        InventoryStockLot.quantity > 0)
    lots = session.scalars(stmt).all()
    today = _today()
    item = session.scalars(select(InventoryItem).where(
        InventoryItem.sku == sku)).first()
    is_kit = item is not None and (getattr(item, "structure", "simple")
                                   or "simple") == "kit"
    stage_blocked = 0
    recall_blocked = 0
    for lot in lots:
        if _lot_expired(lot, today):
            continue
        if (getattr(lot, "stage", None) or "released") \
                not in ("released", "in_use"):
            stage_blocked += lot.quantity
        elif is_kit:
            blocked, _by = _kit_recall_block(session, lot)
            if blocked:
                recall_blocked += lot.quantity
    return {"stage_blocked_qty": stage_blocked,
            "recall_blocked_qty": recall_blocked,
            "stage_blocked": stage_blocked,
            "recall_blocked": recall_blocked}


def _blocked_suffix(session: Session, sku: str,
                    location_code: str) -> str:
    info = _allocatable_info(session, sku, location_code)
    total = info["stage_blocked_qty"] + info["recall_blocked_qty"]
    if total > 0:
        return f" ({total} units blocked by stage/recall)"
    return ""


def _apply_draw(session: Session, *, item: InventoryItem,
                plan: list[tuple[InventoryStockLot, int]], event: str,
                actor: str, reference: str = "",
                patient_ref: str | None = None,
                reason_code: str | None = None, note: str | None = None,
                detail: dict | None = None,
                unit_level: str | None = None):
    """Apply a validated FEFO plan: decrement each lot and write one
    movement per lot drawn. Returns (movements, allocations). For
    container-tracked classes, each drawn lot's containers are
    consumed in container-FEFO order in the same transaction."""
    policy = _policy_for_item(session, item)
    container_tracked = bool(policy.get("containerTracked"))
    movements, allocations = [], []
    for lot, take in plan:
        effective = _effective_cost(lot, item)
        lot.quantity -= take
        session.flush()
        container_details = None
        if container_tracked and _containers_for_lot(session, lot):
            container_details = []
            remaining = take
            for cont in _drawable_containers(session, lot, _today()):
                if remaining <= 0:
                    break
                c_take = min(cont.quantity, remaining)
                if c_take <= 0:
                    continue
                if cont.stage != "in_use":
                    cont.stage = "in_use"
                    cont.opened_at = datetime.now(timezone.utc)
                    cont.opened_by = actor
                    cont.in_use_expiry_date = _container_in_use_expiry(
                        lot, policy)
                    session.flush()
                    _write_movement(
                        session, event="container_open", item=item,
                        lot=lot, delta=0, actor=actor, reference=reference,
                        container_code=cont.container_code,
                        detail={"container_code": cont.container_code,
                                "in_use_expiry_date":
                                cont.in_use_expiry_date})
                cont.quantity -= c_take
                if cont.quantity == 0:
                    cont.stage = "depleted"
                session.flush()
                _write_movement(
                    session, event="container_consume", item=item,
                    lot=lot, delta=-c_take, actor=actor,
                    reference=reference,
                    container_code=cont.container_code,
                    detail={"container_code": cont.container_code})
                container_details.append({
                    "container_code": cont.container_code, "qty": c_take,
                    "effective_expiry": _container_effective_expiry(
                        cont, lot)})
                remaining -= c_take
        movements.append(_write_movement(
            session, event=event, item=item, lot=lot, delta=-take,
            actor=actor, reference=reference, patient_ref=patient_ref,
            reason_code=reason_code, note=note, detail=detail,
            unit_level=unit_level))
        allocation = {
            "lot_number": lot.lot_number, "serial": lot.serial,
            "expiry_date": lot.expiry_date, "qty": take,
            "unit_cost_cents": effective,
            "value_cents": (take * effective
                            if effective is not None else None)}
        if container_details is not None:
            allocation["containers"] = container_details
        allocations.append(allocation)
    return movements, allocations


def _on_hand(session: Session, sku: str, location_code: str) -> int:
    rows = session.scalars(select(InventoryStockLot).where(
        InventoryStockLot.sku == sku,
        InventoryStockLot.location_code == location_code)).all()
    return sum(r.quantity for r in rows)


# ---------------------------------------------------------------------------
# Taxonomy + overview
# ---------------------------------------------------------------------------

@router.get("/taxonomy")
def get_taxonomy(session: Session = Depends(get_db),
                 user: User = Depends(require_capability("inventory.read"))):
    """The published inventory taxonomy payload (categories /
    subcategories, reason codes, expiry warning window)."""
    return _taxonomy_or_404(session)


def _low_stock_rows(session: Session, location_code: str | None = None,
                    category: str | None = None) -> list[dict]:
    stmt = select(InventoryParLevel).order_by(
        InventoryParLevel.sku.asc(), InventoryParLevel.location_code.asc())
    if location_code:
        stmt = stmt.where(InventoryParLevel.location_code == location_code)
    rows = []
    for par in session.scalars(stmt).all():
        item = session.scalars(select(InventoryItem).where(
            InventoryItem.sku == par.sku)).first()
        if item is None:
            continue
        if category and item.category != category:
            continue
        on_hand = _on_hand(session, par.sku, par.location_code)
        if on_hand < par.par_level:
            rows.append({"sku": par.sku, "name": item.name,
                         "location_code": par.location_code,
                         "on_hand": on_hand, "par_level": par.par_level,
                         "deficit": par.par_level - on_hand})
    return rows


def _expiry_rows(session: Session, location_code: str | None = None,
                 category: str | None = None) -> list[dict]:
    window = _expiry_window_days(session)
    today = _today()
    stmt = select(InventoryStockLot).where(InventoryStockLot.quantity > 0,
                                           InventoryStockLot.expiry_date
                                           .is_not(None))
    if location_code:
        stmt = stmt.where(InventoryStockLot.location_code == location_code)
    rows = []
    for lot in session.scalars(stmt).all():
        try:
            expiry = date.fromisoformat(lot.expiry_date)
        except (TypeError, ValueError):
            continue
        days = (expiry - today).days
        expired = days < 0
        if not expired and days > window:
            continue
        item = session.scalars(select(InventoryItem).where(
            InventoryItem.sku == lot.sku)).first()
        if category and (item is None or item.category != category):
            continue
        rows.append({"sku": lot.sku,
                     "name": item.name if item else lot.sku,
                     "location_code": lot.location_code,
                     "lot_number": lot.lot_number,
                     "expiry_date": lot.expiry_date,
                     "quantity": lot.quantity,
                     "days_to_expiry": days, "expired": expired})
    rows.sort(key=lambda r: (not r["expired"], r["expiry_date"],
                             r["sku"], r["lot_number"]))
    return rows


@router.get("/overview")
def overview(session: Session = Depends(get_db),
             user: User = Depends(require_capability("inventory.read"))):
    """Real aggregates over the item master, lot book and ledger —
    nothing here is estimated."""
    items = session.scalars(select(InventoryItem)).all()
    lots = session.scalars(select(InventoryStockLot)).all()
    item_by_sku = {i.sku: i for i in items}
    stock_value = 0
    uncosted = 0
    for lot in lots:
        effective = _effective_cost(lot, item_by_sku.get(lot.sku))
        if effective is None:
            if lot.quantity > 0:
                uncosted += 1
        else:
            stock_value += lot.quantity * effective
    today = _today()
    movements_today = 0
    for movement in session.scalars(select(InventoryMovement)).all():
        created = _aware(movement.created_at)
        if created is not None and created.date() == today:
            movements_today += 1
    expiry = _expiry_rows(session)
    return {
        "active_items": sum(1 for i in items if i.active),
        "stock_locations": len({lot.location_code for lot in lots}),
        "on_hand_units": sum(lot.quantity for lot in lots),
        "low_stock_lines": len(_low_stock_rows(session)),
        "expiring_lines": sum(1 for r in expiry if not r["expired"]),
        "expired_lines": sum(1 for r in expiry if r["expired"]),
        "movements_today": movements_today,
        "stock_value_cents": stock_value,
        "uncosted_lots": uncosted,
    }


# ---------------------------------------------------------------------------
# Worklists (bare lists — the web workstream consumes them directly)
# ---------------------------------------------------------------------------

@router.get("/worklists/low-stock")
def low_stock_worklist(location_code: str | None = Query(default=None),
                       category: str | None = Query(default=None),
                       session: Session = Depends(get_db),
                       user: User = Depends(
                           require_capability("inventory.read"))):
    """Every item x location WITH a PAR level whose on-hand is below
    par: {sku, name, location_code, on_hand, par_level, deficit}."""
    return _low_stock_rows(session, location_code, category)


@router.get("/worklists/expiry")
def expiry_worklist(location_code: str | None = Query(default=None),
                    category: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(
                        require_capability("inventory.read"))):
    """Every stocked lot that is expired or expiring within the
    PUBLISHED taxonomy's expiryWarningDays: {sku, name,
    location_code, lot_number, expiry_date, quantity,
    days_to_expiry, expired}, expired first then earliest expiry."""
    return _expiry_rows(session, location_code, category)


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

def _validate_item_links(session: Session, payload: dict,
                         current: InventoryItem | None = None) -> dict:
    """Shared create/update validation of the taxonomy pair and the
    formulary / asset links. Returns the resolved field values."""
    category = payload.get("category")
    subcategory = payload.get("subcategory")
    if current is not None:
        category = category if isinstance(category, str) \
            and category.strip() else current.category
        subcategory = subcategory if isinstance(subcategory, str) \
            and subcategory.strip() else current.subcategory
    _validate_category_pair(session, category, subcategory)
    formulary_code = payload.get("formulary_code",
                                 current.formulary_code if current else None)
    asset_key = payload.get("asset_key",
                            current.asset_key if current else None)
    _validate_formulary_code(session, formulary_code)
    _validate_asset_key(session, asset_key)
    return {"category": category, "subcategory": subcategory,
            "formulary_code": formulary_code, "asset_key": asset_key}


@router.post("/items", status_code=201)
async def create_item(request: Request, session: Session = Depends(get_db),
                      user: User = Depends(
                          require_capability("inventory.manage"))):
    payload = await _json_body(request)
    sku = _require_str(payload, "sku")
    name = _require_str(payload, "name")
    category = _require_str(payload, "category")
    subcategory = _require_str(payload, "subcategory")
    unit_cost = _int_field(payload, "unit_cost_cents", minimum=0)
    links = _validate_item_links(session, payload)
    structure = _resolve_item_structure(session, payload, sku=sku)
    if session.scalars(select(InventoryItem).where(
            InventoryItem.sku == sku)).first() is not None:
        raise HTTPException(status_code=409,
                            detail=f"Inventory item '{sku}' already exists")
    row = InventoryItem(
        sku=sku, name=name, category=links["category"],
        subcategory=links["subcategory"],
        uom=_optional_str(payload, "uom") or "each",
        structure=structure["structure"], base_uom=structure["base_uom"],
        uom_ladder=structure["uom_ladder"],
        kit_components=structure["kit_components"],
        kit_mode=structure["kit_mode"],
        hazard_class=structure["hazard_class"],
        sds_ref=structure["sds_ref"],
        lot_tracked=_bool_field(payload, "lot_tracked", False),
        serial_tracked=_bool_field(payload, "serial_tracked", False),
        expiry_tracked=_bool_field(payload, "expiry_tracked", False),
        unit_cost_cents=unit_cost,
        formulary_code=links["formulary_code"],
        asset_key=links["asset_key"],
        active=_bool_field(payload, "active", True),
        created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="inventory.item.create",
                 entity=f"InventoryItem/{sku}",
                 payload={"name": name, "category": row.category,
                          "subcategory": row.subcategory})
    session.commit()
    return _item_out(row)


@router.get("/items")
def list_items(category: str | None = Query(default=None),
               subcategory: str | None = Query(default=None),
               active: bool | None = Query(default=None),
               q: str | None = Query(default=None),
               session: Session = Depends(get_db),
               user: User = Depends(require_capability("inventory.read"))):
    stmt = select(InventoryItem).order_by(InventoryItem.sku.asc())
    if category:
        stmt = stmt.where(InventoryItem.category == category)
    if subcategory:
        stmt = stmt.where(InventoryItem.subcategory == subcategory)
    if active is not None:
        stmt = stmt.where(InventoryItem.active == active)
    rows = session.scalars(stmt).all()
    if q:
        needle = q.strip().lower()
        rows = [r for r in rows if needle in r.sku.lower()
                or needle in r.name.lower()]
    return {"items": [_item_out(r) for r in rows], "total": len(rows)}


@router.get("/items/{sku}")
def get_item(sku: str, session: Session = Depends(get_db),
             user: User = Depends(require_capability("inventory.read"))):
    return _item_out(_item_or_404(session, sku))


@router.put("/items/{sku}")
async def update_item(sku: str, request: Request,
                      session: Session = Depends(get_db),
                      user: User = Depends(
                          require_capability("inventory.manage"))):
    """Update an item master record. The sku is immutable: a body
    sku that disagrees with the path is a 422. The resulting
    category/subcategory pair and any links are revalidated against
    the published sources, exactly as on create."""
    payload = await _json_body(request)
    row = _item_or_404(session, sku)
    body_sku = payload.get("sku")
    if body_sku is not None and body_sku != sku:
        raise HTTPException(
            status_code=422,
            detail=f"'sku' is immutable: body sku '{body_sku}' does "
                   f"not match '{sku}'")
    unit_cost = _int_field(payload, "unit_cost_cents", minimum=0)
    links = _validate_item_links(session, payload, current=row)
    structure = _resolve_item_structure(session, payload, sku=sku,
                                        current=row)
    if "name" in payload:
        row.name = _require_str(payload, "name")
    row.category = links["category"]
    row.subcategory = links["subcategory"]
    row.formulary_code = links["formulary_code"]
    row.asset_key = links["asset_key"]
    row.structure = structure["structure"]
    row.base_uom = structure["base_uom"]
    row.uom_ladder = structure["uom_ladder"]
    row.kit_components = structure["kit_components"]
    row.kit_mode = structure["kit_mode"]
    row.hazard_class = structure["hazard_class"]
    row.sds_ref = structure["sds_ref"]
    if "uom" in payload:
        row.uom = _optional_str(payload, "uom") or row.uom
    if "unit_cost_cents" in payload:
        row.unit_cost_cents = unit_cost
    for flag in ("lot_tracked", "serial_tracked", "expiry_tracked",
                 "active"):
        if flag in payload:
            setattr(row, flag, _bool_field(payload, flag,
                                           bool(getattr(row, flag))))
    session.flush()
    append_audit(session, actor=user.username, action="inventory.item.update",
                 entity=f"InventoryItem/{sku}",
                 payload={"category": row.category,
                          "subcategory": row.subcategory})
    session.commit()
    return _item_out(row)


# ---------------------------------------------------------------------------
# Stock + PAR levels
# ---------------------------------------------------------------------------

@router.get("/stock")
def list_stock(sku: str | None = Query(default=None),
               location_code: str | None = Query(default=None),
               category: str | None = Query(default=None),
               session: Session = Depends(get_db),
               user: User = Depends(require_capability("inventory.read"))):
    """Lot rows with the per-row EFFECTIVE unit cost (lot override,
    else the item default) and the line value."""
    stmt = select(InventoryStockLot).order_by(
        InventoryStockLot.sku.asc(), InventoryStockLot.location_code.asc(),
        InventoryStockLot.expiry_date.asc(), InventoryStockLot.id.asc())
    if sku:
        stmt = stmt.where(InventoryStockLot.sku == sku)
    if location_code:
        stmt = stmt.where(InventoryStockLot.location_code == location_code)
    rows = session.scalars(stmt).all()
    if category:
        items = {i.sku: i for i in session.scalars(
            select(InventoryItem).where(
                InventoryItem.category == category)).all()}
        rows = [r for r in rows if r.sku in items]
        return {"items": [_lot_out(session, r, items[r.sku]) for r in rows],
                "total": len(rows)}
    return {"items": [_lot_out(session, r) for r in rows],
            "total": len(rows)}


@router.get("/par-levels")
def list_par_levels(sku: str | None = Query(default=None),
                    location_code: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(
                        require_capability("inventory.read"))):
    stmt = select(InventoryParLevel).order_by(
        InventoryParLevel.sku.asc(), InventoryParLevel.location_code.asc())
    if sku:
        stmt = stmt.where(InventoryParLevel.sku == sku)
    if location_code:
        stmt = stmt.where(InventoryParLevel.location_code == location_code)
    rows = session.scalars(stmt).all()
    return {"items": [{"sku": r.sku, "location_code": r.location_code,
                       "par_level": r.par_level,
                       "par_unit_level": getattr(r, "par_unit_level", None),
                       "par_qty": getattr(r, "par_qty", None)}
                      for r in rows],
            "total": len(rows)}


@router.put("/par-levels")
async def upsert_par_level(request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(
                               require_capability("inventory.manage"))):
    payload = await _json_body(request)
    sku = _require_str(payload, "sku")
    location_code = _require_str(payload, "location_code")
    par_level = _int_field(payload, "par_level", required=True, minimum=0)
    item = _item_or_404(session, sku)
    _validate_location(session, location_code)
    unit_level = _optional_str(payload, "unit_level")
    par_qty = None
    par_base = par_level
    if unit_level is not None:
        par_base = par_level * _ladder_factor(item, unit_level)
        par_qty = par_level
    row = session.scalars(select(InventoryParLevel).where(
        InventoryParLevel.sku == sku,
        InventoryParLevel.location_code == location_code)).first()
    if row is None:
        row = InventoryParLevel(sku=sku, location_code=location_code,
                                par_level=par_base,
                                par_unit_level=unit_level, par_qty=par_qty,
                                updated_by=user.username)
        session.add(row)
    else:
        row.par_level = par_base
        row.par_unit_level = unit_level
        row.par_qty = par_qty
        row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="inventory.par.upsert",
                 entity=f"InventoryItem/{sku}",
                 payload={"location_code": location_code,
                          "par_level": par_base,
                          "par_unit_level": unit_level,
                          "par_qty": par_qty})
    session.commit()
    return {"sku": sku, "location_code": location_code,
            "par_level": par_base, "par_unit_level": unit_level,
            "par_qty": par_qty}


# ---------------------------------------------------------------------------
# Movements: receive / issue / transfer / adjust + ledger read
# ---------------------------------------------------------------------------

@router.post("/movements/receive", status_code=201)
async def receive(request: Request, session: Session = Depends(get_db),
                  user: User = Depends(require_capability("inventory.move"))):
    """Receive stock into a location. The item's tracking flags are
    enforced: lot-tracked items require a lot_number, serial-tracked
    items require a serial AND quantity == 1, expiry-tracked items
    require an expiry_date. A unit_cost_cents in the body becomes the
    lot's cost override."""
    payload = await _json_body(request)
    sku = _require_str(payload, "sku")
    location_code = _require_str(payload, "location_code")
    quantity = _int_field(payload, "quantity", required=True)
    if quantity is None or quantity <= 0:
        raise HTTPException(status_code=422,
                            detail="'quantity' must be > 0")
    item = _item_or_404(session, sku)
    _validate_location(session, location_code)
    base_quantity, unit_level = _convert_entered(item, payload, quantity)
    entered_detail = ({"entered_qty": quantity, "entered_level": unit_level}
                      if unit_level else None)
    lot_number = _optional_str(payload, "lot_number") or ""
    serial = _optional_str(payload, "serial")
    expiry_raw = payload.get("expiry_date")
    expiry_date = None
    if expiry_raw is not None:
        expiry_date = _parse_date(expiry_raw, "expiry_date").isoformat()
    cost_override = _int_field(payload, "unit_cost_cents", minimum=0)
    policy = _policy_for_item(session, item)
    reference = _optional_str(payload, "reference") or ""
    is_kit = (getattr(item, "structure", "simple") or "simple") == "kit"
    component_lots_raw = payload.get("component_lots")

    def _declared_map():
        declared: dict[str, dict] = {}
        if component_lots_raw is None:
            return declared
        if not isinstance(component_lots_raw, list):
            raise HTTPException(
                status_code=422,
                detail="'component_lots' must be a list of "
                       "{sku, lot_number, expiry_date}")
        for entry in component_lots_raw:
            if not isinstance(entry, dict) \
                    or not isinstance(entry.get("sku"), str):
                raise HTTPException(
                    status_code=422,
                    detail="Each 'component_lots' entry must be an "
                           "object {sku, lot_number, expiry_date}")
            declared[entry["sku"].strip()] = entry
        return declared

    # ---- break-on-receipt kits: validate ALL, then credit ----
    if is_kit and item.kit_mode == "break-on-receipt":
        declared = _declared_map()
        comp_defs = {c["sku"]: c
                     for c in (item.kit_components or [])
                     if isinstance(c, dict) and c.get("sku")}
        for dsku in declared:
            if dsku not in comp_defs:
                raise HTTPException(
                    status_code=422,
                    detail=f"Component lot declaration '{dsku}': not "
                           f"a component of kit '{sku}'")
        comp_items: dict[str, InventoryItem] = {}
        for csku, cdef in comp_defs.items():
            citem = session.scalars(select(InventoryItem).where(
                InventoryItem.sku == csku)).first()
            if citem is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"Kit component '{csku}': not an existing "
                           "inventory item")
            comp_items[csku] = citem
            decl = declared.get(csku)
            if citem.lot_tracked and (decl is None or not isinstance(
                    decl.get("lot_number"), str)
                    or not decl.get("lot_number", "").strip()):
                raise HTTPException(
                    status_code=422,
                    detail=f"Kit component '{csku}' is lot-tracked: "
                           "break-on-receipt requires a declaration "
                           "with 'lot_number' in 'component_lots'")
            if citem.expiry_tracked and (decl is None
                                         or not decl.get("expiry_date")):
                raise HTTPException(
                    status_code=422,
                    detail=f"Kit component '{csku}' is expiry-tracked: "
                           "break-on-receipt requires a declaration "
                           "with 'expiry_date' in 'component_lots'")
            if decl is not None and decl.get("expiry_date"):
                _parse_date(decl["expiry_date"], "expiry_date")
        movements_out, lots_out = [], []
        for csku, cdef in comp_defs.items():
            citem = comp_items[csku]
            decl = declared.get(csku) or {}
            clot_number = (decl.get("lot_number") or "")
            if isinstance(clot_number, str):
                clot_number = clot_number.strip()
            else:
                clot_number = ""
            cexpiry = (_parse_date(decl["expiry_date"], "expiry_date")
                       .isoformat() if decl.get("expiry_date") else None)
            amount = cdef["qty"] * base_quantity
            clot = _find_lot(session, csku, location_code, clot_number,
                             None)
            if clot is None:
                clot = InventoryStockLot(
                    sku=csku, location_code=location_code,
                    lot_number=clot_number, serial=None,
                    expiry_date=cexpiry, quantity=0,
                    stage=_policy_for_item(session, citem)["initialStage"])
                session.add(clot)
                session.flush()
            else:
                if cexpiry is not None:
                    clot.expiry_date = cexpiry
            clot.quantity += amount
            session.flush()
            if _policy_for_item(session, citem)["containerTracked"]:
                _make_container(session, lot=clot, quantity=amount)
            movements_out.append(_write_movement(
                session, event="receive", item=citem, lot=clot,
                delta=amount, actor=user.username, reference=reference,
                detail={"via_kit": sku, "via_mode": "break-on-receipt",
                        "kit_qty": base_quantity},
                unit_level=unit_level))
            lots_out.append(clot)
        append_audit(session, actor=user.username,
                     action="inventory.receive",
                     entity=f"InventoryItem/{sku}",
                     payload={"location_code": location_code,
                              "mode": "break-on-receipt",
                              "kit_qty": base_quantity,
                              "entry_ids": [m.entry_id
                                            for m in movements_out]})
        session.commit()
        return {"mode": "break-on-receipt",
                "movements": [_movement_out(m) for m in movements_out],
                "lots": [_lot_out(session, l, comp_items[l.sku])
                          for l in lots_out]}

    # ---- track-whole kits: composition + derived expiry ----
    composition = None
    if is_kit and item.kit_mode == "track-whole":
        declared = _declared_map()
        comp_defs = [c for c in (item.kit_components or [])
                     if isinstance(c, dict) and c.get("sku")]
        missing = [c["sku"] for c in comp_defs
                   if c["sku"] not in declared]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"Track-whole kit '{sku}': missing component "
                       "lot declarations for: " + ", ".join(missing))
        for dsku in declared:
            if dsku not in {c["sku"] for c in comp_defs}:
                raise HTTPException(
                    status_code=422,
                    detail=f"Component lot declaration '{dsku}': not "
                           f"a component of kit '{sku}'")
        composition = []
        declared_expiries = []
        for cdef in comp_defs:
            decl = declared[cdef["sku"]]
            dlot = decl.get("lot_number")
            if not isinstance(dlot, str) or not dlot.strip():
                raise HTTPException(
                    status_code=422,
                    detail=f"Component '{cdef['sku']}': declaration "
                           "requires 'lot_number'")
            dexp = None
            if decl.get("expiry_date"):
                dexp = _parse_date(decl["expiry_date"],
                                   "expiry_date").isoformat()
                declared_expiries.append(dexp)
            composition.append({"sku": cdef["sku"],
                                "lot_number": dlot.strip(),
                                "expiry_date": dexp,
                                "qty_per_kit": cdef["qty"],
                                "qty": cdef["qty"] * base_quantity})
        if declared_expiries:
            expiry_date = min(declared_expiries)

    if item.lot_tracked and not lot_number:
        raise HTTPException(
            status_code=422,
            detail=f"Item '{sku}' is lot-tracked: receiving requires "
                   "'lot_number'")
    if item.serial_tracked:
        if not serial:
            raise HTTPException(
                status_code=422,
                detail=f"Item '{sku}' is serial-tracked: receiving "
                       "requires 'serial'")
        if quantity != 1:
            raise HTTPException(
                status_code=422,
                detail=f"Item '{sku}' is serial-tracked: 'quantity' "
                       "must be 1 (one row per serial)")
    if item.expiry_tracked and not expiry_date:
        raise HTTPException(
            status_code=422,
            detail=f"Item '{sku}' is expiry-tracked: receiving "
                   "requires 'expiry_date'")
    # ---- containers (validate before any write) ----
    containers_raw = payload.get("containers")
    container_count = payload.get("container_count")
    container_specs: list[tuple[str | None, int]] | None = None
    if containers_raw is not None or container_count is not None:
        if not policy.get("containerTracked"):
            raise HTTPException(
                status_code=422,
                detail=f"Item '{sku}' (category '{item.category}', "
                       f"subcategory '{item.subcategory}') is not "
                       "container-tracked: the class policy "
                       "(classPolicy containerTracked) does not allow "
                       "'containers' on receive")
        if containers_raw is not None and container_count is not None:
            raise HTTPException(
                status_code=422,
                detail="Give either 'containers' or 'container_count', "
                       "not both")
        if containers_raw is not None:
            if not isinstance(containers_raw, list) \
                    or not containers_raw:
                raise HTTPException(
                    status_code=422,
                    detail="'containers' must be a non-empty list of "
                           "{container_code?, quantity}")
            container_specs = []
            seen_codes: set[str] = set()
            for entry in containers_raw:
                if not isinstance(entry, dict):
                    raise HTTPException(
                        status_code=422,
                        detail="Each container must be an object "
                               "{container_code?, quantity}")
                cqty = _int_field(entry, "quantity", required=True)
                if cqty is None or cqty <= 0:
                    raise HTTPException(
                        status_code=422,
                        detail="Each container 'quantity' must be > 0")
                ccode = entry.get("container_code")
                if ccode is not None:
                    if not isinstance(ccode, str) or not ccode.strip():
                        raise HTTPException(
                            status_code=422,
                            detail="'container_code' must be a "
                                   "non-empty string")
                    ccode = ccode.strip()
                    if ccode in seen_codes:
                        raise HTTPException(
                            status_code=422,
                            detail=f"Duplicate container_code "
                                   f"'{ccode}' in 'containers'")
                    seen_codes.add(ccode)
                    if session.scalars(select(InventoryContainer).where(
                            InventoryContainer.container_code == ccode)
                            ).first() is not None:
                        raise HTTPException(
                            status_code=422,
                            detail=f"Container '{ccode}' already exists")
                container_specs.append((ccode, cqty))
            total = sum(q for _c, q in container_specs)
            if total != base_quantity:
                raise HTTPException(
                    status_code=422,
                    detail=f"Container quantities sum to {total} but "
                           f"the received quantity is {base_quantity} "
                           "— they must be equal")
        else:
            if isinstance(container_count, bool) \
                    or not isinstance(container_count, int) \
                    or container_count <= 0:
                raise HTTPException(
                    status_code=422,
                    detail="'container_count' must be a positive "
                           "integer")
            each, remainder = divmod(base_quantity, container_count)
            container_specs = [(None, each + (1 if i < remainder else 0))
                               for i in range(container_count)]
    lot = _find_lot(session, sku, location_code, lot_number, serial)
    is_new_lot = lot is None
    if lot is None:
        lot = InventoryStockLot(sku=sku, location_code=location_code,
                                lot_number=lot_number, serial=serial,
                                expiry_date=expiry_date, quantity=0,
                                unit_cost_cents=cost_override,
                                stage=policy["initialStage"])
        session.add(lot)
        session.flush()
    else:
        if expiry_date is not None:
            lot.expiry_date = expiry_date
        if cost_override is not None:
            lot.unit_cost_cents = cost_override
    lot.quantity += base_quantity
    if composition is not None:
        if is_new_lot or not (lot.composition or []):
            lot.composition = composition
        else:
            # Receiving MORE units of an existing kit lot: the
            # stored composition carries REMAINING totals, so the
            # new receipt's totals merge into the matching entries
            # instead of replacing them.
            merged = [dict(e) for e in lot.composition
                      if isinstance(e, dict)]
            for new_entry in composition:
                match = next(
                    (e for e in merged
                     if e.get("sku") == new_entry["sku"]
                     and e.get("lot_number") == new_entry["lot_number"]
                     and e.get("expiry_date") == new_entry["expiry_date"]),
                    None)
                if match is None:
                    merged.append(dict(new_entry))
                else:
                    if match.get("qty") is None:
                        match["qty"] = (match.get("qty_per_kit") or 0) \
                            * (lot.quantity - base_quantity)
                    match["qty"] += new_entry["qty"]
            lot.composition = merged
    session.flush()
    created_containers = []
    if policy.get("containerTracked"):
        if container_specs is not None:
            taken: set[str] = set()
            for ccode, cqty in container_specs:
                if cqty <= 0:
                    continue
                cont = _make_container(session, lot=lot, quantity=cqty,
                                       code=ccode, taken=taken)
                taken.add(cont.container_code)
                created_containers.append(cont)
        else:
            created_containers.append(
                _make_container(session, lot=lot, quantity=base_quantity))
    movement = _write_movement(
        session, event="receive", item=item, lot=lot, delta=base_quantity,
        actor=user.username, reference=reference,
        detail=entered_detail, unit_level=unit_level)
    append_audit(session, actor=user.username, action="inventory.receive",
                 entity=f"InventoryItem/{sku}",
                 payload={"location_code": location_code,
                          "lot_number": lot_number,
                          "quantity": base_quantity,
                          "entry_id": movement.entry_id})
    session.commit()
    out = _movement_out(movement)
    out["lot"] = _lot_out(session, lot, item)
    if created_containers:
        out["containers"] = [_container_out(session, c)
                             for c in created_containers]
    return out


@router.post("/movements/issue")
async def issue(request: Request, session: Session = Depends(get_db),
                user: User = Depends(require_capability("inventory.move"))):
    """Issue stock FEFO across the unexpired lots at a location
    (expired lots never allocate). Insufficient stock is a 409
    naming sku / needed / available and NOTHING is written. One
    movement per lot drawn."""
    payload = await _json_body(request)
    sku = _require_str(payload, "sku")
    location_code = _require_str(payload, "location_code")
    quantity = _int_field(payload, "quantity", required=True)
    if quantity is None or quantity <= 0:
        raise HTTPException(status_code=422,
                            detail="'quantity' must be > 0")
    item = _item_or_404(session, sku)
    _validate_location(session, location_code)
    base_quantity, unit_level = _convert_entered(item, payload, quantity)
    lot_number = _optional_str(payload, "lot_number")
    plan, available = _fefo_plan(session, sku, location_code, base_quantity,
                                 lot_number=lot_number)
    if available < base_quantity:
        raise HTTPException(
            status_code=409,
            detail=f"Issue of '{sku}' at '{location_code}': needs "
                   f"{base_quantity} but only {available} available — "
                   "issue refused, nothing written"
                   + _blocked_suffix(session, sku, location_code))
    movements, allocations = _apply_draw(
        session, item=item, plan=plan, event="issue", actor=user.username,
        reference=_optional_str(payload, "reference") or "",
        detail=({"entered_qty": quantity, "entered_level": unit_level}
                if unit_level else None),
        unit_level=unit_level)
    append_audit(session, actor=user.username, action="inventory.issue",
                 entity=f"InventoryItem/{sku}",
                 payload={"location_code": location_code,
                          "quantity": base_quantity,
                          "entry_ids": [m.entry_id for m in movements]})
    session.commit()
    return {"sku": sku, "location_code": location_code,
            "quantity": base_quantity,
            "movements": [_movement_out(m) for m in movements],
            "allocations": allocations}


@router.post("/movements/transfer")
async def transfer(request: Request, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("inventory.move"))):
    """Transfer stock between two locations: FEFO at the source, the
    same lot identity lands at the destination, and the ledger gets
    a paired movement (negative at the source, positive at the
    destination) per lot drawn — both written or neither."""
    payload = await _json_body(request)
    sku = _require_str(payload, "sku")
    from_location = _require_str(payload, "from_location")
    to_location = _require_str(payload, "to_location")
    quantity = _int_field(payload, "quantity", required=True)
    if quantity is None or quantity <= 0:
        raise HTTPException(status_code=422,
                            detail="'quantity' must be > 0")
    if from_location == to_location:
        raise HTTPException(
            status_code=422,
            detail="'from_location' and 'to_location' must differ")
    item = _item_or_404(session, sku)
    _validate_location(session, from_location)
    _validate_location(session, to_location)
    if _policy_for_item(session, item)["containerTracked"]:
        raise HTTPException(
            status_code=409,
            detail=f"Transfer of '{sku}' refused: the item's class "
                   "policy is container-tracked (classPolicy "
                   "containerTracked), so lot-level transfer would "
                   "break the lot == sum-of-live-containers "
                   "invariant — container-tracked stock moves by "
                   "container: POST /inventory/containers/transfer "
                   "— transfer refused, nothing written")
    base_quantity, unit_level = _convert_entered(item, payload, quantity)
    entered = ({"entered_qty": quantity, "entered_level": unit_level}
               if unit_level else {})
    lot_number = _optional_str(payload, "lot_number")
    plan, available = _fefo_plan(session, sku, from_location, base_quantity,
                                 lot_number=lot_number)
    if available < base_quantity:
        raise HTTPException(
            status_code=409,
            detail=f"Transfer of '{sku}' from '{from_location}': "
                   f"needs {base_quantity} but only {available} available "
                   "— transfer refused, nothing written"
                   + _blocked_suffix(session, sku, from_location))
    reference = _optional_str(payload, "reference") or ""
    movements = []
    for lot, take in plan:
        dest = _find_lot(session, sku, to_location, lot.lot_number,
                         lot.serial)
        if dest is None:
            dest = InventoryStockLot(
                sku=sku, location_code=to_location,
                lot_number=lot.lot_number, serial=lot.serial,
                expiry_date=lot.expiry_date, quantity=0,
                unit_cost_cents=lot.unit_cost_cents,
                stage=getattr(lot, "stage", None) or "released",
                verified_by=getattr(lot, "verified_by", None),
                verification_method=getattr(lot, "verification_method",
                                            None),
                verification_result=getattr(lot, "verification_result",
                                            None),
                verified_on=getattr(lot, "verified_on", None),
                verification_note=getattr(lot, "verification_note", None),
                composition=list(getattr(lot, "composition", None) or []))
            session.add(dest)
            session.flush()
        lot.quantity -= take
        session.flush()
        movements.append(_write_movement(
            session, event="transfer", item=item, lot=lot, delta=-take,
            actor=user.username, to_location_code=to_location,
            reference=reference, unit_level=unit_level,
            detail={"direction": "out", "to_location": to_location,
                    **entered}))
        dest.quantity += take
        session.flush()
        movements.append(_write_movement(
            session, event="transfer", item=item, lot=dest, delta=take,
            actor=user.username, reference=reference,
            unit_level=unit_level,
            detail={"direction": "in", "from_location": from_location,
                    **entered}))
    append_audit(session, actor=user.username, action="inventory.transfer",
                 entity=f"InventoryItem/{sku}",
                 payload={"from_location": from_location,
                          "to_location": to_location,
                          "quantity": base_quantity,
                          "entry_ids": [m.entry_id for m in movements]})
    session.commit()
    return {"sku": sku, "from_location": from_location,
            "to_location": to_location, "quantity": base_quantity,
            "movements": [_movement_out(m) for m in movements]}


@router.post("/movements/adjust")
async def adjust(request: Request, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("inventory.move"))):
    """Adjust one lot by a signed delta. The reason_code MUST be a
    published taxonomy reason code; reason "other" requires a
    non-empty note. A result below zero is a 409, nothing written."""
    payload = await _json_body(request)
    sku = _require_str(payload, "sku")
    location_code = _require_str(payload, "location_code")
    delta = _int_field(payload, "quantity_delta", required=True)
    if delta is None or delta == 0:
        raise HTTPException(status_code=422,
                            detail="'quantity_delta' must be a non-zero "
                                   "integer")
    reason_code = _require_str(payload, "reason_code")
    note = _optional_str(payload, "note")
    if reason_code not in _reason_codes(session):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown reason_code '{reason_code}': not in the "
                   "published inventory taxonomy reason codes")
    if reason_code == "other" and not note:
        raise HTTPException(
            status_code=422,
            detail="Reason 'other' requires a non-empty 'note'")
    item = _item_or_404(session, sku)
    _validate_location(session, location_code)
    base_delta, unit_level = _convert_entered(item, payload, delta)
    if base_delta < 0 and item.asset_key:
        raise HTTPException(
            status_code=409,
            detail=f"Adjustment of '{sku}' at '{location_code}' by "
                   f"{base_delta} refused: instruments are serialized "
                   "assets linked to the device registry and are never "
                   "consumed — an asset-linked item cannot be adjusted "
                   "down")
    lot_number = _optional_str(payload, "lot_number")
    serial = _optional_str(payload, "serial")
    reference = _optional_str(payload, "reference") or ""
    entered_detail = ({"entered_qty": delta, "entered_level": unit_level}
                      if unit_level else None)
    movements = []
    if base_delta > 0:
        lot = _find_lot(session, sku, location_code, lot_number or "",
                        serial)
        if lot is None:
            expiry_raw = payload.get("expiry_date")
            lot = InventoryStockLot(
                sku=sku, location_code=location_code,
                lot_number=lot_number or "", serial=serial,
                expiry_date=(_parse_date(expiry_raw, "expiry_date")
                             .isoformat() if expiry_raw else None),
                quantity=0,
                stage=_policy_for_item(session, item)["initialStage"])
            session.add(lot)
            session.flush()
        lot.quantity += base_delta
        session.flush()
        movements.append(_write_movement(
            session, event="adjust", item=item, lot=lot, delta=base_delta,
            actor=user.username, reference=reference,
            reason_code=reason_code, note=note, detail=entered_detail,
            unit_level=unit_level))
    else:
        if serial is not None:
            # A serial pins the adjustment to exactly that lot row.
            target = _find_lot(session, sku, location_code,
                               lot_number or "", serial)
            available = target.quantity if target is not None else 0
            if available < -base_delta:
                raise HTTPException(
                    status_code=409,
                    detail=f"Adjustment of '{sku}' at '{location_code}' "
                           f"by {base_delta} would take stock below zero "
                           f"(only {available} on hand) — refused, "
                           "nothing written")
            plan = [(target, -base_delta)]
        else:
            plan, available = _fefo_plan(session, sku, location_code,
                                         -base_delta,
                                         lot_number=lot_number)
            if available < -base_delta:
                raise HTTPException(
                    status_code=409,
                    detail=f"Adjustment of '{sku}' at '{location_code}' "
                           f"by {base_delta} would take stock below zero "
                           f"(only {available} on hand) — refused, "
                           "nothing written")
        drawn, _allocations = _apply_draw(
            session, item=item, plan=plan, event="adjust",
            actor=user.username, reference=reference,
            reason_code=reason_code, note=note, detail=entered_detail,
            unit_level=unit_level)
        movements.extend(drawn)
    append_audit(session, actor=user.username, action="inventory.adjust",
                 entity=f"InventoryItem/{sku}",
                 payload={"location_code": location_code,
                          "delta": base_delta,
                          "reason_code": reason_code,
                          "entry_ids": [m.entry_id for m in movements]})
    session.commit()
    return {"sku": sku, "location_code": location_code,
            "quantity_delta": base_delta,
            "movements": [_movement_out(m) for m in movements]}


@router.get("/movements")
def list_movements(sku: str | None = Query(default=None),
                   location_code: str | None = Query(default=None),
                   event: str | None = Query(default=None),
                   reference: str | None = Query(default=None),
                   patient_ref: str | None = Query(default=None),
                   session: Session = Depends(get_db),
                   user: User = Depends(
                       require_capability("inventory.read"))):
    """The append-only ledger, newest first."""
    stmt = select(InventoryMovement).order_by(InventoryMovement.id.desc())
    if sku:
        stmt = stmt.where(InventoryMovement.sku == sku)
    if location_code:
        stmt = stmt.where(InventoryMovement.location_code == location_code)
    if event:
        stmt = stmt.where(InventoryMovement.event == event)
    if reference:
        stmt = stmt.where(InventoryMovement.reference == reference)
    if patient_ref:
        stmt = stmt.where(InventoryMovement.patient_ref == patient_ref)
    rows = session.scalars(stmt.limit(500)).all()
    return {"items": [_movement_out(r) for r in rows], "total": len(rows)}


# ---------------------------------------------------------------------------
# Lot stages + verification
# ---------------------------------------------------------------------------

_STAGE_TRANSITIONS: dict[str, set[str]] = {
    "received": {"quarantine", "released"},
    "quarantine": {"released", "recalled", "disposed"},
    "released": {"in_use", "quarantine", "recalled", "disposed"},
    "in_use": {"quarantine", "recalled", "disposed"},
    "recalled": set(),
    "disposed": set(),
}
_STAGE_REASON_FIELDS = {"quarantine": "quarantineReasonCodes",
                        "recalled": "recallReasonCodes",
                        "disposed": "disposalReasonCodes"}


def _lot_by_identity(session: Session, payload: dict):
    sku = _require_str(payload, "sku")
    location_code = _require_str(payload, "location_code")
    lot_number = payload.get("lot_number") or ""
    if not isinstance(lot_number, str):
        raise HTTPException(status_code=422,
                            detail="'lot_number' must be a string")
    serial = _optional_str(payload, "serial")
    _validate_location(session, location_code)
    lot = _find_lot(session, sku, location_code, lot_number.strip(),
                    serial)
    if lot is None:
        raise HTTPException(
            status_code=404,
            detail=f"Lot '{lot_number}' of '{sku}' at "
                   f"'{location_code}' was not found")
    return sku, location_code, lot


@router.post("/lots/verify")
async def verify_lot(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(
                         require_capability("inventory.verify"))):
    """Stamp a lot's verification record (CLIA/CAP lot-to-lot
    verification): method + result (pass|fail) + actor + UTC date.
    A fail blocks release until a later pass supersedes it."""
    payload = await _json_body(request)
    sku, location_code, lot = _lot_by_identity(session, payload)
    method = _require_str(payload, "method")
    result = _require_str(payload, "result")
    if result not in ("pass", "fail"):
        raise HTTPException(
            status_code=422,
            detail=f"'result' must be 'pass' or 'fail' (got '{result}')")
    note = _optional_str(payload, "note")
    lot.verified_by = user.username
    lot.verification_method = method
    lot.verification_result = result
    lot.verified_on = _today().isoformat()
    lot.verification_note = note
    session.flush()
    append_audit(session, actor=user.username, action="inventory.lot.verify",
                 entity=f"InventoryItem/{sku}",
                 payload={"location_code": location_code,
                          "lot_number": lot.lot_number, "method": method,
                          "result": result,
                          "verified_on": lot.verified_on})
    session.commit()
    item = session.scalars(select(InventoryItem).where(
        InventoryItem.sku == sku)).first()
    return _lot_out(session, lot, item)


@router.post("/lots/stage")
async def stage_lot(request: Request, session: Session = Depends(get_db),
                    user: User = Depends(
                        require_capability("inventory.move"))):
    """Transition a lot's stage through the fixed stage machine.
    Quarantine / recall / disposal require a reason from the
    matching governed reason list; release of a class whose policy
    requires verification needs a CURRENT passing verification.
    Disposal zeroes the lot; recall/disposal cascade to the lot's
    containers, and every other transition syncs live containers
    to the lot's new stage."""
    payload = await _json_body(request)
    sku, location_code, lot = _lot_by_identity(session, payload)
    to_stage = _require_str(payload, "to_stage")
    reason_code = _optional_str(payload, "reason_code")
    note = _optional_str(payload, "note")
    from_stage = getattr(lot, "stage", None) or "released"
    allowed = _STAGE_TRANSITIONS.get(from_stage, set())
    if to_stage not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Illegal stage transition for lot "
                   f"'{lot.lot_number}' of '{sku}': '{from_stage}' -> "
                   f"'{to_stage}' is not allowed")
    reason_field = _STAGE_REASON_FIELDS.get(to_stage)
    if reason_field is not None:
        codes = _governed_reason_codes(session, reason_field)
        if not reason_code:
            raise HTTPException(
                status_code=422,
                detail=f"Transition to '{to_stage}' requires a "
                       f"'reason_code' from the governed "
                       f"{reason_field} list")
        if reason_code not in codes:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown reason_code '{reason_code}': not in "
                       f"the governed {reason_field} list")
    item = _item_or_404(session, sku)
    detail: dict = {"from_stage": from_stage, "to_stage": to_stage,
                    "reason_code": reason_code}
    if to_stage == "released":
        policy = _policy_for_item(session, item)
        if policy.get("verificationRequired"):
            current, why = _verification_status(lot, policy)
            if not current:
                raise HTTPException(
                    status_code=409,
                    detail=f"Release of lot '{lot.lot_number}' of "
                           f"'{sku}' refused: the class policy "
                           "requires verification and there is no "
                           f"current passing verification ({why})")
        if getattr(lot, "verification_result", None):
            detail["verification"] = {
                "verified_by": lot.verified_by,
                "method": lot.verification_method,
                "result": lot.verification_result,
                "verified_on": lot.verified_on}
    delta = 0
    if to_stage == "disposed":
        delta = -lot.quantity
        lot.quantity = 0
    lot.stage = to_stage
    session.flush()
    for cont in _containers_for_lot(session, lot):
        if cont.stage in ("disposed", "depleted"):
            continue
        if to_stage == "disposed":
            cont.quantity = 0
            cont.stage = "disposed"
            cont.disposal_reason_code = reason_code
            cont.disposed_by = user.username
            cont.disposed_at = datetime.now(timezone.utc)
        else:
            cont.stage = to_stage
    session.flush()
    movement = _write_movement(
        session, event="stage_transition", item=item, lot=lot,
        delta=delta, actor=user.username, reason_code=reason_code,
        note=note, detail=detail)
    append_audit(session, actor=user.username, action="inventory.lot.stage",
                 entity=f"InventoryItem/{sku}",
                 payload={"location_code": location_code,
                          "lot_number": lot.lot_number,
                          "from_stage": from_stage, "to_stage": to_stage,
                          "reason_code": reason_code,
                          "entry_id": movement.entry_id})
    session.commit()
    out = _lot_out(session, lot, item)
    out["movement"] = _movement_out(movement)
    return out


# ---------------------------------------------------------------------------
# Kits
# ---------------------------------------------------------------------------

def _kit_item_or_422(session: Session, sku: str) -> InventoryItem:
    item = _item_or_404(session, sku)
    if (getattr(item, "structure", "simple") or "simple") != "kit":
        raise HTTPException(
            status_code=422,
            detail=f"Item '{sku}' is not a kit (structure is "
                   f"'{getattr(item, 'structure', 'simple')}')")
    return item


def _composition_entries(lot: InventoryStockLot,
                         item: InventoryItem) -> list:
    """The kit lot's composition snapshot as credit entries. Every
    entry carries a REMAINING `qty` total: a snapshot entry stamped
    before remaining-qty totals existed (qty_per_kit only) is
    normalised against the lot's current quantity. The snapshot
    RULES kit credits — the item's CURRENT governed kit_components
    are used ONLY to synthesise entries for a legacy lot whose
    composition is empty."""
    entries = [dict(e) for e in (getattr(lot, "composition", None) or [])
               if isinstance(e, dict) and e.get("sku")]
    if entries:
        for entry in entries:
            if entry.get("qty") is None \
                    and entry.get("qty_per_kit") is not None:
                entry["qty"] = entry["qty_per_kit"] * (lot.quantity or 0)
        return entries
    return [{"sku": c["sku"], "lot_number": "",
             "expiry_date": lot.expiry_date,
             "qty": c["qty"] * (lot.quantity or 0)}
            for c in (item.kit_components or [])
            if isinstance(c, dict) and c.get("sku")]


def _kit_credit_plan(entries: list, op_qty: int,
                     kits_in_lot_before_draw: int):
    """Credit plan for opening / disassembling op_qty kits, computed
    from the composition snapshot ALONE. Per component sku,
    total_to_credit = (sum of the snapshot entries' remaining qty
    for that sku) x op_qty / kits_in_lot_before_draw — exact by
    construction for a well-formed snapshot; a total that does not
    divide exactly is data corruption and refuses 409 BEFORE
    anything is written. Credits walk the sku's entries in order
    (take = min(entry remaining qty, still needed), credited to the
    component lot THAT entry identifies).

    Returns (plan, remaining_entries): plan is a list of
    (comp_sku, entry, amount); remaining_entries is the lot's
    post-operation stored composition (entries whose remaining qty
    reaches 0 drop out)."""
    if kits_in_lot_before_draw <= 0:
        raise HTTPException(
            status_code=409,
            detail="Kit composition inconsistency: the kit lot has "
                   f"{kits_in_lot_before_draw} kits before the draw "
                   "— refused, nothing written")
    working = [dict(e) for e in entries]
    plan = []
    skus = []
    for entry in working:
        if entry["sku"] not in skus:
            skus.append(entry["sku"])
    for comp_sku in skus:
        sku_entries = [e for e in working if e.get("sku") == comp_sku]
        total_remaining = 0
        for entry in sku_entries:
            qty = entry.get("qty")
            if not isinstance(qty, int) or isinstance(qty, bool) \
                    or qty < 0:
                raise HTTPException(
                    status_code=409,
                    detail=f"Kit composition inconsistency: component "
                           f"'{comp_sku}' has an entry with no valid "
                           "remaining 'qty' — data corruption "
                           "suspected, refused, nothing written")
            total_remaining += qty
        if (total_remaining * op_qty) % kits_in_lot_before_draw != 0:
            raise HTTPException(
                status_code=409,
                detail=f"Kit composition inconsistency: component "
                       f"'{comp_sku}' remaining qty {total_remaining} "
                       f"x {op_qty} kit(s) does not divide evenly "
                       f"across the {kits_in_lot_before_draw} kit(s) "
                       "in the lot — data corruption suspected, "
                       "refused, nothing written")
        still_needed = (total_remaining * op_qty) \
            // kits_in_lot_before_draw
        for entry in sku_entries:
            if still_needed <= 0:
                break
            take = min(entry["qty"], still_needed)
            if take > 0:
                plan.append((comp_sku, entry, take))
                entry["qty"] -= take
                still_needed -= take
        if still_needed > 0:
            raise HTTPException(
                status_code=409,
                detail=f"Kit composition inconsistency: component "
                       f"'{comp_sku}' entries cover "
                       f"{total_remaining - still_needed} of the "
                       f"{total_remaining} remaining units — data "
                       "corruption suspected, refused, nothing written")
    remaining_entries = [e for e in working if e.get("qty", 0) > 0]
    return plan, remaining_entries


def _credit_component(session: Session, *, comp_sku: str,
                      location_code: str, entry: dict, amount: int,
                      kit_stage: str, event: str, actor: str,
                      kit_sku: str, kit_lot_number: str,
                      movements: list) -> InventoryStockLot:
    comp_item = _item_or_404(session, comp_sku)
    entry_lot = entry.get("lot_number") or ""
    clot = _find_lot(session, comp_sku, location_code, entry_lot, None)
    if clot is None:
        stage = kit_stage if kit_stage in ("received", "quarantine") \
            else "released"
        clot = InventoryStockLot(
            sku=comp_sku, location_code=location_code,
            lot_number=entry_lot, serial=None,
            expiry_date=entry.get("expiry_date"), quantity=0, stage=stage)
        session.add(clot)
        session.flush()
    clot.quantity += amount
    session.flush()
    if _policy_for_item(session, comp_item)["containerTracked"]:
        _make_container(session, lot=clot, quantity=amount)
    movements.append(_write_movement(
        session, event=event, item=comp_item, lot=clot, delta=amount,
        actor=actor,
        detail={"from_kit": kit_sku, "kit_lot_number": kit_lot_number}))
    return clot


@router.post("/kits/open")
async def kit_open(request: Request, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("inventory.move"))):
    """Open track-whole kit units at a location: -quantity kit
    units, +component quantities per the lot's composition
    snapshot, each keeping its declared lot/expiry. A deactivated
    component is a 409 naming it, nothing written."""
    payload = await _json_body(request)
    sku = _require_str(payload, "sku")
    location_code = _require_str(payload, "location_code")
    quantity = _int_field(payload, "quantity")
    if quantity is None:
        quantity = 1
    if quantity <= 0:
        raise HTTPException(status_code=422,
                            detail="'quantity' must be > 0")
    item = _kit_item_or_422(session, sku)
    _validate_location(session, location_code)
    lot_number = _optional_str(payload, "lot_number")
    kit_lot = None
    plan = None
    if lot_number is not None:
        kit_lot = _find_lot(session, sku, location_code, lot_number, None)
        available = kit_lot.quantity if kit_lot is not None else 0
        if kit_lot is None or available < quantity:
            raise HTTPException(
                status_code=409,
                detail=f"Opening kit '{sku}' at '{location_code}': "
                       f"needs {quantity} but only {available} "
                       "available — refused, nothing written")
        stage = getattr(kit_lot, "stage", None) or "released"
        if stage not in ("released", "in_use"):
            raise HTTPException(
                status_code=409,
                detail=f"Opening kit '{sku}' at '{location_code}': lot "
                       f"'{lot_number}' is at stage '{stage}' and "
                       "cannot be opened — refused, nothing written")
        blocked, blocked_by = _kit_recall_block(session, kit_lot)
        if blocked:
            raise HTTPException(
                status_code=409,
                detail=f"Opening kit '{sku}' at '{location_code}': "
                       "the kit is recall-blocked by "
                       + ", ".join(f"{b['sku']}/{b['lot_number']}"
                                   for b in blocked_by)
                       + " — refused, nothing written")
        if _lot_expired(kit_lot, _today()):
            raise HTTPException(
                status_code=409,
                detail=f"Opening kit '{sku}' at '{location_code}': lot "
                       f"'{lot_number}' is expired — refused, "
                       "nothing written")
    else:
        plan, available = _fefo_plan(session, sku, location_code, quantity)
        if available < quantity:
            raise HTTPException(
                status_code=409,
                detail=f"Opening kit '{sku}' at '{location_code}': "
                       f"needs {quantity} but only {available} "
                       "available — refused, nothing written"
                       + _blocked_suffix(session, sku, location_code))
        if len(plan) != 1:
            raise HTTPException(
                status_code=409,
                detail=f"Opening kit '{sku}' at '{location_code}': "
                       "the draw spans multiple kit lots; name a "
                       "'lot_number' — refused, nothing written")
        kit_lot = plan[0][0]
    entries = _composition_entries(kit_lot, item)
    # Validate components before writing anything.
    for entry in entries:
        comp = session.scalars(select(InventoryItem).where(
            InventoryItem.sku == entry["sku"])).first()
        if comp is None:
            raise HTTPException(
                status_code=409,
                detail=f"Opening kit '{sku}': component "
                       f"'{entry['sku']}' is not an inventory item — "
                       "refused, nothing written")
        if not comp.active:
            raise HTTPException(
                status_code=409,
                detail=f"Opening kit '{sku}': component "
                       f"'{entry['sku']}' is deactivated — refused, "
                       "nothing written")
    kit_stage = getattr(kit_lot, "stage", None) or "released"
    kit_qty_before = kit_lot.quantity
    # Plan from the snapshot BEFORE any write: a non-divisible
    # snapshot refuses 409 here and nothing is written. The kit
    # movement keeps `entries` as the PRE-operation snapshot; the
    # lot's stored composition is persisted in its post-operation
    # state (remaining_entries).
    credit_plan, remaining_entries = _kit_credit_plan(
        entries, quantity, kit_qty_before)
    movements = []
    kit_lot.quantity -= quantity
    kit_lot.composition = remaining_entries
    session.flush()
    movements.append(_write_movement(
        session, event="kit_open", item=item, lot=kit_lot,
        delta=-quantity, actor=user.username,
        detail={"composition": entries}))
    lots_out = []
    for comp_sku, entry, amount in credit_plan:
        lots_out.append(_credit_component(
            session, comp_sku=comp_sku, location_code=location_code,
            entry=entry, amount=amount, kit_stage=kit_stage,
            event="kit_open", actor=user.username, kit_sku=sku,
            kit_lot_number=kit_lot.lot_number, movements=movements))
    append_audit(session, actor=user.username, action="inventory.kit.open",
                 entity=f"InventoryItem/{sku}",
                 payload={"location_code": location_code,
                          "lot_number": kit_lot.lot_number,
                          "quantity": quantity,
                          "entry_ids": [m.entry_id for m in movements]})
    session.commit()
    return {"sku": sku, "location_code": location_code,
            "quantity": quantity,
            "kit_lot": _lot_out(session, kit_lot, item),
            "movements": [_movement_out(m) for m in movements],
            "lots": [_lot_out(session, l) for l in lots_out]}


@router.post("/kits/assemble", status_code=201)
async def kit_assemble(request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(
                           require_capability("inventory.move"))):
    """Assemble kits from component stock: FEFO-plan EVERY
    component first (any shortfall -> 409 naming every shortfall,
    zero writes), then draw with kit_assemble and create the kit
    lot whose expiry is the earliest drawn component expiry."""
    payload = await _json_body(request)
    sku = _require_str(payload, "sku")
    location_code = _require_str(payload, "location_code")
    quantity = _int_field(payload, "quantity", required=True)
    if quantity is None or quantity <= 0:
        raise HTTPException(status_code=422,
                            detail="'quantity' must be > 0")
    item = _kit_item_or_422(session, sku)
    _validate_location(session, location_code)
    reserved: dict[int, int] = {}
    plans = []
    shortfalls = []
    for comp in item.kit_components or []:
        if not isinstance(comp, dict) or not comp.get("sku"):
            continue
        csku = comp["sku"]
        needed = comp["qty"] * quantity
        plan, available = _fefo_plan(session, csku, location_code, needed,
                                     reserved=reserved)
        if available < needed:
            shortfalls.append(
                f"'{csku}': needs {needed} but only {available} "
                f"available at '{location_code}'")
            continue
        for lot, take in plan:
            reserved[lot.id] = reserved.get(lot.id, 0) + take
        plans.append((csku, plan))
    if shortfalls:
        raise HTTPException(
            status_code=409,
            detail="Kit assembly refused, shortfalls: "
                   + "; ".join(shortfalls))
    movements = []
    composition = []
    expiries = []
    for csku, plan in plans:
        comp_item = _item_or_404(session, csku)
        drawn, allocations = _apply_draw(
            session, item=comp_item, plan=plan, event="kit_assemble",
            actor=user.username, detail={"for_kit": sku})
        movements.extend(drawn)
        for alloc in allocations:
            composition.append({"sku": csku,
                                "lot_number": alloc["lot_number"],
                                "expiry_date": alloc["expiry_date"],
                                "qty": alloc["qty"]})
            if alloc["expiry_date"]:
                expiries.append(alloc["expiry_date"])
    lot_number = _optional_str(payload, "lot_number") \
        or f"KIT-{uuid.uuid4().hex[:8].upper()}"
    kit_lot = _find_lot(session, sku, location_code, lot_number, None)
    # Assembling into an EXISTING kit lot appends the new draws to
    # the stored composition (whose entries carry remaining totals)
    # instead of replacing it, so credits stay exact.
    prior_entries = []
    if kit_lot is not None:
        prior_entries = [e for e in _composition_entries(kit_lot, item)
                         if e.get("qty", 0) > 0]
    if kit_lot is None:
        kit_lot = InventoryStockLot(
            sku=sku, location_code=location_code, lot_number=lot_number,
            serial=None,
            expiry_date=min(expiries) if expiries else None, quantity=0,
            stage="released")
        session.add(kit_lot)
        session.flush()
    else:
        kit_lot.expiry_date = min(expiries) if expiries \
            else kit_lot.expiry_date
    kit_lot.quantity += quantity
    kit_lot.composition = prior_entries + composition
    session.flush()
    movements.append(_write_movement(
        session, event="kit_assemble", item=item, lot=kit_lot,
        delta=quantity, actor=user.username,
        detail={"composition": composition}))
    append_audit(session, actor=user.username,
                 action="inventory.kit.assemble",
                 entity=f"InventoryItem/{sku}",
                 payload={"location_code": location_code,
                          "lot_number": lot_number, "quantity": quantity,
                          "entry_ids": [m.entry_id for m in movements]})
    session.commit()
    return {"sku": sku, "location_code": location_code,
            "quantity": quantity,
            "lot": _lot_out(session, kit_lot, item),
            "composition": composition,
            "movements": [_movement_out(m) for m in movements]}


@router.post("/kits/disassemble")
async def kit_disassemble(request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(
                              require_capability("inventory.move"))):
    """Disassemble kit units back into their component lots (the
    composition snapshot's original lots). A recalled kit lot
    refuses 409 naming the recall."""
    payload = await _json_body(request)
    sku = _require_str(payload, "sku")
    location_code = _require_str(payload, "location_code")
    quantity = _int_field(payload, "quantity", required=True)
    if quantity is None or quantity <= 0:
        raise HTTPException(status_code=422,
                            detail="'quantity' must be > 0")
    item = _kit_item_or_422(session, sku)
    _validate_location(session, location_code)
    lot_number = _optional_str(payload, "lot_number")
    kit_lot = None
    if lot_number is not None:
        kit_lot = _find_lot(session, sku, location_code, lot_number, None)
    else:
        candidates = session.scalars(select(InventoryStockLot).where(
            InventoryStockLot.sku == sku,
            InventoryStockLot.location_code == location_code,
            InventoryStockLot.quantity > 0).order_by(
            InventoryStockLot.expiry_date.asc(),
            InventoryStockLot.id.asc())).all()
        kit_lot = next((l for l in candidates
                        if not _lot_expired(l, _today())), None)
    if kit_lot is None or kit_lot.quantity < quantity:
        available = kit_lot.quantity if kit_lot is not None else 0
        raise HTTPException(
            status_code=409,
            detail=f"Disassembling kit '{sku}' at '{location_code}': "
                   f"needs {quantity} but only {available} available "
                   "— refused, nothing written")
    if (getattr(kit_lot, "stage", None) or "released") == "recalled":
        raise HTTPException(
            status_code=409,
            detail=f"Disassembling kit '{sku}' at '{location_code}': "
                   f"lot '{kit_lot.lot_number}' is recalled and "
                   "cannot be disassembled — refused, nothing written")
    entries = _composition_entries(kit_lot, item)
    kit_stage = getattr(kit_lot, "stage", None) or "released"
    kit_qty_before = kit_lot.quantity
    # Plan from the snapshot BEFORE any write (see kit_open): the
    # movement keeps the PRE-operation snapshot, the lot's stored
    # composition is persisted post-operation.
    credit_plan, remaining_entries = _kit_credit_plan(
        entries, quantity, kit_qty_before)
    movements = []
    kit_lot.quantity -= quantity
    kit_lot.composition = remaining_entries
    session.flush()
    movements.append(_write_movement(
        session, event="kit_disassemble", item=item, lot=kit_lot,
        delta=-quantity, actor=user.username,
        detail={"composition": entries}))
    lots_out = []
    for comp_sku, entry, amount in credit_plan:
        lots_out.append(_credit_component(
            session, comp_sku=comp_sku, location_code=location_code,
            entry=entry, amount=amount, kit_stage=kit_stage,
            event="kit_disassemble", actor=user.username, kit_sku=sku,
            kit_lot_number=kit_lot.lot_number, movements=movements))
    append_audit(session, actor=user.username,
                 action="inventory.kit.disassemble",
                 entity=f"InventoryItem/{sku}",
                 payload={"location_code": location_code,
                          "lot_number": kit_lot.lot_number,
                          "quantity": quantity,
                          "entry_ids": [m.entry_id for m in movements]})
    session.commit()
    return {"sku": sku, "location_code": location_code,
            "quantity": quantity,
            "kit_lot": _lot_out(session, kit_lot, item),
            "movements": [_movement_out(m) for m in movements],
            "lots": [_lot_out(session, l) for l in lots_out]}


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

def _container_out(session: Session, cont: InventoryContainer) -> dict:
    lot = None
    stmt = select(InventoryStockLot).where(
        InventoryStockLot.sku == cont.sku,
        InventoryStockLot.location_code == cont.location_code,
        InventoryStockLot.lot_number == cont.lot_number)
    if cont.serial is None:
        stmt = stmt.where(InventoryStockLot.serial.is_(None))
    else:
        stmt = stmt.where(InventoryStockLot.serial == cont.serial)
    lot = session.scalars(stmt).first()
    return {
        "container_code": cont.container_code, "sku": cont.sku,
        "location_code": cont.location_code,
        "lot_number": cont.lot_number, "serial": cont.serial,
        "quantity": cont.quantity, "stage": cont.stage,
        "opened_at": _aware(cont.opened_at).isoformat()
        if cont.opened_at else None,
        "opened_by": cont.opened_by,
        "in_use_expiry_date": cont.in_use_expiry_date,
        "effective_expiry": _container_effective_expiry(cont, lot),
        "lot_stage": (getattr(lot, "stage", None) or "released")
        if lot is not None else None,
        "disposal_reason_code": cont.disposal_reason_code,
        "disposed_by": cont.disposed_by,
        "disposed_at": _aware(cont.disposed_at).isoformat()
        if cont.disposed_at else None,
        "created_at": _aware(cont.created_at).isoformat()
        if cont.created_at else None,
        "updated_at": _aware(cont.updated_at).isoformat()
        if cont.updated_at else None,
    }


def _container_or_404(session: Session,
                      container_code: str) -> InventoryContainer:
    cont = session.scalars(select(InventoryContainer).where(
        InventoryContainer.container_code == container_code)).first()
    if cont is None:
        raise HTTPException(
            status_code=404,
            detail=f"Container '{container_code}' was not found")
    return cont


def _lot_for_container(session: Session,
                       cont: InventoryContainer) -> InventoryStockLot:
    lot = _find_lot(session, cont.sku, cont.location_code,
                    cont.lot_number, cont.serial)
    if lot is None:
        raise HTTPException(
            status_code=404,
            detail=f"Lot '{cont.lot_number}' of '{cont.sku}' at "
                   f"'{cont.location_code}' was not found")
    return lot


@router.get("/containers")
def list_containers(sku: str | None = Query(default=None),
                    location_code: str | None = Query(default=None),
                    lot_number: str | None = Query(default=None),
                    stage: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(
                        require_capability("inventory.read"))):
    stmt = select(InventoryContainer).order_by(InventoryContainer.id.asc())
    if sku:
        stmt = stmt.where(InventoryContainer.sku == sku)
    if location_code:
        stmt = stmt.where(InventoryContainer.location_code == location_code)
    if lot_number:
        stmt = stmt.where(InventoryContainer.lot_number == lot_number)
    if stage:
        stmt = stmt.where(InventoryContainer.stage == stage)
    rows = session.scalars(stmt).all()
    return {"items": [_container_out(session, r) for r in rows],
            "total": len(rows)}


@router.get("/containers/{container_code}")
def get_container(container_code: str, session: Session = Depends(get_db),
                  user: User = Depends(require_capability("inventory.read"))):
    return _container_out(session, _container_or_404(session,
                                                     container_code))


@router.post("/containers/open")
async def container_open(request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(
                             require_capability("inventory.move"))):
    """Open a released container: stamps opened_at/opened_by and
    its in-use expiry (the earlier of shelf expiry and opened date
    + the class policy's open-stability days)."""
    payload = await _json_body(request)
    container_code = _require_str(payload, "container_code")
    cont = _container_or_404(session, container_code)
    if cont.stage != "released":
        raise HTTPException(
            status_code=409,
            detail=f"Container '{container_code}' cannot be opened: "
                   f"its stage is '{cont.stage}' (must be 'released')")
    lot = _lot_for_container(session, cont)
    item = _item_or_404(session, cont.sku)
    policy = _policy_for_item(session, item)
    cont.stage = "in_use"
    cont.opened_at = datetime.now(timezone.utc)
    cont.opened_by = user.username
    cont.in_use_expiry_date = _container_in_use_expiry(lot, policy)
    session.flush()
    movement = _write_movement(
        session, event="container_open", item=item, lot=lot, delta=0,
        actor=user.username, container_code=container_code,
        detail={"container_code": container_code,
                "in_use_expiry_date": cont.in_use_expiry_date})
    append_audit(session, actor=user.username,
                 action="inventory.container.open",
                 entity=f"InventoryContainer/{container_code}",
                 payload={"sku": cont.sku,
                          "location_code": cont.location_code,
                          "lot_number": cont.lot_number,
                          "entry_id": movement.entry_id})
    session.commit()
    out = _container_out(session, cont)
    out["movement"] = _movement_out(movement)
    return out


@router.post("/containers/consume")
async def container_consume(request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(
                                require_capability("inventory.move"))):
    """Consume from an OPEN (in_use) container at the bench: the
    container and its lot decrement together, never below zero; an
    emptied container closes as depleted."""
    payload = await _json_body(request)
    container_code = _require_str(payload, "container_code")
    quantity = _int_field(payload, "quantity", required=True)
    if quantity is None or quantity <= 0:
        raise HTTPException(status_code=422,
                            detail="'quantity' must be > 0")
    cont = _container_or_404(session, container_code)
    if cont.stage != "in_use":
        raise HTTPException(
            status_code=409,
            detail=f"Container '{container_code}' cannot be "
                   f"consumed: its stage is '{cont.stage}' — the "
                   "container is not opened (must be 'in_use')")
    if quantity > cont.quantity:
        raise HTTPException(
            status_code=409,
            detail=f"Container '{container_code}': consuming "
                   f"{quantity} exceeds the {cont.quantity} in the "
                   "container — refused, nothing written")
    lot = _lot_for_container(session, cont)
    item = _item_or_404(session, cont.sku)
    cont.quantity -= quantity
    if cont.quantity == 0:
        cont.stage = "depleted"
    lot.quantity -= quantity
    session.flush()
    movement = _write_movement(
        session, event="container_consume", item=item, lot=lot,
        delta=-quantity, actor=user.username,
        container_code=container_code,
        detail={"container_code": container_code})
    append_audit(session, actor=user.username,
                 action="inventory.container.consume",
                 entity=f"InventoryContainer/{container_code}",
                 payload={"sku": cont.sku,
                          "location_code": cont.location_code,
                          "quantity": quantity,
                          "entry_id": movement.entry_id})
    session.commit()
    out = _container_out(session, cont)
    out["movement"] = _movement_out(movement)
    return out


@router.post("/containers/dispose")
async def container_dispose(request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(
                                require_capability("inventory.move"))):
    """Dispose a container (cradle-to-grave): the governed disposal
    reason is required, the lot is decremented by the container's
    quantity, and the container records who/when/why."""
    payload = await _json_body(request)
    container_code = _require_str(payload, "container_code")
    reason_code = _require_str(payload, "reason_code")
    note = _optional_str(payload, "note")
    codes = _governed_reason_codes(session, "disposalReasonCodes")
    if reason_code not in codes:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown reason_code '{reason_code}': not in "
                   "the governed disposalReasonCodes list")
    cont = _container_or_404(session, container_code)
    if cont.stage in ("disposed", "depleted"):
        raise HTTPException(
            status_code=409,
            detail=f"Container '{container_code}' is already "
                   f"'{cont.stage}' (a terminal stage) and cannot "
                   "be disposed")
    lot = _lot_for_container(session, cont)
    item = _item_or_404(session, cont.sku)
    old_qty = cont.quantity
    lot.quantity -= old_qty
    cont.quantity = 0
    cont.stage = "disposed"
    cont.disposal_reason_code = reason_code
    cont.disposed_by = user.username
    cont.disposed_at = datetime.now(timezone.utc)
    session.flush()
    movement = _write_movement(
        session, event="stage_transition", item=item, lot=lot,
        delta=-old_qty, actor=user.username, reason_code=reason_code,
        note=note, container_code=container_code,
        detail={"container_code": container_code,
                "to_stage": "disposed", "reason_code": reason_code})
    append_audit(session, actor=user.username,
                 action="inventory.container.dispose",
                 entity=f"InventoryContainer/{container_code}",
                 payload={"sku": cont.sku,
                          "location_code": cont.location_code,
                          "reason_code": reason_code,
                          "quantity": old_qty,
                          "entry_id": movement.entry_id})
    session.commit()
    out = _container_out(session, cont)
    out["movement"] = _movement_out(movement)
    return out


@router.post("/containers/transfer")
async def container_transfer(request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(
                                 require_capability("inventory.move"))):
    """Move a WHOLE container to another stock location (bottles
    move whole; partial-bottle transfer is not a physical
    operation): the container row's location changes, the source
    lot is decremented by the container's quantity and the
    destination lot is found-or-created with the same
    lot_number/serial/expiry, so lot == sum-of-live-containers
    holds at both locations. The ledger gets the existing transfer
    pair idiom (transfer-out at the source with to_location_code
    set, transfer-in at the destination), both stamped with the
    container_code."""
    payload = await _json_body(request)
    container_code = _require_str(payload, "container_code")
    to_location_code = _require_str(payload, "to_location_code")
    cont = _container_or_404(session, container_code)
    if cont.stage in ("disposed", "depleted", "recalled", "expired"):
        raise HTTPException(
            status_code=409,
            detail=f"Container '{container_code}' cannot be "
                   f"transferred: its stage is '{cont.stage}' (a "
                   "terminal stage) — transfer refused, nothing "
                   "written")
    _validate_location(session, to_location_code)
    if to_location_code == cont.location_code:
        raise HTTPException(
            status_code=422,
            detail=f"Container '{container_code}' is already at "
                   f"'{to_location_code}': 'to_location_code' must "
                   "differ from the container's current location")
    item = _item_or_404(session, cont.sku)
    source_lot = _lot_for_container(session, cont)
    from_location_code = cont.location_code
    container_stage = cont.stage
    quantity = cont.quantity
    dest_lot = _find_lot(session, cont.sku, to_location_code,
                         cont.lot_number, cont.serial)
    if dest_lot is None:
        dest_lot = InventoryStockLot(
            sku=cont.sku, location_code=to_location_code,
            lot_number=cont.lot_number, serial=cont.serial,
            expiry_date=source_lot.expiry_date, quantity=0,
            unit_cost_cents=source_lot.unit_cost_cents,
            stage=container_stage)
        session.add(dest_lot)
        session.flush()
    movements = []
    source_lot.quantity -= quantity
    session.flush()
    movements.append(_write_movement(
        session, event="transfer", item=item, lot=source_lot,
        delta=-quantity, actor=user.username,
        to_location_code=to_location_code,
        container_code=container_code,
        detail={"direction": "out", "to_location": to_location_code,
                "container_code": container_code,
                "container_stage": container_stage}))
    dest_lot.quantity += quantity
    session.flush()
    movements.append(_write_movement(
        session, event="transfer", item=item, lot=dest_lot,
        delta=quantity, actor=user.username,
        container_code=container_code,
        detail={"direction": "in", "from_location": from_location_code,
                "container_code": container_code,
                "container_stage": container_stage}))
    cont.location_code = to_location_code
    session.flush()
    append_audit(session, actor=user.username,
                 action="inventory.container.transfer",
                 entity=f"InventoryContainer/{container_code}",
                 payload={"sku": cont.sku,
                          "from_location": from_location_code,
                          "to_location": to_location_code,
                          "quantity": quantity,
                          "container_stage": container_stage,
                          "entry_ids": [m.entry_id for m in movements]})
    session.commit()
    out = _container_out(session, cont)
    out["movements"] = [_movement_out(m) for m in movements]
    return out


# ---------------------------------------------------------------------------
# Requisitions
# ---------------------------------------------------------------------------

def _req_out(row: InventoryRequisition) -> dict:
    return {
        "req_id": row.req_id, "from_location_code": row.from_location_code,
        "to_location_code": row.to_location_code, "status": row.status,
        "lines": row.lines or [], "reference": row.reference,
        "requested_by": row.requested_by, "issued_by": row.issued_by,
        "created_at": _aware(row.created_at).isoformat()
        if row.created_at else None,
        "updated_at": _aware(row.updated_at).isoformat()
        if row.updated_at else None,
    }


def _req_or_404(session: Session, req_id: str) -> InventoryRequisition:
    row = session.scalars(select(InventoryRequisition).where(
        InventoryRequisition.req_id == req_id)).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Requisition '{req_id}' was not found")
    return row


@router.post("/requisitions", status_code=201)
async def create_requisition(request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(
                                 require_capability("inventory.move"))):
    payload = await _json_body(request)
    from_location = _require_str(payload, "from_location_code")
    to_location = _require_str(payload, "to_location_code")
    _validate_location(session, from_location)
    _validate_location(session, to_location)
    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise HTTPException(status_code=422,
                            detail="'lines' must be a non-empty list")
    lines = []
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422,
                                detail="Each requisition line must be "
                                       "an object")
        sku = _require_str(raw, "sku")
        qty = _int_field(raw, "qty_requested", required=True)
        if qty is None or qty <= 0:
            raise HTTPException(
                status_code=422,
                detail=f"Line '{sku}': 'qty_requested' must be > 0")
        if session.scalars(select(InventoryItem).where(
                InventoryItem.sku == sku)).first() is None:
            raise HTTPException(
                status_code=422,
                detail=f"Line sku '{sku}' is not an inventory item")
        lines.append({"sku": sku, "qty_requested": qty, "qty_issued": 0})
    row = InventoryRequisition(
        req_id=f"req-{uuid.uuid4()}", from_location_code=from_location,
        to_location_code=to_location, status="requested", lines=lines,
        reference=_optional_str(payload, "reference") or "",
        requested_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="inventory.requisition.create",
                 entity=f"InventoryRequisition/{row.req_id}",
                 payload={"from_location_code": from_location,
                          "to_location_code": to_location,
                          "lines": lines})
    session.commit()
    return _req_out(row)


@router.get("/requisitions")
def list_requisitions(session: Session = Depends(get_db),
                      user: User = Depends(
                          require_capability("inventory.read"))):
    rows = session.scalars(select(InventoryRequisition).order_by(
        InventoryRequisition.id.asc())).all()
    return {"items": [_req_out(r) for r in rows], "total": len(rows)}


@router.get("/requisitions/{req_id}")
def get_requisition(req_id: str, session: Session = Depends(get_db),
                    user: User = Depends(
                        require_capability("inventory.read"))):
    return _req_out(_req_or_404(session, req_id))


@router.post("/requisitions/{req_id}/issue")
async def issue_requisition(req_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(
                                require_capability("inventory.move"))):
    """Issue against a requisition, FEFO from its from_location.
    Per line the default draw is min(remaining, available) — a
    partial issue is a normal outcome. Explicit quantities in the
    body ({lines: [{sku, quantity}]}): more than the remaining
    request is a 422; more than available is a 409. Movements carry
    reference=req_id; line qty_issued and the status follow."""
    payload = await _json_body(request)
    row = _req_or_404(session, req_id)
    if row.status in ("issued", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail=f"Requisition '{req_id}' is '{row.status}' and "
                   "cannot be issued")
    explicit: dict[str, int] = {}
    raw_lines = payload.get("lines")
    if raw_lines is not None:
        if not isinstance(raw_lines, list):
            raise HTTPException(status_code=422,
                                detail="'lines' must be a list")
        for raw in raw_lines:
            if not isinstance(raw, dict):
                raise HTTPException(status_code=422,
                                    detail="Each issue line must be an "
                                           "object")
            sku = _require_str(raw, "sku")
            qty = _int_field(raw, "quantity", required=True)
            if qty is None or qty <= 0:
                raise HTTPException(
                    status_code=422,
                    detail=f"Line '{sku}': 'quantity' must be > 0")
            explicit[sku] = qty
    lines = [dict(line) for line in row.lines or []]
    known = {line["sku"] for line in lines}
    for sku in explicit:
        if sku not in known:
            raise HTTPException(
                status_code=422,
                detail=f"Issue line sku '{sku}' is not on requisition "
                       f"'{req_id}'")
    # Validate everything before issuing anything.
    plan_by_sku: dict[str, tuple[InventoryItem, list, int]] = {}
    for line in lines:
        sku = line["sku"]
        remaining = line["qty_requested"] - line["qty_issued"]
        if sku in explicit and explicit[sku] > remaining:
            raise HTTPException(
                status_code=422,
                detail=f"Line '{sku}': issuing {explicit[sku]} exceeds "
                       f"the remaining requested {remaining}")
        if remaining <= 0:
            continue
        item = _item_or_404(session, sku)
        _plan, available = _fefo_plan(session, sku,
                                      row.from_location_code, 0)
        if sku in explicit:
            if explicit[sku] > available:
                raise HTTPException(
                    status_code=409,
                    detail=f"Line '{sku}': issuing {explicit[sku]} "
                           f"exceeds the {available} available at "
                           f"'{row.from_location_code}'"
                           + _blocked_suffix(session, sku,
                                             row.from_location_code))
            target = explicit[sku]
        else:
            target = min(remaining, available)
        plan, _avail = _fefo_plan(session, sku, row.from_location_code,
                                  target)
        plan_by_sku[sku] = (item, plan, target)
    issued_any = False
    for line in lines:
        sku = line["sku"]
        if sku not in plan_by_sku:
            continue
        item, plan, target = plan_by_sku[sku]
        if target <= 0:
            continue
        _apply_draw(session, item=item, plan=plan, event="issue",
                    actor=user.username, reference=req_id)
        line["qty_issued"] += target
        issued_any = True
    row.lines = lines
    if all(line["qty_issued"] >= line["qty_requested"] for line in lines):
        row.status = "issued"
    elif any(line["qty_issued"] > 0 for line in lines):
        row.status = "partially_issued"
    if issued_any:
        row.issued_by = user.username
    session.flush()
    append_audit(session, actor=user.username,
                 action="inventory.requisition.issue",
                 entity=f"InventoryRequisition/{req_id}",
                 payload={"status": row.status,
                          "lines": [{"sku": l["sku"],
                                     "qty_issued": l["qty_issued"]}
                                    for l in lines]})
    session.commit()
    return _req_out(row)


@router.post("/requisitions/{req_id}/cancel")
def cancel_requisition(req_id: str, session: Session = Depends(get_db),
                       user: User = Depends(
                           require_capability("inventory.move"))):
    """Cancel a requisition — only while nothing has been issued."""
    row = _req_or_404(session, req_id)
    if row.status == "cancelled":
        raise HTTPException(status_code=409,
                            detail=f"Requisition '{req_id}' is already "
                                   "cancelled")
    if any((line.get("qty_issued") or 0) > 0 for line in row.lines or []):
        raise HTTPException(
            status_code=409,
            detail=f"Requisition '{req_id}' has issued lines and "
                   "cannot be cancelled")
    row.status = "cancelled"
    session.flush()
    append_audit(session, actor=user.username,
                 action="inventory.requisition.cancel",
                 entity=f"InventoryRequisition/{req_id}", payload={})
    session.commit()
    return _req_out(row)


# ---------------------------------------------------------------------------
# Cycle counts
# ---------------------------------------------------------------------------

def _count_out(row: InventoryCycleCount) -> dict:
    return {
        "count_id": row.count_id, "location_code": row.location_code,
        "status": row.status, "lines": row.lines or [],
        "reason_code": row.reason_code, "counted_by": row.counted_by,
        "created_by": row.created_by,
        "created_at": _aware(row.created_at).isoformat()
        if row.created_at else None,
        "updated_at": _aware(row.updated_at).isoformat()
        if row.updated_at else None,
    }


def _count_or_404(session: Session, count_id: str) -> InventoryCycleCount:
    row = session.scalars(select(InventoryCycleCount).where(
        InventoryCycleCount.count_id == count_id)).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Cycle count '{count_id}' was not found")
    return row


@router.post("/counts", status_code=201)
async def create_count(request: Request, session: Session = Depends(get_db),
                       user: User = Depends(
                           require_capability("inventory.manage"))):
    """Open a cycle count at a location: snapshot the expected
    quantities from the current lot rows (one line per lot row)."""
    payload = await _json_body(request)
    location_code = _require_str(payload, "location_code")
    _validate_location(session, location_code)
    lots = session.scalars(select(InventoryStockLot).where(
        InventoryStockLot.location_code == location_code).order_by(
        InventoryStockLot.sku.asc(), InventoryStockLot.id.asc())).all()
    lines = [{"sku": lot.sku, "lot_number": lot.lot_number,
              "serial": lot.serial, "expected": lot.quantity,
              "counted": None, "variance": None} for lot in lots]
    row = InventoryCycleCount(count_id=f"count-{uuid.uuid4()}",
                              location_code=location_code, status="open",
                              lines=lines, created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="inventory.count.create",
                 entity=f"InventoryCycleCount/{row.count_id}",
                 payload={"location_code": location_code,
                          "lines": len(lines)})
    session.commit()
    return _count_out(row)


@router.get("/counts")
def list_counts(session: Session = Depends(get_db),
                user: User = Depends(require_capability("inventory.read"))):
    rows = session.scalars(select(InventoryCycleCount).order_by(
        InventoryCycleCount.id.asc())).all()
    return {"items": [_count_out(r) for r in rows], "total": len(rows)}


@router.get("/counts/{count_id}")
def get_count(count_id: str, session: Session = Depends(get_db),
              user: User = Depends(require_capability("inventory.read"))):
    return _count_out(_count_or_404(session, count_id))


@router.post("/counts/{count_id}/complete")
async def complete_count(count_id: str, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(
                             require_capability("inventory.manage"))):
    """Complete a count. The counted lines must cover EVERY snapshot
    line (a missing line is a 422). When any variance is non-zero a
    published reason_code is required (422 otherwise). Variances are
    applied as count_adjust movements and the lot quantities are set
    to the counted values; a balanced count writes no movements."""
    payload = await _json_body(request)
    row = _count_or_404(session, count_id)
    if row.status != "open":
        raise HTTPException(
            status_code=409,
            detail=f"Cycle count '{count_id}' is '{row.status}' and "
                   "cannot be completed")
    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, list):
        raise HTTPException(status_code=422,
                            detail="'lines' must be a list")
    counted_by_key: dict[tuple[str, str, str | None], int] = {}
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422,
                                detail="Each count line must be an object")
        sku = _require_str(raw, "sku")
        lot_number = raw.get("lot_number") or ""
        if not isinstance(lot_number, str):
            raise HTTPException(status_code=422,
                                detail="'lot_number' must be a string")
        serial = raw.get("serial")
        if serial is not None and not isinstance(serial, str):
            raise HTTPException(status_code=422,
                                detail="'serial' must be a string")
        counted = _int_field(raw, "counted", required=True, minimum=0)
        counted_by_key[(sku, lot_number, serial)] = counted
    snapshot = [dict(line) for line in row.lines or []]
    for line in snapshot:
        key = (line["sku"], line["lot_number"], line.get("serial"))
        if key not in counted_by_key:
            raise HTTPException(
                status_code=422,
                detail=f"Counted lines are missing the snapshot line "
                       f"for sku '{line['sku']}' lot "
                       f"'{line['lot_number']}' — a completion must "
                       "cover every snapshot line")
        line["counted"] = counted_by_key[key]
        line["variance"] = line["counted"] - line["expected"]
    reason_code = _optional_str(payload, "reason_code")
    if any(line["variance"] != 0 for line in snapshot):
        if reason_code is None or reason_code not in _reason_codes(session):
            raise HTTPException(
                status_code=422,
                detail="Completing a count with variances requires a "
                       "'reason_code' from the published inventory "
                       "taxonomy reason codes")
    # Apply.
    for line in snapshot:
        if line["variance"] == 0:
            continue
        item = _item_or_404(session, line["sku"])
        lot = _find_lot(session, line["sku"], row.location_code,
                        line["lot_number"], line.get("serial"))
        if lot is None:
            lot = InventoryStockLot(sku=line["sku"],
                                    location_code=row.location_code,
                                    lot_number=line["lot_number"],
                                    serial=line.get("serial"),
                                    quantity=0)
            session.add(lot)
            session.flush()
        lot.quantity = line["counted"]
        session.flush()
        _write_movement(session, event="count_adjust", item=item, lot=lot,
                        delta=line["variance"], actor=user.username,
                        reference=count_id, reason_code=reason_code)
    row.lines = snapshot
    row.status = "completed"
    row.reason_code = reason_code
    row.counted_by = user.username
    session.flush()
    append_audit(session, actor=user.username,
                 action="inventory.count.complete",
                 entity=f"InventoryCycleCount/{count_id}",
                 payload={"location_code": row.location_code,
                          "variances": sum(1 for l in snapshot
                                           if l["variance"] != 0),
                          "reason_code": reason_code})
    session.commit()
    return _count_out(row)


@router.post("/counts/{count_id}/cancel")
def cancel_count(count_id: str, session: Session = Depends(get_db),
                 user: User = Depends(
                     require_capability("inventory.manage"))):
    row = _count_or_404(session, count_id)
    if row.status != "open":
        raise HTTPException(
            status_code=409,
            detail=f"Cycle count '{count_id}' is '{row.status}' and "
                   "cannot be cancelled")
    row.status = "cancelled"
    session.flush()
    append_audit(session, actor=user.username,
                 action="inventory.count.cancel",
                 entity=f"InventoryCycleCount/{count_id}", payload={})
    session.commit()
    return _count_out(row)


# ---------------------------------------------------------------------------
# Case draw (preference cards) + implant trace
# ---------------------------------------------------------------------------

@router.post("/case-issues")
async def case_issue(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(
                         require_capability("inventory.move"))):
    """Draw a case's supplies from a surgeon's preference card
    (app.surgery_depth.PreferenceCard). EVERY card line's code must
    be an inventory item sku (missing -> 409 naming the code) and
    fully FEFO-coverable at the location (any shortfall -> 409
    naming all shortfalls) — validate-all-then-apply, so a refused
    draw writes NOTHING. On success each lot drawn writes a
    case_issue movement with reference "Procedure/<case_id>"."""
    payload = await _json_body(request)
    card_id = _require_str(payload, "card_id")
    case_id = _require_str(payload, "case_id")
    location_code = _require_str(payload, "location_code")
    patient_ref = _optional_str(payload, "patient_ref")
    card = session.scalars(select(PreferenceCard).where(
        PreferenceCard.card_id == card_id)).first()
    if card is None:
        raise HTTPException(
            status_code=404,
            detail=f"Preference card '{card_id}' was not found")
    _validate_location(session, location_code)
    # ---- validation / planning (nothing written) ----
    reserved: dict[int, int] = {}
    planned: list[tuple[dict, InventoryItem, list]] = []
    missing: list[str] = []
    shortfalls: list[str] = []
    for line in card.items or []:
        if not isinstance(line, dict):
            continue
        code = line.get("code")
        qty = line.get("qty")
        if not code or not isinstance(qty, int) or isinstance(qty, bool) \
                or qty <= 0:
            continue
        item = session.scalars(select(InventoryItem).where(
            InventoryItem.sku == code)).first()
        if item is None:
            missing.append(str(code))
            continue
        plan, available = _fefo_plan(session, code, location_code, qty,
                                     reserved=reserved)
        if available < qty:
            shortfalls.append(
                f"'{code}': needs {qty} but only {available} available "
                f"at '{location_code}'"
                + _blocked_suffix(session, code, location_code))
            continue
        for lot, take in plan:
            reserved[lot.id] = reserved.get(lot.id, 0) + take
        planned.append((line, item, plan))
    if missing:
        raise HTTPException(
            status_code=409,
            detail="Case draw refused: preference card lines are not "
                   "inventory items: " + ", ".join(missing))
    if shortfalls:
        raise HTTPException(
            status_code=409,
            detail="Case draw refused, shortfalls: "
                   + "; ".join(shortfalls))
    # ---- apply ----
    reference = f"Procedure/{case_id}"
    out_lines = []
    entry_ids = []
    for line, item, plan in planned:
        detail = {"card_id": card.card_id, "surgeon": card.surgeon,
                  "procedure_code": card.procedure_code,
                  "kind": line.get("kind")}
        movements, allocations = _apply_draw(
            session, item=item, plan=plan, event="case_issue",
            actor=user.username, reference=reference,
            patient_ref=patient_ref, detail=detail)
        entry_ids.extend(m.entry_id for m in movements)
        out_lines.append({"sku": item.sku, "code": item.sku,
                          "kind": line.get("kind"), "qty": line["qty"],
                          "allocations": allocations})
    append_audit(session, actor=user.username, action="inventory.case_issue",
                 entity=reference,
                 payload={"card_id": card_id, "case_id": case_id,
                          "location_code": location_code,
                          "patient_ref": patient_ref,
                          "entry_ids": entry_ids})
    session.commit()
    return {"case_id": case_id, "card_id": card_id, "lines": out_lines}


@router.get("/case-issues")
def list_case_issues(case_id: str = Query(...),
                     session: Session = Depends(get_db),
                     user: User = Depends(
                         require_capability("inventory.read"))):
    """The case_issue movements for one case, grouped by sku."""
    reference = f"Procedure/{case_id}"
    rows = session.scalars(select(InventoryMovement).where(
        InventoryMovement.event == "case_issue",
        InventoryMovement.reference == reference).order_by(
        InventoryMovement.id.asc())).all()
    grouped: dict[str, dict] = {}
    for movement in rows:
        line = grouped.setdefault(movement.sku, {
            "sku": movement.sku, "qty": 0, "allocations": []})
        qty = -movement.quantity
        line["qty"] += qty
        line["allocations"].append({
            "lot_number": movement.lot_number, "serial": movement.serial,
            "expiry_date": movement.expiry_date, "qty": qty,
            "unit_cost_cents": movement.unit_cost_cents,
            "value_cents": (-movement.value_cents
                              if movement.value_cents is not None
                              else None)})
    return {"case_id": case_id, "reference": reference,
            "lines": list(grouped.values()),
            "movements": [_movement_out(r) for r in rows],
            "total": len(rows)}


@router.get("/implant-trace")
def implant_trace(lot_number: str | None = Query(default=None),
                  patient_ref: str | None = Query(default=None),
                  sku: str | None = Query(default=None),
                  session: Session = Depends(get_db),
                  user: User = Depends(
                      require_capability("inventory.read"))):
    """Implant traceability in BOTH directions from the one ledger:
    filter by lot_number to see the patients/cases a lot went to, by
    patient_ref to see the lots a patient received, by sku for the
    item's whole trace. Only movements of items in the "implants"
    category (receive + case_issue events) are returned."""
    implant_skus = {i.sku for i in session.scalars(
        select(InventoryItem).where(
            InventoryItem.category == "implants")).all()}
    stmt = select(InventoryMovement).where(
        InventoryMovement.event.in_(("case_issue", "receive"))).order_by(
        InventoryMovement.id.asc())
    if lot_number:
        stmt = stmt.where(InventoryMovement.lot_number == lot_number)
    if patient_ref:
        stmt = stmt.where(InventoryMovement.patient_ref == patient_ref)
    if sku:
        stmt = stmt.where(InventoryMovement.sku == sku)
    rows = [r for r in session.scalars(stmt).all()
            if r.sku in implant_skus]
    return {"entries": [_movement_out(r) for r in rows]}


# ---------------------------------------------------------------------------
# Identification: barcode / RFID scan resolution
# ---------------------------------------------------------------------------

def _stock_for_item(session: Session, sku: str) -> list[dict]:
    rows = session.scalars(select(InventoryStockLot).where(
        InventoryStockLot.sku == sku).order_by(
        InventoryStockLot.location_code.asc(),
        InventoryStockLot.id.asc())).all()
    return [_lot_out(session, r) for r in rows]


def _resolution(session: Session, via: str, item: InventoryItem,
                lot: InventoryStockLot | None) -> dict:
    return {"resolved": True, "via": via, "item": _item_out(item),
            "lot": _lot_out(session, lot, item) if lot is not None
            else None,
            "stock": _stock_for_item(session, item.sku)}


def _resolve_entity_ref(session: Session, entity_ref: str):
    """Resolve an "InventoryItem/<sku>" / "InventoryLot/<lot id>"
    entity_ref to (item, lot|None), else None."""
    if "/" not in entity_ref:
        return None
    head, tail = entity_ref.split("/", 1)
    if head == "InventoryItem":
        item = session.scalars(select(InventoryItem).where(
            InventoryItem.sku == tail)).first()
        return (item, None) if item is not None else None
    if head == "InventoryLot":
        try:
            lot_id = int(tail)
        except ValueError:
            return None
        lot = session.scalars(select(InventoryStockLot).where(
            InventoryStockLot.id == lot_id)).first()
        if lot is None:
            return None
        item = session.scalars(select(InventoryItem).where(
            InventoryItem.sku == lot.sku)).first()
        return (item, lot) if item is not None else None
    return None


def _resolve_scan(session: Session, value: str):
    """The resolution order: (1) barcode registry, (2) RFID registry,
    (3) exact sku, (4) exact lot_number / serial, (5) pipe payload.
    Returns (via, item, lot) or None."""
    record = session.scalars(select(BarcodeRecord).where(
        BarcodeRecord.value == value)).first()
    if record is not None:
        resolved = _resolve_entity_ref(session, record.entity_ref)
        if resolved is not None:
            return ("barcode", resolved[0], resolved[1])
    tag = session.scalars(select(RfidTag).where(
        RfidTag.epc == value)).first()
    if tag is not None and tag.status == "bound" \
            and tag.entity_type and tag.entity_id:
        resolved = _resolve_entity_ref(
            session, f"{tag.entity_type}/{tag.entity_id}")
        if resolved is not None:
            return ("rfid", resolved[0], resolved[1])
    item = session.scalars(select(InventoryItem).where(
        InventoryItem.sku == value)).first()
    if item is not None:
        return ("sku", item, None)
    lot = session.scalars(select(InventoryStockLot).where(
        InventoryStockLot.serial == value)).first()
    if lot is None and value:
        lot = session.scalars(select(InventoryStockLot).where(
            InventoryStockLot.lot_number == value,
            InventoryStockLot.lot_number != "")).first()
    if lot is not None:
        item = session.scalars(select(InventoryItem).where(
            InventoryItem.sku == lot.sku)).first()
        if item is not None:
            return ("serial" if lot.serial == value else "lot",
                    item, lot)
    if "|" in value:
        parts = [part.strip() for part in value.split("|")]
        if len(parts) in (2, 3) and parts[0]:
            item = session.scalars(select(InventoryItem).where(
                InventoryItem.sku == parts[0])).first()
            if item is not None:
                lot = None
                if parts[1]:
                    lot = session.scalars(
                        select(InventoryStockLot).where(
                            InventoryStockLot.sku == item.sku,
                            InventoryStockLot.lot_number == parts[1])
                    ).first()
                return ("pipe", item, lot)
    return None


def _record_scan(session: Session, *, kind: str, value: str,
                 resolved_sku: str | None, resolved_lot: str | None,
                 context: str | None, location_code: str | None,
                 actor: str, detail: dict) -> InventoryScanEvent:
    row = InventoryScanEvent(
        scan_id=f"scan-{uuid.uuid4()}", kind=kind, value=value,
        resolved_sku=resolved_sku, resolved_lot_number=resolved_lot,
        context=context, location_code=location_code, actor=actor,
        detail=detail)
    session.add(row)
    session.flush()
    return row


@router.post("/scan")
async def scan(request: Request, session: Session = Depends(get_db),
               user: User = Depends(require_capability("inventory.read"))):
    """Resolve one presented value (barcode / RFID EPC / sku / lot /
    serial / pipe payload) to its inventory item + lot + stock
    snapshot. Every scan — resolved or not — appends an
    InventoryScanEvent. Unresolved is an honest 404."""
    payload = await _json_body(request)
    value = _require_str(payload, "value")
    context = _optional_str(payload, "context")
    location_code = _optional_str(payload, "location_code")
    resolved = _resolve_scan(session, value)
    if resolved is None:
        _record_scan(session, kind="barcode", value=value,
                     resolved_sku=None, resolved_lot=None,
                     context=context, location_code=location_code,
                     actor=user.username,
                     detail={"resolved": False, "context": context})
        session.commit()
        raise HTTPException(
            status_code=404,
            detail=f"'{value}' did not resolve to any inventory item, "
                   "lot or serial")
    via, item, lot = resolved
    _record_scan(session, kind="barcode", value=value,
                 resolved_sku=item.sku,
                 resolved_lot=lot.lot_number if lot is not None else None,
                 context=context, location_code=location_code,
                 actor=user.username,
                 detail={"resolved": True, "via": via,
                         "context": context})
    append_audit(session, actor=user.username, action="inventory.scan",
                 entity=f"InventoryItem/{item.sku}",
                 payload={"value": value, "via": via,
                          "context": context})
    session.commit()
    return _resolution(session, via, item, lot)


@router.post("/rfid-events")
async def rfid_event(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(
                         require_capability("inventory.move"))):
    """One RFID sighting routed to inventory: resolve the EPC
    through the app.rfid registry; a tag bound to an inventory
    entity records an InventoryScanEvent (kind rfid) and returns
    the resolution + stock snapshot. An unknown EPC or a tag not
    bound to an inventory entity is an honest 404."""
    payload = await _json_body(request)
    epc = _require_str(payload, "epc")
    context = _optional_str(payload, "context")
    location_code = _optional_str(payload, "location_code")
    tag = session.scalars(select(RfidTag).where(
        RfidTag.epc == epc)).first()
    if tag is None:
        raise HTTPException(
            status_code=404,
            detail=f"EPC '{epc}' is not a registered RFID tag")
    if tag.status != "bound" or not tag.entity_type or not tag.entity_id:
        raise HTTPException(
            status_code=404,
            detail=f"EPC '{epc}' is registered but not bound to any "
                   "entity")
    resolved = _resolve_entity_ref(
        session, f"{tag.entity_type}/{tag.entity_id}")
    if resolved is None:
        raise HTTPException(
            status_code=404,
            detail=f"EPC '{epc}' is bound to "
                   f"'{tag.entity_type}/{tag.entity_id}', which is "
                   "not an inventory entity")
    item, lot = resolved
    _record_scan(session, kind="rfid", value=epc, resolved_sku=item.sku,
                 resolved_lot=lot.lot_number if lot is not None else None,
                 context=context, location_code=location_code,
                 actor=user.username,
                 detail={"resolved": True, "via": "rfid",
                         "reader_id": payload.get("reader_id"),
                         "zone": payload.get("zone"),
                         "context": context})
    append_audit(session, actor=user.username, action="inventory.rfid_event",
                 entity=f"InventoryItem/{item.sku}",
                 payload={"epc": epc,
                          "reader_id": payload.get("reader_id"),
                          "zone": payload.get("zone")})
    session.commit()
    out = _resolution(session, "rfid", item, lot)
    out["epc"] = epc
    return out
