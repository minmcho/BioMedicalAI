import { useMemo, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { codeLabel, refLabel } from "../api/display";
import type {
  LabConcept,
  QcRun,
  Role,
  ServiceRequest,
  VerificationItem,
} from "../api/types";
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

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

function StatusBadge({ status }: { status?: string }) {
  return <span className="badge">{status ?? "—"}</span>;
}

export function LabPage({ client, role }: Props) {
  const canWork = role === "admin" || role === "configurator" || role === "clinician";
  const canVerify = role === "admin" || role === "clinician";
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

      <OrdersSection
        client={client}
        ordersState={ordersState}
        orderable={orderable}
        canWork={canWork}
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

      <ResultEntrySection
        client={client}
        orders={ordersState.data ?? []}
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
    </section>
  );
}

// --------------------------------------------------------------------------

function OrdersSection({
  client, ordersState, orderable, canWork, busy, perform,
}: {
  client: ApiClient;
  ordersState: ReturnType<typeof useAsync<ServiceRequest[]>>;
  orderable: LabConcept[];
  canWork: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [statusFilter, setStatusFilter] = useState("");
  const [patientId, setPatientId] = useState("pat-1");
  const [code, setCode] = useState("");
  const [reasons, setReasons] = useState<Record<string, string>>({});

  const orders = (ordersState.data ?? []).filter(
    (o) => !statusFilter || o.status === statusFilter,
  );
  const selectedCode = code || orderable[0]?.code || "";

  return (
    <>
      <h3>Order queue</h3>
      {canWork && (
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
            {ORDER_STATES.map((s) => (
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
                <td>{codeLabel(order.code)}</td>
                <td><StatusBadge status={order.status} /></td>
                <td>
                  {canWork ? (
                    <span className="appt-actions">
                      {(NEXT_ACTIONS[order.status ?? ""] ?? []).map((action) => (
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
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() =>
                              void perform(`${action.label} (${order.id})`, () =>
                                client.transitionLabOrder(order.id ?? "", action.to, {
                                  ...(action.reason
                                    ? { reason: reasons[order.id ?? ""] ?? "" }
                                    : {}),
                                }),
                              )
                            }
                          >
                            {action.label}
                          </button>
                        </span>
                      ))}
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
                <br />
                <span className="result-meta">
                  {item.results
                    .map((r) => `${r.display ?? r.code ?? "?"}: ${r.value ?? "—"} ${r.unit ?? ""}${r.interpretation ? ` [${r.interpretation}]` : ""}`)
                    .join(" · ")}
                </span>
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
              .map((c) => ({
                code: c,
                value: Number(values[`${selected.id}:${c}`]),
              }));
            if (results.length === 0 || results.some((r) => Number.isNaN(r.value))) {
              setLocalError("Enter at least one numeric result value.");
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
              <th>When</th><th>Analyte</th><th>Control</th><th>Expected</th><th>Measured</th><th>Verdict</th><th>By</th>
            </tr>
          </thead>
          <tbody>
            {qcState.data.map((run) => (
              <tr key={run.id}>
                <td>{run.created_at ? new Date(run.created_at).toLocaleString() : "—"}</td>
                <td>{run.analyte_code}</td>
                <td>{run.control_material}</td>
                <td>{run.expected_low}–{run.expected_high}{run.unit ? ` ${run.unit}` : ""}</td>
                <td>{run.measured_value}</td>
                <td>
                  <span className={run.passed ? "badge" : "badge badge-critical"}>
                    {run.passed ? "PASS" : "FAIL"}
                  </span>
                </td>
                <td>{run.performed_by}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="hint">
        Pass/fail is computed by the server from the expected range — the
        form never asserts a verdict.
      </p>
    </>
  );
}
