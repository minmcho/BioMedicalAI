"""Organisation & Registration Placement pass — WS1 hierarchy core.

Contract coverage for app.org_hierarchy + the seed / manager-op /
read-model changes that ride on it:

- the seeded four-level tree (1 region, 1 facility, 4 departments,
  4 units, bed counts 12/6/0/0) via GET /org-hierarchy;
- validate_placement: happy path, missing level, unknown code,
  broken link, partial mode;
- placement_for_unit / placement_view / resolve_scope;
- the Units & Beds manager ops (add-region, add-department with the
  "parent" alias, add-unit with a department, generalised retire
  children guard) composing into a governed draft that only takes
  effect on publish;
- extend_locations_hierarchy migrating a legacy published payload
  (facility-parented units, no regions/departments) as a new
  published version, idempotently;
- ensure_org_columns on a pre-pass schema + ensure_org_placement
  backfilling staff/account placement from the unit;
- the census is unchanged by the hierarchy seed (18 beds).

Synthetic data only. Fixtures come from conftest.py.
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, select, text

from app import org_hierarchy
from app.configstore import published_config
from app.models import AuditLog, Role, StaffMember, UserAccount
from tests.conftest import ADMIN, CONFIG, PATIENT, VIEWER

FULL_PLACEMENT = {"region": "region-main", "location": "facility-main",
                  "department": "dept-critical-care", "unit": "unit-icu"}


def _session(client):
    return client.app.state.session_factory()


def _published_locations_payload(client):
    versions = client.get("/studio/config/terminology/locations",
                          headers=ADMIN).json()
    published = next(v for v in versions if v["status"] == "published")
    return published["payload"]


def _publish_locations(client, payload):
    r = client.post("/studio/config/terminology/locations",
                    json=payload, headers=CONFIG)
    assert r.status_code in (200, 201), r.text
    version = r.json()["version"]
    pub = client.post(
        f"/studio/config/terminology/locations/versions/{version}/publish",
        headers=CONFIG)
    assert pub.status_code == 200, pub.text
    return version


# ---------------------------------------------------------------------------
# Seeded tree + endpoint
# ---------------------------------------------------------------------------

def test_seeded_tree_shape(client):
    r = client.get("/org-hierarchy", headers=ADMIN)
    assert r.status_code == 200, r.text
    tree = r.json()
    assert tree["version"] == 1
    assert len(tree["regions"]) == 1
    region = tree["regions"][0]
    assert region["code"] == "region-main"
    assert region["display"] == "OpenCare Region"
    assert len(region["locations"]) == 1
    location = region["locations"][0]
    assert location["code"] == "facility-main"
    departments = location["departments"]
    assert [d["code"] for d in departments] == [
        "dept-medicine", "dept-critical-care", "dept-emergency",
        "dept-ambulatory"]
    units = {u["code"]: u for d in departments for u in d["units"]}
    assert set(units) == {"unit-medsurg", "unit-icu", "ed", "clinic"}
    assert units["unit-medsurg"]["bed_count"] == 12
    assert units["unit-icu"]["bed_count"] == 6
    assert units["ed"]["bed_count"] == 0
    assert units["clinic"]["bed_count"] == 0
    by_department = {d["code"]: [u["code"] for u in d["units"]]
                     for d in departments}
    assert by_department == {
        "dept-medicine": ["unit-medsurg"],
        "dept-critical-care": ["unit-icu"],
        "dept-emergency": ["ed"],
        "dept-ambulatory": ["clinic"]}


def test_org_hierarchy_endpoint_gates(client):
    assert client.get("/org-hierarchy").status_code == 401
    assert client.get("/org-hierarchy", headers=PATIENT).status_code == 403
    assert client.get("/org-hierarchy", headers=VIEWER).status_code == 200


def test_census_unchanged_by_hierarchy_seed(client):
    census = client.get("/inpatient/census", headers=ADMIN).json()
    assert census["totals"]["beds_total"] == 18
    per_unit = {u["code"]: u["beds_total"] for u in census["units"]}
    assert per_unit == {"unit-medsurg": 12, "unit-icu": 6,
                        "ed": 0, "clinic": 0}


# ---------------------------------------------------------------------------
# validate_placement
# ---------------------------------------------------------------------------

def test_validate_placement_happy_path(client):
    with _session(client) as session:
        assert org_hierarchy.validate_placement(
            session, dict(FULL_PLACEMENT)) == FULL_PLACEMENT


def test_validate_placement_missing_level_names_it(client):
    placement = {k: v for k, v in FULL_PLACEMENT.items()
                 if k != "department"}
    with _session(client) as session:
        with pytest.raises(HTTPException) as excinfo:
            org_hierarchy.validate_placement(session, placement)
    assert excinfo.value.status_code == 422
    assert "missing department" in excinfo.value.detail


def test_validate_placement_unknown_code(client):
    placement = {**FULL_PLACEMENT, "unit": "unit-moon"}
    with _session(client) as session:
        with pytest.raises(HTTPException) as excinfo:
            org_hierarchy.validate_placement(session, placement)
    assert excinfo.value.status_code == 422
    assert "unit-moon" in excinfo.value.detail
    assert "unit" in excinfo.value.detail


def test_validate_placement_broken_link_names_the_link(client):
    placement = {**FULL_PLACEMENT, "department": "dept-medicine"}
    with _session(client) as session:
        with pytest.raises(HTTPException) as excinfo:
            org_hierarchy.validate_placement(session, placement)
    assert excinfo.value.status_code == 422
    assert ("unit 'unit-icu' does not belong to "
            "department 'dept-medicine'") in excinfo.value.detail


def test_validate_placement_partial_mode(client):
    with _session(client) as session:
        out = org_hierarchy.validate_placement(
            session, {"unit": "unit-icu", "location": "facility-main"},
            require_all=False)
        assert out == {"region": None, "location": "facility-main",
                       "department": None, "unit": "unit-icu"}
        # A provided pair that does not chain is a 422 in partial
        # mode too (dept-medicine exists, but unit-icu is not in it).
        with pytest.raises(HTTPException) as excinfo:
            org_hierarchy.validate_placement(
                session, {"unit": "unit-icu",
                          "department": "dept-medicine"},
                require_all=False)
        assert excinfo.value.status_code == 422
        # And an unknown provided code is a 422, never a silent pass.
        with pytest.raises(HTTPException) as excinfo:
            org_hierarchy.validate_placement(
                session, {"location": "facility-moon"},
                require_all=False)
        assert excinfo.value.status_code == 422


def test_validate_placement_partial_wrong_location_via_second_facility(client):
    # Give the hierarchy a second facility so a WRONG (but existing)
    # location can be told apart from an unknown one.
    payload = _published_locations_payload(client)
    payload["concepts"] = list(payload["concepts"]) + [{
        "code": "facility-west", "display": "West Clinic",
        "type": "facility", "parent": "region-main", "active": True}]
    _publish_locations(client, payload)
    with _session(client) as session:
        with pytest.raises(HTTPException) as excinfo:
            org_hierarchy.validate_placement(
                session, {"unit": "unit-icu",
                          "location": "facility-west"},
                require_all=False)
    assert excinfo.value.status_code == 422
    assert "does not belong to location 'facility-west'" \
        in excinfo.value.detail


# ---------------------------------------------------------------------------
# placement_for_unit / placement_view / resolve_scope
# ---------------------------------------------------------------------------

def test_placement_for_unit(client):
    with _session(client) as session:
        assert org_hierarchy.placement_for_unit(session, "unit-icu") == \
            FULL_PLACEMENT
        assert org_hierarchy.placement_for_unit(session, "ed") == {
            "region": "region-main", "location": "facility-main",
            "department": "dept-emergency", "unit": "ed"}
        assert org_hierarchy.placement_for_unit(
            session, "unit-moon") is None
        # A non-unit code is not a unit placement.
        assert org_hierarchy.placement_for_unit(
            session, "facility-main") is None


def test_placement_view_displays(client):
    with _session(client) as session:
        view = org_hierarchy.placement_view(session, FULL_PLACEMENT)
    assert view == {
        "region": {"code": "region-main", "display": "OpenCare Region"},
        "location": {"code": "facility-main",
                     "display": "OpenCare Hospital"},
        "department": {"code": "dept-critical-care",
                       "display": "Critical Care"},
        "unit": {"code": "unit-icu", "display": "Intensive Care Unit"}}


def test_resolve_scope(client):
    with _session(client) as session:
        assert org_hierarchy.resolve_scope(session) is None
        assert org_hierarchy.resolve_scope(
            session, region="region-main") == {
                "unit-medsurg", "unit-icu", "ed", "clinic"}
        assert org_hierarchy.resolve_scope(
            session, department="dept-critical-care") == {"unit-icu"}
        assert org_hierarchy.resolve_scope(
            session, location="facility-main") == {
                "unit-medsurg", "unit-icu", "ed", "clinic"}
        assert org_hierarchy.resolve_scope(
            session, unit="ed") == {"ed"}
        with pytest.raises(HTTPException) as excinfo:
            org_hierarchy.resolve_scope(session, region="region-moon")
        assert excinfo.value.status_code == 422
        assert "region-moon" in excinfo.value.detail


# ---------------------------------------------------------------------------
# Manager ops: add-region / add-department / add-unit(department),
# generalised retire guard — draft first, live on publish
# ---------------------------------------------------------------------------

def test_manager_ops_extend_hierarchy_through_draft_and_publish(client):
    composed = client.post("/inpatient/locations/draft", json={
        "operations": [
            {"op": "add-region", "code": "region-west",
             "display": "West Region"},
            {"op": "add-department", "code": "dept-rehab",
             "display": "Rehabilitation", "location": "facility-main"},
            {"op": "add-unit", "code": "unit-rehab",
             "display": "Rehabilitation Unit",
             "department": "dept-rehab"},
        ]}, headers=ADMIN)
    assert composed.status_code == 201, composed.text
    version = composed.json()["version"]
    # Draft only: the live tree does not have the new department yet.
    tree = client.get("/org-hierarchy", headers=ADMIN).json()
    assert [r["code"] for r in tree["regions"]] == ["region-main"]
    published = client.post(
        f"/studio/config/terminology/locations/versions/{version}/publish",
        headers=ADMIN)
    assert published.status_code == 200, published.text
    tree = client.get("/org-hierarchy", headers=ADMIN).json()
    regions = {r["code"]: r for r in tree["regions"]}
    assert regions["region-west"]["locations"] == []
    rehab = next(d for loc in regions["region-main"]["locations"]
                 for d in loc["departments"] if d["code"] == "dept-rehab")
    assert [u["code"] for u in rehab["units"]] == ["unit-rehab"]
    assert rehab["units"][0]["bed_count"] == 0
    # The republished hierarchy validates a placement through the
    # new department/unit, and knows the new region exists.
    with _session(client) as session:
        assert org_hierarchy.validate_placement(session, {
            "region": "region-main", "location": "facility-main",
            "department": "dept-rehab", "unit": "unit-rehab"}) == {
                "region": "region-main", "location": "facility-main",
                "department": "dept-rehab", "unit": "unit-rehab"}
        assert org_hierarchy.validate_placement(
            session, {"region": "region-west"}, require_all=False) == {
                "region": "region-west", "location": None,
                "department": None, "unit": None}


def test_manager_ops_parent_alias_and_validation(client):
    # "parent" is accepted as the add-department facility alias.
    composed = client.post("/inpatient/locations/draft", json={
        "operations": [{"op": "add-department", "code": "dept-west",
                        "display": "West Department",
                        "parent": "facility-main"}]}, headers=ADMIN)
    assert composed.status_code == 201, composed.text
    # Unknown facility for a department -> 422.
    assert client.post("/inpatient/locations/draft", json={
        "operations": [{"op": "add-department", "code": "dept-x",
                        "display": "X", "location": "facility-moon"}],
    }, headers=ADMIN).status_code == 422
    # Unknown department for a unit -> 422.
    assert client.post("/inpatient/locations/draft", json={
        "operations": [{"op": "add-unit", "code": "unit-x",
                        "display": "X", "department": "dept-moon"}],
    }, headers=ADMIN).status_code == 422
    # Duplicate code -> 409.
    assert client.post("/inpatient/locations/draft", json={
        "operations": [{"op": "add-region", "code": "region-main",
                        "display": "Duplicate"}],
    }, headers=ADMIN).status_code == 409
    # The op list in the unknown-op error names the new ops.
    r = client.post("/inpatient/locations/draft", json={
        "operations": [{"op": "explode", "code": "x"}]}, headers=ADMIN)
    assert r.status_code == 422
    assert "add-region" in r.json()["detail"]
    assert "add-department" in r.json()["detail"]


def test_manager_retire_guard_covers_every_level(client):
    # Every concept with active children refuses retirement, not
    # just units (the pre-pass guard covered units only).
    for code in ("region-main", "facility-main", "dept-medicine",
                 "unit-medsurg"):
        r = client.post("/inpatient/locations/draft", json={
            "operations": [{"op": "retire", "code": code}]},
            headers=ADMIN)
        assert r.status_code == 409, (code, r.text)
    # A childless concept retires fine (a bed-less unit here).
    r = client.post("/inpatient/locations/draft", json={
        "operations": [{"op": "retire", "code": "clinic"}]},
        headers=ADMIN)
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Governed migration of a legacy published payload
# ---------------------------------------------------------------------------

def _legacy_payload(client):
    """The pre-pass published shape: facility-parented units, no
    regions, no departments — plus one unit the demo mapping does
    not know (unit-stepdown)."""
    payload = _published_locations_payload(client)
    concepts = []
    for concept in payload["concepts"]:
        concept = dict(concept)
        if concept["type"] in ("region", "department"):
            continue
        if concept["type"] == "facility":
            concept.pop("parent", None)
        if concept["code"] in ("unit-medsurg", "unit-icu"):
            concept["parent"] = "facility-main"
        concepts.append(concept)
    concepts.append({"code": "unit-stepdown", "display": "Step-Down Unit",
                     "type": "unit", "parent": "facility-main",
                     "active": True})
    payload["concepts"] = concepts
    return payload


def test_extend_locations_hierarchy_migrates_legacy_payload(client):
    _publish_locations(client, _legacy_payload(client))
    with _session(client) as session:
        assert org_hierarchy.extend_locations_hierarchy(session) is True
        cfg = published_config(session, "terminology", "locations")
        assert cfg is not None
        concepts = {c["code"]: c for c in cfg.payload["concepts"]}
        # Full chain rebuilt, existing concepts preserved.
        assert concepts["region-main"]["type"] == "region"
        assert concepts["facility-main"]["parent"] == "region-main"
        for dept in ("dept-medicine", "dept-critical-care",
                     "dept-emergency", "dept-ambulatory"):
            assert concepts[dept]["type"] == "department"
            assert concepts[dept]["parent"] == "facility-main"
        assert concepts["unit-medsurg"]["parent"] == "dept-medicine"
        assert concepts["unit-icu"]["parent"] == "dept-critical-care"
        assert concepts["ed"]["parent"] == "dept-emergency"
        assert concepts["clinic"]["parent"] == "dept-ambulatory"
        # Unknown units land in the facility's General department.
        assert concepts["unit-stepdown"]["parent"] == "dept-general"
        assert concepts["dept-general"]["parent"] == "facility-main"
        # Beds are preserved verbatim.
        beds = [c for c in cfg.payload["concepts"]
                if c["type"] == "bed"]
        assert len(beds) == 18
        assert {b["parent"] for b in beds} == {"unit-medsurg", "unit-icu"}
        # And placements validate against the migrated hierarchy.
        assert org_hierarchy.validate_placement(
            session, dict(FULL_PLACEMENT)) == FULL_PLACEMENT
        version_after_extend = cfg.version
        # Idempotent: a complete hierarchy is a no-op.
        assert org_hierarchy.extend_locations_hierarchy(session) is False
        cfg = published_config(session, "terminology", "locations")
        assert cfg.version == version_after_extend


def test_extend_locations_hierarchy_noop_on_seeded_shape(client):
    with _session(client) as session:
        assert org_hierarchy.extend_locations_hierarchy(session) is False
        cfg = published_config(session, "terminology", "locations")
        assert cfg.version == 1


# ---------------------------------------------------------------------------
# Startup ensures: columns for pre-pass DBs + placement backfill
# ---------------------------------------------------------------------------

def test_ensure_org_columns_adds_columns_to_prepass_tables(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/prepass.db")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE staff_members (id INTEGER PRIMARY KEY, "
            "unit VARCHAR(120))"))
        conn.execute(text(
            "CREATE TABLE user_accounts (id INTEGER PRIMARY KEY, "
            "unit VARCHAR(120))"))
    org_hierarchy.ensure_org_columns(engine)
    inspector = inspect(engine)
    for table in ("staff_members", "user_accounts"):
        columns = {c["name"] for c in inspector.get_columns(table)}
        assert {"region", "location", "department"} <= columns
    # Second run is a no-op (no duplicate-column error).
    org_hierarchy.ensure_org_columns(engine)


def test_ensure_org_placement_backfills_from_unit(client):
    factory = client.app.state.session_factory
    with factory() as session:
        session.add(StaffMember(
            staff_id="staff-ws1-1", full_name="Backfill Test, RN",
            job_role="nurse", unit="unit-icu"))
        session.add(UserAccount(
            username="ws1.backfill", full_name="Backfill Test, RN",
            role=Role.clinician, unit="unit-icu"))
        session.commit()
        audits_before = session.scalar(
            select(AuditLog).where(
                AuditLog.action == "org.placement.backfill")
            .order_by(AuditLog.id.desc()).limit(1)) is not None
        count_before = len(session.scalars(select(AuditLog).where(
            AuditLog.action == "org.placement.backfill")).all())
    assert audits_before and count_before >= 1  # the startup backfill

    with factory() as session:
        org_hierarchy.ensure_org_placement(session)
    with factory() as session:
        staff = session.scalars(select(StaffMember).where(
            StaffMember.staff_id == "staff-ws1-1")).first()
        assert staff.region == "region-main"
        assert staff.location == "facility-main"
        assert staff.department == "dept-critical-care"
        account = session.scalars(select(UserAccount).where(
            UserAccount.username == "ws1.backfill")).first()
        assert account.region == "region-main"
        assert account.location == "facility-main"
        assert account.department == "dept-critical-care"
        rows = session.scalars(select(AuditLog).where(
            AuditLog.action == "org.placement.backfill")
            .order_by(AuditLog.id.asc())).all()
        assert len(rows) == count_before + 1
        assert rows[-1].payload["staff_filled"] >= 1
        assert rows[-1].payload["accounts_filled"] >= 1
    # Idempotent: nothing left to fill, no further audit row.
    with factory() as session:
        org_hierarchy.ensure_org_placement(session)
    with factory() as session:
        rows = session.scalars(select(AuditLog).where(
            AuditLog.action == "org.placement.backfill")).all()
        assert len(rows) == count_before + 1
