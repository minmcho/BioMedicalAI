import pytest
from fastapi.testclient import TestClient

from app.main import create_app

ADMIN = {"Authorization": "Bearer admin-token"}
CONFIG = {"Authorization": "Bearer config-token"}
CLINICIAN = {"Authorization": "Bearer clinician-token"}
VIEWER = {"Authorization": "Bearer viewer-token"}
# Dev patient user is linked to Patient/pat-1 (see app.security DEFAULT_DEV_TOKENS).
PATIENT = {"Authorization": "Bearer patient-token"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# C-AI governance helper (shared by the agent-related suites)
# ---------------------------------------------------------------------------

# The five §4.5 prohibited-use seed codes every statement must carry.
_PROHIBITED_CODES = (
    "no-autonomous-diagnosis",
    "no-unreviewed-order",
    "no-unreviewed-patient-communication",
    "no-excluded-population",
    "no-unlisted-route-tool-modality",
)

# Framework §3.2 tier thresholds, mirrored as statement field 11.
_TIER_THRESHOLDS = {
    "T1": {"overallPass": 0.90, "sliceFloor": 0.85, "disparityCeiling": 0.20},
    "T2": {"overallPass": 0.95, "sliceFloor": 0.90, "disparityCeiling": 0.10},
    "T3": {"overallPass": 0.98, "sliceFloor": 0.95, "disparityCeiling": 0.05},
}

# Approval kinds per tier (framework §3.2) in approval order.
_TIER_APPROVALS = {
    "T1": ("governance_approver",),
    "T2": ("clinical_reviewer", "governance_approver"),
    "T3": ("clinical_reviewer", "governance_approver", "clinical_owner"),
}


def _statement_fields(tier, dataset_key, run_key):
    """A minimal VALID §4.1 fields dict for the governance helper:
    every field the submit-time completeness check requires, with
    the tier's own §3.2 thresholds (tightening-free, loosening-free)
    and the recorded-mode evidence run the helper just minted —
    labelled recorded, never upgraded."""
    text = "Recorded for the C-AI test governance flow (synthetic data)."
    return {
        "intendedUse": text,
        "clinicalDecision": text,
        "riskTierRationale": text,
        "populationScope": text,
        "claims": [{"text": "Every output is a draft for human review."}],
        "evidence": [{"datasetKey": dataset_key, "runKey": run_key,
                      "mode": "recorded"}],
        "thresholds": dict(_TIER_THRESHOLDS[tier]),
        "zeroToleranceClasses": [1, 2, 3, 4, 5],
        "failureModes": text,
        "knownLimitations": text,
        "prohibitedUses": [{"code": code} for code in _PROHIBITED_CODES],
        "dispositionRule": text,
        "toolsDataAccess": text,
        "monitoringPlan": text,
        "changeControlPlan": text,
        "incidentSuspension": text,
    }


def _grant_viewer_validate(client, author):
    """Publish the permissions override that lets the viewer role
    hold `agents.validate` (see governance_for_enable's T3 note).
    Merges over any already-published role matrix so other cells a
    test published survive."""
    capabilities = {}
    r = client.get("/studio/config/permissions/effective/role-matrix",
                   headers=author)
    if r.status_code == 200:
        capabilities = dict(
            (r.json().get("payload") or {}).get("capabilities") or {})
    cells = dict(capabilities.get("agents.validate") or {})
    if cells.get("viewer") is True:
        return
    cells["viewer"] = True
    capabilities["agents.validate"] = cells
    r = client.post("/studio/config/permissions/role-matrix",
                    json={"capabilities": capabilities}, headers=author)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/permissions/role-matrix/versions/{version}"
        "/publish", headers=author)
    assert r.status_code == 200, r.text


def governance_for_enable(client, key, *, author=ADMIN, approver=CONFIG,
                          second=CLINICIAN):
    """The genuine C-AI enablement-governance flow for one PUBLISHED
    agent (the caller publishes first — draft by `author`, publish by
    a second actor, per the four-eyes choreography):

    1. a recorded-mode dataset run minting a current ValidationRecord
       if none is current (the test_phase10_agents._validate_for_enable
       pattern);
    2. a validation statement authored by `author`, submitted, and
       approved with the tier's required approval kinds — the
       governance approval by `approver`, the clinical review by
       `second` (four-eyes: neither may be the statement author or
       the agent config's author).

    T3 note: the tier also requires a clinical_owner approval from a
    THIRD distinct validate-capable identity, and the dev tokens
    provide only two non-author ones (configurator + clinician). The
    helper therefore first publishes a governed permissions override
    (kind=permissions, key=role-matrix — merged over any existing
    published matrix) granting the viewer role `agents.validate` for
    this test database, and the clinical_owner approval is recorded
    by VIEWER. This is the system's own governed mechanism for
    widening a capability, exercised end-to-end.

    Returns {"tier", "runKey", "statementId", "statementStatus"}."""
    # 1 — validation record (mint only when not already current).
    status = client.get(f"/agent-validation/agents/{key}/status",
                        headers=author)
    assert status.status_code == 200, status.text
    run_key = dataset_key = None
    if status.json()["status"] != "current":
        r = client.post("/agent-validation/datasets", json={
            "key": f"enable-{key}",
            "name": f"Enablement validation for {key}",
            "cases": [{"id": "c1", "prompt": "Summarise the input briefly.",
                       "recordedOutput": "DRAFT: brief summary.",
                       "checks": [{"type": "contains", "value": "DRAFT"}]}]},
            headers=author)
        assert r.status_code in (201, 409), r.text
        r = client.post(f"/agent-validation/datasets/enable-{key}/run",
                        json={"agentKey": key, "mode": "recorded"},
                        headers=author)
        assert r.status_code == 201, r.text
        run_key, dataset_key = r.json()["runKey"], r.json()["datasetKey"]
    else:
        runs = client.get("/agent-validation/runs",
                          params={"agent": key}, headers=author)
        assert runs.status_code == 200, runs.text
        latest = runs.json()["items"][0]
        run_key, dataset_key = latest["runKey"], latest["datasetKey"]

    # 2 — statement (skip when one is already current).
    gov = client.get(f"/ai-governance/agents/{key}/status", headers=author)
    assert gov.status_code == 200, gov.text
    tier = gov.json()["tier"]
    if gov.json()["statement"]["status"] == "current":
        return {"tier": tier, "runKey": run_key,
                "statementId": gov.json()["statement"]["statementId"],
                "statementStatus": "current"}
    r = client.post("/ai-governance/statements", json={
        "agentKey": key, "owner": "Governance Owner", "riskTier": tier,
        "fields": _statement_fields(tier, dataset_key, run_key)},
        headers=author)
    assert r.status_code == 201, r.text
    statement_id = r.json()["statementId"]
    r = client.post(f"/ai-governance/statements/{statement_id}/submit",
                    json={}, headers=author)
    assert r.status_code == 200, r.text
    if tier == "T3":
        _grant_viewer_validate(client, author)
    kind_actor = {"clinical_reviewer": second,
                  "governance_approver": approver,
                  "clinical_owner": VIEWER}
    for kind in _TIER_APPROVALS[tier]:
        r = client.post(
            f"/ai-governance/statements/{statement_id}/approve",
            json={"kind": kind}, headers=kind_actor[kind])
        assert r.status_code == 200, r.text
    gov = client.get(f"/ai-governance/agents/{key}/status", headers=author)
    return {"tier": tier, "runKey": run_key, "statementId": statement_id,
            "statementStatus": gov.json()["statement"]["status"]}
