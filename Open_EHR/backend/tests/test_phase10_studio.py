"""Phase 10, Track B — Studio parity core: pack lifecycle completion
(create -> install -> approve -> attach -> enable for authored packs)
and dashboards as governed config.

Synthetic data only.
"""
from sqlalchemy import select

from app.models import AuditLog
from tests.conftest import ADMIN, CLINICIAN, CONFIG

MANIFEST = {
    "name": "phase10-authored-pack",
    "version": "1.0.0",
    "description": "Authored in tests to prove the pack lifecycle.",
    "api_endpoints": [
        {"name": "phase10-probe", "method": "GET", "path": "/health",
         "enabled": False,
         "note": "Existing surface; the pack only governs its enablement."},
    ],
}


def _create(client, manifest=None, headers=ADMIN):
    manifest = manifest or MANIFEST
    return client.post("/studio/packs", headers=headers, json={
        "name": manifest["name"], "version": manifest["version"],
        "manifest": manifest})


def _audit_actions(client):
    factory = client.app.state.session_factory
    with factory() as session:
        return [row.action for row in session.scalars(select(AuditLog)).all()]


def test_authored_pack_full_lifecycle(client):
    r = _create(client)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["name"] == "phase10-authored-pack"
    assert created["manifest"]["api_endpoints"][0]["enabled"] is False
    # Listed alongside file packs, manifest round-tripped from the DB.
    packs = client.get("/studio/packs", headers=ADMIN).json()
    entry = next(p for p in packs if p["name"] == "phase10-authored-pack")
    assert entry["version"] == "1.0.0"
    # The governed lifecycle: install -> approve -> attach -> enable.
    r = client.post("/studio/packs/phase10-authored-pack/install",
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    installation = r.json()
    assert installation["status"] == "installed_draft"
    iid = installation["id"]
    # Out-of-order transitions conflict: enable requires attached.
    assert client.post(f"/studio/installations/{iid}/enable",
                       json={"parameters": {}}, headers=ADMIN).status_code == 409
    for step, expected in (("approve", "approved"), ("attach", "attached")):
        r = client.post(f"/studio/installations/{iid}/{step}", headers=ADMIN)
        assert r.status_code == 200, (step, r.text)
        assert r.json()["status"] == expected
    r = client.post(f"/studio/installations/{iid}/enable",
                    json={"parameters": {}}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "enabled"
    actions = _audit_actions(client)
    for expected in ("studio.pack.create", "studio.install",
                     "studio.approve", "studio.attach", "studio.enable"):
        assert expected in actions


def test_authored_pack_cannot_ship_enabled_endpoints(client):
    manifest = {**MANIFEST, "name": "sneaky-pack",
                "api_endpoints": [
                    {**MANIFEST["api_endpoints"][0], "enabled": True}]}
    r = _create(client, manifest)
    assert r.status_code == 422
    assert "governed step" in r.json()["detail"]


def test_authored_pack_duplicate_conflicts_and_roles_gate(client):
    assert _create(client).status_code == 201
    assert _create(client).status_code == 409
    # Authorship needs studio.manage: clinician is refused, the
    # configurator (who holds studio.manage) may author.
    other = {**MANIFEST, "name": "clinician-pack"}
    assert _create(client, other, headers=CLINICIAN).status_code == 403
    assert _create(client, other, headers=CONFIG).status_code == 201


def test_operations_overview_dashboard_seeded_published(client):
    eff = client.get("/studio/config/dashboard/effective/operations-overview",
                     headers=CLINICIAN)
    assert eff.status_code == 200
    payload = eff.json()["payload"]
    assert payload["title"] == "Operations overview"
    endpoints = {w["endpoint"] for w in payload["widgets"]}
    # Phase 15 (BD-30): the dashboard was rewired at v2 so every
    # widget resolves through the published indicator library; the
    # original endpoint is preserved on each widget as sourceEndpoint
    # (the trace lives at GET /platform-depth/dashboard-indicators).
    assert endpoints == {"/indicators/operations-census/value",
                         "/indicators/operations-critical-open/value",
                         "/indicators/operations-verification-queue/value"}
    sources = {w["sourceEndpoint"] for w in payload["widgets"]}
    assert sources == {"/inpatient/census", "/lab/critical-reports",
                       "/lab/verification-queue"}


def test_dashboard_config_validates_and_publishes(client):
    payload = {
        "title": "Operations overview",
        "widgets": [
            {"id": "census-total", "title": "Census",
             "endpoint": "/inpatient/census", "display": "total"},
            {"id": "critical-open", "title": "Open critical results",
             "endpoint": "/lab/critical-reports", "display": "total"},
        ],
    }
    r = client.post("/studio/config/dashboard/ops", json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/studio/config/dashboard/ops/versions/1/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    eff = client.get("/studio/config/dashboard/effective/ops",
                     headers=CLINICIAN)
    assert eff.status_code == 200
    assert eff.json()["payload"]["title"] == "Operations overview"


def test_dashboard_config_validation(client):
    def post(payload):
        return client.post("/studio/config/dashboard/bad", json=payload,
                           headers=ADMIN)
    # value display requires a field name.
    assert post({"title": "t", "widgets": [
        {"id": "w", "title": "x", "endpoint": "/x", "display": "value"}
    ]}).status_code == 422
    # duplicate widget ids rejected.
    assert post({"title": "t", "widgets": [
        {"id": "w", "title": "x", "endpoint": "/x", "display": "total"},
        {"id": "w", "title": "y", "endpoint": "/y", "display": "total"},
    ]}).status_code == 422
    # endpoint must be a local path.
    assert post({"title": "t", "widgets": [
        {"id": "w", "title": "x", "endpoint": "https://evil.example/x",
         "display": "total"}]}).status_code == 422
