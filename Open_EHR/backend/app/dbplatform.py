"""Database platform — adapters, canonical DDL, schema designer and the
transformer workbench (PLATFORM PASS, Workstream 4).

What lives here — and what deliberately does NOT:

- **Adapter framework.** An in-code ENGINES registry describing every
  database engine OpenCare can target: display name, SQLAlchemy
  dialect/driver, a credential-free URL example, and a capability matrix
  (document/JSON store, vector search posture, transactional DDL,
  identifier case rules) including a per-engine `verification` field that
  states HONESTLY how far that engine has been exercised (live
  conformance subset / baseline suite / dialect-only). The matrix is
  served at GET /dbplatform/engines and is ALSO governable as Studio
  configuration: connection *profiles* (kind "dbplatform") follow the
  same draft -> publish governance as every other Studio kind
  (supersede-on-publish, hash-chained audit on every transition) —
  implemented in-module over StudioConfig rows because this module ships
  without touching configstore's CONFIG_KINDS registry (registration
  there is a listed pending integration edit).

- **PROFILE PAYLOAD RULE (hard).** A profile is
  {key, engine, host, port, database, adapter, vault_key_name,
  capability_expectations?}. It names WHERE a database is and WHICH
  vault key holds its credential — never the credential itself. Any
  payload carrying a credential-shaped field (password / secret / token
  / private key / connection string with an embedded password / a URL
  with userinfo) is refused 422, at any nesting depth.

- **Canonical DDL export.** GET /dbplatform/ddl/{engine} renders the
  full application schema (app.db.Base metadata — every model) plus a
  warehouse variant (app.warehouse.WarehouseBase metadata) compiled for
  the target dialect. This is the portability contract: one canonical
  model set, per-engine DDL generated from it, never hand-maintained
  per-engine schema files.

- **Schema designer.** GET /dbplatform/schema returns the canonical
  schema as a read model (tables -> columns with type / nullable / PK /
  FK target) derived from the same metadata as the DDL export.
  "Schema editing" in this system means exactly ONE thing: a deployment
  may declare ADDITIVE extension columns on DEPLOYMENT-OWNED extension
  tables (names prefixed `ext_`), governed as kind "dbplatform-schema"
  draft -> publish. It REFUSES (422): any extension naming a canonical
  table (every clinical/FHIR table — fhir_resources and friends — and
  every warehouse table), any drop/alter/delete/rename operation, and
  any extension table that is not deployment-owned (`ext_` prefix).
  Canonical clinical schema changes are code migrations, never
  configuration — that is the configure-don't-fork rule applied to the
  database layer.

- **Transformer workbench.** POST /dbplatform/transform/preview applies
  a governed mapping payload (the Phase 13 `mapping` kind shape,
  validated by configstore.validate_mapping) to caller-supplied rows
  using the SAME named transform vocabulary
  (configstore.MAPPING_TRANSFORMS). The transform application itself is
  imported from app.reports (_apply_transform) — it is a pure function
  with no side effects, so the preview reuses the import centre's exact
  semantics rather than re-implementing them. Preview commits NOTHING:
  no config rows, no audit rows, no data writes — it is a pure function
  of (mapping, rows). Export profiles are governed drafts of kind
  "dbplatform-transform" (same in-module governance as profiles).

Auth gates reuse EXISTING capabilities only: reads require
`studio.read`, writes require `studio.manage` (defaults in
configstore.CAPABILITY_DEFAULTS; no new capability is introduced).

Phase 17 (BD-64) — v1 remainders built in this module:

- **Test-connection** (POST /dbplatform/profiles/{key}/test-connection):
  a REAL handshake against the PUBLISHED profile's target through
  `make_profile_engine` (connect + SELECT 1 + a per-engine version
  probe), recorded as a governed `DbConnectionTest` row (timestamp,
  actor, engine, server version, latency, success/failure + error
  class) plus a hash-chained audit entry. Credential hygiene is the
  v1 profile rule extended to runtime: the profile carries only the
  vault key NAME; the secret is resolved from the process environment
  at handshake time (see `resolve_profile_url`) and is never stored
  on the record, never returned, and redacted out of error text.

- **Governed hot-swap** (POST /dbplatform/profiles/{key}/swap +
  POST /dbplatform/runtime/rollback): switches the runtime clinical
  engine between the startup engine and a VERIFIED profile (the
  target's latest test-connection must be a success recorded against
  the CURRENT published version, at or after that version's last
  change). Pre-swap handshake -> rebind -> post-swap health proof
  (engine SELECT 1 + a FHIR Patient search executed through
  app.fhir on the new engine); any post-swap failure rolls back
  automatically to the previous engine. WHAT TRULY SWAPS — and what
  does not — is stated at `swap_profile` below; nothing here flips a
  flag while queries keep hitting the old engine.
"""
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (Boolean, DateTime, Float, Integer, JSON, String,
                        Text, select, text)
from sqlalchemy.orm import Mapped, Session, mapped_column

from .audit import append_audit
from .configstore import (ConfigPayloadError, MAPPING_TRANSFORMS,
                          published_config as _published_config,
                          validate_mapping)
from .db import Base, make_engine, make_session_factory
from .models import ConfigStatus, StudioConfig, User, utcnow
from .security import get_db, require_capability, seed_users
from .warehouse import WarehouseBase

# Importing app.models registers every application table on Base.metadata;
# app.warehouse (imported above) registers the OMOP warehouse tables.
from . import models as _models  # noqa: F401

router = APIRouter(prefix="/dbplatform", tags=["dbplatform"])

# In-module Studio kinds (StudioConfig rows; NOT registered in
# configstore.CONFIG_KINDS — see PENDING INTEGRATION in the handoff).
KIND_PROFILE = "dbplatform"
KIND_SCHEMA = "dbplatform-schema"
KIND_TRANSFORM = "dbplatform-transform"


# ---------------------------------------------------------------------------
# (a) Adapter framework — the ENGINES registry (data, in code)
# ---------------------------------------------------------------------------

ENGINES: list[dict] = [
    {
        "engine": "postgres",
        "display_name": "PostgreSQL 16",
        "sqlalchemy_dialect": "postgresql+psycopg",
        "driver_package": "psycopg[binary]",
        "url_example": "postgresql+psycopg://db.example.internal:5432/opencare",
        "capabilities": {
            "json_document_store": True,
            "json_notes": "Native JSON/JSONB column types; the app stores "
                          "FHIR bodies and config payloads as JSON.",
            "vector_search": "native-pgvector",
            "transactional_ddl": True,
            "identifier_case": "unquoted identifiers fold to lower case",
            "notes": "Primary production engine. Full app schema + OMOP "
                     "warehouse both run on it.",
        },
        "verification": "live-verified (conformance subset)",
    },
    {
        "engine": "sqlite",
        "display_name": "SQLite 3",
        "sqlalchemy_dialect": "sqlite",
        "driver_package": "sqlite3 (Python stdlib)",
        "url_example": "sqlite:///./opencare.db",
        "capabilities": {
            "json_document_store": True,
            "json_notes": "JSON stored as JSON text with JSON1 functions; "
                          "sufficient for the app's JSON payload columns.",
            "vector_search": "none",
            "transactional_ddl": True,
            "identifier_case": "case-insensitive",
            "notes": "Baseline engine: the whole test suite and local "
                     "development run on it. Single-writer; a deployment "
                     "target for solo/edge installs, not multi-node.",
        },
        "verification": "live-verified (baseline suite)",
    },
    {
        "engine": "mysql",
        "display_name": "MySQL 8",
        "sqlalchemy_dialect": "mysql+pymysql",
        "driver_package": "pymysql",
        "url_example": "mysql+pymysql://db.example.internal:3306/opencare",
        "capabilities": {
            "json_document_store": True,
            "json_notes": "Native JSON column type.",
            "vector_search": "external",
            "transactional_ddl": False,
            "identifier_case": "depends on server OS / lower_case_table_names",
            "notes": "DDL compiles and dialect imports verified. No live "
                     "MySQL server in the verification environment — the "
                     "MySQL-wire live run was done against MariaDB.",
        },
        "verification": "dialect-only — live NOT RUN",
    },
    {
        "engine": "mariadb",
        "display_name": "MariaDB 11",
        "sqlalchemy_dialect": "mariadb+pymysql",
        "driver_package": "pymysql",
        "url_example": "mariadb+pymysql://db.example.internal:3306/opencare",
        "capabilities": {
            "json_document_store": True,
            "json_notes": "JSON is an alias for LONGTEXT with JSON checks "
                          "in MariaDB; payload columns behave as JSON.",
            "vector_search": "external",
            "transactional_ddl": False,
            "identifier_case": "depends on server OS / lower_case_table_names",
            "notes": "Live-verified with the identical conformance subset "
                     "as PostgreSQL (same test module, parameterised).",
        },
        "verification": "live-verified (conformance subset)",
    },
    {
        "engine": "mssql",
        "display_name": "Microsoft SQL Server",
        "sqlalchemy_dialect": "mssql+pyodbc",
        "driver_package": "pyodbc",
        "url_example": "mssql+pyodbc://db.example.internal:1433/opencare",
        "capabilities": {
            "json_document_store": True,
            "json_notes": "No native JSON type before SQL Server 2025: "
                          "JSON payloads render as NVARCHAR(max) with JSON "
                          "functions available at query time.",
            "vector_search": "external",
            "transactional_ddl": True,
            "identifier_case": "collation-dependent (default CI)",
            "notes": "Dialect-level only: DDL compiles for the full "
                     "canonical schema; no live server was available.",
        },
        "verification": "dialect-only — live NOT RUN",
    },
    {
        "engine": "oracle",
        "display_name": "Oracle Database",
        "sqlalchemy_dialect": "oracle+oracledb",
        "driver_package": "oracledb",
        "url_example": "oracle+oracledb://db.example.internal:1521/opencare",
        "capabilities": {
            "json_document_store": True,
            "json_notes": "Native JSON type (21c+); identifier limits and "
                          "VARCHAR2 semantics differ from the other engines.",
            "vector_search": "external",
            "transactional_ddl": False,
            "identifier_case": "unquoted identifiers fold to UPPER case",
            "notes": "Dialect-level only: DDL compiles for the full "
                     "canonical schema; no live server was available.",
        },
        "verification": "dialect-only — live NOT RUN",
    },
    {
        "engine": "mongodb-ferretdb",
        "display_name": "MongoDB wire via FerretDB",
        "sqlalchemy_dialect": None,
        "driver_package": "pymongo",
        "url_example": "mongodb://db.example.internal:27017/opencare_docs",
        "capabilities": {
            "json_document_store": True,
            "json_notes": "Native document store. FerretDB translates the "
                          "MongoDB wire protocol onto PostgreSQL or SQLite "
                          "storage (the SSPL-free Mongo-mode route).",
            "vector_search": "none",
            "transactional_ddl": False,
            "identifier_case": "case-sensitive",
            "notes": "Deployment TARGET for document-mode data only. The "
                     "application has NO MongoDB adapter seam today: the "
                     "clinical store is SQLAlchemy-only, so this engine "
                     "cannot run the app — it is listed so a deployment "
                     "can plan document storage beside the SQL store, "
                     "never instead of it.",
        },
        "verification": ("live-verified (document-store smoke only — "
                         "FerretDB 1.24.0, sqlite handler, pymongo "
                         "insert/find; NOT an app engine)"),
    },
]

ENGINES_BY_KEY: dict[str, dict] = {e["engine"]: e for e in ENGINES}

# Engines with a SQLAlchemy DDL story, mapped to the URL used purely for
# DDL compilation (mock engine — nothing ever connects). MariaDB compiles
# through the MySQL dialect family, exactly as SQLAlchemy models it.
_DDL_URLS = {
    "postgres": "postgresql+psycopg://",
    "sqlite": "sqlite://",
    "mysql": "mysql+pymysql://",
    "mariadb": "mysql+pymysql://",
    "mssql": "mssql+pyodbc://",
    "oracle": "oracle+oracledb://",
}


def engines_matrix() -> list[dict]:
    """The adapter capability matrix (service function)."""
    return [dict(e, capabilities=dict(e["capabilities"])) for e in ENGINES]


# ---------------------------------------------------------------------------
# In-module Studio governance (draft -> publish over StudioConfig rows,
# mirroring app.studio_config: drafts are designer data, publish
# supersedes the prior published version, every transition is audited).
# ---------------------------------------------------------------------------

def _config_out(row: StudioConfig) -> dict:
    return {
        "id": row.id, "kind": row.kind, "key": row.key,
        "version": row.version, "status": row.status.value,
        "payload": row.payload,
        "created_by": row.created_by, "updated_by": row.updated_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _versions(session: Session, kind: str, key: str) -> list[StudioConfig]:
    return list(session.scalars(
        select(StudioConfig).where(StudioConfig.kind == kind,
                                   StudioConfig.key == key)
        .order_by(StudioConfig.version.asc())).all())


def _list_kind(session: Session, kind: str, user: User) -> list[dict]:
    from .configstore import role_allowed
    can_manage = role_allowed(session, user.role, "studio.manage")
    rows = session.scalars(
        select(StudioConfig).where(StudioConfig.kind == kind)
        .order_by(StudioConfig.key.asc(), StudioConfig.version.asc())).all()
    return [_config_out(r) for r in rows
            if can_manage or r.status == ConfigStatus.published]


def _create_draft(session: Session, kind: str, key: str, payload: dict,
                  user: User, action_prefix: str) -> StudioConfig:
    versions = _versions(session, kind, key)
    drafts = [v for v in versions if v.status == ConfigStatus.draft]
    if drafts:
        raise HTTPException(
            status_code=409,
            detail=(f"A draft already exists for {kind}/{key} "
                    f"(v{drafts[-1].version}); publish it before drafting "
                    "another version"))
    next_version = (versions[-1].version + 1) if versions else 1
    row = StudioConfig(kind=kind, key=key, version=next_version,
                       status=ConfigStatus.draft, payload=payload,
                       created_by=user.username, updated_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action=f"{action_prefix}.create",
                 entity=f"config:{kind}/{key}@v{next_version}",
                 payload={"kind": kind, "key": key, "version": next_version})
    session.commit()
    return row


def _publish_draft(session: Session, kind: str, key: str, user: User,
                   action_prefix: str) -> StudioConfig:
    versions = _versions(session, kind, key)
    if not versions:
        raise HTTPException(status_code=404,
                            detail=f"No config found for {kind}/{key}")
    drafts = [v for v in versions if v.status == ConfigStatus.draft]
    if not drafts:
        raise HTTPException(
            status_code=409,
            detail=f"No draft to publish for {kind}/{key} "
                   "(the latest version is not a draft)")
    row = drafts[-1]
    current = _published_config(session, kind, key)
    superseded_version = None
    if current is not None and current.id != row.id:
        current.status = ConfigStatus.superseded
        superseded_version = current.version
        session.flush()
    row.status = ConfigStatus.published
    row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username,
                 action=f"{action_prefix}.publish",
                 entity=f"config:{kind}/{key}@v{row.version}",
                 payload={"kind": kind, "key": key, "version": row.version,
                          "superseded_version": superseded_version})
    session.commit()
    return row


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422,
                            detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422,
                            detail="Request body must be a JSON object")
    return body


def _fail422(detail: str):
    raise HTTPException(status_code=422, detail=detail)


# ---------------------------------------------------------------------------
# Profile validation — the credential rule is enforced FIRST, before any
# structural check, and scans recursively (any depth, keys + string values).
# ---------------------------------------------------------------------------

_CREDENTIAL_KEYS = {
    "password", "passwd", "pwd", "secret", "client_secret", "api_key",
    "apikey", "access_token", "refresh_token", "token", "private_key",
    "connection_string", "connectionstring", "dsn", "credentials",
    "credential", "auth", "authorization",
}
_USERINFO_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
_EMBEDDED_PW_RE = re.compile(r"(?i)(password|passwd|pwd)\s*=")


def _scan_for_credentials(node, path: str = "") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            normalised = str(key).strip().lower().replace("-", "_")
            if normalised in _CREDENTIAL_KEYS:
                _fail422(
                    f"Credential-shaped field '{path}{key}' is refused: "
                    "connection profiles reference credentials by vault "
                    "key NAME only (vault_key_name) and never carry "
                    "secret values")
            _scan_for_credentials(value, f"{path}{key}.")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _scan_for_credentials(item, f"{path}[{i}].")
    elif isinstance(node, str):
        if _USERINFO_RE.search(node):
            _fail422(
                f"Value at '{path.rstrip('.')}' embeds a credential in a "
                "URL (userinfo): profiles must reference credentials by "
                "vault key NAME only")
        if _EMBEDDED_PW_RE.search(node):
            _fail422(
                f"Value at '{path.rstrip('.')}' embeds a password: "
                "profiles must reference credentials by vault key NAME "
                "only")


_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-_]*$")
_VAULT_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")
_PROFILE_FIELDS = {"key", "engine", "host", "port", "database", "adapter",
                   "vault_key_name", "capability_expectations"}


def validate_profile_payload(payload) -> dict:
    """Validate a dbplatform connection profile. 422 on any violation."""
    if not isinstance(payload, dict):
        _fail422("Profile payload must be a JSON object")
    _scan_for_credentials(payload)
    unknown = set(payload) - _PROFILE_FIELDS
    if unknown:
        _fail422(f"Unknown profile field(s) {sorted(unknown)}; allowed: "
                 f"{sorted(_PROFILE_FIELDS)}")
    key = payload.get("key")
    if not isinstance(key, str) or not _KEY_RE.match(key):
        _fail422("'key' is required and must be a slug "
                 "(^[a-z0-9][a-z0-9-_]*$)")
    engine = payload.get("engine")
    if engine not in ENGINES_BY_KEY:
        _fail422(f"'engine' must be one of {sorted(ENGINES_BY_KEY)}")
    host = payload.get("host")
    if not isinstance(host, str) or not host.strip() \
            or "://" in host or "@" in host or "/" in host:
        _fail422("'host' is required and must be a bare host name "
                 "(no scheme, credentials or path)")
    port = payload.get("port")
    if isinstance(port, bool) or not isinstance(port, int) \
            or not (1 <= port <= 65535):
        _fail422("'port' is required and must be an integer in 1..65535")
    database = payload.get("database")
    if not isinstance(database, str) or not database.strip():
        _fail422("'database' is required and must be a non-empty string")
    adapter = payload.get("adapter")
    if not isinstance(adapter, str) or not adapter.strip():
        _fail422("'adapter' is required and must be a non-empty string "
                 "(e.g. 'sqlalchemy')")
    vault_key_name = payload.get("vault_key_name")
    if not isinstance(vault_key_name, str) \
            or not _VAULT_KEY_RE.match(vault_key_name):
        _fail422("'vault_key_name' is required and must be a vault key "
                 "NAME (letters, digits, '_', '-', '.', '/') — the name "
                 "of the secret, never the secret itself")
    expectations = payload.get("capability_expectations")
    if expectations is not None:
        if not isinstance(expectations, dict) or any(
                isinstance(v, (dict, list)) for v in expectations.values()):
            _fail422("'capability_expectations' must be an object of "
                     "scalar expectations")
    return payload


# ---------------------------------------------------------------------------
# (a2) Phase 17 BD-64 — test-connection records, the profile engine
# factory, and the runtime engine registry (governed hot-swap).
#
# Governed records are this module's own tables on the shared Base —
# the app.protocols precedent (operational records beside governed
# config, NOT new Studio config kinds).
# ---------------------------------------------------------------------------

class DbConnectionTest(Base):
    """One recorded test-connection outcome for one connection profile.

    Governed data, not a log line: the row is written in the runtime
    database that governs the profile, carries the actor, the profile
    version tested, the measured latency and the server version the
    target reported — and NEVER any credential material (the profile
    holds a vault key NAME; the resolved secret exists only in memory
    for the duration of the handshake and is redacted from error
    text before anything is stored)."""
    __tablename__ = "dbplatform_connection_tests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                             index=True)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    engine: Mapped[str] = mapped_column(String(60), nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False,
                                       default="")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    server_version: Mapped[str | None] = mapped_column(String(300),
                                                       nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False,
                                              default=0.0)
    error_class: Mapped[str | None] = mapped_column(String(120),
                                                    nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    via: Mapped[str] = mapped_column(String(40), nullable=False,
                                     default="manual")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class DbSwapEvent(Base):
    """One hot-swap outcome. Written to the database that is
    authoritative AFTER the event: a successful swap records itself
    in the TARGET database; a rolled-back swap records itself in the
    database the runtime returned to. The hash-chained audit carries
    the matching entry on the same side (audit chains are per-database
    by construction — each side's chain stays internally valid)."""
    __tablename__ = "dbplatform_swap_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False,
                                       default="")
    from_profile: Mapped[str | None] = mapped_column(String(120),
                                                     nullable=True)
    to_profile: Mapped[str | None] = mapped_column(String(120),
                                                   nullable=True)
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None \
        else value.replace(tzinfo=timezone.utc)


def _vault_env_name(vault_key_name: str) -> str:
    """The environment variable a vault key NAME resolves through.

    This deployment's vault seam IS the process environment — the
    same seam that carries DATABASE_URL / WAREHOUSE_DATABASE_URL and
    the OPENCARE_TEST_*_URL live-test URLs. A vault key name such as
    "opencare/db/primary" normalises to OPENCARE_DB_PRIMARY."""
    return re.sub(r"[^A-Za-z0-9]", "_", vault_key_name).upper()


def _password_from_url(url: str) -> str | None:
    try:
        return urlparse(url).password
    except Exception:
        return None


def resolve_profile_url(profile: dict) -> tuple[str, str | None]:
    """Build the SQLAlchemy URL for a profile + the resolved password
    (returned ONLY so error text can be redacted; never stored).

    Resolution order for non-SQLite engines:
    1. The env var named by the profile's vault key holds a full
       SQLAlchemy URL (contains "://") -> used verbatim. This is the
       operator/live-test pattern (OPENCARE_TEST_PG_URL-style): the
       credential never appears in governed config, only in the
       process environment of the running app.
    2. Otherwise the env value is the PASSWORD; the username comes
       from "<VAULT_ENV>_USER" or defaults to "opencare", and the URL
       is built from the profile's dialect/host/port/database.
    3. No env value at all -> a passwordless URL (local trust auth);
       the handshake result will show whether the target accepts it.

    SQLite needs no credential: `database` is the file path.
    mongodb-ferretdb has no SQLAlchemy seam (see ENGINES) -> ValueError.
    """
    engine_key = profile.get("engine")
    if engine_key == "sqlite":
        return f"sqlite:///{profile['database']}", None
    if engine_key not in _DDL_URLS:
        raise ValueError(
            f"Engine '{engine_key}' has no SQLAlchemy adapter seam: it "
            "is a document-store deployment target, not an app engine")
    env_name = _vault_env_name(profile["vault_key_name"])
    raw = os.environ.get(env_name)
    if raw and "://" in raw:
        return raw, _password_from_url(raw)
    password = raw or None
    username = os.environ.get(env_name + "_USER") or "opencare"
    dialect = ENGINES_BY_KEY[engine_key]["sqlalchemy_dialect"]
    auth = quote(username, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    url = (f"{dialect}://{auth}@{profile['host']}:{profile['port']}"
           f"/{profile['database']}")
    return url, password


def make_profile_engine(profile: dict):
    """The platform's engine factory for a connection profile: a real
    SQLAlchemy engine for the profile's target, built through
    app.db.make_engine (the same factory the app boots with)."""
    url, _password = resolve_profile_url(profile)
    return make_engine(url)


def _redact(text_value: str | None, secrets: list) -> str | None:
    """Strip credential material from text bound for a record or a
    response: the resolved password (verbatim) plus any
    userinfo-shaped `://user:pass@` fragment."""
    if text_value is None:
        return None
    out = text_value
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    out = _USERINFO_RE.sub("://***:***@", out)
    return out[:300]


_VERSION_PROBES = {
    "postgres": "SELECT version()",
    "mysql": "SELECT version()",
    "mariadb": "SELECT version()",
    "sqlite": "SELECT sqlite_version()",
    "mssql": "SELECT @@VERSION",
    "oracle": "SELECT banner FROM v$version WHERE rownum = 1",
}


def perform_handshake(profile: dict) -> dict:
    """A REAL handshake against the profile's target: connect through
    the platform engine factory, run SELECT 1 plus the engine's
    version probe, measure latency. NEVER raises — the outcome dict
    is the record (`success`, `server_version`, `latency_ms`,
    `error_class`, redacted `error_detail`)."""
    started = time.perf_counter()
    password = None
    engine = None
    try:
        url, password = resolve_profile_url(profile)
        engine = make_engine(url)
        server_version = None
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            probe = _VERSION_PROBES.get(profile.get("engine") or "")
            if probe:
                try:
                    row = conn.execute(text(probe)).first()
                    if row is not None:
                        server_version = str(row[0])[:300]
                except Exception:
                    server_version = None  # SELECT 1 already proved it
            if server_version is None:
                info = conn.dialect.server_version_info \
                    if conn.dialect is not None else None
                if info:
                    server_version = ".".join(str(p) for p in info)
        latency = round((time.perf_counter() - started) * 1000.0, 3)
        return {"success": True, "server_version": server_version,
                "latency_ms": latency, "error_class": None,
                "error_detail": None}
    except Exception as exc:  # the failure IS the recorded outcome
        latency = round((time.perf_counter() - started) * 1000.0, 3)
        return {"success": False, "server_version": None,
                "latency_ms": latency,
                "error_class": type(exc).__name__,
                "error_detail": _redact(str(exc), [password])}
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


def _test_out(row: DbConnectionTest) -> dict:
    return {
        "id": row.id, "profile_key": row.profile_key,
        "profile_version": row.profile_version, "engine": row.engine,
        "actor": row.actor, "success": row.success,
        "server_version": row.server_version,
        "latency_ms": row.latency_ms,
        "error_class": row.error_class, "error_detail": row.error_detail,
        "via": row.via,
        "tested_at": row.created_at.isoformat() if row.created_at else None,
    }


def run_test_connection(session: Session, published_row: StudioConfig,
                        user: User, via: str = "manual") -> DbConnectionTest:
    """Perform the handshake for a PUBLISHED profile, persist the
    governed record + the hash-chained audit entry, and return the row.
    A failed handshake is a recorded outcome, not an exception."""
    profile = published_row.payload or {}
    outcome = perform_handshake(profile)
    row = DbConnectionTest(
        profile_key=published_row.key,
        profile_version=published_row.version,
        engine=profile.get("engine") or "", actor=user.username,
        success=bool(outcome["success"]),
        server_version=outcome["server_version"],
        latency_ms=outcome["latency_ms"],
        error_class=outcome["error_class"],
        error_detail=outcome["error_detail"], via=via)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="dbplatform.profile.test_connection",
                 entity=f"dbplatform-profile:{published_row.key}",
                 payload={"profile_key": published_row.key,
                          "profile_version": published_row.version,
                          "engine": row.engine, "success": row.success,
                          "latency_ms": row.latency_ms,
                          "error_class": row.error_class, "via": via})
    session.commit()
    return row


def profile_verification(session: Session,
                         published_row: StudioConfig) -> dict:
    """Is this published profile VERIFIED for hot-swap? The rule: the
    LATEST recorded test for the key is a success, recorded against
    the CURRENT published version, at or after that version's last
    change (the published row's updated_at). Any config change after
    the test (a newer published version) unverifies the profile."""
    tests = list(session.scalars(
        select(DbConnectionTest).where(
            DbConnectionTest.profile_key == published_row.key)
        .order_by(DbConnectionTest.id.desc())).all())
    latest = tests[0] if tests else None
    base = {"profile_key": published_row.key,
            "published_version": published_row.version,
            "latest_test": _test_out(latest) if latest else None}
    if latest is None:
        return {**base, "verified": False,
                "reason": "no test-connection has been recorded for "
                          "this profile"}
    if not latest.success:
        return {**base, "verified": False,
                "reason": "the latest test-connection for this profile "
                          "failed"}
    if latest.profile_version != published_row.version:
        return {**base, "verified": False,
                "reason": (f"the profile changed after its last "
                           f"successful test (tested v"
                           f"{latest.profile_version}, published v"
                           f"{published_row.version}); run "
                           "test-connection again")}
    tested_at = _as_utc(latest.created_at)
    changed_at = _as_utc(published_row.updated_at)
    if tested_at is not None and changed_at is not None \
            and tested_at < changed_at:
        return {**base, "verified": False,
                "reason": "the last successful test predates the "
                          "profile's latest change; run "
                          "test-connection again"}
    return {**base, "verified": True, "reason": "verified"}


# ---------------------------------------------------------------------------
# Runtime engine registry (hot-swap)
#
# WHAT TRULY SWAPS: app.state.engine + app.state.session_factory.
# app.security.get_db resolves the factory from app.state PER REQUEST,
# so every route served through Depends(get_db) — FHIR, Studio, lab,
# pharmacy, all of it — reads/writes the NEW engine from the next
# request onward. The swap proof in the test suite asserts a write
# lands in the target database file and NOT in the source.
#
# WHAT DOES NOT SWAP (honest scope):
# - Requests already in flight keep the session they started with.
# - The two middleware guards in app.main (downtime / pack-gating)
#   close over the STARTUP session factory; they keep reading the
#   startup database's published config until restart.
# - The OMOP warehouse engine (app.warehouse, WAREHOUSE_DATABASE_URL)
#   is a separate database by design and never swaps here.
# - Background threads handed a factory at spawn time (e.g. the
#   interop bulk-export thread) keep that factory.
# - A process RESTART always boots from DATABASE_URL again: the swap
#   is a runtime act, not a config rewrite. Persisting a swap across
#   restarts means pointing DATABASE_URL at the target (the v1 env
#   seam) — the swap record + audit say exactly what ran when.
# - NO DATA IS COPIED. The target must already hold the deployment's
#   data (a restored backup / replicated copy) in a real deployment;
#   the swap only guarantees the target has the canonical SCHEMA
#   (create_all is idempotent) and the seeded dev/OIDC user rows the
#   auth boundary upserts anyway.
#
# Rollback depth is ONE step: the registry retains exactly the
# previous (engine, factory, profile). A manual rollback is itself
# recorded and reversible the same way (the engine rolled away from
# becomes the new "previous").
# ---------------------------------------------------------------------------

def _runtime(app) -> dict:
    runtime = getattr(app.state, "dbplatform_runtime", None)
    if runtime is None:
        runtime = {"active_profile": None, "previous": None}
        app.state.dbplatform_runtime = runtime
    return runtime


def _post_swap_health_probe(app) -> dict:
    """Post-swap health proof, executed against the CURRENT
    app.state (i.e. the candidate engine after a rebind): an engine
    SELECT 1, plus a genuine FHIR read — a Patient search run through
    app.fhir.search_resources on the new engine. (/health itself is a
    static payload; the facts it stands for — process up, clinical
    store readable — are what this probe verifies.) Tests may replace
    this function to inject a post-swap failure and prove rollback."""
    engine = app.state.engine
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    with app.state.session_factory() as session:
        admin = session.scalars(select(User).where(
            User.username == "dev-admin")).first()
        if admin is None:
            raise RuntimeError(
                "post-swap probe: no seeded user on the target engine")
        from .fhir import search_resources
        bundle = search_resources("Patient", session, admin)
    return {"health": "ok", "dialect": engine.dialect.name,
            "fhir_patient_total": bundle["total"]}


def _swap_event_out(row: DbSwapEvent) -> dict:
    return {
        "id": row.id, "actor": row.actor,
        "from_profile": row.from_profile, "to_profile": row.to_profile,
        "outcome": row.outcome, "detail": row.detail,
        "created_at": row.created_at.isoformat() if row.created_at
        else None,
    }


# ---------------------------------------------------------------------------
# (b) Canonical DDL export
# ---------------------------------------------------------------------------

def generate_ddl(engine: str, variant: str = "both") -> str:
    """Render DDL for the canonical schema on `engine`'s dialect.

    variant: "app" (Base metadata), "warehouse" (WarehouseBase metadata)
    or "both". Raises ValueError for an unknown engine, an engine with
    no SQL DDL story (mongodb-ferretdb), or a bad variant — the route
    maps ValueError to 422."""
    if engine not in ENGINES_BY_KEY:
        raise ValueError(f"Unknown engine '{engine}'; known: "
                         f"{sorted(ENGINES_BY_KEY)}")
    if engine not in _DDL_URLS:
        raise ValueError(
            f"Engine '{engine}' has no SQL DDL: it is a document-store "
            "deployment target, not a SQLAlchemy engine")
    if variant not in ("app", "warehouse", "both"):
        raise ValueError("'variant' must be one of app|warehouse|both")
    from sqlalchemy import create_mock_engine

    url = _DDL_URLS[engine]
    parts: list[str] = []
    metadatas = []
    if variant in ("app", "both"):
        metadatas.append(("-- OpenCare canonical application schema "
                          f"({engine})", Base.metadata))
    if variant in ("warehouse", "both"):
        metadatas.append(("-- OpenCare OMOP warehouse schema "
                          f"({engine})", WarehouseBase.metadata))
    for header, metadata in metadatas:
        raw_statements: list = []
        mock = create_mock_engine(
            url, lambda sql, *a, **k: raw_statements.append(sql))
        metadata.create_all(mock, checkfirst=False)
        # The mock executor receives most statements pre-compiled to
        # strings, but some DDL elements (notably PostgreSQL CREATE
        # TYPE for Enum columns) arrive raw and MUST be compiled with
        # the target dialect — a default-dialect str() raises
        # AttributeError on CreateEnumType. Compile explicitly.
        statements = [
            s.strip() if isinstance(s, str)
            else str(s.compile(dialect=mock.dialect)).strip()
            for s in raw_statements]
        parts.append(header + "\n" + ";\n\n".join(statements) + ";")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# (c) Schema designer — canonical read model + governed extensions
# ---------------------------------------------------------------------------

def _canonical_table_names() -> set[str]:
    return set(Base.metadata.tables) | set(WarehouseBase.metadata.tables)


def canonical_schema() -> dict:
    """Tables -> columns read model from the SAME metadata the DDL
    export compiles (app Base + warehouse WarehouseBase)."""
    tables: list[dict] = []
    for source, metadata in (("app", Base.metadata),
                             ("warehouse", WarehouseBase.metadata)):
        for table in metadata.sorted_tables:
            columns = []
            for col in table.columns:
                fk = None
                if col.foreign_keys:
                    fk = sorted(col.foreign_keys)[0].target_fullname
                columns.append({
                    "name": col.name,
                    "type": str(col.type),
                    "nullable": bool(col.nullable),
                    "pk": bool(col.primary_key),
                    "fk": fk,
                })
            tables.append({"name": table.name, "source": source,
                           "columns": columns})
    return {"tables": tables, "total": len(tables)}


_EXTENSION_TYPES = ("string", "text", "integer", "float", "boolean",
                    "date", "datetime", "json")
_FORBIDDEN_OPS = {"drop", "alter", "delete", "rename", "modify", "truncate"}


def validate_schema_extension_payload(payload) -> dict:
    """Validate a dbplatform-schema extension declaration. Extensions
    are ADDITIVE columns on deployment-owned `ext_` tables ONLY."""
    if not isinstance(payload, dict):
        _fail422("Schema extension payload must be a JSON object")
    _scan_for_credentials(payload)
    for field in ("action", "operation", "op"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip().lower() in _FORBIDDEN_OPS:
            _fail422(f"Schema operation '{value}' is refused: the schema "
                     "designer declares additive deployment extensions "
                     "only — canonical schema changes are code migrations")
    key = payload.get("key")
    if not isinstance(key, str) or not _KEY_RE.match(key):
        _fail422("'key' is required and must be a slug "
                 "(^[a-z0-9][a-z0-9-_]*$)")
    extensions = payload.get("extensions")
    if not isinstance(extensions, list) or not extensions:
        _fail422("'extensions' must be a non-empty list of "
                 "{table, column, type, purpose} objects")
    canonical = _canonical_table_names()
    for i, ext in enumerate(extensions):
        if not isinstance(ext, dict):
            _fail422(f"extensions[{i}] must be an object")
        for field in ("action", "operation", "op"):
            value = ext.get(field)
            if isinstance(value, str) \
                    and value.strip().lower() in _FORBIDDEN_OPS:
                _fail422(
                    f"extensions[{i}] attempts operation '{value}': "
                    "refused — extensions are additive only; altering or "
                    "dropping canonical clinical (FHIR) schema is never "
                    "configuration")
        table = ext.get("table")
        if not isinstance(table, str) or not table.strip():
            _fail422(f"extensions[{i}].table is required")
        if table in canonical:
            _fail422(
                f"extensions[{i}] names canonical table '{table}': "
                "refused — canonical clinical (FHIR) and warehouse "
                "tables/columns cannot be altered or extended through "
                "configuration; declare a deployment-owned extension "
                "table (prefix 'ext_') instead")
        if not table.startswith("ext_"):
            _fail422(
                f"extensions[{i}].table '{table}' is not a deployment-"
                "owned extension table: extension tables must use the "
                "'ext_' prefix")
        column = ext.get("column")
        if not isinstance(column, str) \
                or not re.match(r"^[a-z][a-z0-9_]*$", column):
            _fail422(f"extensions[{i}].column must be a snake_case "
                     "column name")
        if ext.get("type") not in _EXTENSION_TYPES:
            _fail422(f"extensions[{i}].type must be one of "
                     f"{list(_EXTENSION_TYPES)}")
        purpose = ext.get("purpose")
        if not isinstance(purpose, str) or not purpose.strip():
            _fail422(f"extensions[{i}].purpose is required (why this "
                     "deployment column exists)")
    return payload


# ---------------------------------------------------------------------------
# (d) Transformer workbench — preview (pure) + governed export profiles
# ---------------------------------------------------------------------------

try:  # The import centre's transform application: a pure function.
    from .reports import _apply_transform as _reports_apply_transform
except Exception:  # pragma: no cover — fallback keeps identical semantics
    _reports_apply_transform = None


def _apply_transform_local(value: str, column: dict):
    """Fallback re-implementation of app.reports._apply_transform (same
    named transforms, same semantics) used only if the import fails."""
    from datetime import datetime
    transform = column.get("transform")
    if not transform:
        return value, None
    if transform == "trim":
        return value.strip(), None
    if transform == "upper":
        return value.upper(), None
    if transform == "lower":
        return value.lower(), None
    if transform == "title":
        return value.title(), None
    if transform in ("date-mdY", "date-dMY"):
        pattern = "%m/%d/%Y" if transform == "date-mdY" else "%d/%m/%Y"
        try:
            return datetime.strptime(
                value.strip(), pattern).date().isoformat(), None
        except ValueError:
            return value, f"not a date in {pattern.replace('%', '')} format"
    if transform == "code-map":
        code_map = column.get("codeMap") or {}
        if value in code_map:
            return code_map[value], None
        if value in code_map.values():
            return value, None
        return value, f"'{value}' is not in the profile's code map"
    return value, f"unknown transform '{transform}'"


_apply_transform = _reports_apply_transform or _apply_transform_local
TRANSFORM_SOURCE = ("app.reports._apply_transform (reused)"
                    if _reports_apply_transform is not None
                    else "in-module re-implementation of the named "
                         "transforms (app.reports import failed)")


def preview_transform(mapping: dict, rows) -> dict:
    """Apply a governed mapping payload to rows. Pure: writes NOTHING
    (no config, no audit, no data). Raises ConfigPayloadError (422) for
    an invalid mapping — including an unknown transform name."""
    validate_mapping(mapping)
    if not isinstance(rows, list) or any(not isinstance(r, dict)
                                         for r in rows):
        raise ConfigPayloadError("'rows' must be a list of objects")
    if len(rows) > 1000:
        raise ConfigPayloadError("'rows' is capped at 1000 per preview")
    transformed: list[dict] = []
    errors: list[dict] = []
    for index, raw in enumerate(rows):
        out: dict = {}
        for column in mapping.get("columns") or []:
            value = raw.get(column["source"], "")
            if value is None:
                value = ""
            elif not isinstance(value, str):
                value = str(value)
            new_value, error = _apply_transform(value, column)
            if error:
                errors.append({"row": index + 1,
                               "field": column["target"],
                               "reason": error})
            out[column["target"]] = new_value.strip() \
                if isinstance(new_value, str) else new_value
        transformed.append(out)
    return {"transformed": transformed, "errors": errors,
            "rows_total": len(rows), "valid": not errors,
            "transform_source": TRANSFORM_SOURCE}


def validate_transform_profile_payload(payload) -> dict:
    """An export profile is a mapping-shaped payload plus a key."""
    if not isinstance(payload, dict):
        _fail422("Transform profile payload must be a JSON object")
    _scan_for_credentials(payload)
    key = payload.get("key")
    if not isinstance(key, str) or not _KEY_RE.match(key):
        _fail422("'key' is required and must be a slug "
                 "(^[a-z0-9][a-z0-9-_]*$)")
    mapping = {k: v for k, v in payload.items() if k != "key"}
    try:
        validate_mapping(mapping)
    except ConfigPayloadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return payload


# ---------------------------------------------------------------------------
# Routes — engines + profiles
# ---------------------------------------------------------------------------

@router.get("/engines")
def get_engines(user: User = Depends(require_capability("studio.read"))):
    return {"engines": engines_matrix(), "total": len(ENGINES)}


@router.get("/profiles")
def list_profiles(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("studio.read"))):
    return {"items": _list_kind(session, KIND_PROFILE, user)}


@router.post("/profiles", status_code=201)
async def create_profile(request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(
                             require_capability("studio.manage"))):
    payload = validate_profile_payload(await _json_body(request))
    row = _create_draft(session, KIND_PROFILE, payload["key"], payload,
                        user, "dbplatform.profile")
    return _config_out(row)


@router.post("/profiles/{key}/publish")
def publish_profile(key: str, session: Session = Depends(get_db),
                    user: User = Depends(
                        require_capability("studio.manage"))):
    row = _publish_draft(session, KIND_PROFILE, key, user,
                         "dbplatform.profile")
    return _config_out(row)


# ---------------------------------------------------------------------------
# Routes — DDL export + schema designer
# ---------------------------------------------------------------------------

@router.get("/ddl/{engine}")
def get_ddl(engine: str,
            variant: str = Query(default="both"),
            user: User = Depends(require_capability("studio.read"))):
    try:
        ddl = generate_ddl(engine, variant)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"engine": engine, "variant": variant, "ddl": ddl}


@router.get("/schema")
def get_schema(user: User = Depends(require_capability("studio.read"))):
    return canonical_schema()


@router.get("/schema/extensions")
def list_schema_extensions(session: Session = Depends(get_db),
                           user: User = Depends(
                               require_capability("studio.read"))):
    return {"items": _list_kind(session, KIND_SCHEMA, user)}


@router.post("/schema/extensions", status_code=201)
async def create_schema_extension(request: Request,
                                  session: Session = Depends(get_db),
                                  user: User = Depends(
                                      require_capability("studio.manage"))):
    payload = validate_schema_extension_payload(await _json_body(request))
    row = _create_draft(session, KIND_SCHEMA, payload["key"], payload,
                        user, "dbplatform.schema")
    return _config_out(row)


@router.post("/schema/extensions/{key}/publish")
def publish_schema_extension(key: str, session: Session = Depends(get_db),
                             user: User = Depends(
                                 require_capability("studio.manage"))):
    row = _publish_draft(session, KIND_SCHEMA, key, user,
                         "dbplatform.schema")
    return _config_out(row)


# ---------------------------------------------------------------------------
# Routes — transformer workbench
# ---------------------------------------------------------------------------

@router.post("/transform/preview")
async def transform_preview(request: Request,
                            user: User = Depends(
                                require_capability("studio.read"))):
    body = await _json_body(request)
    mapping = body.get("mapping")
    if not isinstance(mapping, dict):
        raise HTTPException(status_code=422,
                            detail="'mapping' must be a mapping-kind "
                                   "payload object")
    try:
        return preview_transform(mapping, body.get("rows"))
    except ConfigPayloadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/transform/profiles")
def list_transform_profiles(session: Session = Depends(get_db),
                            user: User = Depends(
                                require_capability("studio.read"))):
    return {"items": _list_kind(session, KIND_TRANSFORM, user),
            "transforms": list(MAPPING_TRANSFORMS)}


@router.post("/transform/profiles", status_code=201)
async def create_transform_profile(request: Request,
                                   session: Session = Depends(get_db),
                                   user: User = Depends(require_capability(
                                       "studio.manage"))):
    payload = validate_transform_profile_payload(await _json_body(request))
    row = _create_draft(session, KIND_TRANSFORM, payload["key"], payload,
                        user, "dbplatform.transform")
    return _config_out(row)


@router.post("/transform/profiles/{key}/publish")
def publish_transform_profile(key: str, session: Session = Depends(get_db),
                              user: User = Depends(require_capability(
                                  "studio.manage"))):
    row = _publish_draft(session, KIND_TRANSFORM, key, user,
                         "dbplatform.transform")
    return _config_out(row)


# ---------------------------------------------------------------------------
# Routes — Phase 17 BD-64: test-connection + governed hot-swap.
# These land on the EXISTING Studio DB platform surface (Studio ->
# Platform group -> Database tab; this router is its API). Existing
# capabilities only: actions studio.manage, reads studio.read.
# ---------------------------------------------------------------------------

@router.post("/profiles/{key}/test-connection")
def test_connection(key: str, session: Session = Depends(get_db),
                    user: User = Depends(
                        require_capability("studio.manage"))):
    published = _published_config(session, KIND_PROFILE, key)
    if published is None:
        raise HTTPException(
            status_code=404,
            detail=f"No published connection profile for '{key}'")
    row = run_test_connection(session, published, user, via="manual")
    return _test_out(row)


@router.get("/profiles/{key}/test-connections")
def list_test_connections(key: str, session: Session = Depends(get_db),
                          user: User = Depends(
                              require_capability("studio.read"))):
    published = _published_config(session, KIND_PROFILE, key)
    if published is None:
        raise HTTPException(
            status_code=404,
            detail=f"No published connection profile for '{key}'")
    rows = list(session.scalars(
        select(DbConnectionTest).where(
            DbConnectionTest.profile_key == key)
        .order_by(DbConnectionTest.id.desc())).all())
    return {"items": [_test_out(r) for r in rows],
            "verification": profile_verification(session, published)}


@router.get("/runtime")
def get_runtime(request: Request, session: Session = Depends(get_db),
                user: User = Depends(require_capability("studio.read"))):
    runtime = _runtime(request.app)
    events = list(session.scalars(
        select(DbSwapEvent).order_by(DbSwapEvent.id.desc()).limit(20)
    ).all())
    previous = runtime.get("previous") or {}
    return {
        "active_profile": runtime.get("active_profile") or "startup",
        "dialect": request.app.state.engine.dialect.name,
        "previous_profile": previous.get("profile_key") or "startup"
        if previous else None,
        "can_rollback": bool(previous),
        "swap_events": [_swap_event_out(e) for e in events],
        "scope": "app.state engine/session-factory rebind for new "
                 "requests; warehouse, middleware guard config reads "
                 "and in-flight requests are out of scope (see module "
                 "docstring)",
    }


@router.post("/profiles/{key}/swap")
def swap_profile(key: str, request: Request,
                 session: Session = Depends(get_db),
                 user: User = Depends(
                     require_capability("studio.manage"))):
    """Governed hot-swap of the runtime clinical engine to profile
    `key`. Refuses (409) unless the target is VERIFIED (see
    profile_verification). Sequence: verification gate -> pre-swap
    handshake (recorded) -> target provisioning (canonical schema +
    seeded users; NO data copy) -> rebind app.state -> post-swap
    health proof -> on any proof failure, AUTOMATIC rollback to the
    previous engine and 502."""
    app = request.app
    runtime = _runtime(app)
    published = _published_config(session, KIND_PROFILE, key)
    if published is None:
        raise HTTPException(
            status_code=404,
            detail=f"No published connection profile for '{key}'")
    from_profile = runtime.get("active_profile")
    if from_profile == key:
        raise HTTPException(
            status_code=409,
            detail=f"Profile '{key}' is already the runtime engine")
    verification = profile_verification(session, published)
    if not verification["verified"]:
        append_audit(session, actor=user.username,
                     action="dbplatform.swap.refused",
                     entity=f"dbplatform-profile:{key}",
                     payload={"profile_key": key,
                              "reason": verification["reason"]})
        session.commit()
        raise HTTPException(
            status_code=409,
            detail=f"Hot-swap refused: {verification['reason']}")
    # Pre-swap verification: a fresh, recorded handshake.
    precheck = run_test_connection(session, published, user,
                                   via="swap-precheck")
    if not precheck.success:
        append_audit(session, actor=user.username,
                     action="dbplatform.swap.refused",
                     entity=f"dbplatform-profile:{key}",
                     payload={"profile_key": key,
                              "reason": "pre-swap handshake failed",
                              "error_class": precheck.error_class})
        session.commit()
        raise HTTPException(
            status_code=409,
            detail=(f"Hot-swap refused: pre-swap handshake against "
                    f"'{key}' failed ({precheck.error_class})"))
    profile = published.payload or {}
    # Provision the target (schema + auth seed users; no data copy).
    try:
        new_engine = make_profile_engine(profile)
        Base.metadata.create_all(new_engine)
        new_factory = make_session_factory(new_engine)
        with new_factory() as target_session:
            seed_users(target_session)
    except Exception as exc:
        append_audit(session, actor=user.username,
                     action="dbplatform.swap.refused",
                     entity=f"dbplatform-profile:{key}",
                     payload={"profile_key": key,
                              "reason": "target provisioning failed",
                              "error_class": type(exc).__name__})
        session.commit()
        raise HTTPException(
            status_code=409,
            detail=(f"Hot-swap refused: target '{key}' could not be "
                    f"provisioned ({type(exc).__name__})"))
    # Record intent on the SOURCE side before the rebind.
    append_audit(session, actor=user.username,
                 action="dbplatform.swap.start",
                 entity=f"dbplatform-profile:{key}",
                 payload={"from_profile": from_profile,
                          "to_profile": key,
                          "profile_version": published.version})
    session.commit()
    previous = {"profile_key": from_profile, "engine": app.state.engine,
                "factory": app.state.session_factory}
    # THE REBIND: new requests resolve their session from app.state.
    app.state.engine = new_engine
    app.state.session_factory = new_factory
    runtime["previous"] = previous
    runtime["active_profile"] = key
    try:
        proof = _post_swap_health_probe(app)
    except Exception as exc:
        # AUTOMATIC ROLLBACK to the previous engine.
        app.state.engine = previous["engine"]
        app.state.session_factory = previous["factory"]
        runtime["active_profile"] = previous["profile_key"]
        runtime["previous"] = None
        try:
            new_engine.dispose()
        except Exception:
            pass
        with previous["factory"]() as back_session:
            back_session.add(DbSwapEvent(
                actor=user.username, from_profile=from_profile,
                to_profile=key, outcome="rolled_back",
                detail={"error_class": type(exc).__name__,
                        "stage": "post-swap health proof"}))
            append_audit(back_session, actor=user.username,
                         action="dbplatform.swap.rolled_back",
                         entity=f"dbplatform-profile:{key}",
                         payload={"from_profile": from_profile,
                                  "to_profile": key,
                                  "error_class": type(exc).__name__})
            back_session.commit()
        raise HTTPException(
            status_code=502,
            detail=(f"Post-swap health proof against '{key}' failed "
                    f"({type(exc).__name__}); the runtime was rolled "
                    f"back to the previous engine automatically"))
    # Success: the outcome is recorded in the TARGET database (the
    # now-authoritative side) — event row + hash-chained audit.
    with new_factory() as target_session:
        event = DbSwapEvent(
            actor=user.username, from_profile=from_profile,
            to_profile=key, outcome="swapped",
            detail={"proof": proof,
                    "profile_version": published.version})
        target_session.add(event)
        target_session.flush()
        append_audit(target_session, actor=user.username,
                     action="dbplatform.swap",
                     entity=f"dbplatform-profile:{key}",
                     payload={"from_profile": from_profile,
                              "to_profile": key,
                              "profile_version": published.version,
                              "proof": proof})
        target_session.commit()
        event_out = _swap_event_out(event)
    return {"active_profile": key, "previous_profile": from_profile,
            "profile_version": published.version, "health": proof,
            "event": event_out}


@router.post("/runtime/rollback")
def rollback_runtime(request: Request,
                     session: Session = Depends(get_db),
                     user: User = Depends(
                         require_capability("studio.manage"))):
    """Manual rollback to the engine the runtime last swapped away
    from (one step; the swap depth this registry retains). The
    restored engine must pass the same health proof, or the runtime
    stays where it is and the refusal is 502."""
    app = request.app
    runtime = _runtime(app)
    previous = runtime.get("previous")
    if not previous:
        raise HTTPException(
            status_code=409,
            detail="No previous engine to roll back to (no hot-swap "
                   "has run, or the retained previous engine was "
                   "already consumed by a rollback)")
    current = {"profile_key": runtime.get("active_profile"),
               "engine": app.state.engine,
               "factory": app.state.session_factory}
    app.state.engine = previous["engine"]
    app.state.session_factory = previous["factory"]
    try:
        proof = _post_swap_health_probe(app)
    except Exception as exc:
        app.state.engine = current["engine"]
        app.state.session_factory = current["factory"]
        raise HTTPException(
            status_code=502,
            detail=(f"Rollback refused: the previous engine failed "
                    f"its health proof ({type(exc).__name__}); the "
                    "runtime remains on the current engine"))
    runtime["active_profile"] = previous["profile_key"]
    runtime["previous"] = current
    with previous["factory"]() as back_session:
        event = DbSwapEvent(
            actor=user.username, from_profile=current["profile_key"],
            to_profile=previous["profile_key"],
            outcome="manual_rollback", detail={"proof": proof})
        back_session.add(event)
        back_session.flush()
        append_audit(back_session, actor=user.username,
                     action="dbplatform.rollback",
                     entity="dbplatform-runtime",
                     payload={"from_profile": current["profile_key"],
                              "to_profile": previous["profile_key"],
                              "proof": proof})
        back_session.commit()
        event_out = _swap_event_out(event)
    return {"active_profile": previous["profile_key"],
            "previous_profile": current["profile_key"],
            "health": proof, "event": event_out}
