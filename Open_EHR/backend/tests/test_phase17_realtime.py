"""Phase 17 (BD-69) — realtime fan-out for command-centre updates.

An ADT event (the inpatient module's admit, which writes an AdtEvent
row in-transaction) produces a ``board.changed`` fan-out event that:

- a polling client receives via GET /realtime/events (the retained
  fallback path), and
- an SSE client receives via GET /realtime/stream (replay + live).

Auth mirrors the command centre's own gate (continuum.read):
anonymous 401, patient role 403. The published integration/realtime
config can disable the lane (503 on both paths).
"""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import ADMIN, CLINICIAN, PATIENT


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/rt.db")
    with TestClient(app) as c:
        yield c


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Fanout", "given": ["Test"]}]}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _admit(client, patient="rt-pat-1", bed="MS-101A"):
    _patient(client, patient)
    r = client.post("/inpatient/admissions",
                    json={"patient": patient, "bed": bed},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def test_adt_admit_produces_fanout_event_on_polling_path(client):
    _admit(client)
    r = client.get("/realtime/events", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["channel"] == "command"
    adt_events = [e for e in body["events"]
                  if e["type"] == "board.changed"
                  and e["payload"].get("source") == "adt"
                  and e["payload"].get("patient_ref")
                  == "Patient/rt-pat-1"]
    assert adt_events, body
    event = adt_events[-1]
    assert event["payload"]["event_type"] == "A01"
    assert event["payload"]["patient_ref"] == "Patient/rt-pat-1"
    assert event["payload"]["to_location"] == "MS-101A"
    assert body["latest_id"] >= event["id"]
    # since= paging: asking after the event yields nothing new.
    r = client.get("/realtime/events",
                   params={"since": event["id"]}, headers=CLINICIAN)
    assert r.json()["events"] == []


def test_adt_event_reaches_sse_subscriber(tmp_path):
    # Transport note (hard-won): this test consumes the stream over
    # REAL HTTP from a uvicorn subprocess — the same pattern the
    # MQTT/MLLP suites use for real sockets. The TestClient's
    # streaming transport deadlocks on an infinite SSE body in this
    # Starlette version (the request blocks with ~0 CPU while the
    # identical endpoint streams correctly under uvicorn, verified
    # with curl), so TestClient is used only for the non-streaming
    # assertions in this file.
    import os
    import socket
    import subprocess
    import sys
    import time

    import httpx

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    backend_dir = os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{tmp_path}/sse.db"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=backend_dir, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 240
        while True:
            try:
                if httpx.get(base + "/health", timeout=2).status_code \
                        == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.time() < deadline, "uvicorn did not start"
            time.sleep(2)
        with httpx.Client(base_url=base, timeout=30) as http:
            r = http.post("/fhir/Patient", json={
                "resourceType": "Patient", "id": "rt-pat-1",
                "name": [{"family": "Fanout", "given": ["Test"]}]},
                headers=ADMIN)
            assert r.status_code == 201, r.text
            r = http.post("/inpatient/admissions", json={
                "patient": "rt-pat-1", "bed": "MS-101A"},
                headers=CLINICIAN)
            assert r.status_code == 201, r.text
            received: list[str] = []
            with http.stream("GET", "/realtime/stream",
                             params={"since": 0},
                             headers=CLINICIAN) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in \
                    resp.headers.get("content-type", "")
                for line in resp.iter_lines():
                    received.append(line)
                    if line.startswith("data:") and '"A01"' in line \
                            and "rt-pat-1" in line:
                        break
                    assert len(received) < 50, received
        text = "\n".join(received)
        assert "board.changed" in text
        assert "A01" in text
        assert "rt-pat-1" in text
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_discharge_also_fans_out(client):
    admitted = _admit(client)
    enc_id = admitted["encounter"]["id"]
    before = client.get("/realtime/events",
                        headers=CLINICIAN).json()["latest_id"]
    r = client.post(f"/inpatient/encounters/{enc_id}/discharge",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    events = client.get("/realtime/events", params={"since": before},
                        headers=CLINICIAN).json()["events"]
    assert any(e["payload"].get("event_type") == "A03" for e in events), \
        events


def test_auth_gates(client):
    assert client.get("/realtime/events").status_code == 401
    assert client.get("/realtime/events",
                      headers=PATIENT).status_code == 403
    assert client.get("/realtime/stream").status_code == 401
    assert client.get("/realtime/events",
                      params={"channel": "nope"},
                      headers=ADMIN).status_code == 404


def test_disabled_config_503s_both_paths(client):
    r = client.post("/studio/config/integration/realtime",
                    json={"enabled": False, "channels": ["command"]},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/integration/realtime/versions/{version}"
        "/publish", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert client.get("/realtime/events",
                      headers=ADMIN).status_code == 503
