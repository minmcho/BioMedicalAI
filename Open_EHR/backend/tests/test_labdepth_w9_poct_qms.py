"""Lab Depth W9 (LAB-D15 + LAB-D16) — POCT enforcement go-live and
laboratory QMS surfaces & teeth.

Covers: the governed POCT deployment profile (track-only preserves
legacy behaviour exactly; enforce refuses an uncompetent operator
and a QC-overdue / QC-failed device BY NAME at POCT result entry
with zero writes); per-device lot + QC-range configuration records
with history; per-device/per-operator QC review with corrective-
action comments; CLIA competency gating at result entry AND at
verification (absent + expired refuse by name; unmapped test
systems honestly ungated); nonconformance intake from each of the
four source types (specimen rejection, QC failure, corrected
report, PT failure), mirrored into the safety-event machinery,
with closure refused (Wave C); a failed PT event that cannot close
without a corrective action; the SOP library (versioning,
read-and-acknowledge, overdue reviews); and the internal-audit
programme (finding raises a nonconformance).

Synthetic data only. Router mounted here when app.main has not
wired it yet (coordinator wires via the W9 INTEGRATION NOTE); the
mount is skipped when already wired.
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import lab_poct_qms as qms_mod
from app.db import Base
from app.main import create_app
from tests.conftest import ADMIN, CLINICIAN

A1C = "4548-4"
GLUCOSE = "2345-7"
POTASSIUM = "2823-3"
SIX_ELEMENTS = [
    "direct-observation-performance",
    "monitoring-recording-reporting",
    "review-intermediate-qc-pt",
    "direct-observation-maintenance",
    "assessment-test-performance",
    "assessment-problem-solving",
]


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
    # Register this module's tables when main has not imported it yet.
    Base.metadata.create_all(app.state.engine)
    paths = {getattr(r, "path", None) for r in app.routes}
    if "/lab/qms/poct/profile" not in paths:
        app.include_router(qms_mod.router)
    with TestClient(app) as c:
        with app.state.session_factory() as session:
            qms_mod.ensure_lab_poct_qms_defaults(session)
        yield c


# ---------------------------------------------------------------------------
# Helpers (mirroring tests/test_depth_lab.py)
# ---------------------------------------------------------------------------

def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic",
                                    "given": ["W9"]}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _order(client, code, patient="pat-1", **extra):
    body = {"code": code, "subject": {"reference": f"Patient/{patient}"}}
    body.update(extra)
    r = client.post("/lab/orders", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _transition(client, order_id, to_status):
    r = client.post(f"/lab/orders/{order_id}/transition",
                    json={"to_status": to_status}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r


def _to_received(client, order_id):
    _transition(client, order_id, "collected")
    _transition(client, order_id, "received")


def _publish_config(client, kind, key, payload):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/{kind}/{key}/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    return version


def _effective(client, kind, key):
    r = client.get(f"/studio/config/{kind}/effective/{key}",
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["payload"]


def _poct_device(client, key="poc-glu-1"):
    r = client.post("/devices", json={
        "device_key": key, "device_type": "poct-analyser",
        "name": "Synthetic POCT glucose meter", "location": "Clinic"},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    return key


def _publish_poct_workflow(client):
    workflow = client.get("/lab/catalog",
                          headers=CLINICIAN).json()["workflow"]
    _publish_config(client, "workflow", "lab-order-flow-poct", workflow)


def _poct_a1c_attempt(client, **extra):
    order = _order(client, A1C, poct=True)
    _to_received(client, order["id"])
    body = {"results": [{"code": A1C, "value": 7.4}]}
    body.update(extra)
    return client.post(f"/lab/orders/{order['id']}/results", json=body,
                       headers=CLINICIAN), order


def _publish_profile(client, mode):
    payload = _effective(client, "terminology", "poct-deployment-profile")
    payload["profileMode"] = mode
    _publish_config(client, "terminology", "poct-deployment-profile",
                    payload)


def _elements(days_ago=0, assessor="lab-manager"):
    assessed = (date.today() - timedelta(days=days_ago)).isoformat()
    return [{"element": code, "assessed_on": assessed,
             "assessor": assessor} for code in SIX_ELEMENTS]


def _clia_record(client, staff_id, test_system, days_ago=0):
    r = client.post("/lab/competency/records",
                    json={"staff_id": staff_id,
                          "test_system": test_system,
                          "elements": _elements(days_ago=days_ago)},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["competency"]


def _publish_gating(client, mappings):
    payload = _effective(client, "terminology", "lab-competency-gating")
    payload["mappings"] = mappings
    _publish_config(client, "terminology", "lab-competency-gating",
                    payload)


# ---------------------------------------------------------------------------
# D15 — deployment profile: track-only preserves legacy behaviour
# ---------------------------------------------------------------------------

def test_d15_track_only_preserves_legacy_behaviour(client):
    _patient(client)
    _poct_device(client)
    _publish_poct_workflow(client)
    profile = client.get("/lab/qms/poct/profile",
                         headers=CLINICIAN).json()
    assert profile["profile_mode"] == "track-only"
    assert profile["effective_enforcement"] == {
        "block_uncompetent_operator": False,
        "block_missing_daily_qc": False}
    # The honest bound rides the surface.
    assert profile["external_leg"].startswith("EXTERNAL LEG")
    # Legacy behaviour: no competency, no QC, no device named — the
    # entry succeeds exactly as before W9.
    r, _order_row = _poct_a1c_attempt(client)
    assert r.status_code == 201, r.text
    assert r.json()["report"]["status"] == "final"


def test_d15_enforce_refuses_uncompetent_and_qc_overdue(client):
    _patient(client)
    _poct_device(client)
    _publish_poct_workflow(client)
    _publish_profile(client, "enforce")
    profile = client.get("/lab/qms/poct/profile",
                         headers=CLINICIAN).json()
    assert profile["effective_enforcement"] == {
        "block_uncompetent_operator": True,
        "block_missing_daily_qc": True}
    # The programme's own switches remain OFF — the profile enforces.
    assert profile["programme_enforcement"] == {
        "blockUncompetentOperator": False, "blockMissingDailyQc": False}
    queue_before = client.get("/lab/verification-queue",
                              headers=CLINICIAN).json()["total"]
    # 1) Uncompetent operator: refused BY NAME, zero writes.
    r, order = _poct_a1c_attempt(client, device_key="poc-glu-1")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "dev-clinician" in detail and "competency" in detail
    queue_after = client.get("/lab/verification-queue",
                             headers=CLINICIAN).json()
    assert queue_after["total"] == queue_before
    assert all(i["report"]["id"] not in (None,) for i in queue_after["items"])
    # The order never resulted: a retry still reaches the same gate
    # (no partial draft was left behind).
    r2 = client.post(f"/lab/orders/{order['id']}/results",
                     json={"results": [{"code": A1C, "value": 7.4}],
                           "device_key": "poc-glu-1"}, headers=CLINICIAN)
    assert r2.status_code == 409, r2.text
    # 2) Competent operator (POCT competency for the entering user),
    #    but no device named -> 422; device named, QC missing -> 409.
    today = date.today().isoformat()
    r = client.post("/lab/poct/competency", json={
        "staff_id": "dev-clinician", "test_code": A1C,
        "method": "Direct observation of testing",
        "assessed_on": today}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r, _o = _poct_a1c_attempt(client)
    assert r.status_code == 422, r.text
    r, _o = _poct_a1c_attempt(client, device_key="poc-glu-1")
    assert r.status_code == 409, r.text
    assert "poc-glu-1" in r.json()["detail"]
    assert "QC" in r.json()["detail"]
    # 3) QC run at both levels, then a later FAILURE supersedes.
    for level in ("L1", "L2"):
        client.post("/lab/poct/qc", json={
            "device_key": "poc-glu-1", "level": level, "result": "pass"},
            headers=CLINICIAN)
    client.post("/lab/poct/qc", json={
        "device_key": "poc-glu-1", "level": "L1", "result": "fail"},
        headers=CLINICIAN)
    r, _o = _poct_a1c_attempt(client, device_key="poc-glu-1")
    assert r.status_code == 409, r.text
    # Re-run the failed level green -> entry succeeds.
    client.post("/lab/poct/qc", json={
        "device_key": "poc-glu-1", "level": "L1", "result": "pass"},
        headers=CLINICIAN)
    r, _o = _poct_a1c_attempt(client, device_key="poc-glu-1")
    assert r.status_code == 201, r.text
    # 4) Back to track-only -> legacy behaviour restored exactly.
    _publish_profile(client, "track-only")
    r, _o = _poct_a1c_attempt(client)
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# D15 — device configuration records (history) + QC review surface
# ---------------------------------------------------------------------------

def test_d15_device_config_history_and_qc_review(client):
    _poct_device(client)
    today = date.today()
    r = client.post("/lab/qms/poct/device-config", json={
        "device_key": "poc-glu-1", "reagent_lot": "RL-2026-01",
        "control_lot": "CTL-9",
        "qc_ranges": {"L1": {"low": 40, "high": 70},
                      "L2": {"low": 200, "high": 260}},
        "effective_from": today.isoformat(),
        "note": "Initial configuration push (synthetic)"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    first = r.json()["config"]
    assert first["current"] is True
    assert r.json()["external_leg"].startswith("EXTERNAL LEG")
    # A future-dated record joins the history but is not current yet.
    r = client.post("/lab/qms/poct/device-config", json={
        "device_key": "poc-glu-1", "reagent_lot": "RL-2026-02",
        "control_lot": "CTL-9",
        "qc_ranges": {"L1": {"low": 42, "high": 68},
                      "L2": {"low": 200, "high": 260}},
        "effective_from": (today + timedelta(days=7)).isoformat()},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["config"]["current"] is False
    listing = client.get("/lab/qms/poct/device-config",
                         params={"device_key": "poc-glu-1"},
                         headers=CLINICIAN).json()
    assert listing["total"] == 2
    currents = [i for i in listing["items"] if i["current"]]
    assert len(currents) == 1
    assert currents[0]["reagent_lot"] == "RL-2026-01"
    # Validation: unknown device 404; inverted range / missing lot 422.
    r = client.post("/lab/qms/poct/device-config", json={
        "device_key": "ghost", "reagent_lot": "X",
        "qc_ranges": {"L1": {"low": 1, "high": 2}}}, headers=CLINICIAN)
    assert r.status_code == 404, r.text
    r = client.post("/lab/qms/poct/device-config", json={
        "device_key": "poc-glu-1", "reagent_lot": "X",
        "qc_ranges": {"L1": {"low": 9, "high": 2}}}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post("/lab/qms/poct/device-config", json={
        "device_key": "poc-glu-1",
        "qc_ranges": {"L1": {"low": 1, "high": 2}}}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # QC review surface: record QC, then a review with corrective
    # action; the surface aggregates per device + per operator.
    client.post("/lab/poct/qc", json={
        "device_key": "poc-glu-1", "level": "L1", "result": "pass"},
        headers=CLINICIAN)
    client.post("/lab/poct/qc", json={
        "device_key": "poc-glu-1", "level": "L2", "result": "fail"},
        headers=CLINICIAN)
    r = client.post("/lab/qms/poct/qc-review", json={
        "device_key": "poc-glu-1",
        "comment": "L2 control failed on review (synthetic)",
        "corrective_action": "Repeat L2 with fresh control; retrain "
                             "operator on mixing step"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    review = client.get("/lab/qms/poct/qc-review",
                        headers=CLINICIAN).json()
    dev = next(d for d in review["devices"]
               if d["device_key"] == "poc-glu-1")
    assert dev["qc_current"] is False
    assert dev["levels_today"] == {"L1": "pass", "L2": "fail"}
    assert dev["current_config"]["reagent_lot"] == "RL-2026-01"
    op = next(o for o in review["operators"]
              if o["operator"] == "dev-clinician")
    assert op["runs"] == 2 and op["fails"] == 1
    assert review["reviews"][0]["corrective_action"].startswith(
        "Repeat L2")


# ---------------------------------------------------------------------------
# D16(b) — competency gating at entry AND verification
# ---------------------------------------------------------------------------

def test_d16_competency_gating_entry_and_verify(client):
    _patient(client)
    # Honest seed: nothing mapped -> everything ungated, surface says so.
    gating = client.get("/lab/qms/competency/gating",
                        headers=CLINICIAN).json()
    assert gating["gated_count"] == 0
    pot = next(t for t in gating["tests"] if t["code"] == POTASSIUM)
    assert pot["gated"] is False
    assert pot["state_label"].startswith("UNGATED")
    # Map glucose -> its own CLIA test system.
    _publish_gating(client, [{"testCode": GLUCOSE,
                              "testSystem": GLUCOSE}])
    gating = client.get("/lab/qms/competency/gating",
                        headers=CLINICIAN).json()
    glu = next(t for t in gating["tests"] if t["code"] == GLUCOSE)
    assert glu["gated"] is True
    assert glu["required_test_system"] == GLUCOSE
    # Unmapped potassium still enters freely (honestly ungated).
    order = _order(client, POTASSIUM)
    _to_received(client, order["id"])
    r = client.post(f"/lab/orders/{order['id']}/results",
                    json={"results": [{"code": POTASSIUM, "value": 4.1}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    pot_report = r.json()["report"]["id"]
    # ENTRY gate: absent competency refuses BY NAME, zero writes.
    order = _order(client, GLUCOSE)
    _to_received(client, order["id"])
    r = client.post(f"/lab/orders/{order['id']}/results",
                    json={"results": [{"code": GLUCOSE, "value": 95}]},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "Competency gate (result entry)" in detail
    assert "dev-clinician" in detail and "absent" in detail
    queue = client.get("/lab/verification-queue",
                       headers=CLINICIAN).json()
    assert all(i["order_id"] != order["id"] for i in queue["items"])
    # Expired competency (all six elements 400 days ago) still refuses.
    record = _clia_record(client, "staff-001", GLUCOSE, days_ago=400)
    assert record["status"] == "expired"
    r = client.post(f"/lab/orders/{order['id']}/results",
                    json={"results": [{"code": GLUCOSE, "value": 95}]},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "expired" in r.json()["detail"]
    # Current competency (merge today's elements) -> entry succeeds.
    record = _clia_record(client, "staff-001", GLUCOSE, days_ago=0)
    assert record["status"] == "current"
    r = client.post(f"/lab/orders/{order['id']}/results",
                    json={"results": [{"code": GLUCOSE, "value": 95}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # The competency board surface shows the record + worklist.
    board = client.get("/lab/qms/competency/board",
                       headers=CLINICIAN).json()
    assert any(i["staff_id"] == "staff-001"
               and i["test_system"] == GLUCOSE
               and i["status"] == "current" for i in board["records"])
    # VERIFY gate: map potassium AFTER its results were entered;
    # verification now refuses by name until competency is current.
    _publish_gating(client, [{"testCode": GLUCOSE,
                              "testSystem": GLUCOSE},
                             {"testCode": POTASSIUM,
                              "testSystem": POTASSIUM}])
    r = client.post(f"/lab/reports/{pot_report}/verify",
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "Competency gate (verification)" in r.json()["detail"]
    _clia_record(client, "staff-001", POTASSIUM, days_ago=0)
    r = client.post(f"/lab/reports/{pot_report}/verify",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# D16(a) — lookback / deviation / EQA surfaces read the real backends
# ---------------------------------------------------------------------------

def test_d16_qms_surfaces_read_existing_backends(client):
    r = client.post("/lab/eqa-exchange/enrolments",
                    json={"programme": "PT-2026-Q1",
                          "analyte_codes": [POTASSIUM]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    eqa = client.get("/lab/qms/eqa", headers=CLINICIAN).json()
    assert eqa["total"] == 1
    assert eqa["live_exchange"].startswith("NOT RUN")
    lookback = client.get("/lab/qms/lookback", headers=CLINICIAN).json()
    assert lookback == {"items": [], "total": 0}
    deviations = client.get("/lab/qms/deviations",
                            headers=CLINICIAN).json()
    assert deviations == {"items": [], "total": 0}


# ---------------------------------------------------------------------------
# D16(c) — nonconformance intake from each of the four source types
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source_type,source_ref", [
    ("specimen-rejection", "order-syn-1"),
    ("qc-failure", "qc-run-syn-1"),
    ("corrected-report", "report-syn-1"),
    ("pt-failure", "PT-2026-Q1"),
])
def test_d16_nonconformance_from_each_source(client, source_type,
                                             source_ref):
    r = client.post("/lab/qms/nonconformances", json={
        "source_type": source_type, "source_ref": source_ref,
        "title": f"Synthetic {source_type} for W9 coverage",
        "description": "Synthetic intake record",
        "severity": "major", "owner": "staff-008",
        "corrective_action": "Review and retrain (synthetic)"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    nc = r.json()["nonconformance"]
    assert nc["source_type"] == source_type
    assert nc["status"] == "assigned"
    assert nc["owner"] == "staff-008"
    # Mirrored into the product's safety-event machinery.
    assert nc["safety_event_id"] and nc["safety_event_id"].startswith("sev-")
    # Closure is NOT available here — labelled, Wave C.
    assert nc["closure_state"] == "closure-pending-wave-c"
    assert "Wave C" in nc["closure_label"]
    listing = client.get("/lab/qms/nonconformances",
                         params={"source_type": source_type},
                         headers=CLINICIAN).json()
    assert listing["total"] == 1
    # Closing via the assign endpoint refuses BY NAME.
    r = client.post(f"/lab/qms/nonconformances/{nc['nc_id']}/assign",
                    json={"status": "closed"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    assert "Wave C" in r.json()["detail"]
    # Validation: bad source type / missing source_ref refuse.
    r = client.post("/lab/qms/nonconformances", json={
        "source_type": "weather", "source_ref": "x", "title": "t"},
        headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post("/lab/qms/nonconformances", json={
        "source_type": "manual", "title": "t"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# D16(f) — PT failure cannot close without a corrective action
# ---------------------------------------------------------------------------

def test_d16_pt_failure_requires_corrective_action_to_close(client):
    # A failed PT record filed through the internal EQA machinery.
    r = client.post("/lab/eqa", json={
        "event": "PT-2026-Q1", "analyte_code": POTASSIUM,
        "reported_value": 9.9, "acceptable_low": 3.5,
        "acceptable_high": 4.5}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    failed_id = r.json()["id"]
    # A passing record in the same event.
    r = client.post("/lab/eqa", json={
        "event": "PT-2026-Q1", "analyte_code": POTASSIUM,
        "reported_value": 4.0, "acceptable_low": 3.5,
        "acceptable_high": 4.5}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    passed_id = r.json()["id"]
    events = client.get("/lab/qms/pt-events", headers=CLINICIAN).json()
    failed = next(e for e in events["items"]
                  if e["record_id"] == failed_id)
    assert failed["passed"] is False and failed["closeable"] is False
    # Closing the failure refuses BY NAME.
    r = client.post(f"/lab/qms/pt-events/{failed_id}/close",
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "corrective action" in r.json()["detail"]
    assert "PT-2026-Q1" in r.json()["detail"]
    # A nonconformance WITHOUT corrective-action text is not enough.
    r = client.post("/lab/qms/nonconformances", json={
        "source_type": "pt-failure", "source_ref": str(failed_id),
        "title": "PT failure PT-2026-Q1 potassium (synthetic)"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post(f"/lab/qms/pt-events/{failed_id}/close",
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    # Add the corrective-action text via the assign endpoint -> close.
    nc_id = client.get("/lab/qms/nonconformances",
                       params={"source_type": "pt-failure"},
                       headers=CLINICIAN).json()["items"][0]["nc_id"]
    r = client.post(f"/lab/qms/nonconformances/{nc_id}/assign",
                    json={"corrective_action":
                          "Root cause: transcription error; retrain + "
                          "second-person check (synthetic)"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/lab/qms/pt-events/{failed_id}/close",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["closure"]["nonconformance_id"] == nc_id
    # Double close refuses; the passing record closes directly.
    r = client.post(f"/lab/qms/pt-events/{failed_id}/close",
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    r = client.post(f"/lab/qms/pt-events/{passed_id}/close",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["closure"]["nonconformance_id"] is None


# ---------------------------------------------------------------------------
# D16(d) — SOP library: versions, acknowledge, overdue reviews
# ---------------------------------------------------------------------------

def test_d16_sop_versions_acknowledgements_overdue(client):
    old = date.today() - timedelta(days=400)
    r = client.post("/lab/qms/sops", json={
        "sop_code": "SOP-GLU-01", "title": "Glucose testing procedure",
        "body": "Synthetic procedure v1 text",
        "effective_date": old.isoformat(),
        "review_due_date": (date.today()
                            - timedelta(days=1)).isoformat()},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    sop_v1 = r.json()["sop"]
    assert sop_v1["version"] == 1 and sop_v1["review_overdue"] is True
    listing = client.get("/lab/qms/sops", headers=CLINICIAN).json()
    assert [i["sop_id"] for i in listing["overdue_reviews"]] == [
        sop_v1["sop_id"]]
    # Acknowledge v1 as staff-008; a second acknowledge is idempotent.
    r = client.post(f"/lab/qms/sops/{sop_v1['sop_id']}/acknowledge",
                    json={"staff_id": "staff-008"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    ack = r.json()["acknowledgement"]
    assert ack["version"] == 1 and ack["staff_id"] == "staff-008"
    r = client.post(f"/lab/qms/sops/{sop_v1['sop_id']}/acknowledge",
                    json={"staff_id": "staff-008"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["already_acknowledged"] is True
    # New version supersedes v1; acknowledging the old one refuses.
    r = client.post("/lab/qms/sops", json={
        "sop_code": "SOP-GLU-01", "title": "Glucose testing procedure",
        "body": "Synthetic procedure v2 text"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    sop_v2 = r.json()["sop"]
    assert sop_v2["version"] == 2 and sop_v2["review_overdue"] is False
    # Default review due = effective + governed 12 months.
    assert sop_v2["review_due_date"] > date.today().isoformat()
    r = client.post(f"/lab/qms/sops/{sop_v1['sop_id']}/acknowledge",
                    json={"staff_id": "staff-008"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "superseded" in r.json()["detail"]
    listing = client.get("/lab/qms/sops", headers=CLINICIAN).json()
    versions = {i["version"]: i for i in listing["items"]
                if i["sop_code"] == "SOP-GLU-01"}
    assert versions[1]["status"] == "superseded"
    assert versions[1]["acknowledgements"] == 1
    assert versions[2]["status"] == "current"
    assert listing["overdue_reviews"] == []
    acks = client.get(
        f"/lab/qms/sops/{sop_v1['sop_id']}/acknowledgements",
        headers=CLINICIAN).json()
    assert acks["total"] == 1


# ---------------------------------------------------------------------------
# D16(e) — internal-audit programme: finding raises a nonconformance
# ---------------------------------------------------------------------------

def test_d16_audit_finding_raises_nonconformance(client):
    r = client.post("/lab/qms/audits", json={
        "title": "Q3 POCT site audit (synthetic)",
        "checklist_template": "poct-site-tracer",
        "auditor": "staff-008", "tracer_ref": "Patient/pat-1"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    audit = r.json()["audit"]
    assert audit["checklist_items"]  # from the governed template
    assert audit["status"] == "scheduled"
    r = client.post(f"/lab/qms/audits/{audit['audit_id']}/findings",
                    json={"finding": "Daily QC missing for one device "
                                      "on the tracer day (synthetic)",
                          "severity": "major",
                          "raise_nonconformance": True,
                          "owner": "staff-008"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["nonconformance"]["source_type"] == "audit-finding"
    assert body["nonconformance"]["source_ref"] == audit["audit_id"]
    assert body["finding"]["nonconformance_id"] == \
        body["nonconformance"]["nc_id"]
    listing = client.get("/lab/qms/audits", headers=CLINICIAN).json()
    mine = next(a for a in listing["items"]
                if a["audit_id"] == audit["audit_id"])
    assert mine["status"] == "in-progress"
    assert len(mine["findings"]) == 1
    assert listing["checklist_templates"]
    # Unknown template refuses by name.
    r = client.post("/lab/qms/audits", json={
        "title": "Bad template", "checklist_template": "nope",
        "auditor": "staff-008"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
