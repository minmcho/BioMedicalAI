import { useCallback, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

// Lab Depth W5 (LAB-D10 + LAB-D11) — TAT & Reflex surface.
//
// - Per-bench TAT board (app.lab_tat): approaching-breach and
//   breached worklists computed from the GOVERNED per-test targets
//   on the published lab directory (terminology/lab-catalog
//   directory.tatTargets). Every entry DECLARES its clock —
//   order → result and receipt → result are computed and labelled
//   separately, never blended. Tests with no published target for
//   an order's priority are listed as UNMONITORED, by name.
// - Escalation log: bench -> supervisor -> lab manager firings
//   (one per level per order/report), each with its notification
//   Task. "Run escalation scan" fires every due level now.
// - Critical-value chase list: released critical reports whose
//   notification (notified_to + read-back, recorded on the lab
//   critical worklist) has not landed, timed from RELEASE against
//   the governed notify window. The chase stops the moment the
//   notification is recorded.
// - Reflex activity: every same-specimen reflex firing with its
//   trigger evidence, the specimen consumed and its volume, the
//   reflex-billing flag and cascade step — plus named refusals
//   (insufficient volume) with their recollect action, and the
//   begin-bench-work action that moves a fired add-on to received
//   against its existing specimen.
//
// Governed figures (warning fraction, escalation minutes, notify
// window) are Studio config terminology/lab-tat — the header shows
// the published values and their governed-defaults label.

export interface TatEntry {
  order_id: string;
  code: string;
  display?: string;
  bench: string;
  priority: string;
  order_status: string;
  clock: string;
  clock_label: string;
  target_minutes: number;
  start_at?: string | null;
  due_at?: string | null;
  elapsed_minutes?: number | null;
  fraction?: number | null;
  minutes_past_breach?: number | null;
  state: string;
  specimen_id?: string | null;
}

export interface TatBench {
  bench: string;
  approaching: TatEntry[];
  breached: TatEntry[];
  awaiting_receipt: TatEntry[];
  within_count: number;
}

export interface TatBoard {
  generated_at: string;
  warning_fraction: number;
  clocks: string[];
  benches: TatBench[];
  unmonitored: Array<{
    order_id: string; code: string; bench: string; priority: string;
    monitored: boolean; reason: string;
  }>;
  unmonitored_count: number;
}

export interface CriticalChaseItem {
  report_id: string;
  order_id: string;
  bench: string;
  code?: string | null;
  released_at?: string | null;
  notify_window_minutes: number;
  elapsed_minutes?: number | null;
  minutes_past_window: number;
  chase_state: string;
  notification: { state: string; notified_to?: string | null; notified_at?: string | null };
}

export interface EscalationItem {
  escalation_id: string;
  kind: string;
  subject_key: string;
  bench_code?: string | null;
  level: number;
  level_name: string;
  target_role: string;
  target_staff_id?: string | null;
  clock?: string | null;
  task_id?: string | null;
  created_at?: string | null;
  detail?: { summary?: string };
}

export interface ReflexFiring {
  firing_id: string;
  rule_id: string;
  source_order_id: string;
  trigger_code: string;
  trigger_value?: string | null;
  trigger_kind: string;
  add_code: string;
  status: string;
  refusal_reason?: string | null;
  specimen_key?: string | null;
  required_volume_ml?: number | null;
  remaining_before_ml?: number | null;
  addon_order_id?: string | null;
  reflex_billing: boolean;
  cascade_step: number;
  parent_firing_id?: string | null;
  recollect_task_id?: string | null;
}

export interface TatConfig {
  config: { kind: string; key: string; version?: number | null; publish_in: string };
  warning_fraction: number;
  escalation: { supervisorAfterBreachMinutes?: number; managerAfterBreachMinutes?: number; labManagerDisplay?: string };
  critical_notify: { windowMinutes?: number; supervisorAfterBreachMinutes?: number; managerAfterBreachMinutes?: number };
  governed_defaults_label?: string;
}

function errText(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  return err instanceof Error ? err.message : String(err);
}

function fmt(value: number | null | undefined): string {
  return value == null ? "—" : `${value} min`;
}

export function LabTat({ client }: { client: ApiClient }) {
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);
  const config = useAsync(
    () => client.request<TatConfig>("/lab/tat/config"),
    [client, refreshKey],
  );
  const board = useAsync(
    () => client.request<TatBoard>("/lab/tat/board"),
    [client, refreshKey],
  );
  const chase = useAsync(
    () => client.request<{ items: CriticalChaseItem[]; total: number; unnotified: number }>("/lab/tat/critical-chase"),
    [client, refreshKey],
  );
  const escalations = useAsync(
    () => client.request<{ items: EscalationItem[]; total: number }>("/lab/tat/escalations"),
    [client, refreshKey],
  );
  const reflex = useAsync(
    () => client.request<{ items: ReflexFiring[]; total: number; refused: number }>("/lab/tat/reflex"),
    [client, refreshKey],
  );
  const [scanBusy, setScanBusy] = useState(false);
  const [scanNote, setScanNote] = useState<string | null>(null);
  const [scanError, setScanError] = useState<string | null>(null);

  const runScan = async () => {
    setScanBusy(true); setScanError(null); setScanNote(null);
    try {
      const out = await client.request<{ fired_count: number }>("/lab/tat/scan", {
        method: "POST", body: "{}",
      });
      setScanNote(`Escalation scan complete: ${out.fired_count} new escalation(s) fired.`);
      refresh();
    } catch (err) {
      setScanError(errText(err));
    } finally {
      setScanBusy(false);
    }
  };

  const cfg = config.data;
  return (
    <div className="lab-tat">
      <h2>TAT, critical-value chase &amp; reflex</h2>
      {config.loading && <Loading label="Loading TAT configuration" />}
      {config.error != null && <ErrorView error={config.error} />}
      {cfg && (
        <p role="note">
          Governed by Studio config {cfg.config.kind}/{cfg.config.key}
          {cfg.config.version != null ? ` v${cfg.config.version}` : ""} (publish in {cfg.config.publish_in}).
          Warning at {Math.round(cfg.warning_fraction * 100)}% of target; critical notify window{" "}
          {cfg.critical_notify.windowMinutes ?? "—"} min; TAT escalation to supervisor at breach +
          {cfg.escalation.supervisorAfterBreachMinutes ?? "—"} min, lab manager at breach +
          {cfg.escalation.managerAfterBreachMinutes ?? "—"} min.
          {cfg.governed_defaults_label ? ` ${cfg.governed_defaults_label}` : ""}
        </p>
      )}
      <p>
        <button type="button" onClick={() => void runScan()} disabled={scanBusy}>
          {scanBusy ? "Scanning…" : "Run escalation scan"}
        </button>
        {scanNote && <span role="status"> {scanNote}</span>}
        {scanError && <span role="alert"> {scanError}</span>}
      </p>

      <BoardSection board={board} />
      <ChaseSection chase={chase} />
      <EscalationSection escalations={escalations} />
      <ReflexSection client={client} reflex={reflex} onChanged={refresh} />
    </div>
  );
}

type Async<T> = ReturnType<typeof useAsync<T>>;

function BoardSection({ board }: { board: Async<TatBoard> }) {
  if (board.loading) return <Loading label="Loading TAT board" />;
  if (board.error) return <ErrorView error={board.error} />;
  const data = board.data;
  if (!data) return null;
  return (
    <section aria-label="TAT board">
      <h3>TAT board — declared clocks, per bench</h3>
      <p>
        Targets are the published directory&apos;s per-test / per-priority targets. Every entry declares
        its clock: <strong>order → result</strong> and <strong>receipt → result</strong> are computed
        separately and never blended.
      </p>
      {data.benches.length === 0 && <Empty label="No open lab orders" />}
      {data.benches.map((bench) => (
        <div key={bench.bench}>
          <h4>
            {bench.bench}: {bench.breached.length} breached, {bench.approaching.length} approaching
            breach, {bench.within_count} within target
          </h4>
          <TatTable entries={bench.breached} emptyLabel="No breached work on this bench" />
          <TatTable entries={bench.approaching} emptyLabel="Nothing approaching breach on this bench" />
        </div>
      ))}
      <h4>Unmonitored ({data.unmonitored_count})</h4>
      {data.unmonitored.length === 0
        ? <Empty label="Every open order has a published TAT target" />
        : (
          <ul>
            {data.unmonitored.map((u) => (
              <li key={u.order_id}>
                Order {u.order_id} ({u.code}, {u.priority}) — unmonitored: {u.reason}
              </li>
            ))}
          </ul>
        )}
    </section>
  );
}

function TatTable({ entries, emptyLabel }: { entries: TatEntry[]; emptyLabel: string }) {
  if (entries.length === 0) return <Empty label={emptyLabel} />;
  return (
    <table>
      <thead>
        <tr>
          <th>Order</th><th>Test</th><th>Priority</th><th>Declared clock</th>
          <th>Target</th><th>Elapsed</th><th>Due</th><th>State</th>
        </tr>
      </thead>
      <tbody>
        {entries.map((e) => (
          <tr key={`${e.order_id}-${e.clock}`}>
            <td>{e.order_id}</td>
            <td>{e.display ?? e.code} ({e.code})</td>
            <td>{e.priority}</td>
            <td>{e.clock_label} ({e.clock})</td>
            <td>{fmt(e.target_minutes)}</td>
            <td>{fmt(e.elapsed_minutes)}</td>
            <td>{e.due_at ?? "—"}</td>
            <td>{e.state}{e.state === "breached" ? ` (+${e.minutes_past_breach} min)` : ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ChaseSection({ chase }: { chase: Async<{ items: CriticalChaseItem[]; total: number; unnotified: number }> }) {
  if (chase.loading) return <Loading label="Loading critical chase" />;
  if (chase.error) return <ErrorView error={chase.error} />;
  const data = chase.data;
  if (!data) return null;
  return (
    <section aria-label="Critical-value chase">
      <h3>Critical-value chase — un-notified criticals</h3>
      <p>
        The clock starts at release. Notification is recorded on the lab critical worklist with
        read-back; the chase stops the moment it lands. {data.unnotified} un-notified of {data.total}.
      </p>
      {data.items.length === 0
        ? <Empty label="No released critical reports" />
        : (
          <table>
            <thead>
              <tr>
                <th>Report</th><th>Bench</th><th>Released</th><th>Elapsed</th>
                <th>Notify window</th><th>Chase state</th><th>Notification</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((i) => (
                <tr key={i.report_id}>
                  <td>{i.report_id}</td>
                  <td>{i.bench}</td>
                  <td>{i.released_at ?? "—"}</td>
                  <td>{fmt(i.elapsed_minutes)}</td>
                  <td>{fmt(i.notify_window_minutes)}</td>
                  <td>{i.chase_state}{i.chase_state === "breached" ? ` (+${i.minutes_past_window} min past window)` : ""}</td>
                  <td>{i.notification.state}{i.notification.notified_to ? ` — ${i.notification.notified_to}` : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
    </section>
  );
}

function EscalationSection({ escalations }: { escalations: Async<{ items: EscalationItem[]; total: number }> }) {
  if (escalations.loading) return <Loading label="Loading escalation log" />;
  if (escalations.error) return <ErrorView error={escalations.error} />;
  const data = escalations.data;
  if (!data) return null;
  return (
    <section aria-label="Escalation log">
      <h3>Escalation log — bench → supervisor → lab manager</h3>
      {data.items.length === 0
        ? <Empty label="No escalations fired" />
        : (
          <table>
            <thead>
              <tr>
                <th>When</th><th>Kind</th><th>Subject</th><th>Bench</th>
                <th>Level</th><th>Notified</th><th>Clock</th><th>Task</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((i) => (
                <tr key={i.escalation_id}>
                  <td>{i.created_at ?? "—"}</td>
                  <td>{i.kind}</td>
                  <td>{i.subject_key}</td>
                  <td>{i.bench_code ?? "—"}</td>
                  <td>Level {i.level} — {i.level_name}</td>
                  <td>{i.target_role}{i.target_staff_id ? ` (${i.target_staff_id})` : ""}</td>
                  <td>{i.clock ?? "—"}</td>
                  <td>{i.task_id ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
    </section>
  );
}

function ReflexSection({
  client, reflex, onChanged,
}: {
  client: ApiClient;
  reflex: Async<{ items: ReflexFiring[]; total: number; refused: number }>;
  onChanged: () => void;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const act = async (firing: ReflexFiring, action: "begin" | "request-recollect") => {
    setBusyId(firing.firing_id); setError(null);
    try {
      await client.request(`/lab/tat/reflex-firings/${firing.firing_id}/${action}`, {
        method: "POST", body: "{}",
      });
      onChanged();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusyId(null);
    }
  };

  if (reflex.loading) return <Loading label="Loading reflex activity" />;
  if (reflex.error) return <ErrorView error={reflex.error} />;
  const data = reflex.data;
  if (!data) return null;
  return (
    <section aria-label="Reflex activity">
      <h3>Reflex activity — same-specimen add-ons</h3>
      <p>
        A fired rule runs its add-on against the source order&apos;s existing specimen; volume is
        consumed through the aliquot machinery. Refusals are named on the firing and carry a
        recollect path — a fresh order is never minted silently.
      </p>
      {error && <p role="alert">{error}</p>}
      {data.items.length === 0
        ? <Empty label="No reflex firings yet" />
        : (
          <table>
            <thead>
              <tr>
                <th>Rule</th><th>Trigger</th><th>Add-on</th><th>Specimen consumed</th>
                <th>Volume</th><th>Billing</th><th>Cascade</th><th>Status</th><th>Action</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((f) => (
                <tr key={f.firing_id}>
                  <td>{f.rule_id}</td>
                  <td>{f.trigger_code} = {f.trigger_value ?? "—"} ({f.trigger_kind})</td>
                  <td>{f.add_code}{f.addon_order_id ? ` — order ${f.addon_order_id}` : ""}</td>
                  <td>{f.specimen_key ?? "—"}</td>
                  <td>{f.required_volume_ml != null ? `${f.required_volume_ml} mL` : "—"}</td>
                  <td>{f.reflex_billing ? "reflex-billable" : "not billed (refused)"}</td>
                  <td>step {f.cascade_step}{f.parent_firing_id ? ` (after ${f.parent_firing_id})` : ""}</td>
                  <td>{f.status}{f.refusal_reason ? ` — ${f.refusal_reason}` : ""}</td>
                  <td>
                    {f.status === "fired" && (
                      <button type="button" disabled={busyId === f.firing_id}
                        onClick={() => void act(f, "begin")}>
                        Begin bench work
                      </button>
                    )}
                    {f.status.startsWith("refused") && (
                      <button type="button" disabled={busyId === f.firing_id}
                        onClick={() => void act(f, "request-recollect")}>
                        Request recollect
                      </button>
                    )}
                    {f.status === "recollect-requested" && (
                      <span>recollect task {f.recollect_task_id ?? ""}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
    </section>
  );
}
