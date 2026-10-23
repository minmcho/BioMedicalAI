"""Secure (confidential) printing — Phase 17, WS-COMP (BD-56 / CMP-34).

The hold/release queue in front of the existing print engine
(app.reports: report runs + invoice / receipt / statement / superbill
documents on the published integration/print letterhead):

- A print job naming a document is created through this queue. When
  the job is marked CONFIDENTIAL and the published
  integration/secure-print config has hold enabled, the job is created
  **HELD** — nothing prints. A non-confidential job (or hold disabled
  by config) is created READY.
- **Release requires the owner's identity**: the caller releasing a
  job must BE the job's owner — identity is the authenticated account
  (bearer token -> User), standing in for the badge tap at the
  printer. A release attempt by anyone else is refused 403 and the
  refusal itself is audited. (Badge hardware is site infrastructure;
  the identity check here is the control.)
- **Unreleased jobs expire**: expiry is the published config's
  `holdExpiryHours` from creation. Expiry is computed on every read
  and by the explicit sweep; an expired job can never be released
  (409), and the transition is audited.
- **Every step is audited**: create / release / expire / refused
  release all write hash-chained audit rows (`printqueue.*`), on top
  of app.reports' own `reports.document.print` audit when the
  released document is actually rendered through /reports.

Studio config surfaces: integration/secure-print (hold enabled,
expiry hours, confidential document types), integration/print (the
letterhead the underlying documents render on, app.reports),
permissions/role-matrix (capabilities printqueue.create /
printqueue.read).
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import Boolean, DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .audit import append_audit
from .configstore import (CAPABILITY_DEFAULTS, list_config_versions,
                          published_config, role_allowed)
from .db import Base
from .models import ConfigStatus, StudioConfig, User, utcnow
from .security import get_current_user, get_db

router = APIRouter(prefix="/print-queue", tags=["print-queue"])

SECURE_PRINT_KEY = "secure-print"

_CAP_FALLBACKS = {
    "printqueue.create": ("admin", "configurator", "clinician"),
    "printqueue.read": ("admin", "configurator", "clinician", "viewer"),
}


def _cap_allowed(session: Session, user: User, capability: str) -> bool:
    cfg = published_config(session, "permissions", "role-matrix")
    if cfg is not None:
        overrides = ((cfg.payload or {}).get("capabilities") or {}).get(capability)
        if isinstance(overrides, dict) and user.role.value in overrides:
            return bool(overrides[user.role.value])
    if capability in CAPABILITY_DEFAULTS:
        return role_allowed(session, user.role, capability)
    return user.role.value in _CAP_FALLBACKS.get(capability, ())


def _require(capability: str):
    def dep(session: Session = Depends(get_db),
            user: User = Depends(get_current_user)) -> User:
        if not _cap_allowed(session, user, capability):
            raise HTTPException(
                status_code=403,
                detail=f"Role {user.role.value} lacks capability '{capability}'")
        return user
    return dep


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class PrintJob(Base):
    __tablename__ = "print_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    document_type: Mapped[str] = mapped_column(String(80), nullable=False)
    document_ref: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    owner: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    confidential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ready")  # held|ready|released|expired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


def default_secure_print_payload() -> dict:
    return {"holdEnabled": True,
            "holdExpiryHours": 8,
            "confidentialDocumentTypes": ["statement", "superbill", "invoice",
                                          "receipt", "report"],
            "notes": ("Secure-print policy: confidential jobs hold until "
                      "the owner releases them at the printer by identity; "
                      "unreleased held jobs expire after holdExpiryHours.")}


def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
    if list_config_versions(session, kind, key):
        return
    row = StudioConfig(kind=kind, key=key, version=1, status=ConfigStatus.published,
                       payload=payload, created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:{kind}/{key}@v1",
                 payload={"kind": kind, "key": key, "version": 1, "seeded_default": True})


def ensure_print_queue_defaults(session: Session) -> None:
    _seed_published(session, "integration", SECURE_PRINT_KEY, default_secure_print_payload())
    session.commit()


def _ensure(session: Session) -> None:
    if not list_config_versions(session, "integration", SECURE_PRINT_KEY):
        ensure_print_queue_defaults(session)


def _policy(session: Session) -> dict:
    cfg = published_config(session, "integration", SECURE_PRINT_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    hours = payload.get("holdExpiryHours")
    return {"hold_enabled": payload.get("holdEnabled") is True,
            "expiry_hours": (hours if isinstance(hours, int) and not isinstance(hours, bool)
                             and hours > 0 else 8),
            "source": (f"integration/{SECURE_PRINT_KEY}@v{cfg.version}"
                       if cfg is not None else "built-in defaults")}


def _refresh_expiry(session: Session, job: PrintJob, actor: str) -> PrintJob:
    """Expire a held job whose window has passed (audited once)."""
    if job.status == "held" and job.expires_at is not None \
            and _now() >= _aware(job.expires_at):
        job.status = "expired"
        session.flush()
        append_audit(session, actor=actor, action="printqueue.expire",
                     entity=f"printjob:{job.job_id}",
                     payload={"job_id": job.job_id, "owner": job.owner,
                              "document_type": job.document_type,
                              "document_ref": job.document_ref})
    return job


def _job_out(job: PrintJob) -> dict:
    return {"job_id": job.job_id, "document_type": job.document_type,
            "document_ref": job.document_ref, "title": job.title,
            "owner": job.owner, "confidential": job.confidential,
            "status": job.status,
            "created_at": _aware(job.created_at).isoformat() if job.created_at else None,
            "expires_at": _aware(job.expires_at).isoformat() if job.expires_at else None,
            "released_at": _aware(job.released_at).isoformat() if job.released_at else None,
            "released_by": job.released_by}


def _row(session: Session, job_id: str) -> PrintJob:
    row = session.scalars(select(PrintJob).where(PrintJob.job_id == job_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Print job '{job_id}' not found")
    return row


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


@router.get("/policy")
def policy(session: Session = Depends(get_db),
           user: User = Depends(_require("printqueue.read"))):
    _ensure(session)
    return _policy(session)


@router.post("/jobs", status_code=201)
async def create_job(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(_require("printqueue.create"))):
    _ensure(session)
    body = await _json_body(request)
    doc_type = body.get("document_type") or body.get("documentType")
    if not isinstance(doc_type, str) or not doc_type.strip():
        raise HTTPException(status_code=422, detail="'document_type' is required")
    confidential = body.get("confidential")
    if not isinstance(confidential, bool):
        confidential = False
    pol = _policy(session)
    held = bool(confidential and pol["hold_enabled"])
    now = _now()
    job = PrintJob(
        job_id=f"prn-{uuid.uuid4().hex[:12]}", document_type=doc_type.strip(),
        document_ref=str(body.get("document_ref") or body.get("documentRef") or ""),
        title=str(body.get("title") or ""), owner=user.username,
        confidential=confidential, status="held" if held else "ready",
        expires_at=(now + timedelta(hours=pol["expiry_hours"])) if held else None,
        note=(str(body["note"]) if isinstance(body.get("note"), str) else None))
    session.add(job)
    session.flush()
    append_audit(session, actor=user.username, action="printqueue.create",
                 entity=f"printjob:{job.job_id}",
                 payload={"job_id": job.job_id, "owner": job.owner,
                          "document_type": job.document_type,
                          "document_ref": job.document_ref,
                          "confidential": confidential, "status": job.status,
                          "policy_source": pol["source"]})
    session.commit()
    return _job_out(job)


@router.get("/jobs")
def list_jobs(session: Session = Depends(get_db),
              user: User = Depends(_require("printqueue.read"))):
    """The caller's own jobs (admin/configurator see all — the print
    room's operators). Expiry is refreshed on read."""
    _ensure(session)
    rows = session.scalars(select(PrintJob).order_by(PrintJob.id.asc())).all()
    if user.role.value not in ("admin", "configurator"):
        rows = [r for r in rows if r.owner == user.username]
    for row in rows:
        _refresh_expiry(session, row, user.username)
    session.commit()
    return {"items": [_job_out(r) for r in rows], "total": len(rows)}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, session: Session = Depends(get_db),
            user: User = Depends(_require("printqueue.read"))):
    _ensure(session)
    job = _refresh_expiry(session, _row(session, job_id), user.username)
    session.commit()
    if job.owner != user.username and user.role.value not in ("admin", "configurator"):
        raise HTTPException(status_code=404, detail=f"Print job '{job_id}' not found")
    return _job_out(job)


@router.post("/jobs/{job_id}/release")
def release_job(job_id: str, session: Session = Depends(get_db),
                user: User = Depends(_require("printqueue.create"))):
    """Release a held job at the printer. Identity control: ONLY the
    owner may release — the authenticated account IS the badge. A
    wrong-owner attempt is refused 403 AND audited; an expired job is
    refused 409; a released job cannot be released twice (409)."""
    _ensure(session)
    job = _row(session, job_id)
    _refresh_expiry(session, job, user.username)
    if job.status == "expired":
        session.commit()
        raise HTTPException(status_code=409,
                            detail=f"Print job {job_id} expired unreleased at "
                                   f"{_aware(job.expires_at).isoformat()}; submit a new job")
    if job.owner != user.username:
        append_audit(session, actor=user.username, action="printqueue.release.refused",
                     entity=f"printjob:{job_id}",
                     payload={"job_id": job_id, "owner": job.owner,
                              "attempted_by": user.username,
                              "reason": "release requires the job owner's identity"})
        session.commit()
        raise HTTPException(status_code=403,
                            detail="Release requires the job owner's identity "
                                   "(badge/account): only the owner may release this job")
    if job.status == "released":
        raise HTTPException(status_code=409, detail=f"Print job {job_id} was already released")
    job.status = "released"
    job.released_at = _now()
    job.released_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="printqueue.release",
                 entity=f"printjob:{job_id}",
                 payload={"job_id": job_id, "owner": job.owner,
                          "document_type": job.document_type,
                          "document_ref": job.document_ref})
    session.commit()
    out = _job_out(job)
    out["print_authorised"] = True
    out["note"] = ("Released by the owner. Render/collect the document "
                   "through the reports print path (app.reports), whose "
                   "own reports.document.print audit fires at render.")
    return out


@router.post("/expire-due")
def expire_due(session: Session = Depends(get_db),
               user: User = Depends(_require("printqueue.create"))):
    """The expiry sweep: every held job past its configured window
    expires (audited). Returns what expired in this sweep."""
    _ensure(session)
    rows = session.scalars(select(PrintJob).where(PrintJob.status == "held")).all()
    expired = []
    for row in rows:
        before = row.status
        _refresh_expiry(session, row, user.username)
        if before == "held" and row.status == "expired":
            expired.append(row.job_id)
    session.commit()
    return {"expired": expired, "total": len(expired)}
