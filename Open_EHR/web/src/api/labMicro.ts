// LAB-D12 — microbiology workup depth client (app.lab_micro).
// Types live here, not in api/types.ts (the labDepth.ts precedent).
import type { ApiClient } from "./client";

export interface MicroOption {
  code: string;
  display: string;
  active?: boolean;
  carriesConfidence?: boolean;
  minConfidence?: number;
}

export interface MicroOrganism extends MicroOption {
  mdro?: boolean;
  reportable?: boolean;
}

export interface MicroVocab {
  gramMorphologies: MicroOption[];
  quantities: MicroOption[];
  cellBands: MicroOption[];
  adequacyValues: MicroOption[];
  media: MicroOption[];
  streakPatterns: MicroOption[];
  atmospheres: MicroOption[];
  cultureTypes: MicroOption[];
  bottleTypes: MicroOption[];
  idMethods: MicroOption[];
  antibiogramMinIsolates: number;
  antibiogramPeriodDays: number;
}

export interface BreakpointTable {
  standard: string;
  version: string;
  entries: unknown[];
}

export interface MicroDepthConfig {
  vocab: MicroVocab;
  organisms: MicroOrganism[];
  organisms_version: number | null;
  antibiotics: MicroOption[];
  antibiotics_version: number | null;
  breakpoint_tables: BreakpointTable[];
  breakpoints_version: number | null;
  expert_rules: unknown[];
  selective_rules: unknown[];
}

export interface AstEntry {
  antibiotic: string;
  antibiotic_display?: string;
  mic: number | null;
  zone_mm: number | null;
  standard?: string;
  version?: string;
  computed_interpretation: string | null;
  interpretation_note?: string | null;
  expert_traces: { rule_code: string; rule_display?: string; reason?: string;
                   from_interpretation: string; to_interpretation: string }[];
  final_interpretation: string | null;
  suppressed?: boolean;
  suppression_reason?: string;
}

export interface MicroIsolate {
  isolate_id: string;
  organism_code: string;
  organism_display?: string;
  id_method: string;
  id_confidence: number | null;
  id_below_threshold: boolean;
  ast: AstEntry[];
}

export interface CriticalCall {
  state: string;
  reason?: string;
  notified_to?: string | null;
  read_back?: boolean | null;
  notified_by?: string | null;
  notified_at?: string | null;
}

export interface MicroWorkup {
  order_id: string;
  patient_ref: string | null;
  culture_type: string | null;
  plating: {
    media: string[]; streak_pattern: string;
    incubation: { temperature_c: number; atmosphere: string;
                  duration_hours: number };
    started_at: string; recorded_by: string;
  } | null;
  gram_stain: {
    morphology: string; quantity: string; wbc: string;
    epithelial_cells: string; adequacy: string;
    organisms_present: boolean; comment?: string | null;
  } | null;
  critical_call: CriticalCall | null;
  colony: {
    cfu_ml?: number; cfu_band?: string; growth_amount?: string;
    bottle?: { set_id: string; bottle_type: string;
               time_to_positivity_hours: number };
  } | null;
  isolates: MicroIsolate[];
  mdro_alerts: { kind: string; ref: string; organism: string;
                 hai_type?: string | null; mechanism: string }[];
  finalized_at: string | null;
  view?: string;
}

export interface AntibiogramItem {
  organism: string;
  organism_display?: string;
  antibiotic: string;
  antibiotic_display?: string;
  isolates: number;
  susceptible: number;
  pct_s: number | null;
  suppressed: boolean;
  suppression_reason: string | null;
}

const base = (orderId: string) =>
  `/lab/micro-depth/orders/${encodeURIComponent(orderId)}`;

export function getMicroDepthConfig(client: ApiClient): Promise<MicroDepthConfig> {
  return client.request("/lab/micro-depth/config");
}

export function getMicroWorkup(
  client: ApiClient, orderId: string, view: "lab" | "released",
): Promise<{ workup: MicroWorkup; order_status: string }> {
  return client.request(`${base(orderId)}/workup?view=${view}`);
}

export function recordPlating(client: ApiClient, orderId: string, body: unknown) {
  return client.request(`${base(orderId)}/plating`, {
    method: "POST", body: JSON.stringify(body),
  });
}

export function recordGramStain(client: ApiClient, orderId: string, body: unknown) {
  return client.request(`${base(orderId)}/gram-stain`, {
    method: "POST", body: JSON.stringify(body),
  });
}

export function notifyGramStain(client: ApiClient, orderId: string, body: unknown) {
  return client.request(`${base(orderId)}/gram-stain/notify`, {
    method: "POST", body: JSON.stringify(body),
  });
}

export function acknowledgeGramStain(client: ApiClient, orderId: string) {
  return client.request(`${base(orderId)}/gram-stain/acknowledge`, {
    method: "POST", body: "{}",
  });
}

export function recordColony(client: ApiClient, orderId: string, body: unknown) {
  return client.request(`${base(orderId)}/colony`, {
    method: "POST", body: JSON.stringify(body),
  });
}

export function addIsolate(client: ApiClient, orderId: string, body: unknown) {
  return client.request(`${base(orderId)}/isolates`, {
    method: "POST", body: JSON.stringify(body),
  });
}

export function removeIsolate(client: ApiClient, orderId: string, isolateId: string) {
  return client.request(`${base(orderId)}/isolates/${encodeURIComponent(isolateId)}`, {
    method: "DELETE",
  });
}

export function recordAst(
  client: ApiClient, orderId: string, isolateId: string, body: unknown,
) {
  return client.request(
    `${base(orderId)}/isolates/${encodeURIComponent(isolateId)}/ast`,
    { method: "POST", body: JSON.stringify(body) });
}

export function getAntibiogram(
  client: ApiClient, minIsolates?: number,
): Promise<{ items: AntibiogramItem[]; total: number;
             period: { from: string; to: string };
             min_isolates: number; governed_min_isolates: number }> {
  const qs = minIsolates ? `?min_isolates=${minIsolates}` : "";
  return client.request(`/lab/micro-depth/antibiogram${qs}`);
}
