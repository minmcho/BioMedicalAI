"""Phase 16, WS-C2 — long-context RAG with citations (+ BD-45 store
naming).

Corpus = published kind `knowledge` documents + chart chunks from the
FHIR store. Embeddings are the built-in `local-hash-v1` deterministic
hashing embedder — a lexical stand-in, NOT a semantic model — so
search works with NO model configured. `POST /rag/answer` runs the
governed `rag-answerer` agent through app.agents' runtime transport
(substituted here by monkeypatching app.agents._post_run, the Phase 10
pattern); with no published agent, or a modelless runtime, it is the
explicit 503 honest-off and NOTHING is recorded as a run.

Knowledge documents are seeded by inserting PUBLISHED StudioConfig
rows directly (kind `knowledge` is registered centrally by the
coordinator at integration; published-row reads are exactly what the
module consumes). The `rag.search` capability is granted via a
published permissions override for the same reason (the coordinator
wires the capability defaults centrally).

Synthetic data only.
"""
import hashlib

import httpx
import pytest
from sqlalchemy import select

from app import agents as agents_module
from app.models import AgentRun, AuditLog, ConfigStatus, StudioConfig
from tests.conftest import (ADMIN, CLINICIAN, CONFIG, VIEWER,
                            governance_for_enable)

KNOWLEDGE_BODY = (
    "HbA1c target for most adults with type 2 diabetes is below "
    "7 percent. Review the HbA1c result every three months when "
    "therapy changes and every six months when stable.\n\n"
    "Metformin is the first line glucose lowering therapy for type 2 "
    "diabetes when kidney function permits. Counsel patients about "
    "gastrointestinal effects during titration.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _grant_rag(client):
    """Publish a permissions override granting rag.search (the
    coordinator wires these defaults centrally at integration)."""
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


def _seed_knowledge(client, key, title, body, source="synthetic-source"):
    factory = client.app.state.session_factory
    with factory() as session:
        session.add(StudioConfig(
            kind="knowledge", key=key, version=1,
            status=ConfigStatus.published,
            payload={"title": title, "body": body, "source": source},
            created_by="test", updated_by="test"))
        session.commit()


def _audit(client, action):
    factory = client.app.state.session_factory
    with factory() as session:
        return [{"actor": r.actor, "entity": r.entity, "payload": r.payload}
                for r in session.scalars(
                    select(AuditLog).where(AuditLog.action == action)).all()]


def _agent_run_count(client):
    factory = client.app.state.session_factory
    with factory() as session:
        return len(session.scalars(select(AgentRun)).all())


def _create_patient(client, pid, family="Synthetic"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": family, "given": ["Rag"]}],
        "gender": "female", "birthDate": "1990-01-15"}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _create_condition(client, cid, pid, display):
    r = client.post("/fhir/Condition", json={
        "resourceType": "Condition", "id": cid,
        "subject": {"reference": f"Patient/{pid}"},
        "code": {"coding": [{"system": "http://snomed.info/sct",
                             "code": "44054006", "display": display}],
                 "text": display}}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _validate_for_enable(client, key):
    """Phase 16 registered adjustment (BD-41): agent enablement is
    gated on a CURRENT validation record (app.agent_validation).
    This suite was written in parallel with the gate; tests enabling
    the answerer mint the record first via a one-case recorded-mode
    dataset run. No assertion is changed.

    C-AI registered adjustment (2026-10-21): enablement ADDITIONALLY
    requires a current, approved validation statement
    (app.ai_governance) — the same gate the C-AI suites assert
    directly (enable with a record but no statement is a 409 naming
    the statement). This helper therefore finishes with the shared
    tests.conftest.governance_for_enable flow, which finds the
    record minted above already current and authors + approves the
    statement. This suite was missed by the C-AI test migration
    (phase10/workflows suites were migrated); the three /rag/answer
    tests failed at the parent gate on exactly this gap."""
    r = client.post("/agent-validation/datasets", json={
        "key": f"enable-{key}",
        "name": f"Enablement validation for {key}",
        "cases": [{"id": "c1", "prompt": "Answer from the context.",
                   "recordedOutput": "DRAFT: answer.",
                   "checks": [{"type": "contains", "value": "DRAFT"}]}]},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post(f"/agent-validation/datasets/enable-{key}/run",
                    json={"agentKey": key, "mode": "recorded"},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    governance_for_enable(client, key)


def _publish_agent(client, key, payload):
    """Publish an agent config. If a DRAFT already exists for the key
    (post-integration, ensure_rag_defaults seeds a draft
    `rag-answerer`), publish THAT draft instead of creating one —
    the run paths under test do not depend on which draft payload
    was published, only on published + enabled state."""
    r = client.post(f"/studio/config/agent/{key}", json=payload,
                    headers=ADMIN)
    if r.status_code == 409:
        versions = client.get(f"/studio/config/agent/{key}",
                              headers=ADMIN).json()
        draft = next(v for v in versions if v["status"] == "draft")
        version = draft["version"]
    else:
        assert r.status_code == 201, r.text
        version = r.json()["version"]
    r = client.post(
        f"/studio/config/agent/{key}/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text


def _agent_payload(**overrides):
    payload = {
        "description": "RAG answer agent (test)",
        "model": "ollama_chat/llama3.2:3b",
        "instructions": "Answer only from the context chunks.",
        "tools": ["opencare_api_call"],
        "apiAllowlist": ["/rag", "/fhir"],
        "modalities": {"text": True, "image": False, "audio": False},
    }
    payload.update(overrides)
    return payload


def _setup_corpus(client):
    _grant_rag(client)
    _seed_knowledge(client, "diabetes-hba1c", "Diabetes HbA1c guidance",
                    KNOWLEDGE_BODY)
    r = client.post("/rag/ingest/knowledge", headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Chunking + embedder units (deterministic, no model anywhere)
# ---------------------------------------------------------------------------

def test_chunking_and_hash_embedder_are_deterministic():
    from app import rag
    body = ("Alpha paragraph about diabetes care.\n\n"
            "Beta paragraph about kidney screening.\n\n"
            + "gamma " * 400)
    first = rag.chunk_text(body)
    assert first == rag.chunk_text(body)
    assert len(first) >= 3
    assert all(len(c) <= rag.CHUNK_CHARS for c in first)
    v1 = rag.embed_local_hash("HbA1c target diabetes")
    assert v1 == rag.embed_local_hash("HbA1c target diabetes")
    assert len(v1) == rag.EMBED_DIM
    assert abs(sum(x * x for x in v1) - 1.0) < 1e-6
    # Lexical stand-in honesty: the embedder is labelled, never semantic.
    assert rag.EMBEDDER_LOCAL == "local-hash-v1"


# ---------------------------------------------------------------------------
# Ingest + search (works with NO model configured)
# ---------------------------------------------------------------------------

def test_ingest_knowledge_and_search_returns_cited_chunk(client):
    outcome = _setup_corpus(client)
    assert outcome["ingested_documents"] == 1
    assert outcome["chunks"] >= 1
    assert outcome["store"] == "local-json"
    assert outcome["embedders"] == ["local-hash-v1"]

    r = client.post("/rag/search", json={"query": "HbA1c target diabetes"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["store"] == "local-json"
    assert body["embedder"] == "local-hash-v1"
    assert body["embedder_kind"] == "local-hash"
    assert body["total"] >= 1
    top = body["results"][0]
    assert top["sourceType"] == "knowledge"
    assert top["sourceId"] == "knowledge/diabetes-hba1c"
    assert top["title"] == "Diabetes HbA1c guidance"
    assert "HbA1c" in top["snippet"]
    assert "HbA1c" in top["text"]
    # Citations carry exactly the citation fields.
    citation = body["citations"][0]
    assert citation["sourceType"] == "knowledge"
    assert citation["sourceId"] == "knowledge/diabetes-hba1c"
    assert citation["title"] == "Diabetes HbA1c guidance"
    assert citation["snippet"]

    # Retrieval is audited with the query HASH + cited ids, never the
    # raw query text.
    rows = _audit(client, "rag.search")
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["query_sha256"] == hashlib.sha256(
        b"HbA1c target diabetes").hexdigest()
    assert payload["cited_chunk_ids"] == [
        r0["chunk_id"] for r0 in body["results"]]
    assert "HbA1c target diabetes" not in str(payload)


def test_reingest_knowledge_replaces_chunks(client):
    _setup_corpus(client)
    first = client.post("/rag/search",
                        json={"query": "metformin titration"},
                        headers=CLINICIAN).json()
    r = client.post("/rag/ingest/knowledge", headers=ADMIN)
    assert r.status_code == 201, r.text
    second = client.post("/rag/search",
                         json={"query": "metformin titration"},
                         headers=CLINICIAN).json()
    assert second["total"] == first["total"]


def test_search_requires_rag_search_capability(client):
    _setup_corpus(client)
    r = client.post("/rag/search", json={"query": "HbA1c"},
                    headers=VIEWER)
    assert r.status_code == 403
    r = client.post("/rag/search", json={"query": "HbA1c"})
    assert r.status_code == 401


def test_search_validation(client):
    _grant_rag(client)
    r = client.post("/rag/search", json={"query": "  "}, headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post("/rag/search", json={"query": "x", "topK": 99},
                    headers=CLINICIAN)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Chart ingest + patient-scoped search
# ---------------------------------------------------------------------------

def test_ingest_chart_and_patient_scoped_search(client):
    _grant_rag(client)
    _create_patient(client, "rag-pat-1")
    _create_condition(client, "cond-rag-1", "rag-pat-1",
                      "Type 2 diabetes mellitus with nocturnal "
                      "hypoglycaemia episodes")
    _create_patient(client, "rag-pat-2")
    _create_condition(client, "cond-rag-2", "rag-pat-2",
                      "Seasonal allergic rhinitis with sneezing")

    r = client.post("/rag/ingest/chart/rag-pat-1", headers=ADMIN)
    assert r.status_code == 201, r.text
    outcome = r.json()
    assert outcome["patient_ref"] == "Patient/rag-pat-1"
    assert outcome["resources"] == 2
    assert outcome["chunks"] >= 2
    assert outcome["store"] == "local-json"
    assert _audit(client, "rag.ingest.chart")

    r = client.post("/rag/search",
                    json={"query": "nocturnal hypoglycaemia diabetes",
                          "patientRef": "Patient/rag-pat-1", "topK": 5},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    chart_hits = [x for x in body["results"]
                  if x["sourceType"] == "chart"]
    assert chart_hits, body
    assert chart_hits[0]["sourceId"] == "Condition/cond-rag-1"
    assert chart_hits[0]["patient_ref"] == "Patient/rag-pat-1"
    assert "hypoglycaemia" in chart_hits[0]["snippet"]
    # Patient scoping: the other patient's chart chunks never appear.
    assert all(x["patient_ref"] in (None, "Patient/rag-pat-1")
               for x in body["results"])
    assert not any(x["sourceId"] == "Condition/cond-rag-2"
                   for x in body["results"])
    # Patient-scoped audit: hash + patient ref only, no raw query.
    rows = _audit(client, "rag.search")
    scoped = [x for x in rows
              if x["payload"].get("patient_ref") == "Patient/rag-pat-1"]
    assert scoped
    assert "nocturnal hypoglycaemia diabetes" not in str(scoped[-1]["payload"])
    assert scoped[-1]["payload"]["query_sha256"] == hashlib.sha256(
        b"nocturnal hypoglycaemia diabetes").hexdigest()


def test_ingest_chart_unknown_patient_is_404(client):
    _grant_rag(client)
    r = client.post("/rag/ingest/chart/no-such-patient", headers=ADMIN)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Answer — honest-off, then the substituted-transport path
# ---------------------------------------------------------------------------

def test_answer_honest_off_when_no_agent_published(client):
    _setup_corpus(client)
    r = client.post("/rag/answer",
                    json={"query": "What is the HbA1c target?"},
                    headers=CLINICIAN)
    assert r.status_code == 503, r.text
    assert "No answer was generated" in r.json()["detail"]
    # Nothing recorded as a run, no rag.answer audit entry.
    assert _agent_run_count(client) == 0
    assert _audit(client, "rag.answer") == []
    assert _audit(client, "agents.run") == []


def test_answer_no_model_runtime_503_records_nothing(client, monkeypatch):
    _setup_corpus(client)
    _publish_agent(client, "rag-answerer", _agent_payload(model=""))
    _validate_for_enable(client, "rag-answerer")  # BD-41 gate
    r = client.post("/agents/rag-answerer/enable", headers=ADMIN)
    assert r.status_code == 200, r.text

    def _no_model(payload):
        return 503, {"error": "No model configured: set a model on "
                              "the agent config or AGENT_MODEL on the "
                              "ADK runtime. No answer was generated."}

    monkeypatch.setattr(agents_module, "_post_run", _no_model)
    r = client.post("/rag/answer",
                    json={"query": "What is the HbA1c target?"},
                    headers=CLINICIAN)
    assert r.status_code == 503, r.text
    assert "No model configured" in r.json()["detail"]
    assert _agent_run_count(client) == 0
    assert _audit(client, "rag.answer") == []


def test_answer_runtime_unreachable_is_honest_503(client, monkeypatch):
    _setup_corpus(client)
    _publish_agent(client, "rag-answerer", _agent_payload())
    _validate_for_enable(client, "rag-answerer")  # BD-41 gate
    client.post("/agents/rag-answerer/enable", headers=ADMIN)

    def _boom(payload):
        raise httpx.ConnectError("connection refused", request=None)

    monkeypatch.setattr(agents_module, "_post_run", _boom)
    r = client.post("/rag/answer", json={"query": "HbA1c target?"},
                    headers=CLINICIAN)
    assert r.status_code == 503
    assert "No answer was generated" in r.json()["detail"]
    assert _agent_run_count(client) == 0


def test_answer_with_substituted_transport_returns_citations(
        client, monkeypatch):
    _setup_corpus(client)
    _publish_agent(client, "rag-answerer", _agent_payload())
    _validate_for_enable(client, "rag-answerer")  # BD-41 gate
    client.post("/agents/rag-answerer/enable", headers=ADMIN)
    seen = {}

    def _ok(payload):
        seen.update(payload)
        return 200, {"output": "The HbA1c target is below 7 percent [1].",
                     "model": "ollama_chat/llama3.2:3b",
                     "agent": "rag-answerer"}

    monkeypatch.setattr(agents_module, "_post_run", _ok)
    r = client.post("/rag/answer",
                    json={"query": "What is the HbA1c target for diabetes?"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "The HbA1c target is below 7 percent [1]."
    assert body["store"] == "local-json"
    assert body["run_id"]
    assert body["disposition"] == "pending"
    assert body["citations"]
    citation = body["citations"][0]
    assert citation["sourceType"] == "knowledge"
    assert citation["sourceId"] == "knowledge/diabetes-hba1c"
    assert citation["title"] == "Diabetes HbA1c guidance"
    assert "HbA1c" in citation["snippet"]
    # The runtime received the grounding instruction + retrieved
    # context in the prompt, and the caller's token.
    assert "GROUNDING RULE" in seen["agent"]["instructions"]
    assert "HbA1c target" in seen["prompt"]
    assert "What is the HbA1c target" in seen["prompt"]
    assert seen["apiToken"] == "clinician-token"
    # Recorded as a governed agent run + rag.answer audit with the
    # cited chunk ids and the query hash (never the raw query).
    assert _agent_run_count(client) == 1
    rows = _audit(client, "rag.answer")
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["run_id"] == body["run_id"]
    assert payload["cited_chunk_ids"] == [
        x["chunk_id"] for x in body["results"]]
    assert payload["query_sha256"] == hashlib.sha256(
        b"What is the HbA1c target for diabetes?").hexdigest()
    assert _audit(client, "agents.run")


def test_status_reports_store_and_embedder_honesty(client):
    _setup_corpus(client)
    r = client.get("/rag/status", headers=CONFIG)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["store"] == "local-json"
    assert body["pgvector_available"] is False
    assert body["embedder"] == "local-hash-v1"
    assert "NOT a semantic model" in body["embedder_note"]
    assert body["chunks"] >= 1
