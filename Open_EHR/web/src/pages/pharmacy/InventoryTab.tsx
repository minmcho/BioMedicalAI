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

/** Inventory & procurement tab: lot-level stock by location with PAR
 *  levels from the published pharmacy-ops config, low-stock/expiry
 *  worklists, audited adjustments, purchase orders whose receiving
 *  captures DSCSA lot/serial/expiry into the traceability ledger, and
 *  ADC restock lists generated from PAR + recent dispense activity. */
export function InventoryTab({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canInventory = permissions["pharmacy.inventory"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [locationFilter, setLocationFilter] = useState("all");
  const [adcLocation, setAdcLocation] = useState("adc-icu");
  // Adjustment form
  const [adjLot, setAdjLot] = useState("");
  const [adjDelta, setAdjDelta] = useState("");
  const [adjReason, setAdjReason] = useState("");
  // Purchase order form
  const [poSupplier, setPoSupplier] = useState("");
  const [poCode, setPoCode] = useState("");
  const [poQty, setPoQty] = useState("");
  // Receiving form
  const [recvPo, setRecvPo] = useState("");
  const [recvQty, setRecvQty] = useState("");
  const [recvLot, setRecvLot] = useState("");
  const [recvExpiry, setRecvExpiry] = useState("");
  const [recvSerials, setRecvSerials] = useState("");
  const [recvLocation, setRecvLocation] = useState("main-pharmacy");

  const invState = useAsync(() => client.inventory(), [client, tick]);
  const wlState = useAsync(() => client.inventoryWorklist(), [client, tick]);
  const poState = useAsync(() => client.purchaseOrders(), [client, tick]);
  const traceState = useAsync(() => client.traceability(), [client, tick]);
  const adcState = useAsync(
    () => client.adcRestockList(adcLocation),
    [client, adcLocation, tick],
  );

  const lots = invState.data?.items ?? [];
  const locations = invState.data?.locations ?? [];
  const orders = poState.data ?? [];
  const shownLots =
    locationFilter === "all"
      ? lots
      : lots.filter((l) => l.location === locationFilter);
  const openOrders = orders.filter((o) => o.status !== "received");
  const selectedPo = openOrders.find((o) => o.po_id === recvPo) ?? openOrders[0];

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
      <h3>Inventory worklist</h3>
      <p className="hint">
        PAR levels, reorder points and the expiry warning window are the
        published pharmacy-ops configuration — the worklist is computed
        from them, not from code.
      </p>
      {notice && (
        <p className="state state-empty" role="status">{notice}</p>
      )}
      {actionError && (
        <div className="state state-error" role="alert">{actionError}</div>
      )}
      {wlState.loading && <Loading label="Loading worklist…" />}
      {wlState.error != null && <ErrorView error={wlState.error} />}
      {wlState.data && (
        <>
          {wlState.data.low_stock.length === 0 ? (
            <Empty label="Nothing below its reorder point." />
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Medication</th>
                  <th>Location</th>
                  <th>On hand</th>
                  <th>PAR</th>
                  <th>Suggested order</th>
                </tr>
              </thead>
              <tbody>
                {wlState.data.low_stock.map((i) => (
                  <tr key={`${i.medication_code}-${i.location}`}>
                    <td>{i.medication_code}</td>
                    <td>{i.location}</td>
                    <td>{i.on_hand}</td>
                    <td>{i.par_level ?? "—"}</td>
                    <td>{i.suggested_order ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <h4>Expiring / expired lots</h4>
          {wlState.data.expiring.length + wlState.data.expired.length ===
          0 ? (
            <Empty label="No lots expiring inside the warning window." />
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Medication</th>
                  <th>Lot</th>
                  <th>Location</th>
                  <th>Expiry</th>
                  <th>Days</th>
                  <th>Qty</th>
                </tr>
              </thead>
              <tbody>
                {[...wlState.data.expired, ...wlState.data.expiring].map(
                  (l) => (
                    <tr key={`${l.medication_code}-${l.location}-${l.lot_number}`}>
                      <td>{l.medication_code}</td>
                      <td>{l.lot_number}</td>
                      <td>{l.location}</td>
                      <td>{l.expiry_date ?? "—"}</td>
                      <td>
                        {l.days_to_expiry ?? "—"}
                        {l.expired && <StatusBadge tone="bad">expired</StatusBadge>}
                      </td>
                      <td>{l.quantity}</td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
          )}
        </>
      )}

      <h3>Lots by location</h3>
      <label>
        Location{" "}
        <select
          aria-label="Lot location filter"
          value={locationFilter}
          onChange={(e) => setLocationFilter(e.target.value)}
        >
          <option value="all">All locations</option>
          {locations.map((l) => (
            <option key={l.code} value={l.code}>
              {l.label}
            </option>
          ))}
        </select>
      </label>
      {invState.loading && <Loading label="Loading inventory…" />}
      {invState.error != null && <ErrorView error={invState.error} />}
      {shownLots.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Medication</th>
              <th>Location</th>
              <th>Lot</th>
              <th>Expiry</th>
              <th>Qty</th>
              <th>PAR</th>
            </tr>
          </thead>
          <tbody>
            {shownLots.map((l) => (
              <tr key={`${l.medication_code}-${l.location}-${l.lot_number}`}>
                <td>{l.medication_code}</td>
                <td>{l.location}</td>
                <td>{l.lot_number}</td>
                <td>{l.expiry_date ?? "—"}</td>
                <td>{l.quantity}</td>
                <td>{l.par_level ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {canInventory && (
        <form
          className="toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            const lot = lots.find(
              (l) =>
                `${l.medication_code}|${l.location}|${l.lot_number}` === adjLot,
            );
            if (!lot) return;
            void perform("Adjust stock", () =>
              client.adjustStock({
                medicationCode: lot.medication_code,
                location: lot.location,
                lotNumber: lot.lot_number,
                delta: Number(adjDelta),
                reason: adjReason.trim(),
              }),
            );
          }}
        >
          <strong>Adjust stock</strong>
          <label>
            Lot{" "}
            <select
              aria-label="Adjustment lot"
              value={adjLot}
              onChange={(e) => setAdjLot(e.target.value)}
            >
              <option value="">Select a lot…</option>
              {lots.map((l) => (
                <option
                  key={`${l.medication_code}|${l.location}|${l.lot_number}`}
                  value={`${l.medication_code}|${l.location}|${l.lot_number}`}
                >
                  {l.medication_code} · {l.location} · {l.lot_number} (
                  {l.quantity})
                </option>
              ))}
            </select>
          </label>
          <label>
            Delta{" "}
            <input
              aria-label="Adjustment delta"
              type="number"
              value={adjDelta}
              onChange={(e) => setAdjDelta(e.target.value)}
              style={{ width: "5rem" }}
            />
          </label>
          <label>
            Reason{" "}
            <input
              aria-label="Adjustment reason"
              value={adjReason}
              onChange={(e) => setAdjReason(e.target.value)}
            />
          </label>
          <button
            type="submit"
            disabled={busy || !adjLot || !adjDelta || !adjReason.trim()}
          >
            Adjust
          </button>
        </form>
      )}

      <h3>Procurement</h3>
      {canInventory && (
        <form
          className="toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            void perform("Create purchase order", () =>
              client.createPurchaseOrder({
                supplier: poSupplier.trim(),
                lines: [
                  { medicationCode: poCode.trim(), quantity: Number(poQty) },
                ],
              }),
            );
          }}
        >
          <strong>New purchase order</strong>
          <label>
            Supplier{" "}
            <input
              aria-label="PO supplier"
              value={poSupplier}
              onChange={(e) => setPoSupplier(e.target.value)}
            />
          </label>
          <label>
            Medication code{" "}
            <input
              aria-label="PO medication code"
              value={poCode}
              onChange={(e) => setPoCode(e.target.value)}
            />
          </label>
          <label>
            Qty{" "}
            <input
              aria-label="PO quantity"
              type="number"
              min={1}
              value={poQty}
              onChange={(e) => setPoQty(e.target.value)}
              style={{ width: "5rem" }}
            />
          </label>
          <button
            type="submit"
            disabled={busy || !poSupplier.trim() || !poCode.trim() || !poQty}
          >
            Create PO
          </button>
        </form>
      )}
      {poState.loading && <Loading label="Loading purchase orders…" />}
      {poState.error != null && <ErrorView error={poState.error} />}
      {orders.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>PO</th>
              <th>Supplier</th>
              <th>Status</th>
              <th>Lines</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((o) => (
              <tr key={o.po_id}>
                <td>{o.po_id}</td>
                <td>{o.supplier}</td>
                <td>{o.status}</td>
                <td>
                  {o.lines
                    .map(
                      (l) =>
                        `${l.medication_code}: ${l.received_quantity}/${l.quantity}`,
                    )
                    .join("; ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {canInventory && selectedPo && (
        <form
          className="toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            const serials = recvSerials
              .split(",")
              .map((s) => s.trim())
              .filter(Boolean);
            void perform("Receive", () =>
              client.receivePurchaseOrder(selectedPo.po_id, {
                location: recvLocation,
                lines: [
                  {
                    medicationCode: selectedPo.lines[0].medication_code,
                    quantity: Number(recvQty),
                    lotNumber: recvLot.trim(),
                    expiryDate: recvExpiry,
                    ...(serials.length ? { serials } : {}),
                  },
                ],
              }),
            );
          }}
        >
          <strong>Receive against PO</strong>
          <label>
            PO{" "}
            <select
              aria-label="Receiving PO"
              value={selectedPo.po_id}
              onChange={(e) => setRecvPo(e.target.value)}
            >
              {openOrders.map((o) => (
                <option key={o.po_id} value={o.po_id}>
                  {o.po_id} ({o.supplier})
                </option>
              ))}
            </select>
          </label>
          <label>
            Into location{" "}
            <select
              aria-label="Receiving location"
              value={recvLocation}
              onChange={(e) => setRecvLocation(e.target.value)}
            >
              {locations.map((l) => (
                <option key={l.code} value={l.code}>
                  {l.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Qty{" "}
            <input
              aria-label="Receiving quantity"
              type="number"
              min={1}
              value={recvQty}
              onChange={(e) => setRecvQty(e.target.value)}
              style={{ width: "5rem" }}
            />
          </label>
          <label>
            Lot{" "}
            <input
              aria-label="Receiving lot"
              value={recvLot}
              onChange={(e) => setRecvLot(e.target.value)}
            />
          </label>
          <label>
            Expiry{" "}
            <input
              aria-label="Receiving expiry"
              type="date"
              value={recvExpiry}
              onChange={(e) => setRecvExpiry(e.target.value)}
            />
          </label>
          <label>
            Serials (comma separated, optional){" "}
            <input
              aria-label="Receiving serials"
              value={recvSerials}
              onChange={(e) => setRecvSerials(e.target.value)}
            />
          </label>
          <button
            type="submit"
            disabled={busy || !recvQty || !recvLot.trim() || !recvExpiry}
          >
            Receive
          </button>
        </form>
      )}

      <h3>ADC restock list</h3>
      <label>
        Cabinet location{" "}
        <select
          aria-label="ADC location"
          value={adcLocation}
          onChange={(e) => setAdcLocation(e.target.value)}
        >
          {locations.map((l) => (
            <option key={l.code} value={l.code}>
              {l.label}
            </option>
          ))}
        </select>
      </label>
      {adcState.loading && <Loading label="Loading restock list…" />}
      {adcState.error != null && <ErrorView error={adcState.error} />}
      {adcState.data && adcState.data.length === 0 && (
        <Empty label="This location is at or above PAR on every line." />
      )}
      {adcState.data && adcState.data.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Medication</th>
              <th>On hand</th>
              <th>PAR</th>
              <th>Restock</th>
              <th>Recent dispenses</th>
            </tr>
          </thead>
          <tbody>
            {adcState.data.map((i) => (
              <tr key={i.medication_code}>
                <td>{i.display ?? i.medication_code}</td>
                <td>{i.on_hand}</td>
                <td>{i.par_level}</td>
                <td>{i.suggested_quantity}</td>
                <td>{i.recent_dispenses}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>DSCSA traceability ledger</h3>
      {traceState.loading && <Loading label="Loading traceability…" />}
      {traceState.error != null && <ErrorView error={traceState.error} />}
      {traceState.data && traceState.data.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Event</th>
              <th>Medication</th>
              <th>Lot</th>
              <th>Serials</th>
              <th>Qty</th>
              <th>Reference</th>
              <th>Actor</th>
            </tr>
          </thead>
          <tbody>
            {traceState.data.slice(0, 25).map((e) => (
              <tr key={e.entry_id}>
                <td>{e.event}</td>
                <td>{e.medication_code}</td>
                <td>{e.lot_number ?? "—"}</td>
                <td>
                  {e.detail.serials?.length
                    ? e.detail.serials.join(", ")
                    : (e.serial ?? "—")}
                </td>
                <td>{e.quantity ?? "—"}</td>
                <td>{e.reference ?? "—"}</td>
                <td>{e.actor}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
