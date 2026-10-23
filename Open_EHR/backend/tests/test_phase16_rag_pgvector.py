"""Phase 16, WS-C2 / BD-45 — LIVE pgvector conformance (env-gated).

Gating follows tests/test_platform_db_live.py exactly: the test SKIPS
unless the URL env var is set:
  OPENCARE_TEST_PG_URL  e.g. postgresql+psycopg://user:pw@127.0.0.1:5432/opencare_test
Passwords arrive ONLY via this env var — never in files. The
coordinator stands up a phase-local PostgreSQL + pgvector and runs
this file live during the gate; on any other machine it skips
cleanly.

What it proves against the REAL engine:
  1. app.rag creates the pgvector mirror table (vector extension +
     `rag_chunk_pg` with a vector column) on the live database;
  2. ingesting a published knowledge document lands its chunks in the
     pgvector table;
  3. a raw `<->` query against the live table returns the seeded
     chunk;
  4. the API search is served from pgvector and SAYS so
     (`store == "pgvector"`).

The router is mounted locally in the fixture (the coordinator mounts
it in app.main centrally at integration; mounting again is a no-op
duplicate of the same handler).

Synthetic data only.
"""
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from app import rag as rag_module
from app.db import Base
from app.main import create_app
from app.models import ConfigStatus, StudioConfig

ADMIN = {"Authorization": "Bearer admin-token"}
CLINICIAN = {"Authorization": "Bearer clinician-token"}

PG_ENV = "OPENCARE_TEST_PG_URL"

KNOWLEDGE_BODY = (
    "Warfarin dosing requires INR monitoring. The target INR range "
    "for most indications is 2.0 to 3.0, checked at regular intervals "
    "in the anticoagulation clinic.")


@pytest.fixture()
def live_pg(monkeypatch):
    url = os.environ.get(PG_ENV)
    if not url:
        pytest.skip(f"{PG_ENV} not set — live pgvector subset not run")
    # Clean slate: every Base table + the raw-SQL pgvector mirror.
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS rag_chunk_pg CASCADE"))
    Base.metadata.drop_all(engine)
    engine.dispose()
    application = create_app(database_url=url)
    application.include_router(rag_module.router)
    Base.metadata.create_all(application.state.engine)
    with TestClient(application) as client:
        yield {"client": client, "app": application, "url": url}


def _grant_and_seed(client):
    payload = {"capabilities": {"rag.search": {
        "admin": True, "configurator": True, "clinician": True,
        "viewer": False, "patient": False}}}
    r = client.post("/studio/config/permissions/role-matrix",
                    json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/permissions/role-matrix/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    factory = client.app.state.session_factory
    with factory() as session:
        session.add(StudioConfig(
            kind="knowledge", key="warfarin-inr", version=1,
            status=ConfigStatus.published,
            payload={"title": "Warfarin INR monitoring",
                     "body": KNOWLEDGE_BODY,
                     "source": "synthetic-source"},
            created_by="test", updated_by="test"))
        session.commit()


def test_pgvector_store_live(live_pg):
    client = live_pg["client"]
    url = live_pg["url"]
    _grant_and_seed(client)

    # Ingest: the API reports the pgvector store is in use.
    r = client.post("/rag/ingest/knowledge", headers=ADMIN)
    assert r.status_code == 201, r.text
    outcome = r.json()
    assert outcome["ingested_documents"] == 1
    assert outcome["chunks"] >= 1
    assert outcome["store"] == "pgvector", outcome

    # The mirror table really exists on the live engine, with the
    # chunk rows and a real vector column populated.
    engine = create_engine(url)
    with engine.connect() as conn:
        ext = conn.execute(text(
            "SELECT extname FROM pg_extension WHERE extname = 'vector'"
        )).first()
        assert ext is not None, "vector extension was not created"
        rows = conn.execute(text(
            "SELECT chunk_id, source_id, title,"
            " vector_dims(embedding) AS dims FROM rag_chunk_pg"
        )).mappings().all()
    engine.dispose()
    assert rows, "no chunks landed in rag_chunk_pg"
    assert rows[0]["source_id"] == "knowledge/warfarin-inr"
    assert rows[0]["dims"] == rag_module.EMBED_DIM

    # A raw <-> query against the live table returns the seeded chunk.
    query_vector = rag_module.embed_local_hash("INR target warfarin")
    literal = rag_module._vector_literal(query_vector)
    with engine.connect() as conn:
        hit = conn.execute(text(
            "SELECT source_id, title FROM rag_chunk_pg"
            " ORDER BY embedding <-> CAST(:q AS vector) LIMIT 1"
        ), {"q": literal}).mappings().first()
    engine.dispose()
    assert hit is not None
    assert hit["source_id"] == "knowledge/warfarin-inr"
    assert hit["title"] == "Warfarin INR monitoring"

    # The API search is served from pgvector and names the store.
    r = client.post("/rag/search",
                    json={"query": "INR target range warfarin monitoring"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["store"] == "pgvector", body
    assert body["total"] >= 1
    top = body["results"][0]
    assert top["sourceType"] == "knowledge"
    assert top["sourceId"] == "knowledge/warfarin-inr"
    assert top["title"] == "Warfarin INR monitoring"
    assert "INR" in top["snippet"]
    assert body["citations"][0]["sourceId"] == "knowledge/warfarin-inr"

    # Status agrees: pgvector is the store in use on this database.
    r = client.get("/rag/status", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["store"] == "pgvector"
    assert r.json()["pgvector_available"] is True
