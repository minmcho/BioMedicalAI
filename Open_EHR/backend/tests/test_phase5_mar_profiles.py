"""Phase 5 — MAR (computed from signed orders + BCMA administrations)
and deployment-profile packs (apply-on-enable in app/studio.py, POCT
single-step lab path in app/lab.py).

The MAR stores nothing: due / given / missed are computed at read from
the order's timing (order config defaults when the order carries no
timing) and the MedicationAdministration records written by the
EXISTING pharmacy BCMA path.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

import app.orders as orders_module
from app.models import AuditLog
from tests.conftest import ADMIN, CLINICIAN

LISINOPRIL = "1049221"


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid, "birthDate": "1980-01-01",
        "name": [{"family": "Synth", "given": ["Mar"]}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _signed_med_order(client, pid, code=LISINOPRIL, timing=None):
    body = {"type": "medication", "code": code, "patient": pid}
    if timing:
        # Wave A (CHART-B3): a structured sig must be complete
        # against the published medication-sig vocabulary.
        body["dosageInstruction"] = [{
            "text": "1 tablet by mouth as scheduled",
            "doseAndRate": [{"doseQuantity": {"value": 1,
                                               "unit": "tablets"}}],
            "route": {"coding": [{"code": "oral", "display": "Oral"}]},
            "timing": {"repeat": timing}}]
    r = client.post("/orders", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    oid = r.json()["order"]["id"]
    for to in ("signed", "active"):
        r = client.post(f"/orders/{oid}/transition",
                        json={"to_status": to}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    return oid


def _enable_pack(client, name, parameters=None):
    inst = client.post(f"/studio/packs/{name}/install",
                       headers=ADMIN).json()
    iid = inst["id"]
    assert client.post(f"/studio/installations/{iid}/approve",
                       headers=ADMIN).status_code == 200
    assert client.post(f"/studio/installations/{iid}/attach",
                       headers=ADMIN).status_code == 200
    return client.post(f"/studio/installations/{iid}/enable",
                       json={"parameters": parameters or {}},
                       headers=ADMIN), iid


# ---------------------------------------------------------------------------
# MAR
# ---------------------------------------------------------------------------

def test_mar_due_list_from_signed_order_timing(client):
    pid = _patient(client, "mar-1")
    _signed_med_order(client, pid, timing={
        "frequency": 1, "period": 24, "periodUnit": "h"})
    now = datetime.now(timezone.utc)
    r = client.get("/orders/mar/due", params={
        "patient": pid,
        "from": (now - timedelta(hours=1)).isoformat(),
        "to": (now + timedelta(days=2)).isoformat()},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    doses = r.json()["doses"]
    assert len(doses) == 3  # anchor, +24h, +48h
    assert doses[0]["medication_code"] == LISINOPRIL
    assert doses[0]["status"] == "due"  # anchor is 'now', inside its window
    assert doses[1]["status"] == "scheduled"
    assert r.json()["summary"] == {"given": 0, "due": 1, "missed": 0,
                                   "scheduled": 2}


def test_mar_ignores_draft_orders(client):
    pid = _patient(client, "mar-2")
    r = client.post("/orders", json={
        "type": "medication", "code": LISINOPRIL, "patient": pid},
        headers=CLINICIAN)
    assert r.status_code == 201
    mar = client.get(f"/orders/mar/due?patient={pid}",
                     headers=CLINICIAN).json()
    assert mar["doses"] == []


def test_mar_given_and_missed_computed_from_bcma_records(client, monkeypatch):
    pid = _patient(client, "mar-3")
    oid = _signed_med_order(client, pid, timing={
        "frequency": 1, "period": 24, "periodUnit": "h"})
    # Dispense + administer through the EXISTING pharmacy/BCMA path.
    # Wave A: dispensing requires a current verification (PHARM-B1),
    # and administration is the five-rights BCMA scan only — the
    # BCMA-lite typed-ID path was retired (PHARM-M2). The fixture
    # order's sig is complete (dose 1 tablet, oral, scheduled), so
    # the scan passes all five rights with no override.
    r = client.post(f"/pharmacy/verification/{oid}/verify", json={},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/pharmacy/dispense",
                    json={"medication_request_id": oid}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    dispense_id = r.json()["dispense"]["id"]
    wb = client.post("/pharmacy/barcodes/wristband",
                     json={"patient_id": pid}, headers=CLINICIAN)
    assert wb.status_code == 200, wb.text
    ml = client.post("/pharmacy/barcodes/med-label",
                     json={"dispense_id": dispense_id}, headers=CLINICIAN)
    assert ml.status_code == 200, ml.text
    r = client.post("/pharmacy/bcma/administer", json={
        "wristband_payload": wb.json()["payload"],
        "med_label_payload": ml.json()["payload"]},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text

    mar = client.get(f"/orders/mar/due?patient={pid}",
                     headers=CLINICIAN).json()
    assert mar["doses"][0]["status"] == "given"
    assert mar["doses"][0]["administration_id"]

    # Three days later (clock moved in the MAR module only): the given
    # dose stays given, the two un-administered doses in between are
    # missed, and today's dose is due. Missed is computed, not stored.
    real_now = orders_module._now()
    monkeypatch.setattr(orders_module, "_now",
                        lambda: real_now + timedelta(days=3))
    mar = client.get("/orders/mar/due", params={
        "patient": pid,
        "from": (real_now - timedelta(days=1)).isoformat(),
        "to": (real_now + timedelta(days=4)).isoformat()},
        headers=CLINICIAN).json()
    by_index = mar["doses"]
    assert by_index[0]["status"] == "given"
    assert by_index[1]["status"] == "missed"
    assert by_index[2]["status"] == "missed"
    assert by_index[3]["status"] == "due"
    assert mar["summary"]["missed"] == 2


def test_mar_unknown_patient_is_404(client):
    r = client.get("/orders/mar/due?patient=ghost", headers=CLINICIAN)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Profile packs: apply on enable
# ---------------------------------------------------------------------------

def test_solo_profile_enable_applies_configuration(client):
    r, iid = _enable_pack(client, "profile-solo-practice",
                          {"orgName": "Dr. Synthetic's Office"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "enabled"
    applied = {(a["kind"], a["key"]) for a in r.json()["applied_configuration"]}
    assert applied == {
        ("navigation", "main"), ("permissions", "role-matrix"),
        ("workflow", "lab-order-flow-poct"), ("branding", "default"),
        ("cohort", "diabetes-recall"), ("cohort", "annual-physical-recall"),
        ("cohort", "flu-recall")}

    nav = client.get("/studio/config/navigation/effective/main",
                     headers=ADMIN).json()["payload"]
    modules = {i["module"] for i in nav["items"]}
    assert modules == {"patients", "inbox", "scheduling", "lab", "billing",
                       "pharmacy", "pophealth", "studio", "portal"}
    assert not ({"ed", "radiology", "surgery"} & modules)

    branding = client.get("/studio/config/branding/effective/default",
                          headers=ADMIN).json()["payload"]
    assert branding["orgName"] == "Dr. Synthetic's Office"

    perms = client.get("/studio/config/permissions/effective/role-matrix",
                       headers=ADMIN).json()["payload"]
    assert perms["capabilities"]["portal.self-schedule"]["patient"] is True

    poct = client.get("/studio/config/workflow/effective/lab-order-flow-poct",
                      headers=ADMIN).json()["payload"]
    assert "verified" not in poct["states"]
    assert "released" in poct["governedStates"]

    registries = client.get("/pophealth/registries",
                            headers=CLINICIAN).json()
    keys = {reg["key"] for reg in registries}
    assert {"diabetes-recall", "annual-physical-recall",
            "flu-recall"} <= keys

    # Every applied publish is on the audit chain, attributed to the pack.
    factory = client.app.state.session_factory
    with factory() as session:
        rows = session.scalars(
            select(AuditLog).where(
                AuditLog.action == "studio.config.publish")).all()
    profile_rows = [row for row in rows
                    if (row.payload or {}).get("via_pack")
                    == "profile-solo-practice"]
    assert len(profile_rows) == 7
    assert all(row.payload.get("profile_apply") is True
               for row in profile_rows)


def test_profile_enable_supersedes_existing_published_config(client):
    before = client.get("/studio/config/navigation/main",
                        headers=ADMIN).json()
    published_before = [v for v in before if v["status"] == "published"]
    assert len(published_before) == 1
    _enable_pack(client, "profile-clinic")
    after = client.get("/studio/config/navigation/main",
                       headers=ADMIN).json()
    published_after = [v for v in after if v["status"] == "published"]
    assert len(published_after) == 1
    assert published_after[0]["version"] == \
        published_before[0]["version"] + 1
    assert any(v["status"] == "superseded" for v in after)


def test_clinic_profile_differs_where_the_profile_says_so(client):
    _enable_pack(client, "profile-clinic")
    nav = client.get("/studio/config/navigation/effective/main",
                     headers=ADMIN).json()["payload"]
    modules = {i["module"] for i in nav["items"]}
    assert "radiology" in modules and "ed" not in modules
    perms = client.get("/studio/config/permissions/effective/role-matrix",
                       headers=ADMIN).json()["payload"]
    assert perms["capabilities"]["portal.self-schedule"]["patient"] is False
    # No orgName parameter supplied: falls back to the current org name
    # (the seeded default), never an unresolved placeholder.
    branding = client.get("/studio/config/branding/effective/default",
                          headers=ADMIN).json()["payload"]
    assert branding["orgName"] == "OpenCare"


def test_profile_enable_blocked_by_open_draft_is_atomic(client):
    # A configurator has a draft in flight on a key the profile wants
    # to publish: the whole enable is refused and NOTHING is applied.
    r = client.post("/studio/config/cohort/diabetes-recall",
                    json={"name": "Work in progress",
                          "conditionCodes": [
                              {"system": "http://snomed.info/sct",
                               "code": "44054006"}]},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    inst = client.post("/studio/packs/profile-solo-practice/install",
                       headers=ADMIN).json()
    iid = inst["id"]
    client.post(f"/studio/installations/{iid}/approve", headers=ADMIN)
    client.post(f"/studio/installations/{iid}/attach", headers=ADMIN)
    r = client.post(f"/studio/installations/{iid}/enable",
                    json={"parameters": {}}, headers=ADMIN)
    assert r.status_code == 409, r.text
    installs = client.get("/studio/installations", headers=ADMIN).json()
    mine = [i for i in installs if i["id"] == iid][0]
    assert mine["status"] == "attached"
    nav = client.get("/studio/config/navigation/effective/main",
                     headers=ADMIN).json()["payload"]
    assert any(i["module"] == "ed" for i in nav["items"])  # untouched default


def test_disabling_profile_does_not_unpublish_configuration(client):
    _, iid = _enable_pack(client, "profile-solo-practice",
                          {"orgName": "Solo Org"})
    r = client.post(f"/studio/installations/{iid}/disable", headers=ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    nav = client.get("/studio/config/navigation/effective/main",
                     headers=ADMIN).json()["payload"]
    assert not any(i["module"] == "ed" for i in nav["items"])
    branding = client.get("/studio/config/branding/effective/default",
                          headers=ADMIN).json()["payload"]
    assert branding["orgName"] == "Solo Org"


def test_plain_pack_enable_still_just_enables(client):
    # Phase 17 adjustment: clinical-core is now seeded ENABLED at
    # startup (pack-gate default-ON rollout), so it can no longer be
    # enabled from a draft here. A plain authored pack (no
    # `configuration` list — exactly clinical-core's shape) carries
    # the same assertion: enabling just enables, applying nothing.
    r = client.post("/studio/packs", headers=ADMIN, json={
        "name": "plain-probe", "version": "1.0.0",
        "manifest": {"name": "plain-probe", "version": "1.0.0",
                     "description": "Plain pack, no configuration.",
                     "api_endpoints": [], "modules": [], "forms": []}})
    assert r.status_code == 201, r.text
    r, _ = _enable_pack(client, "plain-probe")
    assert r.status_code == 200 and r.json()["status"] == "enabled"
    assert r.json()["applied_configuration"] == []


# ---------------------------------------------------------------------------
# POCT lab path (profile workflow) vs hospital path (default)
# ---------------------------------------------------------------------------

def _poct_order(client, pid, poct=True):
    body = {"code": "4548-4", "subject": {"reference": f"Patient/{pid}"}}
    if poct:
        body["poct"] = True
    r = client.post("/lab/orders", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    oid = r.json()["id"]
    for to in ("collected", "received"):
        r = client.post(f"/lab/orders/{oid}/transition",
                        json={"to_status": to}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    return oid


def _enter_results(client, oid, value=7.4):
    return client.post(f"/lab/orders/{oid}/results",
                       json={"results": [{"code": "4548-4", "value": value}]},
                       headers=CLINICIAN)


def test_poct_order_without_profile_uses_standard_two_step(client):
    pid = _patient(client, "poct-plain")
    oid = _poct_order(client, pid)
    r = _enter_results(client, oid)
    assert r.status_code == 201, r.text
    assert r.json()["report"]["status"] == "preliminary"
    detail = client.get(f"/lab/orders/{oid}", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "resulted"  # NOT released
    inbox = client.get(f"/fhir/inbox?patient={pid}",
                       headers=CLINICIAN).json()
    assert inbox["total"] == 0  # unreleased results stay hidden


def test_poct_order_with_profile_releases_in_one_step(client):
    _enable_pack(client, "profile-solo-practice")
    pid = _patient(client, "poct-pro")
    oid = _poct_order(client, pid)
    r = _enter_results(client, oid)
    assert r.status_code == 201, r.text
    assert r.json()["report"]["status"] == "final"
    assert all(o["status"] == "final" for o in r.json()["observations"])
    detail = client.get(f"/lab/orders/{oid}", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "released"
    inbox = client.get(f"/fhir/inbox?patient={pid}",
                       headers=CLINICIAN).json()
    # The results bundle carries the report + its observation.
    assert inbox["total"] >= 1
    assert any(e["resource"]["resourceType"] == "DiagnosticReport"
               for e in inbox["entry"])


def test_poct_single_step_refused_whole_when_qc_blocks(client):
    """In POCT mode the QC gate moves BEFORE the write: a failed QC
    run for the analyte refuses the entry whole (409) — no draft
    results, no status change are left behind."""
    _enable_pack(client, "profile-solo-practice")
    pid = _patient(client, "poct-qc")
    # Failed QC for HbA1c (measured outside the expected range).
    r = client.post("/lab/qc", json={
        "analyte_code": "4548-4", "control_material": "SYN-QC-LOT-P5",
        "expected_low": 4.0, "expected_high": 6.0, "measured_value": 9.9},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    oid = _poct_order(client, pid)
    r = _enter_results(client, oid)
    assert r.status_code == 409, r.text
    detail = client.get(f"/lab/orders/{oid}", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "received"  # unchanged
    inbox = client.get(f"/fhir/inbox?patient={pid}",
                       headers=CLINICIAN).json()
    assert inbox["total"] == 0


def test_non_poct_order_unaffected_by_poct_workflow(client):
    _enable_pack(client, "profile-solo-practice")
    pid = _patient(client, "poct-hosp")
    oid = _poct_order(client, pid, poct=False)
    r = _enter_results(client, oid)
    assert r.status_code == 201, r.text
    assert r.json()["report"]["status"] == "preliminary"
    detail = client.get(f"/lab/orders/{oid}", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "resulted"
    # The governed hospital path still applies: verify then release.
    report_id = r.json()["report"]["id"]
    r = client.post(f"/lab/reports/{report_id}/verify", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/lab/reports/{report_id}/release", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    detail = client.get(f"/lab/orders/{oid}", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "released"
