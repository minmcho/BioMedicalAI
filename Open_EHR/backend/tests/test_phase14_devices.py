"""Phase 14 — device registry, calibration gating, ingestion, lake.

Devices are real data; device types/calibration intervals/result
thresholds/the MLLP gate policy are published Studio config
(`terminology/devices`). Results from a calibration-overdue device are
blocked or quarantined per that published policy — both modes are
exercised here through the real Phase 8 MLLP path (`/devices/simulate/
oru` runs `interop.process_inbound`) and through the FHIR Observation
ingestion endpoint. The raw-zone lake is append-only (no update/delete
routes exist) and every curated record links back to its raw entry.
All feeds are simulated and labelled `source_label: simulated`.

Synthetic data only.
"""
from .conftest import ADMIN, CLINICIAN, PATIENT, VIEWER

ORU = "\r".join([
    "MSH|^~\\&|{sender}|CENTRAL LAB|OPENCARE|LIS|20261017080000||ORU^R01|MSG1|P|2.5.1",
    "PID|1||pat-1||Synthetic^Wire||19680412|F",
    "OBR|1|{oid}|{oid}|58410-2^Complete blood count panel",
    "OBX|1|NM|718-7^Hemoglobin||13.2|g/dL|12.0-15.5",
])


def _publish(client, kind, key, payload):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    return version


def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Wire", "given": ["Synthetic"]}],
        "gender": "female", "birthDate": "1968-04-12"}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _lab_order(client, oid, pid="pat-1"):
    r = client.post("/lab/orders", json={
        "id": oid, "code": "58410-2", "subject": {"reference": f"Patient/{pid}"}},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    for status in ("collected", "received"):
        r = client.post(f"/lab/orders/{oid}/transition",
                        json={"to_status": status}, headers=CLINICIAN)
        assert r.status_code == 200, r.text


def _tasks(client):
    r = client.get("/fhir/Task", headers=ADMIN)
    assert r.status_code == 200, r.text
    return [e["resource"] for e in r.json()["entry"]]


def _gate_flip(client, **changes):
    eff = client.get("/studio/config/terminology/effective/devices",
                     headers=ADMIN).json()["payload"]
    eff["mllpGate"].update(changes)
    _publish(client, "terminology", "devices", eff)


# ---------------------------------------------------------------------------
# Registry + calibration
# ---------------------------------------------------------------------------

def test_seeded_devices_and_config_visible(client):
    r = client.get("/devices", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 6
    keys = {d["device_key"] for d in body["items"]}
    assert {"chem-analyser-1", "fridge-pharmacy-1", "vitals-monitor-1"} <= keys
    chem = next(d for d in body["items"] if d["device_key"] == "chem-analyser-1")
    assert chem["hl7_sender"] == "ANALYZER-X"
    assert chem["calibration"]["status"] in ("ok", "due-soon")
    cfg = client.get("/devices/config", headers=ADMIN).json()
    types = {t["type"] for t in cfg["devices"]["deviceTypes"]}
    # Depth pass: the type set gains "poct-analyser" — the POCT
    # programme's device type, registered here like every other type
    # (the registry itself is unchanged: still 6 seeded devices).
    assert types == {"analyser", "vitals-monitor", "fridge-sensor", "pump",
                     "poct-analyser"}
    assert cfg["mqtt_configured"] is False
    assert cfg["feed_label"] == "simulated"


def test_registry_validation_and_duplicate_conflicts(client):
    r = client.post("/devices", json={
        "device_key": "new-pump", "device_type": "toaster"}, headers=ADMIN)
    assert r.status_code == 422  # unknown type per published config
    r = client.post("/devices", json={
        "device_key": "new-pump", "device_type": "pump",
        "name": "Ward pump", "location": "ICU"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/devices", json={
        "device_key": "new-pump", "device_type": "pump", "name": "Duplicate"},
        headers=ADMIN)
    assert r.status_code == 409  # duplicate key
    r = client.post("/devices", json={
        "device_key": "other", "device_type": "analyser", "name": "Other",
        "hl7_sender": "ANALYZER-X"}, headers=ADMIN)
    assert r.status_code == 409  # sender already registered


def test_calibration_computes_due_from_published_interval(client):
    r = client.post("/devices/fridge-lab-1/calibration",
                    json={"performed_at": "2026-01-15T00:00:00Z"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    # fridge-sensor interval is 365 days in the published config.
    assert r.json()["calibration_due"].startswith("2027-01-15")
    assert r.json()["calibration"]["status"] == "ok"


def test_calibration_overdue_flag_and_worklist(client):
    client.post("/devices/chem-analyser-1/calibration",
                json={"performed_at": "2026-01-01T00:00:00Z"}, headers=CLINICIAN)
    detail = client.get("/devices/chem-analyser-1", headers=ADMIN).json()
    assert detail["calibration"]["status"] == "overdue"
    worklist = client.get("/devices/calibration-worklist", headers=ADMIN).json()
    assert [i["device_key"] for i in worklist["items"]] == ["chem-analyser-1"]
    # Recording a fresh calibration clears the flag (config-driven, no code).
    client.post("/devices/chem-analyser-1/calibration",
                json={"performed_at": "2026-10-01T00:00:00Z"}, headers=CLINICIAN)
    detail = client.get("/devices/chem-analyser-1", headers=ADMIN).json()
    assert detail["calibration"]["status"] == "ok"


def test_device_capability_gates(client):
    assert client.get("/devices", headers=PATIENT).status_code == 403
    assert client.get("/devices", headers=VIEWER).status_code == 200
    r = client.post("/devices", json={
        "device_key": "x", "device_type": "pump"}, headers=VIEWER)
    assert r.status_code == 403  # viewer lacks devices.manage


# ---------------------------------------------------------------------------
# ORU over the MLLP machinery — gate modes
# ---------------------------------------------------------------------------

def test_oru_known_sender_lands_as_draft_with_lineage(client):
    _patient(client)
    _lab_order(client, "ord-1")
    r = client.post("/devices/simulate/oru", json={
        "content": ORU.format(sender="ANALYZER-X", oid="ord-1")}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["ack_code"] == "AA"
    queue = client.get("/lab/verification-queue", headers=CLINICIAN).json()
    assert len(queue["items"]) == 1
    report_id = queue["items"][0]["report"]["id"]
    lineage = client.get(f"/devices/lineage/DiagnosticReport/{report_id}",
                         headers=ADMIN).json()
    assert lineage["total"] == 1
    raw = lineage["raw_entries"][0]
    assert raw["device_key"] == "chem-analyser-1"
    assert raw["source_label"] == "simulated"
    assert "ORU^R01" in raw["payload"]


def test_oru_unknown_sender_dead_letters_never_dropped(client):
    _patient(client)
    _lab_order(client, "ord-2")
    r = client.post("/devices/simulate/oru", json={
        "content": ORU.format(sender="ROGUE-9000", oid="ord-2")}, headers=CLINICIAN)
    assert r.json()["ack_code"] == "AR"
    assert "Unknown device" in r.json()["detail"]
    lake = client.get("/devices/lake", headers=ADMIN,
                      params={"payload_type": "hl7"}).json()
    assert lake["by_disposition"].get("dead-letter") == 1
    inbound = client.get("/interop/messages", headers=ADMIN,
                         params={"direction": "inbound"}).json()
    assert inbound["items"][0]["status"] == "dead-letter"


def test_oru_overdue_device_blocks_when_config_says_block(client):
    _patient(client)
    _lab_order(client, "ord-3")
    client.post("/devices/chem-analyser-1/calibration",
                json={"performed_at": "2026-01-01T00:00:00Z"}, headers=CLINICIAN)
    r = client.post("/devices/simulate/oru", json={
        "content": ORU.format(sender="ANALYZER-X", oid="ord-3")}, headers=CLINICIAN)
    assert r.json()["ack_code"] == "AR"
    assert "calibration is overdue" in r.json()["detail"]
    queue = client.get("/lab/verification-queue", headers=CLINICIAN).json()
    assert queue["items"] == []  # nothing ingested


def test_oru_gate_disabled_restores_phase8_behaviour(client):
    _patient(client)
    _lab_order(client, "ord-4")
    _gate_flip(client, enabled=False)
    client.post("/devices/chem-analyser-1/calibration",
                json={"performed_at": "2026-01-01T00:00:00Z"}, headers=CLINICIAN)
    r = client.post("/devices/simulate/oru", json={
        "content": ORU.format(sender="ANALYZER-X", oid="ord-4")}, headers=CLINICIAN)
    assert r.json()["ack_code"] == "AA"


def test_oru_quarantine_mode_holds_then_release_ingests(client):
    _patient(client)
    _lab_order(client, "ord-5")
    _gate_flip(client, onCalibrationOverdue="quarantine")
    client.post("/devices/chem-analyser-1/calibration",
                json={"performed_at": "2026-01-01T00:00:00Z"}, headers=CLINICIAN)
    r = client.post("/devices/simulate/oru", json={
        "content": ORU.format(sender="ANALYZER-X", oid="ord-5")}, headers=CLINICIAN)
    assert r.json()["ack_code"] == "AA"  # accepted into quarantine
    holds = client.get("/devices/quarantine", headers=ADMIN,
                       params={"status": "held", "source": "device-ingest"}).json()
    assert holds["total"] == 1
    hold_id = holds["items"][0]["id"]
    queue = client.get("/lab/verification-queue", headers=CLINICIAN).json()
    assert queue["items"] == []  # held, not ingested
    # Repair the cause, then release: the held message is ingested.
    client.post("/devices/chem-analyser-1/calibration",
                json={"performed_at": "2026-10-01T00:00:00Z"}, headers=CLINICIAN)
    r = client.post(f"/devices/quarantine/{hold_id}/release",
                    json={"note": "calibration restored"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["outcome"]["released_as"]
    queue = client.get("/lab/verification-queue", headers=CLINICIAN).json()
    assert len(queue["items"]) == 1


# ---------------------------------------------------------------------------
# FHIR Observation ingestion (vitals)
# ---------------------------------------------------------------------------

def _obs(oid="obs-1", code="8867-4", value=72, device="vitals-monitor-1",
         patient="pat-1"):
    return {"resourceType": "Observation", "id": oid, "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
            "subject": {"reference": f"Patient/{patient}"},
            "device": {"reference": f"Device/{device}"},
            "valueQuantity": {"value": value, "unit": "/min"}}


def test_observation_ingestion_records_raw_and_lineage(client):
    _patient(client)
    r = client.post("/devices/ingest/observation", json=_obs(), headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["breach"] is None and body["source_label"] == "simulated"
    lineage = client.get("/devices/lineage/Observation/obs-1", headers=ADMIN).json()
    assert lineage["total"] == 1
    assert lineage["raw_entries"][0]["device_key"] == "vitals-monitor-1"


def test_observation_ingestion_validation(client):
    _patient(client)
    bad = _obs()
    del bad["device"]
    r = client.post("/devices/ingest/observation", json=bad, headers=CLINICIAN)
    assert r.status_code == 422  # device reference required
    r = client.post("/devices/ingest/observation",
                    json=_obs(patient="ghost"), headers=CLINICIAN)
    assert r.status_code == 422  # patient must exist
    r = client.post("/devices/ingest/observation",
                    json=_obs(device="no-such-device"), headers=CLINICIAN)
    assert r.status_code == 422  # device must be registered


def test_observation_breach_fires_rules_event_and_task(client):
    _patient(client)
    before = len(_tasks(client))
    r = client.post("/devices/ingest/observation",
                    json=_obs(value=155), headers=CLINICIAN)  # HR > 140
    assert r.json()["breach"] == "high"
    tasks = _tasks(client)
    assert len(tasks) == before + 1
    assert "vitals" in tasks[-1]["description"].lower()


def test_observation_from_overdue_device_quarantines_202(client):
    _patient(client)
    _gate_flip(client, onCalibrationOverdue="quarantine")
    client.post("/devices/vitals-monitor-1/calibration",
                json={"performed_at": "2025-01-01T00:00:00Z"}, headers=CLINICIAN)
    r = client.post("/devices/ingest/observation", json=_obs(), headers=CLINICIAN)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "quarantined"
    assert client.get("/fhir/Observation/obs-1", headers=ADMIN).status_code == 404


# ---------------------------------------------------------------------------
# Fridge readings + excursion rule
# ---------------------------------------------------------------------------

def _reading(client, device, value):
    return client.post("/devices/ingest/reading", json={
        "device_key": device, "metric": "temperature", "value": value,
        "unit": "C"}, headers=CLINICIAN)


def test_fridge_excursion_fires_once_per_transition(client):
    assert _reading(client, "fridge-pharmacy-1", 5.1).json()["excursion"] is False
    before = len(_tasks(client))
    r = _reading(client, "fridge-pharmacy-1", 9.4)  # seeded range 2.0–8.0
    assert r.json()["excursion"] is True
    assert r.json()["excursion_event_fired"] is True
    r = _reading(client, "fridge-pharmacy-1", 10.2)
    assert r.json()["excursion"] is True
    assert r.json()["excursion_event_fired"] is False  # still out: no re-fire
    tasks = _tasks(client)
    assert len(tasks) == before + 1
    assert "temperature excursion" in tasks[-1]["description"].lower()
    # Back in range re-arms the transition.
    assert _reading(client, "fridge-pharmacy-1", 4.0).json()["excursion"] is False
    assert _reading(client, "fridge-pharmacy-1", 9.0).json()[
        "excursion_event_fired"] is True


def test_reading_from_unknown_device_rejected(client):
    r = _reading(client, "ghost-sensor", 4.0)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Lake immutability + offline store-and-forward
# ---------------------------------------------------------------------------

def test_lake_is_append_only_no_update_or_delete_routes(client):
    _reading(client, "fridge-pharmacy-1", 5.0)
    lake = client.get("/devices/lake", headers=ADMIN).json()
    entry_id = lake["items"][0]["id"]
    assert client.request("DELETE", f"/devices/lake/{entry_id}",
                          headers=ADMIN).status_code == 405
    assert client.request("PUT", f"/devices/lake/{entry_id}", json={},
                          headers=ADMIN).status_code == 405
    assert client.request("PATCH", f"/devices/lake/{entry_id}", json={},
                          headers=ADMIN).status_code == 405
    entry = client.get(f"/devices/lake/{entry_id}", headers=ADMIN).json()
    assert entry["content_hash"] and entry["received_at"]


def test_offline_batches_queue_as_held_entries(client):
    r = client.post("/devices/offline/batches", json={"items": [
        {"kind": "reading", "payload": {"device_key": "fridge-lab-1",
                                         "metric": "temperature",
                                         "value": 4.5, "unit": "C"}},
        {"kind": "hl7", "payload": "MSH|^~\\&|X"}]},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["queued"] == 2
    lake = client.get("/devices/lake", headers=ADMIN,
                      params={"disposition": "held-offline"}).json()
    assert lake["total_entries"] == 2
    # Nothing curated yet — store-and-forward holds raw payloads only.
    readings = client.get("/devices/readings", headers=ADMIN,
                          params={"device_key": "fridge-lab-1"}).json()
    assert readings["total"] == 0


def test_offline_mode_disabled_refuses_batches(client):
    r = client.post("/studio/config/integration/offline",
                    json={"enabled": False, "mode": "store-and-forward"},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    client.post(f"/studio/config/integration/offline/versions/{version}/publish",
                headers=ADMIN)
    r = client.post("/devices/offline/batches", json={"items": [
        {"kind": "reading", "payload": {"device_key": "fridge-lab-1",
                                         "metric": "temperature",
                                         "value": 4.5}}]}, headers=CLINICIAN)
    assert r.status_code == 409


def test_feeds_status_reports_gate_and_mqtt_honestly(client):
    r = client.get("/devices/feeds/status", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["mqtt"]["state"] == "off"  # no broker configured: honest off
    assert body["mllp_gate"]["enabled"] is True
    assert body["lake_entries"] >= 0
    assert body["feed_label"] == "simulated"
