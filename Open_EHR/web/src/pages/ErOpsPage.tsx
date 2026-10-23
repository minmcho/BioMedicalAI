import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { refText } from "../api/display";
import type { Activation, Role } from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { ModuleHeader } from "../components/ModuleHeader";
import { StatusBadge } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";
import { esiTone } from "./EdBoardPage";

interface Props {
  client: ApiClient;
  role: Role;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}



/** Phase 12 ER operations: the ED board with per-ESI target timers
 *  from published config, split-flow (fast-track vs main) queues,
 *  STEMI/stroke/trauma activation timers, the results-callback queue
 *  and LWBS capture. EMTALA documentation (MSE + transfer
 *  certification) is enforced server-side: a transfer-out without them
 *  is refused with the missing elements named. */
export function ErOpsPage({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canWork = permissions["continuum.work"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pathways, setPathways] = useState<Record<string, string>>({});

  const boardState = useAsync(() => client.continuumErBoard(), [client, tick]);
  const activationsState = useAsync(() => client.listActivations("open"), [client, tick]);
  const callbacksState = useAsync(() => client.callbackQueue(), [client, tick]);
  const board = boardState.data;
  // UX pass: activation pathways come from the SAME published
  // config the server validates against (terminology /
  // continuum-ops → activationPathways), not a hard-coded list.
  // When the config cannot be read, the pathway select is disabled
  // with the reason shown rather than offering invented options.
  const opsState = useAsync(
    () =>
      typeof client.effectiveConfig === "function"
        ? client.effectiveConfig("terminology", "continuum-ops")
        : Promise.resolve(null),
    [client],
  );
  const pathwayOptions: Array<{ code: string; display: string }> = (() => {
    const raw = (opsState.data?.payload as { activationPathways?: unknown } | undefined)
      ?.activationPathways;
    if (!Array.isArray(raw)) return [];
    return raw
      .filter(
        (p): p is { code: string; display?: string } =>
          typeof p === "object" && p !== null &&
          typeof (p as { code?: unknown }).code === "string",
      )
      .map((p) => ({ code: p.code, display: p.display ?? p.code }));
  })();
  const defaultPathway = pathwayOptions[0]?.code ?? "";

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

  function renderActivation(a: Activation) {
    return (
      <li key={a.id} className="board-card">
        <div>
          <strong>{a.pathway_display}</strong>
          <span className="badge">{refText(a.patient_ref)}</span>
          <span className="badge">elapsed {a.elapsed_minutes} min</span>
          <span className="badge">target {a.target_minutes} min</span>
          {a.target_breached && <StatusBadge tone="bad">TARGET BREACHED</StatusBadge>}
        </div>
        <ul>
          {a.milestones.map((m) => (
            <li key={m.code}>
              {m.display} — target {m.target_minutes} min
              {m.recorded_at
                ? ` · done at ${m.elapsed_minutes} min`
                : m.breached
                  ? " · BREACHED"
                  : " · pending"}
              {canWork && !m.recorded_at && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    perform(`Milestone ${m.display}`, () =>
                      client.recordActivationMilestone(a.id, m.code))}
                >
                  Record
                </button>
              )}
            </li>
          ))}
        </ul>
        {canWork && (
          <div className="row-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() => perform("Activation closed", () => client.closeActivation(a.id))}
            >
              Close activation
            </button>
          </div>
        )}
      </li>
    );
  }

  return (
    <section>
      <ModuleHeader
        icon="activity"
        group="Operations"
        title="ER operations"
        purpose="ED operational targets: split-flow queues, activation pathways and results callbacks."
      />
      <p className="hint">
        Door-to-provider targets per ESI level, fast-track split,
        activation pathways and the callback SLA all come from the
        published terminology/continuum-ops config. Breaches are flagged
        by the server, never estimated in the browser.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <div className="state state-error" role="alert">{actionError}</div>}
      {!opsState.loading && pathwayOptions.length === 0 && (
        <p className="hint" role="status">
          Activation pathways could not be read from the published terminology/continuum-ops
          config, so starting an activation is unavailable — publish the config in System
          Studio to enable it.
        </p>
      )}

      {boardState.loading && <Loading label="Loading ER operations…" />}
      {boardState.error != null && <ErrorView error={boardState.error} />}
      {board && (
        <>
          <div className="board-card" role="status">
            Diversion: <strong>{board.diversion}</strong> · Surge:{" "}
            <strong>{board.surge_level}</strong> · In department:{" "}
            <strong>{board.counts.total}</strong> · Target breaches:{" "}
            <strong>{board.counts.target_breaches}</strong>
          </div>
          {(["fast_track", "main"] as const).map((queue) => (
            <div key={queue}>
              <h3>{queue === "fast_track" ? "Fast track" : "Main ED"} ({board.queues[queue].length})</h3>
              {board.queues[queue].length === 0 && (
                <Empty label="Nobody in this queue." />
              )}
              <ul className="board-list">
                {board.queues[queue].map((entry) => {
                  const id = entry.encounter.id as string;
                  return (
                    <li key={id} className="board-card">
                      <div>
                        <strong>{refText(entry.patient_ref)}</strong>
                        {entry.acuity != null && <StatusBadge tone={esiTone(entry.acuity)}>ESI {entry.acuity}</StatusBadge>}
                        <span className="badge">wait {entry.wait_minutes} min</span>
                        {entry.target_minutes != null && (
                          <span className="badge">target {entry.target_minutes} min</span>
                        )}
                        {entry.target_breached && <StatusBadge tone="bad">TARGET BREACHED</StatusBadge>}
                      </div>
                      <div className="hint">
                        {entry.chief_complaint ?? "—"} · {entry.state}
                      </div>
                      {canWork && (
                        <div className="row-actions">
                          <select
                            aria-label={`Activation pathway for ${id}`}
                            value={pathways[id] ?? defaultPathway}
                            disabled={pathwayOptions.length === 0}
                            onChange={(e) =>
                              setPathways((prev) => ({ ...prev, [id]: e.target.value }))}
                          >
                            {pathwayOptions.map((p) => (
                              <option key={p.code} value={p.code}>{p.display}</option>
                            ))}
                          </select>
                          <button
                            type="button"
                            disabled={busy || pathwayOptions.length === 0}
                            onClick={() =>
                              perform("Activation started", () =>
                                client.startActivation(id, pathways[id] ?? defaultPathway))}
                          >
                            Start activation
                          </button>
                          {(entry.state === "waiting" || entry.state === "triage") && (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() =>
                                perform("LWBS recorded", () => client.markLwbs(id))}
                            >
                              LWBS
                            </button>
                          )}
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </>
      )}

      <h3>Active activations</h3>
      {activationsState.data && activationsState.data.length === 0 && (
        <Empty label="No open activations." />
      )}
      <ul className="board-list">{activationsState.data?.map(renderActivation)}</ul>

      <h3>Results callbacks ({callbacksState.data?.total ?? 0})</h3>
      <p className="hint">
        Departed ED patients whose results are still pending; the SLA
        comes from the published inbox/callback configuration.
      </p>
      {callbacksState.data && callbacksState.data.total === 0 && (
        <Empty label="No pending callbacks." />
      )}
      <ul className="board-list">
        {callbacksState.data?.items.map((item) => (
          <li key={item.report_id} className="board-card">
            <div>
              <strong>{refText(item.patient_ref)}</strong>
              <span className="badge">{item.code_text ?? item.report_id}</span>
              <span className="badge">pending {item.age_minutes} min</span>
              {item.breached && <StatusBadge tone="bad">SLA BREACHED</StatusBadge>}
            </div>
            {canWork && (
              <div className="row-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    perform("Callback completed", () =>
                      client.completeCallback(item.report_id, "patient-reached"))}
                >
                  Mark called back
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
