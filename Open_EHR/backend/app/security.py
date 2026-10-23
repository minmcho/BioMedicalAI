"""Authentication boundary — two modes, selected by the AUTH_MODE env var.

AUTH_MODE=dev (default): bearer tokens map to seeded dev users/roles. This is
a stand-in ONLY at the authentication boundary, for local development/tests.
Dev tokens resolve against the managed account records first
(app.accounts / models.UserAccount, matched by SHA-256 token hash): a
DISABLED account's token stops authenticating here (401). The static
token map below remains the fallback for tokens with no account record
(e.g. OPENEHR_DEV_TOKENS overrides) and is what the built-in accounts
are seeded from, so the well-known dev tokens keep working unchanged.

AUTH_MODE=oidc (Keycloak-ready): bearer tokens are JWTs verified against a
JWKS fetched from OIDC_JWKS_URL (PyJWT, RS256). Claims are mapped to a local
user: `sub` -> user identity, the roles claim (default `realm_access.roles`,
the Keycloak shape; override with OIDC_ROLES_CLAIM as a dotted path) -> our
Role enum (no recognised role -> viewer, least privilege), and a
`patient_ref` claim -> User.patient_ref for patient-portal scoping.
OIDC_ISSUER / OIDC_AUDIENCE are validated when set. Invalid signature or an
expired token -> 401. Production points these at a Keycloak realm; running
that Keycloak server is ops work, not part of this codebase's test suite.

Phase 17 adds a second, additive OIDC path that coexists with dev mode:
when a PUBLISHED Studio config (kind=integration, key=oidc) has
``enabled: true`` plus an issuer, JWT-shaped bearer tokens are verified
against that issuer (discovery -> JWKS, RS256, iss/aud/exp) and mapped to
the same account/role model, while dev tokens and managed accounts keep
working byte-identically. With the config absent or disabled, behaviour
is exactly the dev behaviour above. AUTH_MODE=oidc (env) remains the
exclusive mode and is unchanged.

All data access below this layer (packs, audit, FHIR) is real in both modes.
"""
import hashlib
import json
import os

import httpx
import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .configstore import published_config, role_allowed
from .models import Role, User, UserAccount


def token_hash(token: str) -> str:
    """SHA-256 of a dev bearer token — the only form in which a dev
    token is ever stored on an account record (app.accounts)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

# token -> user info. Overridable via OPENEHR_DEV_TOKENS as JSON:
# {"<token>": {"username": "alice", "role": "admin"}, ...}
DEFAULT_DEV_TOKENS = {
    "admin-token": {"username": "dev-admin", "role": "admin"},
    "config-token": {"username": "dev-configurator", "role": "configurator"},
    "clinician-token": {"username": "dev-clinician", "role": "clinician"},
    "viewer-token": {"username": "dev-viewer", "role": "viewer"},
    "patient-token": {"username": "dev-patient", "role": "patient",
                      "patient_ref": "Patient/pat-1"},
}


def auth_mode() -> str:
    return os.environ.get("AUTH_MODE", "dev").strip().lower()


def dev_tokens() -> dict:
    raw = os.environ.get("OPENEHR_DEV_TOKENS")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return DEFAULT_DEV_TOKENS
    return DEFAULT_DEV_TOKENS


def get_db(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _upsert_user(session: Session, username: str, role: Role,
                 patient_ref: str | None = None) -> User:
    user = session.scalars(select(User).where(User.username == username)).first()
    if user is None:
        user = User(username=username, role=role, patient_ref=patient_ref)
        session.add(user)
    else:
        user.role = role
        user.patient_ref = patient_ref
    session.commit()
    return user


def seed_users(session: Session) -> None:
    for info in dev_tokens().values():
        exists = session.scalars(select(User).where(User.username == info["username"])).first()
        if not exists:
            session.add(User(username=info["username"], role=Role(info["role"]),
                             patient_ref=info.get("patient_ref")))
    session.commit()


# --------------------------------------------------------------------------
# OIDC mode
# --------------------------------------------------------------------------

_JWKS_CACHE: dict[str, dict] = {}


def _fetch_jwks(url: str) -> dict:
    """Fetch (and cache) a JWKS document over HTTP. Tests stub this function
    to serve a fixture JWKS, so the suite needs no network/Keycloak server."""
    if url not in _JWKS_CACHE:
        response = httpx.get(url, timeout=5.0)
        response.raise_for_status()
        _JWKS_CACHE[url] = response.json()
    return _JWKS_CACHE[url]


def _claim_path(claims: dict, dotted: str):
    value = claims
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _map_role(claims: dict, claim_name: str | None = None) -> Role:
    if not claim_name:
        claim_name = os.environ.get("OIDC_ROLES_CLAIM", "realm_access.roles")
    raw = _claim_path(claims, claim_name)
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, (list, tuple)):
        for item in raw:
            try:
                return Role(str(item).strip().lower())
            except ValueError:
                continue
    return Role.viewer  # least privilege when nothing recognised


def _oidc_user(token: str, session: Session, *, jwks_url: str | None = None,
               issuer: str | None = None, audience: str | None = None,
               roles_claim: str | None = None) -> User:
    if jwks_url is None:
        jwks_url = os.environ.get("OIDC_JWKS_URL", "")
    if not jwks_url:
        raise HTTPException(status_code=401, detail="OIDC mode is not configured (OIDC_JWKS_URL unset)")
    try:
        jwks = _fetch_jwks(jwks_url)
    except Exception:
        raise HTTPException(status_code=401, detail="Could not load OIDC signing keys")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Malformed JWT")
    kid = header.get("kid")
    key_data = None
    for candidate in jwks.get("keys", []):
        if kid is None or candidate.get("kid") == kid:
            key_data = candidate
            break
    if key_data is None:
        raise HTTPException(status_code=401, detail="Unknown JWT signing key")
    try:
        public_key = jwt.PyJWK(key_data).key
        decode_kwargs: dict = {"algorithms": ["RS256"]}
        if issuer is None:
            issuer = os.environ.get("OIDC_ISSUER") or None
        if audience is None:
            audience = os.environ.get("OIDC_AUDIENCE") or None
        if issuer:
            decode_kwargs["issuer"] = issuer
        if audience:
            decode_kwargs["audience"] = audience
        else:
            decode_kwargs["options"] = {"verify_aud": False}
        claims = jwt.decode(token, public_key, **decode_kwargs)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired JWT")
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="JWT has no 'sub' claim")
    username = claims.get("preferred_username") or sub
    return _upsert_user(session, username=username,
                        role=_map_role(claims, roles_claim),
                        patient_ref=claims.get("patient_ref"))


# --------------------------------------------------------------------------
# Phase 17: Studio-config OIDC (kind=integration, key=oidc) — additive path
# --------------------------------------------------------------------------

OIDC_CONFIG_KIND = "integration"
OIDC_CONFIG_KEY = "oidc"

_DISCOVERY_CACHE: dict[str, dict] = {}


def fetch_oidc_discovery(issuer: str) -> dict:
    """Fetch (and cache) an issuer's OIDC discovery document. Tests stub
    this function the same way they stub _fetch_jwks."""
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    if url not in _DISCOVERY_CACHE:
        response = httpx.get(url, timeout=5.0)
        response.raise_for_status()
        _DISCOVERY_CACHE[url] = response.json()
    return _DISCOVERY_CACHE[url]


def studio_oidc_config(session: Session) -> dict | None:
    """The effective Studio OIDC config, or None when OIDC is not enabled
    by published config. Enabled requires `enabled: true` and an issuer
    (or an explicit jwksUrl); anything else is today's behaviour."""
    try:
        cfg = published_config(session, OIDC_CONFIG_KIND, OIDC_CONFIG_KEY)
    except Exception:
        return None
    if cfg is None:
        return None
    payload = cfg.payload or {}
    if not isinstance(payload, dict) or payload.get("enabled") is not True:
        return None
    issuer = payload.get("issuer") if isinstance(payload.get("issuer"), str) else ""
    jwks_url = payload.get("jwksUrl") if isinstance(payload.get("jwksUrl"), str) else ""
    if not issuer.strip() and not jwks_url.strip():
        return None
    return payload


def _studio_oidc_user(token: str, session: Session, payload: dict) -> User:
    issuer = (payload.get("issuer") or "").strip() or None
    jwks_url = (payload.get("jwksUrl") or "").strip()
    if not jwks_url and issuer:
        try:
            discovery = fetch_oidc_discovery(issuer)
        except Exception:
            raise HTTPException(status_code=401,
                                detail="Could not load OIDC discovery document")
        jwks_url = discovery.get("jwks_uri") or ""
        if discovery.get("issuer"):
            issuer = discovery["issuer"]
    audience = (payload.get("audience") or payload.get("clientId") or "") or None
    if isinstance(audience, str):
        audience = audience.strip() or None
    roles_claim = payload.get("rolesClaim") if isinstance(
        payload.get("rolesClaim"), str) else None
    return _oidc_user(token, session, jwks_url=jwks_url, issuer=issuer,
                      audience=audience, roles_claim=roles_claim)


def _looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2 and all(token.split("."))


# --------------------------------------------------------------------------


def get_current_user(request: Request, session: Session = Depends(get_db)) -> User:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = auth.removeprefix("Bearer ").strip()
    if auth_mode() == "oidc":
        return _oidc_user(token, session)
    # Managed accounts first (app.accounts): the account record is the
    # governed identity — its role wins, and a disabled account's
    # token is dead even though the token string is unchanged.
    account = session.scalars(select(UserAccount).where(
        UserAccount.dev_token_hash == token_hash(token))).first()
    if account is not None:
        if account.status != "active":
            raise HTTPException(status_code=401,
                                detail="This account is disabled")
        return _upsert_user(session, username=account.username,
                            role=account.role,
                            patient_ref=account.patient_ref)
    info = dev_tokens().get(token)
    if info:
        return _upsert_user(session, username=info["username"], role=Role(info["role"]),
                            patient_ref=info.get("patient_ref"))
    # Phase 17 additive path: a JWT-shaped token that is not a dev token
    # is verified against the published Studio OIDC config when one is
    # enabled. A failed verification is a hard 401 (never a fall-through
    # to any other identity). With no enabled config this branch never
    # runs and the 401 below is exactly the pre-Phase-17 behaviour.
    if _looks_like_jwt(token):
        oidc_payload = studio_oidc_config(session)
        if oidc_payload is not None:
            return _studio_oidc_user(token, session, oidc_payload)
    raise HTTPException(status_code=401, detail="Invalid token")


def require_roles(*roles: Role):
    def dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail=f"Role {user.role.value} may not perform this action")
        return user
    return dep


def require_capability(capability: str):
    """Role gate driven by Studio configuration: the caller's role must be
    allowed the capability by the built-in defaults, as overridden by the
    published permissions config (kind=permissions, key=role-matrix).
    Enforced here, in the API dependency — the UI hiding a control is never
    the enforcement. See app.configstore.CAPABILITY_DEFAULTS."""
    def dep(session: Session = Depends(get_db),
            user: User = Depends(get_current_user)) -> User:
        if not role_allowed(session, user.role, capability):
            raise HTTPException(
                status_code=403,
                detail=f"Role {user.role.value} lacks capability '{capability}'")
        return user
    return dep
