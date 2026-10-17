"""Realignment — configuration consumption (2026-10-16).

Proves the flips the realignment promised: the web shell's data source
(seeded navigation + branding), the permissions matrix governing every
surface (including Studio's own APIs and the previously hardcoded read
gates), per-transition roles in all five state machines, the extracted
thresholds (governed/active state sets, exportable states, maxAppeals,
ESI range, BCMA rights, QC policy), and the critical-value
notification lifecycle (pending -> notified -> acknowledged).

Defaults must reproduce the pre-realignment behaviour exactly; each
test that changes behaviour does so by PUBLISHING configuration, never
by code paths the old system did not have.

Synthetic data only.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import LabQcRun
from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER

BMP = "51990-0"
POTASSIUM = "2823-3"
LISINOPRIL = "1049221"

BMP_RESULTS = [
    {"code": "2345-7", "value": 95},
    {"code": "3094-0", "value": 12},
    {"code": "2160-0", "value": 1.0},
    {"code": "2951-2", "value": 130},
    {"code": POTASSIUM, "value": 6.4},   # critical high (HH)
]
CBC_RESULTS = [
    {"code": "718-7", "value": 13.5},
    {"code": "4544-3", "value": 41.0},
    {"code": "6690-2", "value": 7.2},
    {"code": "777-3", "value": 260},
]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _publish_config(client, kind, key, payload, headers=ADMIN):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=headers)
    assert r.status_code == 200, r.text
    return version


def _effective(client, kind, key, headers=ADMIN):
    r = client.get(f"/studio/config/{kind}/effective/{key}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["payload"]


def _override_permissions(client, mutate):
    # The role-matrix config is not seeded — the built-in defaults serve
    # until a site publishes — so the override starts from the defaults
    # matrix itself (the same matrix the web Permissions tab starts
    # from) and publishes it with the mutation applied.
    from app.configstore import CAPABILITY_DEFAULTS, ROLE_NAMES
    capabilities = {
        cap: {role: role in roles for role in ROLE_NAMES}
        for cap, roles in CAPABILITY_DEFAULTS.items()}
    mutate(capabilities)
    _publish_config(client, "permissions", "role-matrix",
                    {"capabilities": capabilities})


def _workflow_with_roles(client, key, from_state, to_state, roles):
    payload = _effective(client, "workflow", key)
    for t in payload["transitions"]:
        if t["from"] == from_state and t["to"] == to_state:
            t["roles"] = roles
    _publish_config(client, "workflow", key, payload)


def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic", "given": ["Re"]}],
                          "gender": "female", "birthDate": "1980-05-01"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _lab_order(client, code=BMP, patient="pat-1"):
    r = client.post("/lab/orders",
                    json={"code": code,
                          "subject": {"reference": f"Patient/{patient}"}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _lab_transition(client, order_id, to_status, headers=CLINICIAN, expect=200):
    r = client.post(f"/lab/orders/{order_id}/transition",
                    json={"to_status": to_status}, headers=headers)
    assert r.status_code == expect, r.text
    return r


def _resulted(client, code=BMP, results=None):
    """An order with results entered (draft report)."""
    order = _lab_order(client, code=code)
    _lab_transition(client, order["id"], "collected")
    _lab_transition(client, order["id"], "received")
    r = client.post(f"/lab/orders/{order['id']}/results",
                    json={"results": results or BMP_RESULTS}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return order, r.json()


# ---------------------------------------------------------------------------
# A. Seeded navigation + branding (the web shell's data source)
# ---------------------------------------------------------------------------

def test_navigation_and_branding_seeded_as_published_config(client):
    nav = client.get("/studio/config/navigation/effective/main", headers=CLINICIAN)
    assert nav.status_code == 200
    assert nav.json()["version"] == 1 and nav.json()["status"] == "published"
    items = nav.json()["payload"]["items"]
    assert [i["module"] for i in items] == [
        "patients", "inbox", "scheduling", "lab", "pophealth", "billing",
        "pharmacy", "ed", "radiology", "surgery", "studio", "portal"]
    by_module = {i["module"]: i for i in items}
    assert by_module["studio"]["roles"] == ["admin", "configurator"]
    assert by_module["portal"]["roles"] == ["patient"]
    assert "patient" in by_module["billing"]["roles"]
    assert by_module["lab"]["label"] == "Laboratory"

    branding = client.get("/studio/config/branding/effective/default",
                          headers=VIEWER)
    assert branding.status_code == 200
    payload = branding.json()["payload"]
    assert payload["orgName"] == "OpenCare"
    assert payload["portalTitle"] == "My results"
    assert payload["primaryColor"] == "#0b6e4f"
    # Seeding is idempotent: exactly one version exists.
    versions = client.get("/studio/config/branding/default", headers=ADMIN).json()
    assert len(versions) == 1


def test_published_nav_without_a_module_is_the_effective_nav(client):
    payload = _effective(client, "navigation", "main")
    payload["items"] = [i for i in payload["items"] if i["module"] != "lab"]
    _publish_config(client, "navigation", "main", payload)
    eff = _effective(client, "navigation", "main")
    assert "lab" not in [i["module"] for i in eff["items"]]
    assert len(eff["items"]) == 11


# ---------------------------------------------------------------------------
# B. Permissions completion — the matrix governs every surface
# ---------------------------------------------------------------------------

def test_lab_read_revocation_denies_the_lab_reads(client):
    _patient(client)
    _lab_order(client)
    assert client.get("/lab/orders", headers=VIEWER).status_code == 200
    _override_permissions(client, lambda caps: caps["lab.read"].update(
        {"viewer": False}))
    assert client.get("/lab/orders", headers=VIEWER).status_code == 403
    assert client.get("/lab/verification-queue", headers=VIEWER).status_code == 403
    # Other roles are untouched by the override.
    assert client.get("/lab/orders", headers=CLINICIAN).status_code == 200


def test_pophealth_grants_via_published_matrix(client):
    # Defaults: configurator cannot view, clinician cannot run the ETL.
    assert client.get("/pophealth/registries", headers=CONFIG).status_code == 403
    assert client.post("/warehouse/etl/run", headers=CLINICIAN).status_code == 403
    _override_permissions(client, lambda caps: (
        caps["pophealth.view"].update({"configurator": True}),
        caps["pophealth.run"].update({"clinician": True})))
    assert client.get("/pophealth/registries", headers=CONFIG).status_code == 200
    assert client.post("/warehouse/etl/run", headers=CLINICIAN).status_code == 200


def test_studio_manage_revocation_covers_config_and_pack_writes(client):
    _override_permissions(client, lambda caps: caps["studio.manage"].update(
        {"configurator": False}))
    r = client.post("/studio/config/branding/other", headers=CONFIG,
                    json={"orgName": "Nope", "portalTitle": "Nope"})
    assert r.status_code == 403
    assert client.post("/studio/packs/lab/install",
                       headers=CONFIG).status_code == 403
    # Admin keeps full control; reads stay open to the configurator.
    assert client.get("/studio/config/branding/effective",
                      headers=CONFIG).status_code == 200
    assert client.post("/studio/packs/lab/install",
                       headers=ADMIN).status_code == 201


# ---------------------------------------------------------------------------
# C. Per-transition roles in all five state machines
# ---------------------------------------------------------------------------

def test_transition_roles_appointment_adt(client):
    _patient(client)
    r = client.post("/fhir/Appointment", json={
        "resourceType": "Appointment", "id": "appt-1", "status": "booked",
        "start": "2026-10-20T09:00:00Z",
        "participant": [{"actor": {"reference": "Patient/pat-1"}}]},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    _workflow_with_roles(client, "appointment-adt", "booked", "arrived",
                          ["admin"])
    r = client.post("/fhir/Appointment/appt-1/transition",
                    json={"to_status": "arrived"}, headers=CLINICIAN)
    assert r.status_code == 403 and "booked" in r.json()["detail"]
    r = client.post("/fhir/Appointment/appt-1/transition",
                    json={"to_status": "arrived"}, headers=ADMIN)
    assert r.status_code == 200, r.text


def test_transition_roles_lab(client):
    _patient(client)
    order = _lab_order(client)
    _lab_transition(client, order["id"], "collected")
    _workflow_with_roles(client, "lab-order-flow", "collected", "received",
                          ["admin"])
    _lab_transition(client, order["id"], "received", headers=CLINICIAN,
                    expect=403)
    _lab_transition(client, order["id"], "received", headers=ADMIN)


def test_transition_roles_claim(client):
    _patient(client)
    r = client.post("/fhir/Encounter", json={
        "resourceType": "Encounter", "id": "enc-1", "status": "finished",
        "subject": {"reference": "Patient/pat-1"},
        "period": {"start": "2026-01-10T09:00:00Z",
                   "end": "2026-01-10T10:00:00Z"}}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/fhir/Procedure", json={
        "resourceType": "Procedure", "id": "proc-1", "status": "completed",
        "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt",
                             "code": "93005", "display": "ECG"}]},
        "subject": {"reference": "Patient/pat-1"},
        "encounter": {"reference": "Encounter/enc-1"}}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/billing/claims/generate",
                    json={"encounter_id": "enc-1"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    claim_id = r.json()["id"]
    _workflow_with_roles(client, "claim-lifecycle", "ready", "submitted",
                          ["admin"])
    url = f"/billing/claims/{claim_id}/transition"
    # Claim transitions are a billing.manage surface (configurator
    # included); the workflow role restriction is what bites here.
    assert client.post(url, json={"to_status": "ready"},
                       headers=CONFIG).status_code == 200
    r = client.post(url, json={"to_status": "submitted"}, headers=CONFIG)
    assert r.status_code == 403
    assert client.post(url, json={"to_status": "submitted"},
                       headers=ADMIN).status_code == 200


def test_transition_roles_ed(client):
    _patient(client)
    r = client.post("/ed/arrivals", json={"patient": "pat-1",
                                          "chief_complaint": "Chest pain"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    encounter_id = r.json()["id"]
    _workflow_with_roles(client, "ed-flow", "triage", "in-treatment",
                          ["admin"])
    url = f"/ed/encounters/{encounter_id}/transition"
    assert client.post(url, json={"to_status": "triage", "acuity": 3},
                       headers=CLINICIAN).status_code == 200
    assert client.post(url, json={"to_status": "in-treatment"},
                       headers=CLINICIAN).status_code == 403
    assert client.post(url, json={"to_status": "in-treatment"},
                       headers=ADMIN).status_code == 200


def test_transition_roles_surgery(client):
    _patient(client)
    r = client.post("/surgery/cases", json={
        "patient_id": "pat-1", "code": "27447",
        "display": "Total knee arthroplasty", "room": "OR-1",
        "start": "2026-10-20T08:00:00Z", "end": "2026-10-20T10:00:00Z"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    case_id = r.json()["case"]["id"]
    _workflow_with_roles(client, "surgery-flow", "scheduled", "pre-op",
                          ["admin"])
    url = f"/surgery/cases/{case_id}/transition"
    assert client.post(url, json={"to_status": "pre-op"},
                       headers=CLINICIAN).status_code == 403
    assert client.post(url, json={"to_status": "pre-op"},
                       headers=ADMIN).status_code == 200


# ---------------------------------------------------------------------------
# C. Extracted thresholds
# ---------------------------------------------------------------------------

def test_lab_governed_states_come_from_config(client):
    _patient(client)
    order = _lab_order(client)
    _lab_transition(client, order["id"], "collected")
    _lab_transition(client, order["id"], "received")
    _lab_transition(client, order["id"], "in-process")
    # Default governance: "resulted" is a governed state — the generic
    # transition endpoint refuses it (results enter via /results).
    r = client.post(f"/lab/orders/{order['id']}/transition",
                    json={"to_status": "resulted"}, headers=CLINICIAN)
    assert r.status_code == 422 and "governed" in r.json()["detail"]
    # Publish a workflow whose governedStates no longer lists resulted:
    # the bare transition into resulted is now allowed.
    payload = _effective(client, "workflow", "lab-order-flow")
    payload["governedStates"] = ["verified", "released"]
    _publish_config(client, "workflow", "lab-order-flow", payload)
    r = client.post(f"/lab/orders/{order['id']}/transition",
                    json={"to_status": "resulted"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "resulted"


def test_ed_active_states_come_from_config(client):
    _patient(client)
    client.post("/ed/arrivals", json={"patient": "pat-1"}, headers=CLINICIAN)
    board = client.get("/ed/board", headers=CLINICIAN).json()
    assert set(board["groups"].keys()) == {
        "waiting", "triage", "in-treatment", "disposition"}
    payload = _effective(client, "workflow", "ed-flow")
    payload["activeStates"] = ["waiting", "triage"]
    _publish_config(client, "workflow", "ed-flow", payload)
    board = client.get("/ed/board", headers=CLINICIAN).json()
    assert set(board["groups"].keys()) == {"waiting", "triage"}


def test_exportable_states_and_max_appeals_come_from_config(client):
    _patient(client)
    client.post("/fhir/Encounter", json={
        "resourceType": "Encounter", "id": "enc-1", "status": "finished",
        "subject": {"reference": "Patient/pat-1"},
        "period": {"start": "2026-01-10T09:00:00Z",
                   "end": "2026-01-10T10:00:00Z"}}, headers=ADMIN)
    client.post("/fhir/Procedure", json={
        "resourceType": "Procedure", "id": "proc-1", "status": "completed",
        "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt",
                             "code": "93005", "display": "ECG"}]},
        "subject": {"reference": "Patient/pat-1"},
        "encounter": {"reference": "Encounter/enc-1"}}, headers=ADMIN)
    r = client.post("/billing/claims/generate",
                    json={"encounter_id": "enc-1"}, headers=ADMIN)
    claim_id = r.json()["id"]
    export_url = f"/billing/claims/{claim_id}/export/837"
    # Default: a draft claim is not exportable.
    assert client.get(export_url, headers=ADMIN).status_code == 409
    payload = _effective(client, "workflow", "claim-lifecycle")
    payload["exportableStates"] = ["draft", "submitted", "denied", "paid"]
    payload["maxAppeals"] = 2
    _publish_config(client, "workflow", "claim-lifecycle", payload)
    assert client.get(export_url, headers=ADMIN).status_code == 200

    # Appeal cycle with maxAppeals=2: two appeals pass, the third 409s.
    # (Lifecycle: denied -> appealed -> submitted -> denied -> ...)
    url = f"/billing/claims/{claim_id}/transition"
    for target in ("ready", "submitted", "denied"):
        assert client.post(url, json={"to_status": target},
                           headers=ADMIN).status_code == 200
    for _ in range(2):
        assert client.post(url, json={"to_status": "appealed",
                                      "note": "documentation attached"},
                           headers=ADMIN).status_code == 200
        assert client.post(url, json={"to_status": "submitted"},
                           headers=ADMIN).status_code == 200
        assert client.post(url, json={"to_status": "denied"},
                           headers=ADMIN).status_code == 200
    r = client.post(url, json={"to_status": "appealed",
                               "note": "third attempt"}, headers=ADMIN)
    assert r.status_code == 409 and "already been appealed" in r.json()["detail"]


def test_esi_range_comes_from_terminology_config(client):
    _patient(client)
    r = client.post("/ed/arrivals", json={"patient": "pat-1"}, headers=CLINICIAN)
    encounter_id = r.json()["id"]
    payload = _effective(client, "terminology", "ed-triage")
    payload["max"] = 3
    _publish_config(client, "terminology", "ed-triage", payload)
    url = f"/ed/encounters/{encounter_id}/transition"
    r = client.post(url, json={"to_status": "triage", "acuity": 4},
                    headers=CLINICIAN)
    assert r.status_code == 422 and "1-3" in r.json()["detail"]
    assert client.post(url, json={"to_status": "triage", "acuity": 3},
                       headers=CLINICIAN).status_code == 200


def test_bcma_rights_subset_comes_from_formulary_config(client):
    _patient(client)
    r = client.post("/fhir/MedicationRequest", json={
        "resourceType": "MedicationRequest", "id": "med-1",
        "status": "active", "intent": "order",
        "medicationCodeableConcept": {"coding": [{
            "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
            "code": LISINOPRIL, "display": "Lisinopril 10 MG Oral Tablet"}]},
        "subject": {"reference": "Patient/pat-1"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/pharmacy/dispense",
                    json={"medication_request_id": "med-1"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    dispense_id = r.json()["dispense"]["id"]

    payload = _effective(client, "terminology", "formulary")
    payload["bcmaRights"] = ["right-patient"]
    _publish_config(client, "terminology", "formulary", payload)
    # Wrong wristband still fails (right-patient enforced)...
    r = client.post("/pharmacy/administer", json={
        "wristband_id": "pat-999", "medication_code": LISINOPRIL,
        "dispense_id": dispense_id}, headers=CLINICIAN)
    assert r.status_code == 422 and "Right patient" in r.json()["detail"]
    # ...but the medication code is no longer checked.
    r = client.post("/pharmacy/administer", json={
        "wristband_id": "pat-1", "medication_code": "000000",
        "dispense_id": dispense_id}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _catalog_with_qc(client, qc):
    payload = _effective(client, "terminology", "lab-catalog")
    payload["qc"] = qc
    _publish_config(client, "terminology", "lab-catalog", payload)


def _qc_run(client, measured=4.0, material="SYN-QC-1"):
    r = client.post("/lab/qc", json={
        "analyte_code": POTASSIUM, "control_material": material,
        "expected_low": 3.8, "expected_high": 4.2,
        "measured_value": measured}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def test_qc_missing_policy_comes_from_catalog_config(client):
    _patient(client)
    # Default: no QC history does not block verification.
    _, outcome = _resulted(client)
    report_id = outcome["report"]["id"]
    assert client.post(f"/lab/reports/{report_id}/verify",
                       headers=CLINICIAN).status_code == 200
    # Publish the strict policy: missing QC blocks.
    _catalog_with_qc(client, {"blockOnFailedLatest": True,
                              "blockOnMissingQc": True,
                              "freshnessHours": None})
    _, outcome2 = _resulted(client)
    report2 = outcome2["report"]["id"]
    r = client.post(f"/lab/reports/{report2}/verify", headers=CLINICIAN)
    assert r.status_code == 409 and "missing-qc" in r.json()["detail"]


def test_qc_freshness_policy_comes_from_catalog_config(client):
    _patient(client)
    _catalog_with_qc(client, {"blockOnFailedLatest": True,
                              "blockOnMissingQc": False,
                              "freshnessHours": 24})
    _qc_run(client)
    # Age the passing run beyond the freshness window.
    factory = client.app.state.session_factory
    with factory() as session:
        run = session.scalars(select(LabQcRun)).one()
        run.created_at = datetime.now(timezone.utc) - timedelta(hours=72)
        session.commit()
    _, outcome = _resulted(client)
    report_id = outcome["report"]["id"]
    r = client.post(f"/lab/reports/{report_id}/verify", headers=CLINICIAN)
    assert r.status_code == 409 and "stale-qc" in r.json()["detail"]
    # A fresh passing run unblocks.
    _qc_run(client, material="SYN-QC-FRESH")
    assert client.post(f"/lab/reports/{report_id}/verify",
                       headers=CLINICIAN).status_code == 200


# ---------------------------------------------------------------------------
# E. Critical-value notification
# ---------------------------------------------------------------------------

def test_critical_notification_lifecycle(client):
    _patient(client)
    _, outcome = _resulted(client)
    report_id = outcome["report"]["id"]
    notify = f"/lab/reports/{report_id}/notify"
    ack = f"/lab/reports/{report_id}/acknowledge"

    # The verification queue exposes the pending notification state.
    queue = client.get("/lab/verification-queue", headers=CLINICIAN).json()
    item = [i for i in queue["items"] if i["report"]["id"] == report_id][0]
    assert item["critical"] is True
    assert item["notification"]["state"] == "pending"

    # Acknowledge before notify -> 409; notify before release -> 409.
    assert client.post(ack, headers=CLINICIAN).status_code == 409
    r = client.post(notify, json={"notified_to": "Dr A", "read_back": True},
                    headers=CLINICIAN)
    assert r.status_code == 409

    assert client.post(f"/lab/reports/{report_id}/verify",
                       headers=CLINICIAN).status_code == 200
    assert client.post(f"/lab/reports/{report_id}/release",
                       headers=CLINICIAN).status_code == 200

    # The critical worklist shows the released report, still pending;
    # patients cannot see the worklist at all.
    assert client.get("/lab/critical-reports", headers=PATIENT).status_code == 403
    worklist = client.get("/lab/critical-reports", headers=CLINICIAN).json()
    entry = [i for i in worklist["items"] if i["report"]["id"] == report_id]
    assert len(entry) == 1 and entry[0]["notification"]["state"] == "pending"

    # Read-back is mandatory; the notified party must be named.
    assert client.post(notify, json={"notified_to": "Dr A"},
                       headers=CLINICIAN).status_code == 422
    assert client.post(notify, json={"notified_to": "Dr A",
                                     "read_back": False},
                       headers=CLINICIAN).status_code == 422
    assert client.post(notify, json={"read_back": True},
                       headers=CLINICIAN).status_code == 422
    # Viewers cannot record notifications.
    assert client.post(notify, json={"notified_to": "Dr A", "read_back": True},
                       headers=VIEWER).status_code == 403

    r = client.post(notify, json={"notified_to": "Dr A (attending)",
                                  "read_back": True}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    note = r.json()["notification"]
    assert note["state"] == "notified" and note["notified_to"] == "Dr A (attending)"
    assert note["read_back"] is True and note["notified_by"] == "dev-clinician"
    assert note["notified_at"] is not None
    # Double notification -> 409.
    assert client.post(notify, json={"notified_to": "Dr A", "read_back": True},
                       headers=CLINICIAN).status_code == 409

    r = client.post(ack, headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["notification"]["state"] == "acknowledged"
    assert r.json()["notification"]["acknowledged_by"] == "dev-clinician"
    assert client.post(ack, headers=CLINICIAN).status_code == 409

    # The worklist reflects the final state.
    worklist = client.get("/lab/critical-reports", headers=CLINICIAN).json()
    entry = [i for i in worklist["items"] if i["report"]["id"] == report_id][0]
    assert entry["notification"]["state"] == "acknowledged"


def test_notify_non_critical_report_is_400_and_patient_view_clean(client):
    _patient(client)
    _, outcome = _resulted(client, code="58410-2", results=CBC_RESULTS)
    report_id = outcome["report"]["id"]
    assert client.post(f"/lab/reports/{report_id}/verify",
                       headers=CLINICIAN).status_code == 200
    assert client.post(f"/lab/reports/{report_id}/release",
                       headers=CLINICIAN).status_code == 200
    r = client.post(f"/lab/reports/{report_id}/notify",
                    json={"notified_to": "Dr A", "read_back": True},
                    headers=CLINICIAN)
    assert r.status_code == 400
    # Not on the critical worklist.
    worklist = client.get("/lab/critical-reports", headers=CLINICIAN).json()
    assert worklist["items"] == []
    # The patient's own released report stays readable and carries no
    # notification internals in its FHIR body.
    r = client.get(f"/fhir/DiagnosticReport/{report_id}", headers=PATIENT)
    assert r.status_code == 200
    assert "notified_to" not in r.text and "notification" not in r.text
