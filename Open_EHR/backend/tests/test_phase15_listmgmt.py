"""Phase 15 (F1) — governed dropdown/list management (app.listmgmt).

The registry (terminology/manageable-lists, published) names the app's
real terminology-backed selection lists; authorised callers — holders
of lists.manage OR staff whose linked job role is a manager role for
that entry — edit a list's concepts as a Studio DRAFT; consumers read
published only, proven by the radiology modalities flip: draft a new
modality, the endpoint is unchanged, publish through the EXISTING
Studio publish endpoint, the endpoint serves it; deactivate one and it
leaves the active list. Published codes are never hard-deleted:
omitting one is a 422 that explains deactivation.

Synthetic data only.
"""
import copy

from .conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER

MOD = "/lists/terminology/imaging-modalities"
DISP = "/lists/terminology/discharge-dispositions"


def _published_concepts(client, kind_key, headers=ADMIN):
    r = client.get(f"/lists/{kind_key}", headers=headers)
    assert r.status_code == 200, r.text
    return copy.deepcopy(r.json()["published"]["concepts"])


def _active_codes(resp_json):
    return {c["code"] for c in resp_json["modalities"]
            if c.get("active") is not False}


def _modalities(client):
    r = client.get("/radiology/modalities", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_seeded_covers_real_lists_and_excludes_lab_catalog(client):
    r = client.get("/lists", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["registry"] == {"kind": "terminology",
                                "key": "manageable-lists", "version": 1}
    by_key = {i["key"]: i for i in body["items"]}
    for key in ("imaging-modalities", "or-rooms", "discharge-dispositions",
                "adt-event-types", "denial-categories", "payers", "workforce",
                "devices", "micro-organisms", "micro-antibiotics",
                "sendout-labs", "ed-triage", "locations", "order-types",
                "note-types", "referral-services", "slot-types"):
        assert key in by_key, key
        entry = by_key[key]
        assert entry["kind"] == "terminology"
        assert entry["label"] and entry["module"]
        assert entry["managerJobRoles"], key
    assert by_key["imaging-modalities"]["managerJobRoles"] == [
        "radiographer", "department-head"]
    # The lab catalogue is excluded — it has its own Test Manager surface.
    assert "lab-catalog" not in by_key
    excluded = {e["key"]: e for e in body["excluded_lists"]}
    assert "lab-catalog" in excluded
    assert "Test Manager" in excluded["lab-catalog"]["reason"]
    # The registry is also a real published Studio config.
    eff = client.get("/studio/config/terminology/effective/manageable-lists",
                     headers=ADMIN)
    assert eff.status_code == 200, eff.text
    assert eff.json()["version"] == 1


def test_registry_reads_require_studio_read(client):
    assert client.get("/lists", headers=CLINICIAN).status_code == 200
    assert client.get("/lists", headers=VIEWER).status_code == 200
    assert client.get("/lists", headers=PATIENT).status_code == 403
    r = client.get("/lists", headers=ADMIN).json()
    admin_entry = next(i for i in r["items"]
                       if i["key"] == "imaging-modalities")
    assert admin_entry["caller_can_manage"] is True
    assert admin_entry["manage_basis"] == "capability:lists.manage"


def test_get_list_separates_published_and_draft(client):
    r = client.get(MOD, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    codes = {c["code"] for c in body["published"]["concepts"]}
    assert {"CT", "MR", "US"} <= codes
    assert body["published"]["version"] == 1
    assert body["draft"] is None
    assert "only when a human publishes" in body["notice"]
    assert client.get("/lists/terminology/no-such-list",
                      headers=ADMIN).status_code == 404
    assert client.get(MOD, headers=PATIENT).status_code == 403


# ---------------------------------------------------------------------------
# Authorisation: capability path, job-role path, and refusals
# ---------------------------------------------------------------------------

def test_admin_and_configurator_manage_by_capability(client):
    concepts = _published_concepts(client, "terminology/or-rooms")
    concepts[0]["display"] = "Operating Room One"
    r = client.put("/lists/terminology/or-rooms/draft",
                   json={"concepts": concepts}, headers=CONFIG)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["manage_basis"] == "capability:lists.manage"
    assert body["diff"] == {"added": [], "updated": ["OR-1"],
                            "deactivated": []}
    assert body["status"] == "draft"
    # Honesty: the response states the effect plainly.
    assert "only when a human publishes" in body["notice"]
    assert "System Studio" in body["notice"]


def test_department_manager_job_role_path(client):
    # dev-viewer is linked (people.py directory) to staff-009, whose
    # job_role is unit-manager; the viewer ROLE holds no lists.manage.
    concepts = _published_concepts(client, "terminology/discharge-dispositions")
    concepts.append({"code": "swing-bed", "display": "Swing bed",
                     "active": True})
    r = client.put(f"{DISP}/draft", json={"concepts": concepts},
                   headers=VIEWER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["manage_basis"] == "job_role:unit-manager"
    assert body["diff"]["added"] == ["swing-bed"]
    # The draft exists but published is untouched.
    after = client.get(DISP, headers=ADMIN).json()
    assert after["draft"]["concepts"][-1]["code"] == "swing-bed"
    assert "swing-bed" not in {
        c["code"] for c in after["published"]["concepts"]}


def test_clinician_without_manager_job_role_and_patient_are_403(client):
    concepts = _published_concepts(client, "terminology/imaging-modalities")
    concepts.append({"code": "PT", "display": "Positron Emission Tomography",
                     "active": True})
    # dev-clinician is a physician — not a manager role for modalities,
    # and the clinician role holds no lists.manage.
    r = client.put(f"{MOD}/draft", json={"concepts": concepts},
                   headers=CLINICIAN)
    assert r.status_code == 403, r.text
    assert "lists.manage" in r.json()["detail"]
    # unit-manager may manage dispositions but NOT imaging modalities.
    assert client.put(f"{MOD}/draft", json={"concepts": concepts},
                      headers=VIEWER).status_code == 403
    # Patient: no capability, no staff link.
    assert client.put(f"{MOD}/draft", json={"concepts": concepts},
                      headers=PATIENT).status_code == 403
    # No draft was created by any of the refused writes.
    assert client.get(MOD, headers=ADMIN).json()["draft"] is None


# ---------------------------------------------------------------------------
# Draft semantics: validation, deactivation-not-deletion, draft update
# ---------------------------------------------------------------------------

def test_removing_a_published_code_is_422_explaining_deactivation(client):
    concepts = [c for c in _published_concepts(
        client, "terminology/imaging-modalities") if c["code"] != "US"]
    r = client.put(f"{MOD}/draft", json={"concepts": concepts}, headers=ADMIN)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "US" in detail and "never" in detail and "hard-deleted" in detail
    assert "active" in detail and "deactivation" in detail
    assert client.get(MOD, headers=ADMIN).json()["draft"] is None


def test_draft_validation_reuses_terminology_validator(client):
    base = _published_concepts(client, "terminology/imaging-modalities")
    dup = base + [dict(base[0])]
    r = client.put(f"{MOD}/draft", json={"concepts": dup}, headers=ADMIN)
    assert r.status_code == 422 and "Duplicate" in r.json()["detail"]
    no_display = copy.deepcopy(base)
    no_display[0].pop("display")
    assert client.put(f"{MOD}/draft", json={"concepts": no_display},
                      headers=ADMIN).status_code == 422
    empty_code = copy.deepcopy(base)
    empty_code[0]["code"] = "  "
    assert client.put(f"{MOD}/draft", json={"concepts": empty_code},
                      headers=ADMIN).status_code == 422
    assert client.put(f"{MOD}/draft", json={"concepts": []},
                      headers=ADMIN).status_code == 422
    assert client.put(f"{MOD}/draft", json={"nope": []},
                      headers=ADMIN).status_code == 422
    assert client.put("/lists/terminology/no-such-list/draft",
                      json={"concepts": base},
                      headers=ADMIN).status_code == 404


def test_second_put_updates_the_same_draft_version(client):
    base = _published_concepts(client, "terminology/or-rooms")
    first = client.put("/lists/terminology/or-rooms/draft",
                       json={"concepts": base + [
                           {"code": "OR-5", "display": "Operating Room 5",
                            "active": True}]}, headers=ADMIN)
    assert first.status_code == 200, first.text
    version = first.json()["draft_version"]
    second_concepts = _published_concepts(
        client, "terminology/or-rooms")  # published still v1 set
    second_concepts.append({"code": "OR-6", "display": "Operating Room 6",
                            "active": True})
    second = client.put("/lists/terminology/or-rooms/draft",
                        json={"concepts": second_concepts}, headers=ADMIN)
    assert second.status_code == 200, second.text
    assert second.json()["draft_version"] == version  # same draft, replaced
    assert second.json()["diff"]["added"] == ["OR-6"]
    got = client.get("/lists/terminology/or-rooms", headers=ADMIN).json()
    assert got["draft"]["version"] == version
    assert {c["code"] for c in got["draft"]["concepts"]} >= {"OR-6"}
    assert "OR-5" not in {c["code"] for c in got["draft"]["concepts"]}
    assert {c["code"] for c in got["published"]["concepts"]} == {
        "OR-1", "OR-2", "OR-3", "OR-4"}


# ---------------------------------------------------------------------------
# The flip: consumers read published only (radiology modalities)
# ---------------------------------------------------------------------------

def test_modalities_flip_on_studio_publish_and_deactivation(client):
    before = _modalities(client)
    assert "PT" not in _active_codes(before)
    assert "US" in _active_codes(before)

    concepts = _published_concepts(client, "terminology/imaging-modalities")
    concepts.append({"code": "PT", "display": "Positron Emission Tomography",
                     "active": True})
    r = client.put(f"{MOD}/draft", json={"concepts": concepts}, headers=ADMIN)
    assert r.status_code == 200, r.text
    draft_version = r.json()["draft_version"]
    assert r.json()["diff"] == {"added": ["PT"], "updated": [],
                                "deactivated": []}

    # Drafted but NOT published: the consumer endpoint is unchanged.
    assert "PT" not in _active_codes(_modalities(client))
    assert "PT" not in {c["code"] for c in _modalities(client)["modalities"]}

    # Publish through the EXISTING Studio publish endpoint (not this
    # module — listmgmt has no publish path of its own).
    pub = client.post(
        f"/studio/config/terminology/imaging-modalities/versions/"
        f"{draft_version}/publish", headers=ADMIN)
    assert pub.status_code == 200, pub.text
    after = _modalities(client)
    assert "PT" in _active_codes(after)
    assert after["version"] == pub.json()["version"]

    # Deactivation: US marked active:false in a new draft.
    concepts = _published_concepts(client, "terminology/imaging-modalities")
    for c in concepts:
        if c["code"] == "US":
            c["active"] = False
    r = client.put(f"{MOD}/draft", json={"concepts": concepts}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["diff"]["deactivated"] == ["US"]
    # Still unpublished: US is still active for consumers.
    assert "US" in _active_codes(_modalities(client))
    pub = client.post(
        f"/studio/config/terminology/imaging-modalities/versions/"
        f"{r.json()['draft_version']}/publish", headers=ADMIN)
    assert pub.status_code == 200, pub.text
    final = _modalities(client)
    # Gone from the ACTIVE list; the code itself is retained inactive
    # in the published concepts (records may reference it).
    assert "US" not in _active_codes(final)
    us = next(c for c in final["modalities"] if c["code"] == "US")
    assert us["active"] is False
    assert "PT" in _active_codes(final)
