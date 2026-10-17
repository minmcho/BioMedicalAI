import { useMemo, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { codeLabel, refLabel } from "../api/display";
import type {
  BloodHold,
  BloodUnit,
  EqaRecord,
  FhirResource,
  FormDefinition,
  LabConcept,
  LabInstrument,
  LabWorkflowPayload,
  QcRun,
  QuestionnaireAnswerItem,
  Role,
  ServiceRequest,
  TatStats,
  VerificationItem,
} from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { FormRenderer } from "../components/FormRenderer";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role: Role;
}

// The seeded lab workflow (Studio config workflow/lab-order-flow v1). The
// server is the authority — this map only decides which buttons to offer;
// a transition the server rejects comes back as an error, shown inline.
const NEXT_ACTIONS: Record<string, Array<{ to: string; label: string; reason?: boolean }>> = {
  ordered: [
    { to: "collected", label: "Collect specimen" },
    { to: "cancelled", label: "Cancel order" },
  ],
  collected: [
    { to: "received", label: "Receive specimen" },
    { to: "rejected", label: "Reject specimen", reason: true },
  ],
  received: [
    { to: "in-process", label: "Start processing" },
    { to: "rejected", label: "Reject specimen", reason: true },
  ],
  "in-process": [{ to: "rejected", label: "Reject specimen", reason: true }],
};

const ORDER_STATES = [
  "ordered", "collected", "received", "in-process",
  "resulted", "verified", "released", "rejected", "cancelled",
];

// Button labels for workflow targets (the workflow payload names the
// states; the wording is presentation). "rejected" needs a reason.
const ACTION_LABELS: Record<string, { label: string; reason?: boolean }> = {
  collected: { label: "Collect specimen" },
  cancelled: { label: "Cancel order" },
  received: { label: "Receive specimen" },
  rejected: { label: "Reject specimen", reason: true },
  "in-process": { label: "Start processing" },
};

const FALLBACK_GOVERNED = ["resulted", "verified", "released"];

type NextAction = { to: string; label: string; reason?: boolean };

/** Derive the order-queue buttons from the published workflow payload
 *  (returned by GET /lab/catalog): every non-governed transition the
 *  config allows. Without a payload, the built-in map above applies —
 *  under the seeded defaults the two are identical. */
export function deriveNextActions(
  workflow: LabWorkflowPayload | null | undefined,
): Record<string, NextAction[]> {
  if (!workflow || !Array.isArray(workflow.transitions)) return NEXT_ACTIONS;
  const governed = new Set(workflow.governedStates ?? FALLBACK_GOVERNED);
  const derived: Record<string, NextAction[]> = {};
  for (const t of workflow.transitions) {
    if (governed.has(t.to)) continue;
    const meta = ACTION_LABELS[t.to] ?? { label: t.to };
    (derived[t.from] ??= []).push({ to: t.to, ...meta });
  }
  return derived;
}

export function deriveOrderStates(
  workflow: LabWorkflowPayload | null | undefined,
): string[] {
  return workflow && Array.isArray(workflow.states) && workflow.states.length > 0
    ? workflow.states
    : ORDER_STATES;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

/** Read a string extension value off a FHIR resource (Phase 6B stores
 *  reflex markers there). */
function extValue(resource: FhirResource | undefined, url: string): string | undefined {
  const exts = (resource as { extension?: Array<{ url?: string; valueString?: string }> } | undefined)
    ?.extension;
  return exts?.find((e) => e.url === url)?.valueString;
}

function StatusBadge({ status }: { status?: string }) {
  return <span className="badge">{status ?? "—"}</span>;
}

export function LabPage({ client, role }: Props) {
  // Capability flags come from the published permissions matrix (merged
  // over the defaults by the runtime-config provider); with the
  // defaults they are exactly the role sets this page used before.
  const { permissions } = useRuntimeConfig();
  const canWork = permissions["lab.work"]?.[role] === true;
  const canOrder = permissions["lab.order"]?.[role] === true;
  const canVerify = permissions["lab.verify"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const catalogState = useAsync(() => client.labCatalog(), [client]);
  const ordersState = useAsync(() => client.labOrders(), [client, tick]);
  const queueState = useAsync(() => client.verificationQueue(), [client, tick]);
  const specimensState = useAsync(() => client.labSpecimens(), [client, tick]);
  const qcState = useAsync(() => client.qcRuns(), [client, tick]);

  const concepts: LabConcept[] = useMemo(
    () => catalogState.data?.catalog?.concepts ?? [],
    [catalogState.data],
  );
  const conceptByCode = useMemo(() => {
    const map = new Map<string, LabConcept>();
    for (const c of concepts) map.set(c.code, c);
    return map;
  }, [concepts]);
  const orderable = concepts.filter((c) => c.active !== false);
  const analytes = concepts.filter((c) => !c.panel);
  const nextActions = useMemo(
    () => deriveNextActions(catalogState.data?.workflow),
    [catalogState.data],
  );
  const orderStates = useMemo(
    () => deriveOrderStates(catalogState.data?.workflow),
    [catalogState.data],
  );
  // Phase 6B bench routing comes from the published catalog's `bench`
  // field: microbiology and pathology orders are worked in their own
  // bench sections below, not in the standard order queue / result
  // entry (their workflows differ from the standard machine).
  const benchOf = (o: ServiceRequest) =>
    conceptByCode.get(o.code?.coding?.[0]?.code ?? "")?.bench;
  const allOrders = ordersState.data ?? [];
  const standardOrders = allOrders.filter((o) => {
    const b = benchOf(o);
    return b !== "microbiology" && b !== "pathology";
  });
  const microOrders = allOrders.filter((o) => benchOf(o) === "microbiology");
  const pathologyOrders = allOrders.filter((o) => benchOf(o) === "pathology");
  const rejectionCriteria =
    catalogState.data?.catalog?.rejectionCriteria ?? [];

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
      <h2>Laboratory</h2>
      <p className="hint">
        Order → specimen → result → QC. The catalog, reference ranges and the
        workflow are Studio configuration
        {catalogState.data?.catalog_version
          ? ` (terminology/lab-catalog v${catalogState.data.catalog_version}, workflow/lab-order-flow v${catalogState.data.workflow_version ?? "?"})`
          : ""}
        . Results are drafts until a clinician verifies and then releases
        them — nothing here reaches the inbox or the patient portal before
        release.
      </p>
      {notice && (
        <p className="state state-empty" role="status">{notice}</p>
      )}
      {actionError && (
        <div className="state state-error" role="alert">{actionError}</div>
      )}

      <CollectionWorklistSection
        client={client}
        canWork={canWork}
        busy={busy}
        perform={perform}
        tick={tick}
      />

      <OrdersSection
        client={client}
        ordersState={{ ...ordersState, data: standardOrders }}
        orderable={orderable}
        canWork={canWork}
        canOrder={canOrder}
        nextActions={nextActions}
        orderStates={orderStates}
        rejectionCriteria={rejectionCriteria}
        busy={busy}
        perform={perform}
      />

      <VerificationSection
        queueState={queueState}
        canVerify={canVerify}
        busy={busy}
        perform={perform}
        client={client}
      />

      <CriticalSection
        client={client}
        canVerify={canVerify}
        busy={busy}
        perform={perform}
        tick={tick}
      />

      <ResultEntrySection
        client={client}
        orders={standardOrders}
        conceptByCode={conceptByCode}
        canWork={canWork}
        busy={busy}
        perform={perform}
      />

      <IngestSection client={client} canWork={canWork} busy={busy} perform={perform} />

      <SpecimensSection specimensState={specimensState} />

      <QcSection
        qcState={qcState}
        analytes={analytes}
        canWork={canWork}
        busy={busy}
        perform={perform}
        client={client}
      />

      <MicroSection
        client={client}
        orders={microOrders}
        canWork={canWork}
        canVerify={canVerify}
        busy={busy}
        perform={perform}
        tick={tick}
      />

      <BloodBankSection
        client={client}
        orders={standardOrders}
        catalog={catalogState.data?.catalog ?? null}
        canWork={canWork}
        canVerify={canVerify}
        busy={busy}
        perform={perform}
        tick={tick}
      />

      <PathologySection
        client={client}
        orders={pathologyOrders}
        canWork={canWork}
        canVerify={canVerify}
        busy={busy}
        perform={perform}
        tick={tick}
      />

      <OperationsSection
        client={client}
        analytes={analytes}
        canWork={canWork}
        busy={busy}
        perform={perform}
        tick={tick}
      />
    </section>
  );
}

// --------------------------------------------------------------------------

function OrdersSection({
  client, ordersState, orderable, canWork, canOrder, nextActions,
  orderStates, rejectionCriteria, busy, perform,
}: {
  client: ApiClient;
  ordersState: ReturnType<typeof useAsync<ServiceRequest[]>>;
  orderable: LabConcept[];
  canWork: boolean;
  canOrder: boolean;
  nextActions: Record<string, NextAction[]>;
  orderStates: string[];
  /** Published specimen-rejection criteria (catalog config, Phase 6B). */
  rejectionCriteria: Array<{ code: string; display: string; active?: boolean }>;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [statusFilter, setStatusFilter] = useState("");
  const [patientId, setPatientId] = useState("pat-1");
  const [code, setCode] = useState("");
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [reasonCodes, setReasonCodes] = useState<Record<string, string>>({});

  const orders = (ordersState.data ?? []).filter(
    (o) => !statusFilter || o.status === statusFilter,
  );
  const selectedCode = code || orderable[0]?.code || "";

  return (
    <>
      <h3>Order queue</h3>
      {canOrder && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            void perform("Place order", () =>
              client.createLabOrder({ code: selectedCode, patientId: patientId.trim() }),
            );
          }}
        >
          <label className="field">
            Patient id
            <input value={patientId} onChange={(e) => setPatientId(e.target.value)} />
          </label>
          <label className="field">
            Test / panel
            <select value={selectedCode} onChange={(e) => setCode(e.target.value)}>
              {orderable.map((c) => (
                <option key={c.code} value={c.code}>
                  {c.display} ({c.code}){c.panel ? " — panel" : ""}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={busy || !selectedCode || !patientId.trim()}>
            Place order
          </button>
        </form>
      )}
      <form className="inline-form" onSubmit={(e) => e.preventDefault()}>
        <label className="field">
          Status filter
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="">All statuses</option>
            {orderStates.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>
      </form>

      {ordersState.loading ? (
        <Loading label="Loading lab orders…" />
      ) : ordersState.error ? (
        <ErrorView error={ordersState.error} />
      ) : orders.length === 0 ? (
        <Empty label="No lab orders match." />
      ) : (
        <table className="lab-table">
          <thead>
            <tr>
              <th>Order</th><th>Patient</th><th>Test</th><th>Status</th><th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((order) => (
              <tr key={order.id}>
                <td>{order.id}</td>
                <td>{refLabel(order.subject)}</td>
                <td>
                  {codeLabel(order.code)}
                  {extValue(order, "urn:opencare:extension/reflex") && (
                    <span className="badge badge-warn">
                      reflex: {extValue(order, "urn:opencare:extension/reflex")}
                    </span>
                  )}
                </td>
                <td>
                  <StatusBadge status={order.status} />
                  {order.statusReason?.text && (
                    <span className="result-meta"> {order.statusReason.text}</span>
                  )}
                </td>
                <td>
                  {canWork ? (
                    <span className="appt-actions">
                      {(nextActions[order.status ?? ""] ?? []).map((action) => (
                        <span key={action.to}>
                          {action.reason && (
                            <input
                              aria-label={`Reason for rejecting order ${order.id}`}
                              placeholder="Rejection reason"
                              value={reasons[order.id ?? ""] ?? ""}
                              onChange={(e) =>
                                setReasons((r) => ({ ...r, [order.id ?? ""]: e.target.value }))
                              }
                            />
                          )}
                          {action.reason && rejectionCriteria.length > 0 && (
                            <select
                              aria-label={`Rejection criterion for order ${order.id}`}
                              value={reasonCodes[order.id ?? ""] ?? ""}
                              onChange={(e) =>
                                setReasonCodes((r) => ({ ...r, [order.id ?? ""]: e.target.value }))
                              }
                            >
                              <option value="">Criterion…</option>
                              {rejectionCriteria
                                .filter((c) => c.active !== false)
                                .map((c) => (
                                  <option key={c.code} value={c.code}>{c.display}</option>
                                ))}
                            </select>
                          )}
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() =>
                              void perform(`${action.label} (${order.id})`, () =>
                                client.transitionLabOrder(order.id ?? "", action.to, {
                                  ...(action.reason
                                    ? {
                                        reason: reasons[order.id ?? ""] ?? "",
                                        ...(reasonCodes[order.id ?? ""]
                                          ? { reason_code: reasonCodes[order.id ?? ""] }
                                          : {}),
                                      }
                                    : {}),
                                }),
                              )
                            }
                          >
                            {action.label}
                          </button>
                        </span>
                      ))}
                      {order.status === "rejected" && (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() =>
                            void perform(`Recollect order ${order.id}`, () =>
                              client.recollectLabOrder(order.id ?? ""),
                            )
                          }
                        >
                          Request recollection
                        </button>
                      )}
                      {(order.status === "resulted" || order.status === "verified") && (
                        <span className="hint">Use the verification queue below</span>
                      )}
                    </span>
                  ) : (
                    <span className="hint">Read-only</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

// --------------------------------------------------------------------------

function VerificationSection({
  queueState, canVerify, busy, perform, client,
}: {
  queueState: ReturnType<typeof useAsync<{ items: VerificationItem[]; total: number }>>;
  canVerify: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  client: ApiClient;
}) {
  const [amendReasons, setAmendReasons] = useState<Record<string, string>>({});
  return (
    <>
      <h3>Verification queue</h3>
      {queueState.loading ? (
        <Loading label="Loading verification queue…" />
      ) : queueState.error ? (
        <ErrorView error={queueState.error} />
      ) : !queueState.data || queueState.data.items.length === 0 ? (
        <Empty label="Nothing awaiting verification or release." />
      ) : (
        <ul className="result-list">
          {queueState.data.items.map((item) => (
            <li key={item.report.id} className="result-row">
              <span className="result-type">{item.stage.replace(/-/g, " ")}</span>
              <span>
                {codeLabel(item.report.code)} — order {item.order_id ?? "—"}
                {item.critical && <span className="badge badge-critical">CRITICAL</span>}
                {item.qc_blocked.length > 0 && (
                  <span className="badge badge-warn">
                    QC blocked: {item.qc_blocked.map((b) => b.analyte_code).join(", ")}
                  </span>
                )}
                {(item.report.status === "amended" || item.amended) && (
                  <span className="badge badge-warn">
                    AMENDED
                    {item.amendment?.reason ? ` — ${item.amendment.reason}` : ""}
                  </span>
                )}
                {(item.delta_flags ?? []).map((f) => (
                  <span key={f.code} className="badge badge-warn">
                    Δ {f.code}: {f.previous_value} → {f.current_value}
                    {f.change_pct != null
                      ? ` (${f.change_pct > 0 ? "+" : ""}${f.change_pct.toFixed(1)}%)`
                      : ""}
                  </span>
                ))}
                <br />
                <span className="result-meta">
                  {item.results
                    .map((r) => `${r.display ?? r.code ?? "?"}: ${r.value ?? r.value_text ?? "—"} ${r.unit ?? ""}${r.interpretation ? ` [${r.interpretation}]` : ""}`)
                    .join(" · ")}
                </span>
                {item.critical && (
                  <>
                    <br />
                    <span className="result-meta">
                      Critical notification: {item.notification.state}
                      {item.notification.state === "pending" &&
                        " — recorded from Critical results after release"}
                    </span>
                  </>
                )}
              </span>
              <span className="appt-actions">
                {canVerify && item.stage === "awaiting-verification" && (
                  <button
                    type="button"
                    disabled={busy || item.qc_blocked.length > 0}
                    onClick={() =>
                      void perform(`Verify report ${item.report.id}`, () =>
                        client.verifyLabReport(item.report.id ?? ""),
                      )
                    }
                  >
                    Verify
                  </button>
                )}
                {canVerify && item.stage === "awaiting-release" && (
                  <button
                    type="button"
                    disabled={busy || item.qc_blocked.length > 0}
                    onClick={() =>
                      void perform(`Release report ${item.report.id}`, () =>
                        client.releaseLabReport(item.report.id ?? ""),
                      )
                    }
                  >
                    Release
                  </button>
                )}
                {canVerify && item.stage === "released" && (
                  <span>
                    <input
                      aria-label={`Amendment reason for report ${item.report.id}`}
                      placeholder="Amendment reason (required)"
                      value={amendReasons[item.report.id ?? ""] ?? ""}
                      onChange={(e) =>
                        setAmendReasons((m) => ({
                          ...m,
                          [item.report.id ?? ""]: e.target.value,
                        }))
                      }
                    />
                    <button
                      type="button"
                      disabled={busy || !(amendReasons[item.report.id ?? ""] ?? "").trim()}
                      onClick={() =>
                        void perform(`Amend report ${item.report.id}`, () =>
                          client.amendLabReport(item.report.id ?? "", {
                            reason: (amendReasons[item.report.id ?? ""] ?? "").trim(),
                          }),
                        )
                      }
                    >
                      Amend report
                    </button>
                  </span>
                )}
                {!canVerify && <span className="hint">Verification is clinician/admin only</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

// --------------------------------------------------------------------------

/** Released critical reports and their notification lifecycle
 *  (pending -> notified -> acknowledged). The notification is a phone
 *  call with read-back; the server refuses to record one without it. */
function CriticalSection({
  client, canVerify, busy, perform, tick,
}: {
  client: ApiClient;
  canVerify: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  tick: number;
}) {
  const criticalState = useAsync(
    () =>
      typeof client.criticalReports === "function"
        ? client.criticalReports()
        : Promise.resolve({ items: [] as VerificationItem[], total: 0 }),
    [client, tick],
  );
  const [notifiedTo, setNotifiedTo] = useState<Record<string, string>>({});
  const [readBack, setReadBack] = useState<Record<string, boolean>>({});

  const items = criticalState.data?.items ?? [];
  return (
    <>
      <h3>Critical results</h3>
      <p className="hint">
        Released reports containing a critical value. Each one must be
        phoned to the care team with read-back, then acknowledged —
        pending items stay on this list until both steps are recorded.
      </p>
      {criticalState.loading ? (
        <Loading label="Loading critical results…" />
      ) : criticalState.error ? (
        <ErrorView error={criticalState.error} />
      ) : items.length === 0 ? (
        <Empty label="No released critical results." />
      ) : (
        <ul className="result-list">
          {items.map((item) => {
            const id = item.report.id ?? "";
            const note = item.notification;
            return (
              <li key={id} className="result-row">
                <span className="result-type">critical result</span>
                <span>
                  {codeLabel(item.report.code)} — order {item.order_id ?? "—"}
                  <span className="badge badge-critical">CRITICAL</span>
                  <br />
                  <span className="result-meta">
                    Notification: {note.state}
                    {note.state !== "pending" && note.notified_to
                      ? ` — notified ${note.notified_to} by ${note.notified_by ?? "?"}${note.read_back ? " (read-back confirmed)" : ""}`
                      : ""}
                    {note.state === "acknowledged"
                      ? ` · acknowledged by ${note.acknowledged_by ?? "?"}`
                      : ""}
                  </span>
                </span>
                <span className="appt-actions">
                  {canVerify && note.state === "pending" && (
                    <span>
                      <input
                        aria-label={`Notified person for report ${id}`}
                        placeholder="Notified (name/role)"
                        value={notifiedTo[id] ?? ""}
                        onChange={(e) =>
                          setNotifiedTo((m) => ({ ...m, [id]: e.target.value }))
                        }
                      />
                      <label>
                        <input
                          type="checkbox"
                          aria-label={`Read-back confirmed for report ${id}`}
                          checked={readBack[id] ?? false}
                          onChange={(e) =>
                            setReadBack((m) => ({ ...m, [id]: e.target.checked }))
                          }
                        />{" "}
                        Read-back confirmed
                      </label>
                      <button
                        type="button"
                        disabled={busy || !(notifiedTo[id] ?? "").trim() || !readBack[id]}
                        onClick={() =>
                          void perform(`Record notification for report ${id}`, () =>
                            client.notifyLabReport(id, {
                              notifiedTo: (notifiedTo[id] ?? "").trim(),
                              readBack: true,
                            }),
                          )
                        }
                      >
                        Record notification
                      </button>
                    </span>
                  )}
                  {canVerify && note.state === "notified" && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        void perform(`Acknowledge report ${id}`, () =>
                          client.acknowledgeLabReport(id),
                        )
                      }
                    >
                      Acknowledge
                    </button>
                  )}
                  {!canVerify && note.state !== "acknowledged" && (
                    <span className="hint">Notification is clinician/admin only</span>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

// --------------------------------------------------------------------------

function ResultEntrySection({
  client, orders, conceptByCode, canWork, busy, perform,
}: {
  client: ApiClient;
  orders: ServiceRequest[];
  conceptByCode: Map<string, LabConcept>;
  canWork: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const eligible = orders.filter((o) => o.status === "received" || o.status === "in-process");
  const [orderId, setOrderId] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [localError, setLocalError] = useState<string | null>(null);

  const selected = eligible.find((o) => o.id === orderId) ?? eligible[0];
  const orderConcept = selected ? conceptByCode.get(selected.code?.coding?.[0]?.code ?? "") : undefined;
  const expectedCodes = orderConcept
    ? orderConcept.panel
      ? orderConcept.components ?? []
      : [orderConcept.code]
    : [];

  if (!canWork) return null;
  return (
    <>
      <h3>Result entry</h3>
      {eligible.length === 0 ? (
        <Empty label="No received orders are waiting for results." />
      ) : (
        <form
          className="config-editor"
          onSubmit={(e) => {
            e.preventDefault();
            if (!selected?.id) return;
            const results = expectedCodes
              .filter((c) => (values[`${selected.id}:${c}`] ?? "").trim() !== "")
              .map((c) => {
                const concept = conceptByCode.get(c);
                // Qualitative concepts (catalog config, Phase 6B) send
                // the coded text value; numeric concepts keep the
                // numeric path, exactly as before.
                return concept?.qualitative
                  ? { code: c, value: (values[`${selected.id}:${c}`] ?? "").trim() }
                  : { code: c, value: Number(values[`${selected.id}:${c}`]) };
              });
            if (
              results.length === 0 ||
              results.some(
                (r) => typeof r.value === "number" && Number.isNaN(r.value),
              )
            ) {
              setLocalError("Enter at least one result value.");
              return;
            }
            setLocalError(null);
            void perform(`Enter results (${selected.id})`, () =>
              client.enterLabResults(selected.id ?? "", results),
            );
          }}
        >
          <label className="field">
            Order awaiting results
            <select value={selected?.id ?? ""} onChange={(e) => setOrderId(e.target.value)}>
              {eligible.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.id} — {codeLabel(o.code)} ({o.status})
                </option>
              ))}
            </select>
          </label>
          {expectedCodes.map((c) => {
            const concept = conceptByCode.get(c);
            const range = concept?.referenceRange;
            if (concept?.qualitative) {
              return (
                <label key={c} className="field">
                  {concept.display} ({c}) — qualitative
                  <select
                    value={values[`${selected?.id}:${c}`] ?? ""}
                    onChange={(e) =>
                      setValues((v) => ({ ...v, [`${selected?.id}:${c}`]: e.target.value }))
                    }
                  >
                    <option value="">Select…</option>
                    {(concept.values ?? []).map((v) => (
                      <option key={v} value={v}>{v}</option>
                    ))}
                  </select>
                </label>
              );
            }
            return (
              <label key={c} className="field">
                {concept?.display ?? c} ({c})
                {range ? ` — ref ${range.low ?? "…"}–${range.high ?? "…"} ${range.unit ?? ""}` : ""}
                <input
                  inputMode="decimal"
                  placeholder={concept?.unit ?? range?.unit ?? "value"}
                  value={values[`${selected?.id}:${c}`] ?? ""}
                  onChange={(e) =>
                    setValues((v) => ({ ...v, [`${selected?.id}:${c}`]: e.target.value }))
                  }
                />
              </label>
            );
          })}
          {localError && (
            <div className="state state-error" role="alert">{localError}</div>
          )}
          <button type="submit" disabled={busy}>Save draft results</button>
          <p className="hint">
            Saved results are preliminary drafts: flagged against the
            configured ranges, invisible to the inbox and portal until
            verified and released.
          </p>
        </form>
      )}
    </>
  );
}

// --------------------------------------------------------------------------

function IngestSection({
  client, canWork, busy, perform,
}: {
  client: ApiClient;
  canWork: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [format, setFormat] = useState<"hl7" | "csv">("hl7");
  const [content, setContent] = useState("");
  const [summary, setSummary] = useState<string | null>(null);

  if (!canWork) return null;
  return (
    <>
      <h3>Analyser ingest</h3>
      <form
        className="config-editor"
        onSubmit={(e) => {
          e.preventDefault();
          void perform("Ingest analyser results", async () => {
            const outcome = await client.ingestLab(format, content);
            setSummary(
              `Ingested ${outcome.result_count} result(s) into ${outcome.report_count} draft report(s).`,
            );
            return outcome;
          });
        }}
      >
        <label className="field">
          Format
          <select value={format} onChange={(e) => setFormat(e.target.value as "hl7" | "csv")}>
            <option value="hl7">HL7 v2 ORU^R01</option>
            <option value="csv">CSV</option>
          </select>
        </label>
        <label className="field">
          Message content
          <textarea
            rows={8}
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder={
              format === "hl7"
                ? "MSH|^~\\&|ANALYZER|LAB|..."
                : "order_id,patient_id,code,value,unit"
            }
          />
        </label>
        <label className="field">
          …or load from a file
          <input
            type="file"
            onChange={async (e) => {
              const file = e.target.files?.[0];
              if (file) setContent(await file.text());
            }}
          />
        </label>
        <button type="submit" disabled={busy || !content.trim()}>Ingest</button>
        {summary && <p className="hint" role="status">{summary}</p>}
        <p className="hint">
          Ingested results land as preliminary drafts in the verification
          queue — ingest never releases anything by itself.
        </p>
      </form>
    </>
  );
}

// --------------------------------------------------------------------------

function SpecimensSection({
  specimensState,
}: {
  specimensState: ReturnType<typeof useAsync<import("../api/types").Specimen[]>>;
}) {
  return (
    <>
      <h3>Specimens</h3>
      {specimensState.loading ? (
        <Loading label="Loading specimens…" />
      ) : specimensState.error ? (
        <ErrorView error={specimensState.error} />
      ) : !specimensState.data || specimensState.data.length === 0 ? (
        <Empty label="No specimens collected yet." />
      ) : (
        <table className="lab-table">
          <thead>
            <tr>
              <th>Accession</th><th>Patient</th><th>Order</th><th>Type</th><th>Status</th><th>Received</th>
            </tr>
          </thead>
          <tbody>
            {specimensState.data.map((s) => (
              <tr key={s.id}>
                <td>{s.accessionIdentifier?.value ?? s.id}</td>
                <td>{refLabel(s.subject)}</td>
                <td>{s.request?.[0]?.reference ?? "—"}</td>
                <td>{codeLabel(s.type)}</td>
                <td><StatusBadge status={s.status} /></td>
                <td>{s.receivedDateTime ? new Date(s.receivedDateTime).toLocaleString() : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

// --------------------------------------------------------------------------

function QcSection({
  client, qcState, analytes, canWork, busy, perform,
}: {
  client: ApiClient;
  qcState: ReturnType<typeof useAsync<QcRun[]>>;
  analytes: LabConcept[];
  canWork: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [analyte, setAnalyte] = useState("");
  const [material, setMaterial] = useState("");
  const [low, setLow] = useState("");
  const [high, setHigh] = useState("");
  const [measured, setMeasured] = useState("");
  const [level, setLevel] = useState("");
  const selectedAnalyte = analyte || analytes[0]?.code || "";

  // The latest run per analyte decides the gate; show that state plainly.
  const latestByAnalyte = new Map<string, QcRun>();
  for (const run of qcState.data ?? []) {
    if (!latestByAnalyte.has(run.analyte_code)) latestByAnalyte.set(run.analyte_code, run);
  }
  const blockedAnalytes = [...latestByAnalyte.values()].filter((r) => !r.passed);

  return (
    <>
      <h3>Quality control</h3>
      {blockedAnalytes.length > 0 && (
        <div className="state state-error" role="alert">
          <strong>QC gate active:</strong> the latest QC run failed for{" "}
          {blockedAnalytes.map((r) => r.analyte_code).join(", ")} — verification
          and release of results for those analytes are blocked until a
          passing run is recorded.
        </div>
      )}
      {canWork && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            void perform("Record QC run", () =>
              client.createQcRun({
                analyte_code: selectedAnalyte,
                control_material: material.trim(),
                expected_low: Number(low),
                expected_high: Number(high),
                measured_value: Number(measured),
                ...(level.trim() ? { level: level.trim() } : {}),
              }),
            );
          }}
        >
          <label className="field">
            Analyte
            <select value={selectedAnalyte} onChange={(e) => setAnalyte(e.target.value)}>
              {analytes.map((c) => (
                <option key={c.code} value={c.code}>
                  {c.display} ({c.code})
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            Control material / lot
            <input value={material} onChange={(e) => setMaterial(e.target.value)} placeholder="SYN-QC-LOT-1" />
          </label>
          <label className="field">
            Expected low
            <input inputMode="decimal" value={low} onChange={(e) => setLow(e.target.value)} />
          </label>
          <label className="field">
            Expected high
            <input inputMode="decimal" value={high} onChange={(e) => setHigh(e.target.value)} />
          </label>
          <label className="field">
            Measured value
            <input inputMode="decimal" value={measured} onChange={(e) => setMeasured(e.target.value)} />
          </label>
          <label className="field">
            QC level (programme)
            <input value={level} onChange={(e) => setLevel(e.target.value)} placeholder="L1" />
          </label>
          <button
            type="submit"
            disabled={busy || !material.trim() || low === "" || high === "" || measured === ""}
          >
            Record QC run
          </button>
        </form>
      )}
      {qcState.loading ? (
        <Loading label="Loading QC runs…" />
      ) : qcState.error ? (
        <ErrorView error={qcState.error} />
      ) : !qcState.data || qcState.data.length === 0 ? (
        <Empty label="No QC runs recorded yet." />
      ) : (
        <table className="lab-table">
          <thead>
            <tr>
              <th>When</th><th>Analyte</th><th>Control</th><th>Level</th><th>Expected</th><th>Measured</th><th>Verdict</th><th>Westgard</th><th>By</th>
            </tr>
          </thead>
          <tbody>
            {qcState.data.map((run) => (
              <tr key={run.id}>
                <td>{run.created_at ? new Date(run.created_at).toLocaleString() : "—"}</td>
                <td>{run.analyte_code}</td>
                <td>{run.control_material}</td>
                <td>{run.level ?? "—"}</td>
                <td>{run.expected_low}–{run.expected_high}{run.unit ? ` ${run.unit}` : ""}</td>
                <td>{run.measured_value}</td>
                <td>
                  <span className={run.passed ? "badge" : "badge badge-critical"}>
                    {run.passed ? "PASS" : "FAIL"}
                  </span>
                </td>
                <td>
                  {(run.flags ?? []).length > 0 ? (
                    <span className="badge badge-warn">{(run.flags ?? []).join(", ")}</span>
                  ) : (
                    <span className="hint">—</span>
                  )}
                  {run.z_score != null ? ` z=${run.z_score.toFixed(2)}` : ""}
                </td>
                <td>{run.performed_by}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="hint">
        Pass/fail is computed by the server from the expected range — the
        form never asserts a verdict. Westgard-style flags (1-2s warning,
        1-3s / 2-2s rejection) come from the published QC programme config
        and are informational; the gate above still keys on the latest
        run's pass/fail.
      </p>
    </>
  );
}

// --------------------------------------------------------------------------
// Phase 6B sections: collection worklist, microbiology, blood bank,
// pathology, laboratory operations.
// --------------------------------------------------------------------------

/** Pre-analytical: orders awaiting collection, grouped by patient +
 *  collection location and ordered by the catalog's collection
 *  priority (stat first). Custody timestamps ride on the order detail. */
function CollectionWorklistSection({
  client, canWork, busy, perform, tick,
}: {
  client: ApiClient;
  canWork: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  tick: number;
}) {
  const state = useAsync(
    () =>
      typeof client.collectionWorklist === "function"
        ? client.collectionWorklist()
        : Promise.resolve({ total_orders: 0, groups: [] }),
    [client, tick],
  );
  return (
    <>
      <h3>Collection worklist</h3>
      {state.loading ? (
        <Loading label="Loading collection worklist…" />
      ) : state.error ? (
        <ErrorView error={state.error} />
      ) : !state.data || state.data.total_orders === 0 ? (
        <Empty label="Nothing awaiting collection." />
      ) : (
        <table className="lab-table">
          <thead>
            <tr>
              <th>Patient</th><th>Location</th><th>Order</th><th>Test</th><th>Priority</th><th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {state.data.groups.flatMap((group) =>
              group.orders.map((item) => (
                <tr key={item.order_id}>
                  <td>{group.patient_ref}</td>
                  <td>{group.location}</td>
                  <td>{item.order_id}</td>
                  <td>{item.display ?? item.code ?? "—"}</td>
                  <td>{item.priority}</td>
                  <td>
                    {canWork ? (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          void perform(`Collect specimen (${item.order_id})`, () =>
                            client.transitionLabOrder(item.order_id, "collected"),
                          )
                        }
                      >
                        Collect specimen
                      </button>
                    ) : (
                      <span className="hint">Read-only</span>
                    )}
                  </td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      )}
    </>
  );
}

/** Microbiology bench: culture orders run their own published state
 *  machine — collected/plated via the standard transition endpoint,
 *  then staged bench reporting (preliminary → organism → susceptibility
 *  → final). Staged reports stay invisible outside the lab until the
 *  final release. */
function MicroSection({
  client, orders, canWork, canVerify, busy, perform, tick,
}: {
  client: ApiClient;
  orders: ServiceRequest[];
  canWork: boolean;
  canVerify: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  tick: number;
}) {
  const terminologyState = useAsync(
    () =>
      typeof client.microTerminology === "function"
        ? client.microTerminology()
        : Promise.resolve({
            organisms: null, organisms_version: null,
            antibiotics: null, antibiotics_version: null,
          }),
    [client],
  );
  const organisms = terminologyState.data?.organisms?.concepts ?? [];
  const antibiotics = terminologyState.data?.antibiotics?.concepts ?? [];
  const [prelimText, setPrelimText] = useState<Record<string, string>>({});
  const [organism, setOrganism] = useState<Record<string, string>>({});
  const [susc, setSusc] = useState<Record<string, Record<string, string>>>({});

  if (orders.length === 0 && !terminologyState.loading) {
    return (
      <>
        <h3>Microbiology</h3>
        <Empty label="No culture orders on the microbiology bench." />
      </>
    );
  }
  return (
    <>
      <h3>Microbiology</h3>
      <p className="hint">
        Cultures follow the published micro workflow (ordered → collected →
        plated → preliminary → organism identified → susceptibility → final).
        Preliminary culture results are visible to lab staff only until the
        final release.
      </p>
      <ul className="result-list">
        {orders.map((order) => {
          const id = order.id ?? "";
          const status = order.status ?? "";
          return (
            <li key={id} className="result-row">
              <span className="result-type">culture</span>
              <span>
                {codeLabel(order.code)} — {refLabel(order.subject)} · order {id}
                <br />
                <span className="result-meta">Status: {status}</span>
              </span>
              <span className="appt-actions">
                {canWork && status === "ordered" && (
                  <button type="button" disabled={busy} onClick={() =>
                    void perform(`Collect culture ${id}`, () =>
                      client.transitionLabOrder(id, "collected"))}>
                    Collect specimen
                  </button>
                )}
                {canWork && status === "collected" && (
                  <button type="button" disabled={busy} onClick={() =>
                    void perform(`Plate culture ${id}`, () =>
                      client.transitionLabOrder(id, "plated"))}>
                    Plate culture
                  </button>
                )}
                {canWork && status === "plated" && (
                  <span>
                    <input
                      aria-label={`Preliminary finding for order ${id}`}
                      placeholder="Preliminary finding (e.g. Gram-negative rods seen)"
                      value={prelimText[id] ?? ""}
                      onChange={(e) => setPrelimText((m) => ({ ...m, [id]: e.target.value }))}
                    />
                    <button type="button" disabled={busy || !(prelimText[id] ?? "").trim()} onClick={() =>
                      void perform(`Preliminary report ${id}`, () =>
                        client.microPreliminary(id, (prelimText[id] ?? "").trim()))}>
                      Preliminary report
                    </button>
                  </span>
                )}
                {canWork && status === "preliminary-reported" && (
                  <span>
                    <select
                      aria-label={`Organism for order ${id}`}
                      value={organism[id] ?? ""}
                      onChange={(e) => setOrganism((m) => ({ ...m, [id]: e.target.value }))}
                    >
                      <option value="">Organism…</option>
                      {organisms.filter((o) => o.active !== false).map((o) => (
                        <option key={o.code} value={o.code}>{o.display}</option>
                      ))}
                    </select>
                    <button type="button" disabled={busy || !organism[id]} onClick={() =>
                      void perform(`Identify organism ${id}`, () =>
                        client.microOrganism(id, organism[id] ?? ""))}>
                      Identify organism
                    </button>
                  </span>
                )}
                {canWork && status === "organism-identified" && (
                  <span>
                    {antibiotics.filter((a) => a.active !== false).map((a) => (
                      <label key={a.code}>
                        {a.display}{" "}
                        <select
                          aria-label={`${a.display} interpretation for order ${id}`}
                          value={susc[id]?.[a.code] ?? ""}
                          onChange={(e) =>
                            setSusc((m) => ({
                              ...m,
                              [id]: { ...(m[id] ?? {}), [a.code]: e.target.value },
                            }))
                          }
                        >
                          <option value="">—</option>
                          <option value="S">S</option>
                          <option value="I">I</option>
                          <option value="R">R</option>
                        </select>
                      </label>
                    ))}
                    <button
                      type="button"
                      disabled={busy || Object.keys(susc[id] ?? {}).length === 0}
                      onClick={() =>
                        void perform(`Susceptibilities ${id}`, () =>
                          client.microSusceptibility(
                            id,
                            Object.entries(susc[id] ?? {})
                              .filter(([, v]) => v)
                              .map(([antibiotic, interpretation]) => ({
                                antibiotic,
                                interpretation,
                              })),
                          ))}
                    >
                      Report susceptibilities
                    </button>
                  </span>
                )}
                {canVerify &&
                  (status === "organism-identified" ||
                    status === "susceptibility-reported") && (
                    <button type="button" disabled={busy} onClick={() =>
                      void perform(`Finalize culture ${id}`, () =>
                        client.microFinalize(id))}>
                      Finalize report
                    </button>
                  )}
                {!canWork && !canVerify && <span className="hint">Read-only</span>}
              </span>
            </li>
          );
        })}
      </ul>
    </>
  );
}

/** Blood bank: unit inventory, issue/return against a type & screen /
 *  crossmatch order, the transfusion record, and reaction holds. An
 *  expired unit can never be issued (server 409); a reaction places a
 *  hold that blocks further issues until a clinician clears it. */
function BloodBankSection({
  client, orders, catalog, canWork, canVerify, busy, perform, tick,
}: {
  client: ApiClient;
  orders: ServiceRequest[];
  catalog: import("../api/types").LabCatalogPayload | null;
  canWork: boolean;
  canVerify: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  tick: number;
}) {
  const unitsState = useAsync(
    () =>
      typeof client.bloodUnits === "function"
        ? client.bloodUnits()
        : Promise.resolve([] as BloodUnit[]),
    [client, tick],
  );
  const holdsState = useAsync(
    () =>
      typeof client.bloodHolds === "function"
        ? client.bloodHolds()
        : Promise.resolve([] as BloodHold[]),
    [client, tick],
  );
  const [unitForm, setUnitForm] = useState({
    unit_id: "", product_type: "", blood_group: "", expiry_date: "",
  });
  const [issueOrder, setIssueOrder] = useState<Record<string, string>>({});
  const [reactionText, setReactionText] = useState<Record<string, string>>({});

  const products = catalog?.bloodProducts ?? [];
  const aboConcept = catalog?.concepts?.find((c) => c.code === "883-9");
  const groups = aboConcept?.values ?? [];
  const bbOrders = orders.filter((o) => {
    const concept = catalog?.concepts?.find(
      (c) => c.code === o.code?.coding?.[0]?.code,
    );
    return concept?.bench === "blood-bank";
  });
  const units: BloodUnit[] = unitsState.data ?? [];
  const holds: BloodHold[] = (holdsState.data ?? []).filter((h) => h.active);

  return (
    <>
      <h3>Blood bank</h3>
      {holds.length > 0 && (
        <div className="state state-error" role="alert">
          <strong>Transfusion hold:</strong>{" "}
          {holds.map((h) => `${h.patient_ref} (unit ${h.unit_id}: ${h.reason})`).join("; ")}
          {" "}— no units may be issued to these patients until a clinician
          clears the hold.
          {canVerify &&
            holds.map((h) => (
              <button
                key={h.id}
                type="button"
                disabled={busy}
                onClick={() =>
                  void perform(`Clear hold for ${h.patient_ref}`, () =>
                    client.clearBloodHold(
                      (h.patient_ref ?? "").replace(/^Patient\//, ""),
                    ),
                  )
                }
              >
                Clear hold ({h.patient_ref})
              </button>
            ))}
        </div>
      )}
      {canWork && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            void perform("Register blood unit", () =>
              client.createBloodUnit({
                unit_id: unitForm.unit_id.trim(),
                product_type: unitForm.product_type || products[0]?.code || "",
                blood_group: unitForm.blood_group || groups[0] || "",
                expiry_date: unitForm.expiry_date,
              }),
            );
          }}
        >
          <label className="field">
            Unit id
            <input value={unitForm.unit_id} onChange={(e) => setUnitForm((f) => ({ ...f, unit_id: e.target.value }))} placeholder="SYN-UNIT-001" />
          </label>
          <label className="field">
            Product
            <select value={unitForm.product_type || products[0]?.code || ""} onChange={(e) => setUnitForm((f) => ({ ...f, product_type: e.target.value }))}>
              {products.map((p) => (
                <option key={p.code} value={p.code}>{p.display}</option>
              ))}
            </select>
          </label>
          <label className="field">
            Blood group
            <select value={unitForm.blood_group || groups[0] || ""} onChange={(e) => setUnitForm((f) => ({ ...f, blood_group: e.target.value }))}>
              {groups.map((g) => (
                <option key={g} value={g}>{g}</option>
              ))}
            </select>
          </label>
          <label className="field">
            Expiry date
            <input type="date" value={unitForm.expiry_date} onChange={(e) => setUnitForm((f) => ({ ...f, expiry_date: e.target.value }))} />
          </label>
          <button type="submit" disabled={busy || !unitForm.unit_id.trim() || !unitForm.expiry_date}>
            Register unit
          </button>
        </form>
      )}
      {unitsState.loading ? (
        <Loading label="Loading blood units…" />
      ) : unitsState.error ? (
        <ErrorView error={unitsState.error} />
      ) : units.length === 0 ? (
        <Empty label="No blood units in inventory." />
      ) : (
        <table className="lab-table">
          <thead>
            <tr>
              <th>Unit</th><th>Product</th><th>Group</th><th>Expiry</th><th>Status</th><th>Patient</th><th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {units.map((unit) => (
              <tr key={unit.unit_id}>
                <td>{unit.unit_id}</td>
                <td>{unit.product_type}</td>
                <td>{unit.blood_group}</td>
                <td>{unit.expiry_date}</td>
                <td><StatusBadge status={unit.status} /></td>
                <td>{unit.patient_ref ?? "—"}</td>
                <td>
                  {canWork ? (
                    <span className="appt-actions">
                      {unit.status === "available" && (
                        <span>
                          <select
                            aria-label={`Issue order for unit ${unit.unit_id}`}
                            value={issueOrder[unit.unit_id] ?? ""}
                            onChange={(e) => setIssueOrder((m) => ({ ...m, [unit.unit_id]: e.target.value }))}
                          >
                            <option value="">Order…</option>
                            {bbOrders.map((o) => (
                              <option key={o.id} value={o.id}>
                                {o.id} — {refLabel(o.subject)}
                              </option>
                            ))}
                          </select>
                          <button
                            type="button"
                            disabled={busy || !issueOrder[unit.unit_id]}
                            onClick={() =>
                              void perform(`Issue unit ${unit.unit_id}`, () =>
                                client.issueBloodUnit(unit.unit_id, issueOrder[unit.unit_id] ?? ""),
                              )
                            }
                          >
                            Issue
                          </button>
                        </span>
                      )}
                      {unit.status === "issued" && (
                        <>
                          <button type="button" disabled={busy} onClick={() =>
                            void perform(`Record transfusion ${unit.unit_id}`, () =>
                              client.recordTransfusion({ unit_id: unit.unit_id }))}>
                            Record transfusion
                          </button>
                          <button type="button" disabled={busy} onClick={() =>
                            void perform(`Return unit ${unit.unit_id}`, () =>
                              client.returnBloodUnit(unit.unit_id, false))}>
                            Return unused
                          </button>
                        </>
                      )}
                      {(unit.status === "issued" || unit.status === "transfused") && (
                        <span>
                          <input
                            aria-label={`Reaction symptoms for unit ${unit.unit_id}`}
                            placeholder="Reaction symptoms"
                            value={reactionText[unit.unit_id] ?? ""}
                            onChange={(e) => setReactionText((m) => ({ ...m, [unit.unit_id]: e.target.value }))}
                          />
                          <button
                            type="button"
                            disabled={busy || !(reactionText[unit.unit_id] ?? "").trim()}
                            onClick={() =>
                              void perform(`Report reaction ${unit.unit_id}`, () =>
                                client.reportTransfusionReaction(
                                  unit.unit_id,
                                  (reactionText[unit.unit_id] ?? "").trim(),
                                ),
                              )
                            }
                          >
                            Report reaction
                          </button>
                        </span>
                      )}
                    </span>
                  ) : (
                    <span className="hint">Read-only</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="hint">
        Type &amp; screen and crossmatch orders are resulted through the
        standard Result entry above (qualitative ABO/Rh and antibody
        screen values from the published catalog).
      </p>
    </>
  );
}

/** Anatomic pathology: cases follow the published pathology workflow
 *  (ordered → collected → accessioned → grossed → histology → reported
 *  → signed-out). The synoptic report is the published Studio form,
 *  rendered with the shared FormRenderer; sign-out is the release
 *  step — unsigned reports stay invisible outside the lab. */
function PathologySection({
  client, orders, canWork, canVerify, busy, perform, tick,
}: {
  client: ApiClient;
  orders: ServiceRequest[];
  canWork: boolean;
  canVerify: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  tick: number;
}) {
  const [reportFor, setReportFor] = useState<string | null>(null);
  const [form, setForm] = useState<FormDefinition | null>(null);
  const [reports, setReports] = useState<Record<string, string>>({});

  async function openReport(orderId: string) {
    setReportFor(orderId);
    setForm(null);
    try {
      const outcome = await client.pathologySynopticForm();
      setForm(outcome.form);
    } catch {
      setForm(null);
    }
  }

  return (
    <>
      <h3>Anatomic pathology</h3>
      {orders.length === 0 ? (
        <Empty label="No pathology cases." />
      ) : (
        <ul className="result-list">
          {orders.map((order) => {
            const id = order.id ?? "";
            const status = order.status ?? "";
            const nextStep: Record<string, { to: string; label: string }> = {
              ordered: { to: "collected", label: "Collect specimen" },
              collected: { to: "accessioned", label: "Accession case" },
              accessioned: { to: "grossed", label: "Gross examination" },
              grossed: { to: "histology", label: "Histology processing" },
            };
            return (
              <li key={id} className="result-row">
                <span className="result-type">pathology case</span>
                <span>
                  {codeLabel(order.code)} — {refLabel(order.subject)} · order {id}
                  <br />
                  <span className="result-meta">Status: {status}</span>
                </span>
                <span className="appt-actions">
                  {canWork && nextStep[status] && (
                    <button type="button" disabled={busy} onClick={() =>
                      void perform(`${nextStep[status].label} (${id})`, () =>
                        client.transitionLabOrder(id, nextStep[status].to))}>
                      {nextStep[status].label}
                    </button>
                  )}
                  {canWork && status === "histology" && reportFor !== id && (
                    <button type="button" disabled={busy} onClick={() => void openReport(id)}>
                      Write synoptic report
                    </button>
                  )}
                  {canVerify && status === "reported" && reports[id] && (
                    <button type="button" disabled={busy} onClick={() =>
                      void perform(`Sign out case ${id}`, () =>
                        client.pathologySign(reports[id]))}>
                      Sign out
                    </button>
                  )}
                  {canVerify && status === "reported" && !reports[id] && (
                    <span>
                      <input
                        aria-label={`Report id for case ${id}`}
                        placeholder="Report id"
                        onChange={(e) =>
                          setReports((m) => ({ ...m, [id]: e.target.value }))}
                      />
                    </span>
                  )}
                  {!canWork && !canVerify && <span className="hint">Read-only</span>}
                </span>
                {reportFor === id && status === "histology" && (
                  form ? (
                    <FormRenderer
                      form={form}
                      submitLabel="Save draft synoptic report"
                      onSubmit={async (answers) => {
                        await perform(`Synoptic report ${id}`, async () => {
                          const outcome = await client.pathologyReport(id, answers);
                          setReports((m) => ({ ...m, [id]: outcome.report.id ?? "" }));
                          setReportFor(null);
                          return outcome;
                        });
                      }}
                    />
                  ) : (
                    <Loading label="Loading synoptic form…" />
                  )
                )}
              </li>
            );
          })}
        </ul>
      )}
      <p className="hint">
        The synoptic report is a published Studio form; the draft is
        invisible to the inbox and portal until the pathologist signs
        out (Sign out appears once the draft report id is known).
      </p>
    </>
  );
}

/** Laboratory operations: EQA/proficiency records, the instrument
 *  registry with its maintenance/calibration log, the Levey-Jennings
 *  series, and the TAT summary (live-computed percentiles). */
function OperationsSection({
  client, analytes, canWork, busy, perform, tick,
}: {
  client: ApiClient;
  analytes: LabConcept[];
  canWork: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  tick: number;
}) {
  const eqaState = useAsync(
    () =>
      typeof client.eqaRecords === "function"
        ? client.eqaRecords()
        : Promise.resolve([] as EqaRecord[]),
    [client, tick],
  );
  const instrumentsState = useAsync(
    () =>
      typeof client.instruments === "function"
        ? client.instruments()
        : Promise.resolve([] as LabInstrument[]),
    [client, tick],
  );
  const tatState = useAsync(
    (): Promise<TatStats> =>
      typeof client.tatStats === "function"
        ? client.tatStats()
        : Promise.resolve({
            generated_at: "", source: "live-query", benches: {}, analytes: {},
          }),
    [client, tick],
  );
  const [eqaForm, setEqaForm] = useState({
    event: "", analyte_code: "", reported_value: "", acceptable_low: "", acceptable_high: "",
  });
  const [instForm, setInstForm] = useState({ instrument_id: "", name: "", bench: "" });
  const [ljAnalyte, setLjAnalyte] = useState("");
  const [ljMaterial, setLjMaterial] = useState("");
  const [lj, setLj] = useState<import("../api/types").LeveyJennings | null>(null);
  const eqaAnalyte = eqaForm.analyte_code || analytes[0]?.code || "";

  return (
    <>
      <h3>Laboratory operations</h3>

      <h4>Turnaround time</h4>
      {tatState.loading ? (
        <Loading label="Loading TAT…" />
      ) : tatState.error ? (
        <ErrorView error={tatState.error} />
      ) : !tatState.data || Object.keys(tatState.data.benches).length === 0 ? (
        <Empty label="No released orders yet — TAT appears after the first release." />
      ) : (
        <table className="lab-table">
          <thead>
            <tr>
              <th>Bench</th><th>Released</th><th>Order→release p50 (h)</th><th>Order→release p90 (h)</th><th>Received→release p50 (h)</th><th>Received→release p90 (h)</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(tatState.data.benches).map(([bench, bucket]) => (
              <tr key={bench}>
                <td>{bench}</td>
                <td>{bucket.count}</td>
                <td>{bucket.order_to_release_hours.p50?.toFixed(2) ?? "—"}</td>
                <td>{bucket.order_to_release_hours.p90?.toFixed(2) ?? "—"}</td>
                <td>{bucket.received_to_release_hours.p50?.toFixed(2) ?? "—"}</td>
                <td>{bucket.received_to_release_hours.p90?.toFixed(2) ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="hint">
        Percentiles are computed live from custody timestamps (order →
        release, received → release); source: {tatState.data?.source ?? "live-query"}.
      </p>

      <h4>Levey-Jennings series</h4>
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          void (async () => {
            try {
              setLj(
                await client.leveyJennings(
                  ljAnalyte || analytes[0]?.code || "",
                  ljMaterial.trim(),
                ),
              );
            } catch {
              setLj(null);
            }
          })();
        }}
      >
        <label className="field">
          Analyte
          <select value={ljAnalyte || analytes[0]?.code || ""} onChange={(e) => setLjAnalyte(e.target.value)}>
            {analytes.map((c) => (
              <option key={c.code} value={c.code}>{c.display} ({c.code})</option>
            ))}
          </select>
        </label>
        <label className="field">
          Control material
          <input value={ljMaterial} onChange={(e) => setLjMaterial(e.target.value)} placeholder="SYN-QC-LOT-1" />
        </label>
        <button type="submit" disabled={!ljMaterial.trim()}>Load series</button>
      </form>
      {lj && (
        <>
          <p className="hint">
            {lj.count} run(s) — mean {lj.mean?.toFixed(3) ?? "—"}, SD{" "}
            {lj.sd?.toFixed(3) ?? "—"}
            {lj.limits
              ? `, ±2SD ${lj.limits.minus_2sd.toFixed(3)}–${lj.limits.plus_2sd.toFixed(3)}, ±3SD ${lj.limits.minus_3sd.toFixed(3)}–${lj.limits.plus_3sd.toFixed(3)}`
              : ""}
            .
          </p>
          <table className="lab-table">
            <thead>
              <tr><th>Run</th><th>Value</th><th>Level</th><th>z (series)</th><th>Flags</th></tr>
            </thead>
            <tbody>
              {lj.runs.map((run) => (
                <tr key={run.id}>
                  <td>{run.id}</td>
                  <td>{run.measured_value}</td>
                  <td>{run.level ?? "—"}</td>
                  <td>{run.z_score?.toFixed(2) ?? "—"}</td>
                  <td>{run.flags.join(", ") || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <h4>EQA / proficiency testing</h4>
      {canWork && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            void perform("Record EQA result", () =>
              client.createEqa({
                event: eqaForm.event.trim(),
                analyte_code: eqaAnalyte,
                reported_value: Number(eqaForm.reported_value),
                acceptable_low: Number(eqaForm.acceptable_low),
                acceptable_high: Number(eqaForm.acceptable_high),
              }),
            );
          }}
        >
          <label className="field">
            Event
            <input value={eqaForm.event} onChange={(e) => setEqaForm((f) => ({ ...f, event: e.target.value }))} placeholder="SYN-EQA-2026-Q3" />
          </label>
          <label className="field">
            Analyte
            <select value={eqaAnalyte} onChange={(e) => setEqaForm((f) => ({ ...f, analyte_code: e.target.value }))}>
              {analytes.map((c) => (
                <option key={c.code} value={c.code}>{c.display} ({c.code})</option>
              ))}
            </select>
          </label>
          <label className="field">
            Reported value
            <input inputMode="decimal" value={eqaForm.reported_value} onChange={(e) => setEqaForm((f) => ({ ...f, reported_value: e.target.value }))} />
          </label>
          <label className="field">
            Acceptable low
            <input inputMode="decimal" value={eqaForm.acceptable_low} onChange={(e) => setEqaForm((f) => ({ ...f, acceptable_low: e.target.value }))} />
          </label>
          <label className="field">
            Acceptable high
            <input inputMode="decimal" value={eqaForm.acceptable_high} onChange={(e) => setEqaForm((f) => ({ ...f, acceptable_high: e.target.value }))} />
          </label>
          <button
            type="submit"
            disabled={busy || !eqaForm.event.trim() || eqaForm.reported_value === "" || eqaForm.acceptable_low === "" || eqaForm.acceptable_high === ""}
          >
            Record EQA result
          </button>
        </form>
      )}
      {eqaState.loading ? (
        <Loading label="Loading EQA records…" />
      ) : eqaState.error ? (
        <ErrorView error={eqaState.error} />
      ) : !eqaState.data || eqaState.data.length === 0 ? (
        <Empty label="No EQA records yet." />
      ) : (
        <table className="lab-table">
          <thead>
            <tr><th>Event</th><th>Analyte</th><th>Reported</th><th>Acceptable</th><th>Verdict</th><th>By</th></tr>
          </thead>
          <tbody>
            {(eqaState.data as EqaRecord[]).map((record) => (
              <tr key={record.id}>
                <td>{record.event}</td>
                <td>{record.analyte_code}</td>
                <td>{record.reported_value}</td>
                <td>{record.acceptable_low}–{record.acceptable_high}</td>
                <td>
                  <span className={record.passed ? "badge" : "badge badge-critical"}>
                    {record.passed ? "PASS" : "FAIL"}
                  </span>
                </td>
                <td>{record.recorded_by}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h4>Instrument registry</h4>
      {canWork && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            void perform("Register instrument", () =>
              client.createInstrument({
                instrument_id: instForm.instrument_id.trim(),
                name: instForm.name.trim(),
                bench: instForm.bench.trim() || undefined,
              }),
            );
          }}
        >
          <label className="field">
            Instrument id
            <input value={instForm.instrument_id} onChange={(e) => setInstForm((f) => ({ ...f, instrument_id: e.target.value }))} placeholder="SYN-AN-01" />
          </label>
          <label className="field">
            Name
            <input value={instForm.name} onChange={(e) => setInstForm((f) => ({ ...f, name: e.target.value }))} placeholder="Synthetic chemistry analyser" />
          </label>
          <label className="field">
            Bench
            <input value={instForm.bench} onChange={(e) => setInstForm((f) => ({ ...f, bench: e.target.value }))} placeholder="chemistry" />
          </label>
          <button type="submit" disabled={busy || !instForm.instrument_id.trim() || !instForm.name.trim()}>
            Register instrument
          </button>
        </form>
      )}
      {instrumentsState.loading ? (
        <Loading label="Loading instruments…" />
      ) : instrumentsState.error ? (
        <ErrorView error={instrumentsState.error} />
      ) : !instrumentsState.data || instrumentsState.data.length === 0 ? (
        <Empty label="No instruments registered." />
      ) : (
        <table className="lab-table">
          <thead>
            <tr><th>Instrument</th><th>Name</th><th>Bench</th><th>Status</th><th>Actions</th></tr>
          </thead>
          <tbody>
            {(instrumentsState.data as LabInstrument[]).map((inst) => (
              <tr key={inst.instrument_id}>
                <td>{inst.instrument_id}</td>
                <td>{inst.name}</td>
                <td>{inst.bench || "—"}</td>
                <td><StatusBadge status={inst.status} /></td>
                <td>
                  {canWork ? (
                    <span className="appt-actions">
                      {(["active", "maintenance", "down"] as const)
                        .filter((s) => s !== inst.status)
                        .map((s) => (
                          <button
                            key={s}
                            type="button"
                            disabled={busy}
                            onClick={() =>
                              void perform(`Set ${inst.instrument_id} ${s}`, () =>
                                client.setInstrumentStatus(inst.instrument_id, s),
                              )
                            }
                          >
                            Set {s}
                          </button>
                        ))}
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          void perform(`Log calibration ${inst.instrument_id}`, () =>
                            client.addInstrumentLog(inst.instrument_id, {
                              kind: "calibration",
                              note: "Calibration recorded from the Lab page",
                            }),
                          )
                        }
                      >
                        Log calibration
                      </button>
                    </span>
                  ) : (
                    <span className="hint">Read-only</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
