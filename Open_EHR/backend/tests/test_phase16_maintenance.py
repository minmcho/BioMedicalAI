"""Phase 16 (WS-I) — HTM work orders + preventive maintenance.

Work orders generalise the Phase 14 calibration/HTM queue onto the
EXISTING device registry (app.devices): a governed lifecycle
(opened -> assigned -> in-progress -> completed -> closed, illegal
moves 409), PM generation from the published
`terminology/maintenance` intervals (idempotent per device+period),
and downtime work orders that flip the registry device status to
the configured downtime status and restore it on completion.

Baseline rule under test (stated in app.maintenance): a device with
no completed PM is baselined on `last_calibrated_at` (falling back
to `created_at`). The seeded registry makes exactly one device PM
due under the seeded 180-day pump interval: pump-icu-1 was last
calibrated 200 days ago; every other seeded device is inside its
interval.

Synthetic data only.
"""
from .conftest import ADMIN, CLINICIAN, PATIENT, VIEWER


def _create(client, device_id="chem-analyser-1", **overrides):
    body = {"device_id": device_id, "type": "corrective",
            "priority": "routine", "reason": "Synthetic fault"}
    body.update(overrides)
    r = client.post("/maintenance/work-orders", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _transition(client, work_order_id, to_status, **extra):
    body = {"to_status": to_status}
    body.update(extra)
    return client.post(f"/maintenance/work-orders/{work_order_id}/transition",
                       json=body, headers=CLINICIAN)


def _drive_to_completed(client, work_order_id, note="Synthetic repair done"):
    r = client.post(f"/maintenance/work-orders/{work_order_id}/assign",
                    json={"assignee": "tech-1"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = _transition(client, work_order_id, "in-progress")
    assert r.status_code == 200, r.text
    r = _transition(client, work_order_id, "completed",
                    completion_note=note)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_work_order_lifecycle_happy_path(client):
    order = _create(client, priority="urgent",
                    reason="Analyser flagging QC drift")
    assert order["status"] == "opened"
    assert order["opened_by"] == "dev-clinician"
    assert order["completed_at"] is None
    wo = order["work_order_id"]
    r = client.post(f"/maintenance/work-orders/{wo}/assign",
                    json={"assignee": "biomed-tech-7"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assigned"
    assert r.json()["assignee"] == "biomed-tech-7"
    r = _transition(client, wo, "in-progress")
    assert r.status_code == 200 and r.json()["status"] == "in-progress"
    r = _transition(client, wo, "completed",
                    completion_note="Replaced synthetic pump tubing")
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "completed"
    assert done["completed_at"] is not None
    assert done["completion_note"] == "Replaced synthetic pump tubing"
    r = _transition(client, wo, "closed")
    assert r.status_code == 200 and r.json()["status"] == "closed"
    fetched = client.get(f"/maintenance/work-orders/{wo}",
                         headers=ADMIN).json()
    assert fetched["status"] == "closed"
    assert fetched["device_id"] == "chem-analyser-1"


def test_illegal_transitions_are_409(client):
    order = _create(client)
    wo = order["work_order_id"]
    # opened cannot skip the lifecycle, and closing requires completion.
    assert _transition(client, wo, "completed").status_code == 409
    assert _transition(client, wo, "closed").status_code == 409
    assert _transition(client, wo, "opened").status_code == 409
    _drive_to_completed(client, wo)
    # completed cannot reopen.
    assert _transition(client, wo, "opened").status_code == 409
    assert _transition(client, wo, "in-progress").status_code == 409
    r = _transition(client, wo, "closed")
    assert r.status_code == 200
    # closed is terminal.
    assert _transition(client, wo, "opened").status_code == 409
    r = client.post(f"/maintenance/work-orders/{wo}/assign",
                    json={"assignee": "tech-2"}, headers=CLINICIAN)
    assert r.status_code == 409


def test_work_order_validation_and_unknown_device(client):
    r = client.post("/maintenance/work-orders", json={
        "device_id": "ghost-device", "type": "corrective",
        "reason": "No such device"}, headers=CLINICIAN)
    assert r.status_code == 404  # device must exist in the registry
    r = client.post("/maintenance/work-orders", json={
        "device_id": "chem-analyser-1", "type": "emergency",
        "reason": "Bad type"}, headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post("/maintenance/work-orders", json={
        "device_id": "chem-analyser-1", "type": "corrective",
        "priority": "whenever", "reason": "Bad priority"},
        headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post("/maintenance/work-orders", json={
        "device_id": "chem-analyser-1", "type": "corrective"},
        headers=CLINICIAN)
    assert r.status_code == 422  # reason required
    assert client.get("/maintenance/work-orders/WO-9999",
                      headers=ADMIN).status_code == 404


def test_maintenance_capability_gates(client):
    assert client.get("/maintenance/worklist",
                      headers=PATIENT).status_code == 403
    assert client.get("/maintenance/worklist",
                      headers=VIEWER).status_code == 200
    r = client.post("/maintenance/work-orders", json={
        "device_id": "chem-analyser-1", "type": "corrective",
        "reason": "Viewer cannot work"}, headers=VIEWER)
    assert r.status_code == 403
    r = client.post("/maintenance/pm/generate", headers=PATIENT)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Preventive maintenance generation
# ---------------------------------------------------------------------------

def test_pm_generate_due_only_and_idempotent(client):
    # A freshly registered device is inside its interval (baseline:
    # created_at) and must NOT get a PM order.
    r = client.post("/devices", json={
        "device_key": "pump-new-1", "device_type": "pump",
        "name": "New ward pump", "location": "Ward 4"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/maintenance/pm/generate", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    # Exactly one seeded device is due: pump-icu-1 (last calibrated
    # 200 days ago vs the published 180-day pump PM interval).
    assert body["created_count"] == 1, body
    created = body["created"][0]
    assert created["device_id"] == "pump-icu-1"
    assert created["type"] == "preventive"
    assert created["status"] == "opened"
    assert created["overdue"] is True
    # Idempotent: a second generate in the same period creates none.
    r = client.post("/maintenance/pm/generate", headers=CLINICIAN)
    assert r.json()["created_count"] == 0
    assert r.json()["skipped_open"] == 1
    orders = client.get("/maintenance/work-orders", headers=ADMIN,
                        params={"type": "preventive"}).json()
    assert orders["total"] == 1
    # Completing the PM resets the clock: still nothing to generate.
    _drive_to_completed(client, created["work_order_id"],
                        note="Scheduled PM performed")
    r = client.post("/maintenance/pm/generate", headers=CLINICIAN)
    assert r.json()["created_count"] == 0


# ---------------------------------------------------------------------------
# Downtime
# ---------------------------------------------------------------------------

def test_downtime_flips_registry_status_and_completion_restores(client):
    before = client.get("/devices/vitals-monitor-1", headers=ADMIN).json()
    assert before["status"] == "active"
    order = _create(client, device_id="vitals-monitor-1",
                    priority="stat", downtime=True,
                    reason="Monitor display failed — device down")
    during = client.get("/devices/vitals-monitor-1", headers=ADMIN).json()
    assert during["status"] == "out-of-service"  # configured downtime status
    _drive_to_completed(client, order["work_order_id"])
    after = client.get("/devices/vitals-monitor-1", headers=ADMIN).json()
    assert after["status"] == "active"
    # A non-downtime order never touches the registry status.
    other = _create(client, device_id="vitals-monitor-1", downtime=False,
                    reason="Routine inspection")
    mid = client.get("/devices/vitals-monitor-1", headers=ADMIN).json()
    assert mid["status"] == "active"
    _drive_to_completed(client, other["work_order_id"])
    end = client.get("/devices/vitals-monitor-1", headers=ADMIN).json()
    assert end["status"] == "active"


# ---------------------------------------------------------------------------
# Worklist
# ---------------------------------------------------------------------------

def test_worklist_priority_ordering_and_overdue_flag(client):
    stat = _create(client, device_id="chem-analyser-1", priority="stat",
                   reason="STAT: analyser down")
    urgent = _create(client, device_id="heme-analyser-1",
                     priority="urgent", reason="Urgent: sample jam")
    routine = _create(client, device_id="fridge-lab-1",
                      priority="routine", reason="Routine: door seal")
    r = client.post("/maintenance/pm/generate", headers=CLINICIAN)
    assert r.json()["created_count"] == 1
    pm = r.json()["created"][0]
    worklist = client.get("/maintenance/worklist", headers=ADMIN).json()
    assert worklist["total"] == 4
    priorities = [i["priority"] for i in worklist["items"]]
    assert priorities == ["stat", "urgent", "routine", "routine"]
    assert worklist["items"][0]["work_order_id"] == stat["work_order_id"]
    by_id = {i["work_order_id"]: i for i in worklist["items"]}
    assert by_id[pm["work_order_id"]]["overdue"] is True
    assert by_id[stat["work_order_id"]]["overdue"] is False
    assert by_id[urgent["work_order_id"]]["overdue"] is False
    assert by_id[routine["work_order_id"]]["overdue"] is False
    assert worklist["overdue"] == 1
    # Completed orders leave the worklist.
    _drive_to_completed(client, stat["work_order_id"])
    worklist = client.get("/maintenance/worklist", headers=ADMIN).json()
    assert worklist["total"] == 3
    assert stat["work_order_id"] not in {
        i["work_order_id"] for i in worklist["items"]}
