"""Devices, IoT ingestion & the raw-zone lake API — Phase 14, Track H.

What lives here:

- **Device registry.** Analysers, vitals monitors, storage sensors
  (fridge/freezer) and pumps as real operational data: type, location,
  serial, status, HL7 sender identity, MQTT topic key, and calibration
  / maintenance due dates. Device TYPES (with calibration intervals
  and sensor ranges), the MLLP gate policy, the HL7 sender map and the
  vitals thresholds are published Studio config
  (`terminology/devices`) — a site changes intervals, ranges and gate
  behaviour by publishing, never by editing code. Calibration posture
  is computed at read from the stored due date; a device whose
  calibration is overdue (or whose status is a configured blocking
  status) cannot release results — the gate below enforces it.

- **Ingestion.** Three transports, one core:
  (a) HL7 ORU over the Phase 8 MLLP listener — app.interop's
      process_inbound calls :func:`evaluate_oru_gate` first: the
      device is resolved from MSH-3 via the published sender map;
      an unknown sender is dead-lettered (AR + the raw payload kept
      in the lake + the inbound log), never silently dropped; an
      overdue/blocked device is refused (block) or held (quarantine)
      per the published gate policy. Accepted messages flow into the
      EXISTING lab ingest and land as draft lab results, stamped with
      the device + raw-entry lineage.
  (b) FHIR Observation ingestion (`POST /devices/ingest/observation`)
      for vitals monitors: a Device reference is required, the
      patient must exist, the same calibration gate applies, and a
      value outside the published per-code thresholds emits
      `device.vitals.breach` into the rules engine.
  (c) MQTT (below) for IoT sensors, plus sensor readings over HTTP.
  Fridge/freezer temperature readings are range-checked against the
  device type's published range; a transition INTO excursion emits
  `device.temperature.excursion` (the cold-chain alert pattern).

- **MQTT adapter.** :class:`MqttAdapter` speaks to a broker whose URL
  lives in the published `integration/mqtt` config, using paho-mqtt.
  With no broker configured the adapter is HONESTLY OFF — the status
  endpoint says so and names the reason; nothing pretends to be
  connected. Message handling (`handle_message`) is transport-free
  and is exercised with recorded payloads via
  `POST /devices/mqtt/test-message`, and against a real localhost
  broker (amqtt) in the test suite. The adapter runs as a service
  beside the API (the mllp_server pattern): `python -m app.devices`
  is not a thing — call :func:`serve_mqtt` from a runner; the API
  process itself never opens a broker connection implicitly.

- **Raw-zone lake browser + lineage** (app.lake holds the zone):
  list/detail over raw entries and "where did this value come from"
  for any curated record. There are deliberately NO update/delete
  endpoints for raw entries — the zone is immutable.

- **Offline store-and-forward** (deployment mode): when the published
  `integration/offline` config enables it, `POST /devices/offline/
  batches` accepts captured batches and lands them in the raw zone
  as held entries; the pipeline `replay-lake` step (app.pipelines)
  later processes them into the curated zone. No separate sync
  engine exists, by design.

HONESTY RULE (Track H): every feed in this deployment is a simulated
or recorded input. Raw entries, readings and API responses carry
`source_label: "simulated"` and the web surfaces label them the same
way. No endpoint claims a live device link.
"""
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir, lake
from .audit import append_audit
from .configstore import published_config
from .models import (ComplianceRequirement, ConfigStatus, DataQuarantine,
                     Device, DeviceReading, FhirResource, LakeRawEntry,
                     StudioConfig, User)
from .security import get_db, require_capability

router = APIRouter(prefix="/devices", tags=["devices"])

DEVICES_KEY = "devices"              # terminology/devices
MQTT_KEY = "mqtt"                    # integration/mqtt
OFFLINE_KEY = "offline"              # integration/offline
GATEWAY_USERNAME = "device-gateway"
SOURCE_EXTENSION = "http://opencare.org/fhir/data-source"
SIMULATED = "simulated"


# ---------------------------------------------------------------------------
# Small helpers
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
        raise HTTPException(status_code=422, detail=f"'{field}' must be an ISO date/datetime")
    text = value.strip()
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        return _aware(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"'{field}' is not a valid ISO date/datetime: '{value}'")


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


def _gateway_user(session: Session) -> User:
    user = session.scalars(
        select(User).where(User.username == GATEWAY_USERNAME)).first()
    if user is None:
        from .models import Role
        user = User(username=GATEWAY_USERNAME, role=Role.clinician)
        session.add(user)
        session.flush()
    return user


def devices_config(session: Session) -> dict:
    cfg = published_config(session, "terminology", DEVICES_KEY)
    return (cfg.payload or {}) if cfg is not None else {}


def _type_cfg(cfg: dict, device_type: str) -> dict | None:
    for entry in cfg.get("deviceTypes") or []:
        if isinstance(entry, dict) and entry.get("type") == device_type:
            return entry
    return None


def calibration_state(cfg: dict, device: Device, at: datetime | None = None) -> dict:
    """Computed calibration posture: ok | due-soon | overdue | unknown
    (no due date recorded) | not-required (type has no interval)."""
    at = at or _now()
    type_cfg = _type_cfg(cfg, device.device_type) or {}
    if not type_cfg.get("calibrationIntervalDays"):
        return {"status": "not-required", "due": None, "days_remaining": None}
    due = _aware(device.calibration_due)
    if due is None:
        return {"status": "unknown", "due": None, "days_remaining": None}
    days = (due.date() - at.date()).days
    window = cfg.get("calibrationWorklistWindowDays", 30)
    if days < 0:
        status = "overdue"
    elif days <= window:
        status = "due-soon"
    else:
        status = "ok"
    return {"status": status, "due": due.isoformat(), "days_remaining": days}


def gate_block_reason(cfg: dict, device: Device) -> str | None:
    """Why this device may not release results right now, or None.
    Two independent blocks: a configured blocking status, and an
    overdue calibration (the 6B QC-gate pattern, generalised)."""
    blocking = set(cfg.get("blockingStatuses") or [])
    if device.status in blocking:
        return (f"device '{device.device_key}' has status "
                f"'{device.status}' (a blocking status in the published "
                "device config)")
    state = calibration_state(cfg, device)
    if state["status"] == "overdue":
        return (f"device '{device.device_key}' calibration is overdue "
                f"(due {state['due']})")
    return None


def _device_out(cfg: dict, device: Device) -> dict:
    return {
        "device_key": device.device_key,
        "name": device.name,
        "device_type": device.device_type,
        "location": device.location,
        "serial": device.serial,
        "status": device.status,
        "hl7_sender": device.hl7_sender,
        "mqtt_topic_key": device.mqtt_topic_key,
        "calibration_due": _aware(device.calibration_due).isoformat()
        if device.calibration_due else None,
        "maintenance_due": _aware(device.maintenance_due).isoformat()
        if device.maintenance_due else None,
        "last_calibrated_at": _aware(device.last_calibrated_at).isoformat()
        if device.last_calibrated_at else None,
        "calibration": calibration_state(cfg, device),
        "blocked_reason": gate_block_reason(cfg, device),
        # Track H honesty rule: feeds in this deployment are simulated.
        "feed_label": SIMULATED,
    }


def _device_row(session: Session, device_key: str) -> Device:
    row = session.scalars(
        select(Device).where(Device.device_key == device_key)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Device '{device_key}' not found")
    return row


# ---------------------------------------------------------------------------
# Registry API — literal routes are declared BEFORE /{device_key}.
# ---------------------------------------------------------------------------

@router.get("/config")
def get_config(session: Session = Depends(get_db),
               user: User = Depends(require_capability("devices.read"))):
    cfg = devices_config(session)
    mqtt_cfg = published_config(session, "integration", MQTT_KEY)
    return {"devices": cfg,
            "mqtt_configured": bool(
                mqtt_cfg is not None
                and (mqtt_cfg.payload or {}).get("brokerUrl")),
            "feed_label": SIMULATED}


@router.get("")
def list_devices(type: str | None = Query(default=None),
                 status: str | None = Query(default=None),
                 session: Session = Depends(get_db),
                 user: User = Depends(require_capability("devices.read"))):
    cfg = devices_config(session)
    stmt = select(Device).order_by(Device.device_key.asc())
    if type:
        stmt = stmt.where(Device.device_type == type)
    if status:
        stmt = stmt.where(Device.status == status)
    rows = session.scalars(stmt).all()
    items = [_device_out(cfg, d) for d in rows]
    return {"items": items, "total": len(items),
            "overdue": sum(1 for i in items
                           if i["calibration"]["status"] == "overdue"),
            "blocked": sum(1 for i in items if i["blocked_reason"]),
            "feed_label": SIMULATED}


@router.post("", status_code=201)
async def create_device(request: Request, session: Session = Depends(get_db),
                        user: User = Depends(require_capability("devices.manage"))):
    payload = await _json_body(request)
    cfg = devices_config(session)
    device_key = payload.get("device_key")
    name = payload.get("name")
    device_type = payload.get("device_type")
    if not isinstance(device_key, str) or not device_key.strip():
        raise HTTPException(status_code=422, detail="Device requires 'device_key'")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=422, detail="Device requires 'name'")
    if _type_cfg(cfg, str(device_type)) is None:
        known = [t.get("type") for t in cfg.get("deviceTypes") or []]
        raise HTTPException(
            status_code=422,
            detail=f"'device_type' must be one of the published device types {known}")
    if session.scalars(select(Device).where(
            Device.device_key == device_key.strip())).first():
        raise HTTPException(status_code=409,
                            detail=f"Device '{device_key.strip()}' already exists")
    hl7_sender = payload.get("hl7_sender")
    if hl7_sender is not None:
        if not isinstance(hl7_sender, str) or not hl7_sender.strip():
            raise HTTPException(status_code=422, detail="'hl7_sender' must be a string")
        if session.scalars(select(Device).where(
                Device.hl7_sender == hl7_sender.strip())).first():
            raise HTTPException(
                status_code=409,
                detail=f"HL7 sender '{hl7_sender.strip()}' is already bound to another device")
    statuses = cfg.get("statuses") or ["active"]
    status = payload.get("status") or "active"
    if status not in statuses:
        raise HTTPException(status_code=422,
                            detail=f"'status' must be one of {statuses}")
    device = Device(
        device_key=device_key.strip(), name=name.strip(),
        device_type=str(device_type),
        location=str(payload.get("location") or ""),
        serial=str(payload.get("serial") or ""), status=status,
        hl7_sender=hl7_sender.strip() if hl7_sender else None,
        mqtt_topic_key=(str(payload["mqtt_topic_key"]).strip()
                        if payload.get("mqtt_topic_key") else None),
        calibration_due=(_parse_dt(payload["calibration_due"], "calibration_due")
                         if payload.get("calibration_due") else None),
        maintenance_due=(_parse_dt(payload["maintenance_due"], "maintenance_due")
                         if payload.get("maintenance_due") else None))
    session.add(device)
    session.flush()
    append_audit(session, actor=user.username, action="devices.create",
                 entity=f"Device/{device.device_key}",
                 payload={"device_type": device.device_type,
                          "location": device.location})
    session.commit()
    return _device_out(cfg, device)


@router.get("/calibration-worklist")
def calibration_worklist(session: Session = Depends(get_db),
                         user: User = Depends(require_capability("devices.read"))):
    """Devices overdue or inside the published worklist window,
    worst first (the HTM workqueue view)."""
    cfg = devices_config(session)
    items = []
    for device in session.scalars(select(Device)).all():
        state = calibration_state(cfg, device)
        if state["status"] in ("overdue", "due-soon"):
            out = _device_out(cfg, device)
            out["worklist_status"] = state["status"]
            items.append(out)
    items.sort(key=lambda i: (i["calibration"]["days_remaining"]
                              if i["calibration"]["days_remaining"] is not None
                              else 0))
    return {"items": items, "total": len(items),
            "window_days": cfg.get("calibrationWorklistWindowDays", 30)}


@router.post("/{device_key}/calibration")
async def record_calibration(device_key: str, request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability("devices.manage"))):
    """Record a calibration: the next due date is computed from the
    device type's published interval — the registry never takes a
    hand-typed due date for a calibration event."""
    payload = await _json_body(request)
    cfg = devices_config(session)
    device = _device_row(session, device_key)
    type_cfg = _type_cfg(cfg, device.device_type) or {}
    interval = type_cfg.get("calibrationIntervalDays")
    if not interval:
        raise HTTPException(
            status_code=422,
            detail=(f"Device type '{device.device_type}' has no calibration "
                    "interval in the published config — nothing to record "
                    "against"))
    performed = (_parse_dt(payload["performed_at"], "performed_at")
                 if payload.get("performed_at") else _now())
    device.last_calibrated_at = performed
    device.calibration_due = performed + timedelta(days=int(interval))
    session.flush()
    append_audit(session, actor=user.username, action="devices.calibration",
                 entity=f"Device/{device.device_key}",
                 payload={"performed_at": performed.isoformat(),
                          "due": device.calibration_due.isoformat(),
                          "interval_days": interval,
                          "note": payload.get("note") or ""})
    session.commit()
    return _device_out(cfg, device)


@router.post("/{device_key}/status")
async def set_device_status(device_key: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("devices.manage"))):
    payload = await _json_body(request)
    cfg = devices_config(session)
    device = _device_row(session, device_key)
    statuses = cfg.get("statuses") or ["active"]
    status = payload.get("status")
    if status not in statuses:
        raise HTTPException(status_code=422,
                            detail=f"'status' must be one of {statuses}")
    previous = device.status
    device.status = status
    session.flush()
    append_audit(session, actor=user.username, action="devices.status",
                 entity=f"Device/{device.device_key}",
                 payload={"from": previous, "to": status,
                          "note": payload.get("note") or ""})
    session.commit()
    return _device_out(cfg, device)


# ---------------------------------------------------------------------------
# Sensor readings — one core for HTTP + MQTT + offline replay
# ---------------------------------------------------------------------------

def _reading_out(row: DeviceReading) -> dict:
    return {"id": row.id, "device_key": row.device_key, "metric": row.metric,
            "value": row.value, "unit": row.unit,
            "recorded_at": _aware(row.recorded_at).isoformat()
            if row.recorded_at else None,
            "received_at": _aware(row.received_at).isoformat()
            if row.received_at else None,
            "source": row.source, "source_label": SIMULATED,
            "excursion": row.excursion, "raw_entry_id": row.raw_entry_id}


def ingest_reading(session: Session, *, device: Device, metric: str,
                   value: float, unit: str = "", recorded_at=None,
                   source: str = "http", raw_text: str | None = None,
                   actor: str = GATEWAY_USERNAME,
                   existing_raw: LakeRawEntry | None = None) -> dict:
    """Record one sensor reading: raw entry first (the lake gets
    everything as-received), then the reading row, then the excursion
    evaluation. A transition INTO excursion emits
    `device.temperature.excursion` into the rules engine in the same
    transaction. Returns the outcome dict every transport reports.
    `existing_raw` (pipeline replay) links the reading to the raw
    entry that already holds this payload instead of recording a
    duplicate."""
    cfg = devices_config(session)
    recorded = _aware(recorded_at) or _now()
    if existing_raw is not None:
        raw = existing_raw
    else:
        if raw_text is None:
            raw_text = json.dumps({"device_key": device.device_key,
                                   "metric": metric, "value": value,
                                   "unit": unit,
                                   "recorded_at": recorded.isoformat(),
                                   "source": SIMULATED}, sort_keys=True)
        raw = lake.record_raw(session, payload_type="device-reading",
                              payload_text=raw_text, source=source,
                              source_label=SIMULATED, device_key=device.device_key)
    type_cfg = _type_cfg(cfg, device.device_type) or {}
    range_cfg = (type_cfg.get("ranges") or {}).get(metric)
    excursion = False
    if range_cfg is not None:
        excursion = not (float(range_cfg["min"]) <= float(value)
                         <= float(range_cfg["max"]))
    previous = session.scalars(
        select(DeviceReading).where(DeviceReading.device_key == device.device_key,
                                    DeviceReading.metric == metric)
        .order_by(DeviceReading.id.desc())).first()
    reading = DeviceReading(device_key=device.device_key, metric=metric,
                            value=float(value), unit=unit or "",
                            recorded_at=recorded, source=source,
                            excursion=excursion, raw_entry_id=raw.id)
    session.add(reading)
    session.flush()
    lake.link_curated(session, raw.id, f"DeviceReading/{reading.id}")
    event_fired = False
    if excursion and (previous is None or not previous.excursion):
        from .rules import evaluate_rules
        evaluate_rules(session, "device.temperature.excursion", {
            "device_key": device.device_key, "device_name": device.name,
            "location": device.location, "metric": metric,
            "value": float(value), "unit": unit or "",
            "min": range_cfg["min"] if range_cfg else None,
            "max": range_cfg["max"] if range_cfg else None,
            "owner_role": type_cfg.get("ownerRole") or ""}, actor=actor)
        event_fired = True
    append_audit(session, actor=actor, action="devices.reading.ingested",
                 entity=f"Device/{device.device_key}",
                 payload={"metric": metric, "value": float(value),
                          "unit": unit or "", "excursion": excursion,
                          "raw_entry_id": raw.id, "source": source})
    session.flush()
    return {"reading": _reading_out(reading), "excursion": excursion,
            "excursion_event_fired": event_fired, "raw_entry_id": raw.id}


@router.get("/readings")
def list_readings(device_key: str | None = Query(default=None),
                  excursion: bool | None = Query(default=None),
                  limit: int = Query(default=100),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability("devices.read"))):
    stmt = select(DeviceReading).order_by(DeviceReading.id.desc())
    if device_key:
        stmt = stmt.where(DeviceReading.device_key == device_key)
    if excursion is not None:
        stmt = stmt.where(DeviceReading.excursion == excursion)
    rows = session.scalars(stmt.limit(min(limit, 500))).all()
    return {"items": [_reading_out(r) for r in rows], "total": len(rows),
            "feed_label": SIMULATED}


@router.post("/ingest/reading", status_code=201)
async def ingest_reading_endpoint(request: Request,
                                  session: Session = Depends(get_db),
                                  user: User = Depends(require_capability("devices.manage"))):
    """Sensor reading over HTTP. In this deployment this endpoint IS
    the simulated feed: readings posted here are labelled simulated in
    the data, whatever the caller claims."""
    payload = await _json_body(request)
    device_key = payload.get("device_key")
    if not isinstance(device_key, str) or not device_key.strip():
        raise HTTPException(status_code=422, detail="Reading requires 'device_key'")
    device = session.scalars(
        select(Device).where(Device.device_key == device_key.strip())).first()
    if device is None:
        raise HTTPException(status_code=422,
                            detail=f"Reading names unknown device '{device_key}'")
    metric = payload.get("metric")
    if not isinstance(metric, str) or not metric.strip():
        raise HTTPException(status_code=422, detail="Reading requires 'metric'")
    value = payload.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(status_code=422, detail="Reading 'value' must be numeric")
    recorded = (_parse_dt(payload["recorded_at"], "recorded_at")
                if payload.get("recorded_at") else None)
    outcome = ingest_reading(session, device=device, metric=metric.strip(),
                             value=float(value),
                             unit=str(payload.get("unit") or ""),
                             recorded_at=recorded, source="http",
                             actor=user.username)
    session.commit()
    return outcome


# ---------------------------------------------------------------------------
# FHIR Observation ingestion (vitals monitors)
# ---------------------------------------------------------------------------

def _obs_code(obs: dict) -> tuple[str, str]:
    codings = (obs.get("code") or {}).get("coding") or []
    for c in codings:
        if isinstance(c, dict) and c.get("code"):
            return str(c["code"]), str(c.get("system") or "")
    return "", ""


def ingest_observation(session: Session, *, obs: dict, source: str = "http",
                       raw_text: str | None = None,
                       actor: str = GATEWAY_USERNAME,
                       force: bool = False,
                       existing_raw: LakeRawEntry | None = None) -> dict:
    """Validate + record one device FHIR Observation. The Device
    reference is required and must resolve; the patient must exist;
    the calibration gate applies unless `force` (quarantine release —
    a human has decided). Raises HTTPException on validation failure;
    a gate BLOCK raises 409; a gate QUARANTINE returns
    {"status": "quarantined", ...} without creating anything curated."""
    if not isinstance(obs, dict) or obs.get("resourceType") != "Observation":
        raise HTTPException(status_code=422,
                            detail="Ingestion requires a FHIR Observation resource")
    cfg = devices_config(session)
    subject_ref = (obs.get("subject") or {}).get("reference") or ""
    if not subject_ref.startswith("Patient/") or subject_ref == "Patient/":
        raise HTTPException(status_code=422,
                            detail="Observation requires subject.reference 'Patient/<id>'")
    patient_id = subject_ref.split("/", 1)[1]
    patient = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Patient",
                                   FhirResource.resource_id == patient_id)).first()
    if patient is None:
        raise HTTPException(status_code=422,
                            detail=f"Observation names unknown patient '{patient_id}'")
    device_ref = (obs.get("device") or {}).get("reference") or ""
    if not device_ref.startswith("Device/") or device_ref == "Device/":
        raise HTTPException(
            status_code=422,
            detail="Observation requires device.reference 'Device/<device_key>'")
    device_key = device_ref.split("/", 1)[1]
    device = session.scalars(
        select(Device).where(Device.device_key == device_key)).first()
    if device is None:
        raise HTTPException(status_code=422,
                            detail=f"Observation names unknown device '{device_key}'")
    code, _system = _obs_code(obs)
    if not code:
        raise HTTPException(status_code=422,
                            detail="Observation requires a code (code.coding[0].code)")
    qty = obs.get("valueQuantity")
    value = None
    if qty is not None:
        value = qty.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(status_code=422,
                                detail="Observation valueQuantity.value must be numeric")
    elif obs.get("valueString") is None:
        raise HTTPException(status_code=422,
                            detail="Observation requires valueQuantity or valueString")
    if raw_text is None:
        raw_text = json.dumps(obs, sort_keys=True)
    block = None if force else gate_block_reason(cfg, device)
    if block:
        gate = (cfg.get("mllpGate") or {}).get("onCalibrationOverdue", "block")
        raw = lake.record_raw(session, payload_type="fhir-observation",
                              payload_text=raw_text, source=source,
                              source_label=SIMULATED, device_key=device_key,
                              patient_ref=subject_ref,
                              disposition="quarantined" if gate == "quarantine"
                              else "blocked")
        session.flush()
        if gate == "quarantine":
            hold = DataQuarantine(
                source="device-ingest",
                reason=f"Observation held: {block}",
                detail={"kind": "fhir-observation", "device_key": device_key,
                        "patient_ref": subject_ref},
                raw_entry_id=raw.id)
            session.add(hold)
            session.flush()
            append_audit(session, actor=actor,
                         action="devices.ingest.quarantined",
                         entity=f"Device/{device_key}",
                         payload={"kind": "fhir-observation",
                                  "quarantine_id": hold.id, "reason": block})
            # Commit NOW: the held record must survive the caller's
            # response handling (the API dependency rolls back on a
            # raised exception, and a hold that vanishes on a 202
            # would be silently dropped data — the thing quarantine
            # exists to prevent).
            session.commit()
            return {"status": "quarantined", "quarantine_id": hold.id,
                    "reason": block, "raw_entry_id": raw.id}
        append_audit(session, actor=actor, action="devices.ingest.blocked",
                     entity=f"Device/{device_key}",
                     payload={"kind": "fhir-observation", "reason": block})
        session.commit()  # the blocked raw entry + audit must persist
        raise HTTPException(status_code=409, detail=f"Observation refused: {block}")
    raw = existing_raw or lake.record_raw(
        session, payload_type="fhir-observation",
        payload_text=raw_text, source=source,
        source_label=SIMULATED, device_key=device_key,
        patient_ref=subject_ref)
    obs_id = str(obs.get("id") or uuid.uuid4())
    body = dict(obs)
    body["id"] = obs_id
    body["status"] = body.get("status") or "final"
    body.setdefault("effectiveDateTime",
                    _now().isoformat().replace("+00:00", "Z"))
    exts = [e for e in (body.get("extension") or []) if isinstance(e, dict)]
    exts.append({"url": lake.RAW_ENTRY_EXTENSION, "valueString": str(raw.id)})
    exts.append({"url": SOURCE_EXTENSION, "valueString": SIMULATED})
    body["extension"] = exts
    row = FhirResource(resource_type="Observation", resource_id=obs_id,
                       version=1, body=body)
    fhir._apply_projections(row, "Observation", body, obs_id)
    session.add(row)
    session.flush()
    lake.link_curated(session, raw.id, f"Observation/{obs_id}")
    breach = None
    if value is not None:
        thresholds = (cfg.get("vitalsThresholds") or {}).get(code)
        if thresholds:
            if thresholds.get("low") is not None and value < thresholds["low"]:
                breach = "low"
            elif thresholds.get("high") is not None and value > thresholds["high"]:
                breach = "high"
    if breach:
        from .rules import evaluate_rules
        evaluate_rules(session, "device.vitals.breach", {
            "code": code, "value": float(value), "breach": breach,
            "patient_ref": subject_ref, "device_key": device_key}, actor=actor)
    append_audit(session, actor=actor, action="devices.observation.ingested",
                 entity=f"Observation/{obs_id}",
                 payload={"device_key": device_key, "code": code,
                          "patient_ref": subject_ref, "breach": breach,
                          "raw_entry_id": raw.id, "source": source,
                          "source_label": SIMULATED})
    session.flush()
    return {"status": "recorded", "observation_id": obs_id,
            "patient_ref": subject_ref, "device_key": device_key,
            "breach": breach, "raw_entry_id": raw.id,
            "source_label": SIMULATED}


@router.post("/ingest/observation", status_code=201)
async def ingest_observation_endpoint(request: Request,
                                      session: Session = Depends(get_db),
                                      user: User = Depends(require_capability("devices.manage"))):
    """FHIR Observation ingestion for vitals monitors. The request
    body IS the Observation resource. Simulated feed in this
    deployment — labelled as such in the stored resource."""
    obs = await _json_body(request)
    outcome = ingest_observation(session, obs=obs, source="http",
                                 actor=user.username)
    session.commit()
    if outcome.get("status") == "quarantined":
        return JSONResponse(status_code=202, content=outcome)
    return outcome


# ---------------------------------------------------------------------------
# The MLLP ORU gate (called by app.interop.process_inbound)
# ---------------------------------------------------------------------------

def resolve_sender_device(session: Session, sender: str) -> Device | None:
    if not sender:
        return None
    cfg = devices_config(session)
    mapped = (cfg.get("senderMap") or {}).get(sender)
    if mapped:
        row = session.scalars(
            select(Device).where(Device.device_key == mapped)).first()
        if row is not None:
            return row
    return session.scalars(
        select(Device).where(Device.hl7_sender == sender)).first()


def evaluate_oru_gate(session: Session, segments, raw_text: str) -> dict:
    """Decide what happens to one inbound ORU message BEFORE ingest.

    Always records the raw entry (the lake gets everything). Returns
    {action: proceed|block|dead-letter|quarantine, device, raw_entry_id,
    detail, sender}. Behaviour with no published gate config (or the
    gate disabled) is the Phase 8 legacy behaviour: proceed, no device
    attribution. app.interop owns the ACK; this function owns the
    decision + the raw record."""
    from . import hl7
    msh = hl7.segment_fields(segments, "MSH")
    sender = hl7._hl7_field(msh, 3) if msh else ""
    cfg = devices_config(session)
    gate = cfg.get("mllpGate") or {}
    pid = hl7.patient_id_of(segments)
    patient_ref = f"Patient/{pid}" if pid else None
    if gate.get("enabled") is not True or not sender:
        raw = lake.record_raw(session, payload_type="hl7",
                              payload_text=raw_text, source="mllp",
                              source_label=SIMULATED, interface="mllp",
                              patient_ref=patient_ref)
        return {"action": "proceed", "device": None, "raw_entry_id": raw.id,
                "detail": "", "sender": sender}
    device = resolve_sender_device(session, sender)
    if device is None:
        mode = gate.get("onUnknownDevice", "dead-letter")
        if mode == "allow":
            raw = lake.record_raw(session, payload_type="hl7",
                                  payload_text=raw_text, source="mllp",
                                  source_label=SIMULATED, interface="mllp",
                                  patient_ref=patient_ref)
            return {"action": "proceed", "device": None,
                    "raw_entry_id": raw.id, "detail": "", "sender": sender}
        detail = (f"Unknown device: no registered device for HL7 sending "
                  f"application '{sender}' — message dead-lettered; the raw "
                  "payload is retained in the lake")
        raw = lake.record_raw(session, payload_type="hl7",
                              payload_text=raw_text, source="mllp",
                              source_label=SIMULATED, interface="mllp",
                              patient_ref=patient_ref,
                              disposition="dead-letter")
        append_audit(session, actor=GATEWAY_USERNAME,
                     action="devices.ingest.dead_letter",
                     entity="hl7-inbound/ORU^R01",
                     payload={"sender": sender, "reason": detail,
                              "raw_entry_id": raw.id})
        session.flush()
        return {"action": "dead-letter", "device": None,
                "raw_entry_id": raw.id, "detail": detail, "sender": sender}
    block = gate_block_reason(cfg, device)
    if block:
        mode = gate.get("onCalibrationOverdue", "block")
        if mode == "quarantine":
            detail = (f"Results from {block} — held in quarantine for "
                      "human decision; nothing was ingested")
            raw = lake.record_raw(session, payload_type="hl7",
                                  payload_text=raw_text, source="mllp",
                                  source_label=SIMULATED,
                                  device_key=device.device_key,
                                  interface="mllp", patient_ref=patient_ref,
                                  disposition="quarantined")
            append_audit(session, actor=GATEWAY_USERNAME,
                         action="devices.ingest.quarantined",
                         entity=f"Device/{device.device_key}",
                         payload={"kind": "hl7-oru", "reason": block,
                                  "raw_entry_id": raw.id})
            session.flush()
            return {"action": "quarantine", "device": device,
                    "raw_entry_id": raw.id, "detail": detail,
                    "sender": sender, "block_reason": block}
        detail = (f"Results refused: {block}. The raw payload is "
                  "retained in the lake")
        raw = lake.record_raw(session, payload_type="hl7",
                              payload_text=raw_text, source="mllp",
                              source_label=SIMULATED,
                              device_key=device.device_key, interface="mllp",
                              patient_ref=patient_ref, disposition="blocked")
        append_audit(session, actor=GATEWAY_USERNAME,
                     action="devices.ingest.blocked",
                     entity=f"Device/{device.device_key}",
                     payload={"kind": "hl7-oru", "reason": block,
                              "raw_entry_id": raw.id})
        session.flush()
        return {"action": "block", "device": device, "raw_entry_id": raw.id,
                "detail": detail, "sender": sender}
    raw = lake.record_raw(session, payload_type="hl7", payload_text=raw_text,
                          source="mllp", source_label=SIMULATED,
                          device_key=device.device_key, interface="mllp",
                          patient_ref=patient_ref)
    return {"action": "proceed", "device": device, "raw_entry_id": raw.id,
            "detail": "", "sender": sender}


def hold_oru_quarantine(session: Session, gate: dict, raw_text: str) -> DataQuarantine:
    device = gate.get("device")
    hold = DataQuarantine(
        source="device-ingest",
        reason=f"Held ORU results: {gate.get('block_reason') or gate.get('detail')}",
        detail={"kind": "hl7-oru",
                "device_key": device.device_key if device else None,
                "sender": gate.get("sender")},
        raw_entry_id=gate["raw_entry_id"])
    session.add(hold)
    session.flush()
    return hold


def link_oru_ingest(session: Session, gate: dict, created: list[dict]) -> None:
    """After a gated ORU lands as lab drafts: lineage-link the raw
    entry to each created report and stamp the report body with the
    raw-entry extension (the ingest's own instrument-extension
    pattern)."""
    raw_id = gate.get("raw_entry_id")
    if raw_id is None:
        return
    for item in created:
        report_id = item.get("report_id")
        if not report_id:
            continue
        lake.link_curated(session, raw_id, f"DiagnosticReport/{report_id}")
        row = session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "DiagnosticReport",
                FhirResource.resource_id == report_id)).first()
        if row is not None:
            body = dict(row.body or {})
            exts = [e for e in (body.get("extension") or [])
                    if isinstance(e, dict)]
            if not any(e.get("url") == lake.RAW_ENTRY_EXTENSION for e in exts):
                exts.append({"url": lake.RAW_ENTRY_EXTENSION,
                             "valueString": str(raw_id)})
                body["extension"] = exts
                row.body = body
                row.version += 1
    session.flush()


@router.post("/simulate/oru")
async def simulate_oru(request: Request, session: Session = Depends(get_db),
                       user: User = Depends(require_capability("devices.manage"))):
    """Run one HL7 message through the REAL inbound path
    (app.interop.process_inbound — the same function the MLLP
    listener calls) without a socket. This is the simulated analyser
    feed for development; the response is the ACK the listener would
    return, plus the parsed disposition."""
    payload = await _json_body(request)
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=422, detail="Simulation requires 'content' (the HL7 message)")
    from . import hl7, interop
    ack = interop.process_inbound(session, content, peer="http-simulate")
    session.commit()
    parsed = hl7.parse_ack(ack)
    return {"ack": ack, "ack_code": parsed["ack_code"],
            "detail": parsed["text"], "source_label": SIMULATED}


# ---------------------------------------------------------------------------
# Raw-zone lake browser + lineage (immutable: no PUT/PATCH/DELETE here)
# ---------------------------------------------------------------------------

@router.get("/lake")
def list_lake(payload_type: str | None = Query(default=None),
              device_key: str | None = Query(default=None),
              disposition: str | None = Query(default=None),
              patient_ref: str | None = Query(default=None),
              limit: int = Query(default=100),
              session: Session = Depends(get_db),
              user: User = Depends(require_capability("devices.read"))):
    stmt = select(LakeRawEntry).order_by(LakeRawEntry.id.desc())
    if payload_type:
        stmt = stmt.where(LakeRawEntry.payload_type == payload_type)
    if device_key:
        stmt = stmt.where(LakeRawEntry.device_key == device_key)
    if disposition:
        stmt = stmt.where(LakeRawEntry.disposition == disposition)
    if patient_ref:
        stmt = stmt.where(LakeRawEntry.patient_ref == patient_ref)
    rows = session.scalars(stmt.limit(min(limit, 500))).all()
    total = session.scalars(select(LakeRawEntry)).all()
    by_disposition: dict[str, int] = {}
    for row in total:
        by_disposition[row.disposition] = by_disposition.get(row.disposition, 0) + 1
    return {"items": [lake.raw_entry_out(r) for r in rows],
            "total_matching": len(rows), "total_entries": len(total),
            "by_disposition": by_disposition}


@router.get("/lake/{entry_id}")
def get_lake_entry(entry_id: int, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("devices.read"))):
    row = session.scalars(
        select(LakeRawEntry).where(LakeRawEntry.id == entry_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Raw entry {entry_id} not found")
    out = lake.raw_entry_out(row, include_payload=True)
    out["curated_links"] = [
        {"curated_ref": link.curated_ref, "link_kind": link.link_kind,
         "created_at": link.created_at.isoformat() if link.created_at else None}
        for link in lake.links_for_raw(session, row.id)]
    if row.device_key:
        device = session.scalars(
            select(Device).where(Device.device_key == row.device_key)).first()
        if device is not None:
            out["device"] = {"device_key": device.device_key,
                             "name": device.name,
                             "device_type": device.device_type,
                             "location": device.location}
    return out


@router.get("/lineage/{resource_type}/{resource_id}")
def lineage_for(resource_type: str, resource_id: str,
                session: Session = Depends(get_db),
                user: User = Depends(require_capability("devices.read"))):
    """"Where did this value come from": the raw payload(s) a curated
    record was built from, with the source device/interface and any
    pipeline runs that touched it (run ids ride in the quarantine /
    run detail; links carry the raw side)."""
    curated_ref = f"{resource_type}/{resource_id}"
    links = lake.links_for_curated(session, curated_ref)
    if not links:
        raise HTTPException(
            status_code=404,
            detail=f"No raw-zone lineage recorded for {curated_ref}")
    entries = []
    for link in links:
        raw = session.scalars(
            select(LakeRawEntry).where(LakeRawEntry.id == link.raw_entry_id)).first()
        if raw is None:
            continue
        detail = lake.raw_entry_out(raw, include_payload=True)
        detail["link_kind"] = link.link_kind
        if raw.device_key:
            device = session.scalars(
                select(Device).where(Device.device_key == raw.device_key)).first()
            if device is not None:
                detail["device"] = {"device_key": device.device_key,
                                    "name": device.name,
                                    "device_type": device.device_type,
                                    "location": device.location}
        entries.append(detail)
    return {"curated_ref": curated_ref, "raw_entries": entries,
            "total": len(entries)}


# ---------------------------------------------------------------------------
# Quarantine workqueue (device-ingest holds + pipeline holds share it)
# ---------------------------------------------------------------------------

def _quarantine_out(row: DataQuarantine) -> dict:
    return {"id": row.id, "source": row.source, "reason": row.reason,
            "detail": row.detail or {}, "raw_entry_id": row.raw_entry_id,
            "pipeline_run_id": row.pipeline_run_id, "status": row.status,
            "decided_by": row.decided_by,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            "decision_note": row.decision_note,
            "created_at": row.created_at.isoformat() if row.created_at else None}


@router.get("/quarantine")
def list_quarantine(status: str | None = Query(default=None),
                    source: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability("devices.read"))):
    stmt = select(DataQuarantine).order_by(DataQuarantine.id.desc())
    if status:
        stmt = stmt.where(DataQuarantine.status == status)
    if source:
        stmt = stmt.where(DataQuarantine.source == source)
    rows = session.scalars(stmt).all()
    return {"items": [_quarantine_out(r) for r in rows], "total": len(rows)}


def _release_device_hold(session: Session, hold: DataQuarantine,
                         actor: str) -> dict:
    """Re-run a held device payload into the curated zone (the human
    has decided the data may land despite the gate). Raises
    HTTPException(409) when the payload still cannot be ingested —
    the hold then stays held."""
    raw = session.scalars(
        select(LakeRawEntry).where(LakeRawEntry.id == hold.raw_entry_id)).first()
    if raw is None:
        raise HTTPException(status_code=409,
                            detail="Held entry has no raw payload to release")
    kind = (hold.detail or {}).get("kind")
    device_key = (hold.detail or {}).get("device_key")
    device = session.scalars(
        select(Device).where(Device.device_key == device_key)).first() \
        if device_key else None
    gateway = _gateway_user(session)
    try:
        if kind == "hl7-oru":
            from . import lab as lab_mod
            group = lab_mod.parse_hl7_oru(raw.payload_text)
            created = lab_mod.ingest_hl7_groups(
                session, gateway, [group], source_format="hl7",
                instrument_id=device_key)
            link_oru_ingest(session, {"raw_entry_id": raw.id}, created)
            return {"released_as": [c["report_id"] for c in created]}
        if kind == "fhir-observation":
            obs = json.loads(raw.payload_text)
            outcome = ingest_observation(session, obs=obs, source="quarantine-release",
                                         actor=actor, force=True)
            return {"released_as": [outcome["observation_id"]]}
        if kind == "device-reading":
            data = json.loads(raw.payload_text)
            if device is None:
                raise HTTPException(status_code=409,
                                    detail="Held reading names a device that no longer exists")
            outcome = ingest_reading(
                session, device=device, metric=str(data.get("metric") or ""),
                value=float(data.get("value")), unit=str(data.get("unit") or ""),
                source="quarantine-release", actor=actor)
            return {"released_as": [f"DeviceReading/{outcome['reading']['id']}"]}
    except HTTPException as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Held payload still cannot be ingested: {exc.detail}")
    raise HTTPException(status_code=409,
                        detail=f"Unknown held payload kind '{kind}'")


@router.post("/quarantine/{hold_id}/release")
async def release_quarantine(hold_id: int, request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability("devices.manage"))):
    raw_body = await request.body()
    payload: dict = {}
    if raw_body.strip():
        try:
            parsed = json.loads(raw_body)
        except Exception:
            raise HTTPException(status_code=422, detail="Request body must be valid JSON")
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=422, detail="Request body must be a JSON object")
        payload = parsed
    hold = session.scalars(
        select(DataQuarantine).where(DataQuarantine.id == hold_id)).first()
    if hold is None:
        raise HTTPException(status_code=404, detail=f"Quarantine entry {hold_id} not found")
    if hold.status != "held":
        raise HTTPException(status_code=409,
                            detail=f"Quarantine entry {hold_id} is already {hold.status}")
    if hold.source == "pipeline":
        from . import pipelines as pipeline_mod
        outcome = pipeline_mod.resume_quarantined_run(
            session, hold, actor=user.username,
            session_factory=request.app.state.session_factory)
    elif hold.source == "device-ingest":
        outcome = _release_device_hold(session, hold, actor=user.username)
    else:
        raise HTTPException(status_code=409,
                            detail=f"Unknown quarantine source '{hold.source}'")
    hold.status = "released"
    hold.decided_by = user.username
    hold.decided_at = _now()
    hold.decision_note = str(payload.get("note") or "")
    session.flush()
    append_audit(session, actor=user.username,
                 action="devices.quarantine.release",
                 entity=f"DataQuarantine/{hold.id}",
                 payload={"source": hold.source, "outcome": outcome,
                          "note": hold.decision_note})
    session.commit()
    return {"quarantine": _quarantine_out(hold), "outcome": outcome}


@router.post("/quarantine/{hold_id}/reject")
async def reject_quarantine(hold_id: int, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("devices.manage"))):
    payload = await _json_body(request)
    hold = session.scalars(
        select(DataQuarantine).where(DataQuarantine.id == hold_id)).first()
    if hold is None:
        raise HTTPException(status_code=404, detail=f"Quarantine entry {hold_id} not found")
    if hold.status != "held":
        raise HTTPException(status_code=409,
                            detail=f"Quarantine entry {hold_id} is already {hold.status}")
    note = str(payload.get("note") or "")
    if not note:
        raise HTTPException(status_code=422,
                            detail="Rejecting held data requires a 'note' (the reason stays on record)")
    hold.status = "rejected"
    hold.decided_by = user.username
    hold.decided_at = _now()
    hold.decision_note = note
    session.flush()
    append_audit(session, actor=user.username,
                 action="devices.quarantine.reject",
                 entity=f"DataQuarantine/{hold.id}",
                 payload={"source": hold.source, "note": note})
    session.commit()
    return {"quarantine": _quarantine_out(hold)}


# ---------------------------------------------------------------------------
# Offline store-and-forward queue (deployment mode)
# ---------------------------------------------------------------------------

@router.post("/offline/batches", status_code=201)
async def queue_offline_batch(request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(require_capability("devices.manage"))):
    """Accept a captured batch from an offline edge: every item lands
    in the raw zone with disposition 'held-offline' and NOTHING is
    curated yet — the pipeline replay-lake step processes held items
    when connectivity/governance allows. The mode itself is config:
    the published integration/offline flag must be enabled."""
    cfg_row = published_config(session, "integration", OFFLINE_KEY)
    cfg = (cfg_row.payload or {}) if cfg_row is not None else {}
    if cfg.get("enabled") is not True:
        raise HTTPException(
            status_code=409,
            detail=("Offline store-and-forward is not enabled for this "
                    "deployment (published integration/offline config)"))
    payload = await _json_body(request)
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=422,
                            detail="Offline batch requires a non-empty 'items' list")
    entry_ids = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or item.get("kind") not in (
                "reading", "observation", "hl7"):
            raise HTTPException(
                status_code=422,
                detail=f"items[{i}].kind must be one of reading|observation|hl7")
        kind = item["kind"]
        data = item.get("payload")
        if kind == "hl7":
            if not isinstance(data, str):
                raise HTTPException(status_code=422,
                                    detail=f"items[{i}].payload must be the HL7 text")
            text, ptype = data, "hl7"
        else:
            if not isinstance(data, dict):
                raise HTTPException(status_code=422,
                                    detail=f"items[{i}].payload must be an object")
            text = json.dumps(data, sort_keys=True)
            ptype = "device-reading" if kind == "reading" else "fhir-observation"
        raw = lake.record_raw(
            session, payload_type=ptype, payload_text=text,
            source="offline-queue", source_label=SIMULATED,
            device_key=(data.get("device_key") if isinstance(data, dict)
                        else None),
            disposition="held-offline")
        entry_ids.append(raw.id)
    append_audit(session, actor=user.username, action="devices.offline.queued",
                 entity="offline-batch",
                 payload={"items": len(entry_ids), "raw_entry_ids": entry_ids})
    session.commit()
    return {"queued": len(entry_ids), "raw_entry_ids": entry_ids,
            "disposition": "held-offline", "source_label": SIMULATED}


# ---------------------------------------------------------------------------
# Feed status (the Devices page header + ops surface)
# ---------------------------------------------------------------------------

@router.get("/feeds/status")
def feeds_status(session: Session = Depends(get_db),
                 user: User = Depends(require_capability("devices.read"))):
    cfg = devices_config(session)
    gate = cfg.get("mllpGate") or {}
    devices = session.scalars(select(Device)).all()
    held = session.scalars(
        select(DataQuarantine).where(DataQuarantine.status == "held")).all()
    dead = session.scalars(
        select(LakeRawEntry).where(
            LakeRawEntry.disposition == "dead-letter")).all()
    return {
        "feed_label": SIMULATED,
        "note": ("All device feeds in this deployment are simulated or "
                 "recorded inputs. No live device link is claimed."),
        "devices": {"total": len(devices),
                    "overdue": sum(1 for d in devices
                                   if calibration_state(cfg, d)["status"] == "overdue"),
                    "blocked": sum(1 for d in devices
                                   if gate_block_reason(cfg, d))},
        "mllp_gate": {"enabled": gate.get("enabled") is True,
                      "on_unknown_device": gate.get("onUnknownDevice"),
                      "on_calibration_overdue": gate.get("onCalibrationOverdue")},
        "mqtt": mqtt_status(session),
        "lake_entries": len(session.scalars(select(LakeRawEntry.id)).all()),
        "quarantine_held": len(held),
        "dead_letters": len(dead),
    }


# ---------------------------------------------------------------------------
# MQTT adapter (paho-mqtt). Honestly off with no broker configured.
# ---------------------------------------------------------------------------

def mqtt_config(session: Session) -> dict:
    row = published_config(session, "integration", MQTT_KEY)
    return (row.payload or {}) if row is not None else {}


def parse_broker_url(url: str) -> tuple[str, int]:
    text = url.strip()
    for prefix in ("mqtt://", "tcp://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.split("/")[0]
    if ":" in text:
        host, _, port = text.rpartition(":")
        return host or "127.0.0.1", int(port)
    return text or "127.0.0.1", 1883


class MqttAdapter:
    """Subscribes to the configured device topics and routes every
    message through :meth:`handle_message` — the same ingestion core
    the HTTP endpoints use, so a reading that arrives by MQTT is
    indistinguishable in the data from one posted over HTTP (apart
    from its recorded source). The adapter is constructed only when a
    broker URL is configured; tests drive it against a real localhost
    broker (amqtt) and drive handle_message directly with recorded
    payloads."""

    def __init__(self, session_factory, cfg: dict):
        self.session_factory = session_factory
        self.cfg = cfg
        self.broker_url = str(cfg.get("brokerUrl") or "")
        devices_cfg_topic = cfg.get("topicPrefix") or "opencare/devices/"
        self.topic_prefix = devices_cfg_topic
        self.topics = cfg.get("topics") or [f"{self.topic_prefix}+/readings"]
        self.state = "off"          # off | connecting | connected | error
        self.last_error: str | None = None
        self.counts = {"received": 0, "recorded": 0, "quarantined": 0,
                       "blocked": 0, "dead_letter": 0, "ignored": 0,
                       "error": 0}
        self._client = None
        self._lock = threading.Lock()

    # -- message handling (transport-free; recorded payloads welcome) --

    def handle_message(self, topic: str, payload) -> dict:
        if isinstance(payload, bytes):
            text = payload.decode("utf-8", errors="replace")
        else:
            text = str(payload)
        with self._lock:
            self.counts["received"] += 1
        outcome = self._route(topic, text)
        with self._lock:
            key = outcome.get("status")
            if key in self.counts:
                self.counts[key] += 1
        return outcome

    def _route(self, topic: str, text: str) -> dict:
        prefix = self.topic_prefix
        if not topic.startswith(prefix):
            return {"status": "ignored",
                    "reason": f"topic '{topic}' is outside the configured prefix '{prefix}'"}
        rest = topic[len(prefix):]
        parts = rest.split("/")
        if len(parts) != 2 or parts[1] != "readings" or not parts[0]:
            return {"status": "ignored",
                    "reason": f"topic '{topic}' does not match '{{prefix}}{{device_key}}/readings'"}
        topic_key = parts[0]
        try:
            data = json.loads(text)
        except Exception:
            return self._dead(topic_key, text,
                              "payload is not valid JSON")
        if not isinstance(data, dict):
            return self._dead(topic_key, text, "payload must be a JSON object")
        with self.session_factory() as session:
            device = session.scalars(
                select(Device).where(Device.mqtt_topic_key == topic_key)).first()
            if device is None:
                device = session.scalars(
                    select(Device).where(Device.device_key == topic_key)).first()
            if device is None:
                return self._dead(topic_key, text,
                                  f"no registered device for topic key '{topic_key}'",
                                  session=session)
            cfg = devices_config(session)
            block = gate_block_reason(cfg, device)
            if block:
                mode = (cfg.get("mllpGate") or {}).get("onCalibrationOverdue",
                                                       "block")
                raw = lake.record_raw(
                    session, payload_type="device-reading", payload_text=text,
                    source="mqtt", source_label=SIMULATED,
                    device_key=device.device_key,
                    disposition="quarantined" if mode == "quarantine" else "blocked")
                if mode == "quarantine":
                    hold = DataQuarantine(
                        source="device-ingest",
                        reason=f"MQTT reading held: {block}",
                        detail={"kind": "device-reading",
                                "device_key": device.device_key},
                        raw_entry_id=raw.id)
                    session.add(hold)
                    session.commit()
                    return {"status": "quarantined", "device_key": device.device_key,
                            "quarantine_id": hold.id, "reason": block}
                session.commit()
                return {"status": "blocked", "device_key": device.device_key,
                        "reason": block}
            metric = data.get("metric")
            value = data.get("value")
            if not isinstance(metric, str) or not metric.strip() \
                    or isinstance(value, bool) \
                    or not isinstance(value, (int, float)):
                return self._dead(topic_key, text,
                                  "reading payload requires 'metric' (string) and 'value' (number)",
                                  session=session)
            recorded = None
            if data.get("recorded_at"):
                try:
                    recorded = _parse_dt(data["recorded_at"], "recorded_at")
                except HTTPException:
                    return self._dead(topic_key, text,
                                      "'recorded_at' is not a valid ISO datetime",
                                      session=session)
            outcome = ingest_reading(session, device=device, metric=metric,
                                     value=float(value),
                                     unit=str(data.get("unit") or ""),
                                     recorded_at=recorded, source="mqtt",
                                     raw_text=text)
            session.commit()
            return {"status": "recorded", "device_key": device.device_key,
                    "reading_id": outcome["reading"]["id"],
                    "excursion": outcome["excursion"]}

    def _dead(self, topic_key: str, text: str, reason: str,
              session: Session | None = None) -> dict:
        own = session is None
        if own:
            session = self.session_factory()
        try:
            raw = lake.record_raw(session, payload_type="device-reading",
                                  payload_text=text, source="mqtt",
                                  source_label=SIMULATED,
                                  disposition="dead-letter")
            append_audit(session, actor=GATEWAY_USERNAME,
                         action="devices.mqtt.dead_letter", entity="mqtt",
                         payload={"topic_key": topic_key, "reason": reason,
                                  "raw_entry_id": raw.id})
            session.commit()
        finally:
            if own:
                session.close()
        return {"status": "dead_letter", "reason": reason}

    # -- broker lifecycle --

    def start(self) -> bool:
        if not self.broker_url:
            self.state = "off"
            return False
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.state = "error"
            self.last_error = "paho-mqtt is not installed in this environment"
            return False
        host, port = parse_broker_url(self.broker_url)
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"opencare-devices-{uuid.uuid4().hex[:8]}")
        username = self.cfg.get("username")
        if username:
            client.username_pw_set(str(username),
                                   str(self.cfg.get("password") or ""))

        def _on_connect(c, userdata, flags, reason_code, properties=None):
            # paho v2 delivers a ReasonCode object, not a plain int.
            try:
                rc = int(getattr(reason_code, "value", reason_code))
            except (TypeError, ValueError):
                rc = 1
            if rc == 0:
                self.state = "connected"
                for topic in self.topics:
                    c.subscribe(topic)
            else:
                self.state = "error"
                self.last_error = f"broker refused connection: {reason_code}"

        def _on_disconnect(c, userdata, *args):
            if self.state == "connected":
                self.state = "connecting"

        def _on_message(c, userdata, message):
            try:
                self.handle_message(message.topic, message.payload)
            except Exception as exc:  # a poison message must not kill the loop
                with self._lock:
                    self.counts["error"] += 1
                self.last_error = f"{type(exc).__name__}: {exc}"

        client.on_connect = _on_connect
        client.on_disconnect = _on_disconnect
        client.on_message = _on_message
        try:
            self.state = "connecting"
            client.connect(host, port, keepalive=30)
            client.loop_start()
        except Exception as exc:
            self.state = "error"
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        self._client = client
        return True

    def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
        self.state = "off"

    def status(self) -> dict:
        return {"state": self.state, "broker_url": self.broker_url or None,
                "topics": list(self.topics), "counts": dict(self.counts),
                "last_error": self.last_error, "source_label": SIMULATED}


_ADAPTERS: dict[str, MqttAdapter] = {}


def mqtt_status(session: Session) -> dict:
    """The honest feed status: with no broker URL in the published
    integration/mqtt config, the adapter is OFF and says why. When a
    broker is configured, a live adapter's state is reported if one
    is running in this process; otherwise 'configured-not-connected'
    (the adapter runs as its own service — serve_mqtt)."""
    cfg = mqtt_config(session)
    broker_url = str(cfg.get("brokerUrl") or "")
    if not broker_url:
        return {"configured": False, "state": "off",
                "reason": ("No MQTT broker configured: the published "
                           "integration/mqtt config has no brokerUrl. The "
                           "adapter is honestly off — no device link is "
                           "claimed. Message handling is exercised with "
                           "recorded payloads (POST /devices/mqtt/"
                           "test-message) and against a localhost broker "
                           "in the test suite."),
                "source_label": SIMULATED}
    adapter = _ADAPTERS.get(broker_url)
    if adapter is not None:
        out = adapter.status()
        out["configured"] = True
        return out
    return {"configured": True, "state": "configured-not-connected",
            "broker_url": broker_url,
            "reason": ("A broker is configured but no adapter is running "
                       "in this API process; the adapter runs as its own "
                       "service beside the API (serve_mqtt, the "
                       "mllp_server pattern)."),
            "source_label": SIMULATED}


def serve_mqtt(session_factory) -> MqttAdapter:
    """Entry point for the standalone adapter service (mirrors
    app.mllp_server.serve): build config from the database, start the
    adapter, return it (the caller owns the process lifetime)."""
    with session_factory() as session:
        cfg = mqtt_config(session)
        ensure_devices_defaults(session)
    adapter = MqttAdapter(session_factory, cfg)
    _ADAPTERS[adapter.broker_url] = adapter
    adapter.start()
    return adapter


@router.get("/mqtt/status")
def get_mqtt_status(session: Session = Depends(get_db),
                    user: User = Depends(require_capability("devices.read"))):
    return mqtt_status(session)


@router.post("/mqtt/test-message")
async def mqtt_test_message(request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("devices.manage"))):
    """Drive the adapter's message handler with a RECORDED payload —
    no broker involved. Proves routing/gating/ingestion logic; the
    response names it a recorded-payload test, never a live feed."""
    payload = await _json_body(request)
    topic = payload.get("topic")
    message = payload.get("payload")
    if not isinstance(topic, str) or not topic.strip():
        raise HTTPException(status_code=422, detail="Test message requires 'topic'")
    if not isinstance(message, (str, dict)):
        raise HTTPException(status_code=422,
                            detail="Test message requires 'payload' (JSON string or object)")
    text = message if isinstance(message, str) else json.dumps(message)
    cfg = mqtt_config(session)
    adapter = MqttAdapter(lambda: session, cfg)
    outcome = adapter.handle_message(topic.strip(), text)
    session.commit()
    return {"outcome": outcome, "mode": "recorded-payload",
            "note": ("Recorded-payload handler test — no broker was "
                     "involved and no live feed is claimed."),
            "source_label": SIMULATED}


# ---------------------------------------------------------------------------
# Seeds: device config (published), registry rows, register entries
# ---------------------------------------------------------------------------

def default_devices_payload() -> dict:
    return {
        # The device-type value set rides as `concepts` so the
        # document stays a valid, republishable terminology config
        # through the governed API (the Phase 12/13 pattern).
        "concepts": [
            {"code": "analyser", "display": "Laboratory analyser", "active": True},
            {"code": "vitals-monitor", "display": "Bedside vitals monitor", "active": True},
            {"code": "fridge-sensor", "display": "Storage temperature sensor", "active": True},
            {"code": "pump", "display": "Infusion pump", "active": True},
            # Depth pass: point-of-care testing devices (glucose
            # meters, blood-gas / cartridge analysers at the bedside).
            # The POCT programme (app.lab_benches) governs their QC +
            # operator competency; the registry row lives here like
            # every other device.
            {"code": "poct-analyser", "display": "Point-of-care testing device", "active": True},
        ],
        "deviceTypes": [
            {"type": "analyser", "label": "Laboratory analyser",
             "calibrationIntervalDays": 180, "maintenanceIntervalDays": 365},
            {"type": "vitals-monitor", "label": "Bedside vitals monitor",
             "calibrationIntervalDays": 365, "maintenanceIntervalDays": 180},
            {"type": "fridge-sensor", "label": "Storage temperature sensor",
             "calibrationIntervalDays": 365, "maintenanceIntervalDays": 365,
             "ownerRole": "pharmacy",
             "ranges": {"temperature": {"min": 2.0, "max": 8.0, "unit": "C"}}},
            {"type": "pump", "label": "Infusion pump",
             "calibrationIntervalDays": 365, "maintenanceIntervalDays": 180},
            {"type": "poct-analyser", "label": "Point-of-care testing device",
             "calibrationIntervalDays": 180, "maintenanceIntervalDays": 365},
        ],
        "statuses": ["active", "maintenance", "out-of-service", "retired"],
        "blockingStatuses": ["out-of-service", "retired"],
        "calibrationWorklistWindowDays": 30,
        "mllpGate": {"enabled": True, "onUnknownDevice": "dead-letter",
                     "onCalibrationOverdue": "block"},
        "senderMap": {"ANALYZER-X": "chem-analyser-1",
                      "HEME-A2": "heme-analyser-1"},
        "vitalsThresholds": {
            "8867-4": {"label": "Heart rate", "low": 40, "high": 140},
            "9279-1": {"label": "Respiratory rate", "low": 6, "high": 35},
            "8310-5": {"label": "Body temperature", "low": 35.0, "high": 39.5},
            "8480-6": {"label": "Systolic BP", "low": 70, "high": 190},
            "8462-4": {"label": "Diastolic BP", "low": 40, "high": 120},
            "2708-6": {"label": "Oxygen saturation", "low": 88, "high": None},
        },
    }


def default_device_rows() -> list[dict]:
    now = _now()
    return [
        {"device_key": "chem-analyser-1", "name": "Chemistry Analyser X1",
         "device_type": "analyser", "location": "Central Laboratory",
         "serial": "SN-CHEM-1001", "hl7_sender": "ANALYZER-X",
         "last_calibrated_at": now - timedelta(days=60),
         "calibration_due": now + timedelta(days=120),
         "maintenance_due": now + timedelta(days=300)},
        {"device_key": "heme-analyser-1", "name": "Haematology Analyser A2",
         "device_type": "analyser", "location": "Central Laboratory",
         "serial": "SN-HEME-2002", "hl7_sender": "HEME-A2",
         "last_calibrated_at": now - timedelta(days=135),
         "calibration_due": now + timedelta(days=45),
         "maintenance_due": now + timedelta(days=200)},
        {"device_key": "vitals-monitor-1", "name": "Bedside Monitor ICU-01",
         "device_type": "vitals-monitor", "location": "ICU",
         "serial": "SN-VM-3001",
         "last_calibrated_at": now - timedelta(days=100),
         "calibration_due": now + timedelta(days=265),
         "maintenance_due": now + timedelta(days=80)},
        {"device_key": "fridge-pharmacy-1", "name": "Pharmacy Fridge 1",
         "device_type": "fridge-sensor", "location": "Pharmacy",
         "serial": "SN-FR-4001", "mqtt_topic_key": "fridge-pharmacy-1",
         "last_calibrated_at": now - timedelta(days=30),
         "calibration_due": now + timedelta(days=335),
         "maintenance_due": now + timedelta(days=335)},
        {"device_key": "fridge-lab-1", "name": "Laboratory Fridge 1",
         "device_type": "fridge-sensor", "location": "Central Laboratory",
         "serial": "SN-FR-4002", "mqtt_topic_key": "fridge-lab-1",
         "last_calibrated_at": now - timedelta(days=30),
         "calibration_due": now + timedelta(days=335),
         "maintenance_due": now + timedelta(days=335)},
        {"device_key": "pump-icu-1", "name": "Infusion Pump ICU-01",
         "device_type": "pump", "location": "ICU",
         "serial": "SN-PP-5001",
         "last_calibrated_at": now - timedelta(days=200),
         "calibration_due": now + timedelta(days=165),
         "maintenance_due": now + timedelta(days=160)},
    ]


def default_requirement_seeds() -> list[dict]:
    return [
        {"body": "The Joint Commission", "citation": "EC.02.04.03",
         "title": "Medical equipment inspection, testing and maintenance",
         "control": ("Every device in the registry carries calibration "
                     "and maintenance due dates computed from the "
                     "published device-type intervals; overdue devices "
                     "appear on the calibration worklist and their "
                     "results are blocked or quarantined at ingestion "
                     "per the published gate policy — an overdue "
                     "analyser cannot silently keep reporting."),
         "implementation_ref": "backend/app/devices.py (registry, "
                               "calibration worklist, evaluate_oru_gate)",
         "evidence_source": "audit:devices."},
        {"body": "CLIA", "citation": "42 CFR 493.1253",
         "title": "Calibration and calibration verification — laboratory analysers",
         "control": ("Analyser calibration is recorded as an event "
                     "(performed date + the published interval computes "
                     "the next due date — never a hand-typed due date), "
                     "and results arriving from an analyser whose "
                     "calibration is overdue are refused or held at "
                     "the MLLP gate with the raw payload retained."),
         "implementation_ref": "backend/app/devices.py "
                               "(record_calibration, evaluate_oru_gate); "
                               "backend/app/interop.py (process_inbound)",
         "evidence_source": "audit:devices.calibration"},
    ]


def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
    from .configstore import list_config_versions
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


def ensure_devices_defaults(session: Session) -> None:
    """Seed the device config + offline mode (published v1), the
    synthetic device registry, and the Phase 14 register entries.
    Idempotent throughout."""
    _seed_published(session, "terminology", DEVICES_KEY,
                    default_devices_payload())
    _seed_published(session, "integration", OFFLINE_KEY, {
        "enabled": True, "mode": "store-and-forward",
        "note": ("Offline-first deployment mode: edge captures queue "
                 "into the raw zone as held entries and the pipeline "
                 "replay-lake step curates them when connectivity "
                 "returns. No separate sync engine.")})
    for spec in default_device_rows():
        if session.scalars(select(Device).where(
                Device.device_key == spec["device_key"])).first() is None:
            session.add(Device(status="active", **spec))
    session.flush()
    existing = {(r.body, r.citation) for r in
                session.scalars(select(ComplianceRequirement)).all()}
    added = 0
    for seed in default_requirement_seeds():
        if (seed["body"], seed["citation"]) in existing:
            continue
        session.add(ComplianceRequirement(created_by="system", **seed))
        added += 1
    if added:
        session.flush()
        append_audit(session, actor="system",
                     action="compliance.register.seed", entity="register",
                     payload={"added": added, "phase": 14})
    session.commit()


# ---------------------------------------------------------------------------
# Device detail — declared LAST: /{device_key} would otherwise shadow
# the one-segment literal routes above (/lake, /quarantine, /readings).
# ---------------------------------------------------------------------------

@router.get("/{device_key}")
def get_device(device_key: str, session: Session = Depends(get_db),
               user: User = Depends(require_capability("devices.read"))):
    cfg = devices_config(session)
    device = _device_row(session, device_key)
    out = _device_out(cfg, device)
    readings = session.scalars(
        select(DeviceReading).where(DeviceReading.device_key == device_key)
        .order_by(DeviceReading.id.desc()).limit(20)).all()
    out["recent_readings"] = [_reading_out(r) for r in readings]
    return out
