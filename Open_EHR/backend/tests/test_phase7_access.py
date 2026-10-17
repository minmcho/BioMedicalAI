"""Phase 7 — Access (app.access): referrals + triage, prior
authorisations, the order-signing auth gate, eligibility (recorded
local-config checks), and the appointment waitlist.

Everything is Studio configuration: payers / referral services / slot
types are terminology, the three lifecycles are workflow config. The
seeded payer default flags NO service as auth-required, so the
signing gate is inert until a test publishes a payer config that
flags one — the config flip is the behaviour change, with no code
change (roadmap §6).
"""
from sqlalchemy import select

from app.audit import verify_chain
from app.models import AuditLog
from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER


def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic", "given": ["Access"]}],
        "gender": "female", "birthDate": "1985-06-15"}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _publish(client, kind, key, payload):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text


def _payers_payload(auth_required):
    return {"name": "Test payers", "system": "urn:opencare:payer",
            "concepts": [
                {"code": "synthetic-payer", "display": "Synthetic Payer",
                 "active": True, "system": "urn:opencare:payer",
                 "eligibility": {"copay": 25.0, "coinsurancePercent": 20},
                 "authRequiredServices": list(auth_required)}]}


def _referral(client, pid="pat-1", service="cardiology", headers=ADMIN):
    return client.post("/access/referrals", json={
        "patient": pid, "service_code": service,
        "referring_organization": "Synthetic Referring Clinic",
        "referring_provider": "Dr External",
        "reason": "Chest pain workup", "payer": "synthetic-payer"},
        headers=headers)


# ---------------------------------------------------------------------------
# Referrals
# ---------------------------------------------------------------------------

def test_referral_intake_creates_service_request_and_triage_task(client):
    _patient(client)
    r = _referral(client)
    assert r.status_code == 201, r.text
    body = r.json()
    referral, task = body["referral"], body["task"]
    assert referral["resourceType"] == "ServiceRequest"
    assert referral["status"] == "received"
    assert referral["code"]["coding"][0]["code"] == "cardiology"
    assert referral["referringOrganization"] == "Synthetic Referring Clinic"
    assert referral["payer"] == "synthetic-payer"
    assert referral["taskId"] == task["id"]
    assert task["resourceType"] == "Task" and task["status"] == "received"
    assert task["focus"]["reference"] == f"ServiceRequest/{referral['id']}"
    # A referral is NOT a CPOE order: it never enters the orders surfaces.
    orders = client.get("/orders", headers=ADMIN).json()
    assert all(o["id"] != referral["id"] for o in orders["items"])
    # The triage queue holds it.
    queue = client.get("/access/referrals/queue", headers=CLINICIAN).json()
    assert [i["id"] for i in queue["items"]] == [referral["id"]]


def test_referral_intake_validates_service_patient_and_payer(client):
    _patient(client)
    r = _referral(client, service="astrology")
    assert r.status_code == 422 and "astrology" in r.json()["detail"]
    r = _referral(client, pid="ghost")
    assert r.status_code == 404
    r = client.post("/access/referrals", json={
        "patient": "pat-1", "service_code": "cardiology",
        "referring_organization": "Org", "payer": "no-such-payer"},
        headers=ADMIN)
    assert r.status_code == 422 and "no-such-payer" in r.json()["detail"]
    r = client.post("/access/referrals", json={
        "patient": "pat-1", "service_code": "cardiology"}, headers=ADMIN)
    assert r.status_code == 422  # referring_organization is required


def test_referral_lifecycle_moves_task_and_respects_config_roles(client):
    _patient(client)
    rid = _referral(client).json()["referral"]["id"]
    # received -> authorised directly is illegal in the published machine.
    r = client.post(f"/access/referrals/{rid}/transition",
                    json={"to_status": "authorised"}, headers=ADMIN)
    assert r.status_code == 409
    r = client.post(f"/access/referrals/{rid}/transition",
                    json={"to_status": "bogus"}, headers=ADMIN)
    assert r.status_code == 422
    # Triage: any access.work role.
    r = client.post(f"/access/referrals/{rid}/transition",
                    json={"to_status": "triaged"}, headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["status"] == "triaged"
    detail = client.get(f"/access/referrals/{rid}", headers=ADMIN).json()
    assert detail["task"]["status"] == "in-progress"
    # Authorisation is a clinical decision in the seeded config: the
    # configurator may work the queue but may not authorise.
    r = client.post(f"/access/referrals/{rid}/transition",
                    json={"to_status": "authorised"}, headers=CONFIG)
    assert r.status_code == 403
    r = client.post(f"/access/referrals/{rid}/transition",
                    json={"to_status": "authorised"}, headers=CLINICIAN)
    assert r.status_code == 200
    detail = client.get(f"/access/referrals/{rid}", headers=ADMIN).json()
    assert detail["task"]["status"] == "accepted"
    # Through to completion; the queue drains.
    for target in ("scheduled", "completed"):
        r = client.post(f"/access/referrals/{rid}/transition",
                        json={"to_status": target}, headers=ADMIN)
        assert r.status_code == 200, r.text
    queue = client.get("/access/referrals/queue", headers=ADMIN).json()
    assert queue["total"] == 0


def test_referral_role_gates(client):
    _patient(client)
    assert _referral(client, headers=VIEWER).status_code == 403
    assert client.get("/access/referrals", headers=PATIENT).status_code == 403
    assert client.get("/access/referrals", headers=VIEWER).status_code == 200


# ---------------------------------------------------------------------------
# Authorisations + the order-signing gate
# ---------------------------------------------------------------------------

def _auth(client, service="imaging", units=2, headers=ADMIN, **kw):
    payload = {"patient": "pat-1", "payer": "synthetic-payer",
               "service_code": service, "approved_units": units}
    payload.update(kw)
    return client.post("/access/authorisations", json=payload, headers=headers)


def test_authorisation_lifecycle_and_validation(client):
    _patient(client)
    r = client.post("/access/authorisations", json={
        "patient": "pat-1", "payer": "no-such-payer",
        "service_code": "imaging"}, headers=ADMIN)
    assert r.status_code == 422 and "no-such-payer" in r.json()["detail"]
    r = _auth(client, valid_from="2026-02-01", valid_to="2026-01-01")
    assert r.status_code == 422  # window inverted
    r = _auth(client)
    assert r.status_code == 201, r.text
    auth = r.json()
    assert auth["status"] == "requested"
    assert auth["auth_number"].startswith("AUTH-")
    assert auth["used_units"] == 0
    # Approve with the payer-issued number as data.
    r = client.post(f"/access/authorisations/{auth['id']}/transition",
                    json={"to_status": "approved", "auth_number": "PAYER-991"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert r.json()["auth_number"] == "PAYER-991"
    # Terminal-ish: approved -> denied is not in the machine.
    r = client.post(f"/access/authorisations/{auth['id']}/transition",
                    json={"to_status": "denied"}, headers=ADMIN)
    assert r.status_code == 409
    # Configurator cannot approve (seeded per-transition roles).
    r = _auth(client, service="procedure")
    aid = r.json()["id"]
    r = client.post(f"/access/authorisations/{aid}/transition",
                    json={"to_status": "approved"}, headers=CONFIG)
    assert r.status_code == 403


def _imaging_order(client, pid="pat-1"):
    r = client.post("/orders", json={
        "type": "imaging", "code": "71046", "display": "Chest X-ray, 2 views",
        "modality": "CR", "subject": {"reference": f"Patient/{pid}"}},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["order"]["id"]


def _sign(client, order_id, headers=CLINICIAN, **extra):
    payload = {"to_status": "signed"}
    payload.update(extra)
    return client.post(f"/orders/{order_id}/transition", json=payload,
                       headers=headers)


def test_signing_gate_inert_under_seeded_payer_defaults(client):
    _patient(client)
    oid = _imaging_order(client)
    r = _sign(client, oid)
    assert r.status_code == 200, r.text  # no auth required by default


def test_signing_gate_blocks_then_auth_covers_and_consumes_units(client):
    _patient(client)
    # The config flip: imaging now requires authorisation.
    _publish(client, "terminology", "payers", _payers_payload(["imaging"]))
    oid = _imaging_order(client)
    r = _sign(client, oid)
    assert r.status_code == 422
    assert "Prior authorisation required" in r.json()["detail"]
    assert "imaging" in r.json()["detail"]
    # An approved auth covering the service unblocks signing and is
    # linked onto the order; one unit is consumed.
    auth = _auth(client, units=1).json()
    client.post(f"/access/authorisations/{auth['id']}/transition",
                json={"to_status": "approved"}, headers=CLINICIAN)
    r = _sign(client, oid)
    assert r.status_code == 200, r.text
    extensions = {e["url"]: e.get("valueString")
                  for e in r.json().get("extension") or []}
    assert extensions.get("urn:opencare:extension/prior-auth") == auth["id"]
    after = client.get(f"/access/authorisations/{auth['id']}",
                       headers=ADMIN).json()
    assert after["used_units"] == 1
    # The single unit is spent: a second imaging order cannot sign.
    oid2 = _imaging_order(client)
    r = _sign(client, oid2)
    assert r.status_code == 422
    # The override path proceeds with a recorded reason.
    r = _sign(client, oid2, auth_override_reason="Urgent — payer line down")
    assert r.status_code == 200, r.text
    extensions = {e["url"]: e.get("valueString")
                  for e in r.json().get("extension") or []}
    assert extensions.get("urn:opencare:extension/auth-override") == \
        "Urgent — payer line down"


def test_signing_gate_respects_auth_validity_window(client):
    _patient(client)
    _publish(client, "terminology", "payers", _payers_payload(["imaging"]))
    auth = _auth(client, units=5, valid_from="2020-01-01",
                 valid_to="2020-12-31").json()
    client.post(f"/access/authorisations/{auth['id']}/transition",
                json={"to_status": "approved"}, headers=CLINICIAN)
    oid = _imaging_order(client)
    r = _sign(client, oid)
    assert r.status_code == 422  # approved but long expired window


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def test_eligibility_check_is_local_config_labelled_and_recorded(client):
    _patient(client)
    r = client.post("/access/eligibility/check", json={
        "patient": "pat-1", "payer": "synthetic-payer",
        "service_code": "imaging", "service_date": "2026-10-20"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["covered"] is True
    assert body["copay"] == 25.0 and body["coinsurance_percent"] == 20
    assert body["basis"] == "local-config"
    assert "NO live payer connection" in body["disclaimer"]
    assert body["checked_by"] == "dev-clinician"
    # The check is recorded and retrievable.
    checks = client.get("/access/eligibility/checks?patient=pat-1",
                        headers=ADMIN).json()
    assert checks["total"] == 1
    assert checks["items"][0]["id"] == body["id"]
    assert checks["items"][0]["covered"] is True


def test_eligibility_unknown_payer_422_and_covered_services_rule(client):
    _patient(client)
    r = client.post("/access/eligibility/check", json={
        "patient": "pat-1", "payer": "no-such-payer"}, headers=ADMIN)
    assert r.status_code == 422 and "no-such-payer" in r.json()["detail"]
    # A payer whose configured coveredServices exclude the service
    # evaluates to not-covered — figures still come from the config.
    payload = _payers_payload([])
    payload["concepts"][0]["eligibility"] = {
        "copay": 40.0, "coinsurancePercent": 30,
        "coveredServices": ["consult"]}
    _publish(client, "terminology", "payers", payload)
    r = client.post("/access/eligibility/check", json={
        "patient": "pat-1", "payer": "synthetic-payer",
        "service_code": "imaging"}, headers=ADMIN)
    assert r.status_code == 201
    assert r.json()["covered"] is False
    assert r.json()["reason"] == "service-not-covered"
    assert r.json()["copay"] == 40.0


# ---------------------------------------------------------------------------
# Waitlist
# ---------------------------------------------------------------------------

def _appointment(client, appt_id="appt-1", slot="follow-up", status="booked"):
    body = {"resourceType": "Appointment", "id": appt_id, "status": status,
            "appointmentType": {"coding": [{
                "system": "urn:opencare:slot-type", "code": slot,
                "display": "Follow-up visit"}]},
            "start": "2026-10-20T09:00:00Z",
            "participant": [{"actor": {"reference": "Patient/pat-1"},
                             "status": "accepted"}]}
    r = client.post("/fhir/Appointment", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text


def _join(client, pid="pat-1", slot="follow-up", headers=CLINICIAN, **kw):
    payload = {"patient": pid, "slot_type": slot}
    payload.update(kw)
    return client.post("/access/waitlist", json=payload, headers=headers)


def test_waitlist_join_offer_accept_books_real_appointment(client):
    _patient(client)
    _appointment(client)
    r = _join(client, slot="telehealth-visit")
    assert r.status_code == 422  # unknown slot type
    first = _join(client, priority=50).json()
    second = _join(client, priority=10).json()
    assert first["status"] == "waiting" and second["status"] == "waiting"
    # Queue order: priority, then age — the priority-10 entry is first.
    queue = client.get("/access/waitlist?status=waiting", headers=ADMIN).json()
    assert [e["id"] for e in queue["items"]] == [second["id"], first["id"]]
    # Offering against a non-cancelled appointment is refused.
    r = client.post("/access/waitlist/offers",
                    json={"appointment_id": "appt-1"}, headers=ADMIN)
    assert r.status_code == 409
    # Cancel the appointment (the Phase 1 ADT machine), then offer.
    r = client.post("/fhir/Appointment/appt-1/transition",
                    json={"to_status": "cancelled"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    r = client.post("/access/waitlist/offers",
                    json={"appointment_id": "appt-1"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    offered = r.json()["entry"]
    assert offered["id"] == second["id"]  # highest priority wins the slot
    assert offered["status"] == "offered"
    assert offered["offered_appointment_id"] == "appt-1"
    # Accepting books a real Appointment of the entry's slot type.
    r = client.post(f"/access/waitlist/{second['id']}/accept",
                    json={"start": "2026-10-21T09:00:00Z",
                          "end": "2026-10-21T09:30:00Z",
                          "practitioner": "dr-1"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    entry, appt = r.json()["entry"], r.json()["appointment"]
    assert entry["status"] == "booked"
    assert appt["status"] == "booked"
    assert appt["appointmentType"]["coding"][0]["code"] == "follow-up"
    assert appt["id"] == entry["appointment_id"]
    # Accepting again is a state conflict.
    r = client.post(f"/access/waitlist/{second['id']}/accept",
                    json={"start": "2026-10-22T09:00:00Z"}, headers=CLINICIAN)
    assert r.status_code == 409


def test_waitlist_patient_carve_out(client):
    _patient(client)
    _patient(client, "pat-2")
    # The dev patient (linked to Patient/pat-1) may queue THEMSELVES…
    r = _join(client, headers=PATIENT)
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]
    # …but not someone else, and cannot see other patients' entries.
    r = _join(client, pid="pat-2", headers=PATIENT)
    assert r.status_code == 403
    _join(client, pid="pat-2", headers=CLINICIAN)
    mine = client.get("/access/waitlist", headers=PATIENT).json()
    assert [e["id"] for e in mine["items"]] == [entry_id]
    # Patients may withdraw their own entry; staff-only transitions 403.
    r = client.post(f"/access/waitlist/{entry_id}/transition",
                    json={"to_status": "offered"}, headers=PATIENT)
    assert r.status_code == 403
    r = client.post(f"/access/waitlist/{entry_id}/transition",
                    json={"to_status": "cancelled"}, headers=PATIENT)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


def test_access_writes_are_audited_with_intact_chain(client):
    _patient(client)
    rid = _referral(client).json()["referral"]["id"]
    client.post(f"/access/referrals/{rid}/transition",
                json={"to_status": "triaged"}, headers=CLINICIAN)
    client.post("/access/eligibility/check", json={
        "patient": "pat-1", "payer": "synthetic-payer"}, headers=ADMIN)
    with client.app.state.session_factory() as session:
        actions = [row.action for row in session.scalars(
            select(AuditLog).order_by(AuditLog.id.asc())).all()]
        assert verify_chain(session) is True
    for expected in ("access.referral.create", "access.referral.transition",
                     "access.eligibility.check"):
        assert expected in actions
