import { useEffect, useState } from "react";
import { ApiError, type ApiClient } from "../../api/client";
import type {
  BcmaOutcome,
  MarDoseStatus,
  PatientDispenseItem,
  PlacedOrder,
} from "../../api/types";
import { orderSigText } from "./OrdersTab";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

function scanErrorText(err: unknown): string {
  if (err instanceof ApiError)
    return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

interface Props {
  client: ApiClient;
  patientId: string;
}

/** Format a Date for a datetime-local input (local time, no seconds). */
function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

const STATUS_LABEL: Record<MarDoseStatus, string> = {
  given: "Given",
  due: "Due",
  missed: "Missed",
  scheduled: "Scheduled",
};

/** MAR tab: the medication administration record is COMPUTED by the
 *  server from signed/active medication orders and administration
 *  records. Administrations are recorded through the five-rights BCMA
 *  scan — the panel embedded below (Phase 11) or the pharmacy page —
 *  never typed into this table; missed is a computed state, not data. */
export function MarTab({ client, patientId }: Props) {
  const [fromInput, setFromInput] = useState(() =>
    toLocalInput(new Date(Date.now() - 24 * 3600_000)),
  );
  const [toInput, setToInput] = useState(() =>
    toLocalInput(new Date(Date.now() + 24 * 3600_000)),
  );
  const [range, setRange] = useState(() => ({
    from: new Date(Date.now() - 24 * 3600_000).toISOString(),
    to: new Date(Date.now() + 24 * 3600_000).toISOString(),
  }));
  const [tick, setTick] = useState(0);

  const { data, loading, error } = useAsync(
    () => client.marDue(patientId, range.from, range.to),
    [client, patientId, range, tick],
  );

  // Wave A (CHART-B3): sig text per order, for display on each MAR
  // row. Display-only — the MAR computation itself is unchanged. The
  // orders list is the same data the Orders tab loads; a client
  // without it (or a failed load) simply leaves rows without a sig.
  const [sigByOrder, setSigByOrder] = useState<Record<string, string>>({});
  useEffect(() => {
    let cancelled = false;
    if (typeof client.orders !== "function") return;
    client
      .orders(patientId)
      .then((list) => {
        if (cancelled) return;
        const map: Record<string, string> = {};
        for (const order of list.items as PlacedOrder[]) {
          const sig = orderSigText(order);
          if (order.id && sig) map[order.id] = sig;
        }
        setSigByOrder(map);
      })
      .catch(() => {
        if (!cancelled) setSigByOrder({});
      });
    return () => {
      cancelled = true;
    };
  }, [client, patientId, tick]);

  // ---- Phase 11: BCMA scan administration ----
  // Scan (keyboard wedge) the wristband payload, then the medication
  // label payload. The server runs the five rights: patient, medication
  // and dispense (lot) are absolute hard stops; dose, route and time
  // can be overridden only with a reason, which is audited and stamped
  // on the administration record.
  const [showScan, setShowScan] = useState(false);
  const [wristbandPayload, setWristbandPayload] = useState("");
  const [medLabelPayload, setMedLabelPayload] = useState("");
  const [overrideReason, setOverrideReason] = useState("");
  const [scanBusy, setScanBusy] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);
  const [scanOutcome, setScanOutcome] = useState<BcmaOutcome | null>(null);
  // The scan simulator is a dev/testing aid ONLY, clearly labelled; it
  // loads this patient's dispenses and generates real payloads through
  // the same endpoints a printer/scanner pair would use. It loads
  // lazily, only when opened.
  const [simOpen, setSimOpen] = useState(false);
  const [simDispenses, setSimDispenses] =
    useState<PatientDispenseItem[] | null>(null);
  const [simDispenseId, setSimDispenseId] = useState("");
  const [simError, setSimError] = useState<string | null>(null);

  async function administerScan() {
    setScanBusy(true);
    setScanError(null);
    setScanOutcome(null);
    try {
      const outcome = await client.bcmaAdminister({
        wristbandPayload: wristbandPayload.trim(),
        medLabelPayload: medLabelPayload.trim(),
        ...(overrideReason.trim()
          ? { overrideReason: overrideReason.trim() }
          : {}),
      });
      setScanOutcome(outcome);
      setTick((t) => t + 1); // the dose flips to "given" in the MAR
    } catch (err) {
      setScanError(`Scan refused: ${scanErrorText(err)}`);
    } finally {
      setScanBusy(false);
    }
  }

  async function toggleSimulator() {
    const next = !simOpen;
    setSimOpen(next);
    if (next && simDispenses === null) {
      try {
        const items = await client.patientDispenses(patientId);
        setSimDispenses(items);
        if (items.length > 0) setSimDispenseId(items[0].dispense_id);
      } catch (err) {
        setSimError(scanErrorText(err));
      }
    }
  }

  async function fillFromSimulator() {
    setSimError(null);
    try {
      const wb = await client.barcodeWristband(patientId);
      setWristbandPayload(wb.payload);
      if (simDispenseId) {
        const ml = await client.barcodeMedLabel(simDispenseId);
        setMedLabelPayload(ml.payload);
      }
    } catch (err) {
      setSimError(scanErrorText(err));
    }
  }

  return (
    <section aria-label="Medication administration record">
      <div className="mar-controls">
        <div className="form-field">
          <label htmlFor="mar-from">From</label>
          <input
            id="mar-from"
            type="datetime-local"
            value={fromInput}
            onChange={(e) => setFromInput(e.target.value)}
          />
        </div>
        <div className="form-field">
          <label htmlFor="mar-to">To</label>
          <input
            id="mar-to"
            type="datetime-local"
            value={toInput}
            onChange={(e) => setToInput(e.target.value)}
          />
        </div>
        <button
          type="button"
          onClick={() =>
            setRange({
              from: new Date(fromInput).toISOString(),
              to: new Date(toInput).toISOString(),
            })
          }
        >
          Apply window
        </button>
      </div>

      {loading && <Loading label="Loading MAR…" />}
      {error != null && <ErrorView error={error} />}
      {data && (
        <>
          <p role="status" className="mar-summary">
            Given {data.summary.given} · Due {data.summary.due} · Missed{" "}
            {data.summary.missed} · Scheduled {data.summary.scheduled}
          </p>
          {data.doses.length === 0 ? (
            <Empty label="No doses scheduled in this window." />
          ) : (
            <table className="lab-table mar-table">
              <thead>
                <tr>
                  <th scope="col">Medication</th>
                  <th scope="col">Sig</th>
                  <th scope="col">Due</th>
                  <th scope="col">Window</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {data.doses.map((dose, i) => (
                  <tr key={`${dose.order_id}-${dose.due_at}-${i}`}>
                    <td>{dose.medication_display ?? dose.medication_code ?? dose.order_id}</td>
                    <td>{sigByOrder[dose.order_id] ?? "—"}</td>
                    <td>{dose.due_at.replace("T", " ").slice(0, 16)}</td>
                    <td>
                      {dose.window_start.slice(11, 16)}–{dose.window_end.slice(11, 16)}
                    </td>
                    <td>
                      <span className="badge">{STATUS_LABEL[dose.status]}</span>
                      {dose.status === "given" && dose.administration_id
                        ? ` · ${dose.administration_id}`
                        : ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="mar-note">
            A dose is “missed” when its window has closed with no
            administration recorded — computed, never stored.
            Administrations are recorded by the BCMA scan panel below
            (five-rights checked server-side) or the pharmacy page scan.
          </p>
        </>
      )}

      <div className="mar-scan">
        <h4>BCMA scan administration</h4>
        <p className="hint">
          Scan the patient wristband, then the medication label (the
          fields take keyboard-wedge scanner input). The server checks
          the five rights — patient, medication, dispense/lot, dose,
          route and time window. A patient, medication or lot mismatch
          is an absolute hard stop; a dose, route or time failure needs
          an override reason, which is audited and stamped on the
          administration.
        </p>
        <button type="button" onClick={() => setShowScan((s) => !s)}>
          {showScan ? "Hide scan panel" : "Scan to administer"}
        </button>
        {showScan && (
          <form
            className="toolbar"
            onSubmit={(e) => {
              e.preventDefault();
              void administerScan();
            }}
          >
            <label>
              Wristband payload{" "}
              <input
                aria-label="Wristband payload"
                value={wristbandPayload}
                onChange={(e) => setWristbandPayload(e.target.value)}
                placeholder="OCWB1|…"
                autoFocus
              />
            </label>
            <label>
              Medication label payload{" "}
              <input
                aria-label="Medication label payload"
                value={medLabelPayload}
                onChange={(e) => setMedLabelPayload(e.target.value)}
                placeholder="OCML1|…"
              />
            </label>
            <label>
              Override reason (dose/route/time only){" "}
              <input
                aria-label="BCMA override reason"
                value={overrideReason}
                onChange={(e) => setOverrideReason(e.target.value)}
              />
            </label>
            <button
              type="submit"
              disabled={
                scanBusy || !wristbandPayload.trim() || !medLabelPayload.trim()
              }
            >
              Administer scan
            </button>
            <button type="button" onClick={() => void toggleSimulator()}>
              {simOpen ? "Hide scan simulator" : "Scan simulator"}
            </button>
            {simOpen && (
              <div className="hint">
                <strong>Scan simulator (development/testing only)</strong>{" "}
                — generates real payloads for this patient's dispenses
                through the barcode endpoints, standing in for a printed
                wristband/label and a scanner. It performs no
                administration by itself.
                {simError && (
                  <div className="state state-error" role="alert">
                    {simError}
                  </div>
                )}
                {simDispenses === null && !simError && (
                  <p>Loading dispenses…</p>
                )}
                {simDispenses !== null && simDispenses.length === 0 && (
                  <p>No dispenses for this patient yet.</p>
                )}
                {simDispenses !== null && simDispenses.length > 0 && (
                  <>
                    <label>
                      Dispense{" "}
                      <select
                        aria-label="Simulator dispense"
                        value={simDispenseId}
                        onChange={(e) => setSimDispenseId(e.target.value)}
                      >
                        {simDispenses.map((d) => (
                          <option key={d.dispense_id} value={d.dispense_id}>
                            {d.medication_display ?? d.medication_code ?? "?"} ·{" "}
                            {d.dispense_id} · lot {d.lot_number ?? "—"}
                          </option>
                        ))}
                      </select>
                    </label>
                    <button
                      type="button"
                      onClick={() => void fillFromSimulator()}
                    >
                      Fill scan fields
                    </button>
                  </>
                )}
              </div>
            )}
          </form>
        )}
        {scanError && (
          <div className="state state-error" role="alert">
            {scanError}
          </div>
        )}
        {scanOutcome && (
          <div role="status">
            <p>
              Administered ({scanOutcome.administration.id}) — five-rights
              results:
            </p>
            <ul>
              {scanOutcome.bcma.checks.map((c) => (
                <li key={c.right}>
                  {c.right}: <strong>{c.result}</strong> — {c.detail}
                </li>
              ))}
            </ul>
            {scanOutcome.bcma.override && (
              <p className="hint">
                Override recorded for{" "}
                {scanOutcome.bcma.override.rights.join(", ")}: “
                {scanOutcome.bcma.override.reason}”
              </p>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
