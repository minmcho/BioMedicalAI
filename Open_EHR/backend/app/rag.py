"""Long-context RAG with citations — Phase 16, WS-C2 (+ BD-45).

What lives here:

- **Corpus.** Two ingestible source families:
  (a) governed knowledge documents — Studio config kind ``knowledge``
      (payload ``{title, body, source}``; only PUBLISHED versions are
      ingestible, drafts are designer data and never enter the index);
  (b) the chart — ``POST /rag/ingest/chart/{patient_id}`` chunks the
      patient's FHIR resources out of the FHIR store (every resource
      whose ``patient_ref`` projection is that patient, the Patient
      resource itself included).

- **Chunking** is a deterministic paragraph/window splitter
  (:func:`chunk_text`): paragraphs are packed up to ``CHUNK_CHARS`` and
  over-long paragraphs are cut into overlapping windows. Same input ->
  same chunks, always; re-ingesting a source REPLACES its chunks.

- **Embeddings.** The built-in embedder is ``local-hash-v1``: a
  deterministic hashing embedder (token -> sha256 bucket, L2
  normalised). It is a LEXICAL stand-in — bag-of-words overlap, NOT a
  semantic model — and it is labelled that way in code, in the API
  responses (``embedder`` / ``embedder_kind`` fields) and in the
  Studio Knowledge surface. An optional endpoint embedder is used
  when a PUBLISHED ``ai-provider`` config is ``enabled`` and names an
  embedding route (``embeddingModel`` + ``baseUrl``): the module then
  POSTs ``{baseUrl}/embeddings`` (OpenAI-shaped) and names that
  provider's model as the embedder. Any endpoint failure falls back
  to ``local-hash-v1`` and the response says so — a failed embedding
  call is never silently dressed up as a semantic embedding.
  Vectors are stored at a fixed ``EMBED_DIM`` (endpoint vectors are
  truncated / zero-padded and renormalised) so the two stores below
  always rank the same numbers.

- **Vector storage (BD-45).** Default: the ``rag_chunks`` table holds
  each chunk's vector as JSON and search is in-Python cosine. When
  the application database is PostgreSQL, ingest ADDITIONALLY
  maintains ``rag_chunk_pg`` — a table with a real pgvector
  ``vector(64)`` column (``CREATE EXTENSION IF NOT EXISTS vector``) —
  and search is served from it with the ``<->`` operator. The API is
  identical either way and every search/answer response names the
  store that actually served it: ``store: "pgvector" | "local-json"``.

- **Endpoints.**
  ``POST /rag/search`` — retrieval + citations; works with NO model
  configured (it never calls one).
  ``POST /rag/answer`` — retrieval, then ONE governed agent run
  through ``app.agents``' runtime transport (the seeded DRAFT agent
  ``rag-answerer``, published + enabled by an operator like every
  agent) whose instructions are extended with a grounding rule:
  answer ONLY from the retrieved context, cite the chunks used. The
  response carries the answer plus the citation list. Honest-off,
  exactly as ``app.agents``: no published/enabled answer agent, a
  runtime with no model, or an unreachable runtime -> the explicit
  503, NOTHING recorded as a run, no canned text ever emitted.

- **Audit.** Retrieval is hash-chain audited (``rag.search`` /
  ``rag.answer``) with the query's sha256, the cited chunk ids and
  the patient ref. The RAW query text of a patient-scoped search is
  never written to the audit detail (this module in fact never writes
  raw query text at all — the hash is the auditable artefact).
  Ingestion is audited as ``rag.ingest.knowledge`` /
  ``rag.ingest.chart`` in the same transaction as the chunk writes.

Capabilities: everything here rides ``rag.search`` (coordinator wires
the role defaults centrally; proposed: admin / configurator /
clinician, viewer read-only-safe for search only — see the WS-C2
integration note). Studio surface: kind ``knowledge`` documents are
the governed corpus; the answer agent is ordinary kind ``agent``
config; the embedding route is ordinary kind ``ai-provider`` config.
Configure, don't fork.
"""
import hashlib
import math
import re
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import (DateTime, Integer, JSON, String, Text, delete,
                        select, text as sql_text)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import agents as agents_module
from .audit import append_audit
from .configstore import published_config, published_configs
from .db import Base
from .models import AgentRun, FhirResource, User, utcnow
from .security import get_db, require_capability

router = APIRouter(prefix="/rag", tags=["rag"])

KNOWLEDGE_KIND = "knowledge"
RAG_AGENT_KEY = "rag-answerer"
EMBEDDER_LOCAL = "local-hash-v1"
EMBED_DIM = 64
CHUNK_CHARS = 600
CHUNK_OVERLAP = 80
SNIPPET_CHARS = 240
DEFAULT_TOP_K = 5
MAX_TOP_K = 20

GROUNDING_INSTRUCTION = (
    "RAG GROUNDING RULE (added by app.rag for this run): Answer ONLY "
    "from the context chunks in the prompt. Every factual claim must "
    "be supported by one of those chunks; refer to them by their "
    "bracket numbers. If the context does not contain the answer, "
    "say exactly that — never use outside knowledge, never invent "
    "values, findings or history.")


# ---------------------------------------------------------------------------
# This module's own table (JSON-vector store)
# ---------------------------------------------------------------------------

class RagChunk(Base):
    """One retrievable chunk: its source citation identity, the text,
    and its embedding as a JSON vector (the portable default store).
    On PostgreSQL the same chunk is mirrored into ``rag_chunk_pg``
    with a pgvector column — see :func:`ensure_pg_table`."""
    __tablename__ = "rag_chunks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False,
                                             index=True)  # knowledge | chart
    source_id: Mapped[str] = mapped_column(String(200), nullable=False,
                                           index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    patient_ref: Mapped[str | None] = mapped_column(String(200),
                                                    nullable=True, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    embedding: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    embedder: Mapped[str] = mapped_column(String(120), nullable=False,
                                          default=EMBEDDER_LOCAL)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


# ---------------------------------------------------------------------------
# Chunking — deterministic paragraph/window splitter
# ---------------------------------------------------------------------------

def chunk_text(body: str, max_chars: int = CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split `body` into chunks deterministically: paragraphs (split
    on blank lines) are packed in order until the next would exceed
    `max_chars`; a paragraph longer than `max_chars` is cut into
    windows of `max_chars` stepping `max_chars - overlap`. Whitespace
    is normalised per paragraph. Same input -> same output."""
    paragraphs = []
    for raw in re.split(r"\n\s*\n", body or ""):
        para = re.sub(r"\s+", " ", raw).strip()
        if para:
            paragraphs.append(para)
    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        if len(para) > max_chars:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            step = max(1, max_chars - overlap)
            for start in range(0, len(para), step):
                chunks.append(para[start:start + max_chars])
                if start + max_chars >= len(para):
                    break
        elif not buffer:
            buffer = para
        elif len(buffer) + 1 + len(para) <= max_chars:
            buffer = buffer + "\n" + para
        else:
            chunks.append(buffer)
            buffer = para
    if buffer:
        chunks.append(buffer)
    return chunks


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def embed_local_hash(value: str) -> list[float]:
    """`local-hash-v1`: deterministic hashing embedder. Each token is
    hashed (sha256) into one of EMBED_DIM buckets and counted; the
    vector is L2-normalised. This is a LEXICAL stand-in (word-overlap
    geometry) — NOT a semantic model; synonyms do not meet here."""
    vector = [0.0] * EMBED_DIM
    for token in _TOKEN_RE.findall((value or "").lower()):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % EMBED_DIM
        vector[bucket] += 1.0 + (digest[4] / 255.0)
    return _normalise(vector)


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return [0.0] * len(vector)
    return [v / norm for v in vector]


def _resize(vector: list[float]) -> list[float]:
    """Truncate / zero-pad an endpoint vector to EMBED_DIM and
    renormalise, so both stores rank the same numbers."""
    resized = [float(v) for v in list(vector)[:EMBED_DIM]]
    resized.extend([0.0] * (EMBED_DIM - len(resized)))
    return _normalise(resized)


def _endpoint_embedder_config(session: Session) -> dict | None:
    """The first PUBLISHED + enabled ai-provider config that names an
    embedding route (``embeddingModel`` + ``baseUrl``), or None."""
    for cfg in published_configs(session, "ai-provider"):
        payload = cfg.payload or {}
        if payload.get("enabled") is True \
                and isinstance(payload.get("embeddingModel"), str) \
                and payload["embeddingModel"].strip() \
                and isinstance(payload.get("baseUrl"), str) \
                and payload["baseUrl"].strip():
            return {"key": cfg.key, "model": payload["embeddingModel"],
                    "baseUrl": payload["baseUrl"].rstrip("/")}
    return None


def _embed_via_endpoint(config: dict, value: str) -> list[float]:
    """POST {baseUrl}/embeddings (OpenAI-shaped). Raises on any
    transport/shape problem — the caller falls back honestly."""
    with httpx.Client(trust_env=False, timeout=15.0) as client:
        response = client.post(
            f"{config['baseUrl']}/embeddings",
            json={"model": config["model"], "input": value})
    response.raise_for_status()
    body = response.json()
    vector = None
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            vector = data[0].get("embedding")
        if vector is None:
            vector = body.get("embedding")
    if not isinstance(vector, list) or not vector \
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       for v in vector):
        raise ValueError("embedding endpoint returned no numeric vector")
    return _resize([float(v) for v in vector])


def embed_text(session: Session, value: str) -> tuple[list[float], str, str]:
    """(vector, embedder_name, embedder_kind). Uses the endpoint
    embedder when a published ai-provider config names one; ANY
    failure falls back to `local-hash-v1` and the returned name says
    so (`local-hash-v1 (endpoint fallback)`) — never a silent swap."""
    config = _endpoint_embedder_config(session)
    if config is not None:
        try:
            return (_embed_via_endpoint(config, value),
                    config["model"], "endpoint")
        except Exception:
            return (embed_local_hash(value),
                    f"{EMBEDDER_LOCAL} (endpoint fallback)", "local-hash")
    return embed_local_hash(value), EMBEDDER_LOCAL, "local-hash"


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    return sum(x * y for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# pgvector store (BD-45) — maintained only when the app DB is PostgreSQL
# ---------------------------------------------------------------------------

def _is_postgres(session: Session) -> bool:
    try:
        return session.get_bind().url.get_dialect().name == "postgresql"
    except Exception:
        return False


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{float(v):.8f}" for v in vector) + "]"


def ensure_pg_table(session: Session) -> bool:
    """Create the pgvector mirror table when on PostgreSQL. Returns
    True when the pgvector store is available. The table is raw SQL,
    deliberately NOT a Base model: the `vector` type does not exist
    on SQLite, and the JSON store above is the portable default."""
    if not _is_postgres(session):
        return False
    session.execute(sql_text(
        "CREATE EXTENSION IF NOT EXISTS vector"))
    session.execute(sql_text(
        "CREATE TABLE IF NOT EXISTS rag_chunk_pg ("
        " chunk_id VARCHAR(120) PRIMARY KEY,"
        " source_type VARCHAR(40) NOT NULL,"
        " source_id VARCHAR(200) NOT NULL,"
        " title VARCHAR(300) NOT NULL DEFAULT '',"
        " patient_ref VARCHAR(200),"
        " chunk_index INTEGER NOT NULL DEFAULT 0,"
        " text TEXT NOT NULL DEFAULT '',"
        " embedder VARCHAR(120) NOT NULL DEFAULT 'local-hash-v1',"
        f" embedding vector({EMBED_DIM}) NOT NULL)"))
    session.execute(sql_text(
        "CREATE INDEX IF NOT EXISTS rag_chunk_pg_source_idx "
        "ON rag_chunk_pg (source_id)"))
    return True


def _pg_delete_source(session: Session, source_id: str) -> None:
    session.execute(sql_text(
        "DELETE FROM rag_chunk_pg WHERE source_id = :sid"),
        {"sid": source_id})


def _pg_delete_patient_chart(session: Session, patient_ref: str) -> None:
    session.execute(sql_text(
        "DELETE FROM rag_chunk_pg WHERE source_type = 'chart' "
        "AND patient_ref = :pref"), {"pref": patient_ref})


def _pg_upsert_chunk(session: Session, chunk: RagChunk) -> None:
    session.execute(sql_text(
        "INSERT INTO rag_chunk_pg (chunk_id, source_type, source_id,"
        " title, patient_ref, chunk_index, text, embedder, embedding)"
        " VALUES (:cid, :stype, :sid, :title, :pref, :idx, :txt,"
        " :emb, CAST(:vec AS vector))"
        " ON CONFLICT (chunk_id) DO UPDATE SET"
        " source_type = EXCLUDED.source_type,"
        " source_id = EXCLUDED.source_id, title = EXCLUDED.title,"
        " patient_ref = EXCLUDED.patient_ref,"
        " chunk_index = EXCLUDED.chunk_index, text = EXCLUDED.text,"
        " embedder = EXCLUDED.embedder, embedding = EXCLUDED.embedding"),
        {"cid": chunk.chunk_id, "stype": chunk.source_type,
         "sid": chunk.source_id, "title": chunk.title,
         "pref": chunk.patient_ref, "idx": chunk.chunk_index,
         "txt": chunk.text, "emb": chunk.embedder,
         "vec": _vector_literal(chunk.embedding)})


def _pg_search(session: Session, query_vector: list[float],
               patient_ref: str | None, top_k: int) -> list[dict]:
    """`<->` (L2) search over the pgvector mirror. Vectors are
    L2-normalised, so L2 order == cosine order; the score reported
    is 1 - distance^2 / 2 (the cosine of unit vectors)."""
    rows = session.execute(sql_text(
        "SELECT chunk_id, source_type, source_id, title, patient_ref,"
        " chunk_index, text, embedder,"
        " embedding <-> CAST(:qvec AS vector) AS distance"
        " FROM rag_chunk_pg"
        # CASTs are load-bearing: with psycopg's server-side binding,
        # an untyped :pref in an IS NULL disjunct is an
        # AmbiguousParameter on the live engine (found by the BD-45
        # live run, 2026-10-19 — the SQLite path never sees this SQL).
        " WHERE (CAST(:pref AS VARCHAR) IS NULL"
        "        OR patient_ref = CAST(:pref AS VARCHAR)"
        "        OR patient_ref IS NULL)"
        " ORDER BY distance ASC LIMIT :k"),
        {"qvec": _vector_literal(query_vector), "pref": patient_ref,
         "k": top_k}).mappings().all()
    results = []
    for row in rows:
        distance = float(row["distance"])
        results.append({
            "chunk_id": row["chunk_id"], "source_type": row["source_type"],
            "source_id": row["source_id"], "title": row["title"],
            "patient_ref": row["patient_ref"],
            "chunk_index": row["chunk_index"], "text": row["text"],
            "embedder": row["embedder"],
            "score": round(1.0 - (distance * distance) / 2.0, 6)})
    return results


# ---------------------------------------------------------------------------
# Knowledge payload validation (coordinator registers this as the
# kind `knowledge` validator in app.configstore)
# ---------------------------------------------------------------------------

def validate_knowledge_payload(payload) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422,
                            detail="Knowledge document must be a JSON object")
    for field in ("title", "body"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(
                status_code=422,
                detail=f"Knowledge document requires a non-empty '{field}'")
    if payload.get("source") is not None \
            and not isinstance(payload["source"], str):
        raise HTTPException(status_code=422,
                            detail="Knowledge 'source' must be a string")
    return payload


def default_rag_agent_payload() -> dict:
    return {
        "description": ("Phase 16 (WS-C2): answers questions ONLY from "
                        "retrieved RAG context (knowledge documents + "
                        "the patient's chart chunks) and cites the "
                        "chunks used. Draft only — the answer is a "
                        "clinician-facing draft, never a chart write."),
        "riskTier": "T1",
        "model": "",
        "instructions": (
            "You are the OpenCare RAG answer agent. Answer the "
            "question using ONLY the context chunks supplied in the "
            "prompt, and refer to the chunks by their bracket numbers "
            "so the reader can follow the citations. If the context "
            "does not answer the question, say so plainly. Never "
            "invent values, findings, history or guidance that the "
            "context does not contain."),
        "tools": ["opencare_api_call"],
        "apiAllowlist": ["/rag", "/fhir"],
        "modalities": {"text": True, "image": False, "audio": False},
        "temperature": 0.2,
        "maxSteps": 6,
    }


def ensure_rag_defaults(session: Session) -> None:
    """Seed the DRAFT `rag-answerer` agent (never self-published,
    never enabled — an operator reviews, sets a model route,
    publishes and enables it through ordinary Studio governance)."""
    agents_module._seed_draft(session, RAG_AGENT_KEY,
                              default_rag_agent_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

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


def _store_chunk(session: Session, *, source_type: str, source_id: str,
                 title: str, patient_ref: str | None, index: int,
                 chunk_value: str, vector: list[float],
                 embedder: str, pg: bool) -> RagChunk:
    chunk_id = "rag-" + hashlib.sha256(
        f"{source_id}|{index}|{chunk_value}".encode("utf-8")).hexdigest()[:32]
    row = RagChunk(chunk_id=chunk_id, source_type=source_type,
                   source_id=source_id, title=title,
                   patient_ref=patient_ref, chunk_index=index,
                   text=chunk_value, embedding=vector, embedder=embedder)
    session.add(row)
    session.flush()
    if pg:
        _pg_upsert_chunk(session, row)
    return row


@router.post("/ingest/knowledge", status_code=201)
def ingest_knowledge(session: Session = Depends(get_db),
                     user: User = Depends(require_capability("rag.search"))):
    """Ingest every PUBLISHED kind `knowledge` document. Re-ingesting
    replaces a document's chunks (no duplicates). Malformed published
    payloads are reported in `skipped`, never silently indexed."""
    pg = ensure_pg_table(session)
    documents = 0
    chunk_count = 0
    skipped: list[dict] = []
    embedders: set[str] = set()
    for cfg in published_configs(session, KNOWLEDGE_KIND):
        payload = cfg.payload or {}
        title = payload.get("title")
        body = payload.get("body")
        if not isinstance(title, str) or not title.strip() \
                or not isinstance(body, str) or not body.strip():
            skipped.append({"key": cfg.key,
                            "reason": "published knowledge payload lacks "
                                      "a non-empty 'title' or 'body'"})
            continue
        source_id = f"knowledge/{cfg.key}"
        session.execute(delete(RagChunk).where(
            RagChunk.source_id == source_id))
        if pg:
            _pg_delete_source(session, source_id)
        session.flush()
        for index, piece in enumerate(chunk_text(body)):
            vector, embedder, _kind = embed_text(session, piece)
            embedders.add(embedder)
            _store_chunk(session, source_type="knowledge",
                         source_id=source_id, title=title.strip(),
                         patient_ref=None, index=index, chunk_value=piece,
                         vector=vector, embedder=embedder, pg=pg)
            chunk_count += 1
        documents += 1
    session.flush()
    append_audit(session, actor=user.username,
                 action="rag.ingest.knowledge", entity="rag/knowledge",
                 payload={"documents": documents, "chunks": chunk_count,
                          "skipped": [s["key"] for s in skipped],
                          "store": "pgvector" if pg else "local-json"})
    session.commit()
    return {"ingested_documents": documents, "chunks": chunk_count,
            "skipped": skipped,
            "embedders": sorted(embedders) or [EMBEDDER_LOCAL],
            "store": "pgvector" if pg else "local-json"}


def _resource_text(body: dict) -> str:
    """Flatten a FHIR resource body into deterministic readable text:
    `path: scalar` lines in sorted-key order (lists in order)."""
    lines: list[str] = []

    def walk(value, path: str) -> None:
        if isinstance(value, dict):
            for key in sorted(value.keys()):
                walk(value[key], f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, f"{path}[{i}]")
        elif isinstance(value, (str, int, float, bool)):
            if path and str(value).strip() != "":
                lines.append(f"{path}: {value}")

    walk(body or {}, "")
    return "\n".join(lines)


def _resource_title(row: FhirResource) -> str:
    body = row.body or {}
    if row.resource_type == "Patient":
        names = body.get("name") or []
        if names and isinstance(names[0], dict):
            family = names[0].get("family") or ""
            given = " ".join(names[0].get("given") or [])
            label = f"{given} {family}".strip()
            if label:
                return f"Patient {label} ({row.resource_id})"
    code = body.get("code") or {}
    if isinstance(code, dict) and isinstance(code.get("text"), str) \
            and code["text"].strip():
        return f"{row.resource_type} {row.resource_id} — {code['text']}"
    return f"{row.resource_type} {row.resource_id}"


@router.post("/ingest/chart/{patient_id}", status_code=201)
def ingest_chart(patient_id: str, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("rag.search"))):
    """Chunk one patient's FHIR resources from the store (the Patient
    resource + every resource projected to that patient). Re-ingesting
    replaces the patient's chart chunks."""
    patient_ref = f"Patient/{patient_id}"
    patient = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Patient",
        FhirResource.resource_id == patient_id)).first()
    if patient is None:
        raise HTTPException(status_code=404,
                            detail=f"Patient '{patient_id}' not found")
    pg = ensure_pg_table(session)
    session.execute(delete(RagChunk).where(
        RagChunk.source_type == "chart",
        RagChunk.patient_ref == patient_ref))
    if pg:
        _pg_delete_patient_chart(session, patient_ref)
    session.flush()
    rows = session.scalars(select(FhirResource).where(
        FhirResource.patient_ref == patient_ref)
        .order_by(FhirResource.resource_type.asc(),
                  FhirResource.resource_id.asc())).all()
    chunk_count = 0
    embedders: set[str] = set()
    for row in rows:
        body_text = _resource_text(row.body or {})
        if not body_text.strip():
            continue
        source_id = f"{row.resource_type}/{row.resource_id}"
        for index, piece in enumerate(chunk_text(body_text)):
            vector, embedder, _kind = embed_text(session, piece)
            embedders.add(embedder)
            _store_chunk(session, source_type="chart",
                         source_id=source_id,
                         title=_resource_title(row),
                         patient_ref=patient_ref, index=index,
                         chunk_value=piece, vector=vector,
                         embedder=embedder, pg=pg)
            chunk_count += 1
    session.flush()
    append_audit(session, actor=user.username, action="rag.ingest.chart",
                 entity=patient_ref,
                 payload={"patient_ref": patient_ref,
                          "resources": len(rows), "chunks": chunk_count,
                          "store": "pgvector" if pg else "local-json"})
    session.commit()
    return {"patient_ref": patient_ref, "resources": len(rows),
            "chunks": chunk_count,
            "embedders": sorted(embedders) or [EMBEDDER_LOCAL],
            "store": "pgvector" if pg else "local-json"}


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _citation(result: dict) -> dict:
    return {"sourceType": result["source_type"],
            "sourceId": result["source_id"],
            "title": result["title"],
            "snippet": result["text"][:SNIPPET_CHARS]}


def _result_out(result: dict) -> dict:
    return {"chunk_id": result["chunk_id"],
            "sourceType": result["source_type"],
            "sourceId": result["source_id"],
            "title": result["title"],
            "patient_ref": result["patient_ref"],
            "chunk_index": result["chunk_index"],
            "text": result["text"],
            "snippet": result["text"][:SNIPPET_CHARS],
            "score": result["score"],
            "embedder": result["embedder"],
            "citation": _citation(result)}


def _retrieve(session: Session, query: str, patient_ref: str | None,
              top_k: int) -> tuple[list[dict], str, str, str]:
    """(results, store, embedder_name, embedder_kind). pgvector when
    the app DB is PostgreSQL, in-Python cosine over JSON otherwise."""
    vector, embedder, kind = embed_text(session, query)
    pg = ensure_pg_table(session)
    if pg:
        raw = _pg_search(session, vector, patient_ref, top_k)
        return raw, "pgvector", embedder, kind
    stmt = select(RagChunk)
    if patient_ref:
        stmt = stmt.where((RagChunk.patient_ref == patient_ref)
                          | (RagChunk.patient_ref.is_(None)))
    chunks = session.scalars(stmt).all()
    scored = [{"chunk_id": c.chunk_id, "source_type": c.source_type,
               "source_id": c.source_id, "title": c.title,
               "patient_ref": c.patient_ref,
               "chunk_index": c.chunk_index, "text": c.text,
               "embedder": c.embedder,
               "score": round(_cosine(vector, c.embedding or []), 6)}
              for c in chunks]
    scored.sort(key=lambda r: (-r["score"], r["chunk_id"]))
    return scored[:top_k], "local-json", embedder, kind


def _parse_search_body(body: dict) -> tuple[str, str | None, int]:
    query = body.get("query") or body.get("question")
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=422,
                            detail="Search requires a non-empty 'query'")
    patient_ref = body.get("patientRef") or body.get("patient_ref")
    if patient_ref is not None:
        if not isinstance(patient_ref, str) or not patient_ref.strip():
            raise HTTPException(status_code=422,
                                detail="'patientRef' must be a string")
        patient_ref = patient_ref.strip()
        if not patient_ref.startswith("Patient/"):
            patient_ref = f"Patient/{patient_ref}"
    top_k = body.get("topK", body.get("top_k", DEFAULT_TOP_K))
    if isinstance(top_k, bool) or not isinstance(top_k, int) \
            or not (1 <= top_k <= MAX_TOP_K):
        raise HTTPException(
            status_code=422,
            detail=f"'topK' must be an integer in 1..{MAX_TOP_K}")
    return query.strip(), patient_ref, top_k


@router.post("/search")
async def search(request: Request, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("rag.search"))):
    """Retrieval + citations. Works with NO model configured — this
    endpoint never calls one. Patient-scoped searches include the
    patient's chart chunks plus the (patient-less) knowledge corpus."""
    body = await _json_body(request)
    query, patient_ref, top_k = _parse_search_body(body)
    results, store, embedder, kind = _retrieve(
        session, query, patient_ref, top_k)
    query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
    append_audit(session, actor=user.username, action="rag.search",
                 entity=patient_ref or "rag",
                 payload={"query_sha256": query_hash,
                          "patient_ref": patient_ref,
                          "cited_chunk_ids": [r["chunk_id"] for r in results],
                          "result_count": len(results), "store": store,
                          "embedder": embedder})
    session.commit()
    return {"results": [_result_out(r) for r in results],
            "citations": [_citation(r) for r in results],
            "total": len(results), "store": store,
            "embedder": embedder, "embedder_kind": kind,
            "query_sha256": query_hash, "patient_ref": patient_ref}


@router.get("/status")
def status(session: Session = Depends(get_db),
           user: User = Depends(require_capability("rag.search"))):
    pg = ensure_pg_table(session)
    total = len(session.scalars(select(RagChunk)).all())
    return {"chunks": total, "store": "pgvector" if pg else "local-json",
            "embedder": EMBEDDER_LOCAL, "embedder_kind": "local-hash",
            "embedder_note": ("local-hash-v1 is a deterministic lexical "
                              "hashing embedder — a stand-in, NOT a "
                              "semantic model"),
            "pgvector_available": pg,
            "answer_agent": RAG_AGENT_KEY}


# ---------------------------------------------------------------------------
# Answer — retrieval + ONE governed agent run (app.agents machinery)
# ---------------------------------------------------------------------------

@router.post("/answer")
async def answer(request: Request, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("rag.search"))):
    """Retrieve, then run the governed RAG answer agent over the
    retrieved context. The response carries the draft answer + the
    citation list. Honest-off (mirrors app.agents.run_agent): no
    published answer agent -> 503; published but not enabled -> 409;
    runtime unreachable / runtime has no model -> the explicit 503,
    and in every failure case NOTHING is recorded as a run."""
    body = await _json_body(request)
    query, patient_ref, top_k = _parse_search_body(body)
    agent_key = body.get("agentKey") or RAG_AGENT_KEY
    if not isinstance(agent_key, str) or not agent_key.strip():
        raise HTTPException(status_code=422,
                            detail="'agentKey' must be a string")
    agent_key = agent_key.strip()

    results, store, embedder, kind = _retrieve(
        session, query, patient_ref, top_k)
    citations = [_citation(r) for r in results]

    cfg = published_config(session, "agent", agent_key)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=(f"No RAG answer agent is published: agent "
                    f"'{agent_key}' has no published version, so no "
                    "model can answer. Retrieval still works via "
                    "POST /rag/search. No answer was generated."))
    if not agents_module._is_enabled(session, agent_key):
        raise HTTPException(
            status_code=409,
            detail=(f"Agent '{agent_key}' is published but not "
                    "enabled; an operator enables it in System Studio "
                    "before it can run"))

    payload = dict(cfg.payload or {})
    context = "\n\n".join(
        f"[{i + 1}] ({r['source_type']}: {r['source_id']} — "
        f"{r['title']})\n{r['text']}" for i, r in enumerate(results))
    prompt = (f"Context chunks:\n{context or '(no chunks retrieved)'}"
              f"\n\nQuestion: {query}")
    runtime_payload = {
        "agent": {**payload, "key": agent_key,
                  "instructions": ((payload.get("instructions") or "")
                                   + "\n\n" + GROUNDING_INSTRUCTION)},
        "prompt": prompt,
        "apiBaseUrl": agents_module.api_base_url(),
        "apiToken": request.headers.get("Authorization", "")
        .removeprefix("Bearer ").strip(),
    }
    try:
        status_code, result = agents_module._post_run(runtime_payload)
    except httpx.HTTPError:
        raise HTTPException(
            status_code=503,
            detail=(f"Agent runtime unavailable at "
                    f"{agents_module.runtime_url()} — start the ADK "
                    "service (backend/adk) or set ADK_RUNTIME_URL. "
                    "No answer was generated."))
    if status_code == 503:
        raise HTTPException(
            status_code=503,
            detail=result.get("error") or "Agent runtime unavailable")
    if status_code != 200:
        raise HTTPException(
            status_code=status_code if status_code in (400, 422) else 502,
            detail=result.get("error")
            or f"Agent runtime returned {status_code}")

    output = str(result.get("output") or "")
    input_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    run = AgentRun(run_id=str(uuid.uuid4()), agent_key=agent_key,
                   agent_version=cfg.version, actor=user.username,
                   patient_ref=patient_ref, input_hash=input_hash,
                   output=output, model=result.get("model"))
    session.add(run)
    session.flush()
    append_audit(session, actor=user.username, action="agents.run",
                 entity=f"agent:{agent_key}@v{cfg.version}",
                 payload={"agent": agent_key, "version": cfg.version,
                          "run_id": run.run_id, "input_hash": input_hash,
                          "patient": patient_ref,
                          "model": result.get("model"),
                          "output": output, "disposition": "pending",
                          "via": "rag.answer"})
    query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
    append_audit(session, actor=user.username, action="rag.answer",
                 entity=patient_ref or "rag",
                 payload={"query_sha256": query_hash,
                          "patient_ref": patient_ref,
                          "cited_chunk_ids": [r["chunk_id"] for r in results],
                          "run_id": run.run_id, "agent": agent_key,
                          "version": cfg.version, "store": store,
                          "embedder": embedder})
    session.commit()
    return {"answer": output, "output": output,
            "citations": citations,
            "results": [_result_out(r) for r in results],
            "run_id": run.run_id, "agent": agent_key,
            "version": cfg.version, "model": result.get("model"),
            "disposition": "pending", "store": store,
            "embedder": embedder, "embedder_kind": kind,
            "query_sha256": query_hash, "patient_ref": patient_ref}
