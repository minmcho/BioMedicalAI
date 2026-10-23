// Phase 15 (E) — surgery department depth client.
// Source of truth: backend/app/surgery_depth.py (routes under
// /surgery). Types live here, not in types.ts (shared-file rule).
// Labels/lists/thresholds come from published Studio terminology
// (surgery-urgency, or-blocks, instrument-trays, sterile-processing,
// pacu-scoring, surgery-complications); the parsers below read the
// published payload shape and return null when unusable so callers
// keep a built-in fallback identical to the backend seed defaults.

import type { ApiClient } from "./client";
import type { QuestionnaireAnswerItem } from "./types";

// ---------------------------------------------------------------------
// Case requests + waitlist
// ---------------------------------------------------------------------

export interface SurgeryRequestItem {
  request_id: string;
  patient_ref: string;
  procedure_code: string;
  procedure_display?: string | null;
  surgeon: string;
  urgency: string;
  urgency_rank: number;
  requested_window: { start?: string | null; end?: string | null };
  status: string;
  decline_reason?: string | null;
  case_id?: string | null;
  created_by?: string;
  decided_by?: string | null;
  created_at?: string;
  wait_minutes?: number;
  wait_hours?: number;
}

export interface SurgeryRequestsResponse {
  items: SurgeryRequestItem[];
  total: number;
}

export interface SurgeryWaitlistResponse extends SurgeryRequestsResponse {
  generated_at?: string;
}

export interface ScheduleRequestResult {
  case: unknown;
  appointment: unknown;
  room: string;
  state: string;
  request: SurgeryRequestItem;
  in_block: boolean;
  block_code?: string | null;
  block?: Record<string, unknown> | null;
}

export function listSurgeryRequests(client: ApiClient): Promise<SurgeryRequestsResponse> {
  return client.request("/surgery/requests");
}

export function getSurgeryWaitlist(client: ApiClient): Promise<SurgeryWaitlistResponse> {
  return client.request("/surgery/waitlist");
}

export function createSurgeryRequest(
  client: ApiClient,
  input: {
    patientId: string;
    procedureCode: string;
    procedureDisplay?: string;
    surgeon: string;
    urgency: string;
    requestedStart?: string;
    requestedEnd?: string;
  },
): Promise<SurgeryRequestItem> {
  return client.request("/surgery/requests", {
    method: "POST",
    body: JSON.stringify({
      patient_id: input.patientId,
      procedure_code: input.procedureCode,
      ...(input.procedureDisplay ? { procedure_display: input.procedureDisplay } : {}),
      surgeon: input.surgeon,
      urgency: input.urgency,
      ...(input.requestedStart ? { requested_start: input.requestedStart } : {}),
      ...(input.requestedEnd ? { requested_end: input.requestedEnd } : {}),
    }),
  });
}

export function approveSurgeryRequest(
  client: ApiClient,
  requestId: string,
): Promise<SurgeryRequestItem> {
  return client.request(`/surgery/requests/${encodeURIComponent(requestId)}/approve`, {
    method: "POST",
  });
}

export function declineSurgeryRequest(
  client: ApiClient,
  requestId: string,
  reason: string,
): Promise<SurgeryRequestItem> {
  return client.request(`/surgery/requests/${encodeURIComponent(requestId)}/decline`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

/** POST /surgery/requests/{id}/schedule — creates the real boards
 *  case; the response REPORTS the block posture (in_block). */
export function scheduleSurgeryRequest(
  client: ApiClient,
  requestId: string,
  input: { room: string; start: string; end: string },
): Promise<ScheduleRequestResult> {
  return client.request(`/surgery/requests/${encodeURIComponent(requestId)}/schedule`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

// ---------------------------------------------------------------------
// OR blocks (terminology/or-blocks) + utilisation
// ---------------------------------------------------------------------

export interface OrBlock {
  code?: string;
  surgeon?: string;
  service?: string;
  room?: string;
  weekday?: string;
  start?: string;
  end?: string;
}

/** Blocks from a published terminology/or-blocks payload. */
export function blocksFromPayload(payload: unknown): OrBlock[] | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const blocks = (payload as { blocks?: unknown }).blocks;
  if (!Array.isArray(blocks)) return null;
  const out = blocks.filter(
    (b): b is OrBlock => !!b && typeof b === "object",
  );
  return out.length > 0 ? out : null;
}

export interface UtilisationBlock {
  block_code?: string | null;
  surgeon?: string | null;
  service?: string | null;
  start: string;
  end: string;
  block_minutes: number;
  scheduled_minutes: number;
  utilisation_pct: number | null;
}

export interface UtilisationRoom {
  room: string;
  display?: string | null;
  cases: number;
  scheduled_minutes: number;
  block_minutes: number;
  in_block_minutes: number;
  non_block_minutes: number;
  utilisation_pct: number | null;
  blocks: UtilisationBlock[];
  turnover: {
    gaps: Array<{ after_case: string; before_case: string; minutes: number }>;
    total_gap_minutes: number;
    count: number;
  };
}

export interface SurgeryUtilisation {
  date: string;
  weekday: string;
  rooms: UtilisationRoom[];
}

export function getSurgeryUtilisation(
  client: ApiClient,
  date: string,
): Promise<SurgeryUtilisation> {
  return client.request(`/surgery/utilisation?date=${encodeURIComponent(date)}`);
}

// ---------------------------------------------------------------------
// Urgency concepts (terminology/surgery-urgency)
// ---------------------------------------------------------------------

export interface ConceptOption {
  code: string;
  display?: string;
  rank?: number;
  active?: boolean;
  max?: number;
}

/** Concepts from a published terminology payload ({concepts:[...]}). */
export function conceptsFromPayload(payload: unknown): ConceptOption[] | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const concepts = (payload as { concepts?: unknown }).concepts;
  if (!Array.isArray(concepts)) return null;
  const out = concepts.filter(
    (c): c is ConceptOption =>
      !!c && typeof c === "object" && typeof (c as ConceptOption).code === "string",
  );
  return out.length > 0 ? out : null;
}

export const FALLBACK_URGENCIES: ConceptOption[] = [
  { code: "emergent", display: "Emergent — proceed as soon as possible", rank: 1 },
  { code: "urgent", display: "Urgent — within days", rank: 2 },
  { code: "elective", display: "Elective — scheduled routinely", rank: 3 },
];

// ---------------------------------------------------------------------
// Preference cards + pick lists + implant lots
// ---------------------------------------------------------------------

export type PickItemKind = "supply" | "implant" | "instrument-tray";

export interface PreferenceCardItem {
  kind: PickItemKind;
  code: string;
  display?: string | null;
  qty: number;
}

export interface PreferenceCard {
  card_id: string;
  surgeon: string;
  procedure_code: string;
  items: PreferenceCardItem[];
  created_by?: string;
  created_at?: string | null;
}

export interface PickAllocation {
  lot_number: string;
  serial?: string | null;
  expiry_date: string;
  qty: number;
}

export interface PickListItem extends PreferenceCardItem {
  allocations: PickAllocation[];
}

export interface CasePick {
  pick_id: string;
  case_id: string;
  card_id: string;
  surgeon: string;
  procedure_code: string;
  items: PickListItem[];
  created_by?: string;
  created_at?: string | null;
}

export interface ImplantLot {
  item_code: string;
  display?: string | null;
  lot_number: string;
  serial?: string | null;
  expiry_date: string;
  quantity: number;
  days_to_expiry?: number | null;
  expired?: boolean;
}

export function listPreferenceCards(
  client: ApiClient,
): Promise<{ items: PreferenceCard[]; total: number }> {
  return client.request("/surgery/preference-cards");
}

export function createPreferenceCard(
  client: ApiClient,
  input: { surgeon: string; procedureCode: string; items: PreferenceCardItem[] },
): Promise<PreferenceCard> {
  return client.request("/surgery/preference-cards", {
    method: "POST",
    body: JSON.stringify({
      surgeon: input.surgeon,
      procedure_code: input.procedureCode,
      items: input.items,
    }),
  });
}

export function updatePreferenceCard(
  client: ApiClient,
  cardId: string,
  items: PreferenceCardItem[],
): Promise<PreferenceCard> {
  return client.request(`/surgery/preference-cards/${encodeURIComponent(cardId)}`, {
    method: "PUT",
    body: JSON.stringify({ items }),
  });
}

export function deletePreferenceCard(
  client: ApiClient,
  cardId: string,
): Promise<{ deleted: boolean; card: PreferenceCard }> {
  return client.request(`/surgery/preference-cards/${encodeURIComponent(cardId)}`, {
    method: "DELETE",
  });
}

/** POST /surgery/cases/{id}/pick-list — FEFO implant allocation;
 *  oversell is refused 409 before any lot moves. */
export function generatePickList(client: ApiClient, caseId: string): Promise<CasePick> {
  return client.request(`/surgery/cases/${encodeURIComponent(caseId)}/pick-list`, {
    method: "POST",
  });
}

export function listCasePicks(
  client: ApiClient,
  caseId: string,
): Promise<{ items: CasePick[]; total: number }> {
  return client.request(`/surgery/cases/${encodeURIComponent(caseId)}/picks`);
}

export function listImplantLots(
  client: ApiClient,
): Promise<{ items: ImplantLot[]; total: number }> {
  return client.request("/surgery/implants/lots");
}

// ---------------------------------------------------------------------
// Sterile processing (trays + cycles)
// ---------------------------------------------------------------------

export interface TrayInstance {
  tray_id: string;
  tray_type: string;
  status: string;
  sterilised_at?: string | null;
  expires_at?: string | null;
  last_cycle_id?: string | null;
  assigned_case_id?: string | null;
}

export interface TraysResponse {
  items: TrayInstance[];
  total: number;
  states: string[];
  shelf_life_days: number;
}

export interface SterileCycleType {
  code: string;
  display?: string;
  temperatureC?: number;
  minMinutes?: number;
}

export interface SterileConfig {
  states: string[];
  shelfLifeDays: number;
  cycleTypes: SterileCycleType[];
}

export const FALLBACK_STERILE_CONFIG: SterileConfig = {
  states: ["in-use", "decontamination", "assembly", "sterilising", "sterile", "issued"],
  shelfLifeDays: 30,
  cycleTypes: [
    { code: "steam-134", display: "Steam 134C pre-vacuum", temperatureC: 134, minMinutes: 18 },
    { code: "steam-121", display: "Steam 121C gravity", temperatureC: 121, minMinutes: 30 },
  ],
};

export function sterileConfigFromPayload(payload: unknown): SterileConfig | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const p = payload as {
    states?: unknown;
    shelfLifeDays?: unknown;
    cycleTypes?: unknown;
  };
  const states = Array.isArray(p.states)
    ? p.states.filter((s): s is string => typeof s === "string" && s.length > 0)
    : [];
  const cycleTypes = Array.isArray(p.cycleTypes)
    ? p.cycleTypes.filter(
        (c): c is SterileCycleType =>
          !!c && typeof c === "object" && typeof (c as SterileCycleType).code === "string",
      )
    : [];
  if (states.length === 0 && cycleTypes.length === 0) return null;
  return {
    states: states.length > 0 ? states : FALLBACK_STERILE_CONFIG.states,
    shelfLifeDays:
      typeof p.shelfLifeDays === "number" ? p.shelfLifeDays : FALLBACK_STERILE_CONFIG.shelfLifeDays,
    cycleTypes: cycleTypes.length > 0 ? cycleTypes : FALLBACK_STERILE_CONFIG.cycleTypes,
  };
}

export const FALLBACK_TRAY_TYPES: ConceptOption[] = [
  { code: "major-orthopaedic", display: "Major orthopaedic tray" },
  { code: "minor-general", display: "Minor general tray" },
  { code: "laparoscopic", display: "Laparoscopic tray" },
  { code: "arthroscopy", display: "Arthroscopy tray" },
];

export function listTrays(client: ApiClient): Promise<TraysResponse> {
  return client.request("/surgery/trays");
}

export function createTray(
  client: ApiClient,
  input: { trayType: string; trayId?: string; status?: string },
): Promise<TrayInstance> {
  return client.request("/surgery/trays", {
    method: "POST",
    body: JSON.stringify({
      tray_type: input.trayType,
      ...(input.trayId ? { tray_id: input.trayId } : {}),
      ...(input.status ? { status: input.status } : {}),
    }),
  });
}

export function transitionTray(
  client: ApiClient,
  trayId: string,
  toStatus: string,
): Promise<TrayInstance> {
  return client.request(`/surgery/trays/${encodeURIComponent(trayId)}/transition`, {
    method: "POST",
    body: JSON.stringify({ to_status: toStatus }),
  });
}

/** POST /surgery/trays/{id}/cycles — a passing cycle is the ONLY way
 *  a tray becomes sterile; it stamps the shelf-life expiry. */
export function runSterilisationCycle(
  client: ApiClient,
  trayId: string,
  input: { cycleType: string; temperatureC: number; durationMinutes: number; indicatorResult: "pass" | "fail" },
): Promise<{ cycle: Record<string, unknown>; tray: TrayInstance }> {
  return client.request(`/surgery/trays/${encodeURIComponent(trayId)}/cycles`, {
    method: "POST",
    body: JSON.stringify({
      cycle_type: input.cycleType,
      temperature_c: input.temperatureC,
      duration_minutes: input.durationMinutes,
      indicator_result: input.indicatorResult,
    }),
  });
}

/** POST /surgery/cases/{id}/trays — refused 409 unless the tray is
 *  sterile and within its shelf life. */
export function assignTrayToCase(
  client: ApiClient,
  caseId: string,
  trayId: string,
): Promise<{ tray: TrayInstance; case_id: string }> {
  return client.request(`/surgery/cases/${encodeURIComponent(caseId)}/trays`, {
    method: "POST",
    body: JSON.stringify({ tray_id: trayId }),
  });
}

// ---------------------------------------------------------------------
// Intra-operative governed forms
// ---------------------------------------------------------------------

export const INTRAOP_FORMS = [
  { key: "intraop-counts", label: "Intra-op counts" },
  { key: "intraop-positioning", label: "Intra-op positioning" },
  { key: "intraop-specimens", label: "Intra-op specimens" },
] as const;

export type IntraopFormKey = (typeof INTRAOP_FORMS)[number]["key"];

export function submitIntraopForm(
  client: ApiClient,
  caseId: string,
  formKey: IntraopFormKey,
  answers: QuestionnaireAnswerItem[],
): Promise<{ case_id: string; form_key: string; response: unknown; intraop: Record<string, unknown> }> {
  return client.request(
    `/surgery/cases/${encodeURIComponent(caseId)}/intraop/${encodeURIComponent(formKey)}`,
    { method: "POST", body: JSON.stringify({ answers }) },
  );
}

// ---------------------------------------------------------------------
// PACU scoring (terminology/pacu-scoring)
// ---------------------------------------------------------------------

export interface PacuCriterion {
  code: string;
  display?: string;
  max: number;
}

export interface PacuConfig {
  criteria: PacuCriterion[];
  dischargeThreshold: number;
  mustPass: Array<{ code: string; minimum: number }>;
}

export const FALLBACK_PACU_CONFIG: PacuConfig = {
  criteria: [
    { code: "activity", display: "Activity — moves extremities", max: 2 },
    { code: "respiration", display: "Respiration", max: 2 },
    { code: "circulation", display: "Circulation — BP vs pre-anaesthetic", max: 2 },
    { code: "consciousness", display: "Consciousness", max: 2 },
    { code: "oxygen-saturation", display: "Oxygen saturation", max: 2 },
  ],
  dischargeThreshold: 9,
  mustPass: [
    { code: "respiration", minimum: 1 },
    { code: "oxygen-saturation", minimum: 1 },
  ],
};

/** PACU config from a published terminology/pacu-scoring payload
 *  ({concepts:[{code,display,max}], dischargeThreshold, mustPass}). */
export function pacuConfigFromPayload(payload: unknown): PacuConfig | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const p = payload as {
    concepts?: unknown;
    dischargeThreshold?: unknown;
    mustPass?: unknown;
  };
  if (!Array.isArray(p.concepts)) return null;
  const criteria: PacuCriterion[] = [];
  for (const c of p.concepts) {
    if (!c || typeof c !== "object") continue;
    const concept = c as { code?: unknown; display?: unknown; max?: unknown; active?: unknown };
    if (typeof concept.code !== "string" || concept.active === false) continue;
    criteria.push({
      code: concept.code,
      display: typeof concept.display === "string" ? concept.display : concept.code,
      max: typeof concept.max === "number" ? concept.max : 2,
    });
  }
  if (criteria.length === 0) return null;
  const mustPass: Array<{ code: string; minimum: number }> = [];
  if (Array.isArray(p.mustPass)) {
    for (const r of p.mustPass) {
      if (!r || typeof r !== "object") continue;
      const rule = r as { code?: unknown; minimum?: unknown };
      if (typeof rule.code === "string") {
        mustPass.push({
          code: rule.code,
          minimum: typeof rule.minimum === "number" ? rule.minimum : 1,
        });
      }
    }
  }
  return {
    criteria,
    dischargeThreshold:
      typeof p.dischargeThreshold === "number"
        ? p.dischargeThreshold
        : FALLBACK_PACU_CONFIG.dischargeThreshold,
    mustPass,
  };
}

export interface PacuScore {
  score_id: string;
  case_id: string;
  scores: Record<string, number>;
  total: number;
  threshold: number;
  must_pass_failures: Array<{ code: string; minimum: number; scored: number }>;
  passes: boolean;
  discharge_ready?: boolean;
  recorded_by?: string;
  recorded_at?: string | null;
}

/** POST /surgery/cases/{id}/pacu-score — total + verdict computed
 *  server-side from the published config, never by the caller. */
export function recordPacuScore(
  client: ApiClient,
  caseId: string,
  scores: Record<string, number>,
): Promise<PacuScore> {
  return client.request(`/surgery/cases/${encodeURIComponent(caseId)}/pacu-score`, {
    method: "POST",
    body: JSON.stringify({ scores }),
  });
}

export function listPacuScores(
  client: ApiClient,
  caseId: string,
): Promise<{ items: PacuScore[]; total: number; latest: PacuScore | null }> {
  return client.request(`/surgery/cases/${encodeURIComponent(caseId)}/pacu-scores`);
}

// ---------------------------------------------------------------------
// Outcomes + SSI surveillance
// ---------------------------------------------------------------------

export interface SurgeryOutcome {
  outcome_id: string;
  case_id: string;
  patient_ref?: string | null;
  status: string;
  complications: string[];
  ssi: boolean;
  ssi_onset_date?: string | null;
  organism?: string | null;
  recorded_by?: string;
  recorded_at?: string | null;
  procedure_code?: string | null;
  procedure_display?: string | null;
}

export interface OutcomeConfig {
  statuses: string[];
  complications: ConceptOption[];
}

export const FALLBACK_OUTCOME_CONFIG: OutcomeConfig = {
  statuses: ["recovered", "recovering", "complication", "readmitted", "deceased"],
  complications: [
    { code: "haemorrhage", display: "Post-operative haemorrhage" },
    { code: "wound-dehiscence", display: "Wound dehiscence" },
    { code: "wound-infection", display: "Wound infection" },
    { code: "deep-vein-thrombosis", display: "Deep vein thrombosis" },
    { code: "pulmonary-embolism", display: "Pulmonary embolism" },
    { code: "anastomotic-leak", display: "Anastomotic leak" },
    { code: "sepsis", display: "Sepsis" },
    { code: "myocardial-infarction", display: "Myocardial infarction" },
    { code: "acute-kidney-injury", display: "Acute kidney injury" },
    { code: "other", display: "Other complication" },
  ],
};

/** Outcome vocabulary from terminology/surgery-complications
 *  ({statuses:[...], concepts:[...]}). */
export function outcomeConfigFromPayload(payload: unknown): OutcomeConfig | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const p = payload as { statuses?: unknown; concepts?: unknown };
  const statuses = Array.isArray(p.statuses)
    ? p.statuses.filter((s): s is string => typeof s === "string")
    : [];
  const complications = conceptsFromPayload(payload) ?? [];
  if (statuses.length === 0 && complications.length === 0) return null;
  return {
    statuses: statuses.length > 0 ? statuses : FALLBACK_OUTCOME_CONFIG.statuses,
    complications:
      complications.length > 0 ? complications : FALLBACK_OUTCOME_CONFIG.complications,
  };
}

export function recordOutcome(
  client: ApiClient,
  caseId: string,
  input: {
    status: string;
    complications: string[];
    ssi: boolean;
    ssiOnsetDate?: string;
    organism?: string;
  },
): Promise<SurgeryOutcome> {
  return client.request(`/surgery/cases/${encodeURIComponent(caseId)}/outcome`, {
    method: "POST",
    body: JSON.stringify({
      status: input.status,
      complications: input.complications,
      ssi: input.ssi,
      ...(input.ssi && input.ssiOnsetDate ? { ssi_onset_date: input.ssiOnsetDate } : {}),
      ...(input.ssi && input.organism ? { organism: input.organism } : {}),
    }),
  });
}

export interface SsiFeed {
  feed: string;
  purpose: string;
  items: SurgeryOutcome[];
  total: number;
}

/** GET /surgery/outcomes/ssi — infection-prevention SURVEILLANCE
 *  feed (labelled as such by the server payload). */
export function getSsiFeed(client: ApiClient): Promise<SsiFeed> {
  return client.request("/surgery/outcomes/ssi");
}
