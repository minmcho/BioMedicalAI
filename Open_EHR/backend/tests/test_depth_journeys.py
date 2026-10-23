"""Depth pass (Workstream 2) — clinical journeys deepened.

Three governed additions, each proven end-to-end with its config gate:

1. PERIOPERATIVE — the WHO surgical safety checklist as ONE governed
   form (form/surgical-safety-checklist, three sections), recorded per
   section on the surgery case, surfaced on the board payload, and
   enforced by the safetyChecklistGate in workflow/surgery-flow
   (seeded TRUE): in-op requires time-out, done requires sign-out.
2. INPATIENT — the interdisciplinary rounds checklist
   (form/rounds-checklist) on active admissions, surfaced on the
   rounds list as rounds_checklist_today.
3. DISCHARGE — medication reconciliation (form/discharge-med-rec) as
   the readiness board's fourth element (medrec_done), joining
   `missing` only when the published continuum-ops guard
   dischargeGuards.requireMedRec is flipped on (ships FALSE).
"""
import datetime as dt

from .conftest import ADMIN, CLINICIAN, VIEWER

SIGNIN = ["signin-identity-site-procedure", "signin-consent",
          "signin-anaesthesia-check", "signin-allergies",
          "signin-airway-risk"]
TIMEOUT = ["timeout-introductions", "timeout-verbal-confirmation",
           "timeout-antibiotics", "timeout-imaging", "timeout-concerns"]
SIGNOUT = ["signout-procedure-recorded", "signout-counts",
           "signout-specimen", "signout-recovery"]

ROUNDS_REQUIRED = ["overnight-events", "lines-devices-reviewed",
                   "vte-prophylaxis"]
MEDREC_REQUIRED = ["home-medications-reviewed", "medication-changes",
                   "allergies-rechecked"]


def _bool_answers(codes):
    return [{"linkId": c, "answer": [{"valueBoolean": True}]} for c in codes]


def _patient(client, pid, birth="1980-01-01"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Depth", "given": ["Test"]}],
        "birthDate": birth, "gender": "female"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _case(client, pid="dj-pat-1", day="2026-11-02"):
    r = client.post("/surgery/cases", json={
        "patient_id": pid, "code": "27447",
        "display": "Total knee arthroplasty", "room": "OR-1",
        "start": f"{day}T08:00:00Z", "end": f"{day}T10:00:00Z"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["case"]["id"]


def _transition(client, case_id, target):
    return client.post(f"/surgery/cases/{case_id}/transition",
                       json={"to_status": target}, headers=CLINICIAN)


def _safety(client, case_id, section, codes, headers=CLINICIAN):
    return client.post(f"/surgery/cases/{case_id}/safety-checklist",
                       json={"section": section,
                             "answers": _bool_answers(codes)},
                       headers=headers)


def _board_entry(client, case_id, day="2026-11-02"):
    board = client.get("/surgery/board", params={"date": day},
                       headers=CLINICIAN).json()
    for room in board["rooms"]:
        for entry in room["cases"]:
            if entry["case"]["id"] == case_id:
                return entry
    raise AssertionError(f"case {case_id} not on board")


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


def _publish_surgery_flow(client, mutate):
    r = client.get("/studio/config/workflow/effective/surgery-flow",
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    payload = r.json()["payload"]
    mutate(payload)
    r = client.post("/studio/config/workflow/surgery-flow",
                    json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/workflow/surgery-flow/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text


def _admit_bed(client, pid, bed="MS-101A"):
    r = client.post("/inpatient/admissions",
                    json={"patient": pid, "bed": bed}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["encounter"]["id"]


def _config_rows(client, kind, key):
    r = client.get(f"/studio/config/{kind}/{key}", headers=ADMIN)
    assert r.status_code == 200, (kind, key, r.text)
    return r.json()


# ---------------------------------------------------------------------------
# 1) WHO surgical safety checklist
# ---------------------------------------------------------------------------

def test_safety_checklist_form_and_gate_seeded(client):
    rows = _config_rows(client, "form", "surgical-safety-checklist")
    published = [r for r in rows if r["status"] == "published"]
    assert published, "surgical-safety-checklist must be seeded published"
    groups = {item["linkId"]: item
              for item in published[0]["payload"]["item"]}
    assert set(groups) == {"sign-in", "time-out", "sign-out"}
    codes = {child["linkId"] for group in groups.values()
             for child in group["item"]}
    assert codes == set(SIGNIN + TIMEOUT + SIGNOUT)
    assert all(child["required"] is True and child["type"] == "boolean"
               for group in groups.values() for child in group["item"])
    eff = client.get("/studio/config/workflow/effective/surgery-flow",
                     headers=CLINICIAN).json()
    assert eff["payload"]["safetyChecklistGate"] is True


def test_safety_checklist_sections_record_and_surface_on_board(client):
    _patient(client, "dj-pat-1")
    case_id = _case(client)
    entry = _board_entry(client, case_id)
    assert entry["safety_checklist"] == {
        "sign-in": False, "time-out": False, "sign-out": False}
    r = _safety(client, case_id, "sign-in", SIGNIN)
    assert r.status_code == 201, r.text
    assert r.json()["section"] == "sign-in"
    assert r.json()["safety_checklist"] == {
        "sign-in": True, "time-out": False, "sign-out": False}
    assert r.json()["response"]["questionnaire"] == "surgical-safety-checklist"
    entry = _board_entry(client, case_id)
    assert entry["safety_checklist"]["sign-in"] is True
    # Grouped (FormRenderer-shaped) answers are accepted too.
    grouped = [{"linkId": "time-out", "item": [
        {"linkId": c, "answer": [{"valueBoolean": True}]} for c in TIMEOUT]}]
    r = client.post(f"/surgery/cases/{case_id}/safety-checklist",
                    json={"section": "time-out", "answers": grouped},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    entry = _board_entry(client, case_id)
    assert entry["safety_checklist"]["time-out"] is True


def test_safety_checklist_incomplete_section_refused(client):
    _patient(client, "dj-pat-2")
    case_id = _case(client, pid="dj-pat-2")
    r = _safety(client, case_id, "time-out", TIMEOUT[:3])
    assert r.status_code == 422
    assert "timeout-imaging" in r.json()["detail"]
    assert "timeout-concerns" in r.json()["detail"]
    r = client.post(f"/surgery/cases/{case_id}/safety-checklist",
                    json={"section": "bogus", "answers": []},
                    headers=CLINICIAN)
    assert r.status_code == 422
    # A viewer cannot record the checklist (surgery.work gate).
    r = _safety(client, case_id, "sign-in", SIGNIN, headers=VIEWER)
    assert r.status_code == 403
    entry = _board_entry(client, case_id)
    assert entry["safety_checklist"] == {
        "sign-in": False, "time-out": False, "sign-out": False}


def test_safety_gate_blocks_inop_and_done_until_sections_recorded(client):
    _patient(client, "dj-pat-3")
    case_id = _case(client, pid="dj-pat-3")
    assert _transition(client, case_id, "pre-op").status_code == 200
    r = _transition(client, case_id, "in-op")
    assert r.status_code == 409
    assert "time-out" in r.json()["detail"]
    assert _safety(client, case_id, "time-out", TIMEOUT).status_code == 201
    assert _transition(client, case_id, "in-op").status_code == 200
    assert _transition(client, case_id, "pacu").status_code == 200
    r = _transition(client, case_id, "done")
    assert r.status_code == 409
    assert "sign-out" in r.json()["detail"]
    assert _safety(client, case_id, "sign-out", SIGNOUT).status_code == 201
    # Phase 15 (E): with the sign-out recorded, the seeded-ON PACU
    # discharge gate (pacuScoreGate in workflow/surgery-flow) is the
    # next refusal on pacu -> done — 409 naming the missing score.
    r = _transition(client, case_id, "done")
    assert r.status_code == 409
    assert "PACU" in r.json()["detail"]
    r = client.post(f"/surgery/cases/{case_id}/pacu-score",
                    json={"scores": {"activity": 2, "respiration": 2,
                                     "circulation": 2, "consciousness": 2,
                                     "oxygen-saturation": 1}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["passes"] is True
    r = _transition(client, case_id, "done")
    assert r.status_code == 200, r.text
    assert r.json()["case"]["status"] == "done"


def test_safety_gate_config_flip_off_releases_transitions(client):
    # Phase 15 (E): the flip must also publish pacuScoreGate off —
    # the PACU gate is seeded ON alongside the safety gate, and this
    # test's contract is "both gates off = pre-depth behaviour".
    _publish_surgery_flow(client, lambda p: p.update(
        {"safetyChecklistGate": False, "pacuScoreGate": False}))
    _patient(client, "dj-pat-4")
    case_id = _case(client, pid="dj-pat-4")
    # No checklist at all: with the gate published OFF the whole flow
    # runs exactly as it did before the depth pass.
    for target in ("pre-op", "in-op", "pacu", "done"):
        r = _transition(client, case_id, target)
        assert r.status_code == 200, (target, r.text)


# ---------------------------------------------------------------------------
# 2) Interdisciplinary rounds checklist
# ---------------------------------------------------------------------------

def _rounds_answers(full=True):
    answers = [
        {"linkId": "overnight-events",
         "answer": [{"valueString": "Quiet night, no events"}]},
        {"linkId": "lines-devices-reviewed",
         "answer": [{"valueBoolean": True}]},
        {"linkId": "vte-prophylaxis", "answer": [{"valueBoolean": True}]},
    ]
    if full:
        answers += [
            {"linkId": "glucose-control",
             "answer": [{"valueString": "In range"}]},
            {"linkId": "family-updated", "answer": [{"valueBoolean": True}]},
        ]
    return answers


def test_rounds_checklist_flow_and_today_flag(client):
    rows = _config_rows(client, "form", "rounds-checklist")
    assert any(r["status"] == "published" for r in rows)
    _patient(client, "dj-in-1")
    enc = _admit_bed(client, "dj-in-1", "MS-101A")
    rounds = client.get("/continuum/inpatient/rounds?unit=unit-medsurg",
                        headers=CLINICIAN).json()
    assert rounds["total"] == 1
    assert rounds["items"][0]["rounds_checklist_today"] is False
    # Required items are enforced by the form machinery.
    r = client.post("/continuum/inpatient/rounds-checklist",
                    json={"encounter_id": enc,
                          "answers": _rounds_answers()[:1]},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post("/continuum/inpatient/rounds-checklist",
                    json={"encounter_id": enc,
                          "answers": _rounds_answers()},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["form"] == "rounds-checklist"
    rounds = client.get("/continuum/inpatient/rounds?unit=unit-medsurg",
                        headers=CLINICIAN).json()
    assert rounds["items"][0]["rounds_checklist_today"] is True


def test_rounds_checklist_requires_active_admission(client):
    _patient(client, "dj-in-2")
    enc = _admit_bed(client, "dj-in-2", "MS-101B")
    r = client.post(f"/inpatient/encounters/{enc}/discharge", json={},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post("/continuum/inpatient/rounds-checklist",
                    json={"encounter_id": enc,
                          "answers": _rounds_answers()},
                    headers=CLINICIAN)
    assert r.status_code == 409  # discharged — no longer an active admission
    r = client.post("/continuum/inpatient/rounds-checklist",
                    json={"encounter_id": "nope",
                          "answers": _rounds_answers()},
                    headers=CLINICIAN)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 3) Discharge medication reconciliation
# ---------------------------------------------------------------------------

def _medrec_answers():
    return [
        {"linkId": "home-medications-reviewed",
         "answer": [{"valueBoolean": True}]},
        {"linkId": "medication-changes",
         "answer": [{"valueString": "Metformin held; lisinopril continued"}]},
        {"linkId": "new-prescriptions",
         "answer": [{"valueString": "Apixaban 5 mg twice daily"}]},
        {"linkId": "allergies-rechecked",
         "answer": [{"valueBoolean": True}]},
    ]


def _future_appointment(client, pid, days=3):
    start = (dt.datetime.now(dt.timezone.utc)
             + dt.timedelta(days=days)).isoformat()
    r = client.post("/fhir/Appointment", json={
        "resourceType": "Appointment", "status": "booked", "start": start,
        "participant": [{"actor": {"reference": f"Patient/{pid}"},
                         "status": "accepted"}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _complete_other_readiness(client, enc, pid):
    r = client.post(f"/inpatient/encounters/{enc}/discharge-plan",
                    json={"targetDate": dt.date.today().isoformat(),
                          "disposition": "home",
                          "checklistResponseId": "qr-1"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post("/continuum/aftercare/teach-back",
                    json={"encounter_id": enc, "answers": [
                        {"linkId": "instructions-given",
                         "answer": [{"valueString": "given"}]},
                        {"linkId": "teach-back-completed",
                         "answer": [{"valueBoolean": True}]},
                        {"linkId": "understanding-confirmed",
                         "answer": [{"valueBoolean": True}]}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    _future_appointment(client, pid)


def test_medrec_reported_but_not_gating_by_default(client):
    rows = _config_rows(client, "form", "discharge-med-rec")
    assert any(r["status"] == "published" for r in rows)
    _patient(client, "dj-in-3")
    enc = _admit_bed(client, "dj-in-3", "MS-102A")
    ready = client.get(f"/continuum/aftercare/readiness/{enc}",
                       headers=CLINICIAN).json()
    assert ready["medrec_done"] is False
    assert ready["medrec_required"] is False
    # Guard ships OFF: med-rec does NOT join missing, and the patient
    # can be fully ready on the original three elements alone.
    assert "medication-reconciliation" not in ready["missing"]
    _complete_other_readiness(client, enc, "dj-in-3")
    ready = client.get(f"/continuum/aftercare/readiness/{enc}",
                       headers=CLINICIAN).json()
    assert ready["ready"] is True
    assert ready["medrec_done"] is False
    # The form machinery enforces the required items.
    r = client.post("/continuum/inpatient/discharge-med-rec",
                    json={"encounter_id": enc,
                          "answers": _medrec_answers()[:1]},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post("/continuum/inpatient/discharge-med-rec",
                    json={"encounter_id": enc,
                          "answers": _medrec_answers()},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["form"] == "discharge-med-rec"
    ready = client.get(f"/continuum/aftercare/readiness/{enc}",
                       headers=CLINICIAN).json()
    assert ready["medrec_done"] is True
    assert ready["ready"] is True


def test_medrec_gates_readiness_when_guard_flipped_on(client):
    _publish_ops(client, lambda p: p["dischargeGuards"]
                 .update({"requireMedRec": True}))
    _patient(client, "dj-in-4")
    enc = _admit_bed(client, "dj-in-4", "MS-102B")
    _complete_other_readiness(client, enc, "dj-in-4")
    ready = client.get(f"/continuum/aftercare/readiness/{enc}",
                       headers=CLINICIAN).json()
    assert ready["medrec_required"] is True
    assert ready["missing"] == ["medication-reconciliation"]
    assert ready["ready"] is False
    r = client.post("/continuum/inpatient/discharge-med-rec",
                    json={"encounter_id": enc,
                          "answers": _medrec_answers()},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    ready = client.get(f"/continuum/aftercare/readiness/{enc}",
                       headers=CLINICIAN).json()
    assert ready["medrec_done"] is True
    assert ready["missing"] == []
    assert ready["ready"] is True
    # The board endpoint carries the same facts.
    board = client.get("/continuum/inpatient/discharge-readiness",
                       headers=CLINICIAN).json()
    item = next(i for i in board["items"] if i["encounter_id"] == enc)
    assert item["medrec_done"] is True
