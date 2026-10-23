"""Indicator library — PLATFORM PASS Workstream 8 (app.indicators).

Governed KPI definitions as Studio configuration of kind
``indicator``, so a dashboard KPI has ONE shared, versioned source of
truth instead of each widget re-deriving it from a live endpoint.
Dashboards themselves are NOT rewired in this pass (their widgets
still consume live endpoints); this library is the shared source
going forward — stated, not implied.

Definition payload: {name, title?, source, thresholds?, target?,
direction, unit?} where source is one of:

- {"type": "measure", "measure_key"} — evaluated by the Phase 9
  quality engine (app.quality.compute_measure) over the OMOP
  warehouse, exactly as app.quality / people analytics do (warehouse
  session factory + ensure_concept_map). The indicator value is the
  measure RATE (percent).
- {"type": "cohort", "cohort_key"} — evaluated by the Phase 3
  population-health registry evaluator (app.pophealth
  .registry_summary) over the warehouse. The value is the registry
  member count.
- {"type": "named", "name"} — a small in-module registry mapping a
  name to an EXISTING service computation (see NAMED_SOURCES). Names
  outside the registry are refused 422 at definition time: a named
  indicator can only ever bind reviewed server code.

Governance: in-module draft -> publish mirroring app.studio_config,
over the shared StudioConfig store (kind ``indicator``). NOTE
(pending integration): the generic Studio API does not yet know this
kind — registering ``indicator`` in app.configstore is a pending
integration edit; governance here enforces the identical lifecycle.
Capabilities (existing only): governance rides ``studio.manage``;
reads and value evaluation ride ``studio.read``.

Value semantics — "unknown" is defined precisely: the response value
is a NUMBER whenever the source yields data (a genuine 0 rows is the
fact 0, never "unknown"). Status ``unknown`` (value null) happens
only when the source yields NO data: no published measure/cohort
config for the named key, a measure whose counts are small-cell
suppressed or whose denominator is zero (rate is None), or a named
source that is no longer computable. Nothing is fabricated.

Status from thresholds + direction (thresholds are the definition's
own {warn?, critical?}):
- higher-better: value <= critical -> critical; value <= warn ->
  warn; otherwise ok (a healthy value is HIGH).
- lower-better: value >= critical -> critical; value >= warn ->
  warn; otherwise ok (a healthy value is LOW).
With no thresholds a known value is ``ok``. `target` is reported
back for display; it does not by itself change status.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import pophealth as ph
from . import quality as quality_module
from . import warehouse as warehouse_module
from .audit import append_audit
from .configstore import published_config, role_allowed
from .models import ConfigStatus, SafetyEvent, StudioConfig, User
from .security import get_db, require_capability

router = APIRouter(prefix="/indicators", tags=["indicators"])

KIND = "indicator"
DIRECTIONS = ("higher-better", "lower-better")
SOURCE_TYPES = ("measure", "cohort", "named")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Named sources — existing service computations only
# ---------------------------------------------------------------------------

def _named_census_current(session: Session):
    """Current inpatient census (occupied beds, house-wide) via
    app.people._bed_stats — the same service computation the
    workforce analytics overview uses (bed state from the Phase 6
    location machinery)."""
    from .people import _bed_stats
    stats = _bed_stats(session)
    return sum(bucket["occupied"] for bucket in stats.values())


def _named_lab_pending_verification(session: Session):
    """DiagnosticReports awaiting verification/release: reports whose
    lab order (found via app.lab._report_order, the lab module's own
    basedOn lookup) is in status resulted or verified — the exact
    membership rule of the lab verification-queue handler."""
    from . import fhir, lab  # noqa: F401  (lab reuses fhir helpers)
    from .models import FhirResource
    count = 0
    for report in session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "DiagnosticReport")).all():
        order = lab._report_order(session, report)
        if order is not None and (order.body or {}).get("status") in (
                "resulted", "verified"):
            count += 1
    return count


def _named_lab_critical_open(session: Session):
    """Released reports containing a critical value — the exact
    membership rule of the lab critical-reports handler (order
    released AND app.lab._report_is_critical)."""
    from . import lab
    from .models import FhirResource
    count = 0
    for report in session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "DiagnosticReport")).all():
        order = lab._report_order(session, report)
        if order is None or (order.body or {}).get("status") != "released":
            continue
        if lab._report_is_critical(session, report):
            count += 1
    return count


def _named_safety_open_events(session: Session):
    """Open safety events: SafetyEvent rows whose status is not
    'closed' — the same rule as the workforce analytics overview."""
    return len(session.scalars(
        select(SafetyEvent).where(SafetyEvent.status != "closed")).all())


# --- Phase 17 (WS-RESID, BD-30 remainder): named sources extending the
# library so every remaining scalar dashboard widget can resolve
# through a governed indicator. Each function reproduces the EXACT
# computation of the live endpoint the widget used before (the same
# module helpers / membership rules the handler itself uses), so an
# indicator value and the source endpoint's field can never diverge
# silently — the platform-depth tests assert their equality.

def _named_workforce_admissions_today(session: Session):
    """Admissions today: AdtEvent rows of type A01 created today —
    the exact rule of app.people.analytics_overview (house-wide)."""
    from . import people
    from .models import AdtEvent
    today = people._today()
    return sum(1 for e in session.scalars(select(AdtEvent)).all()
               if e.event_type == "A01" and e.created_at
               and e.created_at.date() == today)


def _named_workforce_discharges_today(session: Session):
    """Discharges today: AdtEvent rows of type A03 created today —
    the exact rule of app.people.analytics_overview (house-wide)."""
    from . import people
    from .models import AdtEvent
    today = people._today()
    return sum(1 for e in session.scalars(select(AdtEvent)).all()
               if e.event_type == "A03" and e.created_at
               and e.created_at.date() == today)


def _named_workforce_staff_active(session: Session):
    """Active staff: StaffMember rows with status 'active' — the
    exact rule of app.people.analytics_overview (house-wide)."""
    from .models import StaffMember
    return len(session.scalars(select(StaffMember).where(
        StaffMember.status == "active")).all())


def _named_workforce_credentials_expiring(session: Session):
    """Credentials expiring inside the published workforce worklist
    window: the exact loop of app.people.analytics_overview over
    StaffCredential rows (expired rows count separately there and are
    NOT counted here)."""
    from . import people
    from .models import StaffCredential
    window, _alert = people._expiry_windows(people._ops(session))
    expiring = 0
    for cred in session.scalars(select(StaffCredential)).all():
        days = people._days_to_expiry(cred.expires_on)
        if days is not None and 0 <= days <= window:
            expiring += 1
    return expiring


def _named_workforce_occupancy_pct(session: Session):
    """House occupancy %: occupied / total beds across
    app.people._bed_stats — the exact house-wide rule of
    app.people.analytics_overview (0.0 when no beds exist)."""
    from . import people
    beds = people._bed_stats(session)
    occupied = sum(b["occupied"] for b in beds.values())
    beds_total = sum(b["beds_total"] for b in beds.values())
    return (round(100.0 * occupied / beds_total, 1) if beds_total else 0.0)


def _named_workforce_coverage_pct(session: Session):
    """Staffing coverage %: app.people._coverage's overall fill rate
    against the published roster minimums (None — indicator status
    'unknown' — when no unit carries a published minimum, exactly as
    the coverage endpoint reports overall_pct null)."""
    from . import people
    return people._coverage(session, people._ops(session))["overall_pct"]


def _named_continuum_discharge_ready(session: Session):
    """Patients ready for discharge: the discharge-readiness board's
    own helpers (app.continuum._active_admissions +
    app.continuum._readiness) — the exact computation of
    GET /continuum/inpatient/discharge-readiness's `ready` count."""
    from . import continuum
    now = continuum._now()
    return sum(1 for row in continuum._active_admissions(session)
               if continuum._readiness(session, row, now)["ready"])


def _named_continuum_discharge_board_total(session: Session):
    """Admissions tracked on the discharge-readiness board: the
    board's `total` — every active admission, planned or not."""
    from . import continuum
    return len(continuum._active_admissions(session))


def _named_continuum_boarding_total(session: Session):
    """Patients boarding (awaiting a bed): app.continuum's own
    boarding_snapshot total — the exact value behind
    GET /continuum/er/boarding."""
    from . import continuum
    return continuum.boarding_snapshot(session)["total"]


def _named_safety_total_events(session: Session):
    """Safety events (all time, including closed): the `total` of
    GET /continuum/safety/summary — every SafetyEvent row."""
    return len(session.scalars(select(SafetyEvent)).all())


def _named_safety_rca_open(session: Session):
    """Open RCA work items: SafetyEvent rows with rca_required and
    rca_status 'open' — the exact rule of the safety summary's
    `rca_open` field."""
    return len(session.scalars(select(SafetyEvent).where(
        SafetyEvent.rca_required.is_(True),
        SafetyEvent.rca_status == "open")).all())


def _frontdesk_field(session: Session, field: str):
    """One counter from app.registration.frontdesk_today, called
    directly (its body never uses the caller — the capability check
    lives in the route dependency, and indicator reads ride
    studio.read like every named source here)."""
    from . import registration
    return registration.frontdesk_today(session=session,
                                        user=None)[field]


def _named_frontdesk_appointments(session: Session):
    """Appointments today (front desk): frontdesk_today's
    appointments_total."""
    return _frontdesk_field(session, "appointments_total")


def _named_frontdesk_awaiting_checkin(session: Session):
    """Awaiting check-in (front desk): frontdesk_today's
    awaiting_checkin."""
    return _frontdesk_field(session, "awaiting_checkin")


def _named_frontdesk_awaiting_room(session: Session):
    """Arrived, awaiting a room (front desk): frontdesk_today's
    awaiting_room."""
    return _frontdesk_field(session, "awaiting_room")


def _named_frontdesk_waitlist_open(session: Session):
    """Open waitlist (front desk): frontdesk_today's waitlist_open."""
    return _frontdesk_field(session, "waitlist_open")


def _named_frontdesk_registrations_today(session: Session):
    """Registered today (front desk): frontdesk_today's
    registrations_today."""
    return _frontdesk_field(session, "registrations_today")


def _named_radiology_worklist_total(session: Session):
    """Studies on the RIS worklist: app.radiology.get_worklist
    unfiltered, called directly (explicit None filters — the route's
    Query defaults must not leak in)."""
    from . import radiology
    return radiology.get_worklist(state=None, priority=None,
                                  modality=None, session=session,
                                  user=None)["total"]


def _named_radiology_tat_breaches(session: Session):
    """TAT breaches (reported studies): app.radiology.tat_stats'
    house-wide `breaches` total."""
    from . import radiology
    return radiology.tat_stats(session=session, user=None)["breaches"]


def _named_radiology_unreported_stat(session: Session):
    """STAT studies not yet reported: app.radiology.tat_stats'
    `unreported_stat`."""
    from . import radiology
    return radiology.tat_stats(session=session,
                               user=None)["unreported_stat"]


def _named_radiology_critical_open(session: Session):
    """Radiology critical results on record: app.radiology
    .list_critical's `total` (the widget's original endpoint total;
    the handler's list carries every critical-result record)."""
    from . import radiology
    return radiology.list_critical(session=session, user=None)["total"]


NAMED_SOURCES = {
    "census.current": _named_census_current,
    "lab.pending-verification": _named_lab_pending_verification,
    "lab.critical-open": _named_lab_critical_open,
    "safety.open-events": _named_safety_open_events,
    # Phase 17 BD-30 remainder (see the block comment above).
    "workforce.admissions-today": _named_workforce_admissions_today,
    "workforce.discharges-today": _named_workforce_discharges_today,
    "workforce.staff-active": _named_workforce_staff_active,
    "workforce.credentials-expiring": _named_workforce_credentials_expiring,
    "workforce.occupancy-pct": _named_workforce_occupancy_pct,
    "workforce.coverage-pct": _named_workforce_coverage_pct,
    "continuum.discharge-ready": _named_continuum_discharge_ready,
    "continuum.discharge-board-total": _named_continuum_discharge_board_total,
    "continuum.boarding-total": _named_continuum_boarding_total,
    "safety.total-events": _named_safety_total_events,
    "safety.rca-open": _named_safety_rca_open,
    "registration.frontdesk-appointments": _named_frontdesk_appointments,
    "registration.frontdesk-awaiting-checkin":
        _named_frontdesk_awaiting_checkin,
    "registration.frontdesk-awaiting-room": _named_frontdesk_awaiting_room,
    "registration.frontdesk-waitlist-open": _named_frontdesk_waitlist_open,
    "registration.frontdesk-registrations-today":
        _named_frontdesk_registrations_today,
    "radiology.worklist-total": _named_radiology_worklist_total,
    "radiology.tat-breaches": _named_radiology_tat_breaches,
    "radiology.unreported-stat": _named_radiology_unreported_stat,
    "radiology.critical-open": _named_radiology_critical_open,
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_indicator(payload) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422,
                            detail="Indicator definition must be a JSON object")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=422,
                            detail="'name' is required (non-empty string)")
    if "title" in payload and not isinstance(payload["title"], str):
        raise HTTPException(status_code=422, detail="'title' must be a string")
    source = payload.get("source")
    if not isinstance(source, dict):
        raise HTTPException(status_code=422, detail="'source' must be an object")
    stype = source.get("type")
    if stype not in SOURCE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"'source.type' must be one of {list(SOURCE_TYPES)}")
    if stype == "measure":
        key = source.get("measure_key")
        if not isinstance(key, str) or not key.strip():
            raise HTTPException(status_code=422,
                                detail="'source.measure_key' is required")
    elif stype == "cohort":
        key = source.get("cohort_key")
        if not isinstance(key, str) or not key.strip():
            raise HTTPException(status_code=422,
                                detail="'source.cohort_key' is required")
    else:
        if source.get("name") not in NAMED_SOURCES:
            raise HTTPException(
                status_code=422,
                detail=(f"Unknown named source '{source.get('name')}'; "
                        f"named sources: {sorted(NAMED_SOURCES)}"))
    if payload.get("direction") not in DIRECTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"'direction' must be one of {list(DIRECTIONS)}")
    thresholds = payload.get("thresholds")
    if thresholds is not None:
        if not isinstance(thresholds, dict):
            raise HTTPException(status_code=422,
                                detail="'thresholds' must be an object")
        for field in ("warn", "critical"):
            value = thresholds.get(field)
            if value is not None and (isinstance(value, bool)
                                      or not isinstance(value, (int, float))):
                raise HTTPException(
                    status_code=422,
                    detail=f"'thresholds.{field}' must be a number")
    target = payload.get("target")
    if target is not None and (isinstance(target, bool)
                               or not isinstance(target, (int, float))):
        raise HTTPException(status_code=422, detail="'target' must be a number")
    if "unit" in payload and not isinstance(payload["unit"], str):
        raise HTTPException(status_code=422, detail="'unit' must be a string")
    return payload


# ---------------------------------------------------------------------------
# In-module governance (mirrors app.studio_config for the new kind)
# ---------------------------------------------------------------------------

def _versions(session: Session, key: str) -> list[StudioConfig]:
    return list(session.scalars(
        select(StudioConfig).where(StudioConfig.kind == KIND,
                                   StudioConfig.key == key)
        .order_by(StudioConfig.version.asc())).all())


def _published(session: Session, key: str) -> StudioConfig | None:
    return published_config(session, KIND, key)


def _out(row: StudioConfig) -> dict:
    return {"kind": row.kind, "key": row.key, "version": row.version,
            "status": row.status.value, "payload": row.payload,
            "created_by": row.created_by, "updated_by": row.updated_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def _can_see(session: Session, row: StudioConfig, user: User) -> bool:
    if role_allowed(session, user.role, "studio.manage"):
        return True
    return row.status == ConfigStatus.published


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


@router.post("/definitions/{key}", status_code=201)
async def create_definition(key: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("studio.manage"))):
    payload = validate_indicator(await _json_body(request))
    versions = _versions(session, key)
    if any(v.status == ConfigStatus.draft for v in versions):
        raise HTTPException(
            status_code=409,
            detail=f"A draft already exists for {KIND}/{key}; publish it first")
    next_version = (versions[-1].version + 1) if versions else 1
    row = StudioConfig(kind=KIND, key=key, version=next_version,
                       status=ConfigStatus.draft, payload=payload,
                       created_by=user.username, updated_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="indicator.definition.create",
                 entity=f"config:{KIND}/{key}@v{next_version}",
                 payload={"kind": KIND, "key": key, "version": next_version})
    session.commit()
    return _out(row)


@router.put("/definitions/{key}/versions/{version}")
async def update_definition(key: str, version: int, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("studio.manage"))):
    row = next((v for v in _versions(session, key) if v.version == version),
               None)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Config {KIND}/{key} v{version} not found")
    if row.status != ConfigStatus.draft:
        raise HTTPException(
            status_code=409,
            detail=f"Config {KIND}/{key} v{version} is '{row.status.value}' "
                   "and immutable; create a new draft version")
    row.payload = validate_indicator(await _json_body(request))
    row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username,
                 action="indicator.definition.update",
                 entity=f"config:{KIND}/{key}@v{version}",
                 payload={"kind": KIND, "key": key, "version": version})
    session.commit()
    return _out(row)


@router.post("/definitions/{key}/versions/{version}/publish")
def publish_definition(key: str, version: int,
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("studio.manage"))):
    row = next((v for v in _versions(session, key) if v.version == version),
               None)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Config {KIND}/{key} v{version} not found")
    if row.status == ConfigStatus.published:
        raise HTTPException(status_code=409,
                            detail=f"Config {KIND}/{key} v{version} is already published")
    if row.status == ConfigStatus.superseded:
        raise HTTPException(status_code=409,
                            detail="Superseded versions cannot be republished directly")
    current = _published(session, key)
    superseded = None
    if current is not None and current.id != row.id:
        current.status = ConfigStatus.superseded
        superseded = current.version
        session.flush()
    row.status = ConfigStatus.published
    row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username,
                 action="indicator.definition.publish",
                 entity=f"config:{KIND}/{key}@v{version}",
                 payload={"kind": KIND, "key": key, "version": version,
                          "superseded_version": superseded})
    session.commit()
    return _out(row)


@router.get("/definitions")
def list_definitions(session: Session = Depends(get_db),
                     user: User = Depends(require_capability("studio.read"))):
    rows = session.scalars(
        select(StudioConfig).where(StudioConfig.kind == KIND)
        .order_by(StudioConfig.key.asc(), StudioConfig.version.asc())).all()
    items = [_out(r) for r in rows if _can_see(session, r, user)]
    return {"items": items, "total": len(items)}


@router.get("/definitions/{key}")
def get_definition(key: str, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("studio.read"))):
    rows = [r for r in _versions(session, key) if _can_see(session, r, user)]
    if not rows:
        raise HTTPException(status_code=404,
                            detail=f"No config found for {KIND}/{key}")
    published = _published(session, key)
    return {"key": key, "versions": [_out(r) for r in rows],
            "published": _out(published) if published else None}


@router.get("/named-sources")
def list_named_sources(session: Session = Depends(get_db),
                       user: User = Depends(require_capability("studio.read"))):
    return {"items": sorted(NAMED_SOURCES), "total": len(NAMED_SOURCES)}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _status_for(value, definition: dict) -> str:
    if value is None:
        return "unknown"
    thresholds = definition.get("thresholds") or {}
    warn = thresholds.get("warn")
    critical = thresholds.get("critical")
    if definition.get("direction") == "higher-better":
        if critical is not None and value <= critical:
            return "critical"
        if warn is not None and value <= warn:
            return "warn"
    else:  # lower-better
        if critical is not None and value >= critical:
            return "critical"
        if warn is not None and value >= warn:
            return "warn"
    return "ok"


def evaluate_indicator(session: Session, key: str,
                       definition: dict) -> dict:
    """Evaluate one published indicator definition against live data.
    Returns the response dict; value is None (status unknown) only
    when the source yields no data — see module docstring."""
    source = definition.get("source") or {}
    stype = source.get("type")
    value = None
    detail: dict = {}
    if stype == "measure":
        measure_key = source.get("measure_key")
        cfg = published_config(session, "measure", measure_key)
        if cfg is None:
            detail = {"reason": f"no published measure config '{measure_key}'"}
        else:
            factory = warehouse_module.warehouse_session_factory()
            with factory() as wh_session:
                warehouse_module.ensure_concept_map(wh_session)
                result = quality_module.compute_measure(
                    wh_session, measure_key, cfg.payload or {})
            detail = {"measure": measure_key,
                      "numerator": result["numerator"],
                      "denominator": result["denominator"],
                      "suppressed": result["suppressed"],
                      "as_of": result["as_of"]}
            if result["rate"] is not None:
                value = float(result["rate"])
            else:
                detail["reason"] = ("measure rate unavailable "
                                    "(suppressed counts or zero denominator)")
    elif stype == "cohort":
        cohort_key = source.get("cohort_key")
        cfg = published_config(session, "cohort", cohort_key)
        if cfg is None:
            detail = {"reason": f"no published cohort config '{cohort_key}'"}
        else:
            factory = warehouse_module.warehouse_session_factory()
            with factory() as wh_session:
                warehouse_module.ensure_concept_map(wh_session)
                summary = ph.registry_summary(wh_session, cohort_key,
                                              cfg.payload or {})
            value = int(summary["member_count"])
            detail = {"cohort": cohort_key, "member_count": value,
                      "as_of": summary["stratification"]["as_of"]}
    elif stype == "named":
        fn = NAMED_SOURCES.get(source.get("name"))
        if fn is None:
            detail = {"reason": f"named source '{source.get('name')}' "
                                "is not in the registry"}
        else:
            value = fn(session)
            detail = {"named": source.get("name")}
    else:
        detail = {"reason": f"unknown source type '{stype}'"}
    unit = definition.get("unit")
    if unit is None and stype == "measure":
        unit = "%"
    if unit is None and stype == "cohort":
        unit = "persons"
    return {"key": key, "value": value, "unit": unit,
            "target": definition.get("target"),
            "status": _status_for(value, definition),
            "evaluated_at": _now().isoformat(),
            "source": source, "detail": detail}


@router.get("/{key}/value")
def indicator_value(key: str, session: Session = Depends(get_db),
                    user: User = Depends(require_capability("studio.read"))):
    cfg = _published(session, key)
    if cfg is None:
        raise HTTPException(status_code=404,
                            detail=f"No published indicator '{key}'")
    return evaluate_indicator(session, key, cfg.payload or {})


# ---------------------------------------------------------------------------
# Records drill-down — GET /indicators/{key}/records (Home & Module UX
# pass, WS4a). Min's rule: any reported number must drill to its
# definition -> the underlying record list -> the individual record.
# A resolver enumerates the ACTUAL records behind the number, reusing
# the same underlying queries / membership rules the counter uses, so
# for count-type indicators len(records) == value by construction.
# Ratio/percentage sources list their NUMERATOR-side records and say
# so in `reason` (NAMED_RECORD_NOTES), unless the numerator is itself
# an aggregate with no honest record set — those, and the warehouse
# measure/cohort sources, return records_supported=false with an
# honest reason and NO rows. Never fake rows.
#
# Record shape: {id, label, detail, at, patient_id} — id is the
# underlying record's real id; patient_id is the chart patient id when
# the record is patient-linked (one-click chart open), else None.
# ---------------------------------------------------------------------------

def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return None


def _ref_id(ref, prefix: str) -> str | None:
    if isinstance(ref, str) and ref.startswith(prefix + "/"):
        return ref.split("/", 1)[1]
    return None


def _subject_patient_id(body: dict) -> str | None:
    subject = (body or {}).get("subject") or {}
    if isinstance(subject, dict):
        return _ref_id(subject.get("reference"), "Patient")
    return None


def _patient_display(session: Session, cache: dict, patient_id: str | None):
    """Human name for a Patient id, resolved from the live chart row
    (registration.patient_display_name); falls back to the ref."""
    if not patient_id:
        return None
    if patient_id not in cache:
        from .models import FhirResource as _FR
        from .registration import patient_display_name
        row = session.scalars(select(_FR).where(
            _FR.resource_type == "Patient",
            _FR.resource_id == patient_id)).first()
        cache[patient_id] = (patient_display_name(row.body or {})
                             if row is not None else f"Patient/{patient_id}")
    return cache[patient_id]


def _record(rid, label, detail, at=None, patient_id=None) -> dict:
    return {"id": str(rid), "label": label, "detail": detail,
            "at": _iso(at), "patient_id": patient_id}


def _code_label(body: dict) -> str:
    """Display text for a FHIR CodeableConcept-ish body['code']."""
    code = (body or {}).get("code") or {}
    if isinstance(code, dict):
        if code.get("text"):
            return str(code["text"])
        for coding in code.get("coding") or []:
            if isinstance(coding, dict):
                return str(coding.get("display") or coding.get("code")
                           or "code")
    return "—"


# --- census.current -------------------------------------------------------

def _occupied_bed_entries(session: Session) -> list[dict]:
    """Occupied-bed entries via the SAME machinery the counter sums
    (people._bed_stats is built from inpatient._location_concepts +
    inpatient._bed_entry): one entry per bed whose live status is
    'occupied', so len(result) == census.current's value."""
    from . import inpatient
    _units, beds = inpatient._location_concepts(session)
    now = _now()
    entries = []
    for concept in beds:
        entry = inpatient._bed_entry(session, concept, now)
        if entry.get("status") == "occupied":
            entries.append(entry)
    return entries


def _records_census_current(session: Session) -> list[dict]:
    cache: dict = {}
    records = []
    for entry in _occupied_bed_entries(session):
        patient_id = _ref_id(entry.get("patient_ref"), "Patient")
        name = _patient_display(session, cache, patient_id)
        bed = entry.get("code")
        records.append(_record(
            entry.get("encounter_id") or bed,
            f"{name or entry.get('patient_ref') or 'Patient'} — bed {bed}",
            f"Bed {bed} · unit {entry.get('unit') or '—'} · occupied",
            at=entry.get("admitted_at"), patient_id=patient_id))
    return records


def _records_occupancy_pct(session: Session) -> list[dict]:
    """Numerator-side records for workforce.occupancy-pct: the
    occupied beds (the percentage itself is occupied / total beds —
    see NAMED_RECORD_NOTES). Identical enumeration to census.current."""
    return _records_census_current(session)


# --- lab queues -----------------------------------------------------------

def _lab_report_records(session: Session, kind: str) -> list[dict]:
    """Shared enumeration for the two lab named sources — the same
    DiagnosticReport scan + lab._report_order membership rules the
    counters use. kind: 'pending-verification' | 'critical-open'."""
    from . import lab
    from .models import FhirResource
    cache: dict = {}
    records = []
    for report in session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "DiagnosticReport")).all():
        order = lab._report_order(session, report)
        if order is None:
            continue
        order_status = (order.body or {}).get("status")
        if kind == "pending-verification":
            if order_status not in ("resulted", "verified"):
                continue
        else:  # critical-open
            if order_status != "released":
                continue
            if not lab._report_is_critical(session, report):
                continue
        body = report.body or {}
        patient_id = _subject_patient_id(body) \
            or _subject_patient_id(order.body or {})
        name = _patient_display(session, cache, patient_id)
        label_code = _code_label(body)
        records.append(_record(
            report.resource_id,
            f"{label_code} — {name or 'unknown patient'}",
            f"Report {report.resource_id} · order {order.resource_id} "
            f"· order status {order_status}"
            + (" · critical value" if kind == "critical-open" else ""),
            at=body.get("issued") or report.created_at,
            patient_id=patient_id))
    return records


def _records_lab_pending_verification(session: Session) -> list[dict]:
    return _lab_report_records(session, "pending-verification")


def _records_lab_critical_open(session: Session) -> list[dict]:
    return _lab_report_records(session, "critical-open")


# --- safety ---------------------------------------------------------------

def _safety_record(row: SafetyEvent) -> dict:
    return _record(
        row.event_id,
        f"{row.category} event {row.event_id}",
        f"status {row.status} · harm {row.harm_level}"
        + (f" · unit {row.unit}" if row.unit else "")
        + (f" · RCA {row.rca_status}" if row.rca_required else ""),
        at=row.occurred_at or row.created_at,
        patient_id=_ref_id(row.patient_ref, "Patient"))


def _records_safety_open(session: Session) -> list[dict]:
    return [_safety_record(r) for r in session.scalars(
        select(SafetyEvent).where(SafetyEvent.status != "closed")).all()]


def _records_safety_total(session: Session) -> list[dict]:
    return [_safety_record(r) for r in session.scalars(
        select(SafetyEvent)).all()]


def _records_safety_rca_open(session: Session) -> list[dict]:
    return [_safety_record(r) for r in session.scalars(
        select(SafetyEvent).where(
            SafetyEvent.rca_required.is_(True),
            SafetyEvent.rca_status == "open")).all()]


# --- workforce ------------------------------------------------------------

def _adt_records(session: Session, event_type: str, verb: str) -> list[dict]:
    """Today's ADT events of one type — the same scan
    app.people.analytics_overview's rule (and the counters) use."""
    from . import people
    from .models import AdtEvent
    today = people._today()
    cache: dict = {}
    records = []
    for event in session.scalars(select(AdtEvent)).all():
        if event.event_type != event_type or not event.created_at \
                or event.created_at.date() != today:
            continue
        patient_id = _ref_id(event.patient_ref, "Patient")
        name = _patient_display(session, cache, patient_id)
        bed = (event.detail or {}).get("bed")
        records.append(_record(
            event.id,
            f"{verb} — {name or event.patient_ref or 'unknown patient'}",
            f"Encounter {event.encounter_id or '—'}"
            + (f" · bed {bed}" if bed else "")
            + f" · {event.to_location or event.from_location or ''}",
            at=event.created_at, patient_id=patient_id))
    return records


def _records_workforce_admissions(session: Session) -> list[dict]:
    return _adt_records(session, "A01", "Admission")


def _records_workforce_discharges(session: Session) -> list[dict]:
    return _adt_records(session, "A03", "Discharge")


def _records_workforce_staff_active(session: Session) -> list[dict]:
    from .models import StaffMember
    return [_record(
        row.staff_id,
        row.full_name or row.staff_id,
        f"{row.job_role or 'staff'}"
        + (f" · unit {row.unit}" if row.unit else "") + " · active",
        at=row.created_at, patient_id=None)
        for row in session.scalars(select(StaffMember).where(
            StaffMember.status == "active")).all()]


def _records_workforce_credentials_expiring(session: Session) -> list[dict]:
    """The same loop the counter runs (people._expiry_windows +
    _days_to_expiry over every StaffCredential)."""
    from . import people
    from .models import StaffCredential, StaffMember
    window, _alert = people._expiry_windows(people._ops(session))
    names = {s.staff_id: s.full_name for s in
             session.scalars(select(StaffMember)).all()}
    records = []
    for cred in session.scalars(select(StaffCredential)).all():
        days = people._days_to_expiry(cred.expires_on)
        if days is None or not (0 <= days <= window):
            continue
        records.append(_record(
            cred.credential_id,
            f"{cred.type or 'Credential'} — "
            f"{names.get(cred.staff_id) or cred.staff_id}",
            f"expires {cred.expires_on} · in {days} day(s) "
            f"· window {window} day(s)",
            at=None, patient_id=None))
    return records


# --- continuum ------------------------------------------------------------

def _continuum_admission_records(session: Session,
                                 only_ready: bool) -> list[dict]:
    """Active admissions via continuum._active_admissions, with
    readiness from continuum._readiness — the board's own helpers,
    exactly as the counters use them."""
    from . import continuum
    now = continuum._now()
    cache: dict = {}
    records = []
    for row in continuum._active_admissions(session):
        readiness = continuum._readiness(session, row, now)
        if only_ready and not readiness["ready"]:
            continue
        patient_id = _ref_id(readiness.get("patient_ref"), "Patient")
        name = _patient_display(session, cache, patient_id)
        if only_ready:
            detail = (f"Bed {readiness.get('bed') or '—'} · ready"
                      f" · target {readiness.get('target_date') or '—'}")
        else:
            missing = readiness.get("missing") or []
            detail = (f"Bed {readiness.get('bed') or '—'} · "
                      + ("ready" if readiness["ready"]
                         else "missing: " + ", ".join(missing)))
        records.append(_record(
            row.resource_id,
            name or readiness.get("patient_ref") or row.resource_id,
            detail, at=row.created_at, patient_id=patient_id))
    return records


def _records_continuum_discharge_ready(session: Session) -> list[dict]:
    return _continuum_admission_records(session, only_ready=True)


def _records_continuum_discharge_board(session: Session) -> list[dict]:
    return _continuum_admission_records(session, only_ready=False)


def _records_continuum_boarding(session: Session) -> list[dict]:
    from . import continuum
    cache: dict = {}
    records = []
    for item in continuum.boarding_snapshot(session)["items"]:
        patient_id = _ref_id(item.get("patient_ref"), "Patient")
        name = _patient_display(session, cache, patient_id)
        records.append(_record(
            item["encounter_id"],
            name or item.get("patient_ref") or item["encounter_id"],
            f"Boarding · source {item.get('source')}"
            f" · requested unit {item.get('requested_unit') or '—'}"
            f" · waiting {item.get('wait_minutes')} min",
            at=item.get("waiting_since"), patient_id=patient_id))
    return records


# --- registration / front desk ---------------------------------------------

def _frontdesk_items(session: Session) -> list[dict]:
    from . import registration
    return registration.frontdesk_today(session=session,
                                        user=None)["items"]


def _frontdesk_item_record(item: dict) -> dict:
    return _record(
        item["appointment_id"],
        item.get("patient") or item.get("patient_ref") or "Appointment",
        f"Status {item.get('status')} · {item.get('time') or ''}"
        + (f" · session {item['session']}" if item.get("session") else ""),
        at=item.get("start"),
        patient_id=_ref_id(item.get("patient_ref"), "Patient"))


def _records_frontdesk_appointments(session: Session) -> list[dict]:
    return [_frontdesk_item_record(i) for i in _frontdesk_items(session)]


def _records_frontdesk_awaiting_checkin(session: Session) -> list[dict]:
    # The counter increments exactly for status 'booked' items.
    return [_frontdesk_item_record(i) for i in _frontdesk_items(session)
            if i.get("status") == "booked"]


def _records_frontdesk_awaiting_room(session: Session) -> list[dict]:
    # The counter increments for arrived/checked-in with no rooming;
    # the item's `roomed` flag is that same rooming check.
    return [_frontdesk_item_record(i) for i in _frontdesk_items(session)
            if i.get("status") in ("arrived", "checked-in")
            and not i.get("roomed")]


def _records_frontdesk_waitlist(session: Session) -> list[dict]:
    from .models import WaitlistEntry
    from .registration import OPEN_WAITLIST_STATUSES
    cache: dict = {}
    records = []
    for row in session.scalars(select(WaitlistEntry).where(
            WaitlistEntry.status.in_(OPEN_WAITLIST_STATUSES))).all():
        patient_id = _ref_id(row.patient_ref, "Patient")
        name = _patient_display(session, cache, patient_id)
        records.append(_record(
            row.entry_id,
            name or row.patient_ref,
            f"Waitlist · slot {row.slot_type} · priority {row.priority}"
            f" · status {row.status}",
            at=row.created_at, patient_id=patient_id))
    return records


def _records_frontdesk_registrations(session: Session) -> list[dict]:
    """Patients created today — the counter's own scan."""
    from .models import FhirResource
    from .registration import patient_display_name
    today = _now().date()
    records = []
    for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Patient")).all():
        if row.created_at is None or row.created_at.date() != today:
            continue
        records.append(_record(
            row.resource_id,
            patient_display_name(row.body or {}),
            "Registered today", at=row.created_at,
            patient_id=row.resource_id))
    return records


# --- radiology --------------------------------------------------------------

def _radiology_study_record(session: Session, cache: dict, item: dict,
                            detail: str) -> dict:
    patient_id = _ref_id(item.get("patient_ref"), "Patient")
    name = _patient_display(session, cache, patient_id)
    modality = item.get("modality") or "Imaging"
    description = item.get("description") or item["study_id"]
    return _record(
        item["study_id"], f"{modality} {description} — {name or 'patient'}",
        detail, at=item.get("ordered_at"), patient_id=patient_id)


def _records_radiology_worklist(session: Session) -> list[dict]:
    from . import radiology
    cache: dict = {}
    out = radiology.get_worklist(state=None, priority=None, modality=None,
                                 session=session, user=None)
    return [_radiology_study_record(
        session, cache, item,
        f"State {item.get('state')} · priority {item.get('priority')}")
        for item in out["items"]]


def _radiology_tat_scan(session: Session):
    """Per-study TAT facts mirroring radiology.tat_stats' own loop
    (same helpers: _entry_for / _derive_priority / _first_final_report
    / _tat_target), split into (breaches, unreported_stat) item lists
    in get_worklist item shape."""
    from . import radiology
    from .models import FhirResource
    ops = radiology._ops(session)
    breaches, unreported = [], []
    studies = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "ImagingStudy")
        .order_by(FhirResource.created_at.asc(),
                  FhirResource.id.asc())).all()
    for study in studies:
        entry = radiology._entry_for(session, study)
        if entry is not None:
            priority = entry.priority
            ordered_at = radiology._ensure_aware(entry.ordered_at)
        else:
            priority = radiology._derive_priority(
                session, study.body or {}, ops)
            ordered_at = radiology._ensure_aware(study.created_at)
        item = radiology._entry_out(session, study, entry)
        final = radiology._first_final_report(session, study.resource_id)
        if final is None:
            if priority == "STAT":
                unreported.append((item, ordered_at))
            continue
        if ordered_at is None:
            continue
        issued = radiology._parse_dt((final.body or {}).get("issued")) \
            or radiology._ensure_aware(final.created_at)
        if issued is None:
            continue
        minutes = (issued - ordered_at).total_seconds() / 60.0
        target = radiology._tat_target(ops, priority)
        if target is not None and minutes > target:
            breaches.append((item, minutes, target, priority))
    return breaches, unreported


def _records_radiology_tat_breaches(session: Session) -> list[dict]:
    cache: dict = {}
    breaches, _unreported = _radiology_tat_scan(session)
    return [_radiology_study_record(
        session, cache, item,
        f"TAT breach · {minutes:.0f} min vs target {target} min"
        f" · priority {priority}")
        for item, minutes, target, priority in breaches]


def _records_radiology_unreported_stat(session: Session) -> list[dict]:
    cache: dict = {}
    _breaches, unreported = _radiology_tat_scan(session)
    return [_radiology_study_record(
        session, cache, item,
        "STAT · no final report yet"
        f" · state {item.get('state')}")
        for item, _ordered_at in unreported]


def _records_radiology_critical(session: Session) -> list[dict]:
    from . import radiology
    cache: dict = {}
    out = radiology.list_critical(session=session, user=None)
    records = []
    for item in out["items"]:
        patient_id = _ref_id(item.get("patient_ref"), "Patient")
        name = _patient_display(session, cache, patient_id)
        records.append(_record(
            item["report_id"],
            f"Critical result {item['report_id']} — {name or 'patient'}",
            f"Study {item.get('study_id') or '—'} · state {item.get('state')}"
            + (" · OVERDUE" if item.get("overdue") else ""),
            at=item.get("flagged_at"), patient_id=patient_id))
    return records


# --- resolver registry (mirrors NAMED_SOURCES; None = explicit
# --- unsupported, with the reason in NAMED_RECORD_UNSUPPORTED) --------

NAMED_RECORD_UNSUPPORTED = {
    "workforce.coverage-pct": (
        "Coverage % is an aggregate fill rate — per-unit rostered "
        "staff capped at each unit's published minimum, summed and "
        "divided by the summed minimums. The capped numerator is not "
        "a set of individual records, so there is no honest record "
        "list; inspect rosters in Workforce instead."),
}

NAMED_RECORD_NOTES = {
    "workforce.occupancy-pct": (
        "Value is a percentage (occupied / total beds); the records "
        "listed are the numerator — the occupied beds counted toward "
        "it (len(records) == occupied beds, not the percentage)."),
}

NAMED_RECORD_RESOLVERS = {
    "census.current": _records_census_current,
    "lab.pending-verification": _records_lab_pending_verification,
    "lab.critical-open": _records_lab_critical_open,
    "safety.open-events": _records_safety_open,
    "workforce.admissions-today": _records_workforce_admissions,
    "workforce.discharges-today": _records_workforce_discharges,
    "workforce.staff-active": _records_workforce_staff_active,
    "workforce.credentials-expiring":
        _records_workforce_credentials_expiring,
    "workforce.occupancy-pct": _records_occupancy_pct,
    "workforce.coverage-pct": None,
    "continuum.discharge-ready": _records_continuum_discharge_ready,
    "continuum.discharge-board-total":
        _records_continuum_discharge_board,
    "continuum.boarding-total": _records_continuum_boarding,
    "safety.total-events": _records_safety_total,
    "safety.rca-open": _records_safety_rca_open,
    "registration.frontdesk-appointments":
        _records_frontdesk_appointments,
    "registration.frontdesk-awaiting-checkin":
        _records_frontdesk_awaiting_checkin,
    "registration.frontdesk-awaiting-room": _records_frontdesk_awaiting_room,
    "registration.frontdesk-waitlist-open": _records_frontdesk_waitlist,
    "registration.frontdesk-registrations-today":
        _records_frontdesk_registrations,
    "radiology.worklist-total": _records_radiology_worklist,
    "radiology.tat-breaches": _records_radiology_tat_breaches,
    "radiology.unreported-stat": _records_radiology_unreported_stat,
    "radiology.critical-open": _records_radiology_critical,
}


def records_for_indicator(session: Session, definition: dict):
    """(records, records_supported, reason) for one published
    definition — the drill-down companion to evaluate_indicator."""
    source = definition.get("source") or {}
    stype = source.get("type")
    if stype == "measure":
        return [], False, (
            f"Value is an aggregate warehouse measure "
            f"('{source.get('measure_key')}') computed over the OMOP "
            "warehouse; individual records are explored via Analytics "
            "query (POST /analytics/query), not enumerated here.")
    if stype == "cohort":
        return [], False, (
            f"Value is an aggregate warehouse cohort count "
            f"('{source.get('cohort_key')}') over the OMOP warehouse; "
            "individual records are explored via Analytics query "
            "(POST /analytics/query), not enumerated here.")
    if stype == "named":
        name = source.get("name")
        if name not in NAMED_RECORD_RESOLVERS:
            return [], False, (
                f"Named source '{name}' has no records resolver "
                "registered — this is a server configuration gap, "
                "not an empty result.")
        resolver = NAMED_RECORD_RESOLVERS[name]
        if resolver is None:
            return [], False, NAMED_RECORD_UNSUPPORTED[name]
        return resolver(session), True, NAMED_RECORD_NOTES.get(name)
    return [], False, f"Unknown source type '{stype}'."


@router.get("/{key}/records")
def indicator_records(key: str, session: Session = Depends(get_db),
                      user: User = Depends(require_capability("studio.read"))):
    cfg = _published(session, key)
    if cfg is None:
        raise HTTPException(status_code=404,
                            detail=f"No published indicator '{key}'")
    definition = cfg.payload or {}
    records, supported, reason = records_for_indicator(session, definition)
    return {"key": key, "definition": definition,
            "value": evaluate_indicator(session, key, definition),
            "records": records, "records_supported": supported,
            "reason": reason}


# ---------------------------------------------------------------------------
# Seeds: the operations-overview dashboard KPIs as indicators
# ---------------------------------------------------------------------------

def default_indicator_payloads() -> dict[str, dict]:
    """Indicator definitions reproducing the KPIs of the seeded
    `operations-overview` dashboard (app.studio_config): its three
    widgets are census (/inpatient/census), critical-open
    (/lab/critical-reports) and verification-queue
    (/lab/verification-queue) — each mirrors to a named source here.
    Thresholds on the two lab queues are seed defaults a site
    republishes for its own tolerances."""
    return {
        "operations-census": {
            "name": "Patients in beds",
            "title": "Patients in beds",
            "source": {"type": "named", "name": "census.current"},
            "direction": "lower-better",
            "unit": "patients",
        },
        "operations-critical-open": {
            "name": "Critical reports awaiting closure",
            "title": "Critical reports awaiting closure",
            "source": {"type": "named", "name": "lab.critical-open"},
            "direction": "lower-better",
            "thresholds": {"warn": 1, "critical": 5},
            "unit": "reports",
        },
        "operations-verification-queue": {
            "name": "Lab reports in verification",
            "title": "Lab reports in verification",
            "source": {"type": "named", "name": "lab.pending-verification"},
            "direction": "lower-better",
            "thresholds": {"warn": 5, "critical": 10},
            "unit": "reports",
        },
    }
