"""Report engine — PLATFORM PASS Workstream 7 (app.report_engine).

Generalises the Phase 13 report catalogue (app.reports) WITHOUT
replacing it: Phase 13 definitions name one of five server-defined
REPORT_QUERIES; engine definitions name one of the server-defined
EXPORT DATASETS and compose it — fields (projection), filters and
grouping — as governed Studio configuration of kind
``report-definition``.

Safety (the Phase 13 / design §12 decision, unchanged):

- A definition names a dataset from ``app.reports.EXPORT_DATASETS``
  (the dataset registry; its builders are the ``_ds_*`` functions in
  app.reports — datasets are reachable only through those builders,
  so this module imports the registry and calls the builders).
- Fields / filters / grouping may only reference fields the dataset
  actually returns. The field catalogue per dataset is built from
  the builders' output keys (sampled live at validation time),
  unioned with the builders' declared output keys for the
  empty-database case (a dataset with zero rows still has a known
  shape — the keys its builder constructs).
- There is NO SQL path at all: a payload containing a ``sql`` or
  ``query`` key anywhere, an unknown dataset, or an unknown field is
  refused 422. Raw SQL is refused by construction, not by filtering.

Governance: definitions live in the shared StudioConfig store under
kind ``report-definition`` with in-module draft -> publish governance
mirroring app.studio_config (drafts are designer data gated by
``studio.manage``; published versions are immutable; publishing
supersedes). NOTE (pending integration): the generic Studio API
(app.studio_config / app.configstore CONFIG_KINDS) does not yet know
this kind — registering ``report-definition`` there is a pending
integration edit; until then governance runs through THIS module's
endpoints, which enforce the identical lifecycle and audit actions.

Capabilities (existing only): runs and run-record reads ride
``reports.run``; definition governance rides ``studio.manage``;
definition reads ride ``studio.read``. (Distribution in v1 writes
run-record artefacts only, so no ``data.export`` capability is
consumed — a run that emails/files data would need it, and no such
distribution type exists here.)

Scheduling (honest): a definition may carry ``schedule: {cron}``.
NO daemon runs in this build. ``POST /report-engine/run-due`` is
invoked by an operator or an external cron hitting the endpoint —
the same honesty pattern as Phase 14 pipelines. A definition is due
when its cron matches the current minute AND no scheduler run for
that definition is already recorded for that minute.
Cron subset (5 fields: minute hour day-of-month month day-of-week):
``*``, ``*/n``, single values, comma lists, and ranges ``a-b``
(range-with-step and names are NOT supported).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import DateTime, Integer, JSON, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import reports as reports_module
from .audit import append_audit
from .configstore import published_config, role_allowed
from .db import Base
from .models import ConfigStatus, StudioConfig, User
from .security import get_db, require_capability

router = APIRouter(prefix="/report-engine", tags=["report-engine"])

KIND = "report-definition"
FILTER_OPS = ("eq", "ne", "in", "gte", "lte", "contains")
FORMATS = ("json", "pdf", "csv")
PDF_ROW_CAP = 100

# Declared output keys per dataset — exactly the keys the _ds_*
# builders in app.reports construct. Used as the catalogue floor;
# live builder output is unioned in at validation/run time.
_DECLARED_FIELDS: dict[str, tuple[str, ...]] = {
    "patients": ("id", "name", "gender", "birth_date"),
    "appointments": ("id", "patient", "status", "start", "end", "type"),
    "orders": ("id", "type", "patient", "status", "code", "authored_on"),
    "lab-results": ("id", "patient", "code", "status", "issued"),
    "billing-lines": ("claim_id", "patient", "sequence", "code",
                      "display", "amount"),
}


class ReportRun(Base):
    """One recorded engine run (the v1 distribution artefact)."""
    __tablename__ = "report_engine_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    definition_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                                index=True)
    definition_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    format: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="json")
    artefact_summary: Mapped[dict] = mapped_column(JSON, nullable=False,
                                                   default=dict)
    ran_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Dataset catalogue
# ---------------------------------------------------------------------------

def dataset_fields(session: Session | None, dataset: str) -> set[str]:
    """Field catalogue for one dataset: the builder's declared keys
    unioned with keys sampled from its live output."""
    fields = set(_DECLARED_FIELDS.get(dataset, ()))
    if session is not None and dataset in reports_module.EXPORT_DATASETS:
        _title, _desc, builder = reports_module.EXPORT_DATASETS[dataset]
        for row in builder(session):
            fields.update(row.keys())
    return fields


def _dataset_rows(session: Session, dataset: str) -> list[dict]:
    _title, _desc, builder = reports_module.EXPORT_DATASETS[dataset]
    return builder(session)


# ---------------------------------------------------------------------------
# Cron matcher (documented subset)
# ---------------------------------------------------------------------------

def _field_matches(spec: str, value: int, lo: int, hi: int) -> bool:
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty cron field part in '{spec}'")
        if part == "*":
            return True
        if part.startswith("*/"):
            step = int(part[2:])
            if step <= 0:
                raise ValueError("cron step must be positive")
            if (value - lo) % step == 0:
                return True
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
            if start < lo or end > hi or start > end:
                raise ValueError(f"cron range '{part}' out of bounds")
            if start <= value <= end:
                return True
            continue
        single = int(part)
        if single < lo or single > hi:
            raise ValueError(f"cron value '{part}' out of bounds")
        if single == value:
            return True
    return False


def cron_matches(expr: str, moment: datetime) -> bool:
    """True when the 5-field cron `expr` matches `moment` (minute
    precision). Day-of-week: 0 and 7 are Sunday. Raises ValueError
    on a malformed expression."""
    fields = (expr or "").split()
    if len(fields) != 5:
        raise ValueError("cron expression must have exactly 5 fields")
    minute, hour, dom, month, dow = fields
    cron_dow = (moment.weekday() + 1) % 7  # Mon=1 .. Sat=6, Sun=0
    dow_value = moment.isoweekday() % 7
    # Accept both conventions via the computed Sunday=0 value; a
    # literal 7 also means Sunday and is normalised in the dow field.
    dow_spec = ",".join("0" if p.strip() == "7" else p
                        for p in dow.split(","))
    return (
        _field_matches(minute, moment.minute, 0, 59)
        and _field_matches(hour, moment.hour, 0, 23)
        and _field_matches(dom, moment.day, 1, 31)
        and _field_matches(month, moment.month, 1, 12)
        and _field_matches(dow_spec, dow_value, 0, 6)
    )


def _validate_cron(expr) -> str:
    if not isinstance(expr, str) or not expr.strip():
        raise HTTPException(status_code=422,
                            detail="'schedule.cron' must be a cron string")
    try:
        # Probe with a fixed moment: malformed fields raise.
        cron_matches(expr, datetime(2026, 1, 4, 0, 0, tzinfo=timezone.utc))
    except ValueError as exc:
        raise HTTPException(status_code=422,
                            detail=f"Invalid cron expression: {exc}")
    return expr


# ---------------------------------------------------------------------------
# Definition validation
# ---------------------------------------------------------------------------

def _scan_forbidden(payload, path: str = "") -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in ("sql", "query"):
                raise HTTPException(
                    status_code=422,
                    detail=(f"'{key}' is forbidden in a report definition "
                            "(reports name server-defined datasets; there "
                            "is no SQL path)"))
            _scan_forbidden(value, f"{path}.{key}")
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            _scan_forbidden(item, f"{path}[{i}]")


def validate_report_definition(payload, session: Session | None = None) -> dict:
    """Validate one report-definition payload; returns it normalised
    (defaults applied). Raises HTTPException 422 on any problem."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422,
                            detail="Report definition must be a JSON object")
    _scan_forbidden(payload)
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise HTTPException(status_code=422,
                            detail="'title' is required (non-empty string)")
    dataset = payload.get("dataset")
    if dataset not in reports_module.EXPORT_DATASETS:
        raise HTTPException(
            status_code=422,
            detail=(f"Unknown dataset '{dataset}'; defined datasets: "
                    f"{sorted(reports_module.EXPORT_DATASETS)} "
                    "(server-defined datasets only — no ad-hoc queries)"))
    catalogue = dataset_fields(session, dataset)
    fields = payload.get("fields")
    if not isinstance(fields, list) or not fields or any(
            not isinstance(f, str) or not f for f in fields):
        raise HTTPException(status_code=422,
                            detail="'fields' must be a non-empty list of "
                                   "field names")
    if len(set(fields)) != len(fields):
        raise HTTPException(status_code=422, detail="'fields' must be unique")
    for f in fields:
        if f not in catalogue:
            raise HTTPException(
                status_code=422,
                detail=(f"Unknown field '{f}' for dataset '{dataset}'; "
                        f"fields: {sorted(catalogue)}"))
    filters = payload.get("filters") or []
    if not isinstance(filters, list):
        raise HTTPException(status_code=422, detail="'filters' must be a list")
    for i, flt in enumerate(filters):
        if not isinstance(flt, dict):
            raise HTTPException(status_code=422,
                                detail=f"filters[{i}] must be an object")
        if flt.get("field") not in catalogue:
            raise HTTPException(
                status_code=422,
                detail=f"filters[{i}].field '{flt.get('field')}' is not a "
                       f"field of dataset '{dataset}'")
        if flt.get("op") not in FILTER_OPS:
            raise HTTPException(
                status_code=422,
                detail=f"filters[{i}].op must be one of {list(FILTER_OPS)}")
        if "value" not in flt:
            raise HTTPException(status_code=422,
                                detail=f"filters[{i}].value is required")
        if flt["op"] == "in" and not isinstance(flt["value"], list):
            raise HTTPException(
                status_code=422,
                detail=f"filters[{i}].value must be a list for op 'in'")
    grouping = payload.get("grouping") or []
    if not isinstance(grouping, list) or any(
            not isinstance(g, str) or g not in catalogue for g in grouping):
        raise HTTPException(
            status_code=422,
            detail="'grouping' must be a list of field names of the dataset")
    if len(set(grouping)) != len(grouping):
        raise HTTPException(status_code=422,
                            detail="'grouping' must be unique")
    formats = payload.get("formats") or list(FORMATS)
    if not isinstance(formats, list) or not formats or any(
            f not in FORMATS for f in formats):
        raise HTTPException(
            status_code=422,
            detail=f"'formats' must be a non-empty list drawn from "
                   f"{list(FORMATS)}")
    schedule = payload.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, dict):
            raise HTTPException(status_code=422,
                                detail="'schedule' must be {cron} or null")
        _validate_cron(schedule.get("cron"))
    distribution = payload.get("distribution") or []
    if not isinstance(distribution, list):
        raise HTTPException(status_code=422,
                            detail="'distribution' must be a list")
    for i, dist in enumerate(distribution):
        if not isinstance(dist, dict) or dist.get("type") != "run-record":
            raise HTTPException(
                status_code=422,
                detail=(f"distribution[{i}].type must be 'run-record' "
                        "(v1 distribution = recorded run artefacts only)"))
    return {**payload, "fields": list(fields), "filters": filters,
            "grouping": grouping, "formats": list(formats),
            "schedule": schedule, "distribution": distribution}


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
    payload = validate_report_definition(await _json_body(request), session)
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
                 action="report-engine.definition.create",
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
    row.payload = validate_report_definition(await _json_body(request),
                                               session)
    row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username,
                 action="report-engine.definition.update",
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
                 action="report-engine.definition.publish",
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
    return {"key": key, "versions": [_out(r) for r in rows],
            "published": (_out(_published(session, key))
                          if _published(session, key) else None)}


@router.get("/datasets")
def list_datasets(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("reports.run"))):
    items = [{"key": key, "title": title, "description": desc,
              "fields": sorted(dataset_fields(session, key))}
             for key, (title, desc, _fn)
             in sorted(reports_module.EXPORT_DATASETS.items())]
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Phase 13 re-expression mapping
# ---------------------------------------------------------------------------

def phase13_mapping() -> list[dict]:
    """Per-query verdict: can each Phase 13 REPORT_QUERIES report be
    faithfully re-expressed as a dataset+fields+filters+grouping
    engine definition over an existing EXPORT_DATASETS dataset?

    Verdict (all five NOT faithfully expressible — see reasons; the
    engine generalises the catalogue for dataset-shaped reports,
    it does not silently approximate aggregate queries):
    """
    return [
        {"query": "census-by-unit",
         "title": "Census by unit",
         "expressible": False,
         "engine_definition": None,
         "reason": ("Aggregates live bed state per unit (occupied / "
                    "available / cleaning / blocked counts and an "
                    "occupancy percentage computed from Location/bed "
                    "state). No export dataset carries Location or bed "
                    "rows, and grouping counts rows — it cannot derive "
                    "per-status bed buckets or the occupancy ratio.")},
        {"query": "claims-summary",
         "title": "Claims summary by status",
         "expressible": False,
         "engine_definition": None,
         "reason": ("Groups claims by lifecycle status and SUMS "
                    "remittance financials (billed / paid / adjustments / "
                    "patient responsibility / balance) via "
                    "revenue.claim_financials. The billing-lines dataset "
                    "carries only per-line net amounts — no claim status "
                    "and no remittance fields — and engine grouping "
                    "counts rows, it does not sum money.")},
        {"query": "credential-expiry",
         "title": "Credential expiry",
         "expressible": False,
         "engine_definition": None,
         "reason": ("Computes days_to_expiry relative to today and a "
                    "status from the published workforce worklist "
                    "window, over StaffCredential rows. No export "
                    "dataset carries credentials, and the time-derived "
                    "expiry fields exist in no dataset.")},
        {"query": "safety-events",
         "title": "Safety events",
         "expressible": False,
         "engine_definition": None,
         "reason": ("Reads SafetyEvent rows (category, harm level, "
                    "RCA flag, open/closed filter). No export dataset "
                    "carries safety events.")},
        {"query": "payments-summary",
         "title": "Patient payments by method",
         "expressible": False,
         "engine_definition": None,
         "reason": ("Groups PatientPayment rows by method and SUMS "
                    "amounts. No export dataset carries payments "
                    "(billing-lines are claim charge lines, not "
                    "payments), and engine grouping counts, not sums.")},
    ]


@router.get("/phase13-mapping")
def get_phase13_mapping(session: Session = Depends(get_db),
                        user: User = Depends(require_capability("studio.read"))):
    items = phase13_mapping()
    return {"items": items, "total": len(items),
            "expressible": sum(1 for i in items if i["expressible"])}


# ---------------------------------------------------------------------------
# Run execution
# ---------------------------------------------------------------------------

def _compare(row_value, op: str, value) -> bool:
    if op == "eq":
        return row_value == value
    if op == "ne":
        return row_value != value
    if op == "in":
        return row_value in value
    if op == "contains":
        return row_value is not None and str(value) in str(row_value)
    # gte / lte: numeric when both sides are numbers, else lexical.
    if row_value is None:
        return False
    try:
        left, right = float(row_value), float(value)
    except (TypeError, ValueError):
        left, right = str(row_value), str(value)
    return left >= right if op == "gte" else left <= right


def _apply_filters(rows: list[dict], filters: list[dict]) -> list[dict]:
    out = rows
    for flt in filters:
        out = [r for r in out
               if _compare(r.get(flt["field"]), flt["op"], flt["value"])]
    return out


def _group_rows(rows: list[dict], grouping: list[str]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    order: list[tuple] = []
    for row in rows:
        key = tuple(row.get(g) for g in grouping)
        if key not in groups:
            groups[key] = {"key": {g: row.get(g) for g in grouping},
                           "count": 0, "rows": []}
            order.append(key)
        groups[key]["count"] += 1
        groups[key]["rows"].append(row)
    return [groups[k] for k in order]


def _run_out(row: ReportRun) -> dict:
    return {"id": row.id, "definition_key": row.definition_key,
            "definition_version": row.definition_version,
            "actor": row.actor, "row_count": row.row_count,
            "format": row.format,
            "artefact_summary": row.artefact_summary or {},
            "ran_at": row.ran_at.isoformat() if row.ran_at else None}


def execute_run(session: Session, key: str, cfg: StudioConfig, *,
                actor: str, fmt: str = "json", params: dict | None = None,
                ran_at: datetime | None = None):
    """Execute one published definition over live data, record the
    ReportRun + audit, and return (run_row, payload) where payload
    carries the projected rows/groups for rendering. `params.as_of`
    is recorded on the artefact only: the datasets are current-state
    builders, so as_of does NOT time-travel — stated, not implied."""
    definition = cfg.payload or {}
    params = params or {}
    for name in params:
        if name != "as_of":
            raise HTTPException(
                status_code=422,
                detail=f"Unknown parameter '{name}'; engine runs accept "
                       "only 'as_of'")
    formats = definition.get("formats") or list(FORMATS)
    if fmt not in formats:
        raise HTTPException(
            status_code=422,
            detail=f"Format '{fmt}' is not allowed for this definition "
                   f"(allowed: {formats})")
    rows = _dataset_rows(session, definition["dataset"])
    rows = _apply_filters(rows, definition.get("filters") or [])
    fields = list(definition["fields"])
    projected = [{f: row.get(f) for f in fields} for row in rows]
    grouping = definition.get("grouping") or []
    groups = _group_rows(projected, grouping) if grouping else None
    run = ReportRun(definition_key=key, definition_version=cfg.version,
                    actor=actor, row_count=len(projected), format=fmt,
                    artefact_summary={
                        "dataset": definition["dataset"],
                        "fields": fields,
                        "filters": definition.get("filters") or [],
                        "grouping": grouping,
                        "groups": len(groups) if groups is not None else None,
                        "params": params,
                        "distribution": definition.get("distribution") or []},
                    ran_at=ran_at or _now())
    session.add(run)
    session.flush()
    append_audit(session, actor=actor, action="report-engine.run",
                 entity=f"report-definition:{key}@v{cfg.version}",
                 payload={"dataset": definition["dataset"], "format": fmt,
                          "rows": len(projected), "run_id": run.id,
                          "params": params})
    session.commit()
    payload = {"key": key, "version": cfg.version,
               "title": definition.get("title"), "dataset": definition["dataset"],
               "format": fmt, "row_count": len(projected), "fields": fields,
               "rows": projected, "groups": groups,
               "run": _run_out(run)}
    return run, payload


@router.post("/{key}/run")
async def run_definition(key: str, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("reports.run"))):
    cfg = _published(session, key)
    if cfg is None:
        raise HTTPException(
            status_code=404,
            detail=f"No published report definition '{key}'")
    raw = await request.body()
    body: dict = {}
    if raw.strip():
        try:
            import json as _json
            body = _json.loads(raw)
        except Exception:
            raise HTTPException(status_code=422,
                                detail="Request body must be valid JSON")
        if not isinstance(body, dict):
            raise HTTPException(status_code=422,
                                detail="Request body must be a JSON object")
    fmt = body.get("format") or "json"
    _run, payload = execute_run(session, key, cfg, actor=user.username,
                                fmt=fmt, params=body.get("params") or {})
    definition = cfg.payload or {}
    if fmt == "json":
        return payload
    if fmt == "csv":
        columns = [{"field": f, "title": f} for f in payload["fields"]]
        return PlainTextResponse(
            reports_module._csv_text(columns, payload["rows"]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{key}.csv"',
                     "X-Report-Rows": str(payload["row_count"])})
    # pdf: reuse the Phase 13 renderer + published print template.
    template = reports_module._print_template(session)
    fields = payload["fields"]
    capped = payload["rows"][:PDF_ROW_CAP]
    table_rows = [[reports_module._fmt(r.get(f)) for f in fields]
                  for r in capped]
    notes = [f"Rows: {payload['row_count']} (dataset: {payload['dataset']})."]
    if len(payload["rows"]) > PDF_ROW_CAP:
        notes.append(f"PDF rendering is capped at {PDF_ROW_CAP} rows; "
                     f"{len(payload['rows']) - PDF_ROW_CAP} further rows "
                     "are in the recorded run artefact / JSON / CSV.")
    content = reports_module.build_pdf(
        template, definition.get("title") or key,
        [("Generated", _now().isoformat()), ("Generated by", user.username),
         ("Dataset", payload["dataset"]), ("Rows", str(payload["row_count"]))],
        fields, table_rows, notes)
    return Response(content=content, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="{key}.pdf"'})


@router.get("/runs")
def list_runs(definition_key: str | None = Query(default=None),
              limit: int = Query(default=50),
              session: Session = Depends(get_db),
              user: User = Depends(require_capability("reports.run"))):
    stmt = select(ReportRun).order_by(ReportRun.id.desc())
    if definition_key:
        stmt = stmt.where(ReportRun.definition_key == definition_key)
    rows = session.scalars(stmt.limit(min(limit, 200))).all()
    return {"items": [_run_out(r) for r in rows], "total": len(rows)}


@router.post("/run-due")
async def run_due(request: Request, session: Session = Depends(get_db),
                  user: User = Depends(require_capability("reports.run"))):
    """Execute every published definition whose cron schedule is due
    at `now` (body {now?: ISO datetime}, default: current UTC time).
    Due = cron matches that minute AND no scheduler run for the
    definition is recorded in that minute. No daemon calls this —
    an operator or an external cron does (see module docstring)."""
    raw = await request.body()
    body: dict = {}
    if raw.strip():
        try:
            import json as _json
            body = _json.loads(raw)
        except Exception:
            raise HTTPException(status_code=422,
                                detail="Request body must be valid JSON")
        if not isinstance(body, dict):
            raise HTTPException(status_code=422,
                                detail="Request body must be a JSON object")
    now_raw = body.get("now")
    if now_raw:
        try:
            now = datetime.fromisoformat(str(now_raw).replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422,
                                detail="'now' must be an ISO datetime")
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
    else:
        now = _now()
    executed = []
    for cfg in session.scalars(
            select(StudioConfig).where(StudioConfig.kind == KIND,
                                       StudioConfig.status == ConfigStatus.published)
            .order_by(StudioConfig.key.asc())).all():
        definition = cfg.payload or {}
        schedule = definition.get("schedule")
        if not isinstance(schedule, dict) or not schedule.get("cron"):
            continue
        try:
            if not cron_matches(schedule["cron"], now):
                continue
        except ValueError:
            continue  # a malformed stored cron never fires (validated at write)
        already = False
        for run in session.scalars(
                select(ReportRun).where(
                    ReportRun.definition_key == cfg.key,
                    ReportRun.actor == "scheduler")).all():
            ran = run.ran_at
            if ran is None:
                continue
            if ran.tzinfo is None:
                ran = ran.replace(tzinfo=timezone.utc)
            if ran.astimezone(timezone.utc).replace(second=0, microsecond=0) \
                    == now.astimezone(timezone.utc).replace(second=0, microsecond=0):
                already = True
                break
        if already:
            continue
        run, _payload = execute_run(session, cfg.key, cfg, actor="scheduler",
                                    fmt="json", params={}, ran_at=now)
        executed.append({"key": cfg.key, "run_id": run.id,
                         "row_count": run.row_count})
    return {"now": now.isoformat(), "executed": executed,
            "total": len(executed)}
