"""Phase 16, WS-C4 (BD-43) — digital-health innovation pipeline.

One synthetic capability end-to-end: intake -> triage -> pilot
approval -> unit-scoped pilot -> exercise in-scope patient (agent
transport substituted, app.agents._post_run — plumbing proof only, a
live model run is NOT RUN) -> exercise out-of-scope patient refused
403 -> evaluation aggregates the pilot audit stream -> scale decision
artefact + audited human hand-off record. Throughout, the module
NEVER globally enables the capability: the agent's enablement state
is unchanged (still not enabled) at the end.

A second test proves the pack exercise path is read-only surface
reporting (declared surface + installation state, nothing installed
or enabled), and a third proves the innovation.manage gate.

Synthetic data only.

C-AI registered adjustment: the pilot agent draft (authored by
ADMIN) is published by CONFIG (four-eyes publish gate), and the
pilot exercise path now consults the C-AI runtime gate — so the
agent carries a validation record + approved statement via the
shared tests.conftest.governance_for_enable flow before the pilot
exercises it. The pilot still never globally enables the agent;
every enablement assertion is unchanged.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import agents as agents_module
from app.models import AgentEnablement, AuditLog
from tests.conftest import (ADMIN, CLINICIAN, CONFIG, VIEWER,
                            governance_for_enable)

TODAY = datetime.now(timezone.utc).date()


def _publish(client, kind, key, payload, headers=ADMIN,
             publish_headers=None):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload,
                    headers=headers)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=publish_headers or headers)
    assert r.status_code == 200, r.text
    return version


def _grant_innovation_manage(client):
    """Grant innovation.manage exactly as the coordinator's central
    capability defaults will (admin/configurator/clinician), via the
    governed published permissions override — the established
    role-matrix pattern."""
    _publish(client, "permissions", "role-matrix", {"capabilities": {
        "innovation.manage": {"admin": True, "configurator": True,
                              "clinician": True, "viewer": False,
                              "patient": False}}})


def _agent_payload(**overrides):
    payload = {
        "description": "Pilot trial agent (synthetic)",
        "model": "ollama_chat/llama3.2:3b",
        "instructions": "Summarise the input briefly.",
        "tools": ["opencare_api_call"],
        "apiAllowlist": ["/fhir"],
        "modalities": {"text": True, "image": False, "audio": False},
    }
    payload.update(overrides)
    return payload


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Innovation", "given": ["Test"]}],
        "gender": "female", "birthDate": "1980-05-01",
    }, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _admit(client, patient, bed):
    _patient(client, patient)
    r = client.post("/inpatient/admissions",
                    json={"patient": patient, "bed": bed},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _audit_actions(client):
    factory = client.app.state.session_factory
    with factory() as session:
        return [row.action for row in session.scalars(select(AuditLog)).all()]


def _agent_enabled(client, key):
    factory = client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(AgentEnablement).where(
            AgentEnablement.agent_key == key)).first()
        return bool(row and row.enabled)


def _lifecycle_to_pilot(client, capability_type, capability_key,
                        scope_extra):
    """intake -> triage -> approve-pilot -> pilot. Returns
    (intake_id, pilot_id)."""
    r = client.post("/innovation/intakes", json={
        "title": "Trial capability", "description": "Synthetic pilot.",
        "capabilityType": capability_type, "capabilityKey": capability_key,
        "proposer": "innovation-board",
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    intake = r.json()
    assert intake["status"] == "proposed"
    intake_id = intake["id"]

    r = client.post(f"/innovation/intakes/{intake_id}/triage",
                    json={"notes": "Fits the unit's workflow; low risk."},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "triaged"
    assert r.json()["triage_notes"]

    r = client.post(f"/innovation/intakes/{intake_id}/approve-pilot",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pilot-approved"

    body = {"intakeId": intake_id,
            "startsOn": (TODAY - timedelta(days=1)).isoformat(),
            "endsOn": (TODAY + timedelta(days=14)).isoformat()}
    body.update(scope_extra)
    r = client.post("/innovation/pilots", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    pilot = r.json()
    assert pilot["status"] == "piloting"
    return intake_id, pilot["id"]


# ---------------------------------------------------------------------------
# End-to-end: one agent capability, unit-scoped pilot
# ---------------------------------------------------------------------------

def test_innovation_pipeline_agent_end_to_end(client, monkeypatch):
    _grant_innovation_manage(client)
    # The capability under trial: a PUBLISHED agent that is NOT
    # enabled — a pilot must not need, or perform, global enablement.
    _publish(client, "agent", "pilot-summariser", _agent_payload(),
             publish_headers=CONFIG)  # C-AI four-eyes publish
    # C-AI runtime gate: record + approved statement, WITHOUT global
    # enablement (the pilot's trial authorisation is unchanged).
    governance_for_enable(client, "pilot-summariser")
    assert _agent_enabled(client, "pilot-summariser") is False

    # Synthetic scope: pat-in admitted to unit-medsurg (bed MS-101A),
    # pat-out admitted to unit-icu (bed ICU-201).
    _admit(client, "pat-in", "MS-101A")
    _admit(client, "pat-out", "ICU-201")

    intake_id, pilot_id = _lifecycle_to_pilot(
        client, "agent", "pilot-summariser", {"unit": "unit-medsurg"})

    def _ok(payload):
        return 200, {"output": "DRAFT: pilot summary.",
                     "model": "ollama_chat/llama3.2:3b",
                     "agent": "pilot-summariser"}

    monkeypatch.setattr(agents_module, "_post_run", _ok)

    # In-scope patient: the exercise succeeds, in trial scope only.
    r = client.post(f"/innovation/pilots/{pilot_id}/exercise", json={
        "patientRef": "Patient/pat-in",
        "input": "Summarise the pilot patient's chart",
    }, headers=ADMIN)
    assert r.status_code == 200, r.text
    exercised = r.json()
    assert exercised["exercised"] is True
    assert exercised["scope_basis"].startswith("unit:unit-medsurg")
    run_id = exercised["run"]["run_id"]
    assert exercised["run"]["output"] == "DRAFT: pilot summary."
    assert exercised["globally_enabled"] is False
    assert exercised["global_enablement_changed"] is False

    # Out-of-scope patient: refused 403 with the reason (unit named).
    r = client.post(f"/innovation/pilots/{pilot_id}/exercise", json={
        "patientRef": "Patient/pat-out",
        "input": "Summarise the other unit's patient",
    }, headers=ADMIN)
    assert r.status_code == 403, r.text
    assert "outside this pilot's trial scope" in r.json()["detail"]
    assert "unit-icu" in r.json()["detail"]

    # A patient with no active encounter at all: also 403 — belonging
    # cannot be established.
    _patient(client, "pat-none")
    r = client.post(f"/innovation/pilots/{pilot_id}/exercise", json={
        "patientRef": "Patient/pat-none", "input": "Summarise",
    }, headers=ADMIN)
    assert r.status_code == 403, r.text

    # The human dispositions the pilot run's draft (agents flow).
    d = client.post(f"/agents/runs/{run_id}/disposition",
                    json={"disposition": "accepted"}, headers=CLINICIAN)
    assert d.status_code == 200, d.text

    # Evaluation aggregates the pilot's audit stream.
    r = client.get(f"/innovation/pilots/{pilot_id}/evaluation",
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    evaluation = r.json()
    assert evaluation["exercises"]["succeeded"] == 1
    assert evaluation["exercises"]["refused"] == 2
    assert evaluation["exercises"]["patients"] == ["Patient/pat-in"]
    assert evaluation["runs"]["total"] == 1
    assert evaluation["runs"]["dispositions"] == {"accepted": 1}
    assert evaluation["outcome"] is None

    # Governed outcome payload, then the decision artefact.
    r = client.post(f"/innovation/pilots/{pilot_id}/evaluation", json={
        "summary": "Summaries accepted by the pilot unit.",
        "metrics": {"exercises": 1, "accepted": 1},
    }, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["outcome"]["summary"].startswith("Summaries accepted")
    assert r.json()["pilot"]["status"] == "evaluated"

    r = client.post(f"/innovation/pilots/{pilot_id}/decision", json={
        "decision": "scale",
        "rationale": "Pilot outcome met the unit's bar.",
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    decision = r.json()
    assert decision["decision"] == "scale"
    assert decision["decided_by"] == "dev-admin"
    assert decision["pilot_status"] == "decided"
    handoff = decision["handoff"]
    assert handoff is not None
    assert handoff["status"] == "pending-human-action"
    assert "pilot-summariser" in handoff["required_step"]
    assert "enable" in handoff["required_step"]

    # The decision is visible on the pilot + evaluation artefacts.
    pilot = client.get(f"/innovation/pilots/{pilot_id}",
                       headers=ADMIN).json()
    assert pilot["status"] == "decided"
    assert [d["decision"] for d in pilot["decisions"]] == ["scale"]
    assert len(pilot["handoffs"]) == 1
    intake = client.get(f"/innovation/intakes/{intake_id}",
                        headers=ADMIN).json()
    assert intake["status"] == "decided"

    # CRITICAL: the module never globally enabled the agent — its
    # enablement state is exactly as before the pilot (not enabled).
    assert _agent_enabled(client, "pilot-summariser") is False
    roster = client.get("/agents", headers=CLINICIAN).json()
    by_key = {a["key"]: a for a in roster["agents"]}
    assert by_key["pilot-summariser"]["enabled"] is False

    actions = _audit_actions(client)
    for expected in ("innovation.intake.create", "innovation.intake.triage",
                     "innovation.pilot.create", "innovation.pilot.exercise",
                     "innovation.pilot.exercise.refused",
                     "innovation.pilot.evaluate",
                     "innovation.pilot.decision",
                     "innovation.handoff.created"):
        assert expected in actions, expected
    assert "agents.enable" not in actions


# ---------------------------------------------------------------------------
# Pack exercise: declared surface + installation state, read-only
# ---------------------------------------------------------------------------

def test_innovation_pack_exercise_reports_surface_without_enabling(client):
    _grant_innovation_manage(client)
    r = client.post("/studio/packs", json={
        "name": "pilot-probe-pack", "version": "1.0.0",
        "manifest": {
            "name": "pilot-probe-pack", "version": "1.0.0",
            "description": "Synthetic pilot pack.",
            "api_endpoints": [
                {"method": "GET", "path": "/pilot-probe/status",
                 "enabled": False}],
            "modules": ["pilot-probe"],
        }}, headers=ADMIN)
    assert r.status_code == 201, r.text

    _intake_id, pilot_id = _lifecycle_to_pilot(
        client, "pack", "pilot-probe-pack", {"unit": "unit-medsurg"})

    r = client.post(f"/innovation/pilots/{pilot_id}/exercise",
                    json={"input": "report surface"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    exercised = r.json()
    assert exercised["exercised"] is True
    assert exercised["installation_state"] == "not-installed"
    assert exercised["installations"] == []
    assert exercised["declared_surface"]["api_endpoints"][0]["path"] == \
        "/pilot-probe/status"
    assert exercised["global_enablement_changed"] is False

    # Nothing of the pilot pack was installed (let alone enabled)
    # by the exercise. Phase 17 adjustment: a fresh app now carries
    # the three seeded core installations (pack-gate default-ON
    # rollout), so the assertion is scoped to the pilot pack — the
    # invariant this test owns — instead of global emptiness.
    installations = client.get("/studio/installations",
                               headers=ADMIN).json()
    assert [i for i in installations
            if i["pack_name"] == "pilot-probe-pack"] == []


# ---------------------------------------------------------------------------
# End-to-end: one PACK capability, COHORT-scoped pilot (Phase 17,
# WS-RESID — the BD-43 test-proof the Phase 16 SQA left named as
# "implemented, not test-proven"). The cohort path needs warehouse
# members, so this test builds them the Phase 9 way: FHIR patients +
# conditions, then the real ETL run into a per-test warehouse.
# Scoping is asserted to BIND three ways: (1) exercise with an
# in-cohort patient succeeds in trial scope; (2) exercise with an
# out-of-cohort patient is refused 403 and the refusal is audited;
# (3) the pack's declared endpoint stays refused 403 by the BD-01
# pack gate (app.pack_gate, ON by default) before AND after the
# scale decision — the capability is reachable ONLY inside the
# pilot's cohort scope, and the scale decision produces the audited
# hand-off artefact without enabling anything.
# ---------------------------------------------------------------------------

SNOMED = "http://snomed.info/sct"
T2DM = "44054006"          # the seeded `diabetes` cohort's condition
HYPERTENSION = "38341003"  # a different cohort's condition


def _condition(client, pid, code):
    r = client.post("/fhir/Condition", json={
        "resourceType": "Condition",
        "code": {"coding": [{"system": SNOMED, "code": code}]},
        "subject": {"reference": f"Patient/{pid}"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def test_innovation_pipeline_pack_cohort_scoped_end_to_end(
        client, monkeypatch, tmp_path):
    monkeypatch.setenv("WAREHOUSE_DATABASE_URL",
                       f"sqlite:///{tmp_path}/wh.db")
    _grant_innovation_manage(client)

    # Warehouse cohort members: pat-cohort-in carries the diabetes
    # cohort's condition; pat-cohort-out carries hypertension only.
    _patient(client, "pat-cohort-in")
    _condition(client, "pat-cohort-in", T2DM)
    _patient(client, "pat-cohort-out")
    _condition(client, "pat-cohort-out", HYPERTENSION)
    r = client.post("/warehouse/etl/run", headers=ADMIN)
    assert r.status_code == 200 and r.json()["dead_letter"] == 0, r.text

    # The capability under trial: a pack whose declared endpoint is
    # disabled in its manifest and which is never installed.
    r = client.post("/studio/packs", json={
        "name": "cohort-probe-pack", "version": "1.0.0",
        "manifest": {
            "name": "cohort-probe-pack", "version": "1.0.0",
            "description": "Synthetic cohort pilot pack.",
            "api_endpoints": [
                {"method": "GET", "path": "/cohort-probe/status",
                 "enabled": False}],
            "modules": ["cohort-probe"],
        }}, headers=ADMIN)
    assert r.status_code == 201, r.text

    # BD-01 pack gate: the declared endpoint is refused while no
    # installation of the pack is enabled — before the pilot exists.
    r = client.get("/cohort-probe/status", headers=ADMIN)
    assert r.status_code == 403, r.text
    assert "Pack-enablement gating" in r.json()["detail"]
    assert "cohort-probe-pack" in r.json()["detail"]

    intake_id, pilot_id = _lifecycle_to_pilot(
        client, "pack", "cohort-probe-pack", {"cohortKey": "diabetes"})
    pilot = client.get(f"/innovation/pilots/{pilot_id}",
                       headers=ADMIN).json()
    assert pilot["scope"] == {"cohortKey": "diabetes"}

    # In-cohort patient: the exercise succeeds IN TRIAL SCOPE ONLY.
    r = client.post(f"/innovation/pilots/{pilot_id}/exercise", json={
        "patientRef": "Patient/pat-cohort-in",
        "input": "report surface",
    }, headers=ADMIN)
    assert r.status_code == 200, r.text
    exercised = r.json()
    assert exercised["exercised"] is True
    assert exercised["scope_basis"].startswith("cohort:diabetes")
    assert exercised["installation_state"] == "not-installed"
    assert exercised["global_enablement_changed"] is False

    # Out-of-cohort patient: refused 403, naming the cohort basis.
    r = client.post(f"/innovation/pilots/{pilot_id}/exercise", json={
        "patientRef": "Patient/pat-cohort-out",
        "input": "report surface",
    }, headers=ADMIN)
    assert r.status_code == 403, r.text
    assert "outside this pilot's trial scope" in r.json()["detail"]
    assert "not a member of cohort 'diabetes'" in r.json()["detail"]

    # Evaluation aggregates the cohort pilot's audit stream.
    r = client.get(f"/innovation/pilots/{pilot_id}/evaluation",
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    evaluation = r.json()
    assert evaluation["exercises"]["succeeded"] == 1
    assert evaluation["exercises"]["refused"] == 1
    assert evaluation["exercises"]["patients"] == ["Patient/pat-cohort-in"]
    assert evaluation["exercises"]["refusals"][0]["patient"] == \
        "Patient/pat-cohort-out"

    # Governed outcome, then the scale decision artefact.
    r = client.post(f"/innovation/pilots/{pilot_id}/evaluation", json={
        "summary": "Cohort pilot exercised cleanly in scope.",
        "metrics": {"exercises": 1, "refusals": 1},
    }, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["pilot"]["status"] == "evaluated"

    r = client.post(f"/innovation/pilots/{pilot_id}/decision", json={
        "decision": "scale",
        "rationale": "Cohort-scoped pilot met its outcome bar.",
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    decision = r.json()
    assert decision["decision"] == "scale"
    assert decision["pilot_status"] == "decided"
    handoff = decision["handoff"]
    assert handoff is not None
    assert handoff["status"] == "pending-human-action"
    assert handoff["capability_key"] == "cohort-probe-pack"
    assert "cohort-probe-pack" in handoff["required_step"]

    # The decision artefact is produced on the pilot + intake, and
    # the whole flow is in the hash-chained audit stream.
    pilot = client.get(f"/innovation/pilots/{pilot_id}",
                       headers=ADMIN).json()
    assert pilot["status"] == "decided"
    assert [d["decision"] for d in pilot["decisions"]] == ["scale"]
    assert len(pilot["handoffs"]) == 1
    intake = client.get(f"/innovation/intakes/{intake_id}",
                        headers=ADMIN).json()
    assert intake["status"] == "decided"
    actions = _audit_actions(client)
    for expected in ("innovation.intake.create",
                     "innovation.pilot.create",
                     "innovation.pilot.exercise",
                     "innovation.pilot.exercise.refused",
                     "innovation.pilot.evaluate",
                     "innovation.pilot.decision",
                     "innovation.handoff.created"):
        assert expected in actions, expected

    # The scale decision enabled NOTHING: the pilot pack is still
    # not installed (the only installations are the three seeded
    # core ones from the pack-gate default-ON rollout), and its
    # declared endpoint is still refused by the BD-01 pack gate —
    # reachability existed only inside the cohort-scoped pilot
    # exercise above.
    installations = client.get("/studio/installations",
                               headers=ADMIN).json()
    assert [i for i in installations
            if i["pack_name"] == "cohort-probe-pack"] == []
    r = client.get("/cohort-probe/status", headers=ADMIN)
    assert r.status_code == 403, r.text
    assert "Pack-enablement gating" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Capability gate
# ---------------------------------------------------------------------------

def test_innovation_writes_require_innovation_manage(client):
    _grant_innovation_manage(client)
    r = client.post("/innovation/intakes", json={
        "title": "Nope", "capabilityType": "agent",
        "capabilityKey": "pilot-summariser",
    }, headers=VIEWER)
    assert r.status_code == 403, r.text
    r = client.get("/innovation/intakes", headers=VIEWER)
    assert r.status_code == 403, r.text
