"""Phase 17 (BD-54 / RTM CMP-33) — session security surface.

The idle-lock / tap-and-go policy itself is *published Studio config*
(kind=integration, key=session-security, seeded published in
app.studio_config.ensure_default_configs):

    {"enabled": true, "idleLockMinutes": 15, "reentryGraceMinutes": 5}

The web shell consumes that published config and enforces the lock in
the UI (see web/src/auth/idleLock.ts). This module is the small API
support the shell needs:

- GET /security/session-config — the effective policy plus whether the
  Studio OIDC path (kind=integration, key=oidc) is enabled, so the lock
  screen can label its re-entry action correctly. No secrets: the OIDC
  config carries only issuer/client identifiers by validator law.
- GET /security/session — who the caller's bearer token currently
  resolves to. Tap-and-go re-entry calls this after an idle lock: the
  server session (the token) was never destroyed by the UI lock, so a
  200 here inside the grace window is what re-opens the shell; a 401
  means re-entry is refused and the user must sign in again.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .configstore import published_config
from .models import User
from .security import get_current_user, get_db, studio_oidc_config

router = APIRouter(prefix="/security", tags=["session-security"])

SESSION_SECURITY_KEY = "session-security"

DEFAULT_SESSION_SECURITY = {
    "enabled": True,
    "idleLockMinutes": 15,
    "reentryGraceMinutes": 5,
}


def session_security_policy(session: Session) -> dict:
    """Effective idle-lock policy: the published config merged over the
    built-in defaults. A disabled/absent published config is OFF —
    exactly the pre-Phase-17 shell behaviour."""
    cfg = published_config(session, "integration", SESSION_SECURITY_KEY)
    if cfg is None:
        return {"enabled": False,
                "idleLockMinutes": DEFAULT_SESSION_SECURITY["idleLockMinutes"],
                "reentryGraceMinutes": DEFAULT_SESSION_SECURITY["reentryGraceMinutes"]}
    payload = cfg.payload or {}
    if not isinstance(payload, dict) or payload.get("enabled") is not True:
        return {"enabled": False,
                "idleLockMinutes": DEFAULT_SESSION_SECURITY["idleLockMinutes"],
                "reentryGraceMinutes": DEFAULT_SESSION_SECURITY["reentryGraceMinutes"]}
    def _minutes(field, fallback):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return fallback
        return value
    return {
        "enabled": True,
        "idleLockMinutes": _minutes("idleLockMinutes",
                                    DEFAULT_SESSION_SECURITY["idleLockMinutes"]),
        "reentryGraceMinutes": _minutes("reentryGraceMinutes",
                                        DEFAULT_SESSION_SECURITY["reentryGraceMinutes"]),
    }


@router.get("/session-config")
def session_config(session: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    oidc = studio_oidc_config(session)
    return {
        "sessionSecurity": session_security_policy(session),
        "oidc": {
            "enabled": oidc is not None,
            "issuer": (oidc or {}).get("issuer") if oidc else None,
            "clientId": (oidc or {}).get("clientId") if oidc else None,
        },
    }


@router.get("/session")
def session_whoami(user: User = Depends(get_current_user)):
    """Re-entry probe: 200 (with the resolved identity) while the
    caller's token still authenticates; 401 otherwise (raised by
    get_current_user before this runs)."""
    return {
        "username": user.username,
        "role": user.role.value,
        "patient_ref": user.patient_ref,
    }
