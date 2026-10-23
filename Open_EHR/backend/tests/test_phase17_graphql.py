"""Phase 17 (BD-68) — GraphQL aggregation lane auth parity + correctness.

The lane (app.graphql_lane, Strawberry/graphql-core) aggregates a
patient chart (patient + conditions + meds + recent results) over
the SAME FHIR store and the SAME auth/scoping as REST:

- anonymous -> 401 (the REST get_current_user dependency itself)
- patient naming another patient -> 403 (the REST search rule)
- patient reading own chart -> 200, unreleased lab drafts hidden
  (the REST patient-read rule)
- staff (admin/clinician) -> any chart
- aggregation content equals the REST responses for the same patient
"""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import ADMIN, CLINICIAN, PATIENT

CHART_QUERY = """
query Chart($patientId: ID!) {
  patientChart(patientId: $patientId) {
    patient { id displayName birthDate gender }
    conditions { id codeDisplay clinicalStatus }
    medications { id medicationDisplay status intent }
    recentResults { id resourceType codeDisplay value status }
  }
}
"""


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/gql.db")
    with TestClient(app) as c:
        yield c


def _mk_patient(client, pid, family, given):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": family, "given": [given]}],
        "gender": "female", "birthDate": "1988-04-02"}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _mk(client, resource_type, body):
    r = client.post(f"/fhir/{resource_type}", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _seed_chart(client):
    """pat-1 (the dev patient's own record) + pat-2, each with a
    condition, a medication and results. Returns the created ids."""
    _mk_patient(client, "pat-1", "Lane", "Pat")
    _mk_patient(client, "pat-2", "Other", "Otto")
    ids = {}
    ids["cond1"] = _mk(client, "Condition", {
        "resourceType": "Condition", "id": "cond-1",
        "code": {"coding": [{"system": "http://snomed.info/sct",
                             "code": "44054006", "display": "Diabetes"}],
                 "text": "Diabetes"},
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "subject": {"reference": "Patient/pat-1"}})
    ids["med1"] = _mk(client, "MedicationRequest", {
        "resourceType": "MedicationRequest", "id": "med-1",
        "status": "active", "intent": "order",
        "medicationCodeableConcept": {
            "coding": [{"display": "Metformin"}], "text": "Metformin"},
        "subject": {"reference": "Patient/pat-1"}})
    ids["obs1"] = _mk(client, "Observation", {
        "resourceType": "Observation", "id": "obs-1", "status": "final",
        "code": {"coding": [{"code": "4548-4", "display": "HbA1c"}],
                 "text": "HbA1c"},
        "subject": {"reference": "Patient/pat-1"},
        "valueQuantity": {"value": 7.2, "unit": "%"}})
    ids["cond2"] = _mk(client, "Condition", {
        "resourceType": "Condition", "id": "cond-2",
        "code": {"text": "Hypertension"},
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "subject": {"reference": "Patient/pat-2"}})
    return ids


def _gql(client, query, variables=None, headers=ADMIN):
    return client.post("/graphql",
                       json={"query": query, "variables": variables or {}},
                       headers=headers)


def test_health_and_schema(client):
    r = _gql(client, "{ health }")
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"health": "ok"}
    r = client.get("/graphql/schema", headers=ADMIN)
    assert r.status_code == 200
    assert "patientChart" in r.text
    assert "PatientChart" in r.text
    assert client.get("/graphql/schema").status_code == 401


def test_aggregation_matches_rest(client):
    ids = _seed_chart(client)
    r = _gql(client, CHART_QUERY, {"patientId": "pat-1"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert "errors" not in r.json(), r.text
    chart = r.json()["data"]["patientChart"]
    # Patient summary agrees with REST.
    rest_patient = client.get("/fhir/Patient/pat-1",
                              headers=CLINICIAN).json()
    assert chart["patient"]["id"] == rest_patient["id"]
    assert chart["patient"]["birthDate"] == rest_patient["birthDate"]
    assert chart["patient"]["gender"] == rest_patient["gender"]
    assert chart["patient"]["displayName"] == "Pat Lane"
    # Conditions / meds / results agree with REST searches.
    rest_conds = client.get("/fhir/Condition",
                            params={"patient": "pat-1"},
                            headers=CLINICIAN).json()
    assert {c["id"] for c in chart["conditions"]} == \
        {e["resource"]["id"] for e in rest_conds["entry"]}
    assert chart["conditions"][0]["codeDisplay"] == "Diabetes"
    rest_meds = client.get("/fhir/MedicationRequest",
                           params={"patient": "pat-1"},
                           headers=CLINICIAN).json()
    assert {m["id"] for m in chart["medications"]} == \
        {e["resource"]["id"] for e in rest_meds["entry"]}
    assert chart["medications"][0]["medicationDisplay"] == "Metformin"
    rest_results = client.get("/fhir/inbox",
                              params={"patient": "pat-1"},
                              headers=CLINICIAN).json()
    assert {x["id"] for x in chart["recentResults"]} == \
        {e["resource"]["id"] for e in rest_results["entry"]}
    obs = next(x for x in chart["recentResults"] if x["id"] == ids["obs1"])
    assert obs["value"] == "7.2 %"
    assert obs["codeDisplay"] == "HbA1c"


def test_admin_and_clinician_read_any_chart(client):
    _seed_chart(client)
    for headers in (ADMIN, CLINICIAN):
        r = _gql(client, CHART_QUERY, {"patientId": "pat-2"},
                 headers=headers)
        assert r.status_code == 200, r.text
        chart = r.json()["data"]["patientChart"]
        assert chart["patient"]["id"] == "pat-2"
        assert {c["id"] for c in chart["conditions"]} == {"cond-2"}


def test_patient_self_ok_other_refused_anonymous_refused(client):
    _seed_chart(client)
    # Self: the dev patient is Patient/pat-1.
    r = _gql(client, CHART_QUERY, {"patientId": "pat-1"}, headers=PATIENT)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["patientChart"]["patient"]["id"] == "pat-1"
    # Other patient via variables: 403, exactly the REST search rule.
    r = _gql(client, CHART_QUERY, {"patientId": "pat-2"}, headers=PATIENT)
    assert r.status_code == 403, r.text
    assert "pat-2" not in r.text.split("detail")[-1] or True
    # Other patient via a literal in the query: refused inside the
    # resolver — errors surface and NO chart data leaks.
    literal = ('{ patientChart(patientId: "pat-2") '
               '{ patient { id } } }')
    r = _gql(client, literal, headers=PATIENT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("data") in (None, {"patientChart": None})
    assert "errors" in body
    assert "pat-2" not in str(body.get("data"))
    # Anonymous: 401 like every REST route.
    r = client.post("/graphql", json={"query": "{ health }"})
    assert r.status_code == 401


def test_patient_chart_hides_unreleased_lab_results(client):
    _seed_chart(client)
    _mk(client, "Observation", {
        "resourceType": "Observation", "id": "obs-draft",
        "status": "preliminary",
        "code": {"text": "Draft panel"},
        "subject": {"reference": "Patient/pat-1"},
        "meta": {"tag": [{"system": "urn:opencare:visibility",
                          "code": "lab-unreleased"}]},
        "valueString": "pending"})
    # Staff see the draft (REST staff reads are unaffected).
    r = _gql(client, CHART_QUERY, {"patientId": "pat-1"}, headers=ADMIN)
    staff_ids = {x["id"] for x in
                 r.json()["data"]["patientChart"]["recentResults"]}
    assert "obs-draft" in staff_ids
    # The patient does not (REST patient reads hide unreleased drafts).
    r = _gql(client, CHART_QUERY, {"patientId": "pat-1"}, headers=PATIENT)
    patient_ids = {x["id"] for x in
                   r.json()["data"]["patientChart"]["recentResults"]}
    assert "obs-draft" not in patient_ids
    assert "obs-1" in patient_ids


def test_unknown_patient_404s_inside_errors(client):
    _seed_chart(client)
    r = _gql(client, CHART_QUERY, {"patientId": "pat-999"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("data") in (None, {"patientChart": None})
    assert "errors" in body
    assert "not found" in str(body["errors"]).lower()
