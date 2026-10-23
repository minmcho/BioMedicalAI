"""Phase 12 (Track D): care-continuum operations.

ER operations (ESI targets + split flow, activations, boarding with
rules-engine escalation, diversion, LWBS, EMTALA gate, callbacks),
inpatient operations (acuity assignments, rounds, readiness, I-PASS
handoffs, EVS), outpatient operations (session templates + overbook,
rooming, no-show risk, backfill, referral loop closure, SLA inbox,
care gaps), aftercare (call queue, home health, readmission risk,
hospital-at-home), the Capacity Command Center (transfer centre,
transport, bed requests), safety operations (Armstrong pattern), and
the Phase 12 seeds (config, forms, dashboards, rule, EMTALA pack +
register, safety-triage agent draft).
"""
import datetime as dt

from .conftest import ADMIN, CLINICIAN, PATIENT, VIEWER

MSE_ITEMS = ["presenting-complaint", "vital-signs",
             "screening-exam-performed", "exam-findings",
             "stabilising-treatment", "examiner"]
CERT_ITEMS = ["risks-benefits-discussed", "patient-consent",
              "receiving-facility", "receiving-physician",
              "transport-mode", "medical-records-sent"]
IPASS_ITEMS = ["illness-severity", "patient-summary", "action-list",
               "situation-awareness", "synthesis"]
TEACHBACK_ITEMS = ["instructions-given", "teach-back-completed",
                   "understanding-confirmed"]


def _answers(items, value="documented"):
    return [{"linkId": link,
             "answer": [{"valueBoolean": True}
                        if link in ("teach-back-completed",
                                    "understanding-confirmed")
                        else {"valueString": value}]}
            for link in items]


def _patient(client, pid, birth="1980-01-01", gender="female"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Test", "given": ["P12"]}],
        "birthDate": birth, "gender": gender}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _ed_arrival(client, pid, arrived_at=None):
    payload = {"patient": pid}
    if arrived_at:
        payload["arrived_at"] = arrived_at
    r = client.post("/ed/arrivals", json=payload, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["encounter"]["id"] if "encounter" in r.json() \
        else r.json()["id"]


def _ed_transition(client, enc, to, **kw):
    payload = {"to_status": to}
    payload.update(kw)
    r = client.post(f"/ed/encounters/{enc}/transition", json=payload,
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def _ed_admit(client, enc):
    _ed_transition(client, enc, "triage", acuity=3)
    _ed_transition(client, enc, "in-treatment")
    _ed_transition(client, enc, "disposition")
    r = client.post(f"/ed/encounters/{enc}/admit", json={}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()["inpatient_encounter"]["id"]


def _admit_bed(client, pid, bed="MS-101A"):
    r = client.post("/inpatient/admissions",
                    json={"patient": pid, "bed": bed}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["encounter"]["id"]


def _discharge(client, enc, **kw):
    r = client.post(f"/inpatient/encounters/{enc}/discharge", json=kw,
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def _publish_ops(client, mutate):
    from app.continuum import default_ops_payload
    payload = default_ops_payload()
    mutate(payload)
    r = client.post("/studio/config/terminology/continuum-ops",
                    json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/terminology/continuum-ops/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text


def _future_date(days=1):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)) \
        .date().isoformat()


def _book_future_appointment(client, pid, days=3):
    start = (dt.datetime.now(dt.timezone.utc)
             + dt.timedelta(days=days)).isoformat()
    r = client.post("/fhir/Appointment", json={
        "resourceType": "Appointment", "status": "booked",
        "start": start,
        "participant": [{"actor": {"reference": f"Patient/{pid}"},
                         "status": "accepted"}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# ER operations
# ---------------------------------------------------------------------------

def test_er_board_targets_and_split_flow(client):
    _patient(client, "er-1")
    _patient(client, "er-2")
    fast = _ed_arrival(client, "er-1")
    main = _ed_arrival(client, "er-2")
    _ed_transition(client, fast, "triage", acuity=5)
    _ed_transition(client, main, "triage", acuity=2)
    board = client.get("/continuum/er/board", headers=CLINICIAN).json()
    assert board["counts"]["total"] == 2
    assert [e["encounter"]["id"] for e in board["queues"]["fast_track"]] == [fast]
    assert [e["encounter"]["id"] for e in board["queues"]["main"]] == [main]
    entry = board["queues"]["main"][0]
    assert entry["target_minutes"] == 10  # ESI 2 target from config
    assert entry["target_breached"] is False
    assert board["diversion"] == "open"


def test_er_board_target_breach_from_config(client):
    _patient(client, "er-3")
    two_hours_ago = (dt.datetime.now(dt.timezone.utc)
                     - dt.timedelta(hours=2)).isoformat()
    enc = _ed_arrival(client, "er-3", arrived_at=two_hours_ago)
    _ed_transition(client, enc, "triage", acuity=3)  # target 30 min
    board = client.get("/continuum/er/board", headers=CLINICIAN).json()
    entry = board["queues"]["main"][0]
    assert entry["wait_minutes"] >= 119
    assert entry["target_breached"] is True
    assert board["counts"]["target_breaches"] == 1


def test_activation_lifecycle_and_duplicate_guards(client):
    _patient(client, "er-4")
    enc = _ed_arrival(client, "er-4")
    _ed_transition(client, enc, "triage", acuity=1)
    r = client.post("/continuum/er/activations",
                    json={"encounter_id": enc, "pathway": "bogus"},
                    headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post("/continuum/er/activations",
                    json={"encounter_id": enc, "pathway": "stemi"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    act = r.json()
    assert act["target_minutes"] == 90
    assert [m["code"] for m in act["milestones"]] == [
        "ecg", "cath-lab", "reperfusion"]
    r = client.post("/continuum/er/activations",
                    json={"encounter_id": enc, "pathway": "stemi"},
                    headers=CLINICIAN)
    assert r.status_code == 409  # already open
    r = client.post(f"/continuum/er/activations/{act['id']}/milestone",
                    json={"code": "ecg"}, headers=CLINICIAN)
    assert r.status_code == 200
    assert r.json()["milestones"][0]["recorded_at"] is not None
    r = client.post(f"/continuum/er/activations/{act['id']}/milestone",
                    json={"code": "ecg"}, headers=CLINICIAN)
    assert r.status_code == 409  # already recorded
    r = client.post(f"/continuum/er/activations/{act['id']}/milestone",
                    json={"code": "nope"}, headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post(f"/continuum/er/activations/{act['id']}/close",
                    json={"outcome": "pci-done"}, headers=CLINICIAN)
    assert r.status_code == 200
    assert r.json()["status"] == "closed"


def test_activation_milestone_breach_is_config_driven(client):
    _publish_ops(client, lambda p: p["activationPathways"][1]
                 ["milestones"][0].update({"targetMinutes": -1}))
    _patient(client, "er-5")
    enc = _ed_arrival(client, "er-5")
    _ed_transition(client, enc, "triage", acuity=2)
    r = client.post("/continuum/er/activations",
                    json={"encounter_id": enc, "pathway": "stroke"},
                    headers=CLINICIAN)
    assert r.status_code == 201
    listed = client.get("/continuum/er/activations?status=open",
                        headers=CLINICIAN).json()
    milestone = listed["items"][0]["milestones"][0]
    assert milestone["targetMinutes"] == -1
    assert milestone["breached"] is True  # any elapsed time exceeds it


def test_lwbs_capture_only_while_waiting(client):
    _patient(client, "er-6")
    waiting = _ed_arrival(client, "er-6")
    r = client.post(f"/continuum/er/encounters/{waiting}/lwbs",
                    json={"note": "left before being seen"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["lwbs"] is True
    assert r.json()["ed_encounter"]["status"] == "departed"
    _patient(client, "er-7")
    seen = _ed_arrival(client, "er-7")
    _ed_transition(client, seen, "triage", acuity=3)
    _ed_transition(client, seen, "in-treatment")
    r = client.post(f"/continuum/er/encounters/{seen}/lwbs",
                    json={}, headers=CLINICIAN)
    assert r.status_code == 409  # already being seen — not LWBS


def test_diversion_states_are_config_and_audited(client):
    r = client.post("/continuum/er/diversion",
                    json={"status": "diversion-ed", "surgeLevel": "surge",
                          "reason": "no ICU beds"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["diversion"] == "diversion-ed"
    assert r.json()["surge_level"] == "surge"
    r = client.post("/continuum/er/diversion", json={"status": "closed"},
                    headers=CLINICIAN)
    assert r.status_code == 422  # not a configured state
    r = client.post("/continuum/er/diversion", json={"status": "open"},
                    headers=VIEWER)
    assert r.status_code == 403  # viewer cannot work the board
    board = client.get("/continuum/er/board", headers=CLINICIAN).json()
    assert board["diversion"] == "diversion-ed"
    actions = [a["action"] for a in
               client.get("/studio/audit?limit=200", headers=ADMIN).json()]
    assert "continuum.ops.diversion" in actions


def test_boarding_board_and_command_assign(client):
    _patient(client, "er-8")
    enc = _ed_arrival(client, "er-8")
    inpatient_id = _ed_admit(client, enc)
    board = client.get("/continuum/er/boarding", headers=CLINICIAN).json()
    assert board["total"] == 1 and board["ed_total"] == 1
    assert board["items"][0]["encounter_id"] == inpatient_id
    assert board["items"][0]["source"] == "ed"
    r = client.post(
        f"/continuum/command/bed-requests/{inpatient_id}/assign",
        json={"bed": "MS-101A"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["event"]["event_type"] == "A01"
    board = client.get("/continuum/er/boarding", headers=CLINICIAN).json()
    assert board["total"] == 0


def test_boarding_escalation_rule_fires_at_threshold(client):
    for i in range(4):
        _patient(client, f"er-9-{i}")
        enc = _ed_arrival(client, f"er-9-{i}")
        _ed_admit(client, enc)
    board = client.get("/continuum/er/boarding", headers=CLINICIAN).json()
    assert board["total"] == 4
    bundle = client.get("/fhir/Task", headers=CLINICIAN).json()
    titles = [e["resource"].get("description")
              for e in bundle.get("entry", [])]
    assert any(t and "boarding escalation" in t for t in titles)


def test_emtala_transfer_gate(client):
    _patient(client, "er-10")
    enc = _ed_arrival(client, "er-10")
    _ed_transition(client, enc, "triage", acuity=2)
    _ed_transition(client, enc, "in-treatment")
    _ed_transition(client, enc, "disposition")
    # No MSE on file: the transfer cannot be recorded, and the 422
    # names the missing screening elements.
    r = client.post(f"/continuum/er/encounters/{enc}/transfer-out",
                    json={"answers": _answers(CERT_ITEMS)}, headers=CLINICIAN)
    assert r.status_code == 422
    assert "screening" in r.json()["detail"]
    # An incomplete MSE is refused by the form machinery itself.
    r = client.post(f"/continuum/er/encounters/{enc}/mse",
                    json={"answers": _answers(MSE_ITEMS[:3])},
                    headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post(f"/continuum/er/encounters/{enc}/mse",
                    json={"answers": _answers(MSE_ITEMS)}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # An incomplete certification is refused too.
    r = client.post(f"/continuum/er/encounters/{enc}/transfer-out",
                    json={"answers": _answers(CERT_ITEMS[:4])},
                    headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post(f"/continuum/er/encounters/{enc}/transfer-out",
                    json={"answers": _answers(CERT_ITEMS)}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["ed_encounter"]["status"] == "departed"
    assert r.json()["transfer_response_id"]


def test_results_callback_queue(client):
    _patient(client, "er-11")
    enc = _ed_arrival(client, "er-11")
    _ed_transition(client, enc, "triage", acuity=4)
    _ed_transition(client, enc, "in-treatment")
    _ed_transition(client, enc, "disposition")
    r = client.post(f"/ed/encounters/{enc}/depart", json={}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post("/fhir/DiagnosticReport", json={
        "resourceType": "DiagnosticReport", "id": "rep-cb-1",
        "status": "preliminary",
        "subject": {"reference": "Patient/er-11"},
        "code": {"text": "BMP"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    queue = client.get("/continuum/er/callbacks", headers=CLINICIAN).json()
    assert queue["total"] == 1
    assert queue["items"][0]["report_id"] == "rep-cb-1"
    r = client.post("/continuum/er/callbacks/rep-cb-1/complete",
                    json={"outcome": "patient-reached"},
                    headers=CLINICIAN)
    assert r.status_code == 200
    queue = client.get("/continuum/er/callbacks", headers=CLINICIAN).json()
    assert queue["total"] == 0
    assert len(queue["completed"]) == 1


# ---------------------------------------------------------------------------
# Inpatient operations
# ---------------------------------------------------------------------------

def test_assignments_loads_and_overload_flag(client):
    for pid, bed in [("in-1", "MS-101A"), ("in-2", "MS-101B"),
                     ("in-3", "MS-102A")]:
        _patient(client, pid)
        _admit_bed(client, pid, bed)
    encs = {}
    board = client.get("/continuum/inpatient/discharge-readiness",
                       headers=CLINICIAN).json()
    for item in board["items"]:
        encs[item["patient_ref"]] = item["encounter_id"]
    for pid in ("in-1", "in-2", "in-3"):
        r = client.post("/continuum/inpatient/assignments",
                        json={"encounter_id": encs[f"Patient/{pid}"],
                              "nurse": "nurse-jane", "acuity": 5},
                        headers=CLINICIAN)
        assert r.status_code == 201, r.text
    board = client.get("/continuum/inpatient/assignments",
                       headers=CLINICIAN).json()
    assert board["total"] == 3
    nurse = board["nurses"][0]
    assert nurse["acuity_load"] == 15
    assert nurse["overloaded"] is True  # config max is 12
    r = client.post("/continuum/inpatient/assignments",
                    json={"encounter_id": encs["Patient/in-1"],
                          "nurse": "nurse-jane", "acuity": 9},
                    headers=CLINICIAN)
    assert r.status_code == 422  # not a configured acuity level


def test_rounds_list_per_unit(client):
    _patient(client, "in-4")
    _admit_bed(client, "in-4", "ICU-201")
    rounds = client.get("/continuum/inpatient/rounds?unit=unit-icu",
                        headers=CLINICIAN).json()
    assert rounds["total"] == 1
    assert rounds["items"][0]["bed"] == "ICU-201"
    rounds = client.get("/continuum/inpatient/rounds?unit=unit-medsurg",
                        headers=CLINICIAN).json()
    assert rounds["total"] == 0


def test_discharge_readiness_progression(client):
    _patient(client, "in-5")
    enc = _admit_bed(client, "in-5")
    ready = client.get(f"/continuum/aftercare/readiness/{enc}",
                       headers=CLINICIAN).json()
    assert ready["ready"] is False
    assert set(ready["missing"]) == {"discharge-checklist", "teach-back",
                                     "follow-up-appointment"}
    r = client.post(f"/inpatient/encounters/{enc}/discharge-plan",
                    json={"targetDate": _future_date(0),
                          "disposition": "home",
                          "checklistResponseId": "qr-1"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post("/continuum/aftercare/teach-back",
                    json={"encounter_id": enc,
                          "answers": _answers(TEACHBACK_ITEMS)},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    _book_future_appointment(client, "in-5")
    ready = client.get(f"/continuum/aftercare/readiness/{enc}",
                       headers=CLINICIAN).json()
    assert ready["checklist_done"] is True
    assert ready["teachback_done"] is True
    assert ready["followup_booked"] is True
    assert ready["ready"] is True
    board = client.get("/continuum/inpatient/discharge-readiness",
                       headers=CLINICIAN).json()
    assert board["ready"] == 1


def test_handoff_ipass_form_enforced(client):
    _patient(client, "in-6")
    enc = _admit_bed(client, "in-6")
    r = client.post("/continuum/inpatient/handoffs",
                    json={"encounter_id": enc,
                          "answers": _answers(IPASS_ITEMS[:3])},
                    headers=CLINICIAN)
    assert r.status_code == 422  # required I-PASS sections missing
    r = client.post("/continuum/inpatient/handoffs",
                    json={"encounter_id": enc,
                          "answers": _answers(IPASS_ITEMS)},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    listed = client.get(f"/continuum/inpatient/handoffs?encounter_id={enc}",
                        headers=CLINICIAN).json()
    assert listed["total"] == 1
    assert listed["items"][0]["items"][0]["linkId"] == "illness-severity"


def test_evs_turnaround_lifecycle(client):
    _patient(client, "in-7")
    enc = _admit_bed(client, "in-7", "MS-103A")
    _discharge(client, enc)
    board = client.get("/continuum/inpatient/evs", headers=CLINICIAN).json()
    open_items = [i for i in board["items"] if i["status"] != "completed"]
    assert len(open_items) == 1
    assert open_items[0]["bed_code"] == "MS-103A"
    # Housekeeping flips the bed back to available (Phase 6 path) —
    # the EVS task closes on the same event.
    r = client.post("/inpatient/beds/MS-103A/status",
                    json={"status": "available"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    board = client.get("/continuum/inpatient/evs", headers=CLINICIAN).json()
    done = [i for i in board["items"] if i["status"] == "completed"]
    assert len(done) == 1
    assert done[0]["turnaround_minutes"] is not None
    assert board["open"] == 0


def test_discharge_followup_guard_config_flip(client):
    _patient(client, "in-8")
    enc = _admit_bed(client, "in-8", "MS-103B")
    _discharge(client, enc)  # guard ships OFF: discharge succeeds
    _publish_ops(client, lambda p: p["dischargeGuards"]
                 .update({"followUpRequired": True}))
    _patient(client, "in-9")
    enc2 = _admit_bed(client, "in-9", "MS-104A")
    r = client.post(f"/inpatient/encounters/{enc2}/discharge", json={},
                    headers=CLINICIAN)
    assert r.status_code == 409  # no follow-up booked
    _book_future_appointment(client, "in-9")
    r = client.post(f"/inpatient/encounters/{enc2}/discharge", json={},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Outpatient operations
# ---------------------------------------------------------------------------

def test_clinic_booking_capacity_and_overbook(client):
    day = _future_date(2)
    _patient(client, "out-1")
    for i in range(8):  # fast-track: capacity 8, overbook limit 0
        _patient(client, f"out-ft-{i}")
        r = client.post("/continuum/clinic/book",
                        json={"patient": f"out-ft-{i}",
                              "session": "fast-track-clinic", "date": day},
                        headers=CLINICIAN)
        assert r.status_code == 201, r.text
        assert r.json()["overbooked"] is False
    r = client.post("/continuum/clinic/book",
                    json={"patient": "out-1", "session": "fast-track-clinic",
                          "date": day}, headers=CLINICIAN)
    assert r.status_code == 409  # full, no overbooking allowed
    for i in range(12):  # am-clinic: capacity 12, overbook limit 2
        _patient(client, f"out-am-{i}")
        r = client.post("/continuum/clinic/book",
                        json={"patient": f"out-am-{i}",
                              "session": "am-clinic", "date": day},
                        headers=CLINICIAN)
        assert r.status_code == 201, r.text
    r = client.post("/continuum/clinic/book",
                    json={"patient": "out-1", "session": "am-clinic",
                          "date": day}, headers=CLINICIAN)
    assert r.status_code == 201
    assert r.json()["overbooked"] is True  # 13th = into overbooking
    templates = client.get("/continuum/clinic/templates",
                           headers=CLINICIAN).json()
    assert templates["total"] == 3
    dayview = client.get(f"/continuum/clinic/day?date={day}",
                         headers=CLINICIAN).json()
    am = next(s for s in dayview["sessions"] if s["code"] == "am-clinic")
    assert am["booked"] == 13 and am["overbooked"] == 1


def test_clinic_rooming_recorded_on_appointment(client):
    _patient(client, "out-2")
    r = client.post("/continuum/clinic/book",
                    json={"patient": "out-2", "session": "am-clinic",
                          "date": _future_date(1)}, headers=CLINICIAN)
    appt = r.json()["appointment"]["id"]
    r = client.post(f"/continuum/clinic/appointments/{appt}/room",
                    json={"room": "Exam 4"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    fetched = client.get(f"/fhir/Appointment/{appt}",
                         headers=CLINICIAN).json()
    exts = {e["url"]: e for e in fetched.get("extension", [])}
    assert "Exam 4" in exts["urn:opencare:extension/rooming"]["valueString"]


def test_no_show_risk_from_own_history(client):
    _patient(client, "out-3")
    for i, status in enumerate(["noshow", "noshow", "noshow", "fulfilled"]):
        r = client.post("/fhir/Appointment", json={
            "resourceType": "Appointment", "id": f"hist-{i}",
            "status": status, "start": "2026-09-01T09:00:00Z",
            "participant": [{"actor": {"reference": "Patient/out-3"},
                             "status": "accepted"}]}, headers=CLINICIAN)
        assert r.status_code == 201, r.text
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    _book_future_appointment(client, "out-3", days=0)
    risks = client.get(f"/continuum/clinic/no-show-risks?date={today}",
                       headers=CLINICIAN).json()
    item = next(i for i in risks["items"]
                if i["patient_ref"] == "Patient/out-3")
    assert item["history_missed"] == 3 and item["history_attended"] == 1
    assert item["miss_rate"] == 0.75
    assert item["high_risk"] is True
    assert "no model" in item["basis"]


def test_backfill_worklist_and_match(client):
    _patient(client, "out-4")
    _patient(client, "out-5")
    start = (dt.datetime.now(dt.timezone.utc)
             + dt.timedelta(days=2)).isoformat()
    r = client.post("/fhir/Appointment", json={
        "resourceType": "Appointment", "id": "bf-1", "status": "booked",
        "appointmentType": {"coding": [{
            "system": "urn:opencare:slot-type", "code": "follow-up",
            "display": "Follow-up visit"}]},
        "start": start,
        "participant": [{"actor": {"reference": "Patient/out-4"},
                         "status": "accepted"}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/access/waitlist",
                    json={"patient": "out-5", "slot_type": "follow-up"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/fhir/Appointment/bf-1/transition",
                    json={"to_status": "cancelled"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    work = client.get("/continuum/clinic/backfill", headers=CLINICIAN).json()
    assert any(f["appointment_id"] == "bf-1" for f in work["freed_slots"])
    assert any(w["patient_ref"] == "Patient/out-5" for w in work["waiting"])
    assert any(m["appointment_id"] == "bf-1" for m in work["matches"])


def _referral(client, pid):
    r = client.post("/access/referrals", json={
        "patient": pid, "service_code": "cardiology",
        "referring_organization": "Community Clinic"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["referral"]["id"]


def _referral_transition(client, rid, to):
    r = client.post(f"/access/referrals/{rid}/transition",
                    json={"to_status": to}, headers=CLINICIAN)
    assert r.status_code == 200, r.text


def test_referral_loop_closure(client):
    _patient(client, "out-6")
    rid = _referral(client, "out-6")
    loop = client.get("/continuum/referrals/loop", headers=CLINICIAN).json()
    item = next(i for i in loop["items"] if i["id"] == rid)
    assert item["stage"] == "sent"
    r = client.post(f"/continuum/referrals/{rid}/report-back",
                    json={}, headers=CLINICIAN)
    assert r.status_code == 409  # not completed yet
    for to in ("triaged", "authorised", "scheduled", "completed"):
        _referral_transition(client, rid, to)
    loop = client.get("/continuum/referrals/loop", headers=CLINICIAN).json()
    item = next(i for i in loop["items"] if i["id"] == rid)
    assert item["stage"] == "awaiting-report"
    r = client.post(f"/continuum/referrals/{rid}/report-back",
                    json={"note": "echo report filed"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "closed"
    loop = client.get("/continuum/referrals/loop", headers=CLINICIAN).json()
    assert loop["open"] == 0


def test_inbox_aggregation_and_task_workflow(client):
    _patient(client, "out-7")
    r = client.post("/continuum/aftercare/home-health",
                    json={"patient": "out-7", "service": "wound-care"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    task_id = r.json()["task"]["id"]
    inbox = client.get("/continuum/inbox?type=task", headers=CLINICIAN).json()
    assert any(i["task_id"] == task_id for i in inbox["items"])
    item = next(i for i in inbox["items"] if i["task_id"] == task_id)
    assert item["sla_minutes"] == 480  # from config
    assert item["breached"] is False
    r = client.post(f"/continuum/tasks/{task_id}/transition",
                    json={"to_status": "completed"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    inbox = client.get("/continuum/inbox?type=task", headers=CLINICIAN).json()
    assert not any(i["task_id"] == task_id for i in inbox["items"])
    r = client.post(f"/continuum/tasks/{task_id}/transition",
                    json={"to_status": "completed"}, headers=CLINICIAN)
    assert r.status_code == 409  # already completed


def test_inbox_sla_breach_is_config_driven(client):
    _publish_ops(client, lambda p: p["inboxSlaMinutes"]
                 .update({"task": -1}))
    _patient(client, "out-8")
    r = client.post("/continuum/aftercare/home-health",
                    json={"patient": "out-8", "service": "pt-visit"},
                    headers=CLINICIAN)
    task_id = r.json()["task"]["id"]
    inbox = client.get("/continuum/inbox?type=task", headers=CLINICIAN).json()
    item = next(i for i in inbox["items"] if i["task_id"] == task_id)
    assert item["breached"] is True
    assert inbox["breached"] >= 1
    assert inbox["items"][0]["task_id"] == task_id  # breaches sort first


def _condition(client, pid, code):
    r = client.post("/fhir/Condition", json={
        "resourceType": "Condition",
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10",
                             "code": code}]},
        "subject": {"reference": f"Patient/{pid}"},
        "clinicalStatus": {"coding": [{"code": "active"}]}},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _observation(client, pid, code, value):
    r = client.post("/fhir/Observation", json={
        "resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
        "subject": {"reference": f"Patient/{pid}"},
        "valueQuantity": {"value": value, "unit": "%"},
        "effectiveDateTime": dt.datetime.now(dt.timezone.utc).isoformat()},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text


def test_care_gaps_and_outreach_tracking(client):
    _patient(client, "out-9")
    _condition(client, "out-9", "E11.9")  # diabetes panel, no A1c on file
    _patient(client, "out-10")
    _condition(client, "out-10", "E11.9")
    _observation(client, "out-10", "4548-4", 7.2)  # in range: no gap
    gaps = client.get("/continuum/panels/gaps", headers=CLINICIAN).json()
    mine = [g for g in gaps["items"] if g["gap_code"] == "diabetes-a1c-control"]
    assert [g["patient_ref"] for g in mine] == ["Patient/out-9"]
    assert mine[0]["reason"] == "missing"
    r = client.post("/continuum/panels/gaps/outreach",
                    json={"gap_code": "diabetes-a1c-control",
                          "patient": "out-9"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["existing"] is False
    r = client.post("/continuum/panels/gaps/outreach",
                    json={"gap_code": "diabetes-a1c-control",
                          "patient": "out-9"}, headers=CLINICIAN)
    assert r.json()["existing"] is True  # no duplicate outreach
    gaps = client.get("/continuum/panels/gaps", headers=CLINICIAN).json()
    mine = next(g for g in gaps["items"]
                if g["patient_ref"] == "Patient/out-9")
    assert mine["outreach_task_id"] is not None
    r = client.post("/continuum/panels/gaps/outreach",
                    json={"gap_code": "nope", "patient": "out-9"},
                    headers=CLINICIAN)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Aftercare
# ---------------------------------------------------------------------------

def test_call_queue_and_recording(client):
    _patient(client, "aft-1")
    enc = _admit_bed(client, "aft-1", "MS-105A")
    _discharge(client, enc)
    queue = client.get("/continuum/aftercare/calls", headers=CLINICIAN).json()
    assert queue["total"] == 1
    item = queue["items"][0]
    assert item["encounter_id"] == enc
    assert item["state"] == "scheduled"  # window opens at 48h
    r = client.post("/continuum/aftercare/calls",
                    json={"encounter_id": enc, "outcome": "bogus"},
                    headers=CLINICIAN)
    assert r.status_code == 422  # outcome not in the configured list
    r = client.post("/continuum/aftercare/calls",
                    json={"encounter_id": enc, "outcome": "reached-well",
                          "reached": True}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    queue = client.get("/continuum/aftercare/calls", headers=CLINICIAN).json()
    assert queue["total"] == 0
    _patient(client, "aft-2")
    enc2 = _admit_bed(client, "aft-2", "MS-105B")
    r = client.post("/continuum/aftercare/calls",
                    json={"encounter_id": enc2, "outcome": "reached-well"},
                    headers=CLINICIAN)
    assert r.status_code == 422  # not discharged yet


def test_readmission_risk_deterministic_scoring(client):
    _patient(client, "aft-3", birth="1940-01-01")  # age 86 -> age80Plus (2)
    for _ in range(5):
        _condition(client, "aft-3", "I10")  # 5+ conditions (2)
    enc1 = _admit_bed(client, "aft-3", "MS-106A")
    _discharge(client, enc1)
    # EVS turns the bed around before the next admission.
    r = client.post("/inpatient/beds/MS-106A/status",
                    json={"status": "available"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    enc2 = _admit_bed(client, "aft-3", "MS-106A")
    _discharge(client, enc2)  # + prior admission within 90d (2) = 6
    risks = client.get("/continuum/aftercare/readmission-risks",
                       headers=CLINICIAN).json()
    mine = [i for i in risks["items"]
            if i["patient_ref"] == "Patient/aft-3"]
    assert len(mine) == 1
    assert mine[0]["score"] == 6
    assert set(mine[0]["factors"]) == {"age80Plus", "conditions5Plus",
                                       "priorAdmissions90d"}
    assert "deterministic" in risks["basis"]
    _patient(client, "aft-4", birth="2000-01-01")
    enc = _admit_bed(client, "aft-4", "MS-106B")
    _discharge(client, enc)
    risks = client.get("/continuum/aftercare/readmission-risks",
                       headers=CLINICIAN).json()
    assert not any(i["patient_ref"] == "Patient/aft-4"
                   for i in risks["items"])


def test_hospital_at_home_flow(client):
    _patient(client, "aft-5")
    r = client.post("/continuum/aftercare/hah/enroll",
                    json={"patient": "aft-5"}, headers=CLINICIAN)
    assert r.status_code == 422  # not flagged eligible
    r = client.post("/continuum/aftercare/hah/eligibility",
                    json={"patient": "aft-5", "eligible": True},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post("/continuum/aftercare/hah/enroll",
                    json={"patient": "aft-5"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    hah_enc = r.json()["encounter"]["id"]
    board = client.get("/continuum/aftercare/hah", headers=CLINICIAN).json()
    assert board["total"] == 1
    assert "Patient/aft-5" in board["eligible"]
    assert "Phase 14" in board["note"]  # no fabricated device feeds
    r = client.post(f"/continuum/aftercare/hah/{hah_enc}/visit-task",
                    json={"title": "Nurse visit — wound check"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    board = client.get("/continuum/aftercare/hah", headers=CLINICIAN).json()
    assert board["census"][0]["open_visit_tasks"] == 1
    r = client.post(f"/continuum/aftercare/hah/{hah_enc}/discharge",
                    json={}, headers=CLINICIAN)
    assert r.status_code == 200
    board = client.get("/continuum/aftercare/hah", headers=CLINICIAN).json()
    assert board["total"] == 0


# ---------------------------------------------------------------------------
# Capacity Command Center
# ---------------------------------------------------------------------------

def test_command_overview_from_live_data(client):
    _patient(client, "cc-1")
    enc = _ed_arrival(client, "cc-1")
    _ed_admit(client, enc)  # one boarder
    overview = client.get("/continuum/command/overview",
                          headers=CLINICIAN).json()
    assert overview["boarding_count"] == 1
    assert overview["boarding"]["total"] == 1
    assert overview["boarding"]["items"][0]["patient_ref"] == "Patient/cc-1"
    assert overview["boarding"]["items"][0]["source"] == "ed"
    assert overview["bed_requests"] == 1
    assert overview["census"]["totals"]["beds_total"] == 18
    assert overview["projection"]["projected_occupied_end_of_day"] >= 1
    assert "deterministic" in overview["projection"]["basis"]
    assert overview["diversion"] == "open"


def test_transfer_centre_decisions(client):
    payload = {"patient_name": "External, Erin", "patient_age": 64,
               "patient_sex": "female",
               "referring_facility": "County General",
               "reason": "STEMI — needs cath lab",
               "service_line": "cardiology", "urgency": "stat"}
    r = client.post("/continuum/command/transfers", json=payload,
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    first = r.json()
    r = client.post(f"/continuum/command/transfers/{first['id']}/decide",
                    json={"decision": "decline"}, headers=CLINICIAN)
    assert r.status_code == 422  # decline requires a reason
    r = client.post(f"/continuum/command/transfers/{first['id']}/decide",
                    json={"decision": "decline",
                          "reason": "no ICU capacity"}, headers=CLINICIAN)
    assert r.status_code == 200
    assert r.json()["status"] == "declined"
    r = client.post("/continuum/command/transfers", json=payload,
                    headers=CLINICIAN)
    second = r.json()
    r = client.post(f"/continuum/command/transfers/{second['id']}/decide",
                    json={"decision": "accept"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["admission_task_id"]
    queue = client.get("/continuum/command/bed-requests",
                       headers=CLINICIAN).json()
    transfers = [i for i in queue["items"] if i["kind"] == "transfer"]
    assert len(transfers) == 1
    r = client.post(f"/continuum/command/transfers/{second['id']}/decide",
                    json={"decision": "accept"}, headers=CLINICIAN)
    assert r.status_code == 409  # already decided


def test_transport_dispatch_flow(client):
    _patient(client, "cc-2")
    r = client.post("/continuum/command/transport",
                    json={"patient": "cc-2", "from_location": "ED",
                          "to_location": "CT scanner",
                          "priority": "urgent"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    req = r.json()
    r = client.post(f"/continuum/command/transport/{req['id']}/transition",
                    json={"to_status": "completed"}, headers=CLINICIAN)
    assert r.status_code == 409  # must be assigned / in-transit first
    for to in ("assigned", "in-transit", "completed"):
        r = client.post(
            f"/continuum/command/transport/{req['id']}/transition",
            json={"to_status": to, "transporter": "transport-team-1"},
            headers=CLINICIAN)
        assert r.status_code == 200, r.text
    listing = client.get("/continuum/command/transport",
                         headers=CLINICIAN).json()
    assert listing["open"] == 0


# ---------------------------------------------------------------------------
# Safety & quality operations (Armstrong pattern)
# ---------------------------------------------------------------------------

def test_safety_confidential_shielding(client):
    r = client.post("/continuum/safety/events", json={
        "description": "Wrong-dose near miss on the unit",
        "category": "medication", "harm_level": "near-miss",
        "confidential": True, "unit": "unit-medsurg"}, headers=VIEWER)
    assert r.status_code == 201, r.text  # any staff role may file
    event = r.json()
    assert event["reporter"] == "dev-viewer"  # filer sees their own name
    listing = client.get("/continuum/safety/events", headers=CLINICIAN).json()
    assert listing["items"][0]["reporter"] == "confidential"
    detail = client.get(f"/continuum/safety/events/{event['id']}",
                        headers=CLINICIAN).json()
    assert detail["reporter"] == "confidential"
    own = client.get(f"/continuum/safety/events/{event['id']}",
                     headers=VIEWER)
    assert own.status_code == 200  # the reporter can read their own event
    assert own.json()["reporter"] == "dev-viewer"


def test_safety_triage_followup_and_rca(client):
    r = client.post("/continuum/safety/events", json={
        "description": "Patient fall with serious injury",
        "category": "fall", "harm_level": "permanent",
        "unit": "unit-icu"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    event = r.json()
    assert event["rca_required"] is True  # serious harm auto-opens RCA
    rca = client.get("/continuum/safety/rca", headers=CLINICIAN).json()
    assert rca["total"] == 1
    r = client.post(f"/continuum/safety/events/{event['id']}/triage",
                    json={"status": "in-review",
                          "note": "Please add the staffing context."},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    r = client.post(f"/continuum/safety/events/{event['id']}/messages",
                    json={"body": "Two nurses were covering 14 patients."},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    detail = client.get(f"/continuum/safety/events/{event['id']}",
                        headers=ADMIN).json()
    roles = [m["author_role"] for m in detail["messages"]]
    assert roles == ["reviewer", "reporter"]
    r = client.post(f"/continuum/safety/events/{event['id']}/triage",
                    json={"status": "closed"}, headers=ADMIN)
    assert r.status_code == 200
    rca = client.get("/continuum/safety/rca", headers=CLINICIAN).json()
    assert rca["total"] == 0
    r = client.post(f"/continuum/safety/events/{event['id']}/triage",
                    json={"status": "triaged"}, headers=ADMIN)
    assert r.status_code == 409  # closed is terminal


def test_safety_taxonomy_validation_and_summary(client):
    r = client.post("/continuum/safety/events",
                    json={"description": "x", "category": "bogus"},
                    headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post("/continuum/safety/events",
                    json={"description": "x", "harm_level": "bogus"},
                    headers=CLINICIAN)
    assert r.status_code == 422
    client.post("/continuum/safety/events", json={
        "description": "Equipment alarm missed", "category": "equipment",
        "harm_level": "none", "unit": "unit-icu"}, headers=CLINICIAN)
    summary = client.get("/continuum/safety/summary", headers=CLINICIAN)
    assert summary.status_code == 200
    body = summary.json()
    assert body["total"] == 1 and body["rca_open"] == 0
    assert body["by_category"] == {"equipment": 1}
    assert body["by_unit"] == {"unit-icu": 1}
    r = client.get("/continuum/safety/events", headers=VIEWER)
    assert r.status_code == 403  # triage queue is the safety team's


# ---------------------------------------------------------------------------
# Seeds + capability gates
# ---------------------------------------------------------------------------

def _config_rows(client, kind, key):
    r = client.get(f"/studio/config/{kind}/{key}", headers=ADMIN)
    assert r.status_code == 200, (kind, key, r.text)
    return r.json()


def test_phase12_seeds_present(client):
    rows = _config_rows(client, "terminology", "continuum-ops")
    published = [r for r in rows if r["status"] == "published"]
    assert published
    assert published[0]["payload"]["esiTargetsMinutes"]["2"] == 10
    for form in ("emtala-mse", "emtala-transfer-certification",
                 "handoff-ipass", "discharge-teachback"):
        rows = _config_rows(client, "form", form)
        assert any(r["status"] == "published" for r in rows), form
    for dash in ("discharge-readiness", "unit-safety"):
        rows = _config_rows(client, "dashboard", dash)
        assert any(r["status"] == "published" for r in rows), dash
    rows = _config_rows(client, "rule", "ed-boarding-escalation")
    published = [r for r in rows if r["status"] == "published"]
    assert published
    assert published[0]["payload"]["event"] == "continuum.boarding.changed"
    rows = _config_rows(client, "regulatory_pack", "emtala")
    assert rows[0]["status"] == "draft"  # packs ship as drafts, always
    rows = _config_rows(client, "agent", "safety-triage-assistant")
    assert rows[0]["status"] == "draft"
    assert rows[0]["payload"]["model"] == ""  # honest-off until configured
    reqs = client.get("/compliance/requirements", headers=ADMIN).json()
    pairs = {(r["body"], r["citation"]) for r in reqs["items"]}
    assert ("EMTALA", "42 CFR 489.24(a), (d)") in pairs
    assert ("EMTALA", "42 CFR 489.24(e)") in pairs
    assert ("EMTALA", "42 CFR 489.20(r)(3)") in pairs
    assert ("CMS", "42 CFR 482.43") in pairs
    assert any(body == "The Joint Commission" for body, _ in pairs)


def test_continuum_capability_gates(client):
    for path in ("/continuum/er/board", "/continuum/command/overview",
                 "/continuum/inpatient/discharge-readiness",
                 "/continuum/safety/summary"):
        r = client.get(path, headers=PATIENT)
        assert r.status_code == 403, path
    r = client.get("/continuum/er/board", headers=VIEWER)
    assert r.status_code == 200  # staff read includes the viewer
