"""Phase 17 WS-OIDC — Studio-config OIDC + BD-54 session security.

Two layers:

1. Ungated pure tests: the Studio-config OIDC path (kind=integration,
   key=oidc) verified against a locally generated RSA key / JWKS fixture
   (the app.security._fetch_jwks / fetch_oidc_discovery stub pattern from
   test_phase1). Proves: a correctly signed token is accepted and mapped
   to the app role model; wrong signature / wrong audience / expired are
   refused 401; dev tokens keep working with OIDC enabled; with no
   enabled OIDC config behaviour is unchanged.

2. Live tests (env-gated, the test_platform_db_live.py pattern): the
   SAME app path against a real Keycloak 26.x realm. They SKIP unless
   OPENCARE_TEST_OIDC_ISSUER is set (plus the client/user credential env
   vars, which arrive only via the local credential file — never in the
   repo). They prove: the discovery document is fetched from the live
   Keycloak; a token the live Keycloak issued for the synthetic user is
   accepted and resolves to the right role; a wrong-audience config
   refuses it 401; dev tokens still work; and the BD-54 session flow
   (session-config + re-entry probe) works on Keycloak-issued tokens.
"""
import json
import os
import time

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.main import create_app

ADMIN = {"Authorization": "Bearer admin-token"}
CLINICIAN = {"Authorization": "Bearer clinician-token"}

ISSUER = "https://keycloak.example/realms/opencare"
AUDIENCE = "opencare-backend"
KID = "phase17-test-key"


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _publish(client, kind, key, payload):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    return version


def _rsa_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    jwk = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return pem, {"keys": [jwk]}


def _sign(pem, claims=None, expired=False, kid=KID):
    now = int(time.time())
    payload = {"sub": "kc-user-1", "preferred_username": "oidc-clinician",
               "iss": ISSUER, "aud": AUDIENCE, "iat": now,
               "exp": now - 60 if expired else now + 600,
               "realm_access": {"roles": ["clinician"]}}
    payload.update(claims or {})
    return pyjwt.encode(payload, pem, algorithm="RS256", headers={"kid": kid})


@pytest.fixture()
def oidc_client(tmp_path, monkeypatch):
    """App with the Studio OIDC config published ENABLED, JWKS/discovery
    stubbed to a local RSA fixture."""
    pem, jwks = _rsa_keypair()
    monkeypatch.setattr("app.security._fetch_jwks", lambda url: jwks)
    monkeypatch.setattr(
        "app.security.fetch_oidc_discovery",
        lambda issuer: {"issuer": ISSUER,
                        "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs"})
    app = create_app(database_url=f"sqlite:///{tmp_path}/oidc.db")
    with TestClient(app) as client:
        _publish(client, "integration", "oidc",
                 {"enabled": True, "issuer": ISSUER, "clientId": AUDIENCE,
                  "audience": AUDIENCE, "rolesClaim": "realm_access.roles"})
        yield client, pem


# ---------------------------------------------------------------------------
# Pure Studio-config OIDC tests
# ---------------------------------------------------------------------------

def test_studio_oidc_token_accepted_and_role_mapped(oidc_client):
    client, pem = oidc_client
    token = _sign(pem)
    r = client.get("/security/session", headers=_bearer(token))
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "oidc-clinician"
    assert r.json()["role"] == "clinician"
    # clinician role mapped -> FHIR write allowed, same as a dev clinician
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": "oidc-pat-1",
        "name": [{"family": "Oidc", "given": ["Synthetic"]}],
        "gender": "female", "birthDate": "1990-01-15"}, headers=_bearer(token))
    assert r.status_code == 201, r.text


def test_studio_oidc_wrong_signature_refused(oidc_client):
    client, _pem = oidc_client
    other_pem, _ = _rsa_keypair()
    forged = _sign(other_pem, {"realm_access": {"roles": ["admin"]}})
    assert client.get("/security/session", headers=_bearer(forged)).status_code == 401


def test_studio_oidc_wrong_audience_refused(oidc_client):
    client, pem = oidc_client
    token = _sign(pem, {"aud": "some-other-client"})
    assert client.get("/security/session", headers=_bearer(token)).status_code == 401


def test_studio_oidc_expired_refused(oidc_client):
    client, pem = oidc_client
    token = _sign(pem, expired=True)
    assert client.get("/security/session", headers=_bearer(token)).status_code == 401


def test_dev_tokens_still_work_with_studio_oidc_enabled(oidc_client):
    client, _pem = oidc_client
    r = client.get("/security/session", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json() == {"username": "dev-admin", "role": "admin",
                        "patient_ref": None}
    r = client.get("/security/session", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "clinician"


def test_no_oidc_config_behaviour_unchanged(client):
    # The seeded integration/oidc is DISABLED: a JWT-shaped token is not
    # a dev token (401, exactly as before Phase 17) and dev tokens work.
    fake_jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.signature"
    assert client.get("/security/session", headers=_bearer(fake_jwt)).status_code == 401
    assert client.get("/security/session", headers=ADMIN).status_code == 200
    r = client.get("/studio/config/integration/effective/oidc", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["payload"]["enabled"] is False


def test_oidc_config_rejects_secrets(client):
    r = client.post("/studio/config/integration/oidc",
                    json={"enabled": True, "issuer": ISSUER,
                          "clientSecret": "must-not-be-stored"}, headers=ADMIN)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# BD-54 backend: session-security policy + re-entry probe
# ---------------------------------------------------------------------------

def test_session_config_seed_and_off(client):
    r = client.get("/security/session-config", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    # Seeded published policy (studio_config.ensure_default_configs).
    assert body["sessionSecurity"] == {"enabled": True, "idleLockMinutes": 15,
                                       "reentryGraceMinutes": 5}
    assert body["oidc"]["enabled"] is False
    # Config-off: publishing a disabled policy turns the lock off.
    _publish(client, "integration", "session-security", {"enabled": False})
    r = client.get("/security/session-config", headers=ADMIN)
    assert r.json()["sessionSecurity"]["enabled"] is False
    # Anonymous callers get nothing.
    assert client.get("/security/session-config").status_code == 401


def test_session_reentry_probe(client):
    assert client.get("/security/session").status_code == 401
    r = client.get("/security/session", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "dev-clinician"


# ---------------------------------------------------------------------------
# Live Keycloak tests (env-gated)
# ---------------------------------------------------------------------------

_LIVE_VARS = ("OPENCARE_TEST_OIDC_ISSUER", "OPENCARE_TEST_OIDC_CLIENT_ID",
              "OPENCARE_TEST_OIDC_CLIENT_SECRET", "OPENCARE_TEST_OIDC_USERNAME",
              "OPENCARE_TEST_OIDC_PASSWORD")


def _live_env():
    values = {name: os.environ.get(name) for name in _LIVE_VARS}
    if not all(values.values()):
        pytest.skip("OPENCARE_TEST_OIDC_* not set — live Keycloak tests not run")
    values["OPENCARE_TEST_OIDC_AUDIENCE"] = (
        os.environ.get("OPENCARE_TEST_OIDC_AUDIENCE") or "opencare-backend")
    return values


def _live_token(env):
    issuer = env["OPENCARE_TEST_OIDC_ISSUER"].rstrip("/")
    r = httpx.post(f"{issuer}/protocol/openid-connect/token", data={
        "grant_type": "password",
        "client_id": env["OPENCARE_TEST_OIDC_CLIENT_ID"],
        "client_secret": env["OPENCARE_TEST_OIDC_CLIENT_SECRET"],
        "username": env["OPENCARE_TEST_OIDC_USERNAME"],
        "password": env["OPENCARE_TEST_OIDC_PASSWORD"],
    }, timeout=10.0)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture()
def live_client(tmp_path):
    env = _live_env()
    app = create_app(database_url=f"sqlite:///{tmp_path}/live-oidc.db")
    with TestClient(app) as client:
        _publish(client, "integration", "oidc",
                 {"enabled": True, "issuer": env["OPENCARE_TEST_OIDC_ISSUER"],
                  "clientId": env["OPENCARE_TEST_OIDC_CLIENT_ID"],
                  "audience": env["OPENCARE_TEST_OIDC_AUDIENCE"],
                  "rolesClaim": "realm_access.roles"})
        yield client, env


def test_live_discovery_document():
    env = _live_env()
    issuer = env["OPENCARE_TEST_OIDC_ISSUER"].rstrip("/")
    r = httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=10.0)
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["issuer"] == issuer
    assert doc["jwks_uri"].startswith(issuer)
    assert "authorization_endpoint" in doc and "token_endpoint" in doc
    # The app's own discovery helper resolves the same document.
    from app import security
    security._DISCOVERY_CACHE.clear()
    fetched = security.fetch_oidc_discovery(issuer)
    assert fetched["jwks_uri"] == doc["jwks_uri"]


def test_live_keycloak_token_accepted_with_role(live_client):
    client, env = live_client
    token = _live_token(env)
    r = client.get("/security/session", headers=_bearer(token))
    assert r.status_code == 200, r.text
    assert r.json()["username"] == env["OPENCARE_TEST_OIDC_USERNAME"]
    assert r.json()["role"] == "clinician"


def test_live_keycloak_token_wrong_audience_refused(live_client):
    client, env = live_client
    token = _live_token(env)
    _publish(client, "integration", "oidc",
             {"enabled": True, "issuer": env["OPENCARE_TEST_OIDC_ISSUER"],
              "clientId": env["OPENCARE_TEST_OIDC_CLIENT_ID"],
              "audience": "not-the-issuer-audience",
              "rolesClaim": "realm_access.roles"})
    assert client.get("/security/session", headers=_bearer(token)).status_code == 401
    # A tampered token is refused under the correct config too.
    _publish(client, "integration", "oidc",
             {"enabled": True, "issuer": env["OPENCARE_TEST_OIDC_ISSUER"],
              "clientId": env["OPENCARE_TEST_OIDC_CLIENT_ID"],
              "audience": env["OPENCARE_TEST_OIDC_AUDIENCE"],
              "rolesClaim": "realm_access.roles"})
    head, payload, sig = token.split(".")
    tampered = f"{head}.{payload}.{'A' if sig[0] != 'A' else 'B'}{sig[1:]}"
    assert client.get("/security/session", headers=_bearer(tampered)).status_code == 401


def test_live_dev_tokens_still_work(live_client):
    client, _env = live_client
    r = client.get("/security/session", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin"


def test_live_bd54_session_flow_on_keycloak_token(live_client):
    client, env = live_client
    token = _live_token(env)
    _publish(client, "integration", "session-security",
             {"enabled": True, "idleLockMinutes": 15, "reentryGraceMinutes": 5})
    r = client.get("/security/session-config", headers=_bearer(token))
    assert r.status_code == 200, r.text
    assert r.json()["sessionSecurity"] == {
        "enabled": True, "idleLockMinutes": 15, "reentryGraceMinutes": 5}
    assert r.json()["oidc"]["enabled"] is True
    # Tap-and-go re-entry probe inside the grace window: the UI lock did
    # not destroy the server session, so the same token re-opens it.
    r = client.get("/security/session", headers=_bearer(token))
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "clinician"
