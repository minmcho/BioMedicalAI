"""Phase 15 — Access services (app.access_services):
BD-21 utilisation review, BD-22 online bill pay, BD-23 portal
proxy access, BD-28 telehealth at scale, BD-31 credential-gated
scheduling. Synthetic data only.
"""
from datetime import datetime, timedelta, timezone

from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER

SCT = "http://snomed.info/sct"
CPT = "http://www.ama-assn.org/go/cpt"
LOINC = "http://loinc.org"


def _today():
    return datetime.now(timezone.utc).date()


def _date(offset_days):
    return (_today() + timedelta(days=offset_days)).isoformat()


def _audit(client, action):
    from sqlalchemy import select
    from app.models import AuditLog
    factory = client.app.state.session_factory
    with factory() as session:
        rows = session.scalars(select(AuditLog).where(
            AuditLog.action == action)).all()
        return [{"actor": r.actor, "entity": r.entity,
                 "payload": r.payload} for r in rows]


# ---------------------------------------------------------------------------
# Chart / claim / 835 helpers (mirroring tests/test_phase7_revenue.py)
# ---------------------------------------------------------------------------

def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic", "given": ["Access"]}],
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


def _claim(client, eid, submit=True):
    r = client.post("/billing/claims/generate", json={"encounter_id": eid},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    claim = r.json()
    if submit:
        for target in ("ready", "submitted"):
            r = client.post(f"/billing/claims/{claim['id']}/transition",
                            json={"to_status": target}, headers=ADMIN)
            assert r.status_code == 200, r.text
            claim = r.json()
    return claim


def _835(clp_blocks, check="CHK-P15", total="180.00"):
    segments = ["ST*835*0001", "BHT*0019*00*REF123*20261017*1200",
                f"BPR*I*{total}*C*ACH",
                "N1*PR*SYNTHETIC PAYER*****46*SYNTH-PAYER",
                f"TRN*1*{check}*123456789"]
    for block in clp_blocks:
        segments.extend(block)
    segments.append(f"SE*{len(segments) + 1}*0001")
    envelope = ["ISA*00*          *00*          *ZZ*OPENCARE       "
                "*ZZ*SYNTHPAYER     *261017*1200*^*00501*000000001*0*T*:",
                "GS*HP*OPENCARE*SYNTHPAYER*20261017*1200*1*X*005010X221A1"]
    return "".join(f"{s}~\n" for s in envelope + segments
                   + ["GE*1*1", "IEA*1*000000001"])


def _partial_claim(client, pid, eid, check):
    """240.00 claim, 180 paid, 30 contractual, 20 patient responsibility."""
    _chart(client, pid, eid, "55822004", proc_code="20610")
    claim = _claim(client, eid)
    assert claim["total"]["value"] == 240.00
    text = _835([[f"CLP*{claim['id']}*1*240.00*180.00*20.00**MB*R-1",
                  "SVC*HC:20610*210.00*180.00", "CAS*CO*45*30.00",
                  "CAS*PR*1*20.00"]], check=check)
    r = client.post("/billing/remittances", content=text,
                    headers={**ADMIN, "Content-Type": "text/plain"})
    assert r.status_code == 201, r.text
    return claim


def _telehealth_appointment(client, pid, when="2026-11-02T15:00:00Z"):
    body = {"resourceType": "Appointment", "status": "booked",
            "appointmentType": {"coding": [
                {"system": "urn:opencare:slot-types", "code": "telehealth"}]},
            "start": when, "end": "2026-11-02T15:30:00Z",
            "participant": [{"actor": {"reference": f"Patient/{pid}"},
                             "status": "accepted"}]}
    r = client.post("/fhir/Appointment", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Seeded governed config
# ---------------------------------------------------------------------------

def test_seeded_config_is_fixture_labelled_and_test_mode(client):
    ur = client.get("/studio/config/terminology/effective/ur-criteria",
                    headers=ADMIN).json()
    assert "SITE-FIXTURE" in ur["payload"]["name"]
    concept = ur["payload"]["concepts"][0]
    assert concept["fixture"] is True
    assert "not licensed" in concept["description"]
    assert concept["requiredEvidence"]
    provider = client.get("/studio/config/integration/effective/payment-provider",
                          headers=ADMIN).json()
    assert provider["payload"]["mode"] == "test"
    assert provider["payload"]["provider"] == "test-mode"
    assert provider["payload"]["live"] is False
    scopes = client.get("/studio/config/terminology/effective/proxy-scopes",
                        headers=ADMIN).json()
    assert {c["code"] for c in scopes["payload"]["concepts"]} == {
        "schedule", "messages", "results"}
    form = client.get("/studio/config/form/effective/telehealth-outcome",
                      headers=ADMIN).json()
    assert form["payload"]["title"].startswith("Telehealth visit outcome")


# ---------------------------------------------------------------------------
# BD-21 — Utilisation review
# ---------------------------------------------------------------------------

def _ur_case(client, with_claim=True):
    _chart(client, "pat-ur", "enc-ur", "44054006")
    claim = _claim(client, "enc-ur", submit=False) if with_claim else None
    body = {"patient": "pat-ur", "encounter_id": "enc-ur"}
    if claim:
        body["claim_id"] = claim["id"]
    r = client.post("/ur/cases", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json(), claim


def test_ur_case_review_and_advisor_split(client):
    case, claim = _ur_case(client)
    assert case["status"] == "pending"
    assert case["patient"] == "Patient/pat-ur"
    assert case["encounter_ref"] == "Encounter/enc-ur"
    assert case["claim_id"] == claim["id"]
    assert case["claim_ref"] == f"Claim/{claim['id']}"
    cid = case["case_id"]
    # Gates: viewer holds neither ur.read nor ur.work.
    assert client.get("/ur/cases", headers=VIEWER).status_code == 403
    # pending -> approved directly is illegal (must pass in-review).
    r = client.post(f"/ur/cases/{cid}/review",
                    json={"to_status": "approved",
                          "criteria_code": "site-fixture-med-nec-01",
                          "evidence_checked": ["clinical-notes"]},
                    headers=CLINICIAN)
    assert r.status_code == 409
    r = client.post(f"/ur/cases/{cid}/review",
                    json={"to_status": "in-review"}, headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["status"] == "in-review"
    # Evidence outside the criteria set's required list -> 422.
    r = client.post(f"/ur/cases/{cid}/review",
                    json={"to_status": "denial-risk",
                          "criteria_code": "site-fixture-med-nec-01",
                          "evidence_checked": ["not-a-field"]},
                    headers=CLINICIAN)
    assert r.status_code == 422 and "not-a-field" in r.json()["detail"]
    # Unknown (e.g. invented licensed) criteria -> 422.
    r = client.post(f"/ur/cases/{cid}/review",
                    json={"to_status": "denial-risk",
                          "criteria_code": "interqual-made-up",
                          "evidence_checked": []}, headers=CLINICIAN)
    assert r.status_code == 422
    # First reviewer marks denial-risk, recording criteria + evidence.
    r = client.post(f"/ur/cases/{cid}/review",
                    json={"to_status": "denial-risk",
                          "criteria_code": "site-fixture-med-nec-01",
                          "evidence_checked": ["diagnosis-documented",
                                               "clinical-notes"]},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "denial-risk"
    assert body["first_reviewer"] == "dev-clinician"
    assert body["criteria_code"] == "site-fixture-med-nec-01"
    assert body["evidence_checked"] == ["diagnosis-documented", "clinical-notes"]
    assert body["advisor_review_required"] is True
    # The denial-risk join feed carries the revenue join refs.
    feed = client.get("/ur/denial-risk", headers=ADMIN).json()
    assert feed["total"] == 1
    assert feed["items"][0]["claim_id"] == claim["id"]
    assert feed["items"][0]["encounter_ref"] == "Encounter/enc-ur"
    assert "billing/denials" in feed["joins_with"]
    # Status filter.
    assert client.get("/ur/cases?status=denial-risk",
                      headers=ADMIN).json()["total"] == 1
    assert client.get("/ur/cases?status=approved",
                      headers=ADMIN).json()["total"] == 0
    # Advisor == first reviewer -> 409 (break-glass split pattern).
    r = client.post(f"/ur/cases/{cid}/advisor-review",
                    json={"decision": "approved"}, headers=CLINICIAN)
    assert r.status_code == 409 and "must differ" in r.json()["detail"]
    # A different advisor approves; the case leaves the feed.
    r = client.post(f"/ur/cases/{cid}/advisor-review",
                    json={"decision": "approved"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert r.json()["advisor_reviewer"] == "dev-admin"
    assert client.get("/ur/denial-risk", headers=ADMIN).json()["total"] == 0
    # Advisor review is no longer available for a non-denial-risk case.
    r = client.post(f"/ur/cases/{cid}/advisor-review",
                    json={"decision": "approved"}, headers=ADMIN)
    assert r.status_code == 409


def test_ur_case_creation_validation(client):
    _patient(client, "pat-x")
    # Unknown patient / encounter -> 404; unknown criteria -> 422.
    assert client.post("/ur/cases", json={"patient": "pat-ghost"},
                       headers=CLINICIAN).status_code == 404
    assert client.post("/ur/cases", json={"patient": "pat-x",
                                          "encounter_id": "enc-ghost"},
                       headers=CLINICIAN).status_code == 404
    r = client.post("/fhir/Encounter", json={
        "resourceType": "Encounter", "id": "enc-x", "status": "in-progress",
        "subject": {"reference": "Patient/pat-x"}}, headers=ADMIN)
    assert r.status_code == 201
    assert client.post("/ur/cases", json={
        "patient": "pat-x", "encounter_id": "enc-x",
        "criteria_code": "nope"}, headers=CLINICIAN).status_code == 422


# ---------------------------------------------------------------------------
# BD-22 — Online bill pay
# ---------------------------------------------------------------------------

def test_portal_bill_pay_balance_payment_balance(client):
    claim = _partial_claim(client, "pat-1", "enc-pay", "CHK-PAY1")
    _partial_claim(client, "pat-2", "enc-pay2", "CHK-PAY2")
    # Own balance only: pat-2's claim never appears for the pat-1 token.
    r = client.get("/portal/billing/balance", headers=PATIENT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["patient"] == "Patient/pat-1"
    assert body["balance"] == 20.00 and body["patient_due"] == 20.00
    assert [c["claim_id"] for c in body["claims"]] == [claim["id"]]
    assert body["provider_mode"] == "test"
    # Another patient's balance is invisible (404), staff cannot use portal.
    assert client.get("/portal/billing/balance?patient=pat-2",
                      headers=PATIENT).status_code == 404
    assert client.get("/portal/billing/balance",
                      headers=CLINICIAN).status_code == 403
    # Invalid payments: zero amount, overpayment, unknown method.
    assert client.post("/portal/billing/payments",
                       json={"amount": 0, "method": "card"},
                       headers=PATIENT).status_code == 422
    assert client.post("/portal/billing/payments",
                       json={"amount": 25.00, "method": "card"},
                       headers=PATIENT).status_code == 422
    assert client.post("/portal/billing/payments",
                       json={"amount": 5.00, "method": "bitcoin"},
                       headers=PATIENT).status_code == 422
    # A test-mode payment lands in revenue's ledger; balance drops exactly.
    r = client.post("/portal/billing/payments",
                    json={"amount": 12.50, "method": "card",
                          "claim_id": claim["id"]}, headers=PATIENT)
    assert r.status_code == 201, r.text
    receipt = r.json()
    assert receipt["provider_mode"] == "test"
    assert receipt["provider"] == "test-mode"
    assert receipt["live"] is False
    assert "No live" in receipt["disclaimer"]
    assert receipt["amount"] == 12.50
    assert receipt["patient_due_after"] == 7.50
    after = client.get("/portal/billing/balance", headers=PATIENT).json()
    assert after["balance"] == 7.50
    # Revenue's own statement agrees — one ledger, no fork.
    stmt = client.get("/billing/statements/pat-1", headers=ADMIN).json()
    assert stmt["totals"]["patient_paid"] == 12.50
    assert stmt["totals"]["patient_due"] == 7.50
    payments = client.get(
        f"/billing/claims/{claim['id']}/patient-payments",
        headers=ADMIN).json()
    assert payments["total"] == 1
    assert payments["items"][0]["id"] == receipt["payment_id"]


# ---------------------------------------------------------------------------
# BD-23 — Portal proxy access
# ---------------------------------------------------------------------------

def _proxy_fixture(client):
    _patient(client, "pat-1")
    _patient(client, "pat-2")
    r = client.post("/fhir/Appointment", json={
        "resourceType": "Appointment", "status": "booked",
        "appointmentType": {"coding": [
            {"system": "urn:opencare:slot-types", "code": "follow-up"}]},
        "start": "2026-11-10T09:00:00Z", "end": "2026-11-10T09:30:00Z",
        "participant": [{"actor": {"reference": "Patient/pat-1"},
                         "status": "accepted"}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    appt_id = r.json()["id"]
    r = client.post("/messages", json={"patient": "pat-1",
                                       "text": "Proxy-visible thread"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/fhir/Observation", json={
        "resourceType": "Observation", "id": "obs-proxy-1", "status": "final",
        "code": {"coding": [{"system": LOINC, "code": "2345-7",
                             "display": "Glucose"}]},
        "subject": {"reference": "Patient/pat-1"},
        "valueQuantity": {"value": 110, "unit": "mg/dL"}}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return appt_id


def test_proxy_grants_scoped_reads_revocation_and_audit(client):
    appt_id = _proxy_fixture(client)
    # Validation: unknown scope and unknown proxy account -> 422.
    assert client.post("/portal/proxy/grants", json={
        "proxy_username": "dev-clinician", "scopes": ["billing"]},
        headers=PATIENT).status_code == 422
    assert client.post("/portal/proxy/grants", json={
        "proxy_username": "no-such-user", "scopes": ["schedule"]},
        headers=PATIENT).status_code == 422
    # The patient grants schedule + messages (not results).
    r = client.post("/portal/proxy/grants", json={
        "proxy_username": "dev-clinician",
        "scopes": ["schedule", "messages"]}, headers=PATIENT)
    assert r.status_code == 201, r.text
    grant = r.json()
    assert grant["granted_by"] == "dev-patient"
    assert grant["grant_source"] == "patient"
    assert grant["status"] == "active"
    # Discovery: the proxy sees the patient they act for.
    mine = client.get("/portal/proxy/patients", headers=CLINICIAN).json()
    assert mine["total"] == 1 and mine["items"][0]["patient"] == "Patient/pat-1"
    # Scoped reads that are granted work.
    appts = client.get("/portal/proxy/patients/pat-1/appointments",
                       headers=CLINICIAN)
    assert appts.status_code == 200, appts.text
    assert [a["id"] for a in appts.json()["items"]] == [appt_id]
    msgs = client.get("/portal/proxy/patients/pat-1/messages",
                      headers=CLINICIAN)
    assert msgs.status_code == 200 and msgs.json()["total"] == 1
    # Scope violation -> 403.
    assert client.get("/portal/proxy/patients/pat-1/results",
                      headers=CLINICIAN).status_code == 403
    # A non-proxy gets 403 everywhere.
    assert client.get("/portal/proxy/patients/pat-1/appointments",
                      headers=VIEWER).status_code == 403
    # EVERY proxy read is audit-chained: actor = proxy, on_behalf_of = patient.
    rows = _audit(client, "access.proxy.read")
    assert rows, "expected proxy read audit rows"
    assert all(r["actor"] == "dev-clinician" for r in rows)
    assert all(r["payload"]["on_behalf_of"] == "Patient/pat-1" for r in rows)
    assert {r["payload"]["scope"] for r in rows} == {"schedule", "messages"}
    # Admin-assisted grant gives the viewer results only.
    r = client.post("/proxy/grants", json={
        "patient": "pat-1", "proxy_username": "dev-viewer",
        "scopes": ["results"]}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["granted_by"] == "dev-admin"
    assert r.json()["grant_source"] == "admin-assisted"
    results = client.get("/portal/proxy/patients/pat-1/results",
                         headers=VIEWER)
    assert results.status_code == 200, results.text
    assert results.json()["total"] >= 1
    assert client.get("/portal/proxy/patients/pat-1/appointments",
                      headers=VIEWER).status_code == 403
    # Revocation is immediate.
    r = client.post(f"/portal/proxy/grants/{grant['grant_id']}/revoke",
                    headers=PATIENT)
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert client.get("/portal/proxy/patients/pat-1/appointments",
                      headers=CLINICIAN).status_code == 403
    assert client.get("/portal/proxy/patients",
                      headers=CLINICIAN).json()["total"] == 0


def test_proxy_expired_grant_is_refused(client):
    _proxy_fixture(client)
    r = client.post("/proxy/grants", json={
        "patient": "pat-1", "proxy_username": "dev-configurator",
        "scopes": ["schedule"], "expires_at": _date(-1)}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["expired"] is True
    assert client.get("/portal/proxy/patients/pat-1/appointments",
                      headers=CONFIG).status_code == 403
    # A future-expiry grant works until its instant.
    r = client.post("/proxy/grants", json={
        "patient": "pat-1", "proxy_username": "dev-configurator",
        "scopes": ["schedule"], "expires_at": _date(7)}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert client.get("/portal/proxy/patients/pat-1/appointments",
                      headers=CONFIG).status_code == 200


# ---------------------------------------------------------------------------
# BD-28 — Telehealth at scale
# ---------------------------------------------------------------------------

def _visit(client, appt, service="cardiology", headers=CLINICIAN):
    r = client.post("/telehealth/visits", json={
        "appointment_id": appt, "service_line": service}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _advance(client, visit_id, target, headers=CLINICIAN):
    return client.post(f"/telehealth/visits/{visit_id}/transition",
                       json={"to_status": target}, headers=headers)


def test_telehealth_service_config_layering_and_workspace(client):
    _patient(client, "pat-1")
    appt = _telehealth_appointment(client, "pat-1")
    visit = _visit(client, appt, "cardiology")
    assert visit["status"] == "scheduled"
    ws = client.get(f"/telehealth/visits/{visit['visit_id']}/workspace",
                    headers=CLINICIAN).json()
    # The cardiology service entry wins over the Phase 9 template.
    assert ws["join_url"] == \
        f"https://video.example.invalid/cardiology/{appt}"
    assert "cardiology" in ws["waiting_room_message"]
    assert ws["resolved_config"]["source"] == "service-override"
    # A service entry without a template override, and an unknown
    # service, both fall back to the Phase 9 template.
    appt2 = _telehealth_appointment(client, "pat-1", "2026-11-03T15:00:00Z")
    general = _visit(client, appt2, "general")
    ws2 = client.get(f"/telehealth/visits/{general['visit_id']}/workspace",
                     headers=CLINICIAN).json()
    assert ws2["join_url"] == f"https://video.example.invalid/opencare/{appt2}"
    assert ws2["waiting_room_message"]
    services = client.get("/telehealth/services", headers=CLINICIAN).json()
    assert services["total"] == 2
    assert services["base"]["join_url_template"].endswith("/opencare/{appointmentId}")


def test_telehealth_visit_lifecycle_no_show_and_interpreter(client):
    _patient(client, "pat-1")
    appt = _telehealth_appointment(client, "pat-1")
    visit = _visit(client, appt)
    vid = visit["visit_id"]
    # Illegal jump scheduled -> waiting is refused.
    assert _advance(client, vid, "waiting").status_code == 409
    for target in ("checked-in", "waiting", "in-progress", "completed"):
        r = _advance(client, vid, target)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == target
    done = client.get(f"/telehealth/visits/{vid}", headers=CLINICIAN).json()
    assert done["timestamps"]["checked_in"]
    assert done["timestamps"]["in_progress"]
    assert done["timestamps"]["completed"]
    assert [h["state"] for h in done["state_history"]] == [
        "scheduled", "checked-in", "waiting", "in-progress", "completed"]
    # Completed is terminal.
    assert _advance(client, vid, "virtual-no-show").status_code == 409
    # Virtual no-show: its own terminal state, distinct from clinic noshow.
    appt2 = _telehealth_appointment(client, "pat-1", "2026-11-04T15:00:00Z")
    visit2 = _visit(client, appt2, "general")
    r = _advance(client, visit2["visit_id"], "virtual-no-show")
    assert r.status_code == 200 and r.json()["status"] == "virtual-no-show"
    assert _advance(client, visit2["visit_id"], "checked-in").status_code == 409
    listed = client.get("/telehealth/visits?status=virtual-no-show",
                        headers=CLINICIAN).json()
    assert listed["total"] == 1
    # Interpreter-on-video is a reference + display only.
    r = client.post(
        f"/telehealth/visits/{vid}/interpreter-request",
        json={"interpreter_request_id": "interp-req-123",
              "interpreter_display": "Spanish interpreter (reference only)"},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["interpreter_request_id"] == "interp-req-123"
    assert "reference only" in r.json()["interpreter_display"]


def test_telehealth_portal_outcome_capture(client):
    _patient(client, "pat-1")
    _patient(client, "pat-2")
    appt = _telehealth_appointment(client, "pat-1")
    visit = _visit(client, appt)
    vid = visit["visit_id"]
    answers = [
        {"linkId": "outcome-summary",
         "answer": [{"valueString": "Blood pressure plan agreed"}]},
        {"linkId": "goals-met", "answer": [{"valueBoolean": True}]},
    ]
    # Outcome before completion -> 409.
    assert client.post(f"/portal/telehealth/visits/{vid}/outcome",
                       json={"answers": answers},
                       headers=PATIENT).status_code == 409
    for target in ("checked-in", "waiting", "in-progress", "completed"):
        assert _advance(client, vid, target).status_code == 200
    # Missing required answers -> 422 against the governed form.
    assert client.post(f"/portal/telehealth/visits/{vid}/outcome",
                       json={"answers": []},
                       headers=PATIENT).status_code == 422
    r = client.post(f"/portal/telehealth/visits/{vid}/outcome",
                    json={"answers": answers}, headers=PATIENT)
    assert r.status_code == 201, r.text
    assert r.json()["form"] == "telehealth-outcome"
    assert r.json()["visit"]["outcome_submitted"] is True
    assert r.json()["outcome"]["questionnaire"] == "telehealth-outcome"
    # A second outcome for the same visit -> 409.
    assert client.post(f"/portal/telehealth/visits/{vid}/outcome",
                       json={"answers": answers},
                       headers=PATIENT).status_code == 409
    # Another patient's visit is invisible to this patient (404).
    appt2 = _telehealth_appointment(client, "pat-2", "2026-11-05T15:00:00Z")
    other = _visit(client, appt2, "general")
    for target in ("checked-in", "waiting", "in-progress", "completed"):
        assert _advance(client, other["visit_id"], target).status_code == 200
    assert client.post(
        f"/portal/telehealth/visits/{other['visit_id']}/outcome",
        json={"answers": answers}, headers=PATIENT).status_code == 404


# ---------------------------------------------------------------------------
# BD-31 — Credential-gated scheduling checker
# ---------------------------------------------------------------------------

def _staff(client, staff_id, username):
    r = client.post("/people/staff", json={
        "staff_id": staff_id, "username": username,
        "full_name": f"Dr {username}", "job_role": "physician"}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _credential(client, staff_id, cred_id, expires, ctype="medical-licence"):
    r = client.post("/people/credentials", json={
        "staff_id": staff_id, "credential_id": cred_id, "type": ctype,
        "number": f"LIC-{cred_id}", "expires_on": expires}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def test_credential_check_valid_expired_absent_and_instant(client):
    _staff(client, "staff-valid", "dr-valid")
    cred = _credential(client, "staff-valid", "cred-valid", _date(365))
    _staff(client, "staff-expired", "dr-expired")
    old = _credential(client, "staff-expired", "cred-expired", _date(-1))
    _staff(client, "staff-none", "dr-none")
    # Valid credential -> allowed, naming the deciding credential.
    r = client.post("/scheduling/credential-check", json={
        "staff_username": "dr-valid", "privilege": "medical-licence"},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["allowed"] is True and body["reason"] is None
    assert body["deciding_credential"]["credential_id"] == cred["credential_id"]
    assert body["staff_id"] == "staff-valid"
    # Expired credential -> refused, reason expired, credential named.
    r = client.post("/scheduling/credential-check", json={
        "staff_username": "dr-expired", "privilege": "medical-licence"},
        headers=ADMIN)
    body = r.json()
    assert body["allowed"] is False and body["reason"] == "expired"
    assert body["deciding_credential"]["credential_id"] == old["credential_id"]
    # Absent credential -> refused, reason absent.
    r = client.post("/scheduling/credential-check", json={
        "staff_username": "dr-none", "privilege": "medical-licence"},
        headers=ADMIN)
    assert r.json()["allowed"] is False
    assert r.json()["reason"] == "absent"
    # A different privilege the valid holder does not hold -> absent.
    r = client.post("/scheduling/credential-check", json={
        "staff_username": "dr-valid", "privilege": "rn-licence"},
        headers=ADMIN)
    assert r.json()["allowed"] is False and r.json()["reason"] == "absent"
    # Unknown staff -> refused.
    r = client.post("/scheduling/credential-check", json={
        "staff_username": "dr-ghost", "privilege": "medical-licence"},
        headers=ADMIN)
    assert r.json()["allowed"] is False
    assert r.json()["reason"] == "unknown-staff"
    # The booking instant decides: a credential expiring in 10 days
    # allows a booking in 5 days and refuses one in 20 days.
    _staff(client, "staff-soon", "dr-soon")
    _credential(client, "staff-soon", "cred-soon", _date(10))
    at5 = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    at20 = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()
    r = client.post("/scheduling/credential-check", json={
        "staff_username": "dr-soon", "privilege": "medical-licence",
        "at": at5}, headers=ADMIN)
    assert r.json()["allowed"] is True
    r = client.post("/scheduling/credential-check", json={
        "staff_username": "dr-soon", "privilege": "medical-licence",
        "at": at20}, headers=ADMIN)
    assert r.json()["allowed"] is False and r.json()["reason"] == "expired"
    # Gates + validation.
    assert client.post("/scheduling/credential-check", json={
        "staff_username": "dr-valid", "privilege": "medical-licence"},
        headers=PATIENT).status_code == 403
    assert client.post("/scheduling/credential-check", json={
        "privilege": "medical-licence"}, headers=ADMIN).status_code == 422
