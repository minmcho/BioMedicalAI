"""Phase 3 — transactional outbox + FHIR->OMOP ETL.

Covers: outbox rows written in the same transaction as resource writes
(central hook, incl. lab-free generic paths), ETL end-to-end against the
synthetic seed bundle with hand-computed OMOP expectations, idempotency,
update propagation, dead-letter behaviour, and unmapped-code accounting.
Synthetic data only."""
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app import etl, warehouse as wh
from app.models import OutboxEvent
from tests.conftest import ADMIN, CLINICIAN

SEED_DIR = Path(__file__).resolve().parent.parent / "seed"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("opencare_seed", SEED_DIR / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundle():
    return json.loads((SEED_DIR / "synthea_bundle.json").read_text())


def _seed(client):
    seed = _load_seed_module()
    return seed.load_bundle(_bundle(), seed.ClientTransport(client, ADMIN))


@pytest.fixture()
def wh_factory(tmp_path):
    return wh.warehouse_session_factory(f"sqlite:///{tmp_path}/warehouse.db")


def _clinical_factory(client):
    return client.app.state.session_factory


def _pending(client):
    with _clinical_factory(client)() as s:
        return s.scalars(select(OutboxEvent).where(
            OutboxEvent.status == "pending")).all()


def _counts(factory):
    with factory() as s:
        return wh.table_counts(s)


# ---------------------------------------------------------------------------
# Outbox: written centrally, in the same transaction as the write
# ---------------------------------------------------------------------------

def test_outbox_written_on_create_and_update(client):
    body = {"resourceType": "Patient", "id": "ob-1",
            "name": [{"family": "Synthetic", "given": ["Outbox"]}]}
    r = client.post("/fhir/Patient", json=body, headers=CLINICIAN)
    assert r.status_code == 201
    events = [e for e in _pending(client) if e.resource_id == "ob-1"]
    assert len(events) == 1
    assert events[0].resource_type == "Patient" and events[0].version == 1
    assert events[0].payload["name"][0]["family"] == "Synthetic"

    # Update (same id) -> a second event carrying version 2's snapshot.
    r = client.post("/fhir/Patient", json=dict(body, gender="female"),
                    headers=CLINICIAN)
    assert r.status_code == 201 and r.json()["meta"]["versionId"] == "2"
    events = sorted((e for e in _pending(client) if e.resource_id == "ob-1"),
                    key=lambda e: e.id)
    assert [e.version for e in events] == [1, 2]
    assert events[1].payload["gender"] == "female"

    # Appointment is NOT a watched warehouse type -> no event.
    appt = {"resourceType": "Appointment", "id": "ob-appt", "status": "booked",
            "participant": [{"actor": {"reference": "Patient/ob-1"}}]}
    assert client.post("/fhir/Appointment", json=appt,
                       headers=CLINICIAN).status_code == 201
    assert [e for e in _pending(client) if e.resource_id == "ob-appt"] == []


# ---------------------------------------------------------------------------
# ETL end-to-end on the synthetic seed (hand-computed expectations)
# ---------------------------------------------------------------------------

def test_etl_end_to_end_on_seed(client, wh_factory):
    result = _seed(client)
    assert result["created"] == 86
    # Watched types in the bundle: 6 Patient + 12 Encounter + 10 Condition
    # + 32 Observation + 9 MedicationRequest + 5 Procedure
    # + 6 DiagnosticReport = 80 events (6 Appointments are not watched).
    assert len(_pending(client)) == 80

    stats = etl.run_etl(_clinical_factory(client), wh_factory)
    assert stats["processed"] == 80
    assert stats["dead_letter"] == 0
    assert stats["no_target"] == 6  # the 6 DiagnosticReports
    assert _pending(client) == []

    counts = _counts(wh_factory)
    assert counts == {"person": 6, "visit_occurrence": 12,
                      "condition_occurrence": 10, "measurement": 32,
                      "observation": 0, "drug_exposure": 9,
                      "procedure_occurrence": 5}

    with wh_factory() as s:
        # Diabetes: exactly pat-1 and pat-4 carry SNOMED 44054006.
        dm_persons = s.scalars(
            select(wh.Person.source_patient_id)
            .join(wh.ConditionOccurrence,
                  wh.ConditionOccurrence.person_id == wh.Person.person_id)
            .where(wh.ConditionOccurrence.condition_source_value == "44054006")
            .distinct()).all()
        assert sorted(dm_persons) == ["pat-1", "pat-4"]
        # ...mapped to the standard OMOP concept 201826.
        concept_ids = s.scalars(
            select(wh.ConditionOccurrence.condition_concept_id)
            .where(wh.ConditionOccurrence.condition_source_value == "44054006")
            .distinct()).all()
        assert concept_ids == [201826]
        # Latest HbA1c per diabetic (hand-computed from the bundle):
        # pat-1 -> 6.9 (2025-10-15), pat-4 -> 7.6 (2025-09-20).
        rows = s.execute(
            select(wh.Person.source_patient_id, wh.Measurement.value_as_number,
                   wh.Measurement.measurement_datetime)
            .join(wh.Measurement, wh.Measurement.person_id == wh.Person.person_id)
            .where(wh.Measurement.measurement_source_value == "4548-4")).all()
        latest = {}
        for pid, value, when in rows:
            if pid not in latest or when > latest[pid][0]:
                latest[pid] = (when, value)
        assert {p: v for p, (_, v) in latest.items()} == {"pat-1": 6.9, "pat-4": 7.6}
        # Every seed code is in the curated map -> full coverage.
        coverage = wh.mapping_coverage(s)
        assert coverage["unmapped_rows"] == 0
        assert coverage["coverage_pct"] == 100.0


def test_etl_second_run_is_idempotent(client, wh_factory):
    _seed(client)
    etl.run_etl(_clinical_factory(client), wh_factory)
    before = _counts(wh_factory)
    stats = etl.run_etl(_clinical_factory(client), wh_factory)
    assert stats == {"processed": 0, "no_target": 0, "dead_letter": 0, "by_type": {}}
    assert _counts(wh_factory) == before


def test_etl_update_propagates_to_warehouse(client, wh_factory):
    _seed(client)
    etl.run_etl(_clinical_factory(client), wh_factory)
    # Change obs-1 (pat-1 HbA1c 7.8 -> 8.8) through the real API.
    obs = next(e["resource"] for e in _bundle()["entry"]
               if e["resource"].get("id") == "obs-1")
    obs = json.loads(json.dumps(obs))
    obs["valueQuantity"]["value"] = 8.8
    r = client.post("/fhir/Observation", json=obs, headers=CLINICIAN)
    assert r.status_code == 201

    stats = etl.run_etl(_clinical_factory(client), wh_factory)
    assert stats["processed"] == 1 and stats["dead_letter"] == 0
    with wh_factory() as s:
        row = s.scalars(select(wh.Measurement).where(
            wh.Measurement.source_observation_id == "obs-1")).first()
        assert row is not None and row.value_as_number == 8.8
        # Upsert, not duplicate: still 32 measurements.
        assert wh.table_counts(s)["measurement"] == 32


def test_etl_dead_letter_records_error_and_continues(client, wh_factory):
    _seed(client)
    etl.run_etl(_clinical_factory(client), wh_factory)
    factory = _clinical_factory(client)
    with factory() as s:
        # Poisoned: an Observation snapshot with no subject reference.
        s.add(OutboxEvent(resource_type="Observation", resource_id="obs-poison",
                          version=1, status="pending",
                          payload={"resourceType": "Observation", "id": "obs-poison",
                                   "code": {"coding": [{"system": "http://loinc.org",
                                                        "code": "2345-7"}]}}))
        s.commit()
    # A healthy event AFTER the poisoned one must still be processed.
    body = {"resourceType": "Patient", "id": "after-poison",
            "name": [{"family": "Synthetic", "given": ["Survivor"]}]}
    assert client.post("/fhir/Patient", json=body, headers=CLINICIAN).status_code == 201

    stats = etl.run_etl(factory, wh_factory)
    assert stats["dead_letter"] == 1 and stats["processed"] == 1
    with factory() as s:
        dead = s.scalars(select(OutboxEvent).where(
            OutboxEvent.status == "dead_letter")).all()
        assert len(dead) == 1
        assert dead[0].resource_id == "obs-poison"
        assert dead[0].error and "subject" in dead[0].error
        assert dead[0].attempts == 1
    assert _counts(wh_factory)["person"] == 7


def test_etl_unmapped_code_lands_zero_and_is_counted(client, wh_factory):
    _seed(client)
    obs = {"resourceType": "Observation", "id": "obs-unmapped", "status": "final",
           "code": {"coding": [{"system": "http://loinc.org", "code": "99999-9",
                                "display": "Synthetic unmapped analyte"}]},
           "subject": {"reference": "Patient/pat-1"},
           "effectiveDateTime": "2025-11-01T08:15:00Z",
           "valueQuantity": {"value": 42, "unit": "mg/dL"}}
    assert client.post("/fhir/Observation", json=obs,
                       headers=CLINICIAN).status_code == 201
    stats = etl.run_etl(_clinical_factory(client), wh_factory)
    assert stats["dead_letter"] == 0
    with wh_factory() as s:
        row = s.scalars(select(wh.Measurement).where(
            wh.Measurement.source_observation_id == "obs-unmapped")).first()
        assert row is not None
        assert row.measurement_concept_id == 0
        assert row.measurement_source_value == "99999-9"
        coverage = wh.mapping_coverage(s)
        assert coverage["tables"]["measurement"]["unmapped"] == 1
        assert coverage["unmapped_rows"] == 1
