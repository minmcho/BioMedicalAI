"""Wave C-AI (W1) — validation statement register (AI-G1/G2).

The statement lifecycle end to end: draft (incomplete allowed) ->
submit (full §4 completeness + evidence + threshold discipline) ->
four-eyes approval by the tier's required kinds -> current, with
review-by math per tier, version supersession, suspension /
reinstatement / retirement, and the statement_status transitions
(stale on republication, expired on a passed review-by).

Identities (five distinct validate-capable actors, because approval
is four-eyes against BOTH the statement author and the agent config
author, and one actor may hold only one approval kind):
- OWNER (dev-owner): authors agent configs
- ADMIN (dev-admin): publishes configs, governance approver
- CONFIG (dev-configurator): clinical reviewer
- CLINICIAN (dev-clinician): statement author
- REVIEWER (dev-reviewer): clinical owner (T3)

Synthetic data only.
"""
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import AuditLog
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
    payload = {"description": "C-AI statement test agent",
               "model": "ollama_chat/llama3.2:3b",
               "instructions": "Draft a brief summary of the input.",
               "tools": ["opencare_api_call"], "apiAllowlist": ["/fhir"],
               "modalities": {"text": True, "image": False, "audio": False}}
    if tier:
        payload["riskTier"] = tier
    r = client.post(f"/studio/config/agent/{key}", json=payload,
                    headers=OWNER)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/agent/{key}/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    return version


def _run_evidence(client, key, dataset_key, cases=None):
    cases = cases or [{"id": "c1", "prompt": "Summarise the input.",
                       "recordedOutput": "DRAFT: brief summary.",
                       "checks": [{"type": "contains", "value": "DRAFT"}]}]
    r = client.post("/agent-validation/datasets",
                    json={"key": dataset_key, "name": dataset_key,
                          "cases": cases}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post(f"/agent-validation/datasets/{dataset_key}/run",
                    json={"agentKey": key, "mode": "recorded"},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()["runKey"]


def _full_fields(run_key, dataset_key, tier="T2", **overrides):
    fields = {
        "intendedUse": "Draft a brief summary of the encounter context "
                       "for the treating clinician to edit and sign.",
        "clinicalDecision": "Note content; the clinician authors and "
                            "signs, downstream nothing files itself.",
        "riskTierRationale": "Output proposes clinical text requiring "
                             "named-human disposition; it ranks nothing.",
        "populationScope": "Adult inpatient encounters. Excludes "
                           "paediatrics and behavioural health.",
        "claims": [{"id": "C1",
                    "text": "Every fact in the draft is traceable to "
                            "the input context."}],
        "evidence": [{"datasetKey": dataset_key, "runKey": run_key,
                      "mode": "recorded"}],
        "thresholds": dict(TIER_THRESHOLDS[tier]),
        "zeroToleranceClasses": [1, 2, 3, 5],
        "failureModes": "One case completed a section from boilerplate; "
                        "mitigated by instruction change and claim C1.",
        "knownLimitations": "Weak on multi-problem encounters.",
        "prohibitedUses": [{"code": code, "text": f"Prohibited: {code}"}
                           for code in PROHIBITED],
        "dispositionRule": "The treating clinician accepts, edits or "
                           "rejects; output is a draft; no clinical "
                           "write occurs from agent output.",
        "toolsDataAccess": "Tool opencare_api_call only; allow-list "
                           "/fhir; runs with the invoking user's token.",
        "monitoringPlan": "Monthly drift report and disposition review.",
        "changeControlPlan": "Framework §10 class table adopted as-is.",
        "incidentSuspension": "Owner or governance approver may "
                              "suspend; the kill switch covers this agent.",
    }
    fields.update(overrides)
    return fields


def _create_statement(client, key, tier="T2", owner="Dr Jane Owner",
                      headers=CLINICIAN):
    r = client.post("/ai-governance/statements",
                    json={"agentKey": key, "owner": owner,
                          "riskTier": tier, "fields": {}},
                    headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _approve_all(client, statement_id, tier):
    out = None
    for kind in TIER_APPROVALS[tier]:
        r = client.post(f"/ai-governance/statements/{statement_id}/approve",
                        json={"kind": kind}, headers=APPROVERS[kind])
        assert r.status_code == 200, r.text
        out = r.json()
    return out


def _approved_statement(client, key, tier="T2"):
    run_key = _run_evidence(client, key, f"ds-{key}")
    stmt = _create_statement(client, key, tier=tier)
    sid = stmt["statementId"]
    r = client.put(f"/ai-governance/statements/{sid}",
                   json={"fields": _full_fields(run_key, f"ds-{key}",
                                                tier=tier)},
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/ai-governance/statements/{sid}/submit", json={},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    approved = _approve_all(client, sid, tier)
    assert approved["status"] == "approved", approved
    return sid, approved


def _audit_actions(client):
    factory = client.app.state.session_factory
    with factory() as session:
        return [row.action for row in session.scalars(select(AuditLog)).all()]


# ---------------------------------------------------------------------------
# Tier requirements + create guards
# ---------------------------------------------------------------------------

def test_tier_requirements_endpoint(client):
    r = client.get("/ai-governance/tier-requirements", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] in ("published", "defaults")
    tiers = body["tiers"]
    assert tiers["T1"]["minOverallPass"] == 0.90
    assert tiers["T1"]["statementValidityMonths"] == 12
    assert tiers["T1"]["approvals"] == ["governance_approver"]
    assert tiers["T2"]["minOverallPass"] == 0.95
    assert tiers["T2"]["minSliceFloor"] == 0.90
    assert tiers["T2"]["maxSliceDisparity"] == 0.10
    assert tiers["T2"]["approvals"] == ["clinical_reviewer",
                                        "governance_approver"]
    assert tiers["T3"]["minOverallPass"] == 0.98
    assert tiers["T3"]["statementValidityMonths"] == 6
    assert tiers["T3"]["approvals"] == ["clinical_reviewer",
                                        "governance_approver",
                                        "clinical_owner"]


def test_create_statement_requires_published_agent(client):
    r = client.post("/ai-governance/statements",
                    json={"agentKey": "no-such-agent",
                          "owner": "Dr Jane Owner", "riskTier": "T2",
                          "fields": {}}, headers=CLINICIAN)
    assert r.status_code == 409, r.text


def test_create_statement_owner_must_be_named_individual(client):
    _publish_agent(client, "alias-agent")
    r = client.post("/ai-governance/statements",
                    json={"agentKey": "alias-agent",
                          "owner": "Hospital Medicine Team",
                          "riskTier": "T2", "fields": {}},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    assert "named individual" in r.text


def test_create_statement_tier_must_match_published_payload(client):
    _publish_agent(client, "tiered-agent", tier="T3")
    r = client.post("/ai-governance/statements",
                    json={"agentKey": "tiered-agent",
                          "owner": "Dr Jane Owner", "riskTier": "T2",
                          "fields": {}}, headers=CLINICIAN)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_statement_lifecycle_t2(client):
    _publish_agent(client, "lifecycle-agent", tier="T2")
    run_key = _run_evidence(client, "lifecycle-agent", "ds-lifecycle")
    stmt = _create_statement(client, "lifecycle-agent", tier="T2")
    sid = stmt["statementId"]
    assert sid.startswith("VS-lifecycle-agent-")
    assert stmt["status"] == "draft"
    assert stmt["agentConfigVersion"] == 1
    assert stmt["modelRoute"] == "ollama_chat/llama3.2:3b"
    assert stmt["fields"]["fundingSource"] == "internal R&D"

    # Submit while incomplete: 422 naming the missing §4 fields.
    r = client.post(f"/ai-governance/statements/{sid}/submit", json={},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    missing = " ".join(detail["missing"])
    for named in ("intendedUse", "claims", "evidence", "thresholds",
                  "prohibitedUses", "dispositionRule"):
        assert named in missing, detail

    # Complete the draft, submit, self-approve (refused + audited),
    # then approve with the two required kinds by two other actors.
    r = client.put(f"/ai-governance/statements/{sid}",
                   json={"fields": _full_fields(run_key, "ds-lifecycle")},
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/ai-governance/statements/{sid}/submit", json={},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"

    r = client.post(f"/ai-governance/statements/{sid}/approve",
                    json={"kind": "clinical_reviewer"}, headers=CLINICIAN)
    assert r.status_code == 403, r.text
    assert "governance.four_eyes.refused" in _audit_actions(client)

    # The config author is also four-eyes barred.
    r = client.post(f"/ai-governance/statements/{sid}/approve",
                    json={"kind": "clinical_reviewer"}, headers=OWNER)
    assert r.status_code == 403, r.text

    r = client.post(f"/ai-governance/statements/{sid}/approve",
                    json={"kind": "clinical_reviewer"}, headers=CONFIG)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"
    # Duplicate kind -> 409; same actor, second kind -> 409.
    r = client.post(f"/ai-governance/statements/{sid}/approve",
                    json={"kind": "clinical_reviewer"}, headers=ADMIN)
    assert r.status_code == 409, r.text
    r = client.post(f"/ai-governance/statements/{sid}/approve",
                    json={"kind": "governance_approver"}, headers=CONFIG)
    assert r.status_code == 409, r.text

    r = client.post(f"/ai-governance/statements/{sid}/approve",
                    json={"kind": "governance_approver"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["status"] == "approved"
    assert approved["currency"] == "current"
    approved_at = datetime.fromisoformat(approved["approvedAt"])
    review_by = datetime.fromisoformat(approved["reviewBy"])
    # T2 statement validity: 6 months.
    assert 170 <= (review_by - approved_at).days <= 190

    status = client.get("/ai-governance/agents/lifecycle-agent/status",
                        headers=ADMIN).json()
    assert status["statement"]["status"] == "current"
    assert status["runtimeGate"]["allowed"] is True


def test_statement_lifecycle_t1_review_by_twelve_months(client):
    _publish_agent(client, "info-agent", tier="T1")
    run_key = _run_evidence(client, "info-agent", "ds-info")
    stmt = _create_statement(client, "info-agent", tier="T1")
    sid = stmt["statementId"]
    r = client.put(f"/ai-governance/statements/{sid}",
                   json={"fields": _full_fields(run_key, "ds-info",
                                                tier="T1")},
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/ai-governance/statements/{sid}/submit", json={},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    approved = _approve_all(client, sid, "T1")
    approved_at = datetime.fromisoformat(approved["approvedAt"])
    review_by = datetime.fromisoformat(approved["reviewBy"])
    assert 350 <= (review_by - approved_at).days <= 380


# ---------------------------------------------------------------------------
# Evidence + threshold discipline at submit
# ---------------------------------------------------------------------------

def test_submit_refuses_run_for_wrong_config_version(client):
    _publish_agent(client, "versioned-agent", tier="T2")
    run_key = _run_evidence(client, "versioned-agent", "ds-versioned")
    _publish_agent(client, "versioned-agent", tier="T2")  # v2 published
    stmt = _create_statement(client, "versioned-agent", tier="T2")
    assert stmt["agentConfigVersion"] == 2
    sid = stmt["statementId"]
    r = client.put(f"/ai-governance/statements/{sid}",
                   json={"fields": _full_fields(run_key, "ds-versioned")},
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/ai-governance/statements/{sid}/submit", json={},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    invalid = " ".join(r.json()["detail"]["invalid"])
    assert run_key in invalid and "v1" in invalid, invalid


def test_submit_refuses_below_threshold_run(client):
    _publish_agent(client, "weak-agent", tier="T2")
    cases = [
        {"id": "c1", "prompt": "Summarise.",
         "recordedOutput": "DRAFT: summary.",
         "checks": [{"type": "contains", "value": "DRAFT"}]},
        {"id": "c2", "prompt": "Summarise.",
         "recordedOutput": "unrelated text",
         "checks": [{"type": "contains", "value": "DRAFT"}]},
    ]
    run_key = _run_evidence(client, "weak-agent", "ds-weak", cases=cases)
    stmt = _create_statement(client, "weak-agent", tier="T2")
    sid = stmt["statementId"]
    r = client.put(f"/ai-governance/statements/{sid}",
                   json={"fields": _full_fields(run_key, "ds-weak")},
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/ai-governance/statements/{sid}/submit", json={},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    invalid = " ".join(r.json()["detail"]["invalid"])
    assert run_key in invalid and "overall pass" in invalid, invalid


def test_threshold_loosening_requires_recorded_reason(client):
    _publish_agent(client, "loose-agent", tier="T2")
    run_key = _run_evidence(client, "loose-agent", "ds-loose")
    stmt = _create_statement(client, "loose-agent", tier="T2")
    sid = stmt["statementId"]
    loosened = {"overallPass": 0.80, "sliceFloor": 0.90,
                "disparityCeiling": 0.10}
    r = client.put(f"/ai-governance/statements/{sid}",
                   json={"fields": _full_fields(run_key, "ds-loose",
                                                thresholds=loosened)},
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/ai-governance/statements/{sid}/submit", json={},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    invalid = " ".join(r.json()["detail"]["invalid"])
    assert "thresholdLooseningReason" in invalid, invalid

    fields = _full_fields(
        run_key, "ds-loose", thresholds=loosened,
        thresholdLooseningReason="Recorded-mode harness evidence only; "
                                 "live run scheduled before enablement.")
    r = client.put(f"/ai-governance/statements/{sid}",
                   json={"fields": fields}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/ai-governance/statements/{sid}/submit", json={},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/ai-governance/statements/{sid}/approve",
                    json={"kind": "clinical_reviewer"}, headers=CONFIG)
    assert r.status_code == 200, r.text
    # Governance approver must re-state the loosening reason.
    r = client.post(f"/ai-governance/statements/{sid}/approve",
                    json={"kind": "governance_approver"}, headers=ADMIN)
    assert r.status_code == 422, r.text
    r = client.post(
        f"/ai-governance/statements/{sid}/approve",
        json={"kind": "governance_approver",
              "looseningReason": "Accepted: harness-proof evidence; "
                                 "live run required before enablement."},
        headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    approvals = {a["kind"]: a for a in r.json()["approvals"]}
    assert "live run required" in approvals["governance_approver"][
        "looseningReason"]


# ---------------------------------------------------------------------------
# Versioning, suspension, retirement, statement_status transitions
# ---------------------------------------------------------------------------

def test_new_version_supersedes_previous_on_approval(client):
    _publish_agent(client, "renewal-agent", tier="T2")
    sid, _ = _approved_statement(client, "renewal-agent", tier="T2")
    # A second version may open while v1 is approved; v1 stays
    # approved until v2 is approved.
    stmt2 = _create_statement(client, "renewal-agent", tier="T2")
    assert stmt2["statementId"] == sid
    assert stmt2["version"] == 2
    assert stmt2["status"] == "draft"
    # No parallel draft may open over the existing draft.
    r = client.post("/ai-governance/statements",
                    json={"agentKey": "renewal-agent",
                          "owner": "Dr Jane Owner", "riskTier": "T2",
                          "fields": {}}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "edit the existing draft" in r.text
    listing = client.get("/ai-governance/statements?agent=renewal-agent",
                         headers=ADMIN).json()
    by_version = {i["version"]: i for i in listing["items"]}
    assert by_version[1]["status"] == "approved"

    run_key = client.get("/agent-validation/runs?agent=renewal-agent",
                         headers=ADMIN).json()["items"][0]["runKey"]
    r = client.put(f"/ai-governance/statements/{sid}",
                   json={"fields": _full_fields(run_key, "ds-renewal-agent")},
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/ai-governance/statements/{sid}/submit", json={},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    _approve_all(client, sid, "T2")
    listing = client.get("/ai-governance/statements?agent=renewal-agent",
                         headers=ADMIN).json()
    by_version = {i["version"]: i for i in listing["items"]}
    assert by_version[1]["status"] == "superseded"
    assert by_version[2]["status"] == "approved"
    status = client.get("/ai-governance/agents/renewal-agent/status",
                        headers=ADMIN).json()
    assert status["statement"]["version"] == 2
    assert status["statement"]["status"] == "current"


def test_suspend_reinstate_retire(client):
    _publish_agent(client, "suspend-agent", tier="T2")
    sid, _ = _approved_statement(client, "suspend-agent", tier="T2")
    r = client.post(f"/ai-governance/statements/{sid}/suspend",
                    json={"reason": "Drift concern under review"},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "suspended"
    status = client.get("/ai-governance/agents/suspend-agent/status",
                        headers=ADMIN).json()
    assert status["statement"]["status"] == "suspended"
    assert status["runtimeGate"]["allowed"] is False
    # Reinstatement by the statement author is four-eyes refused.
    r = client.post(f"/ai-governance/statements/{sid}/reinstate", json={},
                    headers=CLINICIAN)
    assert r.status_code == 403, r.text
    # A fresh governance approval (another actor) reinstates it.
    r = client.post(f"/ai-governance/statements/{sid}/reinstate", json={},
                    headers=CONFIG)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    kinds = [a["kind"] for a in r.json()["approvals"]]
    assert kinds.count("governance_approver") >= 2  # original + reinstate
    # Retire is terminal for the version.
    r = client.post(f"/ai-governance/statements/{sid}/retire",
                    json={"note": "Agent withdrawn"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "retired"
    r = client.post(f"/ai-governance/statements/{sid}/suspend",
                    json={"reason": "again"}, headers=ADMIN)
    assert r.status_code == 409, r.text


def test_statement_status_stale_after_republication(client):
    _publish_agent(client, "stale-agent", tier="T2")
    _approved_statement(client, "stale-agent", tier="T2")
    _publish_agent(client, "stale-agent", tier="T2")  # v2
    status = client.get("/ai-governance/agents/stale-agent/status",
                        headers=ADMIN).json()
    assert status["statement"]["status"] == "stale"
    assert "covers config v1" in status["statement"]["reason"]
    # The runtime gate refuses; its reason names the FIRST failing
    # check (the v2 validation record is also missing after
    # republication — the statement check is ordered after it).
    assert status["runtimeGate"]["allowed"] is False


def test_statement_status_expired_when_review_by_passes(client):
    from datetime import timedelta

    from app.ai_governance import AiValidationStatement
    _publish_agent(client, "expiry-agent", tier="T2")
    _approved_statement(client, "expiry-agent", tier="T2")
    factory = client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(AiValidationStatement).where(
            AiValidationStatement.agent_key == "expiry-agent")).first()
        row.review_by = datetime.now(timezone.utc) - timedelta(days=1)
        session.commit()
    status = client.get("/ai-governance/agents/expiry-agent/status",
                        headers=ADMIN).json()
    assert status["statement"]["status"] == "expired"
    assert status["runtimeGate"]["allowed"] is False
    assert "expired" in status["runtimeGate"]["reason"]


def test_put_refused_on_non_draft(client):
    _publish_agent(client, "locked-agent", tier="T2")
    sid, _ = _approved_statement(client, "locked-agent", tier="T2")
    r = client.put(f"/ai-governance/statements/{sid}",
                   json={"fields": {}}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
