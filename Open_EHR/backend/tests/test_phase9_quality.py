"""Phase 9 — Part C (quality measures) + Part D (research workbench).

The measures are PUBLISHED CONFIG evaluated by the Phase 3 pophealth
evaluator over the OMOP warehouse — so these tests build a small,
fully synthetic warehouse population whose measure arithmetic is
known exactly:

- HbA1c control: 10 persons with type 2 diabetes; latest HbA1c < 8.0
  within 365 days for 6 (one at exactly 8.0 does NOT count; one
  controlled value is 19 months stale and does NOT count)
  -> 6/10 = 60.0.
- BP control: 6 persons with hypertension; latest systolic < 140 AND
  diastolic < 90 for 5 -> 5/6 = 83.3.
- Colorectal screening: 8 persons aged 45-75 at the warehouse
  reference date (2026-01-10), 5 with a colonoscopy -> 5/8 = 62.5.
  (Two extra persons with colonoscopies are OUTSIDE the age band and
  must not leak into either side; the diabetes/hypertension patients
  are deliberately born outside 45-75 too.)

Part D: cohort snapshots over time + the de-identified aggregate
export (capability-gated, audited, contains no patient-level data).
"""
import json

import pytest

from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER

SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
T2DM = "44054006"
HTN = "38341003"
ASTHMA = "195967001"
COLONOSCOPY = "73761001"


def _patient(client, pid, birth_date):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid, "birthDate": birth_date,
        "name": [{"family": "Synth", "given": [pid]}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _condition(client, pid, code):
    r = client.post("/fhir/Condition", json={
        "resourceType": "Condition",
        "code": {"coding": [{"system": SNOMED, "code": code}]},
        "subject": {"reference": f"Patient/{pid}"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _observation(client, pid, code, value, when, unit="%"):
    r = client.post("/fhir/Observation", json={
        "resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": LOINC, "code": code}]},
        "subject": {"reference": f"Patient/{pid}"},
        "effectiveDateTime": when,
        "valueQuantity": {"value": value, "unit": unit}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _procedure(client, pid, code, when="2025-06-01T00:00:00Z"):
    r = client.post("/fhir/Procedure", json={
        "resourceType": "Procedure", "status": "completed",
        "code": {"coding": [{"system": SNOMED, "code": code}]},
        "subject": {"reference": f"Patient/{pid}"},
        "performedDateTime": when}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


@pytest.fixture()
def cohort_data(client, monkeypatch, tmp_path):
    monkeypatch.setenv("WAREHOUSE_DATABASE_URL",
                       f"sqlite:///{tmp_path}/wh.db")
    # Diabetes: 10 persons, all born 1995 (age 31 at the reference
    # date — outside the colorectal band on purpose).
    a1c = [6.1, 6.5, 7.0, 7.2, 7.9, 6.8, 8.4, 9.1, 8.0]
    for i, value in enumerate(a1c, start=1):
        pid = f"dm-{i:02d}"
        _patient(client, pid, "1995-06-15")
        _condition(client, pid, T2DM)
        _observation(client, pid, "4548-4", value, "2026-01-10T09:00:00Z")
    _patient(client, "dm-10", "1995-06-15")  # stale control only
    _condition(client, "dm-10", T2DM)
    _observation(client, "dm-10", "4548-4", 6.0, "2024-06-01T09:00:00Z")
    # Hypertension: 6 persons, all born 1945 (age 81 — also outside).
    bps = [(118, 76), (122, 80), (128, 82), (132, 84), (126, 78), (152, 84)]
    for i, (sys_, dia) in enumerate(bps, start=1):
        pid = f"htn-{i:02d}"
        _patient(client, pid, "1945-03-01")
        _condition(client, pid, HTN)
        _observation(client, pid, "8480-6", sys_, "2026-01-09T09:00:00Z",
                     unit="mm[Hg]")
        _observation(client, pid, "8462-4", dia, "2026-01-09T09:00:00Z",
                     unit="mm[Hg]")
    # Colorectal: 8 in-band persons (5 screened — a numerator below
    # the suppression threshold would itself be suppressed), 2
    # out-of-band.
    crc = [("crc-01", "1960-05-01", True), ("crc-02", "1965-05-01", True),
           ("crc-03", "1970-05-01", False), ("crc-04", "1975-05-01", False),
           ("crc-05", "1980-05-01", False), ("crc-06", "1955-05-01", True),
           ("crc-07", "1958-05-01", True), ("crc-08", "1962-05-01", True),
           ("crc-x1", "1940-05-01", True), ("crc-x2", "1990-05-01", False)]
    for pid, dob, screened in crc:
        _patient(client, pid, dob)
        if screened:
            _procedure(client, pid, COLONOSCOPY)
    # Asthma: 3 persons — the small-cell suppression cohort.
    for i in range(1, 4):
        pid = f"asth-{i:02d}"
        _patient(client, pid, "1990-01-01")
        _condition(client, pid, ASTHMA)
    r = client.post("/warehouse/etl/run", headers=ADMIN)
    assert r.status_code == 200 and r.json()["dead_letter"] == 0, r.text
    return client


def _result(client, key, headers=CLINICIAN):
    r = client.get(f"/quality/measures/{key}/result", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Part C — measures: listing, exact math, suppression, governance
# ---------------------------------------------------------------------------

def test_seeded_measures_listed_and_role_gated(cohort_data):
    r = cohort_data.get("/quality/measures", headers=CLINICIAN)
    assert r.status_code == 200
    keys = {m["key"] for m in r.json()["measures"]}
    assert keys == {"hba1c-control", "bp-control", "colorectal-screening"}
    for m in r.json()["measures"]:
        assert m["definition_version"] >= 1
        assert m["small_cell_threshold"] == 5
    assert cohort_data.get("/quality/measures",
                           headers=PATIENT).status_code == 403
    assert cohort_data.get("/quality/measures",
                           headers=VIEWER).status_code == 403


def test_hba1c_control_exact_math(cohort_data):
    out = _result(cohort_data, "hba1c-control")
    assert out["denominator"] == 10
    assert out["numerator"] == 6
    assert out["rate"] == 60.0
    assert out["suppressed"] is False


def test_bp_control_exact_math(cohort_data):
    out = _result(cohort_data, "bp-control")
    assert out["denominator"] == 6
    assert out["numerator"] == 5
    assert out["rate"] == 83.3
    assert out["suppressed"] is False


def test_colorectal_screening_exact_math(cohort_data):
    out = _result(cohort_data, "colorectal-screening")
    assert out["denominator"] == 8
    assert out["numerator"] == 5
    assert out["rate"] == 62.5
    assert out["suppressed"] is False


def _publish(client, kind, key, payload, expect=201):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload,
                    headers=ADMIN)
    assert r.status_code == expect, r.text
    if expect != 201:
        return r
    version = r.json()["version"]
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    return r


def test_small_cell_suppression_from_config_measure(cohort_data):
    """A measure authored as Studio config (asthma: 3 persons) has
    both counts suppressed and NO rate — a rate would leak the
    suppressed numerator."""
    _publish(cohort_data, "measure", "asthma-review", {
        "name": "Asthma review (test measure)",
        "denominator": {"conditionCodes": [
            {"system": SNOMED, "code": ASTHMA}]},
        "numerator": {"conditionCodes": [
            {"system": SNOMED, "code": ASTHMA}]},
    })
    out = _result(cohort_data, "asthma-review")
    assert out["denominator"] == "<5"
    assert out["numerator"] == "<5"
    assert out["rate"] is None
    assert out["suppressed"] is True


def test_measure_config_validation(cohort_data):
    # Both sides need at least one criterion; lab rules need a code.
    _publish(cohort_data, "measure", "bad-empty", {
        "name": "Empty", "denominator": {}, "numerator": {}}, expect=422)
    _publish(cohort_data, "measure", "bad-lab", {
        "name": "Bad lab",
        "denominator": {"conditionCodes": [{"system": SNOMED, "code": T2DM}]},
        "numerator": {"lab": {"comparator": "<", "threshold": 8.0}}},
        expect=422)
    _publish(cohort_data, "measure", "bad-comparator", {
        "name": "Bad comparator",
        "denominator": {"conditionCodes": [{"system": SNOMED, "code": T2DM}]},
        "numerator": {"lab": {"code": "4548-4", "comparator": "~",
                             "threshold": 8.0}}}, expect=422)
    # Unknown measure key -> 404.
    assert cohort_data.get("/quality/measures/nope/result",
                           headers=CLINICIAN).status_code == 404


# ---------------------------------------------------------------------------
# Part D — snapshots over time + de-identified aggregate export
# ---------------------------------------------------------------------------

def test_cohort_snapshots_over_time(cohort_data):
    # Snapshot runs need pophealth.run (admin); reading needs view.
    assert cohort_data.post("/pophealth/registries/diabetes/snapshots",
                            headers=CLINICIAN).status_code == 403
    r = cohort_data.post("/pophealth/registries/diabetes/snapshots",
                         headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["member_count"] == 10
    assert r.json()["definition_version"] == 1

    # One more person with diabetes joins the warehouse population.
    _patient(cohort_data, "dm-11", "1995-06-15")
    _condition(cohort_data, "dm-11", T2DM)
    r = cohort_data.post("/warehouse/etl/run", headers=ADMIN)
    assert r.status_code == 200
    r = cohort_data.post("/pophealth/registries/diabetes/snapshots",
                         headers=ADMIN)
    assert r.json()["member_count"] == 11

    series = cohort_data.get("/pophealth/registries/diabetes/snapshots",
                             headers=CLINICIAN)
    assert series.status_code == 200
    snaps = series.json()["snapshots"]
    assert [s["member_count"] for s in snaps] == [10, 11]
    assert cohort_data.get("/pophealth/registries/nope/snapshots",
                           headers=CLINICIAN).status_code == 404


def test_cohort_export_aggregates_only_gated_and_audited(cohort_data):
    # Capability gate: clinicians (view/run) do NOT hold the export
    # capability; configurators and admins do (interop.export pattern).
    assert cohort_data.get("/pophealth/registries/diabetes/export",
                           headers=CLINICIAN).status_code == 403
    r = cohort_data.get("/pophealth/registries/diabetes/export",
                        headers=CONFIG)
    assert r.status_code == 200, r.text
    export = r.json()
    assert export["registry"] == "diabetes"
    assert export["summary"]["member_count"] == 10
    assert "No patient-level rows" in export["notice"]
    # NO row-level patient data anywhere in the payload.
    blob = json.dumps(export)
    for pid in ("dm-01", "dm-10", "htn-01", "crc-01", "asth-01"):
        assert pid not in blob
    assert "Patient/" not in blob and "person_id" not in blob
    # The export was audited under the acting account.
    audit = cohort_data.get("/studio/audit", headers=ADMIN).json()
    entries = audit if isinstance(audit, list) else (
        audit.get("items") or audit.get("entries") or [])
    assert any(e.get("action") == "pophealth.export"
               and e.get("actor") == "dev-configurator" for e in entries)
