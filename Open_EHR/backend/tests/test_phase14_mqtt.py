"""Phase 14 — MQTT adapter (Track H).

Two levels of proof, both honest:
1. Recorded payloads through the adapter's transport-free message
   handler (`/devices/mqtt/test-message`): recorded / unknown device /
   malformed JSON / foreign topics, with the status endpoint reporting
   the adapter honestly OFF when no broker is configured.
2. A REAL localhost broker round-trip: amqtt (open-source, installed
   in the backend venv for the test-suite) serves a broker on an
   ephemeral port; the adapter connects with paho-mqtt, subscribes,
   and a published fridge reading lands as a curated DeviceReading
   with the excursion rule firing. No external broker is involved;
   production brokers remain NOT RUN.

Synthetic data only.
"""
import asyncio
import json
import socket
import threading
import time

import pytest

from .conftest import ADMIN, CLINICIAN


def test_mqtt_status_honest_off_without_broker_config(client):
    r = client.get("/devices/mqtt/status", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["state"] == "off"


def _test_message(client, topic, payload):
    r = client.post("/devices/mqtt/test-message",
                    json={"topic": topic, "payload": payload}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()["outcome"]


def test_mqtt_handler_records_reading_and_excursion(client):
    outcome = _test_message(
        client, "opencare/devices/fridge-lab-1/readings",
        {"metric": "temperature", "value": 11.5, "unit": "C"})
    assert outcome["status"] == "recorded"
    assert outcome["excursion"] is True  # seeded fridge range 2.0–8.0
    readings = client.get("/devices/readings", headers=ADMIN,
                          params={"device_key": "fridge-lab-1"}).json()
    assert readings["total"] == 1
    assert readings["items"][0]["source"] == "mqtt"


def test_mqtt_handler_unknown_device_dead_letters(client):
    outcome = _test_message(
        client, "opencare/devices/ghost-99/readings",
        {"metric": "temperature", "value": 4.0, "unit": "C"})
    assert outcome["status"] == "dead_letter"
    lake = client.get("/devices/lake", headers=ADMIN,
                      params={"payload_type": "device-reading"}).json()
    assert lake["by_disposition"].get("dead-letter") == 1


def test_mqtt_handler_foreign_topic_and_bad_payload(client):
    outcome = _test_message(client, "other/topic", {"metric": "temperature"})
    assert outcome["status"] == "ignored"
    outcome = _test_message(
        client, "opencare/devices/fridge-lab-1/readings",
        {"metric": "temperature"})  # no value
    assert outcome["status"] == "dead_letter"


# ---------------------------------------------------------------------------
# Live localhost broker round-trip
# ---------------------------------------------------------------------------

def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_mqtt_live_localhost_broker_round_trip(client):
    amqtt = pytest.importorskip("amqtt")
    paho = pytest.importorskip("paho.mqtt.client")
    from amqtt.broker import Broker

    from app.devices import MqttAdapter

    factory = client.app.state.session_factory
    port = _free_port()
    ready = threading.Event()
    holder: dict = {}

    async def _serve():
        broker = Broker({"listeners": {
            "default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}}})
        await broker.start()
        ready.set()
        while not holder.get("stop"):
            await asyncio.sleep(0.05)
        await broker.shutdown()

    thread = threading.Thread(
        target=lambda: asyncio.run(_serve()), daemon=True)
    thread.start()
    assert ready.wait(10), "localhost broker did not start"

    adapter = MqttAdapter(factory, {
        "brokerUrl": f"mqtt://127.0.0.1:{port}",
        "topicPrefix": "opencare/devices/"})
    try:
        assert adapter.start() is True
        deadline = time.time() + 15
        while adapter.state != "connected" and time.time() < deadline:
            time.sleep(0.1)
        assert adapter.state == "connected"

        publisher = paho.Client(paho.CallbackAPIVersion.VERSION2)
        publisher.connect("127.0.0.1", port, 60)
        publisher.loop_start()
        try:
            publisher.publish(
                "opencare/devices/fridge-pharmacy-1/readings",
                json.dumps({"metric": "temperature", "value": 12.3,
                            "unit": "C"}))
            publisher.publish(
                "opencare/devices/fridge-pharmacy-1/readings",
                json.dumps({"metric": "temperature", "value": 5.0,
                            "unit": "C"}))
            deadline = time.time() + 15
            while adapter.counts["recorded"] < 2 and time.time() < deadline:
                time.sleep(0.1)
        finally:
            publisher.loop_stop()
            publisher.disconnect()
        assert adapter.counts["received"] == 2
        assert adapter.counts["recorded"] == 2
    finally:
        adapter.stop()
        holder["stop"] = True
        thread.join(timeout=10)

    readings = client.get("/devices/readings", headers=ADMIN,
                          params={"device_key": "fridge-pharmacy-1"}).json()
    by_value = {r["value"]: r for r in readings["items"]}
    assert by_value[12.3]["excursion"] is True
    assert by_value[12.3]["source"] == "mqtt"
    assert by_value[5.0]["excursion"] is False
    # The excursion crossed the rules engine into a real Task.
    bundle = client.get("/fhir/Task", headers=ADMIN).json()
    descriptions = [e["resource"].get("description", "")
                    for e in bundle["entry"]]
    assert any("temperature excursion" in d.lower() for d in descriptions)
