// Phase 15 care-flow client functions. The sources of truth are the
// backend modules: app.support_ops (/support — command desks, staffing,
// huddles, ICU triage decisions, access-line calls), app.care_coordination
// (/coord — eConsults, multispecialty episodes, coordinator assignments
// and panels) and app.infection_prevention (/infection — HAI surveillance,
// SSI feed, rates, isolation precautions, stewardship referrals).
// Payload types live here (not in the shared types.ts) per the Phase 15
// web-wave brief; every function takes the ApiClient and goes through
// its generic request() so auth/error handling stays centralised.

import type { ApiClient } from "./client";

// ---------------------------------------------------------------------------
// /support — command desks + access line (BD-13 / BD-14)
// ---------------------------------------------------------------------------

export interface CommandDesk {
  code: string;
  display: string;
  roles?: string[];
  huddleCadence?: string;
}

export interface CommandDesksConfig {
  name?: string;
  desks?: CommandDesk[];
  triageAuthorityRole?: string;
  triageAuthorityDisplay?: string;
  triageDecisions?: string[];
  [key: string]: unknown;
}

export interface AccessLineConfig {
  name?: string;
  referralSources?: Array<{ code: string; display: string }>;
  sources?: string[];
  dispositions?: string[];
  [key: string]: unknown;
}

export interface SupportConfig {
  "command-desks"?: CommandDesksConfig;
  "access-line"?: AccessLineConfig;
  [key: string]: unknown;
}

export interface DesksResult {
  items: CommandDesk[];
  total: number;
  triage_authority_role?: string | null;
}

export interface StaffingItem {
  id: string;
  staffing_id: string;
  desk: string;
  staff_id: string;
  staff_username?: string | null;
  staff_name: string;
  starts_at?: string | null;
  ends_at?: string | null;
}

export interface HuddleItem {
  id: string;
  huddle_id: string;
  desk?: string | null;
  census: number;
  boarding: number;
  expected_discharges: number;
  decisions: unknown[];
  chaired_by: string;
  note?: string | null;
  held_at?: string | null;
  artefact?: string;
}

export interface TriageDecisionItem {
  id: string;
  decision_id: string;
  patient_ref: string;
  decision: string;
  decided_by: string;
  authority_role: string;
  authority_basis?: string;
  reason?: string | null;
  created_at?: string | null;
}

export interface AccessCallItem {
  id: string;
  call_id: string;
  caller_name: string;
  caller_org?: string | null;
  referral_source: string;
  patient_ref?: string | null;
  patient_name?: string | null;
  patient_summary: string;
  disposition: string;
  transfer_request_id?: string | null;
  transfer_linkage?: { note?: string; transfer_centre_endpoint?: string } | null;
  call_at?: string | null;
}

export function supportConfig(client: ApiClient): Promise<SupportConfig> {
  return client.request<SupportConfig>("/support/config");
}

export function listDesks(client: ApiClient): Promise<DesksResult> {
  return client.request<DesksResult>("/support/command/desks");
}

export function listStaffing(
  client: ApiClient,
  desk?: string,
): Promise<{ items: StaffingItem[]; total: number }> {
  const qs = desk ? `?desk=${encodeURIComponent(desk)}` : "";
  return client.request(`/support/command/staffing${qs}`);
}

export function createStaffing(
  client: ApiClient,
  input: { desk: string; staff: string; starts_at: string; ends_at: string },
): Promise<StaffingItem> {
  return client.request("/support/command/staffing", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function listHuddles(
  client: ApiClient,
): Promise<{ items: HuddleItem[]; total: number }> {
  return client.request("/support/command/huddles");
}

export function createHuddle(
  client: ApiClient,
  input: {
    desk?: string;
    census: number;
    boarding: number;
    expected_discharges: number;
    decisions: string[];
    chaired_by: string;
    note?: string;
  },
): Promise<HuddleItem> {
  return client.request("/support/command/huddles", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function listTriageDecisions(
  client: ApiClient,
): Promise<{ items: TriageDecisionItem[]; total: number }> {
  return client.request("/support/command/triage-decisions");
}

export function createTriageDecision(
  client: ApiClient,
  input: { patient_ref: string; decision: string; reason?: string },
): Promise<TriageDecisionItem> {
  return client.request("/support/command/triage-decisions", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function listAccessCalls(
  client: ApiClient,
): Promise<{ items: AccessCallItem[]; total: number }> {
  return client.request("/support/access-line/calls");
}

export function createAccessCall(
  client: ApiClient,
  input: {
    caller_name: string;
    caller_org?: string;
    referral_source: string;
    patient_name?: string;
    patient_ref?: string;
    patient_summary: string;
    disposition: string;
  },
): Promise<AccessCallItem> {
  return client.request("/support/access-line/calls", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

// ---------------------------------------------------------------------------
// /coord — eConsults, episodes, coordinator (BD-27 / BD-25 / BD-24)
// ---------------------------------------------------------------------------

export interface EConsultItem {
  id: string;
  econsult_id: string;
  patient_ref: string;
  requester: string;
  specialty: string;
  question: string;
  chart_refs?: string[];
  state: string;
  sla_hours?: number | null;
  sla_deadline?: string | null;
  sla_breached?: boolean;
  answer_text?: string | null;
  recommendations?: string | null;
  needs_visit?: boolean | null;
  referral_id?: string | null;
  referral_ref?: string | null;
  answered_by?: string | null;
  answered_at?: string | null;
  cancel_reason?: string | null;
  trigger_id?: string | null;
  triggering_ref?: string | null;
  created_at?: string | null;
}

export interface EConsultScanResult {
  created: EConsultItem[];
  created_count: number;
  evaluated_patients: number;
  triggers: Array<string | null>;
}

/** Fallback specialty list mirroring the backend's seeded published
 *  terminology/econsult-specialties defaults (the /coord surface
 *  exposes no config read endpoint; the server validates against the
 *  published config on every create). */
export const ECONSULT_SPECIALTY_FALLBACK: Array<{ code: string; display: string }> = [
  { code: "endocrinology", display: "Endocrinology" },
  { code: "cardiology", display: "Cardiology" },
  { code: "dermatology", display: "Dermatology" },
  { code: "neurology", display: "Neurology" },
  { code: "psychiatry", display: "Psychiatry" },
  { code: "gastroenterology", display: "Gastroenterology" },
  { code: "diabetes", display: "Diabetes (endocrinology)" },
];

export function listEConsults(
  client: ApiClient,
): Promise<{ items: EConsultItem[]; total: number }> {
  return client.request("/coord/econsults");
}

export function createEConsult(
  client: ApiClient,
  input: { patient: string; specialty: string; question: string },
): Promise<EConsultItem> {
  return client.request("/coord/econsults", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function answerEConsult(
  client: ApiClient,
  econsultId: string,
  input: { advice: string; needs_visit: boolean; recommendations?: string },
): Promise<EConsultItem> {
  return client.request(`/coord/econsults/${encodeURIComponent(econsultId)}/answer`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function closeEConsult(
  client: ApiClient,
  econsultId: string,
): Promise<EConsultItem> {
  return client.request(`/coord/econsults/${encodeURIComponent(econsultId)}/close`, {
    method: "POST",
  });
}

export function scanEConsultTriggers(client: ApiClient): Promise<EConsultScanResult> {
  return client.request("/coord/econsults/scan", { method: "POST" });
}

export interface EpisodeItem {
  id: string;
  episode_id: string;
  patient_ref: string;
  coordinator_username?: string | null;
  coordinator_staff_id?: string | null;
  title: string;
  condition?: string | null;
  status: string;
  links?: Array<{ ref?: string; reference?: string; type?: string }>;
  created_at?: string | null;
}

export interface EpisodeContributionItem {
  id: string;
  contribution_id: string;
  episode_id: string;
  specialty: string;
  author: string;
  summary: string;
  linked_refs?: string[];
  created_at?: string | null;
}

export interface EpisodeAssembly {
  episode: EpisodeItem;
  coordinator?: {
    username?: string | null;
    staff_id?: string | null;
    name?: string;
    role_label?: string | null;
  } | null;
  contributions: EpisodeContributionItem[];
  contributions_by_specialty: Record<string, EpisodeContributionItem[]>;
  links: Array<{ ref?: string; reference?: string; type?: string }>;
}

export function listEpisodes(
  client: ApiClient,
): Promise<{ items: EpisodeItem[]; total: number }> {
  return client.request("/coord/episodes");
}

export function createEpisode(
  client: ApiClient,
  input: { patient: string; title: string },
): Promise<EpisodeItem> {
  return client.request("/coord/episodes", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function episodeAssembly(
  client: ApiClient,
  episodeId: string,
): Promise<EpisodeAssembly> {
  return client.request(`/coord/episodes/${encodeURIComponent(episodeId)}/assembly`);
}

export function addEpisodeContribution(
  client: ApiClient,
  episodeId: string,
  input: { specialty: string; summary: string },
): Promise<EpisodeContributionItem> {
  return client.request(
    `/coord/episodes/${encodeURIComponent(episodeId)}/contributions`,
    { method: "POST", body: JSON.stringify(input) },
  );
}

export interface CoordinatorAssignmentItem {
  assignment_id: string;
  patient_ref: string;
  staff_username?: string | null;
  staff_id?: string | null;
  staff_name?: string | null;
  role_label: string;
  active: boolean;
  assigned_by?: string;
  created_at?: string | null;
  superseded_at?: string | null;
}

export interface CoordinatorForPatient {
  patient_ref: string;
  coordinator: CoordinatorAssignmentItem | null;
  history: CoordinatorAssignmentItem[];
}

export interface CoordinatorPanelItem {
  patient_ref: string;
  coordinator_role?: string;
  active_episode?: EpisodeItem | null;
  active_episodes?: EpisodeItem[];
  attending?: string | null;
  attending_name?: string | null;
}

export interface CoordinatorPanel {
  staff_username: string;
  staff_id?: string | null;
  staff_name?: string | null;
  items: CoordinatorPanelItem[];
  total: number;
}

function patientIdOf(refOrId: string): string {
  return refOrId.startsWith("Patient/") ? refOrId.slice("Patient/".length) : refOrId;
}

export function coordinatorForPatient(
  client: ApiClient,
  patientRefOrId: string,
): Promise<CoordinatorForPatient> {
  return client.request(
    `/coord/patients/${encodeURIComponent(patientIdOf(patientRefOrId))}/coordinator`,
  );
}

export function assignCoordinator(
  client: ApiClient,
  patientRefOrId: string,
  input: { staff_username: string; role_label?: string },
): Promise<CoordinatorAssignmentItem> {
  return client.request(
    `/coord/patients/${encodeURIComponent(patientIdOf(patientRefOrId))}/coordinator`,
    { method: "POST", body: JSON.stringify(input) },
  );
}

export function coordinatorPanel(
  client: ApiClient,
  staffUsername: string,
): Promise<CoordinatorPanel> {
  return client.request(`/coord/panels/${encodeURIComponent(staffUsername)}`);
}

// ---------------------------------------------------------------------------
// /infection — infection prevention (HAI surveillance + stewardship)
// ---------------------------------------------------------------------------

export interface HaiConcept {
  code: string;
  display: string;
  criteria?: string;
  denominatorType?: string;
  active?: boolean;
}

export interface InfectionDefinitions {
  hai_definitions: {
    concepts?: HaiConcept[];
    stewardship?: { team?: string; display?: string; ownerRole?: string };
    [key: string]: unknown;
  };
  hai_version?: number | null;
  isolation_precautions: {
    concepts?: Array<{ code: string; display: string; description?: string }>;
    [key: string]: unknown;
  };
  isolation_version?: number | null;
}

export interface InfectionCaseItem {
  id: string;
  case_id: string;
  hai_type: string;
  type: string;
  patient_ref: string;
  onset_date: string;
  source_ref: string;
  unit?: string | null;
  status: string;
  note?: string | null;
  stewardship_task_id?: string | null;
  created_by?: string;
}

export interface SsiFeed {
  items: Array<{
    type: string;
    patient_ref?: string | null;
    onset_date?: string | null;
    source_ref: string;
    source_table?: string;
    status: string;
  }>;
  total: number;
  source: string;
  tables_checked: string[];
  note: string;
}

export interface InfectionRateItem {
  type: string;
  display?: string;
  confirmed_cases: number;
  denominator_type: string;
  device_days: number | null;
  rate_per_1000: number | null;
  reason: string | null;
}

export interface PrecautionItem {
  id: string;
  precaution_id: string;
  patient_ref: string;
  precaution_type: string;
  type: string;
  reason: string;
  unit?: string | null;
  status: string;
  started_on: string;
}

export function infectionDefinitions(client: ApiClient): Promise<InfectionDefinitions> {
  return client.request("/infection/definitions");
}

export function listInfectionCases(
  client: ApiClient,
): Promise<{ items: InfectionCaseItem[]; total: number }> {
  return client.request("/infection/cases");
}

export function createInfectionCase(
  client: ApiClient,
  input: { type: string; patient: string; onset_date: string; unit?: string },
): Promise<InfectionCaseItem> {
  return client.request("/infection/cases", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function transitionInfectionCase(
  client: ApiClient,
  caseId: string,
  input: { to_status: string; note?: string },
): Promise<InfectionCaseItem> {
  return client.request(`/infection/cases/${encodeURIComponent(caseId)}/transition`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function stewardshipReferral(
  client: ApiClient,
  caseId: string,
): Promise<{ task: { id: string }; existing: boolean; case_id: string }> {
  return client.request(
    `/infection/cases/${encodeURIComponent(caseId)}/stewardship-referral`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function ssiFeed(client: ApiClient): Promise<SsiFeed> {
  return client.request("/infection/ssi-feed");
}

export function infectionRates(
  client: ApiClient,
): Promise<{ items: InfectionRateItem[]; total_confirmed: number }> {
  return client.request("/infection/rates");
}

export function listPrecautions(
  client: ApiClient,
): Promise<{ items: PrecautionItem[]; total: number }> {
  return client.request("/infection/precautions?active_only=true");
}
