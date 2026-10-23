"""PLATFORM PASS Workstream 4 — LIVE conformance subset (env-gated).

The IDENTICAL subset runs against PostgreSQL and MariaDB, parameterised
by a single fixture — if it passes on one engine and fails on the other,
the failure is an engine-portability bug by definition.

Gating: each engine's tests SKIP unless its URL env var is set:
  OPENCARE_TEST_PG_URL      e.g. postgresql+psycopg://user:pw@127.0.0.1:5432/opencare_test
  OPENCARE_TEST_MARIADB_URL e.g. mariadb+pymysql://user:pw@127.0.0.1:3306/opencare_test
The warehouse target is the SAME server with the database name suffixed
"_wh" (opencare_test -> opencare_test_wh), exported as
WAREHOUSE_DATABASE_URL for the app under test. Passwords arrive ONLY
via these env vars — never in files.

Subset (per engine, in order):
  1. GET /health
  2. dev-token auth (admin token accepted, anonymous refused)
  3. FHIR Patient create + read round-trip
  4. Studio config draft -> publish round-trip via the real studio API
  5. offline-batch endpoint accepting an hl7 item (integration/offline
     published enabled first, via the studio API)
  6. audit chain verification (app.audit.verify_chain)
  7. warehouse ETL drain into a warehouse on the SAME engine (different
     database), asserting the OMOP person row lands
Plus a second test: the dbplatform governed profile draft -> publish
round-trip on the live engine.
"""
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from app import audit as audit_module
from app import dbplatform, warehouse as wh
from app.db import Base
from app.main import create_app

ADMIN = {"Authorization": "Bearer admin-token"}

_ENGINES = [("postgres", "OPENCARE_TEST_PG_URL"),
            ("mariadb", "OPENCARE_TEST_MARIADB_URL")]


def _warehouse_url(clinical_url: str) -> str:
    base, _, db = clinical_url.rpartition("/")
    return f"{base}/{db}_wh"


@pytest.fixture(params=_ENGINES, ids=["postgres", "mariadb"])
def live(request, monkeypatch):
    engine_key, env_name = request.param
    url = os.environ.get(env_name)
    if not url:
        pytest.skip(f"{env_name} not set — live {engine_key} subset "
                    "not run")
    wh_url = _warehouse_url(url)
    # Clean slate on BOTH databases (clinical + warehouse, same server).
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    engine.dispose()
    wh_engine = create_engine(wh_url)
    wh.WarehouseBase.metadata.drop_all(wh_engine)
    wh_engine.dispose()
    monkeypatch.setenv("WAREHOUSE_DATABASE_URL", wh_url)
    application = create_app(database_url=url)
    application.include_router(dbplatform.router)
    Base.metadata.create_all(application.state.engine)
    with TestClient(application) as client:
        yield {"client": client, "app": application, "engine": engine_key,
               "url": url, "warehouse_url": wh_url}


def test_conformance_subset(live):
    client = live["client"]
    app = live["app"]
    suffix = uuid.uuid4().hex[:8]
    patient_id = f"live-{suffix}"

    # 1. Health.
    r = client.get("/health")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"

    # 2. Dev-token auth: anonymous refused, admin accepted (dbplatform
    # read surface stands in for any authenticated route).
    assert client.get("/dbplatform/engines").status_code == 401
    r = client.get("/dbplatform/engines", headers=ADMIN)
    assert r.status_code == 200, r.text

    # 3. FHIR Patient create + read round-trip.
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": patient_id,
        "name": [{"family": "Livecheck", "given": ["Conformance"]}],
        "gender": "female", "birthDate": "1990-01-15"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.get(f"/fhir/Patient/{patient_id}", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == patient_id
    assert r.json()["gender"] == "female"

    # 4. Studio config draft -> publish via the real studio API.
    form_key = f"live-form-{suffix}"
    form_payload = {"item": [{"linkId": "q1", "text": "How are you?",
                              "type": "string"}]}
    r = client.post(f"/studio/config/form/{form_key}",
                    json=form_payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "draft"
    r = client.post(
        f"/studio/config/form/{form_key}/versions/1/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    r = client.get(f"/studio/config/form/effective/{form_key}",
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["payload"] == form_payload

    # 5. Offline batch: publish integration/offline enabled first
    # (v2 over the seeded v1), then queue an hl7 item.
    r = client.get("/studio/config/integration/offline", headers=ADMIN)
    assert r.status_code == 200, r.text
    latest = max(v["version"] for v in r.json())
    r = client.post("/studio/config/integration/offline",
                    json={"enabled": True, "mode": "store-and-forward"},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    new_version = r.json()["version"]
    assert new_version == latest + 1
    r = client.post(
        f"/studio/config/integration/offline/versions/{new_version}"
        "/publish", headers=ADMIN)
    assert r.status_code == 200, r.text
    hl7_text = ("MSH|^~\\&|LIVECHK|LAB|||||ORU^R01|CTRL1|P|2.5\r"
                "PID|1||" + patient_id + "^^^MR||Livecheck^Conformance\r"
                "OBR|1|||4548-4^HbA1c^LN\r"
                "OBX|1|NM|4548-4^HbA1c^LN||7.1|%|||||F")
    r = client.post("/devices/offline/batches",
                    json={"items": [{"kind": "hl7", "payload": hl7_text}]},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["queued"] == 1
    assert r.json()["disposition"] == "held-offline"

    # 6. Audit chain verification.
    with app.state.session_factory() as session:
        assert audit_module.verify_chain(session) is True

    # 7. Warehouse ETL drain (warehouse on the SAME engine, other DB):
    # the Patient created in step 3 must land as an OMOP person row.
    r = client.post("/warehouse/etl/run", headers=ADMIN)
    assert r.status_code == 200, r.text
    stats = r.json()
    assert stats["processed"] >= 1, stats
    assert stats["dead_letter"] == 0, stats
    factory = wh.warehouse_session_factory(live["warehouse_url"])
    with factory() as wh_session:
        person = wh_session.scalars(
            select(wh.Person).where(
                wh.Person.source_patient_id == patient_id)).first()
        assert person is not None, "OMOP person row did not land"
        assert person.gender_source_value == "female"
        assert person.year_of_birth == 1990


def test_dbplatform_governance_on_live_engine(live):
    client = live["client"]
    suffix = uuid.uuid4().hex[:6]
    key = f"live-profile-{suffix}"
    payload = {"key": key, "engine": live["engine"],
               "host": "db.example.internal", "port": 5432,
               "database": "opencare", "adapter": "sqlalchemy",
               "vault_key_name": "opencare/db/live"}
    r = client.post("/dbplatform/profiles", json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post(f"/dbplatform/profiles/{key}/publish", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"
    r = client.get("/dbplatform/ddl/sqlite", headers=ADMIN)
    assert r.status_code == 200
    assert "studio_configs" in r.json()["ddl"]
