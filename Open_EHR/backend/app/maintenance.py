"""Phase 16 (WS-I) — HTM / biomedical maintenance: work orders +
preventive maintenance on the Phase 14 device registry.

What lives here:

- **Work orders** (`WorkOrder`, this module's own table) against the
  EXISTING device registry (`app.models.Device`, seeded and governed
  by `app.devices`) — there is deliberately NO second registry here.
  A work order is corrective or preventive, prioritised
  stat | urgent | routine, and moves through a GOVERNED lifecycle
  opened -> assigned -> in-progress -> completed -> closed
  (opened may also go straight to in-progress). Every other move is
  refused 409; in particular an order can only be CLOSED from
  completed, i.e. closing requires completion. Every mutation writes
  a hash-chained audit entry in the same transaction.

- **Preventive maintenance.** PM intervals per device type are
  published Studio config (`terminology/maintenance`:
  `{"deviceTypes": {"<type>": {"pmIntervalDays": N}},
  "downtimeStatus": "out-of-service"}` — seeded published by
  :func:`ensure_maintenance_defaults`, mirroring the
  `maintenanceIntervalDays` values in `terminology/devices`).
  `POST /maintenance/pm/generate` creates ONE preventive work order
  per due device. A device is due when its last completed PM is
  older than the interval. BASELINE CHOICE (stated per the WS-I
  contract): when a device has never had a PM, the baseline is
  `Device.last_calibrated_at` when recorded — it is the registry's
  only stored hands-on service-event timestamp — falling back to
  `Device.created_at` (registration / install). Generation is
  idempotent: a device with an OPEN preventive order, or a PM
  completed within the interval, gets no new order, so a second
  generate in the same period creates none. Completing a PM order
  also advances `Device.maintenance_due` to completed_at + interval
  (the `app.devices.record_calibration` pattern).

- **Downtime.** Opening a work order with `downtime: true` sets the
  registry device's `status` to the configured downtime status
  (published `terminology/maintenance` `downtimeStatus`, default
  `"out-of-service"` — a value in the published `terminology/devices`
  status set, so the ingestion gate treats the device as blocked
  while it is down). Completing that order restores the device to
  `"active"` — unless another downtime order for the same device is
  still open. Both flips are audited (`maintenance.downtime.start` /
  `maintenance.downtime.end`) as well as in the work-order audit
  entries, so the registry's downtime state is always explainable
  from the audit chain.

- **Worklist.** `GET /maintenance/worklist` mirrors the Phase 14
  calibration-worklist shape ({items, total, ...}): open orders
  only, priority-ordered (stat, then urgent, then routine; oldest
  first within a priority), with preventive orders whose PM due
  date has passed flagged `overdue: true`.

Capabilities: `maintenance.read` / `maintenance.work`.
Synthetic data only; no external calls.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (Boolean, DateTime, Integer, String, select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import ConfigStatus, Device, StudioConfig, User, utcnow
from .security import get_db, require_capability

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

MAINTENANCE_KEY = "maintenance"      # terminology/maintenance
WORK_TYPES = ("corrective", "preventive")
PRIORITIES = ("stat", "urgent", "routine")
PRIORITY_RANK = {name: rank for rank, name in enumerate(PRIORITIES)}
STATUSES = ("opened", "assigned", "in-progress", "completed", "closed")
OPEN_STATUSES = ("opened", "assigned", "in-progress")
# The governed lifecycle. Anything not listed here is a 409.
TRANSITIONS = {
    "opened": {"assigned", "in-progress"},
    "assigned": {"in-progress"},
    "in-progress": {"completed"},
    "completed": {"closed"},
    "closed": set(),
}
DEFAULT_DOWNTIME_STATUS = "out-of-service"
RESTORED_STATUS = "active"


# ---------------------------------------------------------------------------
# This module's own table
# ---------------------------------------------------------------------------

class WorkOrder(Base):
    """One HTM work order against one registry device.

    `device_id` carries the registry `Device.device_key` (the
    registry's public identity everywhere in app.devices)."""
    __tablename__ = "maintenance_work_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_order_id: Mapped[str] = mapped_column(String(40), unique=True,
                                               nullable=False, index=True)
    device_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                           index=True)
    type: Mapped[str] = mapped_column(String(20), nullable=False,
                                      index=True)
    priority: Mapped[str] = mapped_column(String(20), nullable=False,
                                          default="routine", index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="opened", index=True)
    assignee: Mapped[str | None] = mapped_column(String(200), nullable=True)
    downtime: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                           default=False)
    reason: Mapped[str] = mapped_column(String(600), nullable=False,
                                        default="")
    opened_by: Mapped[str] = mapped_column(String(200), nullable=False,
                                           default="")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    completion_note: Mapped[str] = mapped_column(String(600), nullable=False,
                                                 default="")
    pm_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


# ---------------------------------------------------------------------------
# Published config: terminology/maintenance
# ---------------------------------------------------------------------------

def default_maintenance_payload() -> dict:
    return {
        "name": "HTM maintenance: PM intervals + downtime status "
                "(synthetic defaults)",
        # PM intervals per device type — mirrors maintenanceIntervalDays
        # in the published terminology/devices document.
        "deviceTypes": {
            "analyser": {"pmIntervalDays": 365},
            "vitals-monitor": {"pmIntervalDays": 180},
            "fridge-sensor": {"pmIntervalDays": 365},
            "pump": {"pmIntervalDays": 180},
            "poct-analyser": {"pmIntervalDays": 365},
        },
        # Registry status a device takes while a downtime work order
        # is open (a value of the published terminology/devices
        # status set; also a blocking status there).
        "downtimeStatus": DEFAULT_DOWNTIME_STATUS,
    }


def maintenance_config(session: Session) -> dict:
    cfg = published_config(session, "terminology", MAINTENANCE_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    return payload if payload.get("deviceTypes") \
        else default_maintenance_payload()


def _pm_interval_days(cfg: dict, device_type: str) -> int | None:
    entry = (cfg.get("deviceTypes") or {}).get(device_type)
    if isinstance(entry, dict):
        days = entry.get("pmIntervalDays")
        if isinstance(days, int) and not isinstance(days, bool) and days > 0:
            return days
    return None


def _downtime_status(cfg: dict) -> str:
    value = cfg.get("downtimeStatus")
    return value if isinstance(value, str) and value.strip() \
        else DEFAULT_DOWNTIME_STATUS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_dt(value, field: str) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be an ISO date/datetime")
    text = value.strip()
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        return _aware(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"'{field}' is not a valid ISO date/datetime: '{value}'")


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


def _device_row(session: Session, device_id: str) -> Device:
    row = session.scalars(
        select(Device).where(Device.device_key == device_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Device '{device_id}' not found")
    return row


def _order_row(session: Session, work_order_id: str) -> WorkOrder:
    row = session.scalars(select(WorkOrder).where(
        WorkOrder.work_order_id == work_order_id)).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Work order '{work_order_id}' not found")
    return row


def _is_overdue(order: WorkOrder, at: datetime) -> bool:
    """An open preventive order is overdue once its PM due date has
    passed. Corrective orders are not PM-overdue."""
    return bool(order.type == "preventive"
                and order.status in OPEN_STATUSES
                and order.pm_due_at is not None
                and _aware(order.pm_due_at) <= at)


def _order_out(order: WorkOrder, at: datetime | None = None) -> dict:
    at = at or _now()
    return {
        "work_order_id": order.work_order_id,
        "device_id": order.device_id,
        "device_key": order.device_id,
        "type": order.type,
        "priority": order.priority,
        "status": order.status,
        "assignee": order.assignee,
        "downtime": bool(order.downtime),
        "reason": order.reason,
        "opened_by": order.opened_by,
        "opened_at": _aware(order.opened_at).isoformat()
        if order.opened_at else None,
        "completed_at": _aware(order.completed_at).isoformat()
        if order.completed_at else None,
        "completion_note": order.completion_note,
        "pm_due_at": _aware(order.pm_due_at).isoformat()
        if order.pm_due_at else None,
        "overdue": _is_overdue(order, at),
    }


def _pm_baseline(session: Session, device: Device) -> datetime | None:
    """The instant the device's PM clock last reset: the latest
    completed preventive work order's completion, else the baseline
    (device.last_calibrated_at, else device.created_at)."""
    last_pm = session.scalars(
        select(WorkOrder).where(
            WorkOrder.device_id == device.device_key,
            WorkOrder.type == "preventive",
            WorkOrder.status.in_(("completed", "closed")),
            WorkOrder.completed_at.is_not(None))
        .order_by(WorkOrder.completed_at.desc())).first()
    if last_pm is not None:
        return _aware(last_pm.completed_at)
    return _aware(device.last_calibrated_at) or _aware(device.created_at)


def _pm_due_at(session: Session, cfg: dict, device: Device) -> datetime | None:
    interval = _pm_interval_days(cfg, device.device_type)
    baseline = _pm_baseline(session, device)
    if interval is None or baseline is None:
        return None
    return baseline + timedelta(days=interval)


def _open_pm_exists(session: Session, device_key: str) -> bool:
    return session.scalars(
        select(WorkOrder).where(
            WorkOrder.device_id == device_key,
            WorkOrder.type == "preventive",
            WorkOrder.status.in_(OPEN_STATUSES))).first() is not None


def _start_downtime(session: Session, cfg: dict, device: Device,
                    order: WorkOrder, actor: str) -> None:
    """Flip the registry device to the configured downtime status,
    audited as its own entry so the registry state is explainable."""
    target = _downtime_status(cfg)
    previous = device.status
    device.status = target
    session.flush()
    append_audit(session, actor=actor, action="maintenance.downtime.start",
                 entity=f"Device/{device.device_key}",
                 payload={"work_order_id": order.work_order_id,
                          "from": previous, "to": target,
                          "reason": order.reason})


def _end_downtime_if_clear(session: Session, cfg: dict, device: Device,
                           order: WorkOrder, actor: str) -> bool:
    """Restore the device to active on downtime completion — unless
    another downtime order for it is still open. Audited either way
    the flip happens; returns True when the flip was made."""
    target = _downtime_status(cfg)
    if device.status != target:
        return False
    other_open = session.scalars(
        select(WorkOrder).where(
            WorkOrder.device_id == device.device_key,
            WorkOrder.downtime.is_(True),
            WorkOrder.status.in_(OPEN_STATUSES),
            WorkOrder.id != order.id)).first()
    if other_open is not None:
        return False
    previous = device.status
    device.status = RESTORED_STATUS
    session.flush()
    append_audit(session, actor=actor, action="maintenance.downtime.end",
                 entity=f"Device/{device.device_key}",
                 payload={"work_order_id": order.work_order_id,
                          "from": previous, "to": RESTORED_STATUS})
    return True


def _create_order(session: Session, *, device: Device, type_: str,
                  priority: str, reason: str, downtime: bool,
                  assignee: str | None, opened_by: str,
                  pm_due_at: datetime | None,
                  opened_at: datetime | None = None) -> WorkOrder:
    order = WorkOrder(device_id=device.device_key, type=type_,
                      priority=priority, status="opened",
                      assignee=assignee, downtime=downtime,
                      reason=reason, opened_by=opened_by,
                      opened_at=opened_at or _now(),
                      pm_due_at=pm_due_at,
                      work_order_id="pending")
    session.add(order)
    session.flush()
    order.work_order_id = f"WO-{order.id:04d}"
    session.flush()
    return order


# ---------------------------------------------------------------------------
# Config + worklist — literal routes BEFORE /work-orders/{id}
# ---------------------------------------------------------------------------

@router.get("/config")
def get_config(session: Session = Depends(get_db),
               user: User = Depends(require_capability("maintenance.read"))):
    cfg = maintenance_config(session)
    return {"maintenance": cfg,
            "downtime_status": _downtime_status(cfg),
            "work_types": list(WORK_TYPES), "priorities": list(PRIORITIES),
            "statuses": list(STATUSES)}


@router.get("/worklist")
def worklist(session: Session = Depends(get_db),
             user: User = Depends(require_capability("maintenance.read"))):
    """Open work orders, worst first (the HTM workqueue view —
    mirrors the Phase 14 calibration worklist shape): stat before
    urgent before routine, oldest first within a priority; overdue
    PM orders are flagged."""
    now = _now()
    rows = session.scalars(
        select(WorkOrder).where(WorkOrder.status.in_(OPEN_STATUSES))).all()
    items = [_order_out(o, now) for o in rows]
    items.sort(key=lambda i: (PRIORITY_RANK.get(i["priority"], 99),
                              i["opened_at"] or ""))
    return {"items": items, "total": len(items),
            "overdue": sum(1 for i in items if i["overdue"]),
            "downtime": sum(1 for i in items if i["downtime"])}


@router.post("/pm/generate")
def generate_pm(session: Session = Depends(get_db),
                user: User = Depends(require_capability("maintenance.work"))):
    """Create ONE preventive work order per due device (see the
    module docstring for the due rule + baseline). Idempotent: an
    open preventive order or a PM completed within the interval
    suppresses creation, so re-running in the same period creates
    none."""
    cfg = maintenance_config(session)
    now = _now()
    created: list[WorkOrder] = []
    checked = due = skipped_open = not_due = unconfigured = 0
    devices = session.scalars(
        select(Device).order_by(Device.device_key.asc())).all()
    for device in devices:
        interval = _pm_interval_days(cfg, device.device_type)
        if interval is None:
            unconfigured += 1
            continue
        checked += 1
        due_at = _pm_due_at(session, cfg, device)
        if due_at is None or due_at > now:
            not_due += 1
            continue
        due += 1
        if _open_pm_exists(session, device.device_key):
            skipped_open += 1
            continue
        order = _create_order(
            session, device=device, type_="preventive", priority="routine",
            reason=(f"Scheduled preventive maintenance — "
                    f"{interval}-day interval, due "
                    f"{due_at.date().isoformat()}"),
            downtime=False, assignee=None, opened_by=user.username,
            pm_due_at=due_at)
        append_audit(session, actor=user.username,
                     action="maintenance.work_order.create",
                     entity=f"WorkOrder/{order.work_order_id}",
                     payload={"device_id": device.device_key,
                              "type": "preventive", "priority": "routine",
                              "downtime": False, "generated": "pm",
                              "pm_interval_days": interval,
                              "pm_due_at": due_at.isoformat()})
        created.append(order)
    append_audit(session, actor=user.username,
                 action="maintenance.pm.generate",
                 entity="maintenance/pm",
                 payload={"created": [o.work_order_id for o in created],
                          "checked": checked, "due": due,
                          "skipped_open": skipped_open, "not_due": not_due,
                          "unconfigured_types": unconfigured})
    session.commit()
    return {"created": [_order_out(o) for o in created],
            "created_count": len(created), "checked": checked, "due": due,
            "skipped_open": skipped_open, "not_due": not_due,
            "unconfigured_types": unconfigured}


# ---------------------------------------------------------------------------
# Work orders
# ---------------------------------------------------------------------------

@router.post("/work-orders", status_code=201)
async def create_work_order(request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(
                                require_capability("maintenance.work"))):
    payload = await _json_body(request)
    cfg = maintenance_config(session)
    device_id = payload.get("device_id") or payload.get("device_key")
    if not isinstance(device_id, str) or not device_id.strip():
        raise HTTPException(status_code=422,
                            detail="Work order requires 'device_id'")
    device = _device_row(session, device_id.strip())  # 404 if unknown
    type_ = payload.get("type")
    if type_ not in WORK_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"'type' must be one of {list(WORK_TYPES)}")
    priority = payload.get("priority") or "routine"
    if priority not in PRIORITIES:
        raise HTTPException(
            status_code=422,
            detail=f"'priority' must be one of {list(PRIORITIES)}")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise HTTPException(status_code=422,
                            detail="Work order requires 'reason'")
    downtime = payload.get("downtime") or False
    if not isinstance(downtime, bool):
        raise HTTPException(status_code=422,
                            detail="'downtime' must be a boolean")
    assignee = payload.get("assignee")
    if assignee is not None and (not isinstance(assignee, str)
                                 or not assignee.strip()):
        raise HTTPException(status_code=422,
                            detail="'assignee' must be a non-empty string")
    opened_at = (_parse_dt(payload["opened_at"], "opened_at")
                 if payload.get("opened_at") else None)
    order = _create_order(
        session, device=device, type_=type_, priority=priority,
        reason=reason.strip(), downtime=downtime,
        assignee=assignee.strip() if assignee else None,
        opened_by=user.username,
        pm_due_at=_pm_due_at(session, cfg, device)
        if type_ == "preventive" else None,
        opened_at=opened_at)
    if downtime:
        _start_downtime(session, cfg, device, order, user.username)
    append_audit(session, actor=user.username,
                 action="maintenance.work_order.create",
                 entity=f"WorkOrder/{order.work_order_id}",
                 payload={"device_id": device.device_key, "type": type_,
                          "priority": priority, "downtime": downtime,
                          "reason": order.reason})
    session.commit()
    return _order_out(order)


@router.get("/work-orders")
def list_work_orders(status: str | None = Query(default=None),
                     device_id: str | None = Query(default=None),
                     type: str | None = Query(default=None),
                     session: Session = Depends(get_db),
                     user: User = Depends(
                         require_capability("maintenance.read"))):
    stmt = select(WorkOrder).order_by(WorkOrder.id.desc())
    if status:
        stmt = stmt.where(WorkOrder.status == status)
    if device_id:
        stmt = stmt.where(WorkOrder.device_id == device_id)
    if type:
        stmt = stmt.where(WorkOrder.type == type)
    rows = session.scalars(stmt).all()
    return {"items": [_order_out(o) for o in rows], "total": len(rows)}


@router.get("/work-orders/{work_order_id}")
def get_work_order(work_order_id: str, session: Session = Depends(get_db),
                   user: User = Depends(
                       require_capability("maintenance.read"))):
    return _order_out(_order_row(session, work_order_id))


@router.post("/work-orders/{work_order_id}/assign")
async def assign_work_order(work_order_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(
                                require_capability("maintenance.work"))):
    payload = await _json_body(request)
    order = _order_row(session, work_order_id)
    assignee = payload.get("assignee")
    if not isinstance(assignee, str) or not assignee.strip():
        raise HTTPException(status_code=422,
                            detail="Assign requires 'assignee'")
    if order.status not in ("opened", "assigned"):
        raise HTTPException(
            status_code=409,
            detail=(f"Work order '{work_order_id}' is {order.status} and "
                    "cannot be assigned"))
    previous = {"status": order.status, "assignee": order.assignee}
    order.assignee = assignee.strip()
    order.status = "assigned"
    session.flush()
    append_audit(session, actor=user.username,
                 action="maintenance.work_order.assign",
                 entity=f"WorkOrder/{order.work_order_id}",
                 payload={"from": previous, "assignee": order.assignee})
    session.commit()
    return _order_out(order)


@router.post("/work-orders/{work_order_id}/transition")
async def transition_work_order(work_order_id: str, request: Request,
                                session: Session = Depends(get_db),
                                user: User = Depends(
                                    require_capability("maintenance.work"))):
    """Governed lifecycle move. `to_status` must be a legal next
    state (409 otherwise — e.g. completed -> opened, or closing an
    order that was never completed). Completing records
    completed_at + completion_note, advances a PM device's
    maintenance_due, and ends downtime."""
    payload = await _json_body(request)
    cfg = maintenance_config(session)
    order = _order_row(session, work_order_id)
    to_status = payload.get("to_status")
    if to_status not in STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"'to_status' must be one of {list(STATUSES)}")
    if to_status not in TRANSITIONS[order.status]:
        raise HTTPException(
            status_code=409,
            detail=(f"Work order '{work_order_id}' cannot move from "
                    f"'{order.status}' to '{to_status}'"))
    previous = order.status
    audit_payload: dict = {"from": previous, "to": to_status}
    if to_status == "assigned":
        assignee = payload.get("assignee") or order.assignee
        if not assignee:
            raise HTTPException(
                status_code=422,
                detail="Moving to 'assigned' requires 'assignee'")
        order.assignee = str(assignee).strip()
        audit_payload["assignee"] = order.assignee
    order.status = to_status
    if to_status == "completed":
        completed_at = (_parse_dt(payload["completed_at"], "completed_at")
                        if payload.get("completed_at") else _now())
        order.completed_at = completed_at
        note = payload.get("completion_note")
        if note is not None and not isinstance(note, str):
            raise HTTPException(status_code=422,
                                detail="'completion_note' must be a string")
        order.completion_note = (note or "").strip()
        audit_payload["completed_at"] = completed_at.isoformat()
        audit_payload["completion_note"] = order.completion_note
        device = _device_row(session, order.device_id)
        if order.type == "preventive":
            interval = _pm_interval_days(cfg, device.device_type)
            if interval is not None:
                device.maintenance_due = completed_at + timedelta(
                    days=interval)
                audit_payload["maintenance_due"] = \
                    device.maintenance_due.isoformat()
                session.flush()
        if order.downtime:
            audit_payload["downtime_restored"] = _end_downtime_if_clear(
                session, cfg, device, order, user.username)
    session.flush()
    append_audit(session, actor=user.username,
                 action="maintenance.work_order.transition",
                 entity=f"WorkOrder/{order.work_order_id}",
                 payload=audit_payload)
    session.commit()
    return _order_out(order)


# ---------------------------------------------------------------------------
# Seeding — governed config
# ---------------------------------------------------------------------------

def _seed_published(session: Session, kind: str, key: str,
                    payload: dict) -> None:
    if list_config_versions(session, kind, key):
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


def ensure_maintenance_defaults(session: Session) -> None:
    """Seed the published terminology/maintenance document
    (idempotent — existing versions are the truth)."""
    _seed_published(session, "terminology", MAINTENANCE_KEY,
                    default_maintenance_payload())
    session.commit()
