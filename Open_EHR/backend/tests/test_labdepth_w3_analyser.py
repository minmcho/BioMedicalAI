"""Lab Depth W3 (LAB-D6) — analyser integration: LAB-m2 ingest
quarantine on every path, registry reconciliation, worklist,
analyser flags, and the rerun/dilution loop.

Synthetic data only (fabricated patients, instruments, values).
Routers are mounted here when app.main has not wired them yet
(coordinator wires via the W3 INTEGRATION NOTE); the mount is
skipped when already wired."""
import pytest
from fastapi.testclient import TestClient

from app import lab_analyser as lab_analyser_mod
from app import lab_autoverify as lab_autoverify_mod
from app.main import create_app
from tests.conftest import ADMIN, CLINICIAN, VIEWER


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/w3a.db")
    paths = {getattr(r, "path", None) for r in app.routes}
    if "/lab/analyser/instruments" not in paths:
        app.include_router(lab_analyser_mod.router)
    if "/lab/autoverify/status" not in paths:
        app.include_router(lab_autoverify_mod.router)
    with TestClient(app) as c:
        with app.state.session_factory() as session:
            lab_analyser_mod.ensure_lab_analyser_defaults(session)
            lab_autoverify_mod.ensure_lab_autoverify_defaults(session)
        yield c

CBC = "58410-2"
BMP = "51990-0"

CBC_RESULTS = [
    {"code": "718-7", "value": 13.5},
    {"code": "4544-3", "value": 41.0},
    {"code": "6690-2", "value": 7.2},
    {"code": "777-3", "value": 260},
]
BMP_RESULTS = [
    {"code": "2345-7", "value": 95},
    {"code": "3094-0", "value": 12},
    {"code": "2160-0", "value": 1.0},
    {"code": "2951-2", "value": 140},
    {"code": "2823-3", "value": 4.4},
]

HL7_TEMPLATE = "\r".join([
    "MSH|^~\\&|ANALYZER-X|CENTRAL LAB|OPENCARE|LIS|20261015080000||ORU^R01|MSG0001|P|2.5",
    "PID|1||pat-1||Synthetic^Lab||19680412|F",
    "OBR|1|{oid}|{oid}|58410-2^Complete blood count panel",
    "OBX|1|NM|718-7^Hemoglobin||13.2|g/dL|12.0-15.5{flag1}",
    "OBX|2|NM|4544-3^Hematocrit||40.5|%|36-46",
    "OBX|3|NM|6690-2^Leukocytes||6.8|10^9/L|4.0-11.0",
    "OBX|4|NM|777-3^Platelets||255|10^9/L|150-400",
])


def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic", "given": ["Lab"]}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _order(client, code=CBC, oid=None, patient="pat-1"):
    body = {"code": code, "subject": {"reference": f"Patient/{patient}"}}
    if oid:
        body["id"] = oid
    r = client.post("/lab/orders", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _to_received(client, order_id):
    for status in ("collected", "received"):
        r = client.post(f"/lab/orders/{order_id}/transition",
                        json={"to_status": status}, headers=CLINICIAN)
        assert r.status_code == 200, r.text


def _instrument(client, instrument_id="AN-1", status="active",
                bench="chemistry"):
    r = client.post("/lab/instruments",
                    json={"instrument_id": instrument_id,
                          "name": f"Analyser {instrument_id}",
                          "bench": bench, "status": status},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _quarantine(client):
    r = client.get("/lab/analyser/quarantine", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def _hl7(oid, flag1=""):
    return HL7_TEMPLATE.format(oid=oid, flag1=flag1)


# ---------------------------------------------------------------------------
# D6(a) LAB-m2: every ingest path quarantines a down instrument
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["hl7", "csv", "direct", "mllp"])
def test_every_ingest_path_quarantines_down_instrument(client, path):
    """Parametrised over the four ingest paths: HL7 post, CSV post,
    direct/API result entry, and the MLLP listener (via the device
    simulate surface, which runs the real inbound path). Each refuses
    with a NAMED quarantine record and writes zero results."""
    _patient(client)
    if path == "mllp":
        # Seeded device chem-analyser-1 (sender ANALYZER-X, MLLP gate
        # enabled by the seeded device config). Maintenance is NOT a
        # blocking status in the device gate, so the message reaches
        # the lab ingest core — where LAB-m2 must stop it.
        r = client.post("/devices/chem-analyser-1/status",
                        json={"status": "maintenance"}, headers=ADMIN)
        assert r.status_code == 200, r.text
        order = _order(client, code=CBC, oid="ord-mllp")
        _to_received(client, order["id"])
        r = client.post("/devices/simulate/oru",
                        json={"content": _hl7("ord-mllp")}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
        assert r.json()["ack_code"] in ("AR", "AE"), r.json()
        assert "liq-" in r.json()["detail"]
        instrument_id = "chem-analyser-1"
    else:
        status = "down" if path in ("hl7", "direct") else "maintenance"
        _instrument(client, "AN-DOWN", status=status)
        instrument_id = "AN-DOWN"
        if path == "hl7":
            order = _order(client, code=CBC, oid="ord-hl7")
            _to_received(client, order["id"])
            r = client.post("/lab/ingest",
                            json={"format": "hl7", "content": _hl7("ord-hl7"),
                                  "instrument_id": "AN-DOWN"},
                            headers=CLINICIAN)
            assert r.status_code == 409, r.text
            assert "liq-" in r.json()["detail"]
            assert "AN-DOWN" in r.json()["detail"]
        elif path == "csv":
            order = _order(client, code=BMP, oid="ord-csv")
            _to_received(client, order["id"])
            content = ("order_id,patient_id,code,value,unit\n"
                       "ord-csv,pat-1,2345-7,88,mg/dL\n"
                       "ord-csv,pat-1,2823-3,4.4,mmol/L\n")
            r = client.post("/lab/ingest",
                            json={"format": "csv", "content": content,
                                  "instrument_id": "AN-DOWN"},
                            headers=CLINICIAN)
            assert r.status_code == 409, r.text
            assert "liq-" in r.json()["detail"]
        else:  # direct/API entry naming the instrument
            order = _order(client, code=BMP, oid="ord-direct")
            _to_received(client, order["id"])
            r = client.post(f"/lab/orders/{order['id']}/results",
                            json={"results": BMP_RESULTS,
                                  "instrument_id": "AN-DOWN"},
                            headers=CLINICIAN)
            assert r.status_code == 409, r.text
            assert "liq-" in r.json()["detail"]
    # The named quarantine record: held, for this instrument, with the
    # batch payload preserved — and zero results written anywhere.
    q = _quarantine(client)
    assert q["total"] == 1 and q["held"] == 1
    held = q["items"][0]
    assert held["instrument_id"] == instrument_id
    assert held["status"] == "held"
    assert held["effective_status"] in ("in-maintenance", "out-of-service")
    assert held["payload"]["kind"] in ("groups", "results")
    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    assert detail["reports"] == []
    assert detail["order"]["status"] == "received"
    queue = client.get("/lab/verification-queue", headers=CLINICIAN).json()
    assert queue["total"] == 0


def test_active_instrument_ingest_still_lands(client):
    """The gate is a status gate, not a ban: an in-service instrument
    ingests exactly as before (drafts, unreleased)."""
    _patient(client)
    _instrument(client, "AN-OK", status="active")
    order = _order(client, code=CBC, oid="ord-ok")
    _to_received(client, order["id"])
    r = client.post("/lab/ingest",
                    json={"format": "hl7", "content": _hl7("ord-ok"),
                          "instrument_id": "AN-OK"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert _quarantine(client)["total"] == 0


# ---------------------------------------------------------------------------
# Quarantine disposition: reject is terminal; reprocess only in service
# ---------------------------------------------------------------------------

def _held_batch(client, oid="ord-q"):
    _patient(client)
    _instrument(client, "AN-Q", status="down")
    order = _order(client, code=CBC, oid=oid)
    _to_received(client, order["id"])
    r = client.post("/lab/ingest",
                    json={"format": "hl7", "content": _hl7(oid),
                          "instrument_id": "AN-Q"},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    held = _quarantine(client)["items"][0]
    return order, held


def test_quarantine_reject_is_terminal_and_audited(client):
    _order_held = _held_batch(client)
    _order_row, held = _order_held
    qid = held["quarantine_id"]
    # a rejection requires a named reason
    r = client.post(f"/lab/analyser/quarantine/{qid}/reject", json={},
                    headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post(f"/lab/analyser/quarantine/{qid}/reject",
                    json={"note": "Instrument was mid-maintenance; "
                                  "specimens re-collected"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"
    assert r.json()["decided_by"] == "dev-clinician"
    # terminal: neither reject nor reprocess applies again
    assert client.post(f"/lab/analyser/quarantine/{qid}/reject",
                       json={"note": "again"},
                       headers=CLINICIAN).status_code == 409
    assert client.post(f"/lab/analyser/quarantine/{qid}/reprocess",
                       json={}, headers=CLINICIAN).status_code == 409
    # the payload record survives as the history of what was refused
    q = _quarantine(client)
    assert q["items"][0]["payload"]["kind"] == "groups"


def test_quarantine_reprocess_only_when_back_in_service(client):
    order, held = _held_batch(client, oid="ord-q2")
    qid = held["quarantine_id"]
    original_payload = held["payload"]
    # still down: reprocess refuses, batch stays held, nothing lands
    r = client.post(f"/lab/analyser/quarantine/{qid}/reprocess", json={},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "back in service" in r.json()["detail"]
    assert _quarantine(client)["items"][0]["status"] == "held"
    # return to service through the reconciled status surface
    r = client.post("/lab/analyser/instruments/AN-Q/status",
                    json={"status": "in-service"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["effective_status"] == "in-service"
    # reprocess: NEW draft results land now; history is not rewritten
    r = client.post(f"/lab/analyser/quarantine/{qid}/reprocess",
                    json={"note": "analyser returned to service"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "reprocessed"
    assert len(done["reprocessed_report_ids"]) == 1
    assert done["payload"] == original_payload  # payload untouched
    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "resulted"
    assert [rep["id"] for rep in detail["reports"]] == \
        done["reprocessed_report_ids"]
    assert detail["reports"][0]["status"] == "preliminary"  # a draft, as always
    # and a reprocessed batch cannot be reprocessed again
    assert client.post(f"/lab/analyser/quarantine/{qid}/reprocess",
                       json={}, headers=CLINICIAN).status_code == 409


# ---------------------------------------------------------------------------
# Registry reconciliation: cross-check, divergence report, write-through
# ---------------------------------------------------------------------------

def test_registry_divergence_report_and_write_through(client):
    _instrument(client, "AN-DIV", status="active", bench="chemistry")
    r = client.post("/devices",
                    json={"device_key": "AN-DIV", "name": "Analyser AN-DIV",
                          "device_type": "analyser", "status": "maintenance"},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    # cross-check: the registries disagree, and the effective status
    # (most restrictive) is what ingest will use — named, not silent
    div = client.get("/lab/analyser/registry-divergences",
                     headers=CLINICIAN).json()
    mine = [d for d in div["items"] if d["instrument_id"] == "AN-DIV"]
    assert len(mine) == 1
    assert mine[0]["lab_status"] == "active"
    assert mine[0]["device_status"] == "maintenance"
    assert mine[0]["effective_status"] == "in-maintenance"
    listing = client.get("/lab/analyser/instruments", headers=VIEWER).json()
    row = next(i for i in listing["items"] if i["instrument_id"] == "AN-DIV")
    assert row["divergent"] is True and row["ingest_blocked"] is True
    # the reconciled write surface writes through to BOTH registries
    r = client.post("/lab/analyser/instruments/AN-DIV/status",
                    json={"status": "in-service"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    lab_rows = client.get("/lab/instruments", headers=CLINICIAN).json()
    assert next(i for i in lab_rows
                if i["instrument_id"] == "AN-DIV")["status"] == "active"
    device = client.get("/devices/AN-DIV", headers=ADMIN).json()
    assert device["status"] == "active"
    div = client.get("/lab/analyser/registry-divergences",
                     headers=CLINICIAN).json()
    assert all(d["instrument_id"] != "AN-DIV" for d in div["items"])
    # unknown instrument: the status surface refuses by name
    assert client.post("/lab/analyser/instruments/GHOST/status",
                       json={"status": "in-service"},
                       headers=CLINICIAN).status_code == 404


def test_device_only_instrument_status_gates_ingest(client):
    """Reconciliation consequence: an instrument known ONLY to the
    device registry still gates lab ingest by its device status."""
    _patient(client)
    r = client.post("/devices",
                    json={"device_key": "DEV-ONLY", "name": "Device-only",
                          "device_type": "analyser",
                          "status": "out-of-service"},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    order = _order(client, code=BMP, oid="ord-devonly")
    _to_received(client, order["id"])
    r = client.post("/lab/ingest",
                    json={"format": "csv",
                          "content": ("order_id,code,value\n"
                                      "ord-devonly,2345-7,88\n"),
                          "instrument_id": "DEV-ONLY"},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert _quarantine(client)["items"][0]["device_status"] == "out-of-service"


# ---------------------------------------------------------------------------
# D6(b): worklist + flag ingestion
# ---------------------------------------------------------------------------

def test_worklist_is_per_instrument_bench(client):
    _patient(client)
    _instrument(client, "AN-WL", status="active", bench="chemistry")
    bmp = _order(client, code=BMP, oid="ord-wl-bmp")
    _to_received(client, bmp["id"])
    cbc = _order(client, code=CBC, oid="ord-wl-cbc")  # haematology bench
    _to_received(client, cbc["id"])
    _order(client, code=BMP, oid="ord-wl-open")  # not yet received
    r = client.get("/lab/analyser/worklist/AN-WL", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bench"] == "chemistry"
    assert [i["order_id"] for i in body["items"]] == ["ord-wl-bmp"]
    assert body["items"][0]["code"] == BMP
    # unknown instrument 404s; a bench-less device gets an honest
    # empty worklist with the reason, not invented routing
    assert client.get("/lab/analyser/worklist/GHOST",
                      headers=CLINICIAN).status_code == 404
    r = client.get("/lab/analyser/worklist/fridge-pharmacy-1",
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["bench"] is None and r.json()["items"] == []
    assert "no lab-registry bench" in r.json()["note"]


def test_analyser_flags_land_on_results_and_feed_rerun_queue(client):
    _patient(client)
    _instrument(client, "AN-F", status="active", bench="haematology")
    order = _order(client, code=CBC, oid="ord-flag")
    _to_received(client, order["id"])
    # OBX-8 carries DIL on the haemoglobin line
    r = client.post("/lab/ingest",
                    json={"format": "hl7",
                          "content": _hl7("ord-flag", flag1="|DIL"),
                          "instrument_id": "AN-F"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    report_id = r.json()["ingested"][0]["report_id"]
    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    obs_id = detail["reports"][0]["result"][0]["reference"].split("/")[1]
    obs = client.get(f"/fhir/Observation/{obs_id}", headers=CLINICIAN).json()
    flags = [e["valueString"] for e in obs.get("extension") or []
             if e["url"] == "urn:opencare:extension/analyser-flag"]
    assert flags == ["DIL"]
    # DIL is a governed rerun flag: the result is on the rerun queue
    queue = client.get("/lab/analyser/rerun-queue", headers=CLINICIAN).json()
    assert queue["total"] == 1
    item = queue["items"][0]
    assert item["analyte_code"] == "718-7" and item["flags"] == ["DIL"]
    assert item["report_id"] == report_id and item["status"] == "pending"
    # CSV flags land the same way (HEM is not a rerun flag: no item)
    order2 = _order(client, code=BMP, oid="ord-flag2")
    _to_received(client, order2["id"])
    content = ("order_id,code,value,flags\n"
               "ord-flag2,2345-7,88,HEM\n"
               "ord-flag2,2823-3,4.4,\n")
    r = client.post("/lab/ingest", json={"format": "csv", "content": content},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    detail = client.get(f"/lab/orders/{order2['id']}", headers=CLINICIAN).json()
    glucose_ref = detail["reports"][0]["result"][0]["reference"]
    obs = client.get(f"/fhir/Observation/{glucose_ref.split('/')[1]}",
                     headers=CLINICIAN).json()
    flags = [e["valueString"] for e in obs.get("extension") or []
             if e["url"] == "urn:opencare:extension/analyser-flag"]
    assert flags == ["HEM"]
    assert client.get("/lab/analyser/rerun-queue",
                      headers=CLINICIAN).json()["total"] == 1  # unchanged


# ---------------------------------------------------------------------------
# D6(c): rerun/dilution loop re-enters the same validation core
# ---------------------------------------------------------------------------

def _flagged_rerun_item(client):
    _patient(client)
    _instrument(client, "AN-R", status="active", bench="haematology")
    order = _order(client, code=CBC, oid="ord-rerun")
    _to_received(client, order["id"])
    r = client.post("/lab/ingest",
                    json={"format": "hl7",
                          "content": _hl7("ord-rerun", flag1="|DIL"),
                          "instrument_id": "AN-R"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    item = client.get("/lab/analyser/rerun-queue",
                      headers=CLINICIAN).json()["items"][0]
    return order, item


def _qc(client, analyte, measured, low, high, material="SYN-QC"):
    r = client.post("/lab/qc",
                    json={"analyte_code": analyte, "control_material": material,
                          "expected_low": low, "expected_high": high,
                          "measured_value": measured},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def test_rerun_on_failed_qc_still_blocks(client):
    order, item = _flagged_rerun_item(client)
    _qc(client, "718-7", measured=99.0, low=12.0, high=14.0)  # failed latest
    r = client.post(f"/lab/analyser/rerun/{item['item_id']}/submit",
                    json={"value": 13.1}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "QC" in r.json()["detail"]
    # nothing moved: item pending, order still resulted with one report
    queue = client.get("/lab/analyser/rerun-queue", headers=CLINICIAN).json()
    assert queue["items"][0]["status"] == "pending"
    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "resulted"
    assert len(detail["reports"]) == 1
    # a passing QC run unblocks; the dilution rerun then lands through
    # the same core (measured x factor = corrected, both recorded)
    _qc(client, "718-7", measured=13.0, low=12.0, high=14.0,
        material="SYN-QC-PASS")
    r = client.post(f"/lab/analyser/rerun/{item['item_id']}/submit",
                    json={"value": 6.55, "dilution_factor": 2},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    done = r.json()["item"]
    assert done["status"] == "completed"
    assert done["measured_value"] == 6.55 and done["dilution_factor"] == 2
    assert done["corrected_value"] == pytest.approx(13.1)
    assert done["rerun_report_id"] == r.json()["outcome"]["report_id"]
    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    assert len(detail["reports"]) == 2  # original flagged + rerun report
    rerun_report = next(rep for rep in detail["reports"]
                        if rep["id"] == done["rerun_report_id"])
    assert rerun_report["status"] == "preliminary"  # a draft, like any entry


def test_rerun_from_down_instrument_quarantines(client):
    order, item = _flagged_rerun_item(client)
    r = client.post("/lab/instruments/AN-R/status",
                    json={"status": "down"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/lab/analyser/rerun/{item['item_id']}/submit",
                    json={"value": 13.1}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    q = _quarantine(client)
    assert q["total"] == 1
    assert q["items"][0]["source_format"] == "rerun"
    assert q["items"][0]["instrument_id"] == "AN-R"
    queue = client.get("/lab/analyser/rerun-queue", headers=CLINICIAN).json()
    assert queue["items"][0]["status"] == "pending"
    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "resulted"  # never reopened


def test_rerun_cancel_requires_note_and_is_kept(client):
    _order_row, item = _flagged_rerun_item(client)
    assert client.post(f"/lab/analyser/rerun/{item['item_id']}/cancel",
                       json={}, headers=CLINICIAN).status_code == 422
    r = client.post(f"/lab/analyser/rerun/{item['item_id']}/cancel",
                    json={"note": "Specimen exhausted; recollect ordered"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"
    assert client.post(f"/lab/analyser/rerun/{item['item_id']}/submit",
                       json={"value": 1.0},
                       headers=CLINICIAN).status_code == 409
