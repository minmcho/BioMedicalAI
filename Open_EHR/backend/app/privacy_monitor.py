"""Privacy monitoring: access-pattern analytics over the audit chain
— Phase 17, BD-52 (CMP-31).

Break-the-glass (app.privacy, Phase 10) reviews EMERGENCY access at
100%. This module is the other half of a real privacy programme:
routine snooping detection. Staff looking at charts they have no
care relationship with — a celebrity (VIP) admission, a coworker's
chart, a family member's chart, a quiet bulk browse, a 3 a.m.
pattern — rarely touches break-glass at all; it hides in ordinary
audited access. The monitor reads the hash-chained audit and holds
it against governed detection rules:

RULES (published Studio config, kind=integration,
key=privacy-monitoring; thresholds/windows are data, never code):

- vip_access — a chart-touch of a VIP-flagged patient (FHIR meta.tag
  code "VIP" or the urn:opencare:patient-vip extension) by someone
  with NO treatment relationship to that patient.
- coworker_access — the patient is themselves a staff member
  (chart name matches the workforce directory) and the accessor has
  no treatment relationship.
- family_access — the patient shares the accessor's surname and the
  accessor has no treatment relationship (the classic same-last-name
  snooping heuristic; a heuristic — it opens a review case, it does
  not accuse).
- volume_anomaly — one actor touches more than
  `maxDistinctPatients` distinct patients inside `windowHours`.
- after_hours — one actor accumulates at least `minEvents`
  chart-touches inside the configured after-hours window
  (startHour..endHour, UTC) in one night.

TREATMENT RELATIONSHIP (what makes access ordinary, and keeps the
false-positive rate honest): the accessor is the patient's PCP of
record (active empanelment), OR is an active member of a team with
an ACTIVE assignment to the patient, OR held an active break-glass
grant for the patient at the event time. Break-glass openings
themselves never trigger the relationship rules — they are already
reviewed at 100% in the Phase 10 queue; case evidence links them
instead. Volume / after-hours rules are relationship-agnostic
(patterns, not single accesses) but exclude system actors and
break-glass openings.

WHAT COUNTS AS A CHART-TOUCH: an audit-chain row resolvable to a
patient — entity `Patient/<id>`, entity `<Type>/<id>` whose FHIR
row projects that patient, or a payload carrying patient /
patient_ref. Stated limitation: FHIR by-id GET reads are not
audit-logged in this system (app.fhir logs writes; reads are
evidenced through the surfaces that DO audit: messages.read,
break-glass, orders/lab/etc. writes), so the monitor covers the
audited access surface — the same surface a surveyor can verify.

OUTPUT: cases land in the privacy workqueue (PrivacyCase rows,
alongside the break-glass review queue — same officer capability,
privacy.monitor = privacy.review's role set) and are dispositioned
there (confirmed / dismissed / false_positive). Runs are idempotent:
a case's dedupe_key makes re-running over the same window a no-op.
The run first VERIFIES the hash chain and reports chain_valid —
analytics over a broken chain are labelled, never silently trusted.

Synthetic data only, like the rest of the system.
"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import append_audit, verify_chain
from .configstore import (ConfigStatus, list_config_versions,
                          published_config)
from .models import (AuditLog, BreakGlassAccess, Empanelment,
                     FhirResource, PrivacyCase, StaffMember, StudioConfig,
                     TeamMembership, TeamPatientAssignment, User)
from .security import get_db, require_capability

router = APIRouter(prefix="/privacy-monitor", tags=["privacy-monitor"])

CONFIG_KIND = "integration"
CONFIG_KEY = "privacy-monitoring"

DEFAULT_RULES = {
    "vipAccess": {"enabled": True},
    "coworkerAccess": {"enabled": True},
    "familyAccess": {"enabled": True},
    "volumeAnomaly": {"enabled": True, "maxDistinctPatients": 20,
                      "windowHours": 24},
    "afterHours": {"enabled": True, "startHour": 22, "endHour": 6,
                   "minEvents": 3},
}

_PATIENT_ENTITY = re.compile(r"^Patient/([^/]+)$")
_TYPED_ENTITY = re.compile(r"^([A-Za-z]+)/([^/]+)$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(
        tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Governed rules (published Studio config: integration/privacy-monitoring)
# ---------------------------------------------------------------------------

def default_monitoring_payload() -> dict:
    return {
        "enabled": True,
        "lookbackHours": 720,
        "rules": json.loads(json.dumps(DEFAULT_RULES)),
        "notes": ("Snooping / access-pattern detection rules (BD-52/"
                  "CMP-31). Thresholds and windows are governed data: "
                  "publish changes through Studio; the monitor reads "
                  "the published version on every run. Cases are "
                  "review work items for the privacy officer — a case "
                  "is a prompt to review, never a finding."),
    }


def ensure_privacy_monitor_defaults(session: Session) -> None:
    if list_config_versions(session, CONFIG_KIND, CONFIG_KEY):
        return
    session.add(StudioConfig(
        kind=CONFIG_KIND, key=CONFIG_KEY, version=1,
        status=ConfigStatus.published,
        payload=default_monitoring_payload(),
        created_by="system", updated_by="system"))
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:{CONFIG_KIND}/{CONFIG_KEY}@v1",
                 payload={"kind": CONFIG_KIND, "key": CONFIG_KEY,
                          "version": 1, "seeded_default": True})
    session.commit()


def effective_policy(session: Session) -> dict:
    cfg = published_config(session, CONFIG_KIND, CONFIG_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    rules_in = payload.get("rules") if isinstance(
        payload.get("rules"), dict) else {}
    rules: dict = {}
    for name, defaults in DEFAULT_RULES.items():
        merged = dict(defaults)
        override = rules_in.get(name)
        if isinstance(override, dict):
            for key, value in override.items():
                if key == "enabled" and isinstance(value, bool):
                    merged[key] = value
                elif key in ("maxDistinctPatients", "windowHours",
                             "startHour", "endHour", "minEvents") \
                        and isinstance(value, int) \
                        and not isinstance(value, bool) and value > 0:
                    merged[key] = value
        rules[name] = merged
    lookback = payload.get("lookbackHours")
    if isinstance(lookback, bool) or not isinstance(lookback, int) \
            or lookback <= 0:
        lookback = 720
    return {
        "enabled": payload.get("enabled") is not False,
        "lookback_hours": lookback,
        "rules": rules,
        "source": (f"{CONFIG_KIND}/{CONFIG_KEY}@v{cfg.version}"
                   if cfg is not None else
                   "built-in defaults (monitoring config not yet seeded)"),
    }


# ---------------------------------------------------------------------------
# Chart-touch extraction from the hash-chained audit
# ---------------------------------------------------------------------------

def _patient_name(body: dict | None) -> tuple[str, str]:
    """(family, given) from a FHIR Patient body; ('','') otherwise."""
    if not isinstance(body, dict):
        return "", ""
    names = body.get("name")
    if isinstance(names, list) and names and isinstance(names[0], dict):
        family = str(names[0].get("family") or "")
        given_list = names[0].get("given") or []
        given = str(given_list[0]) if isinstance(
            given_list, list) and given_list else ""
        return family, given
    return "", ""


def _norm_name(text: str) -> str:
    return re.sub(r"[^a-z]", "", (text or "").lower())


def _is_vip(body: dict | None) -> bool:
    if not isinstance(body, dict):
        return False
    tags = (body.get("meta") or {}).get("tag") or []
    if any(isinstance(t, dict) and str(t.get("code", "")).upper() == "VIP"
           for t in tags):
        return True
    for ext in body.get("extension") or []:
        if isinstance(ext, dict) and str(ext.get("url", "")).endswith(
                "patient-vip"):
            value = ext.get("valueBoolean", ext.get("valueString"))
            if value is True or (isinstance(value, str)
                                 and value.lower() == "true"):
                return True
    return False


class _Context:
    """Relationship + identity maps for one analysis run."""

    def __init__(self, session: Session):
        self.staff_by_username: dict[str, StaffMember] = {}
        self.staff_names: dict[str, str] = {}  # normalised name -> staff_id
        for row in session.scalars(select(StaffMember)).all():
            if row.username:
                self.staff_by_username[row.username] = row
            norm = _norm_name(row.full_name)
            if norm:
                self.staff_names[norm] = row.staff_id
        self.patients: dict[str, dict] = {}
        for row in session.scalars(select(FhirResource).where(
                FhirResource.resource_type == "Patient")).all():
            self.patients[f"Patient/{row.resource_id}"] = row.body or {}
        self.resource_patient: dict[tuple[str, str], str | None] = {}
        for row in session.scalars(select(FhirResource)).all():
            self.resource_patient[(row.resource_type, row.resource_id)] = \
                row.patient_ref
        self.pcp_of: dict[str, str] = {}
        for row in session.scalars(select(Empanelment).where(
                Empanelment.status == "active")).all():
            self.pcp_of[row.patient_ref] = row.pcp_staff_id
        members: dict[str, set[str]] = {}
        for row in session.scalars(select(TeamMembership).where(
                TeamMembership.status == "active")).all():
            members.setdefault(row.team_id, set()).add(row.staff_id)
        self.team_staff_of_patient: dict[str, set[str]] = {}
        for row in session.scalars(select(TeamPatientAssignment).where(
                TeamPatientAssignment.state == "active")).all():
            self.team_staff_of_patient.setdefault(
                row.patient_ref, set()).update(
                members.get(row.team_id, set()))
        self.breakglass: list[BreakGlassAccess] = list(
            session.scalars(select(BreakGlassAccess)).all())

    def has_relationship(self, actor: str, patient_ref: str,
                         at: datetime) -> bool:
        staff = self.staff_by_username.get(actor)
        if staff is not None:
            if self.pcp_of.get(patient_ref) == staff.staff_id:
                return True
            if staff.staff_id in self.team_staff_of_patient.get(
                    patient_ref, set()):
                return True
        for grant in self.breakglass:
            if grant.actor == actor and grant.patient_ref == patient_ref:
                opened, expires = _aware(grant.opened_at), _aware(
                    grant.expires_at)
                if opened and expires and opened <= at <= expires:
                    return True
        return False

    def breakglass_for(self, actor: str, patient_ref: str) -> list[dict]:
        out = []
        for grant in self.breakglass:
            if grant.actor == actor and grant.patient_ref == patient_ref:
                out.append({
                    "access_id": grant.access_id,
                    "opened_at": _aware(grant.opened_at).isoformat()
                    if grant.opened_at else None,
                    "reviewed": grant.reviewed_by is not None,
                    "review_outcome": grant.review_outcome})
        return out


def _resolve_patient(ctx: _Context, row: AuditLog) -> str | None:
    entity = row.entity or ""
    m = _PATIENT_ENTITY.match(entity)
    if m:
        return f"Patient/{m.group(1)}"
    payload = row.payload if isinstance(row.payload, dict) else {}
    for key in ("patient", "patient_ref"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith("Patient/"):
            return value
    m = _TYPED_ENTITY.match(entity)
    if m:
        return ctx.resource_patient.get((m.group(1), m.group(2)))
    return None


def collect_events(session: Session, ctx: _Context,
                   since: datetime) -> list[dict]:
    events = []
    for row in session.scalars(
            select(AuditLog).order_by(AuditLog.id.asc())).all():
        at = _aware(row.created_at)
        if at is None or at < since or row.actor == "system":
            continue
        patient_ref = _resolve_patient(ctx, row)
        if patient_ref is None:
            continue
        events.append({
            "audit_id": row.id, "actor": row.actor, "action": row.action,
            "patient_ref": patient_ref, "at": at,
            "via_breakglass": row.action.startswith("breakglass."),
        })
    return events


# ---------------------------------------------------------------------------
# The analysis run
# ---------------------------------------------------------------------------

def _dedupe(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts)
                          .encode("utf-8")).hexdigest()


def _open_case(session: Session, opened_by: str, rule: str, actor: str,
               patient_ref: str | None, severity: str, dedupe_key: str,
               evidence: dict, existing: set[str],
               opened: list[dict]) -> None:
    if dedupe_key in existing:
        return
    case = PrivacyCase(
        case_id=f"pcase-{uuid.uuid4().hex[:12]}", rule=rule, actor=actor,
        patient_ref=patient_ref, severity=severity, status="open",
        dedupe_key=dedupe_key, evidence=evidence, opened_by=opened_by)
    session.add(case)
    session.flush()
    existing.add(dedupe_key)
    opened.append(_case_out(case))


def _case_out(row: PrivacyCase) -> dict:
    return {
        "case_id": row.case_id, "rule": row.rule, "actor": row.actor,
        "patient_ref": row.patient_ref, "severity": row.severity,
        "status": row.status, "evidence": row.evidence or {},
        "opened_by": row.opened_by,
        "opened_at": _aware(row.created_at).isoformat()
        if row.created_at else None,
        "disposition": row.disposition,
        "disposition_by": row.disposition_by,
        "disposition_at": _aware(row.disposition_at).isoformat()
        if row.disposition_at else None,
        "disposition_note": row.disposition_note,
    }


def run_analysis(session: Session, actor_username: str,
                 now: datetime | None = None) -> dict:
    """Scan the audit chain under the published rules and open
    workqueue cases. Callable (the endpoint wraps this) and
    idempotent (dedupe keys). Returns the run summary."""
    ensure_privacy_monitor_defaults(session)
    policy = effective_policy(session)
    now = _aware(now) or _now()
    summary = {"ran_at": now.isoformat(), "ran_by": actor_username,
               "policy_source": policy["source"],
               "chain_valid": verify_chain(session),
               "events_scanned": 0, "cases_opened": [], "cases": [],
               "already_open": 0, "by_rule": {}}
    if not policy["enabled"]:
        summary["note"] = "monitoring disabled by published config"
        return summary
    ctx = _Context(session)
    since = now - timedelta(hours=policy["lookback_hours"])
    events = collect_events(session, ctx, since)
    summary["events_scanned"] = len(events)
    existing = {c.dedupe_key for c in session.scalars(
        select(PrivacyCase).where(PrivacyCase.status == "open")).all()}
    opened: list[dict] = []
    rules = policy["rules"]

    # --- Relationship rules (single-access, per actor+patient) -----
    grouped: dict[tuple[str, str], list[dict]] = {}
    for event in events:
        if event["via_breakglass"]:
            continue
        grouped.setdefault((event["actor"], event["patient_ref"]),
                           []).append(event)
    for (actor, patient_ref), touches in sorted(grouped.items()):
        body = ctx.patients.get(patient_ref)
        first_at = min(t["at"] for t in touches)
        related = ctx.has_relationship(actor, patient_ref, first_at)
        evidence_base = {
            "touches": len(touches),
            "audit_ids": [t["audit_id"] for t in touches][:50],
            "first_at": first_at.isoformat(),
            "last_at": max(t["at"] for t in touches).isoformat(),
            "break_glass": ctx.breakglass_for(actor, patient_ref),
            "detection": "synthetic audit data; review before acting",
        }
        if related:
            continue
        staff = ctx.staff_by_username.get(actor)
        family, given = _patient_name(body)
        if rules["vipAccess"]["enabled"] and _is_vip(body):
            _open_case(
                session, actor_username, "vip_access", actor, patient_ref,
                "high", _dedupe("vip_access", actor, patient_ref),
                {**evidence_base, "signal": "VIP-flagged patient, no "
                 "treatment relationship"}, existing, opened)
        patient_norm = _norm_name(f"{given} {family}")
        if rules["coworkerAccess"]["enabled"] and patient_norm \
                and patient_norm in ctx.staff_names:
            _open_case(
                session, actor_username, "coworker_access", actor,
                patient_ref, "high",
                _dedupe("coworker_access", actor, patient_ref),
                {**evidence_base, "signal": "patient is a staff member "
                 f"({ctx.staff_names[patient_norm]}), no treatment "
                 "relationship"}, existing, opened)
        if rules["familyAccess"]["enabled"] and staff is not None \
                and family:
            surname = _norm_name(staff.full_name.split()[-1]
                                 if staff.full_name.split() else "")
            if surname and _norm_name(family) == surname \
                    and patient_norm != _norm_name(staff.full_name):
                _open_case(
                    session, actor_username, "family_access", actor,
                    patient_ref, "medium",
                    _dedupe("family_access", actor, patient_ref),
                    {**evidence_base, "signal": "patient shares the "
                     "accessor's surname, no treatment relationship"},
                    existing, opened)

    # --- Volume anomaly (per actor, per window bucket) -------------
    vol = rules["volumeAnomaly"]
    if vol["enabled"]:
        window_h = vol["windowHours"]
        buckets: dict[tuple[str, int], dict] = {}
        for event in events:
            if event["via_breakglass"]:
                continue
            bucket = int(event["at"].timestamp() // (window_h * 3600))
            slot = buckets.setdefault((event["actor"], bucket),
                                      {"patients": set(), "ids": []})
            slot["patients"].add(event["patient_ref"])
            slot["ids"].append(event["audit_id"])
        for (actor, bucket), slot in sorted(buckets.items()):
            if len(slot["patients"]) > vol["maxDistinctPatients"]:
                _open_case(
                    session, actor_username, "volume_anomaly", actor,
                    None, "high",
                    _dedupe("volume_anomaly", actor, bucket, window_h),
                    {"distinct_patients": len(slot["patients"]),
                     "threshold": vol["maxDistinctPatients"],
                     "window_hours": window_h,
                     "patients": sorted(slot["patients"])[:50],
                     "audit_ids": slot["ids"][:50],
                     "detection": "synthetic audit data; review "
                     "before acting"}, existing, opened)

    # --- After-hours (per actor, per night) ------------------------
    ah = rules["afterHours"]
    if ah["enabled"]:
        start_h, end_h = ah["startHour"], ah["endHour"]

        def _after_hours(at: datetime) -> bool:
            if start_h <= end_h:
                return start_h <= at.hour < end_h
            return at.hour >= start_h or at.hour < end_h

        nights: dict[tuple[str, str], dict] = {}
        for event in events:
            if event["via_breakglass"] or not _after_hours(event["at"]):
                continue
            night = event["at"].date().isoformat()
            if end_h <= start_h and event["at"].hour < end_h:
                night = (event["at"] - timedelta(days=1)).date().isoformat()
            slot = nights.setdefault((event["actor"], night),
                                     {"patients": set(), "ids": [],
                                      "count": 0})
            slot["patients"].add(event["patient_ref"])
            slot["ids"].append(event["audit_id"])
            slot["count"] += 1
        for (actor, night), slot in sorted(nights.items()):
            if slot["count"] >= ah["minEvents"]:
                _open_case(
                    session, actor_username, "after_hours", actor, None,
                    "medium", _dedupe("after_hours", actor, night),
                    {"night": night, "events": slot["count"],
                     "threshold": ah["minEvents"],
                     "window": f"{start_h:02d}:00-{end_h:02d}:00 UTC",
                     "patients": sorted(slot["patients"])[:50],
                     "audit_ids": slot["ids"][:50],
                     "detection": "synthetic audit data; review "
                     "before acting"}, existing, opened)

    summary["cases_opened"] = opened
    summary["cases"] = opened
    summary["cases_opened_count"] = len(opened)
    for case in opened:
        summary["by_rule"][case["rule"]] = \
            summary["by_rule"].get(case["rule"], 0) + 1
    append_audit(session, actor=actor_username,
                 action="privacy.monitor.run", entity="privacy-monitor",
                 payload={"events_scanned": summary["events_scanned"],
                          "cases_opened": len(opened),
                          "by_rule": summary["by_rule"],
                          "chain_valid": summary["chain_valid"],
                          "policy_source": policy["source"]})
    session.commit()
    return summary


# ---------------------------------------------------------------------------
# API — the privacy workqueue surface (alongside /privacy break-glass)
# ---------------------------------------------------------------------------

@router.get("/policy")
def policy(session: Session = Depends(get_db),
           user: User = Depends(require_capability("privacy.monitor"))):
    ensure_privacy_monitor_defaults(session)
    return effective_policy(session)


@router.post("/run")
async def run(request: Request, session: Session = Depends(get_db),
              user: User = Depends(require_capability("privacy.monitor"))):
    """Run the analysis now. Body may carry {"now": iso8601} to run
    as-of a past instant (backfill / drill); default is now."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    now = None
    if isinstance(body, dict) and isinstance(body.get("now"), str):
        try:
            now = datetime.fromisoformat(body["now"].replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422,
                                detail="'now' must be an ISO-8601 datetime")
    return run_analysis(session, user.username, now=now)


@router.get("/cases")
def list_cases(status: str | None = Query(default="open"),
               session: Session = Depends(get_db),
               user: User = Depends(require_capability("privacy.monitor"))):
    """The monitoring workqueue: cases opened by the detection rules,
    oldest first. Sits alongside GET /privacy/review-queue (the
    break-glass queue) — same officer, two queues, one discipline."""
    stmt = select(PrivacyCase).order_by(PrivacyCase.id.asc())
    if status:
        stmt = stmt.where(PrivacyCase.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_case_out(r) for r in rows], "total": len(rows),
            "break_glass_queue": "/privacy/review-queue"}


@router.post("/cases/{case_id}/disposition")
async def disposition(case_id: str, request: Request,
                      session: Session = Depends(get_db),
                      user: User = Depends(
                          require_capability("privacy.monitor"))):
    """Disposition one case: confirmed | dismissed | false_positive
    (+ optional note). Dispositioning twice -> 409; audited."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422,
                            detail="Request body must be valid JSON")
    outcome = (body or {}).get("outcome")
    if outcome not in ("confirmed", "dismissed", "false_positive"):
        raise HTTPException(
            status_code=422,
            detail="'outcome' must be confirmed | dismissed | "
                   "false_positive")
    row = session.scalars(select(PrivacyCase).where(
        PrivacyCase.case_id == case_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Privacy case {case_id} not found")
    if row.status != "open":
        raise HTTPException(
            status_code=409,
            detail=f"Case {case_id} is already {row.status}")
    row.status = "closed"
    row.disposition = outcome
    row.disposition_by = user.username
    row.disposition_at = _now()
    note = (body or {}).get("note")
    row.disposition_note = note if isinstance(note, str) else None
    session.flush()
    append_audit(session, actor=user.username,
                 action="privacy.monitor.disposition",
                 entity=row.patient_ref or f"privacy-case:{case_id}",
                 payload={"case_id": case_id, "rule": row.rule,
                          "case_actor": row.actor, "outcome": outcome})
    session.commit()
    return _case_out(row)
