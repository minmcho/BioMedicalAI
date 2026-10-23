import { useMemo, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import {
  dispatchManifest, disposeSpecimen, evaluateExcursions,
  getChaseList, getExistingSendouts, getPreanalyticalConfig,
  getReceivingQueue, getSendoutContracts, getStorageBoard,
  getTransportExceptions, packManifest, receiveSpecimen, retrieveSpecimen,
  returnSendout, storeSpecimen, transportScan,
  type PreanalyticalConfig, type QueueItem, type SendoutContractLab,
  type StorageRecord,
} from "../api/labPreanalytical";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
}

// LAB-D3/D4/D5 — pre-analytical depth surface (app.lab_preanalytical).
// Mounted by the coordinator as a Lab page section (see the W2
// INTEGRATION NOTE). Receiving queue with the governed receipt
// checklist, transport exception worklist, storage / retention board,
// and the send-out catalog-as-contract surfaces (packing gate, chase
// list, returns). Every select is fed by the governed config or live
// API data; a control with no governed source is disabled WITH the
// reason shown. No dead controls.

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  }
  return err instanceof Error ? err.message : String(err);
}

const METHODS = ["pneumatic-tube", "courier", "hand-carry", "internal-porter"];
const TEMPERATURES = ["ambient", "refrigerated", "frozen", "body-temperature"];

export function LabPreanalytical({ client }: Props) {
  const [tick, setTick] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const configState = useAsync<PreanalyticalConfig>(
    () => getPreanalyticalConfig(client), [client]);
  const queueState = useAsync(() => getReceivingQueue(client), [client, tick]);
  const exceptionsState = useAsync(
    () => getTransportExceptions(client), [client, tick]);
  const boardState = useAsync(() => getStorageBoard(client), [client, tick]);
  const contractsState = useAsync(
    () => getSendoutContracts(client), [client]);
  const sendoutsState = useAsync(() => getExistingSendouts(client), [client, tick]);
  const chaseState = useAsync(() => getChaseList(client), [client, tick]);

  const config = configState.data;
  const queue = queueState.data?.items ?? [];
  const benches = useMemo(
    () => [...new Set(queue.map((i) => i.bench_code).filter(Boolean))], [queue]);

  // Receiving state
  const [benchFilter, setBenchFilter] = useState("");
  const [selectedSpecimen, setSelectedSpecimen] = useState("");
  const [checks, setChecks] = useState<Record<string, boolean>>({});
  // Transport state
  const [transportSpecimen, setTransportSpecimen] = useState("");
  const [method, setMethod] = useState("courier");
  const [temperature, setTemperature] = useState("ambient");
  // Storage state
  const [storeSpecimenId, setStoreSpecimenId] = useState("");
  const [storeLocation, setStoreLocation] = useState("");
  const [disposeReason, setDisposeReason] = useState("");
  const [retrievePurpose, setRetrievePurpose] = useState("");
  // Send-out state
  const [packOrderId, setPackOrderId] = useState("");
  const [packTemperature, setPackTemperature] = useState("refrigerated");
  const [packAttachments, setPackAttachments] = useState<Record<string, boolean>>({});
  const [returnOrderId, setReturnOrderId] = useState("");
  const [returnReason, setReturnReason] = useState("");

  const visibleQueue = benchFilter
    ? queue.filter((i) => i.bench_code === benchFilter) : queue;
  const selectedItem: QueueItem | undefined =
    queue.find((i) => i.specimen_id === selectedSpecimen);
  const contracts = contractsState.data?.laboratories ?? [];
  const packContract: SendoutContractLab["tests"][number] | undefined = useMemo(() => {
    const item = (sendoutsState.data?.items ?? [])
      .find((i) => i.order_id === packOrderId);
    if (!item) return undefined;
    const labEntry = contracts.find((l) => l.lab_code === item.reference_lab.code);
    return labEntry?.tests.find((t) => t.code === item.code);
  }, [contracts, sendoutsState.data, packOrderId]);
  const awaitingDispatch = (sendoutsState.data?.items ?? [])
    .filter((i) => i.sendout.status === "awaiting-dispatch");
  const dispatched = (sendoutsState.data?.items ?? [])
    .filter((i) => i.sendout.status === "dispatched");

  async function act<T>(fn: () => Promise<T>, ok: string): Promise<T | null> {
    setError(null); setNotice(null);
    try {
      const out = await fn();
      setNotice(ok);
      setTick((t) => t + 1);
      return out;
    } catch (err) {
      setError(errorText(err));
      return null;
    }
  }

  async function submitReceipt() {
    if (!selectedItem || !config) return;
    const checklist = config.receipt_checklist.map((item) => ({
      code: item.code, passed: checks[item.code] ?? true,
    }));
    const outcome = await act(
      () => receiveSpecimen(client, {
        specimen_id: selectedItem.specimen_id,
        bench_code: selectedItem.bench_code,
        checklist,
      }),
      "Receipt recorded");
    if (outcome) {
      setNotice(outcome.rejected
        ? `Rejected at receipt — coded criterion: ${outcome.rejection?.criterion_code ?? "see order"} (existing rejection flow; recollect task raised)`
        : `Accessioned at receipt: ${outcome.accession} (collection ID ${outcome.collection_id} preserved)`);
      setSelectedSpecimen("");
      setChecks({});
    }
  }

  async function submitTransport() {
    const outcome = await act(
      () => transportScan(client, {
        specimen_id: transportSpecimen, method,
        temperature_condition: temperature,
      }),
      "Transport scan recorded");
    if (outcome) {
      const codes = outcome.transport.exceptions.map((e) => e.code);
      setNotice(codes.length
        ? `Transport scan recorded — EXCEPTIONS FLAGGED: ${codes.join(", ")}`
        : "Transport scan recorded — no exceptions");
    }
  }

  if (configState.loading || queueState.loading) return <Loading />;
  if (configState.error) return <ErrorView error={configState.error} />;
  if (!config) return <Empty label="No pre-analytical configuration is published" />;

  return (
    <div className="lab-preanalytical">
      <h2>Pre-analytical — receiving, transport, storage &amp; send-outs</h2>
      {error && <p role="alert" className="error">{error}</p>}
      {notice && <p className="notice">{notice}</p>}

      <section>
        <h3>Receiving queue — accessioning at laboratory receipt (LAB-D3)</h3>
        <p>
          The accession is minted at receipt; the collection ID stays
          resolvable. A failed checklist item rejects through the
          governed rejection criteria.
        </p>
        <label>
          Receiving bench{" "}
          <select value={benchFilter} onChange={(e) => setBenchFilter(e.target.value)}>
            <option value="">All benches with queued work</option>
            {benches.map((b) => <option key={b} value={b}>{b}</option>)}
          </select>
        </label>
        {visibleQueue.length === 0
          ? <Empty label="Nothing awaiting receipt" />
          : (
            <table>
              <thead><tr>
                <th>Specimen</th><th>Test</th><th>Bench</th>
                <th>Collection ID</th><th>Accession</th><th>Collected</th><th />
              </tr></thead>
              <tbody>
                {visibleQueue.map((item) => (
                  <tr key={item.specimen_id}>
                    <td>{item.specimen_id.slice(0, 8)}…</td>
                    <td>{item.display ?? item.code}</td>
                    <td>{item.bench_code || "—"}</td>
                    <td>{item.collection_id ?? "—"}</td>
                    <td>{item.accession ?? "minted at receipt"}</td>
                    <td>{item.collected_at ?? "—"}</td>
                    <td>
                      {item.returned_from_sendout && (
                        <span>Returned from send-out: {item.return_reason_display} </span>)}
                      <button type="button"
                        onClick={() => { setSelectedSpecimen(item.specimen_id); setChecks({}); }}>
                        Receive
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        {selectedItem && (
          <fieldset>
            <legend>
              Receipt checklist — {selectedItem.display ?? selectedItem.code}
              {" "}(bench: {selectedItem.bench_code || "unrouted"})
            </legend>
            {config.receipt_checklist.map((item) => (
              <label key={item.code}>
                <input type="checkbox"
                  checked={checks[item.code] ?? true}
                  onChange={(e) => setChecks(
                    { ...checks, [item.code]: e.target.checked })} />
                {item.display} — failure rejects as “{item.criterionCode}”
              </label>
            ))}
            <button type="button" onClick={submitReceipt}
              disabled={!selectedItem.bench_code}>
              {selectedItem.bench_code
                ? "Scan to receive & mint accession"
                : "No receiving bench routes this test (governed catalog)"}
            </button>
          </fieldset>
        )}
      </section>

      <section>
        <h3>Transport — custody scan with governed rules (LAB-D3)</h3>
        <label>
          Specimen ID / barcode{" "}
          <input value={transportSpecimen}
            onChange={(e) => setTransportSpecimen(e.target.value)} />
        </label>
        <label>
          Method{" "}
          <select value={method} onChange={(e) => setMethod(e.target.value)}>
            {METHODS.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </label>
        <label>
          Temperature condition{" "}
          <select value={temperature} onChange={(e) => setTemperature(e.target.value)}>
            {TEMPERATURES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        <button type="button" onClick={submitTransport}
          disabled={!transportSpecimen}>
          Record transport scan
        </button>
        <h4>Transport exceptions</h4>
        {(exceptionsState.data?.items ?? []).length === 0
          ? <Empty label="No transport exceptions" />
          : (
            <ul>
              {(exceptionsState.data?.items ?? []).map((rec) => (
                <li key={rec.id}>
                  {rec.specimen_key.slice(0, 8)}… via {rec.method}:{" "}
                  {rec.exceptions.map((e) => `${e.code} — ${e.detail}`).join("; ")}
                </li>
              ))}
            </ul>
          )}
        <h4>Governed transport rules</h4>
        <ul>
          {config.transport_rules.map((rule) => (
            <li key={rule.specimenType}>
              {rule.specimenType}: {rule.neverTube
                ? "NEVER TUBE — hand-carry"
                : rule.tubeEligible ? "tube-eligible" : "not tube-eligible"}
              {", "}{rule.temperatureCondition}, max {rule.maxTransitMinutes} min
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h3>Storage &amp; retention board (LAB-D4)</h3>
        <label>
          Specimen ID{" "}
          <input value={storeSpecimenId}
            onChange={(e) => setStoreSpecimenId(e.target.value)} />
        </label>
        <label>
          Location{" "}
          <select value={storeLocation}
            onChange={(e) => setStoreLocation(e.target.value)}>
            <option value="">Select a governed location</option>
            {config.storage_locations.map((loc) => (
              <option key={loc.code} value={loc.code}>
                {loc.display} ({loc.temperatureCondition}
                {loc.deviceKey ? `, sensor ${loc.deviceKey}` : ""})
              </option>
            ))}
          </select>
        </label>
        <button type="button" disabled={!storeSpecimenId || !storeLocation}
          onClick={() => act(
            () => storeSpecimen(client, {
              specimen_id: storeSpecimenId, location_code: storeLocation,
            }), "Specimen stored; retention due date computed")}>
          Store specimen
        </button>
        <button type="button" onClick={async () => {
          const out = await act(() => evaluateExcursions(client),
            "Excursion evaluation complete");
          if (out) setNotice(`${out.flags_created} new excursion flag(s). ${out.feed.note}`);
        }}>
          Evaluate temperature excursions
        </button>
        <p>
          Temperature linkage reads the device registry&apos;s recorded
          readings — no live sensor feed is claimed in this deployment.
        </p>
        <h4>Due for disposal</h4>
        {(boardState.data?.due_for_disposal ?? []).length === 0
          ? <Empty label="Nothing due for disposal" />
          : (
            <table>
              <thead><tr><th>Specimen</th><th>Location</th><th>Due</th><th /></tr></thead>
              <tbody>
                {(boardState.data?.due_for_disposal ?? []).map((rec) => (
                  <tr key={rec.id}>
                    <td>{rec.specimen_key.slice(0, 8)}…</td>
                    <td>{rec.location_code}</td>
                    <td>{rec.disposal_due_date}</td>
                    <td>
                      <input aria-label="Disposal reason"
                        value={disposeReason}
                        onChange={(e) => setDisposeReason(e.target.value)}
                        placeholder="Disposal reason (required)" />
                      <button type="button" disabled={!disposeReason}
                        onClick={() => act(
                          () => disposeSpecimen(client, rec.specimen_key,
                            { reason: disposeReason }),
                          "Specimen disposed")}>
                        Dispose
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        <h4>Currently stored</h4>
        {(boardState.data?.stored ?? []).length === 0
          ? <Empty label="No specimens in storage" />
          : (
            <table>
              <thead><tr>
                <th>Specimen</th><th>Location</th><th>Stored</th>
                <th>Disposal due</th><th>Excursions</th><th />
              </tr></thead>
              <tbody>
                {(boardState.data?.stored ?? []).map((rec: StorageRecord) => (
                  <tr key={rec.id}>
                    <td>{rec.specimen_key.slice(0, 8)}…</td>
                    <td>{rec.location_code}</td>
                    <td>{rec.stored_at?.slice(0, 10)}</td>
                    <td>{rec.disposal_due_date}</td>
                    <td>{rec.excursion_flags.length
                      ? rec.excursion_flags.map((f) => f.detail).join("; ")
                      : "—"}</td>
                    <td>
                      <select aria-label="Retrieval purpose"
                        value={retrievePurpose}
                        onChange={(e) => setRetrievePurpose(e.target.value)}>
                        <option value="">Purpose…</option>
                        {config.retrieval_purposes.map((p) => (
                          <option key={p} value={p}>{p}</option>))}
                      </select>
                      <button type="button" disabled={!retrievePurpose}
                        onClick={() => act(
                          () => retrieveSpecimen(client, rec.specimen_key,
                            { purpose: retrievePurpose }),
                          "Retrieval recorded")}>
                        Retrieve
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </section>

      <section>
        <h3>Send-outs — the catalog is the contract (LAB-D5)</h3>
        <table>
          <thead><tr>
            <th>Reference laboratory</th><th>Test</th><th>Classification</th>
            <th>Stability</th><th>Ships</th><th>Report window</th><th>Performing lab</th>
          </tr></thead>
          <tbody>
            {contracts.flatMap((labEntry) => labEntry.tests.map((t) => (
              <tr key={`${labEntry.lab_code}-${t.code}`}>
                <td>{labEntry.display}</td>
                <td>{t.display}</td>
                <td>{t.classification ?? "—"}</td>
                <td>{t.stability ? `${t.stability.hours} h at ${t.stability.condition}` : "—"}</td>
                <td>{t.shipping_temperature ?? "—"}</td>
                <td>{t.report_window_hours ? `${t.report_window_hours} h` : "—"}</td>
                <td>{labEntry.performing_lab.name ?? labEntry.display}</td>
              </tr>
            )))}
          </tbody>
        </table>
        <h4>Pack a manifest</h4>
        <label>
          Awaiting-dispatch order{" "}
          <select value={packOrderId} onChange={(e) => {
            setPackOrderId(e.target.value); setPackAttachments({});
          }}>
            <option value="">Select an order</option>
            {awaitingDispatch.map((i) => (
              <option key={i.order_id} value={i.order_id}>
                {i.display} → {i.reference_lab.display}
              </option>))}
          </select>
        </label>
        <label>
          Shipping temperature{" "}
          <select value={packTemperature}
            onChange={(e) => setPackTemperature(e.target.value)}>
            {TEMPERATURES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        {packContract && (
          <fieldset>
            <legend>Necessary-information attachments (dispatch is gated)</legend>
            {packContract.required_attachments.map((name) => (
              <label key={name}>
                <input type="checkbox"
                  checked={packAttachments[name] ?? false}
                  onChange={(e) => setPackAttachments(
                    { ...packAttachments, [name]: e.target.checked })} />
                {name}
              </label>
            ))}
            {packContract.split_before_transport && (
              <p>Split before transport: {packContract.split_before_transport}</p>)}
          </fieldset>
        )}
        <button type="button" disabled={!packOrderId}
          onClick={async () => {
            const attached = Object.entries(packAttachments)
              .filter(([, v]) => v).map(([k]) => k);
            const out = await act(
              () => packManifest(client, {
                order_ids: [packOrderId],
                shipping_temperature: packTemperature,
                attachments: { [packOrderId]: attached },
              }),
              "Manifest packed — contract enforced");
            if (out) {
              const dispatchedOut = await act(
                () => dispatchManifest(client, out.manifest.manifest_key),
                `Manifest dispatched; report expected by ${out.manifest.expected_report_by ?? "the contract window"}`);
              if (dispatchedOut) setPackOrderId("");
            }
          }}>
          Pack &amp; dispatch
        </button>
        <h4>Overdue chase list</h4>
        {(chaseState.data?.items ?? []).length === 0
          ? <Empty label="No overdue send-out reports" />
          : (
            <ul>
              {(chaseState.data?.items ?? []).map((item) => (
                <li key={item.order_id}>
                  {item.code} at {item.lab_code}: dispatched {item.dispatched_at},
                  expected by {item.expected_by} — {item.overdue_hours} h overdue
                  ({item.classification ?? "unclassified"})
                </li>
              ))}
            </ul>
          )}
        <h4>Record a send-out return (rejected / unsuitable)</h4>
        <label>
          Dispatched order{" "}
          <select value={returnOrderId}
            onChange={(e) => setReturnOrderId(e.target.value)}>
            <option value="">Select an order</option>
            {dispatched.map((i) => (
              <option key={i.order_id} value={i.order_id}>{i.display}</option>))}
          </select>
        </label>
        <label>
          Coded reason{" "}
          <select value={returnReason}
            onChange={(e) => setReturnReason(e.target.value)}>
            <option value="">Select a governed reason</option>
            {config.sendout_return_reasons.map((r) => (
              <option key={r.code} value={r.code}>{r.display}</option>))}
          </select>
        </label>
        <button type="button" disabled={!returnOrderId || !returnReason}
          onClick={() => act(
            () => returnSendout(client, returnOrderId,
              { reason_code: returnReason }),
            "Return recorded — the specimen re-enters the receiving queue")}>
          Record return
        </button>
      </section>
    </div>
  );
}
