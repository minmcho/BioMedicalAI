"""Phase 14 — pipelines as governed Studio config (Track I).

Pipeline definitions are config kind `pipeline`: ordered steps from a
governed vocabulary, optional cron schedule, enabled flag, per-step
retry and data-quality checks. The runner provides idempotency keys
(a re-run over the same scope reuses succeeded steps), retry with
backoff, dead-letter on exhausted failure, backfill/replay over the
raw-zone lake, DQ quarantine with audited release/reject, terminology
normalisation that REPORTS unmapped codes, and an MPI dedup scan that
produces human-review candidates and never merges.

Synthetic data only.
"""
import os

import pytest

from .conftest import ADMIN, CLINICIAN, VIEWER


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # The warehouse step writes to a real warehouse DB — point it at a
    # temp file so the suite never touches shared state.
    monkeypatch.setenv("WAREHOUSE_DATABASE_URL",
                       f"sqlite:///{tmp_path}/warehouse.db")
    from fastapi.testclient import TestClient

    from app.main import create_app
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
    with TestClient(app) as c:
        yield c


def _publish(client, kind, key, payload):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    return version


def _patient(client, pid, family="Smith", given="Anna", dob="1980-03-04",
             phone=None, gender="female"):
    body = {"resourceType": "Patient", "id": pid,
            "name": [{"family": family, "given": [given]}],
            "gender": gender, "birthDate": dob}
    if phone:
        body["telecom"] = [{"system": "phone", "value": phone}]
    r = client.post("/fhir/Patient", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text


def _run(client, key, **kw):
    r = client.post(f"/pipelines/{key}/run", json=kw, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Definition governance
# ---------------------------------------------------------------------------

def test_seeded_pipeline_definitions(client):
    r = client.get("/pipelines/definitions", headers=ADMIN)
    assert r.status_code == 200
    keys = {d["key"] for d in r.json()["items"]}
    # Platform pass: the seed set grows by "deidentify-export" (the
    # Rust de-identification pipeline; ships disabled like lake-replay
    # because the opencare-deid binary must be installed first).
    assert keys == {"nightly-warehouse-load", "terminology-normalisation",
                    "mpi-dedup-scan", "lake-replay", "deidentify-export"}
    replay = client.get("/pipelines/definitions/lake-replay",
                        headers=ADMIN).json()
    assert replay["enabled"] is False  # ships disabled
    deid = client.get("/pipelines/definitions/deidentify-export",
                      headers=ADMIN).json()
    assert deid["enabled"] is False  # ships disabled


def test_pipeline_definition_validation(client):
    r = client.post("/studio/config/pipeline/bad-step",
                    json={"name": "x", "steps": [{"type": "run-sql"}]},
                    headers=ADMIN)
    assert r.status_code == 422  # step type outside the governed vocabulary
    r = client.post("/studio/config/pipeline/bad-check",
                    json={"name": "x", "steps": [
                        {"type": "extract-table",
                         "params": {"table": "patients"},
                         "checks": [{"type": "vibes"}]}]}, headers=ADMIN)
    assert r.status_code == 422  # unknown DQ check
    r = client.post("/studio/config/pipeline/bad-table",
                    json={"name": "x", "steps": [
                        {"type": "extract-table",
                         "params": {"table": "sqlite_master"}}]}, headers=ADMIN)
    assert r.status_code == 422  # extract targets are a named allow-list
    r = client.post("/studio/config/pipeline/no-steps",
                    json={"name": "x", "steps": []}, headers=ADMIN)
    assert r.status_code == 422


def test_disabled_pipeline_refuses_to_run(client):
    r = client.post("/pipelines/lake-replay/run", json={}, headers=ADMIN)
    assert r.status_code == 409


def test_pipeline_run_capability_gate(client):
    assert client.post("/pipelines/mpi-dedup-scan/run", json={},
                       headers=VIEWER).status_code == 403
    assert client.post("/pipelines/mpi-dedup-scan/run", json={},
                       headers=CLINICIAN).status_code == 403
    assert client.get("/pipelines/definitions",
                      headers=VIEWER).status_code == 200


# ---------------------------------------------------------------------------
# MPI dedup scan — candidates for human review, never a merge
# ---------------------------------------------------------------------------

def test_dedup_scan_candidates_and_review(client):
    _patient(client, "pat-a", phone="555-0100")
    _patient(client, "pat-b", phone="555-9999")  # same name + DOB = 80
    _patient(client, "pat-c", family="Jones", given="Maria", dob="1975-06-07")
    run = _run(client, "mpi-dedup-scan")
    assert run["status"] == "succeeded"
    assert run["steps"][0]["detail"]["new_candidates"] == 1
    cands = client.get("/pipelines/dedup/candidates", headers=ADMIN).json()
    assert cands["total"] == 1
    cand = cands["items"][0]
    assert {cand["patient_a"], cand["patient_b"]} == {"pat-a", "pat-b"}
    assert cand["score"] == 80
    assert cand["reasons"].get("dob") == "match"
    assert cand["reasons"].get("name") == "exact"
    r = client.post(f"/pipelines/dedup/candidates/{cand['id']}/review",
                    json={"decision": "not-duplicates", "note": "twins"},
                    headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["merge_performed"] is False
    # Patients are untouched — the scan never merges.
    assert client.get("/fhir/Patient/pat-a", headers=ADMIN).status_code == 200
    assert client.get("/fhir/Patient/pat-b", headers=ADMIN).status_code == 200


def test_dedup_scan_is_idempotent_per_scope(client):
    _patient(client, "pat-a")
    _patient(client, "pat-b")
    first = _run(client, "mpi-dedup-scan")
    second = _run(client, "mpi-dedup-scan")
    assert second["status"] == "succeeded"
    # The second run reuses the first run's succeeded step (same scope)
    # instead of re-executing it.
    assert len((second["detail"] or {}).get("reused_step_ids") or []) == 1
    assert first["run_id"] != second["run_id"]
    cands = client.get("/pipelines/dedup/candidates", headers=ADMIN).json()
    assert cands["total"] == 1  # no duplicate candidate rows


# ---------------------------------------------------------------------------
# Terminology normalisation — unmapped codes reported, not passed silently
# ---------------------------------------------------------------------------

def test_terminology_normalisation_reports_unmapped(client):
    _patient(client, "pat-a")
    for oid, code in (("obs-glu", "GLU"), ("obs-xyz", "XYZ")):
        r = client.post("/fhir/Observation", json={
            "resourceType": "Observation", "id": oid, "status": "final",
            "code": {"coding": [{"system": "http://opencare.org/local/lab",
                                 "code": code}]},
            "subject": {"reference": "Patient/pat-a"},
            "valueQuantity": {"value": 95, "unit": "mg/dL"}}, headers=ADMIN)
        assert r.status_code == 201, r.text
    run = _run(client, "terminology-normalisation")
    assert run["status"] == "succeeded"
    norm = next(s for s in run["steps"]
                if s["step_type"] == "terminology-normalise")
    assert norm["detail"]["mapped"] == 1  # GLU -> LOINC 2345-7
    assert norm["detail"]["unmapped_codes"] == ["XYZ"]


# ---------------------------------------------------------------------------
# Warehouse load through the pipeline
# ---------------------------------------------------------------------------

def test_warehouse_load_pipeline_runs_real_etl(client):
    _patient(client, "pat-a")
    run = _run(client, "nightly-warehouse-load")
    assert run["status"] == "succeeded"
    etl = run["steps"][0]["detail"]["etl"]
    assert etl["processed"] >= 1 and etl["by_type"].get("Patient") == 1
    status = client.get("/warehouse/status", headers=ADMIN).json()
    assert status["warehouse"]["person"] == 1


# ---------------------------------------------------------------------------
# Data-quality gates — quarantine, release resumes, reject stops
# ---------------------------------------------------------------------------

def _dq_pipeline(client, key="dq-demo"):
    _publish(client, "pipeline", key, {
        "name": "DQ demo", "enabled": True, "steps": [
            {"type": "extract-table", "params": {"table": "observations"},
             "checks": [{"type": "row-count-floor", "value": 5}]},
            {"type": "terminology-normalise"}]})


def test_dq_failure_quarantines_and_release_resumes(client):
    _patient(client, "pat-a")
    r = client.post("/fhir/Observation", json={
        "resourceType": "Observation", "id": "obs-1", "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "1234-5"}]},
        "subject": {"reference": "Patient/pat-a"},
        "valueQuantity": {"value": 5, "unit": "mg/dL"}}, headers=ADMIN)
    assert r.status_code == 201
    _dq_pipeline(client)
    run = _run(client, "dq-demo")
    assert run["status"] == "quarantined"
    assert run["steps"][0]["status"] == "quarantined"
    holds = client.get("/devices/quarantine", headers=ADMIN,
                       params={"status": "held", "source": "pipeline"}).json()
    assert holds["total"] == 1
    hold = holds["items"][0]
    assert hold["pipeline_run_id"] == run["run_id"]
    r = client.post(f"/devices/quarantine/{hold['id']}/release",
                    json={"note": "verified small census"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["outcome"]["status"] == "succeeded"
    runs = client.get("/pipelines/runs", headers=ADMIN,
                      params={"pipeline_key": "dq-demo"}).json()
    assert runs["items"][0]["status"] == "succeeded"
    # Remaining steps executed after the release.
    steps = runs["items"][0]["steps"]
    assert [s["status"] for s in steps] == ["succeeded", "succeeded"]


def test_dq_reject_requires_note_and_stops_the_run(client):
    _patient(client, "pat-a")
    _dq_pipeline(client, key="dq-demo-2")
    run = _run(client, "dq-demo-2")
    assert run["status"] == "quarantined"
    holds = client.get("/devices/quarantine", headers=ADMIN,
                       params={"status": "held", "source": "pipeline"}).json()
    hold_id = holds["items"][0]["id"]
    assert client.post(f"/devices/quarantine/{hold_id}/reject", json={},
                       headers=ADMIN).status_code == 422  # note required
    r = client.post(f"/devices/quarantine/{hold_id}/reject",
                    json={"note": "bad extract"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["quarantine"]["status"] == "rejected"
    runs = client.get("/pipelines/runs", headers=ADMIN,
                      params={"pipeline_key": "dq-demo-2"}).json()
    assert runs["items"][0]["status"] == "quarantined"  # never resumed


# ---------------------------------------------------------------------------
# Failure — retry, dead-letter, replay after repair
# ---------------------------------------------------------------------------

def test_failed_step_retries_dead_letters_and_replays(client):
    # Queue an offline reading from an unregistered device: replaying it
    # fails until the device exists.
    r = client.post("/devices/offline/batches", json={"items": [
        {"kind": "reading", "payload": {"device_key": "ghost-device",
                                         "metric": "temperature",
                                         "value": 4.0, "unit": "C"}}]},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    _publish(client, "pipeline", "offline-replay", {
        "name": "Offline replay", "enabled": True, "steps": [
            {"type": "replay-lake",
             "params": {"payloadType": "device-reading", "mode": "offline"},
             "retry": {"maxAttempts": 2, "backoffSeconds": 0}}]})
    run = _run(client, "offline-replay")
    assert run["status"] == "failed"
    assert run["steps"][0]["attempts"] == 2  # retried per the definition
    letters = client.get("/pipelines/dead-letters", headers=ADMIN).json()
    assert letters["total"] == 1
    letter = letters["items"][0]
    assert letter["pipeline_key"] == "offline-replay"
    assert letter["status"] == "open"
    # Repair the cause, then replay the dead letter into a fresh run.
    r = client.post("/devices", json={
        "device_key": "ghost-device", "name": "Ghost Fridge",
        "device_type": "fridge-sensor", "location": "Ward"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post(f"/pipelines/dead-letters/{letter['id']}/replay",
                    json={}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "succeeded"
    readings = client.get("/devices/readings", headers=ADMIN,
                          params={"device_key": "ghost-device"}).json()
    assert readings["total"] == 1  # the held reading finally curated
    letters = client.get("/pipelines/dead-letters", headers=ADMIN).json()
    assert letters["items"][0]["status"] == "replayed"


def test_backfill_replay_skips_already_linked_entries(client):
    _patient(client, "pat-a")
    r = client.post("/devices/ingest/observation", json={
        "resourceType": "Observation", "id": "obs-1", "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
        "subject": {"reference": "Patient/pat-a"},
        "device": {"reference": "Device/vitals-monitor-1"},
        "valueQuantity": {"value": 72, "unit": "/min"}}, headers=CLINICIAN)
    assert r.status_code == 201
    _publish(client, "pipeline", "obs-backfill", {
        "name": "Observation backfill", "enabled": True, "steps": [
            {"type": "replay-lake",
             "params": {"payloadType": "fhir-observation",
                        "mode": "backfill"}}]})
    run = _run(client, "obs-backfill")
    assert run["status"] == "succeeded"
    step = run["steps"][0]
    detail = step["detail"]
    # The entry was already ingested (and linked) at ingest time, so the
    # backfill skips it rather than double-curating.
    assert detail["skipped_already_curated"] >= 1
    assert step["rows_out"] == 0
