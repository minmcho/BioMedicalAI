"""Phase 16 WS-H (BD-44) — self-service ad-hoc analytics +
ATHENA/OMOP vocabulary load.

Non-engineer flow under test: build a definition over the warehouse
(a Studio `analytics_view` shape), run it against ETL-seeded OMOP
data, and read back exact grouped counts — saved-published and ad-hoc
paths, with the no-raw-SQL posture enforced (identifiers only from
the allowlist, values always bound). The vocabulary loader is
exercised on a GENERATED deterministic ATHENA-format corpus
(>= 20,000 concept rows across 3 vocabularies) in tmp; the real
ATHENA bundle (account-gated download) is NOT RUN. Synthetic only.
"""
import csv
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app import warehouse as wh
from app.models import ConfigStatus, StudioConfig
from tests.conftest import ADMIN, CLINICIAN, PATIENT

SEED_DIR = Path(__file__).resolve().parent.parent / "seed"

VOCABS = (("SNOMED", "Condition", "Clinical Finding"),
          ("LOINC", "Measurement", "Laboratory Procedure"),
          ("RxNorm", "Drug", "Ingredient"))
CONCEPT_ROWS = 21000  # >= 20,000, exactly 7,000 per vocabulary
REL_ROWS = 5000


def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "opencare_seed", SEED_DIR / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _grant_analytics(client):
    """Publish the permissions cell for the Phase 16 capability
    `analytics.use` (coordinator wires the CAPABILITY_DEFAULTS entry
    at integration; this mirrors the proposed defaults exactly:
    admin / configurator / clinician)."""
    factory = client.app.state.session_factory
    with factory() as s:
        existing = s.scalars(select(StudioConfig).where(
            StudioConfig.kind == "permissions",
            StudioConfig.key == "role-matrix",
            StudioConfig.status == ConfigStatus.published)).first()
        if existing is None:
            s.add(StudioConfig(
                kind="permissions", key="role-matrix", version=1,
                status=ConfigStatus.published,
                payload={"capabilities": {"analytics.use": {
                    "admin": True, "configurator": True,
                    "clinician": True}}},
                created_by="test", updated_by="test"))
            s.commit()


def _insert_config(client, kind, key, payload, status):
    factory = client.app.state.session_factory
    with factory() as s:
        s.add(StudioConfig(kind=kind, key=key, version=1, status=status,
                           payload=payload, created_by="test",
                           updated_by="test"))
        s.commit()


@pytest.fixture()
def stocked(client, tmp_path, monkeypatch):
    """Seed bundle loaded + ETL drained into a throwaway warehouse."""
    monkeypatch.setenv("WAREHOUSE_DATABASE_URL",
                       f"sqlite:///{tmp_path}/wh.db")
    _grant_analytics(client)
    seed = _load_seed_module()
    bundle = json.loads((SEED_DIR / "synthea_bundle.json").read_text())
    seed.load_bundle(bundle, seed.ClientTransport(client, ADMIN))
    r = client.post("/warehouse/etl/run", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["processed"] == 80 and r.json()["dead_letter"] == 0
    return client


@pytest.fixture()
def wh_only(client, tmp_path, monkeypatch):
    """Empty throwaway warehouse (vocabulary tests)."""
    monkeypatch.setenv("WAREHOUSE_DATABASE_URL",
                       f"sqlite:///{tmp_path}/wh.db")
    _grant_analytics(client)
    return client


def _write_corpus(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    concept_header = ["concept_id", "concept_name", "domain_id",
                      "vocabulary_id", "concept_class_id",
                      "standard_concept", "concept_code",
                      "valid_start_date", "valid_end_date",
                      "invalid_reason"]
    with open(directory / "CONCEPT.csv", "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(concept_header)
        for i in range(CONCEPT_ROWS):
            vocab, domain, klass = VOCABS[i % 3]
            w.writerow([3_000_000 + i, f"Synthetic concept {i}", domain,
                        vocab, klass, "S" if i % 2 == 0 else "",
                        f"{vocab[:2]}-{i:06d}", "19700101", "20991231", ""])
    rel_header = ["concept_id_1", "concept_id_2", "relationship_id",
                  "valid_start_date", "valid_end_date", "invalid_reason"]
    with open(directory / "CONCEPT_RELATIONSHIP.csv", "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(rel_header)
        for i in range(REL_ROWS):
            w.writerow([3_000_000 + i,
                        3_000_000 + ((i + 1) % CONCEPT_ROWS),
                        "Maps to" if i % 2 == 0 else "Is a",
                        "19700101", "20991231", ""])
    return directory


# ---------------------------------------------------------------------------
# Role gates + dataset catalogue
# ---------------------------------------------------------------------------

def test_analytics_role_gates(client):
    _grant_analytics(client)
    assert client.get("/analytics/datasets").status_code == 401
    assert client.get("/analytics/datasets", headers=PATIENT).status_code == 403
    assert client.post("/analytics/query", json={"dataset": "person"},
                       headers=PATIENT).status_code == 403
    assert client.get("/analytics/vocabulary/coverage",
                      headers=PATIENT).status_code == 403
    assert client.get("/analytics/datasets", headers=CLINICIAN).status_code == 200


def test_datasets_allowlist(wh_only):
    r = wh_only.get("/analytics/datasets", headers=ADMIN)
    assert r.status_code == 200
    items = {i["dataset"]: i for i in r.json()["items"]}
    for name in ("person", "visit_occurrence", "condition_occurrence",
                 "measurement", "drug_exposure", "procedure_occurrence",
                 "vocab_concept"):
        assert name in items
    cond_cols = {c["name"] for c in items["condition_occurrence"]["columns"]}
    assert {"condition_concept_id", "condition_source_value",
            "person_id"} <= cond_cols
    vocab_cols = {c["name"] for c in items["vocab_concept"]["columns"]}
    assert {"concept_id", "concept_name", "vocabulary_id",
            "domain_id"} <= vocab_cols


# ---------------------------------------------------------------------------
# Non-engineer flow: build a definition, run it, exact grouped counts
# ---------------------------------------------------------------------------

def test_grouped_counts_non_engineer_flow(stocked):
    definition = {
        "title": "Conditions by source code",
        "dataset": "condition_occurrence",
        "fields": ["condition_source_value"],
        "filters": [],
        "groupBy": ["condition_source_value"],
        "aggregation": {"fn": "count"},
    }
    r = stocked.post("/analytics/query", json={"definition": definition},
                     headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "ad-hoc"
    assert body["definition"]["dataset"] == "condition_occurrence"
    assert body["row_count"] == 7
    grouped = {row["condition_source_value"]: row["value"]
               for row in body["rows"]}
    # Hand-computed from the seed bundle (10 Condition resources).
    assert grouped == {"38341003": 3, "44054006": 2, "195967001": 1,
                       "55822004": 1, "239873007": 1, "48694002": 1,
                       "410429000": 1}


def test_grouped_counts_with_filter_and_bare_body(stocked):
    # A bare definition body (no wrapper) is accepted too; the filter
    # value 201826 is a bound parameter coerced to the integer column.
    r = stocked.post("/analytics/query", json={
        "title": "Diabetes rows by source code",
        "dataset": "condition_occurrence",
        "fields": [],
        "filters": [{"field": "condition_concept_id", "op": "eq",
                     "value": 201826}],
        "groupBy": ["condition_source_value"],
        "aggregation": {"fn": "count"},
    }, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == [{"condition_source_value": "44054006",
                                 "value": 2}]
    assert r.json()["row_count"] == 1


def test_aggregation_avg_over_measurements(stocked):
    bundle = json.loads((SEED_DIR / "synthea_bundle.json").read_text())
    hba1c = [e["resource"]["valueQuantity"]["value"]
             for e in bundle["entry"]
             if e["resource"]["resourceType"] == "Observation"
             and e["resource"]["code"]["coding"][0]["code"] == "4548-4"]
    assert hba1c  # the seed carries HbA1c observations
    r = stocked.post("/analytics/query", json={"definition": {
        "title": "Mean HbA1c",
        "dataset": "measurement",
        "fields": [],
        "filters": [{"field": "measurement_source_value", "op": "eq",
                     "value": "4548-4"}],
        "groupBy": [],
        "aggregation": {"fn": "avg", "field": "value_as_number"},
    }}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["rows"][0]["value"] == pytest.approx(
        sum(hba1c) / len(hba1c))


def test_saved_published_view(stocked):
    _insert_config(stocked, "analytics_view", "diabetes-count", {
        "title": "Diabetes occurrences",
        "dataset": "condition_occurrence",
        "fields": [],
        "filters": [{"field": "condition_source_value", "op": "eq",
                     "value": "44054006"}],
        "groupBy": [],
        "aggregation": {"fn": "count"},
    }, ConfigStatus.published)
    r = stocked.post("/analytics/query", json={"key": "diabetes-count"},
                     headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "saved" and body["key"] == "diabetes-count"
    assert body["rows"] == [{"value": 2}]

    # A DRAFT view is not runnable (governed lifecycle), unknown neither.
    _insert_config(stocked, "analytics_view", "draft-view", {
        "title": "Draft", "dataset": "person", "fields": ["person_id"],
    }, ConfigStatus.draft)
    assert stocked.post("/analytics/query", json={"key": "draft-view"},
                        headers=ADMIN).status_code == 404
    assert stocked.post("/analytics/query", json={"key": "nope"},
                        headers=ADMIN).status_code == 404


# ---------------------------------------------------------------------------
# No-raw-SQL posture
# ---------------------------------------------------------------------------

def test_raw_sql_refused(stocked):
    base = {"title": "x", "dataset": "condition_occurrence",
            "fields": ["condition_concept_id"]}
    r = stocked.post("/analytics/query",
                     json={"definition": {**base, "sql": "SELECT * FROM person"}},
                     headers=ADMIN)
    assert r.status_code == 422
    r = stocked.post("/analytics/query",
                     json={**base, "query": "SELECT 1"}, headers=ADMIN)
    assert r.status_code == 422
    # A SQL-shaped identifier is refused by the allowlist, and the
    # warehouse table survives untouched (still queryable after).
    evil = {**base, "fields": ["condition_concept_id; DROP TABLE person"]}
    r = stocked.post("/analytics/query", json={"definition": evil},
                     headers=ADMIN)
    assert r.status_code == 422
    r = stocked.post("/analytics/query", json={"definition": base},
                     headers=ADMIN)
    assert r.status_code == 200


def test_unknown_field_and_dataset_refused(stocked):
    def q(defn):
        return stocked.post("/analytics/query", json={"definition": defn},
                            headers=ADMIN)
    ok = {"title": "x", "dataset": "condition_occurrence",
          "fields": ["condition_concept_id"]}
    assert q({**ok, "fields": ["not_a_column"]}).status_code == 422
    assert q({**ok, "filters": [{"field": "nope", "op": "eq",
                                  "value": 1}]}).status_code == 422
    assert q({**ok, "groupBy": ["nope"],
              "aggregation": {"fn": "count"}}).status_code == 422
    assert q({**ok, "dataset": "fhir_resource"}).status_code == 422
    assert q({**ok, "filters": [{"field": "condition_concept_id",
                                  "op": "regex", "value": 1}]}).status_code == 422


# ---------------------------------------------------------------------------
# Vocabulary: generated ATHENA corpus, coverage, idempotent reload
# ---------------------------------------------------------------------------

def test_vocabulary_load_and_coverage(wh_only, tmp_path):
    corpus = _write_corpus(tmp_path / "athena")
    r = wh_only.post("/analytics/vocabulary/load",
                     json={"directory": str(corpus)}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["concepts_loaded"] == CONCEPT_ROWS
    assert body["relationships_loaded"] == REL_ROWS
    assert body["skipped_rows"] == 0
    assert body["reason"] is None

    r = wh_only.get("/analytics/vocabulary/coverage", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    cov = r.json()
    assert cov["vocab_concept_total"] == CONCEPT_ROWS
    assert cov["vocab_relationship_total"] == REL_ROWS
    assert {v["vocabulary_id"]: v["count"]
            for v in cov["by_vocabulary"]} == {
                "SNOMED": 7000, "LOINC": 7000, "RxNorm": 7000}
    assert {d["domain_id"]: d["count"]
            for d in cov["by_domain"]} == {
                "Condition": 7000, "Measurement": 7000, "Drug": 7000}
    # Baseline comparison: the curated concept_map subset count.
    assert cov["curated_concept_map_count"] == len(wh.CONCEPT_MAP_SEED)
    assert cov["concept_map_baseline"]["count"] == len(wh.CONCEPT_MAP_SEED)
    assert cov["vocab_concept_total"] > cov["curated_concept_map_count"]


def test_vocabulary_reload_is_idempotent(wh_only, tmp_path):
    corpus = _write_corpus(tmp_path / "athena")
    for _ in range(2):
        r = wh_only.post("/analytics/vocabulary/load",
                         json={"directory": str(corpus)}, headers=ADMIN)
        assert r.status_code == 200, r.text
        assert r.json()["concepts_loaded"] == CONCEPT_ROWS
    cov = wh_only.get("/analytics/vocabulary/coverage",
                      headers=ADMIN).json()
    assert cov["vocab_concept_total"] == CONCEPT_ROWS
    assert cov["vocab_relationship_total"] == REL_ROWS


def test_vocab_concept_is_a_queryable_dataset(wh_only, tmp_path):
    corpus = _write_corpus(tmp_path / "athena")
    assert wh_only.post("/analytics/vocabulary/load",
                        json={"directory": str(corpus)},
                        headers=ADMIN).status_code == 200
    r = wh_only.post("/analytics/query", json={"definition": {
        "title": "Concepts per vocabulary",
        "dataset": "vocab_concept",
        "fields": [],
        "filters": [],
        "groupBy": ["vocabulary_id"],
        "aggregation": {"fn": "count"},
    }}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert {row["vocabulary_id"]: row["value"]
            for row in r.json()["rows"]} == {
                "SNOMED": 7000, "LOINC": 7000, "RxNorm": 7000}


def test_vocabulary_load_empty_directory_reports_zero(wh_only, tmp_path):
    # Seeded config names an empty directory: 0 loaded WITH a reason.
    r = wh_only.post("/analytics/vocabulary/load", json={}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["concepts_loaded"] == 0
    assert body["relationships_loaded"] == 0
    assert body["reason"]  # the reason is stated, rows never faked
    # A directory that does not exist, and one without CONCEPT.csv.
    r = wh_only.post("/analytics/vocabulary/load",
                     json={"directory": str(tmp_path / "missing")},
                     headers=ADMIN)
    assert r.json()["concepts_loaded"] == 0 and r.json()["reason"]
    empty = tmp_path / "empty"
    empty.mkdir()
    r = wh_only.post("/analytics/vocabulary/load",
                     json={"directory": str(empty)}, headers=ADMIN)
    assert r.json()["concepts_loaded"] == 0 and r.json()["reason"]
    cov = wh_only.get("/analytics/vocabulary/coverage",
                      headers=ADMIN).json()
    assert cov["vocab_concept_total"] == 0
