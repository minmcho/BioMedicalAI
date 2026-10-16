"""Phase 4B — specialty boards (app.boards): ED board, radiology, surgery.

Everything site-specific is Studio configuration (workflow/terminology/
integration config seeded published v1), so the tests assert both the
seeded config and config-driven behaviour. Safety-critical behaviours
covered: server-side wait-time computation, ESI acuity validation,
governed ED exits (admit/depart only), the inpatient link on admit,
DICOMweb UID round-trip, imaging report verify gating, and the surgery
room-overlap rule (409 on create AND on reschedule, comparing stored
times).
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.audit import verify_chain
from app.models import AuditLog
from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER


def _patient(client, pid="pat-1", family="Synthetic", given="Pat"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": family, "given": [given]}],
        "gender": "female", "birthDate": "1980-05-01",
    }, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _arrival(client, pid="pat-1", minutes_ago=None, complaint="Chest pain"):
    body = {"patient": pid, "chief_complaint": complaint}
    if minutes_ago is not None:
        body["arrived_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    r = client.post("/ed/arrivals", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _board(client, headers=CLINICIAN):
    r = client.get("/ed/board", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _to_disposition(client, encounter_id):
    for target, extra in (("triage", {"acuity": 3}), ("in-treatment", {}),
                          ("disposition", {})):
        r = client.post(f"/ed/encounters/{encounter_id}/transition",
                        json={"to_status": target, **extra}, headers=CLINICIAN)
        assert r.status_code == 200, r.text


def _service_request(client, srid="sr-img-1", pid="pat-1", code="71046"):
    r = client.post("/fhir/ServiceRequest", json={
        "resourceType": "ServiceRequest", "id": srid, "status": "active",
        "intent": "order",
        "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt",
                             "code": code, "display": "Chest X-ray, 2 views"}]},
        "subject": {"reference": f"Patient/{pid}"},
    }, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _study(client, srid="sr-img-1", modality="CT"):
    r = client.post("/radiology/studies",
                    json={"service_request_id": srid, "modality": modality},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _case(client, room="OR-1", start="2026-10-20T08:00:00Z",
          end="2026-10-20T10:00:00Z", pid="pat-1", code="27447"):
    r = client.post("/surgery/cases", json={
        "patient_id": pid, "code": code, "display": "Total knee arthroplasty",
        "room": room, "start": start, "end": end,
    }, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Seeded Studio config
# ---------------------------------------------------------------------------

def test_boards_config_seeded_as_published_v1(client):
    ed = client.get("/studio/config/workflow/effective/ed-flow", headers=CLINICIAN)
    assert ed.status_code == 200
    states = ed.json()["payload"]["states"]
    assert states == ["admitted", "departed", "disposition", "in-treatment",
                      "triage", "waiting"]
    modalities = client.get("/studio/config/terminology/effective/imaging-modalities",
                            headers=CLINICIAN).json()
    codes = {c["code"] for c in modalities["payload"]["concepts"]}
    assert {"CT", "MR", "US"} <= codes
    rooms = client.get("/studio/config/terminology/effective/or-rooms",
                       headers=CLINICIAN).json()
    assert [c["code"] for c in rooms["payload"]["concepts"]] == \
        ["OR-1", "OR-2", "OR-3", "OR-4"]
    viewer = client.get("/studio/config/integration/effective/dicom-viewer",
                        headers=CLINICIAN).json()
    assert viewer["payload"]["viewer"]["baseUrl"].startswith("https://")


def test_ed_machine_is_config_driven(client):
    """Publishing a new ed-flow version changes the legal transitions —
    the board machine is configuration, not code."""
    _patient(client)
    enc = _arrival(client)
    r = client.post(f"/ed/encounters/{enc['id']}/transition",
                    json={"to_status": "disposition"}, headers=CLINICIAN)
    assert r.status_code == 409  # waiting -> disposition is illegal under v1
    v2 = {"states": ["waiting", "triage", "in-treatment", "disposition",
                     "departed", "admitted"],
          "transitions": [{"from": "waiting", "to": "triage"},
                          {"from": "waiting", "to": "disposition"},
                          {"from": "triage", "to": "in-treatment"},
                          {"from": "in-treatment", "to": "disposition"},
                          {"from": "disposition", "to": "departed"},
                          {"from": "disposition", "to": "admitted"}]}
    r = client.post("/studio/config/workflow/ed-flow", json=v2, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/studio/config/workflow/ed-flow/versions/{version}/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    r = client.post(f"/ed/encounters/{enc['id']}/transition",
                    json={"to_status": "disposition"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# ED board
# ---------------------------------------------------------------------------

def test_ed_full_flow_with_wait_time_and_triage(client):
    _patient(client)
    enc = _arrival(client, minutes_ago=95)
    assert enc["status"] == "waiting"
    assert enc["class"]["code"] == "EMER"

    board = _board(client)
    assert board["counts"] == {"waiting": 1, "triage": 0, "in-treatment": 0,
                                "disposition": 0, "total": 1}
    entry = board["groups"]["waiting"][0]
    assert entry["encounter"]["id"] == enc["id"]
    assert entry["wait_minutes"] >= 95  # server-computed from arrived_at
    assert entry["acuity"] is None
    assert entry["chief_complaint"] == "Chest pain"

    # Triage requires a valid ESI acuity.
    url = f"/ed/encounters/{enc['id']}/transition"
    assert client.post(url, json={"to_status": "triage"},
                       headers=CLINICIAN).status_code == 422
    assert client.post(url, json={"to_status": "triage", "acuity": 6},
                       headers=CLINICIAN).status_code == 422
    assert client.post(url, json={"to_status": "triage", "acuity": "3"},
                       headers=CLINICIAN).status_code == 422
    r = client.post(url, json={"to_status": "triage", "acuity": 3}, headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["status"] == "triage"
    board = _board(client)
    assert board["groups"]["triage"][0]["acuity"] == 3

    # The machine is enforced: triage -> disposition skips a state.
    assert client.post(url, json={"to_status": "disposition"},
                       headers=CLINICIAN).status_code == 409
    assert client.post(url, json={"to_status": "in-treatment"},
                       headers=CLINICIAN).status_code == 200
    assert client.post(url, json={"to_status": "disposition"},
                       headers=CLINICIAN).status_code == 200

    r = client.post(f"/ed/encounters/{enc['id']}/depart", json={}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    departed = r.json()["ed_encounter"]
    assert departed["status"] == "departed" and departed["period"]["end"]
    assert _board(client)["counts"]["total"] == 0  # left the census


def test_ed_admit_creates_linked_inpatient_encounter(client):
    _patient(client)
    enc = _arrival(client)
    _to_disposition(client, enc["id"])
    r = client.post(f"/ed/encounters/{enc['id']}/admit",
                    json={"ward": "Med-Surg 4"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    ed_enc = r.json()["ed_encounter"]
    inpatient = r.json()["inpatient_encounter"]
    assert ed_enc["status"] == "admitted" and ed_enc["period"]["end"]
    assert inpatient["class"]["code"] == "IMP"
    assert inpatient["status"] == "in-progress"
    assert inpatient["partOf"]["reference"] == f"Encounter/{enc['id']}"
    assert inpatient["subject"]["reference"] == "Patient/pat-1"
    # The link is also recorded on the ED encounter itself.
    exts = {e["url"]: e for e in ed_enc["extension"]}
    assert exts["urn:opencare:extension/admission-encounter"][
        "valueReference"]["reference"] == f"Encounter/{inpatient['id']}"
    assert _board(client)["counts"]["total"] == 0


def test_ed_governed_exits_and_validation(client):
    _patient(client)
    enc = _arrival(client)
    url = f"/ed/encounters/{enc['id']}/transition"
    # departed/admitted are governed — a bare transition is refused.
    assert client.post(url, json={"to_status": "departed"},
                       headers=CLINICIAN).status_code == 422
    assert client.post(url, json={"to_status": "admitted"},
                       headers=CLINICIAN).status_code == 422
    # Admit/depart before disposition are illegal transitions (409).
    assert client.post(f"/ed/encounters/{enc['id']}/admit", json={},
                       headers=CLINICIAN).status_code == 409
    assert client.post(f"/ed/encounters/{enc['id']}/depart", json={},
                       headers=CLINICIAN).status_code == 409
    # Unknown state / unknown encounter / unknown patient.
    assert client.post(url, json={"to_status": "bogus"},
                       headers=CLINICIAN).status_code == 422
    assert client.post("/ed/encounters/nope/transition",
                       json={"to_status": "triage", "acuity": 2},
                       headers=CLINICIAN).status_code == 404
    assert client.post("/ed/arrivals", json={"patient": "ghost"},
                       headers=CLINICIAN).status_code == 404
    # A non-ED encounter cannot be driven through the ED endpoints.
    r = client.post("/fhir/Encounter", json={
        "resourceType": "Encounter", "id": "enc-plain", "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                  "code": "AMB", "display": "ambulatory"},
        "subject": {"reference": "Patient/pat-1"},
    }, headers=CLINICIAN)
    assert r.status_code == 201
    assert client.post("/ed/encounters/enc-plain/transition",
                       json={"to_status": "triage", "acuity": 2},
                       headers=CLINICIAN).status_code == 422


def test_ed_roles_and_audit(client):
    _patient(client)
    assert client.get("/ed/board").status_code == 401
    assert client.get("/ed/board", headers=PATIENT).status_code == 403
    assert client.post("/ed/arrivals", json={"patient": "pat-1"},
                       headers=PATIENT).status_code == 403
    assert client.post("/ed/arrivals", json={"patient": "pat-1"},
                       headers=VIEWER).status_code == 403
    assert client.get("/ed/board", headers=VIEWER).status_code == 200
    enc = _arrival(client)
    _to_disposition(client, enc["id"])
    client.post(f"/ed/encounters/{enc['id']}/depart", json={}, headers=CLINICIAN)
    factory = client.app.state.session_factory
    with factory() as session:
        actions = [row.action for row in session.scalars(select(AuditLog)).all()]
        assert "ed.arrival" in actions
        assert "ed.encounter.transition" in actions
        assert "ed.encounter.depart" in actions
        assert verify_chain(session) is True


# ---------------------------------------------------------------------------
# Radiology + DICOMweb
# ---------------------------------------------------------------------------

def test_radiology_study_report_verify_flow(client):
    _patient(client)
    _service_request(client)
    item = _study(client, modality="CT")
    study = item["study"]
    assert study["resourceType"] == "ImagingStudy"
    assert item["study_uid"].startswith("2.25.")
    assert item["has_images"] is False  # metadata-only, stated plainly
    assert item["viewer_url"] == \
        f"https://viewer.ohif.org/viewer?StudyInstanceUIDs={item['study_uid']}"
    assert item["report"] is None
    # The study lives on the generic FHIR surface too.
    got = client.get(f"/fhir/ImagingStudy/{study['id']}", headers=CLINICIAN)
    assert got.status_code == 200 and got.json()["id"] == study["id"]

    # Unknown / inactive modality and unknown order are refused.
    assert client.post("/radiology/studies",
                       json={"service_request_id": "sr-img-1", "modality": "XX"},
                       headers=CLINICIAN).status_code == 422
    assert client.post("/radiology/studies",
                       json={"service_request_id": "nope", "modality": "CT"},
                       headers=CLINICIAN).status_code == 404

    # Report: preliminary on creation; a second draft is refused (409).
    r = client.post(f"/radiology/studies/{study['id']}/report",
                    json={"conclusion": "No acute findings. (synthetic)"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    report = r.json()
    assert report["status"] == "preliminary"
    assert report["imagingStudy"][0]["reference"] == f"ImagingStudy/{study['id']}"
    assert client.post(f"/radiology/studies/{study['id']}/report",
                       json={"conclusion": "duplicate draft"},
                       headers=CLINICIAN).status_code == 409
    assert client.post(f"/radiology/studies/{study['id']}/report",
                       json={}, headers=CLINICIAN).status_code == 422

    # Verification is a clinical sign-off: configurator is excluded.
    assert client.post(f"/radiology/reports/{report['id']}/verify",
                       headers=CONFIG).status_code == 403
    assert client.post(f"/radiology/reports/{report['id']}/verify",
                       headers=PATIENT).status_code == 403
    r = client.post(f"/radiology/reports/{report['id']}/verify", headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["status"] == "final"
    assert client.post(f"/radiology/reports/{report['id']}/verify",
                       headers=CLINICIAN).status_code == 409

    detail = client.get(f"/radiology/studies/{study['id']}", headers=CLINICIAN).json()
    assert detail["report"]["status"] == "final"
    assert [rep["status"] for rep in detail["reports"]] == ["final"]


def test_imaging_study_generic_validation(client):
    _patient(client)
    # No DICOM UID identifier -> 422 on the generic surface.
    r = client.post("/fhir/ImagingStudy", json={
        "resourceType": "ImagingStudy", "status": "available",
        "subject": {"reference": "Patient/pat-1"},
    }, headers=CLINICIAN)
    assert r.status_code == 422
    # A lab (non-imaging) report cannot be verified via radiology.
    _service_request(client)
    item = _study(client)
    r = client.post(f"/radiology/studies/{item['study']['id']}/report",
                    json={"conclusion": "ok"}, headers=CLINICIAN)
    report_id = r.json()["id"]
    other = client.post("/fhir/DiagnosticReport", json={
        "resourceType": "DiagnosticReport", "id": "rep-lab", "status": "preliminary",
        "code": {"text": "Lab panel"},
    }, headers=CLINICIAN)
    assert other.status_code == 201
    assert client.post("/radiology/reports/rep-lab/verify",
                       headers=CLINICIAN).status_code == 422
    assert client.post(f"/radiology/reports/{report_id}/verify",
                       headers=CLINICIAN).status_code == 200


def test_dicomweb_study_json_and_uid_round_trip(client):
    _patient(client, family="Emergency", given="Edna")
    _service_request(client)
    item = _study(client, modality="MR")
    uid = item["study_uid"]

    r = client.get("/dicomweb/studies", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    studies = r.json()
    assert len(studies) == 1
    dicom = studies[0]
    # PS3.18 DICOM JSON: hex tags -> {vr, Value}.
    assert dicom["0020000D"] == {"vr": "UI", "Value": [uid]}
    assert dicom["00100020"]["Value"] == ["pat-1"]
    assert "Emergency" in dicom["00100010"]["Value"][0]["Alphabetic"]
    assert dicom["00080061"]["Value"] == ["MR"]
    assert dicom["00080020"]["Value"][0].isdigit()  # StudyDate YYYYMMDD
    assert dicom["00201206"]["Value"] == [0]  # no series: metadata only
    assert dicom["00201208"]["Value"] == [0]

    # UID round-trip: retrieve by the Study Instance UID.
    one = client.get(f"/dicomweb/studies/{uid}", headers=CLINICIAN)
    assert one.status_code == 200 and one.json() == dicom
    assert client.get("/dicomweb/studies/2.25.999", headers=CLINICIAN).status_code == 404
    assert client.get("/dicomweb/studies", headers=PATIENT).status_code == 403
    assert client.get("/dicomweb/studies").status_code == 401


# ---------------------------------------------------------------------------
# Surgery board
# ---------------------------------------------------------------------------

def test_surgery_flow_and_board_grouping(client):
    _patient(client)
    outcome = _case(client)
    case = outcome["case"]
    assert case["status"] == "scheduled"
    assert outcome["appointment"]["status"] == "booked"

    board = client.get("/surgery/board", params={"date": "2026-10-20"},
                       headers=CLINICIAN).json()
    assert board["total"] == 1
    assert [room["code"] for room in board["rooms"]] == \
        ["OR-1", "OR-2", "OR-3", "OR-4"]  # all configured rooms listed
    or1 = board["rooms"][0]
    assert len(or1["cases"]) == 1
    assert or1["cases"][0]["case"]["id"] == case["id"]
    assert or1["cases"][0]["state"] == "scheduled"
    assert board["rooms"][1]["cases"] == []
    other_day = client.get("/surgery/board", params={"date": "2026-10-21"},
                           headers=CLINICIAN).json()
    assert other_day["total"] == 0
    assert client.get("/surgery/board", params={"date": "20/10/2026"},
                      headers=CLINICIAN).status_code == 422

    url = f"/surgery/cases/{case['id']}/transition"
    assert client.post(url, json={"to_status": "in-op"},
                       headers=CLINICIAN).status_code == 409  # skips pre-op
    for target in ("pre-op", "in-op", "pacu", "done"):
        r = client.post(url, json={"to_status": target}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    assert r.json()["case"]["status"] == "done"
    assert r.json()["appointment"]["status"] == "fulfilled"
    # A finished case cannot move or transition further.
    assert client.post(url, json={"to_status": "cancelled"},
                       headers=CLINICIAN).status_code == 409
    assert client.post(f"/surgery/cases/{case['id']}/reschedule",
                       json={"start": "2026-10-22T08:00:00Z",
                             "end": "2026-10-22T09:00:00Z"},
                       headers=CLINICIAN).status_code == 409


def test_surgery_room_overlap_rules(client):
    _patient(client)
    _case(client, room="OR-1", start="2026-10-21T08:00:00Z",
          end="2026-10-21T10:00:00Z")
    # Overlapping the same room -> 409.
    r = client.post("/surgery/cases", json={
        "patient_id": "pat-1", "code": "27447", "room": "OR-1",
        "start": "2026-10-21T09:00:00Z", "end": "2026-10-21T11:00:00Z",
    }, headers=CLINICIAN)
    assert r.status_code == 409
    # Adjacent (starts exactly at the other's end) is not an overlap.
    adjacent = _case(client, room="OR-1", start="2026-10-21T10:00:00Z",
                     end="2026-10-21T12:00:00Z")
    # The same interval in a different room is fine.
    other_room = _case(client, room="OR-2", start="2026-10-21T09:00:00Z",
                       end="2026-10-21T11:00:00Z")
    # Cancelling frees the room: the cancelled slot can be rebooked.
    r = client.post(f"/surgery/cases/{adjacent['case']['id']}/transition",
                    json={"to_status": "cancelled"}, headers=CLINICIAN)
    assert r.status_code == 200
    assert r.json()["appointment"]["status"] == "cancelled"
    _case(client, room="OR-1", start="2026-10-21T10:30:00Z",
          end="2026-10-21T11:30:00Z")
    # Reschedule: onto another case in the same room -> 409; a free slot
    # works (the case's own current slot does not block itself).
    _case(client, room="OR-2", start="2026-10-21T13:00:00Z",
          end="2026-10-21T14:00:00Z")
    r = client.post(f"/surgery/cases/{other_room['case']['id']}/reschedule",
                    json={"start": "2026-10-21T13:30:00Z",
                          "end": "2026-10-21T15:00:00Z"}, headers=CLINICIAN)
    assert r.status_code == 409
    r = client.post(f"/surgery/cases/{other_room['case']['id']}/reschedule",
                    json={"start": "2026-10-21T15:00:00Z",
                          "end": "2026-10-21T16:00:00Z"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["appointment"]["start"].startswith("2026-10-21T15:00")


def test_surgery_validation_and_roles(client):
    _patient(client)
    base = {"patient_id": "pat-1", "code": "27447", "room": "OR-1",
            "start": "2026-10-22T08:00:00Z", "end": "2026-10-22T09:00:00Z"}
    assert client.post("/surgery/cases", json={**base, "room": "OR-99"},
                       headers=CLINICIAN).status_code == 422
    assert client.post("/surgery/cases",
                       json={**base, "end": "2026-10-22T08:00:00Z"},
                       headers=CLINICIAN).status_code == 422
    assert client.post("/surgery/cases", json={**base, "patient_id": "ghost"},
                       headers=CLINICIAN).status_code == 404
    assert client.post("/surgery/cases", json=base).status_code == 401
    assert client.post("/surgery/cases", json=base, headers=PATIENT).status_code == 403
    assert client.post("/surgery/cases", json=base, headers=VIEWER).status_code == 403
    assert client.get("/surgery/board", headers=PATIENT).status_code == 403
    assert client.get("/surgery/board", headers=VIEWER).status_code == 200
    # A plain (non-case) Procedure is not addressable as a surgery case.
    r = client.post("/fhir/Procedure", json={
        "resourceType": "Procedure", "id": "proc-plain", "status": "completed",
        "code": {"text": "Ward procedure"},
        "subject": {"reference": "Patient/pat-1"},
    }, headers=CLINICIAN)
    assert r.status_code == 201
    assert client.post("/surgery/cases/proc-plain/transition",
                       json={"to_status": "cancelled"},
                       headers=CLINICIAN).status_code == 422

    outcome = _case(client)
    factory = client.app.state.session_factory
    with factory() as session:
        actions = [row.action for row in session.scalars(select(AuditLog)).all()]
        assert "surgery.case.schedule" in actions
        assert verify_chain(session) is True
