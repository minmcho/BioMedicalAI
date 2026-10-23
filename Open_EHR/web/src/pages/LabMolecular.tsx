import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import {
  addAreaStamps, createBatch, createContaminationEvent, createRun,
  decontaminate, getMolecularConfig, getRun, listBatches,
  listContaminationEvents, listRuns, recordControls, recordResults,
  releaseRun, repeatRun,
  type ContaminationEvent, type MolecularBatch, type MolecularConfig,
  type MolecularRun,
} from "../api/labMolecular";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
}

// LAB-D14 — molecular diagnostics surface (app.lab_molecular).
// Mounted by the coordinator as a Lab page section (see the W8
// INTEGRATION NOTE). The NTC run-validity gate is the centre of the
// surface: its status is rendered prominently on every run, an
// invalidated run LOOKS invalidated (banner + discarded, struck
// results — never a tooltip), and the release button is disabled
// WITH the server's named blockers shown while the gate is closed.
// Every dropdown is fed by the governed config endpoint; a control
// whose governed source is empty is disabled WITH the reason shown.

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  }
  return err instanceof Error ? err.message : String(err);
}

function Options({ options }: {
  options: { code: string; display: string }[];
}) {
  return (
    <>
      {options.map((o) => (
        <option key={o.code} value={o.code}>{o.display}</option>
      ))}
    </>
  );
}

const splitList = (text: string) =>
  text.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);

export function LabMolecular({ client }: Props) {
  const configState = useAsync<MolecularConfig>(
    () => getMolecularConfig(client), [client]);
  const [tick, setTick] = useState(0);
  const boardState = useAsync(
    async () => {
      const [batches, runs, events] = await Promise.all([
        listBatches(client), listRuns(client),
        listContaminationEvents(client)]);
      return { batches: batches.items, runs: runs.items,
               events: events.items };
    }, [client, tick]);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const detailState = useAsync<MolecularRun | null>(
    async () => (selectedRunId ? getRun(client, selectedRunId) : null),
    [client, selectedRunId, tick]);
  const [error, setError] = useState<string | null>(null);

  // Batch form
  const [batchAssay, setBatchAssay] = useState("");
  const [batchInstrument, setBatchInstrument] = useState("");
  const [batchOrders, setBatchOrders] = useState("");
  const [stampArea, setStampArea] = useState("");
  const [stampSeq, setStampSeq] = useState<string[]>([]);
  // Run form
  const [runBatchId, setRunBatchId] = useState("");
  // Controls form
  const [ntcCt, setNtcCt] = useState("");
  const [ntcAmplified, setNtcAmplified] = useState(true);
  const [pcStatus, setPcStatus] = useState("passed");
  const [pcCt, setPcCt] = useState("");
  // Per-sample result forms, keyed by order_id
  const [ctInputs, setCtInputs] = useState<Record<string, string>>({});
  const [icInputs, setIcInputs] = useState<Record<string, string>>({});
  const [ecInputs, setEcInputs] = useState<Record<string, string>>({});
  // Repeat + contamination forms
  const [repeatFrom, setRepeatFrom] = useState("amplification");
  const [eventInstrument, setEventInstrument] = useState("");
  const [eventAreas, setEventAreas] = useState<string[]>([]);
  const [eventNote, setEventNote] = useState("");
  const [deconMethod, setDeconMethod] = useState<Record<number, string>>({});
  const [deconSwab, setDeconSwab] = useState<Record<number, string>>({});

  if (configState.loading) return <Loading label="Loading molecular config…" />;
  if (configState.error) return <ErrorView error={configState.error} />;
  const config = configState.data;
  if (!config) return <Empty label="No molecular configuration available." />;
  const board = boardState.data;
  const detail = detailState.data;
  const detailAssay = detail
    ? config.assays.find((a) => a.code === detail.assay_code) : undefined;

  const run = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      setTick((t) => t + 1);
    } catch (err) {
      setError(errorText(err));
    }
  };

  const ntcBanner = (runRow: MolecularRun) => {
    if (runRow.status === "invalidated") {
      return (
        <p role="alert">
          <strong>RUN {runRow.id} INVALIDATED — ALL RESULTS DISCARDED.</strong>
          {" "}{runRow.invalidated_reason} No result from this run can
          be released; use the repeat workflow.
        </p>
      );
    }
    if (runRow.ntc.status === "unresolved") {
      return (
        <p role="status">
          <strong>NTC status: UNRESOLVED</strong> — run {runRow.id} cannot
          release any result until its no-template control is recorded.
        </p>
      );
    }
    if (runRow.ntc.status === "positive") {
      return (
        <p role="alert">
          <strong>NTC status: POSITIVE</strong> — run {runRow.id} is
          invalid; all results are discarded.
        </p>
      );
    }
    return (
      <p role="status">
        NTC status: NEGATIVE (Ct {runRow.ntc.ct ?? "no amplification"}) —
        run {runRow.id}'s validity gate is satisfied.
      </p>
    );
  };

  return (
    <section aria-label="Molecular diagnostics">
      <h2>Molecular diagnostics (LAB-D14)</h2>
      {error && <p role="alert">{error}</p>}

      <h3>Assay configuration (governed)</h3>
      <p>
        Studio terminology/lab-molecular
        {config.config.version ? ` v${config.config.version}` : ""};
        NTC positivity ceiling Ct {config.ntc_positive_max_ct ?? "—"}.
        Unidirectional areas, clean → dirty:{" "}
        {config.areas.map((a) => a.display).join(" → ") || "none published"}.
      </p>
      {config.assays.length === 0 ? (
        <Empty label="No governed molecular assays published." />
      ) : (
        <table>
          <thead>
            <tr><th>Assay</th><th>Targets (Ct bands)</th>
              <th>Controls required</th><th>Max batch</th>
              <th>Run windows</th></tr>
          </thead>
          <tbody>
            {config.assays.map((assay) => (
              <tr key={assay.code}>
                <td>{assay.display} ({assay.code})</td>
                <td>{assay.targets.map((t) =>
                  `${t.display}: detected ≤${t.detectedMaxCt}, ` +
                  `inconclusive ≤${t.inconclusiveMaxCt}`).join("; ")}</td>
                <td>
                  {assay.requiresInternalControl ? "internal " : ""}
                  {assay.requiresExtractionControl ? "extraction " : ""}
                  {assay.requiresPositiveControl ? "positive" : ""}
                </td>
                <td>{assay.maxBatchSize}</td>
                <td>{assay.runWindows.join(", ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Batch / run board</h3>
      {boardState.loading && <Loading label="Loading batches and runs…" />}
      {board && board.batches.length === 0 && (
        <Empty label="No extraction batches yet." />)}
      {board && board.batches.map((batch: MolecularBatch) => (
        <div key={batch.id}>
          <p>
            Batch {batch.id} — {batch.assay_code}, instrument{" "}
            {batch.instrument_id ?? "—"}, {batch.sample_count} sample(s),
            areas: {batch.area_stamps.map((s) => s.area).join(" → ") ||
              "none recorded"}
            {batch.workflow_flagged && (
              <> — <strong>WORKFLOW FLAGGED:</strong>{" "}
                {batch.workflow_flag_reason}</>
            )}
          </p>
        </div>
      ))}
      {board && board.runs.map((runRow) => (
        <div key={runRow.id}>
          <p>
            Run {runRow.id} (batch {runRow.batch_id}) — {runRow.assay_code},
            status <strong>{runRow.status}</strong>, NTC{" "}
            <strong>{runRow.ntc.status}</strong>, {runRow.sample_count}{" "}
            sample(s)
            <button type="button"
                    onClick={() => setSelectedRunId(runRow.id)}>
              Open run {runRow.id}
            </button>
          </p>
          {(runRow.status === "invalidated" ||
            runRow.ntc.status !== "negative") && ntcBanner(runRow)}
        </div>
      ))}

      <fieldset>
        <legend>Create extraction batch</legend>
        <label>
          Assay{" "}
          <select aria-label="Batch assay" value={batchAssay}
                  disabled={!config.assays.length}
                  onChange={(e) => setBatchAssay(e.target.value)}>
            <option value="">Select…</option>
            <Options options={config.assays} />
          </select>
        </label>
        {!config.assays.length && (
          <span> Disabled: no governed molecular assays published.</span>
        )}
        <label>
          Instrument{" "}
          <input aria-label="Batch instrument" value={batchInstrument}
                 onChange={(e) => setBatchInstrument(e.target.value)} />
        </label>
        <label>
          Order IDs (one per sample){" "}
          <input aria-label="Batch order IDs" value={batchOrders}
                 onChange={(e) => setBatchOrders(e.target.value)} />
        </label>
        <label>
          Area stamp{" "}
          <select aria-label="Area stamp" value={stampArea}
                  disabled={!config.areas.length}
                  onChange={(e) => setStampArea(e.target.value)}>
            <option value="">Select…</option>
            <Options options={config.areas} />
          </select>
        </label>
        <button type="button" disabled={!stampArea}
                onClick={() => {
                  setStampSeq((s) => [...s, stampArea]);
                  setStampArea("");
                }}>
          Append area stamp
        </button>
        <span> Recorded sequence: {stampSeq.join(" → ") || "none"}</span>
        <button type="button"
                disabled={!batchAssay || !splitList(batchOrders).length}
                onClick={() => run(async () => {
                  const created = await createBatch(client, {
                    assay_code: batchAssay,
                    instrument_id: batchInstrument || undefined,
                    samples: splitList(batchOrders).map(
                      (order_id) => ({ order_id })),
                    area_stamps: stampSeq.map((area) => ({ area })),
                  });
                  setStampSeq([]);
                  void created;
                })}>
          Create batch
        </button>
        {(!batchAssay || !splitList(batchOrders).length) && (
          <span> An assay and at least one order ID are required.</span>
        )}
      </fieldset>

      <fieldset>
        <legend>Build amplification run</legend>
        <label>
          Batch{" "}
          <select aria-label="Run batch" value={runBatchId}
                  disabled={!board?.batches.length}
                  onChange={(e) => setRunBatchId(e.target.value)}>
            <option value="">Select…</option>
            {(board?.batches ?? []).map((b) => (
              <option key={b.id} value={b.id}>
                Batch {b.id} — {b.assay_code} ({b.sample_count} samples)
              </option>
            ))}
          </select>
        </label>
        {!board?.batches.length && (
          <span> Disabled: no extraction batches exist yet.</span>
        )}
        <button type="button" disabled={!runBatchId}
                onClick={() => run(async () => {
                  const created = await createRun(client, {
                    batch_id: Number(runBatchId) });
                  setSelectedRunId(created.id);
                })}>
          Create run
        </button>
      </fieldset>

      {selectedRunId && detailState.loading && (
        <Loading label="Loading run…" />)}
      {detail && (
        <div>
          <h3>Run {detail.id} detail</h3>
          {ntcBanner(detail)}
          {detail.gate.blockers.length > 0 && (
            <p>Release blockers: {detail.gate.blockers.join("; ")}</p>
          )}
          <fieldset>
            <legend>Run controls — NTC gate</legend>
            <label>
              NTC Ct{" "}
              <input aria-label="NTC Ct" type="number" value={ntcCt}
                     disabled={!ntcAmplified}
                     onChange={(e) => setNtcCt(e.target.value)} />
            </label>
            <label>
              <input aria-label="NTC did not amplify" type="checkbox"
                     checked={!ntcAmplified}
                     onChange={(e) => setNtcAmplified(!e.target.checked)} />
              NTC did not amplify (true negative)
            </label>
            <label>
              Positive control{" "}
              <select aria-label="Positive control status" value={pcStatus}
                      onChange={(e) => setPcStatus(e.target.value)}>
                <option value="passed">Passed</option>
                <option value="failed">Failed</option>
              </select>
            </label>
            <label>
              Positive control Ct{" "}
              <input aria-label="Positive control Ct" type="number"
                     value={pcCt}
                     onChange={(e) => setPcCt(e.target.value)} />
            </label>
            <button type="button"
                    disabled={detail.status !== "open"}
                    onClick={() => run(() => recordControls(
                      client, detail.id, {
                        ntc_ct: ntcAmplified && ntcCt
                          ? Number(ntcCt) : null,
                        positive_control_status: pcStatus,
                        positive_control_ct: pcCt ? Number(pcCt) : undefined,
                      }))}>
              Record run controls
            </button>
            {detail.status !== "open" && (
              <span> Disabled: run is {detail.status}.</span>
            )}
          </fieldset>

          <h4>Per-sample validity</h4>
          {(detail.samples ?? []).map((sample) => {
            const invalidated = sample.validity === "invalidated";
            return (
              <div key={sample.id}>
                <p>
                  Order {sample.order_id} —{" "}
                  {invalidated ? (
                    <s>
                      {sample.discarded_interpretation ??
                        "result"} (DISCARDED)
                    </s>
                  ) : (
                    <>{sample.interpretation ?? "not resulted"}</>
                  )}
                  {" "}· validity <strong>{sample.validity}</strong>
                  {sample.validity_reason &&
                    <> — {sample.validity_reason}</>}
                  {sample.report_id && <> · report {sample.report_id}</>}
                </p>
                {!invalidated && detail.status === "open" && (
                  <fieldset>
                    <legend>Result for {sample.order_id}</legend>
                    {(detailAssay?.targets ?? []).map((target) => (
                      <label key={target.code}>
                        Ct {target.display}{" "}
                        <input
                          aria-label={`Ct ${sample.order_id} ${target.code}`}
                          type="number"
                          value={ctInputs[sample.order_id] ?? ""}
                          onChange={(e) => setCtInputs((prev) => ({
                            ...prev,
                            [sample.order_id]: e.target.value }))} />
                      </label>
                    ))}
                    <label>
                      Internal control{" "}
                      <select
                        aria-label={`Internal control ${sample.order_id}`}
                        value={icInputs[sample.order_id] ?? "passed"}
                        onChange={(e) => setIcInputs((prev) => ({
                          ...prev, [sample.order_id]: e.target.value }))}>
                        <option value="passed">Passed</option>
                        <option value="failed">Failed</option>
                      </select>
                    </label>
                    <label>
                      Extraction control{" "}
                      <select
                        aria-label={`Extraction control ${sample.order_id}`}
                        value={ecInputs[sample.order_id] ?? "passed"}
                        onChange={(e) => setEcInputs((prev) => ({
                          ...prev, [sample.order_id]: e.target.value }))}>
                        <option value="passed">Passed</option>
                        <option value="failed">Failed</option>
                      </select>
                    </label>
                    <button type="button"
                            onClick={() => run(() => recordResults(
                              client, detail.id, {
                                results: [{
                                  order_id: sample.order_id,
                                  ct_values: Object.fromEntries(
                                    (detailAssay?.targets ?? []).map((t) => [
                                      t.code,
                                      ctInputs[sample.order_id]
                                        ? Number(ctInputs[sample.order_id])
                                        : null])),
                                  internal_control: {
                                    status: icInputs[sample.order_id] ??
                                      "passed" },
                                  extraction_control: {
                                    status: ecInputs[sample.order_id] ??
                                      "passed" },
                                }],
                              }))}>
                      Save result {sample.order_id}
                    </button>
                    <span> Leave Ct empty for no amplification.</span>
                  </fieldset>
                )}
              </div>
            );
          })}

          <button type="button"
                  disabled={!detail.gate.can_release}
                  onClick={() => run(() => releaseRun(client, detail.id))}>
            Release run {detail.id} results
          </button>
          {!detail.gate.can_release && (
            <span> Release disabled: {detail.gate.blockers.join("; ")}</span>
          )}

          {detail.status === "invalidated" && (
            <fieldset>
              <legend>Repeat workflow</legend>
              <label>
                Repeat from{" "}
                <select aria-label="Repeat from" value={repeatFrom}
                        disabled={!config.repeat_options.length}
                        onChange={(e) => setRepeatFrom(e.target.value)}>
                  <Options options={config.repeat_options} />
                </select>
              </label>
              <button type="button" disabled={!repeatFrom}
                      onClick={() => run(async () => {
                        const created = await repeatRun(client, detail.id, {
                          repeat_from: repeatFrom });
                        setSelectedRunId(created.id);
                      })}>
                Repeat run {detail.id}
              </button>
            </fieldset>
          )}

          {detail.batch && (
            <fieldset>
              <legend>Append area stamp to batch {detail.batch.id}</legend>
              <label>
                Area{" "}
                <select aria-label="Append area stamp" value={stampArea}
                        onChange={(e) => setStampArea(e.target.value)}>
                  <option value="">Select…</option>
                  <Options options={config.areas} />
                </select>
              </label>
              <button type="button" disabled={!stampArea}
                      onClick={() => run(() => addAreaStamps(
                        client, detail.batch!.id,
                        { area_stamps: [{ area: stampArea }] }))}>
                Append stamp
              </button>
            </fieldset>
          )}
        </div>
      )}

      <h3>Contamination events</h3>
      {board && board.events.length === 0 && (
        <Empty label="No contamination events recorded." />)}
      {board && board.events.map((event: ContaminationEvent) => (
        <div key={event.id}>
          <p>
            Event {event.id} — trigger {event.trigger}, status{" "}
            <strong>{event.status}</strong>; affected runs{" "}
            {event.affected_run_ids.join(", ") || "none"}; affected
            batches {event.affected_batch_ids.join(", ") || "none"};
            instruments {event.instruments.join(", ") || "none"};
            areas {event.areas.join(", ") || "none"}.
            {event.decontamination && (
              <> Decontamination: {event.decontamination.method} —
                swabs {event.decontamination.verification_swabs.map(
                  (s) => `${s.area}:${s.result}`).join(", ")}.</>
            )}
          </p>
          {event.status === "open" && (
            <fieldset>
              <legend>Decontaminate event {event.id}</legend>
              <label>
                Method{" "}
                <select aria-label={`Decontamination method ${event.id}`}
                        value={deconMethod[event.id] ?? ""}
                        disabled={!config.decontamination_methods.length}
                        onChange={(e) => setDeconMethod((prev) => ({
                          ...prev, [event.id]: e.target.value }))}>
                  <option value="">Select…</option>
                  <Options options={config.decontamination_methods} />
                </select>
              </label>
              <label>
                Verification swab result (all affected areas){" "}
                <select aria-label={`Verification swab ${event.id}`}
                        value={deconSwab[event.id] ?? "negative"}
                        onChange={(e) => setDeconSwab((prev) => ({
                          ...prev, [event.id]: e.target.value }))}>
                  <option value="negative">Negative</option>
                  <option value="positive">Positive</option>
                </select>
              </label>
              <button type="button"
                      disabled={!deconMethod[event.id] ||
                                !event.areas.length}
                      onClick={() => run(() => decontaminate(
                        client, event.id, {
                          method: deconMethod[event.id],
                          areas: event.areas,
                          verification_swabs: event.areas.map((area) => ({
                            area,
                            result: deconSwab[event.id] ?? "negative",
                          })),
                        }))}>
                Record decontamination {event.id}
              </button>
              {!event.areas.length && (
                <span> Disabled: this event names no areas to verify —
                  decontamination is recorded per area with swabs.</span>
              )}
            </fieldset>
          )}
        </div>
      ))}
      <fieldset>
        <legend>Raise contamination event</legend>
        <label>
          Instrument{" "}
          <input aria-label="Event instrument" value={eventInstrument}
                 onChange={(e) => setEventInstrument(e.target.value)} />
        </label>
        <label>
          Areas{" "}
          <select aria-label="Event areas" multiple value={eventAreas}
                  onChange={(e) => setEventAreas(
                    Array.from(e.target.selectedOptions, (o) => o.value))}>
            <Options options={config.areas} />
          </select>
        </label>
        <label>
          Note{" "}
          <input aria-label="Event note" value={eventNote}
                 onChange={(e) => setEventNote(e.target.value)} />
        </label>
        <button type="button"
                disabled={!eventInstrument && !eventAreas.length}
                onClick={() => run(() => createContaminationEvent(client, {
                  instrument_id: eventInstrument || undefined,
                  areas: eventAreas,
                  note: eventNote || undefined,
                }))}>
          Raise event
        </button>
        {!eventInstrument && !eventAreas.length && (
          <span> An instrument or at least one area is required.</span>
        )}
      </fieldset>
    </section>
  );
}
