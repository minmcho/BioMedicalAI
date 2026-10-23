"""LAB-D14 (Lab Depth pass, W8) — molecular diagnostics discipline.

Extraction batches + amplification runs, Ct interpretation from
governed bands (detected / not-detected / inconclusive edges),
internal + extraction control failure semantics (a failed control
invalidates THAT sample's negative — inconclusive, named, never a
silent negative), the NTC RUN-VALIDITY GATE (positive NTC discards
every result in the run, release refused by name, explicit repeat
workflow), unresolved NTC blocking release, the contamination
workflow (blast radius, new-work block until decontamination is
recorded with negative verification swabs), dirty -> clean area
progression flagging, and governed batching limits.

The depth router is included on the test app directly: main.py
wiring is the coordinator's step (see the W8 INTEGRATION NOTE), and
including the router here exercises exactly the module the
coordinator will wire. Release lands through app.lab._create_results
(the shared validation core), so released molecular results appear
on the standard verification queue.

Synthetic data only."""
import pytest
from fastapi.testclient import TestClient

from app import lab_molecular
from app.main import create_app
from tests.conftest import ADMIN, CLINICIAN

ASSAY = "94500-6"
TARGET = "sars-cov-2"
CLEAN = "clean-pre-amp"
EXTRACT = "specimen-extraction"
DIRTY = "post-amp-dirty"


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
    app.include_router(lab_molecular.router)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic", "given": ["W8"]}]},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


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


def _order_in_process(client, pid):
    """One NAAT order driven to in-process (resultable on the spine)."""
    _patient(client, pid)
    r = client.post("/lab/orders",
                    json={"code": ASSAY,
                          "subject": {"reference": f"Patient/{pid}"}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    order_id = r.json()["id"]
    for to_status in ("collected", "received", "in-process"):
        r = client.post(f"/lab/orders/{order_id}/transition",
                        json={"to_status": to_status}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    return order_id


def _batch(client, order_ids, instrument="MOL-INST-1", areas=None,
           expect=201):
    r = client.post("/lab/molecular/batches", json={
        "assay_code": ASSAY, "instrument_id": instrument,
        "samples": [{"order_id": oid} for oid in order_ids],
        "area_stamps": [{"area": a} for a in (
            areas if areas is not None else [CLEAN, EXTRACT, DIRTY])]},
        headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r.json()


def _run(client, batch_id, expect=201):
    r = client.post("/lab/molecular/runs", json={"batch_id": batch_id},
                    headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r.json()


def _controls(client, run_id, ntc_ct=None, pc="passed", expect=200):
    payload = {"ntc_ct": ntc_ct, "positive_control_status": pc,
               "positive_control_ct": 28.0}
    r = client.post(f"/lab/molecular/runs/{run_id}/controls",
                    json=payload, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r.json()


def _result_entry(order_id, ct, internal="passed", extraction="passed"):
    return {"order_id": order_id, "ct_values": {TARGET: ct},
            "internal_control": {"status": internal, "ct": 30.0},
            "extraction_control": {"status": extraction, "ct": 29.0}}


def _results(client, run_id, entries, expect=200):
    r = client.post(f"/lab/molecular/runs/{run_id}/results",
                    json={"results": entries}, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r.json()


def _release(client, run_id, expect=200):
    r = client.post(f"/lab/molecular/runs/{run_id}/release",
                    headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r.json()


def _run_detail(client, run_id):
    r = client.get(f"/lab/molecular/runs/{run_id}", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def _samples_by_order(run_detail):
    return {s["order_id"]: s for s in run_detail["samples"]}


def _decontaminate(client, event_id, swab_result="negative", expect=200):
    r = client.post(
        f"/lab/molecular/contamination-events/{event_id}/decontaminate",
        json={"method": "sodium-hypochlorite", "areas": [DIRTY],
              "verification_swabs": [{"area": DIRTY,
                                      "result": swab_result}]},
        headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r.json()


def _setup_run(client, n_orders=1, instrument="MOL-INST-1"):
    order_ids = [_order_in_process(client, f"pat-{i + 1}")
                 for i in range(n_orders)]
    batch = _batch(client, order_ids, instrument=instrument)
    run = _run(client, batch["id"])
    return order_ids, batch, run


# ---------------------------------------------------------------------------
# Happy path: Ct bands + release onto the shared spine
# ---------------------------------------------------------------------------

def test_run_lifecycle_happy_path_with_ct_interpretation_bands(client):
    order_ids, batch, run = _setup_run(client, n_orders=6)
    assert run["ntc"]["status"] == "unresolved"
    assert run["gate"]["can_release"] is False
    cts = [25.0, 37.0, 39.0, 40.0, 41.0, None]
    _results(client, run["id"],
             [_result_entry(oid, ct) for oid, ct in zip(order_ids, cts)])
    detail = _run_detail(client, run["id"])
    by_order = _samples_by_order(detail)
    # Governed bands: <=37 detected (37 is the edge), 37<Ct<=40
    # inconclusive (40 is the edge), >40 or no amplification is
    # not-detected.
    expected = ["Detected", "Detected", "Inconclusive", "Inconclusive",
                "Not detected", "Not detected"]
    for oid, want in zip(order_ids, expected):
        sample = by_order[oid]
        assert sample["interpretation"] == want, sample
        assert sample["target_interpretations"][TARGET] in (
            "detected", "inconclusive", "not-detected")
    out = _controls(client, run["id"], ntc_ct=None)
    assert out["ntc"]["status"] == "negative"
    assert out["gate"]["can_release"] is True
    released = _release(client, run["id"])
    assert released["status"] == "released"
    assert len(released["released"]) == 6
    report_ids = {s["report_id"] for s in released["samples"]}
    assert None not in report_ids and len(report_ids) == 6
    # The results landed on the SHARED spine: they sit on the
    # standard verification queue as preliminary lab reports.
    queue = client.get("/lab/verification-queue", headers=CLINICIAN)
    assert queue.status_code == 200, queue.text
    queued = {item["report"]["id"] for item in queue.json()["items"]}
    assert report_ids <= queued
    values = {}
    for item in queue.json()["items"]:
        if item["report"]["id"] in report_ids:
            values[item["order_id"]] = item["results"][0]["value"]
    assert values[order_ids[0]] == "Detected"
    assert values[order_ids[5]] == "Not detected"


def test_batch_rejects_order_for_a_different_assay(client):
    order_id = _order_in_process(client, "pat-x")
    r = client.post("/lab/molecular/batches", json={
        "assay_code": "VIR-INFA", "instrument_id": "MOL-INST-1",
        "samples": [{"order_id": order_id}], "area_stamps": []},
        headers=CLINICIAN)
    assert r.status_code == 422, r.text
    assert "not molecular assay" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Control failure semantics
# ---------------------------------------------------------------------------

def test_internal_control_failure_makes_sample_inconclusive_not_negative(client):
    order_ids, _batch_row, run = _setup_run(client, n_orders=2)
    _results(client, run["id"], [
        # No amplification + failed internal control: the negative
        # is INVALID — inconclusive, named, never a silent negative.
        _result_entry(order_ids[0], None, internal="failed"),
        # Failed extraction control + detected target: the positive
        # stands (amplification occurred), failure recorded.
        _result_entry(order_ids[1], 24.0, extraction="failed"),
    ])
    by_order = _samples_by_order(_run_detail(client, run["id"]))
    neg = by_order[order_ids[0]]
    assert neg["interpretation"] == "Inconclusive"
    assert neg["validity"] == "inconclusive"
    assert "invalidates this sample's negative" in neg["validity_reason"]
    assert "internal amplification control FAILED" in neg["validity_reason"]
    pos = by_order[order_ids[1]]
    assert pos["interpretation"] == "Detected"
    assert pos["validity"] == "valid"
    assert "positive stands" in pos["validity_reason"]


def test_control_failure_pattern_raises_contamination_event(client):
    order_ids, _batch_row, run = _setup_run(client, n_orders=2)
    out = _results(client, run["id"], [
        _result_entry(order_ids[0], None, internal="failed"),
        _result_entry(order_ids[1], None, extraction="failed"),
    ])
    event = out["contamination_event"]
    assert event["trigger"] == "control-failure-pattern"
    assert event["status"] == "open"
    assert run["id"] in event["affected_run_ids"]
    # The run's instrument is now blocked for NEW work until the
    # event is decontaminated — batch creation itself refuses.
    orders2 = [_order_in_process(client, "pat-9")]
    r = client.post("/lab/molecular/batches", json={
        "assay_code": ASSAY, "instrument_id": "MOL-INST-1",
        "samples": [{"order_id": orders2[0]}],
        "area_stamps": [{"area": CLEAN}]}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "contamination event" in r.json()["detail"]


# ---------------------------------------------------------------------------
# The NTC run-validity gate
# ---------------------------------------------------------------------------

def test_ntc_positive_invalidates_the_entire_run(client):
    order_ids, _batch_row, run = _setup_run(client, n_orders=2)
    _results(client, run["id"], [
        _result_entry(order_ids[0], 26.0),
        _result_entry(order_ids[1], None),
    ])
    out = _controls(client, run["id"], ntc_ct=22.5)
    assert out["status"] == "invalidated"
    assert out["ntc"]["status"] == "positive"
    assert "NTC positive" in out["invalidated_reason"]
    # EVERY result is discarded: live interpretation cleared, the
    # computed value preserved only as the discarded record.
    by_order = _samples_by_order(out)
    assert by_order[order_ids[0]]["validity"] == "invalidated"
    assert by_order[order_ids[0]]["interpretation"] is None
    assert by_order[order_ids[0]]["discarded_interpretation"] == "Detected"
    assert by_order[order_ids[1]]["validity"] == "invalidated"
    assert by_order[order_ids[1]]["discarded_interpretation"] == \
        "Not detected"
    # A contamination event was raised for the run's scope.
    event = out["contamination_event"]
    assert event["trigger"] == "ntc-positive"
    assert run["id"] in event["affected_run_ids"]
    # Release refuses BY NAME, and nothing reached the spine.
    r = client.post(f"/lab/molecular/runs/{run['id']}/release",
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "INVALIDATED" in r.json()["detail"]
    queue = client.get("/lab/verification-queue", headers=CLINICIAN)
    assert queue.json()["total"] == 0
    # The repeat workflow is blocked while contamination is open…
    r = client.post(f"/lab/molecular/runs/{run['id']}/repeat",
                    json={"repeat_from": "amplification"},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "contamination event" in r.json()["detail"]
    # …and works once decontamination is recorded, with the governed
    # choice recorded on both runs.
    _decontaminate(client, event["id"])
    r = client.post(f"/lab/molecular/runs/{run['id']}/repeat",
                    json={"repeat_from": "amplification"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    new_run = r.json()
    assert new_run["repeat_of_run_id"] == run["id"]
    assert new_run["repeat_from"] == "amplification"
    assert new_run["batch_id"] == run["batch_id"]
    assert new_run["status"] == "open"
    assert len(new_run["samples"]) == 2
    old = _run_detail(client, run["id"])
    assert old["repeat_run_id"] == new_run["id"]
    assert old["repeat_from"] == "amplification"


def test_repeat_from_extraction_creates_a_new_batch(client):
    order_ids, _batch_row, run = _setup_run(client, n_orders=1)
    _results(client, run["id"], [_result_entry(order_ids[0], 26.0)])
    out = _controls(client, run["id"], ntc_ct=20.0)
    _decontaminate(client, out["contamination_event"]["id"])
    r = client.post(f"/lab/molecular/runs/{run['id']}/repeat",
                    json={"repeat_from": "extraction"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    new_run = r.json()
    assert new_run["repeat_from"] == "extraction"
    assert new_run["batch_id"] != run["batch_id"]
    batches = client.get("/lab/molecular/batches", headers=CLINICIAN)
    assert batches.json()["total"] == 2


def test_unresolved_ntc_blocks_release_by_name(client):
    order_ids, _batch_row, run = _setup_run(client, n_orders=1)
    _results(client, run["id"], [_result_entry(order_ids[0], 26.0)])
    r = client.post(f"/lab/molecular/runs/{run['id']}/release",
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "NTC gate" in detail and "UNRESOLVED" in detail
    # Zero partial release: the order never reached the spine.
    queue = client.get("/lab/verification-queue", headers=CLINICIAN)
    assert queue.json()["total"] == 0
    # A failed positive control blocks by name too, NTC negative.
    _controls(client, run["id"], ntc_ct=None, pc="failed")
    r = client.post(f"/lab/molecular/runs/{run['id']}/release",
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "positive control FAILED" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Contamination workflow
# ---------------------------------------------------------------------------

def test_contamination_event_blast_radius_and_return_to_service(client):
    orders_a = [_order_in_process(client, "pat-a")]
    batch_a = _batch(client, orders_a, instrument="MOL-INST-9")
    run_a = _run(client, batch_a["id"])
    orders_b = [_order_in_process(client, "pat-b")]
    batch_b = _batch(client, orders_b, instrument="MOL-INST-8")
    run_b = _run(client, batch_b["id"])
    r = client.post("/lab/molecular/contamination-events", json={
        "trigger": "manual", "instrument_id": "MOL-INST-9",
        "areas": [DIRTY], "note": "Environmental monitoring positive"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    event = r.json()
    # Blast radius: the instrument's run AND every run whose batch
    # moved through the named area.
    assert run_a["id"] in event["affected_run_ids"]
    assert run_b["id"] in event["affected_run_ids"]
    assert batch_a["id"] in event["affected_batch_ids"]
    assert batch_b["id"] in event["affected_batch_ids"]
    # New work on the contaminated instrument refuses by name.
    orders_c = [_order_in_process(client, "pat-c")]
    r = client.post("/lab/molecular/batches", json={
        "assay_code": ASSAY, "instrument_id": "MOL-INST-9",
        "samples": [{"order_id": orders_c[0]}],
        "area_stamps": [{"area": CLEAN}]}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "contamination event" in r.json()["detail"]
    assert "not yet cleared" in r.json()["detail"]
    # A positive verification swab does NOT clear the event.
    _decontaminate(client, event["id"], swab_result="positive",
                   expect=409)
    events = client.get("/lab/molecular/contamination-events",
                        headers=CLINICIAN)
    assert events.json()["items"][0]["status"] == "open"
    # Negative swabs + a governed method clear it; work resumes.
    cleared = _decontaminate(client, event["id"])
    assert cleared["status"] == "cleared"
    assert cleared["decontamination"]["method"] == "sodium-hypochlorite"
    assert cleared["decontamination"]["verification_swabs"][0][
        "result"] == "negative"
    ok = _batch(client, orders_c, instrument="MOL-INST-9")
    assert ok["assay_code"] == ASSAY


# ---------------------------------------------------------------------------
# Unidirectional workflow + governed batching
# ---------------------------------------------------------------------------

def test_dirty_to_clean_area_progression_is_flagged_by_name(client):
    order_id = _order_in_process(client, "pat-1")
    batch = _batch(client, [order_id], areas=[CLEAN, EXTRACT, DIRTY])
    assert batch["workflow_flagged"] is False
    flagged = _batch(client, [_order_in_process(client, "pat-2")],
                     areas=[DIRTY, CLEAN])
    assert flagged["workflow_flagged"] is True
    assert "dirty-to-clean" in flagged["workflow_flag_reason"]
    assert "post-amp-dirty" in flagged["workflow_flag_reason"]
    assert "clean-pre-amp" in flagged["workflow_flag_reason"]
    # Appending a backwards stamp flags an initially clean batch.
    r = client.post(
        f"/lab/molecular/batches/{batch['id']}/area-stamps",
        json={"area_stamps": [{"area": CLEAN}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["workflow_flagged"] is True
    stamps = r.json()["area_stamps"]
    assert stamps[-1]["flagged"] is True
    assert stamps[0]["flagged"] is False


def test_batching_limit_from_governed_config_is_enforced(client):
    payload = lab_molecular.default_molecular_payload()
    for assay in payload["assays"]:
        if assay["code"] == ASSAY:
            assay["maxBatchSize"] = 2
    _publish(client, "terminology", "lab-molecular", payload)
    order_ids = [_order_in_process(client, f"pat-{i}") for i in (1, 2, 3)]
    r = client.post("/lab/molecular/batches", json={
        "assay_code": ASSAY, "instrument_id": "MOL-INST-1",
        "samples": [{"order_id": oid} for oid in order_ids],
        "area_stamps": []}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    assert "max batch size 2" in r.json()["detail"]
    ok = _batch(client, order_ids[:2])
    assert ok["sample_count"] == 2
