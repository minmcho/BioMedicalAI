"""Phase 15 (BD-29/30/32/33) — platform depth (app.platform_depth).

Four work items:

- BD-33 Immunisation record + VXU. Capture endpoints write FHIR
  Immunization resources into the FHIR store through
  app.fhir.create_resource (the store's generic create path). The
  vaccine code is validated against the published
  `terminology/vaccines` Studio config (CVX codes) seeded below.
  VXU^V04 messages are built by app.hl7.build_vxu and queued for the
  registries listed in the published
  `terminology/immunisation-registries` config: queueing goes
  through app.interop's outbox machinery (interop._fanout) whenever
  an enabled, VXU-subscribed HL7 endpoint with the registry's
  endpoint name exists; otherwise the generated message is recorded
  on this module's own outbox table with status
  `queued-for-gateway` — labelled honestly, never claimed sent.
  NOTE: `Immunization` is not yet in app.fhir.SUPPORTED, so the
  generic /fhir/Immunization routes do not exist yet (see the
  workstream report's INTEGRATION-NEEDED); this module's own
  endpoints read/write the same FhirResource store directly and
  through create_resource, so they work before and after that
  one-line registration lands.

- BD-29 Service-line & episode cost accounting. Activity-cost rates
  are published Studio config (`terminology/activity-costs`). The
  costing computation runs over the OMOP warehouse (app.warehouse):
  bed-days from visit_occurrence (only for visit classes carrying a
  published bed-day rate), lab tests from measurement rows, imaging
  studies from procedure_occurrence rows whose source code is on the
  published imaging list, OR minutes from procedure rows on the
  published OR list — the warehouse stores no procedure duration, so
  OR minutes are counted 0 and the response says so rather than
  fabricating durations. Activity is attributed to a service line
  through the visit's class code via the published service-line map;
  rows with no visit are `unattributed`. Episode attribution reads
  care-coordination episode data DEFENSIVELY: clinical tables whose
  name contains `episode` are probed at request time; when none
  exist, episodes / cost-per-episode are null and the response
  carries the reason.

- BD-30 Dashboards rewired to the indicator library.
  DECISION (recorded here per the workstream brief): published
  indicator configs ARE auto-seeded by this module's ensure function
  — through app.indicators' own governance functions (create draft +
  publish, programmatically) — because the indicator library and the
  dashboards must share ONE source of truth from first boot; a
  dashboard widget that still pointed at a hand-derived endpoint
  while an equivalent published indicator existed would silently
  fork the KPI definition. Seeding is idempotent (an existing
  published indicator is never re-created or re-published).
  Rewiring itself is governed config change, not code: for every
  published `dashboard` config, a widget whose (endpoint, display,
  field) matches a known indicator binding is republished as a new
  dashboard version whose widget resolves through
  `/indicators/<key>/value` (the original endpoint is preserved on
  the widget as `sourceEndpoint`). Widgets that cannot honestly be
  an indicator (tables, charts/series, gauges and counters with no
  named/measure/cohort source) stay exactly as seeded; the full
  per-widget trace is served at GET /platform-depth/dashboard-
  indicators for SQA.
  Phase 17 (WS-RESID) remainder: the indicator library was extended
  with governed named sources reproducing every remaining scalar
  widget's own endpoint computation (workforce overview, discharge
  readiness + boarding, safety totals, front-desk counters,
  radiology worklist/TAT/critical totals — see
  EXTENDED_INDICATOR_PAYLOADS), so every scalar widget now resolves
  through the library EXCEPT three unit-scoped widgets on the
  nurse / unit-manager home dashboards (coverage gauge x2,
  credentials-expiring x1): app.people rewrites their live
  /people/analytics endpoints with the caller's unit, and a
  house-wide indicator would silently change the figure those
  audiences see — they stay on their live endpoints with that
  reason recorded in the trace, alongside the table/chart widgets.
  The trace also reports trace_complete.

- BD-32 Visual report designer — backend support. The designer is a
  web surface (another workstream); the report engine
  (app.report_engine) already exposes everything a designer needs to
  compose and run definitions (dataset registry, validation,
  draft->publish governance, JSON/CSV/PDF runs), so this module adds
  only the dataset-schema endpoint the palette needs
  (GET /designer/datasets[/{name}] with field names + inferred
  types, built on the engine's registry — no SQL anywhere) plus the
  parity proof in its tests.

Synthetic data only; every mutation is hash-audit-chained.
"""
import asyncio
import math
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import DateTime, Integer, JSON, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import fhir, hl7, indicators, interop, report_engine
from . import reports as reports_module
from . import warehouse as warehouse_module
from .audit import append_audit
from .configstore import (list_config_versions, published_config,
                          published_configs)
from .db import Base
from .models import ConfigStatus, FhirResource, Role, StudioConfig, User
from .security import get_current_user, get_db, require_capability

router = APIRouter(tags=["platform_depth"])

CVX_SYSTEM = "http://hl7.org/fhir/sid/cvx"
VACCINES_KEY = "vaccines"
COSTS_KEY = "activity-costs"
REGISTRIES_KEY = "immunisation-registries"
IMM_STATUSES = ("completed", "entered-in-error", "not-done")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Own outbox row for VXU messages no live gateway endpoint accepted
# ---------------------------------------------------------------------------

class VxUOutbox(Base):
    """One generated VXU message recorded locally because no enabled,
    VXU-subscribed interop endpoint matched the registry (status
    `queued-for-gateway` — generated and queued locally, NOT sent)."""
    __tablename__ = "platform_depth_vxu_outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(120), unique=True,
                                            nullable=False, index=True)
    control_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    registry_name: Mapped[str] = mapped_column(String(120), nullable=False,
                                               default="")
    immunisation_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                                 default="")
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    raw: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False,
                                        default="queued-for-gateway")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


# ---------------------------------------------------------------------------
# Seeded published configuration
# ---------------------------------------------------------------------------

def default_vaccines_payload() -> dict:
    """CVX vaccine codes (terminology/vaccines). A small synthetic
    seed set of real CVX codes — sites govern the full list in
    Studio."""
    def concept(code, display):
        return {"code": code, "display": display, "active": True}

    return {
        "name": "Vaccines (CVX)",
        "system": CVX_SYSTEM,
        "concepts": [
            concept("03", "MMR"),
            concept("08", "Hepatitis B, pediatric or adolescent"),
            concept("20", "DTaP"),
            concept("104", "Hepatitis A + Hepatitis B (adult)"),
            concept("106", "DTaP, 5 pertussis antigens"),
            concept("115", "Tdap"),
            concept("141", "Influenza, seasonal, injectable"),
            concept("207", "COVID-19, Moderna (Spikevax)"),
            concept("208", "COVID-19, Pfizer-BioNTech (Comirnaty)"),
        ],
    }


def default_registries_payload() -> dict:
    """VXU reporting destinations (terminology/immunisation-
    registries). `endpointName` is the app.interop endpoint a site
    enables (subscribed to VXU) for live gateway delivery; without
    one, generated messages are recorded locally as
    queued-for-gateway. The seed registry is synthetic."""
    return {
        "name": "Immunisation registries (VXU destinations)",
        "registries": [
            {"name": "synthetic-state-iis",
             "endpointName": "immunisation-registry",
             "receivingApplication": "STATE-IIS",
             "receivingFacility": "SYNTHETIC STATE IMMUNISATION REGISTRY",
             "enabled": True, "synthetic": True},
        ],
    }


def default_costs_payload() -> dict:
    """Activity-cost rates (terminology/activity-costs). Synthetic
    seed rates in USD — a site republishes its own cost accounting
    rates; amounts are per unit (bed-day / test / study / minute).
    Bed-day rates are keyed by unit class = the warehouse visit class
    code (FHIR v3 ActCode). OR-procedure codes carry no duration in
    the warehouse, so the or-minute rate prices zero minutes until a
    duration source exists (stated in the costing response)."""
    return {
        "name": "Activity cost rates (synthetic seed)",
        "currency": "USD",
        "synthetic": True,
        "rates": [
            {"activity": "bed-day", "unitClass": "IMP",
             "amount": 1200.00, "unit": "day"},
            {"activity": "bed-day", "unitClass": "ACUTE",
             "amount": 1800.00, "unit": "day"},
            {"activity": "lab-test", "amount": 28.50, "unit": "test"},
            {"activity": "imaging-study", "amount": 240.00, "unit": "study",
             "procedureCodes": ["71045", "71046", "70450", "70551",
                                "74176", "76700"]},
            {"activity": "or-minute", "amount": 35.00, "unit": "minute",
             "procedureCodes": []},
        ],
        "serviceLines": {"IMP": "inpatient", "ACUTE": "inpatient",
                         "AMB": "ambulatory", "EMER": "emergency"},
        "serviceLineDisplays": {"inpatient": "Inpatient",
                                "ambulatory": "Ambulatory",
                                "emergency": "Emergency",
                                "unattributed": "Unattributed"},
    }


def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
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


# ---------------------------------------------------------------------------
# BD-30: indicator seeding through app.indicators' governance + rewiring
# ---------------------------------------------------------------------------

SAFETY_INDICATOR_KEY = "safety-open-events"
SAFETY_INDICATOR_PAYLOAD = {
    "name": "Open safety events",
    "title": "Open safety events",
    "source": {"type": "named", "name": "safety.open-events"},
    "direction": "lower-better",
    "thresholds": {"warn": 1, "critical": 5},
    "unit": "events",
}

# Phase 17 (WS-RESID) — BD-30 remainder: governed indicator
# definitions for every remaining scalar widget whose live endpoint
# computes a single measurable quantity. Each binds a Phase 17 named
# source in app.indicators that reproduces the source endpoint's own
# computation, so the widget and the endpoint cannot diverge. Seeded
# through the same indicators governance as the Phase 15 library
# (Studio surface: config kind `indicator`, keys below; dashboards
# remain kind `dashboard`). Widgets that render multiple rows or a
# series (tables / line / bar charts) are NOT bound: no scalar
# indicator can honestly supply them, and the trace says so per
# widget (see _widget_reason). Two definitions here
# (workforce-coverage-pct, workforce-credentials-expiring) are
# published as honest house-wide library indicators but are
# deliberately NOT bound to their widgets — those widgets are
# unit-scoped on the nurse / unit-manager home dashboards (see the
# note at INDICATOR_BINDINGS).
EXTENDED_INDICATOR_PAYLOADS = {
    "workforce-admissions-today": {
        "name": "Admissions today",
        "title": "Admissions today",
        "source": {"type": "named", "name": "workforce.admissions-today"},
        "direction": "higher-better",
        "unit": "admissions",
    },
    "workforce-discharges-today": {
        "name": "Discharges today",
        "title": "Discharges today",
        "source": {"type": "named", "name": "workforce.discharges-today"},
        "direction": "higher-better",
        "unit": "discharges",
    },
    "workforce-staff-active": {
        "name": "Active staff",
        "title": "Active staff",
        "source": {"type": "named", "name": "workforce.staff-active"},
        "direction": "higher-better",
        "unit": "staff",
    },
    "workforce-credentials-expiring": {
        "name": "Credentials expiring (worklist window)",
        "title": "Credentials expiring (90 days)",
        "source": {"type": "named",
                   "name": "workforce.credentials-expiring"},
        "direction": "lower-better",
        "thresholds": {"warn": 1, "critical": 10},
        "unit": "credentials",
    },
    "workforce-occupancy-pct": {
        "name": "Occupancy",
        "title": "Occupancy",
        "source": {"type": "named", "name": "workforce.occupancy-pct"},
        "direction": "higher-better",
        "unit": "%",
    },
    "workforce-coverage-pct": {
        "name": "Staffing coverage today",
        "title": "Staffing coverage today",
        "source": {"type": "named", "name": "workforce.coverage-pct"},
        "direction": "higher-better",
        "thresholds": {"warn": 90, "critical": 75},
        "unit": "%",
    },
    "continuum-discharge-ready": {
        "name": "Patients ready for discharge",
        "title": "Patients ready for discharge",
        "source": {"type": "named", "name": "continuum.discharge-ready"},
        "direction": "higher-better",
        "unit": "patients",
    },
    "continuum-discharge-board-total": {
        "name": "Admissions tracked on the discharge board",
        "title": "Admissions tracked on the board",
        "source": {"type": "named",
                   "name": "continuum.discharge-board-total"},
        "direction": "higher-better",
        "unit": "admissions",
    },
    "continuum-boarding-total": {
        "name": "Patients boarding (awaiting a bed)",
        "title": "Patients boarding (awaiting a bed)",
        "source": {"type": "named", "name": "continuum.boarding-total"},
        "direction": "lower-better",
        "thresholds": {"warn": 1, "critical": 4},
        "unit": "patients",
    },
    "safety-total-events": {
        "name": "Safety events (all time)",
        "title": "Safety events (all time)",
        "source": {"type": "named", "name": "safety.total-events"},
        "direction": "lower-better",
        "unit": "events",
    },
    "safety-rca-open": {
        "name": "Open RCA work items",
        "title": "Open RCA work items",
        "source": {"type": "named", "name": "safety.rca-open"},
        "direction": "lower-better",
        "thresholds": {"warn": 1, "critical": 5},
        "unit": "work items",
    },
    "frontdesk-appointments-total": {
        "name": "Appointments today (front desk)",
        "title": "Appointments today",
        "source": {"type": "named",
                   "name": "registration.frontdesk-appointments"},
        "direction": "higher-better",
        "unit": "appointments",
    },
    "frontdesk-awaiting-checkin": {
        "name": "Awaiting check-in (front desk)",
        "title": "Awaiting check-in",
        "source": {"type": "named",
                   "name": "registration.frontdesk-awaiting-checkin"},
        "direction": "lower-better",
        "unit": "patients",
    },
    "frontdesk-awaiting-room": {
        "name": "Arrived — awaiting a room (front desk)",
        "title": "Arrived — awaiting a room",
        "source": {"type": "named",
                   "name": "registration.frontdesk-awaiting-room"},
        "direction": "lower-better",
        "unit": "patients",
    },
    "frontdesk-waitlist-open": {
        "name": "Waitlist (open)",
        "title": "Waitlist (open)",
        "source": {"type": "named",
                   "name": "registration.frontdesk-waitlist-open"},
        "direction": "lower-better",
        "unit": "entries",
    },
    "frontdesk-registrations-today": {
        "name": "Registered today (front desk)",
        "title": "Registered today",
        "source": {"type": "named",
                   "name": "registration.frontdesk-registrations-today"},
        "direction": "higher-better",
        "unit": "registrations",
    },
    "radiology-worklist-total": {
        "name": "Studies on the RIS worklist",
        "title": "Studies on the RIS worklist",
        "source": {"type": "named", "name": "radiology.worklist-total"},
        "direction": "lower-better",
        "unit": "studies",
    },
    "radiology-tat-breaches": {
        "name": "TAT breaches (reported)",
        "title": "TAT breaches (reported)",
        "source": {"type": "named", "name": "radiology.tat-breaches"},
        "direction": "lower-better",
        "thresholds": {"warn": 1, "critical": 5},
        "unit": "studies",
    },
    "radiology-unreported-stat": {
        "name": "STAT studies not yet reported",
        "title": "STAT studies not yet reported",
        "source": {"type": "named", "name": "radiology.unreported-stat"},
        "direction": "lower-better",
        "thresholds": {"warn": 1, "critical": 5},
        "unit": "studies",
    },
    "radiology-critical-total": {
        "name": "Radiology critical results on record",
        "title": "Critical results awaiting closure",
        "source": {"type": "named", "name": "radiology.critical-open"},
        "direction": "lower-better",
        "thresholds": {"warn": 1, "critical": 5},
        "unit": "results",
    },
}


class _StubRequest:
    """Minimal request stand-in so app.indicators' own create
    governance function can be called programmatically at seed time
    (it only ever awaits request.json())."""

    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self):
        return self._payload


def _seed_indicator(session: Session, key: str, payload: dict) -> None:
    """Create + publish one indicator through app.indicators'
    governance functions (idempotent: an existing published version
    is left exactly as it is; an existing draft is published)."""
    indicators.validate_indicator(payload)
    if indicators._published(session, key) is not None:
        return
    system_user = User(username="system", role=Role.admin)
    versions = indicators._versions(session, key)
    draft = next((v for v in versions if v.status == ConfigStatus.draft),
                 None)
    if draft is None:
        try:
            asyncio.run(indicators.create_definition(
                key, _StubRequest(payload), session, system_user))
        except RuntimeError:
            # A running event loop (embedded hosts): fall back to the
            # identical rows + audit actions the governance function
            # writes, rather than skipping the seed.
            next_version = (versions[-1].version + 1) if versions else 1
            row = StudioConfig(kind=indicators.KIND, key=key,
                               version=next_version,
                               status=ConfigStatus.draft, payload=payload,
                               created_by="system", updated_by="system")
            session.add(row)
            session.flush()
            append_audit(session, actor="system",
                         action="indicator.definition.create",
                         entity=f"config:{indicators.KIND}/{key}@v{next_version}",
                         payload={"kind": indicators.KIND, "key": key,
                                  "version": next_version})
            session.commit()
        # create_definition returns the rendered dict, not the row —
        # re-read the draft it just wrote.
        draft = next((v for v in indicators._versions(session, key)
                      if v.status == ConfigStatus.draft), None)
    if draft is not None:
        indicators.publish_definition(key, draft.version, session,
                                      system_user)


# (endpoint path, display, field) -> indicator key. `field` None means
# the widget carries no field (display "total").
INDICATOR_BINDINGS = [
    ("/inpatient/census", "total", None, "operations-census"),
    ("/people/analytics/overview", "big-number", "occupied",
     "operations-census"),
    ("/people/analytics/overview", "value", "occupied", "operations-census"),
    ("/lab/critical-reports", "total", None, "operations-critical-open"),
    ("/lab/verification-queue", "total", None,
     "operations-verification-queue"),
    ("/continuum/safety/summary", "value", "open", SAFETY_INDICATOR_KEY),
    # Phase 17 (WS-RESID) BD-30 remainder — see
    # EXTENDED_INDICATOR_PAYLOADS. One binding may serve several
    # dashboards (admissions-today appears on two audience views).
    ("/people/analytics/overview", "big-number", "admissions_today",
     "workforce-admissions-today"),
    ("/people/analytics/overview", "big-number", "discharges_today",
     "workforce-discharges-today"),
    ("/people/analytics/overview", "big-number", "staff_active",
     "workforce-staff-active"),
    ("/people/analytics/overview", "gauge", "occupancy_pct",
     "workforce-occupancy-pct"),
    # Deliberately NOT bound: ("/people/analytics/coverage", "gauge",
    # "overall_pct") and ("/people/analytics/overview", "big-number",
    # "credentials_expiring"). Both widgets live on the nurse /
    # unit-manager home dashboards, whose payloads app.people
    # unit-scopes by rewriting /people/analytics endpoints with the
    # caller's unit (?unit=…) — they show the UNIT's coverage /
    # expiring credentials. The governed indicators
    # (workforce-coverage-pct, workforce-credentials-expiring) are
    # published and honest as HOUSE-wide figures, but binding these
    # widgets to them would silently change what those audiences see
    # from their unit's figure to the house's. The trace carries
    # this reason per widget (see _widget_reason).
    ("/continuum/inpatient/discharge-readiness", "value", "ready",
     "continuum-discharge-ready"),
    ("/continuum/inpatient/discharge-readiness", "total", None,
     "continuum-discharge-board-total"),
    ("/continuum/er/boarding", "total", None, "continuum-boarding-total"),
    ("/continuum/safety/summary", "total", None, "safety-total-events"),
    ("/continuum/safety/summary", "value", "rca_open", "safety-rca-open"),
    ("/registration/frontdesk/today", "big-number", "appointments_total",
     "frontdesk-appointments-total"),
    ("/registration/frontdesk/today", "big-number", "awaiting_checkin",
     "frontdesk-awaiting-checkin"),
    ("/registration/frontdesk/today", "big-number", "awaiting_room",
     "frontdesk-awaiting-room"),
    ("/registration/frontdesk/today", "big-number", "waitlist_open",
     "frontdesk-waitlist-open"),
    ("/registration/frontdesk/today", "big-number", "registrations_today",
     "frontdesk-registrations-today"),
    ("/radiology/worklist", "total", None, "radiology-worklist-total"),
    ("/radiology/stats/tat", "value", "breaches", "radiology-tat-breaches"),
    ("/radiology/stats/tat", "value", "unreported_stat",
     "radiology-unreported-stat"),
    ("/radiology/critical", "total", None, "radiology-critical-total"),
]


def _binding_for(widget: dict) -> str | None:
    if not isinstance(widget, dict):
        return None
    path = str(widget.get("endpoint") or "").split("?")[0]
    for ep, display, field, key in INDICATOR_BINDINGS:
        if path == ep and widget.get("display") == display \
                and widget.get("field") == field:
            return key
    return None


# Displays that read ONE named field from the response: they keep
# working unchanged when the field becomes the indicator response's
# `value`, so a rewired widget keeps its exact presentation (a gauge
# stays a gauge, thresholds/max/unit included). Only the aggregate
# displays (`total` / `count`), which read response fields an
# indicator response does not carry, are re-expressed as `value`.
_FIELD_DISPLAYS = ("value", "big-number", "gauge")


def _rewired_widget(widget: dict, indicator_key: str) -> dict:
    out = dict(widget)
    out["sourceEndpoint"] = widget.get("endpoint")
    if widget.get("display") is not None:
        out["sourceDisplay"] = widget.get("display")
    if widget.get("field") is not None:
        out["sourceField"] = widget.get("field")
    out["endpoint"] = f"/indicators/{indicator_key}/value"
    if widget.get("display") not in _FIELD_DISPLAYS:
        out["display"] = "value"
    out["field"] = "value"
    out["indicator"] = indicator_key
    return out


def _rewire_dashboards(session: Session) -> None:
    """Publish a new version of every published dashboard config in
    which at least one widget honestly resolves through a published
    indicator (governed config change: the previous published
    version is superseded, never edited in place). Idempotent: a
    dashboard whose bindable widgets already carry `indicator` is
    left untouched."""
    for cfg in published_configs(session, "dashboard"):
        payload = cfg.payload or {}
        widgets = payload.get("widgets")
        if not isinstance(widgets, list):
            continue
        changed = False
        new_widgets = []
        for widget in widgets:
            if isinstance(widget, dict) and "indicator" not in widget:
                key = _binding_for(widget)
                if key is not None and indicators._published(session, key) \
                        is not None:
                    new_widgets.append(_rewired_widget(widget, key))
                    changed = True
                    continue
            new_widgets.append(widget)
        if not changed:
            continue
        versions = list_config_versions(session, "dashboard", cfg.key)
        next_version = (versions[-1].version + 1) if versions else 1
        cfg.status = ConfigStatus.superseded
        session.flush()
        row = StudioConfig(
            kind="dashboard", key=cfg.key, version=next_version,
            status=ConfigStatus.published,
            payload={**payload, "widgets": new_widgets},
            created_by="system", updated_by="system")
        session.add(row)
        session.flush()
        bindings = {w["id"]: w["indicator"] for w in new_widgets
                    if isinstance(w, dict) and w.get("indicator")}
        for action in ("studio.config.create", "studio.config.publish"):
            append_audit(
                session, actor="system", action=action,
                entity=f"config:dashboard/{cfg.key}@v{next_version}",
                payload={"kind": "dashboard", "key": cfg.key,
                         "version": next_version,
                         "rewired_by": "platform_depth",
                         "superseded_version": cfg.version,
                         "indicator_bindings": bindings})


def _widget_reason(widget: dict) -> str:
    display = widget.get("display")
    path = str(widget.get("endpoint") or "").split("?")[0]
    field = widget.get("field")
    if display == "table":
        return ("Table widget renders multiple rows; an indicator is a "
                "single scalar value — no one indicator can honestly "
                "supply it.")
    if display in ("line", "bar"):
        return ("Chart widget renders a series over time; an indicator "
                "is a single scalar value — no one indicator can "
                "honestly supply it.")
    if path == "/continuum/safety/summary":
        return (f"No indicator source computes '{field}' from the "
                "safety summary; the governed indicator library's "
                "safety sources cover open events, all-time total "
                "events and open RCA items only.")
    if path == "/continuum/inpatient/discharge-readiness":
        return ("No indicator source computes this discharge-board "
                "value; the governed indicator library covers the "
                "board's ready count and board total only.")
    if path == "/continuum/er/boarding":
        return ("No indicator source computes this boarding value; "
                "the governed indicator library covers the boarding "
                "total only.")
    if path == "/people/analytics/coverage" and display == "gauge":
        return ("Unit-scoped widget: on the nurse / unit-manager "
                "home dashboards app.people rewrites this live "
                "endpoint with the caller's unit (?unit=...), so the "
                "gauge shows the UNIT's staffing coverage. The "
                "governed indicator workforce-coverage-pct is a "
                "house-wide figure; rewiring would silently change "
                "what these audiences see from their unit's coverage "
                "to the house's — not rewired, by design.")
    if path == "/people/analytics/coverage":
        return ("No indicator source computes this coverage value; "
                "the governed indicator library covers the house-wide "
                "overall coverage percentage only (per-unit coverage "
                "is a multi-row rollup, not a scalar).")
    if path == "/people/analytics/overview" \
            and field == "credentials_expiring":
        return ("Unit-scoped widget: on the unit-manager home "
                "dashboard app.people rewrites this live endpoint "
                "with the caller's unit (?unit=...), so the counter "
                "shows credentials expiring among the UNIT's staff. "
                "The governed indicator "
                "workforce-credentials-expiring is a house-wide "
                "figure; rewiring would silently change what the "
                "unit manager sees from their unit's count to the "
                "house's — not rewired, by design.")
    if path == "/people/analytics/units":
        return ("Per-unit rollup (multiple rows/series); an indicator "
                "is a single house-wide scalar.")
    if path == "/registration/frontdesk/today":
        return (f"No indicator source computes the front-desk counter "
                f"'{field}'; the governed indicator library's "
                "front-desk sources cover appointments_total, "
                "awaiting_checkin, awaiting_room, waitlist_open and "
                "registrations_today only.")
    if path == "/people/analytics/overview":
        return (f"No named/measure/cohort indicator source computes "
                f"'{field}' from the workforce overview; the governed "
                "indicator library's workforce sources cover occupied, "
                "admissions_today, discharges_today, occupancy_pct, "
                "credentials_expiring and staff_active only.")
    if path.startswith("/radiology/"):
        return (f"No indicator source computes this radiology value "
                f"(endpoint {path}, display {display}, field {field}); "
                "the governed indicator library's radiology sources "
                "cover the RIS worklist total, TAT breaches, "
                "unreported STAT studies and the critical-results "
                "total only.")
    return (f"No indicator source honestly computes this widget's "
            f"value (endpoint {path}, display {display}, "
            f"field {field}).")


def dashboard_trace(session: Session) -> dict:
    """Per-dashboard, per-widget trace: which widgets resolve through
    a published indicator and which stay on their live endpoint (with
    the reason). SQA input for BD-30."""
    dashboards = []
    widgets_total = widgets_indicator = 0
    for cfg in published_configs(session, "dashboard"):
        payload = cfg.payload or {}
        entries = []
        for widget in payload.get("widgets") or []:
            if not isinstance(widget, dict):
                continue
            widgets_total += 1
            indicator_key = widget.get("indicator") or _binding_for(widget)
            if indicator_key is not None:
                widgets_indicator += 1
                entries.append({
                    "id": widget.get("id"), "title": widget.get("title"),
                    "disposition": "indicator",
                    "indicator": indicator_key,
                    "applied": "indicator" in widget,
                    "endpoint": widget.get("endpoint")})
            else:
                entries.append({
                    "id": widget.get("id"), "title": widget.get("title"),
                    "disposition": "live-endpoint",
                    "indicator": None, "applied": False,
                    "endpoint": widget.get("endpoint"),
                    "reason": _widget_reason(widget)})
        dashboards.append({"key": cfg.key, "version": cfg.version,
                           "title": payload.get("title"),
                           "widgets": entries})
    # Completeness (Phase 17, BD-30 remainder): every widget entry
    # above carries EITHER an indicator binding OR a named reason —
    # there is no third state — so widgets_on_indicators +
    # widgets_with_reasons always equals widgets_total. The trace is
    # the migration ledger AND the completeness proof.
    widgets_reasoned = sum(
        1 for d in dashboards for e in d["widgets"]
        if e["disposition"] == "live-endpoint" and e.get("reason"))
    return {"dashboards": dashboards, "total": len(dashboards),
            "widgets_total": widgets_total,
            "widgets_on_indicators": widgets_indicator,
            "widgets_on_live_endpoints": widgets_total - widgets_indicator,
            "widgets_with_reasons": widgets_reasoned,
            "trace_complete": widgets_reasoned
            == widgets_total - widgets_indicator}


@router.get("/platform-depth/dashboard-indicators")
def get_dashboard_indicators(
        session: Session = Depends(get_db),
        user: User = Depends(require_capability("studio.read"))):
    """The BD-30 trace: every published dashboard config, each widget
    traced to its value source (published indicator or live endpoint
    + reason)."""
    return dashboard_trace(session)


# ---------------------------------------------------------------------------
# BD-33: immunisation capture + VXU
# ---------------------------------------------------------------------------

def _vaccine_concepts(session: Session) -> list[dict]:
    cfg = published_config(session, "terminology", VACCINES_KEY)
    if cfg is None:
        return []
    return [c for c in (cfg.payload or {}).get("concepts") or []
            if isinstance(c, dict) and c.get("active", True)]


def _registries(session: Session) -> list[dict]:
    cfg = published_config(session, "terminology", REGISTRIES_KEY)
    if cfg is None:
        return []
    return [r for r in (cfg.payload or {}).get("registries") or []
            if isinstance(r, dict) and r.get("name") and r.get("enabled", True)]


def _parse_occurrence(value) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422,
                            detail="'date' (occurrence datetime) is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422,
                            detail=f"Invalid occurrence datetime: {value}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _patient_row(session: Session, patient_ref: str) -> FhirResource | None:
    patient_id = patient_ref.split("/")[-1]
    return session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Patient",
                                   FhirResource.resource_id == patient_id)
    ).first()


def _immunisation_row(session: Session, imm_id: str) -> FhirResource:
    row = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Immunization",
            FhirResource.resource_id == imm_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Immunization/{imm_id} not found")
    return row


def _imm_out(row: FhirResource) -> dict:
    return fhir._render(row)


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


@router.post("/immunisations", status_code=201)
async def create_immunisation(
        request: Request, session: Session = Depends(get_db),
        user: User = Depends(require_capability("imm.work"))):
    """Capture one immunisation: patient, vaccine (validated against
    the published terminology/vaccines CVX list), lot, date (UTC) and
    status — stored as a FHIR Immunization resource through the FHIR
    store's generic create path."""
    body = await _json_body(request)
    patient_raw = body.get("patient") or body.get("patient_ref")
    if not isinstance(patient_raw, str) or not patient_raw.strip():
        raise HTTPException(status_code=422, detail="'patient' is required")
    patient_ref = fhir._normalize_patient_ref(patient_raw.strip())
    if _patient_row(session, patient_ref) is None:
        raise HTTPException(status_code=404,
                            detail=f"{patient_ref} not found")
    vaccine_code = body.get("vaccine_code") or body.get("vaccineCode")
    if not isinstance(vaccine_code, str) or not vaccine_code.strip():
        raise HTTPException(status_code=422,
                            detail="'vaccine_code' is required")
    vaccine_code = vaccine_code.strip()
    concept = next((c for c in _vaccine_concepts(session)
                    if c.get("code") == vaccine_code), None)
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=(f"Vaccine code '{vaccine_code}' is not in the "
                    f"published terminology/{VACCINES_KEY} list"))
    lot = body.get("lot_number") or body.get("lot")
    if not isinstance(lot, str) or not lot.strip():
        raise HTTPException(status_code=422, detail="'lot_number' is required")
    status = body.get("status") or "completed"
    if status not in IMM_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"'status' must be one of {list(IMM_STATUSES)}")
    occurrence = _parse_occurrence(
        body.get("date") or body.get("occurrence_datetime")
        or body.get("occurrenceDateTime"))
    resource: dict = {
        "resourceType": "Immunization",
        "id": f"imm-{uuid.uuid4().hex[:12]}",
        "status": status,
        "vaccineCode": {"coding": [{"system": CVX_SYSTEM, "code": vaccine_code,
                                     "display": concept.get("display") or ""}],
                        "text": concept.get("display") or vaccine_code},
        "patient": {"reference": patient_ref},
        "occurrenceDateTime": occurrence.isoformat().replace("+00:00", "Z"),
        "recorded": _now().isoformat().replace("+00:00", "Z"),
        "lotNumber": lot.strip(),
        "primarySource": True,
        "performer": [{"actor": {"display": user.username}}],
    }
    if body.get("encounter"):
        enc = str(body["encounter"])
        resource["encounter"] = {
            "reference": enc if "/" in enc else f"Encounter/{enc}"}
    if body.get("manufacturer"):
        resource["manufacturer"] = {"display": str(body["manufacturer"])}
    if body.get("expiration_date"):
        resource["expirationDate"] = str(body["expiration_date"])
    dose_value = body.get("dose_value")
    if dose_value is not None:
        if isinstance(dose_value, bool) or not isinstance(dose_value, (int, float)):
            raise HTTPException(status_code=422,
                                detail="'dose_value' must be a number")
        resource["doseQuantity"] = {
            "value": dose_value, "unit": str(body.get("dose_unit") or "mL"),
            "system": "http://unitsofmeasure.org",
            "code": str(body.get("dose_unit") or "mL")}
    for field, key in (("route", "route"), ("site", "site")):
        code = body.get(f"{field}_code")
        if code:
            resource[key] = {"coding": [{
                "system": "http://snomed.info/sct", "code": str(code),
                "display": str(body.get(f"{field}_display") or "")}]}
    if status == "not-done" and body.get("status_reason_code"):
        resource["statusReason"] = {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActReason",
            "code": str(body["status_reason_code"]),
            "display": str(body.get("status_reason_display") or "")}]}
    rendered = fhir.create_resource("Immunization", resource, session, user)
    append_audit(session, actor=user.username, action="immunisation.create",
                 entity=f"Immunization/{rendered['id']}",
                 payload={"patient": patient_ref, "vaccine_code": vaccine_code,
                          "lot": lot.strip(), "status": status})
    session.commit()
    return rendered


@router.get("/immunisations")
def list_immunisations(patient: str | None = Query(default=None),
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("imm.read"))):
    """Immunisation records, optionally for one patient."""
    stmt = select(FhirResource).where(
        FhirResource.resource_type == "Immunization").order_by(
        FhirResource.created_at.asc(), FhirResource.id.asc())
    rows = session.scalars(stmt).all()
    if patient:
        ref = fhir._normalize_patient_ref(patient)
        rows = [r for r in rows
                if ((r.body or {}).get("patient") or {}).get("reference") == ref]
    items = [_imm_out(r) for r in rows]
    return {"items": items, "total": len(items)}


@router.get("/immunisations/vxu-outbox")
def list_vxu_outbox(session: Session = Depends(get_db),
                    user: User = Depends(require_capability("imm.read"))):
    """Locally recorded VXU messages (queued-for-gateway rows only;
    messages delivered through the interop outbox live in
    /interop/messages)."""
    rows = session.scalars(select(VxUOutbox).order_by(VxUOutbox.id.desc())).all()
    items = [{"id": r.id, "message_id": r.message_id,
              "control_id": r.control_id, "registry": r.registry_name,
              "immunisation_id": r.immunisation_id,
              "patient_ref": r.patient_ref, "status": r.status,
              "raw": r.raw,
              "created_at": r.created_at.isoformat() if r.created_at else None}
             for r in rows]
    return {"items": items, "total": len(items)}


@router.get("/immunisations/{imm_id}")
def get_immunisation(imm_id: str, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("imm.read"))):
    return _imm_out(_immunisation_row(session, imm_id))


@router.get("/portal/immunisations")
def portal_immunisations(session: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """Portal own-record read: a patient caller sees ONLY their own
    immunisation records (matched on the caller's patient_ref)."""
    if user.role != Role.patient or not user.patient_ref:
        raise HTTPException(status_code=403,
                            detail="The portal immunisation view is "
                                   "patient-only")
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Immunization")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    items = [_imm_out(r) for r in rows
             if ((r.body or {}).get("patient") or {}).get("reference")
             == user.patient_ref]
    return {"items": items, "total": len(items),
            "patient_ref": user.patient_ref}


@router.post("/immunisations/{imm_id}/vxu")
async def generate_vxu(imm_id: str, request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("imm.work"))):
    """Build the VXU^V04 for one immunisation record and queue it for
    every enabled registry in the published
    terminology/immunisation-registries config (or one named
    registry). Queueing goes through app.interop's outbox machinery
    when an enabled VXU-subscribed endpoint carries the registry's
    endpoint name; otherwise the message is recorded on this
    module's own outbox as queued-for-gateway."""
    row = _immunisation_row(session, imm_id)
    body = await request.json() if (await request.body()) else {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=422,
                            detail="Request body must be a JSON object")
    imm_body = fhir._render(row)
    patient_ref = ((imm_body.get("patient") or {}).get("reference") or "")
    patient_row = _patient_row(session, patient_ref) if patient_ref else None
    if patient_row is None:
        raise HTTPException(status_code=422,
                            detail="Immunisation has no resolvable Patient "
                                   "record to build a PID from")
    patient_body = fhir._render(patient_row)
    registries = _registries(session)
    wanted = body.get("registry")
    if wanted:
        registries = [r for r in registries if r["name"] == wanted]
        if not registries:
            raise HTTPException(
                status_code=404,
                detail=f"No enabled immunisation registry named '{wanted}' "
                       f"in terminology/{REGISTRIES_KEY}")
    if not registries:
        raise HTTPException(
            status_code=422,
            detail=f"No enabled registries in the published "
                   f"terminology/{REGISTRIES_KEY} config")
    facility = interop._facility(session)
    items = []
    for registry in registries:
        endpoint_name = str(registry.get("endpointName") or "")

        def build(endpoint, _registry=registry):
            return hl7.build_vxu(
                imm_body, patient_body,
                control_id=hl7.new_control_id("VXU"),
                sending_application=facility["sendingApplication"],
                sending_facility=facility["sendingFacility"],
                receiving_application=endpoint.get("receivingApplication")
                or _registry.get("receivingApplication") or "",
                receiving_facility=endpoint.get("receivingFacility")
                or _registry.get("receivingFacility") or "")

        queued = interop._fanout(
            session, "VXU^V04", patient_ref, "immunisation",
            f"Immunization/{imm_id}", build,
            only_name=endpoint_name or None)
        if queued:
            for q in queued:
                items.append({"registry": registry["name"],
                              "queued_via": "interop-outbox",
                              "status": q.status,
                              "message_id": q.message_id,
                              "control_id": q.control_id, "raw": q.raw})
            continue
        raw = hl7.build_vxu(
            imm_body, patient_body, control_id=hl7.new_control_id("VXU"),
            sending_application=facility["sendingApplication"],
            sending_facility=facility["sendingFacility"],
            receiving_application=str(registry.get("receivingApplication")
                                      or ""),
            receiving_facility=str(registry.get("receivingFacility") or ""))
        control_id = hl7.control_id_of(hl7.parse_segments(raw))
        outbox_row = VxUOutbox(
            message_id=f"vxu-{uuid.uuid4().hex[:16]}",
            control_id=control_id, registry_name=registry["name"],
            immunisation_id=imm_id, patient_ref=patient_ref, raw=raw,
            status="queued-for-gateway",
            detail={"reason": "no enabled VXU-subscribed interop endpoint "
                              f"named '{endpoint_name}' — recorded locally "
                              "for gateway pickup, not sent",
                    "synthetic_registry": bool(registry.get("synthetic"))})
        session.add(outbox_row)
        session.flush()
        items.append({"registry": registry["name"],
                      "queued_via": "local-outbox",
                      "status": "queued-for-gateway",
                      "message_id": outbox_row.message_id,
                      "control_id": control_id, "raw": raw})
    append_audit(session, actor=user.username, action="immunisation.vxu.queue",
                 entity=f"Immunization/{imm_id}",
                 payload={"registries": [i["registry"] for i in items],
                          "queued_via": [i["queued_via"] for i in items],
                          "patient": patient_ref})
    session.commit()
    return {"immunisation": f"Immunization/{imm_id}",
            "patient_ref": patient_ref, "items": items,
            "total": len(items)}


# ---------------------------------------------------------------------------
# BD-29: service-line & episode cost accounting over the warehouse
# ---------------------------------------------------------------------------

def _episode_scan(session: Session, service_lines: dict,
                  encounter_lines: dict) -> dict:
    """Defensive probe for care-coordination episode data: clinical
    tables whose name contains `episode` (another workstream owns
    them; they may not exist). Returns availability + counts, with
    per-service-line attribution when the table carries a
    service_line column or an encounter_id that resolves through the
    FHIR Encounter store."""
    from sqlalchemy import func
    # Episode tables only — subordinate tables (contributions etc.)
    # are not episodes and must not inflate the count.
    tables = [t for name, t in Base.metadata.tables.items()
              if "episode" in name and "contribution" not in name]
    if not tables:
        return {"available": False, "source": None, "total": None,
                "by_service_line": {},
                "note": "No episode table exists in the clinical store "
                        "yet (care-coordination episodes are another "
                        "workstream); episodes and cost-per-episode "
                        "are reported as null, never estimated."}
    by_line: dict[str, int] = {}
    total = 0
    sources = []
    for table in tables:
        try:
            total += session.scalar(select(func.count()).select_from(table)) or 0
            sources.append(table.name)
            if "service_line" in table.c:
                for line, count in session.execute(
                        select(table.c.service_line, func.count())
                        .group_by(table.c.service_line)).all():
                    if line:
                        by_line[str(line)] = by_line.get(str(line), 0) + count
            elif "encounter_id" in table.c:
                for enc_id, count in session.execute(
                        select(table.c.encounter_id, func.count())
                        .group_by(table.c.encounter_id)).all():
                    line = encounter_lines.get(str(enc_id or ""))
                    if line:
                        by_line[line] = by_line.get(line, 0) + count
            elif "links" in table.c:
                # app.care_coordination episodes carry typed FHIR
                # links ({ref: "Encounter/<id>", type: "encounter"}):
                # attribute the episode to the service line of its
                # first linked encounter; episodes with no encounter
                # link count toward the total only.
                for (links,) in session.execute(select(table.c.links)).all():
                    for link in links or []:
                        if not isinstance(link, dict):
                            continue
                        ref = link.get("ref") or link.get("reference") or ""
                        if isinstance(ref, str) and ref.startswith("Encounter/"):
                            line = encounter_lines.get(ref.split("/", 1)[1])
                            if line:
                                by_line[line] = by_line.get(line, 0) + 1
                            break
        except Exception:
            continue
    return {"available": True, "source": sources, "total": total,
            "by_service_line": by_line,
            "note": "Episode counts read live from the episode "
                    "table(s) named; per-line attribution follows "
                    "service_line / encounter_id / encounter links "
                    "when the table carries them."}


@router.get("/costing/service-lines")
def costing_service_lines(session: Session = Depends(get_db),
                          user: User = Depends(require_capability("reports.run"))):
    """Service-line & episode cost accounting over the OMOP
    warehouse, priced by the published terminology/activity-costs
    rates. See the module docstring for the attribution rules and
    the honest gaps (OR minutes, episodes)."""
    cfg = published_config(session, "terminology", COSTS_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    rates = [r for r in payload.get("rates") or [] if isinstance(r, dict)]
    bed_day_rates = {str(r.get("unitClass")): float(r["amount"])
                     for r in rates if r.get("activity") == "bed-day"
                     and isinstance(r.get("amount"), (int, float))}
    lab_rate = next((float(r["amount"]) for r in rates
                     if r.get("activity") == "lab-test"
                     and isinstance(r.get("amount"), (int, float))), None)
    imaging = next((r for r in rates if r.get("activity") == "imaging-study"),
                   {})
    imaging_rate = float(imaging["amount"]) \
        if isinstance(imaging.get("amount"), (int, float)) else None
    imaging_codes = {str(c) for c in imaging.get("procedureCodes") or []}
    or_rate = next((float(r["amount"]) for r in rates
                    if r.get("activity") == "or-minute"
                    and isinstance(r.get("amount"), (int, float))), None)
    or_codes = {str(c) for r in rates if r.get("activity") == "or-minute"
                for c in r.get("procedureCodes") or []}
    line_map = payload.get("serviceLines") or {}
    displays = payload.get("serviceLineDisplays") or {}

    wh = warehouse_module
    factory = wh.warehouse_session_factory()
    lines: dict[str, dict] = {}

    def bucket(line: str) -> dict:
        return lines.setdefault(line, {
            "service_line": line,
            "display": displays.get(line) or line,
            "visits": 0,
            "activities": {
                "bed-day": {"count": 0, "cost": 0.0},
                "lab-test": {"count": 0, "cost": 0.0},
                "imaging-study": {"count": 0, "cost": 0.0},
                "or-minute": {"count": 0, "cost": 0.0}},
            "unpriced_procedures": 0})

    with factory() as wh_session:
        visits = wh_session.scalars(select(wh.VisitOccurrence)).all()
        visit_line: dict[int, str] = {}
        for visit in visits:
            class_code = visit.visit_source_value or ""
            line = str(line_map.get(class_code) or "unattributed")
            visit_line[visit.visit_occurrence_id] = line
            entry = bucket(line)
            entry["visits"] += 1
            rate = bed_day_rates.get(class_code)
            if rate is not None:
                if visit.visit_start_datetime and visit.visit_end_datetime:
                    seconds = (visit.visit_end_datetime
                               - visit.visit_start_datetime).total_seconds()
                    days = max(1, math.ceil(seconds / 86400.0)) \
                        if seconds > 0 else 0
                else:
                    days = 1  # open visit: one bed-day to date
                entry["activities"]["bed-day"]["count"] += days
                entry["activities"]["bed-day"]["cost"] += days * rate
        for measurement in wh_session.scalars(select(wh.Measurement)).all():
            line = visit_line.get(measurement.visit_occurrence_id,
                                  "unattributed")
            entry = bucket(line)
            entry["activities"]["lab-test"]["count"] += 1
            if lab_rate is not None:
                entry["activities"]["lab-test"]["cost"] += lab_rate
        for procedure in wh_session.scalars(
                select(wh.ProcedureOccurrence)).all():
            line = visit_line.get(procedure.visit_occurrence_id,
                                  "unattributed")
            entry = bucket(line)
            code = procedure.procedure_source_value or ""
            if code in imaging_codes:
                entry["activities"]["imaging-study"]["count"] += 1
                if imaging_rate is not None:
                    entry["activities"]["imaging-study"]["cost"] += imaging_rate
            elif code in or_codes:
                # Priced per minute, but the warehouse stores no
                # procedure duration — counted as an OR case with
                # zero priced minutes rather than inventing a length.
                entry["activities"]["or-minute"]["count"] += 0
                entry["or_cases_without_duration"] = \
                    entry.get("or_cases_without_duration", 0) + 1
            else:
                entry["unpriced_procedures"] += 1

    # Encounter -> service line (for episode attribution, if any).
    encounter_lines = {}
    for enc in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Encounter")).all():
        cls = (enc.body or {}).get("class") or {}
        code = cls.get("code") if isinstance(cls, dict) else None
        if code:
            encounter_lines[enc.resource_id] = str(
                line_map.get(code) or "unattributed")
    episodes = _episode_scan(session, line_map, encounter_lines)

    out_lines = []
    grand_total = 0.0
    for line in sorted(lines):
        entry = lines[line]
        total_cost = 0.0
        for activity in entry["activities"].values():
            activity["cost"] = round(activity["cost"], 2)
            total_cost += activity["cost"]
        total_cost = round(total_cost, 2)
        grand_total += total_cost
        episode_count = episodes["by_service_line"].get(line) \
            if episodes["available"] else None
        entry["episodes"] = episode_count
        entry["cost_per_episode"] = (
            round(total_cost / episode_count, 2)
            if episode_count else None)
        entry["total_cost"] = total_cost
        out_lines.append(entry)
    return {
        "currency": payload.get("currency") or "USD",
        "rates_config": {"kind": "terminology", "key": COSTS_KEY,
                         "version": cfg.version if cfg is not None else None},
        "rates": rates,
        "service_lines": out_lines,
        "total_cost": round(grand_total, 2),
        "episodes": episodes,
        "notes": [
            "Bed-days are counted only for visit classes carrying a "
            "published bed-day rate; a visit with no end counts one "
            "bed-day to date.",
            "Lab tests are OMOP measurement rows attributed through "
            "their visit; rows with no visit are 'unattributed'.",
            "OR-minute rate is published but warehouse procedure rows "
            "carry no duration, so priced OR minutes are 0 — not "
            "estimated.",
            episodes["note"],
        ],
    }


# ---------------------------------------------------------------------------
# BD-32: report-designer dataset schema (palette support)
# ---------------------------------------------------------------------------

def _field_type(rows: list[dict], field: str) -> str:
    for row in rows:
        value = row.get(field)
        if value is None:
            continue
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)):
            return "number"
        return "string"
    return "string"


def _dataset_schema(session: Session, key: str) -> dict:
    title, description, builder = reports_module.EXPORT_DATASETS[key]
    rows = builder(session)
    fields = [{"name": name, "type": _field_type(rows, name)}
              for name in sorted(report_engine.dataset_fields(session, key))]
    return {"key": key, "title": title, "description": description,
            "fields": fields,
            "filter_ops": list(report_engine.FILTER_OPS),
            "formats": list(report_engine.FORMATS)}


@router.get("/designer/datasets")
def designer_datasets(session: Session = Depends(get_db),
                      user: User = Depends(require_capability("reports.run"))):
    """Dataset catalogue for the visual report designer's palette:
    every dataset the report engine can compose, with its fields and
    inferred types. Built on app.report_engine's registry — the
    designer composes dataset + fields + filters + grouping through
    the engine API; there is no SQL anywhere in this surface."""
    items = []
    for key in sorted(reports_module.EXPORT_DATASETS):
        schema = _dataset_schema(session, key)
        items.append({"key": key, "title": schema["title"],
                      "description": schema["description"],
                      "field_count": len(schema["fields"]),
                      "fields": schema["fields"]})
    return {"items": items, "total": len(items)}


@router.get("/designer/datasets/{name}")
def designer_dataset(name: str, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("reports.run"))):
    if name not in reports_module.EXPORT_DATASETS:
        raise HTTPException(
            status_code=404,
            detail=(f"Unknown dataset '{name}'; defined datasets: "
                    f"{sorted(reports_module.EXPORT_DATASETS)}"))
    return _dataset_schema(session, name)


# ---------------------------------------------------------------------------
# Defaults hook (wired in app.main)
# ---------------------------------------------------------------------------

def ensure_platform_depth_defaults(session: Session) -> None:
    """Seed this module's published Studio configuration defaults
    (idempotent): the vaccines terminology, the activity-cost rates,
    the immunisation registries, the BD-30 indicator library (via
    app.indicators' governance — see the module docstring for the
    auto-seeding decision), and the dashboard rewiring."""
    _seed_published(session, "terminology", VACCINES_KEY,
                    default_vaccines_payload())
    _seed_published(session, "terminology", COSTS_KEY,
                    default_costs_payload())
    _seed_published(session, "terminology", REGISTRIES_KEY,
                    default_registries_payload())
    session.commit()
    for key, payload in indicators.default_indicator_payloads().items():
        _seed_indicator(session, key, payload)
    _seed_indicator(session, SAFETY_INDICATOR_KEY, SAFETY_INDICATOR_PAYLOAD)
    # Phase 17 BD-30 remainder: the extended library must be
    # published BEFORE _rewire_dashboards runs (it only binds widgets
    # to published indicators). Studio surface: kind `indicator`.
    for key, payload in EXTENDED_INDICATOR_PAYLOADS.items():
        _seed_indicator(session, key, payload)
    _rewire_dashboards(session)
    session.commit()
