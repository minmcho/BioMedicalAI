"""Phase 3 — population health API + warehouse operations endpoints.

Registries are Studio cohort config computed over the OMOP warehouse.
Privacy contract under test: aggregate-only responses, small-cell
suppression on every stratum (and on explorer counts), patient/viewer
roles get 403, unauthenticated gets 401. All data synthetic (seed bundle
+ obviously synthetic additions created through the real API).
"""
import importlib.util
import json
from pathlib import Path

import pytest

from tests.conftest import ADMIN, CLINICIAN, PATIENT, VIEWER

SEED_DIR = Path(__file__).resolve().parent.parent / "seed"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("opencare_seed", SEED_DIR / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def stocked(client, tmp_path, monkeypatch):
    """Seed bundle loaded + ETL drained into a throwaway warehouse DB."""
    monkeypatch.setenv("WAREHOUSE_DATABASE_URL", f"sqlite:///{tmp_path}/wh.db")
    seed = _load_seed_module()
    bundle = json.loads((SEED_DIR / "synthea_bundle.json").read_text())
    seed.load_bundle(bundle, seed.ClientTransport(client, ADMIN))
    r = client.post("/warehouse/etl/run", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["processed"] == 80 and r.json()["dead_letter"] == 0
    return client


# ---------------------------------------------------------------------------
# Role gates
# ---------------------------------------------------------------------------

def test_pophealth_role_gates(client):
    assert client.get("/pophealth/registries").status_code == 401
    assert client.get("/pophealth/registries", headers=PATIENT).status_code == 403
    assert client.get("/pophealth/registries", headers=VIEWER).status_code == 403
    assert client.get("/pophealth/registries", headers=CLINICIAN).status_code == 200
    assert client.post("/pophealth/explore", json={"conditionCode": "44054006"},
                       headers=PATIENT).status_code == 403
    assert client.get("/warehouse/status", headers=PATIENT).status_code == 403
    assert client.get("/warehouse/status", headers=VIEWER).status_code == 403


def test_etl_trigger_is_admin_only(client):
    assert client.post("/warehouse/etl/run").status_code == 401
    assert client.post("/warehouse/etl/run", headers=CLINICIAN).status_code == 403
    assert client.post("/warehouse/etl/run", headers=PATIENT).status_code == 403
    r = client.post("/warehouse/etl/run", headers=ADMIN)
    assert r.status_code == 200 and r.json()["processed"] == 0


# ---------------------------------------------------------------------------
# Registry summaries (hand-computed from the seed bundle)
# ---------------------------------------------------------------------------

def test_registries_listed_from_studio_config(stocked):
    r = stocked.get("/pophealth/registries", headers=CLINICIAN)
    assert r.status_code == 200
    keys = {entry["key"] for entry in r.json()}
    assert {"diabetes", "hypertension"} <= keys
    diabetes = next(e for e in r.json() if e["key"] == "diabetes")
    assert diabetes["definition"]["lab"]["code"] == "4548-4"


def test_diabetes_summary_matches_hand_computed(stocked):
    r = stocked.get("/pophealth/registries/diabetes/summary", headers=CLINICIAN)
    assert r.status_code == 200
    body = r.json()
    # Seed: T2DM on pat-1 + pat-4 only.
    assert body["member_count"] == 2
    # Latest HbA1c: pat-1 6.9 (< 7.0 in range), pat-4 7.6 (not) -> 50.0%.
    assert body["lab"]["members_with_current_result"] == 2
    assert body["lab"]["pct_in_range"] == 50.0
    # No patient-level identifiers anywhere in the payload.
    serialized = json.dumps(body)
    assert "pat-1" not in serialized and "pat-4" not in serialized
    strata = {s["value"]: s for s in body["stratification"]["by_gender"]}
    assert strata["female"] == {"value": "female", "count": "<5", "suppressed": True}
    assert strata["male"] == {"value": "male", "count": "<5", "suppressed": True}
    age = {s["value"]: s for s in body["stratification"]["by_age_band"]}
    # As of the latest warehouse activity (2025): pat-1 is 57, pat-4 is 51.
    assert list(age) == ["45-64"]
    assert age["45-64"]["count"] == "<5" and age["45-64"]["suppressed"] is True


def test_hypertension_summary(stocked):
    r = stocked.get("/pophealth/registries/hypertension/summary", headers=CLINICIAN)
    body = r.json()
    assert body["member_count"] == 3  # pat-1, pat-2, pat-6
    assert body["lab"] is None


def test_unknown_registry_is_404(stocked):
    assert stocked.get("/pophealth/registries/nope/summary",
                       headers=CLINICIAN).status_code == 404


def test_registry_can_be_defined_via_studio_config(stocked):
    payload = {"name": "Asthma registry",
               "conditionCodes": [{"system": "http://snomed.info/sct",
                                   "code": "195967001"}],
               "smallCellThreshold": 5}
    r = stocked.post("/studio/config/cohort/asthma", json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = stocked.post("/studio/config/cohort/asthma/versions/1/publish", headers=ADMIN)
    assert r.status_code == 200, r.text
    r = stocked.get("/pophealth/registries/asthma/summary", headers=CLINICIAN)
    assert r.status_code == 200
    assert r.json()["member_count"] == 1  # pat-2 only


# ---------------------------------------------------------------------------
# Cohort explorer + suppression
# ---------------------------------------------------------------------------

def test_explorer_suppresses_small_counts(stocked):
    r = stocked.post("/pophealth/explore", json={"conditionCode": "38341003"},
                     headers=CLINICIAN)
    assert r.status_code == 200
    assert r.json()["count"] == "<5" and r.json()["suppressed"] is True

    # Latest-HbA1c > 7.0 matches pat-4 alone -> still suppressed.
    r = stocked.post("/pophealth/explore",
                     json={"conditionCode": "44054006", "labCode": "4548-4",
                           "comparator": ">", "threshold": 7.0},
                     headers=CLINICIAN)
    assert r.json()["count"] == "<5" and r.json()["suppressed"] is True


def test_explorer_reveals_counts_at_or_above_threshold(stocked):
    # Add 3 more synthetic hypertensive patients (3 seeded + 3 = 6 >= 5).
    for i in range(3):
        pid = f"extra-htn-{i}"
        patient = {"resourceType": "Patient", "id": pid,
                   "name": [{"family": "Synthetic", "given": [f"Extra{i}"]}]}
        assert stocked.post("/fhir/Patient", json=patient,
                            headers=CLINICIAN).status_code == 201
        condition = {"resourceType": "Condition", "id": f"cond-{pid}",
                     "code": {"coding": [{"system": "http://snomed.info/sct",
                                          "code": "38341003",
                                          "display": "Hypertensive disorder"}]},
                     "subject": {"reference": f"Patient/{pid}"}}
        assert stocked.post("/fhir/Condition", json=condition,
                            headers=CLINICIAN).status_code == 201
    r = stocked.post("/warehouse/etl/run", headers=ADMIN)
    assert r.status_code == 200 and r.json()["dead_letter"] == 0
    r = stocked.post("/pophealth/explore", json={"conditionCode": "38341003"},
                     headers=CLINICIAN)
    assert r.json() == {"filters": {"conditionCode": "38341003", "labCode": None,
                                    "comparator": None, "threshold": None},
                        "count": 6, "suppressed": False}


def test_explorer_requires_a_filter(stocked):
    r = stocked.post("/pophealth/explore", json={}, headers=CLINICIAN)
    assert r.status_code == 422
    r = stocked.post("/pophealth/explore", json={"labCode": "4548-4"},
                     headers=CLINICIAN)
    assert r.status_code == 422  # lab without threshold


# ---------------------------------------------------------------------------
# Warehouse status
# ---------------------------------------------------------------------------

def test_warehouse_status_reports_outbox_tables_and_coverage(stocked):
    r = stocked.get("/warehouse/status", headers=CLINICIAN)
    assert r.status_code == 200
    body = r.json()
    assert body["outbox"] == {"pending": 0, "processed": 80, "dead_letter": 0}
    assert body["dead_letters"] == []
    assert body["warehouse"]["person"] == 6
    assert body["warehouse"]["measurement"] == 32
    assert body["mapping_coverage"]["unmapped_rows"] == 0
    assert body["mapping_coverage"]["coverage_pct"] == 100.0
