import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  type ApiClient,
  type IndicatorDefinitionEntry,
  type IndicatorRecordsResponse,
  type IndicatorValue,
} from "../api/client";
import type { Role } from "../api/types";
import {
  IndicatorDefinitionSummary,
  IndicatorRecordsSection,
  fetchIndicatorRecords,
  indicatorStatusTone,
} from "../components/DashboardView";
import { StatusBadge } from "../components/StatusBadge";
import { ModuleHeader } from "../components/ModuleHeader";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role?: Role;
  onOpenChart?: (patientId: string) => void;
}

// Phase 16 (WS-H / K2) — governed self-service analytics over the OMOP
// warehouse. Only allowlisted datasets and their real columns can be
// queried; raw SQL is refused by the server. Saved views are Studio
// config objects (kind `analytics_view`) on the ordinary draft →
// publish lifecycle — building one here authors a draft, and only a
// PUBLISHED view can be run by key. The vocabulary panel reports the
// ATHENA load state honestly: an empty load says so, with the reason.

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

interface Dataset {
  dataset: string;
  table: string;
  columns: Array<{ name: string; type: string }>;
}

interface Coverage {
  vocab_concept_total: number;
  vocab_relationship_total: number;
  by_vocabulary: Array<{ vocabulary_id: string; count: number }>;
  by_domain: Array<{ domain_id: string; count: number }>;
  curated_concept_map_count: number;
  concept_map_baseline: { table: string; count: number; note: string };
}

interface SavedView {
  key: string;
  version: number;
  status: string;
  payload: { title?: string; dataset?: string };
}

interface QueryResult {
  source: string;
  key: string | null;
  dataset: string;
  rows: Array<Record<string, unknown>>;
  row_count: number;
}

const AGGREGATIONS = ["count", "sum", "avg", "min", "max"];

export function AnalyticsPage({ client, onOpenChart }: Props) {
  const [tab, setTab] = useState("query");
  // Indicators browser (WS4b): published definitions from
  // GET /indicators/definitions ({items, total} of _out rows);
  // selecting one loads GET /indicators/{key}/value, and the records
  // drill loads GET /indicators/{key}/records via the client method.
  const definitions = useAsync<{ items: IndicatorDefinitionEntry[]; total: number }>(
    () => client.request("/indicators/definitions"),
    [client],
  );
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [selectedValue, setSelectedValue] = useState<IndicatorValue | null>(null);
  const [valueError, setValueError] = useState<string | null>(null);
  const [records, setRecords] = useState<IndicatorRecordsResponse | null>(null);
  const [recordsError, setRecordsError] = useState<string | null>(null);
  const [recordsBusy, setRecordsBusy] = useState(false);
  // Wave A item 9: the indicator detail renders ABOVE the list and is
  // scrolled into view + focused on selection — previously it rendered
  // after the full 24-row list, two viewports below the click, so
  // "View indicator" looked dead even though the value loaded fine.
  const detailCardRef = useRef<HTMLDivElement>(null);
  const detailHeadingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (!selectedKey) return;
    detailCardRef.current?.scrollIntoView?.({
      behavior: "smooth",
      block: "start",
    });
    detailHeadingRef.current?.focus({ preventScroll: true });
  }, [selectedKey]);

  const publishedIndicators = (definitions.data?.items ?? []).filter(
    (item) => item.status === "published",
  );
  const selectedDefinition = publishedIndicators.find(
    (item) => item.key === selectedKey,
  );

  async function selectIndicator(key: string) {
    setSelectedKey(key);
    setSelectedValue(null);
    setValueError(null);
    setRecords(null);
    setRecordsError(null);
    try {
      setSelectedValue(
        await client.request<IndicatorValue>(
          `/indicators/${encodeURIComponent(key)}/value`,
        ),
      );
    } catch (err) {
      setValueError(errorText(err));
    }
  }

  async function loadRecords() {
    if (!selectedKey) return;
    setRecordsBusy(true);
    setRecordsError(null);
    try {
      setRecords(await fetchIndicatorRecords(client, selectedKey));
    } catch (err) {
      setRecordsError(errorText(err));
    } finally {
      setRecordsBusy(false);
    }
  }
  const datasets = useAsync<{ items: Dataset[] }>(
    () => client.request("/analytics/datasets"),
    [client],
  );
  const coverage = useAsync<Coverage>(
    () => client.request("/analytics/vocabulary/coverage"),
    [client],
  );
  const views = useAsync<SavedView[]>(
    () =>
      client
        .request<SavedView[]>("/studio/config/analytics_view/effective")
        .catch(() => [] as SavedView[]),
    [client],
  );

  const [dataset, setDataset] = useState("");
  const [fields, setFields] = useState<string[]>([]);
  const [groupBy, setGroupBy] = useState("");
  const [aggFn, setAggFn] = useState("count");
  const [aggField, setAggField] = useState("");
  const [viewKey, setViewKey] = useState("");
  const [viewTitle, setViewTitle] = useState("");
  const [result, setResult] = useState<QueryResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [draftVersion, setDraftVersion] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  const selectedDataset = datasets.data?.items.find((d) => d.dataset === dataset);
  const columns = selectedDataset?.columns ?? [];

  function definition(): Record<string, unknown> {
    const def: Record<string, unknown> = {
      title: viewTitle || viewKey || "Ad-hoc view",
      dataset,
      fields,
    };
    if (groupBy) def.groupBy = [groupBy];
    def.aggregation = aggFn === "count" ? { fn: "count" } : { fn: aggFn, field: aggField };
    return def;
  }

  async function run(body: Record<string, unknown>) {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await client.request<QueryResult>("/analytics/query", {
        method: "POST",
        body: JSON.stringify(body),
      }));
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function saveDraft() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const created = await client.request<{ version: number }>(
        `/studio/config/analytics_view/${encodeURIComponent(viewKey)}`,
        { method: "POST", body: JSON.stringify(definition()) },
      );
      setDraftVersion(created.version);
      setNotice(
        `Draft v${created.version} of analytics view “${viewKey}” saved in System Studio. It cannot be run by key until it is published.`,
      );
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function publishDraft() {
    if (draftVersion == null) return;
    setBusy(true);
    setError(null);
    try {
      await client.request(
        `/studio/config/analytics_view/${encodeURIComponent(viewKey)}/versions/${draftVersion}/publish`,
        { method: "POST" },
      );
      setDraftVersion(null);
      setNotice(`Analytics view “${viewKey}” published — it can now be run by key.`);
      views.reload();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  const resultColumns = result && result.rows.length ? Object.keys(result.rows[0]) : [];

  const queryPanel = (
    <>
      <div className="card">
        <h3>Vocabulary status (ATHENA / OMOP)</h3>
        {coverage.loading && <Loading label="Loading vocabulary coverage…" />}
        {coverage.error != null && <ErrorView error={coverage.error} />}
        {coverage.data && (
          <>
            <p role="status">
              Concepts loaded: <strong>{coverage.data.vocab_concept_total}</strong> ·
              Relationships loaded: <strong>{coverage.data.vocab_relationship_total}</strong> ·
              Curated concept-map baseline:{" "}
              <strong>{coverage.data.curated_concept_map_count}</strong>
            </p>
            {coverage.data.vocab_concept_total === 0 ? (
              <p role="status">
                <StatusBadge tone="warn">no ATHENA bundle loaded</StatusBadge> The vocabulary
                tables are empty because no ATHENA export directory is published in{" "}
                <code>integration/vocabulary</code> — the loader loads nothing rather than
                fabricating rows. Queries still run against the curated concept map baseline (
                {coverage.data.concept_map_baseline.count} concepts).
              </p>
            ) : (
              <>
                <ul className="result-list">
                  {coverage.data.by_vocabulary.map((v) => (
                    <li key={v.vocabulary_id} className="result-row">
                      <span className="result-type">{v.vocabulary_id}</span>
                      <span className="result-meta">{v.count} concepts</span>
                    </li>
                  ))}
                </ul>
                <ul className="result-list">
                  {coverage.data.by_domain.map((d) => (
                    <li key={d.domain_id} className="result-row">
                      <span className="result-type">{d.domain_id}</span>
                      <span className="result-meta">{d.count} concepts</span>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </>
        )}
      </div>

      <div className="card">
        <h3>Published views</h3>
        {views.loading && <Loading label="Loading published views…" />}
        {views.data && views.data.length === 0 && (
          <Empty label="No published analytics views yet. Build one below and publish its draft." />
        )}
        <ul className="result-list">
          {views.data?.map((v) => (
            <li key={v.key} className="result-row">
              <span className="result-type">{v.payload.title ?? v.key}</span>
              <span className="result-meta">
                key {v.key} · dataset {v.payload.dataset ?? "?"} · v{v.version}
              </span>
              <button type="button" disabled={busy} onClick={() => void run({ key: v.key })}>
                Run view
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div className="card">
        <h3>View builder</h3>
        {datasets.loading && <Loading label="Loading dataset allowlist…" />}
        {datasets.error != null && <ErrorView error={datasets.error} />}
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void run({ definition: definition() });
          }}
        >
          <label>
            Dataset (allowlist)
            <select
              value={dataset}
              onChange={(e) => {
                setDataset(e.target.value);
                setFields([]);
                setGroupBy("");
                setAggField("");
              }}
            >
              <option value="">Select a dataset…</option>
              {datasets.data?.items.map((d) => (
                <option key={d.dataset} value={d.dataset}>
                  {d.dataset} ({d.table})
                </option>
              ))}
            </select>
          </label>
          <fieldset>
            <legend>Fields</legend>
            {columns.length === 0 && (
              <p className="hint">Select a dataset to choose from its real columns.</p>
            )}
            {columns.map((c) => (
              <label key={c.name}>
                <input
                  type="checkbox"
                  checked={fields.includes(c.name)}
                  onChange={(e) =>
                    setFields(
                      e.target.checked ? [...fields, c.name] : fields.filter((f) => f !== c.name),
                    )
                  }
                />{" "}
                {c.name} <span className="hint">({c.type})</span>
              </label>
            ))}
          </fieldset>
          <label>
            Group by
            <select value={groupBy} onChange={(e) => setGroupBy(e.target.value)} disabled={!dataset}>
              <option value="">No grouping</option>
              {columns.map((c) => (
                <option key={c.name} value={c.name}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Aggregation
            <select value={aggFn} onChange={(e) => setAggFn(e.target.value)}>
              {AGGREGATIONS.map((fn) => (
                <option key={fn} value={fn}>
                  {fn}
                </option>
              ))}
            </select>
          </label>
          {aggFn !== "count" && (
            <label>
              Aggregation field
              <select value={aggField} onChange={(e) => setAggField(e.target.value)} disabled={!dataset}>
                <option value="">Select a column…</option>
                {columns.map((c) => (
                  <option key={c.name} value={c.name}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <button type="submit" disabled={busy || !dataset || fields.length === 0}>
            Run ad-hoc
          </button>
        </form>
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void saveDraft();
          }}
        >
          <h4>Save as a governed view</h4>
          <label>
            View key
            <input value={viewKey} onChange={(e) => setViewKey(e.target.value)} required />
          </label>
          <label>
            Title
            <input value={viewTitle} onChange={(e) => setViewTitle(e.target.value)} />
          </label>
          <button type="submit" disabled={busy || !dataset || !viewKey || fields.length === 0}>
            Save draft in Studio
          </button>{" "}
          {draftVersion != null && (
            <button type="button" disabled={busy} onClick={() => void publishDraft()}>
              Publish draft v{draftVersion}
            </button>
          )}
        </form>
        {notice && <p role="status">{notice}</p>}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </div>

      {result && (
        <div className="card">
          <h3>
            Results — {result.dataset}{" "}
            <StatusBadge tone="info">{result.source}</StatusBadge>
          </h3>
          <p role="status">{result.row_count} row(s)</p>
          {result.rows.length === 0 ? (
            <Empty label="The view ran and returned no rows." />
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  {resultColumns.map((c) => (
                    <th key={c}>{c}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {result.rows.map((row, i) => (
                  <tr key={i}>
                    {resultColumns.map((c) => (
                      <td key={c}>
                        {row[c] === null || row[c] === undefined ? "—" : String(row[c])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </>
  );

  const indicatorsPanel = (
    <>
      {selectedDefinition && (
        <div className="card indicator-detail" ref={detailCardRef}>
          <h3 ref={detailHeadingRef} tabIndex={-1}>
            {selectedDefinition.payload.title ?? selectedDefinition.payload.name}
          </h3>
          {valueError && (
            <p className="form-error" role="alert">
              {valueError}
            </p>
          )}
          {!selectedValue && !valueError && (
            <Loading label="Loading indicator value…" />
          )}
          {selectedValue && (
            <>
              <div className="dashboard-widget-value dashboard-widget-big">
                {selectedValue.value == null ? "—" : selectedValue.value}
                {selectedValue.unit ? (
                  <span className="gauge-unit"> {selectedValue.unit}</span>
                ) : null}{" "}
                <StatusBadge tone={indicatorStatusTone(selectedValue.status)}>
                  {selectedValue.status}
                </StatusBadge>
              </div>
              {selectedValue.target != null && (
                <p className="hint">Target: {selectedValue.target}</p>
              )}
              <p className="hint">Evaluated at {selectedValue.evaluated_at}</p>
            </>
          )}
          <h4>Definition</h4>
          <IndicatorDefinitionSummary definition={selectedDefinition.payload} />
          <details>
            <summary>Raw definition (JSON)</summary>
            <pre>{JSON.stringify(selectedDefinition.payload, null, 2)}</pre>
          </details>
          <button
            type="button"
            disabled={recordsBusy}
            onClick={() => void loadRecords()}
          >
            View underlying records
          </button>
          {recordsError && (
            <p className="form-error" role="alert">
              {recordsError}
            </p>
          )}
          {recordsBusy && <Loading label="Loading underlying records…" />}
          {records && (
            <IndicatorRecordsSection data={records} onOpenChart={onOpenChart} />
          )}
        </div>
      )}

      <div className="card">
        <h3>Published indicators</h3>
        <p className="hint">
          Every reported number drills down: pick an indicator to see its value and its
          published definition, then view the underlying records behind the number.
        </p>
        {definitions.loading && <Loading label="Loading published indicators…" />}
        {definitions.error != null && <ErrorView error={definitions.error} />}
        {definitions.data && publishedIndicators.length === 0 && (
          <Empty label="No published indicators yet. Publish an indicator definition in System Studio." />
        )}
        <ul className="result-list">
          {publishedIndicators.map((item) => (
            <li
              key={item.key}
              className={
                selectedKey === item.key
                  ? "result-row result-row-selected"
                  : "result-row"
              }
            >
              <span className="result-type">
                {item.payload.title ?? item.payload.name}
              </span>
              <span className="result-meta">
                key {item.key} · source{" "}
                {item.payload.source?.type === "named"
                  ? item.payload.source.name
                  : item.payload.source?.type === "measure"
                    ? item.payload.source.measure_key
                    : item.payload.source?.cohort_key}{" "}
                · v{item.version}
              </span>
              <button
                type="button"
                aria-pressed={selectedKey === item.key}
                onClick={() => void selectIndicator(item.key)}
              >
                View indicator
              </button>
            </li>
          ))}
        </ul>
      </div>
    </>
  );

  return (
    <section>
      <ModuleHeader icon="graph" group="Insights" title="Analytics" purpose="Governed measures, views and indicators" />
      <p className="hint">
        Governed ad-hoc analytics over the OMOP warehouse. Datasets and columns come from the
        server allowlist — there is no raw-SQL box, by design. Saved views are Studio{" "}
        <code>analytics_view</code> config: draft → publish, then run.
      </p>
      <Tabs
        tabs={[
          { key: "query", label: "Governed query" },
          { key: "indicators", label: "Indicators" },
        ]}
        activeKey={tab}
        onChange={setTab}
        ariaLabel="Analytics sections"
        panels={{ query: queryPanel, indicators: indicatorsPanel }}
      />
    </section>
  );
}
