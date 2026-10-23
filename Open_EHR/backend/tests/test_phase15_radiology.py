"""Phase 15 (D/BD-35) — the radiology department (app.radiology).

RIS worklist driven by the published radiology-flow workflow + the
radiology-ops terminology config (priorities/TAT/gates/dose/stations/
SLA), DICOM ingest + cold-store archive with byte-exact WADO-RS
retrieval after hash verification, DICOM Modality Worklist,
protocoling + governed pre-exam screening gates (config-gated,
flip-proven), radiation dose with cumulative history, structured
reporting composed onto boards' DiagnosticReport flow + addenda,
the critical-results closed loop with a rules-engine SLA escalation,
exact-math TAT stats, and the guarded demo-department seed.

Synthetic data only. DICOM objects in these tests are generated with
pydicom directly (small synthetic pixel data), never copied from the
module under test.
"""
import base64
import hashlib
import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.audit import verify_chain
from app.main import create_app
from app.models import FhirResource
from app.radiology import (RadCriticalResult, RadDicomInstance,
                           RadWorklistEntry, default_ops_payload,
                           ensure_radiology_defaults)

from .conftest import ADMIN, CLINICIAN, PATIENT, VIEWER


@pytest.fixture()
def rad_client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCARE_COLD_DIR", str(tmp_path / "cold-store"))
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic", "given": ["Pat"]}],
        "gender": "female", "birthDate": "1980-05-01",
    }, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _service_request(client, srid="sr-img-1", pid="pat-1", priority=None,
                     requester=None):
    body = {
        "resourceType": "ServiceRequest", "id": srid, "status": "active",
        "intent": "order",
        "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt",
                             "code": "71046",
                             "display": "Chest X-ray, 2 views"}]},
        "subject": {"reference": f"Patient/{pid}"},
    }
    if priority:
        body["priority"] = priority
    if requester:
        body["requester"] = {"display": requester}
    r = client.post("/fhir/ServiceRequest", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _study(client, srid="sr-img-1", modality="CT"):
    r = client.post("/radiology/studies",
                    json={"service_request_id": srid,
                          "modality": modality},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    item = r.json()
    return item["study"]["id"], item["study_uid"]


def _setup_study(client, pid="pat-1", srid="sr-img-1", modality="CT",
                 priority=None, requester=None):
    _patient(client, pid)
    _service_request(client, srid, pid, priority=priority,
                     requester=requester)
    return _study(client, srid, modality)


def _transition(client, study_id, to_status, headers=CLINICIAN, **extra):
    return client.post(f"/radiology/worklist/{study_id}/transition",
                       json={"to_status": to_status, **extra},
                       headers=headers)


def _walk_to(client, study_id, target):
    chain = ["ordered", "scheduled", "arrived", "in-progress",
             "completed", "reported"]
    r = client.get("/radiology/worklist", headers=CLINICIAN)
    current = next(i["state"] for i in r.json()["items"]
                   if i["study_id"] == study_id)
    for state in chain[chain.index(current) + 1: chain.index(target) + 1]:
        r = _transition(client, study_id, state)
        assert r.status_code == 200, r.text
    return r.json()


def _dicom_bytes(study_uid, series_uid, sop_uid, modality="CT",
                 patient_id="pat-1", pixel=bytes([1, 2, 3, 4])):
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.Modality = modality
    ds.PatientID = patient_id
    ds.PatientName = "Synthetic^Pat"
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    ds.Rows = 2
    ds.Columns = 2
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = pixel
    buffer = io.BytesIO()
    ds.save_as(buffer, enforce_file_format=True)
    return buffer.getvalue()


def _uid():
    return f"2.25.{uuid.uuid4().int}"


def _ingest(client, raw, headers=CLINICIAN):
    return client.post(
        "/radiology/dicom/ingest",
        json={"dicom_base64": base64.b64encode(raw).decode("ascii")},
        headers=headers)


def _publish_ops(client, mutate):
    payload = default_ops_payload()
    mutate(payload)
    r = client.post("/studio/config/terminology/radiology-ops",
                    json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/terminology/radiology-ops/versions/{version}"
        "/publish", headers=ADMIN)
    assert r.status_code == 200, r.text


def _draft_and_verify_report(client, study_id, conclusion="Normal."):
    r = client.post(f"/radiology/studies/{study_id}/report",
                    json={"conclusion": conclusion}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    report = r.json()
    r = client.post(f"/radiology/reports/{report['id']}/verify",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def _session(client):
    return client.app.state.session_factory()


CONTRAST_ANSWERS = [
    {"linkId": "contrast-allergy", "answer": [{"valueBoolean": False}]},
    {"linkId": "renal-impairment", "answer": [{"valueBoolean": False}]},
    {"linkId": "metformin", "answer": [{"valueBoolean": False}]},
]

MR_SAFETY_ANSWERS = [
    {"linkId": "pacemaker", "answer": [{"valueBoolean": False}]},
    {"linkId": "metal-implants", "answer": [{"valueBoolean": False}]},
    {"linkId": "claustrophobia", "answer": [{"valueBoolean": False}]},
]


# ---------------------------------------------------------------------------
# Seeded configuration + worklist basics
# ---------------------------------------------------------------------------

def test_seeded_studio_config(rad_client):
    for kind, key in (("workflow", "radiology-flow"),
                      ("terminology", "radiology-ops"),
                      ("terminology", "imaging-protocols"),
                      ("form", "radiology-screening-contrast"),
                      ("form", "radiology-screening-pregnancy"),
                      ("form", "radiology-screening-mr-safety"),
                      ("form", "radiology-report-ct-chest"),
                      ("form", "radiology-report-xr-chest"),
                      ("dashboard", "radiology-operations"),
                      ("rule", "radiology-critical-escalation")):
        r = rad_client.get(f"/studio/config?kind={kind}", headers=ADMIN)
        assert r.status_code == 200, r.text
        by_key = {row["key"]: row for row in r.json()}
        assert by_key[key]["status"] == "published", (kind, key)


def test_worklist_shows_new_study_ordered_with_derived_priority(rad_client):
    study_id, _uid_str = _setup_study(rad_client, priority="stat")
    r = rad_client.get("/radiology/worklist", headers=CLINICIAN)
    assert r.status_code == 200
    assert r.json()["total"] == 1
    item = r.json()["items"][0]
    assert item["study_id"] == study_id
    assert item["state"] == "ordered"
    assert item["priority"] == "STAT"          # from the order's priority
    assert item["tat_target_minutes"] == 60     # radiology-ops STAT target
    assert item["modality"] == "CT"
    assert item["report"] is None


def test_worklist_filters_and_default_priority(rad_client):
    _setup_study(rad_client, pid="pat-1", srid="sr-a", modality="CT")
    _setup_study(rad_client, pid="pat-2", srid="sr-b", modality="DX",
                 priority="urgent")
    items = rad_client.get("/radiology/worklist",
                           headers=CLINICIAN).json()["items"]
    by_modality = {i["modality"]: i for i in items}
    assert by_modality["CT"]["priority"] == "ROUTINE"   # ops default
    assert by_modality["DX"]["priority"] == "URGENT"
    r = rad_client.get("/radiology/worklist?priority=URGENT",
                       headers=CLINICIAN)
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["modality"] == "DX"
    r = rad_client.get("/radiology/worklist?modality=CT&state=ordered",
                       headers=CLINICIAN)
    assert r.json()["total"] == 1


def test_worklist_transitions_and_illegal_409(rad_client):
    study_id, _ = _setup_study(rad_client)
    # Illegal jumps and unknown states are refused.
    r = _transition(rad_client, study_id, "completed")
    assert r.status_code == 409, r.text
    r = _transition(rad_client, study_id, "bogus-state")
    assert r.status_code == 422, r.text
    # The legal chain walks (protocol/screening gates are exercised in
    # their own test; this study is DX with no protocol needed yet —
    # walk only to arrived here).
    assert _transition(rad_client, study_id, "scheduled").status_code == 200
    assert _transition(rad_client, study_id, "arrived").status_code == 200
    r = _transition(rad_client, study_id, "scheduled")
    assert r.status_code == 409  # no going backwards
    item = next(i for i in rad_client.get(
        "/radiology/worklist", headers=CLINICIAN).json()["items"]
        if i["study_id"] == study_id)
    assert item["state"] == "arrived"
    assert [h["state"] for h in item["state_history"]] == [
        "ordered", "scheduled", "arrived"]


def test_worklist_capability_gates(rad_client):
    study_id, _ = _setup_study(rad_client)
    assert rad_client.get("/radiology/worklist",
                          headers=VIEWER).status_code == 200
    assert rad_client.get("/radiology/worklist",
                          headers=PATIENT).status_code == 403
    r = _transition(rad_client, study_id, "scheduled", headers=VIEWER)
    assert r.status_code == 403


def test_reported_transition_requires_final_report(rad_client):
    study_id, _ = _setup_study(rad_client, modality="DX")
    _publish_ops(rad_client, lambda p: p.update(
        {"protocolRequired": False, "screeningRequired": False}))
    _walk_to(rad_client, study_id, "completed")
    r = _transition(rad_client, study_id, "reported")
    assert r.status_code == 409, r.text  # nothing verified yet
    _draft_and_verify_report(rad_client, study_id)
    r = _transition(rad_client, study_id, "reported")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "reported"


# ---------------------------------------------------------------------------
# Protocoling + screening gates (config-gated, flip-proven)
# ---------------------------------------------------------------------------

def test_protocol_and_screening_gates(rad_client):
    study_id, _ = _setup_study(rad_client, modality="CT")
    _walk_to(rad_client, study_id, "arrived")
    # Gate 1: no protocol assigned.
    r = _transition(rad_client, study_id, "in-progress")
    assert r.status_code == 409, r.text
    assert "Protocol gate" in r.json()["detail"]
    # Wrong-modality and unknown protocols are refused.
    r = rad_client.post(f"/radiology/worklist/{study_id}/protocol",
                        json={"protocol_code": "MR-BRAIN"},
                        headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = rad_client.post(f"/radiology/worklist/{study_id}/protocol",
                        json={"protocol_code": "NOPE"},
                        headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = rad_client.post(f"/radiology/worklist/{study_id}/protocol",
                        json={"protocol_code": "CT-CHEST-CONTRAST"},
                        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["protocol_code"] == "CT-CHEST-CONTRAST"
    # Gate 2: contrast screening not yet complete.
    r = _transition(rad_client, study_id, "in-progress")
    assert r.status_code == 409, r.text
    assert "radiology-screening-contrast" in r.json()["detail"]
    r = rad_client.post(
        f"/radiology/worklist/{study_id}/screening/"
        "radiology-screening-contrast",
        json={"answers": CONTRAST_ANSWERS}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["screening"]["radiology-screening-contrast"][
        "responseId"] == r.json()["response"]["id"]
    r = _transition(rad_client, study_id, "in-progress")
    assert r.status_code == 200, r.text


def test_screening_missing_required_answers_422(rad_client):
    study_id, _ = _setup_study(rad_client, modality="MR")
    r = rad_client.post(
        f"/radiology/worklist/{study_id}/screening/"
        "radiology-screening-mr-safety",
        json={"answers": MR_SAFETY_ANSWERS[:1]}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    assert "pacemaker" not in r.json()["detail"]  # answered one is fine
    assert "metal-implants" in r.json()["detail"]
    # A non-screening form key is refused by the screening endpoint.
    r = rad_client.post(
        f"/radiology/worklist/{study_id}/screening/"
        "radiology-report-ct-chest",
        json={"answers": []}, headers=CLINICIAN)
    assert r.status_code == 422, r.text


def test_gates_flip_off_with_published_config(rad_client):
    study_id, _ = _setup_study(rad_client, modality="CT")
    _walk_to(rad_client, study_id, "arrived")
    assert _transition(rad_client, study_id,
                       "in-progress").status_code == 409
    _publish_ops(rad_client, lambda p: p.update(
        {"protocolRequired": False, "screeningRequired": False}))
    r = _transition(rad_client, study_id, "in-progress")
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# DICOM ingest + archive (BD-35)
# ---------------------------------------------------------------------------

def test_dicom_ingest_store_retrieve_byte_exact(rad_client):
    study_id, study_uid = _setup_study(rad_client, modality="CT")
    series_uid, sop_uid = _uid(), _uid()
    raw = _dicom_bytes(study_uid, series_uid, sop_uid)
    r = _ingest(rad_client, raw)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["content_hash"] == hashlib.sha256(raw).hexdigest()
    assert body["size_bytes"] == len(raw)
    assert body["study_id"] == study_id
    assert body["cold_ref"].startswith("cold://")
    # QIDO-RS series + instance metadata under /radiology/dicomweb.
    r = rad_client.get(f"/radiology/dicomweb/studies/{study_uid}/series",
                       headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()[0]["0020000E"]["Value"] == [series_uid]
    assert r.json()[0]["00201209"]["Value"] == [1]
    r = rad_client.get(
        f"/radiology/dicomweb/studies/{study_uid}/series/{series_uid}"
        "/instances", headers=CLINICIAN)
    assert r.json()[0]["00080018"]["Value"] == [sop_uid]
    # WADO-RS retrieve: byte-exact, application/dicom.
    r = rad_client.get(
        f"/radiology/dicomweb/studies/{study_uid}/series/{series_uid}"
        f"/instances/{sop_uid}", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/dicom")
    assert r.content == raw
    # The boards study view now counts the archived instance.
    r = rad_client.get(f"/radiology/studies/{study_id}", headers=CLINICIAN)
    assert r.json()["study"]["numberOfInstances"] == 1
    assert r.json()["study"]["numberOfSeries"] == 1
    # Unknown instance -> 404.
    r = rad_client.get(
        f"/radiology/dicomweb/studies/{study_uid}/series/{series_uid}"
        f"/instances/{_uid()}", headers=CLINICIAN)
    assert r.status_code == 404


def test_dicom_ingest_validation(rad_client):
    study_id, study_uid = _setup_study(rad_client, modality="CT")
    # Not DICOM at all.
    r = _ingest(rad_client, b"this is not a dicom object at all........")
    assert r.status_code == 422, r.text
    assert "not a DICOM object" in r.json()["detail"]
    # Unknown study UID: ingest is for ordered studies.
    raw = _dicom_bytes(_uid(), _uid(), _uid())
    r = _ingest(rad_client, raw)
    assert r.status_code == 422, r.text
    assert "ordered studies" in r.json()["detail"]
    # PatientID mismatch with the study's patient.
    raw = _dicom_bytes(study_uid, _uid(), _uid(), patient_id="pat-999")
    r = _ingest(rad_client, raw)
    assert r.status_code == 422, r.text
    assert "PatientID" in r.json()["detail"]
    # Missing SOPInstanceUID.
    import pydicom
    raw = _dicom_bytes(study_uid, _uid(), _uid())
    ds = pydicom.dcmread(io.BytesIO(raw))
    del ds.SOPInstanceUID
    buffer = io.BytesIO()
    ds.save_as(buffer, enforce_file_format=True)
    r = _ingest(rad_client, buffer.getvalue())
    assert r.status_code == 422, r.text
    assert "SOPInstanceUID" in r.json()["detail"]
    # Not base64 / missing field.
    r = rad_client.post("/radiology/dicom/ingest",
                        json={"dicom_base64": "!!!"}, headers=CLINICIAN)
    assert r.status_code == 422
    r = rad_client.post("/radiology/dicom/ingest", json={},
                        headers=CLINICIAN)
    assert r.status_code == 422


def test_dicom_ingest_duplicate_sop_rules(rad_client):
    _study_id, study_uid = _setup_study(rad_client, modality="CT")
    series_uid, sop_uid = _uid(), _uid()
    raw = _dicom_bytes(study_uid, series_uid, sop_uid)
    assert _ingest(rad_client, raw).status_code == 201
    # Same UID, same bytes: idempotent.
    r = _ingest(rad_client, raw)
    assert r.status_code == 201, r.text
    assert r.json()["already_archived"] is True
    # Same UID, different bytes: 409, archive is immutable.
    other = _dicom_bytes(study_uid, series_uid, sop_uid,
                         pixel=bytes([9, 9, 9, 9]))
    r = _ingest(rad_client, other)
    assert r.status_code == 409, r.text


def test_mwl_lists_scheduled_studies_with_station(rad_client):
    study_id, study_uid = _setup_study(rad_client, modality="CT")
    other_id, _ = _setup_study(rad_client, pid="pat-2", srid="sr-b",
                               modality="DX")
    when = datetime.now(timezone.utc).replace(microsecond=0)
    r = _transition(rad_client, study_id, "scheduled",
                    scheduled_at=when.isoformat())
    assert r.status_code == 200, r.text
    assert r.json()["station_ae"] == "CT-SCAN-1"  # radiology-ops station
    r = rad_client.get("/radiology/mwl", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    entries = r.json()
    assert len(entries) == 1  # the merely-ordered study is not on MWL
    entry = entries[0]
    assert entry["0020000D"]["Value"] == [study_uid]
    assert entry["00100020"]["Value"] == ["pat-1"]
    step = entry["00400100"]["Value"][0]
    assert step["00400001"]["Value"] == ["CT-SCAN-1"]
    assert step["00400002"]["Value"] == [when.strftime("%Y%m%d")]
    assert step["00400003"]["Value"] == [when.strftime("%H%M%S")]
    assert other_id  # silence unused


# ---------------------------------------------------------------------------
# Radiation dose
# ---------------------------------------------------------------------------

def test_dose_recording_and_cumulative_history(rad_client):
    ct1, _ = _setup_study(rad_client, pid="pat-1", srid="sr-a",
                          modality="CT")
    ct2, _ = _setup_study(rad_client, pid="pat-1", srid="sr-b",
                          modality="CT")
    mr, _ = _setup_study(rad_client, pid="pat-1", srid="sr-c",
                         modality="MR")
    r = rad_client.post(f"/radiology/worklist/{ct1}/dose",
                        json={"quantity": "CTDIvol", "value": 12.5},
                        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["unit"] == "mGy"
    rad_client.post(f"/radiology/worklist/{ct1}/dose",
                    json={"quantity": "DLP", "value": 300},
                    headers=CLINICIAN)
    rad_client.post(f"/radiology/worklist/{ct2}/dose",
                    json={"quantity": "CTDIvol", "value": 7.5},
                    headers=CLINICIAN)
    # DAP is not a CT quantity; MR has no dose quantities configured.
    r = rad_client.post(f"/radiology/worklist/{ct1}/dose",
                        json={"quantity": "DAP", "value": 3},
                        headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = rad_client.post(f"/radiology/worklist/{mr}/dose",
                        json={"quantity": "CTDIvol", "value": 3},
                        headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = rad_client.post(f"/radiology/worklist/{ct1}/dose",
                        json={"quantity": "CTDIvol", "value": -1},
                        headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # Cumulative per-patient history with exact totals.
    r = rad_client.get("/radiology/patients/pat-1/dose-history",
                       headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    totals = {t["quantity"]: t for t in body["totals"]}
    assert totals["CTDIvol"]["total"] == 20.0
    assert totals["CTDIvol"]["count"] == 2
    assert totals["DLP"]["total"] == 300.0


# ---------------------------------------------------------------------------
# Structured reporting + addenda
# ---------------------------------------------------------------------------

def test_structured_report_composes_onto_boards_flow(rad_client):
    study_id, _ = _setup_study(rad_client, modality="DX")
    answers = [
        {"linkId": "findings",
         "answer": [{"valueString": "Lungs clear. No consolidation."}]},
        {"linkId": "impression",
         "answer": [{"valueString": "No acute cardiopulmonary process."}]},
    ]
    r = rad_client.post(
        f"/radiology/studies/{study_id}/structured-report",
        json={"template": "radiology-report-xr-chest",
              "answers": answers}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    report = r.json()
    assert report["status"] == "preliminary"
    assert "Findings: Lungs clear. No consolidation." \
        in report["conclusion"]
    assert "Impression: No acute cardiopulmonary process." \
        in report["conclusion"]
    assert report["questionnaireResponseId"]
    # One open preliminary per study (boards' rule) applies here too.
    r = rad_client.post(
        f"/radiology/studies/{study_id}/structured-report",
        json={"template": "radiology-report-xr-chest",
              "answers": answers}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    # Verification is boards' path — and it works on this report.
    r = rad_client.post(f"/radiology/reports/{report['id']}/verify",
                        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "final"
    # Missing a required template answer -> 422 from the form machinery.
    study2, _ = _setup_study(rad_client, pid="pat-2", srid="sr-b",
                             modality="DX")
    r = rad_client.post(
        f"/radiology/studies/{study2}/structured-report",
        json={"template": "radiology-report-xr-chest",
              "answers": answers[:1]}, headers=CLINICIAN)
    assert r.status_code == 422, r.text


def test_addendum_marks_original_corrected(rad_client):
    study_id, _ = _setup_study(rad_client, modality="DX")
    final = _draft_and_verify_report(rad_client, study_id,
                                     conclusion="Original conclusion.")
    r = rad_client.post(f"/radiology/reports/{final['id']}/addendum",
                        json={"conclusion": "Corrected conclusion."},
                        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    addendum = r.json()
    assert addendum["status"] == "final"
    assert addendum["conclusion"] == "Corrected conclusion."
    assert addendum["id"] != final["id"]
    ext = {e["url"]: e for e in addendum.get("extension") or []}
    assert ext["urn:opencare:extension/addendum-of"]["valueReference"][
        "reference"] == f"DiagnosticReport/{final['id']}"
    # The original is corrected, its content preserved.
    r = rad_client.get(f"/fhir/DiagnosticReport/{final['id']}",
                       headers=CLINICIAN)
    assert r.json()["status"] == "corrected"
    assert r.json()["conclusion"] == "Original conclusion."
    # A corrected report cannot be addended again; a preliminary neither.
    r = rad_client.post(f"/radiology/reports/{final['id']}/addendum",
                        json={"conclusion": "Again."}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    study2, _ = _setup_study(rad_client, pid="pat-2", srid="sr-b",
                             modality="DX")
    r = rad_client.post(f"/radiology/studies/{study2}/report",
                        json={"conclusion": "Draft."}, headers=CLINICIAN)
    draft_id = r.json()["id"]
    r = rad_client.post(f"/radiology/reports/{draft_id}/addendum",
                        json={"conclusion": "Too soon."},
                        headers=CLINICIAN)
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# Critical results closed loop + SLA escalation
# ---------------------------------------------------------------------------

def _final_report(rad_client, pid="pat-1", srid="sr-img-1", **kw):
    study_id, _ = _setup_study(rad_client, pid=pid, srid=srid, **kw)
    return study_id, _draft_and_verify_report(rad_client, study_id)


def test_critical_loop_readback_before_close(rad_client):
    _study_id, final = _final_report(
        rad_client, requester="Dr. Ordering Clinician")
    report_id = final["id"]
    r = rad_client.post(f"/radiology/reports/{report_id}/critical",
                        json={}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["state"] == "notified"
    assert body["ordering_clinician"] == "Dr. Ordering Clinician"
    assert body["task_id"]
    # The notification Task exists and points at the report + clinician.
    r = rad_client.get(f"/fhir/Task/{body['task_id']}", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["focus"]["reference"] == f"DiagnosticReport/{report_id}"
    assert r.json()["owner"]["display"] == "Dr. Ordering Clinician"
    assert r.json()["status"] == "requested"
    # Double-flag while open -> 409.
    r = rad_client.post(f"/radiology/reports/{report_id}/critical",
                        json={}, headers=CLINICIAN)
    assert r.status_code == 409
    # Closure before read-back -> 409.
    r = rad_client.post(f"/radiology/reports/{report_id}/critical/close",
                        headers=CLINICIAN)
    assert r.status_code == 409, r.text
    # Read-back needs a named reader AND the read-back text.
    r = rad_client.post(
        f"/radiology/reports/{report_id}/critical/read-back",
        json={"read_back_text": "PE present, anticoagulate."},
        headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = rad_client.post(
        f"/radiology/reports/{report_id}/critical/read-back",
        json={"reader": "Dr. Ordering Clinician"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = rad_client.post(
        f"/radiology/reports/{report_id}/critical/read-back",
        json={"reader": "Dr. Ordering Clinician",
              "read_back_text": "Critical result read back verbatim."},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "read-back"
    assert r.json()["readback_by"] == "Dr. Ordering Clinician"
    # Now closure succeeds and completes the Task.
    r = rad_client.post(f"/radiology/reports/{report_id}/critical/close",
                        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "closed"
    r = rad_client.get(f"/fhir/Task/{body['task_id']}", headers=CLINICIAN)
    assert r.json()["status"] == "completed"


def test_critical_flag_requires_final_report(rad_client):
    study_id, _ = _setup_study(rad_client, modality="DX")
    r = rad_client.post(f"/radiology/studies/{study_id}/report",
                        json={"conclusion": "Draft."}, headers=CLINICIAN)
    draft_id = r.json()["id"]
    r = rad_client.post(f"/radiology/reports/{draft_id}/critical",
                        json={}, headers=CLINICIAN)
    assert r.status_code == 409, r.text


def test_critical_scan_escalates_only_after_sla(rad_client):
    _study_id, final = _final_report(rad_client)
    report_id = final["id"]
    rad_client.post(f"/radiology/reports/{report_id}/critical",
                    json={}, headers=CLINICIAN)
    # Before the SLA (30 min): the scan finds nothing overdue and the
    # seeded rule creates no escalation Task.
    r = rad_client.post("/radiology/critical/scan", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0
    assert r.json()["overdue"] == 0
    with _session(rad_client) as session:
        tasks = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Task")).all()
        assert len(tasks) == 1  # only the notification Task
    # Age the flag past the SLA: the scan emits the event and the
    # seeded published rule opens the escalation Task.
    with _session(rad_client) as session:
        record = session.scalars(select(RadCriticalResult)).first()
        record.flagged_at = datetime.now(timezone.utc) \
            - timedelta(hours=2)
        session.commit()
    r = rad_client.post("/radiology/critical/scan", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1
    assert r.json()["escalated"][0]["report_id"] == report_id
    assert r.json()["escalated"][0]["firings"] == 1
    with _session(rad_client) as session:
        tasks = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Task")).all()
        titles = [(t.body or {}).get("description") for t in tasks]
        assert len(tasks) == 2
        assert any("unacknowledged past SLA" in (t or "")
                   for t in titles)
        assert verify_chain(session) is True
    # Idempotent: a second scan escalates nothing new.
    r = rad_client.post("/radiology/critical/scan", headers=CLINICIAN)
    assert r.json()["total"] == 0
    assert r.json()["overdue"] == 1  # still overdue, already escalated


# ---------------------------------------------------------------------------
# TAT stats — exact math
# ---------------------------------------------------------------------------

def _set_ordered_at(client, study_id, when):
    with _session(client) as session:
        entry = session.scalars(select(RadWorklistEntry).where(
            RadWorklistEntry.study_id == study_id)).first()
        assert entry is not None
        entry.ordered_at = when
        session.commit()


def test_tat_stats_exact_math(rad_client):
    # ROUTINE study reported exactly 90 minutes after ordering.
    routine_id, _ = _setup_study(rad_client, pid="pat-1", srid="sr-a",
                                 modality="DX")
    _transition(rad_client, routine_id, "scheduled")
    final = _draft_and_verify_report(rad_client, routine_id)
    issued = datetime.fromisoformat(final["issued"])
    _set_ordered_at(rad_client, routine_id, issued - timedelta(minutes=90))
    # STAT study reported exactly 120 minutes after ordering (target 60
    # -> a breach).
    stat_id, _ = _setup_study(rad_client, pid="pat-2", srid="sr-b",
                              modality="CT", priority="stat")
    _transition(rad_client, stat_id, "scheduled")
    final = _draft_and_verify_report(rad_client, stat_id)
    issued = datetime.fromisoformat(final["issued"])
    _set_ordered_at(rad_client, stat_id, issued - timedelta(minutes=120))
    # A third STAT study, never reported.
    _setup_study(rad_client, pid="pat-3", srid="sr-c", modality="CT",
                 priority="stat")
    r = rad_client.get("/radiology/stats/tat", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    stats = r.json()
    assert stats["total"] == 2
    assert stats["breaches"] == 1
    assert stats["unreported_stat"] == 1
    routine = stats["by_priority"]["ROUTINE"]
    assert routine["studies"] == 1
    assert routine["total_minutes"] == 90.0
    assert routine["avg_minutes"] == 90.0
    assert routine["breaches"] == 0
    assert routine["target_minutes"] == 1440
    stat = stats["by_priority"]["STAT"]
    assert stat["studies"] == 1
    assert stat["total_minutes"] == 120.0
    assert stat["avg_minutes"] == 120.0
    assert stat["breaches"] == 1
    assert stat["target_minutes"] == 60


def test_dashboard_widgets_bind_live_endpoints(rad_client):
    r = rad_client.get("/studio/config?kind=dashboard", headers=ADMIN)
    by_key = {row["key"]: row for row in r.json()}
    dashboard = by_key["radiology-operations"]
    assert dashboard["status"] == "published"
    for widget in dashboard["payload"]["widgets"]:
        # Phase 17 (BD-30 remainder): every radiology widget now
        # resolves through the governed indicator library — its
        # endpoint is the indicator value endpoint, and the live
        # radiology endpoint it was rewired from is preserved as
        # sourceEndpoint and must still serve the field/total the
        # indicator reproduces.
        assert widget["endpoint"].startswith("/indicators/")
        assert widget["endpoint"].endswith("/value")
        source = widget["sourceEndpoint"]
        assert source.startswith("/radiology/")
        r = rad_client.get(source, headers=CLINICIAN)
        assert r.status_code == 200, (source, r.text)
        body = r.json()
        if widget.get("sourceDisplay") == "value":
            assert widget["sourceField"] in body
            assert isinstance(body[widget["sourceField"]], (int, float))
        if widget.get("sourceDisplay") == "total":
            assert "total" in body
        r = rad_client.get(widget["endpoint"], headers=CLINICIAN)
        assert r.status_code == 200, (widget["endpoint"], r.text)
        assert "value" in r.json()


# ---------------------------------------------------------------------------
# Demo department seeding (guarded on the demo patients existing)
# ---------------------------------------------------------------------------

def test_demo_seed_only_when_patients_exist(rad_client):
    # No patients in the store: the startup ensure seeded nothing.
    r = rad_client.get("/radiology/worklist", headers=CLINICIAN)
    assert r.json()["total"] == 0
    for pid in ("pat-1", "pat-2", "pat-3"):
        _patient(rad_client, pid)
    with _session(rad_client) as session:
        ensure_radiology_defaults(session)
    r = rad_client.get("/radiology/worklist", headers=CLINICIAN)
    items = r.json()["items"]
    assert r.json()["total"] == 3
    by_state = {i["state"]: i for i in items}
    assert set(by_state) == {"ordered", "reported", "arrived"}
    assert by_state["ordered"]["priority"] == "STAT"
    assert by_state["ordered"]["report"] is None  # STAT, unreported
    assert by_state["reported"]["report"]["status"] == "final"
    # The reported + arrived demo studies carry archived pixel
    # instances that round-trip byte-exact through WADO-RS.
    for state in ("reported", "arrived"):
        study_uid = by_state[state]["study_uid"]
        r = rad_client.get(
            f"/radiology/dicomweb/studies/{study_uid}/series",
            headers=CLINICIAN)
        series_uid = r.json()[0]["0020000E"]["Value"][0]
        r = rad_client.get(
            f"/radiology/dicomweb/studies/{study_uid}/series/"
            f"{series_uid}/instances", headers=CLINICIAN)
        sop_uid = r.json()[0]["00080018"]["Value"][0]
        r = rad_client.get(
            f"/radiology/dicomweb/studies/{study_uid}/series/"
            f"{series_uid}/instances/{sop_uid}", headers=CLINICIAN)
        assert r.status_code == 200
        with _session(rad_client) as session:
            record = session.scalars(select(RadDicomInstance).where(
                RadDicomInstance.sop_uid == sop_uid)).first()
            assert hashlib.sha256(r.content).hexdigest() \
                == record.content_hash
    # Idempotent: re-running ensure adds nothing.
    with _session(rad_client) as session:
        ensure_radiology_defaults(session)
    r = rad_client.get("/radiology/worklist", headers=CLINICIAN)
    assert r.json()["total"] == 3


def test_audit_chain_intact_after_department_work(rad_client):
    study_id, study_uid = _setup_study(rad_client, modality="CT")
    _walk_to(rad_client, study_id, "arrived")
    rad_client.post(f"/radiology/worklist/{study_id}/protocol",
                    json={"protocol_code": "CT-CHEST-CONTRAST"},
                    headers=CLINICIAN)
    rad_client.post(
        f"/radiology/worklist/{study_id}/screening/"
        "radiology-screening-contrast",
        json={"answers": CONTRAST_ANSWERS}, headers=CLINICIAN)
    raw = _dicom_bytes(study_uid, _uid(), _uid())
    assert _ingest(rad_client, raw).status_code == 201
    with _session(rad_client) as session:
        assert verify_chain(session) is True
