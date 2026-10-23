import { useState } from "react";
import type { ApiClient } from "../../api/client";
import { ApiError } from "../../api/client";
import type { ConfigKind } from "../../api/types";
import { RichTextEditor } from "../../components/RichTextEditor";
import { htmlToText } from "../../components/richtext";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";
import { ConfigEditorShell } from "./ConfigEditorShell";

// Knowledge & RAG tab (Phase 16, WS-K1) — long-context retrieval with
// citations (backend/app/rag.py). Knowledge documents are governed
// Studio config (kind=knowledge); only PUBLISHED documents are
// ingested into the chunk store. Search is pure retrieval — it works
// with no model configured and every result carries its citation.
// Answer runs ONE governed agent over the retrieved context; with no
// published/enabled answer agent or no model, the server's honest
// 503/409 is shown verbatim and nothing is recorded as a run. The
// status line names the store and embedder actually in use — the
// default embedder is a lexical hashing stand-in, NOT a semantic
// model, and the UI says so.

const KNOWLEDGE_KIND = "knowledge" as ConfigKind;

export interface KnowledgeModel {
  title: string;
  body: string;
  source: string;
}

export function parseKnowledge(payload: Record<string, unknown>): KnowledgeModel {
  return {
    title: String(payload.title ?? ""),
    body: String(payload.body ?? ""),
    source: String(payload.source ?? ""),
  };
}

export function serializeKnowledge(model: KnowledgeModel): Record<string, unknown> {
  const out: Record<string, unknown> = {
    title: model.title.trim(),
    body: model.body,
  };
  if (model.source.trim()) out.source = model.source.trim();
  return out;
}

export function emptyKnowledge(): KnowledgeModel {
  return { title: "", body: "", source: "" };
}

export function validateKnowledge(model: KnowledgeModel): string | null {
  if (!model.title.trim()) return "Title is required (the server refuses a knowledge document without one).";
  // The body is rich HTML now; "empty" means no text, not just no markup.
  if (!htmlToText(model.body).trim()) return "Body is required (the server refuses an empty knowledge document).";
  return null;
}

function KnowledgeEditor({
  model,
  setModel,
}: {
  model: KnowledgeModel;
  setModel: (m: KnowledgeModel) => void;
}) {
  return (
    <div className="studio-editor">
      <p className="hint">
        One knowledge document per config key. Only the PUBLISHED
        version is ingested into the retrieval corpus — drafts are
        invisible to search and answer.
      </p>
      <p className="studio-row">
        <label>
          Title{" "}
          <input
            aria-label="Knowledge title"
            value={model.title}
            onChange={(e) => setModel({ ...model, title: e.target.value })}
            size={48}
          />
        </label>{" "}
        <label>
          Source{" "}
          <input
            aria-label="Knowledge source"
            value={model.source}
            onChange={(e) => setModel({ ...model, source: e.target.value })}
            size={32}
            placeholder="optional provenance label"
          />
        </label>
      </p>
      <div>
        <RichTextEditor
          ariaLabel="Knowledge body"
          value={model.body}
          onChange={(html) => setModel({ ...model, body: html })}
          minRows={10}
        />
      </div>
      <p className="hint">
        The document text. It is chunked at ingest; keep facts explicit —
        answers may only use what is here.
      </p>
    </div>
  );
}

export interface RagStatus {
  chunks: number;
  store: string;
  embedder: string;
  embedder_kind: string;
  embedder_note: string;
  pgvector_available: boolean;
  answer_agent: string;
}

export interface RagCitation {
  sourceType: string;
  sourceId: string;
  title: string;
  snippet: string;
}

export interface RagResult {
  chunk_id: string;
  sourceType: string;
  sourceId: string;
  title: string;
  patient_ref: string | null;
  text: string;
  score: number;
  citation: RagCitation;
}

export interface RagSearchResponse {
  results: RagResult[];
  citations: RagCitation[];
  total: number;
  store: string;
  embedder: string;
  embedder_kind: string;
  patient_ref: string | null;
}

export interface RagAnswerResponse {
  answer: string;
  citations: RagCitation[];
  results: RagResult[];
  run_id: string;
  agent: string;
  disposition: string;
  store: string;
  embedder: string;
}

export interface IngestKnowledgeResponse {
  ingested_documents: number;
  chunks: number;
  skipped: Array<{ key: string; reason: string }>;
  embedders: string[];
  store: string;
}

function errText(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err);
}

function CitationList({ citations }: { citations: RagCitation[] }) {
  if (citations.length === 0) return <Empty label="No citations." />;
  return (
    <ol>
      {citations.map((c, i) => (
        <li key={i}>
          <strong>{c.title}</strong>{" "}
          <span className="hint">
            ({c.sourceType}: {c.sourceId})
          </span>
          <br />
          <span>{c.snippet}</span>
        </li>
      ))}
    </ol>
  );
}

export function KnowledgeTab({ client }: { client: ApiClient }) {
  const [reloadKey, setReloadKey] = useState(0);
  const status = useAsync<RagStatus>(
    () => client.request("/rag/status"),
    [client, reloadKey],
  );

  const [ingestError, setIngestError] = useState<string | null>(null);
  const [ingestResult, setIngestResult] = useState<IngestKnowledgeResponse | null>(null);
  const [chartPatientId, setChartPatientId] = useState("");
  const [chartResult, setChartResult] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [patientRef, setPatientRef] = useState("");
  const [searchResult, setSearchResult] = useState<RagSearchResponse | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [answerResult, setAnswerResult] = useState<RagAnswerResponse | null>(null);
  const [answerError, setAnswerError] = useState<string | null>(null);

  async function ingestKnowledge() {
    setIngestError(null);
    setIngestResult(null);
    try {
      const out = await client.request<IngestKnowledgeResponse>("/rag/ingest/knowledge", {
        method: "POST",
        body: JSON.stringify({}),
      });
      setIngestResult(out);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setIngestError(errText(err));
    }
  }

  async function ingestChart() {
    setIngestError(null);
    setChartResult(null);
    try {
      const out = await client.request<{ patient_ref: string; resources: number; chunks: number }>(
        `/rag/ingest/chart/${encodeURIComponent(chartPatientId.trim())}`,
        { method: "POST", body: JSON.stringify({}) },
      );
      setChartResult(
        `Ingested ${out.resources} resources for ${out.patient_ref} as ${out.chunks} chunks.`,
      );
      setReloadKey((k) => k + 1);
    } catch (err) {
      setIngestError(errText(err));
    }
  }

  function ragBody() {
    return {
      query: query.trim(),
      patientRef: patientRef.trim() || undefined,
      topK: 5,
    };
  }

  async function doSearch() {
    setSearchError(null);
    setSearchResult(null);
    try {
      setSearchResult(
        await client.request<RagSearchResponse>("/rag/search", {
          method: "POST",
          body: JSON.stringify(ragBody()),
        }),
      );
    } catch (err) {
      setSearchError(errText(err));
    }
  }

  async function doAnswer() {
    setAnswerError(null);
    setAnswerResult(null);
    try {
      setAnswerResult(
        await client.request<RagAnswerResponse>("/rag/answer", {
          method: "POST",
          body: JSON.stringify(ragBody()),
        }),
      );
    } catch (err) {
      // Honest-off: no published answer agent (503), not enabled
      // (409), or the runtime's own 503 — shown verbatim. Retrieval
      // above still works; no answer was generated or recorded.
      setAnswerError(errText(err));
    }
  }

  return (
    <div>
      <p className="hint">
        Retrieval over governed knowledge documents and ingested chart
        chunks, with a citation for every result. Search never calls a
        model. Answering is a governed agent run whose output stays a
        draft for human disposition — when no model is configured the
        answer surface is honestly off, and this tab shows the
        server's reason instead of inventing one.
      </p>

      <h3>Retrieval status</h3>
      {status.loading ? (
        <Loading label="Loading RAG status…" />
      ) : status.error ? (
        <ErrorView error={status.error} />
      ) : status.data ? (
        <p className="hint" role="status">
          Store: <strong>{status.data.store}</strong> · chunks indexed:{" "}
          {status.data.chunks} · embedder: {status.data.embedder} (
          {status.data.embedder_kind}) — {status.data.embedder_note} ·
          pgvector {status.data.pgvector_available ? "available" : "not in use"} ·
          answer agent: {status.data.answer_agent}
        </p>
      ) : null}

      <h3>Knowledge documents</h3>
      <ConfigEditorShell<KnowledgeModel>
        client={client}
        kind={KNOWLEDGE_KIND}
        newKeyHint="e.g. sepsis-pathway-summary"
        parse={parseKnowledge}
        serialize={serializeKnowledge}
        empty={emptyKnowledge}
        validate={validateKnowledge}
      >
        {(model, setModel) => <KnowledgeEditor model={model} setModel={setModel} />}
      </ConfigEditorShell>

      <h3>Ingest</h3>
      <p>
        <button type="button" onClick={() => void ingestKnowledge()}>
          Ingest published knowledge documents
        </button>{" "}
        <label>
          Patient ID{" "}
          <input
            aria-label="Chart ingest patient ID"
            value={chartPatientId}
            onChange={(e) => setChartPatientId(e.target.value)}
            placeholder="e.g. pat-1"
          />
        </label>{" "}
        <button
          type="button"
          disabled={!chartPatientId.trim()}
          onClick={() => void ingestChart()}
        >
          Ingest patient chart
        </button>
      </p>
      {ingestError && (
        <p className="error-note" role="alert">
          {ingestError}
        </p>
      )}
      {ingestResult && (
        <div role="status">
          <p className="hint">
            Ingested {ingestResult.ingested_documents} documents as{" "}
            {ingestResult.chunks} chunks into store {ingestResult.store}{" "}
            (embedder: {ingestResult.embedders.join(", ")}).
          </p>
          {ingestResult.skipped.length > 0 && (
            <ul>
              {ingestResult.skipped.map((s) => (
                <li key={s.key}>
                  Skipped <strong>{s.key}</strong>: {s.reason}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {chartResult && (
        <p className="hint" role="status">
          {chartResult}
        </p>
      )}

      <h3>Search &amp; answer</h3>
      <p>
        <label>
          Patient reference{" "}
          <input
            aria-label="RAG patient reference"
            value={patientRef}
            onChange={(e) => setPatientRef(e.target.value)}
            placeholder="optional — Patient/…"
          />
        </label>
      </p>
      <p>
        <textarea
          aria-label="RAG query"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          rows={3}
          cols={72}
          placeholder="What should retrieval find?"
        />
      </p>
      <p>
        <button type="button" disabled={!query.trim()} onClick={() => void doSearch()}>
          Search (retrieval only)
        </button>{" "}
        <button type="button" disabled={!query.trim()} onClick={() => void doAnswer()}>
          Answer with citations
        </button>
      </p>

      {searchError && (
        <p className="error-note" role="alert">
          {searchError}
        </p>
      )}
      {searchResult && (
        <div>
          <h4>
            Search results — {searchResult.total} chunk
            {searchResult.total === 1 ? "" : "s"} (store {searchResult.store},
            embedder {searchResult.embedder})
          </h4>
          {searchResult.results.length === 0 ? (
            <Empty label="No chunks matched. Ingest documents or a chart above, or broaden the query." />
          ) : (
            <ol>
              {searchResult.results.map((r) => (
                <li key={r.chunk_id}>
                  <strong>{r.title}</strong>{" "}
                  <span className="hint">
                    ({r.sourceType}: {r.sourceId} · score {r.score})
                  </span>
                  <br />
                  {r.text}
                  <br />
                  <span className="hint">
                    Citation: {r.citation.title} — {r.citation.snippet}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}

      {answerError && (
        <p className="error-note" role="alert">
          {answerError}
        </p>
      )}
      {answerResult && (
        <div>
          <h4>Draft answer (pending human disposition)</h4>
          <pre>{answerResult.answer}</pre>
          <h5>Citations</h5>
          <CitationList citations={answerResult.citations} />
        </div>
      )}
    </div>
  );
}
