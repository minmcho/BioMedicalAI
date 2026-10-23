"""Phase 15 (C/H-1): barcode registry, universal lookup, label printing.

Registration validates values against the published
integration/barcode-standards config (per-kind prefix + separator,
ISBT 128 for blood); one scan resolves any registered barcode to
its entity; labels render as label-sized PDFs on the published
print template's org identity, audited like every Phase 13 print.
"""
from .conftest import ADMIN, CLINICIAN, PATIENT, VIEWER

DIN = "=A999922123456"  # ISBT 128: '=' + facility A9999 + year 22 + serial


def _patient(client, pid="pat-bc-1", birth="1962-03-14"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Barcode", "given": ["Synthetic"],
                  "text": "Synthetic Barcode"}],
        "gender": "female", "birthDate": birth,
        "identifier": [{"system": "urn:opencare:mrn", "value": "MRN-BC-1"}]},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _register(client, value, kind, entity_ref, expect=201):
    r = client.post("/barcodes", json={
        "value": value, "kind": kind, "entity_ref": entity_ref},
        headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _lookup(client, value, headers=CLINICIAN):
    return client.get("/barcodes/lookup", params={"value": value},
                      headers=headers)


# ---------------------------------------------------------------------------
# Standards + registration validation
# ---------------------------------------------------------------------------

def test_standards_published_and_registration_validation(client):
    r = client.get("/barcodes/standards", headers=VIEWER)
    assert r.status_code == 200, r.text
    kinds = r.json()["standards"]["kinds"]
    assert kinds["wristband"]["prefix"] == "OCWB1"
    assert kinds["medication"]["prefix"] == "OCML1"
    assert kinds["blood"]["standard"] == "ISBT 128"

    _patient(client)
    # Wrong prefix for the kind -> 422.
    _register(client, "XXXX1|pat-bc-1|19620314", "wristband",
              "Patient/pat-bc-1", expect=422)
    # Prefix but no separator/payload -> 422.
    _register(client, "OCWB1", "wristband", "Patient/pat-bc-1", expect=422)
    # Blood value that is not an ISBT 128 DIN -> 422.
    _register(client, "UNIT-123", "blood", "BloodUnit/UNIT-123", expect=422)
    # Unknown kind -> 422.
    _register(client, "OCWB1|pat-bc-1|19620314", "hat", "Patient/pat-bc-1",
              expect=422)
    # A value registers exactly once.
    _register(client, "OCAS1|scale-1", "asset", "Device/scale-1")
    _register(client, "OCAS1|scale-1", "asset", "Device/scale-1", expect=409)
    # Viewers may read but never manage the registry.
    r = client.post("/barcodes", json={
        "value": "OCAS1|scale-2", "kind": "asset",
        "entity_ref": "Device/scale-2"}, headers=VIEWER)
    assert r.status_code == 403, r.text


def test_unknown_barcode_lookup_is_honest_404(client):
    r = _lookup(client, "OCWB1|nobody|20000101")
    assert r.status_code == 404, r.text
    body = r.json()
    assert body["registered"] is False
    assert "not a registered barcode" in body["detail"]


# ---------------------------------------------------------------------------
# One kind at a time: register -> lookup -> label
# ---------------------------------------------------------------------------

def test_wristband_register_lookup_label(client):
    pid = _patient(client)
    wb = client.post("/pharmacy/barcodes/wristband",
                     json={"patient_id": pid}, headers=CLINICIAN)
    assert wb.status_code == 200, wb.text
    value = wb.json()["payload"]
    out = _register(client, value, "wristband", f"Patient/{pid}").json()
    assert out["status"] == "active"

    r = _lookup(client, value)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "wristband"
    assert body["entity"]["resolved"] is True
    patient = body["entity"]["patient"]
    assert patient["name"] == "Synthetic Barcode"
    assert patient["mrn"] == "MRN-BC-1"
    assert patient["birth_date"] == "1962-03-14"
    assert body["history"][0]["event"] == "registered"
    # Patient identity is staff-only: the patient role holds no
    # barcode.read capability.
    assert _lookup(client, value, headers=PATIENT).status_code == 403

    r = client.get(f"/barcodes/labels/wristband/Patient/{pid}",
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    # The print landed in the barcode's history (print confidentiality).
    history = _lookup(client, value).json()["history"]
    assert any(h["event"] == "label-printed" for h in history)


def test_specimen_and_aliquot_lookup_and_label(client):
    _patient(client)
    parent = {"resourceType": "Specimen", "id": "sp-bc-parent",
              "status": "available",
              "accessionIdentifier": {"system": "urn:opencare:accession",
                                      "value": "ACC-BC-100"},
              "type": {"text": "Blood"},
              "subject": {"reference": "Patient/pat-bc-1"}}
    r = client.post("/fhir/Specimen", json=parent, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    child = {"resourceType": "Specimen", "id": "sp-bc-child",
             "status": "available",
             "accessionIdentifier": {"system": "urn:opencare:accession",
                                     "value": "ACC-BC-100-A"},
             "type": {"text": "Blood aliquot"},
             "parent": [{"reference": "Specimen/sp-bc-parent"}],
             "subject": {"reference": "Patient/pat-bc-1"}}
    r = client.post("/fhir/Specimen", json=child, headers=CLINICIAN)
    assert r.status_code == 201, r.text

    _register(client, "OCSP1|sp-bc-parent", "specimen",
              "Specimen/sp-bc-parent")
    _register(client, "OCAL1|sp-bc-child", "aliquot",
              "Specimen/sp-bc-child")

    body = _lookup(client, "OCSP1|sp-bc-parent").json()
    assert body["entity"]["resolved"] is True
    specimen = body["entity"]["specimen"]
    assert specimen["accession"] == "ACC-BC-100"
    assert "family_barcodes" in body["entity"]["family_chain"]

    body = _lookup(client, "OCAL1|sp-bc-child").json()
    assert body["kind"] == "aliquot"
    assert body["entity"]["specimen"]["accession"] == "ACC-BC-100-A"
    assert body["entity"]["specimen"]["parent_refs"] == [
        "Specimen/sp-bc-parent"]

    r = client.get("/barcodes/labels/specimen/Specimen/sp-bc-parent",
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"%PDF")
    r = client.get("/barcodes/labels/aliquot/Specimen/sp-bc-child",
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"%PDF")
    # No specimen -> no label, honestly.
    r = client.get("/barcodes/labels/specimen/Specimen/sp-nope",
                   headers=CLINICIAN)
    assert r.status_code == 404, r.text


def test_blood_isbt128_lookup_and_label(client):
    r = client.post("/lab/blood-bank/units", json={
        "unit_id": DIN, "product_type": "rbc", "blood_group": "O POS",
        "expiry_date": "2099-01-01"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    _register(client, DIN, "blood", f"BloodUnit/{DIN}")

    body = _lookup(client, DIN).json()
    assert body["kind"] == "blood"
    unit = body["entity"]["blood_unit"]
    assert unit["donation_id"] == DIN
    assert unit["product_code"] == "rbc"
    assert unit["blood_group"] == "O POS"
    assert unit["expiry_date"] == "2099-01-01"

    r = client.get(f"/barcodes/labels/blood/BloodUnit/{DIN}",
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"%PDF")


def test_medication_bcma_context_lookup_and_label(client):
    pid = _patient(client, "pat-bc-med")
    dose = [{
        "text": "1 tablet by mouth once daily",
        "doseAndRate": [{"doseQuantity": {"value": 1, "unit": "tablets"}}],
        "route": {"coding": [{"system": "http://snomed.info/sct",
                              "code": "oral", "display": "Oral"}]},
        "timing": {"repeat": {"frequency": 1, "period": 24,
                              "periodUnit": "h"}}}]
    r = client.post("/orders", json={
        "type": "medication", "code": "1049221",
        "code_display": "Lisinopril 10 MG Oral Tablet", "patient": pid,
        "dosageInstruction": dose}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    oid = r.json()["order"]["id"]
    for to in ("signed", "active"):
        r = client.post(f"/orders/{oid}/transition",
                        json={"to_status": to}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    # Wave A (PHARM-B1): dispensing requires a current verification.
    r = client.post(f"/pharmacy/verification/{oid}/verify", json={},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/pharmacy/dispense", json={
        "medication_request_id": oid, "quantity": 1, "status": "active"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    dispense_id = r.json()["dispense"]["id"]

    ml = client.post("/pharmacy/barcodes/med-label",
                     json={"dispense_id": dispense_id}, headers=CLINICIAN)
    assert ml.status_code == 200, ml.text
    value = ml.json()["payload"]
    _register(client, value, "medication",
              f"MedicationDispense/{dispense_id}")

    body = _lookup(client, value).json()
    assert body["kind"] == "medication"
    assert body["entity"]["resolved"] is True
    assert body["entity"]["dispense"]["dispense_id"] == dispense_id
    assert body["entity"]["dispense"]["medication_code"] == "1049221"
    assert body["entity"]["bcma"]["context"] == "dispense"
    assert body["entity"]["bcma"]["order_ref"] == \
        f"MedicationRequest/{oid}"

    r = client.get(
        f"/barcodes/labels/medication/MedicationDispense/{dispense_id}",
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"%PDF")


def test_asset_lookup_label_and_revoke(client):
    r = client.post("/devices", json={
        "device_key": "scale-bc-1", "name": "Ward scale",
        "device_type": "analyser", "location": "med-surg",
        "serial": "SN-BC-1"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    _register(client, "OCAS1|scale-bc-1", "asset", "Device/scale-bc-1")

    body = _lookup(client, "OCAS1|scale-bc-1").json()
    assert body["entity"]["device"]["device_key"] == "scale-bc-1"
    assert body["entity"]["device"]["location"] == "med-surg"

    r = client.get("/barcodes/labels/asset/Device/scale-bc-1",
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"%PDF")

    r = client.post("/barcodes/OCAS1|scale-bc-1/revoke", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "revoked"
    # A revoked value still resolves — with its status explained.
    body = _lookup(client, "OCAS1|scale-bc-1").json()
    assert body["status"] == "revoked"
    assert any(h["event"] == "revoked" for h in body["history"])
    r = client.post("/barcodes/OCAS1|scale-bc-1/revoke", headers=CLINICIAN)
    assert r.status_code == 409, r.text

    # Registry listing filters by kind.
    r = client.get("/barcodes", params={"kind": "asset"}, headers=VIEWER)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1
