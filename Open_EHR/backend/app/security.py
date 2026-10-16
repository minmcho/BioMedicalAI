"""Authentication boundary — two modes, selected by the AUTH_MODE env var.

AUTH_MODE=dev (default): bearer tokens map to seeded dev users/roles. This is
a stand-in ONLY at the authentication boundary, for local development/tests.

AUTH_MODE=oidc (Keycloak-ready): bearer tokens are JWTs verified against a
JWKS fetched from OIDC_JWKS_URL (PyJWT, RS256). Claims are mapped to a local
user: `sub` -> user identity, the roles claim (default `realm_access.roles`,
the Keycloak shape; override with OIDC_ROLES_CLAIM as a dotted path) -> our
Role enum (no recognised role -> viewer, least privilege), and a
`patient_ref` claim -> User.patient_ref for patient-portal scoping.
OIDC_ISSUER / OIDC_AUDIENCE are validated when set. Invalid signature or an
expired token -> 401. Production points these at a Keycloak realm; running
that Keycloak server is ops work, not part of this codebase's test suite.

All data access below this layer (packs, audit, FHIR) is real in both modes.
"""
import json
import os

import httpx
import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .configstore import role_allowed
from .models import Role, User

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


def _map_role(claims: dict) -> Role:
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


def _oidc_user(token: str, session: Session) -> User:
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
        issuer = os.environ.get("OIDC_ISSUER") or None
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
    return _upsert_user(session, username=username, role=_map_role(claims),
                        patient_ref=claims.get("patient_ref"))


# --------------------------------------------------------------------------


def get_current_user(request: Request, session: Session = Depends(get_db)) -> User:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = auth.removeprefix("Bearer ").strip()
    if auth_mode() == "oidc":
        return _oidc_user(token, session)
    info = dev_tokens().get(token)
    if not info:
        raise HTTPException(status_code=401, detail="Invalid token")
    return _upsert_user(session, username=info["username"], role=Role(info["role"]),
                        patient_ref=info.get("patient_ref"))


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
