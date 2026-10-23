"""Phase 16 (WS-B) — External gateway legs (app.gateway).

Three "built to the interface, live leg external" capabilities. Each
is built end-to-end against its published interface structure and
proven in-suite against LOCAL FIXTURE endpoints only; every
production leg is NOT RUN (external onboarding / certification) and
is recorded as such in the workstream integration note:

- BD-38 Aggregate public-health reporting + gateway leg. Aggregate
  extracts are defined in the published `integration/public-health`
  Studio config (each definition names a quality-measure key or a
  cohort key as its source). POST /gateway/public-health/extract
  computes an aggregate from LIVE data — quality measures through
  app.quality.compute_measure and cohorts through
  app.pophealth.registry_summary, both over the OMOP warehouse — so
  an extract is counts/rates only, never patient-level rows.
  Submission is a dispatcher in the Phase 8 outbox pattern
  (app.interop): a submission row is created, each HTTP attempt is
  an append-only GatewayAttempt row, and a submission whose attempts
  are exhausted dead-letters (history never rewritten). With NO
  gateway URL published, the submission is recorded
  `queued-for-gateway` — generated and queued locally, NOT sent
  (the Phase 15 platform_depth VXU honesty pattern).

- BD-39 Clearinghouse submission. POST /gateway/clearinghouse/submit
  renders the claim's existing X12 837P export
  (app.billing.render_837p — the same bytes the export endpoint
  serves) and posts it to the published
  `integration/clearinghouse` endpoint URL. The acknowledgement is
  parsed for accepted/rejected status (999 AK9 and 277CA STC
  shapes); the outcome + reasons are recorded on the CLAIM itself
  (a supportingInfo entry, the app.revenue remittance pattern) and
  in the hash-chained audit. Transport failures (non-2xx / network)
  are NOT rejections: they follow the outbox retry posture
  (pending -> retry via dispatch -> dead-letter). With no endpoint
  URL published the submission is `queued-for-gateway`, never
  claimed sent. Production clearinghouse leg NOT RUN.

- BD-40 Payer EDI 270/271 + Surescripts interface. POST
  /gateway/eligibility/edi builds an X12 270 from the Patient
  resource + the published payer config, posts it to the payer TEST
  endpoint named in the published `integration/edi` config, and
  parses the 271 into the Phase 7 eligibility record shape
  (app.models.EligibilityCheck, the same table app.access writes),
  with `basis: "payer-edi"` — never conflated with the Phase 7
  local evaluation's `basis: "local-config"`. A 271 that was not
  actually received produces NO record: with no endpoint configured
  the endpoint returns the honest 503 and records nothing.
  Surescripts: POST /gateway/erx/newrx builds an NCPDP SCRIPT
  NewRx XML document to the interface structure, transmits it to
  the configured endpoint when one is published (otherwise the
  transmission is recorded `queued-for-gateway`, not sent), parses
  the SCRIPT response (Status / Error) and records the outcome.
  Surescripts network certification NOT RUN.

Configure, don't fork: gateway URL, clearinghouse URL, payer EDI
endpoints, the Surescripts endpoint, retry policies and the
aggregate definitions are all published Studio config
(kind `integration`); a site onboards a real leg by publishing its
endpoints — no code change. Synthetic data only.
"""
import hashlib
import json
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape as xml_escape

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (DateTime, ForeignKey, Integer, JSON, String, Text,
                        select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import access as access_mod
from . import billing, fhir
from . import pophealth as ph
from . import quality as quality_mod
from . import warehouse as wh
from .audit import append_audit
from .configstore import published_config
from .db import Base
from .models import (ConfigStatus, EligibilityCheck, FhirResource,
                     StudioConfig, User, utcnow)
from .security import get_db, require_capability

router = APIRouter(prefix="/gateway", tags=["gateway"])

PUBLIC_HEALTH_KEY = "public-health"        # integration/public-health
CLEARINGHOUSE_KEY = "clearinghouse"        # integration/clearinghouse
EDI_KEY = "edi"                            # integration/edi

EDI_BASIS = "payer-edi"
EDI_DISCLAIMER = (
    "Eligibility determined from a payer EDI 271 response received "
    "from the configured payer TEST endpoint (integration/edi). "
    "Production payer EDI onboarding is NOT RUN; in-suite evidence "
    "is against local fixture endpoints only.")
ERX_DISCLAIMER = (
    "NewRx built to the NCPDP SCRIPT interface structure and "
    "transmitted only to the configured endpoint; Surescripts "
    "network certification is NOT RUN.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# This module's own tables (the in-module outbox + transmission log)
# ---------------------------------------------------------------------------

class GatewaySubmission(Base):
    """One gateway submission (public-health extract or clearinghouse
    claim). Mirrors app.interop's Hl7OutboundMessage posture: the
    request payload is stored verbatim, `attempts` counts every send
    try, `attempt_base` is the attempts value at the last replay-less
    creation, and the status runs pending -> sent / accepted /
    rejected, or -> dead_letter when the retry budget is exhausted,
    or sits at `queued-for-gateway` when no endpoint is configured
    (generated locally, NOT sent — never claimed otherwise)."""
    __tablename__ = "gateway_submissions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[str] = mapped_column(String(120), unique=True,
                                               nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    endpoint_url: Mapped[str] = mapped_column(String(500), nullable=False,
                                              default="")
    request_payload: Mapped[str] = mapped_column(Text, nullable=False,
                                                 default="")
    response_payload: Mapped[str] = mapped_column(Text, nullable=False,
                                                  default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False,
                                        default="pending", index=True)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_base: Mapped[int] = mapped_column(Integer, nullable=False,
                                              default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                default=utcnow, onupdate=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class GatewayAttempt(Base):
    """One dispatch attempt against one gateway submission —
    append-only (the interop Hl7SendAttempt pattern): retries and
    dead-lettering add rows, they never edit or delete these."""
    __tablename__ = "gateway_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_pk: Mapped[int] = mapped_column(
        ForeignKey("gateway_submissions.id"), nullable=False, index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class ErxTransmission(Base):
    """One NCPDP SCRIPT NewRx transmission record: the exact XML
    built, where it was addressed, and the parsed outcome. A
    transmission with no configured endpoint is recorded
    `queued-for-gateway` — built and recorded locally, NOT sent."""
    __tablename__ = "gateway_erx_transmissions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transmission_id: Mapped[str] = mapped_column(String(120), unique=True,
                                                 nullable=False, index=True)
    patient_ref: Mapped[str | None] = mapped_column(String(200),
                                                    nullable=True)
    endpoint_url: Mapped[str] = mapped_column(String(500), nullable=False,
                                              default="")
    request_xml: Mapped[str] = mapped_column(Text, nullable=False)
    response_payload: Mapped[str] = mapped_column(Text, nullable=False,
                                                  default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False,
                                        default="queued-for-gateway",
                                        index=True)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


# ---------------------------------------------------------------------------
# Seeded published configuration
# ---------------------------------------------------------------------------

def default_public_health_payload() -> dict:
    """Public-health gateway config (integration/public-health).
    `gatewayUrl` is EMPTY in the seed: no aggregate leaves this
    system until a site publishes its jurisdiction gateway endpoint.
    Aggregate definitions name their source — a published quality
    measure key or a published cohort key — and extracts compute
    from live warehouse data at submission time."""
    return {
        "name": "Public-health aggregate reporting (gateway leg NOT RUN)",
        "gatewayUrl": "",
        "retry": {"maxAttempts": 3, "backoffSeconds": 0},
        "aggregates": [
            {"name": "diabetes-hba1c-control",
             "title": "Diabetes HbA1c control (aggregate)",
             "sourceType": "measure", "sourceKey": "hba1c-control"},
            {"name": "hypertension-bp-control",
             "title": "Hypertension BP control (aggregate)",
             "sourceType": "measure", "sourceKey": "bp-control"},
            {"name": "diabetes-registry-count",
             "title": "Diabetes registry member count (aggregate)",
             "sourceType": "cohort", "sourceKey": "diabetes"},
            {"name": "hypertension-registry-count",
             "title": "Hypertension registry member count (aggregate)",
             "sourceType": "cohort", "sourceKey": "hypertension"},
        ],
    }


def default_clearinghouse_payload() -> dict:
    """Clearinghouse config (integration/clearinghouse). The endpoint
    URL is EMPTY in the seed: claims are recorded queued-for-gateway
    until a site publishes its clearinghouse (test) endpoint.
    Production clearinghouse leg NOT RUN."""
    return {
        "name": "Clearinghouse submission (production leg NOT RUN)",
        "endpointUrl": "",
        "retry": {"maxAttempts": 3, "backoffSeconds": 0},
    }


def default_edi_payload() -> dict:
    """Payer EDI + e-prescribing config (integration/edi). Payer
    endpoint URLs are EMPTY in the seed (per payer code, matching
    terminology/payers); the Surescripts endpoint is empty too.
    Production payer EDI onboarding and Surescripts network
    certification are NOT RUN."""
    return {
        "name": "Payer EDI 270/271 + Surescripts (live legs NOT RUN)",
        "retry": {"maxAttempts": 3, "backoffSeconds": 0},
        "payers": [
            {"code": "synthetic-payer", "endpointUrl": "",
             "senderId": "OPENCARE", "receiverId": "SYNTHPAYER"},
            {"code": "synthetic-payer-plus", "endpointUrl": "",
             "senderId": "OPENCARE", "receiverId": "SYNTHPAYERPLUS"},
        ],
        "surescripts": {"endpointUrl": "", "senderId": "OPENCARE"},
    }


def _seed_published(session: Session, kind: str, key: str,
                    payload: dict) -> None:
    existing = session.scalars(
        select(StudioConfig).where(StudioConfig.kind == kind,
                                   StudioConfig.key == key)).first()
    if existing is not None:
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


def ensure_gateway_defaults(session: Session) -> None:
    """Seed the three gateway integration configs as published v1
    (idempotent), exactly like the other modules."""
    _seed_published(session, "integration", PUBLIC_HEALTH_KEY,
                    default_public_health_payload())
    _seed_published(session, "integration", CLEARINGHOUSE_KEY,
                    default_clearinghouse_payload())
    _seed_published(session, "integration", EDI_KEY,
                    default_edi_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Config accessors (published config wins; the seed payload is the
# fallback so the surface behaves before/without seeding, the
# app.rfid config pattern)
# ---------------------------------------------------------------------------

def _config_payload(session: Session, key: str, default_fn) -> dict:
    cfg = published_config(session, "integration", key)
    if cfg is not None and isinstance(cfg.payload, dict) and cfg.payload:
        return cfg.payload
    return default_fn()


def _ph_payload(session: Session) -> dict:
    return _config_payload(session, PUBLIC_HEALTH_KEY,
                           default_public_health_payload)


def _ch_payload(session: Session) -> dict:
    return _config_payload(session, CLEARINGHOUSE_KEY,
                           default_clearinghouse_payload)


def _edi_payload(session: Session) -> dict:
    return _config_payload(session, EDI_KEY, default_edi_payload)


def _retry_policy(payload: dict) -> tuple[int, float]:
    retry = payload.get("retry") or {}
    max_attempts = retry.get("maxAttempts")
    backoff = retry.get("backoffSeconds")
    return (max_attempts if isinstance(max_attempts, int)
            and not isinstance(max_attempts, bool) and max_attempts >= 1
            else 3,
            float(backoff) if isinstance(backoff, (int, float))
            and not isinstance(backoff, bool) and backoff >= 0 else 0.0)


def _aggregate_definitions(session: Session) -> list[dict]:
    return [d for d in (_ph_payload(session).get("aggregates") or [])
            if isinstance(d, dict) and d.get("name")]


def _aggregate_definition(session: Session, name: str) -> dict:
    for definition in _aggregate_definitions(session):
        if definition["name"] == name:
            return definition
    raise HTTPException(
        status_code=404,
        detail=(f"No aggregate definition '{name}' in the published "
                f"integration/{PUBLIC_HEALTH_KEY} config"))


def _payer_edi_entry(session: Session, payer_code: str) -> dict | None:
    for entry in _edi_payload(session).get("payers") or []:
        if isinstance(entry, dict) and entry.get("code") == payer_code:
            return entry
    return None


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


def _post(url: str, content: str, content_type: str) -> tuple[int | None, str, str]:
    """One real HTTP POST. Returns (http_status, response_text,
    transport_error): on a transport failure http_status is None and
    the error text names the problem — the caller records it as a
    failed attempt, never as a business rejection."""
    try:
        with httpx.Client(trust_env=False, timeout=10.0) as client:
            response = client.post(
                url, content=content.encode("utf-8"),
                headers={"Content-Type": content_type})
        return response.status_code, response.text, ""
    except Exception as exc:  # connection refused, DNS, timeout, ...
        return None, "", f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Submission rendering + the shared dispatch pass
# ---------------------------------------------------------------------------

def _submission_out(session: Session, row: GatewaySubmission,
                    detail: bool = False) -> dict:
    out = {"id": row.submission_id, "kind": row.kind, "ref": row.ref,
           "status": row.status, "endpoint_url": row.endpoint_url,
           "attempts": row.attempts, "last_error": row.last_error,
           "detail": row.detail or {},
           "created_by": row.created_by,
           "created_at": row.created_at.isoformat() if row.created_at else None,
           "sent_at": row.sent_at.isoformat() if row.sent_at else None}
    if detail:
        out["request_payload"] = row.request_payload
        out["response_payload"] = row.response_payload
        attempts = session.scalars(
            select(GatewayAttempt)
            .where(GatewayAttempt.submission_pk == row.id)
            .order_by(GatewayAttempt.id.asc())).all()
        out["attempt_history"] = [
            {"attempt_no": a.attempt_no, "outcome": a.outcome,
             "detail": a.detail, "http_status": a.http_status,
             "at": a.created_at.isoformat() if a.created_at else None}
            for a in attempts]
    return out


def _attempt_once(session: Session, row: GatewaySubmission,
                  config_payload: dict, now: datetime,
                  on_response) -> str:
    """Perform exactly one dispatch attempt against one pending
    submission and record it append-only. `on_response(row,
    response_text)` runs on a 2xx and returns the terminal status it
    recorded (e.g. "sent" / "accepted" / "rejected"); transport and
    HTTP failures follow the outbox posture: pending with backoff,
    or dead_letter when the attempt budget is exhausted. Returns the
    row's resulting status."""
    max_attempts, backoff = _retry_policy(config_payload)
    row.attempts += 1
    content_type = "application/json" if row.kind == "public-health" \
        else "text/plain"
    http_status, text, error = _post(row.endpoint_url, row.request_payload,
                                     content_type)
    if http_status is not None and 200 <= http_status < 300:
        row.response_payload = text
        outcome = on_response(row, text)
        session.add(GatewayAttempt(
            submission_pk=row.id, attempt_no=row.attempts,
            outcome=outcome, http_status=http_status,
            detail=(text[:500] if text else "2xx with empty body")))
        row.last_error = None
        row.next_attempt_at = None
        row.sent_at = now
        session.flush()
        return row.status
    detail_text = error or f"HTTP {http_status}: {text[:300]}"
    if row.attempts - row.attempt_base >= max_attempts:
        row.status = "dead_letter"
        row.next_attempt_at = None
        outcome = "dead_letter"
    else:
        wait = backoff * (2 ** (row.attempts - row.attempt_base - 1))
        row.next_attempt_at = now + timedelta(seconds=wait)
        outcome = "error"
    row.last_error = detail_text
    session.add(GatewayAttempt(
        submission_pk=row.id, attempt_no=row.attempts, outcome=outcome,
        detail=detail_text, http_status=http_status))
    session.flush()
    return row.status


def _dispatch_pending(session: Session, kind: str, config_payload: dict,
                      on_response, now: datetime | None = None) -> dict:
    """One dispatcher pass over the pending submissions of one kind
    (the app.interop.dispatch_pending shape): every due row gets
    exactly one attempt. The caller owns commit + audit."""
    now = now or _now()
    stats = {"attempted": 0, "succeeded": 0, "failed": 0,
             "dead_lettered": 0, "skipped": 0}
    rows = session.scalars(
        select(GatewaySubmission)
        .where(GatewaySubmission.kind == kind,
               GatewaySubmission.status == "pending")
        .order_by(GatewaySubmission.id.asc())).all()
    for row in rows:
        if row.next_attempt_at is not None \
                and _aware(row.next_attempt_at) > now:
            stats["skipped"] += 1
            continue
        before = row.status
        _attempt_once(session, row, config_payload, now, on_response)
        stats["attempted"] += 1
        if row.status == "dead_letter":
            stats["dead_lettered"] += 1
        elif row.status == before:
            stats["failed"] += 1
        else:
            stats["succeeded"] += 1
    return stats


def _get_submission(session: Session, kind: str,
                    submission_id: str) -> GatewaySubmission:
    row = session.scalars(
        select(GatewaySubmission).where(
            GatewaySubmission.kind == kind,
            GatewaySubmission.submission_id == submission_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Submission '{submission_id}' not found")
    return row


# ---------------------------------------------------------------------------
# BD-38 — aggregate public-health extracts + gateway submission
# ---------------------------------------------------------------------------

def compute_extract(session: Session, definition: dict,
                    period: str | None) -> dict:
    """Compute one aggregate from LIVE data: a quality measure via
    app.quality.compute_measure or a cohort via
    app.pophealth.registry_summary — both over the OMOP warehouse,
    both aggregate-only with small-cell suppression applied by the
    owning engine. The extract carries counts/rate only; there are
    no patient-level rows anywhere in it."""
    source_type = definition.get("sourceType")
    source_key = definition.get("sourceKey")
    factory = wh.warehouse_session_factory()
    if source_type == "measure":
        cfg = published_config(session, "measure", str(source_key))
        if cfg is None:
            raise HTTPException(
                status_code=422,
                detail=(f"Aggregate '{definition['name']}' names measure "
                        f"'{source_key}', which has no published measure "
                        "config"))
        with factory() as wh_session:
            wh.ensure_concept_map(wh_session)
            result = quality_mod.compute_measure(
                wh_session, str(source_key), cfg.payload or {})
    elif source_type == "cohort":
        cfg = published_config(session, "cohort", str(source_key))
        if cfg is None:
            raise HTTPException(
                status_code=422,
                detail=(f"Aggregate '{definition['name']}' names cohort "
                        f"'{source_key}', which has no published cohort "
                        "config"))
        with factory() as wh_session:
            wh.ensure_concept_map(wh_session)
            result = ph.registry_summary(wh_session, str(source_key),
                                         cfg.payload or {})
    else:
        raise HTTPException(
            status_code=422,
            detail=(f"Aggregate '{definition['name']}' has unknown "
                    f"sourceType '{source_type}' (expected 'measure' "
                    "or 'cohort')"))
    return {
        "definition": definition["name"],
        "title": definition.get("title"),
        "source": {"type": source_type, "key": source_key},
        "period": period,
        "computed_at": _now_iso(),
        "aggregate": result,
        "contains_patient_data": False,
        "notice": ("Aggregate-only public-health extract: counts and "
                   "rates with small-cell suppression, computed from "
                   "live warehouse data. No patient-level rows."),
    }


@router.get("/public-health/definitions")
def list_ph_definitions(session: Session = Depends(get_db),
                        user: User = Depends(require_capability("pophealth.view"))):
    """The aggregate definitions in the published
    integration/public-health config + where submissions would go."""
    payload = _ph_payload(session)
    return {"definitions": _aggregate_definitions(session),
            "gateway_url_configured": bool(payload.get("gatewayUrl")),
            "retry": payload.get("retry")}


@router.post("/public-health/extract")
async def extract_public_health(request: Request,
                                session: Session = Depends(get_db),
                                user: User = Depends(require_capability("pophealth.view"))):
    """Compute one aggregate extract {definition, period} from live
    quality/cohort data. Computation only — nothing is transmitted
    and no submission is recorded."""
    payload = await _json_body(request)
    name = payload.get("definition")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=422,
                            detail="Extract requires 'definition'")
    period = payload.get("period")
    if period is not None and not isinstance(period, str):
        raise HTTPException(status_code=422,
                            detail="'period' must be a string when given")
    definition = _aggregate_definition(session, name)
    extract = compute_extract(session, definition, period)
    append_audit(session, actor=user.username,
                 action="gateway.public_health.extract",
                 entity=f"public-health/{name}",
                 payload={"definition": name, "period": period,
                          "source": extract["source"]})
    session.commit()
    return extract


def _ph_on_response(row: GatewaySubmission, text: str) -> str:
    row.status = "sent"
    row.detail = {**(row.detail or {}),
                  "gateway_response": text[:500]}
    return "sent"


@router.post("/public-health/submit", status_code=201)
async def submit_public_health(request: Request,
                               session: Session = Depends(get_db),
                               user: User = Depends(require_capability("interop.manage"))):
    """Compute the extract and submit it to the configured gateway
    endpoint. No gateway URL published -> the submission is recorded
    `queued-for-gateway` (computed and queued locally, NOT sent).
    With a URL, one dispatch attempt runs now; failures stay pending
    for the dispatch pass until the retry budget dead-letters them."""
    payload = await _json_body(request)
    name = payload.get("definition")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=422,
                            detail="Submission requires 'definition'")
    period = payload.get("period")
    if period is not None and not isinstance(period, str):
        raise HTTPException(status_code=422,
                            detail="'period' must be a string when given")
    definition = _aggregate_definition(session, name)
    extract = compute_extract(session, definition, period)
    config = _ph_payload(session)
    url = str(config.get("gatewayUrl") or "")
    row = GatewaySubmission(
        submission_id=f"ph-{uuid.uuid4().hex[:16]}", kind="public-health",
        ref=name, endpoint_url=url,
        request_payload=json.dumps(extract, sort_keys=True),
        status="pending" if url else "queued-for-gateway",
        detail={"definition": name, "period": period,
                "source": extract["source"],
                "not_sent_reason": None if url else
                "no gateway URL in the published integration/"
                "public-health config — recorded locally for gateway "
                "pickup, NOT sent (production gateway leg NOT RUN)"},
        created_by=user.username)
    session.add(row)
    session.flush()
    if url:
        _attempt_once(session, row, config, _now(), _ph_on_response)
    append_audit(session, actor=user.username,
                 action="gateway.public_health.submit",
                 entity=f"gateway-submission/{row.submission_id}",
                 payload={"definition": name, "period": period,
                          "status": row.status, "attempts": row.attempts,
                          "endpoint_configured": bool(url)})
    session.commit()
    return _submission_out(session, row, detail=True)


@router.post("/public-health/dispatch")
def dispatch_public_health(session: Session = Depends(get_db),
                           user: User = Depends(require_capability("interop.manage"))):
    """Run one dispatcher pass over pending public-health
    submissions (the retry leg of the outbox posture)."""
    config = _ph_payload(session)
    stats = _dispatch_pending(session, "public-health", config,
                              _ph_on_response)
    append_audit(session, actor=user.username,
                 action="gateway.public_health.dispatch",
                 entity="gateway/public-health", payload=stats)
    session.commit()
    return {"stats": stats}


@router.get("/public-health/submissions")
def list_ph_submissions(session: Session = Depends(get_db),
                        user: User = Depends(require_capability("interop.read"))):
    rows = session.scalars(
        select(GatewaySubmission)
        .where(GatewaySubmission.kind == "public-health")
        .order_by(GatewaySubmission.id.desc())).all()
    return {"items": [_submission_out(session, r) for r in rows],
            "total": len(rows)}


@router.get("/public-health/submissions/{submission_id}")
def get_ph_submission(submission_id: str,
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("interop.read"))):
    return _submission_out(
        session, _get_submission(session, "public-health", submission_id),
        detail=True)


# ---------------------------------------------------------------------------
# BD-39 — clearinghouse submission (837 out, 999/277 ack back)
# ---------------------------------------------------------------------------

def parse_clearinghouse_ack(text: str) -> dict:
    """Parse a clearinghouse acknowledgement in the two shapes this
    leg consumes: an X12 999 (functional acknowledgement — AK9
    carries the verdict) or an X12 277CA (claim acknowledgement —
    STC segments carry per-claim accepted/rejected + reasons).
    Returns {ack_type, accepted (True/False/None when the shape
    carries no verdict), reasons, statuses}. A 999 'E'
    (accepted-with-errors) counts as accepted with the errors kept
    as reasons; anything unrecognised yields accepted=None and is
    recorded as ack-unparsed by the caller, never guessed."""
    if not isinstance(text, str) or not text.strip():
        return {"ack_type": "empty", "accepted": None, "reasons": [],
                "statuses": []}
    segments = [[el.strip() for el in seg.split("*")]
                for seg in text.replace("\r", " ").split("~") if seg.strip()]
    reasons: list[str] = []
    statuses: list[str] = []
    ak9 = next((s for s in segments if s and s[0] == "AK9"), None)
    if ak9 is not None:
        code = ak9[1] if len(ak9) > 1 else ""
        for seg in segments:
            if seg and seg[0] in ("AK5", "IK5") and len(seg) > 1 \
                    and seg[1] not in ("A", ""):
                reasons.append(f"{seg[0]}: {'*'.join(seg[1:])}")
        return {"ack_type": "999",
                "accepted": code in ("A", "E"),
                "verdict_code": code,
                "reasons": reasons or
                ([f"AK9 verdict '{code}'"] if code else []),
                "statuses": statuses}
    stcs = [s for s in segments if s and s[0] == "STC"]
    if stcs:
        accepted = True
        for seg in stcs:
            composite = seg[1] if len(seg) > 1 else ""
            category = composite.split(":")[0]
            statuses.append(category)
            if category == "A3":
                accepted = False
                reason = seg[10] if len(seg) > 10 and seg[10] else ""
                reasons.append(reason or f"STC {composite} (rejected)")
            elif category not in ("A1", "A2", "A7", "A6", "A8"):
                accepted = False
                reasons.append(f"STC {composite} (not an acceptance)")
        for seg in segments:
            if seg and seg[0] == "MSG" and len(seg) > 1 and seg[1]:
                reasons.append(seg[1])
        return {"ack_type": "277", "accepted": accepted,
                "verdict_code": ",".join(statuses),
                "reasons": reasons, "statuses": statuses}
    return {"ack_type": "unknown", "accepted": None, "reasons": [],
            "statuses": statuses}


def clearinghouse_entries(body: dict) -> list[dict]:
    """The clearinghouse-submission records on a claim (supportingInfo
    category `clearinghouse`, the app.revenue remittance pattern)."""
    entries = []
    for info in body.get("supportingInfo") or []:
        if not isinstance(info, dict):
            continue
        category = ((info.get("category") or {}).get("coding") or [{}])[0]
        if category.get("system") != billing.STATUS_HISTORY_CATEGORY:
            continue
        if category.get("code") != "clearinghouse":
            continue
        try:
            entries.append(json.loads(info.get("valueString") or "{}"))
        except (TypeError, ValueError):
            continue
    return entries


def _record_ack_on_claim(session: Session, row: GatewaySubmission,
                         ack: dict, actor: str) -> None:
    """Record the parsed ack on the CLAIM record itself (a
    supportingInfo entry) + audit. The claim's lifecycle status is
    NOT touched: clearinghouse acceptance is a transmission fact,
    not a payer adjudication (paid/denied arrive via 835)."""
    claim_row = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Claim",
                                   FhirResource.resource_id == row.ref)
    ).first()
    if claim_row is None:
        return
    body = dict(claim_row.body or {})
    entry = {"submission_id": row.submission_id,
             "outcome": "accepted" if ack["accepted"] else "rejected",
             "ack_type": ack["ack_type"], "reasons": ack["reasons"],
             "at": _now_iso(), "by": actor}
    infos = list(body.get("supportingInfo") or [])
    infos.append({
        "sequence": len(infos) + 1,
        "category": {"coding": [{"system": billing.STATUS_HISTORY_CATEGORY,
                                 "code": "clearinghouse",
                                 "display": "Clearinghouse submission"}]},
        "valueString": json.dumps(entry, sort_keys=True)})
    body["supportingInfo"] = infos
    claim_row.body = body
    claim_row.version += 1
    session.flush()
    append_audit(session, actor=actor,
                 action="gateway.clearinghouse.ack",
                 entity=f"Claim/{row.ref}",
                 payload={"submission_id": row.submission_id,
                          "outcome": entry["outcome"],
                          "ack_type": ack["ack_type"],
                          "reasons": ack["reasons"]})


def _make_ch_on_response(actor: str):
    def on_response(row: GatewaySubmission, text: str) -> str:
        ack = parse_clearinghouse_ack(text)
        if ack["accepted"] is None:
            row.status = "ack-unparsed"
            row.detail = {**(row.detail or {}), "ack": ack}
            return "ack-unparsed"
        row.status = "accepted" if ack["accepted"] else "rejected"
        row.detail = {**(row.detail or {}), "ack": ack}
        return row.status
    on_response._actor = actor  # type: ignore[attr-defined]
    return on_response


@router.post("/clearinghouse/submit", status_code=201)
async def submit_clearinghouse(request: Request,
                               session: Session = Depends(get_db),
                               user: User = Depends(require_capability("billing.manage"))):
    """Submit one claim to the clearinghouse: the claim must be in
    an exportable state (the 837 export rule); its 837P payload is
    rendered by app.billing and posted to the published
    integration/clearinghouse endpoint. The parsed 999/277 ack is
    recorded on the claim + audit. No endpoint published -> the
    submission is recorded `queued-for-gateway`, NOT sent."""
    payload = await _json_body(request)
    claim_id = payload.get("claim_id") or payload.get("claimId")
    if not claim_id:
        raise HTTPException(status_code=422,
                            detail="Submission requires 'claim_id'")
    claim_id = str(claim_id)
    claim = fhir.get_resource("Claim", claim_id, session, user)
    status = claim.get("status")
    if status not in billing._exportable_states(session):
        raise HTTPException(
            status_code=409,
            detail=(f"Claim {claim_id} is in status '{status}'; a "
                    "clearinghouse submission requires a submitted "
                    "claim (the 837 export rule)"))
    patient_ref = (claim.get("patient") or {}).get("reference") or ""
    patient_id = patient_ref.split("/", 1)[1] if "/" in patient_ref else ""
    patient_row = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Patient",
                                   FhirResource.resource_id == patient_id)
    ).first()
    if patient_row is None:
        raise HTTPException(status_code=422,
                            detail=f"Claim references unknown patient "
                                   f"'{patient_ref}'")
    text_837 = billing.render_837p(claim, fhir._render(patient_row))
    config = _ch_payload(session)
    url = str(config.get("endpointUrl") or "")
    row = GatewaySubmission(
        submission_id=f"ch-{uuid.uuid4().hex[:16]}", kind="clearinghouse",
        ref=claim_id, endpoint_url=url, request_payload=text_837,
        status="pending" if url else "queued-for-gateway",
        detail={"claim_status": status,
                "not_sent_reason": None if url else
                "no endpoint URL in the published integration/"
                "clearinghouse config — recorded locally, NOT sent "
                "(production clearinghouse leg NOT RUN)"},
        created_by=user.username)
    session.add(row)
    session.flush()
    if url:
        before_status = row.status
        _attempt_once(session, row, config, _now(),
                      _make_ch_on_response(user.username))
        if row.status in ("accepted", "rejected"):
            ack = (row.detail or {}).get("ack") or {}
            _record_ack_on_claim(session, row, ack, user.username)
        _ = before_status
    append_audit(session, actor=user.username,
                 action="gateway.clearinghouse.submit",
                 entity=f"Claim/{claim_id}",
                 payload={"submission_id": row.submission_id,
                          "status": row.status, "attempts": row.attempts,
                          "endpoint_configured": bool(url)})
    session.commit()
    return _submission_out(session, row, detail=True)


@router.post("/clearinghouse/dispatch")
def dispatch_clearinghouse(session: Session = Depends(get_db),
                           user: User = Depends(require_capability("interop.manage"))):
    """One dispatcher pass over pending clearinghouse submissions;
    acks received on a retry are recorded on their claims exactly as
    on the first attempt."""
    config = _ch_payload(session)
    stats = _dispatch_pending(session, "clearinghouse", config,
                              _make_ch_on_response(user.username))
    # Record any acks that landed during this pass on their claims.
    rows = session.scalars(
        select(GatewaySubmission)
        .where(GatewaySubmission.kind == "clearinghouse",
               GatewaySubmission.status.in_(("accepted", "rejected")))).all()
    for row in rows:
        claim_row = session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "Claim",
                FhirResource.resource_id == row.ref)).first()
        if claim_row is None:
            continue
        recorded = {e.get("submission_id")
                    for e in clearinghouse_entries(claim_row.body or {})}
        if row.submission_id not in recorded:
            ack = (row.detail or {}).get("ack") or {}
            if ack:
                _record_ack_on_claim(session, row, ack, user.username)
    append_audit(session, actor=user.username,
                 action="gateway.clearinghouse.dispatch",
                 entity="gateway/clearinghouse", payload=stats)
    session.commit()
    return {"stats": stats}


@router.get("/clearinghouse/submissions")
def list_ch_submissions(session: Session = Depends(get_db),
                        user: User = Depends(require_capability("interop.read"))):
    rows = session.scalars(
        select(GatewaySubmission)
        .where(GatewaySubmission.kind == "clearinghouse")
        .order_by(GatewaySubmission.id.desc())).all()
    return {"items": [_submission_out(session, r) for r in rows],
            "total": len(rows)}


@router.get("/clearinghouse/submissions/{submission_id}")
def get_ch_submission(submission_id: str,
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("interop.read"))):
    return _submission_out(
        session, _get_submission(session, "clearinghouse", submission_id),
        detail=True)


# ---------------------------------------------------------------------------
# BD-40 — X12 270 build / 271 parse (payer EDI eligibility)
# ---------------------------------------------------------------------------

def _edi_date_parts(raw: str | None) -> tuple[str, str, str]:
    parsed = None
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    if parsed is None:
        parsed = _now()
    return parsed.strftime("%Y%m%d"), parsed.strftime("%y%m%d"), \
        parsed.strftime("%H%M")


def _control_number(seed: str) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 1_000_000_000


def build_270(patient: dict, payer_code: str, payer_display: str,
              service_date: str, trace: str | None = None) -> str:
    """Build an X12 270 (005010X279A1) eligibility inquiry from a
    Patient resource + payer identity. Segment terminators '~' +
    newline; the SE count reconciles by construction."""
    patient_id = str(patient.get("id") or "")
    trace = trace or f"EDI{uuid.uuid4().hex[:10].upper()}"
    ymd, ymd2, hm = _edi_date_parts(service_date)
    isa_ctrl = _control_number(f"270:{patient_id}:{trace}")
    name = (patient.get("name") or [{}])[0]
    family = (name.get("family") or "UNKNOWN").upper()
    given = ((name.get("given") or ["UNKNOWN"])[0]).upper()
    birth = patient.get("birthDate", "").replace("-", "") \
        if patient.get("birthDate") else ""
    gender = {"female": "F", "male": "M"}.get(
        str(patient.get("gender") or "").lower(), "U")
    receiver = (payer_code or "PAYER").upper()[:15]
    segments: list[str] = [
        f"ISA*00*          *00*          *ZZ*OPENCARE       *ZZ*{receiver:<15}"
        f"*{ymd2}*{hm}*^*00501*{isa_ctrl:09d}*0*T*:",
        f"GS*HS*OPENCARE*{receiver}*{ymd}*{hm}*1*X*005010X279A1",
        "ST*270*0001*005010X279A1",
        f"BHT*0022*13*{trace}*{ymd}*{hm}",
        "HL*1**20*1",
        f"NM1*PR*2*{payer_display or payer_code}*****PI*{payer_code}",
        "HL*2*1*21*1",
        "NM1*1P*2*OPENCARE CLINIC*****SV*OPENCARE-PROVIDER",
        "HL*3*2*22*0",
        f"TRN*1*{trace}*OPENCARE",
        f"NM1*IL*1*{family}*{given}****MI*{patient_id}",
    ]
    if birth:
        segments.append(f"DMG*D8*{birth}*{gender}")
    segments.append(f"DTP*291*D8*{ymd}")
    segments.append("EQ*30")
    se_count = len(segments) - 2 + 1
    segments.append(f"SE*{se_count}*0001")
    segments.append("GE*1*1")
    segments.append(f"IEA*1*{isa_ctrl:09d}")
    return "".join(f"{segment}~\n" for segment in segments)


def parse_271(text: str) -> dict:
    """Parse an X12 271 (health-care eligibility benefit response)
    into the Phase 7 eligibility fields. EB segments carry the
    benefit information: EB01 '1' = active coverage, '6' = inactive;
    a copay EB (EB01 'B') carries its amount in EB07; a coinsurance
    EB (EB01 'A') carries its percent (decimal) in EB08. AAA
    segments are request rejections (the inquiry itself refused).
    Returns the parsed fields + `rejected` for AAA."""
    result: dict = {"covered": False, "reason": "no-active-coverage",
                    "copay": None, "coinsurance_percent": None,
                    "payer_display": None, "rejected": False,
                    "messages": []}
    if not isinstance(text, str) or not text.strip():
        result["reason"] = "empty-271"
        return result
    segments = [[el.strip() for el in seg.split("*")]
                for seg in text.replace("\r", " ").split("~") if seg.strip()]
    saw_eb = False
    for seg in segments:
        if not seg:
            continue
        if seg[0] == "NM1" and len(seg) > 3 and seg[1] == "PR":
            result["payer_display"] = seg[3] or result["payer_display"]
        elif seg[0] == "AAA":
            result["rejected"] = True
            code = seg[3] if len(seg) > 3 else ""
            result["reason"] = f"request-rejected-{code or 'aaa'}"
        elif seg[0] == "EB":
            saw_eb = True
            code = seg[1] if len(seg) > 1 else ""
            if code == "1":
                result["covered"] = True
                result["reason"] = "active-coverage"
            elif code == "6":
                result["covered"] = False
                result["reason"] = "inactive-coverage"
            elif code == "B":
                try:
                    result["copay"] = round(float(seg[7]), 2)
                except (IndexError, ValueError):
                    pass
            elif code == "A":
                try:
                    result["coinsurance_percent"] = round(
                        float(seg[8]) * 100, 2)
                except (IndexError, ValueError):
                    pass
        elif seg[0] == "MSG" and len(seg) > 1 and seg[1]:
            result["messages"].append(seg[1])
    if not saw_eb and not result["rejected"]:
        result["reason"] = "no-benefit-segments"
    return result


@router.post("/eligibility/edi", status_code=201)
async def eligibility_edi(request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("access.work"))):
    """EDI eligibility: build the 270 from the patient + the
    published payer config, post it to the payer TEST endpoint from
    integration/edi, parse the 271, and RECORD the check in the
    Phase 7 eligibility table with basis "payer-edi". No endpoint
    configured (or a transport/HTTP failure) -> 503 and NOTHING is
    recorded: a payer-edi record exists only when a 271 was
    actually received. An AAA-rejected inquiry is a received 271,
    so it IS recorded (covered=false, reason request-rejected-*)."""
    payload = await _json_body(request)
    patient_raw = payload.get("patient_id") or payload.get("patientId") \
        or payload.get("patient")
    if not patient_raw:
        raise HTTPException(status_code=422,
                            detail="EDI eligibility requires 'patient_id'")
    patient_ref = fhir._normalize_patient_ref(str(patient_raw))
    patient_id = patient_ref.split("/", 1)[1]
    patient_row = fhir._get_row("Patient", patient_id, session)
    payer = payload.get("payer")
    if not isinstance(payer, str) or not payer:
        raise HTTPException(status_code=422,
                            detail="EDI eligibility requires 'payer'")
    concept = access_mod.payer_concept(session, payer)
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown payer '{payer}': not in the published "
                   "payers terminology")
    entry = _payer_edi_entry(session, payer)
    url = str((entry or {}).get("endpointUrl") or "")
    if not url:
        raise HTTPException(
            status_code=503,
            detail=(f"No payer EDI endpoint configured for payer "
                    f"'{payer}' in the published integration/{EDI_KEY} "
                    "config: no 270 was sent and no eligibility record "
                    "was created (production payer EDI onboarding is "
                    "NOT RUN)"))
    service_code = payload.get("service_code") or payload.get("serviceCode")
    service_date = str(payload.get("service_date")
                       or _now().date().isoformat())
    patient_body = fhir._render(patient_row)
    text_270 = build_270(patient_body, payer,
                         str(concept.get("display") or payer),
                         service_date)
    http_status, text_271, error = _post(url, text_270, "text/plain")
    if http_status is None or not (200 <= http_status < 300):
        detail_text = error or f"HTTP {http_status}"
        append_audit(session, actor=user.username,
                     action="gateway.eligibility.edi_failed",
                     entity=f"Patient/{patient_id}",
                     payload={"payer": payer, "error": detail_text,
                              "recorded": False})
        session.commit()
        raise HTTPException(
            status_code=503,
            detail=(f"Payer EDI endpoint for '{payer}' did not return "
                    f"a 271 ({detail_text}); no eligibility record "
                    "was created"))
    parsed = parse_271(text_271)
    result = {
        "patient": patient_ref, "payer": payer,
        "payer_display": parsed.get("payer_display")
        or concept.get("display"),
        "service_code": service_code, "service_date": service_date,
        "covered": bool(parsed["covered"]),
        "reason": parsed["reason"],
        "copay": parsed.get("copay"),
        "coinsurance_percent": parsed.get("coinsurance_percent"),
        "basis": EDI_BASIS, "disclaimer": EDI_DISCLAIMER,
        "checked_by": user.username, "checked_at": _now_iso(),
        "edi": {"trace": text_270.split("BHT*0022*13*")[1].split("*")[0]
                if "BHT*0022*13*" in text_270 else None,
                "endpoint": url, "messages": parsed.get("messages") or []},
    }
    row = EligibilityCheck(
        check_id=str(uuid.uuid4()), patient_ref=patient_ref,
        payer_code=payer, service_code=service_code,
        service_date=service_date, covered=result["covered"],
        copay=result["copay"],
        coinsurance_percent=result["coinsurance_percent"],
        detail=result, checked_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="gateway.eligibility.edi",
                 entity=f"EligibilityCheck/{row.check_id}",
                 payload={"patient": patient_ref, "payer": payer,
                          "service_code": service_code,
                          "covered": result["covered"],
                          "basis": EDI_BASIS})
    session.commit()
    return {"id": row.check_id, **result,
            "request_270": text_270, "response_271": text_271}


# ---------------------------------------------------------------------------
# BD-40 — Surescripts NCPDP SCRIPT NewRx
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def build_newrx_xml(patient: dict, medication: dict, prescriber: dict,
                    pharmacy: dict | None = None,
                    message_id: str | None = None) -> str:
    """Build an NCPDP SCRIPT NewRx document (Message/Header/Body/
    NewRx with Patient, Prescriber, Pharmacy and
    MedicationPrescribed) as XML text. Values are XML-escaped; the
    document is well-formed by construction (built as a string of
    escaped parts, then verified with ElementTree before return —
    a construction bug raises rather than emitting broken XML)."""
    message_id = message_id or f"NRX{uuid.uuid4().hex[:12].upper()}"
    sent_time = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
    name = (patient.get("name") or [{}])[0]
    given = (name.get("given") or [""])[0]

    def el(tag: str, value) -> str:
        return f"<{tag}>{xml_escape(str(value))}</{tag}>" \
            if value not in (None, "") else ""

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<Message xmlns="http://www.ncpdp.org/schema/SCRIPT">',
             "<Header>",
             el("To", "SURESCRIPTS"),
             el("From", "OPENCARE"),
             el("MessageID", message_id),
             el("SentTime", sent_time),
             "</Header>",
             "<Body><NewRx>",
             "<Patient>",
             el("LastName", name.get("family") or ""),
             el("FirstName", given),
             el("DateOfBirth", patient.get("birthDate") or ""),
             el("Gender", {"female": "F", "male": "M"}.get(
                 str(patient.get("gender") or "").lower(), "U")),
             "</Patient>",
             "<Prescriber>",
             el("LastName", prescriber.get("last_name") or ""),
             el("FirstName", prescriber.get("first_name") or ""),
             el("NPI", prescriber.get("npi") or ""),
             "</Prescriber>"]
    if pharmacy:
        parts += ["<Pharmacy>",
                  el("Name", pharmacy.get("name") or ""),
                  el("NCPDPID", pharmacy.get("ncpdp_id") or ""),
                  "</Pharmacy>"]
    quantity = medication.get("quantity")
    parts += [
        "<MedicationPrescribed>",
        el("DrugDescription", medication.get("description") or ""),
        el("ProductCode", medication.get("product_code") or ""),
        el("Quantity", quantity if quantity is not None else ""),
        el("DaysSupply", medication.get("days_supply")
           if medication.get("days_supply") is not None else ""),
        el("Directions", medication.get("directions") or ""),
        el("Substitutions", medication.get("substitutions")
           if medication.get("substitutions") is not None else "0"),
        el("Refills", medication.get("refills")
           if medication.get("refills") is not None else ""),
        "</MedicationPrescribed>",
        "</NewRx></Body></Message>"]
    text = "".join(parts)
    ET.fromstring(text)  # well-formedness proof; raises if broken
    return text


def parse_erx_response(text: str) -> dict:
    """Parse a SCRIPT response: a Status element with code 000 (or a
    description naming success) is an acceptance; an Error element
    is a rejection carrying its code + description. Anything else is
    `unparsed` — recorded verbatim, never guessed."""
    if not isinstance(text, str) or not text.strip():
        return {"accepted": None, "code": None, "description": "",
                "outcome": "unparsed"}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {"accepted": None, "code": None, "description": "",
                "outcome": "unparsed"}
    error = status = None
    for node in root.iter():
        if _local(node.tag) == "Error" and error is None:
            error = node
        if _local(node.tag) == "Status" and status is None:
            status = node

    def child_text(node, tag):
        for child in node.iter():
            if _local(child.tag) == tag:
                return (child.text or "").strip()
        return ""

    if error is not None:
        return {"accepted": False,
                "code": child_text(error, "Code") or None,
                "description": child_text(error, "Description"),
                "outcome": "rejected"}
    if status is not None:
        code = child_text(status, "Code")
        description = child_text(status, "Description")
        if code in ("000", "0") or "success" in description.lower():
            return {"accepted": True, "code": code or None,
                    "description": description, "outcome": "accepted"}
        return {"accepted": False, "code": code or None,
                "description": description, "outcome": "rejected"}
    return {"accepted": None, "code": None, "description": "",
            "outcome": "unparsed"}


@router.post("/erx/newrx", status_code=201)
async def send_newrx(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("orders.place"))):
    """Build + transmit one NewRx. Body: {patient_id (or patient),
    medication: {description, product_code, quantity, days_supply,
    directions, ...}, prescriber: {last_name, first_name, npi},
    pharmacy?: {...}}. With no Surescripts endpoint published the
    transmission is recorded `queued-for-gateway` (built, NOT sent);
    with one, the SCRIPT response is parsed into the recorded
    status (accepted / rejected / response-unparsed)."""
    payload = await _json_body(request)
    patient_raw = payload.get("patient_id") or payload.get("patientId")
    patient_body = payload.get("patient")
    patient_ref = None
    if patient_raw:
        patient_ref = fhir._normalize_patient_ref(str(patient_raw))
        row = fhir._get_row("Patient", patient_ref.split("/", 1)[1],
                            session)
        patient_body = fhir._render(row)
    if not isinstance(patient_body, dict):
        raise HTTPException(
            status_code=422,
            detail="NewRx requires 'patient_id' (or a 'patient' object)")
    medication = payload.get("medication")
    if not isinstance(medication, dict) or not (
            medication.get("description") or medication.get("product_code")):
        raise HTTPException(
            status_code=422,
            detail="NewRx requires 'medication.description' (or "
                   "'medication.product_code')")
    if medication.get("quantity") is None:
        raise HTTPException(status_code=422,
                            detail="NewRx requires 'medication.quantity'")
    prescriber = payload.get("prescriber")
    if not isinstance(prescriber, dict) or not prescriber.get("last_name"):
        raise HTTPException(
            status_code=422,
            detail="NewRx requires 'prescriber.last_name'")
    pharmacy = payload.get("pharmacy") if isinstance(
        payload.get("pharmacy"), dict) else None
    xml_text = build_newrx_xml(patient_body, medication, prescriber,
                               pharmacy)
    config = _edi_payload(session)
    url = str((config.get("surescripts") or {}).get("endpointUrl") or "")
    row = ErxTransmission(
        transmission_id=f"erx-{uuid.uuid4().hex[:16]}",
        patient_ref=patient_ref, endpoint_url=url, request_xml=xml_text,
        status="queued-for-gateway",
        detail={"not_sent_reason":
                "no Surescripts endpoint in the published "
                "integration/edi config — built and recorded locally, "
                "NOT sent (network certification NOT RUN)"},
        created_by=user.username)
    if url:
        http_status, text, error = _post(url, xml_text,
                                         "application/xml")
        if http_status is not None and 200 <= http_status < 300:
            parsed = parse_erx_response(text)
            row.response_payload = text
            row.status = {"accepted": "accepted",
                          "rejected": "rejected"}.get(
                              parsed["outcome"], "response-unparsed")
            row.detail = {"response": parsed}
        else:
            row.status = "send-failed"
            row.detail = {"error": error or f"HTTP {http_status}",
                          "response": text[:500]}
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="gateway.erx.newrx",
                 entity=f"erx-transmission/{row.transmission_id}",
                 payload={"patient": patient_ref, "status": row.status,
                          "endpoint_configured": bool(url),
                          "drug": medication.get("description")
                          or medication.get("product_code")})
    session.commit()
    return {"id": row.transmission_id, "status": row.status,
            "patient_ref": row.patient_ref, "endpoint_url": url or None,
            "detail": row.detail, "request_xml": row.request_xml,
            "disclaimer": ERX_DISCLAIMER}


@router.get("/erx/transmissions")
def list_erx(session: Session = Depends(get_db),
             user: User = Depends(require_capability("interop.read"))):
    rows = session.scalars(
        select(ErxTransmission).order_by(ErxTransmission.id.desc())).all()
    return {"items": [{"id": r.transmission_id, "status": r.status,
                       "patient_ref": r.patient_ref,
                       "endpoint_url": r.endpoint_url,
                       "detail": r.detail or {},
                       "created_by": r.created_by,
                       "created_at": r.created_at.isoformat()
                       if r.created_at else None} for r in rows],
            "total": len(rows)}
