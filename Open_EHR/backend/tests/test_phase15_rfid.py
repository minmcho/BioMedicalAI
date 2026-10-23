"""Phase 15 (H-2): RFID tag registry + simulated reader event feed.

NO physical hardware exists in this deployment: the reader feed is
simulated by direct POSTs to /rfid/events (the Phase 14 amqtt
precedent) and every response is labelled simulated. Unknown /
inactive readers and unregistered EPCs are dead-lettered with their
disposition recorded; a device-bound asset tag seen outside the
device registry's assigned zone fires the published
rfid-zone-mismatch rule into a Task — and creates none when the
zone agrees.
"""
from .conftest import ADMIN, CLINICIAN, VIEWER

EPC = "E2801170000002091ABC0001"


def _device(client, key="pump-rfid-1", location="icu"):
    r = client.post("/devices", json={
        "device_key": key, "name": "Infusion pump (RFID test)",
        "device_type": "pump", "location": location,
        "serial": "SN-RFID-1"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return key


def _tag(client, epc=EPC, tag_type="asset", entity=None, expect=201):
    payload = {"epc": epc, "tag_type": tag_type}
    if entity:
        payload["entity_type"], payload["entity_id"] = entity
    r = client.post("/rfid/tags", json=payload, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _event(client, reader_id, epc=EPC, **kw):
    payload = {"reader_id": reader_id, "epc": epc}
    payload.update(kw)
    return client.post("/rfid/events", json=payload, headers=CLINICIAN)


def _tasks(client):
    r = client.get("/fhir/Task", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json().get("entry") or []


def test_config_and_tag_lifecycle(client):
    r = client.get("/rfid/config", headers=VIEWER)
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert {t["code"] for t in cfg["tag_types"]} == {
        "asset", "patient-wristband", "medication", "inventory"}
    readers = {x["readerId"]: x for x in cfg["readers"]}
    assert readers["reader-icu-1"]["zone"] == "icu"
    assert readers["reader-storage-1"]["active"] is False
    assert cfg["feed_label"] == "simulated"

    # Unbound tag, then bind / unbind / retire.
    out = _tag(client).json()
    assert out["status"] == "unbound"
    _tag(client, expect=409)  # EPC unique
    _tag(client, epc="EPC-BADTYPE", tag_type="hat", expect=422)

    r = client.post(f"/rfid/tags/{EPC}/bind", json={
        "entity_type": "Device", "entity_id": "pump-rfid-1"},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "bound"
    assert r.json()["entity_ref"] == "Device/pump-rfid-1"

    r = client.post(f"/rfid/tags/{EPC}/unbind", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "unbound"
    assert r.json()["entity_ref"] is None

    r = client.post(f"/rfid/tags/{EPC}/retire", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "retired"
    r = client.post(f"/rfid/tags/{EPC}/bind", json={
        "entity_type": "Device", "entity_id": "pump-rfid-1"},
        headers=CLINICIAN)
    assert r.status_code == 409, r.text

    # Viewers read but never manage.
    r = client.post("/rfid/tags", json={"epc": "EPC-V", "tag_type": "asset"},
                    headers=VIEWER)
    assert r.status_code == 403, r.text
    r = client.get("/rfid/tags", headers=VIEWER)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1


def test_event_in_agreeing_zone_records_sighting_no_task(client):
    _device(client)
    _tag(client, entity=("Device", "pump-rfid-1"))
    before = len(_tasks(client))
    r = _event(client, "reader-icu-1",
               timestamp="2026-10-18T12:00:00Z", signal=-52)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "recorded"
    assert body["source_label"] == "simulated"
    assert body["mismatch"] is False
    assert body["expected_zone"] == "icu"
    assert body["sighting"]["zone"] == "icu"
    assert body["rule_firings"] == []
    assert len(_tasks(client)) == before

    tag = client.get(f"/rfid/tags/{EPC}", headers=CLINICIAN).json()
    assert tag["last_seen_zone"] == "icu"
    assert tag["sighting_count"] == 1

    trail = client.get("/rfid/entities/Device/pump-rfid-1/sightings",
                       headers=VIEWER).json()
    assert trail["total"] == 1
    assert trail["sightings"][0]["reader_id"] == "reader-icu-1"
    assert trail["tags"][0]["epc"] == EPC


def test_event_in_wrong_zone_fires_mismatch_rule_task(client):
    _device(client)
    _tag(client, entity=("Device", "pump-rfid-1"))
    before = len(_tasks(client))
    r = _event(client, "reader-pharmacy-1")  # device assigned to icu
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mismatch"] is True
    assert body["expected_zone"] == "icu"
    assert body["sighting"]["zone"] == "pharmacy"
    fired = [f for f in body["rule_firings"]
             if f["rule"] == "rfid-zone-mismatch"]
    assert len(fired) == 1
    assert fired[0]["detail"]["title"].startswith("RFID zone mismatch")
    assert len(_tasks(client)) == before + 1


def test_dead_letter_paths_are_recorded_never_dropped(client):
    _tag(client, entity=("Device", "pump-rfid-1"))
    # Unknown reader.
    r = _event(client, "reader-ghost-9")
    assert r.status_code == 202, r.text
    assert r.json()["disposition"] == "unknown-reader"
    # Inactive reader (seeded registry).
    r = _event(client, "reader-storage-1")
    assert r.status_code == 202, r.text
    assert r.json()["disposition"] == "inactive-reader"
    # Valid reader, unregistered EPC.
    r = _event(client, "reader-icu-1", epc="EPC-UNREGISTERED")
    assert r.status_code == 202, r.text
    assert r.json()["disposition"] == "unknown-tag"

    letters = client.get("/rfid/dead-letters", headers=CLINICIAN).json()
    assert letters["total"] == 3
    assert {i["disposition"] for i in letters["items"]} == {
        "unknown-reader", "inactive-reader", "unknown-tag"}
    # No sighting was fabricated from any dead-lettered event.
    tag = client.get(f"/rfid/tags/{EPC}", headers=CLINICIAN).json()
    assert tag["sighting_count"] == 0
    assert tag["last_seen_zone"] is None


def test_patient_wristband_tag_sightings_trail(client):
    epc = "E2801170000002091ABC0002"
    _tag(client, epc=epc, tag_type="patient-wristband",
         entity=("Patient", "pat-rfid-1"))
    r = _event(client, "reader-icu-1", epc=epc)
    assert r.status_code == 200, r.text
    # Not a device-bound asset tag: no zone-mismatch evaluation.
    assert r.json()["mismatch"] is None
    trail = client.get("/rfid/entities/Patient/pat-rfid-1/sightings",
                       headers=CLINICIAN).json()
    assert trail["total"] == 1
    assert trail["sightings"][0]["zone"] == "icu"
    assert trail["sightings"][0]["source_label"] == "simulated"
