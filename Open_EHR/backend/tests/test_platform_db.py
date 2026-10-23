"""PLATFORM PASS Workstream 4 — database platform (default gate, SQLite).

Covers: the ENGINES adapter matrix, governed connection profiles
(credential refusal is a HARD rule), canonical DDL export per dialect,
the schema designer read model + additive-only governed extensions, and
the transformer workbench preview (pure — writes nothing).

Composition follows the platform-pass pattern: create_app on a tmp
SQLite file, the dbplatform router included in the test (main.py is
NOT edited by this workstream), metadata created after import.
"""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import dbplatform
from app.db import Base
from app.main import create_app
from app.models import AuditLog, StudioConfig

ADMIN = {"Authorization": "Bearer admin-token"}
CONFIG = {"Authorization": "Bearer config-token"}
CLINICIAN = {"Authorization": "Bearer clinician-token"}
VIEWER = {"Authorization": "Bearer viewer-token"}

EXPECTED_ENGINES = {"postgres", "sqlite", "mysql", "mariadb", "mssql",
                    "oracle", "mongodb-ferretdb"}


@pytest.fixture()
def app(tmp_path):
    application = create_app(database_url=f"sqlite:///{tmp_path}/t.db")
    application.include_router(dbplatform.router)
    Base.metadata.create_all(application.state.engine)
    return application


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


def _profile(key="primary-pg", **overrides):
    payload = {"key": key, "engine": "postgres",
               "host": "db.example.internal", "port": 5432,
               "database": "opencare", "adapter": "sqlalchemy",
               "vault_key_name": "opencare/db/primary"}
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# (a) Engines matrix
# ---------------------------------------------------------------------------

def test_engines_matrix_shape():
    matrix = dbplatform.engines_matrix()
    assert {e["engine"] for e in matrix} == EXPECTED_ENGINES
    for entry in matrix:
        assert entry["display_name"]
        assert "sqlalchemy_dialect" in entry
        assert entry["driver_package"]
        caps = entry["capabilities"]
        assert isinstance(caps["json_document_store"], bool)
        assert caps["vector_search"] in ("native-pgvector", "external",
                                         "none")
        assert isinstance(caps["transactional_ddl"], bool)
        assert caps["identifier_case"]
        assert entry["verification"]
    pg = dbplatform.ENGINES_BY_KEY["postgres"]
    assert pg["capabilities"]["vector_search"] == "native-pgvector"
    assert pg["verification"] == "live-verified (conformance subset)"
    assert dbplatform.ENGINES_BY_KEY["sqlite"]["verification"] == \
        "live-verified (baseline suite)"
    assert dbplatform.ENGINES_BY_KEY["mariadb"]["verification"] == \
        "live-verified (conformance subset)"
    for key in ("mssql", "oracle"):
        assert dbplatform.ENGINES_BY_KEY[key]["verification"] == \
            "dialect-only — live NOT RUN"


def test_engines_matrix_has_no_credentials():
    for entry in dbplatform.engines_matrix():
        example = entry["url_example"]
        assert "password" not in example.lower()
        # No userinfo with a password (user:pass@) in any example URL.
        assert not re.search(r"://[^/\s:@]+:[^/\s@]+@", example), example


def test_engines_endpoint_auth(client):
    assert client.get("/dbplatform/engines").status_code == 401
    for headers in (ADMIN, CONFIG, CLINICIAN, VIEWER):
        r = client.get("/dbplatform/engines", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["total"] == len(EXPECTED_ENGINES)


def test_ferretdb_matrix_is_honest_about_no_app_seam():
    entry = dbplatform.ENGINES_BY_KEY["mongodb-ferretdb"]
    assert entry["sqlalchemy_dialect"] is None
    assert "NO MongoDB adapter seam" in entry["capabilities"]["notes"]


# ---------------------------------------------------------------------------
# (a) Governed profiles + the credential hard rule
# ---------------------------------------------------------------------------

def test_profile_draft_publish_supersede(client):
    r = client.post("/dbplatform/profiles", json=_profile(), headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "draft"
    assert r.json()["version"] == 1
    r = client.post("/dbplatform/profiles/primary-pg/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"
    # Second generation supersedes the first.
    r = client.post("/dbplatform/profiles",
                    json=_profile(port=5433), headers=CONFIG)
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2
    r = client.post("/dbplatform/profiles/primary-pg/publish",
                    headers=ADMIN)
    assert r.status_code == 200
    items = client.get("/dbplatform/profiles",
                       headers=VIEWER).json()["items"]
    mine = [i for i in items if i["key"] == "primary-pg"]
    # Viewer sees published only.
    assert len(mine) == 1 and mine[0]["version"] == 2
    assert mine[0]["payload"]["port"] == 5433
    all_items = client.get("/dbplatform/profiles",
                           headers=ADMIN).json()["items"]
    statuses = {i["version"]: i["status"] for i in all_items
                if i["key"] == "primary-pg"}
    assert statuses == {1: "superseded", 2: "published"}


def test_profile_writes_require_manage(client):
    r = client.post("/dbplatform/profiles", json=_profile(),
                    headers=CLINICIAN)
    assert r.status_code == 403
    r = client.post("/dbplatform/profiles", json=_profile(),
                    headers=VIEWER)
    assert r.status_code == 403


@pytest.mark.parametrize("bad", [
    {"password": "hunter2"},
    {"secret": "x"},
    {"token": "x"},
    {"connection_string": "postgresql://u:p@h:5432/db"},
    {"url": "postgresql://user:pass@db.example.internal:5432/opencare"},
    {"nested": {"password": "deep"}},
    {"note": "postgres://user:pass@host/db"},
    {"note": "Server=h;Password=abc;"},
])
def test_profile_credential_fields_refused_422(client, bad):
    payload = _profile(key="bad-profile")
    payload.update(bad)
    r = client.post("/dbplatform/profiles", json=payload, headers=ADMIN)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("overrides", [
    {"engine": "db2"},
    {"port": 0},
    {"port": "5432"},
    {"host": "https://db.example.internal"},
    {"vault_key_name": ""},
    {"vault_key_name": "the actual secret value!"},
    {"database": ""},
])
def test_profile_structural_validation_422(client, overrides):
    r = client.post("/dbplatform/profiles",
                    json=_profile(**overrides), headers=ADMIN)
    assert r.status_code == 422, r.text


def test_profile_publish_unknown_key_404(client):
    r = client.post("/dbplatform/profiles/nope/publish", headers=ADMIN)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# (b) Canonical DDL export
# ---------------------------------------------------------------------------

# Per-dialect markers: studio_configs must render, and the JSON payload
# column must render in the dialect's JSON-ish form (probed against
# SQLAlchemy 2.1.4: MSSQL renders generic JSON as NVARCHAR(max)).
DDL_MARKERS = {
    "postgres": ["CREATE TABLE studio_configs", "payload JSON", "SERIAL"],
    "sqlite": ["CREATE TABLE studio_configs", "payload JSON"],
    "mysql": ["CREATE TABLE studio_configs", "payload JSON",
              "AUTO_INCREMENT"],
    "mariadb": ["CREATE TABLE studio_configs", "payload JSON"],
    "mssql": ["CREATE TABLE studio_configs", "NVARCHAR(max)", "IDENTITY"],
    "oracle": ["CREATE TABLE studio_configs", "payload JSON", "VARCHAR2"],
}


@pytest.mark.parametrize("engine", sorted(DDL_MARKERS))
def test_ddl_per_dialect(client, engine):
    r = client.get(f"/dbplatform/ddl/{engine}", headers=ADMIN)
    assert r.status_code == 200, r.text
    ddl = r.json()["ddl"]
    for marker in DDL_MARKERS[engine]:
        assert marker in ddl, (engine, marker)
    # The full app schema + the warehouse variant both render.
    assert "fhir_resources" in ddl
    assert "concept_map" in ddl  # warehouse table
    assert "person" in ddl


@pytest.mark.parametrize("engine", sorted(DDL_MARKERS))
def test_ddl_service_function(engine):
    ddl = dbplatform.generate_ddl(engine)
    assert "studio_configs" in ddl
    app_only = dbplatform.generate_ddl(engine, "app")
    assert "concept_map" not in app_only
    wh_only = dbplatform.generate_ddl(engine, "warehouse")
    assert "concept_map" in wh_only and "studio_configs" not in wh_only


def test_ddl_unknown_engine_422(client):
    assert client.get("/dbplatform/ddl/db2",
                      headers=ADMIN).status_code == 422
    with pytest.raises(ValueError):
        dbplatform.generate_ddl("db2")


def test_ddl_ferretdb_has_no_sql_ddl_422(client):
    r = client.get("/dbplatform/ddl/mongodb-ferretdb", headers=ADMIN)
    assert r.status_code == 422
    assert "document" in r.json()["detail"].lower()


def test_dialect_imports():
    from sqlalchemy.dialects import mssql, mysql, oracle, postgresql, \
        sqlite  # noqa: F401
    import sqlalchemy
    for name in ("postgresql", "sqlite", "mysql", "mssql", "oracle"):
        assert sqlalchemy.dialects.registry.load(name) is not None


# ---------------------------------------------------------------------------
# (c) Schema designer
# ---------------------------------------------------------------------------

def test_schema_read_model(client):
    r = client.get("/dbplatform/schema", headers=VIEWER)
    assert r.status_code == 200, r.text
    body = r.json()
    tables = {t["name"]: t for t in body["tables"]}
    assert "fhir_resources" in tables
    assert "studio_configs" in tables
    assert tables["person"]["source"] == "warehouse"
    assert tables["studio_configs"]["source"] == "app"
    cols = {c["name"]: c for c in tables["studio_configs"]["columns"]}
    assert cols["id"]["pk"] is True
    assert cols["payload"]["nullable"] is False
    assert "JSON" in cols["payload"]["type"]
    fhir_cols = {c["name"]: c for c in tables["fhir_resources"]["columns"]}
    assert fhir_cols["resource_id"]["nullable"] is False
    # FK reporting: audit_log has no FK; hl7_send_attempts.outbound_id does.
    attempts = {c["name"]: c
                for c in tables["hl7_send_attempts"]["columns"]}
    assert attempts["outbound_id"]["fk"] == "hl7_outbound_messages.id"


def _extension(key="site-ext", **overrides):
    payload = {"key": key, "extensions": [
        {"table": "ext_site_notes", "column": "legacy_mrn",
         "type": "string", "purpose": "Legacy MRN kept by this site"}]}
    payload.update(overrides)
    return payload


def test_schema_extension_draft_publish(client):
    r = client.post("/dbplatform/schema/extensions", json=_extension(),
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "dbplatform-schema"
    r = client.post("/dbplatform/schema/extensions/site-ext/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    items = client.get("/dbplatform/schema/extensions",
                       headers=VIEWER).json()["items"]
    assert any(i["key"] == "site-ext" and i["status"] == "published"
               for i in items)


@pytest.mark.parametrize("extensions", [
    [{"table": "fhir_resources", "column": "x", "type": "string",
      "purpose": "touch the clinical table"}],
    [{"table": "person", "column": "x", "type": "string",
      "purpose": "touch the warehouse table"}],
    [{"table": "studio_configs", "column": "x", "type": "string",
      "purpose": "touch config"}],
    [{"table": "ext_site", "column": "x", "type": "string",
      "purpose": "sneaky", "action": "drop"}],
    [{"table": "site_notes", "column": "x", "type": "string",
      "purpose": "not deployment-owned (no ext_ prefix)"}],
    [{"table": "ext_site", "column": "x", "type": "xml",
      "purpose": "unknown type"}],
])
def test_schema_extension_refusals_422(client, extensions):
    r = client.post("/dbplatform/schema/extensions",
                    json=_extension(extensions=extensions), headers=ADMIN)
    assert r.status_code == 422, r.text


def test_schema_extension_canonical_refusal_message_is_clear(client):
    r = client.post("/dbplatform/schema/extensions",
                    json=_extension(extensions=[
                        {"table": "fhir_resources", "column": "hack",
                         "type": "string", "purpose": "nope"}]),
                    headers=ADMIN)
    assert r.status_code == 422
    assert "canonical" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# (d) Transformer workbench
# ---------------------------------------------------------------------------

MAPPING = {
    "entityType": "patients",
    "columns": [
        {"source": "MRN", "target": "id", "transform": "trim"},
        {"source": "Sex", "target": "gender", "transform": "code-map",
         "codeMap": {"M": "male", "F": "female"}},
        {"source": "DOB", "target": "birth_date",
         "transform": "date-mdY"},
        {"source": "Last Name", "target": "family_name",
         "transform": "trim"},
    ],
}


def test_transform_preview_applies_governed_transforms(client):
    r = client.post("/dbplatform/transform/preview", json={
        "mapping": MAPPING,
        "rows": [{"MRN": "  pat-9 ", "Sex": "F", "DOB": "04/02/1980",
                  "Last Name": "  Synthetic "}]}, headers=VIEWER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True and body["errors"] == []
    assert body["transformed"] == [
        {"id": "pat-9", "gender": "female", "birth_date": "1980-04-02",
         "family_name": "Synthetic"}]


def test_transform_preview_reports_row_errors(client):
    r = client.post("/dbplatform/transform/preview", json={
        "mapping": MAPPING,
        "rows": [{"MRN": "pat-1", "Sex": "Q", "DOB": "1980-04-02",
                  "Last Name": "X"}]}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is False
    fields = {e["field"] for e in body["errors"]}
    assert "gender" in fields and "birth_date" in fields


def test_transform_preview_bad_transform_name_422(client):
    bad = {"entityType": "patients", "columns": [
        {"source": "A", "target": "id", "transform": "eval-code"}]}
    r = client.post("/dbplatform/transform/preview",
                    json={"mapping": bad, "rows": [{"A": "1"}]},
                    headers=ADMIN)
    assert r.status_code == 422, r.text


def test_transform_preview_writes_nothing(app, client):
    with app.state.session_factory() as session:
        before_configs = session.scalar(
            select(StudioConfig.id).order_by(
                StudioConfig.id.desc()).limit(1)) or 0
        before_audit = session.scalar(
            select(AuditLog.id).order_by(AuditLog.id.desc()).limit(1)) or 0
        counts_before = (
            len(session.scalars(select(StudioConfig)).all()),
            len(session.scalars(select(AuditLog)).all()))
        assert counts_before[0] >= 0 and before_configs >= 0
        assert before_audit >= 0
    r = client.post("/dbplatform/transform/preview", json={
        "mapping": MAPPING,
        "rows": [{"MRN": " pat-1 ", "Sex": "M", "DOB": "01/01/1990",
                  "Last Name": "A"}]}, headers=ADMIN)
    assert r.status_code == 200
    with app.state.session_factory() as session:
        counts_after = (
            len(session.scalars(select(StudioConfig)).all()),
            len(session.scalars(select(AuditLog)).all()))
    assert counts_after == counts_before, (
        "preview must commit NOTHING (choice: no config rows, no audit "
        "rows — preview is a pure function)")


def test_transform_profile_governance(client):
    payload = dict(MAPPING)
    payload["key"] = "export-patients"
    r = client.post("/dbplatform/transform/profiles", json=payload,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "dbplatform-transform"
    r = client.post("/dbplatform/transform/profiles/export-patients/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    listing = client.get("/dbplatform/transform/profiles",
                         headers=VIEWER).json()
    assert any(i["key"] == "export-patients" for i in listing["items"])
    assert "trim" in listing["transforms"]
    bad = {"key": "bad", "entityType": "patients", "columns": [
        {"source": "A", "target": "id", "transform": "nope"}]}
    r = client.post("/dbplatform/transform/profiles", json=bad,
                    headers=ADMIN)
    assert r.status_code == 422
