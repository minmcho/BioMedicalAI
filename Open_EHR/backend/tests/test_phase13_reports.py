"""Phase 13 — report catalogue, printable documents, export & import.

Report definitions are governed Studio config whose data source is a
NAMED server-side query (never SQL from config). Server-side PDF is
ReportLab. Every report run and document print is audited. Exports are
capability-gated, audited with row counts, and above a config row
threshold require a reason. Imports validate fully (row/field/reason)
and commit all-or-nothing.

Synthetic data only.
"""
from .conftest import ADMIN, CLINICIAN, PATIENT, VIEWER

SCT = "http://snomed.info/sct"
CPT = "http://www.ama-assn.org/go/cpt"


def _audit_actions(client):
    r = client.get("/studio/audit", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Billing fixtures (same shapes as the Phase 7 suite)
# ---------------------------------------------------------------------------

def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic", "given": ["Rev"]}],
        "gender": "female", "birthDate": "1980-04-02"}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _chart(client, pid, eid, cond_code, proc_code=None):
    _patient(client, pid)
    r = client.post("/fhir/Encounter", json={
        "resourceType": "Encounter", "id": eid, "status": "finished",
        "subject": {"reference": f"Patient/{pid}"},
        "period": {"start": "2026-01-10T09:00:00Z",
                   "end": "2026-01-10T10:00:00Z"}}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/fhir/Condition", json={
        "resourceType": "Condition", "id": f"cond-{eid}",
        "code": {"coding": [{"system": SCT, "code": cond_code,
                             "display": "Synthetic condition"}]},
        "subject": {"reference": f"Patient/{pid}"}}, headers=ADMIN)
    assert r.status_code == 201, r.text
    if proc_code:
        r = client.post("/fhir/Procedure", json={
            "resourceType": "Procedure", "id": f"proc-{eid}",
            "status": "completed",
            "code": {"coding": [{"system": CPT, "code": proc_code,
                                 "display": "Synthetic procedure"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "encounter": {"reference": f"Encounter/{eid}"}}, headers=ADMIN)
        assert r.status_code == 201, r.text


def _claim(client, eid):
    r = client.post("/billing/claims/generate", json={"encounter_id": eid},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    claim = r.json()
    for target in ("ready", "submitted"):
        r = client.post(f"/billing/claims/{claim['id']}/transition",
                        json={"to_status": target}, headers=ADMIN)
        assert r.status_code == 200, r.text
    return claim


def _835(clp_blocks, check="CHK-1", total="180.00"):
    segments = [
        "ST*835*0001",
        "BHT*0019*00*REF123*20261017*1200",
        f"BPR*I*{total}*C*ACH",
        "N1*PR*SYNTHETIC PAYER*****46*SYNTH-PAYER",
        f"TRN*1*{check}*123456789",
    ]
    for block in clp_blocks:
        segments.extend(block)
    segments.append(f"SE*{len(segments) + 1}*0001")
    envelope = [
        "ISA*00*          *00*          *ZZ*OPENCARE       "
        "*ZZ*SYNTHPAYER     *261017*1200*^*00501*000000001*0*T*:",
        "GS*HP*OPENCARE*SYNTHPAYER*20261017*1200*1*X*005010X221A1",
    ]
    return "".join(f"{s}~\n" for s in envelope + segments
                   + ["GE*1*1", "IEA*1*000000001"])


def _clp(claim_id, status, charge, paid, resp, extra=()):
    return [f"CLP*{claim_id}*{status}*{charge}*{paid}*{resp}**MB*R-{claim_id[:6]}",
            *extra]


def _claim_with_patient_balance(client):
    _chart(client, "pat-1", "enc-p", "55822004", proc_code="20610")
    claim = _claim(client, "enc-p")
    text = _835([_clp(claim["id"], "1", "240.00", "180.00", "20.00",
                      extra=["SVC*HC:20610*210.00*180.00",
                             "CAS*CO*45*30.00", "CAS*PR*1*20.00"])])
    r = client.post("/billing/remittances", content=text,
                    headers={**ADMIN, "Content-Type": "text/plain"})
    assert r.status_code == 201, r.text
    return claim


# ---------------------------------------------------------------------------
# Report catalogue
# ---------------------------------------------------------------------------

def test_catalogue_seeded_and_runs_csv_and_pdf(client):
    r = client.get("/reports/catalogue", headers=CLINICIAN)
    assert r.status_code == 200
    keys = {i["key"] for i in r.json()["items"]}
    assert {"census-by-unit", "claims-summary", "credential-expiry",
            "safety-events", "payments-summary"} <= keys
    entry = [i for i in r.json()["items"] if i["key"] == "credential-expiry"][0]
    assert entry["source"] == "credential-expiry"
    assert entry["parameters"][0]["name"] == "window_days"

    r = client.post("/reports/census-by-unit/run",
                    json={"format": "csv"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "Unit" in r.text and "Medical-Surgical" in r.text
    # Organisation & Registration Placement pass: the locations seed
    # now carries 4 unit concepts (unit-medsurg, unit-icu, ed, clinic)
    # so census-by-unit emits one row per unit.
    assert r.headers["x-report-rows"] == "4"

    r = client.post("/reports/credential-expiry/run",
                    json={"format": "pdf", "params": {"window_days": 30}},
                    headers=ADMIN)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-"
    rows = [a for a in _audit_actions(client) if a["action"] == "reports.run"]
    assert rows and rows[0]["entity"].startswith("report:credential-expiry")
    # Patients cannot run reports.
    assert client.post("/reports/census-by-unit/run",
                       json={"format": "csv"},
                       headers=PATIENT).status_code == 403


def test_report_run_validates_params_and_formats(client):
    # Unknown parameter.
    r = client.post("/reports/credential-expiry/run",
                    json={"format": "csv", "params": {"bogus": 1}},
                    headers=ADMIN)
    assert r.status_code == 422
    # Parameter of the wrong type.
    r = client.post("/reports/credential-expiry/run",
                    json={"format": "csv", "params": {"window_days": "soon"}},
                    headers=ADMIN)
    assert r.status_code == 422
    # Format not offered by the definition.
    r = client.post("/reports/census-by-unit/run",
                    json={"format": "xlsx"}, headers=ADMIN)
    assert r.status_code == 422
    # Unknown report key.
    assert client.post("/reports/nope/run", json={"format": "csv"},
                       headers=ADMIN).status_code == 404
    # Choice parameter outside its choices.
    r = client.post("/reports/safety-events/run",
                    json={"format": "csv", "params": {"status": "weird"}},
                    headers=ADMIN)
    assert r.status_code == 422


def test_report_definitions_are_governed_config(client):
    # A report definition carrying a data source that is not a named
    # server-side query is refused at validation — no ad-hoc SQL.
    r = client.post("/studio/config/report/evil", json={
        "title": "Evil", "source": "SELECT * FROM patients",
        "formats": ["csv"]}, headers=ADMIN)
    assert r.status_code == 422
    r = client.post("/studio/config/report/ok-report", json={
        "title": "Census again", "source": "census-by-unit",
        "formats": ["csv"]}, headers=ADMIN)
    assert r.status_code == 201, r.text
    # Draft only until published: not in the catalogue yet.
    keys = {i["key"] for i in
            client.get("/reports/catalogue", headers=ADMIN).json()["items"]}
    assert "ok-report" not in keys


# ---------------------------------------------------------------------------
# Printable documents
# ---------------------------------------------------------------------------

def test_invoice_statement_superbill_pdfs_and_print_audit(client):
    claim = _claim_with_patient_balance(client)
    r = client.get(f"/reports/documents/invoice/{claim['id']}", headers=ADMIN)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-"
    assert client.get("/reports/documents/invoice/nope",
                      headers=ADMIN).status_code == 404

    r = client.get("/reports/documents/statement/pat-1", headers=ADMIN)
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    assert client.get("/reports/documents/statement/pat-404",
                      headers=ADMIN).status_code == 404

    # Superbill needs a HIM coding record: none yet -> honest 422.
    r = client.get("/reports/documents/superbill/enc-p", headers=ADMIN)
    assert r.status_code == 422
    r = client.post("/billing/coding/enc-p", json={
        "diagnoses": [{"code": "E11.9"}],
        "procedures": [{"code": "20610"}]}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.get("/reports/documents/superbill/enc-p", headers=ADMIN)
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"

    prints = [a for a in _audit_actions(client)
              if a["action"] == "reports.document.print"]
    docs = {a["entity"].split(":")[1] for a in prints}
    assert {"invoice", "statement", "superbill"} <= docs
    # Print is capability-gated: patients cannot print documents.
    assert client.get("/reports/documents/statement/pat-1",
                      headers=PATIENT).status_code == 403


def test_receipt_pdf_for_recorded_payment(client):
    claim = _claim_with_patient_balance(client)
    r = client.post(f"/billing/claims/{claim['id']}/patient-payments",
                    json={"amount": 12.50, "method": "card"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    payment_id = r.json()["id"]
    r = client.get(f"/reports/documents/receipt/{payment_id}", headers=ADMIN)
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    assert client.get("/reports/documents/receipt/nope",
                      headers=ADMIN).status_code == 404


# ---------------------------------------------------------------------------
# Export centre
# ---------------------------------------------------------------------------

def test_exports_gated_audited_with_row_counts(client):
    _patient(client, "pat-1")
    _patient(client, "pat-2")
    r = client.get("/reports/exports/datasets", headers=ADMIN)
    keys = {i["key"] for i in r.json()["items"]}
    assert {"patients", "appointments", "orders", "lab-results",
            "billing-lines"} <= keys
    # Capability gate: clinicians/viewers cannot export.
    assert client.post("/reports/exports", json={"dataset": "patients"},
                       headers=CLINICIAN).status_code == 403
    assert client.post("/reports/exports", json={"dataset": "patients"},
                       headers=VIEWER).status_code == 403
    r = client.post("/reports/exports",
                    json={"dataset": "patients", "format": "csv"},
                    headers=ADMIN)
    assert r.status_code == 200
    assert r.headers["x-export-rows"] == "2"
    assert "pat-1" in r.text
    r = client.post("/reports/exports",
                    json={"dataset": "patients", "format": "json"},
                    headers=ADMIN)
    assert r.json()["total"] == 2
    # Unknown dataset is refused — exports are named queries only.
    r = client.post("/reports/exports", json={"dataset": "everything"},
                    headers=ADMIN)
    assert r.status_code == 422
    exports = [a for a in _audit_actions(client)
               if a["action"] == "data.export"]
    assert exports and all(
        a["entity"].startswith("dataset:patients") for a in exports)


def test_export_reason_required_above_config_threshold(client):
    _patient(client, "pat-1")
    # Republish the export policy with a zero-row threshold: every
    # export then needs a reason — the threshold is config.
    r = client.get("/studio/config/integration/effective/exports",
                   headers=ADMIN)
    payload = r.json()["payload"]
    payload["reasonRequiredAboveRows"] = 0
    r = client.post("/studio/config/integration/exports",
                    json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/integration/exports/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text

    r = client.post("/reports/exports", json={"dataset": "patients"},
                    headers=ADMIN)
    assert r.status_code == 422
    assert "reason" in r.json()["detail"]
    r = client.post("/reports/exports", json={
        "dataset": "patients", "reason": "Monthly registry reconciliation"},
        headers=ADMIN)
    assert r.status_code == 200
    exports = [a for a in _audit_actions(client)
               if a["action"] == "data.export"]
    assert exports and exports[0]["entity"].startswith("dataset:patients")


# ---------------------------------------------------------------------------
# Import centre
# ---------------------------------------------------------------------------

PATIENTS_CSV = ("MRN,Last Name,First Name,Sex,DOB\n"
                "pat-imp-1,Doe,Jane,F,01/15/1990\n"
                "pat-imp-2,Roe,John,M,03/02/1985\n")


def test_import_patients_validate_commit_and_audit(client):
    r = client.post("/reports/imports/validate", json={
        "profile": "patient-intake", "csv": PATIENTS_CSV}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True and body["rows_total"] == 2
    assert body["preview"][0]["family_name"] == "Doe"
    assert body["preview"][0]["gender"] == "female"  # code-map applied
    assert body["preview"][0]["birth_date"] == "1990-01-15"  # date-mdY
    # Validation wrote nothing.
    assert client.get("/fhir/Patient/pat-imp-1",
                      headers=ADMIN).status_code == 404
    r = client.post("/reports/imports/commit", json={
        "profile": "patient-intake", "csv": PATIENTS_CSV}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["imported"] == 2
    got = client.get("/fhir/Patient/pat-imp-1", headers=ADMIN)
    assert got.status_code == 200
    assert got.json()["name"][0]["family"] == "Doe"
    imports = [a for a in _audit_actions(client)
               if a["action"] == "data.import"]
    assert imports and imports[0]["entity"] == "import:patient-intake"
    # Import capability gate.
    assert client.post("/reports/imports/validate", json={
        "profile": "patient-intake", "csv": PATIENTS_CSV},
        headers=CLINICIAN).status_code == 403


def test_import_all_or_nothing_with_full_error_report(client):
    bad = ("MRN,Last Name,First Name,Sex,DOB\n"
           "pat-bad-1,Good,Row,F,01/15/1990\n"
           "pat-bad-2,,Missing,M,03/02/1985\n"
           "pat-bad-3,Bad,Date,F,13/40/1990\n"
           "pat-bad-1,Dup,Row,M,05/05/1995\n")
    r = client.post("/reports/imports/validate", json={
        "profile": "patient-intake", "csv": bad}, headers=ADMIN)
    body = r.json()
    assert body["valid"] is False
    fields = {(e["row"], e["field"]) for e in body["errors"]}
    assert (3, "family_name") in fields       # missing required value
    assert (4, "birth_date") in fields        # bad date format
    assert (5, "id") in fields                # duplicate id within file
    r = client.post("/reports/imports/commit", json={
        "profile": "patient-intake", "csv": bad}, headers=ADMIN)
    assert r.status_code == 422
    assert r.json()["detail"]["errors"]
    # All-or-nothing: not even the good first row landed.
    assert client.get("/fhir/Patient/pat-bad-1",
                      headers=ADMIN).status_code == 404


def test_import_appointments_requires_existing_patient(client):
    _patient(client, "pat-1")
    csv_text = ("Patient ID,Start,Type,Status\n"
                "pat-1,2026-11-01T09:00:00Z,Follow-up,B\n")
    r = client.post("/reports/imports/commit", json={
        "profile": "appointment-load", "csv": csv_text}, headers=ADMIN)
    assert r.status_code == 201, r.text
    appts = client.get("/fhir/Appointment", headers=ADMIN).json()
    assert appts["total"] == 1
    csv_bad = ("Patient ID,Start,Type,Status\n"
               "pat-404,2026-11-01T09:00:00Z,Follow-up,B\n")
    r = client.post("/reports/imports/validate", json={
        "profile": "appointment-load", "csv": csv_bad}, headers=ADMIN)
    assert r.json()["valid"] is False
    assert r.json()["errors"][0]["field"] == "patient_id"


def test_import_pharmacy_lots_move_stock_and_ledger(client):
    before = client.get("/pharmacy/inventory", headers=ADMIN).json()
    stock_before = sum(i["quantity"] for i in before["items"]
                       if i["medication_code"] == "1049221")
    csv_text = ("Medication,Location,Lot,Expiry,Qty\n"
                "1049221,main-pharmacy,LOT-IMP-9,12/31/2027,40\n")
    r = client.post("/reports/imports/commit", json={
        "profile": "pharmacy-lot-load", "csv": csv_text}, headers=ADMIN)
    assert r.status_code == 201, r.text
    after = client.get("/pharmacy/inventory", headers=ADMIN).json()
    lots = [i for i in after["items"] if i["lot_number"] == "LOT-IMP-9"]
    assert lots and lots[0]["quantity"] == 40
    stock_after = sum(i["quantity"] for i in after["items"]
                      if i["medication_code"] == "1049221")
    assert stock_after == stock_before + 40
    # Unknown medication and expiry-mismatch lots are refused cleanly.
    bad = ("Medication,Location,Lot,Expiry,Qty\n"
           "9999999,main-pharmacy,LOT-X,12/31/2027,5\n")
    r = client.post("/reports/imports/commit", json={
        "profile": "pharmacy-lot-load", "csv": bad}, headers=ADMIN)
    assert r.status_code == 422
    bad2 = ("Medication,Location,Lot,Expiry,Qty\n"
            "1049221,main-pharmacy,LOT-IMP-9,01/01/2028,5\n")
    r = client.post("/reports/imports/validate", json={
        "profile": "pharmacy-lot-load", "csv": bad2}, headers=ADMIN)
    assert r.json()["valid"] is False
    assert any("expiry" in e["reason"] for e in r.json()["errors"])


def test_mapping_profiles_are_governed_config(client):
    # A transform outside the governed set is refused at validation.
    r = client.post("/studio/config/mapping/evil-map", json={
        "entityType": "patients",
        "fields": [{"source": "A", "target": "id",
                    "transform": "eval-python"}]}, headers=ADMIN)
    assert r.status_code == 422
    # code-map without a map is refused.
    r = client.post("/studio/config/mapping/bad-map", json={
        "entityType": "patients",
        "fields": [{"source": "A", "target": "id",
                    "transform": "code-map"}]}, headers=ADMIN)
    assert r.status_code == 422
    profiles = client.get("/reports/imports/profiles",
                          headers=ADMIN).json()["items"]
    keys = {p["key"] for p in profiles}
    assert {"patient-intake", "appointment-load", "pharmacy-lot-load"} <= keys


# ---------------------------------------------------------------------------
# Compliance register additions
# ---------------------------------------------------------------------------

def test_phase13_register_entries(client):
    r = client.get("/compliance/requirements", headers=ADMIN)
    assert r.status_code == 200
    entries = r.json()["items"]
    citations = [e["citation"] for e in entries]
    hr = [e for e in entries if e["citation"].endswith("HR.01.02.05")]
    assert hr and "credential" in hr[0]["title"].lower()
    assert any(c.endswith("MS.06.01.03") for c in citations)
    retention = [e for e in entries
                 if e["citation"] == "42 CFR 482.24(b)(1)"]
    assert retention and "retention" in retention[0]["title"].lower()
    # The register grew by exactly the Phase 13 entries (21 -> 24);
    # Phase 14 adds two more (Joint Commission EC.02.04.03 equipment
    # maintenance + CLIA 42 CFR 493.1253 analyser calibration); the
    # Front Door pass adds HIPAA 164.312(a)(2)(i) unique user
    # identification (the account system); Phase 16 adds six more
    # (ICH-GCP E6(R2) 8.1 + 5.18, FDA-GLP 21 CFR 58.195 + 58.29,
    # FDA 21 CFR 11.50 + 11.70 — the research packs' rows).
    # Workforce, Teams & Panels pass adds one more (CMS 42 CFR
    # 482.22 medical staff — credentialing/privileging).
    # Phase 17 (WS-SEC) adds three more (HIPAA 164.312(a)(2)(iv) /
    # CMP-30 key management, 164.312(b) / CMP-31 snooping monitoring,
    # 164.514(d) / CMP-32 log redaction) — final seeded register
    # enumerated at 37 by the Phase 17 coordinator.
    assert r.json()["total"] == 37
