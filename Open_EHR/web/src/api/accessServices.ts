// Phase 15 access services client (BD-21 utilisation review,
// BD-22 online bill pay, BD-23 portal proxy access, BD-28 telehealth
// at scale). Source of truth: backend/app/access_services.py. Types
// live here, not in api/types.ts, per the Phase 15 web brief. Every
// function takes the ApiClient as its first argument and calls
// client.request(...).

import type { ApiClient } from "./client";
import type {
  FhirResource,
  FormDefinition,
  QuestionnaireAnswerItem,
} from "./types";

export interface ListResponse<T> {
  items: T[];
  total: number;
}

/** Shown wherever UR criteria are displayed. The published
 *  terminology/ur-criteria concepts are SITE-FIXTURE examples only —
 *  licensed criteria (InterQual / MCG class) are never shipped. */
export const UR_CRITERIA_FIXTURE_LABEL =
  "Site-fixture example criteria — sites publish their licensed criteria";

/** Shown wherever a portal payment is offered. The published
 *  integration/payment-provider config is { mode: "test",
 *  live: false }; no live card processor is integrated. */
export const PAYMENT_TEST_MODE_LABEL =
  "Test mode — no live payment processor is integrated";

// ---------------------------------------------------------------------------
// BD-21 — Utilisation review (/ur)
// ---------------------------------------------------------------------------

export interface URHistoryEntry {
  from: string | null;
  to: string;
  by: string;
  at: string;
  criteria_code?: string | null;
  evidence_checked?: string[];
  advisor?: boolean;
}

export interface URCase {
  id: string;
  case_id: string;
  patient: string;
  patient_ref: string;
  encounter_ref: string | null;
  encounter_id: string | null;
  claim_ref: string | null;
  claim_id: string | null;
  status: string;
  criteria_code: string | null;
  criteria_set: string | null;
  evidence_checked: string[];
  first_reviewer: string | null;
  advisor_reviewer: string | null;
  advisor_decision: string | null;
  advisor_review_required: boolean;
  history: URHistoryEntry[];
  note: string | null;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface URCriteriaConcept {
  code: string;
  display: string;
  active?: boolean;
  description?: string;
  requiredEvidence?: string[];
  fixture?: boolean;
}

export interface URCriteriaConfig {
  name?: string;
  system?: string;
  notice?: string;
  concepts?: URCriteriaConcept[];
}

/** GET /ur/denial-risk — the labelled JOIN FEED for the revenue
 *  denials workqueue (GET /billing/denials). It is a feed into that
 *  queue, never a second queue: workqueue state lives in revenue. */
export interface DenialRiskFeed extends ListResponse<URCase> {
  join_keys: string[];
  joins_with: string;
  note: string;
}

export function listUrCases(
  client: ApiClient,
  status?: string,
): Promise<ListResponse<URCase>> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  return client.request(`/ur/cases${qs}`);
}

export function getUrCase(client: ApiClient, caseId: string): Promise<URCase> {
  return client.request(`/ur/cases/${encodeURIComponent(caseId)}`);
}

export function createUrCase(
  client: ApiClient,
  input: {
    patient: string;
    encounter_id?: string;
    claim_id?: string;
    criteria_code?: string;
    note?: string;
  },
): Promise<URCase> {
  return client.request("/ur/cases", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function reviewUrCase(
  client: ApiClient,
  caseId: string,
  input: {
    to_status: string;
    criteria_code?: string;
    evidence_checked?: string[];
    note?: string;
  },
): Promise<URCase> {
  return client.request(`/ur/cases/${encodeURIComponent(caseId)}/review`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** Physician-advisor second review. The server requires the case to
 *  be in denial-risk and the advisor to differ from the first
 *  reviewer — the same person gets a 409, surfaced verbatim. */
export function advisorReviewUrCase(
  client: ApiClient,
  caseId: string,
  input: {
    decision: string;
    criteria_code?: string;
    evidence_checked?: string[];
  },
): Promise<URCase> {
  return client.request(
    `/ur/cases/${encodeURIComponent(caseId)}/advisor-review`,
    { method: "POST", body: JSON.stringify(input) },
  );
}

export function urDenialRisk(client: ApiClient): Promise<DenialRiskFeed> {
  return client.request("/ur/denial-risk");
}

/** The published terminology/ur-criteria config (site-fixture
 *  example concepts), or null when it cannot be fetched. */
export async function urCriteria(
  client: ApiClient,
): Promise<URCriteriaConfig | null> {
  if (typeof client.effectiveConfig !== "function") return null;
  try {
    const cfg = await client.effectiveConfig("terminology", "ur-criteria");
    return (cfg?.payload ?? null) as URCriteriaConfig | null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// BD-22 — Online bill pay (/portal/billing, PATIENT token's own data only)
// ---------------------------------------------------------------------------

export interface PortalBillingClaim {
  claim_id: string;
  status?: string | null;
  total?: number;
  paid?: number;
  patient_responsibility?: number;
  patient_paid?: number;
  patient_due?: number;
  [key: string]: unknown;
}

export interface PortalBillingBalance {
  patient: string;
  balance: number;
  patient_due: number;
  patient_paid: number;
  patient_responsibility: number;
  totals: {
    total: number;
    paid: number;
    adjustments: number;
    patient_responsibility: number;
    patient_paid: number;
    patient_due: number;
    balance: number;
    [key: string]: number;
  };
  claims: PortalBillingClaim[];
  provider_mode: string;
  disclaimer: string;
  revenue_disclaimer?: string | null;
}

export interface PortalPaymentReceipt {
  receipt_id: string;
  payment_id: string;
  claim_id: string;
  patient: string;
  amount: number;
  method: string;
  /** The governed provider identity: "test-mode". */
  provider: string;
  provider_mode: string;
  mode: string;
  /** Always false — no live processor is integrated. */
  live: boolean;
  patient_due_after: number;
  balance_after: number;
  recorded_by: string;
  disclaimer: string;
}

export function portalBillingBalance(
  client: ApiClient,
): Promise<PortalBillingBalance> {
  return client.request("/portal/billing/balance");
}

export function portalBillingPay(
  client: ApiClient,
  input: { amount: number; claim_id?: string; method?: string },
): Promise<PortalPaymentReceipt> {
  return client.request("/portal/billing/payments", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

// ---------------------------------------------------------------------------
// BD-23 — Portal proxy access (/portal/proxy, /proxy)
// ---------------------------------------------------------------------------

/** Scopes come from the published terminology/proxy-scopes config:
 *  schedule | messages | results. */
export const PROXY_SCOPES = ["schedule", "messages", "results"] as const;

export interface ProxyGrant {
  id: string;
  grant_id: string;
  patient: string;
  patient_ref: string;
  proxy_username: string;
  scopes: string[];
  granted_by: string;
  grant_source: string;
  status: string;
  expired: boolean;
  effective_status: string;
  expires_at: string | null;
  created_at: string | null;
}

export interface ProxyMessageThread {
  thread_id: string;
  patient?: string | null;
  topic?: string | null;
  message_count?: number;
  unread_count?: number;
  last_message_at?: string | null;
  last_text?: string | null;
  last_sender_role?: string | null;
}

export interface ProxyScopedResponse {
  total: number;
  patient: string;
  on_behalf_of: string;
  proxy: string;
}

export interface ProxyAppointmentsResponse extends ProxyScopedResponse {
  items: FhirResource[];
}

export interface ProxyMessagesResponse extends ProxyScopedResponse {
  items: ProxyMessageThread[];
}

/** The FHIR results Bundle for the patient, plus the proxy audit
 *  fields (every proxy read is audited server-side on-behalf-of). */
export interface ProxyResultsResponse {
  resourceType?: string;
  type?: string;
  total?: number;
  entry?: Array<{ resource: FhirResource }>;
  patient: string;
  on_behalf_of: string;
  proxy: string;
}

/** Grants the calling patient has given (their own record only). */
export function listMyProxyGrants(
  client: ApiClient,
): Promise<ListResponse<ProxyGrant>> {
  return client.request("/portal/proxy/grants");
}

/** The patient themself creates a grant (POST /portal/proxy/grants). */
export function createProxyGrant(
  client: ApiClient,
  input: { proxy_username: string; scopes: string[]; expires_at?: string },
): Promise<ProxyGrant> {
  return client.request("/portal/proxy/grants", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function revokeProxyGrant(
  client: ApiClient,
  grantId: string,
): Promise<ProxyGrant> {
  return client.request(
    `/portal/proxy/grants/${encodeURIComponent(grantId)}/revoke`,
    { method: "POST" },
  );
}

/** Patients for whom the caller is a proxy (active grants only). */
export function listProxyPatients(
  client: ApiClient,
): Promise<ListResponse<ProxyGrant> & { proxy: string }> {
  return client.request("/portal/proxy/patients");
}

export function proxyAppointments(
  client: ApiClient,
  patientRef: string,
): Promise<ProxyAppointmentsResponse> {
  return client.request(
    `/portal/proxy/patients/${encodeURIComponent(patientRef)}/appointments`,
  );
}

export function proxyMessages(
  client: ApiClient,
  patientRef: string,
): Promise<ProxyMessagesResponse> {
  return client.request(
    `/portal/proxy/patients/${encodeURIComponent(patientRef)}/messages`,
  );
}

export function proxyResults(
  client: ApiClient,
  patientRef: string,
): Promise<ProxyResultsResponse> {
  return client.request(
    `/portal/proxy/patients/${encodeURIComponent(patientRef)}/results`,
  );
}

// ---------------------------------------------------------------------------
// BD-28 — Telehealth at scale (/telehealth, /portal/telehealth)
// ---------------------------------------------------------------------------

export interface TelehealthServiceResolved {
  service_line: string | null;
  source: string;
  provider_name: string | null;
  join_url_template: string | null;
  waiting_room_message: string | null;
  intake_form: string | null;
  intake_form_key: string | null;
  disclaimer: string | null;
  service_entry: Record<string, unknown> | null;
}

export interface TelehealthServicesResponse {
  services: TelehealthServiceResolved[];
  total: number;
  base: TelehealthServiceResolved;
}

export interface TelehealthVisit {
  id: string;
  visit_id: string;
  appointment_id: string;
  appointment_ref: string;
  patient: string | null;
  patient_ref: string | null;
  service_line: string;
  status: string;
  state: string;
  interpreter_request_id: string | null;
  interpreter_display: string | null;
  timestamps: Record<string, string>;
  state_history: Array<Record<string, unknown>>;
  outcome_response_id: string | null;
  outcome_submitted: boolean;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
}

/** Staff/portal workspace for one Phase 15 virtual visit: the visit
 *  plus its per-service resolved config (join URL rendered from the
 *  published template, waiting-room message, intake form). */
export interface TelehealthVisitWorkspace {
  visit: TelehealthVisit;
  appointment_id: string;
  patient: string | null;
  service_line: string;
  resolved_config: TelehealthServiceResolved;
  provider_name: string | null;
  join_url: string | null;
  waiting_room_message: string | null;
  intake_form: string | null;
  intake_form_definition: FormDefinition | null;
  disclaimer: string | null;
}

export interface TelehealthOutcomeResult {
  visit: TelehealthVisit;
  outcome: FhirResource;
  form: string;
}

export function telehealthServices(
  client: ApiClient,
): Promise<TelehealthServicesResponse> {
  return client.request("/telehealth/services");
}

export function listTelehealthVisits(
  client: ApiClient,
  filters: { status?: string; service_line?: string } = {},
): Promise<ListResponse<TelehealthVisit>> {
  const qs = new URLSearchParams();
  if (filters.status) qs.set("status", filters.status);
  if (filters.service_line) qs.set("service_line", filters.service_line);
  const query = qs.toString();
  return client.request(`/telehealth/visits${query ? `?${query}` : ""}`);
}

export function createTelehealthVisit(
  client: ApiClient,
  input: {
    appointment_id: string;
    service_line?: string;
    interpreter_request_id?: string;
    interpreter_display?: string;
  },
): Promise<TelehealthVisit> {
  return client.request("/telehealth/visits", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function telehealthVisitWorkspace(
  client: ApiClient,
  visitId: string,
): Promise<TelehealthVisitWorkspace> {
  return client.request(
    `/telehealth/visits/${encodeURIComponent(visitId)}/workspace`,
  );
}

export function transitionTelehealthVisit(
  client: ApiClient,
  visitId: string,
  toStatus: string,
): Promise<TelehealthVisit> {
  return client.request(
    `/telehealth/visits/${encodeURIComponent(visitId)}/transition`,
    { method: "POST", body: JSON.stringify({ to_status: toStatus }) },
  );
}

/** GET /portal/telehealth/visits/{id} — the patient's own visit
 *  workspace (another patient's visit is a 404 server-side). */
export function portalTelehealthVisit(
  client: ApiClient,
  visitId: string,
): Promise<TelehealthVisitWorkspace> {
  return client.request(
    `/portal/telehealth/visits/${encodeURIComponent(visitId)}`,
  );
}

/** GET /portal/telehealth/visits — the patient's OWN virtual visits
 *  (Phase 15 closure: the list the portal outcome action keys on;
 *  ownership is the visit row's patient_ref, server-enforced). */
export function listMyTelehealthVisits(
  client: ApiClient,
): Promise<ListResponse<TelehealthVisit>> {
  return client.request("/portal/telehealth/visits");
}

/** The governed outcome form (form/telehealth-outcome), or null when
 *  it cannot be fetched. */
export async function telehealthOutcomeForm(
  client: ApiClient,
): Promise<FormDefinition | null> {
  if (typeof client.effectiveConfig !== "function") return null;
  try {
    const cfg = await client.effectiveConfig("form", "telehealth-outcome");
    return (cfg?.payload ?? null) as FormDefinition | null;
  } catch {
    return null;
  }
}

/** POST /portal/telehealth/visits/{id}/outcome — the patient submits
 *  the governed outcome form for their own COMPLETED visit. */
export function submitTelehealthOutcome(
  client: ApiClient,
  visitId: string,
  answers: QuestionnaireAnswerItem[],
): Promise<TelehealthOutcomeResult> {
  return client.request(
    `/portal/telehealth/visits/${encodeURIComponent(visitId)}/outcome`,
    { method: "POST", body: JSON.stringify({ answers }) },
  );
}
