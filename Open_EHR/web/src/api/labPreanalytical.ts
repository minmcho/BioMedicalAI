// LAB-D3/D4/D5 — pre-analytical depth client (app.lab_preanalytical).
// Types live here, not in api/types.ts (the labMicro.ts precedent).
import type { ApiClient } from "./client";

export interface ChecklistItem {
  code: string;
  display: string;
  criterionCode: string;
  required: boolean;
}

export interface TransportRule {
  specimenType: string;
  tubeEligible: boolean;
  neverTube: boolean;
  temperatureCondition: string;
  maxTransitMinutes: number;
}

export interface StorageLocation {
  code: string;
  display: string;
  parentCode: string | null;
  temperatureCondition: string;
  deviceKey: string | null;
}

export interface PreanalyticalConfig {
  config_key: string;
  receipt_checklist: ChecklistItem[];
  transport_rules: TransportRule[];
  default_transport_rule: TransportRule | null;
  storage_locations: StorageLocation[];
  retention_schedule: { specimenType: string; holdDays: number }[];
  default_hold_days: number;
  retrieval_purposes: string[];
  sendout_return_reasons: { code: string; display: string }[];
}

export interface QueueItem {
  order_id: string;
  code: string;
  display: string;
  bench_code: string;
  specimen_id: string;
  specimen_type: string;
  collection_id: string | null;
  accession: string | null;
  patient_ref: string;
  collected_at: string | null;
  returned_from_sendout: boolean;
  return_reason_code: string | null;
  return_reason_display: string | null;
}

export interface ReceiptOutcome {
  receipt: {
    id: number; specimen_key: string; bench_code: string;
    collection_id: string | null; accession: string | null;
    status: string; rejection_criterion: string | null;
    transport_exceptions: { code: string; detail: string }[];
  };
  rejected: boolean;
  accession?: string;
  collection_id?: string;
  rejection?: { criterion_code: string | null; failed_items: string[] };
}

export interface TransportException {
  code: string;
  detail: string;
}

export interface TransportRecord {
  id: number;
  specimen_key: string;
  method: string;
  temperature_condition: string;
  transit_minutes: number | null;
  exceptions: TransportException[];
  actor: string;
  at: string | null;
}

export interface StorageRecord {
  id: number;
  specimen_key: string;
  location_code: string;
  status: string;
  stored_by: string;
  stored_at: string | null;
  disposal_due: string | null;
  disposal_due_date: string | null;
  disposed_by: string | null;
  disposal_reason: string | null;
  excursion_flags: { id: number; device_key: string; reading_id: number; detail: string | null }[];
}

export interface StorageBoard {
  stored: StorageRecord[];
  due_for_disposal: StorageRecord[];
  disposed: StorageRecord[];
  excursions: { id: number; specimen_key: string; location_code: string; device_key: string; reading_id: number; detail: string | null }[];
  as_of: string;
}

export interface SendoutContractLab {
  lab_code: string;
  display: string;
  tat_hours: number;
  performing_lab: { code?: string; name?: string; cliaNumber?: string };
  tests: {
    code: string; display: string;
    stability: { hours: number; condition: string } | null;
    shipping_temperature: string | null;
    split_before_transport: string | null;
    required_attachments: string[];
    report_window_hours: number | null;
    classification: string | null;
  }[];
}

export interface SendoutManifest {
  manifest_key: string;
  lab_code: string;
  order_ids: string[];
  shipping_temperature: string;
  split_instructions: Record<string, string | null>;
  attachments: Record<string, string[]>;
  status: string;
  expected_report_by: string | null;
  dispatched_at: string | null;
}

export interface ChaseItem {
  order_id: string;
  code: string;
  lab_code: string;
  dispatched_at: string;
  expected_by: string;
  overdue_hours: number;
  report_window_hours: number;
  classification: string | null;
}

export interface ExistingSendoutItem {
  order_id: string;
  code: string;
  display: string;
  sendout: { status: string; lab?: string; manifest_id?: string };
  reference_lab: { code: string; display: string; tat_hours: number };
}

const post = (client: ApiClient, path: string, body: unknown) =>
  client.request(path, { method: "POST", body: JSON.stringify(body) });

export function getPreanalyticalConfig(client: ApiClient): Promise<PreanalyticalConfig> {
  return client.request("/lab/preanalytical/config");
}

export function getReceivingQueue(client: ApiClient): Promise<{ items: QueueItem[]; total: number }> {
  return client.request("/lab/preanalytical/receiving-queue");
}

export function receiveSpecimen(client: ApiClient, body: unknown): Promise<ReceiptOutcome> {
  return post(client, "/lab/preanalytical/receive", body) as Promise<ReceiptOutcome>;
}

export function transportScan(client: ApiClient, body: unknown) {
  return post(client, "/lab/preanalytical/transport-scan", body) as Promise<{
    transport: { exceptions: TransportException[]; transit_minutes: number | null; rule: TransportRule };
  }>;
}

export function getTransportExceptions(client: ApiClient): Promise<{ items: TransportRecord[]; total: number }> {
  return client.request("/lab/preanalytical/transport-exceptions");
}

export function getStorageBoard(client: ApiClient): Promise<StorageBoard> {
  return client.request("/lab/preanalytical/storage/board");
}

export function storeSpecimen(client: ApiClient, body: unknown) {
  return post(client, "/lab/preanalytical/storage/store", body) as Promise<{ storage: StorageRecord }>;
}

export function disposeSpecimen(client: ApiClient, specimenKey: string, body: unknown) {
  return post(client, `/lab/preanalytical/storage/${specimenKey}/dispose`, body) as Promise<{ storage: StorageRecord }>;
}

export function retrieveSpecimen(client: ApiClient, specimenKey: string, body: unknown) {
  return post(client, `/lab/preanalytical/storage/${specimenKey}/retrieve`, body) as Promise<unknown>;
}

export function evaluateExcursions(client: ApiClient) {
  return post(client, "/lab/preanalytical/storage/evaluate-excursions", {}) as Promise<{
    flags_created: number; feed: { live: boolean; note: string };
  }>;
}

export function getSendoutContracts(client: ApiClient): Promise<{ laboratories: SendoutContractLab[] }> {
  return client.request("/lab/preanalytical/sendouts/contracts");
}

export function getExistingSendouts(client: ApiClient): Promise<{ items: ExistingSendoutItem[] }> {
  return client.request("/lab/sendouts");
}

export function packManifest(client: ApiClient, body: unknown) {
  return post(client, "/lab/preanalytical/sendouts/pack", body) as Promise<{ manifest: SendoutManifest }>;
}

export function dispatchManifest(client: ApiClient, manifestKey: string) {
  return post(client, `/lab/preanalytical/sendouts/manifests/${manifestKey}/dispatch`, {}) as Promise<{ manifest: SendoutManifest }>;
}

export function getChaseList(client: ApiClient): Promise<{ items: ChaseItem[]; total: number }> {
  return client.request("/lab/preanalytical/sendouts/chase-list");
}

export function returnSendout(client: ApiClient, orderId: string, body: unknown) {
  return post(client, `/lab/preanalytical/sendouts/${orderId}/return`, body) as Promise<unknown>;
}
