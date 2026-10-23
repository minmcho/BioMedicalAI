"""People & workforce — Phase 13, Track E (app.people).

The workforce core the rest of the platform stands on:

- **Staff directory**: doctors, nurses, allied health, support and
  management as real data — job role, unit assignment, service-line
  assignment, status, and the dashboard audience the role-aware
  landing resolves for the person.
- **Credentials & licences**: one record per licence/certification
  per staff member. Expiry state (valid / expiring / expired) is
  COMPUTED at read time against the published workforce config
  windows — a config republication re-windows every credential at
  once. The expiry worklist is the credential set inside the worklist
  window, soonest first. A scan emits `workforce.credential.expiring`
  into the rules engine for every credential inside the alert window
  not yet alerted for its current expiry date (idempotent); the
  seeded published rule turns the urgent ones into renewal Tasks.
- **Primary-source verification** (Wave A, WF-B1): a credential row
  is SELF-ASSERTED — the holder's claim, not proof. Proof is a
  CredentialVerification event (source, method, verifier, date,
  outcome, evidence) plus CredentialExclusionCheck records
  (licence-board / NPDB / OIG / SAM class, recorded manual acts).
  Verification state is COMPUTED at read time from the latest event
  and the latest exclusion outcome per kind against the published
  `psv` config section — verified / verification-expired /
  unverified / exclusion-hit — and app.privileging.check_privileges
  requires the "verified" state: the system no longer asserts a
  verification it never performed.
- **Rosters**: shift templates are published config; instances pin a
  staff member to a unit on a date under a template, with start/end
  instants computed at creation (history never rewrites).
- **On-call**: per-service assignments (primary/backup) with overlap
  refusal; `GET /people/on-call/now` is the lookup other modules use
  to answer "who is on call for cardiology right now".

Analytics endpoints (all under /people/analytics) compute from LIVE
module data — ADT events, bed state (Phase 6), roster rows, safety
events, the quality engine over the warehouse — never fixtures. They
are the data endpoints behind the seeded audience dashboards (kind
`dashboard`), which are published Studio config like every other
dashboard: clinician day, nurse/unit, unit manager, department head,
executive. `GET /people/home-dashboard` resolves the caller's
audience (staff directory link by username, else the published
role->audience map) and returns the published dashboard config for
it, or dashboard: null — the shell then falls back to its ordinary
home, exactly as before Phase 13.
"""
import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import inpatient
from . import validate_fields as vf
from .audit import append_audit
from .configstore import (list_config_versions, published_config,
                          published_configs)
from .models import (AdtEvent, ComplianceRequirement,
                     CredentialExclusionCheck, CredentialVerification,
                     FhirResource, OnCallAssignment, RosterShift,
                     SafetyEvent, StaffCredential, StaffMember, User)
from .rules import evaluate_rules
from .security import get_db, require_capability

router = APIRouter(prefix="/people", tags=["people"])

WORKFORCE_KEY = "workforce"

STAFF_STATUSES = ("active", "leave", "inactive")
ON_CALL_ROLES = ("primary", "backup")
SHIFT_STATUSES = ("scheduled", "worked", "called-out", "swapped")

# Job role -> dashboard audience, used when a directory entry carries
# no explicit audience. The published config's audienceDashboards map
# then resolves audience -> dashboard config key.
JOB_ROLE_AUDIENCE = {
    "physician": "clinician",
    "pharmacist": "clinician",
    "lab-technologist": "clinician",
    "radiographer": "clinician",
    "nurse": "nurse",
    "unit-manager": "unit-manager",
    "department-head": "department-head",
    "executive": "executive",
    # Front Door pass: registration/access staff land on the
    # front-desk audience home (today's appointments, arrivals,
    # waitlist, registrations + the register/book quick actions).
    "front-desk": "front-desk",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> date:
    return _now().date()


# ---------------------------------------------------------------------------
# Workforce config (published terminology/workforce)
# ---------------------------------------------------------------------------

def default_workforce_payload() -> dict:
    return {
        # The job-role value set rides as `concepts` so the document
        # stays a valid, republishable terminology config through the
        # governed API (the Phase 12 continuum-ops pattern).
        "concepts": [
            {"code": code, "display": display, "active": True}
            for code, display in (
                ("physician", "Physician"),
                ("nurse", "Nurse"),
                ("pharmacist", "Pharmacist"),
                ("lab-technologist", "Laboratory technologist"),
                ("radiographer", "Radiographer"),
                ("unit-manager", "Unit manager"),
                ("department-head", "Department head"),
                ("executive", "Executive"),
                ("admin-staff", "Administrative staff"),
                ("support-staff", "Support staff"),
                ("front-desk", "Front desk / registration"),
            )
        ],
        "units": [
            {"code": "unit-medsurg", "display": "Medical-Surgical",
             "serviceLine": "medicine", "minOnShift": 3},
            {"code": "unit-icu", "display": "Intensive Care",
             "serviceLine": "critical-care", "minOnShift": 2},
            {"code": "ed", "display": "Emergency Department",
             "serviceLine": "emergency", "minOnShift": 3},
            {"code": "clinic", "display": "Outpatient Clinic",
             "serviceLine": "medicine", "minOnShift": 2},
        ],
        "serviceLines": [
            {"code": "medicine", "display": "Medicine"},
            {"code": "critical-care", "display": "Critical Care"},
            {"code": "emergency", "display": "Emergency Medicine"},
            {"code": "surgery", "display": "Surgery"},
            {"code": "pharmacy", "display": "Pharmacy"},
            {"code": "laboratory", "display": "Laboratory"},
        ],
        "shiftTemplates": [
            {"code": "day", "display": "Day (07:00-19:00)",
             "start": "07:00", "end": "19:00"},
            {"code": "night", "display": "Night (19:00-07:00)",
             "start": "19:00", "end": "07:00"},
            {"code": "clinic-day", "display": "Clinic day (08:00-17:00)",
             "start": "08:00", "end": "17:00"},
        ],
        "credentialTypes": [
            {"code": "medical-licence", "display": "Medical licence"},
            {"code": "rn-licence", "display": "Registered nurse licence"},
            {"code": "pharmacist-licence", "display": "Pharmacist licence"},
            {"code": "technologist-licence",
             "display": "Laboratory technologist licence"},
            {"code": "dea-registration", "display": "DEA registration"},
            {"code": "board-certification", "display": "Board certification"},
            {"code": "bls", "display": "BLS certification"},
            {"code": "acls", "display": "ACLS certification"},
        ],
        # Primary-source verification (Wave A, WF-B1): the governed
        # PSV source list, the exclusion-check kinds, how long a
        # verification stays current, and the exclusion kinds a
        # privilege check demands a clear record for. Codes are
        # validated against THIS published section, never hard-coded
        # lists in endpoint logic.
        "psv": {
            "sources": [
                {"code": "state-licence-board",
                 "display": "State licence board"},
                {"code": "certifying-board",
                 "display": "Certifying board"},
                {"code": "medical-school",
                 "display": "Medical / professional school"},
                {"code": "npdb-query",
                 "display": "National Practitioner Data Bank query"},
                {"code": "dea-registration",
                 "display": "DEA registration verification"},
                {"code": "employer-reference",
                 "display": "Employer reference"},
            ],
            "exclusionKinds": [
                {"code": "licence-board",
                 "display": "Licence board exclusion / discipline"},
                {"code": "npdb", "display": "NPDB"},
                {"code": "oig", "display": "OIG LEIE exclusions"},
                {"code": "sam", "display": "SAM.gov exclusions"},
            ],
            "reverificationDays": 730,
            "requireExclusionKinds": ["licence-board", "oig", "sam"],
        },
        # Expiry windows (days): the worklist shows everything inside
        # the worklist window (plus the already-expired); the scan
        # alerts inside the alert window.
        "expiry": {"worklistWindowDays": 90, "alertWindowDays": 30},
        "audienceDashboards": {
            "clinician": "audience-clinician-day",
            "nurse": "audience-nurse-unit",
            "unit-manager": "audience-unit-manager",
            "department-head": "audience-department-head",
            "executive": "audience-executive",
            "front-desk": "audience-front-desk",
        },
        "audienceByRole": {
            "admin": "executive",
            "configurator": "department-head",
            "clinician": "clinician",
            "viewer": "unit-manager",
        },
    }


def _ops(session: Session) -> dict:
    cfg = published_config(session, "terminology", WORKFORCE_KEY)
    return (cfg.payload or {}) if cfg is not None else default_workforce_payload()


def _config_codes(ops: dict, section: str) -> set[str]:
    return {str(entry["code"]) for entry in ops.get(section) or []
            if isinstance(entry, dict) and entry.get("code")}


## Organisation & Registration Placement pass: the governed
## region -> location -> department -> unit hierarchy lives in
## app.org_hierarchy (published terminology/locations). It is imported
## defensively: while that module is absent the placement helpers
## degrade to legacy behaviour (no hierarchy validation), and every
## placement surface activates unchanged once it lands.
try:
    from . import org_hierarchy as _org_hierarchy
except ImportError:  # pragma: no cover - concurrent workstream
    _org_hierarchy = None

PLACEMENT_LEVELS = ("region", "location", "department", "unit")


def _placement_of(row) -> dict:
    """The row's placement codes {region, location, department,
    unit} (None for unplaced levels / pre-placement rows)."""
    return {level: getattr(row, level, None) for level in PLACEMENT_LEVELS}


def _normalize_placement(raw: dict) -> dict:
    """Type/shape-normalise a placement dict: every level must be a
    string code (blank -> None); anything else is a 422 naming the
    level."""
    placement: dict = {}
    for level in PLACEMENT_LEVELS:
        value = raw.get(level)
        if value is not None:
            if not isinstance(value, str):
                raise HTTPException(
                    status_code=422,
                    detail=f"'{level}' must be a string")
            value = value.strip() or None
        placement[level] = value
    return placement


def _validate_placement(session: Session, placement: dict, *,
                        require_all: bool = False) -> dict:
    """Validate a placement chain via app.org_hierarchy
    (validate_placement): 422s name the missing level, the unknown
    code or the broken link. Returns the normalised placement."""
    placement = _normalize_placement(placement)
    if _org_hierarchy is None:
        return placement
    return _org_hierarchy.validate_placement(
        session, placement, require_all=require_all)


def _placement_view(session: Session, placement: dict | None) -> dict:
    """{level: {"code", "display"} | None} for a placement, via
    app.org_hierarchy.placement_view (code-only fallback while the
    hierarchy module is unavailable)."""
    placement = _normalize_placement(placement or {})
    if _org_hierarchy is not None and any(placement.values()):
        return _org_hierarchy.placement_view(session, placement)
    return {level: ({"code": code, "display": code} if code else None)
            for level, code in placement.items()}


def _resolve_scope(session: Session, region: str | None = None,
                   location: str | None = None,
                   department: str | None = None,
                   unit: str | None = None) -> set[str] | None:
    """Placement filter -> the set of unit codes in scope (None =
    unscoped), via app.org_hierarchy.resolve_scope: unknown ids and
    broken chains are 422s naming them."""
    if _org_hierarchy is not None:
        return _org_hierarchy.resolve_scope(
            session, region=region, location=location,
            department=department, unit=unit)
    if region is None and location is None and department is None \
            and unit is None:
        return None
    raise HTTPException(
        status_code=503,
        detail="Organisational hierarchy is not available in this "
               "deployment")


def _concept_level(session: Session, code: str) -> str | None:
    """The hierarchy level of a concept code ("region" / "location" /
    "department" / "unit"), or None when the code is not an ACTIVE
    concept in the published locations hierarchy."""
    if _org_hierarchy is not None:
        hierarchy = _org_hierarchy.load_hierarchy(session)
        entry = (hierarchy.get("by_code") or {}).get(code)
        return entry.get("level") if entry else None
    cfg = published_config(session, "terminology", "locations")
    if cfg is None:
        return None
    type_level = {"region": "region", "facility": "location",
                  "department": "department", "unit": "unit"}
    for concept in (cfg.payload or {}).get("concepts") or []:
        if isinstance(concept, dict) and concept.get("code") == code \
                and concept.get("active") is not False:
            return type_level.get(concept.get("type"))
    return None


def _expiry_windows(ops: dict) -> tuple[int, int]:
    expiry = ops.get("expiry") or {}
    worklist = expiry.get("worklistWindowDays")
    alert = expiry.get("alertWindowDays")
    return (int(worklist) if isinstance(worklist, int) and worklist > 0 else 90,
            int(alert) if isinstance(alert, int) and alert > 0 else 30)


def _days_to_expiry(expires_on: str, today: date | None = None) -> int | None:
    try:
        expiry = date.fromisoformat(expires_on)
    except (ValueError, TypeError):
        return None
    return (expiry - (today or _today())).days


def _credential_status(days: int | None, worklist_window: int) -> str:
    if days is None:
        return "unknown"
    if days < 0:
        return "expired"
    if days <= worklist_window:
        return "expiring"
    return "valid"


# ---------------------------------------------------------------------------
# Primary-source verification (Wave A, WF-B1)
#
# A credential row is self-asserted. Its verification state is
# COMPUTED at read time (the same posture as expiry state): from the
# latest CredentialVerification event, the latest exclusion outcome
# per kind for the staff member, and the published `psv` config
# section (sources / exclusion kinds / re-verification window /
# demanded exclusion kinds). Nothing about verification is
# hand-stored on the credential.
# ---------------------------------------------------------------------------

VERIFICATION_METHODS = ("direct-source-query", "source-document-review",
                        "source-attestation-call")
VERIFICATION_OUTCOMES = ("verified", "unable-to-verify", "discrepancy")
EXCLUSION_OUTCOMES = ("clear", "hit", "not-run")


def _default_psv() -> dict:
    return default_workforce_payload()["psv"]


def _psv_config(ops: dict) -> dict:
    """The normalised `psv` section of the workforce config: the
    governed source list, exclusion-kind list, re-verification
    window (days) and the exclusion kinds privilege checks demand.
    A published payload that predates WF-B1 (no `psv` key) or
    malforms a field falls back per-field to the built-in default —
    the privileging._published_payload posture — so validation is
    always against a governed list, never a hard-coded one."""
    default = _default_psv()
    raw = ops.get("psv") if isinstance(ops, dict) else None
    if not isinstance(raw, dict):
        raw = {}

    def entries(section: str) -> list[dict]:
        value = raw.get(section)
        if not isinstance(value, list):
            return list(default[section])
        out = [{"code": str(e["code"]),
                "display": str(e.get("display") or e["code"])}
               for e in value if isinstance(e, dict) and e.get("code")]
        return out or list(default[section])

    days = raw.get("reverificationDays")
    required = raw.get("requireExclusionKinds")
    return {
        "sources": entries("sources"),
        "exclusionKinds": entries("exclusionKinds"),
        "reverificationDays": (days if isinstance(days, int) and days > 0
                               else default["reverificationDays"]),
        "requireExclusionKinds": (
            [k for k in required if isinstance(k, str)]
            if isinstance(required, list)
            else list(default["requireExclusionKinds"])),
    }


def _psv_codes(psv: dict, section: str) -> set[str]:
    return {entry["code"] for entry in psv.get(section) or []}


def _latest_verification(session: Session,
                         credential_id: str) -> CredentialVerification | None:
    """The credential's latest PSV event: newest verified_on wins;
    ties break by creation order (the later record supersedes)."""
    rows = session.scalars(select(CredentialVerification).where(
        CredentialVerification.credential_id == credential_id)).all()
    if not rows:
        return None
    return max(rows, key=lambda r: (r.verified_on or "",
                                    r.created_at or _now(), r.id or 0))


def _latest_exclusions(session: Session, staff_id: str) -> dict[str, str]:
    """{kind: outcome} — the latest outcome per exclusion kind for a
    staff member. Exclusions are person-level: a check recorded
    against one credential (or none) speaks for the person, so every
    check for the staff member counts, latest per kind by
    (checked_on, created_at, id)."""
    rows = session.scalars(select(CredentialExclusionCheck).where(
        CredentialExclusionCheck.staff_id == staff_id)).all()
    latest: dict[str, CredentialExclusionCheck] = {}
    for row in rows:
        current = latest.get(row.kind)
        if current is None or (row.checked_on or "",
                               row.created_at or _now(),
                               row.id or 0) >= (current.checked_on or "",
                                                current.created_at
                                                or _now(),
                                                current.id or 0):
            latest[row.kind] = row
    return {kind: row.outcome for kind, row in latest.items()}


def _verification_block(session: Session | None, row: StaffCredential,
                        ops: dict) -> dict:
    """The credential's computed verification block (Wave A, WF-B1).

    state: "exclusion-hit" when any exclusion kind's latest outcome
    for the staff member is a hit (named distinctly, regardless of
    PSV); else "unverified" when there is no PSV event or the latest
    event's outcome is not "verified"; else "verification-expired"
    when the latest verified event is older than the published
    reverificationDays window; else "verified". With no session the
    block cannot be computed and reads "unverified" (callers that
    render credentials pass their session; the onboarding surface's
    legacy 3-arg call keeps working and never claims verified)."""
    psv = _psv_config(ops)
    window = psv["reverificationDays"]
    if session is None:
        return {"state": "unverified", "outcome": None, "source": None,
                "method": None, "verified_by": None, "verified_on": None,
                "evidence": None, "reverification_days": window,
                "exclusions": {}}
    exclusions = _latest_exclusions(session, row.staff_id)
    latest = _latest_verification(session, row.credential_id)
    base = {"outcome": latest.outcome if latest else None,
            "source": latest.source_code if latest else None,
            "method": latest.method if latest else None,
            "verified_by": latest.verified_by if latest else None,
            "verified_on": latest.verified_on if latest else None,
            "evidence": latest.evidence if latest else None,
            "reverification_days": window,
            "exclusions": exclusions}
    if any(outcome == "hit" for outcome in exclusions.values()):
        return {"state": "exclusion-hit", **base}
    if latest is None or latest.outcome != "verified":
        return {"state": "unverified", **base}
    try:
        verified_date = date.fromisoformat(latest.verified_on)
    except (ValueError, TypeError):
        return {"state": "unverified", **base}
    if (_today() - verified_date).days > window:
        return {"state": "verification-expired", **base}
    return {"state": "verified", **base}


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _staff_out(row: StaffMember) -> dict:
    return {"staff_id": row.staff_id, "username": row.username,
            "full_name": row.full_name, "job_role": row.job_role,
            "unit": row.unit, "service_line": row.service_line,
            "region": getattr(row, "region", None),
            "location": getattr(row, "location", None),
            "department": getattr(row, "department", None),
            "status": row.status, "audience": row.audience,
            "email": row.email, "phone": row.phone}


def _credential_out(row: StaffCredential, ops: dict,
                    staff_names: dict[str, str],
                    session: Session | None = None) -> dict:
    worklist_window, _alert = _expiry_windows(ops)
    days = _days_to_expiry(row.expires_on)
    return {"credential_id": row.credential_id, "staff_id": row.staff_id,
            "staff_name": staff_names.get(row.staff_id),
            "type": row.type, "number": row.number,
            "issued_on": row.issued_on, "expires_on": row.expires_on,
            "days_to_expiry": days,
            "status": _credential_status(days, worklist_window),
            "verification": _verification_block(session, row, ops)}


def _staff_names(session: Session) -> dict[str, str]:
    return {row.staff_id: row.full_name for row in
            session.scalars(select(StaffMember)).all()}


def _get_staff(session: Session, staff_id: str) -> StaffMember:
    row = session.scalars(select(StaffMember).where(
        StaffMember.staff_id == staff_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Staff member '{staff_id}' not found")
    return row


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


def _require_str(body: dict, field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422,
                            detail=f"'{field}' is required and must be a non-empty string")
    return value


# ---------------------------------------------------------------------------
# Config surface for the web pickers
# ---------------------------------------------------------------------------

@router.get("/config")
def workforce_config(session: Session = Depends(get_db),
                     user: User = Depends(require_capability("workforce.read"))):
    """The published workforce config (units, service lines, shift
    templates, credential types, expiry windows, audience maps) — the
    same document the server validates against."""
    return _ops(session)


# ---------------------------------------------------------------------------
# Staff directory
# ---------------------------------------------------------------------------

@router.get("/staff")
def list_staff(unit: str | None = Query(default=None),
               service_line: str | None = Query(default=None),
               job_role: str | None = Query(default=None),
               status: str | None = Query(default=None),
               region: str | None = Query(default=None),
               location: str | None = Query(default=None),
               department: str | None = Query(default=None),
               session: Session = Depends(get_db),
               user: User = Depends(require_capability("workforce.read"))):
    stmt = select(StaffMember).order_by(StaffMember.full_name.asc())
    if unit:
        stmt = stmt.where(StaffMember.unit == unit)
    # Organisation placement filters (exact code match on the
    # placement columns, alongside the legacy filters above).
    for param, level in ((region, "region"), (location, "location"),
                         (department, "department")):
        if param:
            column = getattr(StaffMember, level, None)
            if column is None:
                raise HTTPException(
                    status_code=503,
                    detail="Organisational placement is not available "
                           "in this deployment")
            stmt = stmt.where(column == param)
    if service_line:
        stmt = stmt.where(StaffMember.service_line == service_line)
    if job_role:
        stmt = stmt.where(StaffMember.job_role == job_role)
    if status:
        stmt = stmt.where(StaffMember.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_staff_out(r) for r in rows], "total": len(rows)}


@router.get("/staff/{staff_id}")
def get_staff(staff_id: str, session: Session = Depends(get_db),
              user: User = Depends(require_capability("workforce.read"))):
    row = _get_staff(session, staff_id)
    ops = _ops(session)
    names = _staff_names(session)
    credentials = session.scalars(
        select(StaffCredential).where(StaffCredential.staff_id == staff_id)
        .order_by(StaffCredential.expires_on.asc())).all()
    # Workforce, Teams & Panels pass: the staffer's active care-team
    # memberships ride on their directory record (app.teams owns the
    # team tables; imported here to keep the module graph acyclic).
    from .models import CareTeam, TeamMembership
    memberships = session.scalars(
        select(TeamMembership).where(
            TeamMembership.staff_id == staff_id,
            TeamMembership.status == "active")).all()
    team_names = {t.team_id: t.name for t in session.scalars(
        select(CareTeam)).all()}
    teams = [{"team_id": m.team_id, "team_name": team_names.get(m.team_id),
              "role_in_team": m.role_in_team} for m in memberships]
    return {**_staff_out(row),
            "credentials": [_credential_out(c, ops, names, session=session) for c in credentials],
            "teams": teams}


@router.post("/staff", status_code=201)
async def create_staff(request: Request, session: Session = Depends(get_db),
                       user: User = Depends(require_capability("workforce.work"))):
    body = await _json_body(request)
    ops = _ops(session)
    full_name = _require_str(body, "full_name")
    job_role = _require_str(body, "job_role")
    if job_role not in _config_codes(ops, "concepts"):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown job role '{job_role}': not in the published "
                   "workforce config role set")
    unit = body.get("unit")
    if unit is not None:
        known_units = _config_codes(ops, "units") | _config_codes(ops, "serviceLines")
        if unit not in known_units:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown unit '{unit}': not in the published "
                       "workforce config")
    service_line = body.get("service_line")
    if service_line is not None and \
            service_line not in _config_codes(ops, "serviceLines"):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown service line '{service_line}': not in the "
                   "published workforce config")
    status = body.get("status") or "active"
    if status not in STAFF_STATUSES:
        raise HTTPException(status_code=422,
                            detail=f"'status' must be one of {list(STAFF_STATUSES)}")
    staff_id = body.get("staff_id") or f"staff-{uuid.uuid4().hex[:8]}"
    if session.scalars(select(StaffMember).where(
            StaffMember.staff_id == staff_id)).first() is not None:
        raise HTTPException(status_code=409,
                            detail=f"Staff id '{staff_id}' already exists")
    # Organisation placement: when any of region/location/department
    # is supplied, the placement carried by the body is validated
    # against the published hierarchy (partial-safe: provided codes
    # must exist at their level and chain consistently) -> 422 naming
    # the broken link. A bare unit keeps the legacy workforce check
    # above only.
    placement = _normalize_placement(
        {level: body.get(level) for level in PLACEMENT_LEVELS})
    if any(body.get(level) is not None
           for level in ("region", "location", "department")):
        placement = _validate_placement(session, placement,
                                        require_all=False)
    placement_kwargs = {
        level: placement[level]
        for level in ("region", "location", "department")
        if hasattr(StaffMember, level)}
    row = StaffMember(
        staff_id=staff_id, username=body.get("username") or None,
        full_name=full_name, job_role=job_role, unit=unit,
        service_line=service_line, status=status,
        audience=body.get("audience") or JOB_ROLE_AUDIENCE.get(job_role),
        email=body.get("email") or "", phone=body.get("phone") or "",
        **placement_kwargs)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="workforce.staff.create",
                 entity=f"staff:{staff_id}",
                 payload={"job_role": job_role, "unit": unit,
                          "service_line": service_line, "status": status})
    session.commit()
    return _staff_out(row)


@router.put("/staff/{staff_id}")
async def update_staff(staff_id: str, request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("workforce.work"))):
    """Edit a directory entry (Front Door pass): job role, unit,
    service line, status, audience and contact details — validated
    against the published workforce config EXACTLY as create is
    (unknown role/unit/service line -> 422), with email/phone
    format-checked by type. Partial: only the fields sent change.
    The audit carries the changed FIELD NAMES, not values."""
    row = _get_staff(session, staff_id)
    body = await _json_body(request)
    ops = _ops(session)
    editable = {"full_name", "job_role", "unit", "service_line", "status",
                "audience", "email", "phone", "username",
                "region", "location", "department"}
    unknown = set(body) - editable
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown staff field(s): {', '.join(sorted(unknown))}")
    # Organisation placement: when the edit touches region, location
    # or department, the EFFECTIVE placement (stored values overridden
    # by the edit, unit included) is validated against the published
    # hierarchy BEFORE anything is applied -> a stored+new combination
    # whose chain is broken is a 422 naming the link. A unit-only edit
    # keeps the legacy workforce-config check in the unit block below.
    if any(level in body for level in ("region", "location",
                                        "department")):
        effective = _placement_of(row)
        for level in PLACEMENT_LEVELS:
            if level in body:
                value = body.get(level)
                if value is not None and not isinstance(value, str):
                    raise HTTPException(
                        status_code=422,
                        detail=f"'{level}' must be a string or null")
                effective[level] = (value.strip()
                                    if isinstance(value, str)
                                    and value.strip() else None)
        _validate_placement(session, effective, require_all=False)
    changed: list[str] = []
    if "full_name" in body:
        value = _require_str(body, "full_name")
        if value != row.full_name:
            row.full_name = value
            changed.append("full_name")
    if "job_role" in body:
        value = _require_str(body, "job_role")
        if value not in _config_codes(ops, "concepts"):
            raise HTTPException(
                status_code=422,
                detail=f"Unknown job role '{value}': not in the published "
                       "workforce config role set")
        if value != row.job_role:
            row.job_role = value
            changed.append("job_role")
    if "unit" in body:
        value = body.get("unit")
        if value is not None:
            known_units = _config_codes(ops, "units") | _config_codes(ops, "serviceLines")
            if value not in known_units:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown unit '{value}': not in the published "
                           "workforce config")
        if value != row.unit:
            row.unit = value
            changed.append("unit")
    for level in ("region", "location", "department"):
        if level in body:
            value = body.get(level)
            if value is not None and not isinstance(value, str):
                raise HTTPException(
                    status_code=422,
                    detail=f"'{level}' must be a string or null")
            value = (value.strip()
                     if isinstance(value, str) and value.strip() else None)
            if hasattr(row, level) and value != getattr(row, level):
                setattr(row, level, value)
                changed.append(level)
    if "service_line" in body:
        value = body.get("service_line")
        if value is not None and \
                value not in _config_codes(ops, "serviceLines"):
            raise HTTPException(
                status_code=422,
                detail=f"Unknown service line '{value}': not in the "
                       "published workforce config")
        if value != row.service_line:
            row.service_line = value
            changed.append("service_line")
    if "status" in body:
        value = body.get("status")
        if value not in STAFF_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=f"'status' must be one of {list(STAFF_STATUSES)}")
        if value != row.status:
            row.status = value
            changed.append("status")
    if "audience" in body:
        value = body.get("audience")
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=422,
                                detail="'audience' must be a string or null")
        if value != row.audience:
            row.audience = value
            changed.append("audience")
    if "username" in body:
        value = body.get("username")
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=422,
                                detail="'username' must be a string or null")
        if value != row.username:
            row.username = value
            changed.append("username")
    if "email" in body:
        value = vf.check_email(body.get("email")) or ""
        if value != row.email:
            row.email = value
            changed.append("email")
    if "phone" in body:
        value = vf.check_phone(body.get("phone")) or ""
        if value != row.phone:
            row.phone = value
            changed.append("phone")
    session.flush()
    append_audit(session, actor=user.username,
                 action="workforce.staff.update",
                 entity=f"staff:{staff_id}",
                 payload={"changed_fields": changed})
    session.commit()
    return _staff_out(row)


# ---------------------------------------------------------------------------
# Credentials & licence tracking
# ---------------------------------------------------------------------------

@router.get("/credentials")
def list_credentials(status: str | None = Query(default=None),
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability("workforce.read"))):
    ops = _ops(session)
    names = _staff_names(session)
    rows = session.scalars(select(StaffCredential).order_by(
        StaffCredential.expires_on.asc())).all()
    items = [_credential_out(r, ops, names, session=session) for r in rows]
    if status:
        items = [i for i in items if i["status"] == status]
    return {"items": items, "total": len(items)}


@router.get("/credentials/worklist")
def credential_worklist(session: Session = Depends(get_db),
                        user: User = Depends(require_capability("workforce.read"))):
    """The expiry worklist: every expired credential plus everything
    expiring inside the published worklist window, soonest first —
    the renewal team's morning list."""
    ops = _ops(session)
    worklist_window, alert_window = _expiry_windows(ops)
    names = _staff_names(session)
    rows = session.scalars(select(StaffCredential).order_by(
        StaffCredential.expires_on.asc())).all()
    items = []
    for row in rows:
        entry = _credential_out(row, ops, names, session=session)
        if entry["days_to_expiry"] is not None \
                and entry["days_to_expiry"] <= worklist_window:
            entry["alert_window"] = entry["days_to_expiry"] <= alert_window
            items.append(entry)
    return {"items": items, "total": len(items),
            "worklist_window_days": worklist_window,
            "alert_window_days": alert_window,
            "expired": sum(1 for i in items if i["status"] == "expired")}


@router.post("/credentials", status_code=201)
async def create_credential(request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("workforce.work"))):
    body = await _json_body(request)
    ops = _ops(session)
    staff = _get_staff(session, _require_str(body, "staff_id"))
    cred_type = _require_str(body, "type")
    if cred_type not in _config_codes(ops, "credentialTypes"):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown credential type '{cred_type}': not in the "
                   "published workforce config")
    number = _require_str(body, "number")
    expires_on = _require_str(body, "expires_on")
    if _days_to_expiry(expires_on) is None:
        raise HTTPException(status_code=422,
                            detail="'expires_on' must be an ISO date (YYYY-MM-DD)")
    issued_on = body.get("issued_on")
    if issued_on is not None and _days_to_expiry(issued_on) is None:
        raise HTTPException(status_code=422,
                            detail="'issued_on' must be an ISO date (YYYY-MM-DD)")
    credential_id = body.get("credential_id") or f"cred-{uuid.uuid4().hex[:8]}"
    if session.scalars(select(StaffCredential).where(
            StaffCredential.credential_id == credential_id)).first() is not None:
        raise HTTPException(status_code=409,
                            detail=f"Credential id '{credential_id}' already exists")
    row = StaffCredential(credential_id=credential_id, staff_id=staff.staff_id,
                          type=cred_type, number=number, issued_on=issued_on,
                          expires_on=expires_on)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="workforce.credential.create",
                 entity=f"staff:{staff.staff_id}",
                 payload={"credential_id": credential_id, "type": cred_type,
                          "expires_on": expires_on})
    session.commit()
    return _credential_out(row, ops, _staff_names(session), session=session)


# ---------------------------------------------------------------------------
# Primary-source verification + exclusion checks (Wave A, WF-B1)
# ---------------------------------------------------------------------------

def _get_credential(session: Session, credential_id: str) -> StaffCredential:
    row = session.scalars(select(StaffCredential).where(
        StaffCredential.credential_id == credential_id)).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Credential '{credential_id}' not found")
    return row


def _parse_iso_date(body: dict, field: str, *, required: bool = True,
                    default: str | None = None) -> str | None:
    """An ISO date field from a JSON body: 422 naming the field when
    malformed or in the future (a verification/check cannot have
    happened tomorrow)."""
    raw = body.get(field)
    if raw is None:
        if required:
            raise HTTPException(
                status_code=422, detail=f"'{field}' is required")
        return default
    if not isinstance(raw, str):
        raise HTTPException(
            status_code=422,
            detail=f"'{field}' must be an ISO date (YYYY-MM-DD)")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"'{field}' must be an ISO date (YYYY-MM-DD)")
    if parsed > _today():
        raise HTTPException(
            status_code=422,
            detail=f"'{field}' must not be in the future")
    return parsed.isoformat()


def _optional_str(body: dict, field: str) -> str:
    value = body.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be a string")
    return value


def _verification_out(row: CredentialVerification) -> dict:
    return {"verification_id": row.verification_id,
            "credential_id": row.credential_id,
            "source_code": row.source_code, "method": row.method,
            "verified_by": row.verified_by, "verified_on": row.verified_on,
            "outcome": row.outcome, "evidence": row.evidence,
            "note": row.note,
            "created_at": row.created_at.isoformat()
            if row.created_at else None}


def _exclusion_out(row: CredentialExclusionCheck) -> dict:
    return {"check_id": row.check_id, "staff_id": row.staff_id,
            "credential_id": row.credential_id, "kind": row.kind,
            "outcome": row.outcome, "checked_by": row.checked_by,
            "checked_on": row.checked_on, "evidence": row.evidence,
            "note": row.note,
            "created_at": row.created_at.isoformat()
            if row.created_at else None}


@router.post("/credentials/{credential_id}/verifications", status_code=201)
async def create_verification(credential_id: str, request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(
                                  require_capability("workforce.work"))):
    """Record a primary-source verification event for a credential.
    The source must be a code from the PUBLISHED workforce config
    `psv.sources`; the outcome is the governed enum; a "verified"
    outcome must name its evidence (document id / confirmation
    number) — a verification with nothing behind it is exactly the
    assertion WF-B1 removes. Writes are audited
    (workforce.credential.verify)."""
    credential = _get_credential(session, credential_id)
    body = await _json_body(request)
    psv = _psv_config(_ops(session))
    source = _require_str(body, "source_code") \
        if body.get("source_code") is not None else \
        _require_str(body, "source")
    if source not in _psv_codes(psv, "sources"):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown PSV source '{source}': not in the published "
                   "workforce config psv sources")
    method = _require_str(body, "method")
    if method not in VERIFICATION_METHODS:
        raise HTTPException(
            status_code=422,
            detail=f"'method' must be one of {list(VERIFICATION_METHODS)}")
    outcome = _require_str(body, "outcome")
    if outcome not in VERIFICATION_OUTCOMES:
        raise HTTPException(
            status_code=422,
            detail=f"'outcome' must be one of {list(VERIFICATION_OUTCOMES)}")
    verified_on = _parse_iso_date(body, "verified_on")
    evidence = _optional_str(body, "evidence").strip()
    if outcome == "verified" and not evidence:
        raise HTTPException(
            status_code=422,
            detail="'evidence' is required when outcome is 'verified'")
    verified_by = _optional_str(body, "verified_by").strip() \
        or user.username
    verification_id = body.get("verification_id") \
        or f"psv-{uuid.uuid4().hex[:10]}"
    if not isinstance(verification_id, str) or not verification_id.strip():
        raise HTTPException(status_code=422,
                            detail="'verification_id' must be a string")
    if session.scalars(select(CredentialVerification).where(
            CredentialVerification.verification_id == verification_id)
            ).first() is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Verification id '{verification_id}' already exists")
    row = CredentialVerification(
        verification_id=verification_id, credential_id=credential_id,
        source_code=source, method=method, verified_by=verified_by,
        verified_on=verified_on, outcome=outcome, evidence=evidence,
        note=_optional_str(body, "note"))
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="workforce.credential.verify",
                 entity=f"credential:{credential_id}",
                 payload={"verification_id": verification_id,
                          "staff_id": credential.staff_id,
                          "source": source, "method": method,
                          "outcome": outcome, "verified_on": verified_on,
                          "verified_by": verified_by})
    session.commit()
    return _verification_out(row)


@router.get("/credentials/{credential_id}/verifications")
def list_verifications(credential_id: str,
                       session: Session = Depends(get_db),
                       user: User = Depends(
                           require_capability("workforce.read"))):
    """The credential's PSV events, latest first (by verified_on,
    then creation) — the verification history behind its computed
    state."""
    credential = _get_credential(session, credential_id)
    rows = session.scalars(select(CredentialVerification).where(
        CredentialVerification.credential_id == credential_id)).all()
    rows = sorted(rows, key=lambda r: (r.verified_on or "",
                                       r.created_at or _now(),
                                       r.id or 0), reverse=True)
    return {"items": [_verification_out(r) for r in rows],
            "total": len(rows), "credential_id": credential_id,
            "staff_id": credential.staff_id}


@router.post("/staff/{staff_id}/exclusion-checks", status_code=201)
async def create_exclusion_check(staff_id: str, request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(
                                     require_capability("workforce.work"))):
    """Record an exclusion check for a staff member. The kind must
    be a code from the PUBLISHED workforce config
    `psv.exclusionKinds`; a "hit" is recorded as data — its
    consequence surfaces in the computed verification state and in
    check_privileges, never as a silent side effect here. Writes are
    audited (workforce.exclusion.check)."""
    staff = _get_staff(session, staff_id)
    body = await _json_body(request)
    psv = _psv_config(_ops(session))
    kind = _require_str(body, "kind")
    if kind not in _psv_codes(psv, "exclusionKinds"):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown exclusion kind '{kind}': not in the "
                   "published workforce config psv exclusionKinds")
    outcome = _require_str(body, "outcome")
    if outcome not in EXCLUSION_OUTCOMES:
        raise HTTPException(
            status_code=422,
            detail=f"'outcome' must be one of {list(EXCLUSION_OUTCOMES)}")
    checked_on = _parse_iso_date(body, "checked_on", required=False,
                                 default=_today().isoformat())
    credential_id = body.get("credential_id")
    if credential_id is not None:
        if not isinstance(credential_id, str) or not credential_id.strip():
            raise HTTPException(
                status_code=422,
                detail="'credential_id' must be a string or null")
        credential = _get_credential(session, credential_id)
        if credential.staff_id != staff.staff_id:
            raise HTTPException(
                status_code=422,
                detail=f"'credential_id' '{credential_id}' does not "
                       f"belong to staff member '{staff_id}'")
    checked_by = _optional_str(body, "checked_by").strip() or user.username
    check_id = body.get("check_id") or f"excl-{uuid.uuid4().hex[:10]}"
    if not isinstance(check_id, str) or not check_id.strip():
        raise HTTPException(status_code=422,
                            detail="'check_id' must be a string")
    if session.scalars(select(CredentialExclusionCheck).where(
            CredentialExclusionCheck.check_id == check_id)
            ).first() is not None:
        raise HTTPException(status_code=409,
                            detail=f"Exclusion check id '{check_id}' "
                                   "already exists")
    row = CredentialExclusionCheck(
        check_id=check_id, staff_id=staff.staff_id,
        credential_id=credential_id, kind=kind, outcome=outcome,
        checked_by=checked_by, checked_on=checked_on,
        evidence=_optional_str(body, "evidence"),
        note=_optional_str(body, "note"))
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="workforce.exclusion.check",
                 entity=f"staff:{staff.staff_id}",
                 payload={"check_id": check_id, "kind": kind,
                          "outcome": outcome, "checked_on": checked_on,
                          "credential_id": credential_id,
                          "checked_by": checked_by})
    session.commit()
    return _exclusion_out(row)


@router.get("/staff/{staff_id}/exclusion-checks")
def list_exclusion_checks(staff_id: str,
                          session: Session = Depends(get_db),
                          user: User = Depends(
                              require_capability("workforce.read"))):
    """A staff member's exclusion checks, latest first, with the
    computed {kind: latest outcome} summary privilege checks read."""
    staff = _get_staff(session, staff_id)
    rows = session.scalars(select(CredentialExclusionCheck).where(
        CredentialExclusionCheck.staff_id == staff.staff_id)).all()
    rows = sorted(rows, key=lambda r: (r.checked_on or "",
                                       r.created_at or _now(),
                                       r.id or 0), reverse=True)
    return {"items": [_exclusion_out(r) for r in rows],
            "total": len(rows), "staff_id": staff.staff_id,
            "summary": _latest_exclusions(session, staff.staff_id)}


@router.post("/credentials/scan")
def scan_credentials(session: Session = Depends(get_db),
                     user: User = Depends(require_capability("workforce.work"))):
    """Expiry alert scan: emit `workforce.credential.expiring` into the
    rules engine for every credential inside the published alert
    window (or already expired) that has not been alerted for its
    current expiry date. Idempotent — a second scan alerts nothing
    until a credential is renewed (new expires_on) or a new one enters
    the window. Published rules decide what an alert DOES (the seeded
    rule opens a renewal Task at <= 30 days)."""
    ops = _ops(session)
    _worklist_window, alert_window = _expiry_windows(ops)
    names = _staff_names(session)
    rows = session.scalars(select(StaffCredential).order_by(
        StaffCredential.expires_on.asc())).all()
    alerted = []
    for row in rows:
        days = _days_to_expiry(row.expires_on)
        if days is None or days > alert_window:
            continue
        if row.alerted_for_expiry == row.expires_on:
            continue
        firings = evaluate_rules(
            session, "workforce.credential.expiring",
            {"days_to_expiry": days, "staff_id": row.staff_id,
             "staff_name": names.get(row.staff_id),
             "credential_type": row.type, "credential_id": row.credential_id,
             "expires_on": row.expires_on},
            actor=user.username)
        row.alerted_for_expiry = row.expires_on
        alerted.append({"credential_id": row.credential_id,
                        "staff_id": row.staff_id, "type": row.type,
                        "days_to_expiry": days, "firings": len(firings)})
    session.flush()
    append_audit(session, actor=user.username,
                 action="workforce.credentials.scan", entity="workforce",
                 payload={"scanned": len(rows), "alerted": len(alerted),
                          "alert_window_days": alert_window})
    session.commit()
    return {"scanned": len(rows), "alerted": len(alerted), "items": alerted}


# ---------------------------------------------------------------------------
# Rosters
# ---------------------------------------------------------------------------

def _shift_out(row: RosterShift, names: dict[str, str]) -> dict:
    return {"shift_id": row.shift_id, "staff_id": row.staff_id,
            "staff_name": names.get(row.staff_id), "unit": row.unit,
            "date": row.date, "template": row.template_code,
            "starts_at": row.starts_at.isoformat() if row.starts_at else None,
            "ends_at": row.ends_at.isoformat() if row.ends_at else None,
            "status": row.status}


@router.get("/roster")
def list_roster(date_from: str | None = Query(default=None),
                date_to: str | None = Query(default=None),
                unit: str | None = Query(default=None),
                session: Session = Depends(get_db),
                user: User = Depends(require_capability("workforce.read"))):
    """Roster instances in a date window (default: today through six
    days out — the working week view)."""
    first = date_from or _today().isoformat()
    last = date_to or (_today() + timedelta(days=6)).isoformat()
    stmt = select(RosterShift).where(RosterShift.date >= first,
                                     RosterShift.date <= last)
    if unit:
        stmt = stmt.where(RosterShift.unit == unit)
    rows = session.scalars(stmt.order_by(RosterShift.date.asc(),
                                         RosterShift.starts_at.asc())).all()
    names = _staff_names(session)
    return {"items": [_shift_out(r, names) for r in rows],
            "total": len(rows), "from": first, "to": last}


@router.post("/roster", status_code=201)
async def create_shift(request: Request, session: Session = Depends(get_db),
                       user: User = Depends(require_capability("workforce.work"))):
    body = await _json_body(request)
    ops = _ops(session)
    staff = _get_staff(session, _require_str(body, "staff_id"))
    unit = _require_str(body, "unit")
    if unit not in _config_codes(ops, "units"):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown unit '{unit}': rosters are kept for the units "
                   "in the published workforce config")
    day_raw = _require_str(body, "date")
    try:
        day = date.fromisoformat(day_raw)
    except ValueError:
        raise HTTPException(status_code=422,
                            detail="'date' must be an ISO date (YYYY-MM-DD)")
    template_code = _require_str(body, "template")
    template = next((t for t in ops.get("shiftTemplates") or []
                     if isinstance(t, dict) and t.get("code") == template_code),
                    None)
    if template is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown shift template '{template_code}': not in the "
                   "published workforce config")
    try:
        start_t = time.fromisoformat(template["start"])
        end_t = time.fromisoformat(template["end"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=422,
                            detail=f"Shift template '{template_code}' has "
                                   "malformed start/end times in config")
    starts_at = datetime.combine(day, start_t, tzinfo=timezone.utc)
    ends_at = datetime.combine(day, end_t, tzinfo=timezone.utc)
    if ends_at <= starts_at:  # overnight template (e.g. night 19:00-07:00)
        ends_at += timedelta(days=1)
    duplicate = session.scalars(select(RosterShift).where(
        RosterShift.staff_id == staff.staff_id, RosterShift.date == day_raw,
        RosterShift.template_code == template_code)).first()
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{staff.full_name} already has a '{template_code}' shift "
                   f"on {day_raw}")
    row = RosterShift(shift_id=f"shift-{uuid.uuid4().hex[:8]}",
                      staff_id=staff.staff_id, unit=unit, date=day_raw,
                      template_code=template_code, starts_at=starts_at,
                      ends_at=ends_at, status="scheduled")
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="workforce.roster.create",
                 entity=f"staff:{staff.staff_id}",
                 payload={"shift_id": row.shift_id, "unit": unit,
                          "date": day_raw, "template": template_code})
    session.commit()
    return _shift_out(row, _staff_names(session))


# ---------------------------------------------------------------------------
# On-call schedules
# ---------------------------------------------------------------------------

def _oncall_out(row: OnCallAssignment, names: dict[str, str]) -> dict:
    return {"assignment_id": row.assignment_id,
            "service_line": row.service_line, "staff_id": row.staff_id,
            "staff_name": names.get(row.staff_id),
            "role_on_call": row.role_on_call,
            "starts_at": row.starts_at.isoformat() if row.starts_at else None,
            "ends_at": row.ends_at.isoformat() if row.ends_at else None}


@router.get("/on-call")
def list_on_call(service_line: str | None = Query(default=None),
                 current: bool = Query(default=True),
                 session: Session = Depends(get_db),
                 user: User = Depends(require_capability("workforce.read"))):
    stmt = select(OnCallAssignment).order_by(OnCallAssignment.starts_at.asc())
    if service_line:
        stmt = stmt.where(OnCallAssignment.service_line == service_line)
    if current:
        now = _now()
        stmt = stmt.where(OnCallAssignment.starts_at <= now,
                          OnCallAssignment.ends_at > now)
    rows = session.scalars(stmt).all()
    names = _staff_names(session)
    return {"items": [_oncall_out(r, names) for r in rows],
            "total": len(rows)}


@router.get("/on-call/now")
def on_call_now(session: Session = Depends(get_db),
                user: User = Depends(require_capability("workforce.read"))):
    """Who is on call NOW, per service line — the lookup the command
    centre, transfer centre and messaging surfaces consume. A service
    line with nobody covering reads primary: null (an honest gap,
    never a guess)."""
    ops = _ops(session)
    now = _now()
    names = _staff_names(session)
    rows = session.scalars(select(OnCallAssignment).where(
        OnCallAssignment.starts_at <= now,
        OnCallAssignment.ends_at > now)).all()
    services = []
    for line in ops.get("serviceLines") or []:
        if not isinstance(line, dict) or not line.get("code"):
            continue
        code = line["code"]
        covering = [r for r in rows if r.service_line == code]
        primary = next((r for r in covering if r.role_on_call == "primary"),
                       None)
        backup = next((r for r in covering if r.role_on_call == "backup"),
                      None)
        services.append({
            "service_line": code, "display": line.get("display"),
            "primary": ({"staff_id": primary.staff_id,
                         "staff_name": names.get(primary.staff_id),
                         "ends_at": primary.ends_at.isoformat()}
                        if primary else None),
            "backup": ({"staff_id": backup.staff_id,
                        "staff_name": names.get(backup.staff_id),
                        "ends_at": backup.ends_at.isoformat()}
                       if backup else None),
        })
    return {"generated_at": now.isoformat(), "services": services,
            "covered": sum(1 for s in services if s["primary"]),
            "total": len(services)}


@router.post("/on-call", status_code=201)
async def create_on_call(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(require_capability("workforce.work"))):
    body = await _json_body(request)
    ops = _ops(session)
    service_line = _require_str(body, "service_line")
    if service_line not in _config_codes(ops, "serviceLines"):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown service line '{service_line}': not in the "
                   "published workforce config")
    staff = _get_staff(session, _require_str(body, "staff_id"))
    role_on_call = body.get("role_on_call") or "primary"
    if role_on_call not in ON_CALL_ROLES:
        raise HTTPException(status_code=422,
                            detail=f"'role_on_call' must be one of {list(ON_CALL_ROLES)}")
    try:
        starts_at = datetime.fromisoformat(_require_str(body, "starts_at"))
        ends_at = datetime.fromisoformat(_require_str(body, "ends_at"))
    except ValueError:
        raise HTTPException(status_code=422,
                            detail="'starts_at'/'ends_at' must be ISO datetimes")
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=timezone.utc)
    if ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=timezone.utc)
    if ends_at <= starts_at:
        raise HTTPException(status_code=422,
                            detail="'ends_at' must be after 'starts_at'")
    overlap = session.scalars(select(OnCallAssignment).where(
        OnCallAssignment.service_line == service_line,
        OnCallAssignment.role_on_call == role_on_call,
        OnCallAssignment.starts_at < ends_at,
        OnCallAssignment.ends_at > starts_at)).first()
    if overlap is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A {role_on_call} on-call assignment for "
                   f"'{service_line}' already covers part of that window "
                   f"({overlap.assignment_id})")
    row = OnCallAssignment(assignment_id=f"oncall-{uuid.uuid4().hex[:8]}",
                           service_line=service_line,
                           staff_id=staff.staff_id, role_on_call=role_on_call,
                           starts_at=starts_at, ends_at=ends_at)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="workforce.oncall.create",
                 entity=f"staff:{staff.staff_id}",
                 payload={"assignment_id": row.assignment_id,
                          "service_line": service_line,
                          "role_on_call": role_on_call,
                          "starts_at": starts_at.isoformat(),
                          "ends_at": ends_at.isoformat()})
    session.commit()
    return _oncall_out(row, _staff_names(session))


# ---------------------------------------------------------------------------
# Role-aware landing: the caller's audience dashboard
# ---------------------------------------------------------------------------

def resolve_audience(session: Session, ops: dict, user: User) -> str | None:
    """Audience resolution chain: the caller's staff-directory entry
    (explicit audience, else derived from their job role) wins; with
    no directory entry, the published role->audience map applies.
    Patients have no staff audience."""
    if user.role.value == "patient":
        return None
    staff = session.scalars(select(StaffMember).where(
        StaffMember.username == user.username)).first()
    if staff is not None:
        return staff.audience or JOB_ROLE_AUDIENCE.get(staff.job_role)
    by_role = ops.get("audienceByRole") or {}
    audience = by_role.get(user.role.value)
    return audience if isinstance(audience, str) else None


def _caller_staff(session: Session, user: User) -> StaffMember | None:
    """The caller's own staff-directory entry (the username link), or
    None when the sign-in has no directory entry."""
    return session.scalars(select(StaffMember).where(
        StaffMember.username == user.username)).first()


def _scope_dashboard_units(payload: dict, unit: str) -> dict:
    """Rewrite a dashboard payload's /people/analytics/* widget
    endpoints with ?unit=<the caller's unit> (Front Door pass: the
    nurse and unit-manager homes are scoped to the unit the person
    actually works on; department-head/executive stay house-wide)."""
    import copy
    scoped = copy.deepcopy(payload)
    for widget in scoped.get("widgets") or []:
        if not isinstance(widget, dict):
            continue
        endpoint = widget.get("endpoint")
        if isinstance(endpoint, str) \
                and endpoint.startswith("/people/analytics/"):
            joiner = "&" if "?" in endpoint else "?"
            widget["endpoint"] = f"{endpoint}{joiner}unit={unit}"
    return scoped


@router.get("/home-dashboard")
def home_dashboard(session: Session = Depends(get_db),
                   user: User = Depends(require_capability("workforce.read"))):
    """The caller's home dashboard: the PUBLISHED dashboard config for
    their audience, resolved through the published workforce config.
    dashboard is null when no audience resolves or no dashboard is
    published for it — the shell falls back to its ordinary home.
    For the nurse and unit-manager audiences, when the caller's
    directory entry names a unit, the payload's analytics widgets are
    unit-scoped (see _scope_dashboard_units)."""
    ops = _ops(session)
    audience = resolve_audience(session, ops, user)
    dashboard_key = None
    payload = None
    if audience:
        mapping = ops.get("audienceDashboards") or {}
        candidate = mapping.get(audience)
        if isinstance(candidate, str):
            cfg = published_config(session, "dashboard", candidate)
            if cfg is not None:
                dashboard_key = candidate
                payload = cfg.payload
    if payload is not None and audience in ("nurse", "unit-manager"):
        staff = _caller_staff(session, user)
        if staff is not None and staff.unit:
            payload = _scope_dashboard_units(payload, staff.unit)
    return {"audience": audience, "dashboard_key": dashboard_key,
            "dashboard": payload}


# ---------------------------------------------------------------------------
# Unit-first home: my unit's patients + my patients (Front Door pass)
# ---------------------------------------------------------------------------

def _patient_names(session: Session, patient_refs: list[str]) -> dict[str, str]:
    from .registration import patient_display_name
    ids = {ref.split("/", 1)[1] for ref in patient_refs
           if ref and ref.startswith("Patient/")}
    if not ids:
        return {}
    rows = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Patient",
        FhirResource.resource_id.in_(ids))).all()
    return {f"Patient/{r.resource_id}": patient_display_name(r.body or {})
            for r in rows}


def _home_patient_item(session: Session, row, names: dict[str, str],
                       staff_names: dict[str, str], now: datetime) -> dict:
    from . import continuum
    body = row.body or {}
    patient_ref = inpatient._subject_ref(body)
    attending = inpatient._get_extension(body, inpatient.ATTENDING_EXTENSION)
    attending_id = attending.get("valueString") if attending else None
    los = inpatient._los(row, now)
    return {
        "encounter_id": row.resource_id,
        "patient_ref": patient_ref,
        "patient_id": (patient_ref.split("/", 1)[1]
                       if patient_ref and "/" in patient_ref else None),
        "patient_name": names.get(patient_ref) if patient_ref else None,
        "bed": inpatient._current_bed(body),
        "attending_staff_id": attending_id,
        "attending_name": staff_names.get(attending_id) if attending_id else None,
        "admitted_at": los["admitted_at"],
        "los_days": los["los_days"],
        "discharge_ready": continuum._readiness(session, row, now)["ready"],
    }


@router.get("/my-unit-patients")
def my_unit_patients(session: Session = Depends(get_db),
                     user: User = Depends(require_capability("workforce.read"))):
    """The caller's unit list: every active admission currently in
    the unit named on the caller's staff-directory entry — patient,
    bed, attending, admission date, discharge-readiness — computed
    from live inpatient data. A caller with no directory entry or no
    unit gets unit: null and an empty list (the home falls back to
    the house view; nothing is invented)."""
    from . import continuum
    staff = _caller_staff(session, user)
    if staff is None or not staff.unit:
        return {"unit": None, "unit_display": None, "items": [], "total": 0}
    now = _now()
    bed_units = continuum._bed_unit_map(session)
    rows = [r for r in continuum._active_admissions(session)
            if continuum._admission_unit(session, r.body or {},
                                         bed_units) == staff.unit]
    names = _patient_names(session,
                           [inpatient._subject_ref(r.body or {}) for r in rows])
    staff_names = _staff_names(session)
    items = [_home_patient_item(session, r, names, staff_names, now)
             for r in rows]
    items.sort(key=lambda i: i["bed"] or "")
    ops = _ops(session)
    unit_display = next((u.get("display") for u in ops.get("units") or []
                         if isinstance(u, dict)
                         and u.get("code") == staff.unit), staff.unit)
    return {"unit": staff.unit, "unit_display": unit_display,
            "items": items, "total": len(items)}


@router.get("/my-patients")
def my_patients(session: Session = Depends(get_db),
                user: User = Depends(require_capability("workforce.read"))):
    """The caller's own patients: active admissions whose attending
    (recorded on the encounter at admit / reassignment) is the
    caller's staff-directory entry. No directory entry -> an empty
    list with staff_id: null."""
    from . import continuum
    staff = _caller_staff(session, user)
    if staff is None:
        return {"staff_id": None, "items": [], "total": 0}
    now = _now()
    rows = []
    for row in continuum._active_admissions(session):
        attending = inpatient._get_extension(row.body or {},
                                             inpatient.ATTENDING_EXTENSION)
        if attending is not None \
                and attending.get("valueString") == staff.staff_id:
            rows.append(row)
    names = _patient_names(session,
                           [inpatient._subject_ref(r.body or {}) for r in rows])
    staff_names = _staff_names(session)
    items = [_home_patient_item(session, r, names, staff_names, now)
             for r in rows]
    items.sort(key=lambda i: i["admitted_at"] or "")
    return {"staff_id": staff.staff_id, "items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Analytics — computed from live module data, never fixtures
# ---------------------------------------------------------------------------

def _bed_stats(session: Session) -> dict[str, dict]:
    """Per-location-unit bed state from the Phase 6 machinery (bed
    statuses are live state on the materialised Location resources)."""
    now = _now()
    units, beds = inpatient._location_concepts(session)
    stats: dict[str, dict] = {}
    for unit in units:
        stats[unit["code"]] = {"beds_total": 0, "occupied": 0,
                               "available": 0, "cleaning": 0, "blocked": 0}
    for concept in beds:
        entry = inpatient._bed_entry(session, concept, now)
        bucket = stats.setdefault(entry.get("unit") or "",
                                  {"beds_total": 0, "occupied": 0,
                                   "available": 0, "cleaning": 0, "blocked": 0})
        bucket["beds_total"] += 1
        status = entry.get("status")
        if status in bucket:
            bucket[status] += 1
    for bucket in stats.values():
        total = bucket["beds_total"]
        bucket["occupancy_pct"] = (round(100.0 * bucket["occupied"] / total, 1)
                                   if total else 0.0)
    return stats


def _coverage(session: Session, ops: dict) -> dict:
    """Today's roster coverage per unit: distinct staff rostered vs
    the published minimum on shift for the unit."""
    today = _today().isoformat()
    items = []
    total_filled = 0
    total_required = 0
    for unit in ops.get("units") or []:
        if not isinstance(unit, dict) or not unit.get("code"):
            continue
        code = unit["code"]
        scheduled = len({r.staff_id for r in session.scalars(
            select(RosterShift).where(RosterShift.unit == code,
                                      RosterShift.date == today,
                                      RosterShift.status == "scheduled")).all()})
        required = unit.get("minOnShift")
        required = int(required) if isinstance(required, int) and required > 0 \
            else None
        items.append({
            "unit": code, "display": unit.get("display"),
            "scheduled": scheduled, "required": required,
            "coverage_pct": (round(100.0 * scheduled / required, 1)
                             if required else None),
        })
        if required:
            # The house figure is a FILL rate: over-staffing one unit
            # does not cover another unit's gap.
            total_filled += min(scheduled, required)
            total_required += required
    overall = (round(100.0 * total_filled / total_required, 1)
               if total_required else None)
    return {"date": today, "items": items, "total": len(items),
            "overall_pct": overall}


def _check_unit_param(ops: dict, unit: str | None) -> None:
    """The optional ?unit= scope on the analytics endpoints (Front
    Door pass) must name a unit in the published workforce config —
    a hand-typed scope is a 422, not a silently empty result."""
    if unit is None:
        return
    codes = {u["code"] for u in ops.get("units") or []
             if isinstance(u, dict) and u.get("code")}
    if unit not in codes:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown unit '{unit}': not in the published "
                   "workforce config")


def _analytics_scope(session: Session, ops: dict, unit: str | None,
                     region_id: str | None, location_id: str | None,
                     department_id: str | None
                     ) -> tuple[set[str] | None, dict | None]:
    """Resolve the analytics endpoints' placement scope: the legacy
    ?unit= workforce-config check runs exactly as before, and when
    any of region_id / location_id / department_id is present the
    placement is resolved through org_hierarchy.resolve_scope to the
    set of unit codes in scope (unknown placement ids and broken
    chains are the resolver's 422s). Returns (scope_units, echo);
    both None when no placement parameter is present, in which case
    endpoint behaviour is byte-identical to the pre-placement one."""
    _check_unit_param(ops, unit)
    if region_id is None and location_id is None and department_id is None:
        return None, None
    scope = _resolve_scope(session, region=region_id,
                           location=location_id,
                           department=department_id, unit=unit)
    echo = {"region": region_id, "location": location_id,
            "department": department_id, "unit": unit}
    return scope, echo


def _scoped_coverage_pct(coverage: dict, scope: set[str]) -> float | None:
    """House fill-rate over the in-scope units only: over-staffing
    one in-scope unit does not cover another's gap (the _coverage
    overall semantics, restricted to the scope)."""
    filled = 0
    required_total = 0
    for item in coverage["items"]:
        if item["unit"] not in scope:
            continue
        required = item.get("required")
        if required:
            filled += min(item["scheduled"], required)
            required_total += required
    return (round(100.0 * filled / required_total, 1)
            if required_total else None)


def _bed_unit_map(session: Session) -> dict[str, str]:
    _units, beds = inpatient._location_concepts(session)
    return {c["code"]: (c.get("parent") or "") for c in beds
            if isinstance(c, dict) and c.get("code")}


def _event_unit(event: AdtEvent, bed_units: dict[str, str]) -> str | None:
    """The unit an ADT event belongs to, attributed through the bed
    the event names (admit/transfer/discharge events all record it)."""
    bed = (event.detail or {}).get("bed")
    return bed_units.get(bed) if bed else None


@router.get("/analytics/overview")
def analytics_overview(unit: str | None = Query(default=None),
                       region_id: str | None = Query(default=None),
                       location_id: str | None = Query(default=None),
                       department_id: str | None = Query(default=None),
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("workforce.read"))):
    """Flat headline numbers for big-number/gauge widgets — every one
    computed from live module data at call time. With ?unit=, the
    figures scope to that unit (beds, today's ADT flow attributed by
    bed, credentials of the unit's staff, the unit's open safety
    events, the unit's roster coverage, the unit's active staff);
    on-call coverage is a service-line figure and stays house-wide.
    With region_id / location_id / department_id, the figures scope
    to the units under that placement (resolved through the
    published org hierarchy) and the response echoes "scope"."""
    ops = _ops(session)
    scope_units, scope_echo = _analytics_scope(
        session, ops, unit, region_id, location_id, department_id)
    beds = _bed_stats(session)
    if scope_units is not None:
        occupied = 0
        beds_total = 0
        for code in scope_units:
            bucket = beds.get(code) or {"occupied": 0, "beds_total": 0}
            occupied += bucket["occupied"]
            beds_total += bucket["beds_total"]
        occupancy_pct = (round(100.0 * occupied / beds_total, 1)
                         if beds_total else 0.0)
    elif unit:
        bucket = beds.get(unit) or {"occupied": 0, "beds_total": 0,
                                    "occupancy_pct": 0.0}
        occupied = bucket["occupied"]
        beds_total = bucket["beds_total"]
        occupancy_pct = bucket["occupancy_pct"]
    else:
        occupied = sum(b["occupied"] for b in beds.values())
        beds_total = sum(b["beds_total"] for b in beds.values())
        occupancy_pct = (round(100.0 * occupied / beds_total, 1)
                         if beds_total else 0.0)
    today = _today()
    events = session.scalars(select(AdtEvent)).all()
    if scope_units is not None:
        bed_units = _bed_unit_map(session)
        events = [e for e in events
                  if _event_unit(e, bed_units) in scope_units]
    elif unit:
        bed_units = _bed_unit_map(session)
        events = [e for e in events if _event_unit(e, bed_units) == unit]
    admissions_today = sum(1 for e in events if e.event_type == "A01"
                            and e.created_at and e.created_at.date() == today)
    discharges_today = sum(1 for e in events if e.event_type == "A03"
                            and e.created_at and e.created_at.date() == today)
    worklist_window, _alert = _expiry_windows(ops)
    expiring = expired = 0
    credentials = session.scalars(select(StaffCredential)).all()
    if scope_units is not None:
        unit_staff = {s.staff_id for s in session.scalars(
            select(StaffMember).where(
                StaffMember.unit.in_(scope_units))).all()}
        credentials = [c for c in credentials if c.staff_id in unit_staff]
    elif unit:
        unit_staff = {s.staff_id for s in session.scalars(
            select(StaffMember).where(StaffMember.unit == unit)).all()}
        credentials = [c for c in credentials if c.staff_id in unit_staff]
    for cred in credentials:
        days = _days_to_expiry(cred.expires_on)
        if days is None:
            continue
        if days < 0:
            expired += 1
        elif days <= worklist_window:
            expiring += 1
    safety_stmt = select(SafetyEvent).where(SafetyEvent.status != "closed")
    if scope_units is not None:
        safety_stmt = safety_stmt.where(SafetyEvent.unit.in_(scope_units))
    elif unit:
        safety_stmt = safety_stmt.where(SafetyEvent.unit == unit)
    safety_open = session.scalars(safety_stmt).all()
    coverage = _coverage(session, ops)
    coverage_pct = coverage["overall_pct"]
    if scope_units is not None:
        coverage_pct = _scoped_coverage_pct(coverage, scope_units)
    elif unit:
        coverage_pct = next(
            (i["coverage_pct"] for i in coverage["items"]
             if i["unit"] == unit), None)
    staff_stmt = select(StaffMember).where(StaffMember.status == "active")
    if scope_units is not None:
        staff_stmt = staff_stmt.where(StaffMember.unit.in_(scope_units))
    elif unit:
        staff_stmt = staff_stmt.where(StaffMember.unit == unit)
    oncall = on_call_now(session=session, user=user)
    result = {
        "generated_at": _now().isoformat(),
        "unit": unit,
        "occupied": occupied, "beds_total": beds_total,
        "occupancy_pct": occupancy_pct,
        "admissions_today": admissions_today,
        "discharges_today": discharges_today,
        "credentials_expiring": expiring, "credentials_expired": expired,
        "safety_open": len(safety_open),
        "coverage_pct": coverage_pct,
        "staff_active": len(session.scalars(staff_stmt).all()),
        "on_call_covered": oncall["covered"],
        "on_call_services": oncall["total"],
    }
    if scope_echo is not None:
        result["scope"] = scope_echo
    return result


@router.get("/analytics/census-series")
def analytics_census_series(days: int = Query(default=7),
                            unit: str | None = Query(default=None),
                            region_id: str | None = Query(default=None),
                            location_id: str | None = Query(default=None),
                            department_id: str | None = Query(default=None),
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("workforce.read"))):
    """Daily admissions / discharges / end-of-day census for the last
    N days (N clamped 1..31), replayed from the Phase 6 ADT event log:
    the census at a day's end is the set of encounters admitted (A01)
    on or before it and not yet discharged (A03). With ?unit=, only
    events attributed to that unit (through their bed) count; with
    region_id / location_id / department_id, only events attributed
    to the units in that placement scope count."""
    scope_units, scope_echo = _analytics_scope(
        session, _ops(session), unit, region_id, location_id,
        department_id)
    days = max(1, min(31, days))
    today = _today()
    events = session.scalars(select(AdtEvent).order_by(
        AdtEvent.created_at.asc())).all()
    if scope_units is not None:
        bed_units = _bed_unit_map(session)
        events = [e for e in events
                  if _event_unit(e, bed_units) in scope_units]
    elif unit:
        bed_units = _bed_unit_map(session)
        events = [e for e in events if _event_unit(e, bed_units) == unit]
    items = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        admissions = sum(1 for e in events if e.event_type == "A01"
                         and e.created_at and e.created_at.date() == day)
        discharges = sum(1 for e in events if e.event_type == "A03"
                         and e.created_at and e.created_at.date() == day)
        admitted: set[str] = set()
        discharged: set[str] = set()
        for event in events:
            if event.created_at is None or event.created_at.date() > day:
                continue
            key = event.encounter_id or event.patient_ref or ""
            if event.event_type == "A01":
                admitted.add(key)
            elif event.event_type == "A03":
                discharged.add(key)
        items.append({"date": day.isoformat(), "admissions": admissions,
                      "discharges": discharges,
                      "census": len(admitted - discharged)})
    result = {"items": items, "total": len(items), "days": days}
    if scope_echo is not None:
        result["scope"] = scope_echo
    return result


@router.get("/analytics/units")
def analytics_units(unit: str | None = Query(default=None),
                    region_id: str | None = Query(default=None),
                    location_id: str | None = Query(default=None),
                    department_id: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability("workforce.read"))):
    """Per-unit rollup: bed state (Phase 6), today's roster, assigned
    staff, open safety events — the unit table/bar widgets' data.
    With ?unit=, the rollup carries that unit alone; with region_id /
    location_id / department_id, it carries the units in that
    placement scope alone."""
    ops = _ops(session)
    scope_units, scope_echo = _analytics_scope(
        session, ops, unit, region_id, location_id, department_id)
    beds = _bed_stats(session)
    today = _today().isoformat()
    items = []
    for unit_entry in ops.get("units") or []:
        if not isinstance(unit_entry, dict) or not unit_entry.get("code"):
            continue
        code = unit_entry["code"]
        if scope_units is not None and code not in scope_units:
            continue
        if unit and code != unit:
            continue
        bucket = beds.get(code) or {"beds_total": 0, "occupied": 0,
                                    "occupancy_pct": 0.0}
        roster_today = len({r.staff_id for r in session.scalars(
            select(RosterShift).where(RosterShift.unit == code,
                                      RosterShift.date == today,
                                      RosterShift.status == "scheduled")).all()})
        assigned = len(session.scalars(select(StaffMember).where(
            StaffMember.unit == code,
            StaffMember.status == "active")).all())
        open_safety = len(session.scalars(select(SafetyEvent).where(
            SafetyEvent.unit == code,
            SafetyEvent.status != "closed")).all())
        items.append({"unit": code, "display": unit_entry.get("display"),
                      "service_line": unit_entry.get("serviceLine"),
                      "beds_total": bucket["beds_total"],
                      "occupied": bucket["occupied"],
                      "occupancy_pct": bucket["occupancy_pct"],
                      "roster_today": roster_today,
                      "staff_assigned": assigned,
                      "open_safety_events": open_safety})
    result = {"items": items, "total": len(items)}
    if scope_echo is not None:
        result["scope"] = scope_echo
    return result


@router.get("/analytics/service-lines")
def analytics_service_lines(session: Session = Depends(get_db),
                            user: User = Depends(require_capability("workforce.read"))):
    """Per-service-line rollup: assigned staff, occupied beds across
    the line's units, and who is on call for the line right now."""
    ops = _ops(session)
    beds = _bed_stats(session)
    unit_line = {u["code"]: u.get("serviceLine") for u in ops.get("units") or []
                 if isinstance(u, dict) and u.get("code")}
    now_view = on_call_now(session=session, user=user)
    on_call_by_line = {s["service_line"]: s for s in now_view["services"]}
    items = []
    for line in ops.get("serviceLines") or []:
        if not isinstance(line, dict) or not line.get("code"):
            continue
        code = line["code"]
        staff = len(session.scalars(select(StaffMember).where(
            StaffMember.service_line == code,
            StaffMember.status == "active")).all())
        occupied = sum(b["occupied"] for unit_code, b in beds.items()
                       if unit_line.get(unit_code) == code)
        entry = on_call_by_line.get(code) or {}
        primary = entry.get("primary") or {}
        items.append({"service_line": code, "display": line.get("display"),
                      "staff": staff, "occupied": occupied,
                      "on_call": primary.get("staff_name")})
    return {"items": items, "total": len(items)}


@router.get("/analytics/coverage")
def analytics_coverage(unit: str | None = Query(default=None),
                       region_id: str | None = Query(default=None),
                       location_id: str | None = Query(default=None),
                       department_id: str | None = Query(default=None),
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("workforce.read"))):
    """Workforce coverage vs roster for today (see _coverage). With
    ?unit=, the response carries that unit alone and the headline
    figure is that unit's fill rate; with region_id / location_id /
    department_id, it carries the units in that placement scope and
    the headline figure is the scope's fill rate."""
    ops = _ops(session)
    scope_units, scope_echo = _analytics_scope(
        session, ops, unit, region_id, location_id, department_id)
    result = _coverage(session, ops)
    if scope_units is not None:
        items = [i for i in result["items"] if i["unit"] in scope_units]
        result = {**result, "items": items, "total": len(items),
                  "overall_pct": _scoped_coverage_pct(result, scope_units),
                  "scope": scope_echo}
    if unit:
        items = [i for i in result["items"] if i["unit"] == unit]
        result = {**result, "items": items, "total": len(items),
                  "overall_pct": (items[0]["coverage_pct"] if items else None),
                  "unit": unit}
    return result


@router.get("/analytics/quality-summary")
def analytics_quality_summary(session: Session = Depends(get_db),
                              user: User = Depends(require_capability("workforce.read"))):
    """Published quality measures with their current rates, computed
    by the Phase 9 engine over the warehouse (small-cell suppression
    is the engine's own contract and is preserved in the items)."""
    from . import quality as quality_module
    from . import warehouse as warehouse_module
    items = []
    for cfg in published_configs(session, "measure"):
        payload = cfg.payload or {}
        try:
            factory = warehouse_module.warehouse_session_factory()
            with factory() as wh_session:
                warehouse_module.ensure_concept_map(wh_session)
                result = quality_module.compute_measure(
                    wh_session, cfg.key, payload)
            items.append({"measure": cfg.key,
                          "title": payload.get("title") or payload.get("name"),
                          "numerator": result["numerator"],
                          "denominator": result["denominator"],
                          "rate": result["rate"],
                          "suppressed": result["suppressed"]})
        except Exception:
            items.append({"measure": cfg.key,
                          "title": payload.get("title") or payload.get("name"),
                          "numerator": None, "denominator": None,
                          "rate": None, "suppressed": True})
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Seeds: workforce config, synthetic directory, roster, on-call,
# audience dashboards (published), register entries
# ---------------------------------------------------------------------------

def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
    if list_config_versions(session, kind, key):
        return
    from .models import ConfigStatus, StudioConfig
    row = StudioConfig(kind=kind, key=key, version=1,
                       status=ConfigStatus.published, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:{kind}/{key}@v1",
                 payload={"kind": kind, "key": key, "version": 1,
                          "seeded_default": True})
    append_audit(session, actor="system", action="studio.config.publish",
                 entity=f"config:{kind}/{key}@v1",
                 payload={"kind": kind, "key": key, "version": 1,
                          "superseded_version": None, "seeded_default": True})


def _extend_workforce_psv(session: Session) -> None:
    """Governed-append the WF-B1 `psv` section onto the published
    workforce config (the governed evolution pattern of
    inpatient._extend_note_types / surgery_depth's PACU gate): a
    database seeded by an older build keeps its published workforce
    payload forever under insert-if-absent seeding, so when the
    published payload lacks `psv` entirely, the CURRENT payload is
    preserved verbatim except for the added section and republished
    as a NEW version by the system actor with audit rows. A payload
    that already carries `psv` — including a site's own reshaping of
    it — is a site decision and is never overwritten. Idempotent."""
    cfg = published_config(session, "terminology", WORKFORCE_KEY)
    if cfg is None:
        return
    payload = cfg.payload or {}
    if "psv" in payload:
        return
    from .models import ConfigStatus, StudioConfig
    new_payload = dict(payload)
    new_payload["psv"] = _default_psv()
    cfg.status = ConfigStatus.superseded
    session.flush()
    versions = list_config_versions(session, "terminology", WORKFORCE_KEY)
    row = StudioConfig(kind="terminology", key=WORKFORCE_KEY,
                       version=versions[-1].version + 1,
                       status=ConfigStatus.published, payload=new_payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    for action in ("studio.config.create", "studio.config.publish"):
        append_audit(session, actor="system", action=action,
                     entity=f"config:terminology/{WORKFORCE_KEY}"
                            f"@v{row.version}",
                     payload={"kind": "terminology", "key": WORKFORCE_KEY,
                              "version": row.version,
                              "seeded_default": True,
                              "wave_a_extension": ["psv"]})


# Synthetic directory. Credential expiry offsets are RELATIVE to the
# seed date (days from today), so the worklist/alert semantics hold on
# any day the system is first booted: +20 = inside the alert window,
# +45 = worklist only, -5 = already expired.
_STAFF_SEED = [
    # staff_id, username, name, job_role, unit, service_line, status, audience
    ("staff-001", "dev-clinician", "Maya Chen, MD", "physician",
     "unit-medsurg", "medicine", "active", "clinician"),
    ("staff-002", None, "James Okafor, MD", "physician",
     "unit-icu", "critical-care", "active", "clinician"),
    ("staff-003", None, "Priya Nair, RN", "nurse",
     "unit-medsurg", "medicine", "active", "nurse"),
    ("staff-004", None, "Tom Becker, RN", "nurse",
     "unit-medsurg", "medicine", "active", "nurse"),
    ("staff-005", None, "Sara Lindqvist, RN", "nurse",
     "unit-icu", "critical-care", "active", "nurse"),
    ("staff-006", None, "David Kim, RN", "nurse",
     "ed", "emergency", "active", "nurse"),
    ("staff-007", None, "Ana Ruiz, PharmD", "pharmacist",
     "pharmacy", "pharmacy", "active", "clinician"),
    ("staff-008", None, "Mark Doyle, MLT", "lab-technologist",
     "laboratory", "laboratory", "active", "clinician"),
    ("staff-009", "dev-viewer", "Grace Liu, RN", "unit-manager",
     "unit-medsurg", "medicine", "active", "unit-manager"),
    ("staff-010", "dev-configurator", "Robert Hale", "department-head",
     None, "medicine", "active", "department-head"),
    ("staff-011", "dev-admin", "Elena Petrova, MD", "executive",
     None, None, "active", "executive"),
    ("staff-012", None, "Yusuf Ahmed, MD", "physician",
     "ed", "emergency", "active", "clinician"),
    ("staff-013", None, "Karen White, RN", "nurse",
     "unit-icu", "critical-care", "active", "nurse"),
    ("staff-014", None, "Paul Novak", "support-staff",
     "ed", "emergency", "active", None),
    ("staff-015", None, "Nina Park, MD", "physician",
     "clinic", "medicine", "active", "clinician"),
    ("staff-016", None, "Omar Haddad, RN", "nurse",
     "ed", "emergency", "leave", "nurse"),
    ("staff-017", None, "Leo Fischer, RN", "nurse",
     "ed", "emergency", "active", "nurse"),
    ("staff-018", None, "June Park, RN", "nurse",
     "unit-medsurg", "medicine", "active", "nurse"),
    # Front Door pass: the front desk — linked to the dev-frontdesk
    # account (app.accounts seed), so that sign-in lands on the
    # front-desk audience home.
    ("staff-019", "dev-frontdesk", "Rita Santos", "front-desk",
     "clinic", "medicine", "active", "front-desk"),
]

_CREDENTIAL_SEED = [
    # staff_id, type, synthetic number, expiry offset (days from today)
    ("staff-001", "medical-licence", "SYN-MD-1001", 200),
    ("staff-001", "board-certification", "SYN-BC-1001", 400),
    ("staff-001", "bls", "SYN-BLS-1001", 20),
    ("staff-001", "acls", "SYN-ACLS-1001", 300),
    ("staff-002", "medical-licence", "SYN-MD-1002", 45),
    ("staff-002", "acls", "SYN-ACLS-1002", 15),
    ("staff-003", "rn-licence", "SYN-RN-1003", 10),
    ("staff-003", "bls", "SYN-BLS-1003", 120),
    ("staff-004", "rn-licence", "SYN-RN-1004", -5),
    ("staff-004", "bls", "SYN-BLS-1004", 60),
    ("staff-005", "rn-licence", "SYN-RN-1005", 300),
    ("staff-005", "acls", "SYN-ACLS-1005", 25),
    ("staff-006", "rn-licence", "SYN-RN-1006", 80),
    ("staff-006", "acls", "SYN-ACLS-1006", 200),
    ("staff-007", "pharmacist-licence", "SYN-PH-1007", 180),
    ("staff-007", "dea-registration", "SYN-DEA-1007", 55),
    ("staff-008", "technologist-licence", "SYN-MT-1008", 95),
    ("staff-008", "bls", "SYN-BLS-1008", 40),
    ("staff-009", "rn-licence", "SYN-RN-1009", 240),
    ("staff-012", "medical-licence", "SYN-MD-1012", 150),
    ("staff-012", "dea-registration", "SYN-DEA-1012", 35),
    ("staff-013", "rn-licence", "SYN-RN-1013", 500),
    ("staff-013", "acls", "SYN-ACLS-1013", 90),
    ("staff-015", "medical-licence", "SYN-MD-1015", 28),
    ("staff-015", "dea-registration", "SYN-DEA-1015", 600),
    ("staff-017", "rn-licence", "SYN-RN-1017", 33),
    ("staff-018", "rn-licence", "SYN-RN-1018", 12),
]

# (staff_id, unit, template) rostered for today and the next two days.
_ROSTER_SEED = [
    ("staff-001", "unit-medsurg", "day"), ("staff-003", "unit-medsurg", "day"),
    ("staff-009", "unit-medsurg", "day"), ("staff-018", "unit-medsurg", "day"),
    ("staff-004", "unit-medsurg", "night"),
    ("staff-002", "unit-icu", "day"), ("staff-005", "unit-icu", "day"),
    ("staff-013", "unit-icu", "night"),
    ("staff-006", "ed", "day"), ("staff-012", "ed", "day"),
    ("staff-014", "ed", "day"), ("staff-017", "ed", "night"),
    ("staff-015", "clinic", "clinic-day"),
]

# (service_line, staff_id, role) on call for the current week.
_ONCALL_SEED = [
    ("medicine", "staff-001", "primary"), ("medicine", "staff-015", "backup"),
    ("critical-care", "staff-002", "primary"),
    ("emergency", "staff-012", "primary"),
    ("pharmacy", "staff-007", "primary"),
    ("laboratory", "staff-008", "primary"),
]


def _seed_workforce_data(session: Session) -> None:
    if session.scalars(select(StaffMember)).first() is not None:
        return
    today = _today()
    for staff_id, username, name, job_role, unit, line, status, audience \
            in _STAFF_SEED:
        session.add(StaffMember(
            staff_id=staff_id, username=username, full_name=name,
            job_role=job_role, unit=unit, service_line=line, status=status,
            audience=audience,
            email=f"{staff_id}@example.invalid", phone=""))
    session.flush()
    for index, (staff_id, cred_type, number, offset) in enumerate(
            _CREDENTIAL_SEED, start=1):
        expires = today + timedelta(days=offset)
        issued = expires - timedelta(days=730)
        session.add(StaffCredential(
            credential_id=f"cred-{index:03d}", staff_id=staff_id,
            type=cred_type, number=number, issued_on=issued.isoformat(),
            expires_on=expires.isoformat()))
    session.flush()
    ops = default_workforce_payload()
    templates = {t["code"]: t for t in ops["shiftTemplates"]}
    counter = 0
    for day_offset in range(3):
        day = today + timedelta(days=day_offset)
        for staff_id, unit, template_code in _ROSTER_SEED:
            template = templates[template_code]
            start_t = time.fromisoformat(template["start"])
            end_t = time.fromisoformat(template["end"])
            starts_at = datetime.combine(day, start_t, tzinfo=timezone.utc)
            ends_at = datetime.combine(day, end_t, tzinfo=timezone.utc)
            if ends_at <= starts_at:
                ends_at += timedelta(days=1)
            counter += 1
            session.add(RosterShift(
                shift_id=f"shift-seed-{counter:03d}", staff_id=staff_id,
                unit=unit, date=day.isoformat(), template_code=template_code,
                starts_at=starts_at, ends_at=ends_at, status="scheduled"))
    session.flush()
    week_start = datetime.combine(today - timedelta(days=today.weekday()),
                                  time.min, tzinfo=timezone.utc)
    week_end = week_start + timedelta(days=7)
    for index, (line, staff_id, role_on_call) in enumerate(_ONCALL_SEED, 1):
        session.add(OnCallAssignment(
            assignment_id=f"oncall-seed-{index:02d}", service_line=line,
            staff_id=staff_id, role_on_call=role_on_call,
            starts_at=week_start, ends_at=week_end))
    session.flush()
    append_audit(session, actor="system", action="workforce.seed",
                 entity="workforce",
                 payload={"staff": len(_STAFF_SEED),
                          "credentials": len(_CREDENTIAL_SEED),
                          "roster_days": 3, "on_call": len(_ONCALL_SEED),
                          "synthetic": True})


def default_dashboard_payloads() -> dict[str, dict]:
    """The five seeded audience dashboards (published v1). Widgets are
    composition over live endpoints only — the Phase 10 rule."""
    census_line = {
        "id": "census-trend", "title": "Census trend (7 days)",
        "endpoint": "/people/analytics/census-series?days=7",
        "display": "line", "labelField": "date",
        "series": [{"field": "census", "label": "Census"}]}
    quality_table = {
        "id": "quality", "title": "Quality measures",
        "endpoint": "/people/analytics/quality-summary", "display": "table",
        "columns": [{"field": "title", "title": "Measure"},
                    {"field": "numerator", "title": "Num"},
                    {"field": "denominator", "title": "Den"},
                    {"field": "rate", "title": "Rate %"}]}
    service_line_table = {
        "id": "service-lines", "title": "Service lines",
        "endpoint": "/people/analytics/service-lines", "display": "table",
        "columns": [{"field": "display", "title": "Service line"},
                    {"field": "staff", "title": "Staff"},
                    {"field": "occupied", "title": "Patients in beds"},
                    {"field": "on_call", "title": "On call now"}]}
    return {
        "audience-clinician-day": {
            "title": "Clinician day",
            "description": ("Phase 13 (Track E): the working clinician's "
                            "morning view — census, today's flow, and the "
                            "queues that need a clinician."),
            "audience": "clinician",
            "roles": ["clinician", "admin"],
            "widgets": [
                {"id": "in-beds", "title": "Patients in beds",
                 "endpoint": "/people/analytics/overview",
                 "display": "big-number", "field": "occupied"},
                {"id": "admissions", "title": "Admissions today",
                 "endpoint": "/people/analytics/overview",
                 "display": "big-number", "field": "admissions_today"},
                {"id": "verify", "title": "Lab results awaiting verification",
                 "endpoint": "/lab/verification-queue", "display": "total"},
                {"id": "critical", "title": "Critical results open",
                 "endpoint": "/lab/critical-reports", "display": "total"},
                {"id": "ready", "title": "Patients ready for discharge",
                 "endpoint": "/continuum/inpatient/discharge-readiness",
                 "display": "value", "field": "ready"},
                {"id": "flow", "title": "Admissions vs discharges (7 days)",
                 "endpoint": "/people/analytics/census-series?days=7",
                 "display": "line", "labelField": "date",
                 "series": [{"field": "admissions", "label": "Admissions"},
                            {"field": "discharges", "label": "Discharges"}]},
            ]},
        "audience-nurse-unit": {
            "title": "Nurse / unit",
            "description": ("Phase 13 (Track E): the unit at a glance — "
                            "occupancy, staffing coverage, discharge "
                            "readiness and open safety work."),
            "audience": "nurse",
            "roles": ["clinician", "viewer", "admin"],
            "widgets": [
                {"id": "units", "title": "Units today",
                 "endpoint": "/people/analytics/units", "display": "table",
                 "columns": [{"field": "display", "title": "Unit"},
                             {"field": "occupied", "title": "Occupied"},
                             {"field": "beds_total", "title": "Beds"},
                             {"field": "occupancy_pct", "title": "Occupancy %"},
                             {"field": "roster_today", "title": "Rostered"}]},
                {"id": "coverage", "title": "Staffing coverage today",
                 "endpoint": "/people/analytics/coverage",
                 "display": "gauge", "field": "overall_pct", "max": 100,
                 "unit": "%", "thresholds": {"warn": 90, "critical": 75}},
                {"id": "occupancy", "title": "Occupancy by unit",
                 "endpoint": "/people/analytics/units", "display": "bar",
                 "labelField": "display",
                 "series": [{"field": "occupancy_pct", "label": "Occupancy %"}]},
                {"id": "ready", "title": "Patients ready for discharge",
                 "endpoint": "/continuum/inpatient/discharge-readiness",
                 "display": "value", "field": "ready"},
                {"id": "safety", "title": "Open safety events",
                 "endpoint": "/continuum/safety/summary",
                 "display": "value", "field": "open"},
            ]},
        "audience-unit-manager": {
            "title": "Unit manager",
            "description": ("Phase 13 (Track E): staffing vs the published "
                            "minimums, census trend, credential risk and "
                            "safety — the unit manager's operating view."),
            "audience": "unit-manager",
            "roles": ["clinician", "viewer", "admin"],
            "widgets": [
                {"id": "coverage", "title": "Staffing coverage today",
                 "endpoint": "/people/analytics/coverage",
                 "display": "gauge", "field": "overall_pct", "max": 100,
                 "unit": "%", "thresholds": {"warn": 90, "critical": 75}},
                {"id": "coverage-table", "title": "Coverage by unit",
                 "endpoint": "/people/analytics/coverage", "display": "table",
                 "columns": [{"field": "display", "title": "Unit"},
                             {"field": "scheduled", "title": "Rostered"},
                             {"field": "required", "title": "Minimum"},
                             {"field": "coverage_pct", "title": "Coverage %"}]},
                census_line,
                {"id": "expiring", "title": "Credentials expiring (90 days)",
                 "endpoint": "/people/analytics/overview",
                 "display": "big-number", "field": "credentials_expiring"},
                {"id": "safety", "title": "Open safety events",
                 "endpoint": "/continuum/safety/summary",
                 "display": "value", "field": "open"},
            ]},
        "audience-department-head": {
            "title": "Department head — service lines",
            "description": ("Phase 13 (Track E): the service-line view — "
                            "staff, census and on-call per line, with the "
                            "quality measures the line is accountable for."),
            "audience": "department-head",
            "roles": ["configurator", "admin", "clinician"],
            "widgets": [
                service_line_table,
                {"id": "flow", "title": "Admissions (7 days)",
                 "endpoint": "/people/analytics/census-series?days=7",
                 "display": "bar", "labelField": "date",
                 "series": [{"field": "admissions", "label": "Admissions"}]},
                quality_table,
                {"id": "staff", "title": "Active staff",
                 "endpoint": "/people/analytics/overview",
                 "display": "big-number", "field": "staff_active"},
            ]},
        "audience-executive": {
            "title": "Executive",
            "description": ("Phase 13 (Track E): the house at a glance — "
                            "census, flow, staffing coverage, quality and "
                            "service-line accountability."),
            "audience": "executive",
            "roles": ["admin"],
            "widgets": [
                {"id": "in-beds", "title": "Patients in beds",
                 "endpoint": "/people/analytics/overview",
                 "display": "big-number", "field": "occupied"},
                {"id": "occupancy", "title": "Occupancy",
                 "endpoint": "/people/analytics/overview",
                 "display": "gauge", "field": "occupancy_pct", "max": 100,
                 "unit": "%"},
                {"id": "admissions", "title": "Admissions today",
                 "endpoint": "/people/analytics/overview",
                 "display": "big-number", "field": "admissions_today"},
                {"id": "discharges", "title": "Discharges today",
                 "endpoint": "/people/analytics/overview",
                 "display": "big-number", "field": "discharges_today"},
                census_line,
                quality_table,
                service_line_table,
            ]},
        "audience-front-desk": {
            "title": "Front desk",
            "description": ("Front Door pass: the registration desk's "
                            "day — today's appointments and arrivals, "
                            "the open waitlist, and today's "
                            "registrations, all from live data "
                            "(app.registration/frontdesk/today). The "
                            "register-patient and book-appointment "
                            "quick actions render above this dashboard "
                            "on the home page."),
            "audience": "front-desk",
            "roles": ["viewer", "clinician", "configurator", "admin"],
            "widgets": [
                {"id": "appointments", "title": "Appointments today",
                 "endpoint": "/registration/frontdesk/today",
                 "display": "big-number", "field": "appointments_total"},
                {"id": "awaiting-checkin", "title": "Awaiting check-in",
                 "endpoint": "/registration/frontdesk/today",
                 "display": "big-number", "field": "awaiting_checkin"},
                {"id": "awaiting-room", "title": "Arrived — awaiting a room",
                 "endpoint": "/registration/frontdesk/today",
                 "display": "big-number", "field": "awaiting_room"},
                {"id": "waitlist", "title": "Waitlist (open)",
                 "endpoint": "/registration/frontdesk/today",
                 "display": "big-number", "field": "waitlist_open"},
                {"id": "registrations", "title": "Registered today",
                 "endpoint": "/registration/frontdesk/today",
                 "display": "big-number", "field": "registrations_today"},
                {"id": "today-list", "title": "Today's appointments",
                 "endpoint": "/registration/frontdesk/today",
                 "display": "table",
                 "columns": [{"field": "time", "title": "Time"},
                             {"field": "patient", "title": "Patient"},
                             {"field": "session", "title": "Session"},
                             {"field": "status", "title": "Status"}]},
            ]},
    }


def default_requirement_seeds() -> list[dict]:
    return [
        {"body": "The Joint Commission",
         "citation": "Human Resources HR.01.02.05",
         "title": "Credential verification at hire and at renewal",
         "control": ("Every staff member's licences and certifications "
                     "are directory data with expiry dates; expiry state "
                     "is computed against the published workforce "
                     "windows, the expiry worklist surfaces everything "
                     "lapsed or lapsing, and the credential scan raises "
                     "rules-engine renewal alerts (seeded: a Task at "
                     "<= 30 days) before a credential lapses — "
                     "verification is a standing system state, not a "
                     "filing exercise."),
         "implementation_ref": "backend/app/people.py (staff directory, "
                               "credentials, worklist, scan); "
                               "rule credential-expiry-alert",
         "evidence_source": "audit:workforce."},
        {"body": "The Joint Commission",
         "citation": "Medical Staff MS.06.01.03",
         "title": "Privileging grounded in verified credentials",
         "control": ("Assignment surfaces (rosters, on-call schedules) "
                     "read the same credential records the worklist "
                     "governs: a clinician whose licence has lapsed is "
                     "visibly expired on the directory, the worklist "
                     "and the analytics rollups before they are rostered "
                     "or put on call, and every credential change is in "
                     "the hash-chained audit."),
         "implementation_ref": "backend/app/people.py (roster + on-call "
                               "over the credential store)",
         "evidence_source": "audit:workforce.credential.create"},
        {"body": "CMS",
         "citation": "42 CFR 482.24(b)(1)",
         "title": "Record retention — reports, documents and exports",
         "control": ("Medical records are retained at least 5 years; "
                     "the same discipline covers what the data "
                     "platform generates: every report run, printed "
                     "document (invoice, receipt, statement, "
                     "superbill) and dataset export writes a "
                     "hash-chained audit record (actor, definition, "
                     "parameters, row counts) at generation time, and "
                     "exports above the published row threshold "
                     "require a recorded reason — the generation "
                     "record is retained with the audit chain."),
         "implementation_ref": "backend/app/reports.py (catalogue runs, "
                               "documents, export centre); "
                               "backend/app/audit.py",
         "evidence_source": "audit:reports."},
    ]


def ensure_people_defaults(session: Session) -> None:
    """Seed the workforce config (published v1), the synthetic
    directory + credentials + roster + on-call, the five audience
    dashboards (published v1) and the Phase 13 register entries.
    Idempotent throughout."""
    _seed_published(session, "terminology", WORKFORCE_KEY,
                    default_workforce_payload())
    _extend_workforce_psv(session)
    _seed_workforce_data(session)
    for key, payload in default_dashboard_payloads().items():
        _seed_published(session, "dashboard", key, payload)
    existing = {(r.body, r.citation) for r in
                session.scalars(select(ComplianceRequirement)).all()}
    added = 0
    for seed in default_requirement_seeds():
        if (seed["body"], seed["citation"]) in existing:
            continue
        session.add(ComplianceRequirement(created_by="system", **seed))
        added += 1
    if added:
        session.flush()
        append_audit(session, actor="system",
                     action="compliance.register.seed", entity="register",
                     payload={"added": added, "phase": 13})
    session.commit()
