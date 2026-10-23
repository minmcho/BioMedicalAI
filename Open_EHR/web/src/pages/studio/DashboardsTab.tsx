import { useState } from "react";
import type { ApiClient } from "../../api/client";
import { ApiError } from "../../api/client";
import type { StudioConfigObject } from "../../api/types";
import { DashboardView } from "../../components/DashboardView";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

/** Dashboards tab — dashboards are governed config (kind=dashboard,
 *  Phase 10 Track B). A dashboard is a title plus widgets that
 *  reference EXISTING read endpoints; the shared DashboardView renders
 *  any published dashboard config (Phase 13 added the chart widget
 *  types: line, bar, gauge, table, big-number) — a new dashboard is a
 *  config publish, not a code change. A widget whose endpoint fails
 *  shows as unavailable; the rest of the dashboard still renders.
 *
 *  Studio parity row 39: alongside the render view, an "Author
 *  dashboard" mode composes a dashboard through structured widget
 *  rows and saves it through the same governed config API the
 *  ConfigEditorShell uses (createConfigDraft + publishConfig,
 *  kind=dashboard). The widget vocabulary is exactly
 *  configstore.validate_dashboard's DASHBOARD_DISPLAYS —
 *  ("total", "count", "value", "big-number", "line", "bar", "gauge",
 *  "table") — with the per-display fields the validator requires:
 *  `field` for value/big-number/gauge, `series` + `labelField` for
 *  line/bar, `columns` for table, `max`/`thresholds`/`unit` for gauge
 *  (unit for any scalar display). */

export const DASHBOARD_DISPLAYS = [
  "total",
  "count",
  "value",
  "big-number",
  "line",
  "bar",
  "gauge",
  "table",
] as const;

type Display = (typeof DASHBOARD_DISPLAYS)[number];

interface WidgetRow {
  id: string;
  title: string;
  endpoint: string;
  display: Display;
  field: string;
  unit: string;
  labelField: string;
  seriesField: string;
  seriesLabel: string;
  columnsText: string;
  max: string;
  warn: string;
  critical: string;
}

function emptyWidget(): WidgetRow {
  return {
    id: "", title: "", endpoint: "", display: "total", field: "", unit: "",
    labelField: "", seriesField: "", seriesLabel: "", columnsText: "",
    max: "", warn: "", critical: "",
  };
}

function numOrUndef(text: string): number | undefined {
  if (text.trim() === "") return undefined;
  const n = Number(text);
  return Number.isNaN(n) ? undefined : n;
}

/** Compose one validator-shaped widget object from a row (only the
 *  fields the display actually uses are emitted). Returns an error
 *  string instead when a validator-required field is missing. */
function composeWidget(row: WidgetRow, index: number): Record<string, unknown> | string {
  const label = `Widget ${index + 1}`;
  const widget: Record<string, unknown> = {
    id: row.id.trim() || `widget-${index + 1}`,
    title: row.title.trim(),
    endpoint: row.endpoint.trim(),
    display: row.display,
  };
  if (!widget.title) return `${label}: a title is required.`;
  if (!String(widget.endpoint).startsWith("/")) {
    return `${label}: endpoint must be an API path starting with '/'.`;
  }
  if (["value", "big-number", "gauge"].includes(row.display)) {
    if (!row.field.trim()) return `${label}: display '${row.display}' requires a field.`;
    widget.field = row.field.trim();
  } else if (row.field.trim()) {
    widget.field = row.field.trim();
  }
  if (row.unit.trim()) widget.unit = row.unit.trim();
  if (row.display === "line" || row.display === "bar") {
    if (!row.seriesField.trim()) {
      return `${label}: display '${row.display}' requires a series field.`;
    }
    if (!row.labelField.trim()) {
      return `${label}: display '${row.display}' requires a label field.`;
    }
    widget.series = [
      {
        field: row.seriesField.trim(),
        ...(row.seriesLabel.trim() ? { label: row.seriesLabel.trim() } : {}),
      },
    ];
    widget.labelField = row.labelField.trim();
  }
  if (row.display === "gauge") {
    const max = numOrUndef(row.max);
    if (row.max.trim() !== "" && (max === undefined || max <= 0)) {
      return `${label}: gauge max must be a positive number.`;
    }
    if (max !== undefined) widget.max = max;
    const thresholds: Record<string, number> = {};
    const warn = numOrUndef(row.warn);
    const critical = numOrUndef(row.critical);
    if (warn !== undefined) thresholds.warn = warn;
    if (critical !== undefined) thresholds.critical = critical;
    if (Object.keys(thresholds).length > 0) widget.thresholds = thresholds;
  }
  if (row.display === "table") {
    const columns = row.columnsText
      .split(",")
      .map((c) => c.trim())
      .filter(Boolean)
      .map((c) => {
        const [field, ...rest] = c.split(":");
        return { field: field.trim(), title: rest.join(":").trim() || field.trim() };
      });
    if (columns.length === 0) {
      return `${label}: display 'table' requires at least one column (field:Title, comma-separated).`;
    }
    widget.columns = columns;
  }
  return widget;
}

export function DashboardAuthor({ client }: { client: ApiClient }) {
  const [key, setKey] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [widgets, setWidgets] = useState<WidgetRow[]>([emptyWidget()]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const setWidget = (index: number, patch: Partial<WidgetRow>) =>
    setWidgets((rows) => rows.map((r, i) => (i === index ? { ...r, ...patch } : r)));

  function buildPayload(): Record<string, unknown> | string {
    if (!key.trim()) return "A dashboard key is required.";
    if (!title.trim()) return "A dashboard title is required.";
    if (widgets.length === 0) return "Add at least one widget.";
    const composed: Array<Record<string, unknown>> = [];
    for (let i = 0; i < widgets.length; i++) {
      const w = composeWidget(widgets[i], i);
      if (typeof w === "string") return w;
      composed.push(w);
    }
    const ids = composed.map((w) => w.id);
    if (new Set(ids).size !== ids.length) return "Widget ids must be unique.";
    return {
      title: title.trim(),
      ...(description.trim() ? { description: description.trim() } : {}),
      widgets: composed,
    };
  }

  async function save(publish: boolean) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const payload = buildPayload();
      if (typeof payload === "string") {
        setError(payload);
        return;
      }
      const saved = await client.createConfigDraft("dashboard", key.trim(), payload);
      if (publish) {
        const published = await client.publishConfig("dashboard", key.trim(), saved.version);
        setNotice(
          `Published dashboard '${key.trim()}' v${published.version} — now renderable in the view above.`,
        );
      } else {
        setNotice(
          `Draft v${saved.version} saved for dashboard '${key.trim()}'. It is designer-only until published.`,
        );
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Save failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="studio-editor">
      <p className="hint">
        Compose a dashboard from widgets over existing read endpoints, then save it as a
        governed <code>dashboard</code> config draft — publishing makes it renderable, no
        code change. One series per chart widget here; multi-series charts are composed
        in the Advanced (JSON) tab.
      </p>
      {error ? (
        <div className="state state-error" role="alert">
          <strong>Action failed.</strong> {error}
        </div>
      ) : null}
      {notice ? (
        <div className="state state-ok" role="status">
          {notice}
        </div>
      ) : null}
      <p>
        <label>
          Dashboard key{" "}
          <input
            aria-label="Dashboard key"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="e.g. operations-overview"
          />
        </label>{" "}
        <label>
          Title{" "}
          <input
            aria-label="Dashboard title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="e.g. Operations overview"
          />
        </label>{" "}
        <label>
          Description{" "}
          <input
            aria-label="Dashboard description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
      </p>

      {widgets.map((row, i) => (
        <fieldset key={i} className="studio-item">
          <legend>
            Widget {i + 1}{" "}
            <button
              type="button"
              aria-label={`Remove widget ${i + 1}`}
              onClick={() => setWidgets((rows) => rows.filter((_, j) => j !== i))}
            >
              ✕
            </button>
          </legend>
          <label>
            Id{" "}
            <input
              aria-label={`Widget ${i + 1} id`}
              value={row.id}
              placeholder={`widget-${i + 1}`}
              onChange={(e) => setWidget(i, { id: e.target.value })}
            />
          </label>{" "}
          <label>
            Title{" "}
            <input
              aria-label={`Widget ${i + 1} title`}
              value={row.title}
              onChange={(e) => setWidget(i, { title: e.target.value })}
            />
          </label>{" "}
          <label>
            Type{" "}
            <select
              aria-label={`Widget ${i + 1} type`}
              value={row.display}
              onChange={(e) => setWidget(i, { display: e.target.value as Display })}
            >
              {DASHBOARD_DISPLAYS.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          </label>{" "}
          <label>
            Endpoint{" "}
            <input
              aria-label={`Widget ${i + 1} endpoint`}
              value={row.endpoint}
              placeholder="/inpatient/census"
              onChange={(e) => setWidget(i, { endpoint: e.target.value })}
            />
          </label>{" "}
          {["value", "big-number", "gauge", "total", "count"].includes(row.display) ? (
            <label>
              Field{" "}
              <input
                aria-label={`Widget ${i + 1} field`}
                value={row.field}
                placeholder="response field"
                onChange={(e) => setWidget(i, { field: e.target.value })}
              />
            </label>
          ) : null}{" "}
          {row.display === "big-number" || row.display === "gauge" ? (
            <label>
              Unit{" "}
              <input
                aria-label={`Widget ${i + 1} unit`}
                value={row.unit}
                onChange={(e) => setWidget(i, { unit: e.target.value })}
              />
            </label>
          ) : null}
          {row.display === "line" || row.display === "bar" ? (
            <>
              <label>
                Series field{" "}
                <input
                  aria-label={`Widget ${i + 1} series field`}
                  value={row.seriesField}
                  onChange={(e) => setWidget(i, { seriesField: e.target.value })}
                />
              </label>{" "}
              <label>
                Series label{" "}
                <input
                  aria-label={`Widget ${i + 1} series label`}
                  value={row.seriesLabel}
                  onChange={(e) => setWidget(i, { seriesLabel: e.target.value })}
                />
              </label>{" "}
              <label>
                Label field{" "}
                <input
                  aria-label={`Widget ${i + 1} label field`}
                  value={row.labelField}
                  onChange={(e) => setWidget(i, { labelField: e.target.value })}
                />
              </label>
            </>
          ) : null}
          {row.display === "gauge" ? (
            <>
              <label>
                Max{" "}
                <input
                  aria-label={`Widget ${i + 1} max`}
                  value={row.max}
                  inputMode="decimal"
                  onChange={(e) => setWidget(i, { max: e.target.value })}
                />
              </label>{" "}
              <label>
                Warn threshold{" "}
                <input
                  aria-label={`Widget ${i + 1} warn threshold`}
                  value={row.warn}
                  inputMode="decimal"
                  onChange={(e) => setWidget(i, { warn: e.target.value })}
                />
              </label>{" "}
              <label>
                Critical threshold{" "}
                <input
                  aria-label={`Widget ${i + 1} critical threshold`}
                  value={row.critical}
                  inputMode="decimal"
                  onChange={(e) => setWidget(i, { critical: e.target.value })}
                />
              </label>
            </>
          ) : null}
          {row.display === "table" ? (
            <label>
              Columns{" "}
              <input
                aria-label={`Widget ${i + 1} columns`}
                value={row.columnsText}
                placeholder="field:Title, other:Other title"
                onChange={(e) => setWidget(i, { columnsText: e.target.value })}
              />
            </label>
          ) : null}
        </fieldset>
      ))}

      <p>
        <button
          type="button"
          onClick={() => setWidgets((rows) => [...rows, emptyWidget()])}
        >
          Add widget
        </button>
      </p>
      <p className="studio-actions">
        <button type="button" disabled={busy} onClick={() => save(false)}>
          Save draft
        </button>{" "}
        <button type="button" disabled={busy} onClick={() => save(true)}>
          Publish dashboard
        </button>
      </p>
    </div>
  );
}

export function DashboardsTab({ client }: { client: ApiClient }) {
  const configs = useAsync<StudioConfigObject[]>(
    () => client.listConfigs("dashboard"),
    [client],
  );
  const [selected, setSelected] = useState("");
  const [mode, setMode] = useState<"view" | "author">("view");

  if (configs.loading) return <Loading label="Loading dashboards…" />;
  if (configs.error) return <ErrorView error={configs.error} />;

  const published = (configs.data ?? []).filter((c) => c.status === "published");
  const keys = [...new Set(published.map((c) => c.key))];
  const activeKey = selected || keys[0] || "";

  return (
    <div>
      <p className="studio-view-toggle">
        <button
          type="button"
          aria-pressed={mode === "view"}
          onClick={() => setMode("view")}
        >
          View dashboards
        </button>{" "}
        <button
          type="button"
          aria-pressed={mode === "author"}
          onClick={() => setMode("author")}
        >
          Author dashboard
        </button>
      </p>
      {mode === "author" ? (
        <DashboardAuthor client={client} />
      ) : (
        <>
          <p className="hint">
            Every dashboard here is a published Studio config: title + widgets
            over existing data endpoints (a total, a list count, a field value,
            or a chart — line, bar, gauge, table). Sites compose their own
            operations views by publishing a new <code>dashboard</code> config —
            no code change, same governance. The five seeded audience
            dashboards (clinician, nurse/unit, unit manager, department head,
            executive) are what the Home page lands each role on.
          </p>
          {keys.length === 0 ? (
            <Empty label="No published dashboards yet. Author one with the Author dashboard mode above, or in the Advanced (JSON) tab as kind=dashboard, and publish it." />
          ) : (
            <>
              <p>
                <label>
                  Dashboard{" "}
                  <select
                    aria-label="Dashboard"
                    value={activeKey}
                    onChange={(e) => setSelected(e.target.value)}
                  >
                    {keys.map((k) => (
                      <option key={k} value={k}>
                        {k}
                      </option>
                    ))}
                  </select>
                </label>
              </p>
              {activeKey && <DashboardView client={client} dashKey={activeKey} />}
            </>
          )}
        </>
      )}
    </div>
  );
}
