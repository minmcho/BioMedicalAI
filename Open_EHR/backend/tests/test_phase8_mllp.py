"""Phase 8 — MLLP transport over REAL localhost sockets.

- The real asyncio MLLP listener (app.mllp_server) receives a framed
  ORU^R01 -> ACK AA -> the message is in the interface log AND the
  results landed as lab DRAFTS through the existing ingest path.
- Malformed input and conflicting ADT get NAKs (AE/AR) with ERR.
- The dispatcher sends over real sockets to a test downstream:
  AA -> sent; failures retry and dead-letter at the configured max
  attempts; a disabled endpoint is never attempted; a dead letter can
  be replayed and then delivers, with attempt history preserved.

Localhost sockets are available in this environment (verified before
this suite was written); nothing here is mocked at the socket layer.
"""
import asyncio
import socket
import threading
import time

from app import hl7, mllp_server
from tests.conftest import ADMIN, CLINICIAN

ORU_MESSAGE = "\r".join([
    "MSH|^~\\&|ANALYZER-X|CENTRAL LAB|OPENCARE|LIS|20261017080000||ORU^R01|MSG-MLLP-1|P|2.5.1",
    "PID|1||pat-1||Synthetic^Wire||19680412|F",
    "OBR|1|{oid}|{oid}|58410-2^Complete blood count panel",
    "OBX|1|NM|718-7^Hemoglobin||13.2|g/dL|12.0-15.5",
    "OBX|2|NM|4544-3^Hematocrit||40.5|%|36-46",
])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic8", "given": ["Wire"]}],
        "gender": "female", "birthDate": "1968-04-12"}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _publish(client, kind, key, payload):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text


def _lab_order_received(client, oid="ord-mllp-1"):
    r = client.post("/lab/orders", json={
        "id": oid, "code": "58410-2",
        "subject": {"reference": "Patient/pat-1"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    for status in ("collected", "received"):
        r = client.post(f"/lab/orders/{oid}/transition",
                        json={"to_status": status}, headers=CLINICIAN)
        assert r.status_code == 200, r.text


def _start_listener(client):
    """Run the real MLLP listener on an ephemeral localhost port."""
    factory = client.app.state.session_factory
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder = {}

    def run():
        asyncio.set_event_loop(loop)

        async def _serve():
            server = await mllp_server.serve(factory, "127.0.0.1", 0)
            holder["port"] = server.sockets[0].getsockname()[1]
            ready.set()
            async with server:
                await server.serve_forever()

        loop.run_until_complete(_serve())

    threading.Thread(target=run, daemon=True).start()
    assert ready.wait(10), "MLLP listener did not start"
    return holder["port"]


def _mllp_roundtrip(port, raw_message):
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(hl7.wrap_mllp(raw_message))
        buffer = b""
        while hl7.MLLP_END not in buffer:
            chunk = sock.recv(65536)
            assert chunk, "listener closed without an ACK"
            buffer += chunk
    return hl7.unwrap_mllp(buffer)


def _inbound_log(client):
    r = client.get("/interop/messages", params={"direction": "inbound"},
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["items"]


# ---------------------------------------------------------------------------
# Inbound over the wire
# ---------------------------------------------------------------------------

def test_mllp_oru_roundtrip_lands_as_lab_draft(client):
    _patient(client)
    _lab_order_received(client)
    port = _start_listener(client)
    ack = _mllp_roundtrip(port, ORU_MESSAGE.format(oid="ord-mllp-1"))
    parsed = hl7.parse_ack(ack)
    assert parsed["ack_code"] == "AA", ack
    assert parsed["control_id"] == "MSG-MLLP-1"
    # The message is recorded in the interface log as accepted.
    log = _inbound_log(client)
    assert len(log) == 1 and log[0]["status"] == "accepted"
    assert log[0]["message_type"] == "ORU^R01"
    assert log[0]["patient_ref"] == "Patient/pat-1"
    # And the results are DRAFTS through the existing ingest path:
    # order resulted, report preliminary, nothing released.
    detail = client.get("/lab/orders/ord-mllp-1", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "resulted"
    assert detail["reports"][0]["status"] == "preliminary"
    queue = client.get("/lab/verification-queue", headers=CLINICIAN).json()
    assert queue["total"] == 1


def test_mllp_malformed_message_gets_nak(client):
    port = _start_listener(client)
    ack = _mllp_roundtrip(port, "THIS IS NOT HL7 AT ALL")
    assert hl7.parse_ack(ack)["ack_code"] == "AR"
    assert "ERR|" in ack
    log = _inbound_log(client)
    assert len(log) == 1 and log[0]["status"] == "rejected"
    assert log[0]["ack_code"] == "AR"


def test_mllp_oru_for_unknown_order_gets_ae(client):
    _patient(client)
    port = _start_listener(client)
    ack = _mllp_roundtrip(port, ORU_MESSAGE.format(oid="ghost-order"))
    assert hl7.parse_ack(ack)["ack_code"] == "AE"
    assert "unknown lab order" in ack
    log = _inbound_log(client)
    assert log[0]["status"] == "rejected" and log[0]["ack_code"] == "AE"


def _adt_message(event, pid="pat-1", control="ADT-IN-1"):
    return "\r".join([
        f"MSH|^~\\&|EXTERNAL-EHR|EXTERNAL|OPENCARE|OPENCARE HOSPITAL|20261017090000||ADT^{event}|{control}|P|2.5.1",
        f"PID|1||{pid}||Synthetic^Wire||19680412|F",
        "PV1|1|I|unit-medsurg^101^A",
    ])


def test_mllp_inbound_adt_recorded_never_mutates_census(client):
    _patient(client)
    port = _start_listener(client)
    # A01 for a patient with no admission: accepted + RECORDED, but no
    # admission appears — inbound ADT is an external event, not a
    # census write.
    ack = _mllp_roundtrip(port, _adt_message("A01"))
    assert hl7.parse_ack(ack)["ack_code"] == "AA", ack
    log = _inbound_log(client)
    assert log[0]["status"] == "accepted"
    assert "census NOT modified" in log[0]["detail"]
    census = client.get("/inpatient/census", headers=CLINICIAN).json()
    assert census["totals"]["occupied"] == 0
    # A03 with no active admission conflicts -> AR with ERR.
    ack = _mllp_roundtrip(port, _adt_message("A03", control="ADT-IN-2"))
    assert hl7.parse_ack(ack)["ack_code"] == "AR"
    assert "ERR|" in ack and "no active inpatient admission" in ack
    # Unknown patient -> AR.
    ack = _mllp_roundtrip(port, _adt_message("A01", pid="ghost",
                                             control="ADT-IN-3"))
    assert hl7.parse_ack(ack)["ack_code"] == "AR"
    assert "Unknown patient" in ack


def test_mllp_inbound_adt_a01_conflicts_with_real_admission(client):
    _patient(client)
    r = client.post("/inpatient/admissions",
                    json={"patient": "pat-1", "bed": "MS-101A"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    port = _start_listener(client)
    ack = _mllp_roundtrip(port, _adt_message("A01"))
    assert hl7.parse_ack(ack)["ack_code"] == "AR"
    assert "already has an active inpatient admission" in ack


# ---------------------------------------------------------------------------
# Dispatcher over the wire
# ---------------------------------------------------------------------------

class _Downstream:
    """A fake downstream MLLP endpoint on a real socket: answers AA,
    AE, or drops the connection, per `mode`; records what it got."""

    def __init__(self, mode="AA"):
        self.mode = mode
        self.received = []
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn):
        with conn:
            conn.settimeout(10)
            buffer = b""
            try:
                while hl7.MLLP_END not in buffer:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buffer += chunk
            except OSError:
                return
            message, _ = hl7.extract_frame(buffer)
            self.received.append(message)
            if self.mode == "drop":
                return  # connection closes with no ACK at all
            ack = hl7.build_ack(message, self.mode,
                                "downstream received" if self.mode == "AA"
                                else "downstream refused")
            conn.sendall(hl7.wrap_mllp(ack))

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


def _queue_adt(client, downstream_port, max_attempts=3, enabled=True):
    _publish(client, "integration", "hl7-endpoints", {
        "facility": {"sendingApplication": "OPENCARE",
                     "sendingFacility": "OPENCARE HOSPITAL"},
        "retry": {"maxAttempts": max_attempts, "backoffSeconds": 0},
        "endpoints": [{"name": "downstream-ehr", "host": "127.0.0.1",
                       "port": downstream_port, "messageTypes": ["ADT"],
                       "enabled": enabled,
                       "receivingApplication": "DOWNSTREAM",
                       "receivingFacility": "EXT"}]})
    _patient(client)
    r = client.post("/inpatient/admissions",
                    json={"patient": "pat-1", "bed": "MS-101A"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _outbound_rows(client):
    r = client.get("/interop/messages", params={"direction": "outbound"},
                   headers=ADMIN)
    return r.json()["items"]


def _dispatch(client):
    r = client.post("/interop/dispatch", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["stats"]


def test_dispatcher_delivers_and_marks_sent(client):
    downstream = _Downstream("AA")
    try:
        _queue_adt(client, downstream.port)
        stats = _dispatch(client)
        assert stats["attempted"] == 1 and stats["sent"] == 1
        assert len(downstream.received) == 1
        assert "ADT^A01" in downstream.received[0]
        row = _outbound_rows(client)[0]
        assert row["status"] == "sent" and row["ack_code"] == "AA"
        assert row["attempts"] == 1
        # A second pass has nothing to do.
        assert _dispatch(client)["attempted"] == 0
    finally:
        downstream.close()


def test_dispatcher_retries_then_dead_letters_and_replay_delivers(client):
    downstream = _Downstream("AE")   # refuses everything for now
    try:
        _queue_adt(client, downstream.port, max_attempts=2)
        stats = _dispatch(client)
        assert stats["attempted"] == 1 and stats["failed"] == 1
        row = _outbound_rows(client)[0]
        assert row["status"] == "pending" and row["attempts"] == 1
        stats = _dispatch(client)
        assert stats["dead_lettered"] == 1
        row = _outbound_rows(client)[0]
        assert row["status"] == "dead_letter" and row["attempts"] == 2
        dead = client.get("/interop/dead-letters", headers=ADMIN).json()
        assert dead["count"] == 1
        assert len(dead["items"][0]["attempt_history"]) == 2
        assert all(a["outcome"] == "nak"
                   for a in dead["items"][0]["attempt_history"])
        # Further passes leave the dead letter alone.
        assert _dispatch(client)["attempted"] == 0
        # The downstream recovers; replay re-queues with history intact.
        downstream.mode = "AA"
        r = client.post(f"/interop/dead-letters/{row['id']}/replay",
                        headers=ADMIN)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending"
        assert r.json()["replay_count"] == 1
        assert len(r.json()["attempt_history"]) == 2   # preserved verbatim
        stats = _dispatch(client)
        assert stats["sent"] == 1
        row = _outbound_rows(client)[0]
        assert row["status"] == "sent" and row["attempts"] == 3
    finally:
        downstream.close()


def test_dispatcher_never_attempts_disabled_endpoint(client):
    downstream = _Downstream("AA")
    try:
        _queue_adt(client, downstream.port, enabled=False)
        # Nothing was even queued (the endpoint was disabled at the
        # event), and a dispatch pass attempts nothing.
        assert _outbound_rows(client) == []
        assert _dispatch(client)["attempted"] == 0
        assert downstream.received == []
        # Enable the endpoint AFTER the event: the pre-existing event
        # does not retro-queue (queueing is part of the event's
        # transaction), but the NEXT event flows.
        _publish(client, "integration", "hl7-endpoints", {
            "facility": {"sendingApplication": "OPENCARE",
                         "sendingFacility": "OPENCARE HOSPITAL"},
            "retry": {"maxAttempts": 3, "backoffSeconds": 0},
            "endpoints": [{"name": "downstream-ehr", "host": "127.0.0.1",
                           "port": downstream.port, "messageTypes": ["ADT"],
                           "enabled": True,
                           "receivingApplication": "DOWNSTREAM",
                           "receivingFacility": "EXT"}]})
        admission = client.get("/inpatient/patients/pat-1/admission",
                               headers=CLINICIAN).json()["admission"]
        r = client.post("/inpatient/encounters/" +
                        admission["encounter"]["id"]
                        + "/transfer", json={"bed": "MS-102A"},
                        headers=CLINICIAN)
        assert r.status_code == 200, r.text
        stats = _dispatch(client)
        assert stats["sent"] == 1 and len(downstream.received) == 1
        assert "ADT^A02" in downstream.received[0]
    finally:
        downstream.close()


def test_dispatcher_dead_endpoint_counts_as_failed_attempt(client):
    # Nothing listens on this port: connection refused is a failed
    # attempt, not a crash and not a silent drop.
    _queue_adt(client, 1, max_attempts=1)   # port 1: refused
    stats = _dispatch(client)
    assert stats["attempted"] == 1 and stats["failed"] == 1
    assert stats["dead_lettered"] == 1
    row = _outbound_rows(client)[0]
    assert row["status"] == "dead_letter"
    assert "refused" in (row["last_error"] or "").lower() \
        or "error" in (row["last_error"] or "").lower()
