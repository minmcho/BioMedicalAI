"""Wave A — LAB-B1 (blood-bank issue gate) + LAB-M1 (bedside
transfusion verification).

The gate consumes RELEASED results only (resulted + verified +
released on the standard spine; drafts never count — the
lab._previous_released precedent). Helpers therefore run every
type & screen / crossmatch through verify + release, exactly as a
bench would before a unit may move.

Synthetic data only (fabricated patients, fabricated values)."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import AuditLog, FhirResource
from tests.conftest import CLINICIAN

TS_ORDER = "BB-TS"
XM_ORDER = "BB-XM"
DOB = "1990-01-01"
VITALS = {"temperature_c": 36.8, "pulse_bpm": 72, "systolic_bp": 118,
          "diastolic_bp": 76, "resp_rate": 16}


# ---------------------------------------------------------------------------
# Helpers (mirroring test_phase6b_lab's spine helpers)
# ---------------------------------------------------------------------------

def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic",
                                    "given": ["WaveA"]}],
                          "birthDate": DOB},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _order(client, code, patient="pat-1"):
    r = client.post("/lab/orders",
                    json={"code": code,
                          "subject": {"reference": f"Patient/{patient}"}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _transition(client, order_id, to_status):
    r = client.post(f"/lab/orders/{order_id}/transition",
                    json={"to_status": to_status}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r


def _to_in_process(client, order_id):
    _transition(client, order_id, "collected")
    _transition(client, order_id, "received")
    _transition(client, order_id, "in-process")


def _release(client, report_id):
    r = client.post(f"/lab/reports/{report_id}/verify", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/lab/reports/{report_id}/release", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def _released_results(client, code, results, patient="pat-1"):
    """Order -> result -> verify -> release; returns the order id."""
    order = _order(client, code, patient=patient)
    _to_in_process(client, order["id"])
    r = client.post(f"/lab/orders/{order['id']}/results",
                    json={"results": results}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    _release(client, r.json()["report"]["id"])
    return order["id"]


def _type_and_screen(client, patient="pat-1", group="O POS",
                     screen="Negative"):
    return _released_results(client, TS_ORDER, [
        {"code": "883-9", "value": group},
        {"code": "890-4", "value": screen},
    ], patient=patient)


def _unit(client, unit_id, product="rbc", group="O POS",
          expiry="2099-01-01"):
    r = client.post("/lab/blood-bank/units",
                    json={"unit_id": unit_id, "product_type": product,
                          "blood_group": group, "expiry_date": expiry},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _crossmatch(client, unit_id, value, patient="pat-1"):
    """Result a BB-XM for a specific unit via the unit-specific
    crossmatch path, then verify + release it."""
    order = _order(client, XM_ORDER, patient=patient)
    _to_in_process(client, order["id"])
    r = client.post(
        f"/lab/blood-bank/orders/{order['id']}/crossmatch-result",
        json={"unit_id": unit_id, "value": value}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    _release(client, r.json()["report"]["id"])
    return order["id"]


def _issue_order(client, patient="pat-1"):
    return _order(client, XM_ORDER, patient=patient)["id"]


def _issue(client, unit_id, order_id, expect=200, **extra):
    body = {"order_id": order_id}
    body.update(extra)
    r = client.post(f"/lab/blood-bank/units/{unit_id}/issue",
                    json=body, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _unit_state(client, unit_id):
    units = client.get("/lab/blood-bank/units", headers=CLINICIAN).json()
    return {u["unit_id"]: u for u in units}[unit_id]


def _audit(client):
    factory = client.app.state.session_factory
    with factory() as session:
        return [(row.action, row.entity, row.payload)
                for row in session.scalars(select(AuditLog)).all()]


def _procedure_count(client):
    factory = client.app.state.session_factory
    with factory() as session:
        return len(session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "Procedure")).all())


def _backdate_result(tmp_path, code, hours):
    """Move one released Observation's effectiveDateTime into the
    past (the result-entry path always stamps 'now', so the aged
    screen for the expiry test is made by ageing the stored body —
    the same body the gate reads)."""
    db = tmp_path / "test.db"
    target = (datetime.now(timezone.utc)
              - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT id, body FROM fhir_resources "
            "WHERE resource_type = 'Observation'").fetchall()
        for row_id, body_json in rows:
            body = json.loads(body_json)
            coding = (body.get("code") or {}).get("coding") or [{}]
            if coding[0].get("code") == code:
                body["effectiveDateTime"] = target
                conn.execute(
                    "UPDATE fhir_resources SET body = ? WHERE id = ?",
                    (json.dumps(body), row_id))
        conn.commit()
    finally:
        conn.close()


def _assert_unit_untouched(client, unit_id):
    unit = _unit_state(client, unit_id)
    assert unit["status"] == "available", unit
    assert unit["patient_ref"] is None, unit
    assert unit["order_id"] is None, unit


# ---------------------------------------------------------------------------
# LAB-B1 — config shape
# ---------------------------------------------------------------------------

def test_blood_bank_gate_config_seeded_in_catalog(client):
    catalog = client.get("/lab/catalog", headers=CLINICIAN).json()["catalog"]
    gate = catalog["bloodBankGate"]
    assert gate["antibodyScreenValidityHours"] == 72
    assert gate["electronicCrossmatch"] == {"enabled": True,
                                            "products": ["rbc"]}
    codes = {c["code"] for c in gate["emergencyReleaseReasonCodes"]}
    assert "life-threatening-haemorrhage" in codes
    assert "massive-transfusion-protocol" in codes


# ---------------------------------------------------------------------------
# LAB-B1 — refusals (409, named checks, zero state change)
# ---------------------------------------------------------------------------

def test_issue_refused_abo_incompatible_rbc(client):
    _patient(client)
    _type_and_screen(client, group="O POS", screen="Negative")
    _unit(client, "WA-A1", group="A POS")
    _crossmatch(client, "WA-A1", "Compatible")
    r = _issue(client, "WA-A1", _issue_order(client), expect=409)
    detail = r.json()["detail"]
    assert "ABO-incompatible" in detail, detail
    assert "A POS" in detail and "O POS" in detail, detail
    _assert_unit_untouched(client, "WA-A1")


def test_issue_refused_rh_positive_rbc_to_rh_negative_patient(client):
    _patient(client)
    _type_and_screen(client, group="A NEG", screen="Negative")
    _unit(client, "WA-RH1", group="A POS")
    _crossmatch(client, "WA-RH1", "Compatible")
    r = _issue(client, "WA-RH1", _issue_order(client), expect=409)
    assert "Rh-incompatible" in r.json()["detail"], r.text
    _assert_unit_untouched(client, "WA-RH1")


def test_issue_refused_no_resulted_patient_group(client):
    _patient(client)
    _unit(client, "WA-NG1", group="O NEG")
    r = _issue(client, "WA-NG1", _issue_order(client), expect=409)
    detail = r.json()["detail"]
    assert "type and screen required before issue" in detail, detail
    _assert_unit_untouched(client, "WA-NG1")


def test_issue_refused_positive_antibody_screen(client):
    _patient(client)
    _type_and_screen(client, group="O POS", screen="Positive")
    _unit(client, "WA-PS1", group="O POS")
    _crossmatch(client, "WA-PS1", "Compatible")
    r = _issue(client, "WA-PS1", _issue_order(client), expect=409)
    detail = r.json()["detail"]
    assert "Positive" in detail and "antibody screen" in detail, detail
    _assert_unit_untouched(client, "WA-PS1")


def test_issue_refused_expired_antibody_screen(client, tmp_path):
    _patient(client)
    _type_and_screen(client, group="O POS", screen="Negative")
    _backdate_result(tmp_path, "890-4", hours=100)  # window is 72h
    _unit(client, "WA-EX1", group="O POS")
    _crossmatch(client, "WA-EX1", "Compatible")
    r = _issue(client, "WA-EX1", _issue_order(client), expect=409)
    detail = r.json()["detail"]
    assert "expired" in detail and "validity window" in detail, detail
    _assert_unit_untouched(client, "WA-EX1")


def test_issue_refused_crossmatch_incompatible_for_this_unit(client):
    _patient(client)
    _type_and_screen(client, group="O POS", screen="Negative")
    _unit(client, "WA-XI1", group="O POS")
    _crossmatch(client, "WA-XI1", "Incompatible")
    r = _issue(client, "WA-XI1", _issue_order(client), expect=409)
    detail = r.json()["detail"]
    assert "Incompatible" in detail and "WA-XI1" in detail, detail
    _assert_unit_untouched(client, "WA-XI1")


def test_issue_refused_crossmatch_missing_and_exm_ineligible(client):
    _patient(client)
    _type_and_screen(client, group="O POS", screen="Negative")
    # plasma is NOT in the governed eXM products (["rbc"]), so without
    # a resulted unit crossmatch the gate must refuse.
    _unit(client, "WA-XM1", product="plasma", group="AB POS")
    r = _issue(client, "WA-XM1", _issue_order(client), expect=409)
    detail = r.json()["detail"]
    assert "crossmatch" in detail.lower(), detail
    assert "electronic" in detail.lower(), detail
    _assert_unit_untouched(client, "WA-XM1")


def test_issue_refused_plasma_abo_incompatible_inverse_matrix(client):
    _patient(client)
    _type_and_screen(client, group="AB POS", screen="Negative")
    # Plasma matrix is inverse: an A-plasma unit may go to A or O
    # recipients only — NOT to the universal-RBC-recipient AB patient.
    _unit(client, "WA-PL1", product="plasma", group="A POS")
    _crossmatch(client, "WA-PL1", "Compatible")
    r = _issue(client, "WA-PL1", _issue_order(client), expect=409)
    assert "ABO-incompatible" in r.json()["detail"], r.text
    _assert_unit_untouched(client, "WA-PL1")


# ---------------------------------------------------------------------------
# LAB-B1 — allowed paths
# ---------------------------------------------------------------------------

def test_issue_allowed_compatible_with_unit_crossmatch(client):
    _patient(client)
    _type_and_screen(client, group="O POS", screen="Negative")
    _unit(client, "WA-OK1", group="O POS")
    _crossmatch(client, "WA-OK1", "Compatible")
    order_id = _issue_order(client)
    # the gate-status dry run agrees, check by check
    r = client.get(f"/lab/blood-bank/units/WA-OK1/gate-status",
                   params={"order_id": order_id}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    posture = r.json()
    assert posture["issue_allowed"] is True, posture
    assert all(c["status"] == "pass" for c in posture["checks"]), posture
    r = _issue(client, "WA-OK1", order_id)
    assert r.json()["status"] == "issued"
    assert r.json()["patient_ref"] == "Patient/pat-1"
    assert r.json()["emergency_release_reason_code"] is None


def test_issue_allowed_plasma_inverse_matrix_with_crossmatch(client):
    _patient(client)
    _type_and_screen(client, group="O POS", screen="Negative")
    # AB plasma is the universal plasma donor — compatible with the
    # O recipient under the inverse matrix.
    _unit(client, "WA-PL2", product="plasma", group="AB POS")
    _crossmatch(client, "WA-PL2", "Compatible")
    r = _issue(client, "WA-PL2", _issue_order(client))
    assert r.json()["status"] == "issued"


def test_issue_allowed_electronic_crossmatch_path(client):
    _patient(client)
    _type_and_screen(client, group="B POS", screen="Negative")
    _unit(client, "WA-EXM1", group="O POS")
    # No crossmatch resulted at all: eXM eligibility (rbc enabled +
    # group on file + negative in-window screen) carries the issue.
    r = _issue(client, "WA-EXM1", _issue_order(client))
    assert r.json()["status"] == "issued"
    actions = [a for a, _, _ in _audit(client)]
    assert "lab.bloodbank.unit.issue" in actions


# ---------------------------------------------------------------------------
# LAB-B1 — emergency release
# ---------------------------------------------------------------------------

def test_emergency_release_missing_evidence_universal_unit(client):
    _patient(client)
    _unit(client, "WA-EM1", group="O NEG")
    # No type & screen, no crossmatch: every failure is MISSING
    # evidence, the unit is universal for RBC, the reason is governed.
    r = _issue(client, "WA-EM1", _issue_order(client),
               emergency_release={
                   "reason_code": "life-threatening-haemorrhage",
                   "note": "Exsanguinating GI bleed (synthetic)"})
    body = r.json()
    assert body["status"] == "issued", body
    assert body["emergency_release_reason_code"] == \
        "life-threatening-haemorrhage"
    assert body["emergency_released_by"]
    actions = [a for a, _, _ in _audit(client)]
    assert "lab.bloodbank.unit.emergency_issue" in actions


def test_emergency_release_refused_non_universal_unit_group_unknown(client):
    _patient(client)
    _unit(client, "WA-EM2", group="A POS")  # not universal for RBC
    r = _issue(client, "WA-EM2", _issue_order(client), expect=409,
               emergency_release={
                   "reason_code": "life-threatening-haemorrhage",
                   "note": "Bleeding (synthetic)"})
    assert "universally" in r.json()["detail"], r.text
    _assert_unit_untouched(client, "WA-EM2")


def test_emergency_release_refused_bogus_reason_code(client):
    _patient(client)
    _unit(client, "WA-EM3", group="O NEG")
    r = _issue(client, "WA-EM3", _issue_order(client), expect=409,
               emergency_release={"reason_code": "doctor-said-so",
                                  "note": "Nope (synthetic)"})
    assert "reason_code" in r.json()["detail"], r.text
    _assert_unit_untouched(client, "WA-EM3")


def test_emergency_release_never_overrides_positive_screen(client):
    _patient(client)
    _type_and_screen(client, group="O POS", screen="Positive")
    _unit(client, "WA-EM4", group="O NEG")
    r = _issue(client, "WA-EM4", _issue_order(client), expect=409,
               emergency_release={
                   "reason_code": "massive-transfusion-protocol",
                   "note": "MTP (synthetic)"})
    assert "Positive" in r.json()["detail"], r.text
    _assert_unit_untouched(client, "WA-EM4")


def test_emergency_release_never_overrides_incompatible_crossmatch(client):
    _patient(client)
    _type_and_screen(client, group="O POS", screen="Negative")
    _unit(client, "WA-EM5", group="O POS")
    _crossmatch(client, "WA-EM5", "Incompatible")
    r = _issue(client, "WA-EM5", _issue_order(client), expect=409,
               emergency_release={
                   "reason_code": "urgent-clinical-need",
                   "note": "Urgent (synthetic)"})
    assert "Incompatible" in r.json()["detail"], r.text
    _assert_unit_untouched(client, "WA-EM5")


# ---------------------------------------------------------------------------
# LAB-M1 — bedside transfusion verification
# ---------------------------------------------------------------------------

def _issued_unit(client, unit_id="WA-T1"):
    _patient(client)
    _type_and_screen(client, group="O POS", screen="Negative")
    _unit(client, unit_id, group="O POS")
    r = _issue(client, unit_id, _issue_order(client))
    assert r.json()["status"] == "issued"
    return unit_id


def _transfuse(client, payload, expect=201):
    r = client.post("/lab/blood-bank/transfusions", json=payload,
                    headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _good_transfusion(unit_id):
    return {"unit_id": unit_id, "wristband_patient_id": "pat-1",
            "date_of_birth": DOB, "vitals": dict(VITALS),
            "started_at": "2026-10-20T10:00:00+00:00",
            "stopped_at": "2026-10-20T10:15:00+00:00",
            "second_checker": "nurse-two"}


def test_transfusion_dob_mismatch_refused_and_audited(client):
    _issued_unit(client)
    payload = _good_transfusion("WA-T1")
    payload["date_of_birth"] = "1985-05-05"
    r = _transfuse(client, payload, expect=422)
    assert "date_of_birth" in r.json()["detail"], r.text
    # zero writes: unit still issued, no Procedure created
    assert _unit_state(client, "WA-T1")["status"] == "issued"
    assert _procedure_count(client) == 0
    refused = [p for a, _, p in _audit(client)
               if a == "lab.bloodbank.transfusion.verification_refused"]
    assert len(refused) == 1
    assert refused[0]["unit_id"] == "WA-T1"


def test_transfusion_wristband_mismatch_refused(client):
    _issued_unit(client)
    payload = _good_transfusion("WA-T1")
    payload["wristband_patient_id"] = "pat-999"
    r = _transfuse(client, payload, expect=422)
    assert "wristband" in r.json()["detail"], r.text
    assert _unit_state(client, "WA-T1")["status"] == "issued"
    assert _procedure_count(client) == 0


def test_transfusion_missing_vitals_422(client):
    _issued_unit(client)
    payload = _good_transfusion("WA-T1")
    del payload["vitals"]
    r = _transfuse(client, payload, expect=422)
    assert "vitals" in r.json()["detail"], r.text
    assert _procedure_count(client) == 0


def test_transfusion_vital_out_of_range_422_names_field(client):
    _issued_unit(client)
    payload = _good_transfusion("WA-T1")
    payload["vitals"] = dict(VITALS, pulse_bpm=400)
    r = _transfuse(client, payload, expect=422)
    assert "vitals.pulse_bpm" in r.json()["detail"], r.text
    assert _procedure_count(client) == 0


def test_transfusion_happy_path_records_verification_and_vitals(client):
    _issued_unit(client)
    r = _transfuse(client, _good_transfusion("WA-T1"))
    body = r.json()
    assert body["unit"]["status"] == "transfused"
    procedure = body["transfusion"]
    assert procedure["subject"]["reference"] == "Patient/pat-1"
    assert procedure["performedPeriod"] == {
        "start": "2026-10-20T10:00:00+00:00",
        "end": "2026-10-20T10:15:00+00:00"}
    exts = {e["url"]: e.get("valueString")
            for e in procedure["extension"]}
    verification = json.loads(
        exts["urn:opencare:extension/transfusion-verification"])
    assert verification["wristband_patient_id"] == "pat-1"
    assert verification["date_of_birth_matched"] is True
    assert verification["verified_by"]
    assert verification["second_checker"] == "nurse-two"
    vitals = json.loads(exts["urn:opencare:extension/pre-transfusion-vitals"])
    assert vitals == {k: float(v) for k, v in VITALS.items()}
    assert exts["urn:opencare:extension/blood-unit"] == "WA-T1"
