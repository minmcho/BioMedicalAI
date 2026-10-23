import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import type { Role } from "../api/types";
import * as support from "../api/support";
import { ListManager } from "../components/ListManager";
import { ModuleHeader } from "../components/ModuleHeader";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role: Role;
  /** Deep-link contract (LabPage pattern): the shell's nav sub-menu
   *  may name a section; the page reports tab changes back. */
  section?: string;
  onSectionChange?: (section: string) => void;
}

const SUPPORT_TABS = [
  { key: "hah", label: "Hospital at Home" },
  { key: "transport", label: "Transport" },
  { key: "supply", label: "Supply chain" },
  { key: "diet", label: "Diet office" },
  { key: "interpreters", label: "Interpreters" },
  { key: "experience", label: "Patient experience" },
  { key: "cusp", label: "CUSP" },
  { key: "hics", label: "HICS" },
  { key: "gme", label: "GME" },
];

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

function toneFor(status: string): StatusTone {
  if (["active", "completed", "received", "fulfilled", "bound", "available", "responded", "resolved", "closed"].includes(status)) return "ok";
  if (["requested", "assigned", "in-progress", "in-transit", "issued", "open", "contacted", "learning-shared", "distributed"].includes(status)) return "warn";
  if (["cancelled", "rejected", "retired"].includes(status)) return "bad";
  return "neutral";
}

function Badge({ status }: { status: string }) {
  return <StatusBadge tone={toneFor(status)}>{status}</StatusBadge>;
}

type Perform = (label: string, fn: () => Promise<unknown>) => Promise<void>;

export function SupportPage({ client, role, section, onSectionChange }: Props) {
  const canWork = role === "admin" || role === "configurator" || role === "clinician";
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tabLocal, setTabLocal] = useState("hah");
  const activeTab = section && SUPPORT_TABS.some((t) => t.key === section) ? section : tabLocal;
  const setActiveTab = (key: string) => { setTabLocal(key); onSectionChange?.(key); };

  async function perform(label: string, fn: () => Promise<unknown>) {
    setBusy(true); setActionError(null); setNotice(null);
    try { await fn(); setNotice(`${label} — done.`); setTick((t) => t + 1); }
    catch (err) { setActionError(`${label} failed: ${errorText(err)}`); }
    finally { setBusy(false); }
  }

  const ctx = { client, tick, canWork, busy, perform };
  return (
    <section>
      <ModuleHeader
        icon="lifebuoy"
        group="Safety & Support"
        title="Support services"
        purpose="Hospital at Home, transport, supply, diet, interpreters, experience, CUSP, HICS and GME operations."
      />
      <p className="hint">
        Operations (Hospital at Home, transport), services (supply chain, diet office,
        interpreters, patient experience) and safety &amp; training (CUSP, HICS, GME).
        Catalogues, limits and lists are published Studio configuration; the server
        enforces them and every action here is audited.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <div className="state state-error" role="alert">{actionError}</div>}
      <Tabs
        ariaLabel="Support sections"
        tabs={SUPPORT_TABS}
        activeKey={activeTab}
        onChange={setActiveTab}
        panels={{
          hah: <HahSection {...ctx} />,
          transport: <TransportSection {...ctx} />,
          supply: <SupplySection {...ctx} />,
          diet: <DietSection {...ctx} />,
          interpreters: <InterpreterSection {...ctx} />,
          experience: <ExperienceSection {...ctx} />,
          cusp: <CuspSection {...ctx} />,
          hics: <HicsSection {...ctx} />,
          gme: <GmeSection {...ctx} />,
        }}
      />
    </section>
  );
}

interface Ctx { client: ApiClient; tick: number; canWork: boolean; busy: boolean; perform: Perform }

// ---------------------------------------------------------------- HaH

function HahSection({ client, tick, canWork, busy, perform }: Ctx) {
  const bindings = useAsync(() => support.listHahBindings(client), [client, tick]);
  const readings = useAsync(() => support.listHahReadings(client), [client, tick]);
  const dispatches = useAsync(() => support.listHahDispatches(client), [client, tick]);
  const kits = useAsync(() => support.listHahKits(client), [client, tick]);
  if (bindings.loading) return <Loading label="Loading Hospital at Home…" />;
  if (bindings.error) return <ErrorView error={bindings.error} />;
  const latestReadings = [...(readings.data?.items ?? [])].slice(-10).reverse();
  return (
    <>
      <h3>Hospital at Home — telemetry bindings</h3>
      <p className="hint">Home patients bound to device-registry devices. Readings are evaluated against the published hah-ops thresholds; a breach creates a home-care Task and is counted on the binding. Feed label: simulated.</p>
      {!bindings.data || bindings.data.items.length === 0 ? <Empty label="No HaH bindings." /> : (
        <table className="data-table"><thead><tr><th>Patient</th><th>Device</th><th>Status</th><th>Breaches</th></tr></thead>
          <tbody>{bindings.data.items.map((b) => (
            <tr key={b.binding_id}><td>{b.patient_ref}</td><td>{b.device_name ?? b.device_key}<br /><span className="muted">{b.device_key}</span></td>
              <td><Badge status={b.status} /></td>
              <td className="num">{b.breach_count > 0 ? <StatusBadge tone="bad">{b.breach_count} breach(es)</StatusBadge> : "0"}</td></tr>))}
          </tbody></table>
      )}
      <h3>Latest readings</h3>
      {latestReadings.length === 0 ? <Empty label="No readings recorded yet." /> : (
        <table className="data-table"><thead><tr><th>Patient</th><th>Type</th><th>Value</th><th>When</th><th>Range</th></tr></thead>
          <tbody>{latestReadings.map((r) => (
            <tr key={r.reading_id}><td>{r.patient_ref}</td><td>{r.reading_type}</td><td className="num">{r.value} {r.unit}</td>
              <td>{r.at?.slice(0, 19).replace("T", " ") ?? "—"}</td>
              <td>{r.in_range ? <StatusBadge tone="ok">in range</StatusBadge> : <StatusBadge tone="bad">breach {r.breach ?? ""}</StatusBadge>}</td></tr>))}
          </tbody></table>
      )}
      <h3>Dispatch queue</h3>
      {!dispatches.data || dispatches.data.items.length === 0 ? <Empty label="No dispatches." /> : (
        <table className="data-table"><thead><tr><th>Patient</th><th>Type</th><th>Status</th><th>Assignee</th><th>SLA</th><th /></tr></thead>
          <tbody>{dispatches.data.items.map((d) => (
            <tr key={d.dispatch_id}><td>{d.patient_ref}</td><td>{d.dispatch_type}</td><td><Badge status={d.status} /></td>
              <td>{d.assignee ?? "—"}</td><td className="num">{d.sla_minutes != null ? `${d.sla_minutes} min` : "—"}</td>
              <td>{canWork && d.status === "requested" && <button type="button" disabled={busy} onClick={() => void perform(`Cancel dispatch ${d.dispatch_id}`, () => support.transitionHahDispatch(client, d.dispatch_id, "cancelled"))}>Cancel</button>}</td></tr>))}
          </tbody></table>
      )}
      <h3>Supply kits</h3>
      {!kits.data || kits.data.items.length === 0 ? <Empty label="No kits provisioned." /> : (
        <table className="data-table"><thead><tr><th>Kit</th><th>Type</th><th>Status</th><th>Patient</th><th>Expected return</th></tr></thead>
          <tbody>{kits.data.items.map((k) => (
            <tr key={k.kit_id}><td>{k.kit_id}</td><td>{k.kit_type}</td>
              <td><Badge status={k.status} />{k.overdue && <> <StatusBadge tone="bad">overdue</StatusBadge></>}</td>
              <td>{k.patient_ref ?? "—"}</td><td>{k.expected_return_at?.slice(0, 10) ?? "—"}</td></tr>))}
          </tbody></table>
      )}
    </>
  );
}

// ---------------------------------------------------------------- Transport

function TransportSection({ client, tick }: Ctx) {
  const requests = useAsync(() => support.listTransport(client), [client, tick]);
  const metrics = useAsync(() => support.transportMetrics(client), [client, tick]);
  if (requests.loading) return <Loading label="Loading transport…" />;
  if (requests.error) return <ErrorView error={requests.error} />;
  const m = metrics.data;
  return (
    <>
      <h3>Patient transport — requests</h3>
      {m && (
        <div className="cards">
          <div className="card"><div className="card-value">{m.total_requests}</div><div className="card-label">Total requests</div></div>
          <div className="card"><div className="card-value">{m.completed}</div><div className="card-label">Completed</div></div>
          <div className="card"><div className="card-value">{m.request_to_pickup.average_minutes ?? "—"}</div><div className="card-label">Avg request → pickup (min, n={m.request_to_pickup.count})</div></div>
          <div className="card"><div className="card-value">{m.pickup_to_dropoff.average_minutes ?? "—"}</div><div className="card-label">Avg pickup → dropoff (min, n={m.pickup_to_dropoff.count})</div></div>
        </div>
      )}
      {!requests.data || requests.data.items.length === 0 ? <Empty label="No transport requests." /> : (
        <table className="data-table"><thead><tr><th>Patient</th><th>From → To</th><th>Mode</th><th>Priority</th><th>Status</th><th>Dispatcher</th></tr></thead>
          <tbody>{requests.data.items.map((r) => (
            <tr key={r.request_id}><td>{r.patient_ref}</td><td>{r.from_location} → {r.to_location}</td><td>{r.mode ?? "—"}</td>
              <td>{r.priority}</td><td><Badge status={r.status} /></td><td>{r.dispatcher ?? "—"}</td></tr>))}
          </tbody></table>
      )}
    </>
  );
}

// ---------------------------------------------------------------- Supply chain

function SupplySection({ client, tick, canWork, busy, perform }: Ctx) {
  const catalogue = useAsync(() => support.suppliesCatalogue(client), [client, tick]);
  const stock = useAsync(() => support.suppliesStock(client), [client, tick]);
  const par = useAsync(() => support.suppliesPar(client), [client, tick]);
  const reqs = useAsync(() => support.listRequisitions(client), [client, tick]);
  const [rejectReasons, setRejectReasons] = useState<Record<string, string>>({});
  const [unit, setUnit] = useState("");
  const [itemCode, setItemCode] = useState("");
  const [quantity, setQuantity] = useState("1");
  if (catalogue.loading) return <Loading label="Loading supply chain…" />;
  if (catalogue.error) return <ErrorView error={catalogue.error} />;
  const concepts = catalogue.data?.catalogue?.concepts ?? [];
  const stockByCode = new Map((stock.data?.items ?? []).map((s) => [s.item_code, s.quantity]));
  const selectedItem = itemCode || concepts[0]?.code || "";
  return (
    <>
      <h3>Supply catalogue &amp; central stock</h3>
      {concepts.length === 0 ? <Empty label="No published supplies catalogue." /> : (
        <table className="data-table"><thead><tr><th>Item</th><th>Category</th><th>Unit</th><th>Central stock</th></tr></thead>
          <tbody>{concepts.map((c) => (
            <tr key={c.code}><td>{c.display}<br /><span className="muted">{c.code}</span></td><td>{c.category ?? "—"}</td><td>{c.unit ?? "—"}</td><td className="num">{stockByCode.get(c.code) ?? "—"}</td></tr>))}
          </tbody></table>
      )}
      <h3>Unit PAR levels</h3>
      {!par.data || par.data.items.length === 0 ? <Empty label="No PAR levels set." /> : (
        <table className="data-table"><thead><tr><th>Unit</th><th>Item</th><th>PAR</th><th>On hand</th><th /></tr></thead>
          <tbody>{par.data.items.map((p) => (
            <tr key={`${p.unit}-${p.item_code}`}><td>{p.unit}</td><td>{p.item_code}</td><td className="num">{p.par_level}</td><td className="num">{p.on_hand}</td>
              <td>{p.below_par ? <StatusBadge tone="bad">below PAR</StatusBadge> : <StatusBadge tone="ok">at PAR</StatusBadge>}</td></tr>))}
          </tbody></table>
      )}
      <h3>Requisitions</h3>
      {canWork && (
        <form className="inline-form" onSubmit={(e) => { e.preventDefault(); void perform("Create requisition", () => support.createRequisition(client, { unit: unit.trim(), item_code: selectedItem, quantity: Number(quantity) })); }}>
          <label className="field">Unit<input value={unit} onChange={(e) => setUnit(e.target.value)} /></label>
          <label className="field">Item<select value={selectedItem} onChange={(e) => setItemCode(e.target.value)}>{concepts.map((c) => <option key={c.code} value={c.code}>{c.display}</option>)}</select></label>
          <label className="field">Quantity<input inputMode="numeric" value={quantity} onChange={(e) => setQuantity(e.target.value)} /></label>
          <button type="submit" disabled={busy || !unit.trim() || !selectedItem}>New requisition</button>
        </form>
      )}
      {!reqs.data || reqs.data.items.length === 0 ? <Empty label="No requisitions." /> : (
        <table className="data-table"><thead><tr><th>Unit</th><th>Item</th><th>Qty</th><th>Status</th><th>Requested by</th><th>Actions</th></tr></thead>
          <tbody>{reqs.data.items.map((r) => (
            <tr key={r.id}><td>{r.unit}</td><td>{r.item_code}</td><td className="num">{r.quantity}</td><td><Badge status={r.status} />{r.reject_reason && <><br /><span className="muted">{r.reject_reason}</span></>}</td><td>{r.requested_by}</td>
              <td>{canWork && (<span className="appt-actions">
                {r.status === "requested" && <button type="button" disabled={busy} onClick={() => void perform(`Approve requisition ${r.id}`, () => support.requisitionAction(client, r.id, "approve"))}>Approve</button>}
                {(r.status === "requested" || r.status === "approved") && (<><input aria-label={`Reject reason for requisition ${r.id}`} placeholder="Reject reason" value={rejectReasons[r.id] ?? ""} onChange={(e) => setRejectReasons((m) => ({ ...m, [r.id]: e.target.value }))} /><button type="button" disabled={busy || !(rejectReasons[r.id] ?? "").trim()} onClick={() => void perform(`Reject requisition ${r.id}`, () => support.rejectRequisition(client, r.id, (rejectReasons[r.id] ?? "").trim()))}>Reject</button></>)}
                {r.status === "approved" && <button type="button" disabled={busy} onClick={() => void perform(`Fulfil requisition ${r.id}`, () => support.requisitionAction(client, r.id, "fulfil"))}>Fulfil</button>}
                {r.status === "fulfilled" && <button type="button" disabled={busy} onClick={() => void perform(`Receive requisition ${r.id}`, () => support.requisitionAction(client, r.id, "receive"))}>Receive</button>}
              </span>)}</td></tr>))}
          </tbody></table>
      )}
    </>
  );
}

// ---------------------------------------------------------------- Diet office

function DietSection({ client, tick, canWork, busy, perform }: Ctx) {
  const orders = useAsync(() => support.listDietOrders(client), [client, tick]);
  const census = useAsync(() => support.dietCensus(client), [client, tick]);
  const dietTypes = useAsync(
    () => (typeof client.effectiveConfig === "function"
      ? client.effectiveConfig("terminology", "diet-types").then((c) => (c.payload as { concepts?: Array<{ code: string; display: string; active?: boolean }> }).concepts ?? []).catch(() => [] as Array<{ code: string; display: string; active?: boolean }>)
      : Promise.resolve([] as Array<{ code: string; display: string; active?: boolean }>)),
    [client],
  );
  const [trayItems, setTrayItems] = useState<Record<string, string>>({});
  const [trayError, setTrayError] = useState<string | null>(null);
  const [patient, setPatient] = useState("");
  const [dietType, setDietType] = useState("");
  if (orders.loading) return <Loading label="Loading diet office…" />;
  if (orders.error) return <ErrorView error={orders.error} />;
  const types = dietTypes.data ?? [];
  const selectedDiet = dietType || types[0]?.code || "";

  async function doTrayCheck(orderId: string) {
    setTrayError(null);
    const items = (trayItems[orderId] ?? "").split(",").map((s) => s.trim()).filter(Boolean);
    try {
      await support.trayCheck(client, orderId, items);
      await perform("Tray check", () => Promise.resolve());
    } catch (err) {
      // The allergy hard-stop detail is surfaced verbatim — it names
      // the allergen and is the safety signal, never paraphrased.
      setTrayError(errorText(err));
    }
  }
  return (
    <>
      <h3>Diet orders worklist</h3>
      <p className="hint">Diet types come from the published terminology/diet-types list; authorized managers edit it on the governed draft here — changes take effect when a human publishes in Studio.</p>
      <ListManager client={client} kind="terminology" listKey="diet-types" label="Diet types" />
      {canWork && types.length > 0 && (
        <form className="inline-form" onSubmit={(e) => { e.preventDefault(); void perform("Create diet order", () => support.createDietOrder(client, { patient: patient.trim().startsWith("Patient/") ? patient.trim() : `Patient/${patient.trim()}`, diet_type: selectedDiet })); }}>
          <label className="field">Patient id<input value={patient} onChange={(e) => setPatient(e.target.value)} placeholder="pat-1" /></label>
          <label className="field">Diet type<select value={selectedDiet} onChange={(e) => setDietType(e.target.value)}>{types.filter((t) => t.active !== false).map((t) => <option key={t.code} value={t.code}>{t.display}</option>)}</select></label>
          <button type="submit" disabled={busy || !patient.trim() || !selectedDiet}>New diet order</button>
        </form>
      )}
      {trayError && <div className="state state-error" role="alert">{trayError}</div>}
      {!orders.data || orders.data.items.length === 0 ? <Empty label="No diet orders." /> : (
        <table className="data-table"><thead><tr><th>Patient</th><th>Diet</th><th>Texture</th><th>Unit</th><th>Status</th><th>Tray check</th></tr></thead>
          <tbody>{orders.data.items.map((o) => (
            <tr key={o.id}><td>{o.patient}</td><td>{o.diet_type}</td><td>{o.texture ?? "—"}</td><td>{o.unit ?? "—"}</td>
              <td><Badge status={o.status} />{o.tray_checked && <> <StatusBadge tone="ok">tray checked</StatusBadge></>}</td>
              <td>{canWork && !o.tray_checked ? (<span className="appt-actions">
                <input aria-label={`Tray items for order ${o.id}`} placeholder="Tray items, comma-separated" value={trayItems[o.id] ?? ""} onChange={(e) => setTrayItems((m) => ({ ...m, [o.id]: e.target.value }))} />
                <button type="button" disabled={busy || !(trayItems[o.id] ?? "").trim()} onClick={() => void doTrayCheck(o.id)}>Tray check</button>
              </span>) : (o.tray_checked_by ?? "—")}</td></tr>))}
          </tbody></table>
      )}
      <h3>Meal census (by unit / diet)</h3>
      {!census.data || census.data.items.length === 0 ? <Empty label="No active diet orders for the census." /> : (
        <table className="data-table"><thead><tr><th>Unit</th><th>Diet</th><th>Count</th><th>Unit source</th></tr></thead>
          <tbody>{census.data.items.map((c) => (
            <tr key={`${c.unit}-${c.diet_type}`}><td>{c.unit}</td><td>{c.diet_type}</td><td className="num">{c.count}</td><td>{c.unit_source}</td></tr>))}
          </tbody></table>
      )}
    </>
  );
}

// ---------------------------------------------------------------- Interpreters

function InterpreterSection({ client, tick, canWork, busy, perform }: Ctx) {
  const requests = useAsync(() => support.listInterpreterRequests(client), [client, tick]);
  const metrics = useAsync(() => support.interpreterMetrics(client), [client, tick]);
  const [names, setNames] = useState<Record<string, string>>({});
  if (requests.loading) return <Loading label="Loading interpreter requests…" />;
  if (requests.error) return <ErrorView error={requests.error} />;
  const m = metrics.data;
  return (
    <>
      <h3>Interpreter requests</h3>
      <p className="hint">Languages come from the published terminology/languages list; authorized managers edit it on the governed draft here — changes take effect when a human publishes in Studio.</p>
      <ListManager client={client} kind="terminology" listKey="languages" label="Languages" />
      {m && (
        <div className="cards">
          <div className="card"><div className="card-value">{m.total}</div><div className="card-label">Total requests</div></div>
          <div className="card"><div className="card-value">{m.mean_request_to_assign_minutes != null ? m.mean_request_to_assign_minutes.toFixed(1) : "—"}</div><div className="card-label">Mean request → assign (min)</div></div>
          <div className="card"><div className="card-value">{m.mean_request_to_complete_minutes != null ? m.mean_request_to_complete_minutes.toFixed(1) : "—"}</div><div className="card-label">Mean request → complete (min)</div></div>
        </div>
      )}
      {!requests.data || requests.data.items.length === 0 ? <Empty label="No interpreter requests." /> : (
        <table className="data-table"><thead><tr><th>Patient</th><th>Language</th><th>Modality</th><th>Status</th><th>Interpreter</th><th>Actions</th></tr></thead>
          <tbody>{requests.data.items.map((r) => (
            <tr key={r.id}><td>{r.patient}</td><td>{r.language}</td><td>{r.modality}{r.join_url && <><br /><a href={r.join_url}>Join video (external provider)</a></>}</td>
              <td><Badge status={r.status} /></td><td>{r.interpreter_name ?? "—"}{r.agency ? ` (${r.agency})` : ""}</td>
              <td>{canWork && (<span className="appt-actions">
                {r.status === "requested" && (<><input aria-label={`Interpreter name for request ${r.id}`} placeholder="Interpreter name" value={names[r.id] ?? ""} onChange={(e) => setNames((mm) => ({ ...mm, [r.id]: e.target.value }))} /><button type="button" disabled={busy || !(names[r.id] ?? "").trim()} onClick={() => void perform(`Assign interpreter ${r.id}`, () => support.assignInterpreter(client, r.id, (names[r.id] ?? "").trim()))}>Assign</button></>)}
                {r.status === "assigned" && <button type="button" disabled={busy} onClick={() => void perform(`Start interpreter ${r.id}`, () => support.interpreterAction(client, r.id, "start"))}>Start</button>}
                {r.status === "in-progress" && <button type="button" disabled={busy} onClick={() => void perform(`Complete interpreter ${r.id}`, () => support.interpreterAction(client, r.id, "complete"))}>Complete</button>}
                {["requested", "assigned", "in-progress"].includes(r.status) && <button type="button" disabled={busy} onClick={() => void perform(`Cancel interpreter ${r.id}`, () => support.interpreterAction(client, r.id, "cancel"))}>Cancel</button>}
              </span>)}</td></tr>))}
          </tbody></table>
      )}
    </>
  );
}

// ---------------------------------------------------------------- Patient experience

function ExperienceSection({ client, tick, canWork, busy, perform }: Ctx) {
  const summary = useAsync(() => support.experienceSummary(client), [client, tick]);
  const recovery = useAsync(() => support.listRecovery(client), [client, tick]);
  const [notes, setNotes] = useState<Record<string, string>>({});
  if (summary.loading) return <Loading label="Loading patient experience…" />;
  if (summary.error) return <ErrorView error={summary.error} />;
  const s = summary.data;
  return (
    <>
      <h3>Experience summary</h3>
      {s && (
        <>
          <p className="hint">Responses: {String(s.response_count)}{s.suppressed ? ` — small-cell suppression is on (cells below ${s.small_cell_threshold} responses are shown as "<${s.small_cell_threshold}", exactly as the server sends them).` : ""}</p>
          <table className="data-table"><thead><tr><th>Question</th><th>Responses</th><th>Mean</th></tr></thead>
            <tbody>{s.questions.map((q) => (
              <tr key={q.link_id}><td>{q.link_id}</td><td className="num">{String(q.responses)}</td><td className="num">{q.mean != null ? q.mean.toFixed(2) : "—"}</td></tr>))}
            </tbody></table>
        </>
      )}
      <h3>Service recovery queue</h3>
      {!recovery.data || recovery.data.items.length === 0 ? <Empty label="No open recovery items." /> : (
        <table className="data-table"><thead><tr><th>Source</th><th>Patient</th><th>Detail</th><th>Status</th><th>Actions</th></tr></thead>
          <tbody>{recovery.data.items.map((r) => (
            <tr key={r.id}><td>{r.source}</td><td>{r.patient ?? "—"}</td>
              <td>{String(r.detail.description ?? r.detail.reason ?? "")}</td><td><Badge status={r.status} /></td>
              <td>{canWork && (<span className="appt-actions">
                {r.status === "open" && <button type="button" disabled={busy} onClick={() => void perform(`Contact recovery ${r.id}`, () => support.transitionRecovery(client, r.id, "contacted"))}>Contacted</button>}
                {r.status !== "resolved" && (<><input aria-label={`Resolution note for recovery ${r.id}`} placeholder="Resolution note" value={notes[r.id] ?? ""} onChange={(e) => setNotes((m) => ({ ...m, [r.id]: e.target.value }))} /><button type="button" disabled={busy || !(notes[r.id] ?? "").trim()} onClick={() => void perform(`Resolve recovery ${r.id}`, () => support.transitionRecovery(client, r.id, "resolved", (notes[r.id] ?? "").trim()))}>Resolve</button></>)}
              </span>)}</td></tr>))}
          </tbody></table>
      )}
    </>
  );
}

// ---------------------------------------------------------------- CUSP

function CuspSection({ client, tick, canWork, busy, perform }: Ctx) {
  const teams = useAsync(() => support.listCuspTeams(client), [client, tick]);
  const worklist = useAsync(() => support.cuspWorklist(client), [client, tick]);
  if (teams.loading) return <Loading label="Loading CUSP…" />;
  if (teams.error) return <ErrorView error={teams.error} />;
  return (
    <>
      <h3>CUSP teams</h3>
      {!teams.data || teams.data.items.length === 0 ? <Empty label="No CUSP teams." /> : (
        <table className="data-table"><thead><tr><th>Unit</th><th>Facilitator</th><th>Members</th></tr></thead>
          <tbody>{teams.data.items.map((t) => (
            <tr key={t.id}><td>{t.unit}</td><td>{t.facilitator}</td><td>{t.members.join(", ")}</td></tr>))}
          </tbody></table>
      )}
      <h3>Defect worklist{worklist.data ? ` (${worklist.data.total} open, ${worklist.data.overdue} overdue)` : ""}</h3>
      {!worklist.data || worklist.data.items.length === 0 ? <Empty label="No open defects." /> : (
        <table className="data-table"><thead><tr><th>Unit</th><th>Description</th><th>Status</th><th>Follow-up due</th><th>Actions</th></tr></thead>
          <tbody>{worklist.data.items.map((d) => (
            <tr key={d.id}><td>{d.unit}</td><td>{d.description}</td><td><Badge status={d.status} /></td>
              <td>{d.follow_up_due ?? "—"}{d.follow_up_overdue && <> <StatusBadge tone="bad">overdue</StatusBadge></>}</td>
              <td>{canWork && (<span className="appt-actions">
                {d.status === "open" && <button type="button" disabled={busy} onClick={() => void perform(`Share learning for defect ${d.id}`, () => support.transitionCuspDefect(client, d.id, "learning-shared"))}>Share learning</button>}
                {d.status !== "closed" && <button type="button" disabled={busy} onClick={() => void perform(`Close defect ${d.id}`, () => support.transitionCuspDefect(client, d.id, "closed"))}>Close</button>}
              </span>)}</td></tr>))}
          </tbody></table>
      )}
    </>
  );
}

// ---------------------------------------------------------------- HICS

function HicsSection({ client, tick, canWork, busy, perform }: Ctx) {
  const activations = useAsync(() => support.listHics(client), [client, tick]);
  const [name, setName] = useState("");
  const [type, setType] = useState("");
  const [roleCode, setRoleCode] = useState("incident-commander");
  const [staffId, setStaffId] = useState("");
  const [logText, setLogText] = useState<Record<string, string>>({});
  if (activations.loading) return <Loading label="Loading HICS…" />;
  if (activations.error) return <ErrorView error={activations.error} />;
  const items = activations.data?.items ?? [];
  const active = items.find((a) => a.status === "active");
  return (
    <>
      <h3>Incident command (HICS)</h3>
      {active ? (
        <p className="hint"><StatusBadge tone="bad">ACTIVE INCIDENT</StatusBadge> {active.incident_name} ({active.incident_type || "—"}) — downtime posture engaged by activation.</p>
      ) : <p className="hint">No active incident.</p>}
      {canWork && !active && (
        <form className="inline-form" onSubmit={(e) => { e.preventDefault(); void perform("Activate HICS", () => support.activateHics(client, { incident_name: name.trim(), incident_type: type.trim() || undefined, assignments: { [roleCode]: staffId.trim() } })); }}>
          <label className="field">Incident name<input value={name} onChange={(e) => setName(e.target.value)} /></label>
          <label className="field">Incident type<input value={type} onChange={(e) => setType(e.target.value)} /></label>
          <label className="field">Commander role<input value={roleCode} onChange={(e) => setRoleCode(e.target.value)} /></label>
          <label className="field">Commander staff id<input value={staffId} onChange={(e) => setStaffId(e.target.value)} /></label>
          <button type="submit" disabled={busy || !name.trim() || !staffId.trim()}>Activate incident command</button>
        </form>
      )}
      {items.length === 0 ? <Empty label="No HICS activations recorded." /> : items.map((a) => (
        <div key={a.id} className="card">
          <p><strong>{a.incident_name}</strong> — <Badge status={a.status} /> · activated by {a.activated_by}</p>
          <p className="muted">Assignments: {Object.entries(a.assignments).map(([k, v]) => `${k}: ${v}`).join(", ")}</p>
          {canWork && a.status === "active" && (
            <span className="appt-actions">
              <input aria-label={`Log entry for activation ${a.id}`} placeholder="Log entry" value={logText[a.id] ?? ""} onChange={(e) => setLogText((m) => ({ ...m, [a.id]: e.target.value }))} />
              <button type="button" disabled={busy || !(logText[a.id] ?? "").trim()} onClick={() => void perform(`Add log to ${a.id}`, () => support.hicsLog(client, a.id, (logText[a.id] ?? "").trim(), "log"))}>Add log</button>
              <button type="button" disabled={busy || !(logText[a.id] ?? "").trim()} onClick={() => void perform(`Add situation report to ${a.id}`, () => support.hicsLog(client, a.id, (logText[a.id] ?? "").trim(), "situation-report"))}>Add situation report</button>
              <button type="button" disabled={busy} onClick={() => void perform(`Deactivate HICS ${a.id}`, () => support.deactivateHics(client, a.id))}>Deactivate</button>
            </span>
          )}
          {a.records.length > 0 && (
            <ul>{a.records.map((r) => <li key={r.id}><span className="muted">{r.kind} · {r.author}:</span> {r.text}</li>)}</ul>
          )}
        </div>
      ))}
    </>
  );
}

// ---------------------------------------------------------------- GME

function GmeSection({ client, tick }: Ctx) {
  const logs = useAsync(() => support.listDutyHours(client), [client, tick]);
  const violations = useAsync(() => support.gmeViolations(client), [client, tick]);
  const rotations = useAsync(() => support.listRotations(client), [client, tick]);
  if (logs.loading) return <Loading label="Loading GME…" />;
  if (logs.error) return <ErrorView error={logs.error} />;
  const v = violations.data;
  return (
    <>
      <h3>GME duty-hour log</h3>
      {v && <p className="hint">Published limits: max weekly {v.limits.max_weekly}h · max continuous {v.limits.max_continuous}h · min rest {v.limits.min_rest}h.</p>}
      {!logs.data || logs.data.items.length === 0 ? <Empty label="No duty hours logged." /> : (
        <table className="data-table"><thead><tr><th>Trainee</th><th>Start</th><th>End</th><th>Hours</th></tr></thead>
          <tbody>{logs.data.items.map((l) => (
            <tr key={l.id}><td>{l.trainee_staff_id}</td><td>{l.start.slice(0, 16).replace("T", " ")}</td><td>{l.end.slice(0, 16).replace("T", " ")}</td><td className="num">{l.hours}</td></tr>))}
          </tbody></table>
      )}
      <h3>Duty-hour violations</h3>
      {!v || v.items.length === 0 ? <Empty label="No violations against the published limits." /> : (
        <table className="data-table"><thead><tr><th>Trainee</th><th>Rule</th><th>Actual</th><th>Limit</th></tr></thead>
          <tbody>{v.items.map((item, i) => (
            <tr key={`${item.trainee}-${item.rule}-${i}`}><td>{item.trainee}</td><td>{item.rule}</td>
              <td><StatusBadge tone="bad">{item.actual}h actual</StatusBadge></td><td>{item.limit}h limit</td></tr>))}
          </tbody></table>
      )}
      <h3>Rotations</h3>
      {!rotations.data || rotations.data.items.length === 0 ? <Empty label="No rotations scheduled." /> : (
        <table className="data-table"><thead><tr><th>Trainee</th><th>Service line</th><th>Start</th><th>End</th></tr></thead>
          <tbody>{rotations.data.items.map((r) => (
            <tr key={r.id}><td>{r.trainee_staff_id}</td><td>{r.service_line}</td><td>{r.start_date}</td><td>{r.end_date}</td></tr>))}
          </tbody></table>
      )}
    </>
  );
}
