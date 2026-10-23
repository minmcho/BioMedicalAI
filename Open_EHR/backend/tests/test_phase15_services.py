"""Phase 15 — support services (BD-15..20, BD-26).

Supply chain / materials, diet office, interpreter services, patient
experience + service recovery, CUSP, HICS incident command, GME.

Fixtures come from conftest.py (fresh SQLite file per test, dev role
tokens). Patients/staff are created per test through the public APIs.
"""
from datetime import datetime, timezone

from tests.conftest import ADMIN, CLINICIAN, CONFIG, VIEWER


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Support", "given": ["Test"]}],
        "gender": "female", "birthDate": "1985-03-04",
    }, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _admit_discharge(client, pid, bed):
    _patient(client, pid)
    r = client.post("/inpatient/admissions",
                    json={"patient": pid, "bed": bed}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    enc_id = r.json()["encounter"]["id"]
    d = client.post(f"/inpatient/encounters/{enc_id}/discharge",
                    json={"disposition": "home"}, headers=CLINICIAN)
    assert d.status_code == 200, d.text
    return enc_id


def _likert_answers(**scores):
    return [{"linkId": link, "answer": [{"valueInteger": value}]}
            for link, value in scores.items()]


# ---------------------------------------------------------------------------
# BD-17 — supply chain
# ---------------------------------------------------------------------------

def test_supply_catalogue_and_seeded_stock(client):
    cat = client.get("/support/supplies/catalogue", headers=VIEWER)
    assert cat.status_code == 200
    codes = {c["code"] for c in cat.json()["catalogue"]["concepts"]}
    assert "gloves-nitrile" in codes
    stock = client.get("/support/supplies/stock", headers=CLINICIAN).json()
    gloves = next(i for i in stock["items"] if i["item_code"] == "gloves-nitrile")
    assert gloves["quantity"] == 100  # seeded from initialStock


def test_requisition_lifecycle_and_par_receipt(client):
    client.post("/support/supplies/par", json={
        "unit": "unit-medsurg", "item_code": "gauze-pads",
        "par_level": 20, "on_hand": 5}, headers=CLINICIAN)
    r = client.post("/support/requisitions", json={
        "unit": "unit-medsurg", "item_code": "gauze-pads", "quantity": 10,
        "case_ref": "case-123 preference card"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["status"] == "requested" and req["case_ref"] == "case-123 preference card"
    # The requester may not approve their own requisition.
    own = client.post(f"/support/requisitions/{req['id']}/approve", headers=CLINICIAN)
    assert own.status_code == 403
    ok = client.post(f"/support/requisitions/{req['id']}/approve", headers=ADMIN)
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    before = next(i for i in client.get("/support/supplies/stock", headers=ADMIN).json()["items"]
                  if i["item_code"] == "gauze-pads")["quantity"]
    ful = client.post(f"/support/requisitions/{req['id']}/fulfil", headers=CLINICIAN)
    assert ful.status_code == 200 and ful.json()["status"] == "fulfilled"
    after = next(i for i in client.get("/support/supplies/stock", headers=ADMIN).json()["items"]
                 if i["item_code"] == "gauze-pads")["quantity"]
    assert after == before - 10
    rec = client.post(f"/support/requisitions/{req['id']}/receive", headers=CLINICIAN)
    assert rec.status_code == 200 and rec.json()["status"] == "received"
    par = client.get("/support/supplies/par", params={"unit": "unit-medsurg"},
                     headers=ADMIN).json()["items"]
    gauze = next(i for i in par if i["item_code"] == "gauze-pads")
    assert gauze["on_hand"] == 15 and gauze["par_level"] == 20


def test_requisition_oversell_409_and_reject(client):
    client.post("/support/supplies/stock", json={
        "item_code": "syringe-10ml", "quantity": 3}, headers=ADMIN)
    r = client.post("/support/requisitions", json={
        "unit": "unit-icu", "item_code": "syringe-10ml", "quantity": 10},
        headers=CLINICIAN)
    req_id = r.json()["id"]
    client.post(f"/support/requisitions/{req_id}/approve", headers=ADMIN)
    over = client.post(f"/support/requisitions/{req_id}/fulfil", headers=CLINICIAN)
    assert over.status_code == 409
    assert "Insufficient central stock" in over.json()["detail"]
    bad = client.post("/support/requisitions", json={
        "unit": "unit-icu", "item_code": "not-a-supply", "quantity": 1},
        headers=CLINICIAN)
    assert bad.status_code == 422
    r2 = client.post("/support/requisitions", json={
        "unit": "unit-icu", "item_code": "syringe-10ml", "quantity": 1},
        headers=CLINICIAN)
    rej = client.post(f"/support/requisitions/{r2.json()['id']}/reject",
                      json={"reason": "duplicate request"}, headers=ADMIN)
    assert rej.status_code == 200 and rej.json()["status"] == "rejected"
    assert rej.json()["reject_reason"] == "duplicate request"


def test_supply_exchange_file_fixture_roundtrip(client):
    r = client.post("/support/requisitions", json={
        "unit": "unit-medsurg", "item_code": "syringe-10ml", "quantity": 2},
        headers=CLINICIAN)
    req_id = r.json()["id"]
    client.post(f"/support/requisitions/{req_id}/approve", headers=ADMIN)
    artefact = client.get("/support/supplies/exchange/export", headers=ADMIN)
    assert artefact.status_code == 200
    body = artefact.json()
    assert body["basis"] == "file-fixture" and body["mode"] == "file"
    assert any(x["id"] == req_id for x in body["requisitions"])
    imp = client.post("/support/supplies/exchange/import", json={
        "confirmations": [{"requisition_id": req_id, "outcome": "fulfilled"}]},
        headers=CLINICIAN)
    assert imp.status_code == 200
    assert imp.json()["basis"] == "file-fixture" and imp.json()["applied"] == 1
    got = client.get("/support/requisitions", headers=ADMIN).json()["items"]
    assert next(x for x in got if x["id"] == req_id)["status"] == "fulfilled"


# ---------------------------------------------------------------------------
# BD-18 — diet office
# ---------------------------------------------------------------------------

def _allergy(client, pid, code="peanut", display="Peanut"):
    r = client.post("/fhir/AllergyIntolerance", json={
        "resourceType": "AllergyIntolerance", "id": f"alg-{pid}",
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"coding": [{"code": code, "display": display}], "text": display},
        "patient": {"reference": f"Patient/{pid}"},
    }, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def test_diet_order_census_and_tray_allergy_hard_stop(client):
    _patient(client, "pat-diet")
    _allergy(client, "pat-diet")
    bad = client.post("/support/diet/orders", json={
        "patient": "pat-diet", "diet_type": "keto-deluxe"}, headers=CLINICIAN)
    assert bad.status_code == 422
    r = client.post("/support/diet/orders", json={
        "patient": "pat-diet", "diet_type": "diabetic", "texture": "pureed",
        "unit": "unit-medsurg", "notes": "carb count"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    order = r.json()
    census = client.get("/support/diet/census", headers=CLINICIAN).json()
    entry = next(i for i in census["items"] if i["diet_type"] == "diabetic")
    assert entry["unit"] == "unit-medsurg" and entry["count"] == 1
    # Tray containing the allergen: HARD STOP naming it.
    stop = client.post(f"/support/diet/orders/{order['id']}/tray-check",
                       json={"items": ["peanut", "rice"]}, headers=CLINICIAN)
    assert stop.status_code == 422
    assert "Peanut" in stop.json()["detail"]
    # A clean tray is recorded and marks the order tray-checked.
    clean = client.post(f"/support/diet/orders/{order['id']}/tray-check",
                        json={"items": ["rice", "steamed vegetables"]},
                        headers=CLINICIAN)
    assert clean.status_code == 200 and clean.json()["tray_checked"] is True


def test_diet_census_derives_unit_from_bed(client):
    _patient(client, "pat-diet2")
    adm = client.post("/inpatient/admissions",
                      json={"patient": "pat-diet2", "bed": "MS-101A"},
                      headers=CLINICIAN)
    assert adm.status_code == 201, adm.text
    client.post("/support/diet/orders", json={
        "patient": "pat-diet2", "diet_type": "regular"}, headers=CLINICIAN)
    census = client.get("/support/diet/census", headers=CLINICIAN).json()
    entry = next(i for i in census["items"] if i["diet_type"] == "regular")
    assert entry["unit"] == "unit-medsurg" and entry["unit_source"] == "bed"


# ---------------------------------------------------------------------------
# BD-19 — interpreter services
# ---------------------------------------------------------------------------

def test_interpreter_lifecycle_video_join_and_metrics(client):
    _patient(client, "pat-int")
    bad = client.post("/support/interpreter/requests", json={
        "patient": "pat-int", "language": "klingon", "modality": "video"},
        headers=CLINICIAN)
    assert bad.status_code == 422
    r = client.post("/support/interpreter/requests", json={
        "patient": "pat-int", "language": "spanish", "modality": "video",
        "needed_by": "2026-10-19T09:00:00Z"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["status"] == "requested"
    early = client.post(f"/support/interpreter/requests/{req['id']}/complete",
                        headers=CLINICIAN)
    assert early.status_code == 409
    asg = client.post(f"/support/interpreter/requests/{req['id']}/assign",
                      json={"interpreter_name": "Maria Lopez",
                            "agency": "LanguageLine (fixture)"}, headers=CLINICIAN)
    assert asg.status_code == 200 and asg.json()["status"] == "assigned"
    # Video join renders from the published telehealth provider template.
    assert asg.json()["join_url"] == \
        f"https://video.example.invalid/opencare/{req['id']}"
    assert "external-provider" in asg.json()["join_basis"]
    client.post(f"/support/interpreter/requests/{req['id']}/start", headers=CLINICIAN)
    done = client.post(f"/support/interpreter/requests/{req['id']}/complete",
                       headers=CLINICIAN)
    assert done.status_code == 200 and done.json()["status"] == "completed"
    metrics = client.get("/support/interpreter/metrics", headers=CLINICIAN).json()
    item = next(i for i in metrics["items"] if i["id"] == req["id"])
    assert item["request_to_assign_minutes"] is not None
    assert item["request_to_complete_minutes"] >= item["request_to_assign_minutes"]
    assert metrics["mean_request_to_assign_minutes"] == item["request_to_assign_minutes"]


# ---------------------------------------------------------------------------
# BD-20 — patient experience + service recovery
# ---------------------------------------------------------------------------

def test_discharge_survey_low_score_recovery_lifecycle(client):
    _admit_discharge(client, "pat-exp", "MS-101A")
    dist = client.post("/support/experience/distribute", headers=CLINICIAN)
    assert dist.status_code == 200 and dist.json()["created"] == 1
    again = client.post("/support/experience/distribute", headers=CLINICIAN)
    assert again.json()["created"] == 0  # idempotent per discharge
    instance = dist.json()["instances"][0]
    assert instance["patient"] == "Patient/pat-exp"
    resp = client.post(
        f"/support/experience/instances/{instance['id']}/respond",
        json={"answers": _likert_answers(**{
            "overall-rating": 1, "nurse-communication": 5})}, headers=CLINICIAN)
    assert resp.status_code == 201, resp.text
    assert resp.json()["response_id"]
    recovery = resp.json()["recovery_item"]
    assert recovery is not None and recovery["status"] == "open"
    assert recovery["detail"]["low_scores"] == {"overall-rating": 1}
    dup = client.post(
        f"/support/experience/instances/{instance['id']}/respond",
        json={"answers": _likert_answers(**{"overall-rating": 3,
                                            "nurse-communication": 3})},
        headers=CLINICIAN)
    assert dup.status_code == 409
    # Summary is small-cell suppressed below 5 responses.
    summary = client.get("/support/experience/summary", headers=CLINICIAN).json()
    assert summary["suppressed"] is True and summary["response_count"] == "<5"
    # Recovery lifecycle: resolve requires a note.
    rid = recovery["id"]
    no_note = client.post(f"/support/experience/recovery/{rid}/transition",
                          json={"to_status": "resolved"}, headers=CLINICIAN)
    assert no_note.status_code == 422
    client.post(f"/support/experience/recovery/{rid}/transition",
                json={"to_status": "contacted"}, headers=CLINICIAN)
    done = client.post(f"/support/experience/recovery/{rid}/transition",
                       json={"to_status": "resolved",
                             "resolution_note": "Called patient; service plan agreed"},
                       headers=CLINICIAN)
    assert done.status_code == 200 and done.json()["status"] == "resolved"


def test_experience_summary_exact_means_at_five_responses(client):
    beds = ["MS-101A", "MS-101B", "MS-102A", "MS-102B", "MS-103A"]
    for i, bed in enumerate(beds):
        _admit_discharge(client, f"pat-s{i}", bed)
    client.post("/support/experience/distribute", headers=CLINICIAN)
    instances = client.get("/support/experience/instances",
                           headers=CLINICIAN).json()["items"]
    assert len(instances) == 5
    for inst in instances:
        r = client.post(
            f"/support/experience/instances/{inst['id']}/respond",
            json={"answers": _likert_answers(**{
                "overall-rating": 4, "nurse-communication": 5})},
            headers=CLINICIAN)
        assert r.status_code == 201, r.text
        assert r.json()["recovery_item"] is None  # no low scores
    summary = client.get("/support/experience/summary", headers=CLINICIAN).json()
    assert summary["suppressed"] is False and summary["response_count"] == 5
    overall = next(q for q in summary["questions"] if q["link_id"] == "overall-rating")
    assert overall["mean"] == 4.0 and overall["responses"] == 5


def test_complaint_opens_recovery_item(client):
    _patient(client, "pat-comp")
    r = client.post("/support/experience/complaints", json={
        "patient": "pat-comp", "description": "Billing confusion at discharge"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["source"] == "complaint" and r.json()["status"] == "open"
    queue = client.get("/support/experience/recovery",
                       params={"status": "open"}, headers=CLINICIAN).json()
    assert queue["total"] == 1


# ---------------------------------------------------------------------------
# BD-15 — CUSP
# ---------------------------------------------------------------------------

def test_cusp_teams_meetings_defects_worklist(client):
    team = client.post("/support/cusp/teams", json={
        "unit": "unit-icu", "members": ["Priya Nair, RN", "James Okafor, MD"],
        "facilitator": "Grace Liu, RN"}, headers=CLINICIAN)
    assert team.status_code == 201, team.text
    meeting = client.post("/support/cusp/meetings", json={
        "unit": "unit-icu", "date": "2026-10-18",
        "attendees": ["Priya Nair, RN"], "topics": "Line infections",
        "decisions": "Adopt insertion checklist"}, headers=CLINICIAN)
    assert meeting.status_code == 201
    # A defect linked to a non-existent safety event is refused.
    bad = client.post("/support/cusp/defects", json={
        "unit": "unit-icu", "description": "x", "safety_event_id": "nope"},
        headers=CLINICIAN)
    assert bad.status_code == 422
    event = client.post("/continuum/safety/events", json={
        "description": "Central line dressing found undated",
        "category": "procedure", "harm_level": "none", "unit": "unit-icu"},
        headers=CLINICIAN)
    assert event.status_code == 201, event.text
    defect = client.post("/support/cusp/defects", json={
        "unit": "unit-icu", "description": "Dressing-change dates missing",
        "safety_event_id": event.json()["id"],
        "follow_up_due": "2020-01-01"}, headers=CLINICIAN)
    assert defect.status_code == 201, defect.text
    worklist = client.get("/support/cusp/worklist", params={"unit": "unit-icu"},
                          headers=CLINICIAN).json()
    assert worklist["total"] == 1 and worklist["overdue"] == 1
    assert worklist["items"][0]["follow_up_overdue"] is True
    shared = client.post(f"/support/cusp/defects/{defect.json()['id']}/transition",
                         json={"to_status": "learning-shared"}, headers=CLINICIAN)
    assert shared.status_code == 200
    closed = client.post(f"/support/cusp/defects/{defect.json()['id']}/transition",
                         json={"to_status": "closed"}, headers=CLINICIAN)
    assert closed.status_code == 200
    assert client.get("/support/cusp/worklist", headers=CLINICIAN).json()["total"] == 0


# ---------------------------------------------------------------------------
# BD-16 — HICS drill
# ---------------------------------------------------------------------------

def _downtime(client):
    r = client.get("/studio/config/integration/effective/downtime", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["payload"]


def test_hics_drill_activates_and_restores_downtime(client):
    assert _downtime(client).get("enabled") is not True
    bad_role = client.post("/support/hics/activate", json={
        "incident_name": "Drill", "incident_type": "fire",
        "assignments": {"supreme-leader": "staff-001"}}, headers=ADMIN)
    assert bad_role.status_code == 422
    bad_staff = client.post("/support/hics/activate", json={
        "incident_name": "Drill", "incident_type": "fire",
        "assignments": {"incident-commander": "staff-999"}}, headers=ADMIN)
    assert bad_staff.status_code == 422
    act = client.post("/support/hics/activate", json={
        "incident_name": "Exercise Northwind", "incident_type": "earthquake drill",
        "assignments": {"incident-commander": "staff-001",
                        "operations": "staff-002",
                        "safety-officer": "staff-003"}}, headers=ADMIN)
    assert act.status_code == 201, act.text
    activation = act.json()
    # Downtime flag is ON, with the incident notice.
    down = _downtime(client)
    assert down["enabled"] is True
    assert "Exercise Northwind" in down["notice"]
    # Log entries + a situation report land under the activation.
    log = client.post(f"/support/hics/{activation['id']}/log",
                      json={"text": "EOC opened; all sections reporting"},
                      headers=CLINICIAN)
    assert log.status_code == 201
    sitrep = client.post(f"/support/hics/{activation['id']}/situation-reports",
                         json={"text": "Sitrep 1: census stable, no casualties"},
                         headers=CLINICIAN)
    assert sitrep.status_code == 201
    detail = client.get(f"/support/hics/{activation['id']}", headers=CLINICIAN).json()
    kinds = [r["kind"] for r in detail["records"]]
    assert "log" in kinds and "situation-report" in kinds
    # A second activation while one is active is refused.
    again = client.post("/support/hics/activate", json={
        "incident_name": "Second", "incident_type": "fire",
        "assignments": {"incident-commander": "staff-001"}}, headers=ADMIN)
    assert again.status_code == 409
    # Deactivation restores the downtime flag to off.
    deact = client.post(f"/support/hics/{activation['id']}/deactivate",
                        headers=ADMIN)
    assert deact.status_code == 200 and deact.json()["status"] == "deactivated"
    down = _downtime(client)
    assert down["enabled"] is False
    assert down["readOnly"] is False and down["message"] == ""  # prior payload restored


# ---------------------------------------------------------------------------
# BD-26 — GME
# ---------------------------------------------------------------------------

def test_gme_duty_hours_computed_and_violations(client):
    # 30-hour continuous shift: violates max-continuous (24).
    r = client.post("/support/gme/duty-hours", json={
        "trainee_staff_id": "staff-001",
        "start": "2026-01-05T06:00:00Z", "end": "2026-01-06T12:00:00Z"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["hours"] == 30.0  # computed server-side
    bad = client.post("/support/gme/duty-hours", json={
        "trainee_staff_id": "staff-999",
        "start": "2026-01-05T06:00:00Z", "end": "2026-01-05T07:00:00Z"},
        headers=CLINICIAN)
    assert bad.status_code == 422
    violations = client.get("/support/gme/violations", headers=CLINICIAN).json()
    rules = {v["rule"] for v in violations["items"]}
    assert "max-continuous-hours" in rules
    cont = next(v for v in violations["items"] if v["rule"] == "max-continuous-hours")
    assert cont["actual"] == 30.0 and cont["limit"] == 24.0


def test_gme_weekly_and_rest_violations_exact(client):
    # Five 17-hour shifts in one ISO week = 85h (> 80), with 7h gaps (< 8 rest).
    for day in range(5, 10):  # Mon 2026-01-05 .. Fri 2026-01-09
        r = client.post("/support/gme/duty-hours", json={
            "trainee_staff_id": "staff-002",
            "start": f"2026-01-{day:02d}T06:00:00Z",
            "end": f"2026-01-{day:02d}T23:00:00Z"}, headers=CLINICIAN)
        assert r.status_code == 201, r.text
        assert r.json()["hours"] == 17.0
    violations = client.get("/support/gme/violations", headers=CLINICIAN).json()
    mine = [v for v in violations["items"] if v["trainee"] == "staff-002"]
    weekly = next(v for v in mine if v["rule"] == "max-weekly-hours")
    assert weekly["actual"] == 85.0 and weekly["limit"] == 80.0
    rests = [v for v in mine if v["rule"] == "min-rest-hours"]
    assert rests and all(v["actual"] == 7.0 and v["limit"] == 8.0 for v in rests)


def test_gme_evaluation_and_rotations(client):
    ev = client.post("/support/gme/evaluations", json={
        "trainee_staff_id": "staff-001",
        "answers": [{"linkId": "clinical-competence",
                     "answer": [{"valueInteger": 4}]},
                    {"linkId": "professionalism",
                     "answer": [{"valueInteger": 5}]}]}, headers=CLINICIAN)
    assert ev.status_code == 201, ev.text
    assert ev.json()["response_id"]
    missing = client.post("/support/gme/evaluations", json={
        "trainee_staff_id": "staff-001",
        "answers": [{"linkId": "clinical-competence",
                     "answer": [{"valueInteger": 4}]}]}, headers=CLINICIAN)
    assert missing.status_code == 422  # required form item unanswered
    rot = client.post("/support/gme/rotations", json={
        "trainee_staff_id": "staff-001", "service_line": "internal-medicine",
        "start_date": "2026-11-01", "end_date": "2026-11-30"}, headers=CLINICIAN)
    assert rot.status_code == 201, rot.text
    overlap = client.post("/support/gme/rotations", json={
        "trainee_staff_id": "staff-001", "service_line": "cardiology",
        "start_date": "2026-11-15", "end_date": "2026-12-15"}, headers=CLINICIAN)
    assert overlap.status_code == 409
    fine = client.post("/support/gme/rotations", json={
        "trainee_staff_id": "staff-001", "service_line": "cardiology",
        "start_date": "2026-12-01", "end_date": "2026-12-31"}, headers=CLINICIAN)
    assert fine.status_code == 201


def test_support_read_gate_allows_viewer_work_gate_refuses(client):
    assert client.get("/support/supplies/stock", headers=VIEWER).status_code == 200
    assert client.post("/support/requisitions", json={
        "unit": "unit-icu", "item_code": "gauze-pads", "quantity": 1},
        headers=VIEWER).status_code == 403
