"""Phase 17 WS-DBPLAT (BD-64) — LIVE hot-swap drill (env-gated).

Follows tests/test_platform_db_live.py's pattern exactly: each
engine's drill SKIPS unless its URL env var is set:
  OPENCARE_TEST_PG_URL      e.g. postgresql+psycopg://user:pw@127.0.0.1:5432/opencare_test
  OPENCARE_TEST_MARIADB_URL e.g. mariadb+pymysql://user:pw@127.0.0.1:3306/opencare_test

Drill (per engine): the app boots on a throwaway SQLite source; a
governed profile names the live engine (its vault_key_name IS the
env var name, so the credential resolves from the environment, never
from config); test-connection performs the real handshake; the
hot-swap rebinds the runtime to the live engine; a FHIR Patient
written through the API is asserted IN the live database (direct
engine read) and NOT in the SQLite source; manual rollback returns
writes to SQLite. Passwords arrive ONLY via the env vars.
"""
import os
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.db import Base
from app.main import create_app

ADMIN = {"Authorization": "Bearer admin-token"}

_ENGINES = [("postgres", "OPENCARE_TEST_PG_URL"),
            ("mariadb", "OPENCARE_TEST_MARIADB_URL")]


@pytest.fixture(params=_ENGINES, ids=["postgres", "mariadb"])
def live(request, tmp_path):
    engine_key, env_name = request.param
    url = os.environ.get(env_name)
    if not url:
        pytest.skip(f"{env_name} not set — live {engine_key} swap "
                    "drill not run")
    # Clean slate on the live target.
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    engine.dispose()
    src = tmp_path / "source.db"
    application = create_app(database_url=f"sqlite:///{src}")
    Base.metadata.create_all(application.state.engine)
    with TestClient(application) as client:
        yield {"client": client, "app": application, "src": src,
               "engine": engine_key, "env_name": env_name, "url": url}


def _patient_ids_in(url, tmp_path):
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return set(conn.execute(text(
                "SELECT resource_id FROM fhir_resources "
                "WHERE resource_type = 'Patient'")).scalars().all())
    finally:
        engine.dispose()


def _patient_ids_sqlite(path):
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            return set(conn.execute(text(
                "SELECT resource_id FROM fhir_resources "
                "WHERE resource_type = 'Patient'")).scalars().all())
    finally:
        engine.dispose()


def test_live_hot_swap_drill(live):
    client = live["client"]
    parsed = urlparse(live["url"])
    key = f"live-{live['engine']}"
    payload = {"key": key, "engine": live["engine"],
               "host": parsed.hostname or "127.0.0.1",
               "port": parsed.port or 5432,
               "database": (parsed.path or "/opencare_test").lstrip("/"),
               "adapter": "sqlalchemy",
               # The vault key NAME is the env var itself: the full
               # URL (credential included) resolves from the process
               # environment, exactly the platform's live-test seam.
               "vault_key_name": live["env_name"]}
    r = client.post("/dbplatform/profiles", json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post(f"/dbplatform/profiles/{key}/publish", headers=ADMIN)
    assert r.status_code == 200, r.text

    # Real handshake against the live server.
    r = client.post(f"/dbplatform/profiles/{key}/test-connection",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True, r.json()
    assert r.json()["server_version"]

    # Hot-swap SQLite -> live engine.
    r = client.post(f"/dbplatform/profiles/{key}/swap", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["health"]["health"] == "ok"

    patient_id = f"swap-live-{live['engine']}"
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": patient_id,
        "name": [{"family": "Livedrill", "given": ["Swap"]}],
        "gender": "female", "birthDate": "1990-01-15"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    # The write landed in the LIVE database, not the SQLite source.
    assert patient_id in _patient_ids_in(live["url"], None)
    assert patient_id not in _patient_ids_sqlite(live["src"])
    r = client.get(f"/fhir/Patient/{patient_id}", headers=ADMIN)
    assert r.status_code == 200, r.text

    # Manual rollback: writes return to the SQLite source.
    r = client.post("/dbplatform/runtime/rollback", headers=ADMIN)
    assert r.status_code == 200, r.text
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": "swap-live-back",
        "name": [{"family": "Livedrill", "given": ["Back"]}],
        "gender": "male", "birthDate": "1985-06-30"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert "swap-live-back" in _patient_ids_sqlite(live["src"])
    assert "swap-live-back" not in _patient_ids_in(live["url"], None)
