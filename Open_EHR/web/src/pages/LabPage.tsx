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
  LabTrends,
  LabWorkflowPayload,
  QcRun,
  QuestionnaireAnswerItem,
  Role,
  ServiceRequest,
  TatStats,
  VerificationItem,
} from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { ModuleHeader } from "../components/ModuleHeader";
import { saveDownloadedFile } from "../api/download";
import {
  getLabTestDetail,
  printBloodLabel,
  type LabTestDetail,
} from "../api/labDepth";
import {
  assignPlateSpecimen,
  completePlateRun,
  createPlateRun,
  getPlateRun,
  getPlateTemplates,
  isControlRole,
  isPatientRole,
  listPlateRuns,
  parseBulkPlateResults,
  plateGridShape,
  postPlateResults,
  type PlateRun,
} from "../api/plates";
import { FormRenderer } from "../components/FormRenderer";
import { PanelsTab, SpecimensTab, TestCatalogueTab } from "./LabManagerTabs";
import { LabCompendium } from "./LabCompendium";
import { LabPreanalytical } from "./LabPreanalytical";
import { LabAnalyser } from "./LabAnalyser";
import { LabReagentsQc } from "./LabReagentsQc";
import { LabTat } from "./LabTat";
import { LabMicro } from "./LabMicro";
import { LabTransfusion } from "./LabTransfusion";
import { LabMolecular } from "./LabMolecular";
import { LabQms } from "./LabQms";
import { Stepper } from "../components/Stepper";
import { StatusBadge as SharedStatusBadge } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role: Role;
  /** Depth pass (Workstream 1 contract): the shell's nav sub-menu
   *  can deep-link a laboratory section; the page reports tab
   *  changes back so the shell state stays the source of truth. */
  section?: string;
  onSectionChange?: (section: string) => void;
}

/** Laboratory tab-pages (Depth pass): the page's sections as tabs —
 *  the keys are the nav config's section slugs for the lab module. */
const LAB_TABS = [
  { key: "orders", label: "Orders & Results" },
  { key: "verification", label: "Verification" },
  { key: "specimens", label: "Specimens" },
  { key: "plates", label: "Plates" },
  { key: "microbiology", label: "Microbiology" },
  { key: "virology", label: "Virology" },
  { key: "blood-bank", label: "Blood Bank" },
  { key: "pathology", label: "Pathology" },
  { key: "qc", label: "QC & EQA" },
  { key: "test-catalogue", label: "Test Catalogue" },
  { key: "panels", label: "Panels" },
  { key: "sendouts", label: "Send-outs" },
  { key: "poct", label: "POCT" },
  { key: "governance", label: "Governance" },
  // Lab Depth pass (LAB-D1…D16) surfaces — one tab per workstream
  // component, appended so the pre-Depth tab order is untouched.
  { key: "compendium", label: "Compendium & Benches" },
  { key: "preanalytical", label: "Pre-analytical" },
  { key: "analyser", label: "Analysers & Autoverification" },
  { key: "reagents-qc", label: "Reagents & QC" },
  { key: "tat", label: "TAT & Reflex" },
  { key: "micro-depth", label: "Microbiology Depth" },
  { key: "transfusion", label: "Transfusion Service" },
  { key: "molecular", label: "Molecular" },
  { key: "lab-qms", label: "Lab QMS & POCT" },
];

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

export function LabPage({ client, role, section, onSectionChange }: Props) {
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
  // Tab state: the shell's section prop wins when provided (nav
  // deep-link); otherwise the page manages its own active tab.
  const [tabLocal, setTabLocal] = useState("orders");
  const activeTab = section ?? tabLocal;
  const setActiveTab = (key: string) => {
    setTabLocal(key);
    onSectionChange?.(key);
  };

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
  const virologyOrders = allOrders.filter((o) => benchOf(o) === "virology");
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
      <ModuleHeader icon="flask" group="Diagnostics & Pharmacy" title="Laboratory" purpose="Laboratory orders, results and benches" />
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

      <Tabs
        ariaLabel="Laboratory sections"
        tabs={LAB_TABS}
        activeKey={activeTab}
        onChange={setActiveTab}
        panels={{
          orders: (
            <>
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
              <ResultEntrySection
                client={client}
                orders={standardOrders}
                conceptByCode={conceptByCode}
                canWork={canWork}
                busy={busy}
                perform={perform}
              />
              <IngestSection client={client} canWork={canWork} busy={busy} perform={perform} />
            </>
          ),
          verification: (
            <>
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
            </>
          ),
          specimens: (
            <>
              <CollectionWorklistSection
                client={client}
                canWork={canWork}
                busy={busy}
                perform={perform}
                tick={tick}
              />
              <SpecimensSection specimensState={specimensState} />
              <SpecimensTab client={client} />
            </>
          ),
          plates: (
            <PlatesSection client={client} canWork={canWork} />
          ),
          microbiology: (
            <MicroSection
              client={client}
              orders={microOrders}
              canWork={canWork}
              canVerify={canVerify}
              busy={busy}
              perform={perform}
              tick={tick}
            />
          ),
          virology: (
            <VirologySection
              client={client}
              orders={virologyOrders}
              conceptByCode={conceptByCode}
              onJump={setActiveTab}
            />
          ),
          "blood-bank": (
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
          ),
          pathology: (
            <PathologySection
              client={client}
              orders={pathologyOrders}
              conceptByCode={conceptByCode}
              canWork={canWork}
              canVerify={canVerify}
              busy={busy}
              perform={perform}
              tick={tick}
            />
          ),
          qc: (
            <>
              <QcSection
                qcState={qcState}
                analytes={analytes}
                canWork={canWork}
                busy={busy}
                perform={perform}
                client={client}
              />
              <OperationsSection
                client={client}
                analytes={analytes}
                canWork={canWork}
                busy={busy}
                perform={perform}
                tick={tick}
              />
            </>
          ),
          sendouts: (
            <SendoutsSection
              client={client}
              conceptByCode={conceptByCode}
              canWork={canWork}
              busy={busy}
              perform={perform}
              tick={tick}
            />
          ),
          poct: (
            <PoctSection
              client={client}
              analytes={analytes}
              canWork={canWork}
              busy={busy}
              perform={perform}
              tick={tick}
            />
          ),
          "test-catalogue": <TestCatalogueTab client={client} />,
          panels: <PanelsTab client={client} />,
          governance: <GovernanceSection client={client} tick={tick} />,
          compendium: <LabCompendium api={client} />,
          preanalytical: <LabPreanalytical client={client} />,
          analyser: <LabAnalyser client={client} />,
          "reagents-qc": <LabReagentsQc client={client} />,
          tat: <LabTat client={client} />,
          "micro-depth": <LabMicro client={client} />,
          transfusion: <LabTransfusion client={client} />,
          molecular: <LabMolecular client={client} />,
          "lab-qms": <LabQms client={client} />,
        }}
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
  const [detailOrderId, setDetailOrderId] = useState<string | null>(null);

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
                  <button
                    type="button"
                    onClick={() => setDetailOrderId(order.id ?? null)}
                  >
                    Test detail
                  </button>{" "}
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
      {detailOrderId && (
        <TestDetailPanel
          client={client}
          orderId={detailOrderId}
          onClose={() => setDetailOrderId(null)}
        />
      )}
    </>
  );
}

// --------------------------------------------------------------------------

/** Test detail drill-down (UX pass WS3b): GET
 *  /lab/orders/{id}/test-detail (backend/app/lab.py test_detail) —
 *  steps, specimen + aliquot lineage, results with delta/critical,
 *  report stamps and the acknowledgement trail. Every state is
 *  rendered as text, never colour alone. */
function TestDetailPanel({
  client, orderId, onClose,
}: {
  client: ApiClient;
  orderId: string;
  onClose: () => void;
}) {
  const state = useAsync(() => getLabTestDetail(client, orderId), [client, orderId]);
  const detail: LabTestDetail | null = state.data ?? null;
  return (
    <div className="detail-panel">
      <h4>
        Test detail — order {orderId}{" "}
        <button type="button" onClick={onClose}>Close test detail</button>
      </h4>
      {state.loading ? (
        <Loading label="Loading test detail…" />
      ) : state.error ? (
        <ErrorView error={state.error} />
      ) : !detail ? (
        <Empty label="No test detail." />
      ) : (
        <>
          <p className="result-meta">
            {detail.order.display ?? detail.order.code ?? "—"} · patient{" "}
            {detail.order.patient_ref ?? "—"} · status {detail.order.status ?? "—"}
            {detail.order.priority ? ` · priority ${detail.order.priority}` : ""}
            {detail.order.requester ? ` · requested by ${detail.order.requester}` : ""}
          </p>
          <Stepper
            steps={detail.steps.map((s) => s.label)}
            current={(() => {
              const idx = detail.steps.findIndex((s) => s.state === "current");
              return idx >= 0 ? idx : detail.steps.length;
            })()}
          />
          <ul className="result-list">
            {detail.steps.map((s) => (
              <li key={s.key} className="result-row">
                <span className="result-type">{s.label}</span>
                <span>
                  {s.state === "done" ? "Done" : s.state === "current" ? "Current" : "Pending"}
                  {s.at ? ` at ${new Date(s.at).toLocaleString()}` : ""}
                  {s.by ? ` by ${s.by}` : ""}
                  {s.plate_id != null ? ` · plate ${s.plate_id}` : ""}
                </span>
              </li>
            ))}
          </ul>

          <h5>Specimen</h5>
          {detail.specimen ? (
            <p className="result-meta">
              Specimen {detail.specimen.id} · accession {detail.specimen.accession ?? "—"} ·{" "}
              {detail.specimen.type ?? "—"} · status {detail.specimen.status ?? "—"}
              {detail.specimen.collected_at
                ? ` · collected ${new Date(detail.specimen.collected_at).toLocaleString()}` : ""}
              {detail.specimen.received_at
                ? ` · received ${new Date(detail.specimen.received_at).toLocaleString()}` : ""}
              {detail.specimen.in_analysis_at
                ? ` · in analysis ${new Date(detail.specimen.in_analysis_at).toLocaleString()}` : ""}
            </p>
          ) : (
            <p className="hint">No specimen yet.</p>
          )}
          <h5>Aliquot lineage</h5>
          {detail.aliquots.length === 0 ? (
            <p className="hint">No aliquots.</p>
          ) : (
            <ul className="result-list">
              {detail.aliquots.map((a) => (
                <li key={a.specimen_key} className="result-row">
                  <span className="result-type">{a.derivation_type}</span>
                  <span>
                    {a.specimen_key} · specimen {a.specimen_id} · sequence {a.sequence} ·{" "}
                    {a.container} · {a.remaining_volume_ml}/{a.volume_ml} mL remaining ·{" "}
                    status {a.status}
                    {a.barcode ? ` · barcode ${a.barcode}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          )}

          <h5>Results</h5>
          {detail.results.length === 0 ? (
            <p className="hint">No results yet.</p>
          ) : (
            <table className="lab-table">
              <thead>
                <tr>
                  <th>Analyte</th><th>Value</th><th>Reference range</th>
                  <th>Interpretation</th><th>Delta</th><th>Critical</th><th>Status</th>
                </tr>
              </thead>
              <tbody>
                {detail.results.map((r) => (
                  <tr key={r.observation_id}>
                    <td>{r.display ?? r.analyte ?? "—"} ({r.analyte ?? "—"})</td>
                    <td>{r.value ?? "—"}{r.unit ? ` ${r.unit}` : ""}</td>
                    <td>
                      {r.reference_range
                        ? `${r.reference_range.low ?? "…"}–${r.reference_range.high ?? "…"}${r.reference_range.unit ? ` ${r.reference_range.unit}` : ""}`
                        : "—"}
                    </td>
                    <td>
                      <SharedStatusBadge
                        tone={r.critical ? "bad" : r.interpretation && r.interpretation !== "N" ? "warn" : "ok"}
                      >
                        {r.interpretation_text ?? r.interpretation ?? "No interpretation"}
                      </SharedStatusBadge>
                    </td>
                    <td>
                      {r.delta ? (
                        <span className="badge badge-warn">
                          Δ {r.delta.code}: prior {r.delta.previous_value} → {r.delta.current_value}
                          {r.delta.change_pct != null
                            ? ` (${r.delta.change_pct > 0 ? "+" : ""}${r.delta.change_pct.toFixed(1)}%)`
                            : ""}
                        </span>
                      ) : (
                        "No delta flag"
                      )}
                    </td>
                    <td>
                      {r.critical ? (
                        <SharedStatusBadge tone="bad">CRITICAL</SharedStatusBadge>
                      ) : (
                        "Not critical"
                      )}
                    </td>
                    <td>{r.status ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h5>Report</h5>
          {detail.report ? (
            <p className="result-meta">
              Report {detail.report.report_id} · status {detail.report.status ?? "—"}
              {detail.report.critical ? " · CRITICAL" : ""}
              {detail.report.verified_by
                ? ` · verified by ${detail.report.verified_by}${detail.report.verified_at ? ` at ${new Date(detail.report.verified_at).toLocaleString()}` : ""}`
                : " · not yet verified"}
              {detail.report.released_by || detail.report.released_at
                ? ` · released${detail.report.released_by ? ` by ${detail.report.released_by}` : ""}${detail.report.released_at ? ` at ${new Date(detail.report.released_at).toLocaleString()}` : ""}`
                : " · not yet released"}
            </p>
          ) : (
            <p className="hint">No report yet.</p>
          )}

          <h5>Acknowledgements</h5>
          {detail.acknowledgements.length === 0 ? (
            <p className="hint">No acknowledgements.</p>
          ) : (
            <ul className="result-list">
              {detail.acknowledgements.map((ack) => (
                <li key={ack.report_id} className="result-row">
                  <span className="result-type">acknowledgement</span>
                  <span>
                    State: {ack.state}
                    {ack.notified_to
                      ? ` · notified ${ack.notified_to}${ack.notified_by ? ` by ${ack.notified_by}` : ""}${ack.notified_at ? ` at ${new Date(ack.notified_at).toLocaleString()}` : ""}${ack.read_back ? " (read-back confirmed)" : ""}`
                      : " · not yet notified"}
                    {ack.acknowledged_by
                      ? ` · acknowledged by ${ack.acknowledged_by}${ack.acknowledged_at ? ` at ${new Date(ack.acknowledged_at).toLocaleString()}` : ""}`
                      : " · not yet acknowledged"}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
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
          {selected && ORDER_STATES.includes(selected.status ?? "") &&
            !["rejected", "cancelled"].includes(selected.status ?? "") && (
            <div className="field">
              <span>
                Order progress — this order is <strong>{selected.status}</strong>
              </span>
              <Stepper
                steps={["Ordered", "Collected", "Received", "In process",
                        "Resulted", "Verified", "Released"]}
                current={Math.max(
                  0,
                  ["ordered", "collected", "received", "in-process",
                   "resulted", "verified", "released"].indexOf(
                    selected.status ?? "",
                  ),
                )}
              />
            </div>
          )}
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

/** Virtual plates (UX pass WS3b): plate runs materialised from the
 *  governed terminology/lab-plate-templates config, specimen
 *  assignment, per-well results behind the plate QC gate, bulk
 *  import and completion — backend/app/lab_plates.py. Shapes come
 *  from that module's serializers (see api/plates.ts). */
function PlatesSection({ client, canWork }: { client: ApiClient; canWork: boolean }) {
  const [localTick, setLocalTick] = useState(0);
  const [busyLocal, setBusyLocal] = useState(false);
  const [notice, setNoticeLocal] = useState<string | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [assignError, setAssignError] = useState<string | null>(null);
  const [resultError, setResultError] = useState<string | null>(null);

  const templatesState = useAsync(() => getPlateTemplates(client), [client]);
  const templates = (templatesState.data ?? []).filter((t) => t.active !== false);
  const [templateCode, setTemplateCode] = useState("");
  const selectedTemplate = templates.find((t) => t.code === templateCode) ?? templates[0];

  const runsState = useAsync(() => listPlateRuns(client), [client, localTick]);
  const runs = runsState.data?.items ?? [];
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const activeRunId = selectedRunId ?? runs[0]?.id ?? null;
  const detailState = useAsync(
    () =>
      activeRunId != null
        ? getPlateRun(client, activeRunId)
        : Promise.resolve(null),
    [client, activeRunId, localTick],
  );
  const run: PlateRun | null = detailState.data ?? null;

  const [specimenRef, setSpecimenRef] = useState("");
  const [assignPosition, setAssignPosition] = useState("");
  const [wellValues, setWellValues] = useState<Record<string, string>>({});
  const [bulkText, setBulkText] = useState("");
  const [lastProcessed, setLastProcessed] = useState<string | null>(null);

  async function runAction(
    label: string,
    setError: (v: string | null) => void,
    fn: () => Promise<unknown>,
  ) {
    setBusyLocal(true);
    setError(null);
    setNoticeLocal(null);
    try {
      await fn();
      setNoticeLocal(`${label} — done.`);
      setLocalTick((t) => t + 1);
    } catch (err) {
      setError(`${label} failed: ${errorText(err)}`);
    } finally {
      setBusyLocal(false);
    }
  }

  const wells = run?.wells ?? [];
  const { rows, columns } = plateGridShape(wells);
  const wellByPosition = new Map(wells.map((w) => [w.position, w]));
  const gateOpen = run?.qc.gate_open === true;
  const blockingWells = wells.filter(
    (w) => isPatientRole(w.role) && w.specimen_id != null && w.result_status !== "resulted",
  );
  const runComplete = run?.status === "complete";

  function submitWellResult(position: string) {
    if (!run) return;
    const raw = (wellValues[position] ?? "").trim();
    if (!raw) {
      setResultError(`Enter a value for well ${position} first.`);
      return;
    }
    const numeric = Number(raw);
    const value = Number.isNaN(numeric) ? raw : numeric;
    void runAction(`Result well ${position}`, setResultError, async () => {
      const outcome = await postPlateResults(client, run.id, [{ position, value }]);
      const entry = outcome.processed.find((p) => p.position === position);
      if (entry) setLastProcessed(`${position}: ${entry.result_status}`);
      return outcome;
    });
  }

  return (
    <>
      <h3>Virtual plates</h3>
      <p className="hint">
        Plate templates are governed Studio terminology
        (terminology/lab-plate-templates). Patient wells cannot be
        resulted until every control, calibrator and blank well has
        resulted and none has failed — the plate QC gate.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}

      <h4>Create a plate run</h4>
      {templatesState.loading ? (
        <Loading label="Loading plate templates…" />
      ) : templatesState.error ? (
        <>
          <label className="field">
            Plate template
            <select disabled value="">
              <option value="">No plate templates available</option>
            </select>
          </label>
          <p className="hint" role="alert">
            Plate templates unavailable: {errorText(templatesState.error)} — publish
            terminology/lab-plate-templates in Studio before using virtual plates.
          </p>
        </>
      ) : templates.length === 0 ? (
        <>
          <label className="field">
            Plate template
            <select disabled value="">
              <option value="">No plate templates published</option>
            </select>
          </label>
          <p className="hint">
            No published plate templates — publish terminology/lab-plate-templates
            in Studio before using virtual plates.
          </p>
        </>
      ) : (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!selectedTemplate) return;
            void runAction("Create plate run", setCreateError, async () => {
              const created = await createPlateRun(client, selectedTemplate.code);
              setSelectedRunId(created.id);
              return created;
            });
          }}
        >
          <label className="field">
            Plate template
            <select
              value={selectedTemplate?.code ?? ""}
              onChange={(e) => setTemplateCode(e.target.value)}
            >
              {templates.map((t) => (
                <option key={t.code} value={t.code}>
                  {t.display} ({t.code}){t.format ? ` — ${t.format}` : ""}
                  {t.assay ? ` — ${t.assay}` : ""}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={busyLocal || !canWork || !selectedTemplate}>
            Create plate run
          </button>
          {!canWork && <span className="hint">Read-only — plate work requires lab.work</span>}
        </form>
      )}
      {createError && <div className="state state-error" role="alert">{createError}</div>}

      <h4>Plate runs</h4>
      {runsState.loading ? (
        <Loading label="Loading plate runs…" />
      ) : runsState.error ? (
        <ErrorView error={runsState.error} />
      ) : runs.length === 0 ? (
        <Empty label="No plate runs yet." />
      ) : (
        <ul className="result-list">
          {runs.map((r) => (
            <li key={r.id} className="result-row">
              <span className="result-type">plate {r.id}</span>
              <span>
                {r.template_code} · {r.format} · {r.assay} · status {r.status} · QC{" "}
                {r.qc.controls_passed}/{r.qc.controls_total} controls passed
              </span>
              <button type="button" onClick={() => setSelectedRunId(r.id)}>
                Open plate {r.id}
              </button>
            </li>
          ))}
        </ul>
      )}

      {activeRunId != null && (
        <>
          <h4>Plate run {activeRunId} detail</h4>
          {detailState.loading && !run ? (
            <Loading label="Loading plate run…" />
          ) : detailState.error && !run ? (
            <ErrorView error={detailState.error} />
          ) : !run ? (
            <Empty label="Plate run not found." />
          ) : (
            <>
              <p className="result-meta">
                Template {run.template_code} · {run.format} · {run.assay} · status{" "}
                {run.status} · created by {run.created_by}
                {run.completed_by
                  ? ` · completed by ${run.completed_by}${run.completed_at ? ` at ${new Date(run.completed_at).toLocaleString()}` : ""}`
                  : ""}
                {rows.length > 0
                  ? ` · grid ${rows.length} rows × ${columns.length} columns`
                  : ""}
              </p>

              <div className="qc-panel">
                <h5>QC panel</h5>
                <p>
                  Controls: {run.qc.controls_total} total · {run.qc.controls_passed}{" "}
                  passed · {run.qc.controls_failed} failed
                </p>
                {gateOpen ? (
                  <SharedStatusBadge tone="ok">
                    QC gate open — patient wells can result
                  </SharedStatusBadge>
                ) : (
                  <SharedStatusBadge tone="bad">
                    QC gate closed — controls outstanding/failed
                  </SharedStatusBadge>
                )}
              </div>

              {canWork && !runComplete && (
                <form
                  className="inline-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void runAction("Assign specimen", setAssignError, async () => {
                      const outcome = await assignPlateSpecimen(client, run.id, {
                        specimen_ref: specimenRef.trim(),
                        ...(assignPosition.trim()
                          ? { position: assignPosition.trim() }
                          : {}),
                      });
                      setSpecimenRef("");
                      setAssignPosition("");
                      return outcome;
                    });
                  }}
                >
                  <label className="field">
                    Specimen barcode or id
                    <input
                      aria-label="Specimen barcode or id"
                      value={specimenRef}
                      onChange={(e) => setSpecimenRef(e.target.value)}
                      placeholder="Scan or type barcode / specimen id"
                    />
                  </label>
                  <label className="field">
                    Well position (optional)
                    <input
                      aria-label="Well position (optional)"
                      value={assignPosition}
                      onChange={(e) => setAssignPosition(e.target.value)}
                      placeholder="Empty → first free patient well"
                    />
                  </label>
                  <button type="submit" disabled={busyLocal || !specimenRef.trim()}>
                    Assign specimen
                  </button>
                </form>
              )}
              {assignError && (
                <div className="state state-error" role="alert">{assignError}</div>
              )}

              <table className="lab-table plate-grid">
                <thead>
                  <tr>
                    <th>Row</th>
                    {columns.map((c) => <th key={c}>{c}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((rowLabel) => (
                    <tr key={rowLabel}>
                      <th>{rowLabel}</th>
                      {columns.map((col) => {
                        const well = wellByPosition.get(`${rowLabel}${col}`);
                        if (!well) return <td key={col}>—</td>;
                        const isControl = isControlRole(well.role);
                        const isPatient = isPatientRole(well.role);
                        const patientBlocked = isPatient && !gateOpen;
                        const noSpecimen = isPatient && well.specimen_id == null;
                        const inputDisabled =
                          !canWork || busyLocal || runComplete || patientBlocked || noSpecimen;
                        return (
                          <td key={col} className="plate-well">
                            <strong>{well.position}</strong>{" "}
                            <span className="well-role">{well.role}</span>
                            <br />
                            <SharedStatusBadge
                              tone={
                                well.result_status === "passed" || well.result_status === "resulted"
                                  ? "ok"
                                  : well.result_status === "failed"
                                    ? "bad"
                                    : "neutral"
                              }
                            >
                              {well.result_status}
                            </SharedStatusBadge>
                            {well.specimen_id && (
                              <>
                                <br />
                                <span className="result-meta">
                                  Specimen: {well.specimen_id}
                                </span>
                              </>
                            )}
                            {well.result_value != null && (
                              <>
                                <br />
                                <span className="result-meta">
                                  Value: {String(well.result_value)}
                                </span>
                              </>
                            )}
                            {well.expected && (
                              <>
                                <br />
                                <span className="result-meta">
                                  {"result" in well.expected
                                    ? `Expected: ${well.expected.result}`
                                    : `Expected: ${well.expected.low}–${well.expected.high}`}
                                </span>
                              </>
                            )}
                            {canWork && !runComplete && (isControl || isPatient) && (
                              <>
                                <br />
                                <input
                                  aria-label={`Result value for well ${well.position}`}
                                  value={wellValues[well.position] ?? ""}
                                  disabled={inputDisabled}
                                  onChange={(e) =>
                                    setWellValues((v) => ({
                                      ...v, [well.position]: e.target.value,
                                    }))
                                  }
                                />
                                <button
                                  type="button"
                                  disabled={inputDisabled}
                                  onClick={() => submitWellResult(well.position)}
                                >
                                  Result well {well.position}
                                </button>
                                {patientBlocked && (
                                  <span className="hint">
                                    Blocked: QC gate closed
                                  </span>
                                )}
                                {noSpecimen && !patientBlocked && (
                                  <span className="hint">No specimen assigned</span>
                                )}
                              </>
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
              {lastProcessed && (
                <p className="hint" role="status">Last well result: {lastProcessed}</p>
              )}
              {resultError && (
                <div className="state state-error" role="alert">{resultError}</div>
              )}

              {canWork && !runComplete && (
                <>
                  <h5>Bulk result import</h5>
                  <form
                    className="config-editor"
                    onSubmit={(e) => {
                      e.preventDefault();
                      const { entries, malformed } = parseBulkPlateResults(bulkText);
                      if (malformed.length > 0) {
                        setResultError(
                          `Malformed bulk line(s): ${malformed.join(" | ")} — use position,value per line.`,
                        );
                        return;
                      }
                      if (entries.length === 0) {
                        setResultError("Enter at least one position,value line.");
                        return;
                      }
                      void runAction("Import bulk results", setResultError, async () => {
                        const outcome = await postPlateResults(client, run.id, entries);
                        setLastProcessed(
                          outcome.processed
                            .map((p) => `${p.position}: ${p.result_status}`)
                            .join(", "),
                        );
                        setBulkText("");
                        return outcome;
                      });
                    }}
                  >
                    <label className="field">
                      Bulk results (position,value per line)
                      <textarea
                        aria-label="Bulk plate results"
                        rows={5}
                        value={bulkText}
                        onChange={(e) => setBulkText(e.target.value)}
                        placeholder={"A1,100\nB1,95"}
                      />
                    </label>
                    <button type="submit" disabled={busyLocal || !bulkText.trim()}>
                      Import bulk results
                    </button>
                  </form>
                </>
              )}

              <h5>Complete run</h5>
              <button
                type="button"
                disabled={busyLocal || !canWork || runComplete || blockingWells.length > 0}
                onClick={() =>
                  void runAction("Complete plate run", setResultError, () =>
                    completePlateRun(client, run.id),
                  )
                }
              >
                Complete plate run
              </button>
              {blockingWells.length > 0 && !runComplete && (
                <p className="hint">
                  Cannot complete: assigned patient well(s){" "}
                  {blockingWells.map((w) => w.position).join(", ")} have not been resulted.
                </p>
              )}
              {runComplete && <p className="hint">This plate run is complete.</p>}
            </>
          )}
        </>
      )}
    </>
  );
}

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

/** Wave A (LAB-B1 / LAB-M1): the governed blood-bank gate parameters
 *  carried in the published catalog's `bloodBankGate` section (the
 *  web type predates the section, so it is read through this shape). */
interface BloodBankGateConfig {
  antibodyScreenValidityHours?: number;
  electronicCrossmatch?: { enabled?: boolean; products?: string[] };
  emergencyReleaseReasonCodes?: Array<{ code: string; display: string }>;
}

interface GateCheck {
  code: string;
  label: string;
  status: string;
  detail: string;
}

interface GateStatus {
  checks: GateCheck[];
  issue_allowed: boolean;
  patient_group: string | null;
  patient_ref: string | null;
}

interface TransfusionFormState {
  wristband_patient_id: string;
  date_of_birth: string;
  second_checker: string;
  temperature_c: string;
  pulse_bpm: string;
  systolic_bp: string;
  diastolic_bp: string;
  resp_rate: string;
  started_at: string;
  stopped_at: string;
}

const EMPTY_TRANSFUSION_FORM: TransfusionFormState = {
  wristband_patient_id: "", date_of_birth: "", second_checker: "",
  temperature_c: "", pulse_bpm: "", systolic_bp: "", diastolic_bp: "",
  resp_rate: "", started_at: "", stopped_at: "",
};

/** Blood bank: unit inventory, issue/return against a type & screen /
 *  crossmatch order, the transfusion record, and reaction holds. An
 *  expired unit can never be issued (server 409); a reaction places a
 *  hold that blocks further issues until a clinician clears it.
 *  Wave A: issue is gated server-side (LAB-B1 — resulted group +
 *  ABO/Rh matrix + in-window negative antibody screen + unit
 *  crossmatch or electronic-crossmatch eligibility; emergency
 *  release is the only override) and the gate posture + refusal
 *  reasons are surfaced here verbatim; the transfusion record is
 *  the LAB-M1 bedside verification (two identifiers + vitals +
 *  start/stop). */
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
  // Wave A state: emergency-release inputs per unit, the gate
  // posture for the selected (unit, order) pair, the verbatim
  // refusal text, the LAB-M1 transfusion forms, and the
  // unit-specific crossmatch result form.
  const [emergency, setEmergency] = useState<Record<string, {
    enabled: boolean; reason_code: string; note: string;
  }>>({});
  const [gateStatus, setGateStatus] = useState<Record<string, GateStatus | null>>({});
  const [refusal, setRefusal] = useState<string | null>(null);
  const [transfusionOpen, setTransfusionOpen] = useState<Record<string, boolean>>({});
  const [transfusionForm, setTransfusionForm] = useState<
    Record<string, TransfusionFormState>>({});
  const [xmForm, setXmForm] = useState({ order_id: "", unit_id: "", value: "Compatible" });

  const products = catalog?.bloodProducts ?? [];
  const aboConcept = catalog?.concepts?.find((c) => c.code === "883-9");
  const groups = aboConcept?.values ?? [];
  const bbOrders = orders.filter((o) => {
    const concept = catalog?.concepts?.find(
      (c) => c.code === o.code?.coding?.[0]?.code,
    );
    return concept?.bench === "blood-bank";
  });
  const xmOrders = bbOrders.filter(
    (o) => o.code?.coding?.[0]?.code === "BB-XM",
  );
  const units: BloodUnit[] = unitsState.data ?? [];
  const holds: BloodHold[] = (holdsState.data ?? []).filter((h) => h.active);
  const gateCfg: BloodBankGateConfig =
    (catalog as unknown as { bloodBankGate?: BloodBankGateConfig } | null)
      ?.bloodBankGate ?? {};
  const reasonCodes = gateCfg.emergencyReleaseReasonCodes ?? [];
  const canRequest = typeof client.request === "function";

  function captureRefusal(err: unknown): string {
    // Refusal reasons are rendered VERBATIM (the server's detail
    // string names every failed check — no generic "failed" text).
    return err instanceof ApiError ? err.detail : errorText(err);
  }

  async function loadGateStatus(unitId: string, orderId: string) {
    if (!canRequest || !orderId) {
      setGateStatus((m) => ({ ...m, [unitId]: null }));
      return;
    }
    try {
      const status = await client.request<GateStatus>(
        `/lab/blood-bank/units/${encodeURIComponent(unitId)}/gate-status` +
          `?order_id=${encodeURIComponent(orderId)}`,
      );
      setGateStatus((m) => ({ ...m, [unitId]: status }));
    } catch {
      setGateStatus((m) => ({ ...m, [unitId]: null }));
    }
  }

  async function issueUnit(unit: BloodUnit) {
    const orderId = issueOrder[unit.unit_id] ?? "";
    const em = emergency[unit.unit_id];
    try {
      if (canRequest) {
        const payload: Record<string, unknown> = { order_id: orderId };
        if (em?.enabled) {
          payload.emergency_release = {
            reason_code: em.reason_code || reasonCodes[0]?.code || "",
            note: em.note,
          };
        }
        await client.request(
          `/lab/blood-bank/units/${encodeURIComponent(unit.unit_id)}/issue`,
          { method: "POST", body: JSON.stringify(payload) },
        );
      } else {
        await client.issueBloodUnit(unit.unit_id, orderId);
      }
      setRefusal(null);
    } catch (err) {
      setRefusal(captureRefusal(err));
      throw err;
    }
  }

  async function recordTransfusionVerified(unit: BloodUnit) {
    const form = transfusionForm[unit.unit_id] ?? EMPTY_TRANSFUSION_FORM;
    const payload: Record<string, unknown> = {
      unit_id: unit.unit_id,
      wristband_patient_id: form.wristband_patient_id.trim(),
      date_of_birth: form.date_of_birth,
      vitals: {
        temperature_c: Number(form.temperature_c),
        pulse_bpm: Number(form.pulse_bpm),
        systolic_bp: Number(form.systolic_bp),
        diastolic_bp: Number(form.diastolic_bp),
        resp_rate: Number(form.resp_rate),
      },
    };
    if (form.second_checker.trim()) {
      payload.second_checker = form.second_checker.trim();
    }
    if (form.started_at) payload.started_at = form.started_at;
    if (form.stopped_at) payload.stopped_at = form.stopped_at;
    try {
      if (canRequest) {
        await client.request("/lab/blood-bank/transfusions", {
          method: "POST", body: JSON.stringify(payload),
        });
      } else {
        await (client.recordTransfusion as unknown as (
          input: Record<string, unknown>,
        ) => Promise<unknown>)(payload);
      }
      setRefusal(null);
    } catch (err) {
      setRefusal(captureRefusal(err));
      throw err;
    }
  }

  return (
    <>
      <h3>Blood bank</h3>
      {refusal && (
        <div className="state state-error" role="alert">
          <strong>Refused:</strong> {refusal}
        </div>
      )}
      <p className="hint">
        Issue gate (server-enforced): a unit may be issued only when the
        patient&apos;s resulted blood group is ABO/Rh-compatible with the
        unit, the latest antibody screen is Negative within the governed
        validity window
        {gateCfg.antibodyScreenValidityHours
          ? ` (${gateCfg.antibodyScreenValidityHours} hours)`
          : ""}
        , and a crossmatch resulted for THIS unit is Compatible — or the
        patient meets electronic-crossmatch eligibility
        {gateCfg.electronicCrossmatch
          ? ` (enabled for: ${(gateCfg.electronicCrossmatch.products ?? []).join(", ") || "no products"})`
          : ""}
        . Emergency release is the only override and bypasses missing
        evidence only, never contradictory evidence.
      </p>
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
                            onChange={(e) => {
                              const orderId = e.target.value;
                              setIssueOrder((m) => ({ ...m, [unit.unit_id]: orderId }));
                              void loadGateStatus(unit.unit_id, orderId);
                            }}
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
                                issueUnit(unit),
                              )
                            }
                          >
                            Issue
                          </button>
                          {gateStatus[unit.unit_id] && (
                            <span className="result-meta">
                              <br />
                              Gate posture for{" "}
                              {gateStatus[unit.unit_id]?.patient_ref ?? "patient"}
                              {gateStatus[unit.unit_id]?.patient_group
                                ? ` (group ${gateStatus[unit.unit_id]?.patient_group})`
                                : " (no resulted group)"}
                              {": "}
                              {(gateStatus[unit.unit_id]?.checks ?? []).map(
                                (check) => (
                                  <span key={check.code}>
                                    {check.status === "pass" ? "✓" : "✗"}{" "}
                                    {check.label} — {check.detail}{" "}
                                  </span>
                                ),
                              )}
                            </span>
                          )}
                          <label className="field">
                            <input
                              type="checkbox"
                              aria-label={`Emergency release for unit ${unit.unit_id}`}
                              checked={emergency[unit.unit_id]?.enabled ?? false}
                              onChange={(e) =>
                                setEmergency((m) => ({
                                  ...m,
                                  [unit.unit_id]: {
                                    enabled: e.target.checked,
                                    reason_code:
                                      m[unit.unit_id]?.reason_code ??
                                      reasonCodes[0]?.code ?? "",
                                    note: m[unit.unit_id]?.note ?? "",
                                  },
                                }))
                              }
                            />
                            Emergency release
                          </label>
                          {emergency[unit.unit_id]?.enabled && (
                            <span>
                              <select
                                aria-label={`Emergency release reason for unit ${unit.unit_id}`}
                                value={emergency[unit.unit_id]?.reason_code || reasonCodes[0]?.code || ""}
                                onChange={(e) =>
                                  setEmergency((m) => ({
                                    ...m,
                                    [unit.unit_id]: {
                                      enabled: true,
                                      reason_code: e.target.value,
                                      note: m[unit.unit_id]?.note ?? "",
                                    },
                                  }))
                                }
                              >
                                {reasonCodes.map((rc) => (
                                  <option key={rc.code} value={rc.code}>
                                    {rc.display}
                                  </option>
                                ))}
                              </select>
                              <input
                                aria-label={`Emergency release note for unit ${unit.unit_id}`}
                                placeholder="Emergency note"
                                value={emergency[unit.unit_id]?.note ?? ""}
                                onChange={(e) =>
                                  setEmergency((m) => ({
                                    ...m,
                                    [unit.unit_id]: {
                                      enabled: true,
                                      reason_code:
                                        m[unit.unit_id]?.reason_code ??
                                        reasonCodes[0]?.code ?? "",
                                      note: e.target.value,
                                    },
                                  }))
                                }
                              />
                            </span>
                          )}
                        </span>
                      )}
                      {unit.status === "issued" && (
                        <>
                          {(unit as unknown as {
                            emergency_release_reason_code?: string | null;
                          }).emergency_release_reason_code && (
                            <span className="result-meta">
                              Emergency release:{" "}
                              {(unit as unknown as {
                                emergency_release_reason_code?: string | null;
                              }).emergency_release_reason_code}
                              <br />
                            </span>
                          )}
                          <button type="button" disabled={busy} onClick={() =>
                            setTransfusionOpen((m) => ({
                              ...m, [unit.unit_id]: !m[unit.unit_id],
                            }))}>
                            Record transfusion
                          </button>
                          <button type="button" disabled={busy} onClick={() =>
                            void perform(`Return unit ${unit.unit_id}`, () =>
                              client.returnBloodUnit(unit.unit_id, false))}>
                            Return unused
                          </button>
                          {transfusionOpen[unit.unit_id] && (
                            <span>
                              <br />
                              <span className="result-meta">
                                Bedside verification (LAB-M1): two
                                identifiers + pre-transfusion vitals are
                                checked server-side before anything is
                                recorded.
                              </span>
                              {(
                                [
                                  ["wristband_patient_id", "Wristband patient id"],
                                  ["date_of_birth", "Patient date of birth"],
                                  ["second_checker", "Second checker (optional)"],
                                  ["temperature_c", "Temperature °C"],
                                  ["pulse_bpm", "Pulse bpm"],
                                  ["systolic_bp", "Systolic BP"],
                                  ["diastolic_bp", "Diastolic BP"],
                                  ["resp_rate", "Respiratory rate"],
                                  ["started_at", "Started at (ISO, optional)"],
                                  ["stopped_at", "Stopped at (ISO, optional)"],
                                ] as Array<[keyof TransfusionFormState, string]>
                              ).map(([field, label]) => (
                                <label key={field} className="field">
                                  {label}
                                  <input
                                    aria-label={`${label} for unit ${unit.unit_id}`}
                                    type={field === "date_of_birth" ? "date" : "text"}
                                    value={
                                      (transfusionForm[unit.unit_id] ??
                                        EMPTY_TRANSFUSION_FORM)[field]
                                    }
                                    onChange={(e) =>
                                      setTransfusionForm((m) => ({
                                        ...m,
                                        [unit.unit_id]: {
                                          ...(m[unit.unit_id] ??
                                            EMPTY_TRANSFUSION_FORM),
                                          [field]: e.target.value,
                                        },
                                      }))
                                    }
                                  />
                                </label>
                              ))}
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() =>
                                  void perform(
                                    `Record transfusion ${unit.unit_id}`,
                                    () => recordTransfusionVerified(unit),
                                  )
                                }
                              >
                                Verify &amp; record transfusion
                              </button>
                            </span>
                          )}
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
                  {" "}
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void perform(`Print ISBT label for unit ${unit.unit_id}`, async () => {
                        const file = await printBloodLabel(
                          client,
                          `BloodUnit/${unit.unit_id}`,
                        );
                        try {
                          saveDownloadedFile(file);
                        } catch {
                          // No URL.createObjectURL in some environments;
                          // the label was still generated + audited server-side.
                        }
                        return file;
                      })
                    }
                  >
                    Print ISBT label
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="hint">
        Type &amp; screen (ABO group + antibody screen) is resulted
        through the standard Result entry above and, once verified and
        released, is consumed directly by the issue gate — the linkage
        is the patient&apos;s released 883-9 / 890-4 results. A
        crossmatch only counts for the gate when it is resulted FOR A
        SPECIFIC UNIT through the form below, which stamps the unit id
        onto the crossmatch observation; a patient-level crossmatch
        resulted elsewhere does not satisfy the gate.
      </p>
      {canWork && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            void perform(`Result crossmatch for unit ${xmForm.unit_id}`, async () => {
              try {
                await client.request(
                  `/lab/blood-bank/orders/${encodeURIComponent(xmForm.order_id)}/crossmatch-result`,
                  {
                    method: "POST",
                    body: JSON.stringify({
                      unit_id: xmForm.unit_id,
                      value: xmForm.value,
                    }),
                  },
                );
                setRefusal(null);
              } catch (err) {
                setRefusal(captureRefusal(err));
                throw err;
              }
            });
          }}
        >
          <label className="field">
            Crossmatch order
            <select
              aria-label="Crossmatch order"
              value={xmForm.order_id}
              onChange={(e) => setXmForm((f) => ({ ...f, order_id: e.target.value }))}
            >
              <option value="">Order…</option>
              {xmOrders.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.id} — {refLabel(o.subject)}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            Unit
            <select
              aria-label="Crossmatch unit"
              value={xmForm.unit_id}
              onChange={(e) => setXmForm((f) => ({ ...f, unit_id: e.target.value }))}
            >
              <option value="">Unit…</option>
              {units.map((u) => (
                <option key={u.unit_id} value={u.unit_id}>
                  {u.unit_id} ({u.product_type}, {u.blood_group})
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            Result
            <select
              aria-label="Crossmatch result"
              value={xmForm.value}
              onChange={(e) => setXmForm((f) => ({ ...f, value: e.target.value }))}
            >
              <option value="Compatible">Compatible</option>
              <option value="Incompatible">Incompatible</option>
            </select>
          </label>
          <button
            type="submit"
            disabled={busy || !canRequest || !xmForm.order_id || !xmForm.unit_id}
          >
            Result crossmatch for unit
          </button>
          {!canRequest && (
            <span className="hint">
              Unit-specific crossmatch entry needs the live API client —
              unavailable in this view.
            </span>
          )}
        </form>
      )}
    </>
  );
}

/** Anatomic pathology: cases follow the published pathology workflow
 *  (ordered → collected → accessioned → grossed → histology → reported
 *  → signed-out). The synoptic report is the published Studio form,
 *  rendered with the shared FormRenderer; sign-out is the release
 *  step — unsigned reports stay invisible outside the lab. */
function PathologySection({
  client, orders, conceptByCode, canWork, canVerify, busy, perform, tick,
}: {
  client: ApiClient;
  orders: ServiceRequest[];
  conceptByCode: Map<string, LabConcept>;
  canWork: boolean;
  canVerify: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  tick: number;
}) {
  const [reportFor, setReportFor] = useState<string | null>(null);
  const [formKind, setFormKind] = useState<"synoptic" | "cytology">("synoptic");
  const [form, setForm] = useState<FormDefinition | null>(null);
  const [reports, setReports] = useState<Record<string, string>>({});

  async function openReport(orderId: string, cytology: boolean) {
    setReportFor(orderId);
    setFormKind(cytology ? "cytology" : "synoptic");
    setForm(null);
    try {
      const outcome = cytology
        ? await client.pathologyCytologyForm()
        : await client.pathologySynopticForm();
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
            const isCyto =
              conceptByCode.get(order.code?.coding?.[0]?.code ?? "")
                ?.cytology === true;
            const nextStep: Record<string, { to: string; label: string }> = {
              ordered: { to: "collected", label: "Collect specimen" },
              collected: { to: "accessioned", label: "Accession case" },
              accessioned: { to: "grossed", label: "Gross examination" },
              grossed: { to: "histology", label: "Histology processing" },
            };
            return (
              <li key={id} className="result-row">
                <span className="result-type">{isCyto ? "cytology case" : "pathology case"}</span>
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
                    <button type="button" disabled={busy} onClick={() => void openReport(id, isCyto)}>
                      {isCyto ? "Write cytology report" : "Write synoptic report"}
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
                      submitLabel={
                        formKind === "cytology"
                          ? "Save draft cytology report"
                          : "Save draft synoptic report"
                      }
                      onSubmit={async (answers) => {
                        await perform(
                          `${formKind === "cytology" ? "Cytology" : "Synoptic"} report ${id}`,
                          async () => {
                            const outcome =
                              formKind === "cytology"
                                ? await client.pathologyCytologyReport(id, answers)
                                : await client.pathologyReport(id, answers);
                            setReports((m) => ({ ...m, [id]: outcome.report.id ?? "" }));
                            setReportFor(null);
                            return outcome;
                          },
                        );
                      }}
                    />
                  ) : (
                    <Loading label="Loading form…" />
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
        Cytology cases (the catalog's cytology flag) report with the
        published cytology form instead and share the same sign-out.
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

// ---------------------------------------------------------------------------
// Depth pass sections: virology, send-outs, POCT programme, governance
// ---------------------------------------------------------------------------

/** Virology (Depth pass): the bench's worklist plus the viral-load
 *  trend view. NAAT / antigen results are entered on the Orders &
 *  Results tab — virology runs the standard spine — while this tab
 *  is where the bench watches its orders and follows viral loads.
 *  Trends show RELEASED results only (the server enforces it). */
function VirologySection({
  client, orders, conceptByCode, onJump,
}: {
  client: ApiClient;
  orders: ServiceRequest[];
  conceptByCode: Map<string, LabConcept>;
  onJump: (tab: string) => void;
}) {
  const viralLoads = [...conceptByCode.values()].filter(
    (c) => c.bench === "virology" && !c.qualitative && !c.panel,
  );
  const [patientId, setPatientId] = useState("pat-1");
  const [code, setCode] = useState("");
  const [trends, setTrends] = useState<LabTrends | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const selectedCode = code || viralLoads[0]?.code || "";

  return (
    <>
      <h3>Virology</h3>
      <p className="hint">
        Viral NAAT and antigen results are entered on the Orders &amp;
        Results tab (virology runs the standard order → specimen →
        result spine); a Reactive HIV screen reflexes the
        differentiation assay automatically, per the published reflex
        rules (see the Governance tab).
      </p>
      {orders.length === 0 ? (
        <Empty label="No virology orders yet." />
      ) : (
        <table className="lab-table">
          <thead>
            <tr><th>Order</th><th>Test</th><th>Patient</th><th>Status</th></tr>
          </thead>
          <tbody>
            {orders.map((order) => (
              <tr key={order.id}>
                <td>{order.id}</td>
                <td>{codeLabel(order.code)}</td>
                <td>{refLabel(order.subject)}</td>
                <td><StatusBadge status={order.status} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <button type="button" onClick={() => onJump("orders")}>
        Go to Orders &amp; Results
      </button>

      <h4>Viral-load trend</h4>
      <form
        className="inline-form"
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          if (!patientId.trim()) {
            setLocalError("Enter a patient id to load a trend.");
            return;
          }
          if (!selectedCode) {
            setLocalError("No quantitative virology analyte is published in the catalog.");
            return;
          }
          setLocalError(null);
          void (async () => {
            try {
              setTrends(await client.labTrends(patientId.trim(), selectedCode));
            } catch (err) {
              setTrends(null);
              setLocalError(errorText(err));
            }
          })();
        }}
      >
        <label className="field">
          Patient id
          <input
            value={patientId}
            onChange={(e) => setPatientId(e.target.value)}
            placeholder="pat-1"
          />
        </label>
        <label className="field">
          Viral-load analyte
          <select
            value={selectedCode}
            disabled={viralLoads.length === 0}
            onChange={(e) => setCode(e.target.value)}
          >
            {viralLoads.map((c) => (
              <option key={c.code} value={c.code}>
                {c.display} ({c.code})
              </option>
            ))}
          </select>
          {viralLoads.length === 0 && (
            <span className="hint">
              Disabled: the published catalog has no quantitative
              virology analyte to trend.
            </span>
          )}
        </label>
        <button type="submit" disabled={viralLoads.length === 0}>
          Load trend
        </button>
      </form>
      {localError && (
        <div className="state state-error" role="alert">{localError}</div>
      )}
      {trends &&
        (trends.count === 0 ? (
          <Empty label="No released results for this patient and analyte yet — drafts never appear in a trend." />
        ) : (
          <table className="lab-table">
            <thead>
              <tr><th>When</th><th>Value</th><th>Unit</th><th>log10</th></tr>
            </thead>
            <tbody>
              {trends.points.map((point) => (
                <tr key={point.observation_id}>
                  <td>{point.effective ?? "—"}</td>
                  <td>{point.value}</td>
                  <td>{point.unit ?? "—"}</td>
                  <td>{point.log10 ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
    </>
  );
}

/** Send-outs (Depth pass): the reference-laboratory lifecycle —
 *  dispatch collected specimens on a per-laboratory manifest, file
 *  the returned results as drafts for verification, and watch
 *  turnaround against the contracted hours in the published
 *  sendout-labs config. */
function SendoutsSection({
  client, conceptByCode, canWork, busy, perform, tick,
}: {
  client: ApiClient;
  conceptByCode: Map<string, LabConcept>;
  canWork: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  tick: number;
}) {
  const queueState = useAsync(() => client.labSendouts(), [client, tick]);
  const tatState = useAsync(() => client.labSendoutTat(), [client, tick]);
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [tracking, setTracking] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [localError, setLocalError] = useState<string | null>(null);

  const items = queueState.data?.items ?? [];
  const awaiting = items.filter((i) => i.sendout.status === "awaiting-dispatch");
  const dispatched = items.filter((i) => i.sendout.status === "dispatched");
  const resulted = items.filter((i) => i.sendout.status === "resulted");
  const awaitingByLab = new Map<string, typeof awaiting>();
  for (const item of awaiting) {
    const key = item.reference_lab.code ?? "unknown";
    awaitingByLab.set(key, [...(awaitingByLab.get(key) ?? []), item]);
  }

  return (
    <>
      <h3>Send-outs / reference laboratory</h3>
      <p className="hint">
        Routing is the published sendout-labs configuration: a test
        ships to the laboratory whose list names it. Returned results
        file as preliminary drafts — they wait in the Verification
        queue like every other result, never auto-released.
      </p>
      {queueState.loading ? (
        <Loading label="Loading send-outs…" />
      ) : queueState.error ? (
        <ErrorView error={queueState.error} />
      ) : items.length === 0 ? (
        <Empty label="No send-out orders yet." />
      ) : (
        <>
          <h4>Awaiting dispatch</h4>
          {awaiting.length === 0 ? (
            <Empty label="Nothing is waiting to ship." />
          ) : (
            [...awaitingByLab.entries()].map(([labCode, group]) => (
              <div key={labCode}>
                <p className="hint">
                  To {group[0].reference_lab.display ?? labCode} —
                  contracted TAT {group[0].reference_lab.tat_hours ?? "—"} h.
                  A manifest ships to ONE laboratory.
                </p>
                <ul className="result-list">
                  {group.map((item) => (
                    <li key={item.order_id} className="result-row">
                      {canWork && item.order_status === "collected" ? (
                        <label>
                          <input
                            type="checkbox"
                            aria-label={`Select order ${item.order_id} for dispatch`}
                            checked={selected[item.order_id] === true}
                            onChange={(e) =>
                              setSelected((s) => ({
                                ...s,
                                [item.order_id]: e.target.checked,
                              }))}
                          />{" "}
                          {item.display} — {item.patient} · order {item.order_id}
                        </label>
                      ) : (
                        <span>
                          {item.display} — {item.patient} · order {item.order_id}
                          <br />
                          <span className="result-meta">
                            Status: {item.order_status}
                            {item.order_status !== "collected" &&
                              " — collect the specimen before dispatch"}
                          </span>
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
                {canWork && (
                  <form
                    className="inline-form"
                    noValidate
                    onSubmit={(e) => {
                      e.preventDefault();
                      const ids = group
                        .filter(
                          (i) =>
                            i.order_status === "collected" &&
                            selected[i.order_id],
                        )
                        .map((i) => i.order_id);
                      if (ids.length === 0) {
                        setLocalError(
                          "Select at least one collected order to dispatch.",
                        );
                        return;
                      }
                      setLocalError(null);
                      void perform(
                        `Dispatch ${ids.length} send-out(s) to ${labCode}`,
                        () =>
                          client.labSendoutDispatch(
                            ids,
                            tracking.trim() || undefined,
                          ),
                      );
                    }}
                  >
                    <label className="field">
                      Tracking reference (optional)
                      <input
                        value={tracking}
                        onChange={(e) => setTracking(e.target.value)}
                        placeholder="Carrier tracking no."
                      />
                    </label>
                    <button type="submit" disabled={busy}>
                      Dispatch selected to {group[0].reference_lab.display ?? labCode}
                    </button>
                  </form>
                )}
              </div>
            ))
          )}

          <h4>In transit / at the reference laboratory</h4>
          {dispatched.length === 0 ? (
            <Empty label="No send-outs in transit." />
          ) : (
            <ul className="result-list">
              {dispatched.map((item) => (
                <li key={item.order_id} className="result-row">
                  <span>
                    {item.display} — {item.patient} · order {item.order_id}
                    <br />
                    <span className="result-meta">
                      To {item.reference_lab.display} · dispatched{" "}
                      {item.sendout.dispatched_at ?? "—"}
                      {item.sendout.tracking_ref
                        ? ` · tracking ${item.sendout.tracking_ref}`
                        : ""}
                    </span>
                  </span>
                  {canWork && (
                    <span className="appt-actions">
                      <input
                        aria-label={`Returned value for order ${item.order_id}`}
                        inputMode="decimal"
                        placeholder={
                          conceptByCode.get(item.code)?.unit ?? "value"
                        }
                        value={values[item.order_id] ?? ""}
                        onChange={(e) =>
                          setValues((v) => ({
                            ...v,
                            [item.order_id]: e.target.value,
                          }))}
                      />
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => {
                          const raw = (values[item.order_id] ?? "").trim();
                          const value = Number(raw);
                          if (raw === "" || Number.isNaN(value)) {
                            setLocalError(
                              `Enter the numeric returned value for order ${item.order_id}.`,
                            );
                            return;
                          }
                          setLocalError(null);
                          void perform(
                            `File send-out result (${item.order_id})`,
                            () =>
                              client.labSendoutReceiveResult(item.order_id, [
                                { code: item.code, value },
                              ]),
                          );
                        }}
                      >
                        File returned result
                      </button>
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}

          <h4>Resulted</h4>
          {resulted.length === 0 ? (
            <Empty label="No send-out results filed yet." />
          ) : (
            <table className="lab-table">
              <thead>
                <tr><th>Order</th><th>Test</th><th>Laboratory</th><th>Returned</th></tr>
              </thead>
              <tbody>
                {resulted.map((item) => (
                  <tr key={item.order_id}>
                    <td>{item.order_id}</td>
                    <td>{item.display}</td>
                    <td>{item.reference_lab.display}</td>
                    <td>{item.sendout.returned_at ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
      {localError && (
        <div className="state state-error" role="alert">{localError}</div>
      )}

      <h4>Turnaround by laboratory</h4>
      {tatState.loading ? (
        <Loading label="Loading send-out TAT…" />
      ) : tatState.error ? (
        <ErrorView error={tatState.error} />
      ) : (
        <table className="lab-table">
          <thead>
            <tr>
              <th>Laboratory</th><th>Contracted TAT (h)</th><th>Resulted</th>
              <th>In flight</th><th>Average (h)</th><th>Within TAT</th>
            </tr>
          </thead>
          <tbody>
            {(tatState.data?.laboratories ?? []).map((lab) => (
              <tr key={lab.lab}>
                <td>{lab.display}</td>
                <td>{lab.tat_hours ?? "—"}</td>
                <td>{lab.resulted}</td>
                <td>{lab.in_flight}</td>
                <td>{lab.avg_hours ?? "—"}</td>
                <td>
                  {lab.within_tat_pct != null ? `${lab.within_tat_pct}%` : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

/** POCT programme (Depth pass): device QC + operator competency on
 *  the Phase 14 device registry. The programme's enforcement state
 *  is shown as data — seeded OFF (tracking only); a site turns the
 *  blocks on by republishing the poct-programme config, and entry
 *  then refuses before any write. */
function PoctSection({
  client, analytes, canWork, busy, perform, tick,
}: {
  client: ApiClient;
  analytes: LabConcept[];
  canWork: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  tick: number;
}) {
  const overviewState = useAsync(() => client.labPoctOverview(), [client, tick]);
  const devicesState = useAsync(() => client.labPoctDevices(), [client, tick]);
  const competencyState = useAsync(() => client.labPoctCompetency(), [client, tick]);
  const [qcForm, setQcForm] = useState({ device_key: "", level: "", result: "pass" });
  const [compForm, setCompForm] = useState({
    staff_id: "", test_code: "", method: "", assessed_on: "",
  });
  const [localError, setLocalError] = useState<string | null>(null);

  const devices = devicesState.data?.items ?? [];
  const levels = overviewState.data?.qc_levels ?? [];
  const qcDevice = qcForm.device_key || devices[0]?.device_key || "";
  const qcLevel = qcForm.level || levels[0] || "";
  const compTest = compForm.test_code || analytes[0]?.code || "";
  const enforcement = overviewState.data?.enforcement;

  return (
    <>
      <h3>POCT programme</h3>
      {overviewState.loading ? (
        <Loading label="Loading POCT programme…" />
      ) : overviewState.error ? (
        <ErrorView error={overviewState.error} />
      ) : (
        <p role="status">
          <SharedStatusBadge
            tone={
              enforcement?.block_uncompetent_operator ||
              enforcement?.block_missing_daily_qc
                ? "warn"
                : "info"
            }
          >
            {enforcement?.block_uncompetent_operator ||
            enforcement?.block_missing_daily_qc
              ? "Enforcement ON"
              : "Tracking only — enforcement OFF"}
          </SharedStatusBadge>{" "}
          {enforcement?.state_label}
        </p>
      )}

      <h4>Devices &amp; daily QC</h4>
      {devicesState.loading ? (
        <Loading label="Loading POCT devices…" />
      ) : devicesState.error ? (
        <ErrorView error={devicesState.error} />
      ) : devices.length === 0 ? (
        <Empty label="No POCT devices in the registry yet — register a poct-analyser device on the Devices page; the programme reads that registry, it keeps no second one." />
      ) : (
        <table className="lab-table">
          <thead>
            <tr>
              <th>Device</th><th>Location</th><th>Status</th>
              <th>QC today</th><th>Programme state</th>
            </tr>
          </thead>
          <tbody>
            {devices.map((device) => (
              <tr key={device.device_key}>
                <td>{device.name} ({device.device_key})</td>
                <td>{device.location || "—"}</td>
                <td><StatusBadge status={device.status} /></td>
                <td>
                  {Object.entries(device.qc_today)
                    .map(([level, result]) => `${level}: ${result ?? "not run"}`)
                    .join(" · ")}
                </td>
                <td>
                  <SharedStatusBadge tone={device.qc_current ? "ok" : "bad"}>
                    {device.qc_current ? "QC current" : "QC due"}
                  </SharedStatusBadge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {canWork && (
        <form
          className="inline-form"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            if (!qcDevice) {
              setLocalError("No POCT device is registered yet — QC cannot be recorded.");
              return;
            }
            if (!qcLevel) {
              setLocalError("The programme publishes no QC levels — check the poct-programme config in Studio.");
              return;
            }
            setLocalError(null);
            void perform(`Record QC ${qcDevice} ${qcLevel}`, () =>
              client.labPoctRecordQc({
                device_key: qcDevice,
                level: qcLevel,
                result: qcForm.result,
              }),
            );
          }}
        >
          <label className="field">
            Device
            <select
              value={qcDevice}
              disabled={devices.length === 0}
              onChange={(e) => setQcForm((f) => ({ ...f, device_key: e.target.value }))}
            >
              {devices.map((d) => (
                <option key={d.device_key} value={d.device_key}>
                  {d.name} ({d.device_key})
                </option>
              ))}
            </select>
            {devices.length === 0 && (
              <span className="hint">Disabled: no POCT devices are registered.</span>
            )}
          </label>
          <label className="field">
            Control level
            <select
              value={qcLevel}
              disabled={levels.length === 0}
              onChange={(e) => setQcForm((f) => ({ ...f, level: e.target.value }))}
            >
              {levels.map((level) => (
                <option key={level} value={level}>{level}</option>
              ))}
            </select>
            {levels.length === 0 && (
              <span className="hint">Disabled: the programme publishes no QC levels.</span>
            )}
          </label>
          <label className="field">
            Outcome
            <select
              value={qcForm.result}
              onChange={(e) => setQcForm((f) => ({ ...f, result: e.target.value }))}
            >
              <option value="pass">Pass</option>
              <option value="fail">Fail</option>
            </select>
          </label>
          <button type="submit" disabled={busy || devices.length === 0 || levels.length === 0}>
            Record QC run
          </button>
        </form>
      )}

      <h4>Operator competency</h4>
      {canWork && (
        <form
          className="inline-form"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            const missing = [
              !compForm.staff_id.trim() && "staff id",
              !compTest && "test",
              !compForm.method.trim() && "assessment method",
              !compForm.assessed_on && "assessment date",
            ].filter(Boolean);
            if (missing.length > 0) {
              setLocalError(`Competency record needs: ${missing.join(", ")}.`);
              return;
            }
            setLocalError(null);
            void perform(`Record competency ${compForm.staff_id.trim()}`, () =>
              client.labPoctRecordCompetency({
                staff_id: compForm.staff_id.trim(),
                test_code: compTest,
                method: compForm.method.trim(),
                assessed_on: compForm.assessed_on,
              }),
            );
          }}
        >
          <label className="field">
            Staff id
            <input
              value={compForm.staff_id}
              onChange={(e) => setCompForm((f) => ({ ...f, staff_id: e.target.value }))}
              placeholder="staff-001"
            />
          </label>
          <label className="field">
            Test
            <select
              value={compTest}
              onChange={(e) => setCompForm((f) => ({ ...f, test_code: e.target.value }))}
            >
              {analytes.map((c) => (
                <option key={c.code} value={c.code}>
                  {c.display} ({c.code})
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            Assessment method
            <input
              value={compForm.method}
              onChange={(e) => setCompForm((f) => ({ ...f, method: e.target.value }))}
              placeholder="Direct observation of testing"
            />
          </label>
          <label className="field">
            Assessed on
            <input
              type="date"
              value={compForm.assessed_on}
              onChange={(e) => setCompForm((f) => ({ ...f, assessed_on: e.target.value }))}
            />
          </label>
          <button type="submit" disabled={busy}>
            Record competency
          </button>
        </form>
      )}
      {competencyState.loading ? (
        <Loading label="Loading competency records…" />
      ) : competencyState.error ? (
        <ErrorView error={competencyState.error} />
      ) : !competencyState.data || competencyState.data.length === 0 ? (
        <Empty label="No competency records yet." />
      ) : (
        <table className="lab-table">
          <thead>
            <tr>
              <th>Staff</th><th>Test</th><th>Method</th>
              <th>Assessed</th><th>Status</th><th>Expires</th>
            </tr>
          </thead>
          <tbody>
            {competencyState.data.map((record) => (
              <tr key={record.id}>
                <td>{record.staff_id}</td>
                <td>{record.test_code}</td>
                <td>{record.method}</td>
                <td>{record.assessed_on}</td>
                <td>
                  <SharedStatusBadge
                    tone={
                      record.status === "current"
                        ? "ok"
                        : record.status === "expired"
                          ? "bad"
                          : "warn"
                    }
                  >
                    {record.status}
                  </SharedStatusBadge>
                </td>
                <td>{record.expires_on ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {localError && (
        <div className="state state-error" role="alert">{localError}</div>
      )}
      <p className="hint">
        Currency is computed from the assessment date and the
        programme's published validity window — an expired record is
        never edited back to life; a new assessment is recorded.
      </p>
    </>
  );
}

/** Post-analytical governance (Depth pass): the rules a laboratorian
 *  is accountable for, read from the published catalog — auto-verify
 *  state (seeded OFF, shown as data), reflex rules, delta checks
 *  with their comparison windows, and the stewardship duplicate
 *  window with its live worklist. Editing stays in Studio. */
function GovernanceSection({
  client, tick,
}: {
  client: ApiClient;
  tick: number;
}) {
  const govState = useAsync(() => client.labGovernance(), [client, tick]);
  const dupState = useAsync(() => client.labStewardshipDuplicates(), [client, tick]);
  const gov = govState.data;

  return (
    <>
      <h3>Post-analytical governance</h3>
      {govState.loading ? (
        <Loading label="Loading governance…" />
      ) : govState.error ? (
        <ErrorView error={govState.error} />
      ) : gov ? (
        <>
          <p className="hint">
            Governed configuration — edit in {gov.config.edit_in}{" "}
            (published v{gov.config.version}). This tab is the
            operational read of that config and its recent activity.
          </p>

          <h4>Auto-verification</h4>
          <p role="status">
            <SharedStatusBadge tone={gov.auto_verify.enabled ? "warn" : "info"}>
              {gov.auto_verify.enabled ? "Auto-verify ENABLED" : "Auto-verify DISABLED"}
            </SharedStatusBadge>{" "}
            {gov.auto_verify.state_label}
          </p>
          {gov.auto_verify.rules.length === 0 ? (
            <Empty label="No analyte carries an auto-verify range in the published catalog." />
          ) : (
            <table className="lab-table">
              <thead>
                <tr><th>Analyte</th><th>Auto-verify range</th></tr>
              </thead>
              <tbody>
                {gov.auto_verify.rules.map((rule) => (
                  <tr key={rule.code}>
                    <td>{rule.display} ({rule.code})</td>
                    <td>{rule.low ?? "…"} – {rule.high ?? "…"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {gov.auto_verify.recent_auto_verified.length > 0 && (
            <>
              <h5>Recent auto-verifications (from the audit chain)</h5>
              <ul className="result-list">
                {gov.auto_verify.recent_auto_verified.map((entry) => (
                  <li key={entry.report_id} className="result-row">
                    Report {entry.report_id} · order {entry.order_id ?? "—"} ·
                    rules {entry.rule_ids.join(", ") || "—"} · {entry.at ?? "—"}
                  </li>
                ))}
              </ul>
            </>
          )}

          <h4>Reflex rules</h4>
          {gov.reflex_rules.length === 0 ? (
            <Empty label="No reflex rules are published." />
          ) : (
            <table className="lab-table">
              <thead>
                <tr><th>Rule</th><th>Trigger</th><th>When</th><th>Adds</th><th>Note</th></tr>
              </thead>
              <tbody>
                {gov.reflex_rules.map((rule) => (
                  <tr key={rule.id}>
                    <td>{rule.id}</td>
                    <td>{rule.triggerCode}</td>
                    <td>
                      {rule.when}
                      {rule.matchValue ? ` = ${rule.matchValue}` : ""}
                    </td>
                    <td>{rule.addCode}</td>
                    <td>{rule.note ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h4>Delta checks</h4>
          {gov.delta_checks.length === 0 ? (
            <Empty label="No analyte configures a delta check." />
          ) : (
            <table className="lab-table">
              <thead>
                <tr><th>Analyte</th><th>Threshold</th><th>Comparison window</th></tr>
              </thead>
              <tbody>
                {gov.delta_checks.map((check) => (
                  <tr key={check.code}>
                    <td>{check.display} ({check.code})</td>
                    <td>
                      {check.config.absolute != null
                        ? `± ${check.config.absolute} absolute`
                        : check.config.pct != null
                          ? `${check.config.pct}% change`
                          : "—"}
                    </td>
                    <td>
                      {check.config.windowHours != null
                        ? `${check.config.windowHours} h`
                        : "No window — latest released value"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h4>Stewardship — duplicate orders</h4>
          <p>
            Duplicate check{" "}
            <SharedStatusBadge tone={gov.stewardship.duplicateCheckEnabled ? "ok" : "neutral"}>
              {gov.stewardship.duplicateCheckEnabled ? "On" : "Off"}
            </SharedStatusBadge>{" "}
            · window {gov.stewardship.duplicateWindowHours ?? "—"} h. A
            duplicate warns at order placement; it never blocks.
          </p>
          {dupState.loading ? (
            <Loading label="Loading duplicates…" />
          ) : dupState.error ? (
            <ErrorView error={dupState.error} />
          ) : !dupState.data || dupState.data.total === 0 ? (
            <Empty label="No duplicate orders inside the window." />
          ) : (
            <table className="lab-table">
              <thead>
                <tr><th>Patient</th><th>Test</th><th>Open orders</th></tr>
              </thead>
              <tbody>
                {dupState.data.groups.map((group) => (
                  <tr key={`${group.patient}:${group.code}`}>
                    <td>{group.patient}</td>
                    <td>{group.display} ({group.code})</td>
                    <td>
                      {group.orders
                        .map((o) => `${o.order_id} (${o.status ?? "—"})`)
                        .join(" · ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      ) : null}
    </>
  );
}
