import { useState } from "react";
import { allowedTransitions, isTerminal, TRANSITION_LABELS } from "../api/adt";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/client";
import { refLabel } from "../api/display";
import type { Appointment } from "../api/types";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
}

function toIso(local: string): string | undefined {
  if (!local) return undefined;
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

/** Scheduling — Appointment search by practitioner + date range, with ADT
 *  transition buttons driven by the real state machine. Buttons only ever
 *  offer transitions the server allows; terminal states offer none. */
export function SchedulingPage({ client }: Props) {
  const [practitioner, setPractitioner] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [params, setParams] = useState<{ practitioner?: string; start?: string; end?: string }>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [overrides, setOverrides] = useState<Record<string, Appointment>>({});

  const { data, loading, error, reload } = useAsync<Appointment[]>(
    () => client.search<Appointment>("Appointment", params),
    [client, params],
  );

  async function doTransition(appt: Appointment, toStatus: string) {
    if (!appt.id) return;
    setBusyId(appt.id);
    setActionError(null);
    try {
      const updated = await client.transitionAppointment(appt.id, toStatus);
      setOverrides((prev) => ({ ...prev, [appt.id as string]: updated }));
    } catch (err) {
      setActionError(
        err instanceof ApiError ? err.detail : "Transition failed. Reload the list and try again.",
      );
      reload();
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section>
      <h2>Scheduling</h2>
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          setOverrides({});
          setParams({
            practitioner: practitioner.trim() || undefined,
            start: toIso(start),
            end: toIso(end),
          });
        }}
      >
        <label className="field">
          Practitioner id or reference
          <input
            value={practitioner}
            onChange={(e) => setPractitioner(e.target.value)}
            placeholder="dr-1 or Practitioner/dr-1"
          />
        </label>
        <label className="field">
          From
          <input type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} />
        </label>
        <label className="field">
          To
          <input type="datetime-local" value={end} onChange={(e) => setEnd(e.target.value)} />
        </label>
        <button type="submit">Search appointments</button>
      </form>

      {actionError ? (
        <div className="state state-error" role="alert">
          <strong>Transition failed.</strong> {actionError}
        </div>
      ) : null}

      {loading ? (
        <Loading label="Loading appointments…" />
      ) : error ? (
        <ErrorView error={error} />
      ) : !data || data.length === 0 ? (
        <Empty label="No appointments match." />
      ) : (
        <ul className="appt-list">
          {data.map((raw) => {
            const appt = (raw.id && overrides[raw.id]) || raw;
            const targets = allowedTransitions(appt.status);
            return (
              <li key={appt.id} className="appt-row">
                <div className="appt-main">
                  <strong>{appt.description ?? `Appointment ${appt.id}`}</strong>
                  <span className="appt-meta">
                    status: <span data-testid="appt-status">{appt.status ?? "—"}</span>
                    {appt.start ? ` · starts ${new Date(appt.start).toLocaleString()}` : ""}
                  </span>
                  <span className="appt-meta">
                    {(appt.participant ?? [])
                      .map((p) => refLabel(p.actor))
                      .filter((v) => v !== "—")
                      .join(" · ")}
                  </span>
                </div>
                <div className="appt-actions">
                  {isTerminal(appt.status) || targets.length === 0 ? (
                    <span className="hint">No further transitions</span>
                  ) : (
                    targets.map((target) => (
                      <button
                        key={target}
                        type="button"
                        disabled={busyId === appt.id}
                        onClick={() => doTransition(appt, target)}
                      >
                        {TRANSITION_LABELS[target] ?? target}
                      </button>
                    ))
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
