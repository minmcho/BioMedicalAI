"""Phase 2 — Lab LIS: Studio-configured catalog + workflow, order ->
specimen -> result lifecycle, reference ranges / critical flags, two-step
verify + release governance, patient/inbox invisibility of unreleased
results, QC gating, and HL7 v2 ORU / CSV analyser ingest.

Synthetic data only (fabricated patients, fabricated values)."""
from sqlalchemy import select

from app.audit import verify_chain
from app.models import AuditLog
from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER

CBC = "58410-2"
BMP = "51990-0"
POTASSIUM = "2823-3"

CBC_RESULTS = [
    {"code": "718-7", "value": 13.5},
    {"code": "4544-3", "value": 41.0},
    {"code": "6690-2", "value": 7.2},
    {"code": "777-3", "value": 260},
]

BMP_RESULTS = [
    {"code": "2345-7", "value": 95},      # glucose — normal
    {"code": "3094-0", "value": 12},      # BUN — normal
    {"code": "2160-0", "value": 1.0},     # creatinine — normal
    {"code": "2951-2", "value": 130},     # sodium — low (L)
    {"code": POTASSIUM, "value": 6.4},    # potassium — critical high (HH)
]


def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic", "given": ["Lab"]}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _order(client, code=CBC, patient="pat-1", headers=CLINICIAN, **extra):
    body = {"code": code, "subject": {"reference": f"Patient/{patient}"}}
    body.update(extra)
    r = client.post("/lab/orders", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _transition(client, order_id, to_status, headers=CLINICIAN, expect=200, **extra):
    r = client.post(f"/lab/orders/{order_id}/transition",
                    json={"to_status": to_status, **extra}, headers=headers)
    assert r.status_code == expect, r.text
    return r


def _to_received(client, order_id):
    _transition(client, order_id, "collected")
    _transition(client, order_id, "received")


def _enter_results(client, order_id, results, expect=201):
    r = client.post(f"/lab/orders/{order_id}/results",
                    json={"results": results}, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _resulted_bmp(client):
    """A BMP order for pat-1 with results entered (draft state)."""
    _patient(client)
    order = _order(client, code=BMP)
    _to_received(client, order["id"])
    outcome = _enter_results(client, order["id"], BMP_RESULTS).json()
    return order, outcome


def _publish_config(client, kind, key, payload):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    return version


def _qc(client, analyte=POTASSIUM, measured=4.0, low=3.8, high=4.2,
        material="SYN-QC-LOT-1", headers=CLINICIAN, expect=201):
    r = client.post("/lab/qc",
                    json={"analyte_code": analyte, "control_material": material,
                          "expected_low": low, "expected_high": high,
                          "measured_value": measured},
                    headers=headers)
    assert r.status_code == expect, r.text
    return r


# ---------------------------------------------------------------------------
# Studio config seeding (catalog + workflow are configuration, not code)
# ---------------------------------------------------------------------------

def test_lab_catalog_and_workflow_seeded_as_published_config(client):
    eff = client.get("/studio/config/terminology/effective/lab-catalog",
                     headers=CLINICIAN)
    assert eff.status_code == 200
    concepts = {c["code"]: c for c in eff.json()["payload"]["concepts"]}
    assert concepts[CBC]["panel"] is True
    assert concepts[CBC]["components"] == ["718-7", "4544-3", "6690-2", "777-3"]
    potassium = concepts[POTASSIUM]["referenceRange"]
    assert potassium["low"] == 3.5 and potassium["high"] == 5.1
    assert potassium["criticalHigh"] == 6.0 and potassium["criticalLow"] == 2.8

    wf = client.get("/studio/config/workflow/effective/lab-order-flow",
                    headers=CLINICIAN)
    assert wf.status_code == 200
    pairs = {(t["from"], t["to"]) for t in wf.json()["payload"]["transitions"]}
    assert ("ordered", "collected") in pairs
    assert ("in-process", "resulted") in pairs
    assert ("verified", "released") in pairs
    assert ("ordered", "resulted") not in pairs


def test_catalog_endpoint_serves_config_to_staff_not_patients(client):
    r = client.get("/lab/catalog", headers=CLINICIAN)
    assert r.status_code == 200
    assert r.json()["catalog_version"] == 1
    assert client.get("/lab/catalog", headers=PATIENT).status_code == 403
    assert client.get("/lab/catalog").status_code == 401


# ---------------------------------------------------------------------------
# Order placement (catalog validation + role gates)
# ---------------------------------------------------------------------------

def test_place_order_happy_path(client):
    _patient(client)
    order = _order(client)
    assert order["resourceType"] == "ServiceRequest"
    assert order["status"] == "ordered" and order["intent"] == "order"
    assert order["code"]["coding"][0]["code"] == CBC
    queue = client.get("/lab/orders", params={"status": "ordered"},
                       headers=VIEWER).json()
    assert queue["total"] == 1
    assert queue["entry"][0]["resource"]["id"] == order["id"]


def test_place_order_unknown_or_inactive_code_is_422(client):
    _patient(client)
    r = client.post("/lab/orders", json={"code": "0000-0",
                                         "subject": {"reference": "Patient/pat-1"}},
                    headers=CLINICIAN)
    assert r.status_code == 422 and "not in the published lab catalog" in r.json()["detail"]
    # deactivate a code by publishing a new catalog version, then ordering
    # it fails the same governed way
    payload = client.get("/studio/config/terminology/effective/lab-catalog",
                         headers=ADMIN).json()["payload"]
    payload["concepts"].append({"code": "9999-9", "display": "Retired synthetic test",
                                "active": False})
    _publish_config(client, "terminology", "lab-catalog", payload)
    r = client.post("/lab/orders", json={"code": "9999-9",
                                         "subject": {"reference": "Patient/pat-1"}},
                    headers=CLINICIAN)
    assert r.status_code == 422 and "inactive" in r.json()["detail"]


def test_place_order_role_gates_and_patient_existence(client):
    _patient(client)
    body = {"code": CBC, "subject": {"reference": "Patient/pat-1"}}
    assert client.post("/lab/orders", json=body).status_code == 401
    assert client.post("/lab/orders", json=body, headers=VIEWER).status_code == 403
    assert client.post("/lab/orders", json=body, headers=PATIENT).status_code == 403
    assert client.post("/lab/orders", json=body, headers=CONFIG).status_code == 201
    ghost = {"code": CBC, "subject": {"reference": "Patient/nope"}}
    assert client.post("/lab/orders", json=ghost, headers=CLINICIAN).status_code == 404
    missing_code = {"subject": {"reference": "Patient/pat-1"}}
    assert client.post("/lab/orders", json=missing_code,
                       headers=CLINICIAN).status_code == 422


# ---------------------------------------------------------------------------
# Specimen lifecycle
# ---------------------------------------------------------------------------

def test_specimen_lifecycle_and_accession(client):
    _patient(client)
    order = _order(client)
    out = _transition(client, order["id"], "collected",
                      specimen_type="Blood specimen").json()
    specimen = out["specimen"]
    assert specimen is not None and specimen["status"] == "collected"
    accession = specimen["accessionIdentifier"]["value"]
    assert accession.startswith("ACC-")
    assert specimen["subject"]["reference"] == "Patient/pat-1"
    assert specimen["request"][0]["reference"] == f"ServiceRequest/{order['id']}"
    assert out["order"]["specimen"][0]["reference"] == f"Specimen/{specimen['id']}"
    assert out["order"]["status"] == "collected"

    out = _transition(client, order["id"], "received").json()
    assert out["specimen"]["status"] == "received"
    assert out["specimen"]["receivedDateTime"]
    out = _transition(client, order["id"], "in-process").json()
    assert out["order"]["status"] == "in-process"
    assert out["specimen"]["status"] == "in-process"

    listed = client.get("/lab/specimens", params={"status": "in-process"},
                        headers=VIEWER).json()
    assert listed["total"] == 1
    assert listed["entry"][0]["resource"]["accessionIdentifier"]["value"] == accession


def test_illegal_and_governed_transitions(client):
    _patient(client)
    order = _order(client)
    # skipping states is illegal
    _transition(client, order["id"], "received", expect=409)
    _transition(client, order["id"], "in-process", expect=409)
    # unknown target state
    _transition(client, order["id"], "bogus-state", expect=422)
    # missing target
    r = client.post(f"/lab/orders/{order['id']}/transition", json={},
                    headers=CLINICIAN)
    assert r.status_code == 422
    _to_received(client, order["id"])
    # resulted is governed: a bare transition into it is refused with 422
    _transition(client, order["id"], "resulted", expect=422)
    _transition(client, order["id"], "released", expect=422)
    # viewer cannot transition at all
    assert client.post(f"/lab/orders/{order['id']}/transition",
                       json={"to_status": "in-process"},
                       headers=VIEWER).status_code == 403


def test_rejection_requires_reason_and_is_terminal(client):
    _patient(client)
    order = _order(client)
    _transition(client, order["id"], "collected")
    _transition(client, order["id"], "rejected", expect=422)
    out = _transition(client, order["id"], "rejected",
                      reason="Haemolysed sample").json()
    assert out["order"]["status"] == "rejected"
    assert out["order"]["statusReason"]["text"] == "Haemolysed sample"
    assert "Haemolysed" in out["specimen"]["note"][0]["text"]
    _transition(client, order["id"], "collected", expect=409)


# ---------------------------------------------------------------------------
# Result entry, ranges and critical flags
# ---------------------------------------------------------------------------

def test_manual_results_interpretation_and_critical_flag(client):
    order, outcome = _resulted_bmp(client)
    report = outcome["report"]
    assert report["status"] == "preliminary"
    assert outcome["critical"] is True
    assert report["extension"] == [
        {"url": "urn:opencare:extension/critical-result", "valueBoolean": True}]
    by_code = {o["code"]["coding"][0]["code"]: o for o in outcome["observations"]}
    assert by_code[POTASSIUM]["interpretation"][0]["coding"][0]["code"] == "HH"
    assert by_code["2951-2"]["interpretation"][0]["coding"][0]["code"] == "L"
    assert by_code["2345-7"]["interpretation"][0]["coding"][0]["code"] == "N"
    assert all(o["status"] == "preliminary" for o in outcome["observations"])
    potassium_range = by_code[POTASSIUM]["referenceRange"][0]
    assert potassium_range["low"]["value"] == 3.5
    assert potassium_range["high"]["value"] == 5.1

    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "resulted"
    assert detail["specimen"]["status"] == "resulted"
    assert [r["id"] for r in detail["reports"]] == [report["id"]]


def test_result_entry_validation(client):
    _patient(client)
    order = _order(client, code=BMP)
    # results before the specimen is received -> 409
    _enter_results(client, order["id"], BMP_RESULTS, expect=409)
    _transition(client, order["id"], "collected")
    _enter_results(client, order["id"], BMP_RESULTS, expect=409)
    _transition(client, order["id"], "received")
    # analyte outside the ordered panel -> 422
    _enter_results(client, order["id"], [{"code": "718-7", "value": 13.0}], expect=422)
    # the panel code itself is not a resultable analyte -> 422
    _enter_results(client, order["id"], [{"code": BMP, "value": 1}], expect=422)
    # unknown analyte -> 422; non-numeric value -> 422; empty list -> 422
    _enter_results(client, order["id"], [{"code": "1111-1", "value": 1}], expect=422)
    _enter_results(client, order["id"], [{"code": POTASSIUM, "value": "high"}], expect=422)
    _enter_results(client, order["id"], [], expect=422)
    # a good entry succeeds, then a second one conflicts (already resulted)
    _enter_results(client, order["id"], BMP_RESULTS)
    _enter_results(client, order["id"], BMP_RESULTS, expect=409)


def test_ranges_come_from_config_not_code(client):
    """Publishing a new catalog with a lower potassium critical threshold
    changes interpretation of the same value — ranges are Studio data."""
    _patient(client)
    payload = client.get("/studio/config/terminology/effective/lab-catalog",
                         headers=ADMIN).json()["payload"]
    for concept in payload["concepts"]:
        if concept["code"] == POTASSIUM:
            concept["referenceRange"]["criticalHigh"] = 5.5
    _publish_config(client, "terminology", "lab-catalog", payload)
    order = _order(client, code=BMP)
    _to_received(client, order["id"])
    outcome = _enter_results(client, order["id"],
                             [{"code": POTASSIUM, "value": 5.8}]).json()
    interp = outcome["observations"][0]["interpretation"][0]["coding"][0]["code"]
    assert interp == "HH"  # under the seeded v1 threshold (6.0) this is only H


def test_workflow_config_drives_lab_machine(client):
    """A published workflow v2 that removes collected -> received makes
    that transition illegal — the machine is configuration."""
    payload = client.get("/studio/config/workflow/effective/lab-order-flow",
                         headers=ADMIN).json()["payload"]
    payload["transitions"] = [t for t in payload["transitions"]
                              if not (t["from"] == "collected" and t["to"] == "received")]
    _publish_config(client, "workflow", "lab-order-flow", payload)
    _patient(client)
    order = _order(client)
    _transition(client, order["id"], "collected")
    _transition(client, order["id"], "received", expect=409)


# ---------------------------------------------------------------------------
# Visibility governance: unreleased results are invisible to patient+inbox
# ---------------------------------------------------------------------------

def test_unreleased_results_invisible_until_release(client):
    order, outcome = _resulted_bmp(client)
    report_id = outcome["report"]["id"]
    obs_ids = [o["id"] for o in outcome["observations"]]

    # patient portal: none of the lab results are there
    portal = client.get("/portal/my-results", headers=PATIENT).json()
    portal_ids = {e["resource"]["id"] for e in portal["entry"]}
    assert report_id not in portal_ids
    assert not (set(obs_ids) & portal_ids)
    # patient by-id reads and search: non-disclosure 404 / exclusion
    assert client.get(f"/fhir/Observation/{obs_ids[0]}",
                      headers=PATIENT).status_code == 404
    assert client.get(f"/fhir/DiagnosticReport/{report_id}",
                      headers=PATIENT).status_code == 404
    search = client.get("/fhir/Observation", params={"patient": "pat-1"},
                        headers=PATIENT).json()
    assert not ({e["resource"]["id"] for e in search["entry"]} & set(obs_ids))
    # clinician inbox: drafts do not appear there either
    inbox = client.get("/fhir/inbox", params={"patient": "pat-1"},
                       headers=CLINICIAN).json()
    inbox_ids = {e["resource"]["id"] for e in inbox["entry"]}
    assert report_id not in inbox_ids and not (set(obs_ids) & inbox_ids)
    # lab surfaces are staff-only
    assert client.get("/lab/orders", headers=PATIENT).status_code == 403
    assert client.get("/lab/verification-queue", headers=PATIENT).status_code == 403
    # ...but the verification queue shows the draft, flagged critical
    queue = client.get("/lab/verification-queue", headers=CLINICIAN).json()
    assert queue["total"] == 1
    item = queue["items"][0]
    assert item["report"]["id"] == report_id
    assert item["stage"] == "awaiting-verification"
    assert item["critical"] is True and item["qc_blocked"] == []
    # staff CAN read the draft by id through the FHIR surface
    assert client.get(f"/fhir/Observation/{obs_ids[0]}",
                      headers=CLINICIAN).status_code == 200


def test_verify_then_release_publishes_results(client):
    order, outcome = _resulted_bmp(client)
    report_id = outcome["report"]["id"]

    # release before verification -> 409; verify is clinician/admin only
    assert client.post(f"/lab/reports/{report_id}/release",
                       headers=CLINICIAN).status_code == 409
    assert client.post(f"/lab/reports/{report_id}/verify",
                       headers=CONFIG).status_code == 403
    assert client.post(f"/lab/reports/{report_id}/verify",
                       headers=VIEWER).status_code == 403
    assert client.post(f"/lab/reports/{report_id}/verify").status_code == 401

    out = client.post(f"/lab/reports/{report_id}/verify", headers=CLINICIAN)
    assert out.status_code == 200, out.text
    assert out.json()["report"]["status"] == "final"
    assert out.json()["order"]["status"] == "verified"
    # verified is not released: the patient still sees nothing
    portal = client.get("/portal/my-results", headers=PATIENT).json()
    assert report_id not in {e["resource"]["id"] for e in portal["entry"]}
    queue = client.get("/lab/verification-queue", headers=CLINICIAN).json()
    assert queue["items"][0]["stage"] == "awaiting-release"
    # verifying twice conflicts
    assert client.post(f"/lab/reports/{report_id}/verify",
                       headers=CLINICIAN).status_code == 409

    out = client.post(f"/lab/reports/{report_id}/release", headers=CLINICIAN)
    assert out.status_code == 200, out.text
    assert out.json()["order"]["status"] == "released"
    assert out.json()["report"]["issued"]
    # now the results are visible everywhere they should be
    portal = client.get("/portal/my-results", headers=PATIENT).json()
    portal_by_id = {e["resource"]["id"]: e["resource"] for e in portal["entry"]}
    assert report_id in portal_by_id
    released_obs = [r for r in portal_by_id.values()
                    if r["resourceType"] == "Observation"]
    assert len(released_obs) == len(BMP_RESULTS)
    assert all(r["status"] == "final" for r in released_obs)
    potassium = [r for r in released_obs
                 if r["code"]["coding"][0]["code"] == POTASSIUM][0]
    assert potassium["interpretation"][0]["coding"][0]["code"] == "HH"
    inbox = client.get("/fhir/inbox", params={"patient": "pat-1"},
                       headers=CLINICIAN).json()
    assert report_id in {e["resource"]["id"] for e in inbox["entry"]}
    # queue is drained; releasing twice conflicts
    assert client.get("/lab/verification-queue", headers=CLINICIAN).json()["total"] == 0
    assert client.post(f"/lab/reports/{report_id}/release",
                       headers=CLINICIAN).status_code == 409


def test_verify_rejects_non_lab_report(client):
    _patient(client)
    r = client.post("/fhir/DiagnosticReport",
                    json={"resourceType": "DiagnosticReport", "id": "plain-report",
                          "status": "preliminary",
                          "code": {"text": "Non-lab report"},
                          "subject": {"reference": "Patient/pat-1"}},
                    headers=CLINICIAN)
    assert r.status_code == 201
    assert client.post("/lab/reports/plain-report/verify",
                       headers=CLINICIAN).status_code == 422
    assert client.post("/lab/reports/nope/verify",
                       headers=CLINICIAN).status_code == 404


# ---------------------------------------------------------------------------
# QC: records, server-side pass/fail, and the release gate
# ---------------------------------------------------------------------------

def test_qc_run_validation_and_pass_fail_computation(client):
    run = _qc(client, measured=4.1).json()
    assert run["passed"] is True and run["analyte_code"] == POTASSIUM
    run = _qc(client, measured=5.9, material="SYN-QC-LOT-2").json()
    assert run["passed"] is False
    # caller cannot inject a verdict
    r = client.post("/lab/qc",
                    json={"analyte_code": POTASSIUM, "control_material": "LOT",
                          "expected_low": 3.8, "expected_high": 4.2,
                          "measured_value": 99.0, "passed": True},
                    headers=CLINICIAN)
    assert r.status_code == 201 and r.json()["passed"] is False
    # validation
    _qc(client, analyte="0000-0", expect=422)
    _qc(client, low=5.0, high=4.0, expect=422)
    r = client.post("/lab/qc",
                    json={"analyte_code": POTASSIUM, "expected_low": 3.8,
                          "expected_high": 4.2, "measured_value": 4.0},
                    headers=CLINICIAN)
    assert r.status_code == 422  # missing control_material
    assert client.post("/lab/qc", json={}, headers=VIEWER).status_code == 403
    assert client.post("/lab/qc", json={}).status_code == 401
    # listing + status
    runs = client.get("/lab/qc", params={"analyte_code": POTASSIUM},
                      headers=VIEWER).json()
    assert len(runs) == 3
    status = client.get("/lab/qc/status", params={"analyte_code": POTASSIUM},
                        headers=CLINICIAN).json()
    assert status["blocked"] is True  # latest run failed
    assert client.get("/lab/qc", headers=PATIENT).status_code == 403


def test_failed_qc_blocks_verify_and_release_until_passing_qc(client):
    order, outcome = _resulted_bmp(client)
    report_id = outcome["report"]["id"]

    # an unrelated analyte's QC failure does NOT block this report
    _qc(client, analyte="718-7", measured=99.0, low=10.0, high=14.0)
    assert client.post(f"/lab/reports/{report_id}/verify",
                       headers=CLINICIAN).status_code == 200

    # a potassium QC failure AFTER verification blocks the release
    _qc(client, measured=5.9, material="SYN-QC-FAIL")
    r = client.post(f"/lab/reports/{report_id}/release", headers=CLINICIAN)
    assert r.status_code == 409
    assert POTASSIUM in r.json()["detail"] and "QC" in r.json()["detail"]
    # the queue surfaces the block
    queue = client.get("/lab/verification-queue", headers=CLINICIAN).json()
    blocked = queue["items"][0]["qc_blocked"]
    assert [b["analyte_code"] for b in blocked] == [POTASSIUM]
    # a passing QC run after the failure unblocks the release
    _qc(client, measured=4.0, material="SYN-QC-PASS")
    assert client.post(f"/lab/reports/{report_id}/release",
                       headers=CLINICIAN).status_code == 200


def test_failed_qc_blocks_verification(client):
    order, outcome = _resulted_bmp(client)
    report_id = outcome["report"]["id"]
    _qc(client, measured=2.0, material="SYN-QC-FAIL")
    r = client.post(f"/lab/reports/{report_id}/verify", headers=CLINICIAN)
    assert r.status_code == 409 and POTASSIUM in r.json()["detail"]
    _qc(client, measured=4.05, material="SYN-QC-PASS")
    assert client.post(f"/lab/reports/{report_id}/verify",
                       headers=CLINICIAN).status_code == 200


# ---------------------------------------------------------------------------
# Analyser ingest: HL7 v2 ORU^R01 + CSV
# ---------------------------------------------------------------------------

HL7_MESSAGE = "\r".join([
    "MSH|^~\\&|ANALYZER-X|CENTRAL LAB|OPENCARE|LIS|20261015080000||ORU^R01|MSG0001|P|2.5",
    "PID|1||pat-1||Synthetic^Lab||19680412|F",
    "OBR|1|{oid}|{oid}|58410-2^Complete blood count panel",
    "OBX|1|NM|718-7^Hemoglobin||13.2|g/dL|12.0-15.5",
    "OBX|2|NM|4544-3^Hematocrit||40.5|%|36-46",
    "OBX|3|NM|6690-2^Leukocytes||6.8|10^9/L|4.0-11.0",
    "OBX|4|NM|777-3^Platelets||255|10^9/L|150-400",
])


def test_hl7_ingest_creates_draft_results(client):
    _patient(client)
    order = _order(client, id="ord-hl7-1")
    _to_received(client, order["id"])
    r = client.post("/lab/ingest",
                    json={"format": "hl7",
                          "content": HL7_MESSAGE.format(oid="ord-hl7-1")},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["report_count"] == 1 and body["result_count"] == 4
    assert body["ingested"][0]["critical"] is False
    # drafts, not releases: order resulted, report preliminary, patient
    # portal still empty for these results
    detail = client.get("/lab/orders/ord-hl7-1", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "resulted"
    report = detail["reports"][0]
    assert report["status"] == "preliminary"
    obs = client.get(f"/fhir/Observation/{report['result'][0]['reference'].split('/')[1]}",
                     headers=CLINICIAN).json()
    assert obs["status"] == "preliminary"
    assert obs["valueQuantity"]["value"] == 13.2
    portal = client.get("/portal/my-results", headers=PATIENT).json()
    assert report["id"] not in {e["resource"]["id"] for e in portal["entry"]}
    queue = client.get("/lab/verification-queue", headers=CLINICIAN).json()
    assert queue["total"] == 1


def test_hl7_ingest_errors(client):
    _patient(client)
    order = _order(client, id="ord-hl7-2")
    good = HL7_MESSAGE.format(oid="ord-hl7-2")
    # not an HL7 message
    r = client.post("/lab/ingest", json={"format": "hl7", "content": "hello"},
                    headers=CLINICIAN)
    assert r.status_code == 422
    # order not yet received -> 409 (specimen has not arrived)
    r = client.post("/lab/ingest", json={"format": "hl7", "content": good},
                    headers=CLINICIAN)
    assert r.status_code == 409
    _to_received(client, order["id"])
    # patient mismatch (PID names pat-2, order is pat-1's) -> 422
    bad_patient = good.replace("PID|1||pat-1|", "PID|1||pat-2|")
    r = client.post("/lab/ingest", json={"format": "hl7", "content": bad_patient},
                    headers=CLINICIAN)
    assert r.status_code == 422 and "does not match" in r.json()["detail"]
    # unknown order -> 422
    r = client.post("/lab/ingest",
                    json={"format": "hl7", "content": HL7_MESSAGE.format(oid="ghost")},
                    headers=CLINICIAN)
    assert r.status_code == 422 and "unknown lab order" in r.json()["detail"]
    # non-numeric OBX value -> 422
    bad_value = good.replace("||13.2|", "||hemolyzed|")
    r = client.post("/lab/ingest", json={"format": "hl7", "content": bad_value},
                    headers=CLINICIAN)
    assert r.status_code == 422 and "not numeric" in r.json()["detail"]
    # unknown format -> 422; viewer cannot ingest
    r = client.post("/lab/ingest", json={"format": "xml", "content": good},
                    headers=CLINICIAN)
    assert r.status_code == 422
    assert client.post("/lab/ingest", json={"format": "hl7", "content": good},
                       headers=VIEWER).status_code == 403


def test_csv_ingest(client):
    _patient(client)
    order = _order(client, code=BMP, id="ord-csv-1")
    _to_received(client, order["id"])
    content = (
        "order_id,patient_id,code,value,unit\n"
        "ord-csv-1,pat-1,2345-7,88,mg/dL\n"
        "ord-csv-1,pat-1,2823-3,4.4,mmol/L\n"
    )
    r = client.post("/lab/ingest", json={"format": "csv", "content": content},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["result_count"] == 2
    detail = client.get("/lab/orders/ord-csv-1", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "resulted"
    # malformed CSVs
    r = client.post("/lab/ingest",
                    json={"format": "csv", "content": "code,value\n2823-3,4.4\n"},
                    headers=CLINICIAN)
    assert r.status_code == 422 and "order_id" in r.json()["detail"]
    r = client.post("/lab/ingest",
                    json={"format": "csv",
                          "content": "order_id,code,value\nord-csv-1,2823-3,wet\n"},
                    headers=CLINICIAN)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Generic FHIR surface: the new resource types
# ---------------------------------------------------------------------------

def test_new_resource_types_on_generic_surface(client):
    _patient(client)
    sr = {"resourceType": "ServiceRequest", "id": "sr-1", "status": "active",
          "intent": "order", "code": {"text": "Synthetic request"},
          "subject": {"reference": "Patient/pat-1"}}
    assert client.post("/fhir/ServiceRequest", json=sr,
                       headers=CLINICIAN).status_code == 201
    missing_intent = dict(sr, id="sr-2")
    del missing_intent["intent"]
    assert client.post("/fhir/ServiceRequest", json=missing_intent,
                       headers=CLINICIAN).status_code == 422

    specimen = {"resourceType": "Specimen", "id": "sp-1", "status": "available",
                "subject": {"reference": "Patient/pat-1"}}
    assert client.post("/fhir/Specimen", json=specimen,
                       headers=CLINICIAN).status_code == 201
    assert client.get("/fhir/Specimen/sp-1", headers=VIEWER).status_code == 200
    no_subject = {"resourceType": "Specimen", "id": "sp-2", "status": "available"}
    assert client.post("/fhir/Specimen", json=no_subject,
                       headers=CLINICIAN).status_code == 422

    task = {"resourceType": "Task", "id": "task-1", "status": "requested",
            "intent": "order", "for": {"reference": "Patient/pat-1"}}
    assert client.post("/fhir/Task", json=task, headers=CLINICIAN).status_code == 201
    found = client.get("/fhir/Task", params={"patient": "pat-1"},
                       headers=CLINICIAN).json()
    assert found["total"] == 1
    no_intent = {"resourceType": "Task", "id": "task-2", "status": "requested"}
    assert client.post("/fhir/Task", json=no_intent,
                       headers=CLINICIAN).status_code == 422
    # patients cannot write any of these
    assert client.post("/fhir/ServiceRequest", json=dict(sr, id="sr-3"),
                       headers=PATIENT).status_code == 403


# ---------------------------------------------------------------------------
# Audit: lab writes are in the hash chain
# ---------------------------------------------------------------------------

def test_lab_actions_are_audited_and_chain_verifies(client):
    order, outcome = _resulted_bmp(client)
    _qc(client, measured=4.0)
    report_id = outcome["report"]["id"]
    client.post(f"/lab/reports/{report_id}/verify", headers=CLINICIAN)
    client.post(f"/lab/reports/{report_id}/release", headers=CLINICIAN)
    factory = client.app.state.session_factory
    with factory() as session:
        actions = [row.action for row in session.scalars(select(AuditLog)).all()]
        for expected in ("lab.order.transition", "lab.results.create",
                         "lab.qc.run", "lab.report.verify", "lab.report.release"):
            assert expected in actions, expected
        assert verify_chain(session) is True
