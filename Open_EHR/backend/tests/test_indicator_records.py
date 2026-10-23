"""WS4a (Home & Module UX pass) — indicator records drill-down.

GET /indicators/{key}/records: every reported number drills to its
definition -> the underlying record list -> the individual record.
For count-type named sources len(records) == value, by construction
(the resolvers reuse the counters' own membership rules). Warehouse
measure/cohort sources honestly report records_supported=false.

Synthetic data only, created through the real APIs.
"""
import pytest
from fastapi.testclient import TestClient

from app import indicators, report_engine
from app.db import Base
from app.main import create_app
from tests.conftest import ADMIN, CLINICIAN, CONFIG, VIEWER

LOINC = "http://loinc.org"
BMP = "51990-0"
POTASSIUM = "2823-3"
BMP_RESULTS = [
    {"code": "2345-7", "value": 95},
    {"code": "3094-0", "value": 12},
    {"code": "2160-0", "value": 1.0},
    {"code": "2951-2", "value": 130},
    {"code": POTASSIUM, "value": 6.4},   # critical high (HH)
]


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db")
    app.include_router(report_engine.router)
    app.include_router(indicators.router)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as c:
        c.test_app = app
        yield c


def _publish(client, key, payload, headers=CONFIG):
    r = client.post(f"/indicators/definitions/{key}", json=payload,
                    headers=headers)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/indicators/definitions/{key}/versions/{version}/publish",
        headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _records(client, key, headers=CLINICIAN):
    r = client.get(f"/indicators/{key}/records", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synth", "given": [pid]}],
        "gender": "female", "birthDate": "1980-05-01"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _publish_seeds(client):
    for key, payload in indicators.default_indicator_payloads().items():
        _publish(client, key, payload)


# ---------------------------------------------------------------------------
# Registry completeness — a future named source without a resolver (or
# an explicit unsupported entry) fails HERE, not silently in production
# ---------------------------------------------------------------------------

def test_every_named_source_has_resolver_or_explicit_unsupported():
    assert set(indicators.NAMED_RECORD_RESOLVERS) == set(indicators.NAMED_SOURCES)
    for name, resolver in indicators.NAMED_RECORD_RESOLVERS.items():
        if resolver is None:
            assert name in indicators.NAMED_RECORD_UNSUPPORTED, name
            assert indicators.NAMED_RECORD_UNSUPPORTED[name].strip()
        else:
            assert callable(resolver), name


# ---------------------------------------------------------------------------
# Seeded indicators: definition + value + records, count == value
# ---------------------------------------------------------------------------

def test_seeded_indicators_records_shape_and_empty_counts(client):
    _publish_seeds(client)
    for key in ("operations-census", "operations-critical-open",
                "operations-verification-queue"):
        out = _records(client, key)
        assert out["key"] == key
        assert out["definition"] == indicators.default_indicator_payloads()[key]
        assert out["value"]["key"] == key
        assert out["value"]["value"] == 0       # genuine zero, no data yet
        assert out["records"] == []
        assert out["records_supported"] is True
        assert out["reason"] is None
    # studio.read capability is required (same as the value endpoint).
    assert client.get("/indicators/operations-census/records").status_code == 401


def test_unknown_key_records_404(client):
    _publish_seeds(client)
    r = client.get("/indicators/nope/records", headers=CLINICIAN)
    assert r.status_code == 404
    # A draft (unpublished) indicator 404s too, exactly like /value.
    r = client.post("/indicators/definitions/drafty",
                    json=indicators.default_indicator_payloads()
                    ["operations-census"], headers=CONFIG)
    assert r.status_code == 201, r.text
    assert client.get("/indicators/drafty/records",
                      headers=ADMIN).status_code == 404


# ---------------------------------------------------------------------------
# census.current — admit a patient, the encounter IS the record
# ---------------------------------------------------------------------------

def test_census_records_include_admission_encounter(client):
    _publish_seeds(client)
    _patient(client, "pat-drill-1")
    r = client.post("/inpatient/admissions",
                    json={"patient": "pat-drill-1", "bed": "MS-101A"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    encounter_id = r.json()["encounter"]["id"]

    out = _records(client, "operations-census")
    assert out["value"]["value"] == 1
    assert len(out["records"]) == out["value"]["value"]
    rec = out["records"][0]
    assert rec["id"] == encounter_id
    assert rec["patient_id"] == "pat-drill-1"
    assert "MS-101A" in rec["label"] or "MS-101A" in rec["detail"]
    assert rec["at"] is not None

    _patient(client, "pat-drill-2")
    r = client.post("/inpatient/admissions",
                    json={"patient": "pat-drill-2", "bed": "MS-101B"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    out = _records(client, "operations-census")
    assert out["value"]["value"] == 2
    assert len(out["records"]) == 2
    assert {rec["patient_id"] for rec in out["records"]} == {
        "pat-drill-1", "pat-drill-2"}


# ---------------------------------------------------------------------------
# lab.pending-verification + lab.critical-open — real lab flow:
# order -> collected -> received -> critical results -> verify -> release
# ---------------------------------------------------------------------------

def _critical_lab_report_released(client, pid="pat-drill-lab"):
    _patient(client, pid)
    r = client.post("/lab/orders",
                    json={"code": BMP,
                          "subject": {"reference": f"Patient/{pid}"}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    order = r.json()
    for to_status in ("collected", "received"):
        r = client.post(f"/lab/orders/{order['id']}/transition",
                        json={"to_status": to_status}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    r = client.post(f"/lab/orders/{order['id']}/results",
                    json={"results": BMP_RESULTS}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    outcome = r.json()
    assert outcome["critical"] is True
    report_id = outcome["report"]["id"]
    return order, report_id


def test_lab_queue_records_track_the_report_lifecycle(client):
    _publish_seeds(client)
    order, report_id = _critical_lab_report_released(client)

    # Resulted (not yet verified/released): on the verification queue,
    # NOT yet on critical-open (its rule requires a released order).
    out = _records(client, "operations-verification-queue")
    assert out["value"]["value"] == 1
    assert len(out["records"]) == out["value"]["value"]
    rec = out["records"][0]
    assert rec["id"] == report_id
    assert rec["patient_id"] == "pat-drill-lab"
    assert order["id"] in rec["detail"]
    crit = _records(client, "operations-critical-open")
    assert crit["value"]["value"] == 0 and crit["records"] == []

    # Verify + release: leaves the verification queue, joins
    # critical-open — pre-acknowledgement (no notify/ack has happened).
    assert client.post(f"/lab/reports/{report_id}/verify",
                       headers=CLINICIAN).status_code == 200
    out = _records(client, "operations-verification-queue")
    assert out["value"]["value"] == 1  # 'verified' is still queue membership
    assert client.post(f"/lab/reports/{report_id}/release",
                       headers=CLINICIAN).status_code == 200
    out = _records(client, "operations-verification-queue")
    assert out["value"]["value"] == 0 and out["records"] == []

    crit = _records(client, "operations-critical-open")
    assert crit["value"]["value"] == 1
    assert len(crit["records"]) == crit["value"]["value"]
    rec = crit["records"][0]
    assert rec["id"] == report_id
    assert rec["patient_id"] == "pat-drill-lab"
    assert "critical" in rec["detail"]


# ---------------------------------------------------------------------------
# Warehouse sources: honest unsupported, never fake rows
# ---------------------------------------------------------------------------

def test_measure_definition_records_unsupported_with_reason(client):
    _publish(client, "ghost-measure-ind", {
        "name": "Ghost measure",
        "source": {"type": "measure", "measure_key": "no-such-measure"},
        "direction": "higher-better"})
    out = _records(client, "ghost-measure-ind")
    assert out["records_supported"] is False
    assert out["records"] == []
    assert "warehouse" in out["reason"]
    assert "Analytics" in out["reason"]
    # The embedded value still evaluates honestly (unknown, not faked).
    assert out["value"]["value"] is None
    assert out["value"]["status"] == "unknown"


def test_cohort_definition_records_unsupported_with_reason(client):
    _publish(client, "ghost-cohort-ind", {
        "name": "Ghost cohort",
        "source": {"type": "cohort", "cohort_key": "no-such-cohort"},
        "direction": "higher-better"})
    out = _records(client, "ghost-cohort-ind")
    assert out["records_supported"] is False
    assert out["records"] == []
    assert "warehouse" in out["reason"]


def test_coverage_pct_is_explicitly_unsupported(client):
    _publish(client, "coverage-ind", {
        "name": "Coverage",
        "source": {"type": "named", "name": "workforce.coverage-pct"},
        "direction": "higher-better"})
    out = _records(client, "coverage-ind")
    assert out["records_supported"] is False
    assert out["records"] == []
    assert "fill rate" in out["reason"]


def test_occupancy_pct_returns_numerator_bed_records(client):
    _publish(client, "occupancy-ind", {
        "name": "Occupancy",
        "source": {"type": "named", "name": "workforce.occupancy-pct"},
        "direction": "lower-better", "unit": "%"})
    _patient(client, "pat-occ-1")
    r = client.post("/inpatient/admissions",
                    json={"patient": "pat-occ-1", "bed": "MS-101A"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    out = _records(client, "occupancy-ind")
    assert out["records_supported"] is True
    assert out["value"]["value"] > 0
    # Numerator-side convention: one record per occupied bed, and the
    # reason says exactly that (len != the percentage value itself).
    assert len(out["records"]) == 1
    assert out["records"][0]["patient_id"] == "pat-occ-1"
    assert "numerator" in out["reason"]
