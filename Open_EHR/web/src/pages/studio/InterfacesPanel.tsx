import { useCallback, useEffect, useState } from "react";
import type { ApiClient } from "../../api/client";
import type {
  DispatchStats,
  InteropEndpointsResponse,
  InteropMessage,
} from "../../api/types";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";

/** Interfaces panel (Phase 8) — the live HL7 interface engine, shown
 *  under the Integration tab's config editor. Endpoint definitions
 *  themselves are Studio configuration (edited above, draft ->
 *  publish); this panel is the operational view: endpoint status and
 *  queue tallies, the recent message log, one-click dispatcher runs,
 *  and dead-letter replay (attempt history is preserved server-side). */
export function InterfacesPanel({ client }: { client: ApiClient }) {
  const [endpoints, setEndpoints] = useState<InteropEndpointsResponse | null>(null);
  const [messages, setMessages] = useState<InteropMessage[]>([]);
  const [deadLetters, setDeadLetters] = useState<InteropMessage[]>([]);
  const [stats, setStats] = useState<DispatchStats | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [ep, log, dead] = await Promise.all([
        client.interopEndpoints(),
        client.interopMessages({ limit: 25 }),
        client.interopDeadLetters(),
      ]);
      setEndpoints(ep);
      setMessages(log.items);
      setDeadLetters(dead.items);
      setError(null);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [client]);

  useEffect(() => {
    void load();
  }, [load]);

  const runDispatch = async () => {
    setBusy(true);
    try {
      const result = await client.dispatchInterop();
      setStats(result.stats);
      await load();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const replay = async (id: number) => {
    setBusy(true);
    try {
      await client.replayDeadLetter(id);
      await load();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  if (loading && !endpoints) return <Loading label="Loading interfaces…" />;
  if (error && !endpoints) return <ErrorView error={error} />;

  return (
    <div className="interfaces-panel">
      <h3>Live HL7 interfaces (MLLP)</h3>
      <p className="hint">
        Outbound ADT / ORM / ORU messages queue in the same transaction as the clinical
        event and are delivered by the dispatcher to the enabled endpoints below.
        Endpoint definitions are configuration — edit them in the editor above and
        publish; there is no separate write path.
      </p>
      {error ? <ErrorView error={error} /> : null}
      {endpoints && endpoints.endpoints.length === 0 ? (
        <Empty label="No endpoints in the published hl7-endpoints config." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Endpoint</th>
              <th>Address</th>
              <th>Messages</th>
              <th>Status</th>
              <th>Queue (pending / sent / dead)</th>
            </tr>
          </thead>
          <tbody>
            {endpoints?.endpoints.map((e) => (
              <tr key={e.name}>
                <td>{e.name}</td>
                <td>
                  {e.host}:{e.port}
                </td>
                <td>{e.messageTypes.join(", ")}</td>
                <td>{e.enabled ? "enabled" : "disabled"}</td>
                <td>
                  {e.queue.pending} / {e.queue.sent} / {e.queue.dead_letter}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p>
        <button type="button" onClick={() => void runDispatch()} disabled={busy}>
          Run dispatcher pass
        </button>{" "}
        <button type="button" onClick={() => void load()} disabled={busy}>
          Refresh
        </button>
        {stats ? (
          <span className="hint">
            {" "}
            Last pass: {stats.attempted} attempted, {stats.sent} sent, {stats.failed}{" "}
            failed, {stats.dead_lettered} dead-lettered, {stats.skipped} skipped.
          </span>
        ) : null}
      </p>

      <h4>Dead letters</h4>
      {deadLetters.length === 0 ? (
        <Empty label="No dead letters." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Message</th>
              <th>Endpoint</th>
              <th>Patient</th>
              <th>Attempts</th>
              <th>Last error</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {deadLetters.map((m) => (
              <tr key={m.id}>
                <td>{m.message_type}</td>
                <td>{m.endpoint}</td>
                <td>{m.patient_ref ?? "—"}</td>
                <td>{m.attempts}</td>
                <td>{m.last_error ?? "—"}</td>
                <td>
                  <button
                    type="button"
                    onClick={() => void replay(m.id)}
                    disabled={busy}
                  >
                    Replay
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h4>Recent messages</h4>
      {messages.length === 0 ? (
        <Empty label="No interface messages yet." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>When</th>
              <th>Direction</th>
              <th>Type</th>
              <th>Status</th>
              <th>Patient</th>
              <th>Endpoint / peer</th>
            </tr>
          </thead>
          <tbody>
            {messages.map((m) => (
              <tr key={`${m.direction}-${m.id}`}>
                <td>
                  {m.created_at ? new Date(m.created_at).toLocaleString() : "—"}
                </td>
                <td>{m.direction}</td>
                <td>{m.message_type}</td>
                <td>{m.status}</td>
                <td>{m.patient_ref ?? "—"}</td>
                <td>{m.endpoint ?? m.peer ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
