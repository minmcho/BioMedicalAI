"""Workforce onboarding & offboarding — Workforce, Teams & Panels
pass, scope A (app.onboarding).

The realistic institutional joiner flow as ONE governed pipeline over
the stores that already exist (the Phase 13 staff directory and
credential store, the Front Door account records, the sibling
app.privileging grant store):

    hr-profile -> credentialing -> privileging -> account
               -> orientation -> active        (terminal: terminated)

- A record is CREATED at stage "credentialing" — creation IS the
  hr-profile stage: the StaffMember is written (status "inactive")
  validated against the published workforce config EXACTLY as
  app.people.create_staff validates, and an OnboardingRecord opens.
- Each advance is gated on the CURRENT stage's computed blockers:
  credentialing refuses (409) while a job-catalogue required
  credential is missing or its latest instance has expired;
  privileging grants the job role's privilege template through
  app.privileging.grant_privilege (already-granted codes are skipped);
  account provisions a real UserAccount (dev token returned ONCE,
  only its SHA-256 stored — app.accounts semantics) or recognises an
  already-linked account; orientation refuses while any required item
  of the published `form/orientation-checklist` is incomplete, and
  passing it flips the directory entry to status "active".
- Termination is the offboarding mirror: the record closes, the
  directory entry goes inactive, the linked account is disabled (its
  dev token dies at the next request), active team memberships end,
  and active empanelments naming the person as PCP end — the patient
  is unempanelled, never silently reassigned.

Every mutation is hash-chain audited. The orientation checklist is a
governed Studio form (kind "form", key "orientation-checklist"),
seeded published v1 like every other form in this system.
"""
import re
import secrets
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import people
from . import validate_fields as vf
from .audit import append_audit
from .configstore import (CAPABILITY_DEFAULTS, list_config_versions,
                          published_config, validate_form)
from .models import (ConfigStatus, Role, StaffCredential, StaffMember,
                     StudioConfig, User, UserAccount)
from .security import auth_mode, get_db, require_capability, token_hash

# The onboarding capability: workforce administration (the
# workforce.work split) — registered here so the gate exists with the
# module that enforces it; a published permissions config may still
# override individual cells.
CAPABILITY_DEFAULTS.setdefault("workforce.onboard",
                               ("admin", "configurator"))

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

ORIENTATION_FORM_KEY = "orientation-checklist"

STAGES = ["hr-profile", "credentialing", "privileging", "account",
          "orientation", "active"]
TERMINAL_STAGE = "terminated"

# (linkId, text) — every item is a required boolean on the form.
ORIENTATION_ITEMS = [
    ("verify-identity", "Identity verified"),
    ("verify-credentials", "Credentials verified"),
    ("hipaa-training", "HIPAA training completed"),
    ("safety-training", "Safety training completed"),
    ("system-access", "System access provisioned"),
    ("department-orientation", "Department orientation completed"),
    ("equipment-competency", "Equipment competency assessed"),
    ("policy-attestation", "Policy attestation signed"),
]

_DEV_TOKEN_NOTE = (
    "Dev-mode sign-in token — shown once, store it now. "
    "Production sign-in is the identity provider (OIDC).")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> date:
    return _now().date()


def _models():
    """The workforce-pass models, imported lazily so this module binds
    to the sibling-owned model definitions at call time."""
    from . import models
    return models


def _privileging():
    try:
        from . import privileging
        return privileging
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Orientation form (governed Studio config, seeded published v1)
# ---------------------------------------------------------------------------

def default_orientation_form_payload() -> dict:
    return {
        "resourceType": "Questionnaire",
        "title": "Orientation checklist",
        "item": [
            {"linkId": link_id, "text": text, "type": "boolean",
             "required": True}
            for link_id, text in ORIENTATION_ITEMS
        ],
    }


def _seed_published(session: Session, kind: str, key: str,
                    payload: dict) -> None:
    if list_config_versions(session, kind, key):
        return
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
                          "superseded_version": None,
                          "seeded_default": True})


def _orientation_items(session: Session) -> list[dict]:
    """The published orientation form's items (code, text, required),
    falling back to the seeded defaults when unpublished."""
    cfg = published_config(session, "form", ORIENTATION_FORM_KEY)
    payload = (cfg.payload or {}) if cfg is not None \
        else default_orientation_form_payload()
    items = []
    for item in payload.get("item") or []:
        if isinstance(item, dict) and item.get("linkId"):
            items.append({"linkId": item["linkId"],
                          "text": item.get("text") or item["linkId"],
                          "required": bool(item.get("required"))})
    return items or [{"linkId": code, "text": text, "required": True}
                     for code, text in ORIENTATION_ITEMS]


# ---------------------------------------------------------------------------
# Job catalogue (app.privileging contract, with a local default mirror
# used only until that module is present)
# ---------------------------------------------------------------------------

_FALLBACK_ROLES = [
    {"jobRole": "physician", "display": "Physician", "clinical": True,
     "requiredCredentials": ["medical-licence"],
     "defaultAccountRole": "clinician",
     "privilegeTemplate": ["attending-of-record"]},
    {"jobRole": "nurse", "display": "Nurse", "clinical": True,
     "requiredCredentials": ["rn-licence"],
     "defaultAccountRole": "clinician", "privilegeTemplate": []},
    {"jobRole": "pharmacist", "display": "Pharmacist", "clinical": True,
     "requiredCredentials": ["pharmacist-licence"],
     "defaultAccountRole": "clinician", "privilegeTemplate": []},
    {"jobRole": "lab-technologist", "display": "Laboratory technologist",
     "clinical": True, "requiredCredentials": ["technologist-licence"],
     "defaultAccountRole": "clinician", "privilegeTemplate": []},
    {"jobRole": "radiographer", "display": "Radiographer",
     "clinical": True, "requiredCredentials": [],
     "defaultAccountRole": "clinician", "privilegeTemplate": []},
    {"jobRole": "front-desk", "display": "Front desk / registration",
     "clinical": False, "requiredCredentials": [],
     "defaultAccountRole": "viewer", "privilegeTemplate": []},
]


def _job_role_entry(session: Session, job_role: str) -> dict:
    """The job-catalogue entry for one job role ({} when the catalogue
    does not name it — the workforce config remains the authority on
    which roles exist; the catalogue adds credential/privilege data)."""
    priv = _privileging()
    catalogue = None
    if priv is not None:
        try:
            catalogue = priv.job_catalogue(session)
        except Exception:
            catalogue = None
    roles = (catalogue or {}).get("roles") if isinstance(catalogue, dict) \
        else None
    if not isinstance(roles, list):
        roles = _FALLBACK_ROLES
    for entry in roles:
        if isinstance(entry, dict) and entry.get("jobRole") == job_role:
            return entry
    for entry in _FALLBACK_ROLES:
        if entry["jobRole"] == job_role:
            return entry
    return {}


def _required_credentials(entry: dict) -> list[str]:
    values = entry.get("requiredCredentials") or []
    return [str(v) for v in values if isinstance(v, str) and v]


def _privilege_template(entry: dict) -> list[str]:
    values = entry.get("privilegeTemplate") or []
    return [str(v) for v in values if isinstance(v, str) and v]


# ---------------------------------------------------------------------------
# Record access + serializers
# ---------------------------------------------------------------------------

def _get_record(session: Session, onboarding_id: str):
    models = _models()
    row = session.scalars(select(models.OnboardingRecord).where(
        models.OnboardingRecord.onboarding_id == onboarding_id)).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Onboarding record '{onboarding_id}' not found")
    return row


def _record_out(row) -> dict:
    return {
        "onboarding_id": row.onboarding_id, "staff_id": row.staff_id,
        "employee_id": row.employee_id, "stage": row.stage,
        "supervisor_staff_id": row.supervisor_staff_id, "fte": row.fte,
        "start_date": row.start_date,
        "account_username": row.account_username,
        "created_by": row.created_by,
        "terminated_at": (row.terminated_at.isoformat()
                           if isinstance(row.terminated_at, datetime)
                           else row.terminated_at),
        "termination_reason": row.termination_reason,
    }


def _find_account(session: Session, record, staff: StaffMember):
    """The managed account linked to this person: by staff_id link, or
    by username matching the directory entry / the record."""
    by_staff = session.scalars(select(UserAccount).where(
        UserAccount.staff_id == record.staff_id)).first()
    if by_staff is not None:
        return by_staff
    usernames = {u for u in (staff.username if staff else None,
                             record.account_username) if u}
    for username in usernames:
        row = session.scalars(select(UserAccount).where(
            UserAccount.username == username)).first()
        if row is not None:
            return row
    return None


def _account_out_brief(row: UserAccount) -> dict:
    return {"username": row.username, "status": row.status,
            "role": row.role.value if isinstance(row.role, Role)
            else str(row.role)}


def _privilege_out(row) -> dict:
    granted_at = getattr(row, "granted_at", None)
    return {"grant_id": getattr(row, "grant_id", None),
            "privilege_code": getattr(row, "privilege_code", None),
            "service_line": getattr(row, "service_line", None),
            "granted_by": getattr(row, "granted_by", None),
            "granted_at": (granted_at.isoformat()
                           if isinstance(granted_at, datetime)
                           else granted_at)}


def _active_grants(session: Session, staff_id: str) -> list:
    priv = _privileging()
    if priv is not None:
        try:
            return list(priv.active_grants(session, staff_id))
        except Exception:
            pass
    model = getattr(_models(), "StaffPrivilege", None)
    if model is None:
        return []
    rows = session.scalars(select(model).where(
        model.staff_id == staff_id)).all()
    return [r for r in rows
            if getattr(r, "status", "active") in (None, "active")]


def _orientation_state(session: Session, record) -> dict:
    models = _models()
    completions = {
        row.item_code: row
        for row in session.scalars(
            select(models.OrientationCompletion).where(
                models.OrientationCompletion.onboarding_id
                == record.onboarding_id)).all()}
    items = []
    for item in _orientation_items(session):
        done = completions.get(item["linkId"])
        items.append({
            "linkId": item["linkId"], "text": item["text"],
            "required": item["required"],
            "completed": done is not None,
            "completed_by": done.completed_by if done else None,
            "completed_at": (
                done.completed_at.isoformat()
                if done is not None
                and isinstance(done.completed_at, datetime)
                else (done.completed_at if done else None)),
        })
    return {"form_key": ORIENTATION_FORM_KEY, "items": items}


# ---------------------------------------------------------------------------
# Blockers — computed for the CURRENT stage, never stored
# ---------------------------------------------------------------------------

def _credential_problems(session: Session, staff_id: str,
                         required_types: list[str]) -> list[str]:
    """Missing / expired problems for the required credential types,
    reading the EXISTING StaffCredential store: no credential of the
    type on file -> missing; the LATEST one expired -> expired."""
    problems: list[str] = []
    rows = session.scalars(select(StaffCredential).where(
        StaffCredential.staff_id == staff_id)).all()
    by_type: dict[str, list[StaffCredential]] = {}
    for row in rows:
        by_type.setdefault(row.type, []).append(row)
    today = _today()
    for cred_type in required_types:
        held = by_type.get(cred_type) or []
        if not held:
            problems.append(f"missing credential: {cred_type}")
            continue
        latest = max(held, key=lambda r: r.expires_on or "")
        try:
            expires = date.fromisoformat(latest.expires_on)
        except (ValueError, TypeError):
            continue
        if expires < today:
            problems.append(
                f"{cred_type} expired on {latest.expires_on}")
    return problems


def _placement_blockers(session: Session,
                        staff: StaffMember | None) -> list[str]:
    """Organisation & Registration Placement pass: a record whose
    staff placement is incomplete (any of region / location /
    department / unit unset) or no longer validates against the
    published hierarchy cannot advance — the blocker names what is
    missing or the resolver's broken-link detail. Placement is fixed
    by a staff edit (PUT /people/staff/{id}) carrying a valid
    placement. While app.org_hierarchy is unavailable, placement
    gating is inactive (legacy behaviour)."""
    if staff is None or people._org_hierarchy is None:
        return []
    placement = people._placement_of(staff)
    blockers = [
        f"Organisational placement incomplete: missing {level}"
        for level in people.PLACEMENT_LEVELS
        if not placement.get(level)]
    if blockers:
        return blockers
    try:
        people._validate_placement(session, placement, require_all=True)
    except HTTPException as exc:
        return [f"Organisational placement invalid: {exc.detail}"]
    return []


def _blockers(session: Session, record, staff: StaffMember,
              entry: dict) -> list[str]:
    stage = record.stage
    if stage == "credentialing":
        return _placement_blockers(session, staff) \
            + _credential_problems(session, record.staff_id,
                                   _required_credentials(entry))
    if stage == "privileging":
        # Credential problems only: an ungranted template privilege is
        # NOT a blocker (advancing grants it), but a template whose
        # credential basis has failed must not sail through.
        return _placement_blockers(session, staff) \
            + _credential_problems(session, record.staff_id,
                                   _required_credentials(entry))
    if stage == "account":
        # Placement gates provisioning too (credential problems were
        # settled at credentialing/privileging; an already-provisioned
        # account still satisfies the stage once placement is fixed).
        return _placement_blockers(session, staff)
    if stage == "orientation":
        state = _orientation_state(session, record)
        return _placement_blockers(session, staff) \
            + [f"orientation item incomplete: {item['text']}"
               for item in state["items"]
               if item["required"] and not item["completed"]]
    # active/terminated have no gates.
    return []


def _detail(session: Session, record) -> dict:
    staff = people._get_staff(session, record.staff_id)
    ops = people._ops(session)
    names = people._staff_names(session)
    credentials = session.scalars(
        select(StaffCredential).where(
            StaffCredential.staff_id == record.staff_id)
        .order_by(StaffCredential.expires_on.asc())).all()
    account = _find_account(session, record, staff)
    entry = _job_role_entry(session, staff.job_role)
    return {
        "record": _record_out(record),
        "staff": people._staff_out(staff),
        "placement": people._placement_view(
            session, people._placement_of(staff)),
        "credentials": [people._credential_out(c, ops, names)
                        for c in credentials],
        "privileges": [_privilege_out(g)
                       for g in _active_grants(session, record.staff_id)],
        "account": (_account_out_brief(account)
                    if account is not None else None),
        "orientation": _orientation_state(session, record),
        "blockers": _blockers(session, record, staff, entry),
        "stages": STAGES,
    }


# ---------------------------------------------------------------------------
# Helpers mirroring app.people / app.accounts validation
# ---------------------------------------------------------------------------

async def _json_body(request: Request, required: bool = True) -> dict:
    try:
        body = await request.json()
    except Exception:
        if not required:
            return {}
        raise HTTPException(status_code=422,
                            detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        if not required:
            return {}
        raise HTTPException(status_code=422,
                            detail="Request body must be a JSON object")
    return body


def _username_taken(session: Session, username: str,
                    exclude_staff_id: str | None = None) -> bool:
    staff = session.scalars(select(StaffMember).where(
        StaffMember.username == username)).first()
    if staff is not None and staff.staff_id != exclude_staff_id:
        return True
    return session.scalars(select(UserAccount).where(
        UserAccount.username == username)).first() is not None


def _slug_for(session: Session, full_name: str) -> str:
    """Generated username: lowercase first-initial + last name token,
    non-alphanumerics stripped; collisions get -2, -3, ..."""
    base_name = (full_name or "").split(",", 1)[0].strip()
    tokens = [t for t in re.split(r"\s+", base_name) if t]
    if not tokens:
        slug = "staff"
    elif len(tokens) == 1:
        slug = tokens[0]
    else:
        slug = tokens[0][0] + tokens[-1]
    slug = re.sub(r"[^a-z0-9]", "", slug.lower()) or "staff"
    candidate = slug
    suffix = 2
    while _username_taken(session, candidate):
        candidate = f"{slug}-{suffix}"
        suffix += 1
    return candidate


def _unique_username(session: Session, requested: str) -> str:
    candidate = requested
    suffix = 2
    while _username_taken(session, candidate):
        candidate = f"{requested}-{suffix}"
        suffix += 1
    return candidate


def _refuse_advance(session: Session, user: User, record,
                    stage: str, blockers: list[str], detail: str):
    append_audit(session, actor=user.username,
                 action="onboarding.advance.refused",
                 entity=f"onboarding:{record.onboarding_id}",
                 payload={"stage": stage, "blockers": blockers})
    session.commit()
    raise HTTPException(status_code=409, detail=detail)


# ---------------------------------------------------------------------------
# Create (the hr-profile stage) + reads
# ---------------------------------------------------------------------------

@router.post("", status_code=201)
@router.post("/", status_code=201, include_in_schema=False)
async def create_onboarding(request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(
                                require_capability("workforce.onboard"))):
    body = await _json_body(request)
    ops = people._ops(session)
    full_name = people._require_str(body, "full_name")
    job_role = people._require_str(body, "job_role")
    if job_role not in people._config_codes(ops, "concepts"):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown job role '{job_role}': not in the published "
                   "workforce config role set")
    unit = body.get("unit")
    if unit is not None:
        known_units = people._config_codes(ops, "units") | \
            people._config_codes(ops, "serviceLines")
        if unit not in known_units:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown unit '{unit}': not in the published "
                       "workforce config")
    service_line = body.get("service_line")
    if service_line is not None and \
            service_line not in people._config_codes(ops, "serviceLines"):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown service line '{service_line}': not in the "
                   "published workforce config")
    supervisor_staff_id = body.get("supervisor_staff_id")
    if supervisor_staff_id is not None:
        if not isinstance(supervisor_staff_id, str) \
                or session.scalars(select(StaffMember).where(
                    StaffMember.staff_id == supervisor_staff_id)).first() \
                is None:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown supervisor_staff_id "
                       f"'{supervisor_staff_id}': no such staff-directory "
                       "entry")
    fte = body.get("fte")
    if fte is not None:
        if isinstance(fte, bool) or not isinstance(fte, (int, float)) \
                or not (0 < float(fte) <= 1):
            raise HTTPException(
                status_code=422,
                detail="'fte' must be a number greater than 0 and at "
                       "most 1")
        fte = float(fte)
    start_date = body.get("start_date")
    if start_date is not None:
        start_date = vf.check_iso_date(start_date, "start_date")
    employee_id = body.get("employee_id")
    if employee_id is not None and not isinstance(employee_id, str):
        raise HTTPException(status_code=422,
                            detail="'employee_id' must be a string")
    requested_username = body.get("username")
    if requested_username is not None:
        if not isinstance(requested_username, str) \
                or not requested_username.strip() \
                or any(ch.isspace() for ch in requested_username):
            raise HTTPException(
                status_code=422,
                detail="'username' must be a single token (no spaces)")
        username = _unique_username(session, requested_username.strip())
    else:
        username = _slug_for(session, full_name)

    # Organisation & Registration Placement pass: registration
    # REQUIRES a full placement (region, location, department, unit)
    # validated against the published hierarchy BEFORE anything is
    # written — a missing level, an unknown code or a broken chain
    # is a 422 carrying the resolver's detail verbatim. (The legacy
    # workforce-config unit check above must ALSO pass.)
    placement = None
    if people._org_hierarchy is not None:
        placement = people._validate_placement(
            session,
            {"region": body.get("region"),
             "location": body.get("location"),
             "department": body.get("department"),
             "unit": unit},
            require_all=True)
    placement_kwargs = {}
    if placement is not None:
        placement_kwargs = {
            level: placement[level]
            for level in ("region", "location", "department")
            if hasattr(StaffMember, level)}

    staff_id = f"staff-{uuid.uuid4().hex[:8]}"
    staff = StaffMember(
        staff_id=staff_id, username=username, full_name=full_name,
        job_role=job_role,
        unit=placement["unit"] if placement is not None else unit,
        service_line=service_line,
        status="inactive",
        audience=people.JOB_ROLE_AUDIENCE.get(job_role),
        email=body.get("email") or "", phone=body.get("phone") or "",
        **placement_kwargs)
    session.add(staff)
    session.flush()
    models = _models()
    record = models.OnboardingRecord(
        onboarding_id=f"onb-{uuid.uuid4().hex[:8]}", staff_id=staff_id,
        employee_id=employee_id or "", stage="credentialing",
        supervisor_staff_id=supervisor_staff_id, fte=fte,
        start_date=start_date, account_username=username,
        created_by=user.username)
    session.add(record)
    session.flush()
    append_audit(session, actor=user.username, action="onboarding.create",
                 entity=f"staff:{staff_id}",
                 payload={"job_role": job_role,
                          "employee_id": employee_id,
                          **({"placement": placement}
                             if placement is not None else {})})
    session.commit()
    return _detail(session, record)


@router.get("")
@router.get("/", include_in_schema=False)
def list_onboarding(stage: str | None = Query(default=None),
                    job_role: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(
                        require_capability("workforce.read"))):
    models = _models()
    stmt = select(models.OnboardingRecord).order_by(
        models.OnboardingRecord.created_at.asc(),
        models.OnboardingRecord.id.asc())
    if stage:
        stmt = stmt.where(models.OnboardingRecord.stage == stage)
    rows = session.scalars(stmt).all()
    items = []
    staff_map = {s.staff_id: s for s in
                 session.scalars(select(StaffMember)).all()}
    for record in rows:
        staff = staff_map.get(record.staff_id)
        if job_role and (staff is None or staff.job_role != job_role):
            continue
        entry = _job_role_entry(session, staff.job_role) if staff else {}
        blockers = _blockers(session, record, staff, entry) if staff else []
        placement = (people._placement_of(staff) if staff is not None
                     else {level: None
                           for level in people.PLACEMENT_LEVELS})
        items.append({**_record_out(record),
                      "staff_name": staff.full_name if staff else None,
                      "job_role": staff.job_role if staff else None,
                      "region": placement["region"],
                      "location": placement["location"],
                      "department": placement["department"],
                      "unit": staff.unit if staff else None,
                      "blockers_count": len(blockers)})
    return {"items": items, "total": len(items)}


@router.get("/{onboarding_id}")
def get_onboarding(onboarding_id: str,
                   session: Session = Depends(get_db),
                   user: User = Depends(
                       require_capability("workforce.read"))):
    return _detail(session, _get_record(session, onboarding_id))


# ---------------------------------------------------------------------------
# Credentials during onboarding (the EXISTING StaffCredential store)
# ---------------------------------------------------------------------------

@router.post("/{onboarding_id}/credentials", status_code=201)
async def add_credential(onboarding_id: str, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(
                             require_capability("workforce.onboard"))):
    record = _get_record(session, onboarding_id)
    body = await _json_body(request)
    ops = people._ops(session)
    cred_type = people._require_str(body, "type")
    if cred_type not in people._config_codes(ops, "credentialTypes"):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown credential type '{cred_type}': not in the "
                   "published workforce config")
    number = people._require_str(body, "number")
    expires_on = people._require_str(body, "expires_on")
    if people._days_to_expiry(expires_on) is None:
        raise HTTPException(status_code=422,
                            detail="'expires_on' must be an ISO date "
                                   "(YYYY-MM-DD)")
    issued_on = body.get("issued_on")
    if issued_on is not None and people._days_to_expiry(issued_on) is None:
        raise HTTPException(status_code=422,
                            detail="'issued_on' must be an ISO date "
                                   "(YYYY-MM-DD)")
    credential_id = f"cred-{uuid.uuid4().hex[:8]}"
    row = StaffCredential(credential_id=credential_id,
                          staff_id=record.staff_id, type=cred_type,
                          number=number, issued_on=issued_on,
                          expires_on=expires_on)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="workforce.credential.create",
                 entity=f"staff:{record.staff_id}",
                 payload={"credential_id": credential_id, "type": cred_type,
                          "expires_on": expires_on})
    session.commit()
    return people._credential_out(row, ops, people._staff_names(session))


# ---------------------------------------------------------------------------
# Advance — one stage at a time, gated per stage
# ---------------------------------------------------------------------------

def _grant_template(session: Session, user: User, record,
                    staff: StaffMember, entry: dict) -> None:
    template = _privilege_template(entry)
    if not template:
        return
    granted = {g.privilege_code for g in
               _active_grants(session, record.staff_id)}
    priv = _privileging()
    for code in template:
        if code in granted:
            continue
        if priv is not None:
            priv.grant_privilege(session, user.username, record.staff_id,
                                 code, service_line=staff.service_line,
                                 note="onboarding privileging stage")
        else:
            _insert_privilege_fallback(session, user, record, staff, code)
        granted.add(code)


def _insert_privilege_fallback(session: Session, user: User, record,
                               staff: StaffMember, code: str) -> None:
    """Direct grant insert used ONLY when app.privileging is not yet
    importable; kwargs are filtered to the model's real columns."""
    model = getattr(_models(), "StaffPrivilege", None)
    if model is None:
        raise HTTPException(
            status_code=503,
            detail="Privileging is not available in this deployment")
    columns = {c.name for c in model.__table__.columns}
    values = {"grant_id": f"grant-{uuid.uuid4().hex[:8]}",
              "staff_id": record.staff_id, "privilege_code": code,
              "service_line": staff.service_line,
              "granted_by": user.username, "granted_at": _now(),
              "status": "active", "note": "onboarding privileging stage",
              "created_by": user.username}
    session.add(model(**{k: v for k, v in values.items() if k in columns}))
    session.flush()


def _provision_account(session: Session, user: User, record,
                       staff: StaffMember, entry: dict) -> dict:
    """Provision the sign-in account (app.accounts semantics). Returns
    the one-time extras for the advance response ({} when no token was
    minted, e.g. OIDC mode)."""
    role_raw = entry.get("defaultAccountRole") or "viewer"
    try:
        role = Role(str(role_raw))
    except ValueError:
        role = Role.viewer
    username = record.account_username or staff.username \
        or _slug_for(session, staff.full_name)
    dev_token = None
    token_hash_value = None
    if auth_mode() != "oidc":
        dev_token = f"acct-{username}-{secrets.token_hex(8)}"
        token_hash_value = token_hash(dev_token)
    # The provisioned account COPIES the staff record's placement
    # (region / location / department alongside the unit).
    placement = people._placement_of(staff)
    placement_kwargs = {
        level: placement[level]
        for level in ("region", "location", "department")
        if hasattr(UserAccount, level)}
    account = UserAccount(
        username=username, full_name=staff.full_name, role=role,
        unit=staff.unit, service_line=staff.service_line,
        email=staff.email or "", phone=staff.phone or "",
        staff_id=staff.staff_id, patient_ref=None, status="active",
        dev_token_hash=token_hash_value,
        created_by=user.username, updated_by=user.username,
        **placement_kwargs)
    session.add(account)
    session.flush()
    if not staff.username:
        staff.username = username
    record.account_username = username
    append_audit(session, actor=user.username, action="accounts.create",
                 entity=f"account:{username}",
                 payload={"role": role.value, "unit": staff.unit,
                          "service_line": staff.service_line,
                          "staff_id": staff.staff_id,
                          "placement": placement,
                          "dev_token_issued": dev_token is not None,
                          "via": "onboarding",
                          "onboarding_id": record.onboarding_id})
    if dev_token is not None:
        return {"dev_token": dev_token, "dev_token_note": _DEV_TOKEN_NOTE}
    return {}


@router.post("/{onboarding_id}/advance")
def advance_onboarding(onboarding_id: str,
                       session: Session = Depends(get_db),
                       user: User = Depends(
                           require_capability("workforce.onboard"))):
    record = _get_record(session, onboarding_id)
    staff = people._get_staff(session, record.staff_id)
    entry = _job_role_entry(session, staff.job_role)
    stage = record.stage
    if stage in ("active", TERMINAL_STAGE):
        raise HTTPException(
            status_code=409,
            detail=f"Onboarding is '{stage}' — there is no next stage")
    extras: dict = {}
    if stage == "credentialing":
        blockers = _blockers(session, record, staff, entry)
        if blockers:
            _refuse_advance(
                session, user, record, stage, blockers,
                "Credentialing incomplete: " + "; ".join(blockers))
        record.stage = "privileging"
    elif stage == "privileging":
        blockers = _blockers(session, record, staff, entry)
        if blockers:
            _refuse_advance(
                session, user, record, stage, blockers,
                "Privileging blocked: " + "; ".join(blockers))
        _grant_template(session, user, record, staff, entry)
        record.stage = "account"
    elif stage == "account":
        blockers = _blockers(session, record, staff, entry)
        if blockers:
            _refuse_advance(
                session, user, record, stage, blockers,
                "Account provisioning blocked: " + "; ".join(blockers))
        account = _find_account(session, record, staff)
        if account is None:
            username = record.account_username or staff.username
            if username:
                clash = session.scalars(select(UserAccount).where(
                    UserAccount.username == username)).first()
                if clash is not None and clash.staff_id != record.staff_id:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Username '{username}' already belongs "
                               "to a different account; resolve the "
                               "collision before provisioning")
            extras = _provision_account(session, user, record, staff, entry)
        record.stage = "orientation"
    elif stage == "orientation":
        blockers = _blockers(session, record, staff, entry)
        if blockers:
            _refuse_advance(
                session, user, record, stage, blockers,
                "Orientation incomplete: " + "; ".join(blockers))
        record.stage = "active"
        staff.status = "active"
    else:  # a stage this module does not drive (e.g. hr-profile rows)
        raise HTTPException(status_code=409,
                            detail=f"Cannot advance from stage '{stage}'")
    session.flush()
    append_audit(session, actor=user.username, action="onboarding.advance",
                 entity=f"onboarding:{record.onboarding_id}",
                 payload={"from_stage": stage, "to_stage": record.stage})
    session.commit()
    out = _detail(session, record)
    out.update(extras)
    return out


# ---------------------------------------------------------------------------
# Orientation item completion
# ---------------------------------------------------------------------------

@router.post("/{onboarding_id}/orientation/{item_code}/complete")
async def complete_orientation_item(onboarding_id: str, item_code: str,
                                    request: Request,
                                    session: Session = Depends(get_db),
                                    user: User = Depends(require_capability(
                                        "workforce.onboard"))):
    record = _get_record(session, onboarding_id)
    body = await _json_body(request, required=False)
    known = {item["linkId"] for item in _orientation_items(session)}
    if item_code not in known:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown orientation item '{item_code}': not in the "
                   "published orientation checklist")
    models = _models()
    existing = session.scalars(
        select(models.OrientationCompletion).where(
            models.OrientationCompletion.onboarding_id
            == record.onboarding_id,
            models.OrientationCompletion.item_code == item_code)).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Orientation item '{item_code}' is already completed")
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise HTTPException(status_code=422,
                            detail="'note' must be a string")
    session.add(models.OrientationCompletion(
        completion_id=f"orient-{uuid.uuid4().hex[:8]}",
        onboarding_id=record.onboarding_id, item_code=item_code,
        completed_by=user.username, completed_at=_now(), note=note or ""))
    session.flush()
    append_audit(session, actor=user.username,
                 action="onboarding.orientation.complete",
                 entity=f"onboarding:{record.onboarding_id}",
                 payload={"item_code": item_code})
    session.commit()
    return _detail(session, record)


# ---------------------------------------------------------------------------
# Terminate (offboarding)
# ---------------------------------------------------------------------------

def _end_rows(session: Session, model_name: str, staff_id: str,
              reason: str, staff_column: str = "staff_id") -> int:
    """End every ACTIVE row of a sibling-owned model that names this
    staff member (team memberships / empanelments). Column-filtered so
    it binds to the sibling models at call time."""
    model = getattr(_models(), model_name, None)
    if model is None or not hasattr(model, staff_column):
        return 0
    rows = session.scalars(select(model).where(
        getattr(model, staff_column) == staff_id)).all()
    ended = 0
    for row in rows:
        if getattr(row, "status", "active") != "active":
            continue
        row.status = "ended"
        if hasattr(row, "ended_reason"):
            row.ended_reason = reason
        if hasattr(row, "ended_at"):
            row.ended_at = _now()
        ended += 1
    return ended


@router.post("/{onboarding_id}/terminate")
async def terminate_onboarding(onboarding_id: str, request: Request,
                               session: Session = Depends(get_db),
                               user: User = Depends(require_capability(
                                   "workforce.onboard"))):
    record = _get_record(session, onboarding_id)
    body = await _json_body(request)
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise HTTPException(
            status_code=422,
            detail="'reason' is required and must be a non-empty string")
    if record.stage == TERMINAL_STAGE:
        raise HTTPException(status_code=409,
                            detail="Onboarding is already terminated")
    staff = people._get_staff(session, record.staff_id)
    from_stage = record.stage
    record.stage = TERMINAL_STAGE
    record.terminated_at = _now()
    record.termination_reason = reason.strip()
    staff.status = "inactive"
    account = _find_account(session, record, staff)
    memberships_ended = _end_rows(session, "TeamMembership",
                                  record.staff_id,
                                  "employment terminated")
    empanelments_ended = _end_rows(session, "Empanelment",
                                   record.staff_id,
                                   "pcp terminated — patient unempanelled",
                                   staff_column="pcp_staff_id")
    session.flush()
    if account is not None and account.status == "active":
        account.status = "disabled"
        account.updated_by = user.username
        session.flush()
        append_audit(session, actor=user.username,
                     action="accounts.disable",
                     entity=f"account:{account.username}",
                     payload={"status": "disabled"})
    append_audit(session, actor=user.username,
                 action="onboarding.terminate",
                 entity=f"onboarding:{record.onboarding_id}",
                 payload={"from_stage": from_stage, "reason": reason.strip(),
                          "account_disabled": bool(
                              account is not None
                              and account.status == "disabled"),
                          "memberships_ended": memberships_ended,
                          "empanelments_ended": empanelments_ended})
    session.commit()
    return _detail(session, record)


# ---------------------------------------------------------------------------
# Seeds: the orientation form + two records over EXISTING seeded staff
# (no new StaffMember rows — seeded staff totals are asserted elsewhere)
# ---------------------------------------------------------------------------

_SEED_RECORDS = [
    # staff_id, stage, account_username, employee_id, completed items
    ("staff-001", "active", "dev-clinician", "EMP-1001",
     [code for code, _text in ORIENTATION_ITEMS]),
    ("staff-019", "orientation", "dev-frontdesk", "EMP-1019",
     ["verify-identity", "hipaa-training"]),
]


def ensure_onboarding_defaults(session: Session) -> None:
    """Seed the orientation checklist form (published v1) and one
    onboarding record per seeded exemplar, idempotent by staff_id."""
    payload = default_orientation_form_payload()
    validate_form(payload)  # the seed must satisfy the form validator
    _seed_published(session, "form", ORIENTATION_FORM_KEY, payload)
    models = _models()
    created = 0
    for staff_id, stage, account_username, employee_id, items \
            in _SEED_RECORDS:
        staff = session.scalars(select(StaffMember).where(
            StaffMember.staff_id == staff_id)).first()
        if staff is None:
            continue
        existing = session.scalars(select(models.OnboardingRecord).where(
            models.OnboardingRecord.staff_id == staff_id)).first()
        if existing is not None:
            continue
        record = models.OnboardingRecord(
            onboarding_id=f"onb-seed-{staff_id}", staff_id=staff_id,
            employee_id=employee_id or "", stage=stage,
            supervisor_staff_id=None, fte=None, start_date=None,
            account_username=account_username, created_by="system")
        session.add(record)
        session.flush()
        for item_code in items:
            session.add(models.OrientationCompletion(
                completion_id=f"orient-seed-{staff_id}-{item_code}",
                onboarding_id=record.onboarding_id, item_code=item_code,
                completed_by="system", completed_at=_now(), note=""))
        session.flush()
        created += 1
    if created:
        append_audit(session, actor="system", action="onboarding.seed",
                     entity="onboarding",
                     payload={"created": created, "synthetic": True})
    session.commit()
