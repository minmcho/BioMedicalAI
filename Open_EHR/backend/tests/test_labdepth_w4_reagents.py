"""Lab Depth W4 (LAB-D9) — reagent lot lifecycle in the lab.

Covers: the lot-to-lot verification protocol (accept drives
Inventory's own verify + stage surface and releases the lot;
reject stamps a FAIL and leaves it quarantined; acceptance is
refused when the computed comparison misses the director-
configured criteria, and the criteria are GOVERNED — republishing
them changes the outcome); control-lot onboarding (governed run
count enforced, refusal as a patient-QC material before LIVE,
establishment runs never decide the patient QC gate); the
expiry/stage lockout at result entry (each blocking state named,
zero writes); and lot -> result traceability enumeration with the
existing amended-report correction path.

Synthetic data only. The routers under test are mounted here when
app.main has not wired them yet (the coordinator wires them via
the W4 INTEGRATION NOTE); the mount is skipped when already wired.
"""
import copy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import lab as lab_mod
from app import lab_qc as lab_qc_mod
from app import lab_reagents as lab_reagents_mod
from app.main import create_app
from tests.conftest import ADMIN, CLINICIAN

FACILITY = "facility-main"
POTASSIUM = "2823-3"
SKU = "SKU-W4-REAG"


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


def _utc_today():
    return datetime.now(timezone.utc).date()


def _future(days):
    return (_utc_today() + timedelta(days=days)).isoformat()


def _past(days):
    return (_utc_today() - timedelta(days=days)).isoformat()


def _make_reagent_lot(client, lot_number="RL-W4-1", expiry=None, sku=SKU):
    r = client.post("/inventory/items", json={
        "sku": sku, "name": f"Item {sku}", "category": "lab-reagents",
        "subcategory": "chemistry-reagents", "uom": "test",
        "lot_tracked": True, "expiry_tracked": True}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/inventory/movements/receive", json={
        "sku": sku, "location_code": FACILITY, "quantity": 100,
        "lot_number": lot_number,
        "expiry_date": expiry or _future(120)}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    rows = client.get("/inventory/stock", params={"sku": sku},
                      headers=ADMIN).json()["items"]
    assert rows[0]["stage"] == "quarantine"  # class policy initialStage
    return rows[0]


def _release_lot_direct(client, sku=SKU, lot_number="RL-W4-1"):
    """Release a lot through Inventory's own endpoints (the
    pre-protocol path) — used to stage lots for lockout tests."""
    r = client.post("/inventory/lots/verify", json={
        "sku": sku, "location_code": FACILITY, "lot_number": lot_number,
        "method": "qc-parallel", "result": "pass"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post("/inventory/lots/stage", json={
        "sku": sku, "location_code": FACILITY, "lot_number": lot_number,
        "to_stage": "released"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def _lot_row(client, sku=SKU, lot_number="RL-W4-1"):
    rows = client.get("/inventory/stock", params={"sku": sku},
                      headers=ADMIN).json()["items"]
    return next(r for r in rows if r["lot_number"] == lot_number)


def _patient(client, pid="pat-w4-1"):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic",
                                    "given": ["W4"]}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _received_order(client, patient="pat-w4-1", code=POTASSIUM):
    r = client.post("/lab/orders", json={
        "code": code, "subject": {"reference": f"Patient/{patient}"}},
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


def _lot_ref(lot_number="RL-W4-1", sku=SKU):
    return {"sku": sku, "location_code": FACILITY,
            "lot_number": lot_number}


def _evidence(pairs, qc_pass=True):
    qc = [{"control_material": "SYN-QC-LOT-1", "measured_value": 4.0,
           "expected_low": 3.8, "expected_high": 4.2}] if qc_pass else []
    return {"sample_pairs": [
                {"sample_ref": f"s{i + 1}", "old_value": old,
                 "new_value": new}
                for i, (old, new) in enumerate(pairs)],
            "qc_old_lot": qc, "qc_new_lot": qc}


def _open_run(client, lot_number="RL-W4-1", sku=SKU):
    r = client.post("/lab/reagent-verifications", json={
        "sku": sku, "location_code": FACILITY, "lot_number": lot_number,
        "analyte_code": POTASSIUM, "outgoing_lot_number": "RL-OLD"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    run = r.json()
    # Seeded criteria are labelled governed defaults, per analyte.
    assert run["criteria"]["label"].startswith("GOVERNED DEFAULTS")
    assert run["criteria"]["minSamples"] == 3
    return run


# ---------------------------------------------------------------------------
# Verification protocol
# ---------------------------------------------------------------------------

def test_protocol_accept_releases_lot_through_inventory_surface(client):
    _make_reagent_lot(client)
    run = _open_run(client)
    r = client.post(f"/lab/reagent-verifications/{run['id']}/evidence",
                    json=_evidence([(4.0, 4.1), (5.0, 5.1), (6.0, 5.9)]),
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["comparison"]["meets_criteria"] is True
    r = client.post(f"/lab/reagent-verifications/{run['id']}/decide",
                    json={"decision": "accept"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    decided = r.json()
    assert decided["decision"] == "accept"
    assert decided["decided_by"]
    assert decided["inventory_verification"]["result"] == "pass"
    assert "protocol run" in decided["inventory_verification"]["method"]
    assert decided["inventory_stage"]["to_stage"] == "released"
    # Inventory's own state is the truth: the lot is RELEASED with a
    # passing verification stamped by inventory's handler.
    lot = _lot_row(client)
    assert lot["stage"] == "released"
    assert lot["verification"]["result"] == "pass"


def test_protocol_reject_stamps_fail_and_lot_stays_quarantined(client):
    _make_reagent_lot(client)
    run = _open_run(client)
    client.post(f"/lab/reagent-verifications/{run['id']}/evidence",
                json=_evidence([(4.0, 4.9), (5.0, 6.1), (6.0, 7.4)]),
                headers=CLINICIAN)
    r = client.post(f"/lab/reagent-verifications/{run['id']}/decide",
                    json={"decision": "reject",
                          "note": "Bias exceeds director criteria"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["inventory_verification"]["result"] == "fail"
    lot = _lot_row(client)
    assert lot["stage"] == "quarantine"
    assert lot["verification"]["result"] == "fail"
    # And inventory's gate still refuses release on the FAIL.
    r = client.post("/inventory/lots/stage", json={
        "sku": SKU, "location_code": FACILITY, "lot_number": "RL-W4-1",
        "to_stage": "released"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text


def test_protocol_accept_refused_when_comparison_misses_criteria(client):
    _make_reagent_lot(client)
    run = _open_run(client)
    # 25% bias vs the governed default 10% for potassium.
    client.post(f"/lab/reagent-verifications/{run['id']}/evidence",
                json=_evidence([(4.0, 5.0), (5.0, 6.25), (6.0, 7.5)]),
                headers=CLINICIAN)
    r = client.post(f"/lab/reagent-verifications/{run['id']}/decide",
                    json={"decision": "accept"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "acceptance criteria" in r.json()["detail"]
    assert _lot_row(client)["stage"] == "quarantine"
    # Too few samples also refuses, naming the governed minimum.
    run2 = _open_run(client, lot_number="RL-W4-1")
    client.post(f"/lab/reagent-verifications/{run2['id']}/evidence",
                json=_evidence([(4.0, 4.05)]), headers=CLINICIAN)
    r = client.post(f"/lab/reagent-verifications/{run2['id']}/decide",
                    json={"decision": "accept"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "minimum is 3" in r.json()["detail"]


def test_verification_criteria_are_governed_config(client):
    _make_reagent_lot(client)
    pairs = [(4.0, 4.6), (5.0, 5.75), (6.0, 6.9)]  # 15% bias
    run = _open_run(client)
    client.post(f"/lab/reagent-verifications/{run['id']}/evidence",
                json=_evidence(pairs), headers=CLINICIAN)
    r = client.post(f"/lab/reagent-verifications/{run['id']}/decide",
                    json={"decision": "accept"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text  # governed default is 10%
    # The director republishes looser criteria for this analyte in
    # Studio; a NEW run (criteria snapshot at creation) now accepts
    # the identical evidence.
    payload = copy.deepcopy(
        lab_qc_mod.default_lab_reagent_qc_payload())
    payload["verificationCriteria"]["byAnalyte"][POTASSIUM] = {
        "minSamples": 3, "maxBiasPct": 20.0, "maxBiasAbs": None}
    r = client.post("/studio/config/terminology/lab-reagent-qc",
                    json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/terminology/lab-reagent-qc/versions/{version}"
        "/publish", headers=ADMIN)
    assert r.status_code == 200, r.text
    run2 = _open_run(client)
    assert run2["criteria"]["maxBiasPct"] == 20.0
    client.post(f"/lab/reagent-verifications/{run2['id']}/evidence",
                json=_evidence(pairs), headers=CLINICIAN)
    r = client.post(f"/lab/reagent-verifications/{run2['id']}/decide",
                    json={"decision": "accept"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert _lot_row(client)["stage"] == "released"


# ---------------------------------------------------------------------------
# Control-lot onboarding
# ---------------------------------------------------------------------------

def test_control_lot_onboarding_count_refusal_and_live_use(client):
    r = client.post("/lab/control-lots", json={
        "lot_key": "CTL-W4-1", "analyte_code": POTASSIUM, "level": "L1",
        "required_runs": 3}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    lot = r.json()
    assert lot["state"] == "collecting" and lot["collected_runs"] == 0

    def _patient_qc(expect):
        r = client.post("/lab/qc", json={
            "analyte_code": POTASSIUM, "control_material": "CTL-W4-1",
            "expected_low": 3.8, "expected_high": 4.2,
            "measured_value": 4.0}, headers=CLINICIAN)
        assert r.status_code == expect, r.text
        return r

    # Before establishment the lot REFUSES patient-QC use, by name.
    r = _patient_qc(409)
    assert "refuses use as a patient-QC material" in r.json()["detail"]

    def _establish_run(value):
        r = client.post(f"/lab/control-lots/{lot['id']}/runs", json={
            "expected_low": 3.8, "expected_high": 4.2,
            "measured_value": value}, headers=CLINICIAN)
        assert r.status_code == 201, r.text
        return r.json()

    _establish_run(4.0)
    _establish_run(4.1)
    # Count not met: establishment refuses, naming the shortfall.
    r = client.post(f"/lab/control-lots/{lot['id']}/establish",
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "2 of 3" in r.json()["detail"]
    _establish_run(3.9)
    # Establishment runs never decide the patient QC gate.
    with client.app.state.session_factory() as session:
        assert lab_mod._latest_qc(session, POTASSIUM) is None
    r = client.post(f"/lab/control-lots/{lot['id']}/establish",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    established = r.json()
    assert established["state"] == "established"
    assert established["established_mean"] == pytest.approx(4.0)
    assert established["established_sd"] > 0
    # Established is still not LIVE: patient-QC use still refuses.
    _patient_qc(409)
    r = client.post(f"/lab/control-lots/{lot['id']}/activate",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "live"
    # Live: patient QC records and stamps the control-lot identity.
    run = _patient_qc(201).json()
    assert run["control_lot_id"] == lot["id"]
    assert run["control_material"] == "CTL-W4-1"


# ---------------------------------------------------------------------------
# Result-entry lockout: every blocking state named, zero writes
# ---------------------------------------------------------------------------

def test_result_entry_lockout_quarantined_lot(client):
    _patient(client)
    _make_reagent_lot(client)
    order_id = _received_order(client)
    r = _enter(client, order_id, expect=409, reagent_lot=_lot_ref())
    assert "QUARANTINED" in r.json()["detail"]
    assert "no emergency bypass" in r.json()["detail"]
    # Zero writes: the same order still accepts a (lotless) entry.
    _enter(client, order_id, expect=201)


def test_result_entry_lockout_expired_lot(client):
    _patient(client)
    _make_reagent_lot(client, expiry=_past(1))
    _release_lot_direct(client)
    order_id = _received_order(client)
    r = _enter(client, order_id, expect=409, reagent_lot=_lot_ref())
    assert "EXPIRED" in r.json()["detail"]


def test_result_entry_lockout_recalled_lot(client):
    _patient(client)
    _make_reagent_lot(client)
    _release_lot_direct(client)
    r = client.post("/inventory/lots/stage", json={
        "sku": SKU, "location_code": FACILITY, "lot_number": "RL-W4-1",
        "to_stage": "recalled", "reason_code": "manufacturer-recall"},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    order_id = _received_order(client)
    r = _enter(client, order_id, expect=409, reagent_lot=_lot_ref())
    assert "RECALLED" in r.json()["detail"]


def test_result_entry_lockout_failed_verification_lot(client):
    _patient(client)
    _make_reagent_lot(client)
    _release_lot_direct(client)
    # A later FAIL verification supersedes the pass (inventory's own
    # surface); entry against the lot now refuses by that name.
    r = client.post("/inventory/lots/verify", json={
        "sku": SKU, "location_code": FACILITY, "lot_number": "RL-W4-1",
        "method": "repeat-parallel", "result": "fail"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    order_id = _received_order(client)
    r = _enter(client, order_id, expect=409, reagent_lot=_lot_ref())
    assert "FAILED lot verification" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Lot -> result traceability
# ---------------------------------------------------------------------------

def test_lot_result_traceability_enumerates_every_result(client):
    _patient(client)
    _make_reagent_lot(client)
    _release_lot_direct(client)
    for _ in range(2):
        order_id = _received_order(client)
        _enter(client, order_id, expect=201, reagent_lot=_lot_ref(),
               instrument_id="AN-W4-TRACE")
    # A result on another lot / without a lot is NOT in the radius.
    order_id = _received_order(client)
    _enter(client, order_id, expect=201)
    r = client.get("/lab/reagent-lots/results",
                   params={"sku": SKU, "lot_number": "RL-W4-1"},
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    for item in body["items"]:
        assert item["code"] == POTASSIUM
        assert item["instrument_id"] == "AN-W4-TRACE"
        assert item["reagent_lot_key"] == f"{SKU}|{FACILITY}|RL-W4-1"
        assert item["report_id"]
        # The correction path is the EXISTING amended-report route.
        assert item["correction_path"]["endpoint"] == \
            f"POST /lab/reports/{item['report_id']}/amend"
