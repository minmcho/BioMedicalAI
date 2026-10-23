"""Organisation & Registration Placement pass — WS2: registration
placement (app.onboarding, app.people, app.accounts,
app.registration) against the governed hierarchy in
app.org_hierarchy (published terminology/locations).

Covers: onboarding create REQUIRES a full valid placement (422s name
the missing levels / the broken link); the provisioned account
copies the staff placement; a stripped placement blocks stage
advance with the placement blocker named until a staff edit fixes
it; people create/list/analytics placement behaviour; account
create inheritance from the linked staff record and PUT chain
revalidation; patient registration's optional registration_location
(facility-validated, carried as a Patient extension + audit +
response echo).

Synthetic data only.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import onboarding
from app.main import create_app
from app.models import StaffMember

from .conftest import ADMIN

MEDSURG = {"region": "region-main", "location": "facility-main",
           "department": "dept-medicine", "unit": "unit-medsurg"}
ICU = {"region": "region-main", "location": "facility-main",
       "department": "dept-critical-care", "unit": "unit-icu"}
CLINIC = {"region": "region-main", "location": "facility-main",
          "department": "dept-ambulatory", "unit": "clinic"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
    paths = {getattr(route, "path", "") for route in app.routes}
    if "/onboarding" not in paths and "/onboarding/" not in paths:
        app.include_router(onboarding.router)
    with TestClient(app) as c:
        with app.state.session_factory() as session:
            onboarding.ensure_onboarding_defaults(session)
        c.app_ref = app
        yield c


def _onboard(client, placement, **overrides):
    body = {"full_name": "Place Pat", "job_role": "front-desk",
            "service_line": "medicine"}
    body.update(placement)
    body.update(overrides)
    return client.post("/onboarding", json=body, headers=ADMIN)


def _advance(client, onboarding_id):
    return client.post(f"/onboarding/{onboarding_id}/advance",
                       headers=ADMIN)


def _create_staff(client, placement, **overrides):
    body = {"full_name": "Placed Person", "job_role": "nurse",
            "service_line": "medicine", "status": "active"}
    body.update(placement)
    body.update(overrides)
    r = client.post("/people/staff", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Onboarding: create requires placement
# ---------------------------------------------------------------------------

def test_onboarding_create_without_placement_422_names_missing(client):
    r = client.post("/onboarding", json={
        "full_name": "No Place", "job_role": "nurse",
        "unit": "unit-medsurg", "service_line": "medicine"},
        headers=ADMIN)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "missing" in detail
    assert "region" in detail and "department" in detail


def test_onboarding_create_broken_chain_422_names_link(client):
    r = _onboard(client, {"region": "region-main",
                          "location": "facility-main",
                          "department": "dept-medicine",
                          "unit": "unit-icu"})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "unit-icu" in detail and "dept-medicine" in detail


def test_onboarding_create_unknown_code_422_names_it(client):
    r = _onboard(client, {"region": "region-main",
                          "location": "facility-main",
                          "department": "dept-moon",
                          "unit": "unit-medsurg"})
    assert r.status_code == 422, r.text
    assert "dept-moon" in r.json()["detail"]


def test_onboarding_happy_path_placement_reaches_account(client):
    r = _onboard(client, CLINIC, full_name="Clinic Clerk")
    assert r.status_code == 201, r.text
    detail = r.json()
    staff = detail["staff"]
    assert staff["region"] == "region-main"
    assert staff["location"] == "facility-main"
    assert staff["department"] == "dept-ambulatory"
    assert staff["unit"] == "clinic"
    assert detail["placement"]["department"]["code"] == "dept-ambulatory"
    assert detail["placement"]["location"]["display"] == "OpenCare Hospital"
    assert detail["blockers"] == []
    oid = detail["record"]["onboarding_id"]
    # List output carries the placement codes too.
    listed = {i["onboarding_id"]: i
              for i in client.get("/onboarding", headers=ADMIN)
              .json()["items"]}
    assert listed[oid]["department"] == "dept-ambulatory"
    assert listed[oid]["region"] == "region-main"
    # Drive credentialing -> privileging -> account (front-desk has
    # no required credentials and an empty privilege template).
    for _ in range(3):
        r = _advance(client, oid)
        assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["record"]["stage"] == "orientation"
    username = detail["account"]["username"]
    accounts = client.get("/accounts", headers=ADMIN).json()["items"]
    account = next(a for a in accounts if a["username"] == username)
    placement = account["placement"]
    assert placement["region"]["code"] == "region-main"
    assert placement["location"]["code"] == "facility-main"
    assert placement["department"]["code"] == "dept-ambulatory"
    assert placement["unit"]["code"] == "clinic"


def test_onboarding_stripped_placement_blocks_advance_until_fixed(client):
    r = _onboard(client, MEDSURG, full_name="Strip Sue",
                 job_role="front-desk")
    assert r.status_code == 201, r.text
    detail = r.json()
    oid = detail["record"]["onboarding_id"]
    staff_id = detail["record"]["staff_id"]
    # Strip the department directly in the DB (a legacy/broken row).
    app = client.app_ref
    with app.state.session_factory() as session:
        row = session.scalars(select(StaffMember).where(
            StaffMember.staff_id == staff_id)).first()
        row.department = None
        session.commit()
    detail = client.get(f"/onboarding/{oid}", headers=ADMIN).json()
    assert any("placement" in b and "department" in b
               for b in detail["blockers"]), detail["blockers"]
    r = _advance(client, oid)
    assert r.status_code == 409, r.text
    assert "placement" in r.json()["detail"]
    assert "department" in r.json()["detail"]
    # The fix is a staff edit carrying a valid placement.
    r = client.put(f"/people/staff/{staff_id}",
                   json={"department": "dept-medicine"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    r = _advance(client, oid)
    assert r.status_code == 200, r.text
    assert r.json()["record"]["stage"] == "privileging"


# ---------------------------------------------------------------------------
# People: create validation, list filters, analytics scope
# ---------------------------------------------------------------------------

def test_people_create_partial_broken_placement_422(client):
    r = client.post("/people/staff", json={
        "full_name": "Broken Chain", "job_role": "nurse",
        "region": "region-main", "location": "facility-main",
        "department": "dept-medicine", "unit": "unit-icu"},
        headers=ADMIN)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "unit-icu" in detail and "dept-medicine" in detail


def test_people_create_and_list_filter_by_department(client):
    med = _create_staff(client, MEDSURG, full_name="Med Nurse")
    _create_staff(client, ICU, full_name="Icu Nurse")
    assert med["region"] == "region-main"
    assert med["department"] == "dept-medicine"
    r = client.get("/people/staff?department=dept-medicine",
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items and all(i["department"] == "dept-medicine"
                         for i in items)
    ids = {i["staff_id"] for i in items}
    assert med["staff_id"] in ids
    r = client.get("/people/staff?region=region-main&department="
                   "dept-critical-care", headers=ADMIN)
    items = r.json()["items"]
    assert items and all(i["department"] == "dept-critical-care"
                         for i in items)
    assert med["staff_id"] not in {i["staff_id"] for i in items}


def test_people_update_broken_effective_placement_422(client):
    staff = _create_staff(client, MEDSURG, full_name="Shift Shifter")
    r = client.put(f"/people/staff/{staff['staff_id']}",
                   json={"department": "dept-critical-care"},
                   headers=ADMIN)
    assert r.status_code == 422, r.text
    assert "dept-critical-care" in r.json()["detail"]
    # A consistent department+unit move succeeds.
    r = client.put(f"/people/staff/{staff['staff_id']}",
                   json={"department": "dept-critical-care",
                         "unit": "unit-icu"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["department"] == "dept-critical-care"
    assert r.json()["unit"] == "unit-icu"


def test_people_analytics_department_scope(client):
    r = client.get("/people/analytics/units?"
                   "department_id=dept-critical-care", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {i["unit"] for i in body["items"]} == {"unit-icu"}
    assert body["scope"]["department"] == "dept-critical-care"
    assert body["scope"]["region"] is None
    r = client.get("/people/analytics/overview?"
                   "department_id=dept-medicine", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"]["department"] == "dept-medicine"
    assert body["beds_total"] == 12  # unit-medsurg's beds only
    # Unscoped behaviour is unchanged (no scope key).
    body = client.get("/people/analytics/overview",
                      headers=ADMIN).json()
    assert "scope" not in body
    assert body["beds_total"] == 18


def test_people_analytics_unknown_department_422(client):
    r = client.get("/people/analytics/units?department_id=dept-nope",
                   headers=ADMIN)
    assert r.status_code == 422, r.text
    assert "dept-nope" in r.json()["detail"]
    r = client.get("/people/analytics/overview?region_id=region-nope",
                   headers=ADMIN)
    assert r.status_code == 422, r.text
    assert "region-nope" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Accounts: inheritance + PUT revalidation
# ---------------------------------------------------------------------------

def test_account_create_inherits_staff_placement(client):
    staff = _create_staff(client, ICU, full_name="Icu Account Nurse")
    r = client.post("/accounts", json={
        "username": "icu.nurse.acct", "full_name": "Icu Account Nurse",
        "role": "clinician", "staff_id": staff["staff_id"]},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    placement = r.json()["placement"]
    assert placement["region"]["code"] == "region-main"
    assert placement["location"]["code"] == "facility-main"
    assert placement["department"]["code"] == "dept-critical-care"
    assert placement["unit"]["code"] == "unit-icu"
    assert r.json()["unit"] == "unit-icu"
    # Detail GET shows the same placement.
    got = client.get(f"/accounts/{r.json()['account_id']}",
                     headers=ADMIN).json()
    assert got["placement"]["department"]["code"] == "dept-critical-care"


def test_account_create_explicit_placement_and_put_broken_chain(client):
    r = client.post("/accounts", json={
        "username": "placed.acct", "full_name": "Placed Account",
        "role": "viewer", **MEDSURG}, headers=ADMIN)
    assert r.status_code == 201, r.text
    account_id = r.json()["account_id"]
    assert r.json()["placement"]["department"]["code"] == "dept-medicine"
    # PUT a department that breaks the chain with the stored unit.
    r = client.put(f"/accounts/{account_id}",
                   json={"department": "dept-critical-care"},
                   headers=ADMIN)
    assert r.status_code == 422, r.text
    assert "dept-critical-care" in r.json()["detail"]
    # A consistent move (department + unit together) succeeds.
    r = client.put(f"/accounts/{account_id}",
                   json={"department": "dept-critical-care",
                         "unit": "unit-icu"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["placement"]["department"]["code"] \
        == "dept-critical-care"
    assert r.json()["placement"]["unit"]["code"] == "unit-icu"


def test_account_create_broken_placement_422(client):
    r = client.post("/accounts", json={
        "username": "broken.acct", "full_name": "Broken Account",
        "role": "viewer", "region": "region-main",
        "location": "facility-main", "department": "dept-medicine",
        "unit": "unit-icu"}, headers=ADMIN)
    assert r.status_code == 422, r.text
    assert "unit-icu" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Patient registration: registration_location
# ---------------------------------------------------------------------------

def _register(client, **overrides):
    body = {"family": "Placereg", "given": "One",
            "birthDate": "1990-04-02", "gender": "female"}
    body.update(overrides)
    return client.post("/registration/patients", json=body,
                       headers=ADMIN)


def test_registration_with_location_extension_and_echo(client):
    r = _register(client, registration_location="facility-main")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["registration_location"] == "facility-main"
    patient = client.get(f"/fhir/Patient/{body['id']}",
                         headers=ADMIN).json()
    extensions = patient.get("extension") or []
    assert {"url": "urn:opencare:registration-location",
            "valueString": "facility-main"} in extensions


def test_registration_bogus_location_422_names_field(client):
    r = _register(client, given="Two",
                  registration_location="facility-nope")
    assert r.status_code == 422, r.text
    assert "registration_location" in r.json()["detail"]
    # A unit code is not a facility.
    r = _register(client, given="Three",
                  registration_location="unit-icu")
    assert r.status_code == 422, r.text
    assert "registration_location" in r.json()["detail"]


def test_registration_without_location_unchanged(client):
    r = _register(client, given="Four")
    assert r.status_code == 201, r.text
    body = r.json()
    assert "registration_location" not in body
    patient = client.get(f"/fhir/Patient/{body['id']}",
                         headers=ADMIN).json()
    urls = [e.get("url") for e in patient.get("extension") or []]
    assert "urn:opencare:registration-location" not in urls
