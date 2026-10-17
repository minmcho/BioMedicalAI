"""Phase 8 — Live interoperability (app.interop + app.hl7).

HL7 v2 builders (golden), the interface outbox (ADT/ORM/ORU queued in
the SAME transaction as their clinical events), ELR public-health
reporting from the published reportable-conditions list, the
interface admin log, and FHIR bulk ($export-style) NDJSON jobs.

Everything is Studio configuration (§6): endpoints, retry policy and
the reportable list are published config; the seeded endpoints are
disabled, so nothing queues until a test publishes an enabled one —
the config flip is the behaviour change. Synthetic data only.
"""
import json

from sqlalchemy import select

from app import hl7
from app.models import AdtEvent, Hl7OutboundMessage
from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER

GLUCOSE = "2345-7"
POTASSIUM = "2823-3"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "identifier": [{"system": "urn:opencare:synthetic-mrn",
                        "value": f"MRN-{pid}"}],
        "name": [{"family": "Synthetic8", "given": ["Interop"]}],
        "gender": "female", "birthDate": "1968-04-12"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return pid


def _publish(client, kind, key, payload, headers=ADMIN):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=headers)
    assert r.status_code == 200, r.text
    return version


def _endpoints_payload(endpoints, max_attempts=3, backoff=0):
    return {"facility": {"sendingApplication": "OPENCARE",
                         "sendingFacility": "OPENCARE HOSPITAL"},
            "retry": {"maxAttempts": max_attempts, "backoffSeconds": backoff},
            "endpoints": endpoints}


def _endpoint(name, port, types=("ADT", "ORM", "ORU"), enabled=True):
    return {"name": name, "host": "127.0.0.1", "port": port,
            "messageTypes": list(types), "enabled": enabled,
            "receivingApplication": "DOWNSTREAM", "receivingFacility": "EXT"}


def _enable_downstream(client, port=2599, **kw):
    _publish(client, "integration", "hl7-endpoints",
             _endpoints_payload([_endpoint("downstream-ehr", port, **kw)]))


def _outbound(client, **params):
    r = client.get("/interop/messages", params={"direction": "outbound", **params},
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _admit(client, patient="pat-1", bed="MS-101A"):
    r = client.post("/inpatient/admissions",
                    json={"patient": patient, "bed": bed}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _lab_order(client, code, patient="pat-1"):
    r = client.post("/lab/orders",
                    json={"code": code,
                          "subject": {"reference": f"Patient/{patient}"}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _lab_transition(client, order_id, to_status):
    r = client.post(f"/lab/orders/{order_id}/transition",
                    json={"to_status": to_status}, headers=CLINICIAN)
    assert r.status_code == 200, r.text


def _release_lab(client, code, value, patient="pat-1"):
    order = _lab_order(client, code, patient=patient)
    for status in ("collected", "received", "in-process"):
        _lab_transition(client, order["id"], status)
    r = client.post(f"/lab/orders/{order['id']}/results",
                    json={"results": [{"code": code, "value": value}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    report_id = r.json()["report"]["id"]
    assert client.post(f"/lab/reports/{report_id}/verify",
                       headers=CLINICIAN).status_code == 200
    assert client.post(f"/lab/reports/{report_id}/release",
                       headers=CLINICIAN).status_code == 200
    return order["id"], report_id


# ---------------------------------------------------------------------------
# HL7 builders — golden messages
# ---------------------------------------------------------------------------

PATIENT_BODY = {"resourceType": "Patient", "id": "pat-1",
                "identifier": [{"system": "urn:opencare:synthetic-mrn",
                                "value": "SYN-1001"}],
                "name": [{"family": "Synthetic402", "given": ["Alicia"]}],
                "gender": "female", "birthDate": "1968-04-12"}


def _segments(message):
    return [line.split("|") for line in message.split("\r")]


def test_build_adt_golden():
    encounter = {"resourceType": "Encounter", "id": "enc-1",
                 "class": {"coding": [{"code": "IMP"}]},
                 "period": {"start": "2026-10-17T08:30:00Z"}}
    message = hl7.build_adt(
        PATIENT_BODY, encounter, "A01", control_id="ADT0001",
        sending_application="OPENCARE", sending_facility="OPENCARE HOSPITAL",
        receiving_application="DOWNSTREAM", assigned_location="unit-medsurg^101^A")
    segs = _segments(message)
    assert [s[0] for s in segs] == ["MSH", "EVN", "PID", "PV1"]
    msh = segs[0]
    assert msh[9 - 1] == "ADT^A01" or msh[8] == "ADT^A01"  # MSH-9
    assert "ADT0001" in msh
    assert msh[2] == "OPENCARE" and msh[4] == "DOWNSTREAM"
    pid = segs[2]
    assert pid[3].startswith("pat-1") and "SYN-1001" in pid[3]
    assert pid[5] == "Synthetic402^Alicia"
    assert pid[7] == "19680412" and pid[8] == "F"
    pv1 = segs[3]
    assert pv1[2] == "I"                       # PV1-2 inpatient class
    assert pv1[3] == "unit-medsurg^101^A"      # PV1-3 assigned location
    assert pv1[19] == "enc-1"                  # PV1-19 visit number
    assert pv1[44] == "20261017083000"         # PV1-44 admit datetime


def test_build_orm_golden():
    order = {"resourceType": "ServiceRequest", "id": "ord-9", "status": "active",
             "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt",
                                  "code": "93005", "display": "ECG 12-lead"}]},
             "authoredOn": "2026-10-17T09:00:00Z"}
    message = hl7.build_orm(order, PATIENT_BODY, control_id="ORM0001",
                            sending_application="OPENCARE",
                            sending_facility="OPENCARE HOSPITAL")
    segs = _segments(message)
    assert [s[0] for s in segs] == ["MSH", "PID", "ORC", "OBR"]
    assert segs[0][8] == "ORM^O01"
    orc, obr = segs[2], segs[3]
    assert orc[1] == "NW" and orc[2] == "ord-9"
    assert obr[2] == "ord-9" and obr[4].startswith("93005^ECG 12-lead")


def test_build_oru_golden_and_elr_variant():
    report = {"resourceType": "DiagnosticReport", "id": "rep-1",
              "code": {"coding": [{"system": "http://loinc.org",
                                   "code": "24323-8",
                                   "display": "Comprehensive metabolic panel"}]},
              "issued": "2026-10-17T10:15:00Z"}
    order = {"resourceType": "ServiceRequest", "id": "ord-1"}
    observations = [
        {"resourceType": "Observation", "id": "obs-1",
         "code": {"coding": [{"system": "http://loinc.org", "code": "2823-3",
                              "display": "Potassium"}]},
         "valueQuantity": {"value": 4.2, "unit": "mmol/L"},
         "referenceRange": [{"low": {"value": 3.5}, "high": {"value": 5.1}}],
         "interpretation": [{"coding": [{"code": "N"}]}]},
        {"resourceType": "Observation", "id": "obs-2",
         "code": {"coding": [{"system": "http://loinc.org", "code": "2345-7",
                              "display": "Glucose"}]},
         "valueQuantity": {"value": 92, "unit": "mg/dL"}}]
    message = hl7.build_oru(report, observations, order, PATIENT_BODY,
                            control_id="ORU0001",
                            sending_application="OPENCARE",
                            sending_facility="OPENCARE HOSPITAL")
    segs = _segments(message)
    assert [s[0] for s in segs] == ["MSH", "PID", "OBR", "OBX", "OBX"]
    assert segs[0][8] == "ORU^R01"
    obr = segs[2]
    assert obr[2] == "ord-1" and obr[4].startswith("24323-8")
    obx = segs[3]
    assert obx[1] == "1" and obx[2] == "NM"
    assert obx[3].startswith("2823-3^Potassium")
    assert obx[5] == "4.2" and obx[6] == "mmol/L"
    assert obx[7] == "3.5-5.1" and obx[8] == "N" and obx[11] == "F"
    assert segs[4][5] == "92"
    # The ELR variant names the reportable condition in an NTE.
    elr = hl7.build_oru(report, observations, order, PATIENT_BODY,
                        control_id="ELR0001", sending_application="OPENCARE",
                        sending_facility="OPENCARE HOSPITAL",
                        elr_condition="Lead poisoning")
    elr_segs = _segments(elr)
    assert [s[0] for s in elr_segs] == ["MSH", "PID", "OBR", "NTE", "OBX", "OBX"]
    assert "Lead poisoning" in elr_segs[3][3]


def test_ack_build_and_parse():
    original = hl7.build_orm(
        {"resourceType": "ServiceRequest", "id": "o1",
         "code": {"coding": [{"code": "93005", "display": "ECG"}]}},
        PATIENT_BODY, control_id="CTL-42", sending_application="OPENCARE",
        sending_facility="OPENCARE HOSPITAL")
    ack = hl7.build_ack(original, "AA")
    parsed = hl7.parse_ack(ack)
    assert parsed["ack_code"] == "AA" and parsed["control_id"] == "CTL-42"
    nak = hl7.build_ack(original, "AR", "no active admission")
    assert hl7.parse_ack(nak)["ack_code"] == "AR"
    assert any(s.startswith("ERR|") for s in nak.split("\r"))
    assert "no active admission" in nak
    # Garbage in: still an ACK shape, never an exception.
    assert hl7.parse_ack(hl7.build_ack("not hl7", "AR"))["ack_code"] == "AR"


# ---------------------------------------------------------------------------
# Seeded config + governance
# ---------------------------------------------------------------------------

def test_seeded_endpoints_disabled_and_nothing_queues(client):
    _patient(client)
    r = client.get("/interop/endpoints", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["config"]["key"] == "hl7-endpoints"
    names = {e["name"] for e in body["endpoints"]}
    assert names == {"downstream-ehr", "public-health"}
    assert all(e["enabled"] is False for e in body["endpoints"])
    # An admission with no enabled endpoint queues nothing at all.
    _admit(client)
    assert _outbound(client) == []
    # Endpoint config is Studio-governed: no write route on /interop.
    assert client.post("/interop/endpoints", json={}, headers=ADMIN).status_code in (404, 405)
    # ...and the Studio validator refuses a malformed endpoint payload.
    bad = _endpoints_payload([{"name": "x", "host": "h", "port": 99999,
                                "messageTypes": ["ADT"], "enabled": True}])
    r = client.post("/studio/config/integration/hl7-endpoints",
                    json=bad, headers=ADMIN)
    assert r.status_code == 422, r.text


def test_interop_read_capability(client):
    assert client.get("/interop/endpoints", headers=VIEWER).status_code == 200
    assert client.get("/interop/endpoints", headers=PATIENT).status_code == 403
    assert client.post("/interop/dispatch", headers=CLINICIAN).status_code == 403


# ---------------------------------------------------------------------------
# Outbound: ADT / ORM / ORU from live events
# ---------------------------------------------------------------------------

def test_adt_feed_follows_the_event_log(client):
    _patient(client)
    _enable_downstream(client)
    admitted = _admit(client)
    encounter_id = admitted["encounter"]["id"]
    rows = _outbound(client, type="ADT^A01")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "pending" and row["endpoint"] == "downstream-ehr"
    assert row["source"]["kind"] == "adt_event"
    assert row["patient_ref"] == "Patient/pat-1"
    detail = client.get(f"/interop/messages/outbound/{row['id']}",
                        headers=ADMIN).json()
    assert "MSH|" in detail["raw"] and "pat-1" in detail["raw"]
    assert "unit-medsurg^101^A" in detail["raw"]

    r = client.post(f"/inpatient/encounters/{encounter_id}/transfer",
                    json={"bed": "ICU-201"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    rows = _outbound(client, type="ADT^A02")
    assert len(rows) == 1
    detail = client.get(f"/interop/messages/outbound/{rows[0]['id']}",
                        headers=ADMIN).json()
    assert "unit-icu^201^1" in detail["raw"]          # new location
    assert "unit-medsurg^101^A" in detail["raw"]      # prior location (PV1-6)

    r = client.post(f"/inpatient/encounters/{encounter_id}/discharge",
                    json={}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert len(_outbound(client, type="ADT^A03")) == 1
    # Exactly one outbound row per ADT event — no duplicates, none lost.
    factory = client.app.state.session_factory
    with factory() as session:
        events = session.scalars(select(AdtEvent)).all()
        queued = session.scalars(select(Hl7OutboundMessage)).all()
    assert len(events) == 3 and len(queued) == 3


def test_outbox_atomicity_rollback(client):
    """The queue row is written in the event's transaction: roll the
    transaction back and BOTH the event and its message vanish."""
    _patient(client)
    _enable_downstream(client)
    from app import inpatient
    from app.models import User, Role
    factory = client.app.state.session_factory
    with factory() as session:
        user = User(username="atomicity-probe", role=Role.clinician)
        inpatient._record_event(session, user, "A01", encounter_id=None,
                                patient_ref="Patient/pat-1",
                                to_location="MS-101A")
        assert session.scalars(
            select(Hl7OutboundMessage)).all(), "queue row must exist pre-commit"
        session.rollback()
    with factory() as session:
        assert session.scalars(select(Hl7OutboundMessage)).all() == []
        assert session.scalars(
            select(AdtEvent).where(AdtEvent.actor == "atomicity-probe")).all() == []


def test_orm_queued_on_signing_only(client):
    _patient(client)
    _enable_downstream(client)
    r = client.post("/orders", json={"type": "procedure", "code": "93005",
                                     "display": "ECG 12-lead", "patient": "pat-1"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    order_id = r.json()["order"]["id"]
    assert _outbound(client, type="ORM^O01") == []     # draft: nothing
    r = client.post(f"/orders/{order_id}/transition",
                    json={"to_status": "signed"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    rows = _outbound(client, type="ORM^O01")
    assert len(rows) == 1
    assert rows[0]["source"]["kind"] == "order"
    assert rows[0]["source"]["id"] == f"ServiceRequest/{order_id}"
    detail = client.get(f"/interop/messages/outbound/{rows[0]['id']}",
                        headers=ADMIN).json()
    assert "ORC|NW|" + order_id in detail["raw"]
    assert "93005" in detail["raw"]


def test_oru_queued_on_release_not_before(client):
    _patient(client)
    _enable_downstream(client)
    order = _lab_order(client, GLUCOSE)
    for status in ("collected", "received", "in-process"):
        _lab_transition(client, order["id"], status)
    r = client.post(f"/lab/orders/{order['id']}/results",
                    json={"results": [{"code": GLUCOSE, "value": 95}]},
                    headers=CLINICIAN)
    report_id = r.json()["report"]["id"]
    assert _outbound(client, type="ORU^R01") == []     # resulted: nothing
    assert client.post(f"/lab/reports/{report_id}/verify",
                       headers=CLINICIAN).status_code == 200
    assert _outbound(client, type="ORU^R01") == []     # verified: still nothing
    assert client.post(f"/lab/reports/{report_id}/release",
                       headers=CLINICIAN).status_code == 200
    rows = _outbound(client, type="ORU^R01")
    assert len(rows) == 1 and rows[0]["source"]["kind"] == "lab_report"
    detail = client.get(f"/interop/messages/outbound/{rows[0]['id']}",
                        headers=ADMIN).json()
    assert GLUCOSE in detail["raw"] and "|95" in detail["raw"]


# ---------------------------------------------------------------------------
# ELR — public-health reporting
# ---------------------------------------------------------------------------

def _enable_all_with_reportable(client):
    _publish(client, "integration", "hl7-endpoints", _endpoints_payload([
        _endpoint("downstream-ehr", 2599),
        _endpoint("public-health", 2598, types=("ORU",)),
    ]))
    _publish(client, "terminology", "reportable-conditions", {
        "name": "Test reportables", "system": "http://loinc.org",
        "concepts": [
            {"code": POTASSIUM, "display": "Potassium", "active": True,
             "condition": "Hyperkalaemia (synthetic trigger)",
             "valueCondition": {"operator": "above", "value": 6.0}},
        ]})


def test_elr_queued_only_for_reportable_results(client):
    _patient(client)
    _enable_all_with_reportable(client)
    # Potassium 6.4 > 6.0 threshold -> reportable.
    _release_lab(client, POTASSIUM, 6.4)
    rows = _outbound(client, type="ORU^R01")
    by_source = {(r["source"]["kind"], r["endpoint"]) for r in rows}
    assert ("lab_report", "downstream-ehr") in by_source
    assert ("elr", "public-health") in by_source
    elr = next(r for r in rows if r["source"]["kind"] == "elr")
    detail = client.get(f"/interop/messages/outbound/{elr['id']}",
                        headers=ADMIN).json()
    assert "Hyperkalaemia (synthetic trigger)" in detail["raw"]
    assert "NTE|" in detail["raw"]
    # Potassium inside the threshold -> no ELR.
    _release_lab(client, POTASSIUM, 4.4)
    assert len([r for r in _outbound(client, type="ORU^R01")
                if r["source"]["kind"] == "elr"]) == 1
    # A non-reportable analyte -> no ELR, ever.
    _release_lab(client, GLUCOSE, 500)
    rows = _outbound(client, type="ORU^R01")
    assert len([r for r in rows if r["source"]["kind"] == "elr"]) == 1
    assert len([r for r in rows if r["source"]["kind"] == "lab_report"]) == 3


# ---------------------------------------------------------------------------
# Interface admin: log filters, dead letters, replay
# ---------------------------------------------------------------------------

def test_message_log_filters(client):
    _patient(client)
    _enable_downstream(client)
    _admit(client)
    assert len(_outbound(client, type="ADT^A01")) == 1
    assert _outbound(client, type="ADT^A03") == []
    assert len(_outbound(client, status="pending")) == 1
    assert len(_outbound(client, patient="pat-1")) == 1
    assert _outbound(client, patient="pat-2") == []
    r = client.get("/interop/messages", params={"direction": "inbound"},
                   headers=ADMIN)
    assert r.json()["items"] == []


def test_replay_refused_for_non_dead_letter(client):
    _patient(client)
    _enable_downstream(client)
    _admit(client)
    row = _outbound(client)[0]
    r = client.post(f"/interop/dead-letters/{row['id']}/replay", headers=ADMIN)
    assert r.status_code == 409, r.text
    assert client.post("/interop/dead-letters/99999/replay",
                       headers=ADMIN).status_code == 404
    assert client.get("/interop/dead-letters", headers=ADMIN).json()["count"] == 0


# ---------------------------------------------------------------------------
# FHIR bulk export
# ---------------------------------------------------------------------------

def test_bulk_export_job_files_and_privacy(client):
    _patient(client, "pat-1")
    _patient(client, "pat-2")
    r = client.post("/fhir/Condition", json={
        "resourceType": "Condition", "id": "cond-1",
        "subject": {"reference": "Patient/pat-1"},
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10",
                             "code": "E11", "display": "Type 2 diabetes"}]}},
        headers=ADMIN)
    assert r.status_code == 201, r.text

    # The capability gate: clinicians/viewers/patients may not export.
    assert client.post("/interop/$export", json={},
                       headers=CLINICIAN).status_code == 403
    assert client.post("/interop/$export", json={},
                       headers=VIEWER).status_code == 403

    r = client.post("/interop/$export?wait=true",
                    json={"resourceTypes": ["Patient", "Condition"]},
                    headers=ADMIN)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    status = client.get(f"/interop/export-jobs/{job_id}", headers=ADMIN)
    assert status.status_code == 200
    body = status.json()
    assert body["status"] == "complete"
    counts = {f["resource_type"]: f["count"] for f in body["files"]}
    assert counts == {"Patient": 2, "Condition": 1}
    # The status surface carries counts only — no patient data inline.
    assert "Synthetic8" not in status.text and "MRN-pat-1" not in status.text

    r = client.get(f"/interop/export-jobs/{job_id}/files/Patient",
                   headers=ADMIN)
    assert r.status_code == 200
    assert "application/fhir+ndjson" in r.headers["content-type"]
    lines = [json.loads(ln) for ln in r.text.strip().split("\n")]
    assert {ln["id"] for ln in lines} == {"pat-1", "pat-2"}
    assert all(ln["resourceType"] == "Patient" for ln in lines)
    r = client.get(f"/interop/export-jobs/{job_id}/files/Condition",
                   headers=ADMIN)
    lines = [json.loads(ln) for ln in r.text.strip().split("\n")]
    assert len(lines) == 1 and lines[0]["id"] == "cond-1"
    # Downloads are gated + audited; unknown job/file are honest 404s.
    assert client.get(f"/interop/export-jobs/{job_id}/files/Patient",
                      headers=CLINICIAN).status_code == 403
    assert client.get("/interop/export-jobs/nope/files/Patient",
                      headers=ADMIN).status_code == 404
    assert client.get(f"/interop/export-jobs/{job_id}/files/Encounter",
                      headers=ADMIN).status_code == 404


def test_bulk_export_rejects_unknown_types_and_async_shape(client):
    _patient(client)
    r = client.post("/interop/$export", json={"resourceTypes": ["Claim"]},
                    headers=ADMIN)
    assert r.status_code == 422, r.text
    # Async shape: 202 + job that reaches complete via the status URL.
    r = client.post("/interop/$export", json={"resourceTypes": ["Patient"]},
                    headers=ADMIN)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert r.json()["status_url"] == f"/interop/export-jobs/{job_id}"
    import time
    deadline = time.time() + 15
    body = {}
    while time.time() < deadline:
        body = client.get(f"/interop/export-jobs/{job_id}",
                          headers=ADMIN).json()
        if body["status"] in ("complete", "error"):
            break
        time.sleep(0.1)
    assert body["status"] == "complete", body
    assert body["files"][0]["count"] == 1
