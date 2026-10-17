"""Phase 9 — Quality-measure engine (eCQM-style, config-defined).

A quality measure is Studio configuration (kind `measure`): a name,
a DENOMINATOR criteria object and a NUMERATOR criteria object, both
in exactly the criteria style the population-health registries use
(condition codes and/or a latest-lab rule), extended with `labs`
(several lab rules, all must hold), `procedureCodes` and `ageRange`.

The engine does NOT re-implement cohort matching: membership and
latest-lab evaluation are app.pophealth's own functions
(`_members`, `_latest_measurements`, `_compare`), run over the same
OMOP warehouse. The measure module only composes those primitives
(intersection for AND-criteria, denominator-universe restriction for
the numerator) and adds the two criterion kinds registries never
needed (procedures, age band).

Results follow the population-health privacy contract: counts below
the measure's smallCellThreshold (default 5) are suppressed via the
same `_cell` shape, and the RATE is only reported when both counts
are visible — a rate over a known denominator would leak a
suppressed numerator.
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import pophealth as ph
from . import warehouse as wh
from .audit import append_audit
from .configstore import published_config, published_configs
from .models import ConfigStatus, StudioConfig, User
from .security import get_db, require_capability

router = APIRouter(prefix="/quality", tags=["quality"])

SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"


def default_measure_payloads() -> dict[str, dict]:
    return {
        "hba1c-control": {
            "name": "Diabetes: HbA1c control (< 8.0%)",
            "title": "HbA1c control in type 2 diabetes",
            "description": "Persons with type 2 diabetes whose latest "
                           "HbA1c within 365 days is below 8.0%.",
            "denominator": {
                "conditionCodes": [{"system": SNOMED, "code": "44054006"}]},
            "numerator": {
                "lab": {"system": LOINC, "code": "4548-4",
                        "comparator": "<", "threshold": 8.0,
                        "windowDays": 365}},
            "smallCellThreshold": 5,
        },
        "bp-control": {
            "name": "Hypertension: blood pressure control (< 140/90)",
            "title": "Blood pressure control in hypertension",
            "description": "Persons with hypertension whose latest "
                           "systolic is below 140 mmHg AND whose latest "
                           "diastolic is below 90 mmHg.",
            "denominator": {
                "conditionCodes": [{"system": SNOMED, "code": "38341003"}]},
            "numerator": {
                "labs": [
                    {"system": LOINC, "code": "8480-6",
                     "comparator": "<", "threshold": 140},
                    {"system": LOINC, "code": "8462-4",
                     "comparator": "<", "threshold": 90},
                ]},
            "smallCellThreshold": 5,
        },
        "colorectal-screening": {
            "name": "Colorectal cancer screening (ages 45-75)",
            "title": "Colorectal cancer screening",
            "description": "Persons aged 45-75 (at the warehouse "
                           "reference date) with a colonoscopy on record.",
            "denominator": {"ageRange": [45, 75]},
            "numerator": {
                "procedureCodes": [{"system": SNOMED, "code": "73761001"}]},
            "smallCellThreshold": 5,
        },
    }


def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
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


def ensure_quality_defaults(session: Session) -> None:
    """Seed the three built-in quality measures as published Studio
    config v1 (idempotent) — measures are configuration, so a site
    edits thresholds by publishing a new version, never by code."""
    for key, payload in default_measure_payloads().items():
        _seed_published(session, "measure", key, payload)
    session.commit()


# ---------------------------------------------------------------------------
# Criteria evaluation — composed from app.pophealth's evaluator
# ---------------------------------------------------------------------------

def _lab_rule_matches(wh_session: Session, rule: dict,
                      candidate_ids: list[int],
                      reference: date) -> set[int]:
    """Persons (of candidate_ids) whose LATEST measurement of the
    rule's code satisfies the rule — pophealth's own latest-measurement
    lookup and comparator. A `windowDays` rule additionally requires
    the latest result to be no older than the window, measured back
    from the warehouse reference date (the registry summary measures
    its window back from the newest data; the two agree whenever the
    newest measurement in the warehouse is the data's 'today')."""
    latest = ph._latest_measurements(wh_session, rule["code"], candidate_ids)
    window = rule.get("windowDays")
    matched: set[int] = set()
    for pid, measurement in latest.items():
        if measurement.value_as_number is None:
            continue
        if window:
            if measurement.measurement_datetime is None:
                continue
            if measurement.measurement_datetime.date() \
                    < reference - timedelta(days=int(window)):
                continue
        if ph._compare(measurement.value_as_number, rule["comparator"],
                       float(rule["threshold"])):
            matched.add(pid)
    return matched


def criteria_person_ids(wh_session: Session, criteria: dict,
                        universe: set[int] | None = None) -> set[int]:
    """Person ids satisfying ALL of a criteria object's criteria
    (AND). `universe` restricts the candidates (the numerator is
    evaluated inside the denominator); with no universe and no
    membership criterion, the universe is every person in the
    warehouse."""
    ids: set[int] | None = set(universe) if universe is not None else None

    def restrict(new_ids: set[int]) -> None:
        nonlocal ids
        ids = set(new_ids) if ids is None else ids & set(new_ids)

    conditions = [c for c in criteria.get("conditionCodes") or []
                  if isinstance(c, dict) and c.get("code")]
    if conditions:
        members = ph._members(wh_session, {"conditionCodes": conditions})
        restrict({p.person_id for p in members})

    procedures = [c for c in criteria.get("procedureCodes") or []
                  if isinstance(c, dict) and c.get("code")]
    if procedures:
        codes = [c["code"] for c in procedures]
        restrict(set(wh_session.scalars(
            select(wh.ProcedureOccurrence.person_id)
            .where(wh.ProcedureOccurrence.procedure_source_value.in_(codes))
            .distinct()).all()))

    lab_rules: list[dict] = []
    if isinstance(criteria.get("lab"), dict):
        lab_rules.append(criteria["lab"])
    for rule in criteria.get("labs") or []:
        if isinstance(rule, dict):
            lab_rules.append(rule)
    if lab_rules:
        reference = ph._reference_date(wh_session)
        if ids is None:
            candidates = list(wh_session.scalars(
                select(wh.Person.person_id)).all())
        else:
            candidates = sorted(ids)
        for rule in lab_rules:
            restrict(_lab_rule_matches(wh_session, rule, candidates, reference))

    age_range = criteria.get("ageRange")
    if isinstance(age_range, list) and len(age_range) == 2:
        low, high = age_range
        reference = ph._reference_date(wh_session)
        persons = wh_session.scalars(select(wh.Person)).all()
        in_range = set()
        for person in persons:
            if person.year_of_birth is None:
                continue
            age = reference.year - person.year_of_birth
            if low <= age <= high:
                in_range.add(person.person_id)
        restrict(in_range)

    if ids is None:
        ids = set(wh_session.scalars(select(wh.Person.person_id)).all())
    return ids


def compute_measure(wh_session: Session, key: str, definition: dict) -> dict:
    """Denominator + numerator counts and the rate, with the
    population-health small-cell suppression contract applied to
    both counts (and to the rate whenever either count is hidden)."""
    threshold = definition.get("smallCellThreshold") or ph.DEFAULT_SMALL_CELL
    denominator_ids = criteria_person_ids(
        wh_session, definition.get("denominator") or {})
    numerator_ids = criteria_person_ids(
        wh_session, definition.get("numerator") or {},
        universe=denominator_ids)
    den_cell = ph._cell(len(denominator_ids), threshold)
    num_cell = ph._cell(len(numerator_ids), threshold)
    # Public contract mirrors pophealth's: the count value itself
    # (an int, or "<5" when suppressed) plus one suppressed flag.
    suppressed = bool(den_cell["suppressed"] or num_cell["suppressed"])
    rate = None
    if not suppressed and denominator_ids:
        rate = round(100.0 * len(numerator_ids) / len(denominator_ids), 1)
    return {
        "measure": key,
        "name": definition.get("name"),
        "title": definition.get("title"),
        "description": definition.get("description"),
        "denominator": den_cell["count"],
        "numerator": num_cell["count"],
        "rate": rate,
        "suppressed": suppressed,
        "small_cell_threshold": threshold,
        "as_of": ph._reference_date(wh_session).isoformat(),
    }


# ---------------------------------------------------------------------------
# /quality routes
# ---------------------------------------------------------------------------

@router.get("/measures")
def list_measures(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("pophealth.view"))):
    """Published quality-measure definitions from Studio config."""
    measures = []
    for cfg in published_configs(session, "measure"):
        payload = cfg.payload or {}
        measures.append({
            "key": cfg.key,
            "name": payload.get("name"),
            "title": payload.get("title"),
            "description": payload.get("description"),
            "definition_version": cfg.version,
            "small_cell_threshold": payload.get("smallCellThreshold")
            or ph.DEFAULT_SMALL_CELL,
            "definition": payload,
        })
    measures.sort(key=lambda m: m["key"])
    return {"measures": measures, "total": len(measures)}


@router.get("/measures/{key}/result")
def measure_result(key: str, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("pophealth.view"))):
    cfg = published_config(session, "measure", key)
    if cfg is None:
        raise HTTPException(status_code=404,
                            detail=f"No published measure config '{key}'")
    factory = wh.warehouse_session_factory()
    with factory() as wh_session:
        wh.ensure_concept_map(wh_session)
        return compute_measure(wh_session, key, cfg.payload or {})
