"""Inventory & Materials pass (WS-B platform) — governed taxonomy,
capabilities and navigation wiring.

Pins: the "inventory" config kind + validator (the item master may
only ever reference PUBLISHED taxonomy codes), the seeded taxonomy
default (insert-if-absent at startup, never overwriting a site's
republication), the capability defaults, and the navigation seed —
including the seeded-default upgrade path carrying the module card
into an existing database whose seeded nav predates the module.

Synthetic data only.
"""
import copy

import pytest

from app import studio_config
from app.configstore import (
    CAPABILITY_DEFAULTS,
    CONFIG_KINDS,
    INVENTORY_TAXONOMY_KEY,
    NAVIGATION_KEY,
    NAV_MODULE_KEYS,
    ConfigPayloadError,
    default_inventory_taxonomy_payload,
    default_navigation_payload,
    ensure_inventory_defaults,
    list_config_versions,
    published_config,
    validate_payload,
)
from app.db import Base, make_engine, make_session_factory
from app.models import ConfigStatus


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/inventory_platform.db")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    sess = factory()
    studio_config.ensure_default_configs(sess)
    yield sess
    sess.close()


EXPECTED_CATEGORY_CODES = [
    "instruments", "medical-appliances", "surgical-supplies",
    "implants", "consumables", "ppe", "lab-reagents", "linen",
]


def test_inventory_kind_registered():
    assert "inventory" in CONFIG_KINDS
    # validate_payload dispatches through VALIDATORS — an unknown
    # kind raises KeyError there; the seeded default must pass.
    validate_payload("inventory", default_inventory_taxonomy_payload())


def test_default_taxonomy_shape():
    payload = default_inventory_taxonomy_payload()
    codes = [c["code"] for c in payload["categories"]]
    assert codes == EXPECTED_CATEGORY_CODES
    for category in payload["categories"]:
        assert len(category["subcategories"]) >= 4, category["code"]
        sub_codes = [s["code"] for s in category["subcategories"]]
        assert len(sub_codes) == len(set(sub_codes))
    reason_codes = [r["code"] for r in payload["reasonCodes"]]
    assert "recount-correction" in reason_codes
    assert "expired-disposal" in reason_codes
    assert payload["expiryWarningDays"] == 90


def _mutated(mutator):
    payload = copy.deepcopy(default_inventory_taxonomy_payload())
    mutator(payload)
    return payload


@pytest.mark.parametrize("mutator", [
    lambda p: p.update({"surprise": True}),
    lambda p: p.update({"categories": []}),
    lambda p: p["categories"].append(p["categories"][0]),
    lambda p: p["categories"][0].update({"subcategories": []}),
    lambda p: p["categories"][0]["subcategories"].append(
        p["categories"][0]["subcategories"][0]),
    lambda p: p["categories"][0].update({"code": "Not A Slug"}),
    lambda p: p["categories"][0].update({"label": "  "}),
    lambda p: p.update({"reasonCodes": []}),
    lambda p: p["reasonCodes"].append(p["reasonCodes"][0]),
    lambda p: p.update({"expiryWarningDays": 0}),
    lambda p: p.update({"expiryWarningDays": 366}),
    lambda p: p.update({"expiryWarningDays": "90"}),
    lambda p: p.update({"expiryWarningDays": True}),
])
def test_validator_rejects_bad_taxonomies(mutator):
    with pytest.raises(ConfigPayloadError):
        validate_payload("inventory", _mutated(mutator))


def test_ensure_seeds_published_v1_and_is_idempotent(session):
    assert published_config(session, "inventory",
                            INVENTORY_TAXONOMY_KEY) is None
    ensure_inventory_defaults(session)
    row = published_config(session, "inventory", INVENTORY_TAXONOMY_KEY)
    assert row is not None
    assert row.status == ConfigStatus.published
    assert row.version == 1
    assert row.payload == default_inventory_taxonomy_payload()
    ensure_inventory_defaults(session)
    versions = list_config_versions(session, "inventory",
                                    INVENTORY_TAXONOMY_KEY)
    assert len(versions) == 1


def test_capability_defaults():
    assert CAPABILITY_DEFAULTS["inventory.read"] == (
        "admin", "configurator", "clinician", "viewer")
    for capability in ("inventory.manage", "inventory.move"):
        assert CAPABILITY_DEFAULTS[capability] == (
            "admin", "configurator", "clinician")


def test_nav_seed_carries_inventory():
    assert "inventory" in NAV_MODULE_KEYS
    payload = default_navigation_payload()
    items = {i["module"]: i for i in payload["items"]}
    entry = items["inventory"]
    assert entry["label"] == "Inventory & Materials"
    assert entry["group"] == "Safety & Support"
    assert entry["roles"] == ["admin", "configurator", "clinician",
                              "viewer"]
    assert entry["icon"] == "boxes"
    assert entry["purpose"]
    counts: dict[str, int] = {}
    for item in payload["items"]:
        counts[item["group"]] = counts.get(item["group"], 0) + 1
    assert all(2 <= n <= 6 for n in counts.values()), counts


def test_seeded_upgrade_carries_inventory_into_existing_db(tmp_path):
    """An existing database whose published navigation/main is an
    untouched legacy seeded default (the same payload minus the new
    module) gains the Inventory & Materials card through the
    seeded-default upgrade path — no site action, no fork. Uses a
    private session: the shared fixture pre-seeds the CURRENT nav,
    which is exactly the state this test must not start from."""
    engine = make_engine(f"sqlite:///{tmp_path}/inv_upgrade.db")
    Base.metadata.create_all(engine)
    session = make_session_factory(engine)()
    legacy = default_navigation_payload()
    legacy["items"] = [i for i in legacy["items"]
                       if i["module"] != "inventory"]
    studio_config._seed_published(session, "navigation", NAVIGATION_KEY,
                                  legacy)
    before = published_config(session, "navigation", NAVIGATION_KEY)
    assert all(i["module"] != "inventory" for i in before.payload["items"])
    studio_config.upgrade_seeded_default(
        session, "navigation", NAVIGATION_KEY,
        default_navigation_payload())
    after = published_config(session, "navigation", NAVIGATION_KEY)
    modules = [i["module"] for i in after.payload["items"]]
    assert "inventory" in modules
    assert after.version > before.version
    session.close()
