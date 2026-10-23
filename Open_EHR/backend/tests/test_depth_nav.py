"""Depth pass Workstream 1 — grouped navigation (backend).

Proves: the seeded navigation payload carries a `group` per module and
deep-link `sections` for lab/pharmacy; the validator accepts
group/sections and rejects malformed ones (including the modules-only
rule applied inside sections entries); and regrouping is config-only —
publishing a new navigation version with lab in a different group
changes the effective payload with no code change.

Synthetic data only. Helpers mirror tests/test_realignment.py.
"""
from app.configstore import (
    ConfigPayloadError,
    default_navigation_payload,
    validate_navigation,
)
from tests.conftest import ADMIN


def _publish_config(client, kind, key, payload, headers=ADMIN):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=headers)
    assert r.status_code == 200, r.text
    return version


def _effective(client, kind, key, headers=ADMIN):
    r = client.get(f"/studio/config/{kind}/effective/{key}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["payload"]


EXPECTED_GROUPS = {
    # Home & Module UX pass regroup (labels only — roles and module
    # set unchanged; every group holds 2–6 modules).
    "patients": "Patient Care",
    "inbox": "Patient Care",
    "portal": "Patient Care",
    "scheduling": "Access & Revenue",
    "billing": "Access & Revenue",
    "ed": "Clinical",
    "surgery": "Clinical",
    "radiology": "Clinical",
    "lab": "Diagnostics & Pharmacy",
    "pharmacy": "Diagnostics & Pharmacy",
    "beds": "Operations",
    "command": "Operations",
    "er-ops": "Operations",
    "unit-board": "Operations",
    "clinic": "Operations",
    "aftercare": "Operations",
    "safety": "Safety & Support",
    # Phase 15: the support services module (support.read-gated).
    "support": "Safety & Support",
    # Inventory & Materials pass: the inventory module
    # (inventory.read-gated), third in Safety & Support.
    "inventory": "Safety & Support",
    "workforce": "People & Teams",
    # Workforce, Teams & Panels pass: the teams module (teams.read-gated).
    "teams": "People & Teams",
    "pophealth": "Insights",
    "reports": "Insights",
    "analytics": "Insights",
    "devices": "Data Platform",
    "data-platform": "Data Platform",
    # Phase 16: federation + research + analytics + documents join
    # the data platform group (seeded in
    # configstore._DEFAULT_NAV_MODULES).
    "federation": "Data Platform",
    "research": "Data Platform",
    "documents": "Data Platform",
    "studio": "System",
    "imports": "System",
}

LAB_SECTIONS = [
    {"label": "Orders & Results", "section": "orders"},
    {"label": "Microbiology", "section": "microbiology"},
    {"label": "Virology", "section": "virology"},
    {"label": "Blood Bank", "section": "blood-bank"},
    {"label": "Pathology", "section": "pathology"},
    {"label": "QC & EQA", "section": "qc"},
    # Phase 15: Test Catalogue / Panels (Test + Panel Manager) and the
    # specimen chain view join the management cluster (Min's directive).
    {"label": "Test Catalogue", "section": "test-catalogue"},
    {"label": "Panels", "section": "panels"},
    {"label": "Specimens", "section": "specimens"},
    {"label": "Send-outs", "section": "sendouts"},
    {"label": "POCT", "section": "poct"},
]

# Phase 15: command desks / access line and the devices RFID panel
# join as deep-link sections on their existing modules.
COMMAND_SECTIONS = [
    {"label": "Desks", "section": "desks"},
    {"label": "Access line", "section": "access-line"},
]

DEVICES_SECTIONS = [
    {"label": "RFID", "section": "rfid"},
    # Phase 16: the Maintenance (HTM) section joins the devices
    # page (seeded in configstore._DEFAULT_NAV_SECTIONS).
    {"label": "Maintenance", "section": "maintenance"},
]

PHARMACY_SECTIONS = [
    {"label": "Verification", "section": "verification"},
    {"label": "Inventory & procurement", "section": "inventory"},
    {"label": "Controlled substances", "section": "controlled"},
    {"label": "Compounding", "section": "compounding"},
    {"label": "BCMA", "section": "bcma"},
]


def test_seeded_payload_carries_groups_and_sections():
    payload = default_navigation_payload()
    by_module = {i["module"]: i for i in payload["items"]}
    assert set(by_module) == set(EXPECTED_GROUPS)
    for module, group in EXPECTED_GROUPS.items():
        assert by_module[module]["group"] == group, module
    assert by_module["lab"]["sections"] == LAB_SECTIONS
    assert by_module["pharmacy"]["sections"] == PHARMACY_SECTIONS
    assert by_module["command"]["sections"] == COMMAND_SECTIONS
    assert by_module["devices"]["sections"] == DEVICES_SECTIONS
    # Other modules carry no sections in the seed.
    for module, item in by_module.items():
        if module not in ("lab", "pharmacy", "command", "devices"):
            assert "sections" not in item, module


def test_seeded_effective_config_carries_groups(client):
    payload = _effective(client, "navigation", "main")
    by_module = {i["module"]: i for i in payload["items"]}
    # Module list itself is unchanged (test_realignment pins it too).
    assert by_module["lab"]["group"] == "Diagnostics & Pharmacy"
    assert by_module["pharmacy"]["group"] == "Diagnostics & Pharmacy"
    assert by_module["studio"]["group"] == "System"
    assert by_module["lab"]["sections"] == LAB_SECTIONS
    assert by_module["pharmacy"]["sections"] == PHARMACY_SECTIONS
    assert by_module["command"]["sections"] == COMMAND_SECTIONS
    assert by_module["devices"]["sections"] == DEVICES_SECTIONS


def test_validator_accepts_group_and_sections():
    validate_navigation({"items": [
        {"module": "lab", "label": "Laboratory", "roles": ["admin"],
         "group": "Laboratory", "sections": LAB_SECTIONS},
        {"module": "patients", "label": "Patients", "group": "Patient Care"},
        {"module": "billing", "label": "Billing"},
    ]})


def test_validator_rejects_section_with_page_key_400(client):
    payload = {"items": [
        {"module": "lab", "label": "Laboratory",
         "sections": [{"label": "Bad", "section": "bad", "page": "orders"}]},
    ]}
    # Direct validator: status_code must be exactly 400 (modules-only rule).
    try:
        validate_navigation(payload)
        raise AssertionError("expected ConfigPayloadError")
    except ConfigPayloadError as exc:
        assert exc.status_code == 400
    # And through the API the create is refused with 400.
    r = client.post("/studio/config/navigation/main", json=payload, headers=ADMIN)
    assert r.status_code == 400, r.text


def test_validator_rejects_empty_group():
    try:
        validate_navigation({"items": [
            {"module": "lab", "label": "Laboratory", "group": ""}]})
        raise AssertionError("expected ConfigPayloadError")
    except ConfigPayloadError:
        pass
    try:
        validate_navigation({"items": [
            {"module": "lab", "label": "Laboratory", "group": "   "}]})
        raise AssertionError("expected ConfigPayloadError")
    except ConfigPayloadError:
        pass


def test_validator_rejects_bad_section_slug():
    for bad in ("Orders", "blood_bank", "-qc", "qc!", ""):
        try:
            validate_navigation({"items": [
                {"module": "lab", "label": "Laboratory",
                 "sections": [{"label": "X", "section": bad}]}]})
            raise AssertionError(f"expected rejection for slug {bad!r}")
        except ConfigPayloadError:
            pass


def test_regrouping_is_config_only(client):
    payload = _effective(client, "navigation", "main")
    for item in payload["items"]:
        if item["module"] == "lab":
            item["group"] = "Diagnostics"
    _publish_config(client, "navigation", "main", payload)
    eff = _effective(client, "navigation", "main")
    by_module = {i["module"]: i for i in eff["items"]}
    assert by_module["lab"]["group"] == "Diagnostics"
    # Everything else is untouched.
    assert by_module["pharmacy"]["group"] == "Diagnostics & Pharmacy"
