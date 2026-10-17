import { useState } from "react";
import type { ApiClient } from "../../api/client";
import { codeLabel } from "../../api/display";
import type { PlacedOrder } from "../../api/types";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

interface Props {
  client: ApiClient;
  patientId: string;
  /** orders.place in the runtime permissions matrix for the role. */
  canPlace: boolean;
}

function orderLabel(order: PlacedOrder): string {
  if (order.resourceType === "MedicationRequest") {
    const mr = order as { medicationCodeableConcept?: Parameters<typeof codeLabel>[0] };
    return mr.medicationCodeableConcept ? codeLabel(mr.medicationCodeableConcept) : (order.id ?? "");
  }
  const sr = order as { code?: Parameters<typeof codeLabel>[0] };
  return sr.code ? codeLabel(sr.code) : (order.id ?? "");
}

/** Lifecycle actions offered per state — the server enforces the
 *  published workflow/order-flow machine; this is only presentation. */
const ACTIONS: Record<string, { to: string; label: string }[]> = {
  draft: [
    { to: "signed", label: "Sign" },
    { to: "cancelled", label: "Cancel" },
  ],
  signed: [
    { to: "active", label: "Activate" },
    { to: "discontinued", label: "Discontinue" },
    { to: "cancelled", label: "Cancel" },
  ],
  active: [
    { to: "completed", label: "Complete" },
    { to: "discontinued", label: "Discontinue" },
    { to: "cancelled", label: "Cancel" },
  ],
};

/** Orders tab: place single orders or expand a published order set,
 *  then work the lifecycle. Order types, required fields and sets all
 *  come from published Studio config fetched at runtime. */
export function OrdersTab({ client, patientId, canPlace }: Props) {
  const { data, loading, error, reload } = useAsync(async () => {
    const [types, sets, list] = await Promise.all([
      client.orderTypes(),
      client.orderSets(),
      client.orders(patientId),
    ]);
    return {
      types: types.order_types.concepts.filter((c) => c.active !== false),
      sets: sets.sets,
      orders: list.items,
    };
  }, [client, patientId]);

  const [typeCode, setTypeCode] = useState("procedure");
  const [code, setCode] = useState("");
  const [display, setDisplay] = useState("");
  const [performerRole, setPerformerRole] = useState("");
  const [modality, setModality] = useState("");
  const [setKey, setSetKey] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (loading) return <Loading label="Loading orders…" />;
  if (error) return <ErrorView error={error} />;
  if (!data) return <Empty label="No order data." />;

  const selectedType = data.types.find((t) => t.code === typeCode);
  const needs = (field: string) => (selectedType?.requiredFields ?? []).includes(field);

  const run = async (fn: () => Promise<{ warnings: string[] } | unknown>) => {
    setBusy(true);
    setActionError(null);
    try {
      const out = (await fn()) as { warnings?: string[] };
      setWarnings(out.warnings ?? []);
      reload();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Order action failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-label="Orders">
      {canPlace && (
        <div className="order-place">
          <h4>Place an order</h4>
          <div className="form-field">
            <label htmlFor="order-type">Order type</label>
            <select
              id="order-type"
              value={typeCode}
              onChange={(e) => setTypeCode(e.target.value)}
            >
              {data.types.map((t) => (
                <option key={t.code} value={t.code}>
                  {t.display ?? t.code}
                </option>
              ))}
            </select>
          </div>
          <div className="form-field">
            <label htmlFor="order-code">Code</label>
            <input
              id="order-code"
              type="text"
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
          </div>
          <div className="form-field">
            <label htmlFor="order-display">Display (optional)</label>
            <input
              id="order-display"
              type="text"
              value={display}
              onChange={(e) => setDisplay(e.target.value)}
            />
          </div>
          {needs("performerRole") && (
            <div className="form-field">
              <label htmlFor="order-performer">Performing role</label>
              <input
                id="order-performer"
                type="text"
                value={performerRole}
                onChange={(e) => setPerformerRole(e.target.value)}
              />
            </div>
          )}
          {needs("modality") && (
            <div className="form-field">
              <label htmlFor="order-modality">Modality</label>
              <input
                id="order-modality"
                type="text"
                value={modality}
                onChange={(e) => setModality(e.target.value)}
              />
            </div>
          )}
          <button
            type="button"
            disabled={busy || !code}
            onClick={() =>
              run(() =>
                client.placeOrder({
                  type: typeCode,
                  code,
                  ...(display ? { display } : {}),
                  patient: patientId,
                  ...(performerRole ? { performerRole } : {}),
                  ...(modality ? { modality } : {}),
                }),
              )
            }
          >
            Place order
          </button>

          <h4>Place from an order set</h4>
          <div className="form-field">
            <label htmlFor="order-set">Order set</label>
            <select
              id="order-set"
              value={setKey}
              onChange={(e) => setSetKey(e.target.value)}
            >
              <option value="">Select a set…</option>
              {data.sets.map((s) => (
                <option key={s.key} value={s.key}>
                  {s.name ?? s.key} ({s.orders.length} orders)
                </option>
              ))}
            </select>
          </div>
          <button
            type="button"
            disabled={busy || !setKey}
            onClick={() =>
              run(() => client.placeOrderSet({ set: setKey, patient: patientId }))
            }
          >
            Place order set
          </button>
        </div>
      )}

      {warnings.length > 0 && (
        <ul role="status" className="order-warnings">
          {warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      {actionError && (
        <p role="alert" className="form-error">
          {actionError}
        </p>
      )}

      <h4>Orders for this patient</h4>
      {data.orders.length === 0 ? (
        <Empty label="No orders yet." />
      ) : (
        <ul className="order-list">
          {data.orders.map((order) => (
            <li key={order.id}>
              <span>
                {orderLabel(order)} — <span className="badge">{order.status}</span>
              </span>
              {canPlace &&
                order.id &&
                (ACTIONS[order.status ?? ""] ?? []).map((action) => (
                  <button
                    key={action.to}
                    type="button"
                    disabled={busy}
                    onClick={() => run(() => client.transitionOrder(order.id!, action.to))}
                  >
                    {action.label}
                  </button>
                ))}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
