"""Phase 16 WS-B — External gateway legs (app.gateway).

BD-38 aggregate public-health extracts + gateway submission,
BD-39 clearinghouse submission (837 out, 999/277 ack back), and
BD-40 payer EDI 270/271 + Surescripts NewRx.

All three are "built to the interface, live leg external": the
gateway / clearinghouse / payer / Surescripts endpoints in these
tests are LOCAL FIXTURE HTTP servers (stdlib http.server in a
thread) published into Studio integration config by the tests —
the production legs are NOT RUN. Extract math is seeded exactly
(6 diabetes patients, all with a latest HbA1c < 8.0 -> the
hba1c-control measure is 6/6 = 100.0 and the diabetes cohort
member count is 6), run through the real ETL into the warehouse,
mirroring test_phase9_quality's seeding pattern.

NOTE (integration): the coordinator mounts app.gateway's router
and calls ensure_gateway_defaults centrally. This file uses the
standard conftest `client` fixture, so it passes at the
coordinator's post-integration gate.
"""
import json
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import select

from app.audit import verify_chain
from app.models import AuditLog, EligibilityCheck
from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER

SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
CPT = "http://www.ama-assn.org/go/cpt"
SCT = "http://snomed.info/sct"
T2DM = "44054006"


# ---------------------------------------------------------------------------
# Local fixture HTTP servers (gateway / clearinghouse / payer / erx)
# ---------------------------------------------------------------------------

class _Fixture:
    """One threaded HTTP fixture server. `responders` maps a path
    to a callable(body_text) -> (status, content_type, body). Every
    request is captured in `received` for structure assertions."""

    def __init__(self):
        self.received: list[dict] = []
        self.responders: dict = {}
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 (stdlib name)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8", "replace")
                fixture.received.append(
                    {"path": self.path, "body": body,
                     "content_type": self.headers.get("Content-Type")})
                responder = fixture.responders.get(self.path)
                if responder is None:
                    status, ctype, text = 404, "text/plain", "no fixture route"
                else:
                    status, ctype, text = responder(body)
                data = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # keep the suite quiet
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def fixture_server():
    server = _Fixture()
    yield server
    server.stop()


# ---------------------------------------------------------------------------
# Studio config + chart helpers
# ---------------------------------------------------------------------------

def _publish(client, kind, key, payload, headers=ADMIN):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload,
                    headers=headers)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/{kind}/{key}/versions/{version}/publish",
        headers=headers)
    assert r.status_code == 200, r.text
    return version


def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Gateway", "given": ["Synth"]}],
        "gender": "female", "birthDate": "1985-06-15"}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _condition(client, pid, code=T2DM):
    r = client.post("/fhir/Condition", json={
        "resourceType": "Condition",
        "code": {"coding": [{"system": SNOMED, "code": code}]},
        "subject": {"reference": f"Patient/{pid}"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _observation(client, pid, code, value, when="2026-01-10T09:00:00Z"):
    r = client.post("/fhir/Observation", json={
        "resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": LOINC, "code": code}]},
        "subject": {"reference": f"Patient/{pid}"},
        "effectiveDateTime": when,
        "valueQuantity": {"value": value, "unit": "%"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


@pytest.fixture()
def warehouse_data(client, monkeypatch, tmp_path):
    """6 diabetes patients, every latest HbA1c < 8.0, ETL'd into the
    warehouse: hba1c-control = 6/6 (100.0), diabetes cohort = 6."""
    monkeypatch.setenv("WAREHOUSE_DATABASE_URL",
                       f"sqlite:///{tmp_path}/wh.db")
    for i, a1c in enumerate([6.1, 6.5, 7.0, 7.2, 7.9, 6.8], start=1):
        pid = f"gw-dm-{i:02d}"
        _patient(client, pid)
        _condition(client, pid)
        _observation(client, pid, "4548-4", a1c)
    r = client.post("/warehouse/etl/run", headers=ADMIN)
    assert r.status_code == 200 and r.json()["dead_letter"] == 0, r.text
    return client


def _ph_config(url: str = "", max_attempts: int = 3) -> dict:
    return {"name": "Test public-health gateway",
            "gatewayUrl": url,
            "retry": {"maxAttempts": max_attempts, "backoffSeconds": 0},
            "aggregates": [
                {"name": "diabetes-hba1c-control",
                 "title": "Diabetes HbA1c control (aggregate)",
                 "sourceType": "measure", "sourceKey": "hba1c-control"},
                {"name": "diabetes-registry-count",
                 "title": "Diabetes registry count (aggregate)",
                 "sourceType": "cohort", "sourceKey": "diabetes"},
            ]}


# ---------------------------------------------------------------------------
# BD-38 — aggregate extracts
# ---------------------------------------------------------------------------

def test_ph_definitions_seeded_and_listed(client):
    r = client.get("/gateway/public-health/definitions", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    names = {d["name"] for d in r.json()["definitions"]}
    assert {"diabetes-hba1c-control", "hypertension-bp-control",
            "diabetes-registry-count",
            "hypertension-registry-count"} <= names
    # The seeded gateway URL is empty: nothing can leave by default.
    assert r.json()["gateway_url_configured"] is False
    assert client.get("/gateway/public-health/definitions",
                      headers=PATIENT).status_code == 403


def test_ph_extract_measure_math_from_live_data(warehouse_data):
    r = warehouse_data.post(
        "/gateway/public-health/extract",
        json={"definition": "diabetes-hba1c-control", "period": "2026-01"},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["definition"] == "diabetes-hba1c-control"
    assert out["period"] == "2026-01"
    assert out["source"] == {"type": "measure", "key": "hba1c-control"}
    assert out["contains_patient_data"] is False
    agg = out["aggregate"]
    assert agg["denominator"] == 6 and agg["numerator"] == 6
    assert agg["rate"] == 100.0 and agg["suppressed"] is False
    # No patient-level data anywhere in the extract.
    assert "gw-dm-01" not in json.dumps(out) and "Patient/" not in json.dumps(out)


def test_ph_extract_cohort_count_from_live_data(warehouse_data):
    r = warehouse_data.post(
        "/gateway/public-health/extract",
        json={"definition": "diabetes-registry-count", "period": "2026-01"},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["aggregate"]["member_count"] == 6
    assert r.json()["aggregate"]["registry"] == "diabetes"


def test_ph_extract_unknown_definition_404_and_missing_422(client):
    r = client.post("/gateway/public-health/extract",
                    json={"definition": "nope"}, headers=CLINICIAN)
    assert r.status_code == 404
    r = client.post("/gateway/public-health/extract", json={},
                    headers=CLINICIAN)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# BD-38 — gateway submission: round-trip, queued honesty, dead-letter
# ---------------------------------------------------------------------------

def test_ph_submit_queued_for_gateway_when_unconfigured(warehouse_data):
    r = warehouse_data.post(
        "/gateway/public-health/submit",
        json={"definition": "diabetes-hba1c-control", "period": "2026-01"},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    body = r.json()
    # Honest posture (the platform_depth VXU pattern): recorded
    # locally as queued-for-gateway, never claimed sent.
    assert body["status"] == "queued-for-gateway"
    assert body["attempts"] == 0 and body["attempt_history"] == []
    assert "NOT sent" in body["detail"]["not_sent_reason"]
    listed = warehouse_data.get("/gateway/public-health/submissions",
                                headers=ADMIN).json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == body["id"]


def test_ph_submit_round_trip_to_fixture_gateway(warehouse_data,
                                                 fixture_server):
    fixture_server.responders["/ph"] = lambda body: (
        200, "application/json", json.dumps({"received": True}))
    _publish(warehouse_data, "integration", "public-health",
             _ph_config(url=fixture_server.url("/ph")))
    r = warehouse_data.post(
        "/gateway/public-health/submit",
        json={"definition": "diabetes-hba1c-control", "period": "2026-01"},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "sent" and body["attempts"] == 1
    assert body["attempt_history"][0]["outcome"] == "sent"
    # The fixture received the aggregate extract as JSON — counts only.
    assert len(fixture_server.received) == 1
    posted = json.loads(fixture_server.received[0]["body"])
    assert posted["definition"] == "diabetes-hba1c-control"
    assert posted["aggregate"]["denominator"] == 6
    assert posted["contains_patient_data"] is False
    # Status is retrievable through the submissions surface.
    got = warehouse_data.get(
        f"/gateway/public-health/submissions/{body['id']}", headers=ADMIN)
    assert got.status_code == 200 and got.json()["status"] == "sent"


def test_ph_submit_dead_letters_after_retry_budget(warehouse_data,
                                                   fixture_server):
    fixture_server.responders["/ph"] = lambda body: (
        500, "text/plain", "gateway unavailable")
    _publish(warehouse_data, "integration", "public-health",
             _ph_config(url=fixture_server.url("/ph"), max_attempts=3))
    r = warehouse_data.post(
        "/gateway/public-health/submit",
        json={"definition": "diabetes-registry-count", "period": "2026-01"},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    sub_id = r.json()["id"]
    assert r.json()["status"] == "pending" and r.json()["attempts"] == 1
    # Two dispatcher passes exhaust the budget of 3 -> dead letter.
    r = warehouse_data.post("/gateway/public-health/dispatch",
                            headers=ADMIN)
    assert r.status_code == 200 and r.json()["stats"]["attempted"] == 1
    r = warehouse_data.post("/gateway/public-health/dispatch",
                            headers=ADMIN)
    assert r.json()["stats"]["dead_lettered"] == 1
    got = warehouse_data.get(
        f"/gateway/public-health/submissions/{sub_id}",
        headers=ADMIN).json()
    assert got["status"] == "dead_letter" and got["attempts"] == 3
    # Attempts are append-only history: 3 rows, never rewritten.
    assert [a["attempt_no"] for a in got["attempt_history"]] == [1, 2, 3]
    assert got["attempt_history"][-1]["outcome"] == "dead_letter"
    assert len(fixture_server.received) == 3
    # A further pass attempts nothing (dead letters stay dead).
    r = warehouse_data.post("/gateway/public-health/dispatch",
                            headers=ADMIN)
    assert r.json()["stats"]["attempted"] == 0


# ---------------------------------------------------------------------------
# BD-39 — clearinghouse submission
# ---------------------------------------------------------------------------

def _chart_claim(client, pid="pat-1", eid="enc-1"):
    _patient(client, pid)
    r = client.post("/fhir/Encounter", json={
        "resourceType": "Encounter", "id": eid, "status": "finished",
        "subject": {"reference": f"Patient/{pid}"},
        "period": {"start": "2026-01-10T09:00:00Z",
                   "end": "2026-01-10T10:00:00Z"}}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/fhir/Condition", json={
        "resourceType": "Condition", "id": f"cond-{pid}",
        "code": {"coding": [{"system": SCT, "code": T2DM,
                             "display": "Type 2 diabetes mellitus"}]},
        "subject": {"reference": f"Patient/{pid}"}}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/fhir/Procedure", json={
        "resourceType": "Procedure", "id": f"proc-{pid}",
        "status": "completed",
        "code": {"coding": [{"system": CPT, "code": "93005",
                             "display": "Electrocardiogram"}]},
        "subject": {"reference": f"Patient/{pid}"},
        "encounter": {"reference": f"Encounter/{eid}"}}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/billing/claims/generate",
                    json={"encounter_id": eid}, headers=ADMIN)
    assert r.status_code == 201, r.text
    claim_id = r.json()["id"]
    for target in ("ready", "submitted"):
        r = client.post(f"/billing/claims/{claim_id}/transition",
                        json={"to_status": target}, headers=ADMIN)
        assert r.status_code == 200, r.text
    return claim_id


def _ch_config(url: str = "", max_attempts: int = 3) -> dict:
    return {"name": "Test clearinghouse", "endpointUrl": url,
            "retry": {"maxAttempts": max_attempts, "backoffSeconds": 0}}


ACK_999_ACCEPTED = (
    "ISA*00*          *00*          *ZZ*CLEARINGHOUSE  *ZZ*OPENCARE       "
    "*261019*1200*^*00501*000000001*0*T*:~\n"
    "GS*FA*CLEARINGHOUSE*OPENCARE*20261019*1200*1*X*005010X231A1~\n"
    "ST*999*0001*005010X231A1~\nAK1*HC*1~\nAK9*A*1*1*1~\nSE*4*0001~\n"
    "GE*1*1~\nIEA*1*000000001~\n")

ACK_277_REJECTED = (
    "ISA*00*          *00*          *ZZ*CLEARINGHOUSE  *ZZ*OPENCARE       "
    "*261019*1200*^*00501*000000002*0*T*:~\n"
    "GS*HN*CLEARINGHOUSE*OPENCARE*20261019*1200*1*X*005010X214~\n"
    "ST*277*0001*005010X214~\nBHT*0085*08*277-TEST*20261019*1200~\n"
    "STC*A3:87:78*20261019********Invalid diagnosis code for claim~\n"
    "SE*5*0001~\nGE*1*1~\nIEA*1*000000002~\n")


def test_clearinghouse_submit_queued_when_unconfigured(client):
    claim_id = _chart_claim(client)
    r = client.post("/gateway/clearinghouse/submit",
                    json={"claim_id": claim_id}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "queued-for-gateway"
    assert r.json()["attempts"] == 0
    # The stored request payload IS the 837 export.
    assert r.json()["request_payload"].startswith("ISA*")
    assert "ST*837*0001" in r.json()["request_payload"]


def test_clearinghouse_accepted_round_trip_records_on_claim(
        client, fixture_server):
    claim_id = _chart_claim(client)
    fixture_server.responders["/ch"] = lambda body: (
        200, "text/plain", ACK_999_ACCEPTED)
    _publish(client, "integration", "clearinghouse",
             _ch_config(url=fixture_server.url("/ch")))
    r = client.post("/gateway/clearinghouse/submit",
                    json={"claim_id": claim_id}, headers=ADMIN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "accepted" and body["attempts"] == 1
    assert body["detail"]["ack"]["ack_type"] == "999"
    assert body["detail"]["ack"]["accepted"] is True
    # The clearinghouse received the claim's 837 (same bytes as export).
    sent = fixture_server.received[0]["body"]
    exported = client.get(f"/billing/claims/{claim_id}/export/837",
                          headers=ADMIN).text
    assert sent == exported
    # The outcome is recorded ON THE CLAIM (supportingInfo entry).
    claim = client.get(f"/fhir/Claim/{claim_id}", headers=ADMIN).json()
    entries = []
    for info in claim.get("supportingInfo") or []:
        coding = (info.get("category") or {}).get("coding") or [{}]
        if coding[0].get("code") == "clearinghouse":
            entries.append(json.loads(info["valueString"]))
    assert len(entries) == 1
    assert entries[0]["outcome"] == "accepted"
    assert entries[0]["submission_id"] == body["id"]
    # Claim lifecycle untouched (acceptance != adjudication).
    assert claim["status"] == "submitted"


def test_clearinghouse_rejected_round_trip_records_reasons(
        client, fixture_server):
    claim_id = _chart_claim(client)
    fixture_server.responders["/ch"] = lambda body: (
        200, "text/plain", ACK_277_REJECTED)
    _publish(client, "integration", "clearinghouse",
             _ch_config(url=fixture_server.url("/ch")))
    r = client.post("/gateway/clearinghouse/submit",
                    json={"claim_id": claim_id}, headers=ADMIN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "rejected"
    assert body["detail"]["ack"]["ack_type"] == "277"
    assert any("Invalid diagnosis code" in reason
               for reason in body["detail"]["ack"]["reasons"])
    claim = client.get(f"/fhir/Claim/{claim_id}", headers=ADMIN).json()
    recorded = [json.loads(info["valueString"])
                for info in claim.get("supportingInfo") or []
                if ((info.get("category") or {}).get("coding") or [{}])[0]
                .get("code") == "clearinghouse"]
    assert recorded and recorded[0]["outcome"] == "rejected"
    assert any("Invalid diagnosis code" in reason
               for reason in recorded[0]["reasons"])


def test_clearinghouse_transport_failure_retries_then_dead_letters(
        client, fixture_server):
    claim_id = _chart_claim(client)
    fixture_server.responders["/ch"] = lambda body: (
        500, "text/plain", "clearinghouse down")
    _publish(client, "integration", "clearinghouse",
             _ch_config(url=fixture_server.url("/ch"), max_attempts=2))
    r = client.post("/gateway/clearinghouse/submit",
                    json={"claim_id": claim_id}, headers=ADMIN)
    # A transport failure is NOT a business rejection.
    assert r.json()["status"] == "pending"
    sub_id = r.json()["id"]
    r = client.post("/gateway/clearinghouse/dispatch", headers=ADMIN)
    assert r.json()["stats"]["dead_lettered"] == 1
    got = client.get(f"/gateway/clearinghouse/submissions/{sub_id}",
                     headers=ADMIN).json()
    assert got["status"] == "dead_letter" and got["attempts"] == 2
    # No ack was ever recorded on the claim.
    claim = client.get(f"/fhir/Claim/{claim_id}", headers=ADMIN).json()
    assert not any(
        ((info.get("category") or {}).get("coding") or [{}])[0]
        .get("code") == "clearinghouse"
        for info in claim.get("supportingInfo") or [])


def test_clearinghouse_submit_requires_exportable_claim(client):
    _patient(client)
    r = client.post("/fhir/Encounter", json={
        "resourceType": "Encounter", "id": "enc-d", "status": "finished",
        "subject": {"reference": "Patient/pat-1"},
        "period": {"start": "2026-01-10T09:00:00Z",
                   "end": "2026-01-10T10:00:00Z"}}, headers=ADMIN)
    assert r.status_code == 201
    _condition(client, "pat-1")
    r = client.post("/billing/claims/generate",
                    json={"encounter_id": "enc-d"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    draft_id = r.json()["id"]  # still a draft — not exportable
    r = client.post("/gateway/clearinghouse/submit",
                    json={"claim_id": draft_id}, headers=ADMIN)
    assert r.status_code == 409
    r = client.post("/gateway/clearinghouse/submit",
                    json={"claim_id": "no-such-claim"}, headers=ADMIN)
    assert r.status_code == 404
    r = client.post("/gateway/clearinghouse/submit", json={},
                    headers=ADMIN)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# BD-40 — X12 270/271 eligibility
# ---------------------------------------------------------------------------

EDI_271_ACTIVE = (
    "ISA*00*          *00*          *ZZ*SYNTHPAYER     *ZZ*OPENCARE       "
    "*261020*1200*^*00501*000000101*0*T*:~\n"
    "GS*HB*SYNTHPAYER*OPENCARE*20261020*1200*1*X*005010X279A1~\n"
    "ST*271*0001*005010X279A1~\nBHT*0022*11*RESP-1*20261020*1200~\n"
    "HL*1**20*1~\nNM1*PR*2*SYNTHETIC PAYER*****PI*SYNTHPAYER~\n"
    "HL*2*1*21*1~\nHL*3*2*22*0~\nTRN*2*EDI-TRACE*OPENCARE~\n"
    "NM1*IL*1*GATEWAY*SYNTH****MI*pat-1~\n"
    "EB*1*FAM*30~\nEB*B*IND*30****25.00~\nEB*A*IND*30*****0.20~\n"
    "DTP*291*D8*20261020~\nSE*13*0001~\nGE*1*1~\nIEA*1*000000101~\n")

EDI_271_INACTIVE = EDI_271_ACTIVE.replace("EB*1*FAM*30~", "EB*6*FAM*30~")


def _edi_config(edi_url: str = "", erx_url: str = "") -> dict:
    return {"name": "Test EDI", "retry": {"maxAttempts": 3,
                                           "backoffSeconds": 0},
            "payers": [{"code": "synthetic-payer", "endpointUrl": edi_url,
                        "senderId": "OPENCARE", "receiverId": "SYNTHPAYER"},
                       {"code": "synthetic-payer-plus", "endpointUrl": "",
                        "senderId": "OPENCARE",
                        "receiverId": "SYNTHPAYERPLUS"}],
            "surescripts": {"endpointUrl": erx_url, "senderId": "OPENCARE"}}


def test_edi_unconfigured_is_honest_503_and_records_nothing(client):
    _patient(client)
    r = client.post("/gateway/eligibility/edi",
                    json={"patient_id": "pat-1", "payer": "synthetic-payer"},
                    headers=CLINICIAN)
    assert r.status_code == 503, r.text
    assert "NOT RUN" in r.json()["detail"]
    checks = client.get("/access/eligibility/checks?patient=pat-1",
                        headers=ADMIN).json()
    assert checks["total"] == 0


def test_edi_270_271_round_trip_parses_into_payer_edi_record(
        client, fixture_server):
    _patient(client)
    fixture_server.responders["/edi"] = lambda body: (
        200, "text/plain", EDI_271_ACTIVE)
    _publish(client, "integration", "edi",
             _edi_config(edi_url=fixture_server.url("/edi")))
    r = client.post("/gateway/eligibility/edi",
                    json={"patient_id": "pat-1", "payer": "synthetic-payer",
                          "service_code": "30", "service_date": "2026-10-20"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    # Parsed 271 fields in the Phase 7 eligibility record shape.
    assert body["covered"] is True and body["reason"] == "active-coverage"
    assert body["copay"] == 25.0 and body["coinsurance_percent"] == 20.0
    assert body["payer_display"] == "SYNTHETIC PAYER"
    assert body["basis"] == "payer-edi"
    # The generated 270 went to the fixture: well-formed envelope,
    # patient + payer named, SE count reconciles.
    sent = fixture_server.received[0]["body"]
    assert "ST*270*0001*005010X279A1" in sent
    assert "NM1*IL*1*GATEWAY*SYNTH" in sent and "pat-1" in sent
    assert "NM1*PR*2*Synthetic Payer" in sent
    assert "EQ*30" in sent
    segments = [s for s in sent.replace("\n", "").split("~") if s.strip()]
    st_i = next(i for i, s in enumerate(segments) if s.startswith("ST*"))
    se_i = next(i for i, s in enumerate(segments) if s.startswith("SE*"))
    assert int(segments[se_i].split("*")[1]) == se_i - st_i + 1
    # The record persisted in the Phase 7 table, retrievable through
    # the ordinary access list, basis intact.
    checks = client.get("/access/eligibility/checks?patient=pat-1",
                        headers=ADMIN).json()
    assert checks["total"] == 1
    assert checks["items"][0]["id"] == body["id"]
    assert checks["items"][0]["basis"] == "payer-edi"
    with client.app.state.session_factory() as session:
        row = session.scalars(select(EligibilityCheck)).first()
        assert row.detail["basis"] == "payer-edi"
        assert row.copay == 25.0


def test_edi_inactive_271_and_basis_never_conflated_with_local(
        client, fixture_server):
    _patient(client)
    fixture_server.responders["/edi"] = lambda body: (
        200, "text/plain", EDI_271_INACTIVE)
    _publish(client, "integration", "edi",
             _edi_config(edi_url=fixture_server.url("/edi")))
    # A Phase 7 LOCAL check for the same patient/payer first.
    local = client.post("/access/eligibility/check", json={
        "patient": "pat-1", "payer": "synthetic-payer"}, headers=ADMIN)
    assert local.status_code == 201
    assert local.json()["basis"] == "local-config"
    assert local.json()["covered"] is True  # local config says covered
    # The payer's 271 says INACTIVE — a different answer, a different
    # basis, recorded side by side and never merged.
    r = client.post("/gateway/eligibility/edi",
                    json={"patient_id": "pat-1", "payer": "synthetic-payer"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["covered"] is False
    assert r.json()["reason"] == "inactive-coverage"
    assert r.json()["basis"] == "payer-edi"
    checks = client.get("/access/eligibility/checks?patient=pat-1",
                        headers=ADMIN).json()
    bases = sorted(item["basis"] for item in checks["items"])
    assert bases == ["local-config", "payer-edi"]


def test_edi_unknown_payer_422_and_endpoint_failure_records_nothing(
        client, fixture_server):
    _patient(client)
    r = client.post("/gateway/eligibility/edi",
                    json={"patient_id": "pat-1", "payer": "no-such-payer"},
                    headers=CLINICIAN)
    assert r.status_code == 422
    fixture_server.responders["/edi"] = lambda body: (
        500, "text/plain", "payer test endpoint down")
    _publish(client, "integration", "edi",
             _edi_config(edi_url=fixture_server.url("/edi")))
    r = client.post("/gateway/eligibility/edi",
                    json={"patient_id": "pat-1", "payer": "synthetic-payer"},
                    headers=CLINICIAN)
    assert r.status_code == 503
    assert "no eligibility record" in r.json()["detail"]
    checks = client.get("/access/eligibility/checks?patient=pat-1",
                        headers=ADMIN).json()
    assert checks["total"] == 0


# ---------------------------------------------------------------------------
# BD-40 — Surescripts NCPDP SCRIPT NewRx
# ---------------------------------------------------------------------------

NEWRX_BODY = {
    "patient_id": "pat-1",
    "medication": {"description": "Metformin hydrochloride 500 MG Oral Tablet",
                   "product_code": "847232", "quantity": 60,
                   "days_supply": 30,
                   "directions": "Take one tablet by mouth twice daily",
                   "substitutions": 0, "refills": 3},
    "prescriber": {"last_name": "Synth", "first_name": "Prescriber",
                   "npi": "1234567890"},
    "pharmacy": {"name": "Synthetic Pharmacy", "ncpdp_id": "9999999"},
}

ERX_ACCEPTED = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Message xmlns="http://www.ncpdp.org/schema/SCRIPT"><Body>'
    "<Status><Code>000</Code><Description>Success</Description></Status>"
    "</Body></Message>")

ERX_REJECTED = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Message xmlns="http://www.ncpdp.org/schema/SCRIPT"><Body>'
    "<Error><Code>550</Code><Description>Unknown pharmacy NCPDPID"
    "</Description></Error></Body></Message>")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_all(root, name):
    return [n for n in root.iter() if _local(n.tag) == name]


def test_newrx_queued_for_gateway_when_unconfigured(client):
    _patient(client)
    r = client.post("/gateway/erx/newrx", json=NEWRX_BODY, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "queued-for-gateway"
    assert "NOT sent" in body["detail"]["not_sent_reason"]
    # The built XML is still returned for inspection: well-formed
    # SCRIPT with the required NewRx structure.
    root = ET.fromstring(body["request_xml"])
    assert _local(root.tag) == "Message"
    assert len(_find_all(root, "NewRx")) == 1
    med = _find_all(root, "MedicationPrescribed")[0]
    texts = {_local(n.tag): n.text for n in med.iter()}
    assert texts["DrugDescription"].startswith("Metformin")
    assert texts["ProductCode"] == "847232"
    assert texts["Quantity"] == "60" and texts["DaysSupply"] == "30"
    assert "twice daily" in texts["Directions"]
    patient = _find_all(root, "Patient")[0]
    ptexts = {_local(n.tag): n.text for n in patient.iter()}
    assert ptexts["LastName"] == "Gateway"
    assert ptexts["DateOfBirth"] == "1985-06-15"
    assert len(_find_all(root, "Prescriber")) == 1
    assert len(_find_all(root, "Pharmacy")) == 1


def test_newrx_round_trip_accepted(client, fixture_server):
    _patient(client)
    fixture_server.responders["/erx"] = lambda body: (
        200, "application/xml", ERX_ACCEPTED)
    _publish(client, "integration", "edi",
             _edi_config(erx_url=fixture_server.url("/erx")))
    r = client.post("/gateway/erx/newrx", json=NEWRX_BODY, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["detail"]["response"]["code"] == "000"
    # The fixture received exactly the built XML.
    assert fixture_server.received[0]["body"] == body["request_xml"]
    assert "application/xml" in fixture_server.received[0]["content_type"]
    # The transmission is recorded and listed.
    listed = client.get("/gateway/erx/transmissions", headers=ADMIN).json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == body["id"]
    assert listed["items"][0]["status"] == "accepted"


def test_newrx_round_trip_rejected_with_reason(client, fixture_server):
    _patient(client)
    fixture_server.responders["/erx"] = lambda body: (
        200, "application/xml", ERX_REJECTED)
    _publish(client, "integration", "edi",
             _edi_config(erx_url=fixture_server.url("/erx")))
    r = client.post("/gateway/erx/newrx", json=NEWRX_BODY, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "rejected"
    detail = r.json()["detail"]["response"]
    assert detail["code"] == "550"
    assert "Unknown pharmacy" in detail["description"]


def test_newrx_validation(client):
    _patient(client)
    r = client.post("/gateway/erx/newrx",
                    json={"patient_id": "pat-1"}, headers=CLINICIAN)
    assert r.status_code == 422  # no medication
    r = client.post("/gateway/erx/newrx", json={
        "patient_id": "pat-1",
        "medication": {"description": "X", "quantity": 1}}, headers=CLINICIAN)
    assert r.status_code == 422  # no prescriber
    r = client.post("/gateway/erx/newrx",
                    json={**NEWRX_BODY, "patient_id": "ghost"},
                    headers=CLINICIAN)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Cross-cutting: capability gates + audit chain
# ---------------------------------------------------------------------------

def test_gateway_capability_gates(client):
    assert client.get("/gateway/public-health/submissions",
                      headers=PATIENT).status_code == 403
    assert client.post("/gateway/public-health/submit",
                       json={"definition": "diabetes-registry-count"},
                       headers=CLINICIAN).status_code == 403  # interop.manage
    assert client.post("/gateway/clearinghouse/submit",
                       json={"claim_id": "x"},
                       headers=CLINICIAN).status_code == 403  # billing.manage
    assert client.post("/gateway/eligibility/edi",
                       json={"patient_id": "pat-1", "payer": "synthetic-payer"},
                       headers=VIEWER).status_code == 403  # access.work
    assert client.post("/gateway/erx/newrx", json=NEWRX_BODY,
                       headers=VIEWER).status_code == 403  # orders.place


def test_gateway_writes_are_audited_with_intact_chain(
        warehouse_data, fixture_server):
    fixture_server.responders["/ph"] = lambda body: (
        200, "application/json", "{}")
    _publish(warehouse_data, "integration", "public-health",
             _ph_config(url=fixture_server.url("/ph")))
    warehouse_data.post("/gateway/public-health/extract",
                        json={"definition": "diabetes-registry-count"},
                        headers=CLINICIAN)
    warehouse_data.post("/gateway/public-health/submit",
                        json={"definition": "diabetes-registry-count"},
                        headers=ADMIN)
    with warehouse_data.app.state.session_factory() as session:
        actions = [row.action for row in session.scalars(
            select(AuditLog).order_by(AuditLog.id.asc())).all()]
        assert verify_chain(session) is True
    for expected in ("gateway.public_health.extract",
                     "gateway.public_health.submit"):
        assert expected in actions
