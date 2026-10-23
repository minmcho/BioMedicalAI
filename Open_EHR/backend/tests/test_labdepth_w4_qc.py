"""Lab Depth W4 (LAB-D8) — QC & calibration completeness.

Covers: the full Westgard set (R-4s / 4-1s / 10x fire on crafted
series and stay silent on clean series; 1-2s/1-3s/2-2s preserved);
QC keyed to instrument + reagent lot + shift; the instrument-path
lock on QC failure (manual entry AND analyser ingest refuse by
name until a passing QC posts) IN ADDITION TO the unchanged
report-level qc_blockers; calibration records with measured
values + due dates; the AMR calibration-verification workflow
(>=3 levels spanning the reportable range); overdue/failed
calibration locking the instrument path; the one-calibration-truth
write-through to the device registry; and the Run-16 old-schema
regression for the LabQcRun keying columns.

Synthetic data only. Routers are mounted here when app.main has
not wired them yet (coordinator wires via the W4 INTEGRATION
NOTE); the mount is skipped when already wired.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app import lab_qc as lab_qc_mod
from app import lab_reagents as lab_reagents_mod
from app.main import create_app
from tests.conftest import ADMIN, CLINICIAN

POTASSIUM = "2823-3"
MATERIAL = "SYN-QC-LOT-1"  # programme mean 4.0, sd 0.1 for potassium


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
    paths = {getattr(r, "path", None) for r in app.routes}
    if "/lab/reagent-verifications" not in paths:
        app.include_router(lab_reagents_mod.router)
    if "/lab/calibrations" not in paths:
        app.include_router(lab_qc_mod.router)
    with TestClient(app) as c:
        with app.state.session_factory() as session:
            lab_qc_mod.ensure_lab_reagent_qc_defaults(session)
        yield c


def _qc(client, measured, material=MATERIAL, low=0.0, high=100.0,
        expect=201, **extra):
    body = {"analyte_code": POTASSIUM, "control_material": material,
            "expected_low": low, "expected_high": high,
            "measured_value": measured}
    body.update(extra)
    r = client.post("/lab/qc", json=body, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r.json()


def _instrument(client, instrument_id):
    r = client.post("/lab/instruments", json={
        "instrument_id": instrument_id, "name": f"Analyser {instrument_id}",
        "bench": "chemistry"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _patient(client, pid="pat-w4q-1"):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic",
                                    "given": ["W4Q"]}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _received_order(client, patient="pat-w4q-1"):
    r = client.post("/lab/orders", json={
        "code": POTASSIUM, "subject": {"reference": f"Patient/{patient}"}},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    order_id = r.json()["id"]
    for to_status in ("collected", "received"):
        r = client.post(f"/lab/orders/{order_id}/transition",
                        json={"to_status": to_status}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    return order_id


def _enter(client, order_id, expect=201, **extra):
    body = {"results": [{"code": POTASSIUM, "value": 4.2}]}
    body.update(extra)
    r = client.post(f"/lab/orders/{order_id}/results", json=body,
                    headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


# ---------------------------------------------------------------------------
# Full Westgard set
# ---------------------------------------------------------------------------

def test_westgard_clean_series_fires_nothing(client):
    for _ in range(3):
        run = _qc(client, 4.0)
        assert run["flags"] == []
        assert run["z_score"] == pytest.approx(0.0)


def test_westgard_r4s_fires_on_opposite_excursions(client):
    first = _qc(client, 4.35)   # z = +3.5
    assert "1-3s" in first["flags"] and "R-4s" not in first["flags"]
    second = _qc(client, 3.65)  # z = -3.5: range across the pair = 7 SD
    assert "R-4s" in second["flags"]
    assert "1-3s" in second["flags"]


def test_westgard_4_1s_fires_on_fourth_same_side_beyond_1s(client):
    for _ in range(3):
        run = _qc(client, 4.15)  # z = +1.5
        assert "4-1s" not in run["flags"]
    run = _qc(client, 4.15)
    assert "4-1s" in run["flags"]
    # 1.5 SD never trips the 2 SD rules on the way.
    assert "1-2s" not in run["flags"] and "2-2s" not in run["flags"]


def test_westgard_10x_fires_on_tenth_same_side_run(client):
    for _ in range(9):
        run = _qc(client, 4.05)  # z = +0.5, same side of the mean
        assert "10x" not in run["flags"]
    run = _qc(client, 4.05)
    assert "10x" in run["flags"]
    assert run["flags"] == ["10x"]  # nothing else fires at 0.5 SD


def test_westgard_existing_rules_preserved(client):
    run = _qc(client, 4.25)  # z = +2.5
    assert run["flags"] == ["1-2s"]
    run = _qc(client, 4.25)
    assert "2-2s" in run["flags"]
    run = _qc(client, 4.4, material="SYN-QC-LOT-2", low=0.0, high=100.0)
    # LOT-2 has its own mean/sd (5.8/0.12): 4.4 is far below — a
    # different series; use potassium L1 material for the 1-3s check.
    run = _qc(client, 4.35)
    assert "1-3s" in run["flags"]


def test_qc_run_keyed_to_instrument_lot_and_shift(client):
    _instrument(client, "AN-W4-KEY")
    run = _qc(client, 4.0, instrument_id="AN-W4-KEY",
              reagent_lot_key="SKU-W4|facility-main|RL-9")
    assert run["instrument_id"] == "AN-W4-KEY"
    assert run["reagent_lot_key"] == "SKU-W4|facility-main|RL-9"
    assert run["shift"] in ("day", "night")  # derived from governed windows
    r = client.get("/lab/qc/instrument-status",
                   params={"instrument_id": "AN-W4-KEY"},
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["analyte_code"] == POTASSIUM
    assert body["items"][0]["shift"] == run["shift"]
    assert body["path_locked"] is False


# ---------------------------------------------------------------------------
# Instrument-path lock: QC failure
# ---------------------------------------------------------------------------

def test_qc_failure_locks_instrument_path_manual_and_ingest(client):
    _patient(client)
    _instrument(client, "AN-W4-LOCK")
    # Failing QC for this instrument + analyte (range verdict).
    _qc(client, 9.9, material="SYN-QC-PLAIN", low=3.8, high=4.2,
        instrument_id="AN-W4-LOCK")
    order_id = _received_order(client)
    r = _enter(client, order_id, expect=409, instrument_id="AN-W4-LOCK")
    assert "Instrument path locked" in r.json()["detail"]
    assert "AN-W4-LOCK" in r.json()["detail"]
    # Analyser ingest on the same instrument refuses identically.
    order2 = _received_order(client)
    csv_body = (f"order_id,patient_id,code,value\n"
                f"{order2},pat-w4q-1,{POTASSIUM},4.2\n")
    r = client.post("/lab/ingest", json={
        "format": "csv", "content": csv_body,
        "instrument_id": "AN-W4-LOCK"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "Instrument path locked" in r.json()["detail"]
    # A passing QC for the instrument + analyte unlocks both paths
    # (and the zero-write refusals left both orders enterable).
    _qc(client, 4.0, material="SYN-QC-PLAIN", low=3.8, high=4.2,
        instrument_id="AN-W4-LOCK")
    _enter(client, order_id, expect=201, instrument_id="AN-W4-LOCK")
    r = client.post("/lab/ingest", json={
        "format": "csv", "content": csv_body,
        "instrument_id": "AN-W4-LOCK"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # The report-level gate is untouched by all of this: it still
    # reads the latest analyte QC (the passing run above).
    r = client.get("/lab/qc/status", params={"analyte_code": POTASSIUM},
                   headers=CLINICIAN)
    assert r.json()["blocked"] is False


def test_other_instrument_unaffected_by_lock(client):
    _patient(client)
    _instrument(client, "AN-W4-A")
    _instrument(client, "AN-W4-B")
    _qc(client, 9.9, material="SYN-QC-PLAIN", low=3.8, high=4.2,
        instrument_id="AN-W4-A")
    order_id = _received_order(client)
    _enter(client, order_id, expect=201, instrument_id="AN-W4-B")


# ---------------------------------------------------------------------------
# Calibration: records, AMR verification, overdue lock, one truth
# ---------------------------------------------------------------------------

def _cal_levels(within=True):
    if within:
        return [{"label": "low", "nominal": 2.0, "measured": 2.02},
                {"label": "mid", "nominal": 5.0, "measured": 4.97},
                {"label": "high", "nominal": 10.0, "measured": 10.05}]
    return [{"label": "low", "nominal": 2.0, "measured": 2.6},
            {"label": "mid", "nominal": 5.0, "measured": 4.97},
            {"label": "high", "nominal": 10.0, "measured": 10.05}]


def test_calibration_record_with_values_and_due_date(client):
    _instrument(client, "AN-W4-CAL")
    r = client.post("/lab/calibrations", json={
        "instrument_id": "AN-W4-CAL", "levels": _cal_levels()},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    record = r.json()
    assert record["passed"] is True
    assert record["due_date"] is not None  # governed default interval
    assert all(lr["within"] for lr in record["level_results"])
    r = client.get("/lab/calibrations/status",
                   params={"instrument_id": "AN-W4-CAL"},
                   headers=CLINICIAN)
    assert r.json()["posture"]["source"] == "lab-record"
    assert r.json()["posture"]["status"] in ("ok", "due-soon")
    assert r.json()["locked"] is False


def test_amr_verification_requires_three_levels(client):
    _instrument(client, "AN-W4-AMR")
    r = client.post("/lab/calibrations/verify-amr", json={
        "instrument_id": "AN-W4-AMR", "levels": _cal_levels()[:2],
        "reportable_low": 2.0, "reportable_high": 10.0},
        headers=CLINICIAN)
    assert r.status_code == 422, r.text
    assert "at least 3 levels" in r.json()["detail"]
    # Zero writes: no record was created by the refused attempt.
    r = client.get("/lab/calibrations",
                   params={"instrument_id": "AN-W4-AMR"},
                   headers=CLINICIAN)
    assert r.json()["total"] == 0


def test_amr_span_failure_records_and_locks_then_pass_unlocks(client):
    _patient(client)
    _instrument(client, "AN-W4-SPAN")
    # Levels do NOT span the reportable range (max nominal 8 < 10).
    levels = [{"label": "low", "nominal": 2.0, "measured": 2.0},
              {"label": "mid", "nominal": 5.0, "measured": 5.0},
              {"label": "high", "nominal": 8.0, "measured": 8.0}]
    r = client.post("/lab/calibrations/verify-amr", json={
        "instrument_id": "AN-W4-SPAN", "levels": levels,
        "reportable_low": 2.0, "reportable_high": 10.0},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["passed"] is False
    assert r.json()["spans_range"] is False
    order_id = _received_order(client)
    r = _enter(client, order_id, expect=409, instrument_id="AN-W4-SPAN")
    assert "FAILED latest calibration" in r.json()["detail"]
    # A spanning, in-criteria verification unlocks the path.
    r = client.post("/lab/calibrations/verify-amr", json={
        "instrument_id": "AN-W4-SPAN", "levels": _cal_levels(),
        "reportable_low": 2.0, "reportable_high": 10.0},
        headers=CLINICIAN)
    assert r.json()["passed"] is True and r.json()["spans_range"] is True
    _enter(client, order_id, expect=201, instrument_id="AN-W4-SPAN")


def test_overdue_calibration_locks_instrument_path(client):
    _patient(client)
    _instrument(client, "AN-W4-OLD")
    yesterday = (datetime.now(timezone.utc).date()
                 - timedelta(days=1)).isoformat()
    r = client.post("/lab/calibrations", json={
        "instrument_id": "AN-W4-OLD", "levels": _cal_levels(),
        "next_due_date": yesterday}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["passed"] is True  # the calibration itself passed
    order_id = _received_order(client)
    r = _enter(client, order_id, expect=409, instrument_id="AN-W4-OLD")
    assert "calibration is overdue" in r.json()["detail"]
    # A fresh calibration with a future due date unlocks.
    future = (datetime.now(timezone.utc).date()
              + timedelta(days=90)).isoformat()
    client.post("/lab/calibrations", json={
        "instrument_id": "AN-W4-OLD", "levels": _cal_levels(),
        "next_due_date": future}, headers=CLINICIAN)
    _enter(client, order_id, expect=201, instrument_id="AN-W4-OLD")


def test_calibration_writes_through_to_device_registry_one_truth(client):
    # The instrument is a Phase 14 device-registry device (type
    # 'analyser', governed interval 180 days).
    r = client.post("/devices", json={
        "device_key": "AN-W4-DEV", "name": "Registry analyser",
        "device_type": "analyser"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["calibration"]["status"] == "unknown"
    r = client.post("/lab/calibrations", json={
        "instrument_id": "AN-W4-DEV", "levels": _cal_levels()},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    record = r.json()
    assert record["passed"] is True
    assert record["registry_due"] is not None
    # The REGISTRY is the truth and it moved: due = performed + 180d.
    r = client.get("/devices", headers=ADMIN)
    device = next(d for d in r.json()["items"]
                  if d["device_key"] == "AN-W4-DEV")
    assert device["last_calibrated_at"] is not None
    assert device["calibration"]["status"] == "ok"
    assert device["calibration_due"].startswith(record["due_date"])
    # And the lab's posture read defers to the registry.
    r = client.get("/lab/calibrations/status",
                   params={"instrument_id": "AN-W4-DEV"},
                   headers=CLINICIAN)
    assert r.json()["posture"]["source"] == "device-registry"
    assert r.json()["posture"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Run-16 regression: pre-W4 lab_qc_runs schema gains the keying columns
# ---------------------------------------------------------------------------

def test_ensure_lab_qc_columns_on_old_schema(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE lab_qc_runs ("
            "id INTEGER PRIMARY KEY, analyte_code VARCHAR(60) NOT NULL, "
            "control_material VARCHAR(200) NOT NULL, "
            "expected_low FLOAT NOT NULL, expected_high FLOAT NOT NULL, "
            "measured_value FLOAT NOT NULL, unit VARCHAR(40), "
            "passed BOOLEAN NOT NULL, performed_by VARCHAR(120) NOT NULL, "
            "created_at DATETIME, level VARCHAR(40), flags JSON NOT NULL, "
            "z_score FLOAT)"))
    lab_qc_mod.ensure_lab_qc_columns(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("lab_qc_runs")}
    assert {"instrument_id", "reagent_lot_key", "control_lot_id",
            "shift"} <= columns
    # Idempotent: a second run is a no-op, not an error.
    lab_qc_mod.ensure_lab_qc_columns(engine)
