"""Privileging — the shared machinery (Workforce, Teams & Panels
pass, app.privileging).

The medical-staff-office core that assignment gates stand on:

- **Job catalogue** (published `terminology/job-catalogue`): every
  job role in the workforce directory, whether it is clinical, the
  credentials the role requires, the account role a holder gets at
  onboarding, and the privilege template the role starts from.
- **Privileges** (published `terminology/privileges`): the privilege
  catalogue — code, display, which job roles are eligible, and which
  credentials must be verified — plus the
  per-service-line requirements (a service line may demand extra
  privileges on top of the base one, e.g. critical-care demands
  icu-practice). "Verified" is enforced, not asserted (Wave A,
  WF-B1): a credential satisfies a check only when it is on file,
  unexpired, AND its computed primary-source-verification state
  (app.people: latest PSV event + exclusion checks against the
  published workforce `psv` config) is "verified" — current PSV, no
  exclusion hit, and every demanded exclusion kind clear.
- **Teams config** (published `terminology/teams`): team types,
  team roles, panel weighting and panel thresholds — consumed by
  app.teams, seeded here alongside the other two documents.

`check_privileges` is THE gate: given a staff row and the privilege
codes an assignment demands, it returns the list of problems (empty
= pass). Every refusal names its reason — unknown privilege,
ineligible job role, missing grant, missing credential, expired
credential, unverified credential, stale PSV, exclusion hit,
missing exclusion check — and the callers audit the refusal, so a
clinician is never silently unassigned: they (and the medical staff
office) can see exactly what to fix. Credentials are checked at the
LATEST expiry of each required type: a renewed credential supersedes
the lapsed one it replaced; the credential carrying that latest
expiry is the one whose verification state is judged.

All three documents ride the EXISTING `terminology` kind (no new
config kind): each payload carries a `concepts` list mirroring its
primary catalogue, which keeps it a valid, republishable
terminology document through the governed Studio API — the Phase 12
continuum-ops / Phase 13 workforce pattern.
"""
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import people
from .audit import append_audit
from .configstore import published_config
from .models import StaffCredential, StaffMember, StaffPrivilege

JOB_CATALOGUE_KEY = "job-catalogue"
PRIVILEGES_KEY = "privileges"
TEAMS_KEY = "teams"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Seed payloads (published terminology documents)
# ---------------------------------------------------------------------------

def default_job_catalogue_payload() -> dict:
    """The job catalogue: all 11 workforce roles. `clinical` marks
    the roles that deliver care (and so can hold privileges);
    `requiredCredentials` are the credentials onboarding must see
    verified before the person is activated; `defaultAccountRole`
    is the sign-in role the linked account is created with;
    `privilegeTemplate` is the privilege set the medical staff
    office starts the role's appraisal from."""
    def role(job_role, display, clinical, credentials, account_role,
             template):
        return {"jobRole": job_role, "display": display,
                "clinical": clinical, "requiredCredentials": credentials,
                "defaultAccountRole": account_role,
                "privilegeTemplate": template}

    roles = [
        role("physician", "Physician", True, ["medical-licence"],
             "clinician", ["attending-of-record"]),
        role("nurse", "Nurse", True, ["rn-licence"], "clinician", []),
        role("pharmacist", "Pharmacist", True, ["pharmacist-licence"],
             "clinician", []),
        role("lab-technologist", "Laboratory technologist", True,
             ["technologist-licence"], "clinician", []),
        role("radiographer", "Radiographer", True, [], "clinician", []),
        role("unit-manager", "Unit manager", False, ["rn-licence"],
             "viewer", []),
        role("department-head", "Department head", False, [],
             "configurator", []),
        role("executive", "Executive", False, [], "admin", []),
        role("admin-staff", "Administrative staff", False, [],
             "viewer", []),
        role("support-staff", "Support staff", False, [], "viewer", []),
        role("front-desk", "Front desk / registration", False, [],
             "viewer", []),
    ]
    return {
        "concepts": [
            {"code": r["jobRole"], "display": r["display"], "active": True}
            for r in roles],
        "roles": roles,
    }


def default_privileges_payload() -> dict:
    """The privilege catalogue + per-service-line requirements.
    `eligibleRoles` are the job roles that may hold the privilege;
    `requiredCredentials` must be on file, unexpired and in the
    computed PSV "verified" state for a grant to satisfy a check.
    `serviceRequirements` maps a service line to
    the extra privilege codes any assignment in that line demands."""
    def priv(code, display, eligible, credentials):
        return {"code": code, "display": display,
                "eligibleRoles": eligible,
                "requiredCredentials": credentials}

    privileges = [
        priv("attending-of-record", "Attending of record",
             ["physician"], ["medical-licence"]),
        priv("icu-practice", "ICU practice",
             ["physician"], ["medical-licence"]),
        priv("primary-care-panel", "Primary care panel",
             ["physician"], ["medical-licence"]),
        priv("prescribe-controlled-substances",
             "Prescribe controlled substances",
             ["physician"], ["medical-licence", "dea-registration"]),
    ]
    return {
        "concepts": [
            {"code": p["code"], "display": p["display"], "active": True}
            for p in privileges],
        "privileges": privileges,
        "serviceRequirements": {"critical-care": ["icu-practice"]},
    }


def default_teams_payload() -> dict:
    """Team types, the roles a member can hold on a team, panel
    weighting (how much one empanelled patient counts, by age band,
    per active condition, with a frailty uplift) and the panel
    capacity thresholds the panels endpoint judges against."""
    team_types = [
        {"code": "unit", "display": "Unit team"},
        {"code": "service-line", "display": "Service-line team"},
        {"code": "consult-service", "display": "Consult service"},
        {"code": "primary-care", "display": "Primary care team"},
    ]
    return {
        "concepts": [
            {"code": t["code"], "display": t["display"], "active": True}
            for t in team_types],
        "teamTypes": team_types,
        "teamRoles": ["attending", "resident", "rn", "pharmacist",
                      "case-manager", "care-coordinator", "pcp", "member"],
        "panelWeights": {
            "base": 1.0,
            "ageBands": [
                {"min": 0, "max": 17, "weight": 0.8},
                {"min": 18, "max": 64, "weight": 1.0},
                {"min": 65, "max": 84, "weight": 1.4},
                {"min": 85, "max": None, "weight": 1.8},
            ],
            "perCondition": 0.25,
            "frailtyExtra": 0.75,
            "frailtyAge": 85,
            "frailtyConditionCodes": [],
        },
        "panelThresholds": {"maxWeighted": 15.0, "minWeighted": 2.0},
    }


# ---------------------------------------------------------------------------
# Published-config accessors (payload else the built-in default)
# ---------------------------------------------------------------------------

def _published_payload(session: Session, key: str, default) -> dict:
    cfg = published_config(session, "terminology", key)
    if cfg is not None and isinstance(cfg.payload, dict) and cfg.payload:
        return cfg.payload
    return default()


def job_catalogue(session: Session) -> dict:
    return _published_payload(session, JOB_CATALOGUE_KEY,
                              default_job_catalogue_payload)


def privileges_config(session: Session) -> dict:
    return _published_payload(session, PRIVILEGES_KEY,
                              default_privileges_payload)


def teams_config(session: Session) -> dict:
    return _published_payload(session, TEAMS_KEY, default_teams_payload)


def _catalogue_entry(cfg: dict, section: str, key_field: str,
                     code: str) -> dict | None:
    for entry in cfg.get(section) or []:
        if isinstance(entry, dict) and entry.get(key_field) == code:
            return entry
    return None


def job_role_entry(session: Session, job_role: str) -> dict | None:
    return _catalogue_entry(job_catalogue(session), "roles", "jobRole",
                            job_role)


def privilege_entry(cfg: dict, code: str) -> dict | None:
    return _catalogue_entry(cfg, "privileges", "code", code)


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------

def active_grants(session: Session, staff_id: str) -> list[StaffPrivilege]:
    """The staff member's ACTIVE privilege grants (revoked grants
    are history, never authority)."""
    return list(session.scalars(
        select(StaffPrivilege).where(
            StaffPrivilege.staff_id == staff_id,
            StaffPrivilege.status == "active")
        .order_by(StaffPrivilege.granted_at.asc(),
                  StaffPrivilege.id.asc())).all())


def _grant_satisfies(grant: StaffPrivilege, code: str,
                     service_line: str | None) -> bool:
    """A grant satisfies a check when its code matches and its scope
    covers the check's service line: an unscoped grant covers every
    line; a scoped grant covers only its own line; a check with no
    line accepts any grant of the code."""
    if grant.privilege_code != code:
        return False
    return (grant.service_line is None or service_line is None
            or grant.service_line == service_line)


def grant_privilege(session: Session, actor: str, staff_id: str,
                    code: str, service_line: str | None = None,
                    note: str = "") -> StaffPrivilege:
    """Record a privilege grant (flush only — the caller commits).
    The row is the medical staff office's act: who granted, when,
    under what scope. Eligibility/credential fitness is judged by
    check_privileges at assignment time, not here — a grant made
    ahead of a credential renewal is legitimate office workflow."""
    row = StaffPrivilege(
        grant_id=f"grant-{uuid.uuid4().hex[:10]}", staff_id=staff_id,
        privilege_code=code, service_line=service_line, status="active",
        granted_by=actor, granted_at=_now(), note=note or "")
    session.add(row)
    session.flush()
    append_audit(session, actor=actor, action="teams.privilege.grant",
                 entity=f"staff:{staff_id}",
                 payload={"grant_id": row.grant_id, "privilege": code,
                          "service_line": service_line})
    return row


def revoke_privilege(session: Session, actor: str, grant_id: str,
                     reason: str) -> StaffPrivilege:
    """Revoke a grant (flush only — the caller commits). Revocation
    is a state change on the row; the grant, its granter and its
    revoker all stay on record."""
    row = session.scalars(select(StaffPrivilege).where(
        StaffPrivilege.grant_id == grant_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Privilege grant '{grant_id}' not found")
    if row.status == "revoked":
        raise HTTPException(
            status_code=409,
            detail=f"Privilege grant '{grant_id}' is already revoked")
    row.status = "revoked"
    row.revoked_by = actor
    row.revoked_at = _now()
    if reason:
        row.note = reason
    session.flush()
    append_audit(session, actor=actor, action="teams.privilege.revoke",
                 entity=f"staff:{row.staff_id}",
                 payload={"grant_id": grant_id,
                          "privilege": row.privilege_code,
                          "reason": reason})
    return row


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def attending_privilege_codes(session: Session,
                              service_line: str | None) -> list[str]:
    """The privilege codes an attending assignment demands: the base
    attending-of-record plus the service line's published extra
    requirements (none when no line is given)."""
    codes = ["attending-of-record"]
    if service_line:
        cfg = privileges_config(session)
        requirements = cfg.get("serviceRequirements") or {}
        extra = requirements.get(service_line) or []
        for code in extra:
            if isinstance(code, str) and code not in codes:
                codes.append(code)
    return codes


def check_privileges(session: Session, staff_row: StaffMember,
                     privilege_codes: list[str],
                     service_line: str | None = None) -> list[str]:
    """Judge a staff member against the demanded privileges.

    Returns the list of problems (empty = pass). The effective code
    set is `privilege_codes` plus the published service-line
    requirements when a service line is given. For each code, in
    order: an unknown code is a problem; a job role outside the
    privilege's eligible roles is a problem naming both; no
    satisfying ACTIVE grant is a problem naming the privilege.
    Credential fitness is judged over the UNION of the demanded
    privileges' required credentials: for each type, no credential
    on file is a problem, and a LATEST expiry before today (the
    people._days_to_expiry semantics — a credential expiring today
    is still valid) is a problem naming the type and its date.

    Wave A (WF-B1): an unexpired credential must ALSO be verified.
    The credential carrying the latest expiry is judged by its
    computed verification state (app.people._verification_block):
    "unverified" (self-asserted only, or latest PSV outcome not
    verified), "verification-expired" (PSV older than the published
    reverification window) and "exclusion-hit" each add a problem
    naming the type (and, for a hit, the kind). When the demanded
    privileges require any credential, the published
    `psv.requireExclusionKinds` are demanded for the staff member:
    a kind whose latest check is missing / not-run, or a hit not
    already named above, is a problem naming the kind."""
    cfg = privileges_config(session)
    effective: list[str] = []
    for code in privilege_codes or []:
        if isinstance(code, str) and code not in effective:
            effective.append(code)
    if service_line:
        requirements = cfg.get("serviceRequirements") or {}
        for code in requirements.get(service_line) or []:
            if isinstance(code, str) and code not in effective:
                effective.append(code)
    grants = active_grants(session, staff_row.staff_id)
    problems: list[str] = []
    required_credentials: list[str] = []
    for code in effective:
        entry = privilege_entry(cfg, code)
        if entry is None:
            problems.append(f"unknown privilege: {code}")
            continue
        display = entry.get("display") or code
        eligible = entry.get("eligibleRoles") or []
        if staff_row.job_role not in eligible:
            problems.append(
                f"job role '{staff_row.job_role}' is not eligible for "
                f"privilege: {display} ({code})")
        if not any(_grant_satisfies(g, code, service_line) for g in grants):
            problems.append(f"missing privilege: {display} ({code})")
        for cred_type in entry.get("requiredCredentials") or []:
            if isinstance(cred_type, str) \
                    and cred_type not in required_credentials:
                required_credentials.append(cred_type)
    credentials = session.scalars(select(StaffCredential).where(
        StaffCredential.staff_id == staff_row.staff_id)).all()
    wf_ops = people._ops(session)
    psv = people._psv_config(wf_ops)
    named_hit_kinds: set[str] = set()
    for cred_type in required_credentials:
        held = [c for c in credentials if c.type == cred_type]
        if not held:
            problems.append(f"no {cred_type} on file")
            continue
        latest = max(c.expires_on for c in held)
        days = people._days_to_expiry(latest)
        if days is not None and days < 0:
            problems.append(f"{cred_type} expired on {latest}")
            continue
        # WF-B1: the credential carrying the latest expiry must be
        # in the computed "verified" state — unexpired alone is a
        # self-asserted claim, not proof.
        candidate = max(held, key=lambda c: (c.expires_on, c.id or 0))
        block = people._verification_block(session, candidate, wf_ops)
        state = block["state"]
        if state == "exclusion-hit":
            for kind, outcome in (block.get("exclusions") or {}).items():
                if outcome == "hit":
                    problems.append(
                        f"no verified {cred_type} on file "
                        f"(exclusion hit: {kind})")
                    named_hit_kinds.add(kind)
        elif state == "verification-expired":
            problems.append(
                f"no verified {cred_type} on file "
                f"(PSV expired — re-verify)")
        elif state != "verified":
            problems.append(
                f"no verified {cred_type} on file "
                f"(credential unverified)")
    if required_credentials:
        # WF-B1: the exclusion kinds the published psv config demands
        # must each have a CLEAR latest check for this staff member.
        exclusions = people._latest_exclusions(session,
                                               staff_row.staff_id)
        for kind in psv["requireExclusionKinds"]:
            outcome = exclusions.get(kind)
            if outcome == "clear":
                continue
            if outcome == "hit":
                if kind not in named_hit_kinds:
                    problems.append(f"exclusion hit: {kind}")
            else:
                problems.append(f"missing exclusion check: {kind}")
    return problems
