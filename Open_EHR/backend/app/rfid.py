"""Phase 15 (H-2) — RFID tag registry + reader event ingestion.

HONEST LIMIT: NO physical RFID hardware exists in this deployment.
Every reader event here arrives through `POST /rfid/events` — a
SIMULATED feed (direct API posts standing in for reader middleware,
the Phase 14 amqtt precedent: the feed is simulated, real readers
are site work, NOT RUN). Responses and sighting records carry
`source_label: "simulated"`; nothing in this module claims a live
reader link.

What lives here:

- **Governed configuration**: tag types + the zone map are published
  terminology (`terminology/rfid`: tag types asset |
  patient-wristband | medication | inventory, plus the zone list);
  the reader registry is published integration config
  (`integration/rfid-readers`: reader id -> zone, active flag). A
  site adds readers or zones by publishing, never by editing code.

- **Tag registry** (`RfidTag`, this module's own table): EPC unique,
  tag type, bound entity (type + id), status bound | unbound |
  retired, and the last-seen zone/at maintained from sightings.
  Bind / unbind / retire are `rfid.manage` mutations, each
  hash-audit-chained.

- **Reader event ingestion** through the device-ingestion contract
  shape (reader id + EPC + timestamp + optional signal — the
  Phase 14 device spine's posture): the reader must exist AND be
  active in the published registry. An event from an unknown or
  inactive reader — or naming an unregistered EPC — is
  DEAD-LETTERED into `RfidDeadLetter` with its disposition recorded,
  never silently dropped (the Phase 14 pattern). A valid event
  appends an `RfidSighting` to the tag's history and updates its
  last-seen zone/at.

- **Mismatch alerts via the rules engine**: every sighting of a
  device-bound asset tag emits `rfid.zone.mismatch` into
  app.rules.evaluate_rules with signals {mismatch, device_key,
  expected_zone, observed_zone, reader_id, epc}. The seeded
  PUBLISHED rule `rfid-zone-mismatch` (ensure defaults) opens a Task
  when mismatch is true; when the observed zone agrees with the
  device registry's assigned location, the same event evaluates
  with mismatch=false and no Task is created. NOTE: the event name
  is not yet in app.configstore.RULE_EVENTS, so the seeded rule is
  written directly as a published StudioConfig (the
  app.rules._seed_published pattern) — flagged INTEGRATION-NEEDED
  for the coordinator so the rule becomes Studio-editable.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import (DateTime, Float, Integer, JSON, String, select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import (ConfigStatus, Device, StudioConfig, User, utcnow)
from .security import get_db, require_capability

router = APIRouter(prefix="/rfid", tags=["rfid"])

RFID_KEY = "rfid"                    # terminology/rfid
READERS_KEY = "rfid-readers"         # integration/rfid-readers
TAG_STATUSES = ("bound", "unbound", "retired")
SIMULATED = "simulated"
MISMATCH_EVENT = "rfid.zone.mismatch"
MISMATCH_RULE_KEY = "rfid-zone-mismatch"


# ---------------------------------------------------------------------------
# This module's own tables
# ---------------------------------------------------------------------------

class RfidTag(Base):
    """One RFID tag: its EPC (unique), governed type, the entity it is
    bound to (entity_type + entity_id, e.g. Device/<device_key> split
    as type "Device", id the device_key), lifecycle status, and the
    last-seen posture maintained by reader events."""
    __tablename__ = "rfid_tags"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    epc: Mapped[str] = mapped_column(String(120), unique=True,
                                     nullable=False, index=True)
    tag_type: Mapped[str] = mapped_column(String(40), nullable=False,
                                          index=True)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False,
                                             default="")
    entity_id: Mapped[str] = mapped_column(String(200), nullable=False,
                                           default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="unbound", index=True)
    last_seen_zone: Mapped[str | None] = mapped_column(String(80),
                                                       nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


class RfidSighting(Base):
    """One accepted reader sighting of one tag — the location trail.
    Sightings are append-only operational data."""
    __tablename__ = "rfid_sightings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    epc: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    reader_id: Mapped[str] = mapped_column(String(120), nullable=False)
    zone: Mapped[str] = mapped_column(String(80), nullable=False)
    signal: Mapped[float | None] = mapped_column(Float, nullable=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                              nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=utcnow)
    source_label: Mapped[str] = mapped_column(String(40), nullable=False,
                                              default=SIMULATED)


class RfidDeadLetter(Base):
    """One rejected reader event, kept with its disposition — the
    Phase 14 dead-letter pattern: unknown/inactive reader or unknown
    tag events are recorded, never silently dropped."""
    __tablename__ = "rfid_dead_letters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reader_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                           default="")
    epc: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    disposition: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str] = mapped_column(String(400), nullable=False,
                                        default="")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


# ---------------------------------------------------------------------------
# Published config: terminology/rfid + integration/rfid-readers
# ---------------------------------------------------------------------------

def default_rfid_payload() -> dict:
    return {
        "name": "RFID tag types + zone map (synthetic defaults)",
        "tagTypes": [
            {"code": "asset", "display": "Asset tag (devices/equipment)"},
            {"code": "patient-wristband",
             "display": "Patient wristband tag"},
            {"code": "medication", "display": "Medication tag"},
            {"code": "inventory", "display": "Inventory tag"},
        ],
        "zones": [
            {"code": "icu", "display": "Intensive care unit"},
            {"code": "med-surg", "display": "Medical/surgical ward"},
            {"code": "pharmacy", "display": "Main pharmacy"},
            {"code": "lab", "display": "Laboratory"},
            {"code": "storage", "display": "Central storage"},
            {"code": "ed", "display": "Emergency department"},
        ],
    }


def default_readers_payload() -> dict:
    return {
        "name": "RFID reader registry (synthetic defaults)",
        "readers": [
            {"readerId": "reader-icu-1", "zone": "icu", "active": True},
            {"readerId": "reader-pharmacy-1", "zone": "pharmacy",
             "active": True},
            {"readerId": "reader-storage-1", "zone": "storage",
             "active": False},
        ],
        "feedLabel": SIMULATED,
    }


def _rfid_config(session: Session) -> dict:
    cfg = published_config(session, "terminology", RFID_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    return payload if payload.get("tagTypes") else default_rfid_payload()


def _tag_types(session: Session) -> list[str]:
    return [t["code"] for t in _rfid_config(session).get("tagTypes") or []
            if isinstance(t, dict) and t.get("code")]


def _zones(session: Session) -> list[str]:
    return [z["code"] for z in _rfid_config(session).get("zones") or []
            if isinstance(z, dict) and z.get("code")]


def _readers(session: Session) -> dict[str, dict]:
    cfg = published_config(session, "integration", READERS_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    readers = payload.get("readers")
    if not isinstance(readers, list):
        readers = default_readers_payload()["readers"]
    return {str(r["readerId"]): r for r in readers
            if isinstance(r, dict) and r.get("readerId")}


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
                            detail=f"'{field}' must be an ISO datetime")
    try:
        return _aware(datetime.fromisoformat(
            value.strip().replace("Z", "+00:00")))
    except ValueError:
        raise HTTPException(status_code=422,
                            detail=f"'{field}' is not a valid ISO "
                                   f"datetime: '{value}'")


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


def _tag_out(tag: RfidTag) -> dict:
    return {"epc": tag.epc, "tag_type": tag.tag_type,
            "entity_type": tag.entity_type, "entity_id": tag.entity_id,
            "entity_ref": (f"{tag.entity_type}/{tag.entity_id}"
                           if tag.entity_type and tag.entity_id else None),
            "status": tag.status,
            "last_seen_zone": tag.last_seen_zone,
            "last_seen_at": _aware(tag.last_seen_at).isoformat()
            if tag.last_seen_at else None}


def _tag_row(session: Session, epc: str) -> RfidTag:
    tag = session.scalars(select(RfidTag).where(RfidTag.epc == epc)).first()
    if tag is None:
        raise HTTPException(status_code=404,
                            detail=f"RFID tag '{epc}' is not registered")
    return tag


def _sighting_out(row: RfidSighting) -> dict:
    return {"id": row.id, "epc": row.epc, "reader_id": row.reader_id,
            "zone": row.zone, "signal": row.signal,
            "seen_at": _aware(row.seen_at).isoformat()
            if row.seen_at else None,
            "received_at": _aware(row.received_at).isoformat()
            if row.received_at else None,
            "source_label": row.source_label}


# ---------------------------------------------------------------------------
# Config + registry API
# ---------------------------------------------------------------------------

@router.get("/config")
def get_config(session: Session = Depends(get_db),
               user: User = Depends(require_capability("rfid.read"))):
    cfg = _rfid_config(session)
    return {"tag_types": cfg.get("tagTypes") or [],
            "zones": cfg.get("zones") or [],
            "readers": list(_readers(session).values()),
            "feed_label": SIMULATED}


@router.get("/tags")
def list_tags(status: str | None = Query(default=None),
              tag_type: str | None = Query(default=None),
              session: Session = Depends(get_db),
              user: User = Depends(require_capability("rfid.read"))):
    stmt = select(RfidTag).order_by(RfidTag.epc.asc())
    if status:
        stmt = stmt.where(RfidTag.status == status)
    if tag_type:
        stmt = stmt.where(RfidTag.tag_type == tag_type)
    rows = session.scalars(stmt).all()
    return {"items": [_tag_out(t) for t in rows], "total": len(rows),
            "feed_label": SIMULATED}


@router.post("/tags", status_code=201)
async def create_tag(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("rfid.manage"))):
    """Register one tag. With entity_type + entity_id it starts
    bound; without, unbound. The tag type must be one of the
    published terminology types (422 otherwise); EPCs are unique
    (409 on re-register)."""
    payload = await _json_body(request)
    epc = _require_str(payload, "epc")
    tag_type = _require_str(payload, "tag_type")
    if tag_type not in _tag_types(session):
        raise HTTPException(
            status_code=422,
            detail=f"'tag_type' must be one of the published RFID tag "
                   f"types {_tag_types(session)}")
    if session.scalars(select(RfidTag).where(RfidTag.epc == epc)).first() \
            is not None:
        raise HTTPException(status_code=409,
                            detail=f"RFID tag '{epc}' is already "
                                   "registered")
    entity_type = str(payload.get("entity_type") or "")
    entity_id = str(payload.get("entity_id") or "")
    if bool(entity_type) != bool(entity_id):
        raise HTTPException(
            status_code=422,
            detail="'entity_type' and 'entity_id' must be supplied "
                   "together (or neither, for an unbound tag)")
    tag = RfidTag(epc=epc, tag_type=tag_type, entity_type=entity_type,
                  entity_id=entity_id,
                  status="bound" if entity_id else "unbound")
    session.add(tag)
    session.flush()
    append_audit(session, actor=user.username, action="rfid.tag.create",
                 entity=f"RfidTag/{epc}",
                 payload={"tag_type": tag_type,
                          "entity_type": entity_type,
                          "entity_id": entity_id, "status": tag.status})
    session.commit()
    return _tag_out(tag)


@router.get("/tags/{epc}")
def get_tag(epc: str, session: Session = Depends(get_db),
            user: User = Depends(require_capability("rfid.read"))):
    tag = _tag_row(session, epc)
    sightings = session.scalars(
        select(RfidSighting).where(RfidSighting.epc == epc)
        .order_by(RfidSighting.id.desc()).limit(50)).all()
    out = _tag_out(tag)
    out["sightings"] = [_sighting_out(s) for s in sightings]
    out["sighting_count"] = len(sightings)
    return out


@router.post("/tags/{epc}/bind")
async def bind_tag(epc: str, request: Request,
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("rfid.manage"))):
    payload = await _json_body(request)
    tag = _tag_row(session, epc)
    if tag.status == "retired":
        raise HTTPException(status_code=409,
                            detail=f"RFID tag '{epc}' is retired and "
                                   "cannot be bound")
    entity_type = _require_str(payload, "entity_type")
    entity_id = _require_str(payload, "entity_id")
    previous = {"entity_type": tag.entity_type, "entity_id": tag.entity_id,
                "status": tag.status}
    tag.entity_type, tag.entity_id, tag.status = entity_type, entity_id, \
        "bound"
    session.flush()
    append_audit(session, actor=user.username, action="rfid.tag.bind",
                 entity=f"RfidTag/{epc}",
                 payload={"from": previous, "entity_type": entity_type,
                          "entity_id": entity_id})
    session.commit()
    return _tag_out(tag)


@router.post("/tags/{epc}/unbind")
def unbind_tag(epc: str, session: Session = Depends(get_db),
               user: User = Depends(require_capability("rfid.manage"))):
    tag = _tag_row(session, epc)
    if tag.status == "retired":
        raise HTTPException(status_code=409,
                            detail=f"RFID tag '{epc}' is retired")
    previous = {"entity_type": tag.entity_type, "entity_id": tag.entity_id}
    tag.entity_type, tag.entity_id, tag.status = "", "", "unbound"
    session.flush()
    append_audit(session, actor=user.username, action="rfid.tag.unbind",
                 entity=f"RfidTag/{epc}", payload={"from": previous})
    session.commit()
    return _tag_out(tag)


@router.post("/tags/{epc}/retire")
def retire_tag(epc: str, session: Session = Depends(get_db),
               user: User = Depends(require_capability("rfid.manage"))):
    tag = _tag_row(session, epc)
    if tag.status == "retired":
        raise HTTPException(status_code=409,
                            detail=f"RFID tag '{epc}' is already retired")
    previous = tag.status
    tag.status = "retired"
    session.flush()
    append_audit(session, actor=user.username, action="rfid.tag.retire",
                 entity=f"RfidTag/{epc}", payload={"from": previous})
    session.commit()
    return _tag_out(tag)


# ---------------------------------------------------------------------------
# Reader event ingestion (device-ingestion contract shape, simulated feed)
# ---------------------------------------------------------------------------

def _dead_letter(session: Session, payload: dict, disposition: str,
                 reason: str, actor: str) -> dict:
    row = RfidDeadLetter(reader_id=str(payload.get("reader_id") or ""),
                         epc=str(payload.get("epc") or ""),
                         disposition=disposition, reason=reason,
                         payload=payload)
    session.add(row)
    session.flush()
    append_audit(session, actor=actor, action="rfid.event.dead_letter",
                 entity=f"RfidReader/{row.reader_id or 'unknown'}",
                 payload={"disposition": disposition, "reason": reason,
                          "epc": row.epc, "dead_letter_id": row.id})
    session.commit()
    return {"status": "dead-lettered", "disposition": disposition,
            "reason": reason, "dead_letter_id": row.id,
            "source_label": SIMULATED}


@router.post("/events")
async def ingest_event(request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("rfid.manage"))):
    """One reader event: {reader_id, epc, timestamp?, signal?}. The
    reader must exist and be active in the published registry —
    unknown/inactive readers and unregistered EPCs are dead-lettered
    (202, disposition recorded), never silently dropped. A valid
    event appends a sighting, updates the tag's last-seen, and — for
    device-bound asset tags — emits rfid.zone.mismatch into the
    rules engine (mismatch signal true/false), where the published
    rfid-zone-mismatch rule raises a Task on a real mismatch."""
    payload = await _json_body(request)
    reader_id = payload.get("reader_id")
    epc = payload.get("epc")
    if not isinstance(reader_id, str) or not reader_id.strip():
        raise HTTPException(status_code=422,
                            detail="Event requires 'reader_id'")
    if not isinstance(epc, str) or not epc.strip():
        raise HTTPException(status_code=422, detail="Event requires 'epc'")
    reader_id, epc = reader_id.strip(), epc.strip()
    signal = payload.get("signal")
    if signal is not None and (isinstance(signal, bool)
                               or not isinstance(signal, (int, float))):
        raise HTTPException(status_code=422,
                            detail="'signal' must be numeric when given")
    seen_at = (_parse_dt(payload["timestamp"], "timestamp")
               if payload.get("timestamp") else _now())
    readers = _readers(session)
    reader = readers.get(reader_id)
    if reader is None:
        return JSONResponse(status_code=202, content=_dead_letter(
            session, payload, "unknown-reader",
            f"Reader '{reader_id}' is not in the published "
            "integration/rfid-readers registry", user.username))
    if reader.get("active") is not True:
        return JSONResponse(status_code=202, content=_dead_letter(
            session, payload, "inactive-reader",
            f"Reader '{reader_id}' is registered but not active in "
            "the published registry", user.username))
    tag = session.scalars(select(RfidTag).where(RfidTag.epc == epc)).first()
    if tag is None:
        return JSONResponse(status_code=202, content=_dead_letter(
            session, payload, "unknown-tag",
            f"EPC '{epc}' is not a registered RFID tag", user.username))
    if tag.status == "retired":
        return JSONResponse(status_code=202, content=_dead_letter(
            session, payload, "retired-tag",
            f"EPC '{epc}' belongs to a retired tag", user.username))
    zone = str(reader.get("zone") or "")
    sighting = RfidSighting(epc=epc, reader_id=reader_id, zone=zone,
                            signal=float(signal) if signal is not None
                            else None, seen_at=seen_at)
    session.add(sighting)
    tag.last_seen_zone = zone
    tag.last_seen_at = seen_at
    session.flush()
    firings: list[dict] = []
    mismatch = None
    expected_zone = None
    if tag.status == "bound" and tag.entity_type == "Device":
        device = session.scalars(select(Device).where(
            Device.device_key == tag.entity_id)).first()
        if device is not None:
            expected_zone = device.location
            mismatch = zone != expected_zone
            from .rules import evaluate_rules
            firings = evaluate_rules(session, MISMATCH_EVENT, {
                "mismatch": mismatch, "epc": epc,
                "device_key": device.device_key,
                "entity_ref": f"Device/{device.device_key}",
                "expected_zone": expected_zone, "observed_zone": zone,
                "reader_id": reader_id}, actor=user.username)
    append_audit(session, actor=user.username, action="rfid.event.ingested",
                 entity=f"RfidTag/{epc}",
                 payload={"reader_id": reader_id, "zone": zone,
                          "sighting_id": sighting.id,
                          "mismatch": mismatch,
                          "expected_zone": expected_zone,
                          "source_label": SIMULATED})
    session.commit()
    return {"status": "recorded", "sighting": _sighting_out(sighting),
            "tag": _tag_out(tag), "mismatch": mismatch,
            "expected_zone": expected_zone, "rule_firings": firings,
            "source_label": SIMULATED}


@router.get("/dead-letters")
def list_dead_letters(session: Session = Depends(get_db),
                      user: User = Depends(require_capability("rfid.read"))):
    rows = session.scalars(
        select(RfidDeadLetter).order_by(RfidDeadLetter.id.desc())
        .limit(200)).all()
    return {"items": [{"id": r.id, "reader_id": r.reader_id, "epc": r.epc,
                       "disposition": r.disposition, "reason": r.reason,
                       "payload": r.payload or {},
                       "created_at": _aware(r.created_at).isoformat()
                       if r.created_at else None} for r in rows],
            "total": len(rows)}


@router.get("/entities/{entity_type}/{entity_id}/sightings")
def entity_sightings(entity_type: str, entity_id: str,
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability("rfid.read"))):
    """The location trail for one bound entity: every tag bound to it
    and every sighting of those tags, oldest first — the asset
    location trail for devices, wristband sightings for patients."""
    tags = session.scalars(
        select(RfidTag).where(RfidTag.entity_type == entity_type,
                              RfidTag.entity_id == entity_id)).all()
    epcs = [t.epc for t in tags]
    sightings = []
    if epcs:
        sightings = session.scalars(
            select(RfidSighting).where(RfidSighting.epc.in_(epcs))
            .order_by(RfidSighting.id.asc())).all()
    return {"entity_type": entity_type, "entity_id": entity_id,
            "entity_ref": f"{entity_type}/{entity_id}",
            "tags": [_tag_out(t) for t in tags],
            "sightings": [_sighting_out(s) for s in sightings],
            "total": len(sightings), "feed_label": SIMULATED}


# ---------------------------------------------------------------------------
# Seeding — governed config + the published mismatch rule
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


def default_mismatch_rule_payload() -> dict:
    return {
        "name": "RFID zone mismatch",
        "description": ("Phase 15 (H-2): when a reader event places a "
                        "device-bound asset tag in a zone different "
                        "from the device registry's assigned location "
                        "for that device, app.rfid emits "
                        "rfid.zone.mismatch; this rule opens a "
                        "relocation-review Task for the asset team. "
                        "Events whose zone agrees evaluate with "
                        "mismatch=false and create nothing."),
        "event": MISMATCH_EVENT,
        "condition": {"signal": "mismatch", "op": "eq", "value": True},
        "action": {"type": "create_task",
                   "params": {"title": "RFID zone mismatch — asset tag "
                                        "seen outside its assigned zone",
                              "ownerRole": "asset-management"}},
        "enabled": True,
    }


def ensure_rfid_defaults(session: Session) -> None:
    """Seed terminology/rfid, integration/rfid-readers and the
    published rfid-zone-mismatch rule (idempotent — existing versions
    are the truth and are never duplicated)."""
    _seed_published(session, "terminology", RFID_KEY,
                    default_rfid_payload())
    _seed_published(session, "integration", READERS_KEY,
                    default_readers_payload())
    _seed_published(session, "rule", MISMATCH_RULE_KEY,
                    default_mismatch_rule_payload())
    session.commit()
