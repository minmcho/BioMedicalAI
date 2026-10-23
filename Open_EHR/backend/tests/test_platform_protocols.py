"""Platform pass, Workstream 6 — SOP / protocol workflow framework.

End-to-end proof with the LABORATORY module: a real governed SOP
definition ("lab-critical-result-callback") is drafted, published and
executed against a synthetic lab order reference, exercising per-step
role enforcement, checkpoint forms, ordered advancement, timers and
escalations — all computed at read, no daemon. Synthetic data only.

Test composition follows the workstream brief: the app is built with
create_app, the protocols router is included explicitly, and the
module's tables are created afterwards (create_all is idempotent for
the core tables). Dev tokens: admin-token / config-token
(configurator) / clinician-token / viewer-token (app.security).
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import protocols
from app.db import Base
from app.main import create_app
from app.models import AuditLog
from app.protocols import ProtocolInstance

ADMIN = {"Authorization": "Bearer admin-token"}
CONFIG = {"Authorization": "Bearer config-token"}
CLINICIAN = {"Authorization": "Bearer clinician-token"}
VIEWER = {"Authorization": "Bearer viewer-token"}

KEY = "lab-critical-result-callback"

# The proof SOP: critical-result callback for the laboratory.
# Steps 1-3 are the clinician callback chain from the brief; step 4
# ("verify-closure") is deliberately role=configurator so the suite
# can prove the PER-STEP ROLE check itself: a clinician holds
# fhir.write (so the capability gate passes) yet must still get 403
# on a step that is not theirs.
SOP = {
    "name": "lab-critical-result-callback",
    "module": "laboratory",
    "title": "Critical result callback",
    "steps": [
        {"key": "notify", "title": "Notify the responsible clinician",
         "role": "clinician", "timer_minutes": 30,
         "checkpoint": {"formKey": "critical-result-readback"},
         "instructions": "Phone the result; do not message-and-wait."},
        {"key": "readback", "title": "Receive read-back",
         "role": "clinician",
         "checkpoint": {"formKey": "critical-result-readback"}},
        {"key": "document", "title": "Document the callback",
         "role": "clinician"},
        {"key": "verify-closure", "title": "Verify loop closure (QA)",
         "role": "configurator"},
    ],
    "escalations": [
        {"after_minutes": 45, "to_role": "admin", "action": "page-on-call"},
    ],
}

READBACK = {"form_submission": {"read_back_by": "Dr Synthetic",
                                "value_confirmed": True}}


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db")
    app.include_router(protocols.router)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as c:
        yield c


def _session(client):
    return client.app.state.session_factory()


def _publish_sop(client, payload=None):
    r = client.post(f"/protocols/definitions/{KEY}", json=payload or SOP,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/protocols/definitions/{KEY}/versions/{version}/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    return version


def _start(client, module_ref="lab-order:synthetic-1", headers=None):
    r = client.post(f"/protocols/{KEY}/instances",
                    json={"module_ref": module_ref,
                          "subject_ref": "Patient/pat-1"},
                    headers=headers or CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _audit_actions(client):
    with _session(client) as session:
        return [row.action for row in session.scalars(select(AuditLog)).all()]


def _backdate_current_step(client, instance_id, minutes):
    with _session(client) as session:
        instance = session.get(ProtocolInstance, instance_id)
        past = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        instance.current_step_started_at = past
        for record in instance_records(session, instance_id):
            if record.step_key == instance.current_step_key:
                record.started_at = past
        session.commit()


def instance_records(session, instance_id):
    from app.protocols import ProtocolStepRecord
    return list(session.scalars(select(ProtocolStepRecord).where(
        ProtocolStepRecord.instance_id == instance_id)).all())


# ---------------------------------------------------------------------------
# Definition validation + governance
# ---------------------------------------------------------------------------

def test_validation_rejects_bad_definitions(client):
    base = {"name": "x", "module": "laboratory",
            "steps": [{"key": "a", "title": "A", "role": "clinician"}]}
    assert client.post("/protocols/definitions/bad", json=base,
                       headers=ADMIN).status_code == 201  # sanity: base valid
    cases = [
        {**base, "name": ""},
        {**base, "module": ""},
        {**base, "steps": []},
        {**base, "steps": [{"key": "a", "title": "A", "role": "clinician"},
                           {"key": "a", "title": "B", "role": "clinician"}]},
        {**base, "steps": [{"key": "a", "title": "A", "role": "wizard"}]},
        {**base, "steps": [{"key": "a", "title": "A", "role": "clinician",
                            "timer_minutes": 0}]},
        {**base, "steps": [{"key": "a", "title": "A", "role": "clinician",
                            "timer_minutes": -5}]},
        {**base, "steps": [{"key": "a", "title": "A", "role": "clinician",
                            "checkpoint": {"formKey": ""}}]},
        {**base, "escalations": [{"after_minutes": 0, "to_role": "admin",
                                   "action": "page"}]},
        {**base, "escalations": [{"after_minutes": 10, "to_role": "wizard",
                                   "action": "page"}]},
        {**base, "escalations": [{"after_minutes": 10, "to_role": "admin",
                                   "action": ""}]},
    ]
    for i, payload in enumerate(cases):
        r = client.post(f"/protocols/definitions/bad-{i}", json=payload,
                        headers=ADMIN)
        assert r.status_code == 422, (i, r.text)


def test_definition_writes_require_studio_manage(client):
    assert client.post(f"/protocols/definitions/{KEY}", json=SOP,
                       headers=CLINICIAN).status_code == 403
    assert client.post(f"/protocols/definitions/{KEY}", json=SOP,
                       headers=VIEWER).status_code == 403
    r = client.post(f"/protocols/definitions/{KEY}", json=SOP, headers=CONFIG)
    assert r.status_code == 201, r.text
    # Clinician may READ the published definition but not drafts.
    assert client.get("/protocols/definitions", headers=CLINICIAN).json() == []
    version = r.json()["version"]
    assert client.post(
        f"/protocols/definitions/{KEY}/versions/{version}/publish",
        headers=CLINICIAN).status_code == 403
    assert client.post(
        f"/protocols/definitions/{KEY}/versions/{version}/publish",
        headers=CONFIG).status_code == 200
    published = client.get(f"/protocols/definitions/{KEY}/published",
                           headers=CLINICIAN)
    assert published.status_code == 200
    assert published.json()["payload"]["module"] == "laboratory"


def test_publish_supersedes_prior_and_audits(client):
    v1 = _publish_sop(client)
    revised = {**SOP, "title": "Critical result callback v2"}
    r = client.post(f"/protocols/definitions/{KEY}", json=revised, headers=ADMIN)
    assert r.status_code == 201, r.text
    v2 = r.json()["version"]
    assert v2 == v1 + 1
    # A second concurrent draft is refused.
    assert client.post(f"/protocols/definitions/{KEY}", json=revised,
                       headers=ADMIN).status_code == 409
    assert client.post(
        f"/protocols/definitions/{KEY}/versions/{v2}/publish",
        headers=ADMIN).status_code == 200
    history = client.get(f"/protocols/definitions/{KEY}", headers=ADMIN).json()
    by_version = {row["version"]: row["status"] for row in history}
    assert by_version[v1] == "superseded"
    assert by_version[v2] == "published"
    actions = _audit_actions(client)
    assert actions.count("protocols.definition.create") == 2
    assert actions.count("protocols.definition.publish") == 2


# ---------------------------------------------------------------------------
# Instance lifecycle
# ---------------------------------------------------------------------------

def test_start_requires_published_and_fhir_write(client):
    r = client.post(f"/protocols/{KEY}/instances",
                    json={"module_ref": "lab-order:x",
                          "subject_ref": "Patient/pat-1"}, headers=CLINICIAN)
    assert r.status_code == 404  # nothing published yet
    _publish_sop(client)
    # Viewer lacks fhir.write: the capability gate itself refuses.
    r = client.post(f"/protocols/{KEY}/instances",
                    json={"module_ref": "lab-order:x",
                          "subject_ref": "Patient/pat-1"}, headers=VIEWER)
    assert r.status_code == 403


def test_duplicate_active_instance_conflicts(client):
    _publish_sop(client)
    _start(client)
    r = client.post(f"/protocols/{KEY}/instances",
                    json={"module_ref": "lab-order:synthetic-1",
                          "subject_ref": "Patient/pat-1"}, headers=CLINICIAN)
    assert r.status_code == 409
    # A different module_ref is a different piece of work: allowed.
    _start(client, module_ref="lab-order:synthetic-2")


def test_advance_out_of_order_conflicts(client):
    _publish_sop(client)
    inst = _start(client)
    r = client.post(f"/protocols/instances/{inst['id']}/advance",
                    json={"step_key": "readback", **READBACK}, headers=CLINICIAN)
    assert r.status_code == 409


def test_advance_missing_checkpoint_is_422(client):
    _publish_sop(client)
    inst = _start(client)
    r = client.post(f"/protocols/instances/{inst['id']}/advance",
                    json={"step_key": "notify"}, headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post(f"/protocols/instances/{inst['id']}/advance",
                    json={"step_key": "notify", "form_submission": {}},
                    headers=CLINICIAN)
    assert r.status_code == 422


def test_advance_wrong_step_role_is_403_proving_role_check(client):
    """The clinician passes the fhir.write capability gate but the
    'verify-closure' step belongs to configurator: 403 from the
    per-step role check, and the configurator can then perform it."""
    _publish_sop(client)
    inst = _start(client)
    iid = inst["id"]
    assert client.post(f"/protocols/instances/{iid}/advance",
                       json={"step_key": "notify", **READBACK},
                       headers=CLINICIAN).status_code == 200
    assert client.post(f"/protocols/instances/{iid}/advance",
                       json={"step_key": "readback", **READBACK},
                       headers=CLINICIAN).status_code == 200
    assert client.post(f"/protocols/instances/{iid}/advance",
                       json={"step_key": "document"},
                       headers=CLINICIAN).status_code == 200
    r = client.post(f"/protocols/instances/{iid}/advance",
                    json={"step_key": "verify-closure"}, headers=CLINICIAN)
    assert r.status_code == 403, r.text
    assert "configurator" in r.json()["detail"]
    # The step's own role performs it (configurator holds fhir.write).
    r = client.post(f"/protocols/instances/{iid}/advance",
                    json={"step_key": "verify-closure"}, headers=CONFIG)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"


def test_admin_may_advance_any_step(client):
    _publish_sop(client)
    inst = _start(client)
    r = client.post(f"/protocols/instances/{inst['id']}/advance",
                    json={"step_key": "notify", **READBACK}, headers=ADMIN)
    assert r.status_code == 200, r.text


def test_happy_path_completes_with_audit_chain(client):
    _publish_sop(client)
    inst = _start(client)
    iid = inst["id"]
    assert inst["current_step_key"] == "notify"
    assert inst["protocol_version"] == 1
    assert [s["status"] for s in inst["steps"]] == ["pending"] * 4
    # Timer posture is live on the notify step, not yet breached.
    posture = inst["timer_posture"]
    assert posture["step_key"] == "notify"
    assert posture["timer_minutes"] == 30
    assert posture["breached"] is False
    assert posture["escalation"] is None
    assert posture["due_at"] is not None

    r = client.post(f"/protocols/instances/{iid}/advance",
                    json={"step_key": "notify", "note": "phoned",
                          **READBACK}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["current_step_key"] == "readback"
    done = r.json()["steps"][0]
    assert done["status"] == "done" and done["acted_by"] == "dev-clinician"
    assert done["note"] == "phoned"
    assert client.post(f"/protocols/instances/{iid}/advance",
                       json={"step_key": "readback", **READBACK},
                       headers=CLINICIAN).status_code == 200
    assert client.post(f"/protocols/instances/{iid}/advance",
                       json={"step_key": "document"},
                       headers=CLINICIAN).status_code == 200
    r = client.post(f"/protocols/instances/{iid}/advance",
                    json={"step_key": "verify-closure"}, headers=CONFIG)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["current_step_key"] is None
    assert body["timer_posture"] is None
    assert all(s["status"] == "done" for s in body["steps"])
    # Advancing a completed instance conflicts.
    assert client.post(f"/protocols/instances/{iid}/advance",
                       json={"step_key": "notify", **READBACK},
                       headers=CLINICIAN).status_code == 409
    actions = _audit_actions(client)
    assert "protocols.instance.start" in actions
    assert actions.count("protocols.instance.advance") == 4
    assert "protocols.instance.complete" in actions


def test_instance_reads_and_filters(client):
    _publish_sop(client)
    a = _start(client, module_ref="lab-order:a")
    _start(client, module_ref="lab-order:b")
    all_rows = client.get("/protocols/instances", headers=VIEWER).json()
    assert len(all_rows) == 2
    filtered = client.get("/protocols/instances",
                          params={"module_ref": "lab-order:a"},
                          headers=CLINICIAN).json()
    assert [row["id"] for row in filtered] == [a["id"]]
    assert client.get("/protocols/instances",
                      params={"status": "completed"},
                      headers=CLINICIAN).json() == []
    assert client.get("/protocols/instances/9999",
                      headers=CLINICIAN).status_code == 404


def test_abandon_starter_or_admin_only_and_audited(client):
    _publish_sop(client)
    inst = _start(client)  # started by dev-clinician
    iid = inst["id"]
    # A different fhir.write holder who is neither starter nor admin.
    r = client.post(f"/protocols/instances/{iid}/abandon",
                    json={"reason": "not mine"}, headers=CONFIG)
    assert r.status_code == 403
    r = client.post(f"/protocols/instances/{iid}/abandon",
                    json={"reason": "result superseded"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "abandoned"
    assert r.json()["abandon_reason"] == "result superseded"
    assert "protocols.instance.abandon" in _audit_actions(client)
    # An abandoned module_ref no longer blocks a fresh instance.
    _start(client)


# ---------------------------------------------------------------------------
# Timers + escalations (computed at read; applied explicitly)
# ---------------------------------------------------------------------------

def test_escalate_without_due_escalation_conflicts(client):
    _publish_sop(client)
    inst = _start(client)
    r = client.post(f"/protocols/instances/{inst['id']}/escalate",
                    headers=CLINICIAN)
    assert r.status_code == 409


def test_timer_breach_names_escalation_then_escalate_applies(client):
    _publish_sop(client)
    inst = _start(client, module_ref="lab-order:breach-1")
    iid = inst["id"]
    # 60 minutes on a 30-minute timer with a 45-minute escalation.
    _backdate_current_step(client, iid, minutes=60)
    body = client.get(f"/protocols/instances/{iid}", headers=CLINICIAN).json()
    posture = body["timer_posture"]
    assert posture["breached"] is True
    assert posture["elapsed_minutes"] >= 59
    assert posture["escalation"] == {"after_minutes": 45,
                                      "to_role": "admin",
                                      "action": "page-on-call"}
    r = client.post(f"/protocols/instances/{iid}/escalate", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "escalated"
    assert r.json()["escalation_to_role"] == "admin"
    assert r.json()["escalation_action"] == "page-on-call"
    assert "protocols.instance.escalate" in _audit_actions(client)
    # Second escalate: the instance is no longer active — 409.
    assert client.post(f"/protocols/instances/{iid}/escalate",
                       headers=CLINICIAN).status_code == 409


def test_breach_without_reaching_escalation_threshold(client):
    _publish_sop(client)
    inst = _start(client, module_ref="lab-order:breach-2")
    _backdate_current_step(client, inst["id"], minutes=35)  # past 30, under 45
    body = client.get(f"/protocols/instances/{inst['id']}",
                      headers=CLINICIAN).json()
    assert body["timer_posture"]["breached"] is True
    assert body["timer_posture"]["escalation"] is None
    assert client.post(f"/protocols/instances/{inst['id']}/escalate",
                       headers=CLINICIAN).status_code == 409


# ---------------------------------------------------------------------------
# Coverage — honest by construction
# ---------------------------------------------------------------------------

def test_coverage_reflects_only_published_protocols(client):
    empty = client.get("/protocols/coverage", headers=CLINICIAN).json()
    assert empty["total_published_protocols"] == 0
    assert all(not m["has_published_protocol"] for m in empty["modules"])
    _publish_sop(client)
    cov = client.get("/protocols/coverage", headers=CLINICIAN).json()
    by_module = {m["module"]: m for m in cov["modules"]}
    assert set(by_module) == {"laboratory", "pharmacy", "ed-continuum",
                              "inpatient", "orders", "revenue", "workforce",
                              "devices", "engagement"}
    lab = by_module["laboratory"]
    assert lab["has_published_protocol"] is True
    assert lab["protocol_count"] == 1
    assert lab["protocols"][0]["key"] == KEY
    for name, entry in by_module.items():
        if name != "laboratory":
            assert entry["has_published_protocol"] is False, name
            assert entry["protocols"] == []
    assert cov["total_published_protocols"] == 1
