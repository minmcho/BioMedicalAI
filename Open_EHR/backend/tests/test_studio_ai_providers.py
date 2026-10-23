"""PLATFORM PASS Workstream 10 — AI providers registry
(app.ai_providers, Studio parity row 4).

Composition (as in test_platform_reports.py): create_app + the new
router included in the fixture. Governance lives on StudioConfig —
the module adds no models of its own. Capabilities exercised are
existing ones only: governance rides studio.manage, reads ride
studio.read.

The credential law under test: a payload carries an API key by
ENV-VAR NAME ONLY (apiKeyRef); any credential-shaped field is
refused 422, and /ai-providers/effective reports keyConfigured as a
presence boolean (or null when no ref is set) — never a value.

Synthetic data only.

C-AI registered adjustment: ai-provider publish is four-eyes gated
(app.four_eyes) on this module's endpoint exactly as on the generic
Studio API — definitions are authored by CONFIG and published by
ADMIN (a second authorised person). Assertions are unchanged in
strength.
"""
import pytest
from fastapi.testclient import TestClient

from app import ai_providers
from app.db import Base
from app.main import create_app
from tests.conftest import ADMIN, CLINICIAN, CONFIG, VIEWER

# An env-var name that matches ^[A-Z][A-Z0-9_]*$ and is not a real
# variable in this environment (tests set/unset it via monkeypatch).
REF_VAR = "OPENCARE_AIPROV_TEST_KEY"

OLLAMA = {"name": "Local Ollama", "type": "ollama",
          "baseUrl": "http://127.0.0.1:11434", "model": "llama3.2:3b",
          "enabled": True}

HOSTED = {"name": "Hosted (dept default)", "type": "openai-compatible",
          "baseUrl": "https://example.invalid/v1", "model": "synthetic-1",
          "department": "radiology", "apiKeyRef": REF_VAR,
          "enabled": True}


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db")
    app.include_router(ai_providers.router)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as c:
        c.test_app = app
        yield c


def _create(client, key, payload, headers=CONFIG, expect=201):
    r = client.post(f"/ai-providers/definitions/{key}", json=payload,
                    headers=headers)
    assert r.status_code == expect, r.text
    return r


def _publish(client, key, payload, headers=CONFIG):
    r = _create(client, key, payload, headers=headers)
    version = r.json()["version"]
    # C-AI four-eyes: ADMIN publishes what CONFIG authored.
    r = client.post(
        f"/ai-providers/definitions/{key}/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def _effective(client, headers=VIEWER):
    r = client.get("/ai-providers/effective", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Governance flow
# ---------------------------------------------------------------------------

def test_create_draft_update_and_publish_flow(client):
    r = _create(client, "local-ollama", OLLAMA)
    body = r.json()
    assert body["status"] == "draft" and body["version"] == 1
    assert body["kind"] == "ai-provider"
    # Draft-only PUT: change the model while still a draft.
    updated = dict(OLLAMA, model="llama3.2:1b")
    r = client.put("/ai-providers/definitions/local-ollama/versions/1",
                   json=updated, headers=CONFIG)
    assert r.status_code == 200, r.text
    assert r.json()["payload"]["model"] == "llama3.2:1b"
    r = client.post(
        "/ai-providers/definitions/local-ollama/versions/1/publish",
        headers=ADMIN)  # C-AI four-eyes: not the author (CONFIG)
    assert r.status_code == 200 and r.json()["status"] == "published"
    # Published versions are immutable: update -> 409.
    r = client.put("/ai-providers/definitions/local-ollama/versions/1",
                   json=updated, headers=CONFIG)
    assert r.status_code == 409
    got = client.get("/ai-providers/definitions/local-ollama",
                     headers=CONFIG).json()
    assert got["published"]["payload"]["model"] == "llama3.2:1b"


def test_governance_capability_gates(client):
    # studio.manage gates writes: viewer AND clinician are refused.
    _create(client, "gated", OLLAMA, headers=VIEWER, expect=403)
    _create(client, "gated", OLLAMA, headers=CLINICIAN, expect=403)
    _create(client, "gated", OLLAMA)
    r = client.post("/ai-providers/definitions/gated/versions/1/publish",
                    headers=VIEWER)
    assert r.status_code == 403
    # Reads ride studio.read, which the viewer role holds.
    assert client.get("/ai-providers/effective",
                      headers=VIEWER).status_code == 200


def test_publish_supersedes_previous_version(client):
    _publish(client, "local-ollama", OLLAMA)
    _publish(client, "local-ollama", dict(OLLAMA, model="qwen3:4b"))
    got = client.get("/ai-providers/definitions/local-ollama",
                     headers=CONFIG).json()
    by_version = {v["version"]: v for v in got["versions"]}
    assert by_version[1]["status"] == "superseded"
    assert by_version[2]["status"] == "published"
    assert got["published"]["version"] == 2
    # Effective resolves to the newly published version only.
    items = _effective(client)["items"]
    assert len(items) == 1 and items[0]["model"] == "qwen3:4b"


def test_audit_rows_record_create_and_publish(client):
    _publish(client, "local-ollama", OLLAMA)
    # The audit read surface ships with create_app (app.studio router).
    audit = client.get("/studio/audit", headers=ADMIN).json()
    actions = {a["action"] for a in audit}
    assert "ai-providers.definition.create" in actions
    assert "ai-providers.definition.publish" in actions
    create = next(a for a in audit
                  if a["action"] == "ai-providers.definition.create")
    assert create["entity"] == "config:ai-provider/local-ollama@v1"
    assert create["actor"] == "dev-configurator"


# ---------------------------------------------------------------------------
# Validation — the credential law + shape rules
# ---------------------------------------------------------------------------

def test_credential_fields_refused_422(client):
    for field in ("apiKey", "password", "secret", "token", "api_key",
                  "key", "credentials"):
        payload = dict(OLLAMA, **{field: "sk-synthetic-do-not-store"})
        r = _create(client, f"evil-{field}", payload, expect=422)
        # The error NAMES the offending field.
        assert field in r.json()["detail"], r.text


def test_unknown_type_and_wrong_types_refused_422(client):
    _create(client, "bad-type", dict(OLLAMA, type="carrier-pigeon"),
            expect=422)
    _create(client, "no-model",
            {"name": "X", "type": "ollama"}, expect=422)
    _create(client, "empty-name", dict(OLLAMA, name="  "), expect=422)
    _create(client, "bad-enabled", dict(OLLAMA, enabled="yes"),
            expect=422)
    _create(client, "bad-base-url", dict(OLLAMA, baseUrl=11434),
            expect=422)


def test_bad_api_key_ref_refused_422(client):
    # Lowercase is not an env-var NAME.
    _create(client, "lower-ref", dict(HOSTED, apiKeyRef="openai_api_key"),
            expect=422)
    _create(client, "digit-ref", dict(HOSTED, apiKeyRef="1ST_KEY"),
            expect=422)
    _create(client, "dash-ref", dict(HOSTED, apiKeyRef="MY-KEY"),
            expect=422)
    # A well-formed NAME is accepted (the value is never asked for).
    _create(client, "good-ref", HOSTED)


# ---------------------------------------------------------------------------
# Visibility + effective view
# ---------------------------------------------------------------------------

def test_drafts_hidden_from_non_manage_readers(client):
    _create(client, "draft-only", OLLAMA)
    listing = client.get("/ai-providers/definitions", headers=VIEWER)
    assert all(i["key"] != "draft-only"
               for i in listing.json()["items"])
    # A draft-only key is a 404 for a viewer, visible to managers.
    assert client.get("/ai-providers/definitions/draft-only",
                      headers=VIEWER).status_code == 404
    assert client.get("/ai-providers/definitions/draft-only",
                      headers=CONFIG).status_code == 200
    # After publish the viewer can see it.
    client.post("/ai-providers/definitions/draft-only/versions/1/publish",
                headers=ADMIN)  # C-AI four-eyes: not the author (CONFIG)
    listing = client.get("/ai-providers/definitions", headers=VIEWER)
    assert any(i["key"] == "draft-only" for i in listing.json()["items"])


def test_effective_excludes_drafts_and_unpublished(client):
    _create(client, "still-draft", OLLAMA)
    _publish(client, "live-one", dict(OLLAMA, name="Live"))
    body = _effective(client)
    assert body["total"] == 1
    assert [i["key"] for i in body["items"]] == ["live-one"]
    # Top-level honesty note: no live model call is made.
    assert "No live model call" in body["note"]


def test_effective_key_configured_env_states(client, monkeypatch):
    monkeypatch.delenv(REF_VAR, raising=False)
    _publish(client, "hosted", HOSTED)
    _publish(client, "local-ollama", OLLAMA)
    items = {i["key"]: i for i in _effective(client)["items"]}
    # Shape: exactly the documented public fields.
    assert set(items["hosted"]) == {"key", "name", "type", "model",
                                    "baseUrl", "department", "enabled",
                                    "keyConfigured"}
    assert items["hosted"]["department"] == "radiology"
    # Ref set, env var ABSENT -> False (boolean, never a value).
    assert items["hosted"]["keyConfigured"] is False
    # No apiKeyRef (local Ollama) -> null.
    assert items["local-ollama"]["keyConfigured"] is None
    # Ref set, env var PRESENT -> True; the value never leaks.
    monkeypatch.setenv(REF_VAR, "sk-synthetic-secret-value")
    items = {i["key"]: i for i in _effective(client)["items"]}
    assert items["hosted"]["keyConfigured"] is True
    assert "sk-synthetic-secret-value" not in str(items)


def test_default_payload_publishable_not_auto_seeded(client):
    # Nothing is auto-seeded: a fresh deployment has no providers.
    assert _effective(client)["items"] == []
    defaults = ai_providers.default_ai_provider_payloads()
    assert set(defaults) == {"local-ollama"}
    payload = defaults["local-ollama"]
    assert payload["type"] == "ollama" and payload["enabled"] is False
    # The default is publishable through the governance API.
    _publish(client, "local-ollama", payload)
    items = _effective(client)["items"]
    assert len(items) == 1
    assert items[0]["model"] == "llama3.2:3b"
    assert items[0]["keyConfigured"] is None
