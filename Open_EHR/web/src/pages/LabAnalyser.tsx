import { useCallback, useEffect, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

// Lab Depth W3 (LAB-D6 + LAB-D7) — Analyser & Autoverification surface.
//
// - Instruments: the RECONCILED registry view (app.lab_analyser) —
//   lab registry + device registry statuses side by side, the
//   effective status ingest uses, divergence flags, and the
//   write-through status surface (one change updates both
//   registries; it can never create a divergence).
// - Quarantine: batches refused under LAB-m2 (instrument in
//   maintenance / out of service on ANY ingest path), dispositioned
//   reject (terminal) or reprocess (only once the instrument is back
//   in service; reprocessing creates new drafts, never rewrites the
//   held record).
// - Worklist: the governed per-analyser download (open orders on
//   the instrument's bench + its pending reruns).
// - Rerun queue: flagged results awaiting rerun/dilution output;
//   submitting re-enters the same validation core server-side.
// - Autoverification (app.lab_autoverify): per-bench rule-set
//   status (unpublished / published-disabled / live — seeded OFF),
//   the hold queue with full rule traces, and the per-bench
//   autoverification-rate panel.
//
// Honest bound, also stated by the backend: worklist + flags +
// rerun over the existing HL7/CSV/MLLP pipes — parity with
// commercial middleware (Data Innovations class) is NOT claimed.

export interface AnalyserInstrument {
  instrument_id: string;
  name?: string | null;
  bench?: string | null;
  in_lab_registry: boolean;
  in_device_registry: boolean;
  lab_status?: string | null;
  device_status?: string | null;
  effective_status?: string | null;
  divergent: boolean;
  ingest_blocked: boolean;
  block_reason?: string | null;
}

export interface QuarantineItem {
  quarantine_id: string;
  instrument_id: string;
  source_format: string;
  reason: string;
  effective_status: string;
  status: string;
  payload: { kind?: string; order_id?: string; groups?: unknown[] };
  decided_by?: string | null;
  reprocessed_report_ids: string[];
  created_at?: string | null;
}

export interface RerunItem {
  item_id: string;
  order_id: string;
  analyte_code: string;
  instrument_id?: string | null;
  flags: string[];
  status: string;
}

export interface BenchAvStatus {
  bench: string;
  state: string;
  rule_set_version?: number | null;
  rule_count: number;
  enabled: boolean;
}

export interface AvHold {
  id: number;
  report_id: string;
  order_id: string;
  bench: string;
  status: string;
  hold_reasons: string[];
  trace: Array<{ rule_id: string; type: string; outcome: string; reason: string }>;
}

export interface AvRate {
  bench: string;
  released: number;
  held: number;
  total: number;
  rate: number | null;
}

interface Worklist {
  instrument_id: string;
  bench?: string | null;
  note: string;
  items: Array<{ order_id: string; code?: string | null; status: string; patient_ref?: string; accession?: string | null }>;
  rerun_items: RerunItem[];
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

const HONEST_BOUND =
  "Worklist + flags + rerun over the existing HL7/CSV/MLLP pipes. Parity with commercial " +
  "middleware (Data Innovations class) is not claimed. Autoverification is deterministic " +
  "governed rules — not an AI agent — and a bench auto-releases only under its own " +
  "published rule set (seeded OFF).";

export function LabAnalyser({ client }: { client: ApiClient }) {
  const instruments = useAsync(
    () => client.request<{ items: AnalyserInstrument[]; divergent: number; ingest_blocked: number }>("/lab/analyser/instruments"),
    [client],
  );
  const quarantine = useAsync(
    () => client.request<{ items: QuarantineItem[]; total: number; held: number }>("/lab/analyser/quarantine"),
    [client],
  );
  const reruns = useAsync(
    () => client.request<{ items: RerunItem[]; total: number }>("/lab/analyser/rerun-queue"),
    [client],
  );
  const avStatus = useAsync(
    () => client.request<{ benches: BenchAvStatus[] }>("/lab/autoverify/status"),
    [client],
  );
  const holds = useAsync(
    () => client.request<{ items: AvHold[]; total: number }>("/lab/autoverify/holds"),
    [client],
  );
  const rates = useAsync(
    () => client.request<{ items: AvRate[]; window_days: number }>("/lab/autoverify/rates?days=30"),
    [client],
  );

  const instrumentList = instruments.data?.items ?? [];
  const reloadAll = useCallback(() => {
    instruments.reload(); quarantine.reload(); reruns.reload();
    avStatus.reload(); holds.reload(); rates.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  return (
    <div className="lab-analyser">
      <h2>Analysers &amp; Autoverification</h2>
      <p role="note">{HONEST_BOUND}</p>
      <InstrumentsSection client={client} data={instruments} onChanged={reloadAll} />
      <QuarantineSection client={client} data={quarantine} instruments={instrumentList} onChanged={reloadAll} />
      <WorklistSection client={client} instruments={instrumentList} />
      <RerunSection client={client} data={reruns} onChanged={reloadAll} />
      <AutoverifySection client={client} status={avStatus} holds={holds} rates={rates} onChanged={reloadAll} />
    </div>
  );
}

function InstrumentsSection({
  client, data, onChanged,
}: {
  client: ApiClient;
  data: ReturnType<typeof useAsync<{ items: AnalyserInstrument[]; divergent: number; ingest_blocked: number }>>;
  onChanged: () => void;
}) {
  const [selected, setSelected] = useState("");
  const [newStatus, setNewStatus] = useState("in-service");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const items = data.data?.items ?? [];

  const apply = async () => {
    if (!selected) return;
    setBusy(true); setError(null);
    try {
      await client.request(`/lab/analyser/instruments/${encodeURIComponent(selected)}/status`, {
        method: "POST", body: JSON.stringify({ status: newStatus }),
      });
      onChanged();
    } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  };

  return (
    <section aria-label="Instruments and registry reconciliation">
      <h3>Instruments — reconciled registries</h3>
      <p>
        Effective status is the most restrictive normalised status across the lab registry and the
        device registry; ingest reads only the effective status.
        {data.data ? ` ${data.data.divergent} divergent, ${data.data.ingest_blocked} ingest-blocked.` : ""}
      </p>
      {data.loading && <Loading label="Loading instruments…" />}
      {data.error != null && <ErrorView error={data.error} />}
      {data.data && items.length === 0 && <Empty label="No instruments in either registry." />}
      <table>
        <thead>
          <tr><th>Instrument</th><th>Bench</th><th>Lab registry</th><th>Device registry</th><th>Effective</th><th>Ingest</th></tr>
        </thead>
        <tbody>
          {items.map((inst) => (
            <tr key={inst.instrument_id}>
              <td>{inst.instrument_id}{inst.name ? ` — ${inst.name}` : ""}</td>
              <td>{inst.bench ?? "—"}</td>
              <td>{inst.in_lab_registry ? inst.lab_status : "not registered"}</td>
              <td>{inst.in_device_registry ? inst.device_status : "not registered"}</td>
              <td>
                {inst.effective_status ?? "unknown"}
                {inst.divergent ? " (DIVERGENT — registries disagree)" : ""}
              </td>
              <td>{inst.ingest_blocked ? `BLOCKED — ${inst.block_reason ?? "instrument not in service"}` : "open"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <form
        aria-label="Set instrument status"
        onSubmit={(e) => { e.preventDefault(); void apply(); }}
      >
        <label>
          Instrument{" "}
          <select aria-label="Status instrument" value={selected} onChange={(e) => setSelected(e.target.value)} disabled={items.length === 0}>
            <option value="">{items.length === 0 ? "No instruments registered" : "Select an instrument"}</option>
            {items.map((i) => <option key={i.instrument_id} value={i.instrument_id}>{i.instrument_id}</option>)}
          </select>
        </label>
        <label>
          New status{" "}
          <select aria-label="New instrument status" value={newStatus} onChange={(e) => setNewStatus(e.target.value)}>
            <option value="in-service">in-service</option>
            <option value="in-maintenance">in-maintenance</option>
            <option value="out-of-service">out-of-service</option>
          </select>
        </label>
        <button type="submit" disabled={busy || !selected}>Set status (writes both registries)</button>
        {!selected && <span> Select an instrument first.</span>}
      </form>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}

function QuarantineSection({
  client, data, instruments, onChanged,
}: {
  client: ApiClient;
  data: ReturnType<typeof useAsync<{ items: QuarantineItem[]; total: number; held: number }>>;
  instruments: AnalyserInstrument[];
  onChanged: () => void;
}) {
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const items = data.data?.items ?? [];

  const act = async (qid: string, action: "reject" | "reprocess") => {
    setBusy(true); setError(null);
    try {
      await client.request(`/lab/analyser/quarantine/${encodeURIComponent(qid)}/${action}`, {
        method: "POST", body: JSON.stringify({ note: notes[qid] ?? "" }),
      });
      onChanged();
    } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  };

  const stillBlocked = (item: QuarantineItem): boolean => {
    const inst = instruments.find((i) => i.instrument_id === item.instrument_id);
    return inst ? inst.ingest_blocked : item.effective_status !== "in-service";
  };

  return (
    <section aria-label="Ingest quarantine">
      <h3>Ingest quarantine (LAB-m2)</h3>
      <p>Batches refused because the instrument was in maintenance or out of service. Nothing was written for a held batch.</p>
      {data.loading && <Loading label="Loading quarantine…" />}
      {data.error != null && <ErrorView error={data.error} />}
      {data.data && items.length === 0 && <Empty label="No quarantined batches." />}
      <ul>
        {items.map((item) => (
          <li key={item.quarantine_id}>
            <strong>{item.quarantine_id}</strong> — {item.instrument_id} via {item.source_format} —{" "}
            {item.status} — {item.reason}
            {item.status === "reprocessed" && ` — new draft report(s): ${item.reprocessed_report_ids.join(", ")}`}
            {item.status === "held" && (
              <span>
                <label>
                  Disposition note{" "}
                  <input
                    aria-label={`Note for ${item.quarantine_id}`}
                    value={notes[item.quarantine_id] ?? ""}
                    onChange={(e) => setNotes({ ...notes, [item.quarantine_id]: e.target.value })}
                  />
                </label>
                <button type="button" disabled={busy || !(notes[item.quarantine_id] ?? "").trim()} onClick={() => void act(item.quarantine_id, "reject")}>
                  Reject batch
                </button>
                {!(notes[item.quarantine_id] ?? "").trim() && <span> A note is required to reject.</span>}
                <button type="button" disabled={busy || stillBlocked(item)} onClick={() => void act(item.quarantine_id, "reprocess")}>
                  Reprocess batch
                </button>
                {stillBlocked(item) && (
                  <span> Reprocessing is disabled: the instrument is still not in service — return it to service first. Reprocessing creates new drafts; it never rewrites this record.</span>
                )}
              </span>
            )}
          </li>
        ))}
      </ul>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}

function WorklistSection({
  client, instruments,
}: {
  client: ApiClient;
  instruments: AnalyserInstrument[];
}) {
  const [selected, setSelected] = useState("");
  const [worklist, setWorklist] = useState<Worklist | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!selected) { setWorklist(null); return; }
    setError(null);
    try {
      setWorklist(await client.request<Worklist>(`/lab/analyser/worklist/${encodeURIComponent(selected)}`));
    } catch (err) { setError(errorText(err)); }
  }, [client, selected]);

  useEffect(() => { void load(); }, [load]);

  return (
    <section aria-label="Analyser worklist">
      <h3>Worklist download per analyser</h3>
      <label>
        Instrument{" "}
        <select aria-label="Worklist instrument" value={selected} onChange={(e) => setSelected(e.target.value)} disabled={instruments.length === 0}>
          <option value="">{instruments.length === 0 ? "No instruments registered" : "Select an instrument"}</option>
          {instruments.map((i) => <option key={i.instrument_id} value={i.instrument_id}>{i.instrument_id}</option>)}
        </select>
      </label>
      {error && <p role="alert">{error}</p>}
      {worklist && (
        <div>
          <p>{worklist.note}</p>
          {worklist.items.length === 0 && <Empty label="No pending orders for this analyser." />}
          <ul>
            {worklist.items.map((item) => (
              <li key={item.order_id}>
                {item.order_id} — {item.code ?? ""} — {item.status}
                {item.patient_ref ? ` — ${item.patient_ref}` : ""}
                {item.accession ? ` — accession ${item.accession}` : ""}
              </li>
            ))}
          </ul>
          {worklist.rerun_items.length > 0 && (
            <p>Pending reruns for this analyser: {worklist.rerun_items.map((r) => `${r.analyte_code} (${r.order_id})`).join(", ")}</p>
          )}
        </div>
      )}
    </section>
  );
}

function RerunSection({
  client, data, onChanged,
}: {
  client: ApiClient;
  data: ReturnType<typeof useAsync<{ items: RerunItem[]; total: number }>>;
  onChanged: () => void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [factors, setFactors] = useState<Record<string, string>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const items = (data.data?.items ?? []).filter((i) => i.status === "pending");

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); onChanged(); }
    catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  };

  return (
    <section aria-label="Rerun queue">
      <h3>Rerun / dilution queue</h3>
      <p>Flagged results awaiting rerun output. Submitting re-enters the same validation core (QC gate, instrument-path lock, delta + autoverification) — a blocked rerun stays queued.</p>
      {data.loading && <Loading label="Loading rerun queue…" />}
      {data.error != null && <ErrorView error={data.error} />}
      {data.data && items.length === 0 && <Empty label="No pending rerun items." />}
      <ul>
        {items.map((item) => (
          <li key={item.item_id}>
            {item.item_id} — order {item.order_id} — {item.analyte_code} — flags {item.flags.join(", ") || "—"}
            {item.instrument_id ? ` — ${item.instrument_id}` : ""}
            <label>
              Rerun measured value{" "}
              <input
                aria-label={`Rerun value for ${item.item_id}`}
                value={values[item.item_id] ?? ""}
                onChange={(e) => setValues({ ...values, [item.item_id]: e.target.value })}
              />
            </label>
            <label>
              Dilution factor{" "}
              <input
                aria-label={`Dilution factor for ${item.item_id}`}
                value={factors[item.item_id] ?? ""}
                onChange={(e) => setFactors({ ...factors, [item.item_id]: e.target.value })}
              />
            </label>
            <button
              type="button"
              disabled={busy || !(values[item.item_id] ?? "").trim()}
              onClick={() => void act(() => client.request(`/lab/analyser/rerun/${encodeURIComponent(item.item_id)}/submit`, {
                method: "POST",
                body: JSON.stringify({
                  value: Number(values[item.item_id]),
                  dilution_factor: (factors[item.item_id] ?? "").trim() ? Number(factors[item.item_id]) : undefined,
                }),
              }))}
            >
              Submit rerun
            </button>
            {!(values[item.item_id] ?? "").trim() && <span> Enter the measured rerun value first.</span>}
            <label>
              Cancel note{" "}
              <input
                aria-label={`Cancel note for ${item.item_id}`}
                value={notes[item.item_id] ?? ""}
                onChange={(e) => setNotes({ ...notes, [item.item_id]: e.target.value })}
              />
            </label>
            <button
              type="button"
              disabled={busy || !(notes[item.item_id] ?? "").trim()}
              onClick={() => void act(() => client.request(`/lab/analyser/rerun/${encodeURIComponent(item.item_id)}/cancel`, {
                method: "POST", body: JSON.stringify({ note: notes[item.item_id] }),
              }))}
            >
              Cancel rerun
            </button>
            {!(notes[item.item_id] ?? "").trim() && <span> A note is required to cancel.</span>}
          </li>
        ))}
      </ul>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}

function AutoverifySection({
  client, status, holds, rates, onChanged,
}: {
  client: ApiClient;
  status: ReturnType<typeof useAsync<{ benches: BenchAvStatus[] }>>;
  holds: ReturnType<typeof useAsync<{ items: AvHold[]; total: number }>>;
  rates: ReturnType<typeof useAsync<{ items: AvRate[]; window_days: number }>>;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const benches = status.data?.benches ?? [];
  const holdItems = holds.data?.items ?? [];
  const rateItems = rates.data?.items ?? [];

  const acknowledge = async (id: number) => {
    setBusy(true); setError(null);
    try {
      await client.request(`/lab/autoverify/holds/${id}/acknowledge`, {
        method: "POST", body: JSON.stringify({ note: "Reviewed on the analyser surface" }),
      });
      onChanged();
    } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  };

  return (
    <section aria-label="Autoverification">
      <h3>Autoverification rule sets &amp; holds (LAB-D7)</h3>
      {status.loading && <Loading label="Loading rule-set status…" />}
      {status.error != null && <ErrorView error={status.error} />}
      <table>
        <thead>
          <tr><th>Bench</th><th>Rule set</th><th>Version</th><th>Rules</th></tr>
        </thead>
        <tbody>
          {benches.map((b) => (
            <tr key={b.bench}>
              <td>{b.bench}</td>
              <td>{b.state === "live" ? "LIVE — auto-release under rules" : b.state === "published-disabled" ? "Published but disabled — nothing auto-releases" : "Unpublished — nothing auto-releases"}</td>
              <td>{b.rule_set_version ?? "—"}</td>
              <td>{b.rule_count}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {status.data && benches.length === 0 && <Empty label="No benches in the published registry." />}
      <p>Publish or enable a bench&apos;s rule set in System Studio → Terminology → lab-autoverify.</p>

      <h4>Hold queue</h4>
      {holds.loading && <Loading label="Loading holds…" />}
      {holds.error != null && <ErrorView error={holds.error} />}
      {holds.data && holdItems.length === 0 && <Empty label="No autoverification holds." />}
      <ul>
        {holdItems.map((hold) => (
          <li key={hold.id}>
            Hold #{hold.id} — {hold.bench} — order {hold.order_id} — report {hold.report_id} — {hold.status}
            <ul>{hold.hold_reasons.map((r) => <li key={r}>{r}</li>)}</ul>
            <details>
              <summary>Rule trace</summary>
              <ul>
                {hold.trace.map((t, i) => (
                  <li key={i}>{t.rule_id} ({t.type}): {t.outcome} — {t.reason}</li>
                ))}
              </ul>
            </details>
            <button type="button" disabled={busy || hold.status === "acknowledged"} onClick={() => void acknowledge(hold.id)}>
              Acknowledge hold
            </button>
            {hold.status === "acknowledged" && <span> Acknowledged — the draft proceeds through the manual verify/release path.</span>}
          </li>
        ))}
      </ul>

      <h4>Autoverification rate (last {rates.data?.window_days ?? 30} days)</h4>
      {rates.loading && <Loading label="Loading rates…" />}
      {rates.error != null && <ErrorView error={rates.error} />}
      <table>
        <thead>
          <tr><th>Bench</th><th>Released</th><th>Held</th><th>Total</th><th>Rate</th></tr>
        </thead>
        <tbody>
          {rateItems.map((r) => (
            <tr key={r.bench}>
              <td>{r.bench}</td><td>{r.released}</td><td>{r.held}</td><td>{r.total}</td>
              <td>{r.rate == null ? "— (no evaluations)" : `${(r.rate * 100).toFixed(1)}%`}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
