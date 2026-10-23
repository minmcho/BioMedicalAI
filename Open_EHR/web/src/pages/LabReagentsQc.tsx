import { useCallback, useEffect, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

// Lab Depth W4 (LAB-D9 + LAB-D8) — Reagents & QC surface.
//
// - Lot-to-lot verification protocols (app.lab_reagents): create a
//   run for an inventory lot, enter parallel sample pairs + QC on
//   both lots, see the server-computed comparison against the
//   DIRECTOR-CONFIGURED criteria (governed Studio config
//   terminology/lab-reagent-qc — seeded values are labelled
//   governed defaults; there is no universal ±10% standard), and
//   sign the accept/reject decision. Acceptance drives Inventory's
//   own verify + stage surface server-side.
// - Control-lot onboarding board: collecting -> established ->
//   live, with runs collected vs the governed required count.
// - QC status by instrument + reagent lot + shift, with the
//   instrument-path lock state (app.lab_qc).
// - Calibration list with due dates + the AMR calibration-
//   verification workflow (>=3 levels spanning the reportable
//   range). Calibration due-date truth is the device registry;
//   this surface reads the one-truth posture the backend reports.
//
// JIT position, also stated by the backend: reagent release has NO
// emergency bypass — a quarantined/unverified lot cannot be used
// under any pressure pathway.

export interface VerificationRun {
  id: number;
  sku: string;
  location_code: string;
  lot_number: string;
  analyte_code: string;
  outgoing_lot_number?: string | null;
  status: string;
  decision?: string | null;
  decided_by?: string | null;
  criteria: { minSamples?: number; maxBiasPct?: number | null; maxBiasAbs?: number | null; label?: string; source?: string };
  comparison?: {
    meets_criteria: boolean;
    reasons: string[];
    sample_count: number;
    min_samples: number;
    mean_bias?: number | null;
    per_pair: Array<{ sample_ref: string; old_value: number; new_value: number; bias: number; bias_pct?: number | null; within_criteria: boolean }>;
  } | null;
  inventory_lot?: { stage: string; verification_result?: string | null } | null;
}

export interface ControlLot {
  id: number;
  lot_key: string;
  analyte_code: string;
  level?: string | null;
  state: string;
  required_runs: number;
  collected_runs: number;
  runs_remaining: number;
  established_mean?: number | null;
  established_sd?: number | null;
  usable_as_patient_qc: boolean;
}

export interface CalibrationRecord {
  id: number;
  instrument_id: string;
  kind: string;
  passed: boolean;
  due_date?: string | null;
  registry_due?: string | null;
  failure_reasons: string[];
  performed_at?: string | null;
  performed_by: string;
}

interface InstrumentStatus {
  instrument_id: string;
  path_locked: boolean;
  calibration: { status: string; source: string; due?: string | null };
  items: Array<{
    analyte_code: string;
    reagent_lot_key?: string | null;
    shift?: string | null;
    path_locked: boolean;
    latest: { measured_value: number; passed: boolean; flags: string[]; control_material: string } | null;
  }>;
}

interface StockLot { sku: string; location_code: string; lot_number: string; stage: string; quantity: number }
interface Instrument { instrument_id: string; name: string; bench: string; status: string }

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

const JIT_NOTICE =
  "No emergency bypass: a quarantined or unverified reagent lot cannot be used under any pressure pathway. " +
  "Release requires a signed verification protocol below — urgency changes queue order, never the gate.";

export function LabReagentsQc({ client }: { client: ApiClient }) {
  const catalog = useAsync(() => client.labCatalog(), [client]);
  const stock = useAsync(
    () => client.request<{ items: StockLot[] }>("/inventory/stock"),
    [client],
  );
  const instruments = useAsync(
    () => client.request<Instrument[]>("/lab/instruments"),
    [client],
  );
  const runs = useAsync(
    () => client.request<{ items: VerificationRun[] }>("/lab/reagent-verifications"),
    [client],
  );
  const controlLots = useAsync(
    () => client.request<{ items: ControlLot[] }>("/lab/control-lots"),
    [client],
  );
  const calibrations = useAsync(
    () => client.request<{ items: CalibrationRecord[] }>("/lab/calibrations"),
    [client],
  );

  const analytes = (catalog.data?.catalog?.concepts ?? []).filter(
    (c) => c.active !== false && !c.panel,
  );
  const lots = stock.data?.items ?? [];
  const instrumentList = instruments.data ?? [];

  return (
    <div className="lab-reagents-qc">
      <h2>Reagents &amp; QC</h2>
      <p role="note">{JIT_NOTICE}</p>

      <VerificationSection
        client={client}
        runs={runs}
        analytes={analytes}
        lots={lots}
        selectsReady={!catalog.loading && !stock.loading}
      />
      <ControlLotsSection client={client} lots={controlLots} analytes={analytes} />
      <InstrumentQcSection client={client} instruments={instrumentList} />
      <CalibrationSection client={client} records={calibrations} instruments={instrumentList} />
    </div>
  );
}

function VerificationSection({
  client, runs, analytes, lots, selectsReady,
}: {
  client: ApiClient;
  runs: ReturnType<typeof useAsync<{ items: VerificationRun[] }>>;
  analytes: Array<{ code: string; display?: string }>;
  lots: StockLot[];
  selectsReady: boolean;
}) {
  const [lotSel, setLotSel] = useState("");
  const [analyte, setAnalyte] = useState("");
  const [outgoing, setOutgoing] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const create = async () => {
    const lot = lots.find((l) => `${l.sku}|${l.location_code}|${l.lot_number}` === lotSel);
    if (!lot || !analyte) return;
    setBusy(true); setError(null);
    try {
      const run = await client.request<VerificationRun>("/lab/reagent-verifications", {
        method: "POST",
        body: JSON.stringify({
          sku: lot.sku, location_code: lot.location_code, lot_number: lot.lot_number,
          analyte_code: analyte, outgoing_lot_number: outgoing || undefined,
        }),
      });
      setSelectedId(run.id);
      runs.reload();
    } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  };

  const selected = runs.data?.items.find((r) => r.id === selectedId) ?? null;

  return (
    <section aria-label="Lot-to-lot verification">
      <h3>Lot-to-lot verification protocols</h3>
      {runs.loading && <Loading label="Loading verification runs…" />}
      {runs.error != null && <ErrorView error={runs.error} />}
      {runs.data && runs.data.items.length === 0 && (
        <Empty label="No verification runs yet — open one for a quarantined lot below." />
      )}
      <ul>
        {(runs.data?.items ?? []).map((run) => (
          <li key={run.id}>
            <button type="button" onClick={() => setSelectedId(run.id)}>
              #{run.id} {run.sku} / {run.lot_number} — {run.analyte_code} —{" "}
              {run.status}{run.decision ? ` (${run.decision} by ${run.decided_by})` : ""} —{" "}
              lot stage: {run.inventory_lot?.stage ?? "unknown"}
            </button>
          </li>
        ))}
      </ul>

      <form
        onSubmit={(e) => { e.preventDefault(); void create(); }}
        aria-label="Open verification run"
      >
        <label>
          Inventory lot{" "}
          <select
            aria-label="Inventory lot"
            value={lotSel}
            onChange={(e) => setLotSel(e.target.value)}
            disabled={!selectsReady || lots.length === 0}
          >
            <option value="">
              {lots.length === 0 ? "No lots in inventory stock" : "Select a lot"}
            </option>
            {lots.map((l) => (
              <option key={`${l.sku}|${l.location_code}|${l.lot_number}`} value={`${l.sku}|${l.location_code}|${l.lot_number}`}>
                {l.sku} / {l.lot_number} @ {l.location_code} — stage {l.stage}
              </option>
            ))}
          </select>
        </label>
        <label>
          Analyte{" "}
          <select
            aria-label="Analyte"
            value={analyte}
            onChange={(e) => setAnalyte(e.target.value)}
            disabled={!selectsReady || analytes.length === 0}
          >
            <option value="">{analytes.length === 0 ? "No analytes in the published catalog" : "Select an analyte"}</option>
            {analytes.map((a) => (
              <option key={a.code} value={a.code}>{a.code} — {a.display ?? a.code}</option>
            ))}
          </select>
        </label>
        <label>
          Outgoing lot number{" "}
          <input aria-label="Outgoing lot number" value={outgoing} onChange={(e) => setOutgoing(e.target.value)} />
        </label>
        <button type="submit" disabled={busy || !lotSel || !analyte}>
          Open verification run
        </button>
        {!lotSel || !analyte ? (
          <span> Select a lot and an analyte to open a run.</span>
        ) : null}
      </form>
      {error && <p role="alert">{error}</p>}

      {selected && (
        <VerificationDetail client={client} run={selected} onChanged={runs.reload} />
      )}
    </section>
  );
}

function VerificationDetail({
  client, run, onChanged,
}: {
  client: ApiClient;
  run: VerificationRun;
  onChanged: () => void;
}) {
  const [pairs, setPairs] = useState<Array<{ old: string; new: string }>>([
    { old: "", new: "" }, { old: "", new: "" }, { old: "", new: "" },
  ]);
  const [qcOld, setQcOld] = useState("4.0");
  const [qcNew, setQcNew] = useState("4.0");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const decided = run.status === "decided";

  const submitEvidence = async () => {
    setBusy(true); setError(null);
    try {
      const qc = (measured: string) => [{
        control_material: "control", measured_value: Number(measured),
        expected_low: 3.8, expected_high: 4.2,
      }];
      await client.request(`/lab/reagent-verifications/${run.id}/evidence`, {
        method: "POST",
        body: JSON.stringify({
          sample_pairs: pairs
            .filter((p) => p.old !== "" && p.new !== "")
            .map((p, i) => ({ sample_ref: `sample-${i + 1}`, old_value: Number(p.old), new_value: Number(p.new) })),
          qc_old_lot: qc(qcOld), qc_new_lot: qc(qcNew),
        }),
      });
      onChanged();
    } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  };

  const decide = async (decision: "accept" | "reject") => {
    setBusy(true); setError(null);
    try {
      await client.request(`/lab/reagent-verifications/${run.id}/decide`, {
        method: "POST", body: JSON.stringify({ decision }),
      });
      onChanged();
    } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  };

  return (
    <div aria-label={`Verification run ${run.id} detail`}>
      <h4>Run #{run.id} — {run.sku} / {run.lot_number} ({run.analyte_code})</h4>
      <p>
        Acceptance criteria (director-configured, governed Studio config
        terminology/lab-reagent-qc{run.criteria.source ? `, source: ${run.criteria.source}` : ""}):
        minimum {run.criteria.minSamples ?? "—"} parallel samples
        {run.criteria.maxBiasPct != null ? `, max bias ${run.criteria.maxBiasPct}%` : ""}
        {run.criteria.maxBiasAbs != null ? `, max absolute bias ${run.criteria.maxBiasAbs}` : ""}.
        {run.criteria.label ? ` ${run.criteria.label}` : ""}
      </p>
      {pairs.map((pair, i) => (
        <div key={i}>
          <label>
            Sample {i + 1} old-lot value{" "}
            <input
              aria-label={`Sample ${i + 1} old-lot value`}
              value={pair.old}
              disabled={decided}
              onChange={(e) => setPairs(pairs.map((p, j) => (j === i ? { ...p, old: e.target.value } : p)))}
            />
          </label>
          <label>
            Sample {i + 1} new-lot value{" "}
            <input
              aria-label={`Sample ${i + 1} new-lot value`}
              value={pair.new}
              disabled={decided}
              onChange={(e) => setPairs(pairs.map((p, j) => (j === i ? { ...p, new: e.target.value } : p)))}
            />
          </label>
        </div>
      ))}
      <label>
        QC on outgoing lot (measured){" "}
        <input aria-label="QC on outgoing lot" value={qcOld} disabled={decided} onChange={(e) => setQcOld(e.target.value)} />
      </label>
      <label>
        QC on new lot (measured){" "}
        <input aria-label="QC on new lot" value={qcNew} disabled={decided} onChange={(e) => setQcNew(e.target.value)} />
      </label>
      <button type="button" onClick={() => void submitEvidence()} disabled={busy || decided}>
        Record evidence &amp; compute comparison
      </button>
      {decided && <span> This protocol is decided and signed; its evidence is frozen.</span>}

      {run.comparison && (
        <div aria-label="Computed comparison">
          <p>
            Comparison: {run.comparison.sample_count} sample(s), mean bias{" "}
            {run.comparison.mean_bias ?? "—"} —{" "}
            {run.comparison.meets_criteria
              ? "MEETS the director-configured criteria"
              : "does NOT meet the criteria"}
          </p>
          <ul>
            {run.comparison.per_pair.map((p) => (
              <li key={p.sample_ref}>
                {p.sample_ref}: old {p.old_value} → new {p.new_value} (bias {p.bias}
                {p.bias_pct != null ? `, ${p.bias_pct.toFixed(2)}%` : ""}) —{" "}
                {p.within_criteria ? "within criteria" : "OUTSIDE criteria"}
              </li>
            ))}
          </ul>
          {run.comparison.reasons.length > 0 && (
            <ul>{run.comparison.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
          )}
        </div>
      )}
      {error && <p role="alert">{error}</p>}
      <button type="button" onClick={() => void decide("accept")} disabled={busy || decided}>
        Sign acceptance
      </button>
      <button type="button" onClick={() => void decide("reject")} disabled={busy || decided}>
        Sign rejection
      </button>
      {decided && <span> Decision signed by {run.decided_by}: {run.decision}.</span>}
    </div>
  );
}

function ControlLotsSection({
  client, lots, analytes,
}: {
  client: ApiClient;
  lots: ReturnType<typeof useAsync<{ items: ControlLot[] }>>;
  analytes: Array<{ code: string; display?: string }>;
}) {
  const [lotKey, setLotKey] = useState("");
  const [analyte, setAnalyte] = useState("");
  const [required, setRequired] = useState("20");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); lots.reload(); }
    catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  };

  return (
    <section aria-label="Control lot onboarding">
      <h3>Control-lot onboarding</h3>
      {lots.loading && <Loading label="Loading control lots…" />}
      {lots.error != null && <ErrorView error={lots.error} />}
      {lots.data && lots.data.items.length === 0 && (
        <Empty label="No control lots registered — register one below; it starts in 'collecting' and refuses patient-QC use until live." />
      )}
      <table>
        <thead>
          <tr><th>Lot</th><th>Analyte</th><th>State</th><th>Runs</th><th>Mean / SD</th><th>Actions</th></tr>
        </thead>
        <tbody>
          {(lots.data?.items ?? []).map((lot) => (
            <tr key={lot.id}>
              <td>{lot.lot_key}</td>
              <td>{lot.analyte_code}</td>
              <td>{lot.state}{lot.usable_as_patient_qc ? " (usable as patient QC)" : " (refuses patient-QC use)"}</td>
              <td>{lot.collected_runs} of {lot.required_runs} collected ({lot.runs_remaining} remaining)</td>
              <td>{lot.established_mean ?? "—"} / {lot.established_sd ?? "—"}</td>
              <td>
                <button
                  type="button" disabled={busy || lot.state !== "collecting" || lot.runs_remaining > 0}
                  onClick={() => void act(() => client.request(`/lab/control-lots/${lot.id}/establish`, { method: "POST", body: "{}" }))}
                >
                  Establish mean/SD
                </button>
                {lot.state === "collecting" && lot.runs_remaining > 0 && (
                  <span> {lot.runs_remaining} more establishment run(s) required first.</span>
                )}
                <button
                  type="button" disabled={busy || lot.state !== "established"}
                  onClick={() => void act(() => client.request(`/lab/control-lots/${lot.id}/activate`, { method: "POST", body: "{}" }))}
                >
                  Go live
                </button>
                {lot.state !== "established" && <span> Only an established lot can go live.</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <form
        aria-label="Register control lot"
        onSubmit={(e) => {
          e.preventDefault();
          void act(() => client.request("/lab/control-lots", {
            method: "POST",
            body: JSON.stringify({ lot_key: lotKey, analyte_code: analyte, required_runs: Number(required) }),
          }));
        }}
      >
        <label>
          Control lot key{" "}
          <input aria-label="Control lot key" value={lotKey} onChange={(e) => setLotKey(e.target.value)} />
        </label>
        <label>
          Analyte{" "}
          <select aria-label="Control lot analyte" value={analyte} onChange={(e) => setAnalyte(e.target.value)} disabled={analytes.length === 0}>
            <option value="">{analytes.length === 0 ? "No analytes in the published catalog" : "Select an analyte"}</option>
            {analytes.map((a) => <option key={a.code} value={a.code}>{a.code}</option>)}
          </select>
        </label>
        <label>
          Required runs (governed default 20){" "}
          <input aria-label="Required runs" value={required} onChange={(e) => setRequired(e.target.value)} />
        </label>
        <button type="submit" disabled={busy || !lotKey || !analyte}>Register control lot</button>
        {(!lotKey || !analyte) && <span> Name the lot and pick an analyte to register.</span>}
      </form>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}

function InstrumentQcSection({
  client, instruments,
}: {
  client: ApiClient;
  instruments: Instrument[];
}) {
  const [instrumentId, setInstrumentId] = useState("");
  const [status, setStatus] = useState<InstrumentStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!instrumentId) return;
    setError(null);
    try {
      setStatus(await client.request<InstrumentStatus>(
        `/lab/qc/instrument-status?instrument_id=${encodeURIComponent(instrumentId)}`));
    } catch (err) { setError(errorText(err)); }
  }, [client, instrumentId]);

  useEffect(() => { void load(); }, [load]);

  return (
    <section aria-label="QC status by instrument">
      <h3>QC status by instrument / lot / shift</h3>
      <label>
        Instrument{" "}
        <select
          aria-label="QC instrument"
          value={instrumentId}
          onChange={(e) => setInstrumentId(e.target.value)}
          disabled={instruments.length === 0}
        >
          <option value="">{instruments.length === 0 ? "No instruments registered" : "Select an instrument"}</option>
          {instruments.map((i) => (
            <option key={i.instrument_id} value={i.instrument_id}>
              {i.instrument_id} — {i.name} ({i.status})
            </option>
          ))}
        </select>
      </label>
      {error && <p role="alert">{error}</p>}
      {status && (
        <div>
          <p>
            Instrument path: {status.path_locked ? "LOCKED" : "open"} — calibration{" "}
            {status.calibration.status} (truth: {status.calibration.source}
            {status.calibration.due ? `, due ${status.calibration.due}` : ""})
          </p>
          <table>
            <thead>
              <tr><th>Analyte</th><th>Latest QC</th><th>Flags</th><th>Reagent lot</th><th>Shift</th><th>Path</th></tr>
            </thead>
            <tbody>
              {status.items.map((item) => (
                <tr key={item.analyte_code}>
                  <td>{item.analyte_code}</td>
                  <td>{item.latest ? `${item.latest.measured_value} (${item.latest.passed ? "pass" : "FAIL"} — ${item.latest.control_material})` : "no QC recorded"}</td>
                  <td>{item.latest?.flags.join(", ") || "—"}</td>
                  <td>{item.reagent_lot_key ?? "—"}</td>
                  <td>{item.shift ?? "—"}</td>
                  <td>{item.path_locked ? "locked" : "open"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {status.items.length === 0 && <Empty label="No QC runs keyed to this instrument yet." />}
        </div>
      )}
    </section>
  );
}

function CalibrationSection({
  client, records, instruments,
}: {
  client: ApiClient;
  records: ReturnType<typeof useAsync<{ items: CalibrationRecord[] }>>;
  instruments: Instrument[];
}) {
  const [instrumentId, setInstrumentId] = useState("");
  const [nominals, setNominals] = useState(["2", "5", "10"]);
  const [measureds, setMeasureds] = useState(["2.0", "5.0", "10.0"]);
  const [rangeLow, setRangeLow] = useState("2");
  const [rangeHigh, setRangeHigh] = useState("10");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const levels = () => nominals.map((n, i) => ({
    label: `L${i + 1}`, nominal: Number(n), measured: Number(measureds[i]),
  }));

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); records.reload(); }
    catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  };

  return (
    <section aria-label="Calibration">
      <h3>Calibration &amp; AMR verification</h3>
      {records.loading && <Loading label="Loading calibrations…" />}
      {records.error != null && <ErrorView error={records.error} />}
      {records.data && records.data.items.length === 0 && (
        <Empty label="No calibration records yet — record one below with measured values." />
      )}
      <table>
        <thead>
          <tr><th>Instrument</th><th>Kind</th><th>Verdict</th><th>Due</th><th>By</th></tr>
        </thead>
        <tbody>
          {(records.data?.items ?? []).map((rec) => (
            <tr key={rec.id}>
              <td>{rec.instrument_id}</td>
              <td>{rec.kind}</td>
              <td>{rec.passed ? "PASS" : `FAIL — ${rec.failure_reasons.join("; ")}`}</td>
              <td>{rec.registry_due ?? rec.due_date ?? "—"}</td>
              <td>{rec.performed_by}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <form
        aria-label="Record calibration"
        onSubmit={(e) => {
          e.preventDefault();
          void act(() => client.request("/lab/calibrations", {
            method: "POST",
            body: JSON.stringify({ instrument_id: instrumentId, levels: levels() }),
          }));
        }}
      >
        <label>
          Instrument{" "}
          <select aria-label="Calibration instrument" value={instrumentId} onChange={(e) => setInstrumentId(e.target.value)} disabled={instruments.length === 0}>
            <option value="">{instruments.length === 0 ? "No instruments registered" : "Select an instrument"}</option>
            {instruments.map((i) => <option key={i.instrument_id} value={i.instrument_id}>{i.instrument_id}</option>)}
          </select>
        </label>
        {nominals.map((n, i) => (
          <span key={i}>
            <label>
              Level {i + 1} nominal{" "}
              <input aria-label={`Level ${i + 1} nominal`} value={n} onChange={(e) => setNominals(nominals.map((v, j) => (j === i ? e.target.value : v)))} />
            </label>
            <label>
              Level {i + 1} measured{" "}
              <input aria-label={`Level ${i + 1} measured`} value={measureds[i]} onChange={(e) => setMeasureds(measureds.map((v, j) => (j === i ? e.target.value : v)))} />
            </label>
          </span>
        ))}
        <button type="submit" disabled={busy || !instrumentId}>Record calibration</button>
        {!instrumentId && <span> Select an instrument first.</span>}
      </form>
      <form
        aria-label="Verify AMR"
        onSubmit={(e) => {
          e.preventDefault();
          void act(() => client.request("/lab/calibrations/verify-amr", {
            method: "POST",
            body: JSON.stringify({
              instrument_id: instrumentId, levels: levels(),
              reportable_low: Number(rangeLow), reportable_high: Number(rangeHigh),
            }),
          }));
        }}
      >
        <label>
          Reportable low{" "}
          <input aria-label="Reportable low" value={rangeLow} onChange={(e) => setRangeLow(e.target.value)} />
        </label>
        <label>
          Reportable high{" "}
          <input aria-label="Reportable high" value={rangeHigh} onChange={(e) => setRangeHigh(e.target.value)} />
        </label>
        <button type="submit" disabled={busy || !instrumentId}>
          Run AMR verification (≥3 levels spanning the range)
        </button>
        {!instrumentId && <span> Select an instrument in the calibration form first.</span>}
      </form>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
