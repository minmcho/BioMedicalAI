"""Home & Module UX pass (WS1a) — backend shell config.

Pins: the NAV REGROUP (10 groups of 2–6, all 31 modules exactly
once, roles unchanged), the optional nav purpose/icon fields and
their validation, the branding theme validation, and the
job-profiles config (seed, validation, effective-menu resolution).

Synthetic data only.
"""
import pytest

from app import studio_config
from app.configstore import (
    ConfigPayloadError,
    NAV_GROUP_ORDER,
    _DEFAULT_NAV_MODULES,
    caller_job_role,
    default_branding_payload,
    default_job_profiles_payload,
    default_navigation_payload,
    effective_modules,
    ensure_job_profiles_default,
    published_config,
    validate_branding,
    validate_job_profiles,
    validate_navigation,
)
from app.db import Base, make_engine, make_session_factory
from app.models import (ConfigStatus, Role, StaffMember, StudioConfig,
                        User, UserAccount)
from tests.conftest import ADMIN

EXPECTED_GROUPS = {
    "patients": "Patient Care", "inbox": "Patient Care",
    "portal": "Patient Care",
    "scheduling": "Access & Revenue", "billing": "Access & Revenue",
    "ed": "Clinical", "radiology": "Clinical", "surgery": "Clinical",
    "lab": "Diagnostics & Pharmacy", "pharmacy": "Diagnostics & Pharmacy",
    "beds": "Operations", "command": "Operations",
    "er-ops": "Operations", "unit-board": "Operations",
    "clinic": "Operations", "aftercare": "Operations",
    "safety": "Safety & Support", "support": "Safety & Support",
    "inventory": "Safety & Support",
    "workforce": "People & Teams", "teams": "People & Teams",
    "pophealth": "Insights", "reports": "Insights",
    "analytics": "Insights",
    "devices": "Data Platform", "data-platform": "Data Platform",
    "federation": "Data Platform", "research": "Data Platform",
    "documents": "Data Platform",
    "studio": "System", "imports": "System",
}

EXPECTED_ORDER = [
    "patients", "inbox", "portal", "scheduling", "billing",
    "ed", "radiology", "surgery", "lab", "pharmacy",
    "beds", "command", "er-ops", "unit-board", "clinic", "aftercare",
    "safety", "support", "inventory", "workforce", "teams",
    "pophealth", "reports", "analytics",
    "devices", "data-platform", "federation", "research", "documents",
    "studio", "imports",
]


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/ux.db")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    sess = factory()
    studio_config.ensure_default_configs(sess)
    ensure_job_profiles_default(sess)
    yield sess
    sess.close()


def _user(username, role):
    return User(username=username, role=Role(role))


def _staff(session, staff_id, username, job_role):
    row = StaffMember(staff_id=staff_id, username=username,
                      full_name="Synthetic Staffer", job_role=job_role,
                      status="active")
    session.add(row)
    session.commit()
    return row


# ---------------------------------------------------------------------------
# NAV REGROUP
# ---------------------------------------------------------------------------

def test_regroup_groups_2_to_6_all_30_modules_exactly_once():
    payload = default_navigation_payload()
    items = payload["items"]
    assert len(items) == 31
    modules = [i["module"] for i in items]
    assert len(set(modules)) == 31, "every module exactly once"
    by_module = {i["module"]: i for i in items}
    for module, group in EXPECTED_GROUPS.items():
        assert by_module[module]["group"] == group, module
    counts = {}
    for i in items:
        counts[i["group"]] = counts.get(i["group"], 0) + 1
    assert set(counts) == set(NAV_GROUP_ORDER)
    assert all(2 <= n <= 6 for n in counts.values()), counts
    assert payload["groupOrder"] == list(NAV_GROUP_ORDER)


def test_regroup_array_order_is_group_contiguous():
    payload = default_navigation_payload()
    assert [i["module"] for i in payload["items"]] == EXPECTED_ORDER
    assert [i["order"] for i in payload["items"]] == list(range(1, 32))
    seen_groups = []
    for i in payload["items"]:
        if not seen_groups or seen_groups[-1] != i["group"]:
            seen_groups.append(i["group"])
    assert seen_groups == list(NAV_GROUP_ORDER)


def test_regroup_roles_unchanged():
    by_module = {m: roles for m, _l, roles, _g in _DEFAULT_NAV_MODULES}
    assert by_module["patients"] == ["admin", "configurator", "clinician", "viewer"]
    assert by_module["inbox"] == ["admin", "configurator", "clinician"]
    assert by_module["safety"] == ["admin", "configurator", "clinician"]
    assert by_module["portal"] == ["patient"]
    assert by_module["billing"] == ["admin", "configurator", "clinician", "patient"]
    assert by_module["pophealth"] == ["admin", "clinician"]
    assert by_module["studio"] == ["admin", "configurator"]
    assert by_module["imports"] == ["admin", "configurator"]
    assert by_module["analytics"] == ["admin", "configurator", "clinician"]
    assert by_module["documents"] == ["admin", "configurator", "clinician"]


# ---------------------------------------------------------------------------
# purpose / icon
# ---------------------------------------------------------------------------

def test_seed_carries_purpose_and_icon_for_all_30():
    for item in default_navigation_payload()["items"]:
        assert isinstance(item["purpose"], str) and item["purpose"]
        assert len(item["purpose"]) <= 140, item["module"]
        assert isinstance(item["icon"], str) and item["icon"]


def test_validate_navigation_accepts_purpose_and_icon():
    validate_navigation({"items": [
        {"module": "lab", "label": "Laboratory",
         "purpose": "Laboratory orders, results and benches",
         "icon": "flask"}]})


def test_validate_navigation_rejects_bad_purpose_and_icon():
    base = {"module": "lab", "label": "Laboratory"}
    for bad in ({"purpose": 42}, {"purpose": "x" * 141},
                {"purpose": ""}, {"icon": 7}, {"icon": ["flask"]},
                {"icon": "x" * 41}):
        with pytest.raises(ConfigPayloadError):
            validate_navigation({"items": [dict(base, **bad)]})


def test_validate_navigation_still_rejects_non_module_entries_400():
    with pytest.raises(ConfigPayloadError) as exc:
        validate_navigation({"items": [
            {"module": "lab", "label": "Laboratory", "type": "page"}]})
    assert exc.value.status_code == 400
    with pytest.raises(ConfigPayloadError) as exc:
        validate_navigation({"items": [{"label": "No module"}]})
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# BRANDING THEME
# ---------------------------------------------------------------------------

def test_default_branding_payload_unchanged_no_theme():
    assert default_branding_payload() == {
        "orgName": "OpenCare", "portalTitle": "My results",
        "primaryColor": "#0b6e4f"}


def test_validate_branding_accepts_each_theme_field():
    base = dict(default_branding_payload())
    validate_branding(base)  # theme absent = defaults
    validate_branding({**base, "theme": {}})
    validate_branding({**base, "theme": {"density": "compact"}})
    validate_branding({**base, "theme": {"density": "comfortable"}})
    validate_branding({**base, "theme": {"corner": "sharp"}})
    validate_branding({**base, "theme": {"corner": "rounded"}})
    validate_branding({**base, "theme": {"fontScale": "large"}})
    validate_branding({**base, "theme": {"fontScale": "standard"}})
    validate_branding({**base, "theme": {"statusColors": {
        "ok": "#00aa00", "warn": "#ffaa00", "bad": "#cc0000",
        "info": "#0066cc"}}})
    validate_branding({**base, "theme": {
        "density": "compact", "corner": "sharp", "fontScale": "large",
        "statusColors": {"ok": "#00AA00"}}})


def test_validate_branding_rejects_bad_theme_shapes():
    base = dict(default_branding_payload())
    for bad_theme in (
            "dark", [], {"density": "roomy"}, {"density": 3},
            {"corner": "round"}, {"fontScale": "huge"},
            {"statusColors": ["#00aa00"]},
            {"statusColors": {"ok": "green"}},
            {"statusColors": {"ok": "#00aa0"}},
            {"statusColors": {"ok": "#00aa0000"}},
            {"statusColors": {"critical": "#cc0000"}},
            {"unknownField": True},
    ):
        with pytest.raises(ConfigPayloadError):
            validate_branding({**base, "theme": bad_theme})


def test_validate_branding_accepts_group_colors():
    # Rich Design Elevation pass: theme.groupColors is optional and
    # partial — absent = the built-in group palette (the defaults ARE
    # the design); a full map and a one-group partial both validate.
    base = dict(default_branding_payload())
    validate_branding({**base, "theme": {"groupColors": {}}})
    validate_branding({**base, "theme": {
        "groupColors": {"Patient Care": "#0b6e4f"}}})
    validate_branding({**base, "theme": {"groupColors": {
        "Patient Care": "#0B6E4F", "Access & Revenue": "#8A5A13",
        "Clinical": "#1D4E7E", "Diagnostics & Pharmacy": "#5F4B9E",
        "Operations": "#0E6B7A", "Safety & Support": "#8E2A4E",
        "People & Teams": "#4A4E9C", "Insights": "#5C6618",
        "Data Platform": "#41586B", "System": "#5D4037"}}})


def test_validate_branding_rejects_bad_group_colors():
    base = dict(default_branding_payload())
    for bad_group_colors in (
            ["#0b6e4f"],                                  # not an object
            {"Ward Nine": "#0b6e4f"},                     # unknown group
            {"patient care": "#0b6e4f"},                  # case-sensitive
            {"Patient Care": "teal"},                     # not a hex
            {"Patient Care": "#0b6e4"},                   # short hex
            {"Patient Care": "#0b6e4f00"},                # long hex
            {"Patient Care": 123},                        # not a string
    ):
        with pytest.raises(ConfigPayloadError):
            validate_branding(
                {**base, "theme": {"groupColors": bad_group_colors}})


def test_branding_theme_round_trips_through_studio_api(client):
    payload = dict(default_branding_payload())
    payload["theme"] = {"density": "compact", "corner": "sharp",
                        "statusColors": {"bad": "#cc0000"}}
    r = client.post("/studio/config/branding/themed", json=payload,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/studio/config/branding/themed/versions/1/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    eff = client.get("/studio/config/branding/effective/themed",
                     headers=ADMIN)
    assert eff.json()["payload"]["theme"]["density"] == "compact"
    bad = dict(payload, theme={"density": "roomy"})
    r = client.post("/studio/config/branding/themed2", json=bad,
                    headers=ADMIN)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# JOB PROFILES — validation
# ---------------------------------------------------------------------------

def test_job_profiles_seed_covers_catalogue_and_validates():
    payload = default_job_profiles_payload()
    validate_job_profiles(payload)
    validate_navigation(payload)  # navigation kind, profiles shape
    profiles = payload["profiles"]
    for role in ("physician", "nurse", "pharmacist", "lab-technologist",
                 "radiographer", "unit-manager", "department-head",
                 "executive", "admin-staff", "support-staff", "front-desk"):
        assert role in profiles, role
    assert len(profiles["front-desk"]["modules"]) <= 6


def test_job_profiles_validation_rejects_bad_shapes():
    for bad in (
            {"profiles": []},
            {"profiles": {"nurse": []}},
            {"profiles": {"nurse": {"modules": "lab"}}},
            {"profiles": {"nurse": {"modules": [42]}}},
            {"profiles": {"nurse": {"modules": ["not-a-module"]}}},
            {"profiles": {"": {"modules": ["lab"]}}},
    ):
        with pytest.raises(ConfigPayloadError):
            validate_job_profiles(bad)


def test_job_profiles_unknown_job_role_is_allowed():
    validate_job_profiles({"profiles": {
        "chief-of-fun": {"modules": ["patients", "reports"]}}})


def test_job_profiles_seeded_published_v1_at_startup(client):
    r = client.get("/studio/config/navigation/effective/job-profiles",
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1 and body["status"] == "published"
    assert body["payload"] == default_job_profiles_payload()


def test_job_profiles_api_rejects_unknown_module(client):
    r = client.post("/studio/config/navigation/job-profiles",
                    json={"profiles": {"nurse": {"modules": ["nope"]}}},
                    headers=ADMIN)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# JOB PROFILES — effective resolution
# ---------------------------------------------------------------------------

def test_effective_modules_no_staff_record_no_filtering(session):
    modules = effective_modules(session, _user("ghost", "clinician"))
    assert "patients" in modules and "lab" in modules
    assert "studio" not in modules and "portal" not in modules
    assert len(modules) == 28  # 31 - studio - imports - portal


def test_effective_modules_front_desk_intersection(session):
    _staff(session, "staff-fd", "fd-user", "front-desk")
    modules = effective_modules(session, _user("fd-user", "clinician"))
    # Nav order, intersected with the front-desk profile.
    assert modules == ["patients", "inbox", "scheduling", "billing",
                       "clinic", "support"]


def test_effective_modules_via_user_account_staff_link(session):
    _staff(session, "staff-nurse", None, "nurse")
    session.add(UserAccount(username="nurse-acct", full_name="Nurse",
                            role=Role.clinician, staff_id="staff-nurse",
                            status="active"))
    session.commit()
    user = _user("nurse-acct", "clinician")
    assert caller_job_role(session, user) == "nurse"
    modules = effective_modules(session, user)
    assert modules == ["patients", "inbox", "scheduling", "beds",
                       "unit-board", "clinic", "aftercare", "safety"]


def test_effective_modules_admin_exempt_despite_staff_link(session):
    """Operator exemption, username link path (the live defect's
    exact shape): an admin whose sign-in matches a staff record
    with a profiled job role (executive) still gets the FULL admin
    role resolution — no intersection. caller_job_role itself is
    unchanged (it still resolves `executive`); the exemption lives
    in effective_modules, not in the resolver."""
    _staff(session, "staff-011", "dev-admin", "executive")
    user = _user("dev-admin", "admin")
    assert caller_job_role(session, user) == "executive"
    modules = effective_modules(session, user)
    assert modules == effective_modules(session, _user("ghost", "admin"))
    assert "studio" in modules and "lab" in modules
    assert "pharmacy" in modules and "surgery" in modules
    assert len(modules) == 30  # 31 - portal (patient-only)


def test_effective_modules_configurator_exempt_via_account_link(session):
    """Operator exemption, UserAccount staff_id link path: a
    configurator linked to a department-head staff record gets the
    full configurator resolution, not the 8-module profile."""
    _staff(session, "staff-dh", None, "department-head")
    session.add(UserAccount(username="cfg-acct", full_name="Configurator",
                            role=Role.configurator, staff_id="staff-dh",
                            status="active"))
    session.commit()
    user = _user("cfg-acct", "configurator")
    assert caller_job_role(session, user) == "department-head"
    modules = effective_modules(session, user)
    assert modules == effective_modules(
        session, _user("ghost", "configurator"))
    assert "studio" in modules and "lab" in modules
    assert len(modules) == 29  # 31 - portal (patient) - pophealth (admin/clinician)


def test_effective_modules_unknown_job_role_no_filtering(session):
    _staff(session, "staff-x", "x-user", "chief-of-fun")
    modules = effective_modules(session, _user("x-user", "viewer"))
    base = effective_modules(session, _user("ghost", "viewer"))
    assert modules == base and len(modules) > 6


def test_effective_modules_empty_intersection_falls_back(session):
    _staff(session, "staff-doc", "doc-user", "physician")
    # Republish job-profiles with a physician profile that cannot
    # intersect a clinician's nav (studio is admin/configurator only).
    seeded = published_config(session, "navigation", "job-profiles")
    seeded.status = ConfigStatus.superseded
    session.add(StudioConfig(
        kind="navigation", key="job-profiles", version=2,
        status=ConfigStatus.published,
        payload={"profiles": {"physician": {"modules": ["studio"]}}},
        created_by="test", updated_by="test"))
    session.commit()
    modules = effective_modules(session, _user("doc-user", "clinician"))
    assert modules == effective_modules(session, _user("ghost", "clinician"))
    assert "patients" in modules  # fell back, never emptied


def test_effective_modules_patient_unaffected(session):
    _staff(session, "staff-p", "pat-user", "front-desk")
    modules = effective_modules(session, _user("pat-user", "patient"))
    assert modules == ["portal", "billing"]
