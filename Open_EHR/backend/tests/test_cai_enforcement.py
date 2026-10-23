"""C-AI — backend enforcement points (Worker W2).

Four-eyes publish/enable (app.four_eyes), the statement gate at
enable, the runtime gate on expiry (agents + workflow steps),
change-control approver integrity, tierAssessment on evaluation
runs, and the ai-provider generic-Studio lifecycle + credential law.

Choreography (the compliant path): agent drafts authored by ADMIN,
published by CONFIG, statements authored by ADMIN and approved by
CONFIG/CLINICIAN via tests.conftest.governance_for_enable, agents
enabled by CONFIG. Synthetic data only; the ADK transport is
substituted (the Phase 10 pattern) — no live model run is claimed.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import agents as agents_module
from app.agent_validation import ValidationRecord
from app.ai_governance import AiValidationStatement
from app.models import AuditLog, ConfigStatus, StudioConfig
from tests.conftest import (ADMIN, CLINICIAN, CONFIG,
                            governance_for_enable)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _agent_payload(**overrides):
    payload = {
        "description": "C-AI enforcement test agent",
        "model": "ollama_chat/llama3.2:3b",
        "instructions": "Summarise the input briefly.",
        "tools": ["opencare_api_call"],
        "apiAllowlist": ["/fhir"],
        "modalities": {"text": True, "image": False, "audio": False},
    }
    payload.update(overrides)
    return payload


def _draft(client, kind, key, payload, headers=ADMIN):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload,
                    headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["version"]


def _publish(client, kind, key, version, headers):
    return client.post(
        f"/studio/config/{kind}/{key}/versions/{version}/publish",
        headers=headers)


def _draft_and_publish(client, key, payload, kind="agent"):
    version = _draft(client, kind, key, payload)
    r = _publish(client, kind, key, version, CONFIG)
    assert r.status_code == 200, r.text
    return version


def _audit(client):
    factory = client.app.state.session_factory
    with factory() as session:
        return [(row.actor, row.action, row.entity)
                for row in session.scalars(select(AuditLog)).all()]


def _four_eyes_refusals(client):
    return [a for a in _audit(client)
            if a[1] == "governance.four_eyes.refused"]


def _enable_compliant(client, key):
    governance_for_enable(client, key)
    r = client.post(f"/agents/{key}/enable", headers=CONFIG)
    assert r.status_code == 200, r.text
    return r


def _ok_transport(payload):
    return 200, {"output": "DRAFT: brief summary.",
                 "model": "ollama_chat/llama3.2:3b"}


def _backdate_validation_record(client, key):
    factory = client.app.state.session_factory
    with factory() as session:
        record = session.scalars(select(ValidationRecord).where(
            ValidationRecord.agent_key == key)).first()
        assert record is not None
        record.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        session.commit()


def _backdate_statement_review_by(client, key):
    factory = client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(AiValidationStatement).where(
            AiValidationStatement.agent_key == key,
            AiValidationStatement.status == "approved")).first()
        assert row is not None
        row.review_by = datetime.now(timezone.utc) - timedelta(days=1)
        session.commit()


# ---------------------------------------------------------------------------
# Four-eyes: publish
# ---------------------------------------------------------------------------

def test_four_eyes_publish_t2_agent_author_refused_other_allowed(client):
    version = _draft(client, "agent", "fe-t2",
                     _agent_payload(riskTier="T2"))
    r = _publish(client, "agent", "fe-t2", version, ADMIN)
    assert r.status_code == 403, r.text
    assert "Four-eyes" in r.json()["detail"]
    # The refusal itself is audited.
    refusals = _four_eyes_refusals(client)
    assert refusals and refusals[-1][0] == "dev-admin"
    assert refusals[-1][2] == "config:agent/fe-t2@v1"
    # A second authorised person publishes compliantly.
    r = _publish(client, "agent", "fe-t2", version, CONFIG)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"


def test_four_eyes_publish_unstamped_agent_defaults_to_t2(client):
    # No riskTier in the payload: governed as T2 (four_eyes.agent_tier_of).
    version = _draft(client, "agent", "fe-unstamped", _agent_payload())
    r = _publish(client, "agent", "fe-unstamped", version, ADMIN)
    assert r.status_code == 403, r.text
    r = _publish(client, "agent", "fe-unstamped", version, CONFIG)
    assert r.status_code == 200, r.text


def test_four_eyes_publish_t1_agent_author_allowed(client):
    version = _draft(client, "agent", "fe-t1",
                     _agent_payload(riskTier="T1"))
    r = _publish(client, "agent", "fe-t1", version, ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"
    assert _four_eyes_refusals(client) == []


def test_agent_risk_tier_validation(client):
    r = client.post("/studio/config/agent/fe-bad-tier",
                    json=_agent_payload(riskTier="T9"), headers=ADMIN)
    assert r.status_code == 422, r.text


def test_seeded_agents_carry_framework_tiers(client):
    roster = {row["key"]: row for row in client.get(
        "/studio/config?kind=agent", headers=ADMIN).json()}
    for key in ("note-coding-assistant", "lab-result-summariser",
                "pharmacy-dur-reviewer"):
        assert roster[key]["payload"]["riskTier"] == "T2", key
        # Honesty invariant: tiers are metadata; no seeded model route.
        assert roster[key]["payload"]["model"] == ""
    rag = {row["key"]: row for row in client.get(
        "/studio/config?kind=agent", headers=ADMIN).json()}
    assert rag["rag-answerer"]["payload"]["riskTier"] == "T1"
    assert rag["radiology-vision-assistant"]["payload"]["riskTier"] == "T3"


# ---------------------------------------------------------------------------
# Four-eyes: ai-provider publish (generic API + module endpoint)
# ---------------------------------------------------------------------------

_PROVIDER = {"name": "Local Ollama", "type": "ollama",
             "baseUrl": "http://127.0.0.1:11434",
             "model": "llama3.2:3b", "enabled": True}


def test_four_eyes_publish_ai_provider_generic_api(client):
    version = _draft(client, "ai-provider", "fe-provider", _PROVIDER)
    r = _publish(client, "ai-provider", "fe-provider", version, ADMIN)
    assert r.status_code == 403, r.text
    assert "Four-eyes" in r.json()["detail"]
    assert _four_eyes_refusals(client)
    r = _publish(client, "ai-provider", "fe-provider", version, CONFIG)
    assert r.status_code == 200, r.text


def test_four_eyes_publish_ai_provider_module_endpoint(client):
    r = client.post("/ai-providers/definitions/fe-mod-provider",
                    json=_PROVIDER, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post(
        "/ai-providers/definitions/fe-mod-provider/versions/1/publish",
        headers=ADMIN)
    assert r.status_code == 403, r.text
    assert "Four-eyes" in r.json()["detail"]
    assert _four_eyes_refusals(client)
    r = client.post(
        "/ai-providers/definitions/fe-mod-provider/versions/1/publish",
        headers=CONFIG)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"


def test_ai_provider_generic_lifecycle_and_credential_law(client):
    # Credential-shaped payload through the GENERIC Studio path -> 422.
    bad = dict(_PROVIDER, apiKey="sk-synthetic-do-not-store")
    r = client.post("/studio/config/ai-provider/fe-bad", json=bad,
                    headers=ADMIN)
    assert r.status_code == 422, r.text
    # Valid draft -> publish (second actor) -> effective via generic API.
    version = _draft(client, "ai-provider", "fe-generic", _PROVIDER)
    r = _publish(client, "ai-provider", "fe-generic", version, CONFIG)
    assert r.status_code == 200, r.text
    r = client.get("/studio/config/ai-provider/effective/fe-generic",
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["payload"]["model"] == "llama3.2:3b"
    r = client.get("/ai-providers/effective", headers=ADMIN)
    assert [i["key"] for i in r.json()["items"]] == ["fe-generic"]


# ---------------------------------------------------------------------------
# Statement gate + four-eyes at enable
# ---------------------------------------------------------------------------

def test_enable_without_statement_is_409_naming_statement(client):
    _draft_and_publish(client, "fe-nostmt", _agent_payload())
    # Validation record only (the BD-41 evidence) — no statement.
    r = client.post("/agent-validation/datasets", json={
        "key": "enable-fe-nostmt", "name": "record only",
        "cases": [{"id": "c1", "prompt": "Summarise the input briefly.",
                   "recordedOutput": "DRAFT: brief summary.",
                   "checks": [{"type": "contains", "value": "DRAFT"}]}]},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/agent-validation/datasets/enable-fe-nostmt/run",
                    json={"agentKey": "fe-nostmt", "mode": "recorded"},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/agents/fe-nostmt/enable", headers=CONFIG)
    assert r.status_code == 409, r.text
    assert "statement" in r.json()["detail"].lower()


def test_enable_with_expired_statement_is_409(client):
    _draft_and_publish(client, "fe-expstmt", _agent_payload())
    _enable_compliant(client, "fe-expstmt")
    client.post("/agents/fe-expstmt/disable", headers=ADMIN)
    _backdate_statement_review_by(client, "fe-expstmt")
    r = client.post("/agents/fe-expstmt/enable", headers=CONFIG)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"].lower()
    assert "statement" in detail and "expired" in detail


def test_four_eyes_enable_author_refused_other_allowed(client):
    _draft_and_publish(client, "fe-enable", _agent_payload())
    governance_for_enable(client, "fe-enable")  # statement by ADMIN
    # The author (config + statement) may not enable, even with the
    # record and statement current.
    r = client.post("/agents/fe-enable/enable", headers=ADMIN)
    assert r.status_code == 403, r.text
    assert "Four-eyes" in r.json()["detail"]
    assert _four_eyes_refusals(client)
    # Another authorised person enables compliantly.
    r = client.post("/agents/fe-enable/enable", headers=CONFIG)
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True
    assert r.json()["risk_tier"] == "T2"


# ---------------------------------------------------------------------------
# Runtime gate: expiry refusals after compliant enablement
# ---------------------------------------------------------------------------

def test_runtime_refusal_when_validation_record_expires(client, monkeypatch):
    _draft_and_publish(client, "fe-rt-record", _agent_payload())
    _enable_compliant(client, "fe-rt-record")
    monkeypatch.setattr(agents_module, "_post_run", _ok_transport)
    r = client.post("/agents/fe-rt-record/run", json={"prompt": "hi"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    _backdate_validation_record(client, "fe-rt-record")
    r = client.post("/agents/fe-rt-record/run", json={"prompt": "hi"},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"].lower()
    assert "expired" in detail and "validation record" in detail


def test_runtime_refusal_when_statement_expires(client, monkeypatch):
    _draft_and_publish(client, "fe-rt-stmt", _agent_payload())
    _enable_compliant(client, "fe-rt-stmt")
    monkeypatch.setattr(agents_module, "_post_run", _ok_transport)
    r = client.post("/agents/fe-rt-stmt/run", json={"prompt": "hi"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    _backdate_statement_review_by(client, "fe-rt-stmt")
    r = client.post("/agents/fe-rt-stmt/run", json={"prompt": "hi"},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"].lower()
    assert "expired" in detail and "statement" in detail


def test_workflow_step_refusal_on_expired_record(client, monkeypatch):
    _draft_and_publish(client, "fe-flow-agent", _agent_payload())
    _enable_compliant(client, "fe-flow-agent")
    monkeypatch.setattr(agents_module, "_post_run", _ok_transport)
    factory = client.app.state.session_factory
    with factory() as session:
        session.add(StudioConfig(
            kind="agentflow", key="fe-flow", version=1,
            status=ConfigStatus.published,
            payload={"description": "one agent step",
                     "steps": [{"type": "agent",
                                "agentKey": "fe-flow-agent"}]},
            created_by="test", updated_by="test"))
        session.commit()
    r = client.post("/agent-workflows/fe-flow/run",
                    json={"input": "Draft please"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "completed"
    _backdate_validation_record(client, "fe-flow-agent")
    r = client.post("/agent-workflows/fe-flow/run",
                    json={"input": "Draft please"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["status"] == "failed"
    assert "expired" in (run["failure_reason"] or "").lower()


# ---------------------------------------------------------------------------
# Change control: approver integrity
# ---------------------------------------------------------------------------

def test_change_control_self_approve_refused_and_spoof_ignored(client):
    _draft_and_publish(client, "fe-cc", _agent_payload())
    r = client.post("/agent-validation/change-control", json={
        "agentKey": "fe-cc", "fromVersion": 1, "toVersion": 2,
        "intendedChange": "Model version change",
        "validationPlanId": "plan-1"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    record = r.json()
    # Self-approval is refused + audited, even naming another approver.
    r = client.post(
        f"/agent-validation/change-control/{record['id']}/approve",
        json={"approver": "dev-configurator"}, headers=ADMIN)
    assert r.status_code == 403, r.text
    assert _four_eyes_refusals(client)
    # A second actor approves; a spoofed body approver is ignored —
    # the recorded approver is the authenticated user — while
    # resultRef from the body is honoured.
    r = client.post(
        f"/agent-validation/change-control/{record['id']}/approve",
        json={"approver": "mallory", "resultRef": "run-123"},
        headers=CONFIG)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert r.json()["approver"] == "dev-configurator"
    assert r.json()["resultRef"] == "run-123"


# ---------------------------------------------------------------------------
# tierAssessment on evaluation runs
# ---------------------------------------------------------------------------

def _run_dataset(client, dataset_key, agent_key, cases):
    r = client.post("/agent-validation/datasets",
                    json={"key": dataset_key, "name": dataset_key,
                          "cases": cases}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post(f"/agent-validation/datasets/{dataset_key}/run",
                    json={"agentKey": agent_key, "mode": "recorded"},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def test_tier_assessment_present_and_scored(client):
    _draft_and_publish(client, "fe-tier", _agent_payload(riskTier="T2"))
    run = _run_dataset(client, "fe-tier-ds", "fe-tier", [
        {"id": "c1", "prompt": "Summarise.",
         "recordedOutput": "DRAFT: ok.",
         "checks": [{"type": "contains", "value": "DRAFT"}]}])
    assessment = run["tierAssessment"]
    assert assessment["tier"] == "T2"
    assert assessment["thresholds"] == {
        "minOverallPass": 0.95, "minSliceFloor": 0.90,
        "maxSliceDisparity": 0.10}
    assert assessment["meets"] is True
    assert "T2" in assessment["detail"]
    # A failing run does NOT meet the tier thresholds.
    run = _run_dataset(client, "fe-tier-ds-fail", "fe-tier", [
        {"id": "c1", "prompt": "Summarise.",
         "recordedOutput": "nothing useful",
         "checks": [{"type": "contains", "value": "DRAFT"}]}])
    assert run["tierAssessment"]["meets"] is False
    assert run["tierAssessment"]["tier"] == "T2"
