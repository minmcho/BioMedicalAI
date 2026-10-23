from app.audit import verify_chain
from app.models import AuditLog
from tests.conftest import ADMIN, CLINICIAN, CONFIG, VIEWER
from sqlalchemy import select


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_viewer_cannot_install_pack(client):
    r = client.post("/studio/packs/clinical-core/install", headers=VIEWER)
    assert r.status_code == 403


def test_clinician_cannot_install_pack(client):
    r = client.post("/studio/packs/clinical-core/install", headers=CLINICIAN)
    assert r.status_code == 403


def test_admin_install_and_list_packs(client):
    r = client.get("/studio/packs", headers=ADMIN)
    assert r.status_code == 200
    names = {p["name"] for p in r.json()}
    assert {"clinical-core", "lab"} <= names
    # Pack surface ships disabled.
    for pack in r.json():
        for ep in pack["manifest"]["api_endpoints"]:
            assert ep["enabled"] is False


def test_studio_lifecycle_happy_path(client):
    # Phase 17 adjustment: clinical-core is now seeded ENABLED at
    # startup (pack-gate default-ON rollout), so its installation
    # already exists in `enabled` and cannot walk the lifecycle from
    # a draft. The from-draft lifecycle is proven unchanged on a
    # non-seeded file pack (specialty-cardiology); assertions are
    # identical.
    r = client.post("/studio/packs/specialty-cardiology/install", headers=ADMIN)
    assert r.status_code == 201
    inst = r.json()
    assert inst["status"] == "installed_draft"
    iid = inst["id"]
    for action, expected in [("approve", "approved"), ("attach", "attached"), ("enable", "enabled")]:
        r = client.post(f"/studio/installations/{iid}/{action}", headers=CONFIG)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == expected
    r = client.post(f"/studio/installations/{iid}/disable", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["status"] == "disabled"


def test_lifecycle_violations(client):
    inst = client.post("/studio/packs/lab/install", headers=ADMIN).json()
    iid = inst["id"]
    # attach before approve
    assert client.post(f"/studio/installations/{iid}/attach", headers=ADMIN).status_code == 409
    # enable before attach
    assert client.post(f"/studio/installations/{iid}/enable", headers=ADMIN).status_code == 409
    client.post(f"/studio/installations/{iid}/approve", headers=ADMIN)
    # enable still blocked before attach
    assert client.post(f"/studio/installations/{iid}/enable", headers=ADMIN).status_code == 409


def test_install_idempotent(client):
    a = client.post("/studio/packs/lab/install", headers=ADMIN).json()
    b = client.post("/studio/packs/lab/install", headers=ADMIN).json()
    assert a["id"] == b["id"]
    installs = client.get("/studio/installations", headers=ADMIN).json()
    assert len([i for i in installs if i["pack_name"] == "lab"]) == 1


PATIENT = {"resourceType": "Patient", "id": "pat-1",
           "name": [{"family": "Synthetic", "given": ["Pat"]}], "gender": "unknown"}


def test_patient_create_read_roundtrip(client):
    r = client.post("/fhir/Patient", json=PATIENT, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "pat-1"
    assert body["meta"]["versionId"] == "1"
    r = client.get("/fhir/Patient/pat-1", headers=VIEWER)
    assert r.status_code == 200
    assert r.json()["name"][0]["family"] == "Synthetic"


def test_patient_not_found(client):
    assert client.get("/fhir/Patient/nope", headers=VIEWER).status_code == 404


def test_patient_missing_name_and_identifier_422(client):
    r = client.post("/fhir/Patient", json={"resourceType": "Patient"}, headers=CLINICIAN)
    assert r.status_code == 422


def test_wrong_resource_type_422(client):
    r = client.post("/fhir/Patient", json={"resourceType": "Observation"}, headers=CLINICIAN)
    assert r.status_code == 422


def test_viewer_cannot_write_fhir(client):
    assert client.post("/fhir/Patient", json=PATIENT, headers=VIEWER).status_code == 403


def test_encounter_observation_search_by_patient(client):
    client.post("/fhir/Patient", json=PATIENT, headers=CLINICIAN)
    enc = {"resourceType": "Encounter", "id": "enc-1", "status": "finished",
           "subject": {"reference": "Patient/pat-1"}}
    assert client.post("/fhir/Encounter", json=enc, headers=CLINICIAN).status_code == 201
    obs = {"resourceType": "Observation", "id": "obs-1", "status": "final",
           "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4", "display": "Heart rate"}]},
           "subject": {"reference": "Patient/pat-1"}, "valueQuantity": {"value": 72, "unit": "/min"}}
    assert client.post("/fhir/Observation", json=obs, headers=CLINICIAN).status_code == 201

    bundle = client.get("/fhir/Observation", params={"patient": "pat-1"}, headers=VIEWER).json()
    assert bundle["resourceType"] == "Bundle" and bundle["total"] == 1
    assert bundle["entry"][0]["resource"]["id"] == "obs-1"

    bundle = client.get("/fhir/Encounter", params={"patient": "Patient/pat-1"}, headers=VIEWER).json()
    assert bundle["total"] == 1

    bundle = client.get("/fhir/Observation", params={"patient": "other"}, headers=VIEWER).json()
    assert bundle["total"] == 0


def test_encounter_missing_subject_422(client):
    r = client.post("/fhir/Encounter", json={"resourceType": "Encounter", "status": "planned"},
                    headers=CLINICIAN)
    assert r.status_code == 422


def test_observation_missing_code_422(client):
    obs = {"resourceType": "Observation", "subject": {"reference": "Patient/pat-1"}}
    assert client.post("/fhir/Observation", json=obs, headers=CLINICIAN).status_code == 422


def test_audit_chain_grows_and_verifies(client):
    client.post("/studio/packs/lab/install", headers=ADMIN)
    client.post("/fhir/Patient", json=PATIENT, headers=CLINICIAN)
    factory = client.app.state.session_factory
    with factory() as session:
        rows = session.scalars(select(AuditLog)).all()
        actions = {row.action for row in rows}
        assert "studio.install" in actions
        assert "fhir.create" in actions
        assert len(rows) >= 2
        assert verify_chain(session) is True
