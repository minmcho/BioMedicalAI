import type { ApiClient } from "../../api/client";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

/** Approvals tab — one inbox over every surface that holds work
 *  for a human decision (Studio parity row 5): pack lifecycle
 *  steps, config drafts awaiting publish, break-glass reviews, data
 *  quality quarantine and dedup candidates. The inbox AGGREGATES
 *  the real pending items from /studio-ops/inbox; it does not
 *  approve anything itself — each item names the owning surface
 *  (its action hint) where the decision is actually made. */

interface InboxItem {
  source: string;
  id: string | number;
  title: string;
  detail?: string;
  created_at?: string | null;
  action?: string;
}

interface InboxResponse {
  items: InboxItem[];
  total?: number;
  by_source?: Record<string, number>;
}

function formatWhen(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

export function ApprovalsTab({ client }: { client: ApiClient }) {
  const inbox = useAsync<InboxResponse>(
    () => client.request<InboxResponse>("/studio-ops/inbox"),
    [client],
  );

  if (inbox.loading) return <Loading label="Loading approvals inbox…" />;
  if (inbox.error) return <ErrorView error={inbox.error} />;

  const items = inbox.data?.items ?? [];
  const total = inbox.data?.total ?? items.length;

  if (items.length === 0) {
    return (
      <div>
        <p className="hint">
          Every pending human decision across Studio — pack lifecycle steps,
          drafts awaiting publish, break-glass reviews, data-quality
          quarantine and dedup candidates — in one inbox.
        </p>
        <Empty label="Nothing is awaiting approval right now." />
      </div>
    );
  }

  const bySource = new Map<string, InboxItem[]>();
  for (const item of items) {
    const list = bySource.get(item.source) ?? [];
    list.push(item);
    bySource.set(item.source, list);
  }
  // Prefer the server's counts for the summary; fall back to the
  // grouped items when by_source is absent.
  const counts: Array<[string, number]> =
    inbox.data?.by_source && Object.keys(inbox.data.by_source).length > 0
      ? Object.entries(inbox.data.by_source)
      : [...bySource.entries()].map(([source, list]) => [source, list.length]);

  return (
    <div>
      <p className="hint">
        Every pending human decision across Studio — pack lifecycle steps,
        drafts awaiting publish, break-glass reviews, data-quality quarantine
        and dedup candidates — in one inbox. Decisions are made in each
        item&rsquo;s owning surface; this tab aggregates, it does not approve.
      </p>
      <p>
        <strong>{total}</strong> awaiting approval{" "}
        {counts.map(([source, count]) => (
          <span key={source}>
            <span className="badge">
              {source}: {count}
            </span>{" "}
          </span>
        ))}
      </p>

      {[...bySource.entries()].map(([source, list]) => (
        <section key={source}>
          <h3>{`${source} (${list.length})`}</h3>
          <table className="studio-table">
            <thead>
              <tr>
                <th>Item</th>
                <th>Detail</th>
                <th>Action</th>
                <th>Received</th>
              </tr>
            </thead>
            <tbody>
              {list.map((item) => (
                <tr key={`${item.source}-${item.id}`}>
                  <td>
                    <strong>{item.title}</strong>
                    <br />
                    <span className="hint">{item.id}</span>
                  </td>
                  <td>{item.detail || "—"}</td>
                  <td>{item.action || "—"}</td>
                  <td>{formatWhen(item.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}
    </div>
  );
}
