"""Phase 16, WS-G2 / BD-48 — inbound fax / document AI intake.

Governed intake: bytes hash-stored (dedupe 409), text extracted
(pypdf for PDFs, import-guarded), the seeded DRAFT classifier agent
SUGGESTS patient + type through the app.agents run machinery, a HUMAN
confirms before a DocumentReference files to a chart, a confirmation
against the suggestion needs a reason, and unfile marks the filed
reference entered-in-error — nothing is ever deleted.

Honest-off: with the classifier still a draft (the seeded state) the
document parks in awaiting-classification and a human classifies
manually. The runtime transport is substituted in the classified
test (monkeypatch of app.agents._post_run, the Phase 10 pattern);
live fax hardware / carrier integration is NOT RUN.

Synthetic data only.

C-AI registered adjustment: classifier enablement runs the shared
tests.conftest.governance_for_enable flow (validation record +
approved statement) and the enable is performed by CONFIG; the
intake classifier path also consults the C-AI runtime gate. The
seeded draft's author is "system", so ADMIN may still publish it.
Assertions are unchanged in strength.
"""
import base64
import io
import json

import pytest
from sqlalchemy import select

from app import agents as agents_module
from app.models import AuditLog, FhirResource
from tests.conftest import ADMIN, CLINICIAN, CONFIG, governance_for_enable

TEXT_DOC = (b"Outside laboratory report for Patient/pat-intake-1\n"
            b"Sodium 140 mmol/L. Potassium 4.1 mmol/L.\n")


def _patient(client, pid="pat-intake-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic", "given": ["Intake"]}],
        "gender": "unknown"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return pid


def _receive(client, data=TEXT_DOC, filename="lab-report.txt",
             media_type="text/plain"):
    return client.post("/intake/documents", json={
        "filename": filename, "mediaType": media_type,
        "contentBase64": base64.b64encode(data).decode("ascii")},
        headers=CLINICIAN)


def _publish_and_enable_classifier(client):
    r = client.post("/studio/config/agent/document-intake-classifier"
                    "/versions/1/publish", headers=ADMIN)
    assert r.status_code == 200, r.text
    # BD-41 + C-AI gates: validation record + approved statement via
    # the shared governance helper, then enable by CONFIG.
    governance_for_enable(client, "document-intake-classifier")
    r = client.post("/agents/document-intake-classifier/enable",
                    headers=CONFIG)
    assert r.status_code == 200, r.text


def _stub_classifier(monkeypatch, patient_ref="Patient/pat-intake-1",
                     doc_type="lab-report"):
    def _ok(payload):
        return 200, {
            "output": json.dumps({"patientRef": patient_ref,
                                  "docType": doc_type}),
            "model": "ollama_chat/llama3.2:3b",
            "agent": "document-intake-classifier"}
    monkeypatch.setattr(agents_module, "_post_run", _ok)


def _audit_actions(client):
    factory = client.app.state.session_factory
    with factory() as session:
        return [row.action for row in session.scalars(select(AuditLog)).all()]


def _doc_ref(client, doc_id):
    factory = client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "DocumentReference",
            FhirResource.resource_id == doc_id)).first()
        return None if row is None else dict(row.body)


# ---------------------------------------------------------------------------
# End-to-end: receive -> extract -> classify (agent) -> confirm -> chart
# ---------------------------------------------------------------------------

def test_text_document_end_to_end(client, monkeypatch):
    _patient(client)
    _publish_and_enable_classifier(client)
    _stub_classifier(monkeypatch)
    r = _receive(client)
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["status"] == "classified"
    assert doc["suggested_patient_ref"] == "Patient/pat-intake-1"
    assert doc["suggested_type"] == "lab-report"
    assert doc["classifier_run_id"]
    assert doc["extracted_text"].startswith("Outside laboratory report")
    # The suggestion is backed by a real recorded AgentRun.
    runs = client.get("/agents/runs?agent=document-intake-classifier",
                      headers=CLINICIAN).json()
    assert [x["run_id"] for x in runs] == [doc["classifier_run_id"]]

    r = client.post(f"/intake/documents/{doc['id']}/confirm", json={
        "patientRef": "Patient/pat-intake-1", "docType": "lab-report"},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    filed = r.json()
    assert filed["status"] == "filed"
    assert filed["filed_by"] == "dev-clinician"
    filed_id = filed["filed_document_id"]
    assert filed_id
    # A real DocumentReference now exists in the patient's chart.
    chart = client.get("/notes?patient=Patient/pat-intake-1",
                       headers=CLINICIAN).json()
    assert filed_id in [n["id"] for n in chart["items"]]
    body = _doc_ref(client, filed_id)
    assert body["subject"] == {"reference": "Patient/pat-intake-1"}
    assert body["status"] == "current"
    provenance = [e for e in body.get("extension") or []
                  if e.get("url") == "urn:opencare:extension/intake-provenance"]
    assert len(provenance) == 1
    prov = json.loads(provenance[0]["valueString"])
    assert prov["intakeId"] == doc["id"]
    assert prov["classifierRunId"] == doc["classifier_run_id"]
    assert prov["sha256"] == doc["sha256"]
    actions = _audit_actions(client)
    for action in ("intake.received", "intake.extracted",
                   "intake.classified", "intake.filed"):
        assert action in actions


# ---------------------------------------------------------------------------
# PDF extraction (tiny PDF generated in-test with reportlab)
# ---------------------------------------------------------------------------

def test_pdf_document_extracts_text(client):
    reportlab = pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, "Referral letter for Patient/pat-intake-9")
    pdf.drawString(72, 700, "Please assess synthetic knee pain.")
    pdf.save()
    data = buffer.getvalue()
    assert data.startswith(b"%PDF")
    r = _receive(client, data=data, filename="referral.pdf",
                 media_type="application/pdf")
    assert r.status_code == 201, r.text
    doc = r.json()
    # pypdf present in the venv -> real extraction; the classifier is
    # still a seeded draft, so the document parks for a human.
    assert doc["status"] == "awaiting-classification", doc
    assert "Referral letter for Patient/pat-intake-9" in doc["extracted_text"]
    assert doc["extraction_error"] is None


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def test_duplicate_bytes_refused_409_with_existing_id(client):
    first = _receive(client)
    assert first.status_code == 201, first.text
    second = _receive(client, filename="same-fax-again.txt")
    assert second.status_code == 409, second.text
    body = second.json()
    assert body["existing_id"] == first.json()["id"]
    assert first.json()["id"] in body["detail"]
    listing = client.get("/intake/documents", headers=CLINICIAN).json()
    assert listing["total"] == 1


# ---------------------------------------------------------------------------
# Honest-off: no model/agent -> park + manual classification
# ---------------------------------------------------------------------------

def test_no_model_parks_and_manual_classify_works(client):
    _patient(client)
    # The classifier is only a seeded DRAFT: nothing can run.
    drafts = client.get("/studio/config?kind=agent", headers=ADMIN).json()
    by_key = {d["key"]: d for d in drafts}
    assert by_key["document-intake-classifier"]["status"] == "draft"
    r = _receive(client)
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["status"] == "awaiting-classification"
    assert doc["suggested_patient_ref"] is None
    assert doc["classifier_run_id"] is None
    # Confirm is refused until classified.
    r = client.post(f"/intake/documents/{doc['id']}/confirm", json={
        "patientRef": "Patient/pat-intake-1", "docType": "lab-report"},
        headers=CLINICIAN)
    assert r.status_code == 409
    # A human classifies manually; filing then works end-to-end.
    r = client.post(f"/intake/documents/{doc['id']}/classify", json={
        "patientRef": "Patient/pat-intake-1", "docType": "lab-report"},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "classified"
    assert "intake.classify" in _audit_actions(client)
    r = client.post(f"/intake/documents/{doc['id']}/confirm", json={
        "patientRef": "Patient/pat-intake-1", "docType": "lab-report"},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "filed"


def test_enabled_classifier_without_model_parks(client, monkeypatch):
    _patient(client)
    _publish_and_enable_classifier(client)

    def _no_model(payload):
        return 503, {"error": "No model configured: set a model on the "
                              "agent config or AGENT_MODEL on the ADK "
                              "runtime. No answer was generated."}

    monkeypatch.setattr(agents_module, "_post_run", _no_model)
    r = _receive(client)
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["status"] == "awaiting-classification"
    assert doc["classifier_run_id"] is None
    assert "No model configured" in doc["classification_note"]
    # A 503 records NOTHING as a run (the agents.py honest-off rule).
    runs = client.get("/agents/runs?agent=document-intake-classifier",
                      headers=CLINICIAN).json()
    assert runs == []


# ---------------------------------------------------------------------------
# Misfile safeguards: override reason + unfile
# ---------------------------------------------------------------------------

def test_confirm_to_different_patient_requires_reason(client, monkeypatch):
    _patient(client, "pat-intake-1")
    _patient(client, "pat-intake-2")
    _publish_and_enable_classifier(client)
    _stub_classifier(monkeypatch)  # suggests pat-intake-1
    doc = _receive(client).json()
    assert doc["suggested_patient_ref"] == "Patient/pat-intake-1"
    r = client.post(f"/intake/documents/{doc['id']}/confirm", json={
        "patientRef": "Patient/pat-intake-2", "docType": "lab-report"},
        headers=CLINICIAN)
    assert r.status_code == 422, r.text
    assert "reason" in r.json()["detail"]
    r = client.post(f"/intake/documents/{doc['id']}/confirm", json={
        "patientRef": "Patient/pat-intake-2", "docType": "lab-report",
        "reason": "Cover sheet MRN was the sibling's; verified by phone"},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["confirmed_patient_ref"] == "Patient/pat-intake-2"
    body = _doc_ref(client, r.json()["filed_document_id"])
    assert body["subject"] == {"reference": "Patient/pat-intake-2"}


def test_unfile_marks_entered_in_error_and_deletes_nothing(client,
                                                            monkeypatch):
    _patient(client)
    _publish_and_enable_classifier(client)
    _stub_classifier(monkeypatch)
    doc = _receive(client).json()
    filed = client.post(f"/intake/documents/{doc['id']}/confirm", json={
        "patientRef": "Patient/pat-intake-1", "docType": "lab-report"},
        headers=CLINICIAN).json()
    filed_id = filed["filed_document_id"]
    r = client.post(f"/intake/documents/{doc['id']}/unfile", json={
        "reason": "Filed to the wrong chart"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "classified"
    # The DocumentReference still exists — marked entered-in-error.
    body = _doc_ref(client, filed_id)
    assert body is not None
    assert body["status"] == "entered-in-error"
    got = client.get(f"/fhir/DocumentReference/{filed_id}", headers=CLINICIAN)
    assert got.status_code == 200
    assert got.json()["status"] == "entered-in-error"
    assert "intake.unfiled" in _audit_actions(client)
    # The intake document can be re-filed to the correct chart.
    r = client.post(f"/intake/documents/{doc['id']}/confirm", json={
        "patientRef": "Patient/pat-intake-1", "docType": "lab-report"},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["filed_document_id"] != filed_id
