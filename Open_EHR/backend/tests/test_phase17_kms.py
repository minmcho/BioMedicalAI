"""Phase 17, BD-51 (CMP-30) — KMS key management + rotation.

The rotation drill is the acceptance evidence: data encrypted BEFORE
rotation must still read AFTER rotation + re-wrap, and the retired
key must no longer unwrap anything. The software provider is a local
SOFTWARE-KMS — labelled as such, never an HSM. Synthetic data only.
"""
import base64
import json

import pytest
from sqlalchemy import select

from app import crypto
from app.models import FhirResource
from tests.conftest import ADMIN, CLINICIAN, CONFIG

MASTER = base64.b64encode(b"\x07" * 32).decode()


@pytest.fixture()
def kms_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENEHR_KMS_PROVIDER", "software-kms")
    monkeypatch.setenv("OPENEHR_KMS_KEYRING", str(tmp_path / "keyring.json"))
    monkeypatch.setenv("OPENEHR_KMS_MASTER_KEY", MASTER)
    return tmp_path


def _patient_and_message(client, secret):
    r = client.post("/fhir/Patient", headers=CLINICIAN, json={
        "resourceType": "Patient", "id": "pat-1",
        "name": [{"family": "Synthetic", "given": ["Kms"]}]})
    assert r.status_code == 201, r.text
    r = client.post("/messages", headers=CLINICIAN, json={
        "patient": "pat-1", "text": secret, "topic": "check-in"})
    assert r.status_code == 201, r.text
    return r.json()["threadId"], r.json()["id"]


def _stored_envelope(client, message_id):
    factory = client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(FhirResource).where(
            FhirResource.resource_id == message_id)).first()
        content = row.body["payload"][0]["contentString"]
        return json.loads(content), row.body


def test_rotation_drill_rotate_rewrap_retire(client, kms_env):
    """rotate -> data still readable -> re-wrap -> data still
    readable -> retire -> data STILL readable (now under the new
    key) and the retired key no longer unwraps the old envelope."""
    secret = "Synthetic KMS drill: rotate me, rewrap me, retire my key"
    thread_id, message_id = _patient_and_message(client, secret)
    old_envelope, _body = _stored_envelope(client, message_id)
    old_kid = old_envelope["kid"]

    status = client.get("/security/kms/status", headers=ADMIN)
    assert status.status_code == 200, status.text
    status = status.json()
    assert status["provider"] == "software-kms"
    assert "SOFTWARE-KMS" in status["provider_label"]
    assert "NOT AN HSM" in status["provider_label"]
    assert status["is_hsm"] is False
    assert status["active_kid"] == old_kid
    assert status["schedule"]["rotation_interval_days"] == 90
    assert "kms-rotation@v1" in status["schedule"]["source"]

    # ROTATE: new active key; the old one is retiring (still resolves).
    rotated = client.post("/security/kms/rotate", headers=ADMIN)
    assert rotated.status_code == 200, rotated.text
    rotated = rotated.json()
    new_kid = rotated["active_kid"]
    assert rotated["previous_kid"] == old_kid and new_kid != old_kid

    # Data encrypted before rotation still reads (API + unit level).
    thread = client.get(f"/messages/threads/{thread_id}",
                        headers=CLINICIAN).json()
    assert thread["messages"][0]["text"] == secret
    assert crypto.decrypt(old_envelope) == secret

    # RE-WRAP: every stored envelope moves onto the new KEK.
    rewrapped = client.post("/security/kms/rewrap", headers=ADMIN)
    assert rewrapped.status_code == 200, rewrapped.text
    rewrapped = rewrapped.json()
    assert rewrapped["rewrapped"] >= 1
    assert rewrapped["active_kid"] == new_kid
    new_envelope, body = _stored_envelope(client, message_id)
    assert new_envelope["kid"] == new_kid
    assert new_envelope["ct"] == old_envelope["ct"]  # DEK re-wrap only
    assert new_envelope["n"] == old_envelope["n"]
    flags = [e for e in body["extension"]
             if e["url"].endswith("encrypted-content")]
    assert flags and flags[0]["valueString"] == new_kid

    # RETIRE the old key: material destroyed.
    retired = client.post(f"/security/kms/keys/{old_kid}/retire",
                          headers=ADMIN)
    assert retired.status_code == 200, retired.text
    assert retired.json()["material_destroyed"] is True
    keys = client.get("/security/kms/keys", headers=ADMIN).json()["keys"]
    old_key = [k for k in keys if k["kid"] == old_kid][0]
    assert old_key["status"] == "retired" and old_key["hasMaterial"] is False

    # Data still reads — it now lives under the new key…
    thread = client.get(f"/messages/threads/{thread_id}",
                        headers=CLINICIAN).json()
    assert thread["messages"][0]["text"] == secret
    assert crypto.decrypt(new_envelope) == secret
    # …and the retired key no longer unwraps the pre-rotation envelope.
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(old_envelope)


def test_retire_active_key_refused_and_role_gate(client, kms_env):
    _patient_and_message(client, "guard me")  # noqa: F841
    status = client.get("/security/kms/status", headers=ADMIN).json()
    active = status["active_kid"]
    r = client.post(f"/security/kms/keys/{active}/retire", headers=ADMIN)
    assert r.status_code == 409
    # Key operations are a security-admin act, not a clinical one.
    assert client.post("/security/kms/rotate",
                       headers=CLINICIAN).status_code == 403
    assert client.get("/security/kms/status",
                      headers=CLINICIAN).status_code == 403
    assert client.get("/security/kms/status",
                      headers=CONFIG).status_code == 200


def test_schedule_is_published_studio_config(client, kms_env):
    client.get("/security/kms/status", headers=ADMIN)  # lazy seed
    eff = client.get("/studio/config/integration/effective/kms-rotation",
                     headers=ADMIN)
    assert eff.status_code == 200, eff.text
    payload = eff.json()["payload"]
    assert payload["rotationIntervalDays"] == 90
    assert payload["keyAlgorithm"] == "AES-256-GCM"
    assert "NOT AN HSM" in payload["notes"]


def test_env_provider_is_default_and_rotation_is_deployment_side(client):
    """No OPENEHR_KMS_PROVIDER -> the Phase 10 env/key-file provider,
    byte-for-byte the old behaviour; in-process rotation refuses
    with the deployment procedure (never faked)."""
    status = client.get("/security/kms/status", headers=ADMIN)
    assert status.status_code == 200, status.text
    assert status.json()["provider"] == "env"
    assert "ENV/KEY-FILE" in status.json()["provider_label"]
    envelope = crypto.encrypt("phase-10 behaviour preserved")
    assert crypto.decrypt(envelope) == "phase-10 behaviour preserved"
    r = client.post("/security/kms/rotate", headers=ADMIN)
    assert r.status_code == 409
    assert "deployment" in r.json()["detail"]


def test_auto_rewrap_on_rotate_via_published_schedule(client, kms_env):
    secret = "auto rewrap on rotate"
    thread_id, _mid = _patient_and_message(client, secret)
    # Publish schedule v2 with autoRewrapOnRotate.
    draft = client.post("/studio/config/integration/kms-rotation",
                        headers=ADMIN, json={
                            "provider": "software-kms",
                            "rotationIntervalDays": 30,
                            "autoRewrapOnRotate": True,
                            "keyAlgorithm": "AES-256-GCM"})
    assert draft.status_code == 201, draft.text
    version = draft.json()["version"]
    pub = client.post(
        f"/studio/config/integration/kms-rotation/versions/{version}/publish",
        headers=ADMIN)
    assert pub.status_code == 200, pub.text
    rotated = client.post("/security/kms/rotate", headers=ADMIN)
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["rewrap"] is not None
    assert rotated.json()["rewrap"]["rewrapped"] >= 1
    thread = client.get(f"/messages/threads/{thread_id}",
                        headers=CLINICIAN).json()
    assert thread["messages"][0]["text"] == secret
    status = client.get("/security/kms/status", headers=ADMIN).json()
    assert status["schedule"]["rotation_interval_days"] == 30
