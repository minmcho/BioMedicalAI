"""Navigation seed vs capability gate consistency (UX audit S-10).

Two artefacts decide whether a role sees a module: the seeded
navigation payload's per-module `roles` (app.configstore
_DEFAULT_NAV_MODULES) and the web shell's page gate
(web/src/auth/session.ts PAGE_CAPABILITY + canAccessWith, enforced
against the published permissions matrix whose defaults are
CAPABILITY_DEFAULTS here). They silently disagreed for Safety: the
seed promised the viewer a module whose page gate (safety.review)
the viewer does not hold. It failed safe — the AND of the filters
hid the module — but navigation truth lived in two places that
could drift without any test noticing.

This test pins them together: for every seeded module, every role
in the seed must actually pass the module's page gate under the
default capability map. MODULE_GATES transcribes session.ts's
PAGE_CAPABILITY (module key -> the capability its page requires);
the special-cased pages in canAccessWith (patients / billing /
portal) are asserted by their documented rules instead.
"""

from __future__ import annotations

from app.configstore import CAPABILITY_DEFAULTS, _DEFAULT_NAV_MODULES

# Transcribed from web/src/auth/session.ts PAGE_CAPABILITY
# (module -> page -> required capability). Pages absent from that
# map are the canAccessWith special cases handled below.
MODULE_GATES: dict[str, str] = {
    "inbox": "fhir.inbox",
    "scheduling": "fhir.write",
    "lab": "lab.read",
    "pophealth": "pophealth.view",
    "pharmacy": "pharmacy.read",
    "ed": "boards.read",
    "radiology": "boards.read",
    "surgery": "boards.read",
    "beds": "inpatient.read",
    "command": "continuum.read",
    "er-ops": "continuum.read",
    "unit-board": "continuum.read",
    "clinic": "continuum.read",
    "aftercare": "continuum.read",
    "safety": "safety.review",
    # Phase 15: the support services module's page gate is
    # support.read (staff-wide), transcribed from session.ts.
    "support": "support.read",
    # Inventory & Materials pass: the inventory page gates on
    # inventory.read (staff-wide incl. viewer), matching its seed.
    "inventory": "inventory.read",
    "workforce": "workforce.read",
    "reports": "reports.run",
    "imports": "data.import",
    "devices": "devices.read",
    "data-platform": "devices.read",
    "studio": "studio.manage",
    # Phase 16: federation / research pages gate on their read
    # capabilities (viewer included, matching the seed); analytics
    # and documents gate on their work capabilities (viewer excluded
    # by default, matching the seed). Transcribed from session.ts
    # PAGE_CAPABILITY as wired by the Phase 16 coordinator.
    "federation": "federation.read",
    "research": "research.read",
    "analytics": "analytics.use",
    "documents": "intake.work",
    # Workforce, Teams & Panels pass: the teams page gate is
    # teams.read (staff incl. viewer, matching the seed).
    # Transcribed from web/src/auth/session.ts PAGE_CAPABILITY as
    # wired by the Workforce, Teams & Panels coordinator.
    "teams": "teams.read",
}

STAFF_ROLES = {"admin", "configurator", "clinician", "viewer"}


def _holders(capability: str) -> set[str]:
    return set(CAPABILITY_DEFAULTS.get(capability, ()))


def test_every_seeded_module_is_covered_by_this_test():
    """A new module added to the seed without a gate entry here
    fails loudly instead of drifting silently."""
    seeded = {module for module, _label, _roles, _group
              in _DEFAULT_NAV_MODULES}
    covered = set(MODULE_GATES) | {"patients", "billing", "portal"}
    assert seeded == covered, (
        f"seed/gate coverage mismatch: unaccounted={seeded - covered}, "
        f"stale={covered - seeded}")


def test_seed_roles_pass_the_module_page_gate():
    failures = []
    for module, _label, roles, _group in _DEFAULT_NAV_MODULES:
        gate = MODULE_GATES.get(module)
        if gate is None:
            continue
        holders = _holders(gate)
        for role in roles:
            if role not in holders:
                failures.append(
                    f"{module}: seed role '{role}' does not hold the "
                    f"page gate '{gate}' (holders: {sorted(holders)})")
    assert not failures, "\n".join(failures)


def test_special_cased_pages_follow_their_documented_rules():
    by_module = {m: roles for m, _l, roles, _g in _DEFAULT_NAV_MODULES}
    # patients/chart/home (canAccessWith): any non-patient role.
    assert set(by_module["patients"]) <= STAFF_ROLES
    # portal: patients only.
    assert by_module["portal"] == ["patient"]
    # billing: staff roles ride billing.view; a patient sees only
    # their own claims (the page's documented special case).
    billing_holders = _holders("billing.view")
    for role in by_module["billing"]:
        if role == "patient":
            continue
        assert role in billing_holders, (
            f"billing: seed role '{role}' does not hold billing.view")


def test_safety_seed_matches_safety_review_exactly():
    """The S-10 drift, pinned: the Safety module's seed roles are
    exactly the safety.review holders — no wider (the queue is the
    safety team's), no narrower (every reviewer sees the module)."""
    by_module = {m: roles for m, _l, roles, _g in _DEFAULT_NAV_MODULES}
    assert set(by_module["safety"]) == _holders("safety.review")
    # Filing stays staff-wide: safety.report keeps the viewer.
    assert "viewer" in _holders("safety.report")
