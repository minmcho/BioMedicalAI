"""Self-service ad-hoc analytics + ATHENA/OMOP vocabulary load —
Phase 16 WS-H (BD-44).

Two surfaces, both gated by the `analytics.use` capability:

- **Ad-hoc analytics over the OMOP warehouse** (`app.warehouse`).
  A definition is governed Studio configuration of kind
  ``analytics_view`` (registered centrally in app.configstore by the
  coordinator; payload-shape validation lives here in
  `validate_analytics_definition` and is enforced by this module's
  endpoints):

      {title, dataset, fields[], filters[{field, op, value}],
       groupBy[], aggregation {fn, field?}}

  `dataset` names a warehouse table from the ALLOWLIST below — the
  OMOP subset tables plus the Phase 16 vocabulary tables. There is
  NO raw-SQL path, mirroring app.report_engine's posture: identifiers
  (dataset, fields, filter fields, groupBy, aggregation field) are
  accepted ONLY when they exactly match the allowlist / the dataset's
  real columns, and every filter VALUE travels as a bound SQLAlchemy
  parameter — a value is data, never SQL text. A payload carrying a
  ``sql``/``query`` key anywhere, an unknown dataset, or a field
  outside the dataset's columns is refused 422.

  Execution semantics: filters AND together; with `groupBy` (with or
  without an explicit `aggregation`, which then defaults to count)
  the result is one row per group carrying the groupBy values plus
  the aggregation under the key ``value``; with `aggregation` alone,
  a single ``{value}`` row; otherwise a plain projection of `fields`.

- **Vocabulary load + coverage.** `POST /analytics/vocabulary/load`
  reads ATHENA-format files (CONCEPT.csv, CONCEPT_RELATIONSHIP.csv —
  TAB-separated per ATHENA convention) from the directory named in
  the published `integration/vocabulary` config (a request body
  ``{directory}`` may override it for one load). Loads are batched
  and IDEMPOTENT: reloading replaces rows by concept_id (concepts)
  and by (concept_id_1, concept_id_2, relationship_id)
  (relationships), so a reload never duplicates. An empty/unset
  directory, a missing directory, or a missing CONCEPT.csv reports
  0 loaded WITH the reason — it never fabricates rows. The real
  ATHENA bundle itself (account-gated external download, GB-scale)
  is NOT RUN in this build; the loader is format-complete and is
  exercised on a generated deterministic corpus in the test suite.
  `GET /analytics/vocabulary/coverage` reports loaded counts per
  vocabulary_id / domain_id and the grand total against the curated
  `concept_map` baseline count (the documented subset in
  app.warehouse that the ETL resolves against today).
"""
import csv
import os
import re
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import Date as SADate
from sqlalchemy import DateTime as SADateTime
from sqlalchemy import Float as SAFloat
from sqlalchemy import Integer as SAInteger
from sqlalchemy import String as SAString
from sqlalchemy import Text as SAText
from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.orm import Session

from . import warehouse as wh
from .audit import append_audit
from .configstore import list_config_versions, published_config
from .models import ConfigStatus, StudioConfig, User
from .security import get_db, require_capability

router = APIRouter(prefix="/analytics", tags=["analytics"])

KIND = "analytics_view"
VOCAB_KIND = "integration"
VOCAB_KEY = "vocabulary"
FILTER_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "contains")
AGG_FNS = ("count", "sum", "avg", "min", "max")
MAX_ROWS = 5000
BATCH = 2000
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# The dataset allowlist: warehouse table name -> model. Identifiers
# are ONLY ever taken from this registry and the models' real
# columns — never from request text.
DATASETS: dict[str, object] = {
    "person": wh.Person,
    "visit_occurrence": wh.VisitOccurrence,
    "condition_occurrence": wh.ConditionOccurrence,
    "measurement": wh.Measurement,
    "observation": wh.ObservationOmop,
    "drug_exposure": wh.DrugExposure,
    "procedure_occurrence": wh.ProcedureOccurrence,
    "concept_map": wh.ConceptMap,
    "vocab_concept": wh.VocabConcept,
    "vocab_relationship": wh.VocabRelationship,
}


# ---------------------------------------------------------------------------
# Defaults (seeded published config — the Studio surface of this module)
# ---------------------------------------------------------------------------

def default_vocabulary_payload() -> dict:
    return {
        "name": "ATHENA / OMOP standard vocabulary load (Phase 16 WS-H)",
        "source": "ATHENA",
        # Empty = no bundle staged on this deployment: loads report 0
        # with that reason until a site downloads the bundle (an
        # account-gated external download — NOT RUN here) and
        # publishes the directory it staged it in.
        "directory": "",
        "files": {"concepts": "CONCEPT.csv",
                  "relationships": "CONCEPT_RELATIONSHIP.csv"},
        "format": "tab-separated ATHENA export",
    }


def default_example_view_payload() -> dict:
    return {
        "title": "Condition occurrences by concept",
        "dataset": "condition_occurrence",
        "fields": ["condition_concept_id"],
        "filters": [],
        "groupBy": ["condition_concept_id"],
        "aggregation": {"fn": "count"},
    }


def _seed_published(session: Session, kind: str, key: str,
                    payload: dict) -> None:
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


def ensure_analytics_defaults(session: Session) -> None:
    """Seed the published `integration/vocabulary` config (empty
    directory — the honest no-bundle state) and one published
    example `analytics_view` (idempotent)."""
    _seed_published(session, VOCAB_KIND, VOCAB_KEY,
                    default_vocabulary_payload())
    _seed_published(session, KIND, "condition-counts-by-concept",
                    default_example_view_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Definition validation (no raw SQL — identifiers from the allowlist only)
# ---------------------------------------------------------------------------

def _scan_forbidden(payload, path: str = "") -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in ("sql", "query", "raw_sql", "rawsql"):
                raise HTTPException(
                    status_code=422,
                    detail=(f"'{key}' is forbidden in an analytics "
                            "definition (definitions name allowlisted "
                            "warehouse datasets; there is no SQL path)"))
            _scan_forbidden(value, f"{path}.{key}")
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            _scan_forbidden(item, f"{path}[{i}]")


def _columns(model) -> dict:
    return {c.name: c for c in model.__table__.columns}


def validate_analytics_definition(payload) -> dict:
    """Validate one `analytics_view` payload; returns it normalised
    (defaults applied). Raises HTTPException 422 on any problem.
    This is the validator the coordinator's configstore registration
    for kind `analytics_view` delegates to."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422,
                            detail="Analytics definition must be a JSON object")
    _scan_forbidden(payload)
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise HTTPException(status_code=422,
                            detail="'title' is required (non-empty string)")
    dataset = payload.get("dataset")
    if dataset not in DATASETS:
        raise HTTPException(
            status_code=422,
            detail=(f"Unknown dataset '{dataset}'; defined datasets: "
                    f"{sorted(DATASETS)} (allowlisted warehouse tables "
                    "only — no ad-hoc SQL)"))
    columns = _columns(DATASETS[dataset])

    def _check_field(name, where: str) -> str:
        if not isinstance(name, str) or not _IDENT_RE.match(name) \
                or name not in columns:
            raise HTTPException(
                status_code=422,
                detail=(f"{where} '{name}' is not a column of dataset "
                        f"'{dataset}'; columns: {sorted(columns)}"))
        return name

    fields = payload.get("fields") or []
    if not isinstance(fields, list) or any(not isinstance(f, str) for f in fields):
        raise HTTPException(status_code=422,
                            detail="'fields' must be a list of field names")
    fields = [_check_field(f, "fields entry") for f in fields]
    if len(set(fields)) != len(fields):
        raise HTTPException(status_code=422, detail="'fields' must be unique")

    filters = payload.get("filters") or []
    if not isinstance(filters, list):
        raise HTTPException(status_code=422, detail="'filters' must be a list")
    for i, flt in enumerate(filters):
        if not isinstance(flt, dict):
            raise HTTPException(status_code=422,
                                detail=f"filters[{i}] must be an object")
        _check_field(flt.get("field"), f"filters[{i}].field")
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
        if flt["op"] == "contains" and not isinstance(
                columns[flt["field"]].type, (SAString, SAText)):
            raise HTTPException(
                status_code=422,
                detail=f"filters[{i}]: op 'contains' needs a text column")

    group_by = payload.get("groupBy") or []
    if not isinstance(group_by, list) or any(
            not isinstance(g, str) for g in group_by):
        raise HTTPException(status_code=422,
                            detail="'groupBy' must be a list of field names")
    group_by = [_check_field(g, "groupBy entry") for g in group_by]
    if len(set(group_by)) != len(group_by):
        raise HTTPException(status_code=422, detail="'groupBy' must be unique")

    aggregation = payload.get("aggregation")
    if aggregation is not None:
        if not isinstance(aggregation, dict):
            raise HTTPException(status_code=422,
                                detail="'aggregation' must be an object {fn, field?}")
        if aggregation.get("fn") not in AGG_FNS:
            raise HTTPException(
                status_code=422,
                detail=f"'aggregation.fn' must be one of {list(AGG_FNS)}")
        agg_field = aggregation.get("field")
        if agg_field is not None:
            _check_field(agg_field, "aggregation.field")
        elif aggregation["fn"] != "count":
            raise HTTPException(
                status_code=422,
                detail=f"'aggregation.field' is required for fn "
                       f"'{aggregation['fn']}'")
    if not fields and not group_by and aggregation is None:
        raise HTTPException(
            status_code=422,
            detail="Definition needs at least one of 'fields', "
                   "'groupBy' or 'aggregation'")
    if group_by and aggregation is None:
        aggregation = {"fn": "count"}
    return {**payload, "title": title, "dataset": dataset,
            "fields": fields, "filters": filters, "groupBy": group_by,
            "aggregation": aggregation}


# ---------------------------------------------------------------------------
# Query execution (bound parameters for values; identifiers allowlisted)
# ---------------------------------------------------------------------------

def _coerce(column, value, where: str):
    """Coerce a JSON filter value to the column's type. A value that
    cannot coerce is a 422 — it is never interpolated into SQL."""
    ctype = column.type
    if isinstance(value, bool) and isinstance(ctype, SAInteger):
        raise HTTPException(status_code=422,
                            detail=f"{where}: boolean is not a valid value")
    try:
        if isinstance(ctype, SADateTime):
            if isinstance(value, datetime):
                return value
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if isinstance(ctype, SADate):
            if isinstance(value, date):
                return value
            text = str(value).strip()
            if re.fullmatch(r"\d{8}", text):
                return datetime.strptime(text, "%Y%m%d").date()
            return date.fromisoformat(text[:10])
        if isinstance(ctype, SAInteger):
            if isinstance(value, int):
                return value
            return int(str(value), 10)
        if isinstance(ctype, SAFloat):
            return float(value)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail=f"{where}: value {value!r} does not fit the column type")
    return value


def _apply_filter(stmt, model, columns: dict, flt: dict, index: int):
    column = getattr(model, flt["field"])
    col_type = columns[flt["field"]]
    op, raw = flt["op"], flt["value"]
    where = f"filters[{index}]"
    if op == "in":
        values = [_coerce(col_type, v, where) for v in raw]
        return stmt.where(column.in_(values))
    if op == "contains":
        return stmt.where(column.contains(str(raw)))
    value = _coerce(col_type, raw, where)
    if op == "eq":
        return stmt.where(column == value)
    if op == "ne":
        return stmt.where(column != value)
    if op == "gt":
        return stmt.where(column > value)
    if op == "gte":
        return stmt.where(column >= value)
    if op == "lt":
        return stmt.where(column < value)
    return stmt.where(column <= value)  # lte


def _serialise(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def execute_definition(wh_session: Session, definition: dict) -> list[dict]:
    """Run one validated definition against the warehouse. Every
    identifier comes from the allowlist registry / model columns;
    every value is a bound parameter."""
    model = DATASETS[definition["dataset"]]
    columns = _columns(model)
    group_by = definition.get("groupBy") or []
    aggregation = definition.get("aggregation")
    fields = definition.get("fields") or []

    if aggregation is not None:
        fn = aggregation["fn"]
        agg_field = aggregation.get("field")
        agg_col = getattr(model, agg_field) if agg_field else None
        if fn == "count":
            agg_expr = func.count(agg_col) if agg_col is not None \
                else func.count()
        elif fn == "sum":
            agg_expr = func.sum(agg_col)
        elif fn == "avg":
            agg_expr = func.avg(agg_col)
        elif fn == "min":
            agg_expr = func.min(agg_col)
        else:
            agg_expr = func.max(agg_col)
        group_cols = [getattr(model, g) for g in group_by]
        stmt = select(*group_cols, agg_expr.label("value"))
        for i, flt in enumerate(definition.get("filters") or []):
            stmt = _apply_filter(stmt, model, columns, flt, i)
        if group_cols:
            stmt = stmt.group_by(*group_cols).order_by(*group_cols)
        rows = wh_session.execute(stmt.limit(MAX_ROWS)).all()
        out = []
        for row in rows:
            item = {g: _serialise(row[i]) for i, g in enumerate(group_by)}
            item["value"] = _serialise(row[len(group_by)])
            out.append(item)
        return out

    selected = fields or sorted(columns)
    cols = [getattr(model, f) for f in selected]
    stmt = select(*cols)
    for i, flt in enumerate(definition.get("filters") or []):
        stmt = _apply_filter(stmt, model, columns, flt, i)
    stmt = stmt.order_by(*cols).limit(MAX_ROWS)
    return [{f: _serialise(row[i]) for i, f in enumerate(selected)}
            for row in wh_session.execute(stmt).all()]


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


@router.get("/datasets")
def list_datasets(user: User = Depends(require_capability("analytics.use"))):
    """The dataset allowlist + columns — the builder UI's catalogue."""
    items = []
    for name in sorted(DATASETS):
        model = DATASETS[name]
        items.append({
            "dataset": name,
            "table": model.__tablename__,
            "columns": [{"name": c.name, "type": str(c.type)}
                        for c in model.__table__.columns],
        })
    return {"items": items, "total": len(items)}


@router.post("/query")
async def run_query(request: Request,
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability("analytics.use"))):
    """Execute a definition: `{key}` runs the PUBLISHED
    `analytics_view` of that key; `{definition: {...}}` (or a bare
    definition body) runs ad-hoc. Both paths validate identically."""
    body = await _json_body(request)
    _scan_forbidden(body)
    key = body.get("key")
    if isinstance(body.get("definition"), dict):
        definition = validate_analytics_definition(body["definition"])
        source = "ad-hoc"
    elif isinstance(key, str) and key and "dataset" not in body:
        cfg = published_config(session, KIND, key)
        if cfg is None:
            raise HTTPException(
                status_code=404,
                detail=f"No published analytics_view '{key}'")
        definition = validate_analytics_definition(cfg.payload or {})
        source = "saved"
    elif "dataset" in body:
        definition = validate_analytics_definition(body)
        source = "ad-hoc"
        key = None
    else:
        raise HTTPException(
            status_code=422,
            detail="Query body must carry a saved 'key' or an ad-hoc "
                   "definition ('definition' object or a bare definition)")
    factory = wh.warehouse_session_factory()
    with factory() as wh_session:
        wh.ensure_concept_map(wh_session)
        rows = execute_definition(wh_session, definition)
    append_audit(session, actor=user.username, action="analytics.query",
                 entity=f"analytics_view:{key}" if key else "analytics:ad-hoc",
                 payload={"dataset": definition["dataset"],
                          "source": source, "rows": len(rows),
                          "key": key})
    session.commit()
    return {"definition": definition, "source": source, "key": key,
            "dataset": definition["dataset"], "rows": rows,
            "row_count": len(rows)}


# ---------------------------------------------------------------------------
# ATHENA vocabulary load + coverage
# ---------------------------------------------------------------------------

def _athena_date(raw) -> date | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"\d{8}", text):
            return datetime.strptime(text, "%Y%m%d").date()
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _clean(raw) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _load_concepts(wh_session: Session, path: str) -> tuple[int, int]:
    """Load one CONCEPT.csv (tab-separated). Returns (loaded, skipped).
    Idempotent: each batch first deletes the concept_ids it carries."""
    loaded = skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if not reader.fieldnames or "concept_id" not in reader.fieldnames:
            raise HTTPException(
                status_code=422,
                detail="CONCEPT.csv is missing its ATHENA header "
                       "(expected a tab-separated header incl. concept_id)")
        batch: list[wh.VocabConcept] = []

        def _flush():
            nonlocal loaded
            if not batch:
                return
            ids = [r.concept_id for r in batch]
            wh_session.execute(
                delete(wh.VocabConcept).where(
                    wh.VocabConcept.concept_id.in_(ids)))
            wh_session.add_all(batch)
            wh_session.flush()
            loaded += len(batch)
            batch.clear()

        for raw in reader:
            try:
                concept_id = int(str(raw.get("concept_id") or "").strip())
            except ValueError:
                skipped += 1
                continue
            batch.append(wh.VocabConcept(
                concept_id=concept_id,
                concept_name=_clean(raw.get("concept_name")) or "",
                domain_id=_clean(raw.get("domain_id")) or "",
                vocabulary_id=_clean(raw.get("vocabulary_id")) or "",
                concept_class_id=_clean(raw.get("concept_class_id")) or "",
                standard_concept=_clean(raw.get("standard_concept")),
                concept_code=_clean(raw.get("concept_code")) or "",
                valid_start_date=_athena_date(raw.get("valid_start_date")),
                valid_end_date=_athena_date(raw.get("valid_end_date")),
                invalid_reason=_clean(raw.get("invalid_reason"))))
            if len(batch) >= BATCH:
                _flush()
        _flush()
    return loaded, skipped


def _load_relationships(wh_session: Session, path: str) -> tuple[int, int]:
    loaded = skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if not reader.fieldnames or "concept_id_1" not in reader.fieldnames:
            raise HTTPException(
                status_code=422,
                detail="CONCEPT_RELATIONSHIP.csv is missing its ATHENA "
                       "header (expected concept_id_1/concept_id_2/"
                       "relationship_id)")
        batch: list[wh.VocabRelationship] = []

        def _flush():
            nonlocal loaded
            if not batch:
                return
            triples = [(r.concept_id_1, r.concept_id_2, r.relationship_id)
                       for r in batch]
            wh_session.execute(
                delete(wh.VocabRelationship).where(
                    tuple_(wh.VocabRelationship.concept_id_1,
                           wh.VocabRelationship.concept_id_2,
                           wh.VocabRelationship.relationship_id).in_(triples)))
            wh_session.add_all(batch)
            wh_session.flush()
            loaded += len(batch)
            batch.clear()

        for raw in reader:
            try:
                c1 = int(str(raw.get("concept_id_1") or "").strip())
                c2 = int(str(raw.get("concept_id_2") or "").strip())
            except ValueError:
                skipped += 1
                continue
            batch.append(wh.VocabRelationship(
                concept_id_1=c1, concept_id_2=c2,
                relationship_id=_clean(raw.get("relationship_id")) or "",
                valid_start_date=_athena_date(raw.get("valid_start_date")),
                valid_end_date=_athena_date(raw.get("valid_end_date")),
                invalid_reason=_clean(raw.get("invalid_reason"))))
            if len(batch) >= BATCH:
                _flush()
        _flush()
    return loaded, skipped


@router.post("/vocabulary/load")
async def load_vocabulary(request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("analytics.use"))):
    """Load the ATHENA bundle staged in the configured directory
    (body `{directory}` overrides the published config for this
    load). Reports 0 with the reason when nothing is staged — the
    real bundle download is NOT RUN and rows are never fabricated."""
    raw = await request.body()
    body: dict = {}
    if raw.strip():
        body = await _json_body(request)
    cfg = published_config(session, VOCAB_KIND, VOCAB_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    files = payload.get("files") or {}
    directory = body.get("directory") or payload.get("directory") or ""
    result: dict = {"directory": directory or None,
                    "concepts_loaded": 0, "relationships_loaded": 0,
                    "skipped_rows": 0, "reason": None}
    if not isinstance(directory, str) or not directory.strip():
        result["reason"] = (
            "No vocabulary directory is configured: the published "
            "integration/vocabulary config names an empty directory "
            "(the ATHENA bundle is an account-gated external download "
            "and is NOT RUN in this build), so there is nothing to load.")
    elif not os.path.isdir(directory):
        result["reason"] = f"Vocabulary directory '{directory}' does not exist."
    else:
        concept_file = os.path.join(
            directory, files.get("concepts") or "CONCEPT.csv")
        rel_file = os.path.join(
            directory, files.get("relationships") or "CONCEPT_RELATIONSHIP.csv")
        if not os.path.isfile(concept_file):
            result["reason"] = (
                f"CONCEPT.csv not found in '{directory}' — the directory "
                "holds no ATHENA bundle to load.")
        else:
            factory = wh.warehouse_session_factory()
            with factory() as wh_session:
                wh.ensure_concept_map(wh_session)
                concepts, skipped = _load_concepts(wh_session, concept_file)
                relationships, rel_skipped = 0, 0
                if os.path.isfile(rel_file):
                    relationships, rel_skipped = _load_relationships(
                        wh_session, rel_file)
                else:
                    result["reason"] = (
                        "CONCEPT_RELATIONSHIP.csv not found — concepts "
                        "loaded, relationships left unchanged.")
                wh_session.commit()
            result["concepts_loaded"] = concepts
            result["relationships_loaded"] = relationships
            result["skipped_rows"] = skipped + rel_skipped
    append_audit(session, actor=user.username,
                 action="analytics.vocabulary.load", entity="vocabulary",
                 payload={"directory": result["directory"],
                          "concepts_loaded": result["concepts_loaded"],
                          "relationships_loaded": result["relationships_loaded"],
                          "skipped_rows": result["skipped_rows"],
                          "reason": result["reason"]})
    session.commit()
    return result


@router.get("/vocabulary/coverage")
def vocabulary_coverage(user: User = Depends(require_capability("analytics.use"))):
    """Loaded vocabulary counts (per vocabulary_id / domain_id) vs
    the curated `concept_map` baseline the ETL resolves against."""
    factory = wh.warehouse_session_factory()
    with factory() as wh_session:
        wh.ensure_concept_map(wh_session)
        total = wh_session.scalar(
            select(func.count()).select_from(wh.VocabConcept)) or 0
        rel_total = wh_session.scalar(
            select(func.count()).select_from(wh.VocabRelationship)) or 0
        by_vocab = [{"vocabulary_id": v, "count": n} for v, n in
                    wh_session.execute(
                        select(wh.VocabConcept.vocabulary_id, func.count())
                        .group_by(wh.VocabConcept.vocabulary_id)
                        .order_by(wh.VocabConcept.vocabulary_id)).all()]
        by_domain = [{"domain_id": d, "count": n} for d, n in
                     wh_session.execute(
                         select(wh.VocabConcept.domain_id, func.count())
                         .group_by(wh.VocabConcept.domain_id)
                         .order_by(wh.VocabConcept.domain_id)).all()]
        baseline = wh_session.scalar(
            select(func.count()).select_from(wh.ConceptMap)) or 0
    return {
        "vocab_concept_total": total,
        "vocab_relationship_total": rel_total,
        "by_vocabulary": by_vocab,
        "by_domain": by_domain,
        "curated_concept_map_count": baseline,
        "concept_map_baseline": {
            "table": "concept_map", "count": baseline,
            "note": ("The curated, documented subset in app.warehouse "
                     "that the FHIR->OMOP ETL resolves against today; "
                     "the ATHENA load above is the production-scale "
                     "vocabulary it stands in for.")},
    }
