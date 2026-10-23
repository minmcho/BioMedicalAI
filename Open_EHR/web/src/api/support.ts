// Phase 15 support services + support operations client.
// Source of truth: backend/app/support_services.py + backend/app/support_ops.py
// (both mounted under /support). Types live here, not in api/types.ts,
// per the Phase 15 web brief. Every function takes the ApiClient as
// its first argument and calls client.request(...).

import type { ApiClient } from "./client";

export interface ListResponse<T> {
  items: T[];
  total: number;
}

// ---- BD-11 Hospital at Home (support_ops) ----

export interface HahBinding {
  id: string;
  binding_id: string;
  patient_ref: string;
  device_id: string;
  device_key: string;
  device_name: string | null;
  status: string;
  breach_count: number;
  breaches: unknown[];
  created_at: string | null;
}

export interface HahReading {
  id: string;
  reading_id: string;
  binding_id: string;
  patient_ref: string;
  reading_type: string;
  value: number;
  unit: string;
  at: string | null;
  in_range: boolean;
  breach: string | null;
  task_id: string | null;
}

export interface HahDispatch {
  id: string;
  dispatch_id: string;
  patient_ref: string;
  dispatch_type: string;
  status: string;
  assignee: string | null;
  note: string | null;
  sla_minutes: number | null;
  due_at: string | null;
}

export interface HahKit {
  id: string;
  kit_id: string;
  kit_type: string;
  contents: string[];
  status: string;
  patient_ref: string | null;
  expected_return_at: string | null;
  overdue: boolean;
}

export function listHahBindings(client: ApiClient): Promise<ListResponse<HahBinding>> {
  return client.request("/support/hah/bindings");
}
export function listHahReadings(client: ApiClient): Promise<ListResponse<HahReading> & { breaches: number }> {
  return client.request("/support/hah/readings");
}
export function listHahDispatches(client: ApiClient): Promise<ListResponse<HahDispatch>> {
  return client.request("/support/hah/dispatches");
}
export function listHahKits(client: ApiClient): Promise<ListResponse<HahKit>> {
  return client.request("/support/hah/kits");
}
export function transitionHahDispatch(client: ApiClient, id: string, toState: string, assignee?: string): Promise<HahDispatch> {
  return client.request(`/support/hah/dispatches/${encodeURIComponent(id)}/transition`, {
    method: "POST",
    body: JSON.stringify({ to_state: toState, ...(assignee ? { assignee } : {}) }),
  });
}

// ---- BD-12 Transport (support_ops, on Phase 12 rows) ----

export interface TransportRequestItem {
  id: string;
  request_id: string;
  patient_ref: string;
  from_location: string;
  to_location: string;
  mode: string | null;
  priority: string;
  status: string;
  dispatcher: string | null;
  requested_at: string | null;
  pickup_at: string | null;
  dropoff_at: string | null;
}

export interface TransportMetrics {
  request_to_pickup: { count: number; average_minutes: number | null };
  pickup_to_dropoff: { count: number; average_minutes: number | null };
  total_requests: number;
  completed: number;
  basis: string;
}

export function listTransport(client: ApiClient): Promise<ListResponse<TransportRequestItem>> {
  return client.request("/support/transport");
}
export function transportMetrics(client: ApiClient): Promise<TransportMetrics> {
  return client.request("/support/transport/metrics");
}
export function transitionTransport(client: ApiClient, id: string, toStatus: string, extra: Record<string, unknown> = {}): Promise<TransportRequestItem> {
  return client.request(`/support/transport/${encodeURIComponent(id)}/transition`, {
    method: "POST",
    body: JSON.stringify({ to_status: toStatus, ...extra }),
  });
}

// ---- BD-17 Supply chain (support_services) ----

export interface SupplyConcept { code: string; display: string; unit?: string; category?: string; active?: boolean }
export interface SupplyCatalogue { catalogue: { name?: string; concepts?: SupplyConcept[] } | null; version: number | null }
export interface StockItem { item_code: string; quantity: number }
export interface ParItem { unit: string; item_code: string; par_level: number; on_hand: number; below_par: boolean }
export interface Requisition {
  id: string; unit: string; item_code: string; quantity: number;
  case_ref: string | null; status: string; requested_by: string;
  approved_by: string | null; reject_reason: string | null; created_at: string | null;
}

export function suppliesCatalogue(client: ApiClient): Promise<SupplyCatalogue> {
  return client.request("/support/supplies/catalogue");
}
export function suppliesStock(client: ApiClient): Promise<ListResponse<StockItem>> {
  return client.request("/support/supplies/stock");
}
export function suppliesPar(client: ApiClient): Promise<ListResponse<ParItem>> {
  return client.request("/support/supplies/par");
}
export function listRequisitions(client: ApiClient): Promise<ListResponse<Requisition>> {
  return client.request("/support/requisitions");
}
export function createRequisition(client: ApiClient, input: { unit: string; item_code: string; quantity: number }): Promise<Requisition> {
  return client.request("/support/requisitions", { method: "POST", body: JSON.stringify(input) });
}
export function requisitionAction(client: ApiClient, id: string, action: "approve" | "fulfil" | "receive"): Promise<Requisition> {
  return client.request(`/support/requisitions/${encodeURIComponent(id)}/${action}`, { method: "POST" });
}
export function rejectRequisition(client: ApiClient, id: string, reason: string): Promise<Requisition> {
  return client.request(`/support/requisitions/${encodeURIComponent(id)}/reject`, {
    method: "POST", body: JSON.stringify({ reason }),
  });
}

// ---- BD-18 Diet office (support_services) ----

export interface DietOrder {
  id: string; patient: string; diet_type: string; texture: string | null;
  notes: string | null; unit: string | null; status: string;
  tray_checked: boolean; tray_items: string[] | null; tray_checked_by: string | null;
}
export interface DietCensusItem { unit: string; diet_type: string; unit_source: string; count: number }

export function listDietOrders(client: ApiClient): Promise<ListResponse<DietOrder>> {
  return client.request("/support/diet/orders");
}
export function dietCensus(client: ApiClient): Promise<{ items: DietCensusItem[]; total_orders: number }> {
  return client.request("/support/diet/census");
}
export function trayCheck(client: ApiClient, orderId: string, items: string[]): Promise<DietOrder> {
  return client.request(`/support/diet/orders/${encodeURIComponent(orderId)}/tray-check`, {
    method: "POST", body: JSON.stringify({ items }),
  });
}
export function createDietOrder(client: ApiClient, input: { patient: string; diet_type: string; texture?: string; unit?: string }): Promise<DietOrder> {
  return client.request("/support/diet/orders", {
    method: "POST",
    body: JSON.stringify({ subject: { reference: input.patient }, diet_type: input.diet_type, ...(input.texture ? { texture: input.texture } : {}), ...(input.unit ? { unit: input.unit } : {}) }),
  });
}

// ---- BD-19 Interpreter services ----

export interface InterpreterRequestItem {
  id: string; patient: string; language: string; modality: string;
  status: string; interpreter_name: string | null; agency: string | null;
  join_url?: string; join_basis?: string; requested_at: string | null;
}
export interface InterpreterMetrics {
  total: number;
  mean_request_to_assign_minutes: number | null;
  mean_request_to_complete_minutes: number | null;
}

export function listInterpreterRequests(client: ApiClient): Promise<ListResponse<InterpreterRequestItem>> {
  return client.request("/support/interpreter/requests");
}
export function interpreterMetrics(client: ApiClient): Promise<InterpreterMetrics> {
  return client.request("/support/interpreter/metrics");
}
export function interpreterAction(client: ApiClient, id: string, action: "start" | "complete" | "cancel"): Promise<InterpreterRequestItem> {
  return client.request(`/support/interpreter/requests/${encodeURIComponent(id)}/${action}`, { method: "POST" });
}
export function assignInterpreter(client: ApiClient, id: string, interpreterName: string, agency?: string): Promise<InterpreterRequestItem> {
  return client.request(`/support/interpreter/requests/${encodeURIComponent(id)}/assign`, {
    method: "POST", body: JSON.stringify({ interpreter_name: interpreterName, ...(agency ? { agency } : {}) }),
  });
}

// ---- BD-20 Patient experience ----

export interface ExperienceSummary {
  form_key: string;
  response_count: number | string;
  suppressed: boolean;
  small_cell_threshold: number;
  questions: Array<{ link_id: string; responses: number | string; mean: number | null; suppressed: boolean }>;
}
export interface RecoveryItem {
  id: string; source: string; patient: string | null; status: string;
  detail: Record<string, unknown>; resolution_note: string | null;
}

export function experienceSummary(client: ApiClient): Promise<ExperienceSummary> {
  return client.request("/support/experience/summary");
}
export function listRecovery(client: ApiClient): Promise<ListResponse<RecoveryItem>> {
  return client.request("/support/experience/recovery");
}
export function transitionRecovery(client: ApiClient, id: string, toStatus: string, resolutionNote?: string): Promise<RecoveryItem> {
  return client.request(`/support/experience/recovery/${encodeURIComponent(id)}/transition`, {
    method: "POST", body: JSON.stringify({ to_status: toStatus, ...(resolutionNote ? { resolution_note: resolutionNote } : {}) }),
  });
}

// ---- BD-15 CUSP ----

export interface CuspTeam { id: string; unit: string; members: string[]; facilitator: string }
export interface CuspDefect { id: string; unit: string; description: string; status: string; follow_up_due: string | null; follow_up_overdue: boolean; safety_event_id: string | null }

export function listCuspTeams(client: ApiClient): Promise<ListResponse<CuspTeam>> {
  return client.request("/support/cusp/teams");
}
export function cuspWorklist(client: ApiClient): Promise<ListResponse<CuspDefect> & { overdue: number }> {
  return client.request("/support/cusp/worklist");
}
export function transitionCuspDefect(client: ApiClient, id: string, toStatus: string): Promise<CuspDefect> {
  return client.request(`/support/cusp/defects/${encodeURIComponent(id)}/transition`, {
    method: "POST", body: JSON.stringify({ to_status: toStatus }),
  });
}

// ---- BD-16 HICS ----

export interface HicsRecord { id: string; kind: string; text: string; author: string; created_at: string | null }
export interface HicsActivation {
  id: string; incident_name: string; incident_type: string;
  assignments: Record<string, string>; status: string; activated_by: string;
  created_at: string | null; deactivated_at: string | null; records: HicsRecord[];
}

export function listHics(client: ApiClient): Promise<ListResponse<HicsActivation>> {
  return client.request("/support/hics/activations");
}
export function activateHics(client: ApiClient, input: { incident_name: string; incident_type?: string; assignments: Record<string, string> }): Promise<HicsActivation> {
  return client.request("/support/hics/activate", { method: "POST", body: JSON.stringify(input) });
}
export function deactivateHics(client: ApiClient, id: string): Promise<HicsActivation> {
  return client.request(`/support/hics/${encodeURIComponent(id)}/deactivate`, { method: "POST" });
}
export function hicsLog(client: ApiClient, id: string, text: string, kind: "log" | "situation-report"): Promise<unknown> {
  const path = kind === "log" ? "log" : "situation-reports";
  return client.request(`/support/hics/${encodeURIComponent(id)}/${path}`, { method: "POST", body: JSON.stringify({ text }) });
}

// ---- BD-26 GME ----

export interface GmeDutyLog { id: string; trainee_staff_id: string; start: string; end: string; hours: number }
export interface GmeViolation { trainee: string; rule: string; actual: number; limit: number; log_id?: string; iso_week?: string }
export interface GmeRotation { id: string; trainee_staff_id: string; service_line: string; start_date: string; end_date: string }

export function listDutyHours(client: ApiClient): Promise<ListResponse<GmeDutyLog>> {
  return client.request("/support/gme/duty-hours");
}
export function gmeViolations(client: ApiClient): Promise<{ items: GmeViolation[]; total: number; limits: { max_weekly: number; max_continuous: number; min_rest: number } }> {
  return client.request("/support/gme/violations");
}
export function listRotations(client: ApiClient): Promise<ListResponse<GmeRotation>> {
  return client.request("/support/gme/rotations");
}
