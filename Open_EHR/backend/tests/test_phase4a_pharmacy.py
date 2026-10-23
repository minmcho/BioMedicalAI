"""Phase 4A — Pharmacy (app.pharmacy).

Formulary as Studio config, stock-controlled dispensing (decrement,
oversell 409 with no side effects, request status flow active ->
dispensed) and med-admin. Wave A updates: dispensing requires a
CURRENT pharmacist verification (PHARM-B1), so the dispense helper
verifies first (the gate itself is proven in
tests/test_wave_a_pharmacy.py); and the BCMA-lite typed-ID admin
path was retired (PHARM-M2) — administration goes through the
five-rights BCMA scan in app.pharmacy_ops, exercised here through
its barcode payload endpoints with an override reason for the
fixture orders' undocumented dose/route.
"""
from sqlalchemy import select

from app.audit import verify_chain
from app.models import AuditLog
from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER

RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
LISINOPRIL = "1049221"      # stock-tracked, initial stock 120
SERTRALINE = "1048467"      # stock-tracked, initial stock 60
ALBUTEROL = "245314"        # NOT stock-tracked


def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid, "birthDate": "1962-03-14",
        "name": [{"family": "Synthetic", "given": ["Pat"]}]}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _request(client, rid="med-1", pid="pat-1", code=LISINOPRIL,
             display="Lisinopril 10 MG Oral Tablet"):
    r = client.post("/fhir/MedicationRequest", json={
        "resourceType": "MedicationRequest", "id": rid, "status": "active",
        "intent": "order",
        "medicationCodeableConcept": {"coding": [
            {"system": RXNORM, "code": code, "display": display}]},
        "subject": {"reference": f"Patient/{pid}"}}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return rid


def _stock(client, code, headers=CLINICIAN):
    items = client.get("/pharmacy/stock", headers=headers).json()["items"]
    return next(i for i in items if i["code"] == code)


def _verify(client, rid, headers=CLINICIAN):
    """Wave A (PHARM-B1): dispensing requires a current verification —
    fixture helper records one (the seeded fixture medications carry
    no hard-stop signals, so a bare verify succeeds)."""
    return client.post(f"/pharmacy/verification/{rid}/verify",
                       json={}, headers=headers)


def _dispense(client, rid, headers=CLINICIAN, **extra):
    _verify(client, rid)
    return client.post("/pharmacy/dispense",
                       json={"medication_request_id": rid, **extra}, headers=headers)


# ---------------------------------------------------------------------------
# Formulary + stock seeds
# ---------------------------------------------------------------------------

def test_formulary_and_stock_are_seeded_from_studio_config(client):
    eff = client.get("/studio/config/terminology/effective/formulary",
                     headers=ADMIN)
    assert eff.status_code == 200
    codes = {c["code"] for c in eff.json()["payload"]["concepts"]}
    assert {LISINOPRIL, SERTRALINE, ALBUTEROL} <= codes
    r = client.get("/pharmacy/stock", headers=CLINICIAN)
    assert r.status_code == 200
    lisinopril = _stock(client, LISINOPRIL)
    assert lisinopril["stock_tracked"] is True and lisinopril["quantity"] == 120
    albuterol = _stock(client, ALBUTEROL)
    assert albuterol["stock_tracked"] is False and albuterol["quantity"] is None
    # Reads are staff-only: patient 403, unauthenticated 401.
    assert client.get("/pharmacy/stock", headers=PATIENT).status_code == 403
    assert client.get("/pharmacy/stock").status_code == 401
    assert client.get("/pharmacy/dispense-queue", headers=PATIENT).status_code == 403


# ---------------------------------------------------------------------------
# Dispensing
# ---------------------------------------------------------------------------

def test_dispense_creates_record_decrements_stock_and_flips_request(client):
    _patient(client)
    _request(client)
    r = _dispense(client, "med-1", quantity=10)
    assert r.status_code == 201, r.text
    out = r.json()
    dispense = out["dispense"]
    assert dispense["resourceType"] == "MedicationDispense"
    assert dispense["status"] == "completed"
    assert dispense["medicationCodeableConcept"]["coding"][0]["code"] == LISINOPRIL
    assert dispense["subject"]["reference"] == "Patient/pat-1"
    assert dispense["authorizingPrescription"][0]["reference"] == "MedicationRequest/med-1"
    assert dispense["quantity"]["value"] == 10
    assert out["medication_request"]["status"] == "dispensed"
    assert out["stock_after"] == 110
    # The effect is real and persisted, visible through independent reads.
    assert _stock(client, LISINOPRIL)["quantity"] == 110
    req = client.get("/fhir/MedicationRequest/med-1", headers=CLINICIAN).json()
    assert req["status"] == "dispensed"
    listed = client.get("/pharmacy/dispenses", headers=CLINICIAN).json()
    assert listed["total"] == 1


def test_dispense_queue_lists_active_requests_with_stock(client):
    _patient(client)
    _request(client)
    queue = client.get("/pharmacy/dispense-queue", headers=VIEWER).json()
    assert queue["total"] == 1
    item = queue["items"][0]
    assert item["request"]["id"] == "med-1"
    assert item["medication_code"] == LISINOPRIL
    assert item["stock_quantity"] == 120
    # Wave A: the queue annotates the verification state (none yet).
    assert item["verification"]["state"] == "none"
    assert item["verification"]["verified"] is False
    assert _dispense(client, "med-1").status_code == 201
    assert client.get("/pharmacy/dispense-queue", headers=VIEWER).json()["total"] == 0


def test_oversell_is_409_and_changes_nothing(client):
    _patient(client)
    _request(client, "med-ser", code=SERTRALINE,
             display="Sertraline 50 MG Oral Tablet")
    r = _dispense(client, "med-ser", quantity=61)  # only 60 on hand
    assert r.status_code == 409 and "Insufficient stock" in r.json()["detail"]
    # No dispense written, request still active, stock untouched.
    assert client.get("/pharmacy/dispenses", headers=CLINICIAN).json()["total"] == 0
    req = client.get("/fhir/MedicationRequest/med-ser", headers=CLINICIAN).json()
    assert req["status"] == "active"
    assert _stock(client, SERTRALINE)["quantity"] == 60
    # Exactly-at-stock dispensing succeeds and empties the shelf.
    r = _dispense(client, "med-ser", quantity=60)
    assert r.status_code == 201 and r.json()["stock_after"] == 0


def test_dispense_requires_active_request_and_formulary_medication(client):
    _patient(client)
    _request(client)
    assert _dispense(client, "med-1").status_code == 201
    # Second dispense of the same (now dispensed) request -> 409.
    assert _dispense(client, "med-1").status_code == 409
    # Unknown request -> 404; non-formulary medication -> 422.
    assert _dispense(client, "med-nope").status_code == 404
    _request(client, "med-off", code="9999999", display="Off-formulary synthetic")
    r = _dispense(client, "med-off")
    assert r.status_code == 422 and "formulary" in r.json()["detail"]


def test_dispense_untracked_medication_has_no_stock_effect(client):
    _patient(client)
    _request(client, "med-alb", code=ALBUTEROL,
             display="Albuterol 0.09 MG/ACTUAT Metered Dose Inhaler")
    r = _dispense(client, "med-alb", quantity=2)
    assert r.status_code == 201, r.text
    assert r.json()["stock_after"] is None
    assert _stock(client, ALBUTEROL)["quantity"] is None


def test_dispense_role_gates(client):
    _patient(client)
    _request(client)
    assert _dispense(client, "med-1", headers=VIEWER).status_code == 403
    assert _dispense(client, "med-1", headers=PATIENT).status_code == 403
    assert client.post("/pharmacy/dispense",
                       json={"medication_request_id": "med-1"}).status_code == 401
    assert _dispense(client, "med-1", headers=CONFIG).status_code == 201


# ---------------------------------------------------------------------------
# Med-admin — Wave A (PHARM-M2): the BCMA-lite typed-ID path is
# retired; administration is the five-rights BCMA scan
# (app.pharmacy_ops). The Phase 4A fixture orders carry no structured
# dose/route, so dose+route fail the scan and the happy path proceeds
# on an audited override reason — the same governed override the
# BCMA engine offers for dose/route/time.
# ---------------------------------------------------------------------------

OVERRIDE = "Fixture order carries no structured dose/route"


def _dispensed(client, rid="med-1", pid="pat-1", code=LISINOPRIL):
    _patient(client, pid)
    _request(client, rid, pid, code)
    r = _dispense(client, rid)
    assert r.status_code == 201, r.text
    return r.json()["dispense"]["id"]


def _payloads(client, pid, dispense_id):
    wb = client.post("/pharmacy/barcodes/wristband",
                     json={"patient_id": pid}, headers=CLINICIAN)
    assert wb.status_code == 200, wb.text
    ml = client.post("/pharmacy/barcodes/med-label",
                     json={"dispense_id": dispense_id}, headers=CLINICIAN)
    assert ml.status_code == 200, ml.text
    return wb.json()["payload"], ml.json()["payload"]


def _administer(client, dispense_id, pid="pat-1", headers=CLINICIAN,
                **extra):
    wristband, med_label = _payloads(client, pid, dispense_id)
    body = {"wristband_payload": wristband,
            "med_label_payload": med_label,
            "override_reason": OVERRIDE, **extra}
    return client.post("/pharmacy/bcma/administer", json=body,
                       headers=headers)


def test_bcma_happy_path_creates_administration(client):
    dispense_id = _dispensed(client)
    r = _administer(client, dispense_id)
    assert r.status_code == 201, r.text
    admin = r.json()["administration"]
    assert admin["resourceType"] == "MedicationAdministration"
    assert admin["status"] == "completed"
    assert admin["subject"]["reference"] == "Patient/pat-1"
    assert admin["medicationCodeableConcept"]["coding"][0]["code"] == LISINOPRIL
    ext = [e for e in admin["extension"]
           if e["url"].endswith("/dispense")][0]
    assert ext["valueReference"]["reference"] == f"MedicationDispense/{dispense_id}"
    # Persisted and readable through the generic surface.
    got = client.get(f"/fhir/MedicationAdministration/{admin['id']}",
                     headers=CLINICIAN)
    assert got.status_code == 200


def test_bcma_mismatch_matrix_names_the_failed_right(client):
    from app.pharmacy_ops import _make_payload, default_ops_payload
    dispense_id = _dispensed(client)
    wristband, med_label = _payloads(client, "pat-1", dispense_id)
    # Right patient fails: a wristband payload for a different patient
    # (forged with the governed standard, as a foreign wristband
    # would scan) — absolute even with an override reason.
    forged_wb = _make_payload(default_ops_payload(), "OCWB1",
                              "pat-2", "19750101")
    r = client.post("/pharmacy/bcma/administer", json={
        "wristband_payload": forged_wb, "med_label_payload": med_label,
        "override_reason": OVERRIDE}, headers=CLINICIAN)
    assert r.status_code == 422 and "Right patient" in r.json()["detail"]
    # Right dispense fails: the scanned package is a different lot.
    forged_ml = _make_payload(default_ops_payload(), "OCML1",
                              dispense_id, "LOT-WRONG")
    r = client.post("/pharmacy/bcma/administer", json={
        "wristband_payload": wristband, "med_label_payload": forged_ml,
        "override_reason": OVERRIDE}, headers=CLINICIAN)
    assert r.status_code == 422 and "Right dispense" in r.json()["detail"]
    # Right dispense fails: no such dispense.
    ghost_ml = _make_payload(default_ops_payload(), "OCML1",
                             "disp-nope", "-")
    r = client.post("/pharmacy/bcma/administer", json={
        "wristband_payload": wristband, "med_label_payload": ghost_ml,
        "override_reason": OVERRIDE}, headers=CLINICIAN)
    assert r.status_code == 422 and "Right dispense" in r.json()["detail"]
    # Missing scan payloads are 422 as well.
    r = client.post("/pharmacy/bcma/administer",
                    json={"med_label_payload": med_label},
                    headers=CLINICIAN)
    assert r.status_code == 422 and "wristband_payload" in r.json()["detail"]
    # None of the failed scans created an administration: the happy
    # path still works afterwards.
    assert _administer(client, dispense_id).status_code == 201


def test_bcma_duplicate_administration_is_409(client):
    dispense_id = _dispensed(client)
    assert _administer(client, dispense_id).status_code == 201
    r = _administer(client, dispense_id)
    assert r.status_code == 409 and "already been administered" in r.json()["detail"]


def test_bcma_lite_typed_id_path_is_retired(client):
    # Wave A (PHARM-M2): POST /pharmacy/administer no longer exists —
    # there is exactly one administration engine (the BCMA scan).
    dispense_id = _dispensed(client)
    r = client.post("/pharmacy/administer", json={
        "wristband_id": "pat-1", "medication_code": LISINOPRIL,
        "dispense_id": dispense_id}, headers=CLINICIAN)
    assert r.status_code == 404


def test_administer_role_gates(client):
    dispense_id = _dispensed(client)
    wristband, med_label = _payloads(client, "pat-1", dispense_id)
    body = {"wristband_payload": wristband,
            "med_label_payload": med_label, "override_reason": OVERRIDE}
    # Configurator can dispense but not administer (bedside clinical act).
    assert client.post("/pharmacy/bcma/administer", json=body,
                       headers=CONFIG).status_code == 403
    assert client.post("/pharmacy/bcma/administer", json=body,
                       headers=VIEWER).status_code == 403
    assert client.post("/pharmacy/bcma/administer", json=body,
                       headers=PATIENT).status_code == 403
    assert client.post("/pharmacy/bcma/administer",
                       json=body).status_code == 401
    assert client.post("/pharmacy/bcma/administer", json=body,
                       headers=ADMIN).status_code == 201


def test_pharmacy_writes_are_audited_and_chain_verifies(client):
    dispense_id = _dispensed(client)
    assert _administer(client, dispense_id).status_code == 201
    factory = client.app.state.session_factory
    with factory() as session:
        actions = [row.action for row in session.scalars(select(AuditLog)).all()]
        assert "pharmacy.dispense" in actions
        assert "pharmacy.verify" in actions
        assert "pharmacy.bcma.administer" in actions
        assert verify_chain(session) is True
