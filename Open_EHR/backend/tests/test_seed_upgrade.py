"""WS1c — seeded-default upgrade path (studio_config).

Pins the three behaviours from the coordinator ledger plus both
recognition arms:

(1) an untouched legacy seeded default upgrades to the current code
    default as a NEW published version (old row superseded, payload
    intact, seed_upgrade publish audit);
(2) a customised payload is untouched and noted as drift exactly
    once per distinct payload hash;
(3) a fresh DB seeds v1 with the current default and the upgrade
    no-ops on re-run;
(4) provenance-only arm: a legacy payload matching NO fingerprint
    but with system-only seeded provenance upgrades;
(4b) fingerprint-only arm: a legacy payload matching a fingerprint
    but with dirty provenance upgrades;
(5) an unknown payload with no audit history and no fingerprint
    match is untouched + drift noted.

Synthetic data only. Sessions are direct (like
test_ux_shell_config) so a legacy payload can be seeded BEFORE
ensure_default_configs runs — a TestClient app would already have
run the startup seed/upgrade at creation.
"""
import copy

from sqlalchemy import select

from app import studio_config
from app.audit import append_audit
from app.configstore import (BRANDING_KEY, NAVIGATION_KEY,
                             default_branding_payload,
                             default_navigation_payload,
                             list_config_versions, published_config)
from app.db import Base, make_engine, make_session_factory
from app.models import AuditLog, ConfigStatus, Role, StudioConfig, User

# Pre-UX groups (Era D), copied from the task brief — the test builds
# legacy payloads independently of the implementation's registry.
LEGACY_GROUPS = {
    "patients": "Patient Care", "inbox": "Patient Care",
    "scheduling": "Patient Care", "portal": "Patient Care",
    "billing": "Revenue",
    "ed": "Clinical", "radiology": "Clinical", "surgery": "Clinical",
    "lab": "Laboratory", "pharmacy": "Pharmacy",
    "beds": "Operations", "command": "Operations",
    "er-ops": "Operations", "unit-board": "Operations",
    "clinic": "Operations", "aftercare": "Operations",
    "safety": "Operations", "support": "Operations",
    "workforce": "Operations", "teams": "Operations",
    "pophealth": "Data & Platform", "reports": "Data & Platform",
    "imports": "Data & Platform", "devices": "Data & Platform",
    "data-platform": "Data & Platform", "federation": "Data & Platform",
    "research": "Data & Platform", "analytics": "Data & Platform",
    "documents": "Data & Platform",
    "studio": "System Studio",
}

LEGACY_GROUP_ORDER = ["Patient Care", "Revenue", "Clinical", "Laboratory",
                      "Pharmacy", "Operations", "Data & Platform",
                      "System Studio"]


def _open_session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/seed_upgrade.db")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    return factory()


def _legacy_payload(exclude=frozenset()):
    """A legacy seeded default built by transforming the CURRENT
    default: strip purpose/icon, restore the pre-UX (Era D) groups,
    optionally drop modules to emulate an older era."""
    payload = copy.deepcopy(default_navigation_payload())
    items = []
    for item in payload["items"]:
        if item["module"] in exclude:
            continue
        if item["module"] not in LEGACY_GROUPS:
            # A module added after the legacy eras (e.g. inventory)
            # cannot appear in a legacy payload at all.
            continue
        item.pop("purpose", None)
        item.pop("icon", None)
        item["group"] = LEGACY_GROUPS[item["module"]]
        items.append(item)
    payload["items"] = items
    payload["groupOrder"] = list(LEGACY_GROUP_ORDER)
    return payload


def _seed_system_published(session, kind, key, payload, version=1):
    """Seed one published version exactly as _seed_published does:
    system row + create/publish audits carrying seeded_default."""
    row = StudioConfig(kind=kind, key=key, version=version,
                       status=ConfigStatus.published,
                       payload=copy.deepcopy(payload),
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:{kind}/{key}@v{version}",
                 payload={"kind": kind, "key": key, "version": version,
                          "seeded_default": True})
    append_audit(session, actor="system", action="studio.config.publish",
                 entity=f"config:{kind}/{key}@v{version}",
                 payload={"kind": kind, "key": key, "version": version,
                          "superseded_version": None,
                          "seeded_default": True})
    session.commit()
    return row


def _human_publish(session, kind, key, payload):
    """A human publish through the real publish machinery the Studio
    endpoint uses (_publish_row): a draft created by a non-system
    user, published with NO seeded_default flag."""
    versions = list_config_versions(session, kind, key)
    next_version = (versions[-1].version + 1) if versions else 1
    row = StudioConfig(kind=kind, key=key, version=next_version,
                       status=ConfigStatus.draft,
                       payload=copy.deepcopy(payload),
                       created_by="dev-admin", updated_by="dev-admin")
    session.add(row)
    session.flush()
    studio_config._publish_row(
        row, session, User(username="dev-admin", role=Role.admin),
        "studio.config.publish", {})
    return row


def _publish_audits(session, kind, key):
    prefix = f"config:{kind}/{key}@"
    return [r for r in session.scalars(select(AuditLog)).all()
            if r.action == "studio.config.publish"
            and r.entity.startswith(prefix)]


def _seed_upgrade_audits(session, kind, key):
    return [r for r in _publish_audits(session, kind, key)
            if (r.payload or {}).get("seed_upgrade") is True]


def _drift_rows(session, kind, key):
    return [r for r in session.scalars(select(AuditLog)).all()
            if r.action == "studio.config.seed_drift"
            and (r.payload or {}).get("kind") == kind
            and (r.payload or {}).get("key") == key]


# ---------------------------------------------------------------------------
# (1) Legacy upgrades
# ---------------------------------------------------------------------------

def test_legacy_era_d_upgrades(tmp_path):
    session = _open_session(tmp_path)
    legacy = _legacy_payload()
    _seed_system_published(session, "navigation", NAVIGATION_KEY, legacy)

    studio_config.ensure_default_configs(session)

    published = published_config(session, "navigation", NAVIGATION_KEY)
    assert published is not None
    assert published.payload == default_navigation_payload()
    assert published.version == 2
    # Old version row UNCHANGED: payload intact, status superseded.
    versions = list_config_versions(session, "navigation", NAVIGATION_KEY)
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].payload == legacy
    assert versions[0].status == ConfigStatus.superseded
    assert versions[1].status == ConfigStatus.published
    assert versions[1].created_by == "system"
    # Audit contains the seed_upgrade publish.
    upgrades = _seed_upgrade_audits(session, "navigation", NAVIGATION_KEY)
    assert len(upgrades) == 1
    audit = upgrades[0]
    assert audit.actor == "system"
    assert audit.payload["seeded_default"] is True
    assert audit.payload["seed_upgrade"] is True
    assert audit.payload["from_version"] == 1
    assert audit.payload["version"] == 2
    # Idempotent: a second run no-ops.
    studio_config.ensure_default_configs(session)
    versions = list_config_versions(session, "navigation", NAVIGATION_KEY)
    assert [v.version for v in versions] == [1, 2]
    assert published_config(session, "navigation",
                             NAVIGATION_KEY).version == 2
    session.close()


def test_legacy_older_era_upgrades(tmp_path):
    """Era B variant: Era D minus teams / federation / research /
    analytics / documents (the exact gap on Min's Air)."""
    session = _open_session(tmp_path)
    legacy = _legacy_payload(exclude={"teams", "federation", "research",
                                      "analytics", "documents"})
    assert len(legacy["items"]) == 25
    _seed_system_published(session, "navigation", NAVIGATION_KEY, legacy)

    studio_config.ensure_default_configs(session)

    published = published_config(session, "navigation", NAVIGATION_KEY)
    assert published.payload == default_navigation_payload()
    assert published.version == 2
    versions = list_config_versions(session, "navigation", NAVIGATION_KEY)
    assert versions[0].payload == legacy
    assert versions[0].status == ConfigStatus.superseded
    assert len(_seed_upgrade_audits(session, "navigation",
                                    NAVIGATION_KEY)) == 1
    session.close()


# ---------------------------------------------------------------------------
# (2) Customised payload: untouched + drift noted once
# ---------------------------------------------------------------------------

def test_customised_payload_untouched_and_drift_once(tmp_path):
    session = _open_session(tmp_path)
    studio_config.ensure_default_configs(session)
    assert published_config(session, "navigation",
                            NAVIGATION_KEY).version == 1

    customised = copy.deepcopy(default_navigation_payload())
    for item in customised["items"]:
        if item["module"] == "patients":
            item["label"] = "Our Patients"
    _human_publish(session, "navigation", NAVIGATION_KEY, customised)
    assert published_config(session, "navigation",
                            NAVIGATION_KEY).payload == customised

    studio_config.ensure_default_configs(session)
    published = published_config(session, "navigation", NAVIGATION_KEY)
    assert published.payload == customised
    assert published.version == 2
    drift = _drift_rows(session, "navigation", NAVIGATION_KEY)
    assert len(drift) == 1
    assert drift[0].actor == "system"
    assert drift[0].payload.get("payload_hash")

    # Run again: still exactly one drift row, payload unchanged.
    studio_config.ensure_default_configs(session)
    assert published_config(session, "navigation",
                            NAVIGATION_KEY).payload == customised
    assert len(_drift_rows(session, "navigation", NAVIGATION_KEY)) == 1
    session.close()


# ---------------------------------------------------------------------------
# (3) Fresh DB
# ---------------------------------------------------------------------------

def test_fresh_db_seeds_v1_and_upgrade_noops(tmp_path):
    session = _open_session(tmp_path)
    studio_config.ensure_default_configs(session)

    nav = published_config(session, "navigation", NAVIGATION_KEY)
    assert nav.version == 1
    assert nav.payload == default_navigation_payload()
    branding = published_config(session, "branding", BRANDING_KEY)
    assert branding.version == 1
    assert branding.payload == default_branding_payload()

    studio_config.ensure_default_configs(session)
    assert published_config(session, "navigation",
                            NAVIGATION_KEY).version == 1
    assert len(list_config_versions(session, "navigation",
                                    NAVIGATION_KEY)) == 1
    assert len(list_config_versions(session, "branding",
                                    BRANDING_KEY)) == 1
    assert _drift_rows(session, "navigation", NAVIGATION_KEY) == []
    assert _drift_rows(session, "branding", BRANDING_KEY) == []
    assert _seed_upgrade_audits(session, "navigation", NAVIGATION_KEY) == []
    session.close()


# ---------------------------------------------------------------------------
# (4) Provenance-only arm
# ---------------------------------------------------------------------------

def test_provenance_only_arm_upgrades(tmp_path):
    session = _open_session(tmp_path)
    legacy = _legacy_payload()
    # One label changed by a SYSTEM re-seed: the projection no longer
    # matches any known fingerprint, so only (P) can recognise it.
    for item in legacy["items"]:
        if item["module"] == "patients":
            item["label"] = "Patients (legacy)"
    assert studio_config._nav_projection(legacy) not in \
        studio_config._legacy_nav_projections()
    _seed_system_published(session, "navigation", NAVIGATION_KEY, legacy)

    studio_config.ensure_default_configs(session)

    published = published_config(session, "navigation", NAVIGATION_KEY)
    assert published.payload == default_navigation_payload()
    assert published.version == 2
    assert len(_seed_upgrade_audits(session, "navigation",
                                    NAVIGATION_KEY)) == 1
    session.close()


# ---------------------------------------------------------------------------
# (4b) Fingerprint-only arm
# ---------------------------------------------------------------------------

def test_fingerprint_only_arm_upgrades(tmp_path):
    session = _open_session(tmp_path)
    legacy = _legacy_payload()
    # Dirty provenance: the row was NOT created by "system" and its
    # publish audit carries no seeded_default flag — (P) must fail,
    # so only (F) can recognise this payload.
    row = StudioConfig(kind="navigation", key=NAVIGATION_KEY, version=1,
                       status=ConfigStatus.published,
                       payload=copy.deepcopy(legacy),
                       created_by="legacy-import",
                       updated_by="legacy-import")
    session.add(row)
    session.flush()
    append_audit(session, actor="legacy-import",
                 action="studio.config.publish",
                 entity=f"config:navigation/{NAVIGATION_KEY}@v1",
                 payload={"kind": "navigation", "key": NAVIGATION_KEY,
                          "version": 1, "superseded_version": None})
    session.commit()
    versions = list_config_versions(session, "navigation", NAVIGATION_KEY)
    assert not studio_config._is_provenance_clean(
        session, "navigation", NAVIGATION_KEY, versions)
    assert studio_config._matches_legacy_fingerprint(
        "navigation", NAVIGATION_KEY, legacy,
        default_navigation_payload())

    studio_config.ensure_default_configs(session)

    published = published_config(session, "navigation", NAVIGATION_KEY)
    assert published.payload == default_navigation_payload()
    assert published.version == 2
    session.close()


# ---------------------------------------------------------------------------
# (5) Unknown payload, no audit history
# ---------------------------------------------------------------------------

def test_unknown_payload_no_history_untouched_drift(tmp_path):
    session = _open_session(tmp_path)
    unknown = {"items": [{"module": "patients", "label": "Totally Custom",
                          "roles": ["admin"], "group": "Custom"}],
               "groupOrder": ["Custom"]}
    row = StudioConfig(kind="navigation", key=NAVIGATION_KEY, version=1,
                       status=ConfigStatus.published,
                       payload=copy.deepcopy(unknown),
                       created_by="external-import",
                       updated_by="external-import")
    session.add(row)
    session.commit()
    # No audit history at all for this key.
    assert _publish_audits(session, "navigation", NAVIGATION_KEY) == []

    studio_config.ensure_default_configs(session)

    published = published_config(session, "navigation", NAVIGATION_KEY)
    assert published.payload == unknown
    assert published.version == 1
    assert len(_drift_rows(session, "navigation", NAVIGATION_KEY)) == 1

    studio_config.ensure_default_configs(session)
    assert published_config(session, "navigation",
                            NAVIGATION_KEY).payload == unknown
    assert len(_drift_rows(session, "navigation", NAVIGATION_KEY)) == 1
    session.close()


# ---------------------------------------------------------------------------
# Branding machinery (wired, provenance arm)
# ---------------------------------------------------------------------------

def test_branding_provenance_clean_legacy_upgrades(tmp_path):
    session = _open_session(tmp_path)
    old_branding = {"orgName": "Old Org", "portalTitle": "My results",
                    "primaryColor": "#0b6e4f"}
    _seed_system_published(session, "branding", BRANDING_KEY, old_branding)

    studio_config.ensure_default_configs(session)

    published = published_config(session, "branding", BRANDING_KEY)
    assert published.payload == default_branding_payload()
    assert published.version == 2
    versions = list_config_versions(session, "branding", BRANDING_KEY)
    assert versions[0].payload == old_branding
    assert versions[0].status == ConfigStatus.superseded
    assert len(_seed_upgrade_audits(session, "branding",
                                    BRANDING_KEY)) == 1
    session.close()
