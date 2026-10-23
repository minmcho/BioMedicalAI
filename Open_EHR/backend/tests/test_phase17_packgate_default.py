"""Phase 17 WS-PACKGATE (BD-01 completion) — pack-enablement route
gating DEFAULT-ON rollout.

The gate mechanism is proven in tests/test_pack_gate.py. This file
proves the ROLLOUT posture:

(a) a fresh app — no operator gate-configuration action — GATES:
    a declaring pack that is not enabled has its routes refused,
    while the seeded-enabled core packs' routes serve;
(b) the ESCAPE HATCH: publishing integration/pack-gating
    {"enabled": false} restores the pre-Phase-17 open behaviour
    (and publishing {"enabled": true} turns gating back on);
(c) /studio and /health are ALWAYS reachable — exempt by
    construction, even when a non-enabled pack declares them and
    every core pack is disabled;
(d) the seeded production configuration serves every seeded
    navigation module's representative route under default-ON.

Plus the two load-bearing seed invariants: absent published
pack-gating config still means ON (the seed is not the only thing
holding the gate up), and the set of seeded-enabled packs is
EXACTLY the set of file packs whose manifests declare api_endpoints
— so a future pack that starts declaring routes fails this file
loudly until its seeded state is decided deliberately.

Synthetic data only.
"""
import json
from pathlib import Path

from sqlalchemy import select

from app.configstore import _DEFAULT_NAV_MODULES
from app.models import ConfigStatus, StudioConfig
from app.pack_gate import gating_enabled
from tests.conftest import ADMIN, CLINICIAN, PATIENT, VIEWER

PACKS_DIR = Path(__file__).resolve().parent.parent / "packs"


# ---------------------------------------------------------------------------
# Helpers (mirrors of tests/test_pack_gate.py's, kept local so this
# file stands alone in the Phase 17 gate)
# ---------------------------------------------------------------------------

def _publish_gating(client, enabled: bool):
    r = client.post("/studio/config/integration/pack-gating",
                    json={"enabled": enabled}, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/integration/pack-gating/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text


def _installation(client, pack_name: str) -> dict:
    installs = client.get("/studio/installations", headers=ADMIN).json()
    match = [i for i in installs if i["pack_name"] == pack_name]
    assert len(match) == 1, installs
    return match[0]


def _disable_seeded(client, pack_name: str):
    inst = _installation(client, pack_name)
    assert inst["status"] == "enabled", inst
    r = client.post(f"/studio/installations/{inst['id']}/disable",
                    headers=ADMIN)
    assert r.status_code == 200, r.text


def _author_pack(client, name: str, endpoints: list[dict]):
    r = client.post("/studio/packs", headers=ADMIN, json={
        "name": name, "version": "1.0.0",
        "manifest": {"name": name, "version": "1.0.0",
                     "description": "Phase 17 gate probe.",
                     "api_endpoints": endpoints,
                     "modules": [], "forms": []}})
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# (a) Fresh app gates with no operator gate-configuration action
# ---------------------------------------------------------------------------

def test_fresh_app_gates_unenabled_pack_and_serves_seeded_core(client):
    # A pack authored as a designer draft and NEVER installed (let
    # alone enabled) declares GET /tiering/status. No one has touched
    # the pack-gating config in this test — the gate is on purely
    # because the fresh deployment seeds it on.
    _author_pack(client, "p17-probe", [
        {"method": "GET", "path": "/tiering/status", "enabled": False}])
    r = client.get("/tiering/status", headers=VIEWER)
    assert r.status_code == 403, r.text
    assert "p17-probe" in r.json()["detail"]

    # The seeded-enabled core packs serve at the same moment.
    assert client.get("/interop/endpoints",
                      headers=ADMIN).status_code == 200
    assert client.get("/lab/catalog", headers=ADMIN).status_code == 200
    r = client.post("/fhir/Patient", headers=CLINICIAN, json={
        "resourceType": "Patient", "id": "p17-pat-1",
        "name": [{"family": "Gate", "given": ["Fresh"]}]})
    assert r.status_code == 201, r.text
    assert client.get("/fhir/Patient/p17-pat-1",
                      headers=VIEWER).status_code == 200


def test_seeded_enabled_packs_are_exactly_the_endpoint_declaring_packs(client):
    declaring = set()
    for path in sorted(PACKS_DIR.glob("*.pack.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("api_endpoints"):
            declaring.add(manifest["name"])
    assert declaring == {"clinical-core", "interop", "lab"}
    installs = client.get("/studio/installations", headers=ADMIN).json()
    # Nothing else is seeded installed (specialty / profile packs are
    # operator choices), and every seeded installation is enabled.
    assert {i["pack_name"] for i in installs} == declaring
    assert all(i["status"] == "enabled" for i in installs)
    assert all(i["installed_by"] == "system" for i in installs)


def test_absent_published_config_still_means_on(client):
    _author_pack(client, "p17-absent-probe", [
        {"method": "GET", "path": "/tiering/status", "enabled": False}])
    assert client.get("/tiering/status",
                      headers=VIEWER).status_code == 403
    # Remove the published pack-gating config from the published
    # state entirely (direct store write: there is deliberately no
    # delete API for published config). Absent must still mean ON —
    # the seeded {"enabled": true} is not the only thing holding
    # the gate up.
    factory = client.app.state.session_factory
    with factory() as session:
        rows = session.scalars(
            select(StudioConfig).where(
                StudioConfig.kind == "integration",
                StudioConfig.key == "pack-gating")).all()
        assert rows  # the seed landed
        for row in rows:
            row.status = ConfigStatus.disabled
        session.commit()
    with factory() as session:
        assert gating_enabled(session) is True
    assert client.get("/tiering/status",
                      headers=VIEWER).status_code == 403
    # And the governed escape hatch still works from that state.
    _publish_gating(client, False)
    with factory() as session:
        assert gating_enabled(session) is False
    assert client.get("/tiering/status",
                      headers=VIEWER).status_code == 200


# ---------------------------------------------------------------------------
# (b) Escape hatch
# ---------------------------------------------------------------------------

def test_escape_hatch_publishing_disabled_restores_open_behaviour(client):
    _disable_seeded(client, "interop")
    assert client.get("/interop/endpoints",
                      headers=ADMIN).status_code == 403
    # The hatch: a governed, audited config publish — the pack stays
    # disabled, the gate turns off, the route serves again (the
    # pre-Phase-17 open behaviour).
    _publish_gating(client, False)
    eff = client.get("/studio/config/integration/effective/pack-gating",
                     headers=ADMIN)
    assert eff.json()["payload"] == {"enabled": False}
    assert _installation(client, "interop")["status"] == "disabled"
    r = client.get("/interop/endpoints", headers=ADMIN)
    assert r.status_code == 200, r.text
    # The hatch is reversible through the same governance.
    _publish_gating(client, True)
    assert client.get("/interop/endpoints",
                      headers=ADMIN).status_code == 403


# ---------------------------------------------------------------------------
# (c) /studio + /health always reachable
# ---------------------------------------------------------------------------

def test_studio_and_health_reachable_with_all_core_packs_disabled(client):
    # Even a (never-installed) pack declaring the exempt surfaces
    # cannot gate them: exemption is by construction in the
    # middleware, before any pack lookup.
    _author_pack(client, "p17-exempt-probe", [
        {"method": "GET", "path": "/health", "enabled": False},
        {"method": "GET", "path": "/studio/packs", "enabled": False}])
    for pack_name in ("clinical-core", "interop", "lab"):
        _disable_seeded(client, pack_name)
    # Declared clinical routes really are gated in this state...
    assert client.get("/lab/catalog", headers=ADMIN).status_code == 403
    assert client.get("/interop/endpoints",
                      headers=ADMIN).status_code == 403
    # ...while the operator surfaces stay up.
    assert client.get("/health").status_code == 200
    assert client.get("/studio/packs", headers=ADMIN).status_code == 200
    assert client.get("/studio/installations",
                      headers=ADMIN).status_code == 200
    eff = client.get("/studio/config/integration/effective/pack-gating",
                     headers=ADMIN)
    assert eff.status_code == 200, eff.text


# ---------------------------------------------------------------------------
# (d) Broad smoke: every seeded nav module serves under default-ON
# ---------------------------------------------------------------------------

# One representative read route per seeded navigation module,
# exercised with a role the module's page gate admits.
SMOKE_ROUTES: dict[str, tuple[str, dict]] = {
    "patients": ("/fhir/Patient", CLINICIAN),
    "inbox": ("/inbox/summary", ADMIN),
    "scheduling": ("/fhir/Appointment", CLINICIAN),
    "lab": ("/lab/catalog", ADMIN),
    "pophealth": ("/pophealth/registries", ADMIN),
    "billing": ("/billing/claims", ADMIN),
    "pharmacy": ("/pharmacy/stock", ADMIN),
    "ed": ("/ed/board", ADMIN),
    "radiology": ("/dicomweb/studies", ADMIN),
    "surgery": ("/surgery/board", ADMIN),
    "beds": ("/inpatient/census", ADMIN),
    "command": ("/continuum/command/overview", ADMIN),
    "er-ops": ("/continuum/er/board", ADMIN),
    "unit-board": ("/continuum/inpatient/discharge-readiness", ADMIN),
    "clinic": ("/continuum/clinic/templates", ADMIN),
    "aftercare": ("/continuum/aftercare/calls", ADMIN),
    "safety": ("/continuum/safety/summary", ADMIN),
    "support": ("/support/config", ADMIN),
    # Inventory & Materials pass: the inventory overview smoke route.
    "inventory": ("/inventory/overview", ADMIN),
    "workforce": ("/people/staff", ADMIN),
    "teams": ("/teams/", ADMIN),
    "reports": ("/reports/catalogue", ADMIN),
    "imports": ("/reports/imports/profiles", ADMIN),
    "devices": ("/devices", ADMIN),
    "data-platform": ("/pipelines/definitions", ADMIN),
    "federation": ("/federation/peers", ADMIN),
    "research": ("/research/studies", ADMIN),
    "analytics": ("/analytics/datasets", ADMIN),
    "documents": ("/intake/documents", ADMIN),
    "studio": ("/studio/packs", ADMIN),
    "portal": ("/portal/self-scheduling/config", PATIENT),
}


def test_seeded_configuration_serves_all_nav_modules_under_default_on(client):
    seeded_modules = {module for module, _label, _roles, _group
                      in _DEFAULT_NAV_MODULES}
    # The smoke map must cover the seed exactly: a new seeded module
    # without a smoke route fails here instead of shipping ungated
    # or unserved.
    assert set(SMOKE_ROUTES) == seeded_modules
    failures = []
    for module, (path, headers) in SMOKE_ROUTES.items():
        r = client.get(path, headers=headers)
        if r.status_code != 200:
            failures.append(f"{module}: GET {path} -> {r.status_code}")
    assert not failures, "; ".join(failures)
    # The interop core pack is not a nav module but is a seeded core
    # surface: it serves too.
    assert client.get("/interop/endpoints",
                      headers=ADMIN).status_code == 200
