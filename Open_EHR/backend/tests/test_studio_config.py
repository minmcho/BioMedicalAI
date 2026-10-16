"""Studio customization engine (docs/STUDIO_CUSTOMIZATION.md):
versioned config store, per-kind validation, draft -> publish governance,
config-driven ADT workflow, capability enforcement from published
permissions config, and QuestionnaireResponse validation."""
from sqlalchemy import select

from app.audit import verify_chain
from app.models import AuditLog
from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER

FORM = {
    "resourceType": "Questionnaire",
    "title": "Intake form",
    "item": [
        {"linkId": "q1", "text": "Chief complaint", "type": "string", "required": True},
        {"linkId": "q2", "text": "Current smoker?", "type": "boolean",
         "required": False, "valueSet": "ValueSet/yes-no"},
    ],
}

BRANDING = {"orgName": "OpenCare Clinic", "portalTitle": "OpenCare Patient Portal",
            "primaryColor": "#1a7f37", "logoUrl": "https://example.org/logo.png"}

TERMINOLOGY = {
    "name": "Basic metabolic panel",
    "concepts": [
        {"code": "2339-0", "display": "Glucose", "active": True,
         "referenceRange": {"low": 70, "high": 99, "unit": "mg/dL"}},
        {"code": "6299-2", "display": "Urea nitrogen", "active": True},
    ],
}


def _create(client, kind, key, payload, headers=ADMIN):
    return client.post(f"/studio/config/{kind}/{key}", json=payload, headers=headers)


def _publish(client, kind, key, version, headers=ADMIN):
    return client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                       headers=headers)


def _make_published(client, kind, key, payload):
    r = _create(client, kind, key, payload)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = _publish(client, kind, key, version)
    assert r.status_code == 200, r.text
    return version


def _appointment(appt_id, status="booked"):
    return {"resourceType": "Appointment", "id": appt_id, "status": status,
            "participant": [{"actor": {"reference": "Patient/pat-1"}},
                            {"actor": {"reference": "Practitioner/dr-1"}}]}


# ---------------------------------------------------------------------------
# Lifecycle: draft invisible -> publish -> supersede -> rollback
# ---------------------------------------------------------------------------

def test_draft_invisible_until_published(client):
    r = _create(client, "branding", "default", BRANDING)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "draft" and body["version"] == 1
    # not effective for anyone yet
    assert client.get("/studio/config/branding/effective/default",
                      headers=CLINICIAN).status_code == 404
    assert client.get("/studio/config/branding/effective",
                      headers=CLINICIAN).json() == []
    # clinician cannot see the draft version at all
    assert client.get("/studio/config/branding/default/versions/1",
                      headers=CLINICIAN).status_code == 404
    # publish -> effective for clinician and viewer
    assert _publish(client, "branding", "default", 1).status_code == 200
    eff = client.get("/studio/config/branding/effective/default", headers=VIEWER)
    assert eff.status_code == 200 and eff.json()["payload"]["orgName"] == "OpenCare Clinic"
    listed = client.get("/studio/config/branding/effective", headers=CLINICIAN).json()
    assert [c["key"] for c in listed] == ["default"]


def test_publish_supersedes_and_effective_follows(client):
    _make_published(client, "branding", "default", BRANDING)
    changed = dict(BRANDING, orgName="Renamed Clinic")
    r = _create(client, "branding", "default", changed)
    assert r.status_code == 201 and r.json()["version"] == 2
    # v1 still published until v2 publishes
    eff = client.get("/studio/config/branding/effective/default", headers=ADMIN).json()
    assert eff["version"] == 1
    assert _publish(client, "branding", "default", 2).status_code == 200
    history = client.get("/studio/config/branding/default", headers=ADMIN).json()
    assert [(h["version"], h["status"]) for h in history] == [
        (1, "superseded"), (2, "published")]
    eff = client.get("/studio/config/branding/effective/default", headers=ADMIN).json()
    assert eff["payload"]["orgName"] == "Renamed Clinic"


def test_rollback_republishes_old_payload_as_new_version(client):
    _make_published(client, "branding", "default", BRANDING)
    _make_published(client, "branding", "default", dict(BRANDING, orgName="V2 Clinic"))
    r = client.post("/studio/config/branding/default/rollback",
                    json={"version": 1}, headers=ADMIN)
    assert r.status_code == 201
    body = r.json()
    assert body["version"] == 3 and body["status"] == "published"
    assert body["payload"]["orgName"] == "OpenCare Clinic"
    history = client.get("/studio/config/branding/default", headers=ADMIN).json()
    assert [(h["version"], h["status"]) for h in history] == [
        (1, "superseded"), (2, "superseded"), (3, "published")]


def test_rollback_to_draft_rejected(client):
    _create(client, "branding", "default", BRANDING)  # v1 stays a draft
    r = client.post("/studio/config/branding/default/rollback",
                    json={"version": 1}, headers=ADMIN)
    assert r.status_code == 422


def test_disable_removes_effective_config(client):
    _make_published(client, "branding", "default", BRANDING)
    r = client.post("/studio/config/branding/default/versions/1/disable", headers=ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    assert client.get("/studio/config/branding/effective/default",
                      headers=ADMIN).status_code == 404
    # disabling again -> 409 (not published)
    assert client.post("/studio/config/branding/default/versions/1/disable",
                       headers=ADMIN).status_code == 409


def test_published_versions_are_immutable_new_draft_spawns(client):
    _make_published(client, "branding", "default", BRANDING)
    # direct edit of the published version -> 409
    r = client.put("/studio/config/branding/default/versions/1",
                   json=dict(BRANDING, orgName="Sneaky edit"), headers=ADMIN)
    assert r.status_code == 409
    # the published payload is untouched
    eff = client.get("/studio/config/branding/effective/default", headers=ADMIN).json()
    assert eff["payload"]["orgName"] == "OpenCare Clinic"
    # editing happens by spawning a new draft version
    r = _create(client, "branding", "default", dict(BRANDING, orgName="Edited Clinic"))
    assert r.status_code == 201 and r.json()["version"] == 2 and r.json()["status"] == "draft"


def test_draft_edit_and_duplicate_draft_conflict(client):
    _create(client, "branding", "default", BRANDING)
    assert _create(client, "branding", "default", BRANDING).status_code == 409
    r = client.put("/studio/config/branding/default/versions/1",
                   json=dict(BRANDING, orgName="Draft edit"), headers=CONFIG)
    assert r.status_code == 200 and r.json()["payload"]["orgName"] == "Draft edit"


def test_config_writes_are_role_gated(client):
    assert client.post("/studio/config/branding/default", json=BRANDING).status_code == 401
    assert _create(client, "branding", "default", BRANDING,
                   headers=CLINICIAN).status_code == 403
    assert _create(client, "branding", "default", BRANDING,
                   headers=PATIENT).status_code == 403
    _make_published(client, "branding", "default", BRANDING)
    assert _publish(client, "branding", "default", 1, headers=VIEWER).status_code == 403
    # patient may not read config either
    assert client.get("/studio/config/branding/effective",
                      headers=PATIENT).status_code == 403


def test_unknown_kind_is_404(client):
    assert _create(client, "widgets", "x", {}).status_code == 404
    assert client.get("/studio/config/widgets/effective", headers=ADMIN).status_code == 404


def test_config_actions_are_audited(client):
    _make_published(client, "branding", "default", BRANDING)
    client.post("/studio/config/branding/default/versions/1/disable", headers=ADMIN)
    factory = client.app.state.session_factory
    with factory() as session:
        actions = [row.action for row in session.scalars(select(AuditLog)).all()]
        assert "studio.config.create" in actions
        assert "studio.config.publish" in actions
        assert "studio.config.disable" in actions
        assert verify_chain(session) is True


def test_config_diff_between_versions(client):
    _make_published(client, "branding", "default", BRANDING)
    _make_published(client, "branding", "default", dict(BRANDING, orgName="New Name"))
    r = client.get("/studio/config/branding/default/diff",
                   params={"from": 1, "to": 2}, headers=ADMIN)
    assert r.status_code == 200
    changes = r.json()["changes"]
    assert any(c["path"] == "orgName" and c["change"] == "changed"
               and c["from"] == "OpenCare Clinic" and c["to"] == "New Name"
               for c in changes)


# ---------------------------------------------------------------------------
# Per-kind payload validation
# ---------------------------------------------------------------------------

def test_invalid_payloads_are_422_with_details(client):
    r = _create(client, "form", "intake", {"item": []})
    assert r.status_code == 422 and "item" in r.json()["detail"]
    r = _create(client, "form", "intake",
                {"item": [{"linkId": "a", "text": "A", "type": "nonsense"}]})
    assert r.status_code == 422 and "type" in r.json()["detail"]
    r = _create(client, "branding", "default", dict(BRANDING, primaryColor="blue"))
    assert r.status_code == 422 and "primaryColor" in r.json()["detail"]
    r = _create(client, "workflow", "w",
                {"states": ["a"], "transitions": [{"from": "a", "to": "b"}]})
    assert r.status_code == 422
    r = _create(client, "terminology", "panel", {"concepts": [{"code": "1"}]})
    assert r.status_code == 422 and "display" in r.json()["detail"]
    r = _create(client, "permissions", "role-matrix",
                {"capabilities": {"fhir.fly": {"viewer": True}}})
    assert r.status_code == 422 and "Unknown capability" in r.json()["detail"]


def test_terminology_with_reference_ranges_round_trips(client):
    _make_published(client, "terminology", "bmp", TERMINOLOGY)
    eff = client.get("/studio/config/terminology/effective/bmp", headers=CLINICIAN).json()
    glucose = eff["payload"]["concepts"][0]
    assert glucose["referenceRange"] == {"low": 70, "high": 99, "unit": "mg/dL"}


def test_navigation_modules_only_rule(client):
    page_item = {"items": [{"label": "Intake page", "type": "page", "module": "core"}]}
    r = _create(client, "navigation", "main", page_item)
    assert r.status_code == 400 and "modules" in r.json()["detail"]
    form_item = {"items": [{"label": "Intake", "form": "intake", "module": "core"}]}
    r = _create(client, "navigation", "main", form_item)
    assert r.status_code == 400 and "modules" in r.json()["detail"]
    no_module = {"items": [{"label": "Loose link", "url": "https://example.org"}]}
    r = _create(client, "navigation", "main", no_module)
    assert r.status_code == 400 and "modules" in r.json()["detail"]
    good = {"items": [{"label": "Scheduling", "module": "scheduling",
                       "roles": ["clinician"], "order": 1, "visible": True}]}
    assert _create(client, "navigation", "main", good).status_code == 201


# ---------------------------------------------------------------------------
# ADT workflow is config-driven
# ---------------------------------------------------------------------------

def test_adt_workflow_seeded_as_published_config(client):
    eff = client.get("/studio/config/workflow/effective/appointment-adt",
                     headers=CLINICIAN)
    assert eff.status_code == 200
    payload = eff.json()["payload"]
    assert "booked" in payload["states"] and "fulfilled" in payload["states"]
    pairs = {(t["from"], t["to"]) for t in payload["transitions"]}
    assert ("booked", "arrived") in pairs and ("arrived", "fulfilled") in pairs
    assert ("booked", "fulfilled") not in pairs


def test_adt_config_change_alters_transitions(client):
    # baseline from the seeded config: booked -> noshow is illegal
    client.post("/fhir/Appointment", json=_appointment("c1"), headers=CLINICIAN)
    assert client.post("/fhir/Appointment/c1/transition",
                       json={"to_status": "noshow"}, headers=CLINICIAN).status_code == 409
    # publish a v2 workflow that adds booked -> noshow and drops arrived -> cancelled
    v2 = {
        "states": ["proposed", "pending", "booked", "arrived", "checked-in",
                   "fulfilled", "cancelled", "noshow"],
        "transitions": [
            {"from": "proposed", "to": "booked"}, {"from": "proposed", "to": "cancelled"},
            {"from": "pending", "to": "booked"}, {"from": "pending", "to": "cancelled"},
            {"from": "booked", "to": "arrived"}, {"from": "booked", "to": "cancelled"},
            {"from": "booked", "to": "noshow", "action": "mark-noshow",
             "roles": ["clinician"]},
            {"from": "arrived", "to": "fulfilled"},
            {"from": "checked-in", "to": "fulfilled"},
            {"from": "checked-in", "to": "cancelled"},
        ],
    }
    _make_published(client, "workflow", "appointment-adt", v2)
    # the added transition now works
    r = client.post("/fhir/Appointment/c1/transition",
                    json={"to_status": "noshow"}, headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["status"] == "noshow"
    # the removed transition is now illegal
    client.post("/fhir/Appointment", json=_appointment("c2"), headers=CLINICIAN)
    client.post("/fhir/Appointment/c2/transition",
                json={"to_status": "arrived"}, headers=CLINICIAN)
    assert client.post("/fhir/Appointment/c2/transition",
                       json={"to_status": "cancelled"}, headers=CLINICIAN).status_code == 409
    # unknown status still 422 under config
    assert client.post("/fhir/Appointment/c2/transition",
                       json={"to_status": "bogus"}, headers=CLINICIAN).status_code == 422


def test_adt_falls_back_when_workflow_config_disabled(client):
    eff = client.get("/studio/config/workflow/effective/appointment-adt",
                     headers=ADMIN).json()
    client.post(f"/studio/config/workflow/appointment-adt/versions/{eff['version']}/disable",
                headers=ADMIN)
    assert client.get("/studio/config/workflow/effective/appointment-adt",
                      headers=ADMIN).status_code == 404
    # hardcoded fallback keeps the standard machine working
    client.post("/fhir/Appointment", json=_appointment("f1"), headers=CLINICIAN)
    r = client.post("/fhir/Appointment/f1/transition",
                    json={"to_status": "arrived"}, headers=CLINICIAN)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Permissions config actually changes API outcomes
# ---------------------------------------------------------------------------

PATIENT_BODY = {"resourceType": "Patient", "id": "perm-1",
                "name": [{"family": "Synthetic", "given": ["Perm"]}]}


def test_permissions_override_grants_and_revokes_writes(client):
    # defaults: viewer cannot write, clinician can
    assert client.post("/fhir/Patient", json=PATIENT_BODY,
                       headers=VIEWER).status_code == 403
    assert client.post("/fhir/Patient", json=PATIENT_BODY,
                       headers=CLINICIAN).status_code == 201
    # grant viewer the fhir.write capability via published Studio config
    _make_published(client, "permissions", "role-matrix",
                    {"capabilities": {"fhir.write": {"viewer": True}}})
    assert client.post("/fhir/Patient", json=dict(PATIENT_BODY, id="perm-2"),
                       headers=VIEWER).status_code == 201
    # a new published version revoking clinician write takes effect too
    _make_published(client, "permissions", "role-matrix",
                    {"capabilities": {"fhir.write": {"clinician": False}}})
    r = client.post("/fhir/Patient", json=dict(PATIENT_BODY, id="perm-3"),
                    headers=CLINICIAN)
    assert r.status_code == 403 and "fhir.write" in r.json()["detail"]
    # ...while admin (not overridden) still writes
    assert client.post("/fhir/Patient", json=dict(PATIENT_BODY, id="perm-3"),
                       headers=ADMIN).status_code == 201


def test_permissions_override_can_open_inbox(client):
    assert client.get("/fhir/inbox", params={"patient": "pat-1"},
                      headers=VIEWER).status_code == 403
    _make_published(client, "permissions", "role-matrix",
                    {"capabilities": {"fhir.inbox": {"viewer": True}}})
    assert client.get("/fhir/inbox", params={"patient": "pat-1"},
                      headers=VIEWER).status_code == 200


# ---------------------------------------------------------------------------
# QuestionnaireResponse validation against published forms
# ---------------------------------------------------------------------------

def _qr(qref="intake", items=None):
    return {"resourceType": "QuestionnaireResponse", "questionnaire": qref,
            "status": "completed",
            "subject": {"reference": "Patient/pat-1"},
            "item": items if items is not None else [
                {"linkId": "q1", "answer": [{"valueString": "Headache"}]},
                {"linkId": "q2", "answer": [{"valueBoolean": False}]},
            ]}


def test_questionnaire_response_requires_published_form(client):
    # no form at all -> 422
    assert client.post("/fhir/QuestionnaireResponse", json=_qr(),
                       headers=CLINICIAN).status_code == 422
    # draft-only form -> still 422 (drafts never render/accept use)
    _create(client, "form", "intake", FORM)
    r = client.post("/fhir/QuestionnaireResponse", json=_qr(), headers=CLINICIAN)
    assert r.status_code == 422 and "not published" in r.json()["detail"]
    # publish -> accepted
    _publish(client, "form", "intake", 1)
    r = client.post("/fhir/QuestionnaireResponse", json=_qr(), headers=CLINICIAN)
    assert r.status_code == 201
    qr_id = r.json()["id"]
    got = client.get(f"/fhir/QuestionnaireResponse/{qr_id}", headers=CLINICIAN)
    assert got.status_code == 200 and got.json()["questionnaire"] == "intake"
    bundle = client.get("/fhir/QuestionnaireResponse",
                        params={"patient": "pat-1"}, headers=CLINICIAN).json()
    assert bundle["total"] == 1


def test_questionnaire_response_required_items_enforced(client):
    _make_published(client, "form", "intake", FORM)
    missing = _qr(items=[{"linkId": "q2", "answer": [{"valueBoolean": True}]}])
    r = client.post("/fhir/QuestionnaireResponse", json=missing, headers=CLINICIAN)
    assert r.status_code == 422 and "q1" in r.json()["detail"]
    empty_answer = _qr(items=[{"linkId": "q1", "answer": []}])
    assert client.post("/fhir/QuestionnaireResponse", json=empty_answer,
                       headers=CLINICIAN).status_code == 422
    no_ref = _qr()
    del no_ref["questionnaire"]
    assert client.post("/fhir/QuestionnaireResponse", json=no_ref,
                       headers=CLINICIAN).status_code == 422
    # viewer cannot write responses
    assert client.post("/fhir/QuestionnaireResponse", json=_qr(),
                       headers=VIEWER).status_code == 403


def test_questionnaire_response_patient_scoping(client):
    _make_published(client, "form", "intake", FORM)
    r = client.post("/fhir/QuestionnaireResponse", json=_qr(), headers=CLINICIAN)
    qr_id = r.json()["id"]
    # owning patient can read it back; another patient's id would 404 by the
    # shared non-disclosure rule (covered for other resource types already)
    assert client.get(f"/fhir/QuestionnaireResponse/{qr_id}",
                      headers=PATIENT).status_code == 200
