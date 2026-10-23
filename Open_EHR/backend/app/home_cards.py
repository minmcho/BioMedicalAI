"""Home card menu data — Home & Module UX pass (app.home_cards).

GET /home/cards returns one live card per staff module that has a
REAL provider: {cards: {module: {value, label, status_text, tone,
timer_text?}}}. Every number is computed at call time by REUSING an
existing service computation — the indicator library's NAMED_SOURCES
(app.indicators), the continuum boarding snapshot (app.continuum),
the pharmacy verification-queue membership helpers
(app.pharmacy_ops), the bed-state helper behind the workforce
analytics (app.people._bed_stats) and the inventory worklist
computations (app.inventory._low_stock_rows / _expiry_rows). A
module with no honest provider
is ABSENT from the response — the Home card menu shows its purpose
line and an Open action instead of an invented number (fixed
contract, PROGRESS ledger).

Tone: derived from the thresholds of the PUBLISHED indicator
definition bound to the same named source, when that definition
carries thresholds (app.indicators._status_for: critical -> bad,
warn -> warn, ok -> ok); sources without thresholds read `neutral`
with a factual status_text. Auth: any authenticated staff role; the
patient role gets an empty card set (the portal is their home).

GET /home/effective-modules exposes app.configstore.effective_modules
(the published-nav role resolution intersected with the caller's
job profile) for the web shell wave to consume.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from . import continuum, inventory, people, pharmacy_ops
from .configstore import (OPERATOR_ROLES, caller_job_role,
                          effective_modules, published_configs)
from .indicators import NAMED_SOURCES, _status_for
from .models import User
from .security import get_current_user, get_db

router = APIRouter(prefix="/home", tags=["home"])

_TONE_BY_STATUS = {"ok": "ok", "warn": "warn", "critical": "bad"}


def _threshold_tone(session: Session, source_name: str, value) -> str:
    """The tone for a value under the published indicator definition
    bound to `source_name`, when that definition carries thresholds;
    `neutral` otherwise (the source has no governed tolerance)."""
    for cfg in published_configs(session, "indicator"):
        payload = cfg.payload or {}
        source = payload.get("source") or {}
        if source.get("type") == "named" and source.get("name") == source_name:
            if payload.get("thresholds"):
                return _TONE_BY_STATUS.get(
                    _status_for(value, payload), "neutral")
            return "neutral"
    return "neutral"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or singular + "s")


def _timer_text(oldest_wait_minutes: int) -> str:
    minutes = max(0, int(oldest_wait_minutes))
    if minutes < 60:
        return f"oldest wait {minutes}m"
    return f"oldest wait {minutes // 60}h {minutes % 60:02d}m"


def _card(value, label: str, status_text: str, tone: str,
          timer_text: str | None = None) -> dict:
    card = {"value": value, "label": label, "status_text": status_text,
            "tone": tone}
    if timer_text is not None:
        card["timer_text"] = timer_text
    return card


# ---------------------------------------------------------------------------
# Providers — one per module with an honest computation
# ---------------------------------------------------------------------------

def _inbox_card(session: Session) -> dict:
    pending = NAMED_SOURCES["lab.pending-verification"](session)
    critical = NAMED_SOURCES["lab.critical-open"](session)
    status = ("No critical results open" if critical == 0 else
              f"{critical} critical {_plural(critical, 'result')} open")
    return _card(pending, "Results awaiting verification", status,
                 _threshold_tone(session, "lab.pending-verification", pending))


def _lab_card(session: Session) -> dict:
    critical = NAMED_SOURCES["lab.critical-open"](session)
    pending = NAMED_SOURCES["lab.pending-verification"](session)
    return _card(critical, "Critical results open",
                 f"{pending} {_plural(pending, 'report')} awaiting "
                 "verification",
                 _threshold_tone(session, "lab.critical-open", critical))


def _pharmacy_card(session: Session) -> dict:
    verified = pharmacy_ops._verified_order_ids(session)
    awaiting = sum(1 for row in pharmacy_ops._open_med_orders(session)
                   if row.resource_id not in verified)
    from . import pharmacy
    dispensing = pharmacy.dispense_queue(session=session, user=None)["total"]
    return _card(awaiting, "Orders awaiting verification",
                 f"{dispensing} awaiting dispensing", "neutral")


def _beds_card(session: Session) -> dict:
    occupied = NAMED_SOURCES["census.current"](session)
    stats = people._bed_stats(session)
    total = sum(b["beds_total"] for b in stats.values())
    available = sum(b["available"] for b in stats.values())
    status = (f"{available} of {total} beds available" if total
              else "No beds configured")
    return _card(occupied, "Patients in beds", status, "neutral")


def _command_card(session: Session) -> dict:
    snap = continuum.boarding_snapshot(session)
    ready = NAMED_SOURCES["continuum.discharge-ready"](session)
    timer = _timer_text(snap["oldest_wait_minutes"]) if snap["total"] else None
    return _card(snap["total"], "Patients boarding",
                 f"{ready} {_plural(ready, 'patient')} ready for discharge",
                 _threshold_tone(session, "continuum.boarding-total",
                                 snap["total"]),
                 timer_text=timer)


def _ed_card(session: Session) -> dict:
    snap = continuum.boarding_snapshot(session)
    timer = _timer_text(snap["oldest_wait_minutes"]) if snap["total"] else None
    return _card(snap["ed_total"], "ED patients boarding",
                 f"{snap['total']} boarding house-wide", "neutral",
                 timer_text=timer)


def _safety_card(session: Session) -> dict:
    open_events = NAMED_SOURCES["safety.open-events"](session)
    rca_open = NAMED_SOURCES["safety.rca-open"](session)
    return _card(open_events, "Open safety events",
                 f"{rca_open} {_plural(rca_open, 'RCA')} open",
                 _threshold_tone(session, "safety.open-events", open_events))


def _workforce_card(session: Session) -> dict:
    active = NAMED_SOURCES["workforce.staff-active"](session)
    expiring = NAMED_SOURCES["workforce.credentials-expiring"](session)
    return _card(active, "Active staff",
                 f"{expiring} {_plural(expiring, 'credential')} expiring "
                 "in the worklist window", "neutral")


def _scheduling_card(session: Session) -> dict:
    appointments = NAMED_SOURCES["registration.frontdesk-appointments"](session)
    checkin = NAMED_SOURCES["registration.frontdesk-awaiting-checkin"](session)
    room = NAMED_SOURCES["registration.frontdesk-awaiting-room"](session)
    return _card(appointments, "Appointments today",
                 f"{checkin} awaiting check-in, {room} awaiting a room",
                 "neutral")


def _inventory_card(session: Session) -> dict:
    # Inventory & Materials pass: real count sources exist — the
    # below-PAR worklist and the governed-window expiry worklist.
    low_stock = inventory._low_stock_rows(session)
    expiring = inventory._expiry_rows(session)
    expired = sum(1 for r in expiring if r["expired"])
    return _card(len(low_stock), "Stock lines below PAR",
                 f"{len(expiring)} {_plural(len(expiring), 'lot')} "
                 f"expiring or expired in the governed window "
                 f"({expired} expired)", "neutral")


_PROVIDERS = {
    "inbox": _inbox_card,
    "lab": _lab_card,
    "pharmacy": _pharmacy_card,
    "beds": _beds_card,
    "command": _command_card,
    "ed": _ed_card,
    "safety": _safety_card,
    "workforce": _workforce_card,
    "scheduling": _scheduling_card,
    "inventory": _inventory_card,
}


def build_home_cards(session: Session) -> dict:
    """The card set for one staff caller, computed live. Only
    modules with a provider in _PROVIDERS appear — never a zero
    invented for a module nobody can compute."""
    return {module: provider(session)
            for module, provider in _PROVIDERS.items()}


@router.get("/cards")
def home_cards(session: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """Live Home cards for the card menu. Any authenticated staff
    role; the patient role gets an empty set (their home is the
    portal, not the staff card menu)."""
    if user.role.value == "patient":
        return {"cards": {}}
    return {"cards": build_home_cards(session)}


@router.get("/effective-modules")
def home_effective_modules(session: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """The caller's effective menu (app.configstore.effective_modules):
    the published-nav role resolution intersected with their job
    profile when their job role is resolvable. The web shell wave
    wires consumption; this is the endpoint home for the resolver.
    Operator roles (admin / configurator) report job_role null:
    job profiles scope staff menus, never operator menus, so no
    consumer of this payload can profile an operator's menu down."""
    job_role = None
    if user.role.value != "patient" \
            and user.role.value not in OPERATOR_ROLES:
        job_role = caller_job_role(session, user)
    return {"modules": effective_modules(session, user),
            "job_role": job_role}
