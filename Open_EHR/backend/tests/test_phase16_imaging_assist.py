"""Phase 16 (WS-E, Scope F) — radiology vision-model agentic assist
(app.imaging_assist).

A governed multimodal copilot over Phase 15's DICOM pixel pipeline:
the seeded DRAFT agent `radiology-vision-assistant` (image modality,
empty model route) drafts structured report sections from converted
PNG frames + order/protocol/prior-report context; the draft persists
as an ImagingAssistDraft; disposition rides the EXISTING AgentRun
disposition flow; accept-into-report creates a PRELIMINARY
DiagnosticReport through boards' radiology report flow — and nothing
else. The agent transport is substituted (app.agents._post_run);
a live vision-model run is NOT RUN.

Synthetic data only. DICOM objects are generated with pydicom
directly (small synthetic pixel data), like the Phase 15 suite.

C-AI registered adjustment: the seeded vision agent is stamped
riskTier T3, so enablement runs the shared
tests.conftest.governance_for_enable flow (validation record +
statement approved with the T3 approval kinds) and the enable is
performed by CONFIG, not the statement author. The seeded draft's
author is "system", so ADMIN may still publish it. Assertions are
unchanged in strength.
"""
import base64
import io
import uuid

import pytest
from sqlalchemy import select

from app import agents as agents_module
from app.imaging_assist import ImagingAssistDraft

from .conftest import (ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER,
                       governance_for_enable)


@pytest.fixture()
def ia_client(client, tmp_path, monkeypatch):
    """The standard conftest client, with the tiering cold store
    pointed at the test's tmp dir (resolved at call time)."""
    monkeypatch.setenv("OPENCARE_COLD_DIR", str(tmp_path / "cold-store"))
    return client


STRUCTURED_OUTPUT = (
    "FINDINGS:\n"
    "No focal consolidation. Cardiomediastinal silhouette is normal. "
    "Compared with the prior study, no interval change.\n"
    "IMPRESSION:\n"
    "No acute cardiopulmonary process.\n"
    "PROTOCOL_SUGGESTION:\n"
    "CT chest with IV contrast per protocol CT-CHEST-CONTRAST.\n"
    "TRIAGE_SUGGESTION:\n"
    "Routine — preliminary draft for radiologist review within 24h.\n"
    "MEASUREMENTS:\n"
    "- Heart diameter: 12.5 cm\n"
    "- Aortic diameter: 3.1 cm\n"
)

PRIOR_CONCLUSION = "Prior study: lungs clear, no effusion. (Synthetic.)"


# ---------------------------------------------------------------------------
# Helpers (the Phase 15 radiology suite's shapes)
# ---------------------------------------------------------------------------

def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic", "given": ["Pat"]}],
        "gender": "female", "birthDate": "1980-05-01",
    }, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _service_request(client, srid, pid="pat-1", priority=None):
    body = {
        "resourceType": "ServiceRequest", "id": srid, "status": "active",
        "intent": "order",
        "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt",
                             "code": "71260",
                             "display": "CT chest with contrast"}]},
        "subject": {"reference": f"Patient/{pid}"},
    }
    if priority:
        body["priority"] = priority
    r = client.post("/fhir/ServiceRequest", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _study(client, srid, modality="CT"):
    r = client.post("/radiology/studies",
                    json={"service_request_id": srid,
                          "modality": modality},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    item = r.json()
    return item["study"]["id"], item["study_uid"]


def _uid():
    return f"2.25.{uuid.uuid4().int}"


def _dicom_bytes(study_uid, series_uid, sop_uid, modality="CT",
                 patient_id="pat-1", pixel=bytes([0, 85, 170, 255]),
                 with_pixels=True):
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
    if with_pixels:
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


def _ingest(client, raw):
    r = client.post(
        "/radiology/dicom/ingest",
        json={"dicom_base64": base64.b64encode(raw).decode("ascii")},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _enable_vision_agent(client):
    r = client.post(
        "/studio/config/agent/radiology-vision-assistant/versions/1"
        "/publish", headers=ADMIN)
    assert r.status_code == 200, r.text
    # BD-41 + C-AI gates: validation record + approved T3 statement
    # via the shared governance helper, then enable by CONFIG.
    governance_for_enable(client, "radiology-vision-assistant")
    r = client.post("/agents/radiology-vision-assistant/enable",
                    headers=CONFIG)
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True


def _stub_transport(monkeypatch, output=STRUCTURED_OUTPUT, captured=None):
    def _ok(payload):
        if captured is not None:
            captured.update(payload)
        return 200, {"output": output,
                     "model": "stub-vision-model",
                     "agent": "radiology-vision-assistant"}
    monkeypatch.setattr(agents_module, "_post_run", _ok)


def _setup_study_with_pixels(client, with_prior=False):
    """(study_id, study_uid, prior_study_id|None) — a CT study with one
    archived pixel instance and an assigned protocol; optionally a
    prior DX study of the same patient with a VERIFIED report."""
    _patient(client, "pat-1")
    prior_id = None
    if with_prior:
        _service_request(client, "sr-prior")
        prior_id, prior_uid = _study(client, "sr-prior", modality="DX")
        r = client.post(f"/radiology/studies/{prior_id}/report",
                        json={"conclusion": PRIOR_CONCLUSION},
                        headers=CLINICIAN)
        assert r.status_code == 201, r.text
        r = client.post(f"/radiology/reports/{r.json()['id']}/verify",
                        headers=CLINICIAN)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "final"
    _service_request(client, "sr-current", priority="stat")
    study_id, study_uid = _study(client, "sr-current", modality="CT")
    _ingest(client, _dicom_bytes(study_uid, _uid(), _uid()))
    r = client.post(f"/radiology/worklist/{study_id}/protocol",
                    json={"protocol_code": "CT-CHEST-CONTRAST"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return study_id, study_uid, prior_id


def _draft(client, study_id, **kw):
    return client.post(f"/imaging-assist/studies/{study_id}/draft",
                       json=kw, headers=CLINICIAN)


def _draft_rows(client):
    factory = client.app.state.session_factory
    with factory() as session:
        return session.scalars(select(ImagingAssistDraft)).all()


# ---------------------------------------------------------------------------
# Seed governance
# ---------------------------------------------------------------------------

def test_vision_agent_seeded_as_draft_with_image_modality(ia_client):
    rows = ia_client.get("/studio/config?kind=agent",
                         headers=ADMIN).json()
    by_key = {row["key"]: row for row in rows}
    seeded = by_key["radiology-vision-assistant"]
    assert seeded["status"] == "draft"
    assert seeded["payload"]["modalities"] == {
        "text": True, "image": True, "audio": False}
    assert seeded["payload"]["model"] == ""  # empty model route
    # A draft cannot run: the runnable roster does not list it.
    roster = ia_client.get("/agents", headers=CLINICIAN).json()
    assert all(a["key"] != "radiology-vision-assistant"
               for a in roster["agents"])


# ---------------------------------------------------------------------------
# Draft creation — pixels, prompt context, structured storage
# ---------------------------------------------------------------------------

def test_draft_stores_all_sections_and_names_prior(ia_client,
                                                    monkeypatch):
    study_id, _uid_str, prior_id = _setup_study_with_pixels(
        ia_client, with_prior=True)
    _enable_vision_agent(ia_client)
    captured = {}
    _stub_transport(monkeypatch, captured=captured)
    r = _draft(ia_client, study_id,
               instruction="Compare carefully with the prior.")
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["studyId"] == study_id
    assert draft["status"] == "draft"
    assert draft["findingsDraft"].startswith("No focal consolidation.")
    assert draft["impressionDraft"] == "No acute cardiopulmonary process."
    assert draft["protocolSuggestion"] == \
        "CT chest with IV contrast per protocol CT-CHEST-CONTRAST."
    assert draft["triageSuggestion"].startswith("Routine")
    assert draft["measurements"] == [
        {"name": "Heart diameter", "value": 12.5, "unit": "cm"},
        {"name": "Aortic diameter", "value": 3.1, "unit": "cm"}]
    # Prior comparison: the response names the prior study used, and
    # the prompt the runtime received carries its report text, the
    # order and the assigned protocol.
    assert draft["priorStudyId"] == prior_id
    assert draft["agentRunId"]
    assert draft["skipped_instances"] == []
    assert draft["framesConverted"] == 1
    assert captured["agent"]["key"] == "radiology-vision-assistant"
    prompt = captured["prompt"]
    assert prior_id in prompt
    assert PRIOR_CONCLUSION in prompt
    assert "CT chest with contrast" in prompt
    assert "CT-CHEST-CONTRAST" in prompt
    assert "Compare carefully with the prior." in prompt
    # The image part is a real PNG converted from the DICOM pixels.
    png = base64.b64decode(captured["imageBase64"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert captured["imageMime"] == "image/png"
    # Exactly one draft row + one AgentRun were recorded.
    assert len(_draft_rows(ia_client)) == 1
    runs = ia_client.get("/agents/runs", headers=CLINICIAN).json()
    assert [run["run_id"] for run in runs] == [draft["agentRunId"]]
    assert runs[0]["disposition"] == "pending"


def test_draft_without_prior_names_none(ia_client, monkeypatch):
    study_id, _uid_str, _ = _setup_study_with_pixels(ia_client)
    _enable_vision_agent(ia_client)
    captured = {}
    _stub_transport(monkeypatch, captured=captured)
    r = _draft(ia_client, study_id)
    assert r.status_code == 201, r.text
    assert r.json()["priorStudyId"] is None
    assert "No prior study report" in captured["prompt"]


# ---------------------------------------------------------------------------
# Accept into report — PRELIMINARY only, verified state untouched
# ---------------------------------------------------------------------------

def test_accept_into_report_creates_preliminary_only(ia_client,
                                                     monkeypatch):
    study_id, _uid_str, _ = _setup_study_with_pixels(
        ia_client, with_prior=True)
    _enable_vision_agent(ia_client)
    _stub_transport(monkeypatch)
    draft = _draft(ia_client, study_id).json()

    r = ia_client.post(
        f"/imaging-assist/drafts/{draft['id']}/accept-into-report",
        json={}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    accepted = r.json()
    assert accepted["status"] == "accepted"
    assert accepted["reportId"]
    assert accepted["report"] == {"id": accepted["reportId"],
                                  "status": "preliminary"}
    # The chart report is preliminary, carries the draft's sections,
    # and NO report for this study is in a verified state.
    report = ia_client.get(
        f"/fhir/DiagnosticReport/{accepted['reportId']}",
        headers=CLINICIAN).json()
    assert report["status"] == "preliminary"
    assert "Findings: No focal consolidation." in report["conclusion"]
    assert "Impression: No acute cardiopulmonary process." \
        in report["conclusion"]
    detail = ia_client.get(f"/radiology/studies/{study_id}",
                           headers=CLINICIAN).json()
    assert [rep["status"] for rep in detail["reports"]] == \
        ["preliminary"]
    # The underlying AgentRun was disposed through the existing flow.
    runs = ia_client.get("/agents/runs", headers=CLINICIAN).json()
    assert runs[0]["disposition"] == "accepted"
    # Accepting twice is refused.
    r = ia_client.post(
        f"/imaging-assist/drafts/{draft['id']}/accept-into-report",
        json={}, headers=CLINICIAN)
    assert r.status_code == 409, r.text


def test_accept_with_edited_text_marks_draft_edited(ia_client,
                                                    monkeypatch):
    study_id, _uid_str, _ = _setup_study_with_pixels(ia_client)
    _enable_vision_agent(ia_client)
    _stub_transport(monkeypatch)
    draft = _draft(ia_client, study_id).json()
    edited = "Radiologist-edited conclusion: normal study."
    r = ia_client.post(
        f"/imaging-assist/drafts/{draft['id']}/accept-into-report",
        json={"editedText": edited}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "edited"
    report = ia_client.get(
        f"/fhir/DiagnosticReport/{r.json()['reportId']}",
        headers=CLINICIAN).json()
    assert report["status"] == "preliminary"
    assert report["conclusion"] == edited


# ---------------------------------------------------------------------------
# Reject path — disposition rides the AgentRun
# ---------------------------------------------------------------------------

def test_reject_path_via_agent_run_disposition(ia_client, monkeypatch):
    study_id, _uid_str, _ = _setup_study_with_pixels(ia_client)
    _enable_vision_agent(ia_client)
    _stub_transport(monkeypatch)
    draft = _draft(ia_client, study_id).json()
    r = ia_client.post(
        f"/agents/runs/{draft['agentRunId']}/disposition",
        json={"disposition": "rejected"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = ia_client.get(f"/imaging-assist/drafts/{draft['id']}",
                      headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"
    # A rejected draft can never be accepted into a report.
    r = ia_client.post(
        f"/imaging-assist/drafts/{draft['id']}/accept-into-report",
        json={}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    detail = ia_client.get(f"/radiology/studies/{study_id}",
                           headers=CLINICIAN).json()
    assert detail["reports"] == []


# ---------------------------------------------------------------------------
# Honest-off + no-pixel / unconvertible refusals
# ---------------------------------------------------------------------------

def test_no_model_503_writes_nothing(ia_client, monkeypatch):
    study_id, _uid_str, _ = _setup_study_with_pixels(ia_client)
    _enable_vision_agent(ia_client)

    def _no_model(payload):
        return 503, {"error": "No model configured: set a model on "
                              "the agent config or AGENT_MODEL on the "
                              "ADK runtime. No answer was generated."}

    monkeypatch.setattr(agents_module, "_post_run", _no_model)
    r = _draft(ia_client, study_id)
    assert r.status_code == 503, r.text
    assert "No model configured" in r.json()["detail"]
    # Nothing was written: no draft row, no AgentRun.
    assert _draft_rows(ia_client) == []
    assert ia_client.get("/agents/runs", headers=CLINICIAN).json() == []
    r = ia_client.get(f"/imaging-assist/studies/{study_id}/drafts",
                      headers=CLINICIAN)
    assert r.json() == {"items": [], "total": 0}


def test_study_without_pixels_is_422(ia_client, monkeypatch):
    _patient(ia_client, "pat-1")
    _service_request(ia_client, "sr-bare")
    study_id, _uid_str = _study(ia_client, "sr-bare", modality="CT")
    _enable_vision_agent(ia_client)
    _stub_transport(monkeypatch)
    r = _draft(ia_client, study_id)
    assert r.status_code == 422, r.text
    assert "no archived DICOM instances" in r.json()["detail"]
    assert _draft_rows(ia_client) == []


def test_unconvertible_frame_is_422_with_reason(ia_client, monkeypatch):
    _patient(ia_client, "pat-1")
    _service_request(ia_client, "sr-nopix")
    study_id, study_uid = _study(ia_client, "sr-nopix", modality="CT")
    # A well-formed DICOM object that carries NO PixelData element:
    # ingest archives it, but there is no frame to convert.
    _ingest(ia_client, _dicom_bytes(study_uid, _uid(), _uid(),
                                    with_pixels=False))
    _enable_vision_agent(ia_client)
    _stub_transport(monkeypatch)
    r = _draft(ia_client, study_id)
    assert r.status_code == 422, r.text
    assert "no convertible pixel frames" in r.json()["detail"]
    assert "PixelData" in r.json()["detail"]
    assert _draft_rows(ia_client) == []


def test_draft_capability_gates(ia_client, monkeypatch):
    study_id, _uid_str, _ = _setup_study_with_pixels(ia_client)
    _enable_vision_agent(ia_client)
    _stub_transport(monkeypatch)
    r = ia_client.post(f"/imaging-assist/studies/{study_id}/draft",
                       json={}, headers=VIEWER)
    assert r.status_code == 403, r.text
    r = ia_client.post(f"/imaging-assist/studies/{study_id}/draft",
                       json={}, headers=PATIENT)
    assert r.status_code == 403, r.text
    assert _draft_rows(ia_client) == []
