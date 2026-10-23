"""PLATFORM PASS Workstream 7 — report engine (app.report_engine).

Composition per the workstream spec: create_app + BOTH platform
routers included in the fixture, in-module models created after
import. Capabilities exercised are existing ones only: definition
governance rides studio.manage/studio.read, runs ride reports.run.

Synthetic data only.
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import indicators, report_engine
from app.db import Base
from app.main import create_app
from tests.conftest import ADMIN, CLINICIAN, CONFIG, VIEWER


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db")
    app.include_router(report_engine.router)
    app.include_router(indicators.router)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as c:
        c.test_app = app
        yield c


def _patient(client, pid, gender):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synth", "given": [pid]}],
        "gender": gender, "birthDate": "1980-04-02"}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _seed_patients(client):
    _patient(client, "pat-a", "female")
    _patient(client, "pat-b", "female")
    _patient(client, "pat-c", "male")


def _create(client, key, payload, headers=CONFIG, expect=201):
    r = client.post(f"/report-engine/definitions/{key}", json=payload,
                    headers=headers)
    assert r.status_code == expect, r.text
    return r


def _publish(client, key, payload, headers=CONFIG):
    r = _create(client, key, payload, headers=headers)
    version = r.json()["version"]
    r = client.post(
        f"/report-engine/definitions/{key}/versions/{version}/publish",
        headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


PATIENT_DEF = {"title": "Patients by gender", "dataset": "patients",
               "fields": ["id", "gender"],
               "filters": [{"field": "gender", "op": "eq",
                            "value": "female"}],
               "grouping": ["gender"],
               "formats": ["json", "csv", "pdf"]}


# ---------------------------------------------------------------------------
# Validation — the no-SQL safety decision
# ---------------------------------------------------------------------------

def test_sql_and_query_keys_refused(client):
    payload = dict(PATIENT_DEF, sql="SELECT * FROM patients")
    _create(client, "evil-sql", payload, expect=422)
    payload = {"title": "X", "dataset": "patients", "fields": ["id"],
               "filters": [{"field": "id", "op": "eq", "value": "1",
                            "query": "anything"}]}
    _create(client, "evil-query", payload, expect=422)


def test_unknown_dataset_and_fields_refused(client):
    _create(client, "bad-ds", {"title": "X", "dataset": "everything",
                               "fields": ["id"]}, expect=422)
    _create(client, "bad-field", {"title": "X", "dataset": "patients",
                                  "fields": ["id", "ssn"]}, expect=422)
    _create(client, "bad-filter-field",
            {"title": "X", "dataset": "patients", "fields": ["id"],
             "filters": [{"field": "ssn", "op": "eq", "value": "1"}]},
            expect=422)
    _create(client, "bad-grouping",
            {"title": "X", "dataset": "patients", "fields": ["id"],
             "grouping": ["ssn"]}, expect=422)
    _create(client, "bad-op",
            {"title": "X", "dataset": "patients", "fields": ["id"],
             "filters": [{"field": "id", "op": "regex", "value": "1"}]},
            expect=422)
    _create(client, "bad-cron",
            {"title": "X", "dataset": "patients", "fields": ["id"],
             "schedule": {"cron": "not a cron"}}, expect=422)
    _create(client, "bad-distribution",
            {"title": "X", "dataset": "patients", "fields": ["id"],
             "distribution": [{"type": "email"}]}, expect=422)


def test_datasets_catalogue_lists_fields(client):
    r = client.get("/report-engine/datasets", headers=ADMIN)
    assert r.status_code == 200
    items = {i["key"]: i for i in r.json()["items"]}
    assert {"patients", "appointments", "orders", "lab-results",
            "billing-lines"} <= set(items)
    assert "gender" in items["patients"]["fields"]


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------

def test_draft_publish_governance_and_audit(client):
    # Clinicians cannot author definitions (studio.manage gate).
    _create(client, "pat-def", PATIENT_DEF, headers=CLINICIAN, expect=403)
    r = _create(client, "pat-def", PATIENT_DEF)
    assert r.json()["status"] == "draft"
    # A draft is not runnable and is invisible to a viewer.
    assert client.post("/report-engine/pat-def/run", json={},
                       headers=ADMIN).status_code == 404
    listing = client.get("/report-engine/definitions", headers=VIEWER)
    assert all(i["key"] != "pat-def" for i in listing.json()["items"])
    version = r.json()["version"]
    r = client.post(
        f"/report-engine/definitions/pat-def/versions/{version}/publish",
        headers=CONFIG)
    assert r.status_code == 200 and r.json()["status"] == "published"
    # Published versions are immutable: update -> 409.
    r = client.put(f"/report-engine/definitions/pat-def/versions/{version}",
                   json=PATIENT_DEF, headers=CONFIG)
    assert r.status_code == 409
    audit = client.get("/studio/audit", headers=ADMIN).json()
    actions = {a["action"] for a in audit}
    assert "report-engine.definition.create" in actions
    assert "report-engine.definition.publish" in actions


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def test_run_filtered_grouped_rows_and_counts(client):
    _seed_patients(client)
    _publish(client, "pat-def", PATIENT_DEF)
    r = client.post("/report-engine/pat-def/run", json={}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 2
    assert {row["id"] for row in body["rows"]} == {"pat-a", "pat-b"}
    assert all(set(row) == {"id", "gender"} for row in body["rows"])
    assert len(body["groups"]) == 1
    group = body["groups"][0]
    assert group["key"] == {"gender": "female"}
    assert group["count"] == 2
    assert len(group["rows"]) == 2


def test_run_records_run_and_audit(client):
    _seed_patients(client)
    _publish(client, "pat-def", PATIENT_DEF)
    r = client.post("/report-engine/pat-def/run", json={}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    runs = client.get("/report-engine/runs", headers=ADMIN).json()
    assert runs["total"] == 1
    run = runs["items"][0]
    assert run["definition_key"] == "pat-def"
    assert run["actor"] == "dev-clinician"
    assert run["row_count"] == 2
    assert run["artefact_summary"]["dataset"] == "patients"
    audit = client.get("/studio/audit", headers=ADMIN).json()
    runs_audited = [a for a in audit if a["action"] == "report-engine.run"]
    assert runs_audited
    assert runs_audited[0]["entity"].startswith("report-definition:pat-def")
    assert runs_audited[0]["actor"] == "dev-clinician"


def test_run_csv_and_pdf_formats(client):
    _seed_patients(client)
    _publish(client, "pat-def", PATIENT_DEF)
    r = client.post("/report-engine/pat-def/run",
                    json={"format": "csv"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "pat-a" in r.text and "pat-c" not in r.text
    assert r.headers["x-report-rows"] == "2"
    r = client.post("/report-engine/pat-def/run",
                    json={"format": "pdf"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-"


def test_run_format_not_allowed_and_unknown_param(client):
    _seed_patients(client)
    _publish(client, "json-only", {"title": "J", "dataset": "patients",
                                   "fields": ["id"], "formats": ["json"]})
    r = client.post("/report-engine/json-only/run",
                    json={"format": "pdf"}, headers=ADMIN)
    assert r.status_code == 422
    r = client.post("/report-engine/json-only/run",
                    json={"params": {"bogus": 1}}, headers=ADMIN)
    assert r.status_code == 422


def test_run_filter_ops_in_gte_contains(client):
    _seed_patients(client)
    _publish(client, "ops-def", {
        "title": "Ops", "dataset": "patients", "fields": ["id", "name"],
        "filters": [{"field": "id", "op": "in", "value": ["pat-a", "pat-c"]},
                    {"field": "name", "op": "contains", "value": "Synth"}]})
    r = client.post("/report-engine/ops-def/run", json={}, headers=ADMIN)
    assert {row["id"] for row in r.json()["rows"]} == {"pat-a", "pat-c"}


# ---------------------------------------------------------------------------
# Cron matcher + run-due
# ---------------------------------------------------------------------------

def _dt(minute=0, hour=0, day=18, month=10, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_cron_matcher_subset():
    m = report_engine.cron_matches
    assert m("*/15 * * * *", _dt(minute=30)) is True
    assert m("*/15 * * * *", _dt(minute=31)) is False
    assert m("0,30 9 * * *", _dt(minute=30, hour=9)) is True
    assert m("0,30 9 * * *", _dt(minute=30, hour=10)) is False
    assert m("0 9-17 * * *", _dt(hour=12)) is True
    assert m("0 9-17 * * *", _dt(hour=18)) is False
    assert m("0 0 1 1 *", _dt(day=1, month=1)) is True
    assert m("0 0 1 1 *", _dt(day=18, month=10)) is False
    assert m("* * * * *", _dt(minute=47, hour=23)) is True
    # 2026-10-18 is a Sunday: dow 0 (and 7) match, Monday (1) does not.
    assert m("0 0 * * 0", _dt()) is True
    assert m("0 0 * * 7", _dt()) is True
    assert m("0 0 * * 1", _dt()) is False
    with pytest.raises(ValueError):
        m("bad cron", _dt())
    with pytest.raises(ValueError):
        m("61 * * * *", _dt())


def test_run_due_executes_once_per_minute_with_scheduler_actor(client):
    _seed_patients(client)
    _publish(client, "due-def", {
        "title": "Due", "dataset": "patients", "fields": ["id"],
        "schedule": {"cron": "30 12 * * *"}})
    _publish(client, "not-due-def", {
        "title": "Not due", "dataset": "patients", "fields": ["id"],
        "schedule": {"cron": "0 0 1 1 *"}})
    now = "2026-10-18T12:30:00Z"
    r = client.post("/report-engine/run-due", json={"now": now},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    assert [e["key"] for e in r.json()["executed"]] == ["due-def"]
    # Same minute again: the recorded scheduler run makes it not due.
    r = client.post("/report-engine/run-due", json={"now": now},
                    headers=ADMIN)
    assert r.json()["executed"] == []
    runs = client.get("/report-engine/runs", headers=ADMIN).json()
    assert runs["total"] == 1
    assert runs["items"][0]["actor"] == "scheduler"
    assert runs["items"][0]["definition_key"] == "due-def"


# ---------------------------------------------------------------------------
# Phase 13 mapping
# ---------------------------------------------------------------------------

def test_phase13_mapping_verdicts(client):
    mapping = report_engine.phase13_mapping()
    by_key = {m["query"]: m for m in mapping}
    assert set(by_key) == {"census-by-unit", "claims-summary",
                           "credential-expiry", "safety-events",
                           "payments-summary"}
    # Every Phase 13 query computes an aggregate or reads a source no
    # export dataset carries — none is faithfully expressible, and
    # each verdict carries its reason.
    for entry in mapping:
        assert entry["expressible"] is False
        assert entry["engine_definition"] is None
        assert entry["reason"]
    r = client.get("/report-engine/phase13-mapping", headers=VIEWER)
    assert r.status_code == 200
    assert r.json()["total"] == 5 and r.json()["expressible"] == 0
