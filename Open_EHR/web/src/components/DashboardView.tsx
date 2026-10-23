import { useState } from "react";
import type {
  ApiClient,
  IndicatorDefinition,
  IndicatorRecordsResponse,
} from "../api/client";
import type {
  DashboardConfig,
  DashboardWidgetConfig,
  StudioConfigObject,
} from "../api/types";
import { StatusBadge, type StatusTone } from "./StatusBadge";
import { Empty, ErrorView, Loading } from "./StatusViews";
import { useAsync } from "../hooks";

/** Shared dashboard renderer (Phase 10 mechanism, Phase 13 charts).
 *
 *  A dashboard is governed config (kind=dashboard): a title plus
 *  widgets that reference EXISTING read endpoints. This component
 *  renders any published dashboard config — Studio's Dashboards tab
 *  and the role-aware Home landing both use it, so a new dashboard is
 *  a config publish, never a code change.
 *
 *  Displays:
 *  - total / count / value — the Phase 10 scalar widgets.
 *  - big-number — a prominent scalar (audience-dashboard headlines).
 *  - line / bar — hand-rolled SVG over the endpoint's `items` list
 *    (or a FHIR Bundle's `entry[].resource`): `series` names the
 *    numeric fields to plot, `labelField` the x labels. No chart
 *    library — the shapes are deliberately simple and testable.
 *  - gauge — a scalar against `max`, coloured by `thresholds` with
 *    HIGHER-IS-BETTER semantics: value >= warn is on target, below
 *    critical is critical, between is warning. (Occupancy-style
 *    lower-is-better gauges are expressed by publishing the inverse
 *    metric, keeping one semantics everywhere.)
 *  - table — `columns` over `items`.
 *
 *  A widget whose endpoint fails degrades alone ("unavailable"); the
 *  rest of the dashboard still renders. */

const SERIES_COLORS = ["#0b6e4f", "#1f6fb2", "#b25f00", "#6d4fa1", "#a33327"];
const GAUGE_OK = "#0b6e4f";
const GAUGE_WARN = "#b25f00";
const GAUGE_CRITICAL = "#a33327";

export function DashboardGrid({
  client,
  config,
  onOpenChart,
}: {
  client: ApiClient;
  config: DashboardConfig;
  onOpenChart?: (patientId: string) => void;
}) {
  return (
    <section aria-label={config.title}>
      <h3>{config.title}</h3>
      <div className="dashboard-grid">
        {config.widgets.map((w) => (
          <Widget key={w.id} client={client} widget={w} onOpenChart={onOpenChart} />
        ))}
      </div>
    </section>
  );
}

export function DashboardView({
  client,
  dashKey,
  onOpenChart,
}: {
  client: ApiClient;
  dashKey: string;
  onOpenChart?: (patientId: string) => void;
}) {
  const effective = useAsync<StudioConfigObject>(
    () => client.effectiveConfig("dashboard", dashKey),
    [client, dashKey],
  );
  if (effective.loading) return <Loading label="Loading dashboard…" />;
  if (effective.error) return <ErrorView error={effective.error} />;
  if (!effective.data) return <Empty label="Dashboard not found." />;
  const config = effective.data.payload as unknown as DashboardConfig;
  return <DashboardGrid client={client} config={config} onOpenChart={onOpenChart} />;
}

// ---------------------------------------------------------------------------
// Analysis drill-down (Home & Module UX pass, WS4b) — shared by
// DashboardView widgets and the AnalyticsPage indicators browser:
// any reported number drills to definition -> records -> record.
// Shapes are the REAL backend payloads (backend/app/indicators.py):
// GET /indicators/{key}/records returns {key, definition, value,
// records: [{id, label, detail, at, patient_id}], records_supported,
// reason}.
// ---------------------------------------------------------------------------

/** The indicator key a widget resolves through, or null when the
 *  widget is NOT indicator-backed. Rewired widgets (backend
 *  platform_depth._rewired_widget) carry `indicator` explicitly and
 *  point their endpoint at /indicators/{key}/value; either signal
 *  alone is sufficient. Non-indicator widgets stay non-clickable. */
export function indicatorKeyOfWidget(widget: DashboardWidgetConfig): string | null {
  const carried = (widget as DashboardWidgetConfig & { indicator?: unknown })
    .indicator;
  if (typeof carried === "string" && carried) return carried;
  const match = /^\/indicators\/([^/?#]+)\/value(?:[?#].*)?$/.exec(widget.endpoint);
  return match ? decodeURIComponent(match[1]) : null;
}

/** Load the records payload through the client's typed method when
 *  present, else the generic request surface (older test doubles). */
export function fetchIndicatorRecords(
  client: ApiClient,
  key: string,
): Promise<IndicatorRecordsResponse> {
  if (typeof client.indicatorRecords === "function") {
    return client.indicatorRecords(key);
  }
  return client.request<IndicatorRecordsResponse>(
    `/indicators/${encodeURIComponent(key)}/records`,
  );
}

/** Backend value status -> StatusBadge tone (text travels with tone). */
export function indicatorStatusTone(status: string): StatusTone {
  if (status === "ok") return "ok";
  if (status === "warn") return "warn";
  if (status === "critical") return "bad";
  return "neutral";
}

/** Count-type = the records list IS the number (named counts, cohort
 *  counts), as opposed to a percentage whose records are only its
 *  numerator side. */
export function isCountIndicator(definition: IndicatorDefinition): boolean {
  const source = definition.source;
  if (!source) return false;
  if (source.type === "cohort") return true;
  if (source.type === "named") {
    return definition.unit !== "%" && !(source.name ?? "").endsWith("-pct");
  }
  return false;
}

function sourceText(definition: IndicatorDefinition): string {
  const source = definition.source ?? { type: "?" };
  if (source.type === "named") return `Named source ${source.name ?? "?"}`;
  if (source.type === "measure") return `Measure ${source.measure_key ?? "?"}`;
  if (source.type === "cohort") return `Cohort ${source.cohort_key ?? "?"}`;
  return `Source ${source.type}`;
}

/** The published definition rendered readably (never raw JSON alone):
 *  name, source, direction, thresholds, target and unit. */
export function IndicatorDefinitionSummary({
  definition,
}: {
  definition: IndicatorDefinition;
}) {
  const thresholds = definition.thresholds ?? {};
  return (
    <dl className="indicator-definition">
      <div>
        <dt>Name</dt>
        <dd>{definition.title ?? definition.name}</dd>
      </div>
      <div>
        <dt>Source</dt>
        <dd>{sourceText(definition)}</dd>
      </div>
      <div>
        <dt>Direction</dt>
        <dd>
          {definition.direction}
          {definition.direction === "lower-better"
            ? " (lower is better)"
            : definition.direction === "higher-better"
              ? " (higher is better)"
              : ""}
        </dd>
      </div>
      {(thresholds.warn != null || thresholds.critical != null) && (
        <div>
          <dt>Thresholds</dt>
          <dd>
            {thresholds.warn != null ? `Warn at ${thresholds.warn}` : ""}
            {thresholds.warn != null && thresholds.critical != null ? " · " : ""}
            {thresholds.critical != null ? `Critical at ${thresholds.critical}` : ""}
          </dd>
        </div>
      )}
      {definition.target != null && (
        <div>
          <dt>Target</dt>
          <dd>{definition.target}</dd>
        </div>
      )}
      {definition.unit && (
        <div>
          <dt>Unit</dt>
          <dd>{definition.unit}</dd>
        </div>
      )}
    </dl>
  );
}

/** The records half of the drill: a count header ("N records" — for
 *  count-type indicators N equals the reported value from the same
 *  payload, by backend construction), the records table, or — when
 *  records_supported is false — the backend's reason, plainly, with
 *  no fake table. Patient-linked rows are 1-click chart buttons when
 *  the host page provides a chart opener; otherwise plain text. */
export function IndicatorRecordsSection({
  data,
  onOpenChart,
}: {
  data: IndicatorRecordsResponse;
  onOpenChart?: (patientId: string) => void;
}) {
  if (!data.records_supported) {
    return (
      <p role="status">
        {data.reason ?? "Underlying records are not available for this indicator."}
      </p>
    );
  }
  return (
    <div>
      <h4>
        {data.records.length} records
        {isCountIndicator(data.definition) &&
        typeof data.value.value === "number"
          ? ` — matches the reported value ${data.value.value}`
          : ""}
      </h4>
      {data.reason && <p className="hint">{data.reason}</p>}
      {data.records.length === 0 ? (
        <Empty label="No underlying records — the reported value is a genuine zero." />
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">Record</th>
              <th scope="col">Detail</th>
              <th scope="col">At</th>
            </tr>
          </thead>
          <tbody>
            {data.records.map((record) => (
              <tr key={record.id}>
                <td>
                  {record.patient_id && onOpenChart ? (
                    <button
                      type="button"
                      onClick={() => onOpenChart(record.patient_id as string)}
                    >
                      {record.label}
                    </button>
                  ) : (
                    record.label
                  )}
                </td>
                <td>{record.detail}</td>
                <td>{record.at ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** Full drill panel for one indicator key: value + status, readable
 *  definition, then the records section. Used by dashboard widgets;
 *  AnalyticsPage composes the same pieces around its own selection. */
export function IndicatorDrillPanel({
  client,
  indicatorKey,
  onOpenChart,
}: {
  client: ApiClient;
  indicatorKey: string;
  onOpenChart?: (patientId: string) => void;
}) {
  const drill = useAsync<IndicatorRecordsResponse>(
    () => fetchIndicatorRecords(client, indicatorKey),
    [client, indicatorKey],
  );
  if (drill.loading) return <Loading label="Loading underlying records…" />;
  if (drill.error != null) return <ErrorView error={drill.error} />;
  if (!drill.data) return <Empty label="No drill-down data for this indicator." />;
  const { definition, value } = drill.data;
  return (
    <div className="indicator-drill">
      <div className="dashboard-widget-value dashboard-widget-big">
        {value.value == null ? "—" : value.value}
        {value.unit ? <span className="gauge-unit"> {value.unit}</span> : null}{" "}
        <StatusBadge tone={indicatorStatusTone(value.status)}>
          {value.status}
        </StatusBadge>
      </div>
      {value.target != null && <p className="hint">Target: {value.target}</p>}
      <IndicatorDefinitionSummary definition={definition} />
      <IndicatorRecordsSection data={drill.data} onOpenChart={onOpenChart} />
    </div>
  );
}

type Row = Record<string, unknown>;

function itemsOf(data: Row): Row[] | null {
  if (Array.isArray(data.items)) return data.items as Row[];
  if (Array.isArray(data.entry)) {
    return (data.entry as Array<{ resource?: Row }>).map(
      (e) => e.resource ?? {},
    );
  }
  return null;
}

function formatCell(raw: unknown): string {
  if (raw == null) return "—";
  if (typeof raw === "number" || typeof raw === "string") return String(raw);
  if (typeof raw === "boolean") return raw ? "yes" : "no";
  return JSON.stringify(raw);
}

function scalarOf(widget: DashboardWidgetConfig, data: Row): string {
  if (widget.display === "total") {
    return typeof data.total === "number" ? String(data.total) : "—";
  }
  if (widget.display === "count") {
    const items = itemsOf(data);
    return items ? String(items.length) : "—";
  }
  const raw = widget.field ? data[widget.field] : undefined;
  return formatCell(raw);
}

function numberOf(raw: unknown): number | null {
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

function Gauge({ widget, data }: { widget: DashboardWidgetConfig; data: Row }) {
  const value = widget.field ? numberOf(data[widget.field]) : null;
  if (value == null) return <div className="dashboard-widget-value">—</div>;
  const max = widget.max && widget.max > 0 ? widget.max : 100;
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  const warn = widget.thresholds?.warn;
  const critical = widget.thresholds?.critical;
  const color =
    critical != null && value < critical
      ? GAUGE_CRITICAL
      : warn != null && value < warn
        ? GAUGE_WARN
        : GAUGE_OK;
  return (
    <div>
      <div className="dashboard-widget-value">
        {value}
        {widget.unit ? <span className="gauge-unit"> {widget.unit}</span> : null}
      </div>
      <svg
        viewBox="0 0 220 14"
        width="220"
        height="14"
        role="img"
        aria-label={`${widget.title}: ${value} of ${max}`}
      >
        <rect x="0" y="2" width="220" height="10" rx="5" fill="#e3e9ec" />
        <rect
          x="0"
          y="2"
          width={(220 * pct) / 100}
          height="10"
          rx="5"
          fill={color}
        />
      </svg>
    </div>
  );
}

function Chart({
  widget,
  data,
}: {
  widget: DashboardWidgetConfig;
  data: Row;
}) {
  const items = itemsOf(data) ?? [];
  const series = widget.series ?? [];
  const W = 264;
  const H = 110;
  const PAD = 10;
  const labelOf = (row: Row) =>
    widget.labelField ? formatCell(row[widget.labelField]) : "";
  const maxValue = Math.max(
    1,
    ...items.flatMap((row) =>
      series.map((s) => numberOf(row[s.field]) ?? 0),
    ),
  );
  if (items.length === 0 || series.length === 0) {
    return <div className="dashboard-widget-value">—</div>;
  }
  if (widget.display === "line") {
    const stepX = items.length > 1 ? (W - 2 * PAD) / (items.length - 1) : 0;
    const y = (v: number) => H - PAD - (v / maxValue) * (H - 2 * PAD);
    return (
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width={W}
        height={H}
        role="img"
        aria-label={`${widget.title} line chart`}
      >
        <line x1={PAD} y1={H - PAD} x2={W - PAD} y2={H - PAD} stroke="#d6dee3" />
        {series.map((s, si) => {
          const points = items
            .map((row, i) => {
              const v = numberOf(row[s.field]) ?? 0;
              return `${PAD + i * stepX},${y(v)}`;
            })
            .join(" ");
          return (
            <polyline
              key={s.field}
              points={points}
              fill="none"
              stroke={SERIES_COLORS[si % SERIES_COLORS.length]}
              strokeWidth="2"
            />
          );
        })}
        {items.map((row, i) => (
          <text
            key={i}
            x={PAD + i * stepX}
            y={H - 1}
            fontSize="7"
            fill="#5b6b76"
            textAnchor="middle"
          >
            {labelOf(row).slice(5)}
          </text>
        ))}
      </svg>
    );
  }
  // bar
  const groupW = (W - 2 * PAD) / items.length;
  const barW = Math.max(2, groupW / series.length - 2);
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      width={W}
      height={H}
      role="img"
      aria-label={`${widget.title} bar chart`}
    >
      <line x1={PAD} y1={H - PAD} x2={W - PAD} y2={H - PAD} stroke="#d6dee3" />
      {items.map((row, i) =>
        series.map((s, si) => {
          const v = numberOf(row[s.field]) ?? 0;
          const h = (v / maxValue) * (H - 2 * PAD);
          return (
            <rect
              key={`${i}-${s.field}`}
              x={PAD + i * groupW + si * (barW + 2)}
              y={H - PAD - h}
              width={barW}
              height={h}
              fill={SERIES_COLORS[si % SERIES_COLORS.length]}
            >
              <title>{`${labelOf(row)} ${s.label ?? s.field}: ${v}`}</title>
            </rect>
          );
        }),
      )}
      {items.map((row, i) => (
        <text
          key={i}
          x={PAD + i * groupW + groupW / 2}
          y={H - 1}
          fontSize="7"
          fill="#5b6b76"
          textAnchor="middle"
        >
          {labelOf(row).slice(0, 10)}
        </text>
      ))}
    </svg>
  );
}

function DataTable({
  widget,
  data,
}: {
  widget: DashboardWidgetConfig;
  data: Row;
}) {
  const items = itemsOf(data) ?? [];
  const columns = widget.columns ?? [];
  if (columns.length === 0) return <div className="dashboard-widget-value">—</div>;
  return (
    <table className="dashboard-table">
      <thead>
        <tr>
          {columns.map((c) => (
            <th key={c.field} scope="col">
              {c.title}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {items.map((row, i) => (
          <tr key={i}>
            {columns.map((c) => (
              <td key={c.field}>{formatCell(row[c.field])}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Widget({
  client,
  widget,
  onOpenChart,
}: {
  client: ApiClient;
  widget: DashboardWidgetConfig;
  onOpenChart?: (patientId: string) => void;
}) {
  const { data, loading, error } = useAsync<Row>(
    () => client.request<Row>(widget.endpoint),
    [client, widget.endpoint],
  );
  const [drillOpen, setDrillOpen] = useState(false);
  // Only indicator-backed widgets get a drill affordance; every other
  // widget stays non-clickable (no dead affordance).
  const indicatorKey = indicatorKeyOfWidget(widget);
  const isChart =
    widget.display === "line" ||
    widget.display === "bar" ||
    widget.display === "gauge" ||
    widget.display === "table";
  return (
    <div
      className={`dashboard-widget${isChart ? " dashboard-widget-wide" : ""}`}
    >
      <div className="dashboard-widget-title">{widget.title}</div>
      {loading ? (
        <div className="dashboard-widget-value">…</div>
      ) : error || !data ? (
        <div className="dashboard-widget-value">unavailable</div>
      ) : widget.display === "gauge" ? (
        <Gauge widget={widget} data={data} />
      ) : widget.display === "line" || widget.display === "bar" ? (
        <Chart widget={widget} data={data} />
      ) : widget.display === "table" ? (
        <DataTable widget={widget} data={data} />
      ) : (
        <div
          className={`dashboard-widget-value${
            widget.display === "big-number" ? " dashboard-widget-big" : ""
          }`}
        >
          {scalarOf(widget, data)}
          {widget.display === "big-number" && widget.unit ? (
            <span className="gauge-unit"> {widget.unit}</span>
          ) : null}
        </div>
      )}
      <div className="hint">{widget.endpoint}</div>
      {indicatorKey && !loading && !error && data && (
        <>
          <button
            type="button"
            aria-expanded={drillOpen}
            onClick={() => setDrillOpen((open) => !open)}
          >
            {drillOpen ? "Hide underlying records" : "View underlying records"}
          </button>
          {drillOpen && (
            <IndicatorDrillPanel
              client={client}
              indicatorKey={indicatorKey}
              onOpenChart={onOpenChart}
            />
          )}
        </>
      )}
    </div>
  );
}
