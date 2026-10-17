"""Phase 5 — clinical documentation (backend/app/documentation.py).

Notes are DocumentReference resources whose structured content comes
from published Studio FORM templates (Questionnaire): the note stores
the QuestionnaireResponse reference plus a Composition-lite summary.
Amendment creates a NEW DocumentReference linked by relatesTo; the
original is never modified. Flowsheets are QuestionnaireResponse
collections over published forms, read back as time-series rows.
"""
from tests.conftest import ADMIN, CLINICIAN, PATIENT, VIEWER


def _patient(client, pid="doc-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synth", "given": ["Doc"]}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


PROGRESS_ANSWERS = [
    {"linkId": "subjective", "answer": [{"valueString": "Feels better"}]},
    {"linkId": "assessment", "answer": [{"valueString": "Improving"}]},
    {"linkId": "plan", "answer": [{"valueString": "Continue current plan"}]},
]

VITALS_ANSWERS = [
    {"linkId": "systolic", "answer": [{"valueInteger": 121}]},
    {"linkId": "heart-rate", "answer": [{"valueInteger": 71}]},
    {"linkId": "spo2", "answer": [{"valueInteger": 99}]},
]


def _note(client, pid, answers=PROGRESS_ANSWERS, headers=CLINICIAN, **kw):
    body = {"patient": pid, "note_type": "progress", "answers": answers}
    body.update(kw)
    return client.post("/notes", json=body, headers=headers)


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

def test_note_types_come_from_published_config(client):
    r = client.get("/notes/types", headers=CLINICIAN)
    assert r.status_code == 200
    codes = {c["code"] for c in r.json()["note_types"]["concepts"]}
    assert {"progress", "hp", "procedure", "discharge"} <= codes


def test_note_from_template_stores_response_and_content(client):
    pid = _patient(client)
    r = _note(client, pid)
    assert r.status_code == 201, r.text
    note = r.json()
    assert note["resourceType"] == "DocumentReference"
    assert note["docStatus"] == "preliminary"
    content = note["noteContent"]
    assert content["templateForm"] == "note-progress"
    # The answers live in a real QuestionnaireResponse resource.
    resp = client.get(
        f"/fhir/QuestionnaireResponse/{content['questionnaireResponseId']}",
        headers=CLINICIAN)
    assert resp.status_code == 200
    assert resp.json()["questionnaire"] == "note-progress"
    answers = {item["linkId"]: item["answer"][0]["valueString"]
               for item in resp.json()["item"]}
    assert answers["assessment"] == "Improving"


def test_note_template_required_answers_enforced(client):
    pid = _patient(client)
    r = _note(client, pid, answers=[
        {"linkId": "subjective", "answer": [{"valueString": "x"}]}])
    assert r.status_code == 422
    assert "assessment" in r.json()["detail"]


def test_unknown_note_type_is_422(client):
    pid = _patient(client)
    r = client.post("/notes", json={"patient": pid, "note_type": "opera"},
                    headers=CLINICIAN)
    assert r.status_code == 422


def test_free_text_note_without_template(client):
    pid = _patient(client)
    r = client.post("/notes", json={"patient": pid, "note_type": "progress",
                                    "text": "Seen at bedside."},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["noteContent"]["text"] == "Seen at bedside."
    assert r.json()["noteContent"]["questionnaireResponseId"] is None


def test_sign_lifecycle_and_double_sign_409(client):
    pid = _patient(client)
    nid = _note(client, pid).json()["id"]
    r = client.post(f"/notes/{nid}/sign", headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["docStatus"] == "final"
    assert client.post(f"/notes/{nid}/sign",
                       headers=CLINICIAN).status_code == 409


def test_amend_creates_linked_version_original_immutable(client):
    pid = _patient(client)
    nid = _note(client, pid).json()["id"]
    # A draft cannot be amended — it can still be edited by signing flow.
    assert client.post(f"/notes/{nid}/amend", json={},
                       headers=CLINICIAN).status_code == 409
    signed = client.post(f"/notes/{nid}/sign", headers=CLINICIAN).json()
    r = client.post(f"/notes/{nid}/amend",
                    json={"text": "Correction: dose was 5 mg, not 50 mg."},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    amended = r.json()
    assert amended["id"] != nid
    assert amended["docStatus"] == "amended"
    assert amended["relatesTo"][0]["target"]["reference"] == \
        f"DocumentReference/{nid}"
    assert amended["noteContent"]["text"] == \
        "Correction: dose was 5 mg, not 50 mg."
    # The original is byte-identical to its signed state.
    original = client.get(f"/notes/{nid}", headers=CLINICIAN).json()
    assert original["meta"]["versionId"] == signed["meta"]["versionId"]
    assert original["docStatus"] == "final"
    assert original["noteContent"]["text"] is None
    # Both appear in the patient's note list.
    listed = client.get(f"/notes?patient={pid}", headers=CLINICIAN).json()
    assert listed["total"] == 2


def test_viewer_can_read_but_not_write_notes(client):
    pid = _patient(client)
    nid = _note(client, pid).json()["id"]
    assert client.get(f"/notes/{nid}", headers=VIEWER).status_code == 200
    assert _note(client, pid, headers=VIEWER).status_code == 403
    assert client.post(f"/notes/{nid}/sign",
                       headers=VIEWER).status_code == 403


def test_patient_cannot_read_another_patients_notes(client):
    pid = _patient(client)
    _note(client, pid)
    # PATIENT token is linked to Patient/pat-1, not to doc-1.
    r = client.get(f"/notes?patient={pid}", headers=PATIENT)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Flowsheets
# ---------------------------------------------------------------------------

def test_flowsheet_submit_and_time_series_view(client):
    pid = _patient(client)
    r = client.post("/flowsheets", json={
        "patient": pid, "form": "flowsheet-vitals",
        "answers": VITALS_ANSWERS}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["resourceType"] == "QuestionnaireResponse"
    r = client.post("/flowsheets", json={
        "patient": pid, "form": "flowsheet-vitals",
        "answers": [{"linkId": "systolic", "answer": [{"valueInteger": 118}]},
                    {"linkId": "heart-rate", "answer": [{"valueInteger": 68}]},
                    {"linkId": "spo2", "answer": [{"valueInteger": 98}]}]},
        headers=CLINICIAN)
    assert r.status_code == 201
    view = client.get(f"/flowsheets/flowsheet-vitals?patient={pid}",
                      headers=CLINICIAN).json()
    assert view["total"] == 2
    link_ids = {i["linkId"] for i in view["form"]["items"]}
    assert {"systolic", "heart-rate", "spo2"} <= link_ids
    first = view["entries"][0]["answers"]
    assert first["systolic"]["value"] == 121
    assert first["heart-rate"]["value"] == 71
    assert view["entries"][1]["answers"]["systolic"]["value"] == 118


def test_flowsheet_missing_required_item_is_422(client):
    pid = _patient(client)
    r = client.post("/flowsheets", json={
        "patient": pid, "form": "flowsheet-vitals",
        "answers": [{"linkId": "diastolic",
                     "answer": [{"valueInteger": 80}]}]}, headers=CLINICIAN)
    assert r.status_code == 422
    assert "systolic" in r.json()["detail"]


def test_flowsheet_unpublished_form_is_422(client):
    pid = _patient(client)
    r = client.post("/flowsheets", json={
        "patient": pid, "form": "flowsheet-does-not-exist",
        "answers": []}, headers=CLINICIAN)
    assert r.status_code == 422


def test_flowsheet_entries_are_patient_scoped(client):
    pid = _patient(client, "doc-fs-a")
    other = _patient(client, "doc-fs-b")
    client.post("/flowsheets", json={
        "patient": pid, "form": "flowsheet-vitals",
        "answers": VITALS_ANSWERS}, headers=CLINICIAN)
    view = client.get(f"/flowsheets/flowsheet-vitals?patient={other}",
                      headers=CLINICIAN).json()
    assert view["total"] == 0
