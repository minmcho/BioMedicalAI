"""GET /studio/audit — the read surface for the Studio audit tab.

The audit log is one hash chain covering pack lifecycle actions, Studio
config writes and clinical writes. This endpoint exposes the most recent
entries, newest first, to admin/configurator only.
"""
from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER


def test_audit_requires_authentication(client):
    assert client.get("/studio/audit").status_code == 401


def test_audit_is_studio_roles_only(client):
    for headers in (CLINICIAN, VIEWER, PATIENT):
        assert client.get("/studio/audit", headers=headers).status_code == 403
    assert client.get("/studio/audit", headers=ADMIN).status_code == 200
    assert client.get("/studio/audit", headers=CONFIG).status_code == 200


def test_audit_entries_newest_first_with_hash_fields(client):
    # Two auditable actions: a pack install, then a config draft create.
    r = client.post("/studio/packs/lab/install", headers=ADMIN)
    assert r.status_code == 201, r.text
    # A fresh branding key (branding/default is seeded at startup by the
    # realignment, so its first draft would be v2, not v1).
    r = client.post("/studio/config/branding/audit-clinic", headers=CONFIG, json={
        "orgName": "Audit Clinic", "portalTitle": "Audit Portal"})
    assert r.status_code == 201, r.text

    entries = client.get("/studio/audit", headers=ADMIN).json()
    assert len(entries) >= 2
    actions = [e["action"] for e in entries]
    # Newest first: the config create is more recent than the install.
    assert actions.index("studio.config.create") < actions.index("studio.install")
    newest = entries[0]
    assert newest["actor"] == "dev-configurator"
    assert newest["entity"] == "config:branding/audit-clinic@v1"
    assert len(newest["entry_hash"]) == 64
    assert len(newest["prev_hash"]) == 64
    assert newest["created_at"] is not None
    ids = [e["id"] for e in entries]
    assert ids == sorted(ids, reverse=True)


def test_audit_limit_is_respected_and_bounded(client):
    client.post("/studio/packs/lab/install", headers=ADMIN)
    client.post("/studio/packs/clinical-core/install", headers=ADMIN)
    entries = client.get("/studio/audit?limit=1", headers=ADMIN).json()
    assert len(entries) == 1
    # Out-of-range limits are rejected by validation, not silently clamped.
    assert client.get("/studio/audit?limit=0", headers=ADMIN).status_code == 422
    assert client.get("/studio/audit?limit=500", headers=ADMIN).status_code == 422
