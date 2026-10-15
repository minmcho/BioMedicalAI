"""Phase 1: new FHIR resources, scheduling/ADT, results inbox, patient-portal
scoping, and OIDC auth mode. Synthetic data only."""
import json
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.audit import verify_chain
from app.main import create_app
from app.models import AuditLog
from tests.conftest import ADMIN, CLINICIAN, PATIENT, VIEWER

# ---------------------------------------------------------------------------
# Fixtures / sample resources
# ---------------------------------------------------------------------------

PATIENT1 = {"resourceType": "Patient", "id": "pat-1",
            "name": [{"family": "Synthetic", "given": ["Pat"]}], "gender": "unknown"}
PATIENT2 = {"resourceType": "Patient", "id": "pat-2",
            "name": [{"family": "Synthetic", "given": ["Other"]}], "gender": "unknown"}


def _obs(obs_id, patient, status="final"):
    return {"resourceType": "Observation", "id": obs_id, "status": status,
            "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4",
                                 "display": "Heart rate"}]},
            "subject": {"reference": f"Patient/{patient}"},
            "valueQuantity": {"value": 72, "unit": "/min"}}


def _report(report_id, patient, status="final"):
    return {"resourceType": "DiagnosticReport", "id": report_id, "status": status,
            "code": {"coding": [{"system": "http://loinc.org", "code": "24323-8",
                                 "display": "Comprehensive metabolic panel"}]},
            "subject": {"reference": f"Patient/{patient}"}}


def _appointment(appt_id, patient="pat-1", practitioner="prac-1",
                 start="2026-11-02T09:00:00Z", status="booked"):
    return {"resourceType": "Appointment", "id": appt_id, "status": status,
            "start": start, "end": start,
            "participant": [
                {"actor": {"reference": f"Patient/{patient}"}, "status": "accepted"},
                {"actor": {"reference": f"Practitioner/{practitioner}"}, "status": "accepted"},
            ]}


def seed_patients(client):
    assert client.post("/fhir/Patient", json=PATIENT1, headers=CLINICIAN).status_code == 201
    assert client.post("/fhir/Patient", json=PATIENT2, headers=CLINICIAN).status_code == 201


# ---------------------------------------------------------------------------
# New resources: round-trips + validation
# ---------------------------------------------------------------------------

def test_condition_roundtrip_and_validation(client):
    seed_patients(client)
    cond = {"resourceType": "Condition", "id": "cond-1",
            "code": {"coding": [{"system": "http://snomed.info/sct", "code": "38341003",
                                  "display": "Hypertension"}]},
            "subject": {"reference": "Patient/pat-1"}}
    r = client.post("/fhir/Condition", json=cond, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["meta"]["versionId"] == "1"
    r = client.get("/fhir/Condition/cond-1", headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["code"]["coding"][0]["code"] == "38341003"
    # missing code -> 422
    bad = {"resourceType": "Condition", "subject": {"reference": "Patient/pat-1"}}
    assert client.post("/fhir/Condition", json=bad, headers=CLINICIAN).status_code == 422
    # wrong resourceType -> 422
    assert client.post("/fhir/Condition", json=_obs("x", "pat-1"),
                       headers=CLINICIAN).status_code == 422
    # missing -> 404
    assert client.get("/fhir/Condition/nope", headers=CLINICIAN).status_code == 404


def test_procedure_roundtrip_and_validation(client):
    seed_patients(client)
    proc = {"resourceType": "Procedure", "id": "proc-1", "status": "completed",
            "code": {"coding": [{"system": "http://snomed.info/sct", "code": "80146002",
                                  "display": "Appendectomy"}]},
            "subject": {"reference": "Patient/pat-1"}}
    assert client.post("/fhir/Procedure", json=proc, headers=CLINICIAN).status_code == 201
    assert client.get("/fhir/Procedure/proc-1", headers=CLINICIAN).status_code == 200
    # missing subject -> 422
    bad = {"resourceType": "Procedure", "code": {"text": "x"}}
    assert client.post("/fhir/Procedure", json=bad, headers=CLINICIAN).status_code == 422


def test_medication_request_roundtrip_and_validation(client):
    seed_patients(client)
    med = {"resourceType": "MedicationRequest", "id": "med-1", "status": "active",
           "intent": "order",
           "medicationCodeableConcept": {"coding": [
               {"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                "code": "1049221", "display": "Synthetic medication"}]},
           "subject": {"reference": "Patient/pat-1"}}
    r = client.post("/fhir/MedicationRequest", json=med, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert client.get("/fhir/MedicationRequest/med-1",
                      headers=CLINICIAN).json()["intent"] == "order"
    # missing intent -> 422
    bad = dict(med)
    bad.pop("intent")
    assert client.post("/fhir/MedicationRequest", json=bad, headers=CLINICIAN).status_code == 422
    # missing medication -> 422
    bad2 = {"resourceType": "MedicationRequest", "intent": "order",
            "subject": {"reference": "Patient/pat-1"}}
    assert client.post("/fhir/MedicationRequest", json=bad2, headers=CLINICIAN).status_code == 422


def test_diagnostic_report_roundtrip_and_validation(client):
    seed_patients(client)
    assert client.post("/fhir/DiagnosticReport", json=_report("rep-1", "pat-1"),
                       headers=CLINICIAN).status_code == 201
    assert client.get("/fhir/DiagnosticReport/rep-1", headers=CLINICIAN).status_code == 200
    # missing status -> 422
    bad = _report("rep-2", "pat-1")
    bad.pop("status")
    assert client.post("/fhir/DiagnosticReport", json=bad, headers=CLINICIAN).status_code == 422
    # missing code -> 422
    bad2 = {"resourceType": "DiagnosticReport", "status": "final"}
    assert client.post("/fhir/DiagnosticReport", json=bad2, headers=CLINICIAN).status_code == 422


def test_appointment_roundtrip_and_validation(client):
    seed_patients(client)
    r = client.post("/fhir/Appointment", json=_appointment("appt-1"), headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert client.get("/fhir/Appointment/appt-1", headers=CLINICIAN).status_code == 200
    # participant without actor -> 422
    bad = {"resourceType": "Appointment", "status": "booked",
           "participant": [{"status": "accepted"}]}
    assert client.post("/fhir/Appointment", json=bad, headers=CLINICIAN).status_code == 422
    # missing status -> 422
    bad2 = _appointment("appt-2")
    bad2.pop("status")
    assert client.post("/fhir/Appointment", json=bad2, headers=CLINICIAN).status_code == 422


def test_viewer_cannot_write_phase1_resources(client):
    seed_patients(client)
    assert client.post("/fhir/Condition",
                       json={"resourceType": "Condition", "code": {"text": "x"},
                             "subject": {"reference": "Patient/pat-1"}},
                       headers=VIEWER).status_code == 403
    assert client.post("/fhir/Appointment", json=_appointment("appt-v"),
                       headers=VIEWER).status_code == 403


# ---------------------------------------------------------------------------
# Scheduling / ADT
# ---------------------------------------------------------------------------

def test_appointment_search_by_practitioner(client):
    seed_patients(client)
    client.post("/fhir/Appointment", json=_appointment("a1", practitioner="prac-1"), headers=CLINICIAN)
    client.post("/fhir/Appointment", json=_appointment("a2", practitioner="prac-2"), headers=CLINICIAN)
    bundle = client.get("/fhir/Appointment", params={"practitioner": "prac-1"},
                        headers=CLINICIAN).json()
    assert bundle["total"] == 1 and bundle["entry"][0]["resource"]["id"] == "a1"
    bundle = client.get("/fhir/Appointment", params={"practitioner": "Practitioner/prac-2"},
                        headers=CLINICIAN).json()
    assert bundle["total"] == 1 and bundle["entry"][0]["resource"]["id"] == "a2"


def test_appointment_search_by_date_range(client):
    seed_patients(client)
    client.post("/fhir/Appointment", json=_appointment("d1", start="2026-11-02T09:00:00Z"), headers=CLINICIAN)
    client.post("/fhir/Appointment", json=_appointment("d2", start="2026-11-20T09:00:00Z"), headers=CLINICIAN)
    client.post("/fhir/Appointment", json=_appointment("d3", start="2026-12-15T09:00:00Z"), headers=CLINICIAN)
    bundle = client.get("/fhir/Appointment",
                        params={"start": "2026-11-01", "end": "2026-11-30"},
                        headers=CLINICIAN).json()
    assert bundle["total"] == 2
    assert {e["resource"]["id"] for e in bundle["entry"]} == {"d1", "d2"}
    bundle = client.get("/fhir/Appointment", params={"start": "2026-12-01"},
                        headers=CLINICIAN).json()
    assert bundle["total"] == 1 and bundle["entry"][0]["resource"]["id"] == "d3"


def test_appointment_search_malformed_datetime_422(client):
    seed_patients(client)
    client.post("/fhir/Appointment", json=_appointment("d-bad"), headers=CLINICIAN)
    assert client.get("/fhir/Appointment", params={"start": "not-a-date"},
                      headers=CLINICIAN).status_code == 422


def test_appointment_adt_transitions(client):
    seed_patients(client)
    client.post("/fhir/Appointment", json=_appointment("t1"), headers=CLINICIAN)
    url = "/fhir/Appointment/t1/transition"
    # booked -> fulfilled directly is illegal
    assert client.post(url, json={"to_status": "fulfilled"}, headers=CLINICIAN).status_code == 409
    # happy path: booked -> arrived -> fulfilled
    r = client.post(url, json={"to_status": "arrived"}, headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["status"] == "arrived"
    r = client.post(url, json={"to_status": "fulfilled"}, headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["status"] == "fulfilled"
    assert r.json()["meta"]["versionId"] == "3"
    # fulfilled is terminal
    assert client.post(url, json={"to_status": "cancelled"}, headers=CLINICIAN).status_code == 409
    # persisted, not just returned
    assert client.get("/fhir/Appointment/t1", headers=CLINICIAN).json()["status"] == "fulfilled"


def test_appointment_cancel_terminal_and_audit(client):
    seed_patients(client)
    client.post("/fhir/Appointment", json=_appointment("t2"), headers=CLINICIAN)
    r = client.post("/fhir/Appointment/t2/transition", json={"to_status": "cancelled"},
                    headers=CLINICIAN)
    assert r.status_code == 200
    # cancelled is terminal
    assert client.post("/fhir/Appointment/t2/transition", json={"to_status": "arrived"},
                       headers=CLINICIAN).status_code == 409
    # unknown target status -> 422, missing body field -> 422
    client.post("/fhir/Appointment", json=_appointment("t3"), headers=CLINICIAN)
    assert client.post("/fhir/Appointment/t3/transition", json={"to_status": "bogus"},
                       headers=CLINICIAN).status_code == 422
    assert client.post("/fhir/Appointment/t3/transition", json={},
                       headers=CLINICIAN).status_code == 422
    # viewer may not transition
    assert client.post("/fhir/Appointment/t3/transition", json={"to_status": "arrived"},
                       headers=VIEWER).status_code == 403
    # audit rows exist and the chain still verifies
    factory = client.app.state.session_factory
    with factory() as session:
        actions = [row.action for row in session.scalars(select(AuditLog)).all()]
        assert "fhir.appointment.transition" in actions
        assert verify_chain(session) is True


# ---------------------------------------------------------------------------
# Results inbox
# ---------------------------------------------------------------------------

def test_inbox_contents_ordering_and_isolation(client):
    seed_patients(client)
    client.post("/fhir/Observation", json=_obs("in-obs-1", "pat-1"), headers=CLINICIAN)
    client.post("/fhir/DiagnosticReport", json=_report("in-rep-1", "pat-1", status="preliminary"),
                headers=CLINICIAN)
    client.post("/fhir/Observation", json=_obs("in-obs-2", "pat-1"), headers=CLINICIAN)
    client.post("/fhir/Observation", json=_obs("in-obs-other", "pat-2"), headers=CLINICIAN)

    bundle = client.get("/fhir/inbox", params={"patient": "pat-1"}, headers=CLINICIAN).json()
    assert bundle["resourceType"] == "Bundle" and bundle["total"] == 3
    ids = [e["resource"]["id"] for e in bundle["entry"]]
    assert ids[0] == "in-obs-2"  # newest first
    assert set(ids) == {"in-obs-1", "in-rep-1", "in-obs-2"}  # pat-2 result excluded

    bundle = client.get("/fhir/inbox", params={"patient": "Patient/pat-1", "status": "preliminary"},
                        headers=CLINICIAN).json()
    assert bundle["total"] == 1 and bundle["entry"][0]["resource"]["id"] == "in-rep-1"

    # staff-only: viewer and patient roles are refused here
    assert client.get("/fhir/inbox", params={"patient": "pat-1"}, headers=VIEWER).status_code == 403
    assert client.get("/fhir/inbox", params={"patient": "pat-1"}, headers=PATIENT).status_code == 403


# ---------------------------------------------------------------------------
# Patient portal scoping
# ---------------------------------------------------------------------------

def test_patient_reads_own_and_not_others_by_id(client):
    seed_patients(client)
    client.post("/fhir/Observation", json=_obs("own-obs", "pat-1"), headers=CLINICIAN)
    client.post("/fhir/Observation", json=_obs("other-obs", "pat-2"), headers=CLINICIAN)

    assert client.get("/fhir/Patient/pat-1", headers=PATIENT).status_code == 200
    assert client.get("/fhir/Observation/own-obs", headers=PATIENT).status_code == 200
    # other patient's resources: 404 by design (non-disclosure)
    assert client.get("/fhir/Patient/pat-2", headers=PATIENT).status_code == 404
    assert client.get("/fhir/Observation/other-obs", headers=PATIENT).status_code == 404
    # clinician unaffected
    assert client.get("/fhir/Observation/other-obs", headers=CLINICIAN).status_code == 200


def test_patient_search_is_scoped(client):
    seed_patients(client)
    client.post("/fhir/Observation", json=_obs("s-own", "pat-1"), headers=CLINICIAN)
    client.post("/fhir/Observation", json=_obs("s-other", "pat-2"), headers=CLINICIAN)

    bundle = client.get("/fhir/Observation", headers=PATIENT).json()
    assert bundle["total"] == 1 and bundle["entry"][0]["resource"]["id"] == "s-own"
    bundle = client.get("/fhir/Observation", params={"patient": "pat-1"}, headers=PATIENT).json()
    assert bundle["total"] == 1
    # explicitly searching another patient -> 403
    assert client.get("/fhir/Observation", params={"patient": "pat-2"},
                      headers=PATIENT).status_code == 403


def test_portal_my_results(client):
    seed_patients(client)
    client.post("/fhir/Observation", json=_obs("p-obs", "pat-1"), headers=CLINICIAN)
    client.post("/fhir/DiagnosticReport", json=_report("p-rep", "pat-1"), headers=CLINICIAN)
    client.post("/fhir/Observation", json=_obs("p-other", "pat-2"), headers=CLINICIAN)

    bundle = client.get("/portal/my-results", headers=PATIENT).json()
    ids = {e["resource"]["id"] for e in bundle["entry"]}
    assert ids == {"p-obs", "p-rep"}
    # staff roles do not use the portal path
    assert client.get("/portal/my-results", headers=CLINICIAN).status_code == 403
    # patient role cannot write
    assert client.post("/fhir/Observation", json=_obs("p-x", "pat-1"),
                       headers=PATIENT).status_code == 403


# ---------------------------------------------------------------------------
# OIDC auth mode
# ---------------------------------------------------------------------------

KID = "phase1-test-key"
ISSUER = "https://keycloak.example/realms/opencare"


def _rsa_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    jwk = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return pem, {"keys": [jwk]}


@pytest.fixture()
def oidc(tmp_path, monkeypatch):
    pem, jwks = _rsa_keypair()
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_JWKS_URL", f"{ISSUER}/protocol/openid-connect/certs")
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.setattr("app.security._fetch_jwks", lambda url: jwks)
    app = create_app(database_url=f"sqlite:///{tmp_path}/oidc.db")
    with TestClient(app) as c:
        yield c, pem


def _sign(pem, claims=None, expired=False, kid=KID):
    now = int(time.time())
    payload = {"sub": "kc-user-1", "preferred_username": "kc-user",
               "iss": ISSUER, "iat": now,
               "exp": now - 60 if expired else now + 600}
    payload.update(claims or {})
    return pyjwt.encode(payload, pem, algorithm="RS256", headers={"kid": kid})


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_oidc_valid_token_role_mapping(oidc):
    client, pem = oidc
    token = _sign(pem, {"realm_access": {"roles": ["clinician"]}})
    r = client.post("/fhir/Patient", json=PATIENT1, headers=_bearer(token))
    assert r.status_code == 201, r.text  # clinician role mapped -> write allowed
    assert client.get("/fhir/Patient/pat-1", headers=_bearer(token)).status_code == 200


def test_oidc_unknown_role_falls_back_to_viewer(oidc):
    client, pem = oidc
    client.post("/fhir/Patient", json=PATIENT1,
                headers=_bearer(_sign(pem, {"realm_access": {"roles": ["clinician"]}})))
    token = _sign(pem, {"realm_access": {"roles": ["some-unmapped-role"]}})
    assert client.get("/fhir/Patient/pat-1", headers=_bearer(token)).status_code == 200
    assert client.post("/fhir/Patient", json=PATIENT2,
                       headers=_bearer(token)).status_code == 403  # viewer cannot write


def test_oidc_patient_claim_scoping(oidc):
    client, pem = oidc
    staff = _bearer(_sign(pem, {"realm_access": {"roles": ["clinician"]}}))
    client.post("/fhir/Patient", json=PATIENT1, headers=staff)
    client.post("/fhir/Patient", json=PATIENT2, headers=staff)
    token = _sign(pem, {"realm_access": {"roles": ["patient"]},
                        "patient_ref": "Patient/pat-1"})
    assert client.get("/fhir/Patient/pat-1", headers=_bearer(token)).status_code == 200
    assert client.get("/fhir/Patient/pat-2", headers=_bearer(token)).status_code == 404


def test_oidc_tampered_signature_401(oidc):
    client, pem = oidc
    other_pem, _ = _rsa_keypair()  # different key, same kid
    forged = _sign(other_pem, {"realm_access": {"roles": ["admin"]}})
    assert client.get("/fhir/Patient/pat-1", headers=_bearer(forged)).status_code == 401


def test_oidc_expired_token_401(oidc):
    client, pem = oidc
    expired = _sign(pem, {"realm_access": {"roles": ["admin"]}}, expired=True)
    assert client.get("/fhir/Patient/pat-1", headers=_bearer(expired)).status_code == 401


def test_dev_mode_unchanged(client):
    # Default mode still uses dev tokens, and a JWT-shaped string is not one.
    assert client.get("/fhir/Patient/pat-1", headers=ADMIN).status_code in (200, 404)
    fake_jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.signature"
    assert client.get("/fhir/Patient/pat-1", headers=_bearer(fake_jwt)).status_code == 401
