"""Platform pass — the Rust de-identification pipeline step, integrated.

The Phase 14 pipeline vocabulary gains a governed `deidentify` step
(configstore.PIPELINE_STEP_TYPES) executed by app.pipelines through the
Rust binary in services/rust (crate opencare-deid, rule-based HIPAA
Safe Harbor-style removal/generalisation — no certification claimed).
The step FAILS CLOSED: a missing or failing binary fails the step so
the runner's retry/dead-letter machinery engages; rows are never
passed through un-deidentified.

Binary-dependent tests use the locally built binary
(OPENCARE_DEID_BIN, default the workspace cargo target) and skip —
with the reason visible — when it has not been built; the governance
and fail-closed tests always run.

Synthetic data only.
"""
import json
import os

import pytest

from .conftest import ADMIN

DEID_BIN = os.environ.get("OPENCARE_DEID_BIN") or os.path.expanduser(
    "~/workspace/tmp/rust/target/debug/opencare-deid")
requires_bin = pytest.mark.skipif(
    not os.path.exists(DEID_BIN),
    reason="opencare-deid binary not built "
           "(cd services/rust && cargo build)")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WAREHOUSE_DATABASE_URL",
                       f"sqlite:///{tmp_path}/warehouse.db")
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db")
    with TestClient(app) as c:
        yield c


def _publish(client, kind, key, payload):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/{kind}/{key}/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    return version


def _deid_payload(command):
    return {"name": "De-identify appointments",
            "enabled": True,
            "steps": [
                {"type": "extract-table", "name": "Extract appointments",
                 "params": {"table": "appointments"}},
                {"type": "deidentify", "name": "De-identify (Rust)",
                 "params": {"command": command}},
            ]}


# ---------------------------------------------------------------------------
# Governance: the step is in the governed vocabulary, params validated
# ---------------------------------------------------------------------------

def test_deidentify_step_requires_command(client):
    r = client.post("/studio/config/pipeline/deid-no-command",
                    json={"name": "x", "steps": [
                        {"type": "deidentify", "params": {}}]},
                    headers=ADMIN)
    assert r.status_code == 422  # command is mandatory — no silent default run
    r = client.post("/studio/config/pipeline/deid-bad-timeout",
                    json={"name": "x", "steps": [
                        {"type": "deidentify",
                         "params": {"command": "opencare-deid",
                                    "timeoutSeconds": 9999}}]},
                    headers=ADMIN)
    assert r.status_code == 422


def test_deidentify_step_publishes_with_valid_params(client):
    _publish(client, "pipeline", "deid-ok",
             _deid_payload("opencare-deid"))


def test_seeded_deidentify_export_ships_disabled(client):
    r = client.get("/pipelines/definitions/deidentify-export",
                   headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False  # binary must be installed first
    assert [s["type"] for s in body["steps"]] == ["extract-table",
                                                  "deidentify"]
    r = client.post("/pipelines/deidentify-export/run", json={},
                    headers=ADMIN)
    assert r.status_code == 409  # disabled pipelines refuse to run


# ---------------------------------------------------------------------------
# The step itself (unit level, through pipelines._step_deidentify)
# ---------------------------------------------------------------------------

@requires_bin
def test_step_transforms_rows_and_preserves_structure():
    from app.pipelines import _step_deidentify

    rows = [{"id": "appt-1", "code": "93000",
             "email": "jane.doe@example.org",
             "note": "Reach patient on 415-555-0132 before 2026-10-20",
             "start": "2026-10-18T09:30:00Z"}]
    result = _step_deidentify(None, {"params": {"command": DEID_BIN}},
                              {"dataset": rows}, None, None)
    assert result["rows_in"] == 1 and result["rows_out"] == 1
    out = result["dataset"][0]
    blob = json.dumps(out)
    assert "jane.doe@example.org" not in blob      # identifier removed
    assert "415-555-0132" not in blob              # phone removed
    assert "2026-10-20" not in blob               # full date generalised
    assert "2026-10-18" not in blob
    assert out["id"] == "appt-1" and out["code"] == "93000"  # structure kept
    assert result["detail"]["engine"].startswith("rust opencare-deid")


def test_step_fails_closed_without_binary():
    from app.pipelines import _step_deidentify

    with pytest.raises(RuntimeError, match="fails closed"):
        _step_deidentify(
            None, {"params": {"command": "/nonexistent/opencare-deid"}},
            {"dataset": [{"id": "x"}]}, None, None)


def test_step_fails_closed_on_nonzero_exit(tmp_path):
    from app.pipelines import _step_deidentify

    fake = tmp_path / "failing-deid"
    fake.write_text("#!/bin/sh\necho 'boom' >&2\nexit 3\n")
    fake.chmod(0o755)
    with pytest.raises(RuntimeError, match="exited 3"):
        _step_deidentify(None, {"params": {"command": str(fake)}},
                         {"dataset": [{"id": "x"}]}, None, None)


def test_step_requires_upstream_dataset():
    from app.pipelines import _step_deidentify

    with pytest.raises(ValueError, match="upstream"):
        _step_deidentify(None, {"params": {"command": "opencare-deid"}},
                         {}, None, None)


# ---------------------------------------------------------------------------
# End to end through the runner
# ---------------------------------------------------------------------------

@requires_bin
def test_run_end_to_end_with_real_binary(client):
    r = client.post("/fhir/Appointment", headers=ADMIN, json={
        "resourceType": "Appointment", "id": "appt-deid-1",
        "status": "booked", "start": "2026-10-18T09:30:00Z",
        "participant": [{"actor": {"reference": "Patient/pat-1"},
                         "status": "accepted"}]})
    assert r.status_code == 201, r.text
    _publish(client, "pipeline", "deid-run", _deid_payload(DEID_BIN))
    r = client.post("/pipelines/deid-run/run", json={}, headers=ADMIN)
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["status"] == "succeeded", run
    step = next(s for s in run["steps"] if s["step_type"] == "deidentify")
    assert step["status"] == "succeeded"
    assert step["rows_in"] >= 1 and step["rows_out"] == step["rows_in"]
    assert step["detail"]["binary"] == DEID_BIN


def test_run_fails_closed_with_missing_binary(client):
    r = client.post("/fhir/Appointment", headers=ADMIN, json={
        "resourceType": "Appointment", "id": "appt-deid-2",
        "status": "booked", "start": "2026-10-19T10:00:00Z",
        "participant": [{"actor": {"reference": "Patient/pat-1"},
                         "status": "accepted"}]})
    assert r.status_code == 201, r.text
    _publish(client, "pipeline", "deid-run-missing",
             _deid_payload("/nonexistent/opencare-deid"))
    r = client.post("/pipelines/deid-run-missing/run", json={},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["status"] == "failed", run
    step = next(s for s in run["steps"] if s["step_type"] == "deidentify")
    assert step["status"] == "failed"
    assert "not found" in (step["error"] or "")
