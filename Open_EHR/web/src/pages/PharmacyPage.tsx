import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { codeLabel, refLabel } from "../api/display";
import type { MedicationAdministration, Role } from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role: Role;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

export function PharmacyPage({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canDispense = permissions["pharmacy.dispense"]?.[role] === true;
  const canAdminister = permissions["pharmacy.administer"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [quantities, setQuantities] = useState<Record<string, string>>({});
  const [lastDispenseId, setLastDispenseId] = useState<string | null>(null);
  const [wristband, setWristband] = useState("");
  const [scanCode, setScanCode] = useState("");
  const [scanDispenseId, setScanDispenseId] = useState("");
  const [lastAdministration, setLastAdministration] =
    useState<MedicationAdministration | null>(null);

  const queueState = useAsync(() => client.dispenseQueue(), [client, tick]);
  const stockState = useAsync(() => client.stock(), [client, tick]);
  const formularyState = useAsync(() => client.formulary(), [client]);

  const queue = queueState.data ?? [];
  const stock = stockState.data ?? [];

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
    <section>
      <h2>Pharmacy</h2>
      <p className="hint">
        The formulary is Studio configuration
        {formularyState.data?.version
          ? ` (terminology/formulary v${formularyState.data.version})`
          : ""}
        . Dispensing an active request creates a MedicationDispense and
        decrements stock for stock-tracked medications — dispensing more
        than the on-hand stock is refused. Med-admin is BCMA-lite: scan the
        wristband, the medication code and the dispense; the server checks
        right patient / right medication / right dispense and refuses a
        second administration of the same dispense.
      </p>
      {notice && (
        <p className="state state-empty" role="status">{notice}</p>
      )}
      {actionError && (
        <div className="state state-error" role="alert">{actionError}</div>
      )}

      <h3>Dispense queue</h3>
      {queueState.loading && <Loading label="Loading dispense queue…" />}
      {queueState.error != null && <ErrorView error={queueState.error} />}
      {!queueState.loading && !queueState.error && queue.length === 0 && (
        <Empty label="No active medication requests awaiting dispensing." />
      )}
      {queue.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Request</th>
              <th>Medication</th>
              <th>Code</th>
              <th>Patient</th>
              <th>Stock</th>
              <th>Qty</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {queue.map((item) => {
              const rid = item.request.id as string;
              const oversold = Boolean(
                item.stock_tracked && item.stock_quantity !== null &&
                Number(quantities[rid] || "1") > (item.stock_quantity ?? 0));
              return (
                <tr key={rid}>
                  <td>{rid}</td>
                  <td>{codeLabel(item.request.medicationCodeableConcept)}</td>
                  <td>{item.medication_code ?? "—"}</td>
                  <td>{refLabel(item.request.subject)}</td>
                  <td>
                    {item.stock_tracked
                      ? `${item.stock_quantity ?? 0} on hand`
                      : item.in_formulary
                        ? "not stock-tracked"
                        : "not in formulary"}
                  </td>
                  <td>
                    <input
                      aria-label={`Quantity for ${rid}`}
                      type="number"
                      min={1}
                      value={quantities[rid] ?? "1"}
                      onChange={(e) =>
                        setQuantities((q) => ({ ...q, [rid]: e.target.value }))}
                      style={{ width: "4.5rem" }}
                    />
                  </td>
                  <td>
                    {canDispense && (
                      <button
                        type="button"
                        disabled={busy || !item.in_formulary || oversold}
                        onClick={() =>
                          void perform("Dispense", async () => {
                            const outcome = await client.dispense(
                              rid, Number(quantities[rid] || "1"));
                            setLastDispenseId(outcome.dispense.id as string);
                            setScanDispenseId(outcome.dispense.id as string);
                            setScanCode(item.medication_code ?? "");
                            setWristband(
                              (item.request.subject?.reference ?? "").replace(
                                "Patient/", ""));
                          })
                        }
                      >
                        Dispense
                      </button>
                    )}
                    {oversold && <span className="hint"> exceeds stock</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {lastDispenseId && (
        <p className="hint">
          Last dispense: <code>{lastDispenseId}</code> — its id, the
          medication code and the wristband id have been filled into the
          med-admin scan form below.
        </p>
      )}

      <h3>Stock</h3>
      {stockState.loading && <Loading label="Loading stock…" />}
      {stockState.error != null && <ErrorView error={stockState.error} />}
      {stock.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Code</th>
              <th>Medication</th>
              <th>Tracked</th>
              <th>On hand</th>
            </tr>
          </thead>
          <tbody>
            {stock.map((item) => (
              <tr key={item.code}>
                <td>{item.code}</td>
                <td>{item.display ?? "—"}</td>
                <td>{item.stock_tracked ? "yes" : "no"}</td>
                <td>{item.stock_tracked ? (item.quantity ?? 0) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Med-admin scan (BCMA-lite)</h3>
      <form
        className="toolbar"
        onSubmit={(e) => {
          e.preventDefault();
          void perform("Administer", async () => {
            const admin = await client.administer({
              wristbandId: wristband.trim(),
              medicationCode: scanCode.trim(),
              dispenseId: scanDispenseId.trim(),
            });
            setLastAdministration(admin);
          });
        }}
      >
        <label>
          Wristband id{" "}
          <input
            aria-label="Wristband id"
            value={wristband}
            onChange={(e) => setWristband(e.target.value)}
            placeholder="pat-1"
          />
        </label>
        <label>
          Medication code{" "}
          <input
            aria-label="Medication code"
            value={scanCode}
            onChange={(e) => setScanCode(e.target.value)}
            placeholder="1049221"
          />
        </label>
        <label>
          Dispense id{" "}
          <input
            aria-label="Dispense id"
            value={scanDispenseId}
            onChange={(e) => setScanDispenseId(e.target.value)}
          />
        </label>
        {canAdminister && (
          <button
            type="submit"
            disabled={busy || !wristband.trim() || !scanCode.trim() || !scanDispenseId.trim()}
          >
            Administer
          </button>
        )}
      </form>
      {lastAdministration && (
        <p className="hint" role="status">
          Administered: {codeLabel(lastAdministration.medicationCodeableConcept)} to{" "}
          {refLabel(lastAdministration.subject)} at{" "}
          {lastAdministration.effectiveDateTime ?? "—"} (
          {lastAdministration.id}).
        </p>
      )}
    </section>
  );
}
