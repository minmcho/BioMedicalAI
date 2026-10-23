import { useState } from "react";
import type { ApiClient } from "../../api/client";
import type { Role } from "../../api/types";
import { useRuntimeConfig } from "../../auth/runtimeConfig";
import { StatusBadge } from "../../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";
import { errorText } from "./shared";

interface Props {
  client: ApiClient;
  role: Role;
}

/** Controlled substances tab (DEA rules as config): the perpetual
 *  count book by medication + location. Shift counts are two-person
 *  (counter + witness); a variance opens a discrepancy that must be
 *  resolved with a reason before the book balances; waste is a
 *  witnessed act that decrements the lot book. Every movement is in
 *  the audit chain server-side. */
export function ControlledSubstancesTab({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canCs = permissions["pharmacy.cs"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [countItem, setCountItem] = useState("");
  const [counted, setCounted] = useState("");
  const [countWitness, setCountWitness] = useState("");
  const [wasteItem, setWasteItem] = useState("");
  const [wasteLot, setWasteLot] = useState("");
  const [wasteQty, setWasteQty] = useState("");
  const [wasteReason, setWasteReason] = useState("");
  const [wasteWitness, setWasteWitness] = useState("");
  const [reasons, setReasons] = useState<Record<string, string>>({});

  const bookState = useAsync(() => client.csBook(), [client, tick]);
  const book = bookState.data;
  const items = book?.items ?? [];
  const stocked = items.filter((i) => i.expected > 0 || i.last_count);
  const itemOptions = (items.length ? items : []).map((i) => ({
    value: `${i.medication_code}|${i.location}`,
    label: `${i.display ?? i.medication_code} (Sch ${i.schedule ?? "—"}) · ${i.location} · expected ${i.expected}`,
    code: i.medication_code,
    location: i.location,
  }));
  const selectedCount =
    itemOptions.find((o) => o.value === countItem) ?? itemOptions[0];
  const selectedWaste =
    itemOptions.find((o) => o.value === wasteItem) ?? itemOptions[0];

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

  return (
    <div>
      <h3>Controlled-substance count book</h3>
      <p className="hint">
        Expected balances are the perpetual lot book. The book balances
        only when every discrepancy is resolved with a recorded reason.
      </p>
      {notice && (
        <p className="state state-empty" role="status">{notice}</p>
      )}
      {actionError && (
        <div className="state state-error" role="alert">{actionError}</div>
      )}
      {bookState.loading && <Loading label="Loading count book…" />}
      {bookState.error != null && <ErrorView error={bookState.error} />}
      {book && items.length === 0 && (
        <Empty label="No controlled substances on the formulary." />
      )}
      {book && items.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Medication</th>
              <th>Schedule</th>
              <th>Location</th>
              <th>Expected</th>
              <th>Open discrepancies</th>
              <th>Book</th>
            </tr>
          </thead>
          <tbody>
            {items.map((i) => (
              <tr key={`${i.medication_code}-${i.location}`}>
                <td>{i.display ?? i.medication_code}</td>
                <td>{i.schedule ?? "—"}</td>
                <td>{i.location}</td>
                <td>{i.expected}</td>
                <td>{i.unresolved_discrepancies}</td>
                <td>
                  {i.balanced ? (
                    <StatusBadge tone="ok">balanced</StatusBadge>
                  ) : (
                    <StatusBadge tone="bad">DOES NOT BALANCE</StatusBadge>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {book && book.open_discrepancies.length > 0 && (
        <>
          <h4>Open discrepancies</h4>
          <table className="table">
            <thead>
              <tr>
                <th>Medication</th>
                <th>Location</th>
                <th>Expected</th>
                <th>Counted</th>
                <th>Variance</th>
                <th>Counted by</th>
                <th>Resolution reason</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {book.open_discrepancies.map((d) => (
                <tr key={d.count_id}>
                  <td>{d.medication_code}</td>
                  <td>{d.location}</td>
                  <td>{d.expected}</td>
                  <td>{d.counted}</td>
                  <td>{d.variance}</td>
                  <td>
                    {d.counter} (witness {d.witness})
                  </td>
                  <td>
                    <input
                      aria-label={`Resolution reason for ${d.count_id}`}
                      value={reasons[d.count_id] ?? ""}
                      onChange={(e) =>
                        setReasons((r) => ({
                          ...r,
                          [d.count_id]: e.target.value,
                        }))}
                    />
                  </td>
                  <td>
                    {canCs && (
                      <button
                        type="button"
                        disabled={busy || !(reasons[d.count_id] ?? "").trim()}
                        onClick={() =>
                          void perform("Resolve discrepancy", () =>
                            client.csResolve(
                              d.count_id,
                              (reasons[d.count_id] ?? "").trim(),
                            ),
                          )}
                      >
                        Resolve
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {canCs && selectedCount && (
        <form
          className="toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            void perform("Record count", async () => {
              const out = await client.csCount({
                medicationCode: selectedCount.code,
                location: selectedCount.location,
                countedQuantity: Number(counted),
                witness: countWitness.trim(),
              });
              setNotice(
                out.status === "balanced"
                  ? `Count balanced: expected ${out.expected}, counted ${out.counted}.`
                  : `DISCREPANCY recorded: expected ${out.expected}, counted ${out.counted} (variance ${out.variance}). The book does not balance until it is resolved.`,
              );
            });
          }}
        >
          <strong>Shift count</strong>
          <label>
            Book line{" "}
            <select
              aria-label="Count book line"
              value={selectedCount.value}
              onChange={(e) => setCountItem(e.target.value)}
            >
              {itemOptions.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Counted{" "}
            <input
              aria-label="Counted quantity"
              type="number"
              min={0}
              value={counted}
              onChange={(e) => setCounted(e.target.value)}
              style={{ width: "5rem" }}
            />
          </label>
          <label>
            Witness{" "}
            <input
              aria-label="Count witness"
              value={countWitness}
              onChange={(e) => setCountWitness(e.target.value)}
              placeholder="second person's id"
            />
          </label>
          <button
            type="submit"
            disabled={busy || counted === "" || !countWitness.trim()}
          >
            Record count
          </button>
        </form>
      )}

      {canCs && selectedWaste && (
        <form
          className="toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            void perform("Record waste", () =>
              client.csWaste({
                medicationCode: selectedWaste.code,
                location: selectedWaste.location,
                lotNumber: wasteLot.trim(),
                quantity: Number(wasteQty),
                reason: wasteReason.trim(),
                witness: wasteWitness.trim(),
              }),
            );
          }}
        >
          <strong>Witnessed waste</strong>
          <label>
            Book line{" "}
            <select
              aria-label="Waste book line"
              value={selectedWaste.value}
              onChange={(e) => setWasteItem(e.target.value)}
            >
              {itemOptions.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Lot{" "}
            <input
              aria-label="Waste lot"
              value={wasteLot}
              onChange={(e) => setWasteLot(e.target.value)}
            />
          </label>
          <label>
            Qty{" "}
            <input
              aria-label="Waste quantity"
              type="number"
              min={1}
              value={wasteQty}
              onChange={(e) => setWasteQty(e.target.value)}
              style={{ width: "5rem" }}
            />
          </label>
          <label>
            Reason{" "}
            <input
              aria-label="Waste reason"
              value={wasteReason}
              onChange={(e) => setWasteReason(e.target.value)}
            />
          </label>
          <label>
            Witness{" "}
            <input
              aria-label="Waste witness"
              value={wasteWitness}
              onChange={(e) => setWasteWitness(e.target.value)}
              placeholder="second person's id"
            />
          </label>
          <button
            type="submit"
            disabled={
              busy ||
              !wasteLot.trim() ||
              !wasteQty ||
              !wasteReason.trim() ||
              !wasteWitness.trim()
            }
          >
            Record waste
          </button>
        </form>
      )}

      {book && book.recent_waste.length > 0 && (
        <>
          <h4>Recent waste</h4>
          <table className="table">
            <thead>
              <tr>
                <th>Medication</th>
                <th>Lot</th>
                <th>Qty</th>
                <th>Reason</th>
                <th>Wasted by</th>
                <th>Witness</th>
              </tr>
            </thead>
            <tbody>
              {book.recent_waste.map((w) => (
                <tr key={w.waste_id}>
                  <td>{w.medication_code}</td>
                  <td>{w.lot_number}</td>
                  <td>{w.quantity}</td>
                  <td>{w.reason}</td>
                  <td>{w.wasted_by}</td>
                  <td>{w.witness}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {stocked.length === 0 && book && (
        <p className="hint">
          No controlled-substance stock or counts yet at any location.
        </p>
      )}
    </div>
  );
}
