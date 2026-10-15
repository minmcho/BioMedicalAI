import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.audit import AuditLog
from app.deidentify import assert_clean, scan_record, scan_text
from app.eval_gate import run_evals, score_case
from app.ingest_fhir import to_omop
from app.rbac import can, require
from app.tenant_store import TenantStore, TenantViolation

# ---- tenant isolation ----
def test_tenant_cannot_read_other_tenants_record():
    store = TenantStore()
    store.put("lab-a", "r1", {"value": 42})
    assert store.get("lab-a", "r1")["value"] == 42
    with pytest.raises(TenantViolation):
        store.get("lab-b", "r1")

def test_tenant_listing_is_scoped():
    store = TenantStore()
    store.put("lab-a", "r1", {})
    store.put("lab-a", "r2", {})
    store.put("lab-b", "r9", {})
    assert store.list_ids("lab-a") == ["r1", "r2"]
    assert store.list_ids("lab-b") == ["r9"]

def test_delete_requires_visibility():
    store = TenantStore()
    store.put("lab-a", "r1", {})
    with pytest.raises(TenantViolation):
        store.delete("lab-b", "r1")
    store.delete("lab-a", "r1")
    assert store.list_ids("lab-a") == []

# ---- RBAC ----
def test_rbac_deny_by_default():
    assert can("viewer", "read")
    assert not can("viewer", "ingest")
    assert not can("nobody", "read")
    assert can("admin", "view_audit")
    with pytest.raises(PermissionError):
        require("analyst", "manage_users")

# ---- audit chain ----
def test_audit_chain_verifies_and_detects_tampering():
    log = AuditLog()
    log.append("min", "ingest", "bundle-1")
    log.append("min", "query_ai", "case-7")
    assert log.verify_chain()
    log._entries[0]["detail"] = "forged"
    assert not log.verify_chain()

# ---- PHI guardrail ----
def test_deidentify_blocks_identifiers():
    assert "ssn" in scan_text("patient ssn 123-45-6789")
    assert scan_record({"note": "call 415-555-0132"}) == ["phone"]
    assert scan_record({"city": "Berkeley"}) == []
    with pytest.raises(ValueError):
        assert_clean({"email": "a@b.org"})

# ---- FHIR -> OMOP ----
BUNDLE = {"entry": [
    {"resource": {"resourceType": "Patient", "id": "p1", "gender": "female", "birthDate": "1980-05-01"}},
    {"resource": {"resourceType": "Encounter", "id": "e1", "subject": {"reference": "Patient/p1"}, "period": {"start": "2026-01-10"}}},
    {"resource": {"resourceType": "Condition", "id": "c1", "subject": {"reference": "Patient/p1"}, "code": {"coding": [{"display": "Diabetes mellitus"}]}}},
    {"resource": {"resourceType": "Observation", "id": "o1", "subject": {"reference": "Patient/p1"}, "code": {"coding": [{"display": "HbA1c"}]}, "valueQuantity": {"value": 6.1, "unit": "%"}}},
]}

def test_fhir_to_omop_mapping():
    rows = to_omop(BUNDLE)
    assert rows["person"] == [{"person_id": "p1", "gender": "female", "year_of_birth": 1980}]
    assert rows["visit_occurrence"][0]["person_id"] == "p1"
    assert rows["condition_occurrence"][0]["condition_name"] == "Diabetes mellitus"
    assert rows["measurement"][0]["value"] == 6.1

# ---- eval gate ----
def test_eval_gate_pass_and_fail():
    cases = [{"id": "1", "prompt": "unit of HbA1c?", "expected": "percent"}]
    good = run_evals(cases, lambda p: "Percent")
    assert good["passed"] and good["mean_score"] == 1.0
    bad = run_evals(cases, lambda p: "banana")
    assert not bad["passed"]
    assert score_case("HbA1c", "the HbA1c result") == 0.7
