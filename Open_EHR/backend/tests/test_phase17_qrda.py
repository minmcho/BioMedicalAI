"""Phase 17, WS-COMP — BD-60 eCQM QRDA I / QRDA III export.

Synthetic warehouse population with known HbA1c-control arithmetic
(6 denominator, 5 numerator), QRDA III aggregate + QRDA I patient
documents generated from the Phase 9 measure engine, and the local
structural validation report clean on the implemented checks.
Full CMS schematron validation is NOT RUN (artefacts unavailable
locally) — the report says so.
"""
import pytest

from tests.conftest import ADMIN, CLINICIAN, PATIENT

SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
T2DM = "44054006"


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid, "birthDate": "1990-06-15",
        "gender": "female",
        "name": [{"family": "Synth", "given": [pid]}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _condition(client, pid):
    r = client.post("/fhir/Condition", json={
        "resourceType": "Condition",
        "code": {"coding": [{"system": SNOMED, "code": T2DM}]},
        "subject": {"reference": f"Patient/{pid}"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _a1c(client, pid, value):
    r = client.post("/fhir/Observation", json={
        "resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": LOINC, "code": "4548-4"}]},
        "subject": {"reference": f"Patient/{pid}"},
        "effectiveDateTime": "2026-01-10T09:00:00Z",
        "valueQuantity": {"value": value, "unit": "%"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


@pytest.fixture()
def measure_data(client, monkeypatch, tmp_path):
    monkeypatch.setenv("WAREHOUSE_DATABASE_URL", f"sqlite:///{tmp_path}/wh.db")
    for i, value in enumerate([6.1, 6.5, 7.0, 7.2, 7.9, 8.4], start=1):
        pid = f"dm-{i:02d}"
        _patient(client, pid)
        _condition(client, pid)
        _a1c(client, pid, value)
    r = client.post("/warehouse/etl/run", headers=ADMIN)
    assert r.status_code == 200 and r.json()["dead_letter"] == 0, r.text
    return client


def test_qrda3_aggregate_from_measure_engine(measure_data):
    r = measure_data.post("/qrda/measures/hba1c-control/qrda3", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["document_type"] == "QRDA III"
    assert out["denominator"] == 6
    assert out["numerator"] == 5
    assert "2.16.840.1.113883.10.20.27.1.1" in out["xml"]
    assert out["validation"]["valid"] is True, out["validation"]
    assert out["validation"]["document_type"] == "QRDA III"
    assert "NOT RUN" in out["validation"]["not_run"]


def test_qrda1_patient_level_membership(measure_data):
    r = measure_data.post("/qrda/measures/hba1c-control/qrda1/dm-01",
                          headers=CLINICIAN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["in_denominator"] is True and out["in_numerator"] is True
    assert "2.16.840.1.113883.10.20.24.1.1" in out["xml"]
    assert out["validation"]["valid"] is True, out["validation"]
    # dm-06 (HbA1c 8.4) is denominator-only.
    r = measure_data.post("/qrda/measures/hba1c-control/qrda1/dm-06",
                          headers=CLINICIAN)
    out = r.json()
    assert out["in_denominator"] is True and out["in_numerator"] is False
    assert out["validation"]["valid"] is True, out["validation"]


def test_qrda1_unknown_patient_404(measure_data):
    r = measure_data.post("/qrda/measures/hba1c-control/qrda1/nope-99",
                          headers=CLINICIAN)
    assert r.status_code == 404


def test_qrda_role_gate_and_unknown_measure(measure_data):
    assert measure_data.post("/qrda/measures/hba1c-control/qrda3",
                             headers=PATIENT).status_code == 403
    assert measure_data.post("/qrda/measures/no-such-measure/qrda3",
                             headers=CLINICIAN).status_code == 404


def test_validate_endpoint_clean_and_dirty(measure_data):
    good = measure_data.post("/qrda/measures/hba1c-control/qrda3",
                             headers=CLINICIAN).json()["xml"]
    r = measure_data.post("/qrda/validate", headers=CLINICIAN,
                          json={"xml": good, "document_type": "QRDA III"})
    assert r.status_code == 200 and r.json()["valid"] is True
    failed = {c["check"]: c["status"] for c in r.json()["checks"]}
    assert all(status == "pass" for status in failed.values())
    assert set(failed) == set(r.json()["implemented_checks"])
    # Numerator > denominator fails the aggregate sanity check.
    dirty = good.replace('value="5"', 'value="9"', 1) if 'value="5"' in good \
        else good.replace('value="6"', 'value="2"', 1)
    r = measure_data.post("/qrda/validate", headers=CLINICIAN, json={"xml": dirty})
    checks = {c["check"]: c["status"] for c in r.json()["checks"]}
    assert checks["cat3-aggregate-counts-sane"] == "fail"
    assert r.json()["valid"] is False
    # Malformed XML fails well-formedness.
    r = measure_data.post("/qrda/validate", headers=CLINICIAN,
                          json={"xml": "<ClinicalDocument><oops>"})
    assert r.json()["valid"] is False
    checks = {c["check"]: c["status"] for c in r.json()["checks"]}
    assert checks["well-formed-xml"] == "fail"


def test_checks_endpoint_lists_implemented_and_not_run(measure_data):
    r = measure_data.get("/qrda/checks", headers=CLINICIAN)
    assert r.status_code == 200
    assert "well-formed-xml" in r.json()["implemented_checks"]
    assert "cat3-aggregate-counts-sane" in r.json()["implemented_checks"]
    assert "NOT RUN" in r.json()["not_run"]
