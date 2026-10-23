"""ADK runtime service tests (Phase 10, Track A).

Honesty is the contract under test: with no model configured /run is
a 503 explanation, never a fabricated answer; modalities the agent
config did not enable are refused; the API tool cannot leave its
allowlist. The model path itself is exercised only up to the boundary
(no live LLM endpoint in development) — parse/validation, resolution
and the no-model 503 are what ship.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.config import is_path_allowed, parse_agent_config, resolve_model
from app.main import app

client = TestClient(app)

AGENT = {
    "key": "test-agent",
    "version": 1,
    "model": "",
    "instructions": "Summarise.",
    "tools": ["opencare_api_call"],
    "apiAllowlist": ["/fhir"],
    "modalities": {"text": True, "image": False, "audio": False},
}


def test_health_reports_no_model_honestly():
    out = client.get("/health").json()
    assert out["ok"] is True
    assert out["service"] == "opencare-adk"
    # The test environment configures no AGENT_MODEL.
    assert out["modelConfigured"] is False


def test_run_without_model_is_503_never_fabricated():
    r = client.post("/run", json={"agent": AGENT, "prompt": "hi"})
    assert r.status_code == 503
    assert "No model configured" in r.json()["error"]
    assert "No answer was generated" in r.json()["error"]


def test_run_requires_agent_config_object():
    r = client.post("/run", json={"prompt": "hi"})
    assert r.status_code == 422
    bad = dict(AGENT, tools=["shell"])
    r = client.post("/run", json={"agent": bad, "prompt": "hi"})
    assert r.status_code == 422


def test_run_modality_refused_before_model_check():
    r = client.post("/run", json={"agent": AGENT, "prompt": "hi",
                                  "imageBase64": "aGVsbG8="})
    assert r.status_code == 422
    assert "image" in r.json()["error"]


def test_sync_and_roster():
    r = client.post("/agents/sync", json={"agents": [AGENT]})
    assert r.status_code == 200, r.text
    assert r.json()["agents"] == ["test-agent"]
    roster = client.get("/agents").json()["agents"]
    assert roster[0]["name"] == "test-agent"
    assert roster[0]["modalities"] == {"image": False, "audio": False}
    bad = dict(AGENT, key="bad", model="sk-secret")
    r = client.post("/agents/sync", json={"agents": [bad]})
    assert r.status_code == 422


def test_resolve_model_prefers_config_then_env_then_none():
    assert resolve_model("openai/gpt-x") == "openai/gpt-x"
    env = os.environ.get("AGENT_MODEL", "").strip()
    assert resolve_model("") == env


def test_allowlist_is_strict():
    assert is_path_allowed("/fhir/Patient/pat-1", "GET", ["/fhir"]) is True
    assert is_path_allowed("/lab/orders", "GET", ["/fhir"]) is False
    assert is_path_allowed("/fhir/../studio/config", "GET", ["/fhir"]) is False
    assert is_path_allowed("https://evil.example/fhir", "GET", ["/fhir"]) is False
    assert is_path_allowed("/fhir", "GET", []) is False


def test_parse_rejects_credential_in_model_and_unknown_tool():
    import pytest
    with pytest.raises(ValueError):
        parse_agent_config(dict(AGENT, model="sk-live-secret"))
    with pytest.raises(ValueError):
        parse_agent_config(dict(AGENT, tools=["shell"]))
    with pytest.raises(ValueError):
        parse_agent_config(dict(AGENT, instructions=""))
