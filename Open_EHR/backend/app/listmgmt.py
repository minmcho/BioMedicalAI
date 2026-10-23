"""Phase 15 (F1) — governed dropdown/list management.

Min's rule: "Add update delete should available for authorized role" —
for ALL dropdowns, not one list at a time. This module is the in-context
editing surface for the terminology-backed selection lists the app
already runs on:

- **Registry**: a PUBLISHED Studio config, ``terminology/
  manageable-lists``, whose concepts ARE the registry entries (the
  Phase 12/13 pattern: extra fields ride on concepts so the document
  stays a valid, republishable terminology config). Each entry names
  one real list: {kind, key, label, module, managerJobRoles[]}. The
  registry is DATA — a list that is missing here is one config publish
  away from being manageable, never a code change. The registry itself
  is NOT in the registry: changing who may manage lists is a Studio
  governance act (studio.manage), not a list edit.
- **Authorisation**: an entry is manageable by (a) a caller whose role
  holds the ``lists.manage`` capability (admin / configurator by
  default), OR (b) a caller whose linked staff-directory entry
  (app.people: StaffMember.username == user.username) carries a
  job_role named in the entry's ``managerJobRoles`` — the
  department-manager path. Everyone else gets 403 on the draft
  endpoint. Registry reads ride ``studio.read``.
- **Draft editing**: PUT .../draft accepts the FULL edited concept
  list and stores it as the DRAFT version of that list's Studio
  config (creating the next draft version, or replacing the existing
  draft's payload — the protocols/studio_config draft pattern).
  Published config is NEVER mutated here, and this module has no
  publish path: publishing stays the existing Studio publish endpoint,
  performed by a human in System Studio. Effects:
    ADD      = a code that is not in the published list;
    UPDATE   = changed fields on an existing published code;
    DELETE   = the concept marked ``active: false`` (deactivation).
  Removing a published code from the submitted list outright is a 422:
  published values are never hard-deleted because clinical records
  reference them. Validation reuses configstore's per-kind validator,
  so a draft the real validator would reject at publish 422s here.
  Every draft write is hash-audit-chained with the diff summary
  (added / updated / deactivated codes, computed against PUBLISHED).
- **Consumers read PUBLISHED only** — unchanged code paths; the flip
  is proven in tests through the radiology modalities endpoint.
- **Excluded, honestly**: ``terminology/lab-catalog`` is NOT in the
  registry — the laboratory catalogue has its own Test Manager
  surface. Lists whose published payload is not concepts-shaped
  (e.g. ``terminology/rfid`` tagTypes/zones) and operational (non-
  dropdown) terminology configs are likewise not registered.
  Transport / diet-type lists do not exist yet (the parallel support
  workstream had not landed them when this seed was written); when
  they land as terminology configs, they join by registry publish.
"""
import copy

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import append_audit
from .configstore import (ConfigPayloadError, get_config_version,
                          list_config_versions, published_config,
                          role_allowed, validate_payload)
from .models import ConfigStatus, StaffMember, StudioConfig, User
from .security import get_current_user, get_db, require_capability

router = APIRouter(prefix="/lists", tags=["listmgmt"])

REGISTRY_KIND = "terminology"
REGISTRY_KEY = "manageable-lists"

HONESTY_NOTICE = (
    "Draft only: these changes take effect only when a human publishes "
    "this draft in System Studio. Consumers read the published list and "
    "are unchanged until then."
)

# (key, label, module, managerJobRoles) — terminology kind for all.
# Job-role codes are the published workforce concepts (terminology/
# workforce): physician, nurse, pharmacist, lab-technologist,
# radiographer, unit-manager, department-head, executive, admin-staff,
# support-staff, front-desk.
_REGISTRY_SEED: tuple[tuple[str, str, str, list[str]], ...] = (
    ("imaging-modalities", "Imaging modalities", "radiology",
     ["radiographer", "department-head"]),
    ("or-rooms", "Operating rooms", "surgery",
     ["unit-manager", "department-head"]),
    ("ed-triage", "ESI triage levels", "ed",
     ["unit-manager", "department-head"]),
    ("discharge-dispositions", "Discharge dispositions", "inpatient",
     ["unit-manager", "department-head"]),
    ("adt-event-types", "ADT event types", "inpatient",
     ["unit-manager", "department-head"]),
    ("locations", "Locations (facility / units / beds)", "inpatient",
     ["unit-manager", "department-head"]),
    ("order-types", "Order types", "orders", ["department-head"]),
    ("note-types", "Note types", "documentation", ["department-head"]),
    ("referral-services", "Referral services", "access",
     ["unit-manager", "department-head"]),
    ("slot-types", "Appointment slot types", "access",
     ["unit-manager", "department-head"]),
    ("payers", "Payers", "access", ["department-head"]),
    ("coding-diagnoses", "Coding diagnoses (ICD-10)", "revenue",
     ["department-head"]),
    ("coding-procedures", "Coding procedures (CPT)", "revenue",
     ["department-head"]),
    ("denial-categories", "Denial categories", "revenue",
     ["department-head"]),
    ("remittance-status", "Remittance claim-status map", "revenue",
     ["department-head"]),
    ("payment-methods", "Patient payment methods", "revenue",
     ["department-head"]),
    ("workforce", "Workforce job roles", "workforce",
     ["executive", "department-head"]),
    # Laboratory lists that are NOT the lab catalogue (excluded below):
    ("micro-organisms", "Microbiology organisms", "laboratory",
     ["lab-technologist", "department-head"]),
    ("micro-antibiotics", "Microbiology antibiotics", "laboratory",
     ["lab-technologist", "department-head"]),
    ("sendout-labs", "Send-out reference laboratories", "laboratory",
     ["lab-technologist", "department-head"]),
    ("specimen-lifecycle", "Specimen lifecycle scan actions",
     "laboratory", ["lab-technologist", "department-head"]),
    ("devices", "Device types", "devices",
     ["unit-manager", "department-head"]),
    ("reportable-conditions", "Reportable conditions (ELR trigger list)",
     "interop", ["lab-technologist", "department-head"]),
    ("surgery-urgency", "Surgery case-request urgency levels", "surgery",
     ["unit-manager", "department-head"]),
    ("instrument-trays", "Instrument tray types", "surgery",
     ["unit-manager", "department-head"]),
    ("surgery-complications", "Surgery outcome / complication vocabulary",
     "surgery", ["department-head"]),
    # Phase 15 integration pass: the support-services lists landed
    # after this registry was first seeded (support_services.py) and
    # join the registry here, per the registry's own rule that a
    # missing list joins as data.
    ("diet-types", "Diet types", "support",
     ["unit-manager", "department-head"]),
    ("languages", "Interpreter languages", "support",
     ["unit-manager", "department-head"]),
)

_EXCLUDED_SEED = (
    {"kind": "terminology", "key": "lab-catalog",
     "label": "Laboratory test catalogue",
     "reason": ("Excluded: the lab catalogue has its own Test Manager "
                "surface in the Laboratory module; it is not edited "
                "through this generic list surface.")},
    {"kind": "terminology", "key": "rfid",
     "label": "RFID tag types / zones",
     "reason": ("Excluded: its published payload is not concepts-shaped "
                "(tagTypes/zones sections), so the concepts draft "
                "surface cannot edit it without corrupting the shape.")},
)


def default_manageable_lists_payload() -> dict:
    return {
        "name": "OpenCare manageable lists registry (Phase 15)",
        "concepts": [
            {"code": key, "display": label, "active": True,
             "kind": "terminology", "key": key, "label": label,
             "module": module, "managerJobRoles": list(roles)}
            for key, label, module, roles in _REGISTRY_SEED
        ],
        "excludedLists": [dict(e) for e in _EXCLUDED_SEED],
        "notes": (
            "Entries name the terminology-backed selection lists that "
            "exist in the app today. A missing list joins by "
            "publishing a new version of this registry — data, not "
            "code."),
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
    for action in ("studio.config.create", "studio.config.publish"):
        append_audit(session, actor="system", action=action,
                     entity=f"config:{kind}/{key}@v1",
                     payload={"kind": kind, "key": key, "version": 1,
                              "seeded_default": True})


def ensure_listmgmt_defaults(session: Session) -> None:
    """Seed the manageable-lists registry as published v1 (idempotent:
    any existing version means the seed already ran — never overwrite
    a site's published registry)."""
    _seed_published(session, REGISTRY_KIND, REGISTRY_KEY,
                    default_manageable_lists_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Registry + authorisation helpers
# ---------------------------------------------------------------------------

def _registry(session: Session) -> StudioConfig:
    cfg = published_config(session, REGISTRY_KIND, REGISTRY_KEY)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=("No published manageable-lists registry "
                    "(terminology/manageable-lists); publish one in "
                    "Studio before using list management"))
    return cfg


def _entries(session: Session) -> list[dict]:
    cfg = _registry(session)
    entries = []
    for concept in (cfg.payload or {}).get("concepts") or []:
        if not isinstance(concept, dict) or not concept.get("code"):
            continue
        entries.append({
            "kind": concept.get("kind") or REGISTRY_KIND,
            "key": concept.get("key") or concept["code"],
            "label": concept.get("label") or concept.get("display"),
            "module": concept.get("module"),
            "managerJobRoles": list(concept.get("managerJobRoles") or []),
            "active": concept.get("active") is not False,
        })
    return entries


def _get_entry(session: Session, kind: str, key: str) -> dict:
    for entry in _entries(session):
        if entry["kind"] == kind and entry["key"] == key:
            return entry
    raise HTTPException(
        status_code=404,
        detail=(f"List '{kind}/{key}' is not in the published "
                "manageable-lists registry; a list joins the registry "
                "by publishing a new registry version in System Studio"))


def _caller_staff(session: Session, user: User) -> StaffMember | None:
    """The caller's staff-directory entry — the people.py link
    (StaffMember.username == user.username), or None."""
    return session.scalars(select(StaffMember).where(
        StaffMember.username == user.username)).first()


def _manage_basis(session: Session, user: User,
                  entry: dict) -> str | None:
    """Why this caller may manage this entry, or None: the
    lists.manage capability, else the department-manager job-role path
    through the caller's linked staff record."""
    if role_allowed(session, user.role, "lists.manage"):
        return "capability:lists.manage"
    staff = _caller_staff(session, user)
    if staff is not None and staff.job_role in entry["managerJobRoles"]:
        return f"job_role:{staff.job_role}"
    return None


def _require_manage(session: Session, user: User, entry: dict) -> str:
    basis = _manage_basis(session, user, entry)
    if basis is None:
        staff = _caller_staff(session, user)
        job_role = staff.job_role if staff is not None else None
        raise HTTPException(
            status_code=403,
            detail=(f"Role {user.role.value} may not manage list "
                    f"'{entry['kind']}/{entry['key']}': it requires the "
                    "'lists.manage' capability or a linked staff job "
                    f"role in {entry['managerJobRoles']} (caller's "
                    f"linked job role: {job_role or 'none'})"))
    return basis


def _draft_row(session: Session, kind: str, key: str) -> StudioConfig | None:
    drafts = [v for v in list_config_versions(session, kind, key)
              if v.status == ConfigStatus.draft]
    return drafts[-1] if drafts else None


def _published_row(session: Session, kind: str, key: str) -> StudioConfig:
    row = published_config(session, kind, key)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No published config for {kind}/{key} to manage")
    return row


async def _json_any(request: Request):
    try:
        return await request.json()
    except Exception:
        raise HTTPException(status_code=422,
                            detail="Request body must be valid JSON")


def _concepts_of(payload) -> list:
    concepts = (payload or {}).get("concepts") if isinstance(payload, dict) \
        else None
    return [c for c in concepts or [] if isinstance(c, dict)]


# ---------------------------------------------------------------------------
# Routes: registry read, single-list read, draft write
# ---------------------------------------------------------------------------

@router.get("")
def list_registry(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("studio.read"))):
    """The published manageable-lists registry: every governed
    dropdown/selection list, its module and the job roles that may
    manage it, plus the caller's manage basis per entry."""
    cfg = _registry(session)
    items = []
    for entry in _entries(session):
        items.append({**entry,
                      "caller_can_manage":
                          _manage_basis(session, user, entry) is not None,
                      "manage_basis": _manage_basis(session, user, entry)})
    return {"registry": {"kind": REGISTRY_KIND, "key": REGISTRY_KEY,
                         "version": cfg.version},
            "items": items, "total": len(items),
            "excluded_lists": (cfg.payload or {}).get("excludedLists") or [],
            "notes": (cfg.payload or {}).get("notes")}


@router.get("/{kind}/{key}")
def get_list(kind: str, key: str, session: Session = Depends(get_db),
             user: User = Depends(require_capability("studio.read"))):
    """One manageable list, published and draft CLEARLY SEPARATED:
    consumers read `published` only; `draft` (null when none) is the
    pending edit that takes effect only on a Studio publish."""
    entry = _get_entry(session, kind, key)
    published = _published_row(session, kind, key)
    draft = _draft_row(session, kind, key)
    return {
        **entry,
        "caller_can_manage": _manage_basis(session, user, entry) is not None,
        "manage_basis": _manage_basis(session, user, entry),
        "published": {"version": published.version,
                      "concepts": _concepts_of(published.payload)},
        "draft": ({"version": draft.version,
                   "concepts": _concepts_of(draft.payload),
                   "created_by": draft.created_by,
                   "updated_by": draft.updated_by}
                  if draft is not None else None),
        "notice": HONESTY_NOTICE,
    }


@router.put("/{kind}/{key}/draft")
async def put_draft(kind: str, key: str, request: Request,
                    session: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Write the full edited concept list as the DRAFT version of
    {kind}/{key} (Studio draft machinery: a new draft version, or the
    existing draft's payload replaced; published is never touched).

    Body: {"concepts": [...]} — the FULL list. ADD = new code;
    UPDATE = changed fields on an existing code; DELETE = an existing
    code with "active": false. Omitting a published code is a 422
    (deactivate instead — published values are never hard-deleted).
    """
    entry = _get_entry(session, kind, key)
    basis = _require_manage(session, user, entry)
    body = await _json_any(request)
    if isinstance(body, list):
        submitted = body
    elif isinstance(body, dict) and isinstance(body.get("concepts"), list):
        submitted = body["concepts"]
    else:
        raise HTTPException(
            status_code=422,
            detail=("Draft requires a JSON body {\"concepts\": [...]} "
                    "carrying the full edited concept list"))
    published = _published_row(session, kind, key)
    composed = copy.deepcopy(published.payload or {})
    composed["concepts"] = copy.deepcopy(submitted)
    # Reuse the kind's real Studio validator: a draft the validator
    # would reject at publish is rejected here, identically.
    try:
        validate_payload(kind, composed)
    except ConfigPayloadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    published_by_code = {c["code"]: c
                         for c in _concepts_of(published.payload)}
    missing = sorted(set(published_by_code) - {
        c.get("code") for c in submitted if isinstance(c, dict)})
    if missing:
        raise HTTPException(
            status_code=422,
            detail=(f"Published code(s) {missing} are missing from the "
                    "submitted list. Published values are never "
                    "hard-deleted because records reference them: to "
                    "delete one, keep its code in the list and mark it "
                    "\"active\": false (deactivation)."))
    submitted_by_code = {c["code"]: c for c in submitted}
    added = sorted(set(submitted_by_code) - set(published_by_code))
    deactivated = sorted(
        code for code in set(submitted_by_code) & set(published_by_code)
        if published_by_code[code].get("active") is not False
        and submitted_by_code[code].get("active") is False)
    updated = sorted(
        code for code in set(submitted_by_code) & set(published_by_code)
        if submitted_by_code[code] != published_by_code[code]
        and code not in deactivated)
    diff = {"added": added, "updated": updated,
            "deactivated": deactivated}
    draft = _draft_row(session, kind, key)
    if draft is not None:
        draft.payload = composed
        draft.updated_by = user.username
        row, action = draft, "lists.draft.update"
    else:
        versions = list_config_versions(session, kind, key)
        next_version = (versions[-1].version + 1) if versions else 1
        row = StudioConfig(kind=kind, key=key, version=next_version,
                           status=ConfigStatus.draft, payload=composed,
                           created_by=user.username,
                           updated_by=user.username)
        session.add(row)
        action = "lists.draft.create"
    session.flush()
    append_audit(session, actor=user.username, action=action,
                 entity=f"config:{kind}/{key}@v{row.version}",
                 payload={"kind": kind, "key": key,
                          "version": row.version,
                          "published_version": published.version,
                          "manage_basis": basis, "diff": diff})
    session.commit()
    return {"kind": kind, "key": key, "label": entry["label"],
            "module": entry["module"], "status": "draft",
            "draft_version": row.version,
            "published_version": published.version,
            "manage_basis": basis, "diff": diff,
            "diff_basis": "published",
            "concepts": submitted,
            "notice": HONESTY_NOTICE}
