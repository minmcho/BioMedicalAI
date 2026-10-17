"""Population health + warehouse operations API (Phase 3).

Two routers, both staff-only and AGGREGATE-ONLY:

- /pophealth (admin, clinician): population registries whose definitions
  are Studio configuration (kind='cohort', draft -> publish lifecycle in
  app.studio_config). Registry summaries and the cohort explorer are
  computed FROM THE OMOP WAREHOUSE (app.warehouse), never from the
  clinical store, and never return patient-level rows. Small-cell
  suppression: any stratum count below the cohort's smallCellThreshold
  (default 5) is returned as the string "<5" with suppressed=true — the
  raw number never leaves the server. Headline registry metrics (member
  count, % with latest lab in range) are reported as specified; the raw
  in-range numerator is not exposed.

- /warehouse (admin, clinician; the ETL trigger is admin-only): outbox
  tallies, dead-letter detail, warehouse table counts, and the
  mapping-coverage statistic (unmapped concept rows counted, not hidden).
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import etl, warehouse as wh
from .audit import append_audit
from .configstore import published_config, published_configs
from .models import ConfigStatus, OutboxEvent, StudioConfig, User
from .outbox import outbox_counts
from .security import get_db, require_capability

router = APIRouter(prefix="/pophealth", tags=["pophealth"])
warehouse_router = APIRouter(prefix="/warehouse", tags=["warehouse"])

DEFAULT_SMALL_CELL = 5
# Fallback age bands for cohort payloads that do not carry `ageBands`
# (the seeded registries below carry them explicitly; a cohort's bands
# are its own configuration).
AGE_BANDS = ((0, 17, "0-17"), (18, 44, "18-44"), (45, 64, "45-64"),
             (65, 74, "65-74"), (75, None, "75+"))
_AGE_BANDS_PAYLOAD = [[low, high, label] for low, high, label in AGE_BANDS]

DIABETES_COHORT = {
    "name": "Diabetes registry",
    "description": "Persons with Type 2 diabetes mellitus; latest HbA1c "
                   "in range when < 7.0% within 365 days.",
    "conditionCodes": [{"system": "http://snomed.info/sct", "code": "44054006"}],
    "lab": {"system": "http://loinc.org", "code": "4548-4",
            "comparator": "<", "threshold": 7.0, "windowDays": 365},
    "smallCellThreshold": 5,
    "ageBands": _AGE_BANDS_PAYLOAD,
}

HYPERTENSION_COHORT = {
    "name": "Hypertension registry",
    "description": "Persons with a hypertensive disorder diagnosis.",
    "conditionCodes": [{"system": "http://snomed.info/sct", "code": "38341003"}],
    "smallCellThreshold": 5,
    "ageBands": _AGE_BANDS_PAYLOAD,
}


def _age_bands(definition: dict) -> list:
    """(low, high, label) bands from the cohort payload's `ageBands`
    ([low, high|null, label] triples); the built-in bands when the
    payload carries none (or carries malformed ones)."""
    raw = definition.get("ageBands")
    bands = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, (list, tuple)) and len(entry) == 3:
                low, high, label = entry
                if isinstance(low, int) and not isinstance(low, bool) \
                        and (high is None or (isinstance(high, int)
                                              and not isinstance(high, bool))) \
                        and isinstance(label, str) and label:
                    bands.append((low, high, label))
    return bands or list(AGE_BANDS)


def ensure_pophealth_defaults(session: Session) -> None:
    """Seed the two built-in registries as published cohort config v1
    (idempotent), exactly like the lab catalog / ADT workflow seeds."""
    for key, payload in (("diabetes", DIABETES_COHORT),
                         ("hypertension", HYPERTENSION_COHORT)):
        existing = session.scalars(
            select(StudioConfig).where(StudioConfig.kind == "cohort",
                                       StudioConfig.key == key)).first()
        if existing is not None:
            continue
        session.add(StudioConfig(kind="cohort", key=key, version=1,
                                 status=ConfigStatus.published, payload=payload,
                                 created_by="system", updated_by="system"))
        session.flush()
        for action in ("studio.config.create", "studio.config.publish"):
            append_audit(session, actor="system", action=action,
                         entity=f"config:cohort/{key}@v1",
                         payload={"kind": "cohort", "key": key, "version": 1,
                                  "seeded_default": True})
    session.commit()


# ---------------------------------------------------------------------------
# Cohort computation over the warehouse
# ---------------------------------------------------------------------------

def _members(wh_session: Session, definition: dict) -> list[wh.Person]:
    """Registry members: persons matching ANY condition code; when the
    cohort is lab-only, persons with at least one such measurement."""
    conditions = definition.get("conditionCodes") or []
    person_ids: set[int] = set()
    if conditions:
        codes = [c["code"] for c in conditions if isinstance(c, dict) and c.get("code")]
        if codes:
            person_ids = set(wh_session.scalars(
                select(wh.ConditionOccurrence.person_id)
                .where(wh.ConditionOccurrence.condition_source_value.in_(codes))
                .distinct()).all())
    elif definition.get("lab"):
        code = definition["lab"]["code"]
        person_ids = set(wh_session.scalars(
            select(wh.Measurement.person_id)
            .where(wh.Measurement.measurement_source_value == code)
            .distinct()).all())
    if not person_ids:
        return []
    return list(wh_session.scalars(
        select(wh.Person).where(wh.Person.person_id.in_(person_ids))
        .order_by(wh.Person.person_id.asc())).all())


def _latest_measurements(wh_session: Session, code: str,
                         person_ids: list[int]) -> dict[int, wh.Measurement]:
    """Latest measurement of `code` per person (by datetime, then id)."""
    if not person_ids:
        return {}
    rows = wh_session.scalars(
        select(wh.Measurement)
        .where(wh.Measurement.measurement_source_value == code,
               wh.Measurement.person_id.in_(person_ids))
        .order_by(wh.Measurement.measurement_datetime.asc().nullsfirst(),
                  wh.Measurement.measurement_id.asc())).all()
    latest: dict[int, wh.Measurement] = {}
    for row in rows:  # ascending order: the last write per person wins
        latest[row.person_id] = row
    return latest


def _compare(value: float, comparator: str, threshold: float) -> bool:
    if comparator == "<":
        return value < threshold
    if comparator == "<=":
        return value <= threshold
    if comparator == ">":
        return value > threshold
    if comparator == ">=":
        return value >= threshold
    return value == threshold  # "=="


def _cell(count: int, threshold: int) -> dict:
    if count < threshold:
        return {"count": "<5" if threshold == 5 else f"<{threshold}",
                "suppressed": True}
    return {"count": count, "suppressed": False}


def _reference_date(wh_session: Session) -> date:
    """Data-relative 'today': the latest activity date in the warehouse
    (deterministic for a given dataset), falling back to the real today
    for an empty warehouse."""
    latest = wh_session.scalar(select(func.max(wh.Measurement.measurement_datetime)))
    if latest is None:
        latest = wh_session.scalar(select(func.max(wh.VisitOccurrence.visit_start_datetime)))
    return latest.date() if latest is not None else date.today()


def registry_summary(wh_session: Session, key: str, definition: dict) -> dict:
    threshold = definition.get("smallCellThreshold") or DEFAULT_SMALL_CELL
    members = _members(wh_session, definition)
    person_ids = [p.person_id for p in members]
    as_of = _reference_date(wh_session)

    lab_block = None
    lab = definition.get("lab")
    if lab:
        latest = _latest_measurements(wh_session, lab["code"], person_ids)
        window = lab.get("windowDays")
        cutoff = None
        if window:
            all_latest = _latest_measurements(
                wh_session, lab["code"],
                list(wh_session.scalars(select(wh.Measurement.person_id).distinct()).all()))
            dated = [m.measurement_datetime for m in all_latest.values()
                     if m.measurement_datetime is not None]
            if dated:
                cutoff = max(dated) - timedelta(days=window)
        with_result = 0
        in_range = 0
        for pid in person_ids:
            m = latest.get(pid)
            if m is None or m.value_as_number is None:
                continue
            if cutoff is not None and (m.measurement_datetime is None
                                       or m.measurement_datetime < cutoff):
                continue
            with_result += 1
            if _compare(m.value_as_number, lab["comparator"], float(lab["threshold"])):
                in_range += 1
        lab_block = {
            "code": lab["code"],
            "comparator": lab["comparator"],
            "threshold": lab["threshold"],
            "window_days": window,
            "members_with_current_result": with_result,
            # The raw in-range numerator is deliberately NOT exposed:
            # with small registries it would undo stratum suppression.
            "pct_in_range": (round(100.0 * in_range / with_result, 1)
                             if with_result else None),
        }

    age_bands = _age_bands(definition)
    by_gender: dict[str, int] = {}
    by_age: dict[str, int] = {label: 0 for _, _, label in age_bands}
    for p in members:
        gender = p.gender_source_value or "unknown"
        by_gender[gender] = by_gender.get(gender, 0) + 1
        if p.year_of_birth is not None:
            age = as_of.year - p.year_of_birth
            for low, high, label in age_bands:
                if age >= low and (high is None or age <= high):
                    by_age[label] += 1
                    break
    stratification = {
        "by_gender": [{"value": g, **_cell(n, threshold)}
                      for g, n in sorted(by_gender.items())],
        "by_age_band": [{"value": label, **_cell(n, threshold)}
                        for label, n in by_age.items() if n > 0],
        "as_of": as_of.isoformat(),
    }
    return {
        "registry": key,
        "name": definition.get("name"),
        "member_count": len(members),
        "lab": lab_block,
        "stratification": stratification,
        "small_cell_threshold": threshold,
    }


# ---------------------------------------------------------------------------
# /pophealth routes
# ---------------------------------------------------------------------------

@router.get("/registries")
def list_registries(session: Session = Depends(get_db),
                    user: User = Depends(require_capability("pophealth.view"))):
    """Published cohort (registry) definitions from Studio config."""
    return [{"key": cfg.key, "name": (cfg.payload or {}).get("name"),
             "definition": cfg.payload}
            for cfg in published_configs(session, "cohort")]


@router.get("/registries/{key}/summary")
def get_registry_summary(key: str, session: Session = Depends(get_db),
                         user: User = Depends(require_capability("pophealth.view"))):
    cfg = published_config(session, "cohort", key)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"No published cohort config '{key}'")
    factory = wh.warehouse_session_factory()
    with factory() as wh_session:
        wh.ensure_concept_map(wh_session)
        return registry_summary(wh_session, key, cfg.payload or {})


@router.post("/explore")
async def explore(request: Request, session: Session = Depends(get_db),
                  user: User = Depends(require_capability("pophealth.view"))):
    """Cohort explorer: persons matching ALL supplied filters (condition
    code and/or latest-lab rule). Aggregate count only — with small-cell
    suppression applied to the count itself, since an explorer result IS
    a stratum."""
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Explorer body must be a JSON object")
    condition_code = body.get("conditionCode")
    lab_code = body.get("labCode")
    if not condition_code and not lab_code:
        raise HTTPException(
            status_code=422,
            detail="Explorer requires at least one of 'conditionCode' or 'labCode'")
    comparator = body.get("comparator", "<")
    threshold = body.get("threshold")
    if lab_code:
        if comparator not in ("<", "<=", ">", ">=", "=="):
            raise HTTPException(status_code=422,
                                detail=f"Unknown comparator '{comparator}'")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise HTTPException(status_code=422,
                                detail="Explorer 'threshold' must be a number when 'labCode' is given")
    cell_threshold = body.get("smallCellThreshold") or DEFAULT_SMALL_CELL
    if isinstance(cell_threshold, bool) or not isinstance(cell_threshold, int) \
            or cell_threshold < 1:
        raise HTTPException(status_code=422, detail="'smallCellThreshold' must be a positive integer")

    factory = wh.warehouse_session_factory()
    with factory() as wh_session:
        wh.ensure_concept_map(wh_session)
        person_ids: set[int] | None = None
        if condition_code:
            person_ids = set(wh_session.scalars(
                select(wh.ConditionOccurrence.person_id)
                .where(wh.ConditionOccurrence.condition_source_value == str(condition_code))
                .distinct()).all())
        if lab_code:
            all_ids = list(wh_session.scalars(
                select(wh.Measurement.person_id).distinct()).all())
            latest = _latest_measurements(wh_session, str(lab_code), all_ids)
            matched = {pid for pid, m in latest.items()
                       if m.value_as_number is not None
                       and _compare(m.value_as_number, comparator, float(threshold))}
            person_ids = matched if person_ids is None else person_ids & matched
        count = len(person_ids or set())
    return {"filters": {"conditionCode": condition_code, "labCode": lab_code,
                        "comparator": comparator if lab_code else None,
                        "threshold": threshold if lab_code else None},
            **_cell(count, cell_threshold)}


# ---------------------------------------------------------------------------
# /warehouse routes
# ---------------------------------------------------------------------------

@warehouse_router.get("/status")
def warehouse_status(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("pophealth.view"))):
    factory = wh.warehouse_session_factory()
    with factory() as wh_session:
        wh.ensure_concept_map(wh_session)
        tables = wh.table_counts(wh_session)
        coverage = wh.mapping_coverage(wh_session)
    factory_clin = request.app.state.session_factory
    with factory_clin() as clin:
        counts = outbox_counts(clin)
        dead = clin.scalars(
            select(OutboxEvent).where(OutboxEvent.status == "dead_letter")
            .order_by(OutboxEvent.id.asc()).limit(20)).all()
        dead_letters = [{"event_id": e.id, "resource": f"{e.resource_type}/{e.resource_id}",
                         "version": e.version, "attempts": e.attempts,
                         "error": e.error} for e in dead]
    return {"outbox": counts, "dead_letters": dead_letters,
            "warehouse": tables, "mapping_coverage": coverage}


@warehouse_router.post("/etl/run")
def run_etl_now(request: Request, session: Session = Depends(get_db),
                user: User = Depends(require_capability("pophealth.run"))):
    """Trigger an ETL drain (admin only). Audited. Warehouse DB comes from
    WAREHOUSE_DATABASE_URL, exactly like the CLI."""
    stats = etl.run_etl(request.app.state.session_factory,
                        wh.warehouse_session_factory())
    append_audit(session, actor=user.username, action="warehouse.etl.run",
                 entity="warehouse",
                 payload={"processed": stats["processed"],
                          "dead_letter": stats["dead_letter"],
                          "no_target": stats["no_target"]})
    session.commit()
    return stats
