"""User accounts & registration — Front Door pass (app.accounts).

The managed identity layer the sign-in boundary resolves against:

- **Accounts are real records** (models.UserAccount): unique
  username, full name, sign-in role, unit / service-line assignment,
  contact email/phone, an optional link to the person's
  staff-directory entry, and an active/disabled status. Create,
  update, disable and enable are admin-only (`accounts.manage`) and
  every mutation is hash-chain audited with the changed FIELD NAMES
  (never credential material).
- **Passwords are NOT stored and NOT faked.** Production sign-in
  credentials live in the identity provider (AUTH_MODE=oidc —
  Keycloak; see app.security). Accounts govern identity, role, unit
  and status in both modes.
- **Dev tokens (AUTH_MODE=dev only):** the built-in dev users are
  seeded as accounts whose dev_token_hash is the SHA-256 of the
  well-known documented token (admin-token, clinician-token, ...),
  so existing dev sign-ins keep working byte-identically — but now
  through the account record, which means disabling the account
  kills the token (security.get_current_user checks the account
  first and refuses disabled accounts with 401). An account created
  through this API gets a generated dev token returned ONCE, in the
  creation response only; only its hash is stored. In OIDC mode no
  token is generated or returned — the identity provider owns
  credentials.
- **The staff link is live:** setting an account's staff_id also
  writes the account username onto the directory entry when it has
  none, because that link is what the role-aware home resolution
  (app.people.resolve_audience) reads to find the person's unit and
  audience.

Validation follows the system quality bar (app.validate_fields):
role must be a real Role, unit/service line must exist in the
published workforce config, email/phone are format-checked, and
every failure is a 422 naming the field.
"""
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import people
from . import validate_fields as vf
from .audit import append_audit
from .configstore import published_config
from .models import ComplianceRequirement, Role, StaffMember, User, UserAccount
from .security import auth_mode, dev_tokens, get_db, require_capability, token_hash

router = APIRouter(prefix="/accounts", tags=["accounts"])

ACCOUNT_STATUSES = ("active", "disabled")
EDITABLE_FIELDS = ("full_name", "role", "unit", "service_line",
                   "email", "phone", "staff_id", "patient_ref",
                   "region", "location", "department")
PLACEMENT_FIELDS = ("region", "location", "department", "unit")


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422,
                            detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422,
                            detail="Request body must be a JSON object")
    return body


def _workforce_codes(session: Session) -> tuple[set[str], set[str]]:
    cfg = published_config(session, "terminology", "workforce")
    payload = (cfg.payload or {}) if cfg is not None else {}
    units = {str(e["code"]) for e in payload.get("units") or []
             if isinstance(e, dict) and e.get("code")}
    lines = {str(e["code"]) for e in payload.get("serviceLines") or []
             if isinstance(e, dict) and e.get("code")}
    return units, lines


def _check_assignment(session: Session, unit: str | None,
                      service_line: str | None) -> None:
    units, lines = _workforce_codes(session)
    if unit is not None and unit not in units | lines:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown unit '{unit}': not in the published "
                   "workforce config")
    if service_line is not None and service_line not in lines:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown service line '{service_line}': not in the "
                   "published workforce config")


def _check_role(raw) -> Role:
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=422,
                            detail="'role' is required (one of: "
                                   + ", ".join(r.value for r in Role) + ")")
    try:
        return Role(raw.strip())
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown role '{raw}': must be one of "
                   + ", ".join(r.value for r in Role))


def _link_staff(session: Session, account: UserAccount) -> None:
    """Write the account username onto the linked directory entry
    when it has none — the link the home resolution reads."""
    if not account.staff_id:
        return
    staff = session.scalars(select(StaffMember).where(
        StaffMember.staff_id == account.staff_id)).first()
    if staff is not None and not staff.username:
        staff.username = account.username


def _account_out(row: UserAccount, session: Session | None = None) -> dict:
    # Organisation placement: codes + displays via
    # org_hierarchy.placement_view (None-safe when unplaced).
    placement = people._placement_of(row)
    if session is not None:
        placement_view = people._placement_view(session, placement)
    else:
        placement_view = {
            level: ({"code": code, "display": code} if code else None)
            for level, code in placement.items()}
    return {"account_id": row.id, "username": row.username,
            "full_name": row.full_name, "role": row.role.value,
            "unit": row.unit, "service_line": row.service_line,
            "placement": placement_view,
            "email": row.email, "phone": row.phone,
            "staff_id": row.staff_id, "patient_ref": row.patient_ref,
            "status": row.status,
            "has_dev_token": row.dev_token_hash is not None,
            "created_by": row.created_by, "updated_by": row.updated_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def _get_account(session: Session, account_id: int) -> UserAccount:
    row = session.get(UserAccount, account_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Account {account_id} not found")
    return row


# ---------------------------------------------------------------------------
# CRUD + lifecycle
# ---------------------------------------------------------------------------

@router.get("")
def list_accounts(status: str | None = Query(default=None),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability("accounts.manage"))):
    stmt = select(UserAccount).order_by(UserAccount.username.asc())
    if status:
        if status not in ACCOUNT_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=f"'status' must be one of {list(ACCOUNT_STATUSES)}")
        stmt = stmt.where(UserAccount.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_account_out(r, session) for r in rows],
            "total": len(rows)}


@router.post("", status_code=201)
async def create_account(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(require_capability("accounts.manage"))):
    body = await _json_body(request)
    username = vf.require_str(body, "username")
    if len(username) > 120 or any(ch.isspace() for ch in username):
        raise HTTPException(
            status_code=422,
            detail="'username' must be a single token of at most 120 "
                   "characters (no spaces)")
    if session.scalars(select(UserAccount).where(
            UserAccount.username == username)).first() is not None:
        raise HTTPException(status_code=409,
                            detail=f"Username '{username}' is already taken")
    full_name = vf.require_str(body, "full_name")
    role = _check_role(body.get("role"))
    unit = vf.optional_str(body, "unit")
    service_line = vf.optional_str(body, "service_line")
    _check_assignment(session, unit, service_line)
    email = vf.check_email(vf.optional_str(body, "email")) or ""
    phone = vf.check_phone(vf.optional_str(body, "phone")) or ""
    staff_id = vf.optional_str(body, "staff_id")
    staff_row = None
    if staff_id is not None:
        staff_row = session.scalars(select(StaffMember).where(
            StaffMember.staff_id == staff_id)).first()
        if staff_row is None:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown staff_id '{staff_id}': no such staff-directory entry")
    # Organisation placement: the body may carry region / location /
    # department / unit. Provided placement is validated against the
    # published hierarchy (partial-safe) -> 422 naming the broken
    # link. With a staff link and NO placement fields provided, the
    # account inherits the staff record's placement wholesale.
    placement_provided = any(body.get(field) is not None
                             for field in PLACEMENT_FIELDS)
    placement = {"region": vf.optional_str(body, "region"),
                 "location": vf.optional_str(body, "location"),
                 "department": vf.optional_str(body, "department"),
                 "unit": unit}
    if staff_row is not None and not placement_provided:
        placement = people._placement_of(staff_row)
        unit = placement["unit"]
    elif placement_provided:
        placement = people._validate_placement(session, placement,
                                               require_all=False)
        unit = placement["unit"]
    placement_kwargs = {
        level: placement[level]
        for level in ("region", "location", "department")
        if hasattr(UserAccount, level)}
    patient_ref = vf.optional_str(body, "patient_ref")
    if role == Role.patient and not patient_ref:
        raise HTTPException(
            status_code=422,
            detail="A patient account requires 'patient_ref' "
                   "(e.g. 'Patient/pat-1') so portal reads can be scoped")
    dev_token = None
    token_hash_value = None
    if auth_mode() != "oidc":
        dev_token = f"acct-{username}-{secrets.token_hex(8)}"
        token_hash_value = token_hash(dev_token)
    row = UserAccount(
        username=username, full_name=full_name, role=role, unit=unit,
        service_line=service_line, email=email, phone=phone,
        staff_id=staff_id, patient_ref=patient_ref, status="active",
        dev_token_hash=token_hash_value,
        created_by=user.username, updated_by=user.username,
        **placement_kwargs)
    session.add(row)
    session.flush()
    _link_staff(session, row)
    append_audit(session, actor=user.username, action="accounts.create",
                 entity=f"account:{username}",
                 payload={"role": role.value, "unit": unit,
                          "service_line": service_line,
                          "staff_id": staff_id,
                          "placement": placement,
                          "dev_token_issued": dev_token is not None})
    session.commit()
    out = _account_out(row, session)
    if dev_token is not None:
        # Returned exactly once. Only the hash is stored; in OIDC
        # mode no token exists at all (the IdP owns credentials).
        out["dev_token"] = dev_token
        out["dev_token_note"] = (
            "Dev-mode sign-in token — shown once, store it now. "
            "Production sign-in is the identity provider (OIDC).")
    return out


@router.get("/{account_id}")
def get_account(account_id: int, session: Session = Depends(get_db),
                user: User = Depends(require_capability("accounts.manage"))):
    return _account_out(_get_account(session, account_id), session)


@router.put("/{account_id}")
async def update_account(account_id: int, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("accounts.manage"))):
    row = _get_account(session, account_id)
    body = await _json_body(request)
    unknown = set(body) - set(EDITABLE_FIELDS) - {"username"}
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown account field(s): {', '.join(sorted(unknown))}")
    if "username" in body and body["username"] != row.username:
        raise HTTPException(
            status_code=422,
            detail="'username' is the account's identity and cannot be "
                   "changed; disable this account and create a new one")
    changed: list[str] = []
    # Organisation placement: if any placement field is present, the
    # EFFECTIVE placement (stored values overridden by the edit) is
    # revalidated against the published hierarchy BEFORE anything is
    # applied -> a broken chain is a 422 naming the link.
    placement_effective: dict | None = None
    if any(field in body for field in PLACEMENT_FIELDS):
        placement_effective = people._placement_of(row)
        for field in PLACEMENT_FIELDS:
            if field in body:
                placement_effective[field] = vf.optional_str(body, field)
        placement_effective = people._validate_placement(
            session, placement_effective, require_all=False)
    if "full_name" in body:
        value = vf.require_str(body, "full_name")
        if value != row.full_name:
            row.full_name = value
            changed.append("full_name")
    if "role" in body:
        value = _check_role(body.get("role"))
        if value != row.role:
            row.role = value
            changed.append("role")
    if "unit" in body or "service_line" in body:
        new_unit = vf.optional_str(body, "unit") if "unit" in body else row.unit
        new_line = (vf.optional_str(body, "service_line")
                    if "service_line" in body else row.service_line)
        _check_assignment(session, new_unit, new_line)
        if "unit" in body and new_unit != row.unit:
            row.unit = new_unit
            changed.append("unit")
        if "service_line" in body and new_line != row.service_line:
            row.service_line = new_line
            changed.append("service_line")
    if placement_effective is not None:
        for level in ("region", "location", "department"):
            if level in body and hasattr(row, level) \
                    and placement_effective[level] != getattr(row, level):
                setattr(row, level, placement_effective[level])
                changed.append(level)
    if "email" in body:
        value = vf.check_email(vf.optional_str(body, "email")) or ""
        if value != row.email:
            row.email = value
            changed.append("email")
    if "phone" in body:
        value = vf.check_phone(vf.optional_str(body, "phone")) or ""
        if value != row.phone:
            row.phone = value
            changed.append("phone")
    if "staff_id" in body:
        value = vf.optional_str(body, "staff_id")
        if value is not None and session.scalars(select(StaffMember).where(
                StaffMember.staff_id == value)).first() is None:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown staff_id '{value}': no such "
                       "staff-directory entry")
        if value != row.staff_id:
            row.staff_id = value
            changed.append("staff_id")
    if "patient_ref" in body:
        value = vf.optional_str(body, "patient_ref")
        if value != row.patient_ref:
            row.patient_ref = value
            changed.append("patient_ref")
    if row.role == Role.patient and not row.patient_ref:
        raise HTTPException(
            status_code=422,
            detail="A patient account requires 'patient_ref' so portal "
                   "reads can be scoped")
    row.updated_by = user.username
    session.flush()
    _link_staff(session, row)
    append_audit(session, actor=user.username, action="accounts.update",
                 entity=f"account:{row.username}",
                 payload={"changed_fields": changed})
    session.commit()
    return _account_out(row, session)


def _set_status(session: Session, user: User, account_id: int,
                target: str) -> dict:
    row = _get_account(session, account_id)
    if row.status == target:
        raise HTTPException(
            status_code=409,
            detail=f"Account '{row.username}' is already {target}")
    row.status = target
    row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username,
                 action="accounts.enable" if target == "active"
                 else "accounts.disable",
                 entity=f"account:{row.username}",
                 payload={"status": target})
    session.commit()
    return _account_out(row, session)


@router.post("/{account_id}/disable")
def disable_account(account_id: int, session: Session = Depends(get_db),
                    user: User = Depends(require_capability("accounts.manage"))):
    """Disabling is immediate: the account's dev token stops
    authenticating at the next request (security resolves accounts
    first and refuses disabled ones)."""
    return _set_status(session, user, account_id, "disabled")


@router.post("/{account_id}/enable")
def enable_account(account_id: int, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("accounts.manage"))):
    return _set_status(session, user, account_id, "active")


# ---------------------------------------------------------------------------
# Seeds: the built-in dev accounts + the front-desk account, and the
# HIPAA unique-user-identification register entry
# ---------------------------------------------------------------------------

_FRONTDESK_USERNAME = "dev-frontdesk"
_FRONTDESK_TOKEN = "frontdesk-token"


def _seed_accounts(session: Session) -> None:
    """One account per effective dev token (the map security.py
    resolves), so the built-in sign-ins are governed records from
    first boot. Idempotent by username."""
    existing = {a.username for a in session.scalars(select(UserAccount)).all()}
    created = 0
    for token, info in dev_tokens().items():
        username = info["username"]
        if username in existing:
            continue
        session.add(UserAccount(
            username=username,
            full_name=f"Built-in dev user ({username})",
            role=Role(info["role"]),
            patient_ref=info.get("patient_ref"),
            status="active",
            dev_token_hash=token_hash(token) if auth_mode() != "oidc" else None,
            created_by="system", updated_by="system"))
        created += 1
    if _FRONTDESK_USERNAME not in existing and auth_mode() != "oidc":
        # The front desk: viewer sign-in role (the non-clinical staff
        # role), front-desk JOB role on the linked directory entry —
        # that link is what lands this sign-in on the front-desk
        # audience home (app.people.resolve_audience).
        session.add(UserAccount(
            username=_FRONTDESK_USERNAME,
            full_name="Rita Santos (front desk)",
            role=Role.viewer, unit="clinic", service_line="medicine",
            staff_id="staff-019", status="active",
            dev_token_hash=token_hash(_FRONTDESK_TOKEN),
            created_by="system", updated_by="system"))
        created += 1
    if created:
        session.flush()
        append_audit(session, actor="system", action="accounts.seed",
                     entity="accounts",
                     payload={"created": created, "synthetic": True})


def _seed_register_entry(session: Session) -> None:
    seed = {
        "body": "HIPAA",
        "citation": "45 CFR 164.312(a)(2)(i)",
        "title": "Unique user identification",
        "control": ("Every person who signs in has exactly one managed "
                    "account with a unique username (duplicates are "
                    "refused); the account carries their role, unit "
                    "assignment and staff-directory link, its whole "
                    "lifecycle (create, update, disable, enable) is "
                    "hash-chain audited, and disabling the account "
                    "stops its sign-in immediately — a shared or "
                    "anonymous identity cannot act in the system."),
        "implementation_ref": "backend/app/accounts.py (account "
                              "records + lifecycle); "
                              "backend/app/security.py (dev token "
                              "resolution refuses disabled accounts)",
        "evidence_source": "audit:accounts.",
    }
    exists = session.scalars(select(ComplianceRequirement).where(
        ComplianceRequirement.body == seed["body"],
        ComplianceRequirement.citation == seed["citation"])).first()
    if exists is None:
        session.add(ComplianceRequirement(created_by="system", **seed))
        session.flush()
        append_audit(session, actor="system",
                     action="compliance.register.seed", entity="register",
                     payload={"added": 1, "citation": seed["citation"],
                              "pass": "front-door"})


def ensure_account_defaults(session: Session) -> None:
    """Seed the built-in dev accounts, the front-desk account and
    the HIPAA unique-user-identification register entry. Idempotent."""
    _seed_accounts(session)
    _seed_register_entry(session)
    session.commit()
