"""Studio configuration store — shared core (no FastAPI imports, so both the
Studio config router and the security layer can use it without import cycles).

Everything a deploying organisation changes — forms, workflows, terminology,
navigation, branding, permissions — is configuration *data* with a
draft -> publish lifecycle, versioning and audit (docs/STUDIO_CUSTOMIZATION.md).
This module owns: the kind registry, per-kind payload validation, published
("effective") lookups, the capability defaults that published permission
overrides merge over, and a small structural diff for version comparison.
"""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ConfigStatus, Role, StudioConfig

# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------

CONFIG_KINDS = ("form", "workflow", "terminology", "navigation", "branding", "permissions",
                "cohort", "integration")

# The one key under which the role/capability matrix lives (kind=permissions).
PERMISSIONS_KEY = "role-matrix"

# Capability -> roles allowed by default. A published permissions config may
# override individual (capability, role) cells; everything else falls back to
# these defaults. Enforced server-side in the API dependencies
# (app.security.require_capability), never by hiding UI alone.
CAPABILITY_DEFAULTS: dict[str, tuple[str, ...]] = {
    "fhir.write": ("admin", "configurator", "clinician"),
    "fhir.inbox": ("admin", "configurator", "clinician"),
    "studio.manage": ("admin", "configurator"),
    # Phase 2 (Lab LIS). Ordering and bench work follow the fhir.write
    # pattern; verification/release is a clinical sign-off, so the
    # configurator (a systems role, not a clinician) is excluded by default.
    "lab.order": ("admin", "configurator", "clinician"),
    "lab.work": ("admin", "configurator", "clinician"),
    "lab.verify": ("admin", "clinician"),
    # Phase 4A (billing + pharmacy). Billing: clinicians may VIEW claims
    # (their patients' encounters are billed from the chart they work in);
    # managing the claim lifecycle is a revenue-cycle role. A patient
    # caller's access to their OWN claims is handled in app.billing (own
    # claims only, read-only) — the patient role is deliberately not in
    # these defaults. Pharmacy: dispensing is pharmacy/clinical work;
    # barcode med-admin is a bedside clinical act (admin/clinician).
    "billing.view": ("admin", "configurator", "clinician"),
    "billing.manage": ("admin", "configurator"),
    "pharmacy.dispense": ("admin", "configurator", "clinician"),
    "pharmacy.administer": ("admin", "clinician"),
    # Phase 4B (specialty boards). Board work follows the fhir.write
    # pattern; imaging-report verification is a clinical sign-off, so —
    # like lab.verify — the configurator is excluded by default.
    "ed.work": ("admin", "configurator", "clinician"),
    "radiology.work": ("admin", "configurator", "clinician"),
    "radiology.verify": ("admin", "clinician"),
    "surgery.work": ("admin", "configurator", "clinician"),
}

ROLE_NAMES = tuple(r.value for r in Role)

# FHIR Questionnaire item types accepted by the form designer.
FORM_ITEM_TYPES = ("group", "display", "boolean", "decimal", "integer", "string",
                   "text", "date", "dateTime", "time", "choice", "open-choice",
                   "quantity")

# The Appointment ADT workflow, seeded as published workflow config v1
# (key "appointment-adt") so the state machine is configuration data from the
# start. fhir.APPOINTMENT_TRANSITIONS is the hardcoded fallback used only if
# the published config is disabled/removed; behaviour is identical either way.
ADT_WORKFLOW_KEY = "appointment-adt"
DEFAULT_ADT_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"booked", "cancelled"},
    "pending": {"booked", "cancelled"},
    "booked": {"arrived", "cancelled"},
    "arrived": {"fulfilled", "cancelled"},
    "checked-in": {"fulfilled", "cancelled"},
    "fulfilled": set(),
    "cancelled": set(),
    "noshow": set(),
}


def default_adt_workflow_payload() -> dict:
    states = sorted(DEFAULT_ADT_TRANSITIONS.keys())
    transitions = [
        {"from": src, "to": dst}
        for src in sorted(DEFAULT_ADT_TRANSITIONS)
        for dst in sorted(DEFAULT_ADT_TRANSITIONS[src])
    ]
    return {"states": states, "transitions": transitions}


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def get_config_version(session: Session, kind: str, key: str, version: int) -> StudioConfig | None:
    return session.scalars(
        select(StudioConfig).where(StudioConfig.kind == kind, StudioConfig.key == key,
                                   StudioConfig.version == version)).first()


def list_config_versions(session: Session, kind: str, key: str) -> list[StudioConfig]:
    return list(session.scalars(
        select(StudioConfig).where(StudioConfig.kind == kind, StudioConfig.key == key)
        .order_by(StudioConfig.version.asc())).all())


def published_config(session: Session, kind: str, key: str) -> StudioConfig | None:
    """The effective (published) version of one config object, or None."""
    return session.scalars(
        select(StudioConfig).where(StudioConfig.kind == kind, StudioConfig.key == key,
                                   StudioConfig.status == ConfigStatus.published)).first()


def published_configs(session: Session, kind: str) -> list[StudioConfig]:
    """All effective (published) config objects of one kind."""
    return list(session.scalars(
        select(StudioConfig).where(StudioConfig.kind == kind,
                                   StudioConfig.status == ConfigStatus.published)
        .order_by(StudioConfig.key.asc())).all())


# ---------------------------------------------------------------------------
# Permissions: published overrides merged over built-in defaults
# ---------------------------------------------------------------------------

def role_allowed(session: Session, role: Role, capability: str) -> bool:
    cfg = published_config(session, "permissions", PERMISSIONS_KEY)
    if cfg is not None:
        overrides = (cfg.payload or {}).get("capabilities", {}).get(capability, {})
        if isinstance(overrides, dict) and role.value in overrides:
            return bool(overrides[role.value])
    return role.value in CAPABILITY_DEFAULTS.get(capability, ())


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------

class ConfigPayloadError(Exception):
    """Payload failed validation. status_code is 422 for structural problems;
    400 for the navigation modules-only rule (a product rule, not a syntax
    problem), so the API can return exactly the code the rule specifies."""

    def __init__(self, detail: str, status_code: int = 422):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _fail(detail: str, status_code: int = 422):
    raise ConfigPayloadError(detail, status_code=status_code)


def _require_obj(payload) -> dict:
    if not isinstance(payload, dict):
        _fail("Config payload must be a JSON object")
    return payload


def _require_str(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"'{field}' is required and must be a non-empty string")
    return value


def _check_roles(value, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, list) or any(r not in ROLE_NAMES for r in value):
        _fail(f"'{field}' must be a list of role names drawn from {list(ROLE_NAMES)}")


def validate_form(payload) -> None:
    """FHIR Questionnaire-shaped form definition."""
    payload = _require_obj(payload)
    if payload.get("resourceType") not in (None, "Questionnaire"):
        _fail("'resourceType' must be 'Questionnaire' for form configs")
    items = payload.get("item")
    if not isinstance(items, list) or not items:
        _fail("Form config requires a non-empty 'item' list")
    seen: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            _fail(f"item[{i}] must be an object")
        link_id = _require_str(item.get("linkId"), f"item[{i}].linkId")
        if link_id in seen:
            _fail(f"Duplicate linkId '{link_id}' in form items")
        seen.add(link_id)
        _require_str(item.get("text"), f"item[{i}].text")
        if item.get("type") not in FORM_ITEM_TYPES:
            _fail(f"item[{i}].type must be one of {list(FORM_ITEM_TYPES)}")
        if "required" in item and not isinstance(item["required"], bool):
            _fail(f"item[{i}].required must be a boolean")
        if "valueSet" in item and not isinstance(item["valueSet"], str):
            _fail(f"item[{i}].valueSet must be a string reference")


def validate_workflow(payload) -> None:
    """State-machine definition: states + transitions (from/to/action/roles)."""
    payload = _require_obj(payload)
    states = payload.get("states")
    if not isinstance(states, list) or not states or any(
            not isinstance(s, str) or not s for s in states):
        _fail("Workflow config requires a non-empty 'states' list of strings")
    if len(set(states)) != len(states):
        _fail("Workflow 'states' must be unique")
    transitions = payload.get("transitions")
    if not isinstance(transitions, list):
        _fail("Workflow config requires a 'transitions' list")
    state_set = set(states)
    for i, t in enumerate(transitions):
        if not isinstance(t, dict):
            _fail(f"transitions[{i}] must be an object")
        for field in ("from", "to"):
            if t.get(field) not in state_set:
                _fail(f"transitions[{i}].{field} must be one of the declared states")
        if "action" in t and not isinstance(t["action"], str):
            _fail(f"transitions[{i}].action must be a string")
        _check_roles(t.get("roles"), f"transitions[{i}].roles")


def validate_terminology(payload) -> None:
    """Code list / value set: concepts with code, display, active flag, and an
    optional referenceRange ({low, high, criticalLow, criticalHigh, unit})
    — lab panels, analytes and their ranges are data of exactly this shape.
    Extra concept fields (e.g. a panel's `components` list) pass through
    unvalidated: they are data for the owning module, not structure."""
    payload = _require_obj(payload)
    concepts = payload.get("concepts")
    if not isinstance(concepts, list) or not concepts:
        _fail("Terminology config requires a non-empty 'concepts' list")
    seen: set[str] = set()
    for i, c in enumerate(concepts):
        if not isinstance(c, dict):
            _fail(f"concepts[{i}] must be an object")
        code = _require_str(c.get("code"), f"concepts[{i}].code")
        if code in seen:
            _fail(f"Duplicate concept code '{code}'")
        seen.add(code)
        _require_str(c.get("display"), f"concepts[{i}].display")
        if "active" in c and not isinstance(c["active"], bool):
            _fail(f"concepts[{i}].active must be a boolean")
        rng = c.get("referenceRange")
        if rng is not None:
            if not isinstance(rng, dict):
                _fail(f"concepts[{i}].referenceRange must be an object")
            for bound in ("low", "high", "criticalLow", "criticalHigh"):
                if bound in rng and not isinstance(rng[bound], (int, float)):
                    _fail(f"concepts[{i}].referenceRange.{bound} must be a number")
            if "unit" in rng and not isinstance(rng["unit"], str):
                _fail(f"concepts[{i}].referenceRange.unit must be a string")


NAV_MODULES_ONLY_MESSAGE = (
    "Navigation menu items may only reference modules: pages and forms live "
    "inside modules and cannot be menu entries (System Studio rule)."
)


def validate_navigation(payload) -> None:
    """Menu items: label, module, roles, order, visible. Modules ONLY —
    an item naming a page or a form is rejected with HTTP 400."""
    payload = _require_obj(payload)
    items = payload.get("items")
    if not isinstance(items, list):
        _fail("Navigation config requires an 'items' list")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            _fail(f"items[{i}] must be an object")
        if item.get("type") in ("page", "form") or item.get("page") or item.get("form"):
            _fail(NAV_MODULES_ONLY_MESSAGE, status_code=400)
        if not isinstance(item.get("module"), str) or not item["module"].strip():
            _fail(NAV_MODULES_ONLY_MESSAGE, status_code=400)
        _require_str(item.get("label"), f"items[{i}].label")
        _check_roles(item.get("roles"), f"items[{i}].roles")
        if "order" in item and not isinstance(item["order"], int):
            _fail(f"items[{i}].order must be an integer")
        if "visible" in item and not isinstance(item["visible"], bool):
            _fail(f"items[{i}].visible must be a boolean")


_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def validate_branding(payload) -> None:
    payload = _require_obj(payload)
    _require_str(payload.get("orgName"), "orgName")
    _require_str(payload.get("portalTitle"), "portalTitle")
    if "logoUrl" in payload and not isinstance(payload["logoUrl"], str):
        _fail("'logoUrl' must be a string")
    color = payload.get("primaryColor")
    if color is not None and not (isinstance(color, str) and _COLOR_RE.match(color)):
        _fail("'primaryColor' must be a #RRGGBB hex colour")


def validate_permissions(payload) -> None:
    payload = _require_obj(payload)
    caps = payload.get("capabilities")
    if not isinstance(caps, dict) or not caps:
        _fail("Permissions config requires a non-empty 'capabilities' object")
    for capability, per_role in caps.items():
        if capability not in CAPABILITY_DEFAULTS:
            _fail(f"Unknown capability '{capability}'; known: {sorted(CAPABILITY_DEFAULTS)}")
        if not isinstance(per_role, dict) or not per_role:
            _fail(f"capabilities.{capability} must be an object mapping role -> boolean")
        for role, allowed in per_role.items():
            if role not in ROLE_NAMES:
                _fail(f"capabilities.{capability} names unknown role '{role}'")
            if not isinstance(allowed, bool):
                _fail(f"capabilities.{capability}.{role} must be a boolean")


COHORT_COMPARATORS = ("<", "<=", ">", ">=", "==")


def validate_cohort(payload) -> None:
    """Population-health registry definition (Phase 3): a cohort is a set
    of condition codes and/or a lab rule (latest value vs threshold within
    a window). At least one criterion is required. `smallCellThreshold`
    (default 5) drives small-cell suppression in /pophealth responses."""
    payload = _require_obj(payload)
    _require_str(payload.get("name"), "name")
    conditions = payload.get("conditionCodes")
    if conditions is not None:
        if not isinstance(conditions, list):
            _fail("'conditionCodes' must be a list of {system, code} objects")
        for i, c in enumerate(conditions):
            if not isinstance(c, dict):
                _fail(f"conditionCodes[{i}] must be an object with 'system' and 'code'")
            _require_str(c.get("code"), f"conditionCodes[{i}].code")
            if "system" in c and not isinstance(c["system"], str):
                _fail(f"conditionCodes[{i}].system must be a string")
    lab = payload.get("lab")
    if lab is not None:
        if not isinstance(lab, dict):
            _fail("'lab' must be an object")
        _require_str(lab.get("code"), "lab.code")
        if lab.get("comparator") not in COHORT_COMPARATORS:
            _fail(f"'lab.comparator' must be one of {list(COHORT_COMPARATORS)}")
        threshold = lab.get("threshold")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            _fail("'lab.threshold' must be a number")
        window = lab.get("windowDays")
        if window is not None and (isinstance(window, bool)
                                   or not isinstance(window, int) or window <= 0):
            _fail("'lab.windowDays' must be a positive integer")
    if not conditions and lab is None:
        _fail("Cohort config requires at least one of 'conditionCodes' or 'lab'")
    threshold_cells = payload.get("smallCellThreshold")
    if threshold_cells is not None and (isinstance(threshold_cells, bool)
                                        or not isinstance(threshold_cells, int)
                                        or threshold_cells < 1):
        _fail("'smallCellThreshold' must be a positive integer")


def validate_integration(payload) -> None:
    """Integration config (Phase 4B): external-system connection data —
    in v1 the DICOM viewer link target. Shape: an object whose known
    sections are objects; `viewer`, when present, must carry a string
    `baseUrl` (an http(s) URL the web app appends the study UID to).
    Unknown extra sections pass through: they are data for the owning
    integration, not structure."""
    payload = _require_obj(payload)
    viewer = payload.get("viewer")
    if viewer is not None:
        if not isinstance(viewer, dict):
            _fail("'viewer' must be an object")
        base = viewer.get("baseUrl")
        if base is not None:
            if not isinstance(base, str) or not base.startswith(("http://", "https://")):
                _fail("'viewer.baseUrl' must be an http(s) URL string")
        if "name" in viewer and not isinstance(viewer["name"], str):
            _fail("'viewer.name' must be a string")


VALIDATORS = {
    "form": validate_form,
    "workflow": validate_workflow,
    "terminology": validate_terminology,
    "navigation": validate_navigation,
    "branding": validate_branding,
    "permissions": validate_permissions,
    "cohort": validate_cohort,
    "integration": validate_integration,
}


def validate_payload(kind: str, payload) -> None:
    VALIDATORS[kind](payload)


# ---------------------------------------------------------------------------
# Structural diff (version comparison for the Studio UI / API consumers)
# ---------------------------------------------------------------------------

def diff_payloads(old, new, path: str = "") -> list[dict]:
    """Recursive JSON diff: entries {path, change: added|removed|changed,
    from?, to?}. Lists are compared element-wise by index."""
    out: list[dict] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            p = f"{path}.{key}" if path else key
            if key not in old:
                out.append({"path": p, "change": "added", "to": new[key]})
            elif key not in new:
                out.append({"path": p, "change": "removed", "from": old[key]})
            else:
                out.extend(diff_payloads(old[key], new[key], p))
    elif isinstance(old, list) and isinstance(new, list):
        for i in range(max(len(old), len(new))):
            p = f"{path}[{i}]"
            if i >= len(old):
                out.append({"path": p, "change": "added", "to": new[i]})
            elif i >= len(new):
                out.append({"path": p, "change": "removed", "from": old[i]})
            else:
                out.extend(diff_payloads(old[i], new[i], p))
    elif old != new:
        out.append({"path": path, "change": "changed", "from": old, "to": new})
    return out
