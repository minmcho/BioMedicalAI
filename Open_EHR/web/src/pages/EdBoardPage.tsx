import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { refLabel } from "../api/display";
import type { EdBoardEntry, EdState, Role } from "../api/types";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role: Role;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

const STATE_ORDER: Array<{ key: EdState; label: string }> = [
  { key: "waiting", label: "Waiting" },
  { key: "triage", label: "Triage" },
  { key: "in-treatment", label: "In treatment" },
  { key: "disposition", label: "Disposition" },
];

// Next-step actions per state. The server enforces the published ed-flow
// workflow config; these buttons only offer the standard path, and a
// config change that removes a transition surfaces as a server error.
const NEXT_ACTION: Partial<Record<EdState, { to: EdState; label: string }>> = {
  "triage": { to: "in-treatment", label: "Start treatment" },
  "in-treatment": { to: "disposition", label: "To disposition" },
};

export function EdBoardPage({ client, role }: Props) {
  const canWork = role === "admin" || role === "configurator" || role === "clinician";
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [acuities, setAcuities] = useState<Record<string, string>>({});
  const [arrivalPatient, setArrivalPatient] = useState("");
  const [arrivalComplaint, setArrivalComplaint] = useState("");

  const boardState = useAsync(() => client.edBoard(), [client, tick]);
  const board = boardState.data;

  async function perform(label: string, fn: () => Promise<unknown>) {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(`${label} — done.`);
      setTick((t) => t + 1);
    } catch (err) {
      setActionError(`${label} failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  function renderEntry(entry: EdBoardEntry) {
    const id = entry.encounter.id as string;
    const state = entry.state as EdState;
    const next = NEXT_ACTION[state];
    return (
      <li key={id} className="board-card">
        <div>
          <strong>{refLabel(entry.encounter.subject)}</strong>
          {entry.acuity != null && (
            <span className="badge">ESI {entry.acuity}</span>
          )}
          <span className="badge">wait {entry.wait_minutes} min</span>
        </div>
        <div className="hint">
          {entry.chief_complaint ?? "—"} · arrived{" "}
          {new Date(entry.arrived_at).toLocaleTimeString()}
        </div>
        {canWork && state === "waiting" && (
          <div className="row-actions">
            <label>
              Acuity (ESI){" "}
              <select
                aria-label={`Acuity for ${id}`}
                value={acuities[id] ?? "3"}
                onChange={(e) =>
                  setAcuities((prev) => ({ ...prev, [id]: e.target.value }))}
              >
                {[1, 2, 3, 4, 5].map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </select>
            </label>
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                perform("Triage", () =>
                  client.edTransition(id, "triage", Number(acuities[id] ?? "3")))}
            >
              Triage
            </button>
          </div>
        )}
        {canWork && next && (
          <div className="row-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() => perform(next.label, () => client.edTransition(id, next.to))}
            >
              {next.label}
            </button>
          </div>
        )}
        {canWork && state === "disposition" && (
          <div className="row-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() => perform("Admit", () => client.edAdmit(id))}
            >
              Admit
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => perform("Depart", () => client.edDepart(id))}
            >
              Depart
            </button>
          </div>
        )}
      </li>
    );
  }

  return (
    <section>
      <h2>Emergency department board</h2>
      <p className="hint">
        Live ED census by tracking state (waiting → triage → in-treatment →
        disposition), driven by the published Studio workflow config
        (workflow/ed-flow). Wait times are computed on the server from the
        recorded arrival time. Triage records the ESI acuity (1–5). Admit
        creates a linked inpatient encounter; Depart closes the visit —
        both are governed exits, not bare status flips.
      </p>
      {notice && (
        <p className="state state-empty" role="status">{notice}</p>
      )}
      {actionError && (
        <div className="state state-error" role="alert">{actionError}</div>
      )}

      {canWork && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!arrivalPatient.trim()) return;
            void perform("Arrival registered", async () => {
              await client.edArrive({
                patientId: arrivalPatient.trim(),
                chiefComplaint: arrivalComplaint.trim() || undefined,
              });
              setArrivalPatient("");
              setArrivalComplaint("");
            });
          }}
        >
          <h3>Register arrival</h3>
          <label>
            Patient id{" "}
            <input
              aria-label="Arrival patient id"
              value={arrivalPatient}
              onChange={(e) => setArrivalPatient(e.target.value)}
              placeholder="pat-1"
            />
          </label>
          <label>
            Chief complaint{" "}
            <input
              aria-label="Chief complaint"
              value={arrivalComplaint}
              onChange={(e) => setArrivalComplaint(e.target.value)}
              placeholder="Chest pain"
            />
          </label>
          <button type="submit" disabled={busy}>Register arrival</button>
        </form>
      )}

      {boardState.loading && <Loading label="Loading ED board…" />}
      {boardState.error != null && <ErrorView error={boardState.error} />}
      {board && board.counts.total === 0 && (
        <Empty label="The department is empty — no active ED encounters." />
      )}
      {board && STATE_ORDER.map(({ key, label }) => {
        const entries = board.groups[key] ?? [];
        if (entries.length === 0) return null;
        return (
          <div key={key}>
            <h3>{label} ({entries.length})</h3>
            <ul className="board-list">{entries.map(renderEntry)}</ul>
          </div>
        );
      })}
    </section>
  );
}
