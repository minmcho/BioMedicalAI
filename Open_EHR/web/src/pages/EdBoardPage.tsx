import { useEffect, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { refLabel } from "../api/display";
import type { EdBoardEntry, EdState, Role } from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { ModuleHeader, scrollToSection } from "../components/ModuleHeader";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

/** ESI acuity tone map (UX spec P-3): 1 = resuscitation, 2 = emergent. */
export function esiTone(acuity?: number | null): StatusTone {
  if (acuity === 1) return "bad";
  if (acuity === 2) return "warn";
  if (acuity === 3) return "info";
  return "neutral";
}

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

const NEXT_LABELS: Record<string, string> = {
  "in-treatment": "Start treatment",
  "disposition": "To disposition",
};

interface EdMachine {
  columns: Array<{ key: EdState; label: string }>;
  nextAction: Partial<Record<string, { to: EdState; label: string }>>;
  esiRange: number[];
}

/** Derive board columns, next-step buttons and the ESI option range
 *  from the published ed-flow workflow + ed-triage terminology
 *  payloads. Governed targets (admitted/departed) never become buttons
 *  — they keep their dedicated actions — and waiting -> triage keeps
 *  its ESI-select flow. */
export function edMachineFromPayloads(
  workflowPayload: unknown,
  triagePayload: unknown,
): EdMachine | null {
  if (!workflowPayload || typeof workflowPayload !== "object" || Array.isArray(workflowPayload)) {
    return null;
  }
  const p = workflowPayload as Record<string, unknown>;
  const governed = new Set(
    Array.isArray(p.governedStates) ? (p.governedStates as string[]) : ["admitted", "departed"],
  );
  const labels: Record<string, string> = {
    waiting: "Waiting", triage: "Triage",
    "in-treatment": "In treatment", disposition: "Disposition",
  };
  const active = Array.isArray(p.activeStates)
    ? (p.activeStates as unknown[]).filter((s): s is string => typeof s === "string")
    : [];
  const columns = (active.length > 0
    ? active
    : STATE_ORDER.map((s) => s.key as string)
  ).map((key) => ({ key: key as EdState, label: labels[key] ?? key }));
  const nextAction: EdMachine["nextAction"] = {};
  if (Array.isArray(p.transitions)) {
    for (const t of p.transitions) {
      if (!t || typeof t !== "object") continue;
      const { from, to } = t as { from?: unknown; to?: unknown };
      if (typeof from !== "string" || typeof to !== "string") continue;
      if (governed.has(to) || to === "triage") continue;
      if (nextAction[from]) continue;
      nextAction[from] = {
        to: to as EdState,
        label: NEXT_LABELS[to] ?? `Move to ${to}`,
      };
    }
  }
  let esiRange = [1, 2, 3, 4, 5];
  if (triagePayload && typeof triagePayload === "object" && !Array.isArray(triagePayload)) {
    const tp = triagePayload as Record<string, unknown>;
    const min = tp.min;
    const max = tp.max;
    if (typeof min === "number" && typeof max === "number" && min <= max) {
      esiRange = Array.from({ length: max - min + 1 }, (_, i) => min + i);
    }
  }
  return { columns, nextAction, esiRange };
}

const DEFAULT_ED_MACHINE: EdMachine = {
  columns: STATE_ORDER,
  nextAction: NEXT_ACTION,
  esiRange: [1, 2, 3, 4, 5],
};

export function EdBoardPage({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canWork = permissions["ed.work"]?.[role] === true;
  const [machine, setMachine] = useState<EdMachine>(DEFAULT_ED_MACHINE);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        if (typeof client.effectiveConfig !== "function") return;
        const [wf, tri] = await Promise.all([
          client.effectiveConfig("workflow", "ed-flow"),
          client.effectiveConfig("terminology", "ed-triage").catch(() => null),
        ]);
        const built = edMachineFromPayloads(wf?.payload, tri?.payload);
        if (!cancelled && built) setMachine(built);
      } catch {
        // keep the built-in board shape
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client]);
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
    const next = machine.nextAction[state];
    return (
      <li key={id} className="board-card">
        <div>
          <strong>{refLabel(entry.encounter.subject)}</strong>
          {entry.acuity != null && (
            <StatusBadge tone={esiTone(entry.acuity)}>ESI {entry.acuity}</StatusBadge>
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
                {machine.esiRange.map((n) => (
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
      <ModuleHeader
        icon="siren"
        group="Clinical"
        title="Emergency department board"
        purpose="Live ED tracking: waiting, triage, treatment and disposition, with admit and depart."
        primaryAction={canWork
          ? { label: "Register arrival", onClick: () => scrollToSection("ed-arrival-form") }
          : undefined}
      />
      <p className="hint">
        Live ED census by tracking state (waiting → triage → in-treatment →
        disposition), driven by the published Studio workflow config
        (workflow/ed-flow). Wait times are computed on the server from the
        recorded arrival time. Triage records the ESI acuity (the
        published ed-triage range; 1–5 by default). Admit
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
          id="ed-arrival-form"
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
      {board && machine.columns.map(({ key, label }) => {
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
