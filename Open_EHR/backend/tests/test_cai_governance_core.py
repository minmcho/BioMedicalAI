"""Wave C-AI (W1) — governance core: worklist, disclosure, kill
switch, drift-breach tracking + auto-suspension, zero-tolerance
suspension, and per-run incident reports (AI-G5/G6/G7/G9/G10).

Identity choreography mirrors test_cai_statements (five distinct
validate-capable actors; four-eyes is enforced against both the
statement author and the agent config author):
- OWNER authors configs, ADMIN publishes + governance-approves,
  CONFIG clinically reviews, CLINICIAN authors statements,
  REVIEWER accepts T3 risk as clinical owner.

Synthetic data only.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import AgentEnablement, AgentRun, SafetyEvent
from tests.conftest import ADMIN, CLINICIAN, CONFIG

OWNER = {"Authorization": "Bearer owner-token"}
REVIEWER = {"Authorization": "Bearer reviewer-token"}

_TOKENS = {
    "admin-token": {"username": "dev-admin", "role": "admin"},
    "config-token": {"username": "dev-configurator", "role": "configurator"},
    "clinician-token": {"username": "dev-clinician", "role": "clinician"},
    "viewer-token": {"username": "dev-viewer", "role": "viewer"},
    "patient-token": {"username": "dev-patient", "role": "patient",
                      "patient_ref": "Patient/pat-1"},
    "owner-token": {"username": "dev-owner", "role": "admin"},
    "reviewer-token": {"username": "dev-reviewer", "role": "admin"},
}

PROHIBITED = ["no-autonomous-diagnosis", "no-unreviewed-order",
              "no-unreviewed-patient-communication",
              "no-excluded-population", "no-unlisted-route-tool-modality"]
APPROVERS = {"clinical_reviewer": CONFIG, "governance_approver": ADMIN,
             "clinical_owner": REVIEWER}
TIER_APPROVALS = {"T1": ["governance_approver"],
                  "T2": ["clinical_reviewer", "governance_approver"],
                  "T3": ["clinical_reviewer", "governance_approver",
                         "clinical_owner"]}
TIER_THRESHOLDS = {
    "T1": {"overallPass": 0.90, "sliceFloor": 0.85, "disparityCeiling": 0.20},
    "T2": {"overallPass": 0.95, "sliceFloor": 0.90, "disparityCeiling": 0.10},
    "T3": {"overallPass": 0.98, "sliceFloor": 0.95, "disparityCeiling": 0.05}}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENEHR_DEV_TOKENS", json.dumps(_TOKENS))
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
    with TestClient(app) as c:
        yield c


def _publish_agent(client, key, tier="T2"):
    payload = {"description": "C-AI governance test agent",
               "model": "ollama_chat/llama3.2:3b",
               "instructions": "Draft a brief summary of the input.",
               "tools": ["opencare_api_call"], "apiAllowlist": ["/fhir"],
               "modalities": {"text": True, "image": False, "audio": False},
               "riskTier": tier}
    r = client.post(f"/studio/config/agent/{key}", json=payload,
                    headers=OWNER)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/agent/{key}/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    return version


def _run_evidence(client, key, dataset_key):
    r = client.post("/agent-validation/datasets", json={
        "key": dataset_key, "name": dataset_key,
        "cases": [{"id": "c1", "prompt": "Summarise the input.",
                   "recordedOutput": "DRAFT: brief summary.",
                   "checks": [{"type": "contains", "value": "DRAFT"}]}]},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post(f"/agent-validation/datasets/{dataset_key}/run",
                    json={"agentKey": key, "mode": "recorded"},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()["runKey"]


def _full_fields(run_key, dataset_key, tier):
    return {
        "intendedUse": "Draft a brief summary of the encounter context "
                       "for the treating clinician to edit and sign.",
        "clinicalDecision": "Note content; the clinician authors and "
                            "signs the final note.",
        "riskTierRationale": "Output proposes clinical text requiring "
                             "named-human disposition.",
        "populationScope": "Adult inpatient encounters only.",
        "claims": [{"id": "C1",
                    "text": "Every fact in the draft is traceable to "
                            "the input."}],
        "evidence": [{"datasetKey": dataset_key, "runKey": run_key,
                      "mode": "recorded"}],
        "thresholds": dict(TIER_THRESHOLDS[tier]),
        "zeroToleranceClasses": [1, 2],
        "failureModes": "None found in the harness run.",
        "knownLimitations": "Recorded-mode evidence only.",
        "prohibitedUses": [{"code": c, "text": f"Prohibited: {c}"}
                           for c in PROHIBITED],
        "dispositionRule": "Clinician accepts/edits/rejects; output "
                           "is a draft; no clinical write occurs.",
        "toolsDataAccess": "opencare_api_call only; allow-list /fhir.",
        "monitoringPlan": "Monthly drift report.",
        "changeControlPlan": "Framework §10 class table adopted.",
        "incidentSuspension": "Governance approver may suspend; kill "
                              "switch covers this agent.",
    }


def _approved_statement(client, key, tier="T2"):
    run_key = _run_evidence(client, key, f"ds-{key}")
    r = client.post("/ai-governance/statements",
                    json={"agentKey": key, "owner": "Dr Jane Owner",
                          "riskTier": tier, "fields": {}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    sid = r.json()["statementId"]
    r = client.put(f"/ai-governance/statements/{sid}",
                   json={"fields": _full_fields(run_key, f"ds-{key}",
                                                tier)},
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/ai-governance/statements/{sid}/submit", json={},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    for kind in TIER_APPROVALS[tier]:
        r = client.post(f"/ai-governance/statements/{sid}/approve",
                        json={"kind": kind}, headers=APPROVERS[kind])
        assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    return sid


def _enable(client, key):
    r = client.post(f"/agents/{key}/enable", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True


def _insert_agent_run(client, key, run_id, output="A long draft output "
                                                  "produced after "
                                                  "validation."):
    factory = client.app.state.session_factory
    with factory() as session:
        session.add(AgentRun(
            run_id=run_id, agent_key=key, agent_version=1,
            actor="dev-clinician", input_hash="0" * 64, output=output,
            model="ollama_chat/llama3.2:3b", disposition="pending"))
        session.commit()


def _agent_enabled(client, key):
    agents = client.get("/agents", headers=ADMIN).json()["agents"]
    return {a["key"]: a["enabled"] for a in agents}[key]


# ---------------------------------------------------------------------------
# Worklist (AI-G10)
# ---------------------------------------------------------------------------

def test_worklist_ordering_and_owner_attribution(client):
    from app.ai_governance import AiValidationStatement
    # Agent A: validation record only, no statement -> owner falls
    # back to the published config's updated_by (the publisher).
    _publish_agent(client, "worklist-record-only", tier="T2")
    _run_evidence(client, "worklist-record-only", "ds-worklist-a")
    # Agent B: approved statement, then backdate its review-by so it
    # is overdue -> must sort first with dueState "overdue".
    _publish_agent(client, "worklist-overdue", tier="T2")
    _approved_statement(client, "worklist-overdue", tier="T2")
    factory = client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(AiValidationStatement).where(
            AiValidationStatement.agent_key == "worklist-overdue")).first()
        row.review_by = datetime.now(timezone.utc) - timedelta(days=5)
        session.commit()

    r = client.get("/ai-governance/worklist", headers=ADMIN)
    assert r.status_code == 200, r.text
    items = {i["agentKey"]: i for i in r.json()["items"]}
    assert "worklist-record-only" in items
    assert "worklist-overdue" in items
    overdue = items["worklist-overdue"]
    assert overdue["dueState"] == "overdue"
    assert overdue["owner"] == "Dr Jane Owner"
    assert overdue["statementStatus"] == "expired"
    assert overdue["statementDaysToExpiry"] < 0
    record_only = items["worklist-record-only"]
    assert record_only["statementStatus"] == "missing"
    assert record_only["recordStatus"] == "current"
    assert record_only["owner"] == "dev-admin"
    assert r.json()["items"][0]["agentKey"] == "worklist-overdue"


# ---------------------------------------------------------------------------
# Disclosure (AI-G7)
# ---------------------------------------------------------------------------

def test_disclosure_without_statement_is_honest(client):
    _publish_agent(client, "bare-agent", tier="T2")
    _run_evidence(client, "bare-agent", "ds-bare")
    r = client.get("/ai-governance/agents/bare-agent/disclosure",
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    attrs = {a["attribute"]: a["value"] for a in r.json()["attributes"]}
    assert attrs["Intended use"] == \
        "not recorded — no approved validation statement"
    assert attrs["Funding source"] == \
        "not recorded — no approved validation statement"
    # Config/record-sourced attributes are still generated, honestly.
    assert "ollama_chat/llama3.2:3b" in attrs[
        "Model / algorithm description"]
    assert "ds-bare" in attrs["Training & validation data summary"]
    assert "no external validation exists" in attrs[
        "Validation process & external validation status"]


def test_disclosure_with_approved_statement(client):
    _publish_agent(client, "described-agent", tier="T2")
    _approved_statement(client, "described-agent", tier="T2")
    r = client.get("/ai-governance/agents/described-agent/disclosure",
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["statementStatus"] == "current"
    attrs = {a["attribute"]: a["value"] for a in r.json()["attributes"]}
    assert attrs["Intended use"].startswith("Draft a brief summary")
    assert attrs["Developer / owner"] == "Dr Jane Owner"
    assert attrs["Funding source"] == "internal R&D"
    data_summary = attrs["Training & validation data summary"]
    assert "ds-described-agent" in data_summary
    assert "sha256" in data_summary and "recorded" in data_summary
    assert "overall pass 1.0000" in attrs[
        "Performance measures (overall + by subgroup)"]
    limitations = attrs["Known limitations & risk mitigations"]
    assert "no-autonomous-diagnosis" in str(limitations)


# ---------------------------------------------------------------------------
# Kill switch (AI-G5)
# ---------------------------------------------------------------------------

def test_kill_switch_flips_all_enablements_and_release_restores_none(
        client):
    factory = client.app.state.session_factory
    with factory() as session:
        for key in ("kill-a", "kill-b"):
            session.add(AgentEnablement(agent_key=key, enabled=True,
                                        updated_by="dev-admin"))
        session.commit()
    r = client.post("/ai-governance/kill-switch",
                    json={"reason": "Systemic fabricated-output incident"},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True
    assert r.json()["agentsDisabled"] == 2
    with factory() as session:
        rows = session.scalars(select(AgentEnablement)).all()
        assert all(not row.enabled for row in rows)
    # Engaging twice is a conflict; the runtime gate names the switch.
    r = client.post("/ai-governance/kill-switch",
                    json={"reason": "again"}, headers=ADMIN)
    assert r.status_code == 409, r.text
    _publish_agent(client, "kill-a", tier="T2")
    status = client.get("/ai-governance/agents/kill-a/status",
                        headers=ADMIN).json()
    assert status["killSwitchActive"] is True
    assert status["runtimeGate"]["allowed"] is False
    assert "kill switch" in status["runtimeGate"]["reason"]
    # Release clears only the state — nothing is re-enabled.
    r = client.post("/ai-governance/kill-switch/release", json={},
                    headers=CONFIG)
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False
    with factory() as session:
        rows = session.scalars(select(AgentEnablement)).all()
        assert all(not row.enabled for row in rows)
    r = client.post("/ai-governance/kill-switch/release", json={},
                    headers=CONFIG)
    assert r.status_code == 409, r.text
    # A reason is mandatory.
    r = client.post("/ai-governance/kill-switch", json={}, headers=ADMIN)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Breach checks + auto-suspension (AI-G6)
# ---------------------------------------------------------------------------

def test_breach_check_t3_auto_suspends_after_two_consecutive(client):
    _publish_agent(client, "breach-t3", tier="T3")
    _approved_statement(client, "breach-t3", tier="T3")
    _enable(client, "breach-t3")
    # Post-validation runs with real output length vs a zero-length
    # validation baseline -> the drift report flags output length.
    _insert_agent_run(client, "breach-t3", "breach-t3-run-1")
    r = client.post("/ai-governance/agents/breach-t3/breach-check",
                    json={}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["consecutiveBreaches"] == 1
    assert r.json()["flags"], r.json()
    incidents = client.get("/ai-governance/incidents", headers=ADMIN).json()
    assert incidents["total"] >= 1
    r = client.post("/ai-governance/agents/breach-t3/breach-check",
                    json={}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["consecutiveBreaches"] == 2
    assert "agent-auto-suspended" in r.json()["actionsTaken"]
    assert _agent_enabled(client, "breach-t3") is False
    factory = client.app.state.session_factory
    with factory() as session:
        events = session.scalars(select(SafetyEvent).where(
            SafetyEvent.category == "ai-agent")).all()
        assert len(events) >= 2  # breach event + auto-suspend event


def test_breach_check_t2_flags_without_auto_suspend(client):
    _publish_agent(client, "breach-t2", tier="T2")
    _approved_statement(client, "breach-t2", tier="T2")
    _enable(client, "breach-t2")
    _insert_agent_run(client, "breach-t2", "breach-t2-run-1")
    for expected in (1, 2):
        r = client.post("/ai-governance/agents/breach-t2/breach-check",
                        json={}, headers=ADMIN)
        assert r.status_code == 200, r.text
        assert r.json()["consecutiveBreaches"] == expected
        assert "agent-auto-suspended" not in r.json()["actionsTaken"]
    assert _agent_enabled(client, "breach-t2") is True


# ---------------------------------------------------------------------------
# Zero tolerance (§9.4 rule 3)
# ---------------------------------------------------------------------------

def test_zero_tolerance_suspends_agent_and_statement(client):
    _publish_agent(client, "zt-agent", tier="T2")
    _approved_statement(client, "zt-agent", tier="T2")
    _enable(client, "zt-agent")
    r = client.post("/ai-governance/agents/zt-agent/zero-tolerance",
                    json={"description": "Draft fabricated a lab value "
                                         "not present in the input.",
                          "harmLevel": "temporary"},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    assert "agent-disabled" in r.json()["actionsTaken"]
    assert r.json()["statementSuspended"] is not None
    assert _agent_enabled(client, "zt-agent") is False
    status = client.get("/ai-governance/agents/zt-agent/status",
                        headers=ADMIN).json()
    assert status["statement"]["status"] == "suspended"
    incidents = client.get("/ai-governance/incidents",
                           headers=ADMIN).json()
    assert incidents["total"] == 1
    event = incidents["items"][0]
    assert event["eventId"] == r.json()["eventId"]
    assert event["category"] == "ai-agent"
    assert event["harmLevel"] == "temporary"
    assert "fabricated a lab value" in event["description"]


# ---------------------------------------------------------------------------
# Run reports (AI-G9)
# ---------------------------------------------------------------------------

def test_run_report_creates_linked_safety_events(client):
    _publish_agent(client, "report-agent", tier="T2")
    _insert_agent_run(client, "report-agent", "report-run-1",
                      output="Draft impression text.")
    r = client.post("/ai-governance/runs/report-run-1/report",
                    json={"description": "Draft stated a finding not "
                                         "in the source image.",
                          "alsoCategories": ["medication",
                                             "not-a-category"]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    event_id = r.json()["eventId"]
    assert len(r.json()["linkedEventIds"]) == 1
    incidents = client.get("/ai-governance/incidents",
                           headers=ADMIN).json()
    assert incidents["total"] == 1
    primary = incidents["items"][0]
    assert primary["eventId"] == event_id
    assert "report-run-1" in primary["description"]
    assert "report-agent" in primary["description"]
    factory = client.app.state.session_factory
    with factory() as session:
        sibling = session.scalars(select(SafetyEvent).where(
            SafetyEvent.event_id ==
            r.json()["linkedEventIds"][0])).first()
        assert sibling is not None
        assert sibling.category == "medication"
        assert event_id in sibling.description
    # Unknown run -> 404.
    r = client.post("/ai-governance/runs/no-such-run/report",
                    json={"description": "x"}, headers=CLINICIAN)
    assert r.status_code == 404, r.text
