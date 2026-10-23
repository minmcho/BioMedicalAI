"""LAB-D13 (Lab Depth pass, W7) — transfusion service completion.

T&S specimen-validity lifecycle (standard + extended presurgical,
eligibility both ways), the antibody-identification pathway and the
PERMANENT historical antibody record the Wave A issue gate consults,
special-requirements flags, crossmatch reservation exclusivity /
expiry / release, thaw-preparation post-thaw expiry, wastage
accounting, and the fixed-sequence reaction workup with grading and
a haemovigilance artefact filed into the existing BPD backend.

The depth router is included on the test app directly: main.py
wiring is the coordinator's step (see the W7 INTEGRATION NOTE), and
including the router here exercises exactly the module the
coordinator will wire — the W6 precedent.

Gate-behaviour anchors: the historical-antibody tests carry a
NO-HISTORY CONTROL CASE whose gate verdict must be the Wave A
verdict, unchanged; every stricter verdict is asserted against that
control. Synthetic data only; fixture dates derive from the UTC
date/now at run time."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import lab_transfusion
from app.main import create_app
from app.models import LabUnitReservation
from tests.conftest import CLINICIAN

TS_ORDER = "BB-TS"
XM_ORDER = "BB-XM"
DOB = "1990-01-01"
VITALS = {"temperature_c": 36.8, "pulse_bpm": 72, "systolic_bp": 118,
          "diastolic_bp": 76, "resp_rate": 16}
BASE = "/lab/transfusion"


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
    app.include_router(lab_transfusion.router)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Spine helpers (mirroring test_wave_a_lab)
# ---------------------------------------------------------------------------

def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic",
                                    "given": ["W7"]}],
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


def _to_in_process(client, order_id):
    _transition(client, order_id, "collected")
    _transition(client, order_id, "received")
    _transition(client, order_id, "in-process")


def _release(client, report_id):
    r = client.post(f"/lab/reports/{report_id}/verify", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/lab/reports/{report_id}/release", headers=CLINICIAN)
    assert r.status_code == 200, r.text


def _released_results(client, code, results, patient="pat-1"):
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


def _crossmatch(client, unit_id, value="Compatible", patient="pat-1",
                expect=201):
    order = _order(client, XM_ORDER, patient=patient)
    _to_in_process(client, order["id"])
    r = client.post(
        f"/lab/blood-bank/orders/{order['id']}/crossmatch-result",
        json={"unit_id": unit_id, "value": value}, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    if expect == 201:
        _release(client, r.json()["report"]["id"])
    return r


def _issue_order(client, patient="pat-1"):
    return _order(client, XM_ORDER, patient=patient)["id"]


def _issue(client, unit_id, order_id, expect=200, **extra):
    body = {"order_id": order_id}
    body.update(extra)
    r = client.post(f"/lab/blood-bank/units/{unit_id}/issue",
                    json=body, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _gate_checks(client, unit_id, patient="pat-1"):
    r = client.get(f"/lab/blood-bank/units/{unit_id}/gate-status",
                   params={"order_id": _issue_order(client, patient)},
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return {c["code"]: c for c in r.json()["checks"]}, r.json()


def _backdate_result(tmp_path, code, hours):
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


def _ago_iso(hours):
    return (datetime.now(timezone.utc)
            - timedelta(hours=hours)).isoformat()


def _register_specimen(client, patient="pat-1", collected=None, kind="standard",
                       expect=201, **extra):
    body = {"patient_ref": f"Patient/{patient}",
            "collected_at": collected or
            datetime.now(timezone.utc).isoformat(),
            "validity_kind": kind}
    body.update(extra)
    r = client.post(f"{BASE}/ts-specimens", json=body, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r.json()


def _record_historical(client, patient="pat-1", specificity="anti-E"):
    r = client.post(f"{BASE}/historical-antibodies",
                    json={"patient_ref": f"Patient/{patient}",
                          "specificity": specificity,
                          "source": "prior-facility-record"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_transfusion_config_governed_defaults(client):
    r = client.get(f"{BASE}/config", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg["config"] == {"kind": "terminology",
                             "key": "transfusion-service"}
    assert cfg["standard_validity_hours"] == 72
    assert cfg["extended_validity_days"] == 28
    assert cfg["extended_lookback_days"] == 90
    codes = {c["code"] for c in cfg["special_requirements"]}
    assert codes == {"irradiated", "cmv-negative", "washed",
                     "antigen-negative"}
    steps = [s["code"] for s in cfg["reaction_steps"]]
    assert steps == ["clerical-check", "visual-haemolysis-inspection",
                     "dat", "repeat-abo-screen-crossmatch",
                     "cultures-coag"]


# ---------------------------------------------------------------------------
# T&S specimen-validity lifecycle
# ---------------------------------------------------------------------------

def test_ts_specimen_valid_standard_and_gate_passes(client):
    _patient(client)
    _type_and_screen(client)
    specimen = _register_specimen(client)
    assert specimen["status"] == "valid", specimen
    assert specimen["extended_granted"] is False
    assert specimen["screen_observation_id"], specimen
    board = client.get(f"{BASE}/ts-specimens", headers=CLINICIAN).json()
    assert board["valid"] == 1 and board["expired"] == 0
    _unit(client, "W7-TS1")
    checks, posture = _gate_checks(client, "W7-TS1")
    assert checks["antibody_screen"]["status"] == "pass"
    assert "T&S specimen" in checks["antibody_screen"]["detail"]
    assert posture["issue_allowed"] is True
    r = _issue(client, "W7-TS1", _issue_order(client))
    assert r.json()["status"] == "issued"


def test_ts_specimen_expired_lifecycle_fails_gate_by_name(client, tmp_path):
    _patient(client)
    _type_and_screen(client)
    _backdate_result(tmp_path, "890-4", hours=100)
    specimen = _register_specimen(client, collected=_ago_iso(100))
    assert specimen["status"] == "expired", specimen
    board = client.get(f"{BASE}/ts-specimens", headers=CLINICIAN).json()
    assert board["expired"] == 1
    _unit(client, "W7-TS2")
    _crossmatch(client, "W7-TS2")
    checks, posture = _gate_checks(client, "W7-TS2")
    assert checks["antibody_screen"]["status"] == "fail"
    assert "T&S specimen" in checks["antibody_screen"]["detail"]
    assert "expired" in checks["antibody_screen"]["detail"]
    assert posture["issue_allowed"] is False
    r = _issue(client, "W7-TS2", _issue_order(client), expect=409)
    assert "T&S specimen" in r.json()["detail"], r.text


def test_ts_extended_validity_granted_when_eligible(client, tmp_path):
    _patient(client)
    _type_and_screen(client)
    _backdate_result(tmp_path, "890-4", hours=100)  # beyond 72 h standard
    specimen = _register_specimen(client, collected=_ago_iso(100),
                                  kind="presurgical-extended",
                                  transfusion_in_lookback=False,
                                  pregnancy_in_lookback=False)
    assert specimen["extended_granted"] is True, specimen
    assert specimen["status"] == "valid", specimen
    _unit(client, "W7-TS3")
    checks, posture = _gate_checks(client, "W7-TS3")
    assert checks["antibody_screen"]["status"] == "pass", checks
    assert "extended presurgical" in checks["antibody_screen"]["detail"]
    # eXM carries the issue: screen valid under the lifecycle window.
    assert posture["issue_allowed"] is True
    r = _issue(client, "W7-TS3", _issue_order(client))
    assert r.json()["status"] == "issued"


def test_ts_extended_validity_denied_both_ineligibility_legs(client, tmp_path):
    _patient(client, "pat-1")
    _patient(client, "pat-2")
    _type_and_screen(client, patient="pat-1")
    _type_and_screen(client, patient="pat-2")
    _backdate_result(tmp_path, "890-4", hours=100)
    denied_tx = _register_specimen(
        client, patient="pat-1", collected=_ago_iso(100),
        kind="presurgical-extended", transfusion_in_lookback=True,
        pregnancy_in_lookback=False)
    assert denied_tx["extended_granted"] is False
    assert "transfusion" in denied_tx["extended_denial_reason"]
    # Denied extended falls back to the standard window: collected
    # 100 h ago is expired, visibly.
    assert denied_tx["status"] == "expired", denied_tx
    denied_preg = _register_specimen(
        client, patient="pat-2", collected=_ago_iso(100),
        kind="presurgical-extended", transfusion_in_lookback=False,
        pregnancy_in_lookback=True)
    assert denied_preg["extended_granted"] is False
    assert "pregnancy" in denied_preg["extended_denial_reason"]
    assert denied_preg["status"] == "expired"


# ---------------------------------------------------------------------------
# Historical antibody record feeding the gate
# ---------------------------------------------------------------------------

def test_historical_antibody_blocks_exm_control_unchanged(client):
    # Control case (no history): the Wave A verdict — eXM carries an
    # rbc issue with a negative in-window screen and no crossmatch.
    _patient(client, "pat-2")
    _type_and_screen(client, patient="pat-2", group="B POS")
    _unit(client, "W7-H0", group="O POS")
    r = _issue(client, "W7-H0", _issue_order(client, "pat-2"))
    assert r.json()["status"] == "issued"
    # Same shape WITH a historical antibody: gate verdict changes —
    # eXM is not permitted with an antibody history.
    _patient(client, "pat-1")
    _type_and_screen(client, patient="pat-1", group="B POS")
    _record_historical(client, patient="pat-1", specificity="anti-E")
    _unit(client, "W7-H1", group="O POS")
    checks, posture = _gate_checks(client, "W7-H1", patient="pat-1")
    assert checks["crossmatch"]["status"] == "fail"
    assert "historical" in checks["crossmatch"]["detail"]
    assert posture["issue_allowed"] is False
    r = _issue(client, "W7-H1", _issue_order(client, "pat-1"), expect=409)
    assert "historical" in r.json()["detail"], r.text


def test_historical_antibody_requires_antigen_negative_unit(client):
    _patient(client)
    _type_and_screen(client)
    _record_historical(client, specificity="anti-E")
    _unit(client, "W7-H2")
    _crossmatch(client, "W7-H2")
    # Compatible crossmatch alone does not satisfy a known antibody:
    # the unit must be antigen-negative for anti-E.
    checks, _ = _gate_checks(client, "W7-H2")
    assert checks["historical_antibody"]["status"] == "fail"
    assert "antigen-negative" in checks["historical_antibody"]["detail"]
    r = _issue(client, "W7-H2", _issue_order(client), expect=409)
    assert "antigen-negative" in r.json()["detail"], r.text
    # Attest the attribute and the same gate lets the unit through.
    r = client.post(f"{BASE}/unit-attributes",
                    json={"unit_id": "W7-H2",
                          "attribute_code": "antigen-negative",
                          "specificity": "anti-E"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    checks, posture = _gate_checks(client, "W7-H2")
    assert checks["historical_antibody"]["status"] == "pass"
    assert posture["issue_allowed"] is True
    r = _issue(client, "W7-H2", _issue_order(client))
    assert r.json()["status"] == "issued"


def test_antibody_workup_files_historical_record(client):
    _patient(client)
    r = client.post(f"{BASE}/antibody-workups", json={
        "patient_ref": "Patient/pat-1", "method": "antibody-panel",
        "panel_reactions": [{"cell": "Cell 1", "reaction": "2+"},
                            {"cell": "Cell 2", "reaction": "0"}],
        "identified_specificities": ["anti-E"],
        "ruled_out": ["anti-K"],
        "conclusion": "Anti-E identified (synthetic panel)"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["workup"]["identified_specificities"] == ["anti-E"]
    assert [h["specificity"] for h in body["historical_antibodies"]] == \
        ["anti-E"]
    historical = client.get(f"{BASE}/historical-antibodies",
                            params={"patient_ref": "Patient/pat-1"},
                            headers=CLINICIAN).json()
    # Ruled-out specificities never enter the permanent record.
    assert [h["specificity"] for h in historical["items"]] == ["anti-E"]
    assert historical["items"][0]["source"] == \
        "antibody-identification-workup"
    # A duplicate direct record is refused by name.
    r = client.post(f"{BASE}/historical-antibodies",
                    json={"patient_ref": "Patient/pat-1",
                          "specificity": "anti-E"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# Special requirements
# ---------------------------------------------------------------------------

def test_special_requirement_unmet_is_named_gate_failure(client):
    _patient(client)
    _type_and_screen(client)
    r = client.post(f"{BASE}/special-requirements",
                    json={"patient_ref": "Patient/pat-1",
                          "requirement_code": "irradiated"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    _unit(client, "W7-S1")
    _crossmatch(client, "W7-S1")
    checks, _ = _gate_checks(client, "W7-S1")
    assert checks["special_requirements"]["status"] == "fail"
    assert "irradiated" in checks["special_requirements"]["detail"]
    r = _issue(client, "W7-S1", _issue_order(client), expect=409)
    assert "special requirement unmet" in r.json()["detail"], r.text
    assert "irradiated" in r.json()["detail"], r.text
    # Satisfy it per unit and the gate passes.
    r = client.post(f"{BASE}/unit-attributes",
                    json={"unit_id": "W7-S1",
                          "attribute_code": "irradiated"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = _issue(client, "W7-S1", _issue_order(client))
    assert r.json()["status"] == "issued"


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------

def test_reservation_exclusivity_and_release_on_issue(client):
    _patient(client, "pat-1")
    _patient(client, "pat-2")
    _type_and_screen(client, patient="pat-1")
    _type_and_screen(client, patient="pat-2")
    _unit(client, "W7-R1")
    _crossmatch(client, "W7-R1", patient="pat-1")
    reservations = client.get(f"{BASE}/reservations",
                              headers=CLINICIAN).json()
    assert reservations["active"] == 1, reservations
    mine = reservations["items"][0]
    assert mine["patient_ref"] == "Patient/pat-1"
    assert mine["unit_id"] == "W7-R1"
    # Another patient cannot crossmatch the reserved unit.
    r = _crossmatch(client, "W7-R1", patient="pat-2", expect=409)
    assert "reserved" in r.json()["detail"], r.text
    # ... nor be issued it (gate names the reservation).
    checks, _ = _gate_checks(client, "W7-R1", patient="pat-2")
    assert checks["unit_reservation"]["status"] == "fail"
    r = _issue(client, "W7-R1", _issue_order(client, "pat-2"), expect=409)
    assert "reserved" in r.json()["detail"], r.text
    # The reserving patient issues: the reservation releases, reason
    # 'issued', audited path.
    r = _issue(client, "W7-R1", _issue_order(client, "pat-1"))
    assert r.json()["status"] == "issued"
    reservations = client.get(f"{BASE}/reservations",
                              headers=CLINICIAN).json()
    released = [x for x in reservations["items"]
                if x["unit_id"] == "W7-R1"][0]
    assert released["status"] == "released", released
    assert released["release_reason"] == "issued"


def test_reservation_manual_release_and_expiry(client):
    _patient(client, "pat-1")
    _patient(client, "pat-2")
    _type_and_screen(client, patient="pat-1")
    _type_and_screen(client, patient="pat-2")
    _unit(client, "W7-R2")
    _crossmatch(client, "W7-R2", patient="pat-1")
    reservations = client.get(f"{BASE}/reservations",
                              headers=CLINICIAN).json()
    auto_id = reservations["items"][0]["reservation_id"]
    # Manual release requires a reason.
    r = client.post(f"{BASE}/reservations/{auto_id}/release",
                    json={}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post(f"{BASE}/reservations/{auto_id}/release",
                    json={"reason": "Patient discharged (synthetic)"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "released"
    # After release the explicit reserve endpoint can re-reserve for
    # the same patient off the still-compatible crossmatch...
    r = client.post(f"{BASE}/reservations",
                    json={"unit_id": "W7-R2",
                          "patient_ref": "Patient/pat-1"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    second_id = r.json()["reservation_id"]
    # ... expiry (governed) frees the unit for another patient.
    factory = client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(LabUnitReservation).where(
            LabUnitReservation.reservation_id == second_id)).first()
        row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session.commit()
    listed = client.get(f"{BASE}/reservations", headers=CLINICIAN).json()
    expired = [x for x in listed["items"]
               if x["reservation_id"] == second_id][0]
    assert expired["status"] == "expired", expired
    r = _crossmatch(client, "W7-R2", patient="pat-2")
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Thaw / preparation
# ---------------------------------------------------------------------------

def _run_prep(client, unit_id, kind, at_hours):
    r = client.post(f"{BASE}/preparations",
                    json={"unit_id": unit_id, "prep_kind": kind},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    prep = r.json()
    for step, hours in zip(
            [s["code"] for s in prep["expected_steps"]], at_hours):
        r = client.post(f"{BASE}/preparations/{prep['prep_id']}/steps",
                        json={"step": step, "at": _ago_iso(hours)},
                        headers=CLINICIAN)
        assert r.status_code == 201, r.text
        prep = r.json()
    return prep


def test_prep_step_sequence_enforced_and_post_thaw_expiry_blocks(client):
    _patient(client)
    _type_and_screen(client)
    _unit(client, "W7-P1", product="plasma", group="AB POS")
    r = client.post(f"{BASE}/preparations",
                    json={"unit_id": "W7-P1", "prep_kind": "thaw-plasma"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    prep_id = r.json()["prep_id"]
    # Out-of-order step refused by name.
    r = client.post(f"{BASE}/preparations/{prep_id}/steps",
                    json={"step": "label-verified"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "thaw-started" in r.json()["detail"], r.text
    # Complete the sequence with times 30 h in the past: plasma's
    # governed post-thaw window is 24 h, so the unit is expired.
    prep = None
    for step, hours in (("thaw-started", 30), ("thaw-completed", 29.9),
                        ("visual-inspection", 29.8),
                        ("label-verified", 29.7)):
        r = client.post(f"{BASE}/preparations/{prep_id}/steps",
                        json={"step": step, "at": _ago_iso(hours)},
                        headers=CLINICIAN)
        assert r.status_code == 201, r.text
        prep = r.json()
    assert prep["status"] == "expired", prep
    _crossmatch(client, "W7-P1")
    checks, _ = _gate_checks(client, "W7-P1")
    assert checks["preparation_expiry"]["status"] == "fail"
    assert "post-thaw expiry" in checks["preparation_expiry"]["detail"]
    r = _issue(client, "W7-P1", _issue_order(client), expect=409)
    assert "post-thaw expiry" in r.json()["detail"], r.text
    # A fresh preparation on a second unit is issuable.
    _unit(client, "W7-P2", product="plasma", group="AB POS")
    fresh = _run_prep(client, "W7-P2", "thaw-plasma",
                      [0.03, 0.02, 0.01, 0.0])
    assert fresh["status"] == "ready", fresh
    _crossmatch(client, "W7-P2")
    r = _issue(client, "W7-P2", _issue_order(client))
    assert r.json()["status"] == "issued"


# ---------------------------------------------------------------------------
# Wastage / discard accounting
# ---------------------------------------------------------------------------

def test_wastage_accounting_totals_and_guards(client):
    _unit(client, "W7-W1")
    _unit(client, "W7-W2")
    _unit(client, "W7-W3")
    # A wastage disposition without a governed reason is refused.
    r = client.post(f"{BASE}/dispositions",
                    json={"unit_id": "W7-W1", "disposition": "wasted"},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # 'transfused' accounting cannot bypass BCMA: the unit is merely
    # available, so recording it as transfused is refused.
    r = client.post(f"{BASE}/dispositions",
                    json={"unit_id": "W7-W1", "disposition": "transfused"},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "BCMA" in r.json()["detail"], r.text
    cases = (("W7-W1", "wasted", "expired-before-use"),
             ("W7-W2", "discarded", "temperature-excursion"),
             ("W7-W3", "expired", "post-thaw-expiry"))
    for unit_id, disposition, reason in cases:
        r = client.post(f"{BASE}/dispositions",
                        json={"unit_id": unit_id,
                              "disposition": disposition,
                              "reason_code": reason}, headers=CLINICIAN)
        assert r.status_code == 201, r.text
        assert r.json()["unit_status"] == disposition
    report = client.get(f"{BASE}/wastage-report",
                        headers=CLINICIAN).json()
    assert report["total"] == 3, report
    assert report["by_reason"] == {"expired-before-use": 1,
                                    "temperature-excursion": 1,
                                    "post-thaw-expiry": 1}, report
    assert report["by_disposition"] == {"wasted": 1, "discarded": 1,
                                        "expired": 1}, report


# ---------------------------------------------------------------------------
# Reaction workup
# ---------------------------------------------------------------------------

def _issued_and_transfused(client, unit_id="W7-X1"):
    _patient(client)
    _type_and_screen(client)
    _unit(client, unit_id)
    _crossmatch(client, unit_id)
    r = _issue(client, unit_id, _issue_order(client))
    assert r.json()["status"] == "issued"
    r = client.post("/lab/blood-bank/transfusions", json={
        "unit_id": unit_id, "wristband_patient_id": "pat-1",
        "date_of_birth": DOB, "vitals": dict(VITALS),
        "started_at": "2026-10-20T10:00:00+00:00",
        "stopped_at": "2026-10-20T10:15:00+00:00"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text


def test_reaction_workup_requires_containment_first(client):
    _patient(client)
    _unit(client, "W7-X0")
    r = client.post(f"{BASE}/reaction-workups",
                    json={"unit_id": "W7-X0"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "containment" in r.json()["detail"], r.text
    assert "quarantined" in r.json()["detail"], r.text


def test_reaction_workup_sequence_grading_and_haemovigilance(client):
    _issued_and_transfused(client)
    r = client.post("/lab/blood-bank/units/W7-X1/reaction",
                    json={"symptoms": "Fever and rigors (synthetic)"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["unit"]["status"] == "quarantined"
    assert r.json()["hold"]["active"] is True
    r = client.post(f"{BASE}/reaction-workups",
                    json={"unit_id": "W7-X1"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    workup_id = r.json()["workup_id"]
    assert r.json()["next_step"] == "clerical-check"
    # Out-of-order step refused, naming the fixed sequence position.
    r = client.post(f"{BASE}/reaction-workups/{workup_id}/steps",
                    json={"step": "dat", "result": "Negative"},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "clerical-check" in r.json()["detail"], r.text
    # Grading before the sequence completes is refused.
    steps = (("clerical-check", "No clerical error found"),
             ("visual-haemolysis-inspection", "No visible haemolysis"),
             ("dat", "DAT negative"),
             ("repeat-abo-screen-crossmatch",
              "ABO/screen/crossmatch concordant on repeat"),
             ("cultures-coag", "not-indicated"))
    for i, (step, result) in enumerate(steps):
        if i == 2:
            r = client.post(f"{BASE}/reaction-workups/{workup_id}/grading",
                            json={"severity": "severe",
                                  "imputability": "probable"},
                            headers=CLINICIAN)
            assert r.status_code == 409, r.text
        r = client.post(f"{BASE}/reaction-workups/{workup_id}/steps",
                        json={"step": step, "result": result},
                        headers=CLINICIAN)
        assert r.status_code == 201, r.text
    graded = client.post(f"{BASE}/reaction-workups/{workup_id}/grading",
                         json={"severity": "severe",
                               "imputability": "probable"},
                         headers=CLINICIAN)
    assert graded.status_code == 200, graded.text
    body = graded.json()
    assert all(s["done"] and s["recorded_by"] for s in body["steps"])
    r = client.post(
        f"{BASE}/reaction-workups/{workup_id}/haemovigilance-report",
        json={}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    report_id = r.json()["haemovigilance_report"]["report_id"]
    assert report_id.startswith("hv-"), report_id
    assert "biological-product-deviation" in \
        r.json()["haemovigilance_report"]["filed_into"]
    # The artefact exists in the EXISTING BPD backend.
    r = client.get(f"/lab/blood-bank/deviations/{report_id}",
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    artefact = r.json()["artefact"]
    assert artefact["type"] == "haemovigilance-report"
    assert artefact["severity"] == "severe"
    assert artefact["imputability"] == "probable"
    assert len(artefact["steps"]) == 5
    assert artefact["traceability"]["regulation"] == "21 CFR 606.160"
