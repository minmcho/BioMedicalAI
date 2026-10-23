"""Privacy operations: break-the-glass + review queue — Phase 10, Track K.

Break-the-glass is the HIPAA emergency-access procedure
(45 CFR 164.312(a)(2)(ii)) as an operable workflow:

- OPEN always works in the moment for authorised clinical roles
  (`privacy.breakglass`: admin/clinician) — availability is a safety
  property — and always demands a reason (no reason, no opening, 422).
- Access is TIME-BOXED: the duration comes from configuration (the
  published HIPAA regulatory pack's `breakGlassDurationMinutes`
  parameter; built-in default 60 minutes until a site publishes it).
- Every opening writes a FLAGGED hash-chained audit row
  (`breakglass.open`) and lands in the privacy review queue.
- Review is a separate role set (`privacy.review`:
  admin/configurator) and the expectation is 100% — the queue lists
  EVERY unreviewed opening, unsampled, with the justification window
  (pack parameter `justificationWindowHours`, default 48h, the
  24–48h institutional pattern) shown as a per-item due time and an
  overdue flag. Review records outcome (justified / unjustified) and
  is itself audited (`breakglass.review`).
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir
from .audit import append_audit
from .configstore import published_config
from .models import BreakGlassAccess, User
from .security import get_db, require_capability

router = APIRouter(prefix="/privacy", tags=["privacy"])

DEFAULT_DURATION_MINUTES = 60
DEFAULT_JUSTIFICATION_WINDOW_HOURS = 48


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes for DateTime(timezone=True)
    columns; normalise to aware UTC before comparing (the pattern
    app.inpatient uses for the same storage)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def effective_policy(session: Session) -> dict:
    """The break-glass parameters in force: the published HIPAA
    regulatory pack's parameters when a site has published them,
    built-in defaults otherwise. The source is reported so the UI (and
    a surveyor) can see whether the numbers are governed config or the
    shipped defaults."""
    cfg = published_config(session, "regulatory_pack", "hipaa-baseline")
    params = (cfg.payload or {}).get("parameters") if cfg is not None else {}
    params = params if isinstance(params, dict) else {}
    duration = params.get("breakGlassDurationMinutes")
    window = params.get("justificationWindowHours")
    return {
        "duration_minutes": (duration if isinstance(duration, int)
                             and not isinstance(duration, bool) and duration > 0
                             else DEFAULT_DURATION_MINUTES),
        "justification_window_hours": (window if isinstance(window, int)
                                       and not isinstance(window, bool)
                                       and window > 0
                                       else DEFAULT_JUSTIFICATION_WINDOW_HOURS),
        "review_expectation": "100% — every break-glass opening is reviewed; "
                              "the queue is never sampled",
        "source": (f"regulatory_pack/hipaa-baseline@v{cfg.version}"
                   if cfg is not None else "built-in defaults (no HIPAA "
                   "regulatory pack published yet)"),
    }


def _access_out(row: BreakGlassAccess, now: datetime) -> dict:
    expires = _aware(row.expires_at)
    due = _aware(row.justification_due_at)
    reviewed = row.reviewed_by is not None
    return {
        "access_id": row.access_id,
        "patient_ref": row.patient_ref,
        "actor": row.actor,
        "reason": row.reason,
        "opened_at": _aware(row.opened_at).isoformat() if row.opened_at else None,
        "expires_at": expires.isoformat() if expires else None,
        "active": bool(expires and now < expires),
        "justification_due_at": due.isoformat() if due else None,
        "reviewed": reviewed,
        "reviewed_by": row.reviewed_by,
        "reviewed_at": (_aware(row.reviewed_at).isoformat()
                        if row.reviewed_at else None),
        "review_outcome": row.review_outcome,
        "review_note": row.review_note,
        "overdue": bool(not reviewed and due and now > due),
    }


@router.get("/break-glass/policy")
def policy(session: Session = Depends(get_db),
           user: User = Depends(require_capability("privacy.review"))):
    """The effective break-glass policy: time-box duration,
    justification window, the 100%-review expectation, and where the
    numbers came from (governed config vs built-in defaults)."""
    return effective_policy(session)


@router.post("/break-glass", status_code=201)
async def open_break_glass(request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("privacy.breakglass"))):
    """Open emergency access to one patient's chart. A reason is
    mandatory; the opening is flagged in the audit chain and queued
    for mandatory review in the same transaction."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    patient_ref = body.get("patient_ref") or body.get("patient")
    if not isinstance(patient_ref, str) or not patient_ref.strip():
        raise HTTPException(status_code=422, detail="Break-glass requires 'patient_ref'")
    patient_ref = fhir._normalize_patient_ref(patient_ref.strip())
    fhir._get_row("Patient", patient_ref.split("/", 1)[1], session)  # 404 if unknown
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise HTTPException(
            status_code=422,
            detail="Break-glass requires a 'reason' — emergency access is "
                   "never opened without a stated justification")
    now = _now()
    pol = effective_policy(session)
    import uuid
    row = BreakGlassAccess(
        access_id=str(uuid.uuid4()), patient_ref=patient_ref,
        actor=user.username, reason=reason.strip(), opened_at=now,
        expires_at=now + timedelta(minutes=pol["duration_minutes"]),
        justification_due_at=now + timedelta(
            hours=pol["justification_window_hours"]))
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="breakglass.open",
                 entity=patient_ref,
                 payload={"access_id": row.access_id, "patient": patient_ref,
                          "reason": row.reason, "flagged": True,
                          "expires_at": row.expires_at.isoformat(),
                          "justification_due_at": row.justification_due_at.isoformat(),
                          "policy_source": pol["source"]})
    session.commit()
    return _access_out(row, now)


@router.get("/break-glass/mine")
def my_openings(session: Session = Depends(get_db),
                user: User = Depends(require_capability("privacy.breakglass"))):
    """The caller's own break-glass openings (their review state is
    visible to them: an opening you cannot justify is your problem
    first)."""
    rows = session.scalars(
        select(BreakGlassAccess).where(BreakGlassAccess.actor == user.username)
        .order_by(BreakGlassAccess.id.desc())).all()
    now = _now()
    return {"items": [_access_out(r, now) for r in rows], "total": len(rows)}


@router.get("/review-queue")
def review_queue(session: Session = Depends(get_db),
                 user: User = Depends(require_capability("privacy.review"))):
    """The privacy review queue: EVERY unreviewed opening (the 100%
    rule — nothing is sampled away), oldest first, overdue items
    flagged against their justification window."""
    rows = session.scalars(
        select(BreakGlassAccess).where(BreakGlassAccess.reviewed_by.is_(None))
        .order_by(BreakGlassAccess.id.asc())).all()
    now = _now()
    items = [_access_out(r, now) for r in rows]
    return {"items": items, "total": len(items),
            "overdue": sum(1 for i in items if i["overdue"]),
            "policy": effective_policy(session)}


@router.post("/review-queue/{access_id}/review")
async def review(access_id: str, request: Request,
                 session: Session = Depends(get_db),
                 user: User = Depends(require_capability("privacy.review"))):
    """Record the review of one opening: outcome justified or
    unjustified (+ optional note). Reviewing twice -> 409; the review
    itself is hash-chain audited."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    outcome = (body or {}).get("outcome")
    if outcome not in ("justified", "unjustified"):
        raise HTTPException(
            status_code=422,
            detail="'outcome' must be 'justified' or 'unjustified'")
    row = session.scalars(select(BreakGlassAccess).where(
        BreakGlassAccess.access_id == access_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Break-glass access {access_id} not found")
    if row.reviewed_by is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Access {access_id} was already reviewed by {row.reviewed_by}")
    row.reviewed_by = user.username
    row.reviewed_at = _now()
    row.review_outcome = outcome
    note = (body or {}).get("note")
    row.review_note = note if isinstance(note, str) else None
    session.flush()
    append_audit(session, actor=user.username, action="breakglass.review",
                 entity=row.patient_ref,
                 payload={"access_id": access_id, "patient": row.patient_ref,
                          "opened_by": row.actor, "outcome": outcome})
    session.commit()
    return _access_out(row, _now())
