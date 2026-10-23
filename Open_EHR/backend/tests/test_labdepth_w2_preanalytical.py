"""LAB-D3/D4/D5 — pre-analytical depth (Lab Depth pass, worker W2).

Accession minted at laboratory RECEIPT (collection keeps its
collection ID; both resolvable), receipt checklist failures routing
into the EXISTING coded rejection flow, transport exception flags
(never-tube / transit / temperature), storage + retention due dates
+ disposal workflow, retrieval events, temperature-excursion linkage
from the device registry, and the send-out catalog-as-contract:
packing refusals named with zero dispatch, overdue chase list,
performing-laboratory stamping, and the unsuitable-return path.

Synthetic data only. Fixture dates derive from the UTC date.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import lab_preanalytical
from app.main import create_app
from tests.conftest import CLINICIAN


@pytest.fixture()
def client(tmp_path):
    """The conftest client with this workstream's router mounted —
    the coordinator wires lab_preanalytical into app.main at pass
    integration (see the worker's INTEGRATION NOTE); mounting it
    here keeps this suite runnable on the unwired tree."""
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
    app.include_router(lab_preanalytical.router)
    with TestClient(app) as c:
        yield c

CBC = "58410-2"
METANEPH = "SO-METANEPH"
VLCFA = "SO-VLCFA"

FULL_CHECKLIST = [
    {"code": "label-legible", "passed": True},
    {"code": "container-correct", "passed": True},
    {"code": "volume-adequate", "passed": True},
    {"code": "no-haemolysis", "passed": True},
    {"code": "no-clot", "passed": True},
    {"code": "temperature-in-range", "passed": True},
    {"code": "within-stability", "passed": True},
]


def _patient(client, pid="pat-w2"):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic", "given": ["Pre"]}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _order(client, code=CBC, patient="pat-w2"):
    r = client.post("/lab/orders",
                    json={"code": code,
                          "subject": {"reference": f"Patient/{patient}"}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _collect(client, order_id, specimen_type="Blood specimen"):
    r = client.post(f"/lab/orders/{order_id}/transition",
                    json={"to_status": "collected",
                          "specimen_type": specimen_type},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()["specimen"]


def _queue_item(client, specimen_id, bench=None):
    url = "/lab/preanalytical/receiving-queue"
    if bench:
        url += f"?bench={bench}"
    r = client.get(url, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    for item in r.json()["items"]:
        if item["specimen_id"] == specimen_id:
            return item
    return None


def _receive(client, specimen_id, bench, checklist=None, expect=201, **extra):
    body = {"specimen_id": specimen_id, "bench_code": bench,
            "checklist": checklist or FULL_CHECKLIST}
    body.update(extra)
    r = client.post("/lab/preanalytical/receive", json=body, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _collected(client, code=CBC, specimen_type="Blood specimen",
               patient="pat-w2"):
    order = _order(client, code=code, patient=patient)
    specimen = _collect(client, order["id"], specimen_type)
    return order, specimen


def _received(client, code=CBC, specimen_type="Blood specimen",
              patient="pat-w2"):
    order, specimen = _collected(client, code, specimen_type, patient)
    item = _queue_item(client, specimen["id"])
    assert item is not None
    r = _receive(client, specimen["id"], item["bench_code"])
    return order, specimen, r.json()


def _body(tmp_path, resource_type, resource_id):
    con = sqlite3.connect(f"{tmp_path}/test.db")
    row = con.execute(
        "SELECT body FROM fhir_resources WHERE resource_type=? AND resource_id=?",
        (resource_type, resource_id)).fetchone()
    con.close()
    return json.loads(row[0])


def _rewrite_body(tmp_path, resource_type, resource_id, body):
    con = sqlite3.connect(f"{tmp_path}/test.db")
    con.execute(
        "UPDATE fhir_resources SET body=? WHERE resource_type=? AND resource_id=?",
        (json.dumps(body), resource_type, resource_id))
    con.commit()
    con.close()


def _utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# D3 — accessioning
# ---------------------------------------------------------------------------

def test_accession_minted_at_receipt_not_collection(client):
    _patient(client)
    order, specimen = _collected(client)
    collection_id = specimen["accessionIdentifier"]["value"]
    # Before receipt: the specimen carries ONLY its collection ID —
    # no laboratory accession exists yet.
    r = client.get(f"/lab/preanalytical/resolve/{collection_id}",
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    resolved = r.json()
    assert resolved["collection_id"] == collection_id
    assert resolved["accession"] is None
    assert resolved["received"] is False
    # It sits on a receiving bench's queue, accession still None.
    item = _queue_item(client, specimen["id"])
    assert item is not None and item["accession"] is None
    assert item["collection_id"] == collection_id
    bench = item["bench_code"]
    assert bench  # routed bench from the published catalog
    # Receipt mints the accession HERE.
    r = _receive(client, specimen["id"], bench)
    outcome = r.json()
    accession = outcome["accession"]
    assert accession and accession != collection_id
    assert accession.startswith("ACC-")
    assert outcome["collection_id"] == collection_id
    # The specimen record now carries the accession; the collection
    # ID is preserved on it.
    detail = client.get(f"/fhir/Specimen/{specimen['id']}",
                        headers=CLINICIAN).json()
    assert detail["accessionIdentifier"]["value"] == accession
    preserved = {i["value"] for i in detail.get("identifier") or []}
    assert collection_id in preserved
    # BOTH identifiers resolve to the same specimen, as does the id.
    for identifier in (collection_id, accession, specimen["id"]):
        r = client.get(f"/lab/preanalytical/resolve/{identifier}",
                       headers=CLINICIAN)
        assert r.status_code == 200, (identifier, r.text)
        assert r.json()["specimen_id"] == specimen["id"]
        assert r.json()["accession"] == accession
        assert r.json()["collection_id"] == collection_id
    # And it has left the receiving queue.
    assert _queue_item(client, specimen["id"]) is None


def test_receipt_requires_complete_checklist_and_known_bench(client):
    _patient(client)
    _order_rec, specimen = _collected(client)
    item = _queue_item(client, specimen["id"])
    r = client.post("/lab/preanalytical/receive",
                    json={"specimen_id": specimen["id"],
                          "bench_code": "no-such-bench",
                          "checklist": FULL_CHECKLIST}, headers=CLINICIAN)
    assert r.status_code == 404, r.text
    r = client.post("/lab/preanalytical/receive",
                    json={"specimen_id": specimen["id"],
                          "bench_code": item["bench_code"],
                          "checklist": FULL_CHECKLIST[:3]}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    assert "missing required" in r.json()["detail"]


def test_checklist_failure_routes_to_coded_rejection(client):
    _patient(client)
    order, specimen = _collected(client)
    item = _queue_item(client, specimen["id"])
    checklist = [dict(e) for e in FULL_CHECKLIST]
    for entry in checklist:
        if entry["code"] == "no-haemolysis":
            entry["passed"] = False
    r = _receive(client, specimen["id"], item["bench_code"],
                 checklist=checklist)
    outcome = r.json()
    assert outcome["rejected"] is True
    assert outcome["receipt"]["status"] == "rejected"
    assert outcome["receipt"]["rejection_criterion"] == "hemolysed"
    assert outcome["receipt"]["accession"] is None
    # The EXISTING rejection flow ran: order rejected with the coded
    # criterion in its rejection history, and no accession was minted.
    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "rejected"
    r = client.get(f"/lab/preanalytical/resolve/{specimen['id']}",
                   headers=CLINICIAN)
    assert r.json()["accession"] is None
    # The recollect path of the existing flow is live for this order.
    r = client.post(f"/lab/orders/{order['id']}/recollect",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text


def test_checklist_failure_uses_failed_items_criterion(client, tmp_path):
    _patient(client)
    order, specimen = _collected(client)
    item = _queue_item(client, specimen["id"])
    checklist = [dict(e) for e in FULL_CHECKLIST]
    for entry in checklist:
        if entry["code"] == "container-correct":
            entry["passed"] = False
    _receive(client, specimen["id"], item["bench_code"], checklist=checklist)
    body = _body(tmp_path, "ServiceRequest", order["id"])
    history = body.get("rejectionHistory") or []
    assert history and history[-1]["criterion_code"] == "wrong-container"
    assert (body.get("statusReason") or {}).get("coding", [{}])[0].get(
        "code") == "wrong-container"


# ---------------------------------------------------------------------------
# D3 — transport rules + exception flags
# ---------------------------------------------------------------------------

def test_never_tube_specimen_flagged(client):
    _patient(client)
    _order_rec, specimen = _collected(client, specimen_type="Sputum specimen")
    r = client.post("/lab/preanalytical/transport-scan",
                    json={"specimen_id": specimen["id"],
                          "method": "pneumatic-tube",
                          "temperature_condition": "ambient"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    codes = [e["code"] for e in r.json()["transport"]["exceptions"]]
    assert "never-tube" in codes
    # The flag is ON the specimen record.
    detail = client.get(f"/fhir/Specimen/{specimen['id']}",
                        headers=CLINICIAN).json()
    stamped = [e.get("valueString", "") for e in detail.get("extension") or []]
    assert any(s.startswith("never-tube") for s in stamped)
    # And on the exception worklist.
    r = client.get("/lab/preanalytical/transport-exceptions", headers=CLINICIAN)
    assert r.json()["total"] == 1
    # Hand-carrying the same specimen type flags nothing.
    _order2, specimen2 = _collected(client, specimen_type="Sputum specimen")
    r = client.post("/lab/preanalytical/transport-scan",
                    json={"specimen_id": specimen2["id"],
                          "method": "hand-carry",
                          "temperature_condition": "ambient"},
                    headers=CLINICIAN)
    assert r.json()["transport"]["exceptions"] == []


def test_transit_and_temperature_exceptions_flagged(client, tmp_path):
    _patient(client)
    _order_rec, specimen = _collected(client)  # Blood: ambient, max 240 min
    body = _body(tmp_path, "Specimen", specimen["id"])
    collected = datetime.fromisoformat(
        body["collection"]["collectedDateTime"].replace("Z", "+00:00"))
    arrived = collected + timedelta(minutes=300)
    r = client.post("/lab/preanalytical/transport-scan",
                    json={"specimen_id": specimen["id"],
                          "method": "courier",
                          "temperature_condition": "refrigerated",
                          "at": arrived.isoformat()},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    transport = r.json()["transport"]
    assert transport["transit_minutes"] == 300.0
    codes = [e["code"] for e in transport["exceptions"]]
    assert "transit-over-max" in codes
    assert "temperature-breach" in codes
    # A compliant movement flags nothing.
    _o, specimen2 = _collected(client)
    r = client.post("/lab/preanalytical/transport-scan",
                    json={"specimen_id": specimen2["id"],
                          "method": "pneumatic-tube",
                          "temperature_condition": "ambient"},
                    headers=CLINICIAN)
    assert r.json()["transport"]["exceptions"] == []


# ---------------------------------------------------------------------------
# D4 — storage, retention, disposal, retrieval, excursions
# ---------------------------------------------------------------------------

def _stored(client, specimen_id, location="fridge-retained-a"):
    r = client.post("/lab/preanalytical/storage/store",
                    json={"specimen_id": specimen_id,
                          "location_code": location}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["storage"]


def test_storage_retention_due_dates_per_governed_schedule(client):
    _patient(client)
    _o, blood, _r = _received(client)  # Blood specimen: hold 7 days
    stored = _stored(client, blood["id"])
    stored_at = datetime.fromisoformat(stored["stored_at"])
    due = datetime.fromisoformat(stored["disposal_due"])
    assert (due.date() - stored_at.date()).days == 7
    _o2, urine, _r2 = _received(client, specimen_type="Urine specimen")
    stored2 = _stored(client, urine["id"])
    due2 = datetime.fromisoformat(stored2["disposal_due"])
    stored2_at = datetime.fromisoformat(stored2["stored_at"])
    assert (due2.date() - stored2_at.date()).days == 2
    # Neither is due today.
    r = client.get("/lab/preanalytical/storage/due", headers=CLINICIAN)
    assert r.json()["total"] == 0
    board = client.get("/lab/preanalytical/storage/board",
                       headers=CLINICIAN).json()
    assert len(board["stored"]) == 2


def test_storage_requires_receipt_and_known_location(client):
    _patient(client)
    _order_rec, specimen = _collected(client)
    r = client.post("/lab/preanalytical/storage/store",
                    json={"specimen_id": specimen["id"],
                          "location_code": "fridge-retained-a"},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text  # not accessioned yet
    _o, received_specimen, _r = _received(client)
    r = client.post("/lab/preanalytical/storage/store",
                    json={"specimen_id": received_specimen["id"],
                          "location_code": "no-such-place"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text


def test_disposal_workflow_and_due_list(client, tmp_path):
    _patient(client)
    _o, specimen, _r = _received(client, specimen_type="Urine specimen")
    stored = _stored(client, specimen["id"])
    # Early disposal without an override is refused, by name.
    r = client.post(f"/lab/preanalytical/storage/{specimen['id']}/dispose",
                    json={"reason": "Retention expired"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "not due for disposal" in r.json()["detail"]
    # Age the placement past its 2-day hold: now it is on the due list.
    con = sqlite3.connect(f"{tmp_path}/test.db")
    past = (_utcnow() - timedelta(days=3)).isoformat()
    con.execute("UPDATE lab_storage_records SET stored_at=?, disposal_due=?",
                (past, past))
    con.commit()
    con.close()
    r = client.get("/lab/preanalytical/storage/due", headers=CLINICIAN)
    assert [i["specimen_key"] for i in r.json()["items"]] == [specimen["id"]]
    # Disposal completes with actor + reason and leaves the due list.
    r = client.post(f"/lab/preanalytical/storage/{specimen['id']}/dispose",
                    json={"reason": "Retention expired"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    out = r.json()["storage"]
    assert out["status"] == "disposed"
    assert out["disposal_reason"] == "Retention expired"
    assert out["disposed_by"]
    r = client.get("/lab/preanalytical/storage/due", headers=CLINICIAN)
    assert r.json()["total"] == 0
    board = client.get("/lab/preanalytical/storage/board",
                       headers=CLINICIAN).json()
    assert len(board["disposed"]) == 1 and board["stored"] == []
    assert stored["id"] == out["id"]


def test_retrieval_events_recorded_with_purpose(client):
    _patient(client)
    _o, specimen, _r = _received(client)
    _stored(client, specimen["id"])
    r = client.post(f"/lab/preanalytical/storage/{specimen['id']}/retrieve",
                    json={"purpose": "teleport"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post(f"/lab/preanalytical/storage/{specimen['id']}/retrieve",
                    json={"purpose": "addon", "note": "Add-on request"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["retrieval"]["purpose"] == "addon"
    r = client.get(f"/lab/preanalytical/storage/{specimen['id']}/retrievals",
                   headers=CLINICIAN)
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["purpose"] == "addon"
    # A retrieved specimen is no longer stored: a second retrieval 404s.
    r = client.post(f"/lab/preanalytical/storage/{specimen['id']}/retrieve",
                    json={"purpose": "repeat"}, headers=CLINICIAN)
    assert r.status_code == 404, r.text


def test_temperature_excursion_flags_stored_specimens(client):
    _patient(client)
    _o, specimen, _r = _received(client)
    _stored(client, specimen["id"], location="fridge-retained-a")
    # An in-range reading flags nothing.
    r = client.post("/devices/ingest/reading",
                    json={"device_key": "fridge-lab-1", "metric": "temperature",
                          "value": 5.0, "unit": "C"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/lab/preanalytical/storage/evaluate-excursions",
                    json={}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["flags_created"] == 0
    assert r.json()["feed"]["live"] is False  # honest bound, labelled
    # An excursion reading (fridge-sensor range is 2-8 C) flags the
    # specimen stored in that unit.
    r = client.post("/devices/ingest/reading",
                    json={"device_key": "fridge-lab-1", "metric": "temperature",
                          "value": 14.5, "unit": "C"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["excursion"] is True
    r = client.post("/lab/preanalytical/storage/evaluate-excursions",
                    json={}, headers=CLINICIAN)
    assert r.json()["flags_created"] == 1
    flag = r.json()["flags"][0]
    assert flag["specimen_key"] == specimen["id"]
    assert flag["device_key"] == "fridge-lab-1"
    # Idempotent: re-evaluating creates no duplicate flag.
    r = client.post("/lab/preanalytical/storage/evaluate-excursions",
                    json={}, headers=CLINICIAN)
    assert r.json()["flags_created"] == 0
    link = client.get(
        f"/lab/preanalytical/storage/{specimen['id']}/temperature-link",
        headers=CLINICIAN).json()
    assert link["device_key"] == "fridge-lab-1"
    assert len(link["excursion_flags"]) == 1
    assert len(link["readings"]) == 2


# ---------------------------------------------------------------------------
# D5 — send-out contract: packing gate, dispatch, chase, stamp, return
# ---------------------------------------------------------------------------

def _sendout_received(client, code=METANEPH):
    return _received(client, code=code)


def _pack(client, order_ids, temperature="refrigerated", attachments=None,
          expect=201):
    body = {"order_ids": order_ids, "shipping_temperature": temperature,
            "attachments": attachments or {}}
    r = client.post("/lab/preanalytical/sendouts/pack", json=body,
                    headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _sendout_state(client, order_id):
    queue = client.get("/lab/sendouts", headers=CLINICIAN).json()
    for item in queue["items"]:
        if item["order_id"] == order_id:
            return item["sendout"]
    return None


def test_packing_refusals_named_and_zero_dispatch(client):
    _patient(client)
    order, _specimen, _r = _sendout_received(client)
    attachments = {order["id"]: ["requisition-form", "clinical-history"]}
    # Wrong shipping temperature (contract: refrigerated).
    r = _pack(client, [order["id"]], temperature="frozen",
              attachments=attachments, expect=422)
    assert "wrong shipping temperature" in r.json()["detail"]
    assert "refrigerated" in r.json()["detail"]
    # Missing necessary-information attachment.
    r = _pack(client, [order["id"]],
              attachments={order["id"]: ["requisition-form"]}, expect=422)
    assert "clinical-history" in r.json()["detail"]
    # Zero dispatch: no manifest exists and the order was never sent.
    r = client.get("/lab/preanalytical/sendouts/manifests", headers=CLINICIAN)
    assert r.json()["total"] == 0
    assert _sendout_state(client, order["id"])["status"] == "awaiting-dispatch"


def test_packing_stability_refusal(client, tmp_path):
    _patient(client)
    order, specimen, _r = _sendout_received(client)
    # Age the specimen beyond the 72 h refrigerated stability window.
    body = _body(tmp_path, "Specimen", specimen["id"])
    old = (_utcnow() - timedelta(hours=100)).isoformat()
    body["collection"]["collectedDateTime"] = old
    _rewrite_body(tmp_path, "Specimen", specimen["id"], body)
    r = _pack(client, [order["id"]],
              attachments={order["id"]: ["requisition-form",
                                          "clinical-history"]}, expect=422)
    assert "stability expired" in r.json()["detail"]
    assert _sendout_state(client, order["id"])["status"] == "awaiting-dispatch"


def test_packing_requires_accession(client):
    _patient(client)
    order, _specimen = _collected(client, code=METANEPH)  # not received
    r = _pack(client, [order["id"]],
              attachments={order["id"]: ["requisition-form",
                                          "clinical-history"]}, expect=409)
    assert "accessioned" in r.json()["detail"]


def _packed_and_dispatched(client, code=METANEPH, temperature="refrigerated",
                           attachments=("requisition-form",
                                          "clinical-history")):
    order, _specimen, _r = _sendout_received(client, code=code)
    r = _pack(client, [order["id"]], temperature=temperature,
              attachments={order["id"]: list(attachments)})
    manifest = r.json()["manifest"]
    r = client.post(
        f"/lab/preanalytical/sendouts/manifests/{manifest['manifest_key']}/dispatch",
        json={"tracking_ref": "SYN-TRACK-W2"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return order, r.json()["manifest"]


def test_pack_dispatch_and_contract_surfaces(client):
    _patient(client)
    contracts = client.get("/lab/preanalytical/sendouts/contracts",
                           headers=CLINICIAN).json()
    regional = [l for l in contracts["laboratories"]
                if l["lab_code"] == "ref-regional"][0]
    assert regional["performing_lab"]["name"]
    test = regional["tests"][0]
    assert test["classification"] == "FDA-cleared"
    assert test["stability"]["hours"] == 72
    assert test["report_window_hours"] == 120
    specialty = [l for l in contracts["laboratories"]
                 if l["lab_code"] == "ref-specialty"][0]
    assert specialty["tests"][0]["classification"] == "LDT"
    order, manifest = _packed_and_dispatched(client)
    assert manifest["status"] == "dispatched"
    assert manifest["split_instructions"][METANEPH]  # carried on the manifest
    assert manifest["expected_report_by"] is not None
    state = _sendout_state(client, order["id"])
    assert state["status"] == "dispatched"
    assert state["manifest_id"] == manifest["manifest_key"]
    assert state["classification"] == "FDA-cleared"
    # Not yet overdue.
    chase = client.get("/lab/preanalytical/sendouts/chase-list",
                       headers=CLINICIAN).json()
    assert chase["total"] == 0


def test_overdue_chase_list(client, tmp_path):
    _patient(client)
    order, _manifest = _packed_and_dispatched(client)
    # Age the dispatch beyond the 120 h report window.
    body = _body(tmp_path, "ServiceRequest", order["id"])
    body["sendout"]["dispatched_at"] = (
        _utcnow() - timedelta(hours=150)).isoformat()
    _rewrite_body(tmp_path, "ServiceRequest", order["id"], body)
    chase = client.get("/lab/preanalytical/sendouts/chase-list",
                       headers=CLINICIAN).json()
    assert chase["total"] == 1
    item = chase["items"][0]
    assert item["order_id"] == order["id"]
    assert item["overdue_hours"] >= 29  # 150 - 120, clock slack
    assert item["report_window_hours"] == 120.0


def test_receive_result_stamps_performing_lab(client):
    _patient(client)
    order, _manifest = _packed_and_dispatched(client)
    r = client.post(
        f"/lab/preanalytical/sendouts/{order['id']}/receive-result",
        json={"results": [{"code": METANEPH, "value": 42}]},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    outcome = r.json()
    assert outcome["classification"] == "FDA-cleared"
    assert outcome["performing_lab"]["code"] == "ref-regional"
    report = client.get(f"/fhir/DiagnosticReport/{outcome['report']['id']}",
                        headers=CLINICIAN).json()
    performers = report.get("performer") or []
    assert performers and "Regional Reference Laboratory" in \
        performers[0]["display"]
    state = _sendout_state(client, order["id"])
    assert state["status"] == "resulted"
    assert state["performing_lab"]["code"] == "ref-regional"


def test_sendout_return_reenters_receipt_with_coded_reason(client):
    _patient(client)
    order, _manifest = _packed_and_dispatched(client)
    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    specimen_id = detail["specimen"]["id"]
    r = client.post(f"/lab/preanalytical/sendouts/{order['id']}/return",
                    json={"reason_code": "not-a-reason"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post(f"/lab/preanalytical/sendouts/{order['id']}/return",
                    json={"reason_code": "temperature-excursion",
                          "note": "Arrived warm at reference lab"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["return"]["reason_code"] == "temperature-excursion"
    assert r.json()["re_enters_receipt"] is True
    # The specimen is back on the receiving queue, carrying the reason.
    item = _queue_item(client, specimen_id)
    assert item is not None
    assert item["returned_from_sendout"] is True
    assert item["return_reason_code"] == "temperature-excursion"
    # And it can be re-received (a fresh accession is minted).
    r = _receive(client, specimen_id, item["bench_code"])
    assert r.json()["rejected"] is False
    assert r.json()["accession"]
    assert _queue_item(client, specimen_id) is None
