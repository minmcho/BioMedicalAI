"""Phase 17 WS-DBPLAT (BD-64) — DB platform v1 remainders.

Covers: the test-connection action (a REAL handshake, recorded as
governed data, credential hygiene), the governed hot-swap (verified-
profile gate, pre/post verification, writes land in the NEW engine —
asserted against both database files directly), automatic rollback
on post-swap health failure, manual rollback, and the audit entries
for every action.

Composition follows the platform-pass pattern: create_app on a tmp
SQLite file (the dbplatform router is mounted by create_app itself).
The swap targets are second/third SQLite files named by governed
profiles — a profile's `database` IS the SQLite file path, so the
two engines are two real databases on disk.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from app import dbplatform
from app.audit import verify_chain
from app.db import Base
from app.main import create_app
from app.models import AuditLog

ADMIN = {"Authorization": "Bearer admin-token"}
CONFIG = {"Authorization": "Bearer config-token"}
CLINICIAN = {"Authorization": "Bearer clinician-token"}
VIEWER = {"Authorization": "Bearer viewer-token"}

SECRET = "sup3r-secret-value-17"


@pytest.fixture()
def env(tmp_path):
    src = tmp_path / "source.db"
    application = create_app(database_url=f"sqlite:///{src}")
    Base.metadata.create_all(application.state.engine)
    with TestClient(application) as client:
        yield {"app": application, "client": client, "tmp": tmp_path,
               "src": src, "src_factory": application.state.session_factory}


def _sqlite_profile(key, db_path, **overrides):
    payload = {"key": key, "engine": "sqlite", "host": "localhost",
               "port": 9, "database": str(db_path),
               "adapter": "sqlalchemy",
               "vault_key_name": "opencare/db/target"}
    payload.update(overrides)
    return payload


def _publish(client, payload):
    r = client.post("/dbplatform/profiles", json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post(f"/dbplatform/profiles/{payload['key']}/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def _patient_ids(db_path):
    import os
    if not os.path.exists(db_path):
        return set()
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            return set(conn.execute(text(
                "SELECT resource_id FROM fhir_resources "
                "WHERE resource_type = 'Patient'")).scalars().all())
    finally:
        engine.dispose()


def _create_patient(client, patient_id):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": patient_id,
        "name": [{"family": "Swapcheck", "given": ["Dbplat"]}],
        "gender": "female", "birthDate": "1990-01-15"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r


def _audit_actions(factory):
    with factory() as session:
        return [row.action for row in session.scalars(
            select(AuditLog).order_by(AuditLog.id.asc())).all()]


# ---------------------------------------------------------------------------
# Test-connection: real handshake, governed record, credential hygiene
# ---------------------------------------------------------------------------

def test_test_connection_records_success(env):
    client = env["client"]
    target = env["tmp"] / "target.db"
    _publish(client, _sqlite_profile("target-sqlite", target))
    r = client.post("/dbplatform/profiles/target-sqlite/test-connection",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["profile_key"] == "target-sqlite"
    assert body["profile_version"] == 1
    assert body["engine"] == "sqlite"
    assert body["actor"] == "dev-admin"
    assert body["server_version"]  # sqlite_version() probe answered
    assert body["latency_ms"] >= 0
    assert body["error_class"] is None
    # The record is governed data: listed back, and it VERIFIES the
    # profile for swap.
    r = client.get("/dbplatform/profiles/target-sqlite/test-connections",
                   headers=VIEWER)
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 1
    assert r.json()["verification"]["verified"] is True
    # Audited, hash chain intact.
    assert "dbplatform.profile.test_connection" in \
        _audit_actions(env["src_factory"])
    with env["src_factory"]() as session:
        assert verify_chain(session) is True


def test_test_connection_records_failure_for_unreachable_target(env):
    client = env["client"]
    _publish(client, {"key": "dead-pg", "engine": "postgres",
                      "host": "127.0.0.1", "port": 1,
                      "database": "opencare", "adapter": "sqlalchemy",
                      "vault_key_name": "opencare/db/dead"})
    r = client.post("/dbplatform/profiles/dead-pg/test-connection",
                    headers=ADMIN)
    # A failed handshake is a recorded outcome, not an API error.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is False
    assert body["error_class"]
    assert body["server_version"] is None
    r = client.get("/dbplatform/profiles/dead-pg/test-connections",
                   headers=ADMIN)
    assert r.json()["verification"]["verified"] is False


def test_test_connection_never_leaks_credential_material(env, monkeypatch):
    # The vault seam is the process environment: the env var named by
    # the (normalised) vault key holds the password for this profile.
    monkeypatch.setenv("OPENCARE_DB_PRIMARY", SECRET)
    client = env["client"]
    _publish(client, {"key": "secret-pg", "engine": "postgres",
                      "host": "127.0.0.1", "port": 1,
                      "database": "opencare", "adapter": "sqlalchemy",
                      "vault_key_name": "opencare/db/primary"})
    r = client.post("/dbplatform/profiles/secret-pg/test-connection",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    assert SECRET not in r.text
    listing = client.get(
        "/dbplatform/profiles/secret-pg/test-connections",
        headers=ADMIN)
    assert SECRET not in listing.text
    # ... and nothing in the stored governed record carries it either.
    with env["src_factory"]() as session:
        rows = session.scalars(select(dbplatform.DbConnectionTest)).all()
        assert rows, "the handshake must have been recorded"
        for row in rows:
            assert SECRET not in str(row.error_detail or "")
            assert SECRET not in str(row.server_version or "")
            assert SECRET not in str(row.error_class or "")


def test_test_connection_unknown_or_unpublished_profile_404(env):
    client = env["client"]
    assert client.post("/dbplatform/profiles/nope/test-connection",
                       headers=ADMIN).status_code == 404
    r = client.post("/dbplatform/profiles",
                    json=_sqlite_profile("draft-only",
                                         env["tmp"] / "d.db"),
                    headers=ADMIN)
    assert r.status_code == 201
    assert client.post("/dbplatform/profiles/draft-only/test-connection",
                       headers=ADMIN).status_code == 404


def test_test_connection_requires_manage(env):
    client = env["client"]
    _publish(client, _sqlite_profile("gated", env["tmp"] / "g.db"))
    for headers in (CLINICIAN, VIEWER):
        r = client.post("/dbplatform/profiles/gated/test-connection",
                        headers=headers)
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Hot-swap: the verification gate
# ---------------------------------------------------------------------------

def test_swap_refused_without_test_connection(env):
    client = env["client"]
    _publish(client, _sqlite_profile("unverified", env["tmp"] / "u.db"))
    r = client.post("/dbplatform/profiles/unverified/swap", headers=ADMIN)
    assert r.status_code == 409, r.text
    assert "test-connection" in r.json()["detail"]
    assert client.get("/dbplatform/runtime",
                      headers=ADMIN).json()["active_profile"] == "startup"
    assert "dbplatform.swap.refused" in _audit_actions(env["src_factory"])


def test_swap_refused_when_latest_test_failed(env):
    client = env["client"]
    _publish(client, {"key": "failing", "engine": "postgres",
                      "host": "127.0.0.1", "port": 1,
                      "database": "opencare", "adapter": "sqlalchemy",
                      "vault_key_name": "opencare/db/dead"})
    client.post("/dbplatform/profiles/failing/test-connection",
                headers=ADMIN)
    r = client.post("/dbplatform/profiles/failing/swap", headers=ADMIN)
    assert r.status_code == 409, r.text


def test_swap_refused_after_profile_changes_until_retested(env):
    client = env["client"]
    target = env["tmp"] / "changing.db"
    _publish(client, _sqlite_profile("changing", target))
    client.post("/dbplatform/profiles/changing/test-connection",
                headers=ADMIN)
    # A new published version is a config change AFTER the test.
    r = client.post("/dbplatform/profiles",
                    json=_sqlite_profile("changing", target, port=10),
                    headers=CONFIG)
    assert r.status_code == 201, r.text
    r = client.post("/dbplatform/profiles/changing/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    r = client.post("/dbplatform/profiles/changing/swap", headers=ADMIN)
    assert r.status_code == 409, r.text
    assert "changed after" in r.json()["detail"]
    # Re-testing the new version re-verifies it; the swap then runs.
    r = client.post("/dbplatform/profiles/changing/test-connection",
                    headers=ADMIN)
    assert r.json()["success"] is True
    assert r.json()["profile_version"] == 2
    r = client.post("/dbplatform/profiles/changing/swap", headers=ADMIN)
    assert r.status_code == 200, r.text


def test_swap_requires_manage(env):
    client = env["client"]
    _publish(client, _sqlite_profile("gated2", env["tmp"] / "g2.db"))
    client.post("/dbplatform/profiles/gated2/test-connection",
                headers=ADMIN)
    assert client.post("/dbplatform/profiles/gated2/swap",
                       headers=CLINICIAN).status_code == 403
    assert client.post("/dbplatform/runtime/rollback",
                       headers=VIEWER).status_code == 403


# ---------------------------------------------------------------------------
# Hot-swap: the real rebind (writes land in the NEW engine)
# ---------------------------------------------------------------------------

def test_successful_swap_writes_land_in_new_engine(env):
    client, app = env["client"], env["app"]
    target = env["tmp"] / "swapped.db"
    _publish(client, _sqlite_profile("target-sqlite", target))
    client.post("/dbplatform/profiles/target-sqlite/test-connection",
                headers=ADMIN)
    _create_patient(client, "before-swap")
    assert "before-swap" in _patient_ids(env["src"])

    r = client.post("/dbplatform/profiles/target-sqlite/swap",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["active_profile"] == "target-sqlite"
    assert body["health"]["health"] == "ok"
    assert body["health"]["dialect"] == "sqlite"
    assert body["event"]["outcome"] == "swapped"

    runtime = client.get("/dbplatform/runtime", headers=ADMIN).json()
    assert runtime["active_profile"] == "target-sqlite"
    assert runtime["can_rollback"] is True

    # THE PROOF: a write through the API now lands in the NEW
    # database — asserted against both files directly.
    _create_patient(client, "after-swap")
    assert "after-swap" in _patient_ids(target)
    assert "after-swap" not in _patient_ids(env["src"])
    # Reads rebind too: the pre-swap patient is not on the new engine.
    assert client.get("/fhir/Patient/after-swap",
                      headers=ADMIN).status_code == 200
    assert client.get("/fhir/Patient/before-swap",
                      headers=ADMIN).status_code == 404
    assert client.get("/health").json()["status"] == "ok"

    # Audit split by design: intent on the source side, the swap
    # outcome on the (now authoritative) target side.
    src_actions = _audit_actions(env["src_factory"])
    assert "dbplatform.swap.start" in src_actions
    assert "dbplatform.profile.test_connection" in src_actions
    with app.state.session_factory() as session:
        target_actions = [row.action for row in session.scalars(
            select(AuditLog).order_by(AuditLog.id.asc())).all()]
        assert "dbplatform.swap" in target_actions
        assert verify_chain(session) is True


def test_manual_rollback_restores_previous_engine(env):
    client = env["client"]
    target = env["tmp"] / "rolled.db"
    _publish(client, _sqlite_profile("target-sqlite", target))
    client.post("/dbplatform/profiles/target-sqlite/test-connection",
                headers=ADMIN)
    assert client.post("/dbplatform/profiles/target-sqlite/swap",
                       headers=ADMIN).status_code == 200
    _create_patient(client, "on-target")
    assert "on-target" in _patient_ids(target)

    r = client.post("/dbplatform/runtime/rollback", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["active_profile"] is None
    assert r.json()["event"]["outcome"] == "manual_rollback"
    runtime = client.get("/dbplatform/runtime", headers=ADMIN).json()
    assert runtime["active_profile"] == "startup"

    # Writes land back on the source engine; the target's patient is
    # no longer visible through the API.
    _create_patient(client, "back-home")
    assert "back-home" in _patient_ids(env["src"])
    assert "back-home" not in _patient_ids(target)
    assert client.get("/fhir/Patient/on-target",
                      headers=ADMIN).status_code == 404
    actions = _audit_actions(env["src_factory"])
    assert "dbplatform.rollback" in actions
    events = [e for e in client.get("/dbplatform/runtime",
                                    headers=ADMIN)
              .json()["swap_events"]
              if e["outcome"] == "manual_rollback"]
    assert len(events) == 1


def test_rollback_without_swap_is_409(env):
    r = env["client"].post("/dbplatform/runtime/rollback", headers=ADMIN)
    assert r.status_code == 409, r.text


def test_post_swap_failure_rolls_back_automatically(env, monkeypatch):
    client = env["client"]
    target = env["tmp"] / "fragile.db"
    _publish(client, _sqlite_profile("fragile", target))
    client.post("/dbplatform/profiles/fragile/test-connection",
                headers=ADMIN)

    def _boom(app):
        raise RuntimeError("injected post-swap failure")

    monkeypatch.setattr(dbplatform, "_post_swap_health_probe", _boom)
    r = client.post("/dbplatform/profiles/fragile/swap", headers=ADMIN)
    assert r.status_code == 502, r.text
    assert "rolled back" in r.json()["detail"]

    # The runtime is back on the startup engine, for reads AND writes.
    runtime = client.get("/dbplatform/runtime", headers=ADMIN).json()
    assert runtime["active_profile"] == "startup"
    assert runtime["can_rollback"] is False
    _create_patient(client, "still-home")
    assert "still-home" in _patient_ids(env["src"])
    assert "still-home" not in _patient_ids(target)
    # The rollback itself is governed data + audit on the source side.
    actions = _audit_actions(env["src_factory"])
    assert "dbplatform.swap.rolled_back" in actions
    with env["src_factory"]() as session:
        events = session.scalars(select(dbplatform.DbSwapEvent)).all()
        assert [e.outcome for e in events] == ["rolled_back"]
        assert verify_chain(session) is True


def test_swap_to_document_engine_profile_refused(env):
    # mongodb-ferretdb has no SQLAlchemy seam: the handshake honestly
    # fails, so the profile can never verify, so it can never swap.
    client = env["client"]
    _publish(client, {"key": "docs", "engine": "mongodb-ferretdb",
                      "host": "db.example.internal", "port": 27017,
                      "database": "opencare_docs",
                      "adapter": "sqlalchemy",
                      "vault_key_name": "opencare/db/docs"})
    r = client.post("/dbplatform/profiles/docs/test-connection",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["success"] is False
    assert r.json()["error_class"] == "ValueError"
    r = client.post("/dbplatform/profiles/docs/swap", headers=ADMIN)
    assert r.status_code == 409, r.text
