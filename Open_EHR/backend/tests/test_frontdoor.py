"""Front Door & Master Data pass — accounts & dev sign-in, patient
registration + FHIR update, staff edit, the Units & Beds draft
manager, unit-first home (my unit / my patients / attending),
front-desk audience + home, unit-scoped dashboards, and the HIPAA
unique-user-identification register entry.

Baseline entering this pass (parent-verified): backend 492.
Everything here runs on synthetic data against the per-test tmp
SQLite app, exactly like the earlier phases.
"""
from datetime import datetime, timedelta, timezone

from .conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER

FRD = {"Authorization": "Bearer frontdesk-token"}


def _audit(client, action):
    """Audit rows for an action, read from the table directly: the
    /studio/audit read surface deliberately exposes hashes and
    actions, not payloads — payload assertions (changed-field names,
    no PHI values) belong against the stored rows."""
    from sqlalchemy import select

    from app.models import AuditLog
    factory = client.app.state.session_factory
    with factory() as session:
        rows = session.scalars(select(AuditLog).where(
            AuditLog.action == action)).all()
        return [{"actor": r.actor, "entity": r.entity,
                 "payload": r.payload} for r in rows]


def _register(client, headers=CLINICIAN, **overrides):
    payload = {"family": "Lovelace", "given": "Ada",
               "birthDate": "1990-05-04", "gender": "female",
               "phone": "+1 555-0101", "email": "ada@example.org"}
    payload.update(overrides)
    return client.post("/registration/patients", json=payload, headers=headers)


def _admit(client, patient_ref, bed, attending=None):
    payload = {"patient": patient_ref, "bed": bed}
    if attending:
        payload["attending_staff_id"] = attending
    resp = client.post("/inpatient/admissions", json=payload, headers=ADMIN)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _verify_staff_for_privileges(client, staff_id):
    """Wave A (WF-B1) fixture: attending assignment now requires the
    attending's licence to be primary-source verified with the
    demanded exclusion kinds clear — record that PSV for a SEEDED
    staff member (licence found via the credentials list)."""
    today = datetime.now(timezone.utc).date().isoformat()
    items = client.get("/people/credentials", headers=ADMIN).json()["items"]
    held = [i for i in items if i["staff_id"] == staff_id
            and i["type"] == "medical-licence"]
    assert held, f"seeded staff {staff_id} has no medical licence"
    latest = max(held, key=lambda i: i["expires_on"])
    resp = client.post(
        f"/people/credentials/{latest['credential_id']}/verifications",
        json={"source_code": "state-licence-board",
              "method": "direct-source-query", "outcome": "verified",
              "verified_on": today, "evidence": "SYN-CONF-1"},
        headers=ADMIN)
    assert resp.status_code == 201, resp.text
    for kind in ("licence-board", "oig", "sam"):
        resp = client.post(
            f"/people/staff/{staff_id}/exclusion-checks",
            json={"kind": kind, "outcome": "clear",
                  "checked_on": today, "evidence": "SYN-EV-1"},
            headers=ADMIN)
        assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# 1. Accounts & dev sign-in
# ---------------------------------------------------------------------------

def test_builtin_accounts_seeded_and_tokens_still_work(client):
    resp = client.get("/accounts", headers=ADMIN)
    assert resp.status_code == 200
    usernames = {a["username"] for a in resp.json()["items"]}
    assert {"dev-admin", "dev-configurator", "dev-clinician",
            "dev-viewer", "dev-patient", "dev-frontdesk"} <= usernames
    # The pre-existing dev tokens keep authenticating byte-identically.
    for headers in (ADMIN, CONFIG, CLINICIAN, VIEWER, FRD):
        assert client.get("/people/home-dashboard",
                          headers=headers).status_code == 200
    # The patient token authenticates too (its 403 here is the
    # workforce.read capability gate, not an auth failure).
    assert client.get("/people/home-dashboard",
                      headers=PATIENT).status_code == 403


def test_account_create_returns_dev_token_once_and_it_authenticates(client):
    resp = client.post("/accounts", json={
        "username": "newclerk", "full_name": "Nia Clerk", "role": "viewer",
        "unit": "clinic", "service_line": "medicine",
        "email": "nia@example.org", "phone": "+1 555-0142",
    }, headers=ADMIN)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    token = body["dev_token"]
    assert token.startswith("acct-newclerk-")
    assert "shown once" in body["dev_token_note"]
    # The token is NOT readable back from the record afterwards.
    got = client.get(f"/accounts/{body['account_id']}", headers=ADMIN)
    assert got.status_code == 200
    assert "dev_token" not in got.json()
    assert got.json()["username"] == "newclerk"
    # ...but it authenticates, with the account's role.
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/people/home-dashboard",
                      headers=headers).status_code == 200
    # viewer role: accounts administration itself stays forbidden.
    assert client.get("/accounts", headers=headers).status_code == 403
    assert _audit(client, "accounts.create")


def test_account_create_validation_and_conflicts(client):
    assert client.post("/accounts", json={
        "username": "dev-admin", "full_name": "Dup", "role": "viewer",
    }, headers=ADMIN).status_code == 409
    assert client.post("/accounts", json={
        "username": "ok1", "full_name": "Bad Email", "role": "viewer",
        "email": "not-an-email",
    }, headers=ADMIN).status_code == 422
    assert client.post("/accounts", json={
        "username": "ok2", "full_name": "Bad Role", "role": "superuser",
    }, headers=ADMIN).status_code == 422
    assert client.post("/accounts", json={
        "username": "ok3", "full_name": "Bad Unit", "role": "viewer",
        "unit": "unit-moon",
    }, headers=ADMIN).status_code == 422
    assert client.post("/accounts", json={
        "username": "ok4", "full_name": "Bad Staff", "role": "viewer",
        "staff_id": "staff-999",
    }, headers=ADMIN).status_code == 422
    assert client.post("/accounts", json={
        "username": "has space", "full_name": "Space", "role": "viewer",
    }, headers=ADMIN).status_code == 422
    # accounts.manage is admin-only.
    assert client.post("/accounts", json={
        "username": "ok5", "full_name": "Nope", "role": "viewer",
    }, headers=CLINICIAN).status_code == 403
    assert client.get("/accounts", headers=VIEWER).status_code == 403


def test_account_update_disable_enable_lifecycle(client):
    created = client.post("/accounts", json={
        "username": "lifecycle", "full_name": "Liv Cycle", "role": "viewer",
        "phone": "+1 555-0100",
    }, headers=ADMIN).json()
    account_id = created['account_id']
    token = created["dev_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/people/home-dashboard",
                      headers=headers).status_code == 200

    updated = client.put(f"/accounts/{account_id}", json={
        "phone": "+1 555-0199", "full_name": "Liv C. Cycle",
    }, headers=ADMIN)
    assert updated.status_code == 200
    assert updated.json()["phone"] == "+1 555-0199"
    rows = _audit(client, "accounts.update")
    assert rows and set(rows[0]["payload"]["changed_fields"]) == {
        "phone", "full_name"}
    # The username is the identity anchor: it never changes.
    assert client.put(f"/accounts/{account_id}", json={
        "username": "renamed",
    }, headers=ADMIN).status_code == 422

    disabled = client.post(f"/accounts/{account_id}/disable", headers=ADMIN)
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    # A disabled account's token stops authenticating immediately.
    assert client.get("/people/home-dashboard",
                      headers=headers).status_code == 401
    assert client.post(f"/accounts/{account_id}/disable",
                       headers=ADMIN).status_code == 409
    enabled = client.post(f"/accounts/{account_id}/enable", headers=ADMIN)
    assert enabled.status_code == 200
    assert client.get("/people/home-dashboard",
                      headers=headers).status_code == 200
    assert _audit(client, "accounts.disable")
    assert _audit(client, "accounts.enable")


def test_account_staff_link_sets_directory_username(client):
    resp = client.post("/accounts", json={
        "username": "priya.account", "full_name": "James Okafor, MD",
        "role": "clinician", "staff_id": "staff-002",
    }, headers=ADMIN)
    assert resp.status_code == 201, resp.text
    staff = client.get("/people/staff/staff-002", headers=ADMIN).json()
    assert staff["username"] == "priya.account"


# ---------------------------------------------------------------------------
# 2. Patient registration + FHIR update
# ---------------------------------------------------------------------------

def test_registration_creates_patient_with_config_mrn(client):
    resp = _register(client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["mrn"] == "MRN-000001"
    assert body["name"][0]["family"] == "Lovelace"
    assert body["duplicates_confirmed"] is False
    second = _register(client, family="Hopper", given="Grace",
                        birthDate="1985-12-09", email="grace@example.org")
    assert second.status_code == 201
    assert second.json()["mrn"] == "MRN-000002"
    assert _audit(client, "registration.patient.create")


def test_registration_typed_validation_rejects_bad_values(client):
    assert _register(client, birthDate="2026-02-30").status_code == 422
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)) \
        .date().isoformat()
    assert _register(client, birthDate=tomorrow).status_code == 422
    assert _register(client, email="ada@").status_code == 422
    assert _register(client, phone="call me maybe").status_code == 422
    assert _register(client, family="").status_code == 422
    assert _register(client, gender="unspecified").status_code == 422
    assert _register(client, emergency_contact={"phone": "+1 555-7777"}) \
        .status_code == 422  # phone without the contact's name
    assert _register(client, emergency_contact={
        "name": "Mum", "phone": "call me"}) \
        .status_code == 422  # the contact's phone is format-checked too


def test_registration_duplicate_warning_requires_confirm(client):
    first = _register(client)
    assert first.status_code == 201
    check = client.post("/registration/patients/check-duplicates", json={
        "family": "Lovelace", "given": "Ada", "birthDate": "1990-05-04",
    }, headers=CLINICIAN)
    assert check.status_code == 200
    assert check.json()["total"] == 1
    assert check.json()["candidates"][0]["id"] == first.json()["id"]
    blocked = _register(client)
    assert blocked.status_code == 409
    assert blocked.json()["candidates"][0]["id"] == first.json()["id"]
    confirmed = _register(client, confirm_duplicate=True)
    assert confirmed.status_code == 201


def test_registration_manual_mrn_system_identifier_is_respected(client):
    resp = _register(client, identifiers=[
        {"system": "urn:opencare:mrn", "value": "EXT-99"}])
    assert resp.status_code == 201, resp.text
    assert resp.json()["mrn"] == "EXT-99"
    # The generated sequence is untouched by the manual value.
    nxt = _register(client, family="Hopper", given="Grace",
                    birthDate="1985-12-09", email="grace@example.org")
    assert nxt.json()["mrn"] == "MRN-000001"


def test_fhir_patient_update_round_trip_and_entered_in_error(client):
    created = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": "pat-fd1", "active": True,
        "name": [{"family": "Doe", "given": ["Jane"]}],
        "birthDate": "1988-03-02", "gender": "female",
        "telecom": [{"system": "phone", "value": "+1 555-0111"}],
    }, headers=CLINICIAN)
    assert created.status_code == 201, created.text
    body = created.json()
    body["telecom"] = [{"system": "phone", "value": "+1 555-0222"}]
    updated = client.put("/fhir/Patient/pat-fd1", json=body, headers=CLINICIAN)
    assert updated.status_code == 200, updated.text
    assert updated.json()["meta"]["versionId"] == "2"
    got = client.get("/fhir/Patient/pat-fd1", headers=CLINICIAN).json()
    assert got["telecom"][0]["value"] == "+1 555-0222"
    rows = _audit(client, "fhir.update")
    assert rows and "telecom" in rows[0]["payload"]["changed_fields"]
    # The audit carries field NAMES, never the PHI values themselves.
    assert "555-0222" not in str(rows[0]["payload"])
    # Entered-in-error: active false, never a delete.
    body["active"] = False
    voided = client.put("/fhir/Patient/pat-fd1", json=body, headers=CLINICIAN)
    assert voided.status_code == 200
    assert client.get("/fhir/Patient/pat-fd1",
                      headers=CLINICIAN).json()["active"] is False


def test_fhir_patient_update_guards(client):
    assert client.put("/fhir/Patient/pat-nope", json={
        "resourceType": "Patient", "id": "pat-nope", "active": True,
        "name": [{"family": "Ghost", "given": ["Gus"]}],
        "birthDate": "1990-01-01", "gender": "male",
    }, headers=CLINICIAN).status_code == 404
    client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": "pat-fd2",
        "name": [{"family": "Roe", "given": ["Jo"]}],
    }, headers=CLINICIAN)
    assert client.put("/fhir/Patient/pat-fd2", json={
        "resourceType": "Patient", "id": "pat-other",
    }, headers=CLINICIAN).status_code == 422
    assert client.put("/fhir/Patient/pat-fd2", json={
        "resourceType": "Patient", "id": "pat-fd2",
        "birthDate": "2026-02-30",
    }, headers=CLINICIAN).status_code == 422
    # This pass registers an update route for Patient only.
    assert client.put("/fhir/Encounter/enc-x", json={
        "resourceType": "Encounter", "id": "enc-x",
    }, headers=CLINICIAN).status_code == 405
    # Patients cannot write at all.
    assert client.put("/fhir/Patient/pat-fd2", json={
        "resourceType": "Patient", "id": "pat-fd2",
    }, headers=PATIENT).status_code == 403


# ---------------------------------------------------------------------------
# 3. Staff edit
# ---------------------------------------------------------------------------

def test_staff_update_validates_like_create(client):
    resp = client.put("/people/staff/staff-005", json={
        "unit": "ed", "phone": "+1 555-0150",
    }, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert resp.json()["unit"] == "ed"
    assert resp.json()["phone"] == "+1 555-0150"
    rows = _audit(client, "workforce.staff.update")
    assert rows and set(rows[0]["payload"]["changed_fields"]) == {
        "unit", "phone"}
    assert client.put("/people/staff/staff-005", json={
        "unit": "unit-moon",
    }, headers=ADMIN).status_code == 422
    assert client.put("/people/staff/staff-005", json={
        "email": "not-an-email",
    }, headers=ADMIN).status_code == 422
    assert client.put("/people/staff/staff-005", json={
        "nickname": "Speedy",
    }, headers=ADMIN).status_code == 422
    assert client.put("/people/staff/staff-999", json={
        "unit": "unit-icu",
    }, headers=ADMIN).status_code == 404
    assert client.put("/people/staff/staff-005", json={
        "unit": "unit-icu",
    }, headers=CLINICIAN).status_code == 403


# ---------------------------------------------------------------------------
# 4. Units & Beds manager: governed draft, publish makes it live
# ---------------------------------------------------------------------------

def _census_codes(client):
    census = client.get("/inpatient/census", headers=ADMIN).json()
    return {b["code"] for u in census["units"] for b in u["beds"]}


def test_bed_manager_draft_then_publish(client):
    assert "MS-107A" not in _census_codes(client)
    composed = client.post("/inpatient/locations/draft", json={
        "operations": [{"op": "add-bed", "code": "MS-107A",
                        "display": "Med-Surg 107A", "unit": "unit-medsurg",
                        "room": "107", "bed": "A"}],
    }, headers=ADMIN)
    assert composed.status_code == 201, composed.text
    version = composed.json()["version"]
    assert "published in System Studio" in composed.json()["note"]
    # Nothing is live yet: the census still reads the published version.
    assert "MS-107A" not in _census_codes(client)
    status = client.get("/inpatient/locations/draft-status",
                        headers=ADMIN).json()
    assert status["draft"]["version"] == version
    # A second compose EXTENDS the same draft (Studio one-draft rule).
    again = client.post("/inpatient/locations/draft", json={
        "operations": [{"op": "add-bed", "code": "MS-107B",
                        "display": "Med-Surg 107B", "unit": "unit-medsurg",
                        "room": "107", "bed": "B"}],
    }, headers=ADMIN)
    assert again.json()["version"] == version
    published = client.post(
        f"/studio/config/terminology/locations/versions/{version}/publish",
        headers=ADMIN)
    assert published.status_code == 200, published.text
    codes = _census_codes(client)
    assert "MS-107A" in codes and "MS-107B" in codes
    status = client.get("/inpatient/locations/draft-status",
                        headers=ADMIN).json()
    assert status["draft"] is None


def test_bed_manager_rename_and_retire(client):
    composed = client.post("/inpatient/locations/draft", json={
        "operations": [
            {"op": "rename", "code": "MS-101A",
             "display": "Med-Surg 101A (Cohort)"},
            {"op": "retire", "code": "ICU-206"},
        ],
    }, headers=ADMIN)
    assert composed.status_code == 201, composed.text
    version = composed.json()["version"]
    assert client.post(
        f"/studio/config/terminology/locations/versions/{version}/publish",
        headers=ADMIN).status_code == 200
    locations = client.get("/inpatient/locations",
                           headers=ADMIN).json()["locations"]
    by_code = {loc["code"]: loc for loc in locations}
    assert by_code["MS-101A"]["display"] == "Med-Surg 101A (Cohort)"
    assert by_code["ICU-206"]["active"] is False
    # Retired beds leave the census (history and the resource stay).
    assert "ICU-206" not in _census_codes(client)


def test_bed_manager_validation(client):
    assert client.post("/inpatient/locations/draft", json={
        "operations": [{"op": "add-bed", "code": "NEW-1", "display": "X",
                        "unit": "unit-moon"}],
    }, headers=ADMIN).status_code == 422
    assert client.post("/inpatient/locations/draft", json={
        "operations": [{"op": "add-bed", "code": "MS-101A", "display": "Dup",
                        "unit": "unit-medsurg"}],
    }, headers=ADMIN).status_code == 409
    assert client.post("/inpatient/locations/draft", json={
        "operations": [{"op": "retire", "code": "unit-medsurg"}],
    }, headers=ADMIN).status_code == 409  # unit still has active beds
    assert client.post("/inpatient/locations/draft", json={
        "operations": [{"op": "explode", "code": "MS-101A"}],
    }, headers=ADMIN).status_code == 422
    assert client.post("/inpatient/locations/draft", json={
        "operations": [{"op": "add-bed", "code": "NEW-1", "display": "X",
                        "unit": "unit-medsurg"}],
    }, headers=VIEWER).status_code == 403


# ---------------------------------------------------------------------------
# 5. Unit-first home: my unit, my patients, attending
# ---------------------------------------------------------------------------

def test_my_unit_patients_and_attending_flow(client):
    # WF-B1: both attendings' licences are primary-source verified
    # before the flow assigns / reassigns them.
    _verify_staff_for_privileges(client, "staff-001")
    _verify_staff_for_privileges(client, "staff-002")
    reg = _register(client)
    patient_ref = f"Patient/{reg.json()['id']}"
    admitted = _admit(client, patient_ref, "MS-101A", attending="staff-001")
    encounter_id = admitted["encounter"]["id"]

    mine = client.get("/people/my-patients", headers=CLINICIAN).json()
    assert mine["staff_id"] == "staff-001"
    assert [i["encounter_id"] for i in mine["items"]] == [encounter_id]
    assert mine["items"][0]["patient_name"] == "Ada Lovelace"
    assert mine["items"][0]["discharge_ready"] is False

    unit = client.get("/people/my-unit-patients", headers=CLINICIAN).json()
    assert unit["unit"] == "unit-medsurg"
    assert unit["total"] == 1
    item = unit["items"][0]
    assert item["bed"] == "MS-101A"
    assert item["attending_staff_id"] == "staff-001"
    assert item["attending_name"] == "Maya Chen, MD"

    # Reassignment moves the patient to the new attending's list.
    moved = client.post(f"/inpatient/encounters/{encounter_id}/attending",
                        json={"attending_staff_id": "staff-002"},
                        headers=ADMIN)
    assert moved.status_code == 200, moved.text
    assert moved.json()["attending_staff_id"] == "staff-002"
    assert client.get("/people/my-patients",
                      headers=CLINICIAN).json()["items"] == []
    unit = client.get("/people/my-unit-patients", headers=CLINICIAN).json()
    assert unit["items"][0]["attending_name"] == "James Okafor, MD"
    assert _audit(client, "inpatient.attending.assign")


def test_attending_must_be_a_directory_entry(client):
    reg = _register(client)
    patient_ref = f"Patient/{reg.json()['id']}"
    resp = client.post("/inpatient/admissions", json={
        "patient": patient_ref, "bed": "MS-101A",
        "attending_staff_id": "staff-999",
    }, headers=ADMIN)
    assert resp.status_code == 422


def test_my_unit_patients_no_unit_caller_gets_empty(client):
    # dev-configurator's directory entry has no unit: the home falls
    # back to the house view — no list, nothing invented.
    resp = client.get("/people/my-unit-patients", headers=CONFIG).json()
    assert resp["unit"] is None
    assert resp["items"] == [] and resp["total"] == 0
    mine = client.get("/people/my-patients", headers=CONFIG).json()
    assert mine["items"] == []


# ---------------------------------------------------------------------------
# 6. Front desk: audience, home widgets, register -> book flow
# ---------------------------------------------------------------------------

def test_front_desk_seed_audience_and_config(client):
    staff = client.get("/people/staff", headers=ADMIN).json()
    assert staff["total"] == 19
    entry = next(s for s in staff["items"] if s["staff_id"] == "staff-019")
    assert entry["job_role"] == "front-desk"
    config = client.get("/people/config", headers=ADMIN).json()
    roles = {c["code"] for c in config["concepts"]}
    assert "front-desk" in roles
    assert config["audienceDashboards"]["front-desk"] == "audience-front-desk"
    home = client.get("/people/home-dashboard", headers=FRD).json()
    assert home["audience"] == "front-desk"
    assert home["dashboard_key"] == "audience-front-desk"


def test_front_desk_today_and_register_book_flow(client):
    today0 = client.get("/registration/frontdesk/today", headers=FRD).json()
    assert today0["appointments_total"] == 0
    assert today0["registrations_today"] == 0

    reg = _register(client, headers=FRD)
    assert reg.status_code == 201, reg.text
    patient_ref = f"Patient/{reg.json()['id']}"

    booked = client.post("/continuum/clinic/book", json={
        "patient": patient_ref, "session": "am-clinic",
        "date": datetime.now(timezone.utc).date().isoformat(),
    }, headers=FRD)
    assert booked.status_code == 201, booked.text

    today = client.get("/registration/frontdesk/today", headers=FRD).json()
    assert today["registrations_today"] == 1
    assert today["appointments_total"] == 1
    assert today["awaiting_checkin"] == 1
    assert today["items"][0]["patient_ref"] == patient_ref

    # The desk books and registers; rooming stays a continuum.work
    # act — the boundary is deliberate and visible.
    appt_id = booked.json()["appointment"]["id"]
    assert client.post(f"/continuum/clinic/appointments/{appt_id}/room",
                       json={"room": "Exam 3"},
                       headers=FRD).status_code == 403
    # Patients get neither the desk data nor booking.
    assert client.get("/registration/frontdesk/today",
                      headers=PATIENT).status_code == 403


def test_front_desk_waitlist_count(client):
    reg = _register(client)
    assert reg.status_code == 201, reg.text
    joined = client.post("/access/waitlist", json={
        "patient": reg.json()["id"], "slot_type": "follow-up",
    }, headers=ADMIN)
    assert joined.status_code == 201, joined.text
    today = client.get("/registration/frontdesk/today", headers=FRD).json()
    assert today["waitlist_open"] == 1


# ---------------------------------------------------------------------------
# 7. Unit-scoped dashboards
# ---------------------------------------------------------------------------

def test_unit_scoped_analytics(client):
    reg_a = _register(client)
    _admit(client, f"Patient/{reg_a.json()['id']}", "MS-101A")
    reg_b = _register(client, family="Hopper", given="Grace",
                      birthDate="1985-12-09", email="grace@example.org")
    _admit(client, f"Patient/{reg_b.json()['id']}", "ICU-201")

    house = client.get("/people/analytics/overview", headers=ADMIN).json()
    assert house["occupied"] == 2
    icu = client.get("/people/analytics/overview?unit=unit-icu",
                     headers=ADMIN).json()
    assert icu["unit"] == "unit-icu"
    assert icu["occupied"] == 1
    assert icu["beds_total"] == 6
    assert icu["admissions_today"] == 1
    medsurg = client.get("/people/analytics/overview?unit=unit-medsurg",
                         headers=ADMIN).json()
    assert medsurg["occupied"] == 1
    assert medsurg["staff_active"] >= 3
    clinic = client.get("/people/analytics/overview?unit=clinic",
                        headers=ADMIN).json()
    assert clinic["occupied"] == 0
    # Scoping is real: the clinic sees only its own (smaller) roster.
    assert 1 <= clinic["staff_active"] < medsurg["staff_active"]
    assert client.get("/people/analytics/overview?unit=unit-moon",
                      headers=ADMIN).status_code == 422

    units = client.get("/people/analytics/units?unit=unit-icu",
                       headers=ADMIN).json()
    assert units["total"] == 1 and units["items"][0]["unit"] == "unit-icu"
    coverage = client.get("/people/analytics/coverage?unit=unit-medsurg",
                          headers=ADMIN).json()
    assert coverage["total"] == 1
    series = client.get("/people/analytics/census-series?unit=unit-icu",
                        headers=ADMIN).json()
    assert series["items"][-1]["admissions"] == 1
    assert series["items"][-1]["census"] == 1


def test_home_dashboard_scopes_nurse_and_manager_only(client):
    def endpoints(headers):
        home = client.get("/people/home-dashboard", headers=headers).json()
        return home, [w.get("endpoint", "")
                      for w in home["dashboard"]["widgets"]]

    # dev-viewer: unit-manager on unit-medsurg -> analytics widgets
    # are rewritten with the caller's unit.
    home, eps = endpoints(VIEWER)
    assert home["audience"] == "unit-manager"
    analytics_eps = [e for e in eps if e.startswith("/people/analytics/")]
    assert analytics_eps and all("unit=unit-medsurg" in e
                                 for e in analytics_eps)
    # A nurse sign-in (an account created for staff-003, a nurse on
    # unit-medsurg): audience nurse -> scoped the same way.
    created = client.post("/accounts", json={
        "username": "priya.nurse", "full_name": "Priya Nair, RN",
        "role": "clinician", "staff_id": "staff-003",
    }, headers=ADMIN).json()
    nurse = {"Authorization": f"Bearer {created['dev_token']}"}
    home, eps = endpoints(nurse)
    assert home["audience"] == "nurse"
    assert all("unit=unit-medsurg" in e for e in eps
               if e.startswith("/people/analytics/"))
    # dev-clinician's audience is clinician (a physician home) — the
    # unit rewrite deliberately covers nurse + unit-manager only.
    home, eps = endpoints(CLINICIAN)
    assert home["audience"] == "clinician"
    # dev-admin: executive -> house-wide, untouched.
    home, eps = endpoints(ADMIN)
    assert home["audience"] == "executive"
    assert all("unit=" not in e for e in eps)


# ---------------------------------------------------------------------------
# 8. Compliance: HIPAA unique user identification
# ---------------------------------------------------------------------------

def test_register_gains_unique_user_identification(client):
    reg = client.get("/compliance/requirements", headers=ADMIN).json()
    # Phase 16: the register legitimately grew 27 -> 33 with the
    # GCP/GLP/Part 11 seed entries (research packs' register rows).
    # Workforce, Teams & Panels pass: +1 (CMS 42 CFR 482.22 medical
    # staff — credentialing/privileging, app.teams seed).
    # Phase 17 (WS-SEC): +3 HIPAA rows (CMP-30 key management,
    # CMP-31 snooping monitoring, CMP-32 log redaction) — total
    # 34 -> 37, HIPAA subset 7 -> 10; enumerated on the final tree
    # by the Phase 17 coordinator.
    assert reg["total"] == 37
    hipaa = [i for i in reg["items"] if i["body"] == "HIPAA"]
    assert len(hipaa) == 10
    entry = next(i for i in hipaa
                 if i["citation"] == "45 CFR 164.312(a)(2)(i)")
    assert "unique user identification" in entry["title"].lower()
