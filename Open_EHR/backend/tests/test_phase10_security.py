"""Phase 10, Track K — security core: break-the-glass with mandatory
review, field-level envelope encryption of secure-message bodies, and
the downtime read-only mode. Synthetic data only.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import crypto
from app.models import BreakGlassAccess, FhirResource
from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER


def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic", "given": ["Sec"]}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


# ---------------------------------------------------------------------------
# Envelope encryption (unit level)
# ---------------------------------------------------------------------------

def test_crypto_roundtrip_and_envelope_shape():
    envelope = crypto.encrypt("highly sensitive synthetic text")
    assert envelope["v"] == 1 and envelope["kid"].startswith("k-")
    assert "highly sensitive" not in json.dumps(envelope)
    assert crypto.decrypt(envelope) == "highly sensitive synthetic text"
    # Distinct data key per value: two envelopes of the same text differ.
    assert crypto.encrypt("same")["ct"] != crypto.encrypt("same")["ct"]


def test_crypto_tamper_and_wrong_key_fail(tmp_path, monkeypatch):
    envelope = crypto.encrypt("integrity matters")
    tampered = dict(envelope)
    raw = bytearray(__import__("base64").b64decode(envelope["ct"]))
    raw[0] ^= 0x01
    import base64
    tampered["ct"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(tampered)
    # A different master key cannot open the envelope (kid mismatch).
    other = tmp_path / "other-key"
    other.write_text(base64.b64encode(b"\x01" * 32).decode())
    monkeypatch.setenv("OPENEHR_MASTER_KEY_FILE", str(other))
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(envelope)


# ---------------------------------------------------------------------------
# Message bodies are ciphertext at rest, plaintext to authorised reads
# ---------------------------------------------------------------------------

def test_message_body_encrypted_at_rest(client):
    _patient(client)
    secret = "Synthetic secret: patient feels dizzy since Tuesday"
    r = client.post("/messages", headers=CLINICIAN, json={
        "patient": "pat-1", "text": secret, "topic": "check-in"})
    assert r.status_code == 201, r.text
    message_id = r.json()["id"]
    # Authorised reads decrypt transparently.
    assert r.json()["text"] == secret
    # The STORED row holds an envelope — the plaintext is nowhere in it.
    factory = client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(FhirResource).where(
            FhirResource.resource_id == message_id)).first()
        stored = json.dumps(row.body)
        assert secret not in stored
        content = row.body["payload"][0]["contentString"]
        envelope = json.loads(content)
        assert crypto.is_envelope(envelope)
        assert envelope["kid"].startswith("k-")
        flags = [e for e in row.body["extension"]
                 if e["url"].endswith("encrypted-content")]
        assert flags and flags[0]["valueString"] == envelope["kid"]
    # Thread views + summaries decrypt too.
    thread_id = r.json()["threadId"]
    thread = client.get(f"/messages/threads/{thread_id}",
                        headers=CLINICIAN).json()
    assert thread["messages"][0]["text"] == secret
    inbox = client.get("/messages/inbox?patient=pat-1",
                       headers=CLINICIAN).json()
    assert inbox["items"][0]["last_text"] == secret
    # The patient reads their own message decrypted via the portal.
    portal = client.get("/portal/my-messages", headers=PATIENT).json()
    texts = [m["text"] for m in portal["messages"]] \
        if "messages" in portal else [i["last_text"] for i in portal["items"]]
    assert secret in texts


def test_legacy_plaintext_message_still_reads(client):
    """Messages written before Phase 10 (no encryption flag) stay
    readable: the flag, not the shape, decides decryption."""
    _patient(client)
    factory = client.app.state.session_factory
    from app.models import utcnow
    with factory() as session:
        row = FhirResource(
            resource_type="Communication", resource_id="legacy-1", version=1,
            patient_ref="Patient/pat-1", status="completed",
            body={"resourceType": "Communication", "id": "legacy-1",
                  "status": "completed",
                  "subject": {"reference": "Patient/pat-1"},
                  "sent": "2026-10-01T00:00:00+00:00",
                  "sender": {"display": "dev-clinician"},
                  "payload": [{"contentString": "old plaintext note"}],
                  "extension": [
                      {"url": "urn:opencare:extension/message-thread",
                       "valueString": "legacy-1"},
                      {"url": "urn:opencare:extension/sender-role",
                       "valueString": "clinician"},
                      {"url": "urn:opencare:extension/read-by",
                       "valueString": "dev-clinician"}]})
        session.add(row)
        session.commit()
    thread = client.get("/messages/threads/legacy-1", headers=CLINICIAN).json()
    assert thread["messages"][0]["text"] == "old plaintext note"


# ---------------------------------------------------------------------------
# Break-the-glass
# ---------------------------------------------------------------------------

def test_break_glass_open_requires_reason_and_known_patient(client):
    _patient(client)
    assert client.post("/privacy/break-glass", headers=CLINICIAN,
                       json={"patient_ref": "Patient/pat-1"}
                       ).status_code == 422
    assert client.post("/privacy/break-glass", headers=CLINICIAN,
                       json={"patient_ref": "Patient/ghost",
                             "reason": "emergency"}).status_code == 404
    r = client.post("/privacy/break-glass", headers=CLINICIAN, json={
        "patient_ref": "Patient/pat-1", "reason": "unresponsive patient, "
        "no proxy available"})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["active"] is True and out["reviewed"] is False
    opened = datetime.fromisoformat(out["opened_at"])
    expires = datetime.fromisoformat(out["expires_at"])
    assert expires - opened == timedelta(minutes=60)  # built-in default
    due = datetime.fromisoformat(out["justification_due_at"])
    assert due - opened == timedelta(hours=48)


def test_break_glass_role_split_open_vs_review(client):
    _patient(client)
    # The configurator (review role) cannot OPEN; the clinician
    # (opening role) cannot REVIEW. Separation is the control.
    assert client.post("/privacy/break-glass", headers=CONFIG, json={
        "patient_ref": "Patient/pat-1", "reason": "x"}).status_code == 403
    assert client.get("/privacy/review-queue",
                      headers=CLINICIAN).status_code == 403
    assert client.get("/privacy/review-queue",
                      headers=VIEWER).status_code == 403
    r = client.post("/privacy/break-glass", headers=CLINICIAN, json={
        "patient_ref": "Patient/pat-1", "reason": "emergency"})
    access_id = r.json()["access_id"]
    assert client.post(f"/privacy/review-queue/{access_id}/review",
                       headers=CLINICIAN,
                       json={"outcome": "justified"}).status_code == 403
    out = client.post(f"/privacy/review-queue/{access_id}/review",
                      headers=CONFIG,
                      json={"outcome": "justified", "note": "verified verbally"})
    assert out.status_code == 200, out.text
    assert out.json()["review_outcome"] == "justified"
    assert client.post(f"/privacy/review-queue/{access_id}/review",
                       headers=CONFIG,
                       json={"outcome": "justified"}).status_code == 409


def test_review_queue_is_complete_and_flags_overdue(client):
    _patient(client)
    ids = []
    for i in range(3):
        r = client.post("/privacy/break-glass", headers=CLINICIAN, json={
            "patient_ref": "Patient/pat-1", "reason": f"emergency {i}"})
        ids.append(r.json()["access_id"])
    queue = client.get("/privacy/review-queue", headers=CONFIG).json()
    assert queue["total"] == 3 and queue["overdue"] == 0
    assert "100%" in queue["policy"]["review_expectation"]
    # Age one item past its justification window (direct state edit —
    # the API deliberately offers no way to fake a due date).
    factory = client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(BreakGlassAccess).where(
            BreakGlassAccess.access_id == ids[0])).first()
        row.justification_due_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session.commit()
    queue = client.get("/privacy/review-queue", headers=CONFIG).json()
    overdue = [i for i in queue["items"] if i["overdue"]]
    assert [i["access_id"] for i in overdue] == [ids[0]]
    assert queue["overdue"] == 1


# ---------------------------------------------------------------------------
# Downtime read-only mode
# ---------------------------------------------------------------------------

def _set_downtime(client, read_only, message=""):
    r = client.post("/studio/config/integration/downtime",
                    json={"readOnly": read_only, "message": message},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post(
        f"/studio/config/integration/downtime/versions/{r.json()['version']}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text


def test_downtime_flag_seeded_off_and_blocks_writes_when_on(client):
    eff = client.get("/studio/config/integration/effective/downtime",
                     headers=ADMIN)
    assert eff.status_code == 200
    assert eff.json()["payload"]["readOnly"] is False
    _patient(client)  # writes work while off
    _set_downtime(client, True, "planned downtime drill")
    w = client.post("/fhir/Patient", headers=CLINICIAN, json={
        "resourceType": "Patient", "id": "pat-blocked",
        "name": [{"family": "Synthetic"}]})
    assert w.status_code == 503
    assert "planned downtime drill" in w.json()["detail"]
    assert "reads continue" in w.json()["detail"]
    # Reads keep working.
    assert client.get("/fhir/Patient/pat-1", headers=CLINICIAN).status_code == 200
    assert client.get("/health").status_code == 200
    # Studio stays writable so the operator can end the downtime
    # (draft creation is a /studio write; publishing is too).
    _set_downtime(client, False)
    w = client.post("/fhir/Patient", headers=CLINICIAN, json={
        "resourceType": "Patient", "id": "pat-blocked",
        "name": [{"family": "Synthetic"}]})
    assert w.status_code == 201, w.text
