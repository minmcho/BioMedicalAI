// LAB-D14 — molecular diagnostics client (app.lab_molecular).
// Types live here, not in api/types.ts (the labMicro.ts precedent).
import type { ApiClient } from "./client";

export interface MolecularOption {
  code: string;
  display: string;
  active?: boolean;
  order?: number;
  steps?: string[];
}

export interface MolecularTarget {
  code: string;
  display: string;
  detectedMaxCt: number;
  inconclusiveMaxCt: number;
}

export interface MolecularAssay {
  code: string;
  display: string;
  active?: boolean;
  targets: MolecularTarget[];
  requiresInternalControl: boolean;
  requiresExtractionControl: boolean;
  requiresPositiveControl: boolean;
  maxBatchSize: number;
  runWindows: string[];
}

export interface MolecularConfig {
  config: { kind: string; key: string; version: number | null };
  assays: MolecularAssay[];
  areas: MolecularOption[];
  ntc_positive_max_ct: number | null;
  repeat_options: MolecularOption[];
  decontamination_methods: MolecularOption[];
}

export interface AreaStamp {
  area: string;
  at: string;
  by: string;
  flagged: boolean;
}

export interface MolecularBatch {
  id: number;
  assay_code: string;
  instrument_id: string | null;
  operator: string;
  status: string;
  samples: { order_id: string; specimen_id: string | null;
             patient_ref: string | null }[];
  sample_count: number;
  area_stamps: AreaStamp[];
  workflow_flagged: boolean;
  workflow_flag_reason: string | null;
  created_by: string;
  created_at: string | null;
}

export interface MolecularSample {
  id: number;
  run_id: number;
  order_id: string;
  specimen_id: string | null;
  patient_ref: string | null;
  ct_values: Record<string, number | null> | null;
  target_interpretations: Record<string, string> | null;
  internal_control: { status: string; ct: number | null } | null;
  extraction_control: { status: string; ct: number | null } | null;
  interpretation: string | null;
  discarded_interpretation: string | null;
  validity: string;
  validity_reason: string | null;
  report_id: string | null;
  resulted_by: string | null;
  resulted_at: string | null;
}

export interface MolecularRun {
  id: number;
  batch_id: number;
  assay_code: string;
  instrument_id: string | null;
  operator: string;
  status: string;
  ntc: { status: string; ct: number | null };
  positive_control: { status: string | null; ct: number | null };
  invalidated_reason: string | null;
  invalidated_by: string | null;
  invalidated_at: string | null;
  repeat_of_run_id: number | null;
  repeat_from: string | null;
  repeat_run_id: number | null;
  released_by: string | null;
  released_at: string | null;
  gate: { can_release: boolean; blockers: string[] };
  sample_count: number;
  samples?: MolecularSample[];
  batch?: MolecularBatch | null;
  released?: { order_id: string; report_id: string;
               interpretation: string }[];
  contamination_event?: ContaminationEvent;
}

export interface ContaminationEvent {
  id: number;
  trigger: string;
  status: string;
  note: string | null;
  source_run_id: number | null;
  affected_run_ids: number[];
  affected_batch_ids: number[];
  instruments: string[];
  areas: string[];
  decontamination: {
    method: string; areas: string[];
    verification_swabs: { area: string; result: string }[];
    recorded_by: string;
  } | null;
  raised_by: string;
  created_at: string | null;
  cleared_by: string | null;
  cleared_at: string | null;
}

const post = (client: ApiClient, path: string, body: unknown) =>
  client.request(path, { method: "POST", body: JSON.stringify(body) });

export const getMolecularConfig = (c: ApiClient) =>
  c.request<MolecularConfig>("/lab/molecular/config");
export const listBatches = (c: ApiClient) =>
  c.request<{ items: MolecularBatch[]; total: number }>(
    "/lab/molecular/batches");
export const createBatch = (c: ApiClient, body: unknown) =>
  post(c, "/lab/molecular/batches", body) as Promise<MolecularBatch>;
export const addAreaStamps = (c: ApiClient, batchId: number, body: unknown) =>
  post(c, `/lab/molecular/batches/${batchId}/area-stamps`, body) as
    Promise<MolecularBatch>;
export const listRuns = (c: ApiClient) =>
  c.request<{ items: MolecularRun[]; total: number }>("/lab/molecular/runs");
export const getRun = (c: ApiClient, runId: number) =>
  c.request<MolecularRun>(`/lab/molecular/runs/${runId}`);
export const createRun = (c: ApiClient, body: unknown) =>
  post(c, "/lab/molecular/runs", body) as Promise<MolecularRun>;
export const recordControls = (c: ApiClient, runId: number, body: unknown) =>
  post(c, `/lab/molecular/runs/${runId}/controls`, body) as
    Promise<MolecularRun>;
export const recordResults = (c: ApiClient, runId: number, body: unknown) =>
  post(c, `/lab/molecular/runs/${runId}/results`, body) as
    Promise<MolecularRun>;
export const releaseRun = (c: ApiClient, runId: number) =>
  post(c, `/lab/molecular/runs/${runId}/release`, {}) as
    Promise<MolecularRun>;
export const repeatRun = (c: ApiClient, runId: number, body: unknown) =>
  post(c, `/lab/molecular/runs/${runId}/repeat`, body) as
    Promise<MolecularRun>;
export const listContaminationEvents = (c: ApiClient) =>
  c.request<{ items: ContaminationEvent[]; total: number }>(
    "/lab/molecular/contamination-events");
export const createContaminationEvent = (c: ApiClient, body: unknown) =>
  post(c, "/lab/molecular/contamination-events", body) as
    Promise<ContaminationEvent>;
export const decontaminate = (c: ApiClient, eventId: number, body: unknown) =>
  post(c, `/lab/molecular/contamination-events/${eventId}/decontaminate`,
       body) as Promise<ContaminationEvent>;
