// Results Inbox client (Phase 15, scope G). The single source of
// truth is backend/app/results_inbox.py: routes under /inbox, gated
// by the fhir.inbox capability. Flags are derived server-side ONLY
// from stored data (critical-result extension / Observation
// interpretations) — the web never invents a flag. Types for these
// payloads live here, not in the shared types.ts.

import type { ApiClient } from "./client";
import type { FhirResource } from "./types";

export type InboxResultKind = "lab" | "imaging" | "pathology";
export type InboxResultFlag = "critical" | "abnormal" | null;
export type InboxScope = "mine" | "unit" | "all";

export interface InboxResultItem {
  result_ref: string;
  result_type: string;
  result_id: string;
  patient_ref: string | null;
  kind: InboxResultKind;
  title: string;
  code: string | null;
  effective_at: string;
  flag: InboxResultFlag;
  status: string | null;
  observation_count: number;
  acknowledged: boolean;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
}

export interface InboxResultsResponse {
  items: InboxResultItem[];
  total: number;
  page_size: number;
  limit: number;
  truncated: boolean;
  scope: string;
  filters: {
    kind: string | null;
    flag: string | null;
    ack: string;
    patient: string | null;
  };
}

export interface InboxResultsFilters {
  kind?: InboxResultKind;
  /** "none" selects results carrying no flag. */
  flag?: "critical" | "abnormal" | "none";
  ack?: "unacknowledged" | "acknowledged" | "all";
  patient?: string;
  scope?: InboxScope;
  limit?: number;
}

export interface InboxSummary {
  results: {
    unacknowledged: number;
    critical_unacknowledged: number;
    module: string;
    nav_hint: { module: string; label: string };
  };
  continuum: {
    open_tasks: number;
    breached: number;
    module: string;
    nav_hint: { module: string; label: string };
  };
  generated_at: string;
}

export interface InboxPatientSummary {
  patient_ref: string;
  found: boolean;
  id?: string;
  name?: string | null;
  gender?: string | null;
  birth_date?: string | null;
  patient?: FhirResource;
}

export interface InboxEncounterContext {
  encounter_ref: string;
  found: boolean;
  id?: string;
  status?: string | null;
  class_code?: string | null;
  period?: { start?: string; end?: string } | null;
  attending_staff_id?: string | null;
  encounter?: FhirResource;
}

export interface InboxTrendEntry {
  observation_id: string;
  code: string | null;
  display: string | null;
  value: unknown;
  unit: string | null;
  interpretation: string | null;
  effective_at: string;
}

export interface InboxResultDetail extends InboxResultItem {
  report: FhirResource;
  conclusion: string | null;
  observations: FhirResource[];
  patient_summary: InboxPatientSummary | null;
  encounter_context: InboxEncounterContext | null;
  trend: InboxTrendEntry[];
}

export interface InboxAckResult {
  result_ref: string;
  kind: string;
  result_id: string;
  patient_ref: string | null;
  acknowledged_by: string;
  acknowledged_at: string | null;
  note: string | null;
  already_acknowledged: boolean;
}

export interface InboxFollowUpResult {
  task: FhirResource;
  result_ref: string;
}

export interface InboxMessageResult {
  message: FhirResource;
  thread_id: string | null;
  result_ref: string;
}

export interface InboxEscalateResult {
  task: FhirResource;
  audience: string | null;
  result_ref: string;
}

export interface InboxScanResult {
  scanned_critical: number;
  escalated: Array<{
    result_ref: string;
    kind: string;
    patient_ref: string | null;
    age_hours: number;
    firings: unknown[];
  }>;
  escalated_count: number;
  sla_hours: number;
  generated_at: string;
}

function resultsPath(filters: InboxResultsFilters): string {
  const qs = new URLSearchParams();
  if (filters.kind) qs.set("kind", filters.kind);
  if (filters.flag) qs.set("flag", filters.flag);
  if (filters.ack && filters.ack !== "all") qs.set("ack", filters.ack);
  if (filters.patient) qs.set("patient", filters.patient);
  if (filters.scope && filters.scope !== "all") qs.set("scope", filters.scope);
  if (filters.limit) qs.set("limit", String(filters.limit));
  const query = qs.toString();
  return query ? `/inbox/results?${query}` : "/inbox/results";
}

function detailPath(kind: string, resultId: string, suffix = ""): string {
  return `/inbox/results/${encodeURIComponent(kind)}/${encodeURIComponent(resultId)}${suffix}`;
}

export function listInboxResults(
  client: ApiClient,
  filters: InboxResultsFilters = {},
): Promise<InboxResultsResponse> {
  return client.request<InboxResultsResponse>(resultsPath(filters));
}

export function getInboxSummary(client: ApiClient): Promise<InboxSummary> {
  return client.request<InboxSummary>("/inbox/summary");
}

export function getInboxResultDetail(
  client: ApiClient,
  kind: string,
  resultId: string,
): Promise<InboxResultDetail> {
  return client.request<InboxResultDetail>(detailPath(kind, resultId));
}

export function acknowledgeInboxResult(
  client: ApiClient,
  kind: string,
  resultId: string,
  note?: string,
): Promise<InboxAckResult> {
  return client.request<InboxAckResult>(detailPath(kind, resultId, "/acknowledge"), {
    method: "POST",
    body: JSON.stringify(note ? { note } : {}),
  });
}

export function createInboxFollowUpTask(
  client: ApiClient,
  kind: string,
  resultId: string,
  body: { description: string; owner?: string },
): Promise<InboxFollowUpResult> {
  return client.request<InboxFollowUpResult>(detailPath(kind, resultId, "/follow-up-task"), {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function messageInboxPatient(
  client: ApiClient,
  kind: string,
  resultId: string,
  body: { text: string; topic?: string },
): Promise<InboxMessageResult> {
  return client.request<InboxMessageResult>(detailPath(kind, resultId, "/message-patient"), {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function escalateInboxResult(
  client: ApiClient,
  kind: string,
  resultId: string,
  body: { reason?: string; audience?: string } = {},
): Promise<InboxEscalateResult> {
  return client.request<InboxEscalateResult>(detailPath(kind, resultId, "/escalate"), {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function scanInboxSla(client: ApiClient): Promise<InboxScanResult> {
  return client.request<InboxScanResult>("/inbox/scan", {
    method: "POST",
    body: JSON.stringify({}),
  });
}
