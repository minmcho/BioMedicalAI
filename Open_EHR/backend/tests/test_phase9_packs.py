"""Phase 9 — the 11 specialty operations packs: governance + structure.

Every pack must (a) install as a DRAFT and move through the governed
lifecycle (no self-enable: enable before approve/attach is a 409),
(b) apply its configuration at enable (order sets, forms, terminology,
registry cohort), (c) have every order set place real orders and every
form validate a complete submission through the ordinary machinery.

Deep specialty behaviour (ophthalmology laterality, rehabilitation
flowsheets, psychiatry lab gating) is proven in
test_phase9_specialty_deep.py; this file is the structural proof for
all eleven, exactly as the Phase 9 brief specifies.
"""
import json
from pathlib import Path

import pytest

from tests.conftest import ADMIN, CLINICIAN, VIEWER

PACKS_DIR = Path(__file__).resolve().parent.parent / "packs"

EXPECTED = {
    "specialty-oncology": "oncology",
    "specialty-cardiology": "cardiology",
    "specialty-diabetes-endo": "diabetes-endo",
    "specialty-gastroenterology": "gastroenterology",
    "specialty-geriatrics": "geriatrics",
    "specialty-pulmonology": "pulmonology",
    "specialty-obgyn": "obgyn",
    "specialty-ophthalmology": "ophthalmology",
    "specialty-orthopedics": "orthopedics",
    "specialty-psychiatry": "psychiatry",
    "specialty-rehabilitation": "rehabilitation",
}


def _manifest(name):
    return json.loads((PACKS_DIR / f"{name}.pack.json").read_text())


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synth", "given": ["Pack"]}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _install(client, name):
    r = client.post(f"/studio/packs/{name}/install", headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _enable(client, name):
    inst = _install(client, name)
    iid = inst["id"]
    assert client.post(f"/studio/installations/{iid}/approve",
                       headers=ADMIN).status_code == 200
    assert client.post(f"/studio/installations/{iid}/attach",
                       headers=ADMIN).status_code == 200
    r = client.post(f"/studio/installations/{iid}/enable",
                    json={"parameters": {}}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def _answer_for(item):
    type_ = item["type"]
    if type_ == "boolean":
        value = {"valueBoolean": True}
    elif type_ == "integer":
        value = {"valueInteger": 1}
    elif type_ == "decimal":
        value = {"valueDecimal": 1.5}
    elif type_ == "date":
        value = {"valueDate": "2026-01-01"}
    elif type_ == "dateTime":
        value = {"valueDateTime": "2026-01-01T09:00:00Z"}
    elif type_ == "time":
        value = {"valueTime": "09:00:00"}
    elif type_ in ("choice", "open-choice"):
        options = item.get("answerOption") or []
        code = ((options[0].get("valueCoding") or {}).get("code")
                if options else "x")
        value = {"valueCoding": {"code": code}}
    else:
        value = {"valueString": "synthetic"}
    return {"linkId": item["linkId"], "answer": [value]}


def _form_items(client, form_key):
    r = client.get(f"/studio/config/form/effective/{form_key}", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["payload"]["item"]


# ---------------------------------------------------------------------------
# Governance: install is a draft; enable out of order is refused
# ---------------------------------------------------------------------------

def test_pack_lifecycle_is_governed(client):
    inst = _install(client, "specialty-oncology")
    assert inst["status"] == "installed_draft"
    iid = inst["id"]
    # Enable directly from draft -> 409 (approve + attach are separate
    # human steps; a pack never self-enables).
    r = client.post(f"/studio/installations/{iid}/enable",
                    json={"parameters": {}}, headers=ADMIN)
    assert r.status_code == 409
    # Attach before approve -> 409.
    r = client.post(f"/studio/installations/{iid}/attach", headers=ADMIN)
    assert r.status_code == 409
    # Clinicians cannot run the pack lifecycle at all.
    assert client.post("/studio/packs/specialty-cardiology/install",
                       headers=CLINICIAN).status_code == 403
    assert client.post(f"/studio/installations/{iid}/approve",
                       headers=VIEWER).status_code == 403


def test_all_eleven_packs_exist_and_enable(client):
    names = sorted(p["name"] for p in
                   client.get("/studio/packs", headers=ADMIN).json())
    for expected in EXPECTED:
        assert expected in names
    for name in EXPECTED:
        out = _enable(client, name)
        manifest = _manifest(name)
        applied = {(a["kind"], a["key"]) for a in out["applied_configuration"]}
        declared = {(c["kind"], c["key"]) for c in manifest["configuration"]}
        assert applied == declared, (name, applied, declared)
        assert out["status"] == "enabled"


# ---------------------------------------------------------------------------
# Structure: every pack's order sets place, every form validates,
# every registry is listed
# ---------------------------------------------------------------------------

@pytest.fixture()
def all_enabled(client):
    for name in EXPECTED:
        _enable(client, name)
    _patient(client, "pack-patient")
    return client


def test_every_pack_order_set_places(all_enabled):
    sets = {s["key"]: s for s in
            all_enabled.get("/orders/sets", headers=CLINICIAN).json()["sets"]}
    for name in EXPECTED:
        manifest = _manifest(name)
        set_keys = [c["key"] for c in manifest["configuration"]
                    if c["kind"] == "orderset"]
        assert set_keys, f"{name} declares no order set"
        for key in set_keys:
            assert key in sets, f"{name}: order set {key} not published"
            r = all_enabled.post("/orders/from-set", json={
                "set": key, "patient": "pack-patient"}, headers=CLINICIAN)
            assert r.status_code == 201, (name, key, r.text)
            body = r.json()
            assert body["total"] >= 1
            assert all(o["status"] == "draft" for o in body["orders"])


def test_every_pack_form_validates(all_enabled):
    for name in EXPECTED:
        manifest = _manifest(name)
        form_keys = [c["key"] for c in manifest["configuration"]
                     if c["kind"] == "form"]
        assert form_keys, f"{name} declares no form"
        for key in form_keys:
            items = _form_items(all_enabled, key)
            answers = [_answer_for(i) for i in items
                       if i.get("required") and i["type"] not in ("group", "display")]
            r = all_enabled.post("/flowsheets", json={
                "form": key, "patient": "pack-patient",
                "answers": answers}, headers=CLINICIAN)
            assert r.status_code == 201, (name, key, r.text)


def test_every_pack_registry_is_listed(all_enabled):
    registries = {e["key"] for e in
                  all_enabled.get("/pophealth/registries",
                                  headers=CLINICIAN).json()}
    for name in EXPECTED:
        manifest = _manifest(name)
        cohort_keys = [c["key"] for c in manifest["configuration"]
                       if c["kind"] == "cohort"]
        assert cohort_keys, f"{name} declares no registry cohort"
        for key in cohort_keys:
            assert key in registries, f"{name}: registry {key} not listed"


def test_diabetes_pack_references_not_duplicates_phase5_set(all_enabled):
    """The Phase 5 seeded diabetes-admission set must survive the
    diabetes pack untouched, and the pack must not re-declare it."""
    manifest = _manifest("specialty-diabetes-endo")
    declared = {(c["kind"], c["key"]) for c in manifest["configuration"]}
    assert ("orderset", "diabetes-admission") not in declared
    sets = {s["key"] for s in
            all_enabled.get("/orders/sets", headers=CLINICIAN).json()["sets"]}
    assert "diabetes-admission" in sets  # the Phase 5 seed, still there
    assert "diabetes-insulin-titration" in sets
    assert "diabetes-screening-bundle" in sets
