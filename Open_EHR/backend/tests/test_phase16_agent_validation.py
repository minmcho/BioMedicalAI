"""Phase 16, WS-C3 / BD-41 — AI model validation harness.

Governed evaluation datasets (hashed + audited), recorded-mode runs
scoring known-answer cases exactly, per-slice pass rates with a
disparity flag, ValidationRecords minted by completed runs and
honoured (current / expired / missing) by validation_status, drift
flagging a shifted disposition reject rate against the validation
baseline, PCCP change-control with an exceptions list, seeded
grandfathered baselines for the two Phase 10 agents, and live mode
honest-off (503, nothing recorded) when there is no model.

Synthetic data only. No live model run is claimed: recorded mode
scores fixture outputs, and live mode is exercised only on its
no-model 503 path (transport substituted, the Phase 10 pattern).

C-AI registered adjustment: agent drafts authored by ADMIN are now
published by CONFIG (four-eyes publish gate for T2/T3 agents,
app.four_eyes), and change-control approval is four-eyes against
the record's creator with the approver taken from the authenticated
user — the change-control test now asserts the self-approval
refusal and the compliant second-actor approval. Assertions are
unchanged in strength.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import agent_validation as validation_module
from app import agents as agents_module
from app.agent_validation import (EvaluationRun, ValidationRecord,
                                  validation_status)
from app.models import AgentRun, AuditLog
from tests.conftest import ADMIN, CLINICIAN, CONFIG, VIEWER


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _publish_agent(client, key, **overrides):
    payload = {
        "description": "Validation test agent",
        "model": "ollama_chat/llama3.2:3b",
        "instructions": "Summarise the input briefly.",
        "tools": ["opencare_api_call"],
        "apiAllowlist": ["/fhir"],
        "modalities": {"text": True, "image": False, "audio": False},
    }
    payload.update(overrides)
    r = client.post(f"/studio/config/agent/{key}", json=payload,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/agent/{key}/versions/{version}/publish",
        headers=CONFIG)  # C-AI four-eyes: author ADMIN may not publish
    assert r.status_code == 200, r.text
    return version


def _publish_seeded(client, key):
    r = client.post(f"/studio/config/agent/{key}/versions/1/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text


def _dataset(client, key, cases, **extra):
    body = {"key": key, "name": key, "cases": cases}
    body.update(extra)
    r = client.post("/agent-validation/datasets", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _case(cid, output, checks, slices=None, prompt=None):
    case = {"id": cid, "prompt": prompt or f"Prompt for {cid}",
            "checks": checks, "slices": slices or []}
    if output is not None:
        case["recordedOutput"] = output
    return case


def _audit_actions(client):
    factory = client.app.state.session_factory
    with factory() as session:
        return [row.action for row in session.scalars(select(AuditLog)).all()]


def _run_dataset(client, dataset_key, agent_key, mode="recorded"):
    r = client.post(f"/agent-validation/datasets/{dataset_key}/run",
                    json={"agentKey": agent_key, "mode": mode},
                    headers=ADMIN)
    return r


# ---------------------------------------------------------------------------
# Governance config + datasets
# ---------------------------------------------------------------------------

def test_validation_config_is_published_with_gate_thresholds(client):
    r = client.get("/agent-validation/config", headers=ADMIN)
    assert r.status_code == 200, r.text
    cfg = r.json()["config"]
    assert cfg["requireValidationForEnable"] is True
    assert cfg["validationValidityDays"] == 180
    assert cfg["sliceDisparityThreshold"] == 0.2
    # And it is a real published Studio config, not a constant.
    published = client.get(
        "/studio/config/integration/effective/agent-validation",
        headers=ADMIN)
    assert published.status_code == 200


def test_dataset_create_hashes_audits_and_rejects_duplicates(client):
    cases = [_case("c1", "alpha draft",
                   [{"type": "contains", "value": "alpha"}])]
    ds = _dataset(client, "ds-one", cases)
    assert ds["caseCount"] == 1
    assert len(ds["casesHash"]) == 64
    assert ds["cases"][0]["id"] == "c1"
    assert "agent_validation.dataset.create" in _audit_actions(client)
    # Same key again -> 409 (datasets are governed, never silently
    # overwritten).
    r = client.post("/agent-validation/datasets",
                    json={"key": "ds-one", "cases": cases}, headers=ADMIN)
    assert r.status_code == 409
    # Unknown check type -> 422; empty case list -> 422.
    bad = [_case("c2", "x", [{"type": "regex", "value": "x"}])]
    r = client.post("/agent-validation/datasets",
                    json={"key": "ds-bad", "cases": bad}, headers=ADMIN)
    assert r.status_code == 422
    r = client.post("/agent-validation/datasets",
                    json={"key": "ds-empty", "cases": []}, headers=ADMIN)
    assert r.status_code == 422
    # The viewer role holds neither agents.view nor agents.validate.
    r = client.post("/agent-validation/datasets",
                    json={"key": "ds-viewer", "cases": cases},
                    headers=VIEWER)
    assert r.status_code == 403
    r = client.get("/agent-validation/datasets", headers=VIEWER)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Recorded-mode scoring + ValidationRecord minting
# ---------------------------------------------------------------------------

def test_recorded_run_scores_known_answers_exactly_and_mints_record(client):
    _publish_agent(client, "val-agent")
    cases = [
        _case("pass-contains", "Draft note for human review",
              [{"type": "contains", "value": "Draft"},
               {"type": "not-contains", "value": "signed"}]),
        _case("pass-exact", "EXACT-OUTPUT",
              [{"type": "exact", "value": "EXACT-OUTPUT"}]),
        _case("deliberate-fail", "Draft note for human review",
              [{"type": "contains", "value": "MISSING-TERM"}]),
    ]
    _dataset(client, "ds-scoring", cases)
    r = _run_dataset(client, "ds-scoring", "val-agent")
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["mode"] == "recorded"
    assert run["totalCases"] == 3 and run["passedCases"] == 2
    assert run["passRate"] == pytest.approx(2 / 3, abs=0.001)
    by_id = {c["caseId"]: c for c in run["caseResults"]}
    assert by_id["pass-contains"]["passed"] is True
    assert by_id["pass-exact"]["passed"] is True
    assert by_id["deliberate-fail"]["passed"] is False
    assert by_id["deliberate-fail"]["checks"][0]["passed"] is False
    # The completed run minted a ValidationRecord for v1 + the model.
    record = run["validationRecord"]
    assert record["agentKey"] == "val-agent"
    assert record["agentConfigVersion"] == 1
    assert record["modelRoute"] == "ollama_chat/llama3.2:3b"
    assert record["runId"] == run["runId"]
    assert record["passRate"] == pytest.approx(2 / 3, abs=0.001)
    assert "agent_validation.run" in _audit_actions(client)
    assert "agent_validation.validation.issue" in _audit_actions(client)
    # validation_status (the enablement-gate function) reads it back.
    status = client.get("/agent-validation/agents/val-agent/status",
                        headers=CLINICIAN).json()
    assert status["status"] == "current"
    assert status["required"] is True
    assert status["record"]["recordKey"] == record["recordKey"]


def test_slice_disparity_flag_fires(client):
    _publish_agent(client, "slice-agent")
    cases = [
        _case("a1", "good output", [{"type": "contains", "value": "good"}],
              slices=["group-a"]),
        _case("a2", "good again", [{"type": "contains", "value": "good"}],
              slices=["group-a"]),
        _case("b1", "poor output", [{"type": "contains", "value": "good"}],
              slices=["group-b"]),
        _case("b2", "poor again", [{"type": "contains", "value": "good"}],
              slices=["group-b"]),
    ]
    _dataset(client, "ds-slices", cases)
    r = _run_dataset(client, "ds-slices", "slice-agent")
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["sliceMetrics"]["group-a"]["passRate"] == 1.0
    assert run["sliceMetrics"]["group-b"]["passRate"] == 0.0
    disparity = [f for f in run["disparityFlags"]
                 if f["type"] == "slice-disparity"]
    assert disparity and disparity[0]["disparity"] == 1.0
    assert disparity[0]["threshold"] == 0.2


def test_validation_status_expiry_and_missing(client):
    _publish_agent(client, "expiry-agent")
    # Missing: published but never validated.
    status = client.get("/agent-validation/agents/expiry-agent/status",
                        headers=ADMIN).json()
    assert status["status"] == "missing"
    # Validate, then age the record past its validity window directly
    # (deterministic; the harness honours expires_at, not wall-clock
    # luck).
    _dataset(client, "ds-expiry",
             [_case("c1", "fine", [{"type": "contains", "value": "fine"}])])
    r = _run_dataset(client, "ds-expiry", "expiry-agent")
    assert r.status_code == 201, r.text
    factory = client.app.state.session_factory
    with factory() as session:
        record = session.scalars(select(ValidationRecord).where(
            ValidationRecord.agent_key == "expiry-agent")).first()
        assert record is not None
        record.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        session.commit()
        aged = validation_status(session, "expiry-agent")
        assert aged["status"] == "expired"
    status = client.get("/agent-validation/agents/expiry-agent/status",
                        headers=ADMIN).json()
    assert status["status"] == "expired"
    # An agent with no published version is missing, not an error.
    status = client.get("/agent-validation/agents/ghost-agent/status",
                        headers=ADMIN).json()
    assert status["status"] == "missing"
    assert status["publishedVersion"] is None


def test_seeded_agents_have_current_baseline_validation(client):
    # The grandfathered baselines: publishing the seeded v1s (model
    # route still the empty string) leaves both agents with current
    # validation, so their Phase 10 enable flow keeps working with
    # the gate ON.
    for key in ("note-coding-assistant", "lab-result-summariser"):
        _publish_seeded(client, key)
        status = client.get(f"/agent-validation/agents/{key}/status",
                            headers=ADMIN).json()
        assert status["status"] == "current", (key, status)
        assert status["record"]["agentConfigVersion"] == 1
        assert status["record"]["datasetKey"] == \
            "baseline-seeded-agents"
    runs = client.get("/agent-validation/runs", headers=ADMIN).json()
    baseline_runs = [r for r in runs["items"]
                     if r["datasetKey"] == "baseline-seeded-agents"]
    assert {r["agentKey"] for r in baseline_runs} == {
        "note-coding-assistant", "lab-result-summariser"}
    assert all(r["passRate"] == 1.0 for r in baseline_runs)


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------

def _insert_agent_run(client, agent_key, output, disposition):
    factory = client.app.state.session_factory
    with factory() as session:
        row = AgentRun(run_id=f"test-{agent_key}-{disposition}-"
                              f"{abs(hash((output, disposition))) % 10**8}-"
                              f"{session.scalars(select(AgentRun)).all().__len__()}",
                       agent_key=agent_key, agent_version=1,
                       actor="dev-admin", input_hash="0" * 64,
                       output=output, model="ollama_chat/llama3.2:3b",
                       disposition=disposition,
                       disposition_by="dev-admin")
        session.add(row)
        session.commit()


def test_drift_flags_shifted_reject_rate(client):
    _publish_agent(client, "drift-agent")
    # Baseline behaviour at validation time: short outputs, all
    # accepted (reject rate 0).
    for i in range(4):
        _insert_agent_run(client, "drift-agent", "short ok", "accepted")
    _dataset(client, "ds-drift",
             [_case("c1", "fine", [{"type": "contains", "value": "fine"}])])
    r = _run_dataset(client, "ds-drift", "drift-agent")
    assert r.status_code == 201, r.text
    assert r.json()["validationRecord"]["baseline"]["rejectRate"] == 0.0
    before = client.get("/agent-validation/agents/drift-agent/drift",
                        headers=ADMIN).json()
    assert before["status"] == "within-threshold"
    # Live behaviour shifts: everything is now rejected.
    for i in range(10):
        _insert_agent_run(client, "drift-agent", "short ok", "rejected")
    drift = client.get("/agent-validation/agents/drift-agent/drift",
                       headers=ADMIN).json()
    assert drift["status"] == "drift-flagged"
    assert drift["baseline"]["rejectRate"] == 0.0
    assert drift["recent"]["rejectRate"] == pytest.approx(10 / 14, abs=0.01)
    flags = [f for f in drift["flags"] if f["metric"] == "rejectRate"]
    assert flags and flags[0]["delta"] > 0.2


def test_drift_without_validation_record_is_no_baseline(client):
    _publish_agent(client, "unvalidated-agent")
    drift = client.get("/agent-validation/agents/unvalidated-agent/drift",
                       headers=ADMIN).json()
    assert drift["status"] == "no-baseline"
    assert drift["flags"] == []


# ---------------------------------------------------------------------------
# Change control + exceptions
# ---------------------------------------------------------------------------

def test_change_control_and_exceptions(client):
    # A freshly published agent with no change-control record is an
    # exception; the seeded agents are NOT (baseline records).
    _publish_agent(client, "cc-agent")
    _publish_seeded(client, "note-coding-assistant")
    exceptions = client.get("/agent-validation/exceptions",
                            headers=ADMIN).json()
    keys = {item["agentKey"] for item in exceptions["items"]}
    assert "cc-agent" in keys
    assert "note-coding-assistant" not in keys
    # File the PCCP record -> the exception clears.
    r = client.post("/agent-validation/change-control", json={
        "agentKey": "cc-agent", "fromVersion": 1, "toVersion": 2,
        "intendedChange": "Model route change to a larger local model",
        "validationPlanId": "ds-scoring"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    record = r.json()
    assert record["status"] == "proposed"
    # C-AI: the record's creator may not approve it (four-eyes), even
    # naming someone else in the body — the approver is the
    # authenticated user. Self-approval is refused...
    r = client.post(
        f"/agent-validation/change-control/{record['id']}/approve",
        json={"approver": "dev-configurator"}, headers=ADMIN)
    assert r.status_code == 403, r.text
    # ...and a second actor approves compliantly.
    r = client.post(
        f"/agent-validation/change-control/{record['id']}/approve",
        json={}, headers=CONFIG)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert r.json()["approver"] == "dev-configurator"
    assert "agent_validation.change_control.approve" in _audit_actions(client)
    # The record names toVersion 2, so v1 remains the live exception
    # only if no v1 record exists — cc-agent still lacks a v1 record:
    # file one for v1 and the exception list empties for this agent.
    r = client.post("/agent-validation/change-control", json={
        "agentKey": "cc-agent", "fromVersion": 0, "toVersion": 1,
        "intendedChange": "Initial publication of cc-agent",
        "validationPlanId": "ds-scoring"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    exceptions = client.get("/agent-validation/exceptions",
                            headers=ADMIN).json()
    assert "cc-agent" not in {i["agentKey"] for i in exceptions["items"]}


# ---------------------------------------------------------------------------
# Live mode: honest-off
# ---------------------------------------------------------------------------

def test_live_mode_no_model_is_honest_503_and_records_nothing(client,
                                                              monkeypatch):
    _publish_agent(client, "live-agent", model="")
    _dataset(client, "ds-live",
             [_case("c1", None, [{"type": "contains", "value": "Draft"}],
                    prompt="Summarise the synthetic input")])

    def _no_model(payload):
        return 503, {"error": "No model configured: set a model on the "
                              "agent config or AGENT_MODEL on the ADK "
                              "runtime. No answer was generated."}

    monkeypatch.setattr(agents_module, "_post_run", _no_model)
    r = _run_dataset(client, "ds-live", "live-agent", mode="live")
    assert r.status_code == 503, r.text
    assert "No model configured" in r.json()["detail"]
    # Nothing was recorded: no evaluation run, no validation record.
    factory = client.app.state.session_factory
    with factory() as session:
        assert session.scalars(select(EvaluationRun).where(
            EvaluationRun.agent_key == "live-agent")).all() == []
        assert session.scalars(select(ValidationRecord).where(
            ValidationRecord.agent_key == "live-agent")).all() == []
    status = client.get("/agent-validation/agents/live-agent/status",
                        headers=ADMIN).json()
    assert status["status"] == "missing"
