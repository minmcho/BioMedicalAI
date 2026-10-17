import { useState } from "react";
import type { ApiClient } from "../../api/client";
import type { AuditEntry } from "../../api/types";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

/** Audit tab — read-only view of the hash-chained audit log via
 *  GET /studio/audit. One chain covers pack lifecycle actions, Studio
 *  config writes and clinical writes. */
export function AuditTab({ client }: { client: ApiClient }) {
  const [limit, setLimit] = useState(50);
  const { data, loading, error } = useAsync<AuditEntry[]>(
    () => client.listAudit(limit),
    [client, limit],
  );

  if (loading) return <Loading label="Loading audit log…" />;
  if (error) return <ErrorView error={error} />;
  if (!data) return <Empty label="No audit data." />;

  return (
    <div>
      <p className="hint">
        Append-only, hash-chained: every Studio and clinical write records an entry whose
        hash covers the previous entry's hash. This view is read-only.
      </p>
      <p>
        <label>
          Show{" "}
          <select
            aria-label="Audit entries to show"
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
          >
            {[25, 50, 100, 200].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>{" "}
          most recent entries
        </label>
      </p>
      {data.length === 0 ? (
        <Empty label="No audit entries yet." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Actor</th>
              <th>Action</th>
              <th>Subject</th>
              <th>Hash</th>
            </tr>
          </thead>
          <tbody>
            {data.map((e) => (
              <tr key={e.id}>
                <td>{e.created_at ? new Date(e.created_at).toLocaleString() : "—"}</td>
                <td>{e.actor}</td>
                <td>{e.action}</td>
                <td>{e.entity}</td>
                <td>
                  <code title={e.entry_hash}>{e.entry_hash.slice(0, 12)}…</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
