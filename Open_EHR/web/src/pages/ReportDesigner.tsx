import { useEffect, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import {
  createReportDraft,
  dashboardIndicators,
  downloadReportRun,
  FALLBACK_FILTER_OPS,
  FALLBACK_FORMATS,
  getDesignerDataset,
  getReportDefinition,
  listDesignerDatasets,
  runReportPreview,
  updateReportDraft,
  validateReportDefinition,
  type DashboardIndicatorsTrace,
  type DesignerDatasetDetail,
  type DesignerDatasetsResponse,
  type ReportDefinitionObject,
  type ReportDefinitionPayload,
  type ReportRunResult,
} from "../api/designer";
import { saveDownloadedFile } from "../api/download";
import { StatusBadge } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

/** Visual report designer (Phase 15, BD-32) — a System Studio panel.
 *
 *  This is an assistant-built, designer-draft-only surface (Min's
 *  standing rule): definitions composed here are saved as governed
 *  DRAFTS of kind `report-definition`; a human publishes in Studio
 *  before the report runs for others, and the run preview below only
 *  ever executes the PUBLISHED version.
 *
 *  Composition is dataset + field picks + filter rows + grouping over
 *  the server-defined datasets from GET /designer/datasets. There is
 *  deliberately NO free-text SQL field anywhere in this panel — the
 *  report engine has no SQL path and refuses one with 422.
 *
 *  Below the designer, the BD-30 indicator trace (GET /platform-depth/
 *  dashboard-indicators) makes the dashboard rewiring visible: which
 *  widgets resolve through a published indicator, and which stay on a
 *  live endpoint, with the server's reason. */

interface FilterRow {
  field: string;
  op: string;
  value: string;
}

interface PanelData {
  datasets: DesignerDatasetsResponse;
  trace: DashboardIndicatorsTrace;
}

function errorDetail(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  return err instanceof Error ? err.message : String(err);
}

/** Coerce one filter-row text value into the payload value the engine
 *  expects: `in` takes a comma-separated list; number/boolean fields
 *  (per the palette's inferred types) take real numbers/booleans. */
function coerceValue(raw: string, op: string, type: string | undefined): unknown {
  if (op === "in") {
    return raw
      .split(",")
      .map((part) => part.trim())
      .filter((part) => part !== "")
      .map((part) => coerceValue(part, "eq", type));
  }
  if (type === "number") {
    const n = Number(raw);
    return raw.trim() !== "" && !Number.isNaN(n) ? n : raw;
  }
  if (type === "boolean") {
    if (raw === "true") return true;
    if (raw === "false") return false;
  }
  return raw;
}

export function ReportDesignerPanel({ client }: { client: ApiClient }) {
  const { data, loading, error } = useAsync<PanelData>(async () => {
    const [datasets, trace] = await Promise.all([
      listDesignerDatasets(client),
      dashboardIndicators(client),
    ]);
    return { datasets, trace };
  }, [client]);

  const [datasetKey, setDatasetKey] = useState("");
  const [detail, setDetail] = useState<DesignerDatasetDetail | null>(null);
  const [title, setTitle] = useState("");
  const [reportKey, setReportKey] = useState("");
  const [selectedFields, setSelectedFields] = useState<string[]>([]);
  const [grouping, setGrouping] = useState<string[]>([]);
  const [filters, setFilters] = useState<FilterRow[]>([]);
  const [formats, setFormats] = useState<string[]>([...FALLBACK_FORMATS]);
  const [busy, setBusy] = useState<string | null>(null);
  const [validation, setValidation] = useState<{ ok: boolean; text: string } | null>(null);
  const [savedNote, setSavedNote] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ key: string; version: number } | null>(null);
  const [savedPayload, setSavedPayload] = useState<string | null>(null);
  const [runResult, setRunResult] = useState<ReportRunResult | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [runNote, setRunNote] = useState<string | null>(null);

  const datasets = data?.datasets.items ?? [];

  // Auto-select the first dataset once the palette loads.
  useEffect(() => {
    if (!datasetKey && datasets.length > 0) {
      setDatasetKey(datasets[0].key);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  // Load the selected dataset's schema (filter ops + formats) and
  // reset the composition: all fields picked, no filters/grouping.
  useEffect(() => {
    if (!datasetKey) return;
    let cancelled = false;
    getDesignerDataset(client, datasetKey)
      .then((d) => {
        if (cancelled) return;
        setDetail(d);
        setSelectedFields(d.fields.map((f) => f.name));
        setGrouping([]);
        setFilters([]);
        setFormats(d.formats.length > 0 ? d.formats : [...FALLBACK_FORMATS]);
      })
      .catch(() => {
        if (!cancelled) setDetail(null);
      });
    return () => {
      cancelled = true;
    };
  }, [client, datasetKey]);

  if (loading) return <Loading label="Loading report designer…" />;
  if (error) return <ErrorView error={error} />;
  if (!data) return <Empty label="No designer data." />;

  const summary = datasets.find((d) => d.key === datasetKey);
  const paletteFields = summary?.fields ?? detail?.fields ?? [];
  const fieldTypes = new Map(paletteFields.map((f) => [f.name, f.type]));
  const filterOps = detail?.filter_ops ?? FALLBACK_FILTER_OPS;
  const availableFormats = detail?.formats ?? FALLBACK_FORMATS;

  function buildDefinition(): ReportDefinitionPayload {
    return {
      title: title.trim(),
      dataset: datasetKey,
      // Dataset field order, filtered to the picks — deterministic.
      fields: paletteFields.map((f) => f.name).filter((n) => selectedFields.includes(n)),
      filters: filters.map((row) => ({
        field: row.field,
        op: row.op,
        value: coerceValue(row.value, row.op, fieldTypes.get(row.field)),
      })),
      grouping: [...grouping],
      formats: [...formats],
    };
  }

  /** Persist the current composition as the governed draft, adopting
   *  an existing draft for the key when the server reports one (409
   *  from create) by updating that draft version instead. */
  async function persistDraft(
    definition: ReportDefinitionPayload,
  ): Promise<ReportDefinitionObject> {
    const key = reportKey.trim();
    if (draft && draft.key === key) {
      return updateReportDraft(client, key, draft.version, definition);
    }
    try {
      return await createReportDraft(client, key, definition);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        const history = await getReportDefinition(client, key);
        const existing = history.versions.find((v) => v.status === "draft");
        if (existing) {
          setDraft({ key, version: existing.version });
          return updateReportDraft(client, key, existing.version, definition);
        }
      }
      throw err;
    }
  }

  async function onValidate() {
    setBusy("validate");
    setValidation(null);
    const definition = buildDefinition();
    try {
      const result = await validateReportDefinition(
        client,
        reportKey.trim(),
        definition,
        draft && draft.key === reportKey.trim() ? draft.version : null,
      ).catch(async (err) => {
        // A 409 from create means the server validated the definition
        // (validation runs before the draft-exists check in
        // app.report_engine.create_definition) but a draft already
        // exists for this key: adopt it and validate via its update.
        if (err instanceof ApiError && err.status === 409 && !draft) {
          const history = await getReportDefinition(client, reportKey.trim());
          const existing = history.versions.find((v) => v.status === "draft");
          if (existing) {
            setDraft({ key: reportKey.trim(), version: existing.version });
            return updateReportDraft(client, reportKey.trim(), existing.version, definition);
          }
        }
        throw err;
      });
      setDraft({ key: result.key, version: result.version });
      setSavedPayload(JSON.stringify(definition));
      setValidation({
        ok: true,
        text: `Server validation passed — the definition is valid (stored as governed draft ${result.key} v${result.version}).`,
      });
    } catch (err) {
      setValidation({ ok: false, text: errorDetail(err) });
    } finally {
      setBusy(null);
    }
  }

  async function onSaveDraft() {
    setBusy("save");
    setSavedNote(null);
    const definition = buildDefinition();
    try {
      if (
        draft &&
        draft.key === reportKey.trim() &&
        savedPayload === JSON.stringify(definition)
      ) {
        setSavedNote(
          `Draft ${draft.key} v${draft.version} is already saved — no changes since.`,
        );
        return;
      }
      const result = await persistDraft(definition);
      setDraft({ key: result.key, version: result.version });
      setSavedPayload(JSON.stringify(definition));
      setSavedNote(
        `Draft saved: report-definition/${result.key} v${result.version} (status: ${result.status}).`,
      );
    } catch (err) {
      setSavedNote(null);
      setValidation({ ok: false, text: errorDetail(err) });
    } finally {
      setBusy(null);
    }
  }

  async function onRunPreview() {
    setBusy("run");
    setRunError(null);
    setRunNote(null);
    try {
      const result = await runReportPreview(client, reportKey.trim());
      setRunResult(result);
    } catch (err) {
      setRunResult(null);
      setRunError(errorDetail(err));
    } finally {
      setBusy(null);
    }
  }

  async function onDownload(format: "csv" | "pdf") {
    setBusy(`download-${format}`);
    setRunError(null);
    setRunNote(null);
    try {
      const file = await downloadReportRun(client, reportKey.trim(), format);
      saveDownloadedFile(file);
      setRunNote(
        `${format.toUpperCase()} downloaded (${file.filename}` +
          `${file.rows != null ? `, ${file.rows} rows` : ""}).`,
      );
    } catch (err) {
      setRunError(errorDetail(err));
    } finally {
      setBusy(null);
    }
  }

  function toggle(list: string[], setList: (next: string[]) => void, value: string) {
    setList(list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);
  }

  const trace = data.trace;

  return (
    <section aria-label="Report designer">
      <h3>Report designer</h3>
      <div className="state state-empty" role="note">
        <strong>Draft only</strong> — a human publishes in Studio before this report
        runs for others. Definitions composed here are saved as governed drafts
        (kind <code>report-definition</code>); publishing is a separate human step,
        and the run preview below only ever runs a published definition.
      </div>
      <p className="hint">
        Compose from server-defined datasets only — pick a dataset, pick its fields,
        add filter rows and grouping. There is no SQL field in this designer: the
        report engine has no SQL path and the server refuses one.
      </p>

      <div style={{ display: "flex", gap: "1.5rem", alignItems: "flex-start", flexWrap: "wrap" }}>
        {/* Left: dataset palette */}
        <div style={{ minWidth: "220px", flex: "0 1 260px" }}>
          <h4>Datasets</h4>
          {datasets.length === 0 ? (
            <Empty label="No datasets are defined on the server." />
          ) : (
            <ul>
              {datasets.map((d) => (
                <li key={d.key}>
                  <button
                    type="button"
                    aria-pressed={d.key === datasetKey}
                    onClick={() => setDatasetKey(d.key)}
                  >
                    {d.title}
                  </button>{" "}
                  <span className="hint">
                    {d.key} — {d.field_count} fields
                  </span>
                  {d.description ? <p className="hint">{d.description}</p> : null}
                </li>
              ))}
            </ul>
          )}
          {summary ? (
            <>
              <h4>Fields — {summary.title}</h4>
              <ul>
                {paletteFields.map((f) => (
                  <li key={f.name}>
                    <label>
                      <input
                        type="checkbox"
                        aria-label={`Field ${f.name}`}
                        checked={selectedFields.includes(f.name)}
                        onChange={() => toggle(selectedFields, setSelectedFields, f.name)}
                      />{" "}
                      {f.name}{" "}
                      <StatusBadge tone="neutral">{f.type}</StatusBadge>
                    </label>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </div>

        {/* Centre: definition composer */}
        <div style={{ flex: "1 1 420px", minWidth: "300px" }}>
          <h4>Definition</h4>
          <div className="inline-form">
            <label>
              Report title{" "}
              <input
                aria-label="Report title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="e.g. Patients by gender"
              />
            </label>
            <label>
              Report key{" "}
              <input
                aria-label="Report key"
                value={reportKey}
                onChange={(e) => setReportKey(e.target.value)}
                placeholder="e.g. patients-by-gender"
              />
            </label>
            <label>
              Dataset{" "}
              <select
                aria-label="Dataset"
                value={datasetKey}
                onChange={(e) => setDatasetKey(e.target.value)}
              >
                {datasets.map((d) => (
                  <option key={d.key} value={d.key}>
                    {d.title}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <p className="hint">
            Selected fields:{" "}
            {buildDefinition().fields.length > 0
              ? buildDefinition().fields.join(", ")
              : "none — pick at least one field in the palette."}
          </p>

          <h4>Filters</h4>
          {filters.length === 0 ? (
            <p className="hint">No filters — the report covers every row of the dataset.</p>
          ) : (
            filters.map((row, i) => (
              <div key={i} className="inline-form">
                <label>
                  Field{" "}
                  <select
                    aria-label={`Filter ${i + 1} field`}
                    value={row.field}
                    onChange={(e) =>
                      setFilters(filters.map((r, j) => (j === i ? { ...r, field: e.target.value } : r)))
                    }
                  >
                    {paletteFields.map((f) => (
                      <option key={f.name} value={f.name}>
                        {f.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Operator{" "}
                  <select
                    aria-label={`Filter ${i + 1} operator`}
                    value={row.op}
                    onChange={(e) =>
                      setFilters(filters.map((r, j) => (j === i ? { ...r, op: e.target.value } : r)))
                    }
                  >
                    {filterOps.map((op) => (
                      <option key={op} value={op}>
                        {op}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Value{" "}
                  <input
                    aria-label={`Filter ${i + 1} value`}
                    value={row.value}
                    placeholder={row.op === "in" ? "comma-separated values" : "value"}
                    onChange={(e) =>
                      setFilters(filters.map((r, j) => (j === i ? { ...r, value: e.target.value } : r)))
                    }
                  />
                </label>
                <button
                  type="button"
                  aria-label={`Remove filter ${i + 1}`}
                  onClick={() => setFilters(filters.filter((_, j) => j !== i))}
                >
                  Remove
                </button>
              </div>
            ))
          )}
          <p>
            <button
              type="button"
              disabled={paletteFields.length === 0}
              onClick={() =>
                setFilters([
                  ...filters,
                  { field: paletteFields[0]?.name ?? "", op: filterOps[0] ?? "eq", value: "" },
                ])
              }
            >
              Add filter
            </button>
          </p>

          <h4>Grouping</h4>
          <div className="inline-form">
            {paletteFields.map((f) => (
              <label key={f.name}>
                <input
                  type="checkbox"
                  aria-label={`Group by ${f.name}`}
                  checked={grouping.includes(f.name)}
                  onChange={() => toggle(grouping, setGrouping, f.name)}
                />{" "}
                {f.name}
              </label>
            ))}
          </div>

          <h4>Formats</h4>
          <div className="inline-form">
            {availableFormats.map((f) => (
              <label key={f}>
                <input
                  type="checkbox"
                  aria-label={`Format ${f}`}
                  checked={formats.includes(f)}
                  onChange={() => toggle(formats, setFormats, f)}
                />{" "}
                {f}
              </label>
            ))}
          </div>

          <p>
            <button
              type="button"
              disabled={busy !== null || !reportKey.trim()}
              onClick={onValidate}
            >
              Validate
            </button>{" "}
            <button
              type="button"
              disabled={busy !== null || !reportKey.trim()}
              onClick={onSaveDraft}
            >
              Save as draft
            </button>
          </p>
          <p className="hint">
            The server validates every definition on the draft endpoint (there is no
            separate validation service and no SQL path): Validate runs that server
            validation and shows its result verbatim; a valid definition is kept as
            the governed draft.
          </p>
          {validation ? (
            validation.ok ? (
              <p className="state state-empty" role="status">
                {validation.text}
              </p>
            ) : (
              <div className="state state-error" role="alert">
                <strong>Server validation failed.</strong> {validation.text}
              </div>
            )
          ) : null}
          {savedNote ? (
            <p className="state state-empty" role="status">
              {savedNote}
            </p>
          ) : null}

          <h4>Run preview</h4>
          <p className="hint">
            Runs the <strong>published</strong> definition for the report key above —
            drafts do not run. A definition your draft has not been published under
            yet returns the server&apos;s “no published report definition” message.
          </p>
          <p>
            <button
              type="button"
              disabled={busy !== null || !reportKey.trim()}
              onClick={onRunPreview}
            >
              Run preview
            </button>{" "}
            <button
              type="button"
              disabled={busy !== null || !reportKey.trim()}
              onClick={() => onDownload("csv")}
            >
              Download CSV
            </button>{" "}
            <button
              type="button"
              disabled={busy !== null || !reportKey.trim()}
              onClick={() => onDownload("pdf")}
            >
              Download PDF
            </button>
          </p>
          {runNote ? (
            <p className="state state-empty" role="status">
              {runNote}
            </p>
          ) : null}
          {runError ? (
            <div className="state state-error" role="alert">
              <strong>Run failed.</strong> {runError}
            </div>
          ) : null}
          {runResult ? (
            <div>
              <p>
                <strong>{runResult.title}</strong> — {runResult.row_count} rows
                (dataset: {runResult.dataset}, published v{runResult.version})
                {runResult.groups
                  ? `, grouped into ${runResult.groups.length} groups`
                  : ""}
                .
              </p>
              {runResult.rows.length === 0 ? (
                <Empty label="The published definition returned no rows." />
              ) : (
                <table className="studio-table">
                  <thead>
                    <tr>
                      {runResult.fields.map((f) => (
                        <th key={f}>{f}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {runResult.rows.map((row, i) => (
                      <tr key={i}>
                        {runResult.fields.map((f) => (
                          <td key={f}>{row[f] == null ? "—" : String(row[f])}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          ) : null}
        </div>
      </div>

      {/* Below: BD-30 indicator trace */}
      <h4>Dashboard indicator trace</h4>
      <p className="hint">
        BD-30: which dashboard widgets resolve through the published indicator
        library (one source of truth for the KPI) and which stay on a live
        endpoint — with the server&apos;s reason where a widget cannot honestly
        be an indicator. {trace.widgets_on_indicators} of {trace.widgets_total}{" "}
        widgets resolve through published indicators across {trace.total}{" "}
        dashboards.
      </p>
      {trace.dashboards.length === 0 ? (
        <Empty label="No published dashboards to trace." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Dashboard</th>
              <th>Widget</th>
              <th>Source</th>
              <th>Endpoint</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {trace.dashboards.flatMap((dash) =>
              dash.widgets.map((w) => (
                <tr key={`${dash.key}/${w.id ?? w.title ?? ""}`}>
                  <td>
                    {dash.title ?? dash.key}{" "}
                    <span className="hint">
                      ({dash.key} v{dash.version})
                    </span>
                  </td>
                  <td>{w.title ?? w.id ?? "—"}</td>
                  <td>
                    {w.disposition === "indicator" ? (
                      <StatusBadge tone="ok">Indicator: {w.indicator}</StatusBadge>
                    ) : (
                      <StatusBadge tone="neutral">Live endpoint</StatusBadge>
                    )}
                  </td>
                  <td>{w.endpoint ?? "—"}</td>
                  <td>
                    {w.disposition === "indicator"
                      ? `Resolves through /indicators/${w.indicator}/value` +
                        (w.applied ? " (applied in the published dashboard)." : " (binding known; rewiring not yet applied).")
                      : (w.reason ?? "—")}
                  </td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      )}
    </section>
  );
}
