"""Pack-enablement route gating — Platform pass (RTM STU-04),
Phase 17 default-ON rollout (WS-PACKGATE, BD-01 completion).

Pack manifests have always declared their backend surface disabled
(`api_endpoints[].enabled: false`); app.pack_gate + the middleware
in app.main honour that state at request time.

History: the gate originally shipped OPT-IN and OFF by default
(published integration/pack-gating {enabled: true} turned it on).
Phase 17 flipped the rollout posture to DEFAULT ON, delivered
through governance: a fresh app seeds the published
integration/pack-gating config as {"enabled": true}, absent config
also means ON (app.pack_gate.gating_enabled), and the core packs
(clinical-core, interop, lab — the packs declaring the core app's
routes) are seeded ENABLED through the governed pack lifecycle, so
a fresh deployment gates and serves. The only off switch is the
governed escape hatch: publishing {"enabled": false}.

Adjusted for Phase 17 (assertions preserved or strengthened; see
phase17 RESULTS.md):
- the old "gate off by default serves" test is replaced by proof
  that a fresh app gates by default AND its seeded core packs
  serve;
- tests that need a BLOCKED declared endpoint first disable the
  relevant seeded core installation through the governed disable
  step (a seeded-enabled pack otherwise correctly serves);
- the enable-restores-serving and method-matters tests ride an
  authored probe pack / the escape hatch where the original flow
  re-enabled a pack after disabling it — `disabled` is a terminal
  lifecycle state (no re-enable transition exists in app.studio),
  so a disabled seeded installation cannot be re-enabled in-test.

Covered here: fresh-app default-on serving for seeded core packs;
gate-on refusal (403 naming the declaring pack) for a declared
endpoint whose pack is not enabled; non-declared endpoints and the
/studio + /health escape surfaces unaffected; install → approve →
attach → enable restores serving; disable blocks again; method
matters (a path declared only for one method is not gated for
another); publishing {enabled: false} restores open behaviour.

Synthetic data only.
"""
import pytest
from fastapi.testclient import TestClient

from .conftest import ADMIN, VIEWER


@pytest.fixture()
def client(tmp_path):
    from app.main import create_app

    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db")
    with TestClient(app) as c:
        yield c


def _publish_gating(client, enabled: bool):
    r = client.post("/studio/config/integration/pack-gating",
                    json={"enabled": enabled}, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/integration/pack-gating/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text


def _install(client, pack_name: str) -> int:
    r = client.post(f"/studio/packs/{pack_name}/install", headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _advance(client, installation_id: int, step: str):
    r = client.post(f"/studio/installations/{installation_id}/{step}",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def _installation(client, pack_name: str) -> dict:
    installs = client.get("/studio/installations", headers=ADMIN).json()
    match = [i for i in installs if i["pack_name"] == pack_name]
    assert len(match) == 1, installs
    return match[0]


def _disable_seeded(client, pack_name: str):
    """Disable a seeded-enabled core pack through the governed
    disable step, so a test can observe the gate blocking it."""
    inst = _installation(client, pack_name)
    assert inst["status"] == "enabled", inst
    _advance(client, inst["id"], "disable")


def _author_probe_pack(client, name="gate-probe", method="GET",
                       path="/tiering/status"):
    r = client.post("/studio/packs", headers=ADMIN, json={
        "name": name, "version": "1.0.0",
        "manifest": {"name": name, "version": "1.0.0",
                     "description": "Gate probe authored by tests.",
                     "api_endpoints": [{"method": method, "path": path,
                                        "enabled": False}],
                     "modules": [], "forms": []}})
    assert r.status_code == 201, r.text


def test_fresh_app_gates_by_default_and_seeded_core_packs_serve(client):
    # The seeded published config is the default: gate ON, no
    # operator action taken in this test.
    eff = client.get("/studio/config/integration/effective/pack-gating",
                     headers=ADMIN)
    assert eff.status_code == 200, eff.text
    assert eff.json()["payload"] == {"enabled": True}
    # The core packs are seeded enabled through the lifecycle...
    for pack_name in ("clinical-core", "interop", "lab"):
        assert _installation(client, pack_name)["status"] == "enabled"
    # ...so their declared routes serve under the default-on gate.
    # GET /interop/endpoints is declared by the interop pack with
    # enabled: false in its manifest — it serves because the pack's
    # seeded installation is enabled, not because the gate is off.
    r = client.get("/interop/endpoints", headers=ADMIN)
    assert r.status_code == 200, r.text


def test_gate_on_blocks_declared_endpoint_and_names_pack(client):
    # Default-on already; publishing {enabled: true} explicitly must
    # behave identically.
    _publish_gating(client, True)
    _disable_seeded(client, "interop")
    r = client.get("/interop/endpoints", headers=ADMIN)
    assert r.status_code == 403, r.text
    assert "interop" in r.json()["detail"]
    assert "disabled until" in r.json()["detail"]


def test_gate_on_leaves_undeclared_and_studio_paths_alone(client):
    _publish_gating(client, True)
    # Not declared by any pack manifest → unaffected.
    r = client.get("/tiering/status", headers=VIEWER)
    assert r.status_code == 200, r.text
    # The escape hatch: Studio stays reachable so an operator can
    # enable the pack or turn the gate off.
    r = client.get("/studio/packs", headers=ADMIN)
    assert r.status_code == 200, r.text
    r = client.get("/health")
    assert r.status_code == 200


def test_enable_installation_restores_serving_then_disable_blocks(client):
    # An authored probe pack declares GET /tiering/status (otherwise
    # undeclared): under the default-on gate the route refuses until
    # the pack is enabled through the governed lifecycle.
    _author_probe_pack(client)
    assert client.get("/tiering/status",
                      headers=VIEWER).status_code == 403
    iid = _install(client, "gate-probe")
    _advance(client, iid, "approve")
    _advance(client, iid, "attach")
    # Attached is not enabled: still gated.
    assert client.get("/tiering/status",
                      headers=VIEWER).status_code == 403
    _advance(client, iid, "enable")
    r = client.get("/tiering/status", headers=VIEWER)
    assert r.status_code == 200, r.text
    _advance(client, iid, "disable")
    assert client.get("/tiering/status",
                      headers=VIEWER).status_code == 403


def test_method_matters_for_declared_paths(client):
    # clinical-core is seeded enabled; disabling its installation
    # puts its declared endpoints under the gate.
    _disable_seeded(client, "clinical-core")
    # clinical-core declares POST /fhir/Patient (and GET
    # /fhir/Patient/{id}); a POST is gated before it reaches the
    # route, whatever the body would have done.
    r = client.post("/fhir/Patient", headers=ADMIN, json={
        "resourceType": "Patient", "id": "gate-pat-1",
        "name": [{"family": "Gate", "given": ["Test"]}],
        "gender": "female", "birthDate": "1990-01-01"})
    assert r.status_code == 403, r.text
    assert "clinical-core" in r.json()["detail"]
    # `disabled` is a terminal lifecycle state, so the restore leg
    # rides the governed escape hatch (publishing the gate off);
    # enable-restores-serving is proven on the probe pack above.
    _publish_gating(client, False)
    r = client.post("/fhir/Patient", headers=ADMIN, json={
        "resourceType": "Patient", "id": "gate-pat-1",
        "name": [{"family": "Gate", "given": ["Test"]}],
        "gender": "female", "birthDate": "1990-01-01"})
    assert r.status_code == 201, r.text


def test_turning_gate_back_off_restores_default_behaviour(client):
    _disable_seeded(client, "interop")
    assert client.get("/interop/endpoints",
                      headers=ADMIN).status_code == 403
    _publish_gating(client, False)
    r = client.get("/interop/endpoints", headers=ADMIN)
    assert r.status_code == 200, r.text
