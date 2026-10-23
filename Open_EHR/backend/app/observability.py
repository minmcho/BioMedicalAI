"""Observability — Phase 17 (BD-61, PRD-06).

Real wiring, stated precisely:

- **Metrics:** ``GET /metrics`` serves Prometheus text exposition from
  a dedicated registry: process/python collectors plus OpenCare
  gauges computed live from the store at scrape time (FHIR resource
  counts, outbox backlog, lake rows, ADT events) and counters the
  Phase 17 lanes increment (GraphQL executions, realtime fan-out
  events). No labels carry patient identifiers — aggregate counts
  only. The endpoint is unauthenticated like ``/health`` because
  Prometheus scrapes it from the local stack; it exposes no record
  data.
- **Traces:** OpenTelemetry SDK tracing is initialised lazily (first
  status/stream call or app startup) when the published
  ``integration/observability`` config (or the
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var) enables it. Spans go to
  an OTLP/HTTP exporter when an endpoint is configured AND to an
  in-memory exporter whose recent spans ``GET /observability/traces``
  returns — the in-process evidence surface. FastAPI / SQLAlchemy
  auto-instrumentation is applied when the instrumentation packages
  are installed; otherwise a tracer is still available to code that
  asks for one and the status endpoint says so honestly.
- **Studio surface:** kind ``integration``, key ``observability`` —
  ``{"enabled": bool, "service_name": str, "otlp_endpoint": str,
  "metrics_enabled": bool}``. Defaults: enabled, service
  ``opencare-ehr``, no OTLP endpoint (in-memory only), metrics on.

Dashboards + alert rules as code live in ``infra/observability/``
(Prometheus config, alert rules, Grafana provisioning + dashboards);
the collector config lives there too. What runs in this environment
(MinIO/Prometheus/Grafana/otelcol as downloaded binaries under
``~/workspace/live-stack``) is evidenced in the Phase 17 RESULTS.
"""
from __future__ import annotations

import os
import threading
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .configstore import published_config
from .models import AdtEvent, FhirResource, LakeRawEntry, OutboxEvent, User
from .security import get_db, require_capability

router = APIRouter(tags=["observability"])

OBS_KIND = "integration"
OBS_KEY = "observability"

_lock = threading.Lock()
_counters: dict[str, float] = {
    "graphql_requests_total": 0.0,
    "graphql_errors_total": 0.0,
    "realtime_events_total": 0.0,
    "realtime_subscribers": 0.0,
}
_tracing_state: dict = {"initialised": False, "otlp_endpoint": None,
                        "instrumented": [], "error": None}
_memory_exporter = None


def observability_config(session: Session) -> dict:
    cfg = published_config(session, OBS_KIND, OBS_KEY)
    payload = dict(cfg.payload) if cfg is not None and isinstance(
        cfg.payload, dict) else {}
    enabled = payload.get("enabled")
    metrics = payload.get("metrics_enabled")
    return {
        "enabled": enabled if isinstance(enabled, bool) else True,
        "service_name": (payload.get("service_name")
                         if isinstance(payload.get("service_name"), str)
                         and payload.get("service_name")
                         else "opencare-ehr"),
        "otlp_endpoint": (
            os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            or (payload.get("otlp_endpoint")
                if isinstance(payload.get("otlp_endpoint"), str)
                else None)),
        "metrics_enabled": metrics if isinstance(metrics, bool) else True,
    }


def record_graphql(ok: bool) -> None:
    with _lock:
        _counters["graphql_requests_total"] += 1
        if not ok:
            _counters["graphql_errors_total"] += 1


def record_realtime_event() -> None:
    with _lock:
        _counters["realtime_events_total"] += 1


def set_realtime_subscribers(n: int) -> None:
    with _lock:
        _counters["realtime_subscribers"] = float(n)


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------

def ensure_tracing(session: Session | None = None) -> dict:
    """Initialise the OTel SDK once (idempotent). Returns the state."""
    global _memory_exporter
    if _tracing_state["initialised"]:
        return dict(_tracing_state)
    with _lock:
        if _tracing_state["initialised"]:
            return dict(_tracing_state)
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import (
                BatchSpanProcessor, SimpleSpanProcessor)

            from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E501
                InMemorySpanExporter)

            service_name = "opencare-ehr"
            otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            if session is not None:
                cfg = observability_config(session)
                service_name = cfg["service_name"]
                otlp_endpoint = cfg["otlp_endpoint"]
            provider = TracerProvider(resource=Resource.create(
                {"service.name": service_name}))
            _memory_exporter = InMemorySpanExporter()
            provider.add_span_processor(
                SimpleSpanProcessor(_memory_exporter))
            if otlp_endpoint:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: E501
                    OTLPSpanExporter)
                provider.add_span_processor(BatchSpanProcessor(
                    OTLPSpanExporter(endpoint=otlp_endpoint)))
            trace.set_tracer_provider(provider)
            instrumented: list[str] = []
            try:
                from opentelemetry.instrumentation.sqlalchemy import (
                    SQLAlchemyInstrumentor)
                SQLAlchemyInstrumentor().instrument()
                instrumented.append("sqlalchemy")
            except Exception:
                pass
            try:
                from opentelemetry.instrumentation.httpx import (
                    HTTPXClientInstrumentor)
                HTTPXClientInstrumentor().instrument()
                instrumented.append("httpx")
            except Exception:
                pass
            try:
                from opentelemetry.instrumentation.fastapi import (
                    FastAPIInstrumentor)
                FastAPIInstrumentor().instrument()
                instrumented.append("fastapi")
            except Exception:
                pass
            _tracing_state.update({
                "initialised": True, "otlp_endpoint": otlp_endpoint,
                "instrumented": instrumented, "error": None})
        except Exception as exc:  # honest: tracing must never break the API
            _tracing_state.update({"initialised": False, "error": str(exc)})
    return dict(_tracing_state)


def recent_spans(limit: int = 50) -> list[dict]:
    if _memory_exporter is None:
        return []
    spans = _memory_exporter.get_finished_spans()[-limit:]
    out = []
    for span in spans:
        ctx = span.get_span_context()
        out.append({
            "name": span.name,
            "trace_id": format(ctx.trace_id, "032x"),
            "span_id": format(ctx.span_id, "016x"),
            "parent_span_id": (format(span.parent.span_id, "016x")
                               if span.parent else None),
            "status": str(span.status.status_code),
        })
    return out


def get_tracer():
    from opentelemetry import trace

    return trace.get_tracer("opencare.ehr")


@router.on_event("startup")
def _startup_tracing() -> None:
    ensure_tracing()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.get("/metrics")
def metrics(request: Request):
    """Prometheus exposition. Computed live on each scrape."""
    from prometheus_client import (CollectorRegistry, Counter, Gauge,
                                   generate_latest)
    from prometheus_client import PlatformCollector

    registry = CollectorRegistry()
    # PlatformCollector must be GIVEN the registry: constructed
    # bare it registers into the global default registry, and the
    # second scrape then raises DuplicateTimeseries.
    PlatformCollector(registry=registry)
    with _lock:
        snapshot = dict(_counters)
    gql_total = Counter("opencare_graphql_requests_total",
                        "GraphQL executions", registry=registry)
    gql_total.inc(snapshot["graphql_requests_total"])
    gql_err = Counter("opencare_graphql_errors_total",
                      "GraphQL executions with errors", registry=registry)
    gql_err.inc(snapshot["graphql_errors_total"])
    rt_events = Counter("opencare_realtime_events_total",
                        "Realtime fan-out events published",
                        registry=registry)
    rt_events.inc(snapshot["realtime_events_total"])
    rt_subs = Gauge("opencare_realtime_subscribers",
                    "Current realtime subscribers", registry=registry)
    rt_subs.set(snapshot["realtime_subscribers"])
    patients = Gauge("opencare_fhir_patients", "Stored Patient resources",
                     registry=registry)
    resources = Gauge("opencare_fhir_resources", "Stored FHIR resources",
                      registry=registry)
    outbox_pending = Gauge("opencare_outbox_pending",
                           "Outbox events awaiting ETL", registry=registry)
    lake_rows = Gauge("opencare_lake_raw_rows", "Raw-zone lake rows",
                      registry=registry)
    adt_events = Gauge("opencare_adt_events", "ADT events recorded",
                       registry=registry)
    try:
        factory = request.app.state.session_factory
        with factory() as session:
            patients.set(session.scalar(
                select(func.count()).select_from(FhirResource).where(
                    FhirResource.resource_type == "Patient")) or 0)
            resources.set(session.scalar(
                select(func.count()).select_from(FhirResource)) or 0)
            outbox_pending.set(session.scalar(
                select(func.count()).select_from(OutboxEvent).where(
                    OutboxEvent.status == "pending")) or 0)
            lake_rows.set(session.scalar(
                select(func.count()).select_from(LakeRawEntry)) or 0)
            adt_events.set(session.scalar(
                select(func.count()).select_from(AdtEvent)) or 0)
    except Exception:
        pass  # a scrape must never 500 on a store hiccup
    body = generate_latest(registry)
    return PlainTextResponse(body.decode("utf-8"),
                             media_type="text/plain; version=0.0.4")


@router.get("/observability/status")
def obs_status(session: Session = Depends(get_db),
               user: User = Depends(require_capability("continuum.read"))):
    cfg = observability_config(session)
    state = ensure_tracing(session)
    return {"config": cfg, "tracing": state,
            "counters": dict(_counters),
            "spans_in_memory": len(recent_spans(limit=10000)),
            "checked_at": time.time()}


@router.get("/observability/traces")
def obs_traces(limit: int = 50, session: Session = Depends(get_db),
               user: User = Depends(require_capability("continuum.read"))):
    ensure_tracing(session)
    spans = recent_spans(limit=limit)
    trace_ids = sorted({s["trace_id"] for s in spans})
    return {"spans": spans, "trace_ids": trace_ids, "total": len(spans)}
