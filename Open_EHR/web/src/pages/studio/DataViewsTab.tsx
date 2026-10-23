import type { ApiClient } from "../../api/client";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

/** Data Views tab — the governed data-source registry (Studio
 *  parity row 15, adapted). Agentic_App's join designer + raw-SQL
 *  views become, in OpenCare, a READ-ONLY registry over governed
 *  sources only (report-engine datasets, warehouse tables, lake
 *  zones) with live counts, served by /studio-ops/data-views.
 *  Raw SQL is refused by design in the clinical runtime (the
 *  report-engine hard rule), so this surface has no SQL editor. */

interface DataViewItem {
  name: string;
  kind?: string;
  description?: string;
  columns?: unknown;
  row_count?: number | null;
  rowCount?: number | null;
  source?: string;
}

interface DataViewsResponse {
  items: DataViewItem[];
}

function columnsLabel(columns: unknown): string {
  if (typeof columns === "number") return String(columns);
  if (Array.isArray(columns)) return String(columns.length);
  return "—";
}

function rowCountLabel(item: DataViewItem): string {
  const count = item.row_count ?? item.rowCount;
  return typeof count === "number" ? String(count) : "—";
}

export function DataViewsTab({ client }: { client: ApiClient }) {
  const views = useAsync<DataViewsResponse>(
    () => client.request<DataViewsResponse>("/studio-ops/data-views"),
    [client],
  );

  if (views.loading) return <Loading label="Loading data views…" />;
  if (views.error) return <ErrorView error={views.error} />;

  const items = views.data?.items ?? [];

  return (
    <div>
      <p className="hint">
        Governed sources only — this surface has no SQL editor, by design.
        Every entry is a governed source (a report-engine dataset, a
        warehouse table, a lake zone) with its live shape and count; new
        sources arrive by publishing governed config, never by writing a
        query here.
      </p>
      {items.length === 0 ? (
        <Empty label="No governed data views are registered yet." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Kind</th>
              <th>Source</th>
              <th>Description</th>
              <th>Columns</th>
              <th>Rows</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.name}>
                <td>
                  <strong>{item.name}</strong>
                </td>
                <td>{item.kind || "—"}</td>
                <td>{item.source || "—"}</td>
                <td>{item.description || "—"}</td>
                <td>{columnsLabel(item.columns)}</td>
                <td>{rowCountLabel(item)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
