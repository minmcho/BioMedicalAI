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
                "cohort", "integration", "orderset", "measure")

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
    # Read gates (realignment): these used to be hardcoded role tuples in
    # the modules (STAFF_READ / POPHEALTH_ROLES). The defaults below are
    # copied tuple-for-tuple from those constants, so behaviour is
    # unchanged until a site publishes a permissions override.
    "studio.read": ("admin", "configurator", "clinician", "viewer"),
    "lab.read": ("admin", "configurator", "clinician", "viewer"),
    "pharmacy.read": ("admin", "configurator", "clinician", "viewer"),
    "boards.read": ("admin", "configurator", "clinician", "viewer"),
    "pophealth.view": ("admin", "clinician"),
    "pophealth.run": ("admin",),
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
    # Phase 5 (orders + documentation). Placing/signing orders and
    # authoring documentation are clinical write work (fhir.write
    # pattern); reads follow the staff-read pattern incl. viewer.
    # Per-transition restrictions (e.g. who may SIGN) live in the
    # published order-flow workflow config, not here.
    "orders.read": ("admin", "configurator", "clinician", "viewer"),
    "orders.place": ("admin", "configurator", "clinician"),
    "notes.read": ("admin", "configurator", "clinician", "viewer"),
    "notes.write": ("admin", "configurator", "clinician"),
    # Phase 6 (inpatient operations). Reads follow the boards pattern
    # (staff incl. viewer; the census is staff-only operational data —
    # the patient role is deliberately absent); bed/ADT work is
    # clinical-operations work on the fhir.write pattern. Housekeeping
    # flips (cleaning -> available) ride inpatient.work in v1; a site
    # can split them out by publishing a permissions override plus a
    # capability of its own.
    "inpatient.read": ("admin", "configurator", "clinician", "viewer"),
    "inpatient.work": ("admin", "configurator", "clinician"),
    # Phase 7 (access & revenue cycle). Referral intake/triage,
    # authorisations, eligibility checks and the waitlist are
    # registration/access work: reads follow the staff-read pattern,
    # writes follow the fhir.write pattern. HIM coding is revenue-cycle
    # work: coders read the queue (billing.view pattern — no viewer)
    # and assign codes under a billing.manage-like gate; the queue
    # surfaces clinical documentation references, so the patient role
    # is deliberately absent from both.
    "access.read": ("admin", "configurator", "clinician", "viewer"),
    "access.work": ("admin", "configurator", "clinician"),
    "coding.read": ("admin", "configurator", "clinician"),
    "coding.work": ("admin", "configurator"),
    # Phase 8 (interoperability). The interface log is staff operational
    # data (message metadata + patient refs, never message bodies in
    # list views beyond the raw text an interface analyst needs) —
    # reads follow the staff-read pattern; dispatch / replay are
    # interface-engine operations (admin + configurator, like
    # studio.manage). Bulk export is a SYSTEM-LEVEL, record-level
    # extract: admin/configurator only, every job and download audited.
    "interop.read": ("admin", "configurator", "clinician", "viewer"),
    "interop.manage": ("admin", "configurator"),
    "interop.export": ("admin", "configurator"),
    # Phase 9 (engagement + quality/research). Secure messaging is
    # clinical communication: staff write/read under one capability;
    # the patient side is scoped to the caller's own record in
    # app.engagement (the portal pattern), not by this capability.
    "messages.use": ("admin", "configurator", "clinician"),
    # Patient self-scheduling: the portal appointment-request endpoint.
    # A site turns it off (or on for more roles) by publishing a
    # permissions override — the solo-practice profile pack does
    # exactly that. Staff confirm requests through the ordinary
    # Appointment ADT machine either way.
    "portal.self-schedule": ("admin", "clinician", "patient"),
    # Research workbench aggregate export (app.pophealth): a
    # de-identified, aggregate-only extract — gated like
    # interop.export (admin/configurator), every export audited.
    "pophealth.export": ("admin", "configurator"),
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
# Shell defaults: navigation + branding (seeded published v1 at startup,
# mirroring the web shell's built-in defaults EXACTLY — same modules,
# labels, order and per-module roles as web/src/auth/session.ts — so the
# shell can consume the published config with zero visible change, and a
# site re-skins/re-menus the deployment by publishing over them.)
# ---------------------------------------------------------------------------

NAVIGATION_KEY = "main"
BRANDING_KEY = "default"

_STAFF = ["admin", "configurator", "clinician", "viewer"]
_WRITE_STAFF = ["admin", "configurator", "clinician"]

# (module, label, roles) in shell order — mirrors NAV + canAccess in
# web/src/auth/session.ts at the time of the realignment.
_DEFAULT_NAV_MODULES: tuple[tuple[str, str, list[str]], ...] = (
    ("patients", "Patients", _STAFF),
    ("inbox", "Results inbox", _WRITE_STAFF),
    ("scheduling", "Scheduling", _WRITE_STAFF),
    ("lab", "Laboratory", _STAFF),
    ("pophealth", "Population health", ["admin", "clinician"]),
    ("billing", "Billing", ["admin", "configurator", "clinician", "patient"]),
    ("pharmacy", "Pharmacy", _STAFF),
    ("ed", "ED board", _STAFF),
    ("radiology", "Radiology", _STAFF),
    ("surgery", "Surgery board", _STAFF),
    ("beds", "Bed board", _STAFF),
    ("studio", "System Studio", ["admin", "configurator"]),
    ("portal", "My results", ["patient"]),
)


def default_navigation_payload() -> dict:
    return {"items": [
        {"module": module, "label": label, "roles": list(roles),
         "order": index + 1, "visible": True}
        for index, (module, label, roles) in enumerate(_DEFAULT_NAV_MODULES)
    ]}


def default_branding_payload() -> dict:
    # orgName + primaryColor are the web shell's literals (App.tsx brand
    # heading, styles.css --accent); portalTitle is the portal heading.
    return {"orgName": "OpenCare", "portalTitle": "My results",
            "primaryColor": "#0b6e4f"}


# ---------------------------------------------------------------------------
# Workflow machine loading (shared by every config-driven state machine)
# ---------------------------------------------------------------------------

def machine_from_payload(payload, default_transitions):
    """(transitions, known states, per-transition roles) from a workflow
    payload. `roles` maps (from, to) -> list of role names for transitions
    that declare a `roles` list; transitions without one are absent from
    the map, meaning any otherwise-authorised role may perform them."""
    payload = payload or {}
    transitions: dict[str, set[str]] = {}
    roles: dict[tuple[str, str], list[str]] = {}
    for t in payload.get("transitions") or []:
        if isinstance(t, dict) and t.get("from") and t.get("to"):
            transitions.setdefault(t["from"], set()).add(t["to"])
            if isinstance(t.get("roles"), list):
                roles[(t["from"], t["to"])] = [
                    r for r in t["roles"] if isinstance(r, str)]
    states = set(payload.get("states") or transitions.keys())
    for targets in transitions.values():
        states |= targets
    return transitions, states, roles


def load_workflow_machine(session: Session, key: str,
                          default_transitions):
    """machine_from_payload over the PUBLISHED workflow config `key`,
    falling back to the built-in map when nothing is published."""
    cfg = published_config(session, "workflow", key)
    if cfg is not None:
        return machine_from_payload(cfg.payload, default_transitions)
    return ({src: set(dst) for src, dst in default_transitions.items()},
            set(default_transitions.keys()), {})


def transition_role_allowed(roles: dict, current, target, role_value: str) -> bool:
    """True when the transition declares no role restriction, or the
    caller's role is in the declared list. A restriction naming no valid
    caller (empty list) blocks everyone — that is what the config says."""
    allowed = roles.get((current, target))
    return allowed is None or role_value in allowed


def payload_state_set(payload, field: str, fallback) -> set:
    """A state-set field (governedStates / activeStates / ...) from a
    workflow payload, falling back to the built-in set when the field is
    absent or malformed."""
    values = (payload or {}).get(field)
    if isinstance(values, list) and values and all(
            isinstance(v, str) and v for v in values):
        return set(values)
    return set(fallback)


def workflow_payload(session: Session, key: str) -> dict | None:
    cfg = published_config(session, "workflow", key)
    return (cfg.payload or {}) if cfg is not None else None


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
    # Optional policy fields the owning modules read at runtime (the
    # realignment moved these state sets / limits out of code constants):
    # governedStates (bare transitions refused), activeStates (board
    # census), specimenStates (lab specimen mirror), exportableStates
    # (837 export) — each a list of declared states.
    for field in ("governedStates", "activeStates", "specimenStates",
                  "exportableStates"):
        values = payload.get(field)
        if values is not None:
            if not isinstance(values, list) or any(
                    not isinstance(v, str) or v not in state_set for v in values):
                _fail(f"'{field}' must be a list of declared states")
    max_appeals = payload.get("maxAppeals")
    if max_appeals is not None and (isinstance(max_appeals, bool)
                                    or not isinstance(max_appeals, int)
                                    or max_appeals < 0):
        _fail("'maxAppeals' must be a non-negative integer")
    # Phase 7: the claim lifecycle may additionally carry
    # `requireCoding` (boolean) — when true, app.billing refuses claim
    # generation for an encounter with no HIM coding record. Absent =
    # false (the pre-Phase-7 behaviour).
    require_coding = payload.get("requireCoding")
    if require_coding is not None and not isinstance(require_coding, bool):
        _fail("'requireCoding' must be a boolean")


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
        # A capability NOT in CAPABILITY_DEFAULTS is accepted and stored:
        # sites may define new capabilities in configuration ahead of (or
        # without) code that enforces them. The distinction matters —
        # DEFINING a capability is configuration; the ENFORCEMENT POINTS
        # (which endpoint checks which capability) remain code-defined,
        # and role_allowed() resolves an undefined capability to its
        # published cells, defaulting to denied.
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
    bands = payload.get("ageBands")
    if bands is not None:
        if not isinstance(bands, list) or not bands:
            _fail("'ageBands' must be a non-empty list of [low, high, label] triples")
        for i, band in enumerate(bands):
            if not isinstance(band, list) or len(band) != 3:
                _fail(f"ageBands[{i}] must be a [low, high, label] triple")
            low, high, label = band
            if isinstance(low, bool) or not isinstance(low, int) or low < 0:
                _fail(f"ageBands[{i}][0] (low) must be a non-negative integer")
            if high is not None and (isinstance(high, bool)
                                     or not isinstance(high, int) or high < low):
                _fail(f"ageBands[{i}][1] (high) must be null or an integer >= low")
            if not isinstance(label, str) or not label:
                _fail(f"ageBands[{i}][2] (label) must be a non-empty string")


def validate_integration(payload) -> None:
    """Integration config (Phase 4B): external-system connection data —
    in v1 the DICOM viewer link target. Shape: an object whose known
    sections are objects; `viewer`, when present, must carry a string
    `baseUrl` (an http(s) URL the web app appends the study UID to).
    Unknown extra sections pass through: they are data for the owning
    integration, not structure.

    Phase 8 adds the HL7 interface-engine sections (used by the
    `hl7-endpoints` key): `facility` (MSH identity strings), `retry`
    ({maxAttempts, backoffSeconds}) and `endpoints` — a list of
    {name, host, port, messageTypes, enabled, receivingApplication?,
    receivingFacility?}. Those sections are validated when present so
    a malformed endpoint can never reach the dispatcher."""
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
    facility = payload.get("facility")
    if facility is not None:
        if not isinstance(facility, dict):
            _fail("'facility' must be an object")
        for field in ("sendingApplication", "sendingFacility"):
            if field in facility and not isinstance(facility[field], str):
                _fail(f"'facility.{field}' must be a string")
    retry = payload.get("retry")
    if retry is not None:
        if not isinstance(retry, dict):
            _fail("'retry' must be an object")
        if "maxAttempts" in retry:
            value = retry["maxAttempts"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                _fail("'retry.maxAttempts' must be a positive integer")
        if "backoffSeconds" in retry:
            value = retry["backoffSeconds"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                _fail("'retry.backoffSeconds' must be a non-negative number")
    endpoints = payload.get("endpoints")
    if endpoints is not None:
        if not isinstance(endpoints, list):
            _fail("'endpoints' must be a list")
        seen_names: set[str] = set()
        for i, endpoint in enumerate(endpoints):
            if not isinstance(endpoint, dict):
                _fail(f"endpoints[{i}] must be an object")
            name = _require_str(endpoint.get("name"), f"endpoints[{i}].name")
            if name in seen_names:
                _fail(f"Duplicate endpoint name '{name}'")
            seen_names.add(name)
            _require_str(endpoint.get("host"), f"endpoints[{i}].host")
            port = endpoint.get("port")
            if isinstance(port, bool) or not isinstance(port, int) \
                    or not (1 <= port <= 65535):
                _fail(f"endpoints[{i}].port must be an integer in 1..65535")
            types = endpoint.get("messageTypes")
            if not isinstance(types, list) or not types \
                    or not all(isinstance(t, str) and t for t in types):
                _fail(f"endpoints[{i}].messageTypes must be a non-empty list of strings")
            if "enabled" in endpoint and not isinstance(endpoint["enabled"], bool):
                _fail(f"endpoints[{i}].enabled must be a boolean")
            for field in ("receivingApplication", "receivingFacility"):
                if field in endpoint and not isinstance(endpoint[field], str):
                    _fail(f"endpoints[{i}].{field} must be a string")


def validate_orderset(payload) -> None:
    """Order set (Phase 5): a named bundle of order templates that CPOE
    expands into individual orders on placement. Shape: {name, orders:
    [{type, code, ...}]} — each item names an order TYPE (validated
    against the published order-types catalogue at placement time, not
    here: a set must remain publishable before/after catalogue edits)
    and a code. Extra item fields (display, performerRole, modality,
    dosage text, ...) pass through unvalidated: they are placement
    fields for the owning orders module, not structure."""
    payload = _require_obj(payload)
    _require_str(payload.get("name"), "name")
    if "description" in payload and not isinstance(payload["description"], str):
        _fail("'description' must be a string")
    orders = payload.get("orders")
    if not isinstance(orders, list) or not orders:
        _fail("Order set config requires a non-empty 'orders' list")
    for i, item in enumerate(orders):
        if not isinstance(item, dict):
            _fail(f"orders[{i}] must be an object")
        _require_str(item.get("type"), f"orders[{i}].type")
        _require_str(item.get("code"), f"orders[{i}].code")
        if "display" in item and not isinstance(item["display"], str):
            _fail(f"orders[{i}].display must be a string")


def _validate_lab_rule(lab, field: str) -> None:
    """One lab criterion — the cohort `lab` shape (latest value vs
    threshold, optional window), shared by cohort and measure configs."""
    if not isinstance(lab, dict):
        _fail(f"'{field}' must be an object")
    _require_str(lab.get("code"), f"{field}.code")
    if lab.get("comparator") not in COHORT_COMPARATORS:
        _fail(f"'{field}.comparator' must be one of {list(COHORT_COMPARATORS)}")
    threshold = lab.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        _fail(f"'{field}.threshold' must be a number")
    window = lab.get("windowDays")
    if window is not None and (isinstance(window, bool)
                               or not isinstance(window, int) or window <= 0):
        _fail(f"'{field}.windowDays' must be a positive integer")


def _validate_code_list(values, field: str) -> None:
    if not isinstance(values, list):
        _fail(f"'{field}' must be a list of {{system, code}} objects")
    for i, c in enumerate(values):
        if not isinstance(c, dict):
            _fail(f"{field}[{i}] must be an object with 'system' and 'code'")
        _require_str(c.get("code"), f"{field}[{i}].code")
        if "system" in c and not isinstance(c["system"], str):
            _fail(f"{field}[{i}].system must be a string")


def _validate_measure_criteria(criteria, field: str) -> None:
    """One side (denominator or numerator) of a quality measure: a set
    of criteria in the SAME style as a cohort definition (condition
    codes and/or lab rules — evaluated by the same warehouse code in
    app.pophealth / app.quality), extended with `labs` (several lab
    rules, ALL must hold), `procedureCodes` and `ageRange`. At least
    one criterion is required; criteria combine as AND."""
    if not isinstance(criteria, dict):
        _fail(f"'{field}' must be an object of measure criteria")
    present = False
    if criteria.get("conditionCodes") is not None:
        _validate_code_list(criteria["conditionCodes"], f"{field}.conditionCodes")
        present = present or bool(criteria["conditionCodes"])
    if criteria.get("procedureCodes") is not None:
        _validate_code_list(criteria["procedureCodes"], f"{field}.procedureCodes")
        present = present or bool(criteria["procedureCodes"])
    if criteria.get("lab") is not None:
        _validate_lab_rule(criteria["lab"], f"{field}.lab")
        present = True
    if criteria.get("labs") is not None:
        labs = criteria["labs"]
        if not isinstance(labs, list) or not labs:
            _fail(f"'{field}.labs' must be a non-empty list of lab rules")
        for i, lab in enumerate(labs):
            _validate_lab_rule(lab, f"{field}.labs[{i}]")
        present = True
    age = criteria.get("ageRange")
    if age is not None:
        if not isinstance(age, list) or len(age) != 2:
            _fail(f"'{field}.ageRange' must be a [low, high] pair")
        low, high = age
        for bound in (low, high):
            if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
                _fail(f"'{field}.ageRange' bounds must be non-negative integers")
        if high < low:
            _fail(f"'{field}.ageRange' high must be >= low")
        present = True
    if not present:
        _fail(f"'{field}' requires at least one criterion (conditionCodes, "
              "procedureCodes, lab, labs or ageRange)")


def validate_measure(payload) -> None:
    """Quality measure definition (Phase 9): {name, title?,
    description?, denominator, numerator, smallCellThreshold?}. The
    numerator/denominator are cohort-style criteria (see
    _validate_measure_criteria); the measure ENGINE (app.quality)
    evaluates them with the population-health evaluator over the
    warehouse — a measure is configuration, never code."""
    payload = _require_obj(payload)
    _require_str(payload.get("name"), "name")
    for field in ("title", "description"):
        if field in payload and not isinstance(payload[field], str):
            _fail(f"'{field}' must be a string")
    if "denominator" not in payload:
        _fail("Measure config requires a 'denominator' criteria object")
    if "numerator" not in payload:
        _fail("Measure config requires a 'numerator' criteria object")
    _validate_measure_criteria(payload["denominator"], "denominator")
    _validate_measure_criteria(payload["numerator"], "numerator")
    threshold_cells = payload.get("smallCellThreshold")
    if threshold_cells is not None and (isinstance(threshold_cells, bool)
                                        or not isinstance(threshold_cells, int)
                                        or threshold_cells < 1):
        _fail("'smallCellThreshold' must be a positive integer")


VALIDATORS = {
    "form": validate_form,
    "workflow": validate_workflow,
    "terminology": validate_terminology,
    "navigation": validate_navigation,
    "branding": validate_branding,
    "permissions": validate_permissions,
    "cohort": validate_cohort,
    "integration": validate_integration,
    "orderset": validate_orderset,
    "measure": validate_measure,
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
