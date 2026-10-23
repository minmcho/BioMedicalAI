"""Phase 15 (BD-11..14) — support operations.

Hospital-at-home operating stack (telemetry bindings to Phase 14
registry devices, threshold evaluation creating real FHIR Tasks,
dispatch queue, kit provisioning), patient transport dispatch on
the Phase 12 TransportRequest table with exact timing metrics,
the command centre as a staffed unit (desks, staffing overlap,
huddle artefacts, authority-gated ICU triage) and the access-line
call centre (transfer-requested calls creating real Phase 12
transfer-centre records).

All config is published Studio terminology config; all people are
validated against the Phase 13 staff directory; synthetic data only.
"Today"/windows are computed in UTC.
"""
from datetime import datetime, timedelta, timezone

from .conftest import ADMIN, CLINICIAN, PATIENT, VIEWER


def _patient(client, pid="pat-hah-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic", "given": ["Home"]}],
        "gender": "female", "birthDate": "1970-05-01"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return f"Patient/{pid}"


def _staff(client, staff_id, username, job_role, name="Synthetic Staff"):
    r = client.post("/people/staff", json={
        "staff_id": staff_id, "username": username, "full_name": name,
        "job_role": job_role}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _tasks(client):
    r = client.get("/fhir/Task", headers=ADMIN)
    assert r.status_code == 200, r.text
    return [e["resource"] for e in r.json()["entry"]]


def _binding(client, patient_ref, device="vitals-monitor-1"):
    r = client.post("/support/hah/bindings", json={
        "patient_ref": patient_ref, "device_id": device}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Config seeding
# ---------------------------------------------------------------------------

def test_support_configs_seeded_published(client):
    r = client.get("/support/config", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    hah = body["hah-ops"]
    spo2 = next(t for t in hah["readingThresholds"] if t["type"] == "spo2")
    assert spo2["min"] == 92 and spo2["max"] == 100
    assert {d["code"] for d in hah["dispatchTypes"]} == {
        "visit", "supply", "rapid-response"}
    assert {k["code"] for k in hah["kitTypes"]} >= {"standard-hah"}
    transport = body["transport-ops"]
    assert {m["code"] for m in transport["modes"]} >= {"ambulance", "wheelchair-van"}
    assert {p["code"] for p in transport["priorities"]} == {"routine", "urgent", "stat"}
    desks = body["command-desks"]
    assert {d["code"] for d in desks["desks"]} == {
        "bed-management", "access-transfer", "transport"}
    assert all(d["roles"] and d["huddleCadence"] for d in desks["desks"])
    assert desks["triageAuthorityRole"] == "physician"
    access = body["access-line"]
    assert "primary-care" in access["sources"]
    assert access["dispositions"] == [
        "advice", "transfer-requested", "clinic-referral", "other"]


# ---------------------------------------------------------------------------
# BD-11 — bindings + telemetry readings
# ---------------------------------------------------------------------------

def test_binding_validates_device_and_one_active_per_device(client):
    p1 = _patient(client, "pat-bind-1")
    p2 = _patient(client, "pat-bind-2")
    binding = _binding(client, p1)
    assert binding["device_key"] == "vitals-monitor-1"
    assert binding["active"] is True
    # Unknown device -> 422; unknown patient -> 404.
    r = client.post("/support/hah/bindings", json={
        "patient_ref": p1, "device_id": "no-such-device"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post("/support/hah/bindings", json={
        "patient_ref": "Patient/ghost", "device_id": "pump-icu-1"},
        headers=CLINICIAN)
    assert r.status_code == 404, r.text
    # Second ACTIVE binding for the same device -> 409.
    r = client.post("/support/hah/bindings", json={
        "patient_ref": p2, "device_id": "vitals-monitor-1"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    # Deactivating frees the device.
    r = client.post(f"/support/hah/bindings/{binding['binding_id']}/deactivate",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    rebound = _binding(client, p2)
    assert rebound["patient_ref"] == p2


def test_out_of_range_reading_creates_task_and_breach_in_range_creates_nothing(client):
    patient_ref = _patient(client)
    binding = _binding(client, patient_ref)
    before = len(_tasks(client))
    # In-range reading: no Task, no breach.
    r = client.post("/support/hah/readings", json={
        "binding_id": binding["binding_id"], "reading_type": "spo2",
        "value": 98}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["in_range"] is True and body["task_id"] is None
    assert len(_tasks(client)) == before
    # Out-of-range (spo2 low): real FHIR Task + breach on the binding.
    r = client.post("/support/hah/readings", json={
        "binding_id": binding["binding_id"], "reading_type": "spo2",
        "value": 85, "at": "2026-10-18T09:15:00Z"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["in_range"] is False and body["breach"] == "low"
    assert body["task_id"]
    tasks = _tasks(client)
    assert len(tasks) == before + 1
    task = next(t for t in tasks if t["id"] == body["task_id"])
    assert task["for"]["reference"] == patient_ref
    assert "spo2" in task["description"]
    bindings = client.get("/support/hah/bindings", headers=ADMIN).json()
    row = next(b for b in bindings["items"]
               if b["binding_id"] == binding["binding_id"])
    assert row["breach_count"] == 1
    assert row["breaches"][0]["task_id"] == body["task_id"]
    assert row["breaches"][0]["value"] == 85
    # Unknown reading type -> 422.
    r = client.post("/support/hah/readings", json={
        "binding_id": binding["binding_id"], "reading_type": "altitude",
        "value": 10}, headers=CLINICIAN)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# BD-11 — dispatch queue + kits
# ---------------------------------------------------------------------------

def test_dispatch_lifecycle_and_queue_filters(client):
    patient_ref = _patient(client)
    _staff(client, "staff-hah-nurse", "hah-nurse-1", "nurse", "Hah Nurse")
    r = client.post("/support/hah/dispatches", json={
        "patient_ref": patient_ref, "type": "visit"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    dispatch = r.json()
    assert dispatch["status"] == "requested" and dispatch["sla_minutes"] == 240
    did = dispatch["dispatch_id"]
    # Illegal jump + assignment without a validated assignee.
    r = client.post(f"/support/hah/dispatches/{did}/transition", json={
        "to_state": "completed"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    r = client.post(f"/support/hah/dispatches/{did}/transition", json={
        "to_state": "assigned"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post("/support/hah/dispatches", json={
        "patient_ref": patient_ref, "type": "visit", "assignee": "ghost"},
        headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # Full lifecycle with the directory-validated assignee.
    for target in ("assigned", "en-route", "arrived", "completed"):
        payload = {"to_state": target}
        if target == "assigned":
            payload["assignee"] = "hah-nurse-1"
        r = client.post(f"/support/hah/dispatches/{did}/transition",
                        json=payload, headers=CLINICIAN)
        assert r.status_code == 200, (target, r.text)
    assert r.json()["status"] == "completed"
    assert r.json()["assignee_staff_id"] == "staff-hah-nurse"
    queue = client.get("/support/hah/dispatches",
                       params={"state": "completed", "type": "visit"},
                       headers=ADMIN).json()
    assert queue["total"] == 1 and queue["items"][0]["dispatch_id"] == did
    queue = client.get("/support/hah/dispatches",
                       params={"state": "requested"}, headers=ADMIN).json()
    assert queue["total"] == 0


def test_kit_issue_return_checkoff_and_overdue_worklist(client):
    patient_ref = _patient(client)
    r = client.post("/support/hah/kits", json={"kit_type": "standard-hah"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    kit = r.json()
    assert "bp-cuff" in kit["contents"] and kit["status"] == "available"
    kid = kit["kit_id"]
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    r = client.post(f"/support/hah/kits/{kid}/issue", json={
        "patient_ref": patient_ref, "expected_return_at": past},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "issued"
    overdue = client.get("/support/hah/kits/overdue", headers=ADMIN).json()
    assert [i["kit_id"] for i in overdue["items"]] == [kid]
    # Return missing an item is refused, naming it; kit stays issued.
    partial = [c for c in kit["contents"] if c != "thermometer"]
    r = client.post(f"/support/hah/kits/{kid}/return", json={
        "returned_contents": partial}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    assert "thermometer" in r.json()["detail"]
    overdue = client.get("/support/hah/kits/overdue", headers=ADMIN).json()
    assert overdue["total"] == 1
    # Full check-off returns the kit and clears the worklist.
    r = client.post(f"/support/hah/kits/{kid}/return", json={
        "returned_contents": kit["contents"]}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "returned"
    overdue = client.get("/support/hah/kits/overdue", headers=ADMIN).json()
    assert overdue["total"] == 0
    # Unknown kit type -> 422.
    r = client.post("/support/hah/kits", json={"kit_type": "moon-kit"},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# BD-12 — transport dispatch + metrics
# ---------------------------------------------------------------------------

def _transport(client, patient_ref, requested_at, dispatcher=None):
    payload = {"patient_ref": patient_ref, "from_location": "Ward 4",
               "to_location": "Radiology", "mode": "wheelchair-van",
               "priority": "routine", "requested_at": requested_at}
    if dispatcher:
        payload["dispatcher"] = dispatcher
    r = client.post("/support/transport", json=payload, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _move(client, request_id, target, **extra):
    r = client.post(f"/support/transport/{request_id}/transition",
                    json={"to_status": target, **extra}, headers=CLINICIAN)
    assert r.status_code == 200, (target, r.text)
    return r.json()


def test_transport_metrics_exact_math_and_shared_phase12_row(client):
    patient_ref = _patient(client)
    _staff(client, "staff-dispatcher", "dispatcher-1", "support-staff",
           "Dee Dispatcher")
    # Request 1: pickup +30m, dropoff +60m after pickup.
    t1 = _transport(client, patient_ref, "2026-10-18T08:00:00Z")
    _move(client, t1["request_id"], "assigned", dispatcher="dispatcher-1")
    _move(client, t1["request_id"], "in-transit", at="2026-10-18T08:30:00Z")
    done = _move(client, t1["request_id"], "completed", at="2026-10-18T09:30:00Z")
    assert done["status"] == "completed" and done["dispatcher"] == "dispatcher-1"
    # Request 2: pickup +60m, dropoff +60m after pickup.
    t2 = _transport(client, patient_ref, "2026-10-18T09:00:00Z")
    _move(client, t2["request_id"], "assigned", dispatcher="staff-dispatcher")
    _move(client, t2["request_id"], "in-transit", at="2026-10-18T10:00:00Z")
    _move(client, t2["request_id"], "completed", at="2026-10-18T11:00:00Z")

    metrics = client.get("/support/transport/metrics", headers=ADMIN).json()
    assert metrics["request_to_pickup"]["count"] == 2
    assert metrics["request_to_pickup"]["average_minutes"] == 45.0
    assert metrics["pickup_to_dropoff"]["count"] == 2
    assert metrics["pickup_to_dropoff"]["average_minutes"] == 60.0
    assert metrics["completed"] == 2
    # The same rows are the Phase 12 command centre's transport list —
    # extended, not forked.
    shared = client.get("/continuum/command/transport", headers=ADMIN).json()
    shared_ids = {i["id"] for i in shared["items"]}
    assert {t1["request_id"], t2["request_id"]} <= shared_ids


def test_transport_validation(client):
    patient_ref = _patient(client)
    r = client.post("/support/transport", json={
        "patient_ref": patient_ref, "from_location": "A", "to_location": "B",
        "mode": "helicopter"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post("/support/transport", json={
        "patient_ref": patient_ref, "from_location": "A", "to_location": "B",
        "priority": "whenever"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post("/support/transport", json={
        "patient_ref": patient_ref, "from_location": "A", "to_location": "B",
        "dispatcher": "ghost"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# BD-13 — command desks: staffing, huddles, triage
# ---------------------------------------------------------------------------

def test_command_staffing_overlap_rules(client):
    _staff(client, "staff-bed-mgr", "bed-mgr-1", "nurse", "Bed Manager")
    payload = {"desk": "bed-management", "staff": "bed-mgr-1",
               "starts_at": "2026-10-18T08:00:00Z",
               "ends_at": "2026-10-18T16:00:00Z"}
    r = client.post("/support/command/staffing", json=payload, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["staff_id"] == "staff-bed-mgr"
    overlap = dict(payload, starts_at="2026-10-18T12:00:00Z",
                   ends_at="2026-10-18T20:00:00Z")
    r = client.post("/support/command/staffing", json=overlap, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    # Same person, different desk, overlapping window: allowed.
    other = dict(overlap, desk="transport")
    r = client.post("/support/command/staffing", json=other, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/support/command/staffing", json={
        "desk": "moon-desk", "staff": "bed-mgr-1",
        "starts_at": "2026-10-19T08:00:00Z", "ends_at": "2026-10-19T16:00:00Z"},
        headers=CLINICIAN)
    assert r.status_code == 422, r.text
    listing = client.get("/support/command/staffing",
                         params={"desk": "bed-management"}, headers=ADMIN).json()
    assert listing["total"] == 1


def test_huddle_artefact_roundtrip(client):
    r = client.post("/support/command/huddles", json={
        "desk": "bed-management", "census": 120, "boarding": 8,
        "expected_discharges": 15,
        "decisions": ["Open surge unit", "Expedite 3 discharges"],
        "chaired_by": "dev-admin"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    huddle = r.json()
    assert huddle["census"] == 120 and huddle["boarding"] == 8
    assert huddle["expected_discharges"] == 15
    detail = client.get(f"/support/command/huddles/{huddle['huddle_id']}",
                        headers=ADMIN).json()
    assert detail["decisions"] == ["Open surge unit", "Expedite 3 discharges"]
    assert detail["chaired_by"] == "dev-admin"
    listing = client.get("/support/command/huddles", headers=ADMIN).json()
    assert listing["total"] == 1


def test_triage_decisions_require_authority_role_or_admin(client):
    patient_ref = _patient(client)
    _staff(client, "staff-icu-doc", "icu-doc-1", "physician", "Icu Doc")
    _staff(client, "staff-icu-nurse", "icu-nurse-1", "nurse", "Icu Nurse")
    # The config's triage-authority role (physician) may decide.
    r = client.post("/support/command/triage-decisions", json={
        "patient_ref": patient_ref, "decision": "admit-icu",
        "decided_by": "icu-doc-1"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["authority_role"] == "physician"
    assert r.json()["decided_by"] == "icu-doc-1"
    # A nurse may not.
    r = client.post("/support/command/triage-decisions", json={
        "patient_ref": patient_ref, "decision": "decline",
        "decided_by": "icu-nurse-1"}, headers=CLINICIAN)
    assert r.status_code == 403, r.text
    # An admin may (authority recorded as admin).
    r = client.post("/support/command/triage-decisions", json={
        "patient_ref": patient_ref, "decision": "defer"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["authority_role"] == "admin"
    listing = client.get("/support/command/triage-decisions",
                         params={"patient_ref": patient_ref},
                         headers=ADMIN).json()
    assert listing["total"] == 2
    r = client.post("/support/command/triage-decisions", json={
        "patient_ref": patient_ref, "decision": "maybe"}, headers=ADMIN)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# BD-14 — access line
# ---------------------------------------------------------------------------

def _call(client, **overrides):
    payload = {"caller_name": "Dr Referrer", "caller_org": "St Mary Hospital",
               "referral_source": "primary-care",
               "patient_summary": "Synthetic summary", "disposition": "advice"}
    payload.update(overrides)
    r = client.post("/support/access-line/calls", json=payload, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def test_access_line_transfer_creates_linked_phase12_transfer(client):
    call = _call(client, disposition="transfer-requested",
                 patient_name="Synthetic Pat")
    assert call["transfer_request_id"]
    transfers = client.get("/support/access-line/transfers", headers=ADMIN).json()
    assert transfers["total"] == 1
    item = transfers["items"][0]
    assert item["transfer_request_id"] == call["transfer_request_id"]
    assert item["status"] == "pending"
    assert item["linkage"]["model"] == "queued-intake-reference"
    assert "transfer centre" in item["linkage"]["joins"]
    # It IS the Phase 12 transfer centre's record (not a fork).
    centre = client.get("/continuum/command/transfers", headers=ADMIN).json()
    assert [i["id"] for i in centre["items"]] == [call["transfer_request_id"]]
    # An advice call creates no transfer.
    _call(client)
    transfers = client.get("/support/access-line/transfers", headers=ADMIN).json()
    assert transfers["total"] == 1
    r = client.post("/support/access-line/calls", json={
        "caller_name": "X", "referral_source": "moon",
        "patient_summary": "s", "disposition": "advice"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text


def test_access_line_volume_by_source_and_disposition_over_range(client):
    _call(client, call_at="2026-10-10T09:00:00Z")
    _call(client, call_at="2026-10-10T10:00:00Z", disposition="clinic-referral")
    _call(client, call_at="2026-10-11T09:00:00Z",
          referral_source="referring-hospital")
    _call(client, call_at="2026-10-15T09:00:00Z")  # outside the queried range
    volume = client.get("/support/access-line/volume",
                        params={"date_from": "2026-10-09",
                                "date_to": "2026-10-11"},
                        headers=ADMIN).json()
    assert volume["total"] == 3
    assert volume["by_source"] == {"primary-care": 2, "referring-hospital": 1}
    assert volume["by_disposition"] == {"advice": 2, "clinic-referral": 1}


# ---------------------------------------------------------------------------
# Capability gates
# ---------------------------------------------------------------------------

def test_support_capability_gates(client):
    r = client.get("/support/hah/bindings", headers=VIEWER)
    assert r.status_code == 200, r.text
    r = client.post("/support/hah/bindings", json={
        "patient_ref": "Patient/x", "device_id": "vitals-monitor-1"},
        headers=VIEWER)
    assert r.status_code == 403, r.text
    r = client.get("/support/hah/bindings", headers=PATIENT)
    assert r.status_code == 403, r.text
