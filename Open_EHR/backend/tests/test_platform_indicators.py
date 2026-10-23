"""PLATFORM PASS Workstream 8 — indicator library (app.indicators).

Composition per the workstream spec: create_app + BOTH platform
routers in the fixture. Measure-backed values reuse the Phase 9
seeding approach (synthetic FHIR population -> ETL -> OMOP
warehouse). Named values move with live module state created through
the public APIs.

Synthetic data only.
"""
import pytest
from fastapi.testclient import TestClient

from app import indicators, report_engine
from app.db import Base
from app.main import create_app
from tests.conftest import ADMIN, CLINICIAN, CONFIG, VIEWER

SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
T2DM = "44054006"


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db")
    app.include_router(report_engine.router)
    app.include_router(indicators.router)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as c:
        c.test_app = app
        yield c


def _create(client, key, payload, headers=CONFIG, expect=201):
    r = client.post(f"/indicators/definitions/{key}", json=payload,
                    headers=headers)
    assert r.status_code == expect, r.text
    return r


def _publish(client, key, payload, headers=CONFIG):
    r = _create(client, key, payload, headers=headers)
    version = r.json()["version"]
    r = client.post(
        f"/indicators/definitions/{key}/versions/{version}/publish",
        headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _value(client, key, headers=CLINICIAN):
    r = client.get(f"/indicators/{key}/value", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


CENSUS_DEF = {"name": "Current census",
              "source": {"type": "named", "name": "census.current"},
              "direction": "lower-better", "unit": "patients"}


# ---------------------------------------------------------------------------
# Validation + governance
# ---------------------------------------------------------------------------

def test_validation_rules(client):
    _create(client, "bad-dir", {"name": "X", "direction": "sideways",
                                "source": {"type": "named",
                                           "name": "census.current"}},
            expect=422)
    _create(client, "bad-source", {"name": "X", "direction": "lower-better",
                                   "source": {"type": "sql",
                                              "name": "census.current"}},
            expect=422)
    _create(client, "bad-named", {"name": "X", "direction": "lower-better",
                                  "source": {"type": "named",
                                             "name": "census.made-up"}},
            expect=422)
    _create(client, "bad-threshold",
            {"name": "X", "direction": "lower-better",
             "source": {"type": "named", "name": "census.current"},
             "thresholds": {"warn": "soon"}}, expect=422)
    _create(client, "bad-measure-key",
            {"name": "X", "direction": "higher-better",
             "source": {"type": "measure"}}, expect=422)


def test_governance_draft_publish_and_audit(client):
    _create(client, "census-ind", CENSUS_DEF, headers=CLINICIAN, expect=403)
    r = _create(client, "census-ind", CENSUS_DEF)
    assert r.json()["status"] == "draft"
    # Drafts have no value endpoint and are invisible to viewers.
    assert client.get("/indicators/census-ind/value",
                      headers=ADMIN).status_code == 404
    listing = client.get("/indicators/definitions", headers=VIEWER)
    assert all(i["key"] != "census-ind" for i in listing.json()["items"])
    version = r.json()["version"]
    r = client.post(
        f"/indicators/definitions/census-ind/versions/{version}/publish",
        headers=CONFIG)
    assert r.status_code == 200 and r.json()["status"] == "published"
    audit = client.get("/studio/audit", headers=ADMIN).json()
    actions = {a["action"] for a in audit}
    assert "indicator.definition.create" in actions
    assert "indicator.definition.publish" in actions


def test_seed_payloads_mirror_operations_overview():
    seeds = indicators.default_indicator_payloads()
    assert set(seeds) == {"operations-census", "operations-critical-open",
                          "operations-verification-queue"}
    named = {p["source"]["name"] for p in seeds.values()}
    assert named == {"census.current", "lab.critical-open",
                     "lab.pending-verification"}
    for payload in seeds.values():
        indicators.validate_indicator(payload)  # seeds are valid defs


# ---------------------------------------------------------------------------
# Named sources against live state
# ---------------------------------------------------------------------------

def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synth", "given": [pid]}],
        "gender": "female", "birthDate": "1980-05-01"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _admit(client, patient, bed):
    _patient(client, patient)
    r = client.post("/inpatient/admissions",
                    json={"patient": patient, "bed": bed},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text


def test_named_census_moves_with_admissions_and_thresholds(client):
    _publish(client, "census-ind", {
        **CENSUS_DEF, "thresholds": {"warn": 1, "critical": 2}})
    out = _value(client, "census-ind")
    assert out["value"] == 0 and out["status"] == "ok"  # genuine zero
    _admit(client, "pat-c1", "MS-101A")
    out = _value(client, "census-ind")
    assert out["value"] == 1 and out["status"] == "warn"
    _admit(client, "pat-c2", "MS-101B")
    out = _value(client, "census-ind")
    assert out["value"] == 2 and out["status"] == "critical"
    assert out["unit"] == "patients"
    assert out["source"] == {"type": "named", "name": "census.current"}


def test_named_lab_pending_verification_and_safety_counts(client):
    _publish(client, "lab-queue", {
        "name": "Lab pending verification",
        "source": {"type": "named", "name": "lab.pending-verification"},
        "direction": "lower-better"})
    assert _value(client, "lab-queue")["value"] == 0
    _patient(client, "pat-l1")
    r = client.post("/fhir/ServiceRequest", json={
        "resourceType": "ServiceRequest", "id": "sr-plat-1",
        "status": "resulted", "intent": "order",
        "code": {"coding": [{"system": LOINC, "code": "2345-7"}]},
        "subject": {"reference": "Patient/pat-l1"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/fhir/DiagnosticReport", json={
        "resourceType": "DiagnosticReport", "id": "dr-plat-1",
        "status": "preliminary",
        "code": {"coding": [{"system": LOINC, "code": "2345-7"}]},
        "basedOn": [{"reference": "ServiceRequest/sr-plat-1"}],
        "subject": {"reference": "Patient/pat-l1"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert _value(client, "lab-queue")["value"] == 1

    _publish(client, "safety-open", {
        "name": "Open safety events",
        "source": {"type": "named", "name": "safety.open-events"},
        "direction": "lower-better"})
    assert _value(client, "safety-open")["value"] == 0
    from app.models import SafetyEvent
    with client.test_app.state.session_factory() as session:
        session.add(SafetyEvent(event_id="SE-PLAT-1", reporter="dev-admin",
                                category="fall", harm_level="none",
                                status="reported"))
        session.add(SafetyEvent(event_id="SE-PLAT-2", reporter="dev-admin",
                                category="fall", harm_level="none",
                                status="closed"))
        session.commit()
    # Only the non-closed event counts.
    assert _value(client, "safety-open")["value"] == 1


def test_higher_better_threshold_direction(client):
    _publish(client, "census-hb", {
        "name": "Census HB", "source": {"type": "named",
                                        "name": "census.current"},
        "direction": "higher-better",
        "thresholds": {"warn": 2, "critical": 1}, "target": 3})
    out = _value(client, "census-hb")
    # Value 0 <= critical 1 for a higher-better indicator.
    assert out["value"] == 0 and out["status"] == "critical"
    assert out["target"] == 3


# ---------------------------------------------------------------------------
# Measure + cohort sources over the warehouse (Phase 9 seeding pattern)
# ---------------------------------------------------------------------------

def _fhir_patient(client, pid, birth_date):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid, "birthDate": birth_date,
        "name": [{"family": "Synth", "given": [pid]}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _condition(client, pid, code):
    r = client.post("/fhir/Condition", json={
        "resourceType": "Condition",
        "code": {"coding": [{"system": SNOMED, "code": code}]},
        "subject": {"reference": f"Patient/{pid}"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _observation(client, pid, code, value, when):
    r = client.post("/fhir/Observation", json={
        "resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": LOINC, "code": code}]},
        "subject": {"reference": f"Patient/{pid}"},
        "effectiveDateTime": when,
        "valueQuantity": {"value": value, "unit": "%"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


@pytest.fixture()
def warehouse_data(client, monkeypatch, tmp_path):
    monkeypatch.setenv("WAREHOUSE_DATABASE_URL",
                       f"sqlite:///{tmp_path}/wh.db")
    # 10 persons with T2DM; 6 with latest HbA1c < 8.0 -> rate 60.0
    # (the Phase 9 hba1c-control measure: denominator T2DM, numerator
    # latest HbA1c < 8.0 within 365 days).
    a1c = [6.1, 6.5, 7.0, 7.2, 7.9, 6.8, 8.4, 9.1, 8.0, 9.5]
    for i, value in enumerate(a1c, start=1):
        pid = f"dm-{i:02d}"
        _fhir_patient(client, pid, "1995-06-15")
        _condition(client, pid, T2DM)
        _observation(client, pid, "4548-4", value, "2026-01-10T09:00:00Z")
    r = client.post("/warehouse/etl/run", headers=ADMIN)
    assert r.status_code == 200 and r.json()["dead_letter"] == 0, r.text
    return client


def test_measure_backed_value_and_status(warehouse_data):
    _publish(warehouse_data, "a1c-ind", {
        "name": "HbA1c control rate",
        "source": {"type": "measure", "measure_key": "hba1c-control"},
        "direction": "higher-better",
        "thresholds": {"warn": 70, "critical": 50}, "target": 80})
    out = _value(warehouse_data, "a1c-ind")
    assert out["value"] == 60.0
    assert out["unit"] == "%"
    assert out["status"] == "warn"  # 60 <= warn 70, above critical 50
    assert out["detail"]["denominator"] == 10
    assert out["detail"]["numerator"] == 6


def test_cohort_backed_value(warehouse_data):
    _publish(warehouse_data, "dm-cohort-ind", {
        "name": "Diabetes registry size",
        "source": {"type": "cohort", "cohort_key": "diabetes"},
        "direction": "higher-better"})
    out = _value(warehouse_data, "dm-cohort-ind")
    assert out["value"] == 10
    assert out["unit"] == "persons"
    assert out["status"] == "ok"


def test_unknown_source_handling(warehouse_data):
    # A measure key with no published measure config: the indicator
    # exists and evaluates — to unknown, never a fabricated number.
    _publish(warehouse_data, "ghost-ind", {
        "name": "Ghost measure",
        "source": {"type": "measure", "measure_key": "no-such-measure"},
        "direction": "higher-better"})
    out = _value(warehouse_data, "ghost-ind")
    assert out["value"] is None
    assert out["status"] == "unknown"
    assert "no published measure" in out["detail"]["reason"]
    # An unpublished indicator key is a plain 404.
    assert warehouse_data.get("/indicators/nope/value",
                              headers=CLINICIAN).status_code == 404
