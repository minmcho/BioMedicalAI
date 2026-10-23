"""Phase 16 (BD-50) — Rust terminology service parity.

The `terminology-normalise` pipeline step gains a governed
`params.engine`: "core" (default; the in-core Python implementation,
byte-identical behaviour) or "rust" (the services/rust
`opencare-term` binary, invoked once per batch as a subprocess).

These tests run IDENTICAL fixture rows through BOTH engines via real
pipeline runs and assert identical normalised rows AND identical
unmapped reporting. The Rust engine fails closed — a bogus binary
path fails the step (and the run) with NO silent fallback to core.

Extract-shape note: the governed extracts carry a `code` field but
no `system` field, so pipeline-run parity exercises the generic
code-map fallback exactly as production extracts produce it. The
system-specific lookup branch is covered by the Rust crate's own
unit tests (services/rust/src/term.rs), including the Python
`setdefault` first-entry-wins rule for the generic slot.

Synthetic data only.
"""
from pathlib import Path

import pytest

from .conftest import ADMIN

_ROOT = Path(__file__).resolve().parents[2]
_TERM_CANDIDATES = [
    _ROOT / "services" / "rust" / "target" / "debug" / "opencare-term",
    _ROOT / "services" / "rust" / "target" / "release" / "opencare-term",
]
TERM_BIN = next((str(p) for p in _TERM_CANDIDATES if p.is_file()), None)

requires_rust = pytest.mark.skipif(
    TERM_BIN is None,
    reason=("opencare-term binary is not built (run `cargo build` in "
            "services/rust); the coordinator builds it before the gate"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WAREHOUSE_DATABASE_URL",
                       f"sqlite:///{tmp_path}/warehouse.db")
    from fastapi.testclient import TestClient

    from app.main import create_app
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
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


def _patient(client, pid, family="Smith", given="Anna", dob="1980-03-04"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": family, "given": [given]}],
        "gender": "female", "birthDate": dob}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _observation(client, oid, code, system="http://opencare.org/local/lab",
                 value=95):
    coding = [] if code is None else [{"system": system, "code": code}]
    r = client.post("/fhir/Observation", json={
        "resourceType": "Observation", "id": oid, "status": "final",
        "code": {"coding": coding},
        "subject": {"reference": "Patient/pat-a"},
        "valueQuantity": {"value": value, "unit": "mg/dL"}}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _run(client, key):
    r = client.post(f"/pipelines/{key}/run", json={}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _norm_step(run):
    return next(s for s in run["steps"]
                if s["step_type"] == "terminology-normalise")


def _detail_summary(step):
    d = step["detail"]
    return {"mapped": d["mapped"], "unmapped_rows": d["unmapped_rows"],
            "unmapped_codes": d["unmapped_codes"],
            "code_map_entries": d["code_map_entries"]}


def _engine_pipeline(client, key, table, engine_params):
    _publish(client, "pipeline", key, {
        "name": f"Parity {key}", "enabled": True, "steps": [
            {"type": "extract-table", "params": {"table": table}},
            {"type": "terminology-normalise", "params": engine_params,
             "retry": {"maxAttempts": 1, "backoffSeconds": 0}}]})


def _seed_observations(client):
    _patient(client, "pat-a")
    _observation(client, "obs-glu", "GLU")          # mapped -> 2345-7
    _observation(client, "obs-hgb", "HGB")          # mapped -> 718-7
    _observation(client, "obs-xyz", "XYZ")          # unmapped
    _observation(client, "obs-abc", "ABC")          # unmapped
    _observation(client, "obs-xyz2", "XYZ")         # unmapped (repeat)
    _observation(client, "obs-loinc", "2345-7",
                 system="http://loinc.org")        # unmapped (target code)
    _observation(client, "obs-empty", None)         # code-less row


# ---------------------------------------------------------------------------
# Parity — same fixture rows through both engines, identical outputs
# ---------------------------------------------------------------------------

@requires_rust
def test_parity_observations_core_vs_rust(client):
    _seed_observations(client)
    _engine_pipeline(client, "term-parity-core", "observations",
                     {"engine": "core"})
    _engine_pipeline(client, "term-parity-rust", "observations",
                     {"engine": "rust", "command": TERM_BIN})
    core_run = _run(client, "term-parity-core")
    rust_run = _run(client, "term-parity-rust")
    assert core_run["status"] == "succeeded"
    assert rust_run["status"] == "succeeded"
    core_step, rust_step = _norm_step(core_run), _norm_step(rust_run)

    # Identical summary detail (mapped / unmapped reporting).
    assert _detail_summary(rust_step) == _detail_summary(core_step)
    assert _detail_summary(core_step) == {
        "mapped": 2, "unmapped_rows": 4,
        "unmapped_codes": ["2345-7", "ABC", "XYZ"],
        "code_map_entries": 5}
    # Identical normalised rows, row for row.
    assert rust_step["detail"]["dataset"] == core_step["detail"]["dataset"]
    rows = core_step["detail"]["dataset"]
    assert len(rows) == 7
    by_id = {r["id"]: r for r in rows}
    assert by_id["obs-glu"]["code"] == "2345-7"
    assert by_id["obs-glu"]["source_code"] == "GLU"
    assert by_id["obs-glu"]["system"] == "http://loinc.org"
    assert by_id["obs-glu"]["normalised"] is True
    assert by_id["obs-hgb"]["code"] == "718-7"
    # Unmapped rows are passed through unchanged (no new fields).
    assert by_id["obs-xyz"]["code"] == "XYZ"
    assert "normalised" not in by_id["obs-xyz"]
    assert "source_code" not in by_id["obs-xyz"]
    # The code-less row passes through with its empty code intact.
    assert by_id["obs-empty"]["code"] == ""


@requires_rust
def test_parity_codeless_patient_rows_core_vs_rust(client):
    _patient(client, "pat-a")
    _patient(client, "pat-b", family="Jones", given="Maria",
             dob="1975-06-07")
    _engine_pipeline(client, "term-parity-pat-core", "patients",
                     {"engine": "core"})
    _engine_pipeline(client, "term-parity-pat-rust", "patients",
                     {"engine": "rust", "command": TERM_BIN})
    core_step = _norm_step(_run(client, "term-parity-pat-core"))
    rust_step = _norm_step(_run(client, "term-parity-pat-rust"))
    # Rows without a `code` field pass through untouched on both
    # engines: identical datasets, nothing mapped, nothing unmapped.
    assert rust_step["detail"]["dataset"] == core_step["detail"]["dataset"]
    assert _detail_summary(rust_step) == _detail_summary(core_step)
    assert _detail_summary(core_step)["mapped"] == 0
    assert _detail_summary(core_step)["unmapped_rows"] == 0
    assert _detail_summary(core_step)["unmapped_codes"] == []
    for row in core_step["detail"]["dataset"]:
        assert "code" not in row
        assert "normalised" not in row


def test_default_engine_is_core_and_matches_explicit_core(client):
    # The seeded terminology-normalisation pipeline carries no engine
    # param: it must behave exactly like an explicit engine:"core".
    _seed_observations(client)
    _engine_pipeline(client, "term-explicit-core", "observations",
                     {"engine": "core"})
    default_step = _norm_step(_run(client, "terminology-normalisation"))
    core_step = _norm_step(_run(client, "term-explicit-core"))
    assert _detail_summary(default_step) == _detail_summary(core_step)
    assert default_step["detail"]["dataset"] == \
        core_step["detail"]["dataset"]


# ---------------------------------------------------------------------------
# Fail-closed — the Rust engine never falls back to core
# ---------------------------------------------------------------------------

def test_rust_engine_missing_binary_fails_closed(client):
    _seed_observations(client)
    _engine_pipeline(client, "term-rust-bogus", "observations",
                     {"engine": "rust",
                      "command": "/nonexistent/opencare-term-bogus"})
    run = _run(client, "term-rust-bogus")
    # The run FAILS — it does not succeed via a silent core fallback.
    assert run["status"] == "failed"
    step = _norm_step(run)
    assert step["status"] == "failed"
    assert step["attempts"] == 1
    assert "not found" in (step["error"] or "")
    assert "fails closed" in (step["error"] or "")
    # No normalised dataset was produced by the failed step.
    assert not (step["detail"] or {}).get("dataset")
    letters = client.get("/pipelines/dead-letters", headers=ADMIN).json()
    assert any(l["pipeline_key"] == "term-rust-bogus"
               for l in letters["items"])


def test_unknown_engine_value_fails_the_step(client):
    _seed_observations(client)
    _engine_pipeline(client, "term-bad-engine", "observations",
                     {"engine": "bogus"})
    run = _run(client, "term-bad-engine")
    assert run["status"] == "failed"
    step = _norm_step(run)
    assert step["status"] == "failed"
    assert "engine must be 'core' or 'rust'" in (step["error"] or "")
