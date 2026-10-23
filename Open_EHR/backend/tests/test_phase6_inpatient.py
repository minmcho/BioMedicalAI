"""Phase 6 — inpatient operations.

Locations & beds materialised from Studio terminology config, the ADT
event system (A01 admit / A02 transfer / A03 discharge / A08 update),
census + bed board, case management + discharge planning, and
anaesthesia / perioperative depth on the Phase 4B surgery board.

Fixtures come from conftest.py (fresh SQLite file per test, dev role
tokens). Patients are created per test through the FHIR API.
"""
from datetime import date, datetime, timedelta, timezone

from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER

# "Today" in UTC: the server's date defaults are UTC (e.g.
# /inpatient/expected-discharges "defaults to today, UTC"), so a
# local-zone date here made this module fail whenever the local and
# UTC dates differed (17:00-24:00 PDT). Parent gate, 2026-10-18.
TODAY = datetime.now(timezone.utc).date().isoformat()


def _patient(client, pid="pat-9", family="Inpatient"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": family, "given": ["Test"]}],
        "gender": "female", "birthDate": "1980-05-01",
    }, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _ensure_patient(client, pid):
    if client.get(f"/fhir/Patient/{pid}",
                  headers=CLINICIAN).status_code == 404:
        _patient(client, pid)


def _admit(client, patient="pat-1", bed="MS-101A", headers=CLINICIAN):
    _ensure_patient(client, patient.split("/")[-1])
    r = client.post("/inpatient/admissions",
                    json={"patient": patient, "bed": bed}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _bed(client, code, headers=CLINICIAN):
    beds = client.get("/inpatient/beds", headers=headers).json()["beds"]
    return next(b for b in beds if b["code"] == code)


def _unit(client, code, headers=CLINICIAN):
    units = client.get("/inpatient/census", headers=headers).json()["units"]
    return next(u for u in units if u["code"] == code)


def _events(client, encounter_id, headers=CLINICIAN):
    r = client.get("/inpatient/adt-events",
                   params={"encounter_id": encounter_id}, headers=headers)
    assert r.status_code == 200, r.text
    return list(reversed(r.json()["events"]))  # chronological


def _publish_config(client, kind, key, payload, headers=CONFIG):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload,
                    headers=headers)
    assert r.status_code in (200, 201), r.text
    body = r.json()
    version = body.get("version") if isinstance(body, dict) else None
    if version is not None:
        pub = client.post(
            f"/studio/config/{kind}/{key}/versions/{version}/publish",
            headers=headers)
        assert pub.status_code == 200, pub.text
    return body


# ---------------------------------------------------------------------------
# Locations & beds as data
# ---------------------------------------------------------------------------

def test_locations_materialised_from_published_config(client):
    # 1 facility + 2 units + 18 beds = 21 Location resources exist as
    # real FHIR rows, materialised from the locations config.
    r = client.get("/fhir/Location/MS-101A", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Med-Surg Room 101 Bed A"
    assert body["partOf"]["reference"] == "Location/unit-medsurg"
    icu = client.get("/fhir/Location/ICU-204", headers=CLINICIAN).json()
    assert icu["partOf"]["reference"] == "Location/unit-icu"
    listing = client.get("/inpatient/locations", headers=CLINICIAN).json()
    assert listing["version"] == 1
    assert listing["bed_statuses"] == ["available", "occupied",
                                       "cleaning", "blocked"]
    beds = [loc for loc in listing["locations"] if loc["type"] == "bed"]
    assert len(beds) == 18
    assert all(b["status"] == "available" for b in beds)


def test_new_unit_added_via_config_appears_without_code_change(client):
    current = client.get("/inpatient/locations", headers=CLINICIAN).json()
    payload = client.get("/studio/config/terminology/locations",
                         headers=ADMIN).json()
    published = next(v for v in payload if v["status"] == "published")
    new_payload = dict(published["payload"])
    concepts = list(new_payload["concepts"])
    concepts.append({"code": "unit-stepdown", "display": "Step-Down Unit",
                     "type": "unit", "parent": "facility-main",
                     "active": True})
    for code in ("SD-301A", "SD-301B"):
        concepts.append({"code": code, "display": f"Step-Down Bed {code}",
                         "type": "bed", "parent": "unit-stepdown",
                         "room": "301", "bed": code[-1], "active": True})
    new_payload["concepts"] = concepts
    _publish_config(client, "terminology", "locations", new_payload)

    unit = _unit(client, "unit-stepdown")
    assert unit["beds_total"] == 2 and unit["available"] == 2
    # And the new beds are fully operational: admit into one.
    _patient(client, "pat-sd")
    admitted = _admit(client, patient="pat-sd", bed="SD-301A")
    assert admitted["bed"]["unit"] == "unit-stepdown"
    assert _unit(client, "unit-stepdown")["occupied"] == 1


# ---------------------------------------------------------------------------
# ADT: admit / transfer / discharge
# ---------------------------------------------------------------------------

def test_admit_to_occupied_bed_is_409(client):
    _patient(client, "pat-a")
    _patient(client, "pat-b")
    _admit(client, patient="pat-a", bed="MS-101A")
    r = client.post("/inpatient/admissions",
                    json={"patient": "pat-b", "bed": "MS-101A"},
                    headers=CLINICIAN)
    assert r.status_code == 409
    assert "occupied" in r.json()["detail"]
    # A blocked bed refuses too, and an unknown bed is a 422.
    client.post("/inpatient/beds/MS-102A/status",
                json={"status": "blocked"}, headers=CLINICIAN)
    r = client.post("/inpatient/admissions",
                    json={"patient": "pat-b", "bed": "MS-102A"},
                    headers=CLINICIAN)
    assert r.status_code == 409 and "blocked" in r.json()["detail"]
    r = client.post("/inpatient/admissions",
                    json={"patient": "pat-b", "bed": "XX-999"},
                    headers=CLINICIAN)
    assert r.status_code == 422
    # Double-admitting the same patient is refused (already has a bed).
    r = client.post("/inpatient/admissions",
                    json={"patient": "pat-a", "bed": "MS-103A"},
                    headers=CLINICIAN)
    assert r.status_code == 409


def test_transfer_frees_old_bed_to_cleaning_and_event_sequence(client):
    admitted = _admit(client, patient="pat-1", bed="MS-101A")
    enc_id = admitted["encounter"]["id"]
    r = client.post(f"/inpatient/encounters/{enc_id}/transfer",
                    json={"bed": "ICU-201"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["event"]["event_type"] == "A02"
    assert _bed(client, "MS-101A")["status"] == "cleaning"
    assert _bed(client, "ICU-201")["status"] == "occupied"
    assert _bed(client, "ICU-201")["encounter_id"] == enc_id
    # Transfer onto the current bed / an unknown encounter is refused.
    assert client.post(f"/inpatient/encounters/{enc_id}/transfer",
                       json={"bed": "ICU-201"},
                       headers=CLINICIAN).status_code == 422

    r = client.post(f"/inpatient/encounters/{enc_id}/discharge",
                    json={"disposition": "home"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert _bed(client, "ICU-201")["status"] == "cleaning"

    events = _events(client, enc_id)
    assert [e["event_type"] for e in events] == ["A01", "A02", "A03"]
    assert events[0]["to_location"] == "MS-101A"
    assert events[1]["from_location"] == "MS-101A"
    assert events[1]["to_location"] == "ICU-201"
    assert events[2]["from_location"] == "ICU-201"
    # Event labels are the published config's displays.
    assert events[0]["event_display"] == "Admit / visit notification"
    assert events[2]["detail"]["disposition"] == "home"


def test_discharge_closes_encounter_and_releases_bed(client):
    admitted = _admit(client, patient="pat-1", bed="MS-104B")
    enc_id = admitted["encounter"]["id"]
    r = client.post(f"/inpatient/encounters/{enc_id}/discharge",
                    json={}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    encounter = client.get(f"/fhir/Encounter/{enc_id}",
                           headers=CLINICIAN).json()
    assert encounter["status"] == "finished"
    assert encounter["period"]["end"]
    locations = encounter["location"]
    assert locations[0]["status"] == "completed"
    assert locations[0]["period"]["end"]
    assert _bed(client, "MS-104B")["status"] == "cleaning"
    # A second discharge is an illegal transition.
    assert client.post(f"/inpatient/encounters/{enc_id}/discharge",
                       json={}, headers=CLINICIAN).status_code == 409
    # The patient no longer has a current admission.
    current = client.get("/inpatient/patients/pat-1/admission",
                         headers=CLINICIAN).json()
    assert current["admission"] is None


def test_bed_status_flip_rules(client):
    admitted = _admit(client, patient="pat-1", bed="MS-101A")
    enc_id = admitted["encounter"]["id"]
    # An occupied bed cannot be flipped bare; occupancy is never set bare.
    assert client.post("/inpatient/beds/MS-101A/status",
                       json={"status": "available"},
                       headers=CLINICIAN).status_code == 409
    assert client.post("/inpatient/beds/MS-101B/status",
                       json={"status": "occupied"},
                       headers=CLINICIAN).status_code == 422
    assert client.post("/inpatient/beds/MS-101B/status",
                       json={"status": "bogus"},
                       headers=CLINICIAN).status_code == 422
    # Discharge -> cleaning; housekeeping flips cleaning -> available.
    client.post(f"/inpatient/encounters/{enc_id}/discharge",
                json={}, headers=CLINICIAN)
    assert _bed(client, "MS-101A")["status"] == "cleaning"
    r = client.post("/inpatient/beds/MS-101A/status",
                    json={"status": "available"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["bed"]["status"] == "available"
    # available <-> blocked both ways.
    assert client.post("/inpatient/beds/MS-101A/status",
                       json={"status": "blocked"},
                       headers=CLINICIAN).status_code == 200
    assert client.post("/inpatient/beds/MS-101A/status",
                       json={"status": "available"},
                       headers=CLINICIAN).status_code == 200


def test_bed_status_changes_are_audited(client):
    _admit(client, patient="pat-1", bed="MS-101A")
    entries = client.get("/studio/audit", headers=ADMIN).json()
    actions = [e["action"] for e in entries]
    assert "inpatient.bed.status" in actions
    assert "inpatient.adt.A01" in actions


# ---------------------------------------------------------------------------
# Census math
# ---------------------------------------------------------------------------

def test_census_math_on_seeded_ward(client):
    for pid in ("pat-c1", "pat-c2", "pat-c3", "pat-c4"):
        _patient(client, pid)
    _admit(client, patient="pat-c1", bed="MS-101A")
    _admit(client, patient="pat-c2", bed="MS-101B")
    _admit(client, patient="pat-c3", bed="MS-102A")
    _admit(client, patient="pat-c4", bed="ICU-201")
    client.post("/inpatient/beds/MS-106B/status",
                json={"status": "blocked"}, headers=CLINICIAN)

    census = client.get("/inpatient/census", headers=CLINICIAN).json()
    medsurg = next(u for u in census["units"] if u["code"] == "unit-medsurg")
    assert medsurg["beds_total"] == 12
    assert medsurg["occupied"] == 3
    assert medsurg["available"] == 8
    assert medsurg["blocked"] == 1
    assert medsurg["cleaning"] == 0
    assert medsurg["occupancy_pct"] == 25.0
    icu = next(u for u in census["units"] if u["code"] == "unit-icu")
    assert icu["beds_total"] == 6 and icu["occupied"] == 1
    assert icu["available"] == 5 and icu["occupancy_pct"] == 16.7
    totals = census["totals"]
    assert totals["beds_total"] == 18 and totals["occupied"] == 4
    assert totals["available"] == 13 and totals["blocked"] == 1
    assert totals["occupancy_pct"] == 22.2
    # Patient-per-bed listing carries the occupant + LOS facts.
    bed = _bed(client, "MS-101A")
    assert bed["patient_ref"] == "Patient/pat-c1"
    assert bed["los_days"] == 0.0 and bed["admitted_at"]


# ---------------------------------------------------------------------------
# Discharge planning + case management
# ---------------------------------------------------------------------------

def test_discharge_checklist_form_submit_and_expected_discharges(client):
    admitted = _admit(client, patient="pat-1", bed="MS-103A")
    enc_id = admitted["encounter"]["id"]
    # The checklist is a published Studio form; submit it the way the
    # web FormRenderer does (a QuestionnaireResponse via the FHIR API).
    answers = [
        {"linkId": "disposition",
         "answer": [{"valueCoding": {"code": "snf",
                                     "display": "Skilled nursing facility"}}]},
        {"linkId": "target-discharge-date",
         "answer": [{"valueDate": TODAY}]},
        {"linkId": "meds-reconciled", "answer": [{"valueBoolean": True}]},
        {"linkId": "transport-arranged", "answer": [{"valueBoolean": True}]},
        {"linkId": "barriers", "answer": [{"valueString": "SNF bed pending"}]},
    ]
    qr = client.post("/fhir/QuestionnaireResponse", json={
        "resourceType": "QuestionnaireResponse",
        "questionnaire": "Questionnaire/discharge-checklist",
        "status": "completed",
        "subject": {"reference": "Patient/pat-1"},
        "encounter": {"reference": f"Encounter/{enc_id}"},
        "item": [{"linkId": a["linkId"], "answer": a["answer"]}
                 for a in answers],
    }, headers=CLINICIAN)
    assert qr.status_code == 201, qr.text
    # Record the plan (target date + disposition + checklist linkage).
    r = client.post(f"/inpatient/encounters/{enc_id}/discharge-plan",
                    json={"targetDate": TODAY, "disposition": "snf",
                          "barriers": "SNF bed pending",
                          "checklistResponseId": qr.json()["id"]},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["event"]["event_type"] == "A08"
    # The expected-discharge feed (explicit date and default-today).
    for params in ({"date": TODAY}, {}):
        feed = client.get("/inpatient/expected-discharges", params=params,
                          headers=CLINICIAN).json()
        assert feed["total"] == 1
        entry = feed["discharges"][0]
        assert entry["encounter_id"] == enc_id
        assert entry["patient_ref"] == "Patient/pat-1"
        assert entry["bed_code"] == "MS-103A"
        assert entry["disposition"] == "snf"
    # Census embeds today's expected discharges.
    census = client.get("/inpatient/census", headers=CLINICIAN).json()
    assert census["expected_discharges_today"]["total"] == 1
    # A disposition outside the published value set is refused, and a
    # discharge falls back to the plan's disposition.
    assert client.post(f"/inpatient/encounters/{enc_id}/discharge-plan",
                       json={"disposition": "moon"},
                       headers=CLINICIAN).status_code == 422
    done = client.post(f"/inpatient/encounters/{enc_id}/discharge",
                       json={}, headers=CLINICIAN)
    assert done.status_code == 200
    assert done.json()["disposition"] == "snf"


def test_care_plan_task_lifecycle(client):
    admitted = _admit(client, patient="pat-1", bed="MS-105A")
    enc_id = admitted["encounter"]["id"]
    r = client.post(f"/inpatient/encounters/{enc_id}/care-plan",
                    json={"description": "Arrange SNF placement"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    task = r.json()["task"]
    assert task["status"] == "requested"
    assert task["encounter"]["reference"] == f"Encounter/{enc_id}"
    listed = client.get(f"/inpatient/encounters/{enc_id}/care-plan",
                        headers=CLINICIAN).json()
    assert listed["total"] == 1
    tid = task["id"]
    url = f"/inpatient/care-plan/{tid}/transition"
    assert client.post(url, json={"to": "completed"},
                       headers=CLINICIAN).status_code == 409  # must start first
    assert client.post(url, json={"to": "in-progress"},
                       headers=CLINICIAN).status_code == 200
    done = client.post(url, json={"to": "completed"}, headers=CLINICIAN)
    assert done.status_code == 200
    assert done.json()["task"]["status"] == "completed"
    assert client.post(url, json={"to": "in-progress"},
                       headers=CLINICIAN).status_code == 409  # terminal


def test_dispositions_come_from_published_config(client):
    r = client.get("/inpatient/discharge-dispositions", headers=CLINICIAN)
    assert r.status_code == 200
    codes = [d["code"] for d in r.json()["dispositions"]]
    assert codes == ["home", "home-health", "snf", "rehab", "ltach", "hospice"]


# ---------------------------------------------------------------------------
# ED admit -> bed assignment (the Phase 4B linkage completed)
# ---------------------------------------------------------------------------

def test_ed_admitted_encounter_gets_bed_via_assign_bed(client):
    _patient(client, "pat-ed6")
    arrival = client.post("/ed/arrivals",
                          json={"patient": "pat-ed6",
                                "chief_complaint": "Sepsis"},
                          headers=CLINICIAN)
    assert arrival.status_code == 201, arrival.text
    ed_id = arrival.json()["id"]
    for target in ("triage", "in-treatment", "disposition"):
        body = {"to_status": target}
        if target == "triage":
            body["acuity"] = 2
        r = client.post(f"/ed/encounters/{ed_id}/transition", json=body,
                        headers=CLINICIAN)
        assert r.status_code == 200, r.text
    admitted = client.post(f"/ed/encounters/{ed_id}/admit", json={},
                           headers=CLINICIAN)
    assert admitted.status_code == 200, admitted.text
    imp_id = admitted.json()["inpatient_encounter"]["id"]

    current = client.get("/inpatient/patients/pat-ed6/admission",
                         headers=CLINICIAN).json()["admission"]
    assert current["encounter"]["id"] == imp_id
    assert current["bed_code"] is None

    r = client.post(f"/inpatient/encounters/{imp_id}/assign-bed",
                    json={"bed": "ICU-202"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["event"]["event_type"] == "A01"
    assert _bed(client, "ICU-202")["encounter_id"] == imp_id
    # A second assignment is refused — transfers move beds.
    assert client.post(f"/inpatient/encounters/{imp_id}/assign-bed",
                       json={"bed": "ICU-203"},
                       headers=CLINICIAN).status_code == 409


# ---------------------------------------------------------------------------
# Anaesthesia + perioperative depth on the surgery board
# ---------------------------------------------------------------------------

def _case(client, pid="pat-1"):
    _ensure_patient(client, pid)
    start = datetime.now(timezone.utc).replace(microsecond=0)
    r = client.post("/surgery/cases", json={
        "patient_id": pid, "code": "27447", "display": "Total knee replacement",
        "room": "OR-1", "start": start.isoformat(),
        "end": (start + timedelta(hours=2)).isoformat(),
    }, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["case"]["id"]


def _move_case(client, case_id, target):
    r = client.post(f"/surgery/cases/{case_id}/transition",
                    json={"to_status": target}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


ANAESTHESIA_ANSWERS = [
    {"linkId": "anaesthesia-type",
     "answer": [{"valueCoding": {"code": "general", "display": "General"}}]},
    {"linkId": "airway",
     "answer": [{"valueCoding": {"code": "ett",
                                 "display": "Endotracheal tube"}}]},
    {"linkId": "agents",
     "answer": [{"valueString": "Propofol, rocuronium, sevoflurane"}]},
    {"linkId": "fluids", "answer": [{"valueString": "LR 1000 mL"}]},
    {"linkId": "estimated-blood-loss-ml", "answer": [{"valueInteger": 150}]},
]

VITALS_ANSWERS = [
    {"linkId": "systolic", "answer": [{"valueInteger": 118}]},
    {"linkId": "heart-rate", "answer": [{"valueInteger": 72}]},
    {"linkId": "spo2", "answer": [{"valueInteger": 99}]},
]


def test_anaesthesia_flowsheet_attach_and_case_timeline(client):
    case_id = _case(client)
    # Pre-op checklist (published form) attaches to the case.
    r = client.post(f"/inpatient/surgery-cases/{case_id}/preop-checklist",
                    json={"answers": [
                        {"linkId": "consent-signed",
                         "answer": [{"valueBoolean": True}]},
                        {"linkId": "site-marked",
                         "answer": [{"valueBoolean": True}]},
                        {"linkId": "allergies-reviewed",
                         "answer": [{"valueBoolean": True}]},
                        {"linkId": "npo-confirmed",
                         "answer": [{"valueBoolean": True}]},
                    ]}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    _move_case(client, case_id, "pre-op")
    # Depth pass (Workstream 2): the WHO safety-checklist gate ships
    # seeded TRUE, so the time-out section is recorded before the
    # case may move to in-op (gate proven in test_depth_journeys.py).
    r = client.post(f"/surgery/cases/{case_id}/safety-checklist",
                    json={"section": "time-out",
                          "answers": [{"linkId": c,
                                       "answer": [{"valueBoolean": True}]}
                                      for c in ("timeout-introductions",
                                                "timeout-verbal-confirmation",
                                                "timeout-antibiotics",
                                                "timeout-imaging",
                                                "timeout-concerns")]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    _move_case(client, case_id, "in-op")
    # Anaesthesia record (published form) attaches to the case.
    r = client.post(f"/inpatient/surgery-cases/{case_id}/anaesthesia",
                    json={"answers": ANAESTHESIA_ANSWERS}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["response"]["questionnaire"] == "anaesthesia-record"
    # Two vitals readings land on the case's flowsheet series.
    for answers in (VITALS_ANSWERS, [
            {"linkId": "systolic", "answer": [{"valueInteger": 110}]},
            {"linkId": "heart-rate", "answer": [{"valueInteger": 68}]},
            {"linkId": "spo2", "answer": [{"valueInteger": 98}]}]):
        r = client.post(
            f"/inpatient/surgery-cases/{case_id}/anaesthesia-vitals",
            json={"answers": answers}, headers=CLINICIAN)
        assert r.status_code == 201, r.text
    _move_case(client, case_id, "pacu")

    record = client.get(f"/inpatient/surgery-cases/{case_id}/record",
                        headers=CLINICIAN).json()
    assert record["preop_checklist"]["response"]["id"]
    ana = record["anaesthesia"]
    assert ana["meta"]["summary"]["agents"] == \
        "Propofol, rocuronium, sevoflurane"
    assert ana["response"]["subject"]["reference"] == "Patient/pat-1"
    series = record["anaesthesia_vitals"]
    assert series["form_key"] == "anaesthesia-vitals"
    assert series["total"] == 2 and len(series["entries"]) == 2
    item_ids = {i["linkId"] for i in series["form"]["items"]}
    assert {"systolic", "heart-rate", "spo2"} <= item_ids
    assert series["entries"][0]["answers"]["systolic"]["value"] == 118
    assert series["entries"][1]["answers"]["systolic"]["value"] == 110
    # The case timeline is server-timestamped, in workflow order.
    timeline = record["case"]["timeline"]
    assert [e["state"] for e in timeline] == \
        ["scheduled", "pre-op", "in-op", "pacu"]
    assert all(e["at"] and e["by"] == "dev-clinician" for e in timeline)
    # Unknown case / non-case Procedure -> 404.
    assert client.get("/inpatient/surgery-cases/nope/record",
                      headers=CLINICIAN).status_code == 404


def test_pacu_note_type_signs_like_other_notes(client):
    _ensure_patient(client, "pat-1")
    payload = client.get("/notes/types", headers=CLINICIAN).json()["note_types"]
    concepts = payload["concepts"]
    codes = [t["code"] for t in concepts]
    assert "preop" in codes and "pacu" in codes
    pacu = next(t for t in concepts if t["code"] == "pacu")
    assert pacu["templateForm"] == "note-pacu"
    r = client.post("/notes", json={
        "patient": "pat-1", "note_type": "pacu",
        "answers": [
            {"linkId": "recovery-status",
             "answer": [{"valueString": "Awake, comfortable, vitals stable"}]},
            {"linkId": "pain-score", "answer": [{"valueInteger": 2}]},
            {"linkId": "discharge-ready", "answer": [{"valueBoolean": True}]},
        ]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    note_id = r.json()["id"]
    signed = client.post(f"/notes/{note_id}/sign", headers=CLINICIAN)
    assert signed.status_code == 200, signed.text
    assert signed.json()["docStatus"] == "final"


# ---------------------------------------------------------------------------
# Role gates: the bed board is staff-only
# ---------------------------------------------------------------------------

def test_bed_board_is_staff_only(client):
    for path in ("/inpatient/census", "/inpatient/beds",
                 "/inpatient/locations", "/inpatient/expected-discharges",
                 "/inpatient/adt-events", "/inpatient/discharge-dispositions"):
        assert client.get(path, headers=PATIENT).status_code == 403, path
        assert client.get(path, headers=VIEWER).status_code == 200, path
    assert client.post("/inpatient/admissions",
                       json={"patient": "pat-1", "bed": "MS-101A"},
                       headers=PATIENT).status_code == 403
    assert client.post("/inpatient/admissions",
                       json={"patient": "pat-1", "bed": "MS-101A"},
                       headers=VIEWER).status_code == 403
    admitted = _admit(client)
    enc_id = admitted["encounter"]["id"]
    assert client.post(f"/inpatient/encounters/{enc_id}/transfer",
                       json={"bed": "MS-101B"},
                       headers=VIEWER).status_code == 403
    assert client.post(f"/inpatient/encounters/{enc_id}/discharge",
                       json={}, headers=PATIENT).status_code == 403
