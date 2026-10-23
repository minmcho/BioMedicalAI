"""Phase 11 (Track C/G): BCMA — barcode medication administration.

Barcode payload standards live in the published pharmacy-ops config
(prefixes + layout); payloads carry mod-97 check digits. The scan flow
runs the five rights: right patient, right medication and right
dispense (lot) are absolute — no override exists for them; right dose,
route and time may be overridden only with a reason that lands in the
audit chain and on the administration record.
"""
import datetime as dt

from .conftest import ADMIN, CLINICIAN

LISINOPRIL = "1049221"
DOSE = [{
    # Wave A (CHART-B3): a structured sig must be complete against
    # the published medication-sig vocabulary — text, a dose unit
    # from the vocabulary ("tablets"), a route code from the
    # vocabulary ("oral"), and timing.
    "text": "1 tablet by mouth once daily",
    "doseAndRate": [{"doseQuantity": {"value": 1, "unit": "tablets"}}],
    "route": {"coding": [{"system": "http://snomed.info/sct",
                          "code": "oral", "display": "Oral"}]},
    "timing": {"repeat": {"frequency": 1, "period": 24, "periodUnit": "h"}},
}]


def _patient(client, pid, birth="1962-03-14"):
    body = {"resourceType": "Patient", "id": pid,
            "name": [{"family": "Scan", "given": ["P11"]}]}
    if birth:
        body["birthDate"] = birth
    r = client.post("/fhir/Patient", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _dispensed(client, pid, quantity=1):
    r = client.post("/orders", json={
        "type": "medication", "code": LISINOPRIL,
        "code_display": "Lisinopril 10 MG Oral Tablet", "patient": pid,
        "dosageInstruction": DOSE}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    oid = r.json()["order"]["id"]
    for to in ("signed", "active"):
        r = client.post(f"/orders/{oid}/transition",
                        json={"to_status": to}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    # Wave A (PHARM-B1): dispensing requires a current verification,
    # and BCMA requires it to still be current at administration.
    r = client.post(f"/pharmacy/verification/{oid}/verify", json={},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/pharmacy/dispense", json={
        "medication_request_id": oid, "quantity": quantity,
        "status": "active"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return oid, r.json()["dispense"]["id"]


def _payloads(client, pid, dispense_id):
    wb = client.post("/pharmacy/barcodes/wristband",
                     json={"patient_id": pid}, headers=CLINICIAN)
    assert wb.status_code == 200, wb.text
    ml = client.post("/pharmacy/barcodes/med-label",
                     json={"dispense_id": dispense_id}, headers=CLINICIAN)
    assert ml.status_code == 200, ml.text
    return wb.json()["payload"], ml.json()["payload"]


def _scan(client, wristband, med_label, **extra):
    return client.post("/pharmacy/bcma/administer", json={
        "wristband_payload": wristband, "med_label_payload": med_label,
        **extra}, headers=CLINICIAN)


def test_barcode_roundtrip_and_tamper_detection(client):
    pid = _patient(client, "pat-p11-scan")
    _, dispense_id = _dispensed(client, pid)
    wristband, med_label = _payloads(client, pid, dispense_id)
    r = client.post("/pharmacy/barcodes/parse",
                    json={"payload": wristband}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["type"] == "wristband"
    assert r.json()["patient_id"] == pid
    assert r.json()["dob"] == "19620314"
    r = client.post("/pharmacy/barcodes/parse",
                    json={"payload": med_label}, headers=CLINICIAN)
    assert r.json()["type"] == "med-label"
    assert r.json()["dispense_id"] == dispense_id
    assert r.json()["lot"] == f"SEED-{LISINOPRIL}"
    # Flip the final check digit: the payload no longer validates.
    tampered = med_label[:-1] + ("0" if med_label[-1] != "0" else "1")
    r = client.post("/pharmacy/barcodes/parse",
                    json={"payload": tampered}, headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post("/pharmacy/barcodes/parse",
                    json={"payload": "XXXX|a|b|00"}, headers=CLINICIAN)
    assert r.status_code == 422


def test_bcma_full_pass_writes_administration_and_mar(client):
    pid = _patient(client, "pat-p11-pass")
    _, dispense_id = _dispensed(client, pid)
    wristband, med_label = _payloads(client, pid, dispense_id)
    r = _scan(client, wristband, med_label)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["administration"]["status"] == "completed"
    assert out["bcma"]["override"] is None
    checks = {c["right"]: c["result"] for c in out["bcma"]["checks"]}
    assert checks == {"right-patient": "pass", "right-medication": "pass",
                      "right-dispense": "pass", "right-dose": "pass",
                      "right-route": "pass", "right-time": "pass"}
    due = client.get("/orders/mar/due", params={"patient": pid},
                     headers=CLINICIAN).json()
    assert due["doses"][0]["status"] == "given"
    actions = [e["action"] for e in
               client.get("/studio/audit?limit=200", headers=ADMIN).json()]
    assert "pharmacy.bcma.administer" in actions


def test_bcma_wrong_patient_is_absolute_even_with_override(client):
    pid_a = _patient(client, "pat-p11-wrong-a")
    pid_b = _patient(client, "pat-p11-wrong-b", birth="1975-07-07")
    _, dispense_id = _dispensed(client, pid_a)
    wristband_b, med_label = _payloads(client, pid_b, dispense_id)
    r = _scan(client, wristband_b, med_label,
              override_reason="I meant to do this")
    assert r.status_code == 422
    assert "patient" in r.json()["detail"].lower()
    actions = [e["action"] for e in
               client.get("/studio/audit?limit=200", headers=ADMIN).json()]
    assert "pharmacy.bcma.scan_refused" in actions


def test_bcma_lot_mismatch_is_absolute(client):
    from app.pharmacy_ops import _make_payload, default_ops_payload
    pid = _patient(client, "pat-p11-lot")
    _, dispense_id = _dispensed(client, pid)
    wristband, _ = _payloads(client, pid, dispense_id)
    forged = _make_payload(default_ops_payload(), "OCML1",
                            dispense_id, "LOT-WRONG")
    r = _scan(client, wristband, forged, override_reason="Different lot")
    assert r.status_code == 422
    assert "lot" in r.json()["detail"].lower()


def test_bcma_dose_mismatch_needs_audited_override(client):
    pid = _patient(client, "pat-p11-dose")
    _, dispense_id = _dispensed(client, pid, quantity=2)  # ordered dose: 1
    wristband, med_label = _payloads(client, pid, dispense_id)
    r = _scan(client, wristband, med_label)
    assert r.status_code == 422
    assert "dose" in r.json()["detail"].lower()
    r = _scan(client, wristband, med_label,
              override_reason="Dose increased verbally, order to follow")
    assert r.status_code == 201, r.text
    checks = {c["right"]: c["result"] for c in r.json()["bcma"]["checks"]}
    assert checks["right-dose"] == "fail"
    assert r.json()["bcma"]["override"]["rights"] == ["right-dose"]
    admin_id = r.json()["administration"]["id"]
    record = client.get(f"/fhir/MedicationAdministration/{admin_id}",
                        headers=CLINICIAN).json()
    ext = [e for e in record.get("extension", [])
           if e["url"] == "urn:opencare:extension/bcma-override"]
    assert ext and "Dose increased verbally" in ext[0]["valueString"]


def test_bcma_time_window_override(client, monkeypatch):
    import app.pharmacy_ops as ops
    pid = _patient(client, "pat-p11-time")
    _, dispense_id = _dispensed(client, pid)
    wristband, med_label = _payloads(client, pid, dispense_id)
    # Six hours after the dose anchor: outside the +/-1h MAR window.
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=6)
    monkeypatch.setattr(ops, "_now", lambda: future)
    r = _scan(client, wristband, med_label)
    assert r.status_code == 422
    assert "time" in r.json()["detail"].lower()
    r = _scan(client, wristband, med_label,
              override_reason="Dose delayed by procedure")
    assert r.status_code == 201, r.text
    checks = {c["right"]: c["result"] for c in r.json()["bcma"]["checks"]}
    assert checks["right-time"] == "fail"


def test_bcma_double_administration_conflict(client):
    pid = _patient(client, "pat-p11-double")
    _, dispense_id = _dispensed(client, pid)
    wristband, med_label = _payloads(client, pid, dispense_id)
    assert _scan(client, wristband, med_label).status_code == 201
    r = _scan(client, wristband, med_label)
    assert r.status_code == 409


def test_wristband_requires_patient_dob(client):
    pid = _patient(client, "pat-p11-nodob", birth=None)
    r = client.post("/pharmacy/barcodes/wristband",
                    json={"patient_id": pid}, headers=CLINICIAN)
    assert r.status_code == 422


def test_patient_dispenses_listing_with_lot(client):
    pid = _patient(client, "pat-p11-history")
    _, dispense_id = _dispensed(client, pid)
    out = client.get(f"/pharmacy/patients/{pid}/dispenses",
                     headers=CLINICIAN).json()
    assert out["total"] == 1
    item = out["items"][0]
    assert item["dispense_id"] == dispense_id
    assert item["medication_code"] == LISINOPRIL
    assert item["lot_number"] == f"SEED-{LISINOPRIL}"
    out = client.get("/pharmacy/patients/pat-nobody/dispenses",
                     headers=CLINICIAN).json()
    assert out["total"] == 0
