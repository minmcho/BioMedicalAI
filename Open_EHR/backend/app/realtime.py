"""Realtime fan-out — Phase 17 (BD-69, ARC-09).

Server-Sent Events for board / command-centre updates, replacing
per-board polling where it matters (command centre first), with the
polling path retained as a first-class fallback:

- ``GET /realtime/stream?channel=command&since=<id>`` — an SSE stream.
  Events since ``since`` replay first (the buffer is the catch-up
  mechanism), then live events arrive as they are published, with a
  ``: heartbeat`` comment every ~15s of silence. Browsers consume it
  through ``fetch`` + ReadableStream (see web/src/api/realtime.ts)
  because EventSource cannot send the bearer token.
- ``GET /realtime/events?channel=command&since=<id>`` — the SAME
  buffered events as JSON. This is the polling fallback's server path:
  the web client polls it when the stream cannot be held, so both
  paths deliver identical events.

Event sources (no invented domain): a SQLAlchemy ``before_flush``
listener on the Session class — the app.outbox pattern — watches for
new :class:`~app.models.AdtEvent` rows (admit A01 / transfer A02 /
discharge A03 / update A08: every bed movement the inpatient module
records) and publishes a ``board.changed`` event on the ``command``
channel in the same process. :func:`publish` is the explicit API for
other producers. Delivery is in-process fan-out (single-instance
deployments); the buffer is bounded (last 500 events per channel)
and process-local by design — a restart loses buffered events and
clients resync by polling, which is exactly the retained fallback.

Studio surface: kind ``integration``, key ``realtime`` —
``{"enabled": bool, "channels": [str]}``. Default: enabled, channel
``command``. When disabled, both endpoints answer 503.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import event
from sqlalchemy.orm import Session

from .configstore import published_config
from .models import AdtEvent, User
from .security import get_db, require_capability

router = APIRouter(prefix="/realtime", tags=["realtime"])

REALTIME_KIND = "integration"
REALTIME_KEY = "realtime"
DEFAULT_CHANNELS = ("command",)
_BUFFER_MAX = 500

_lock = threading.Lock()
_buffers: dict[str, deque] = {}
_next_id: dict[str, int] = {}
_subscribers: dict[str, int] = {}


def realtime_config(session: Session) -> dict:
    cfg = published_config(session, REALTIME_KIND, REALTIME_KEY)
    payload = dict(cfg.payload) if cfg is not None and isinstance(
        cfg.payload, dict) else {}
    enabled = payload.get("enabled")
    channels = payload.get("channels")
    if not isinstance(channels, list) or not channels or not all(
            isinstance(c, str) and c for c in channels):
        channels = list(DEFAULT_CHANNELS)
    return {"enabled": enabled if isinstance(enabled, bool) else True,
            "channels": channels}


def publish(channel: str, event_type: str, payload: dict) -> dict:
    """Append one event to a channel buffer and return it. This is
    the fan-out point: SSE generators and the polling endpoint both
    read the same buffer."""
    with _lock:
        event_id = _next_id.get(channel, 0) + 1
        _next_id[channel] = event_id
        record = {
            "id": event_id, "channel": channel, "type": event_type,
            "payload": payload,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        buf = _buffers.setdefault(channel, deque(maxlen=_BUFFER_MAX))
        buf.append(record)
    try:
        from . import observability

        observability.record_realtime_event()
        observability.set_realtime_subscribers(
            sum(_subscribers.values()))
    except Exception:
        pass
    return record


def events_since(channel: str, since: int = 0) -> list[dict]:
    with _lock:
        buf = list(_buffers.get(channel, ()))
    return [e for e in buf if e["id"] > since]


def latest_id(channel: str) -> int:
    with _lock:
        return _next_id.get(channel, 0)


# ---------------------------------------------------------------------------
# Domain-event source: ADT rows (the outbox listener pattern)
# ---------------------------------------------------------------------------

@event.listens_for(Session, "before_flush")
def _publish_adt_events(session: Session, flush_context, instances) -> None:
    for row in list(session.new):
        if isinstance(row, AdtEvent):
            publish("command", "board.changed", {
                "source": "adt", "event_type": row.event_type,
                "encounter_id": row.encounter_id,
                "patient_ref": row.patient_ref,
                "from_location": row.from_location,
                "to_location": row.to_location,
            })


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _check_channel(session: Session, channel: str) -> dict:
    cfg = realtime_config(session)
    if not cfg["enabled"]:
        raise HTTPException(
            status_code=503,
            detail=("Realtime fan-out is disabled by the published "
                    "integration/realtime config"))
    if channel not in cfg["channels"]:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown realtime channel '{channel}' "
                   f"(configured: {', '.join(cfg['channels'])})")
    return cfg


@router.get("/events")
def poll_events(channel: str = Query(default="command"),
                since: int = Query(default=0),
                session: Session = Depends(get_db),
                user: User = Depends(require_capability("continuum.read"))):
    """The polling fallback path: buffered events after ``since``."""
    _check_channel(session, channel)
    return {"channel": channel, "events": events_since(channel, since),
            "latest_id": latest_id(channel)}


@router.get("/stream")
async def stream(request: Request, channel: str = Query(default="command"),
                 since: int = Query(default=0),
                 session: Session = Depends(get_db),
                 user: User = Depends(require_capability("continuum.read"))):
    """SSE stream: replay from ``since``, then live events."""
    _check_channel(session, channel)

    async def generate():
        # Disconnect handling: deliberately NOT a request.is_disconnected()
        # poll — under the TestClient ASGI transport that call awaits a
        # disconnect message that only arrives on disconnect, stalling
        # the generator before it can yield buffered events. On a real
        # server (uvicorn) a client disconnect cancels this generator
        # at its next await, which the finally below handles; that is
        # the standard SSE pattern.
        cursor = since
        with _lock:
            _subscribers[channel] = _subscribers.get(channel, 0) + 1
        try:
            yield ": opencare realtime connected\n\n"
            last_beat = time.monotonic()
            while True:
                fresh = events_since(channel, cursor)
                for record in fresh:
                    cursor = record["id"]
                    yield f"id: {record['id']}\n"
                    yield f"event: {record['type']}\n"
                    yield f"data: {json.dumps(record)}\n\n"
                now = time.monotonic()
                if fresh:
                    last_beat = now
                elif now - last_beat >= 15:
                    yield ": heartbeat\n\n"
                    last_beat = now
                await asyncio.sleep(0.05)
        finally:
            with _lock:
                _subscribers[channel] = max(
                    0, _subscribers.get(channel, 1) - 1)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
