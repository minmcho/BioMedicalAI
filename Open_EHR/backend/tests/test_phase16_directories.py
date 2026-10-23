"""Phase 16 (WS-A1, BD-49) — FHIR Practitioner / Organization /
Consent resources + directory sync (app.directories).

- The three new types ride the generic FHIR surface (app.fhir
  SUPPORTED) with per-type validation in fhir.py's existing style:
  Practitioner requires `name`, Organization requires `name`,
  Consent requires `status` + `scope` + `patient.reference`.
- Directory sync materialises Practitioner from people.StaffMember
  rows (19 seeded) and Organization from the published
  terminology/locations facility + unit concepts (3 seeded: 1
  facility + 2 units; beds are Locations, not Organizations),
  idempotently keyed by identifier (staff_id / location code):
  a second sync creates nothing. `dryRun` returns the plan and
  writes no FHIR resources. A locations republication adding a
  unit adds exactly one Organization on the next sync.

Synthetic data only.
"""
from .conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER


def _publish(client, kind, key, payload, headers=ADMIN):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload,
                    headers=headers)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/{kind}/{key}/versions/{version}/publish",
        headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _fhir_total(client, resource_type):
    r = client.get(f"/fhir/{resource_type}", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["total"]


def _locations_payload(client):
    r = client.get("/studio/config/terminology/effective/locations",
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["payload"]


# ---------------------------------------------------------------------------
# FHIR validation: Practitioner / Organization / Consent
# ---------------------------------------------------------------------------

def test_practitioner_validation_and_crud(client):
    # Missing `name` -> 422 (FHIR requires a name for a Practitioner).
    r = client.post("/fhir/Practitioner", json={
        "resourceType": "Practitioner", "id": "prac-bad",
        "active": True}, headers=ADMIN)
    assert r.status_code == 422, r.text
    assert "name" in r.json()["detail"]
    # Wrong resourceType -> 422.
    r = client.post("/fhir/Practitioner", json={
        "resourceType": "Patient", "id": "prac-bad2",
        "name": [{"text": "Wrong Type"}]}, headers=ADMIN)
    assert r.status_code == 422, r.text
    # Valid create + read + search.
    r = client.post("/fhir/Practitioner", json={
        "resourceType": "Practitioner", "id": "prac-1",
        "identifier": [{"system": "urn:opencare:staff-id",
                        "value": "prac-1"}],
        "active": True,
        "name": [{"text": "Synthetic Practitioner, MD",
                  "family": "Practitioner", "given": ["Synthetic"]}]},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["id"] == "prac-1"
    r = client.get("/fhir/Practitioner/prac-1", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["name"][0]["family"] == "Practitioner"
    assert _fhir_total(client, "Practitioner") == 1
    # Clinician may write (fhir.write); a patient caller sees nothing
    # (directory resources carry no patient_ref -> non-disclosure 404).
    assert client.get("/fhir/Practitioner/prac-1",
                      headers=PATIENT).status_code == 404


def test_organization_validation_and_crud(client):
    r = client.post("/fhir/Organization", json={
        "resourceType": "Organization", "id": "org-bad",
        "active": True}, headers=ADMIN)
    assert r.status_code == 422, r.text
    assert "name" in r.json()["detail"]
    r = client.post("/fhir/Organization", json={
        "resourceType": "Organization", "id": "org-1",
        "identifier": [{"system": "urn:opencare:location-code",
                        "value": "org-1"}],
        "active": True, "name": "Synthetic Hospital"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.get("/fhir/Organization/org-1", headers=VIEWER)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Synthetic Hospital"
    assert _fhir_total(client, "Organization") == 1


def test_consent_validation_and_patient_scoping(client):
    base = {"resourceType": "Consent", "id": "consent-1"}
    # Each required field is enforced in turn.
    r = client.post("/fhir/Consent", json={
        **base, "scope": {"coding": [{"code": "treatment"}]},
        "patient": {"reference": "Patient/pat-1"}}, headers=ADMIN)
    assert r.status_code == 422, r.text  # no status
    r = client.post("/fhir/Consent", json={
        **base, "status": "active",
        "patient": {"reference": "Patient/pat-1"}}, headers=ADMIN)
    assert r.status_code == 422, r.text  # no scope
    r = client.post("/fhir/Consent", json={
        **base, "status": "active",
        "scope": {"coding": [{"code": "treatment"}]}}, headers=ADMIN)
    assert r.status_code == 422, r.text  # no patient
    # Valid Consent for pat-1 (the dev patient's own record).
    r = client.post("/fhir/Consent", json={
        **base, "status": "active",
        "scope": {"coding": [{"system": "http://terminology.hl7.org/"
                                     "CodeSystem/consentscope",
                              "code": "patient-privacy"}]},
        "category": [{"coding": [{"code": "59284-0"}]}],
        "patient": {"reference": "Patient/pat-1"},
        "policyRule": {"coding": [{"code": "OPTIN"}]}}, headers=ADMIN)
    assert r.status_code == 201, r.text
    # A Consent for another patient.
    r = client.post("/fhir/Consent", json={
        "resourceType": "Consent", "id": "consent-2", "status": "active",
        "scope": {"coding": [{"code": "patient-privacy"}]},
        "patient": {"reference": "Patient/pat-2"}}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert _fhir_total(client, "Consent") == 2
    # Patient scoping rides the `patient` projection: the dev patient
    # reads their own Consent, the other patient's is a 404, and a
    # patient search only returns their own.
    assert client.get("/fhir/Consent/consent-1",
                      headers=PATIENT).status_code == 200
    assert client.get("/fhir/Consent/consent-2",
                      headers=PATIENT).status_code == 404
    r = client.get("/fhir/Consent", headers=PATIENT)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1
    assert r.json()["entry"][0]["resource"]["id"] == "consent-1"


# ---------------------------------------------------------------------------
# Directory sync
# ---------------------------------------------------------------------------

def test_sync_gated_and_status_before_sync(client):
    # Clinician holds neither studio.manage nor directories.sync.
    assert client.post("/directories/sync", json={},
                       headers=CLINICIAN).status_code == 403
    assert client.post("/directories/sync", json={},
                       headers=PATIENT).status_code == 403
    # Reads ride workforce.read: viewer may read, patient may not.
    r = client.get("/directories/status", headers=VIEWER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["practitioners"] == {"fhir_total": 0, "source_total": 19}
    # Org-placement pass: the locations seed is a four-level
    # hierarchy (1 region + 1 facility + 4 departments + 4 units),
    # and every level syncs to an Organization: source_total 10.
    assert body["organizations"] == {"fhir_total": 0, "source_total": 10}
    assert body["consents"] == {"fhir_total": 0}
    assert body["sync_runs"] == 0
    assert body["last_sync"] is None
    assert client.get("/directories/status",
                      headers=PATIENT).status_code == 403


def test_sync_creates_then_idempotent(client):
    r = client.post("/directories/sync", json={}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is False
    assert body["audit_ref"]
    assert body["practitioners"]["total"] == 19
    assert body["practitioners"]["create"] == 19
    assert body["practitioners"]["update"] == 0
    # Seeded locations (org-placement pass): the full hierarchy —
    # region-main, facility-main, 4 departments, unit-medsurg,
    # unit-icu, ed, clinic — syncs to 10 Organizations.
    assert body["organizations"]["total"] == 10
    assert body["organizations"]["create"] == 10
    assert _fhir_total(client, "Practitioner") == 19
    assert _fhir_total(client, "Organization") == 10
    # Materialised shapes: identifier is the sync key, name carried.
    r = client.get("/fhir/Practitioner/staff-001", headers=ADMIN)
    assert r.status_code == 200, r.text
    prac = r.json()
    assert prac["identifier"] == [{"system": "urn:opencare:staff-id",
                                   "value": "staff-001"}]
    assert prac["name"][0]["text"] == "Maya Chen, MD"
    assert prac["active"] is True
    r = client.get("/fhir/Organization/unit-icu", headers=ADMIN)
    assert r.status_code == 200, r.text
    org = r.json()
    assert org["name"] == "Intensive Care Unit"
    assert org["identifier"][0]["value"] == "unit-icu"
    # Org-placement pass: a unit's parent in the hierarchy is its
    # DEPARTMENT, which is itself an Organization now, so partOf
    # names the department (previously the facility, when units
    # hung directly under it).
    assert org["partOf"] == {
        "reference": "Organization/dept-critical-care"}
    # Beds did NOT become Organizations.
    assert client.get("/fhir/Organization/MS-101A",
                      headers=ADMIN).status_code == 404
    # Status reflects the sync + its audit ref.
    r = client.get("/directories/status", headers=ADMIN)
    status = r.json()
    assert status["practitioners"]["fhir_total"] == 19
    assert status["organizations"]["fhir_total"] == 10
    assert status["sync_runs"] == 1
    assert status["last_sync"]["audit_ref"] == body["audit_ref"]
    assert status["last_sync"]["practitioners"]["created"] == 19
    # Directory list endpoints render the stored resources.
    assert client.get("/directories/practitioners",
                      headers=ADMIN).json()["total"] == 19
    assert client.get("/directories/organizations",
                      headers=ADMIN).json()["total"] == 10
    # Second sync: no duplicates, everything unchanged.
    r = client.post("/directories/sync", json={}, headers=CONFIG)
    assert r.status_code == 200, r.text
    body2 = r.json()
    assert body2["practitioners"]["create"] == 0
    assert body2["practitioners"]["update"] == 0
    assert body2["practitioners"]["unchanged"] == 19
    assert body2["organizations"]["create"] == 0
    assert body2["organizations"]["unchanged"] == 10
    assert _fhir_total(client, "Practitioner") == 19
    assert _fhir_total(client, "Organization") == 10
    # Versions did not move on an unchanged re-sync.
    r = client.get("/fhir/Practitioner/staff-001", headers=ADMIN)
    assert r.json()["meta"]["versionId"] == "1"


def test_sync_updates_after_staff_change(client):
    assert client.post("/directories/sync", json={},
                       headers=ADMIN).status_code == 200
    # A new directory entry appears as a new Practitioner...
    r = client.post("/people/staff", json={
        "staff_id": "staff-900", "full_name": "New Joiner, RN",
        "job_role": "nurse", "unit": "unit-icu",
        "service_line": "critical-care"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/directories/sync", json={}, headers=ADMIN)
    body = r.json()
    assert body["practitioners"]["create"] == 1
    assert body["practitioners"]["unchanged"] == 19
    assert _fhir_total(client, "Practitioner") == 20
    # ...and editing the entry updates the SAME resource (keyed by
    # staff_id identifier) — no duplicate, version bumps.
    r = client.put("/people/staff/staff-900", json={
        "full_name": "Renamed Joiner, RN"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    r = client.post("/directories/sync", json={}, headers=ADMIN)
    body = r.json()
    assert body["practitioners"]["create"] == 0
    assert body["practitioners"]["update"] == 1
    assert _fhir_total(client, "Practitioner") == 20
    r = client.get("/fhir/Practitioner/staff-900", headers=ADMIN)
    assert r.json()["name"][0]["text"] == "Renamed Joiner, RN"
    assert r.json()["meta"]["versionId"] == "2"


def test_sync_dry_run_writes_nothing(client):
    r = client.post("/directories/sync", json={"dryRun": True},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["practitioners"]["create"] == 19
    assert body["organizations"]["create"] == 10
    assert body["audit_ref"]  # the dry run itself is audited
    # Nothing was written: no FHIR resources, no sync-run row.
    assert _fhir_total(client, "Practitioner") == 0
    assert _fhir_total(client, "Organization") == 0
    status = client.get("/directories/status", headers=ADMIN).json()
    assert status["sync_runs"] == 0
    assert status["practitioners"]["fhir_total"] == 0
    # The plan the dry run promised is the plan the real sync runs.
    r = client.post("/directories/sync", json={}, headers=ADMIN)
    assert r.json()["practitioners"]["create"] == 19
    assert _fhir_total(client, "Practitioner") == 19
    # A dry run after a real sync (with a new staff member pending)
    # still writes nothing.
    r = client.post("/people/staff", json={
        "staff_id": "staff-901", "full_name": "Pending Person, RN",
        "job_role": "nurse", "unit": "unit-icu",
        "service_line": "critical-care"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/directories/sync", json={"dry_run": True},
                    headers=ADMIN)
    assert r.json()["practitioners"]["create"] == 1
    assert _fhir_total(client, "Practitioner") == 19


def test_locations_republish_adds_organization(client):
    assert client.post("/directories/sync", json={},
                       headers=ADMIN).status_code == 200
    assert _fhir_total(client, "Organization") == 10
    # Republish terminology/locations with one extra unit (the
    # governed way locations change — a new published version).
    payload = _locations_payload(client)
    payload["concepts"] = list(payload["concepts"]) + [{
        "code": "unit-stepdown", "display": "Step-Down Unit",
        "type": "unit", "parent": "facility-main", "active": True}]
    _publish(client, "terminology", "locations", payload)
    # The next sync adds exactly that one Organization, in place.
    r = client.post("/directories/sync", json={}, headers=ADMIN)
    body = r.json()
    assert body["organizations"]["create"] == 1
    assert body["organizations"]["unchanged"] == 10
    assert body["practitioners"]["create"] == 0
    assert _fhir_total(client, "Organization") == 11
    r = client.get("/fhir/Organization/unit-stepdown", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Step-Down Unit"
    assert r.json()["partOf"] == {
        "reference": "Organization/facility-main"}
    # And a further sync is again fully idempotent.
    r = client.post("/directories/sync", json={}, headers=ADMIN)
    assert r.json()["organizations"]["unchanged"] == 11
    assert _fhir_total(client, "Organization") == 11
