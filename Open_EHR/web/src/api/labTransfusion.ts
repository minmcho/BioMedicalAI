// LAB-D13 — transfusion service completion client (app.lab_transfusion).
// Types live here, not in api/types.ts (the labMicro.ts precedent).
import type { ApiClient } from "./client";

export interface TransfusionOption {
  code: string;
  display: string;
  active?: boolean;
  products?: string[];
  steps?: string[];
}

export interface TransfusionConfig {
  config: { kind: string; key: string };
  standard_validity_hours: number;
  extended_validity_days: number;
  extended_lookback_days: number;
  reservation_expiry_hours: number;
  post_thaw_expiry_hours: Record<string, number>;
  special_requirements: TransfusionOption[];
  unit_attributes: TransfusionOption[];
  antibody_id_methods: TransfusionOption[];
  prep_kinds: TransfusionOption[];
  prep_step_displays: Record<string, string>;
  dispositions: TransfusionOption[];
  wastage_reason_codes: TransfusionOption[];
  severity_grades: TransfusionOption[];
  imputability_grades: TransfusionOption[];
  reaction_steps: { code: string; display: string }[];
}

export interface TsSpecimen {
  specimen_id: string;
  patient_ref: string;
  order_id: string | null;
  screen_observation_id: string | null;
  collected_at: string;
  validity_kind: string;
  extended_requested: boolean;
  extended_granted: boolean;
  extended_denial_reason: string | null;
  expires_at: string;
  status: "valid" | "expired";
}

export interface HistoricalAntibody {
  id: number;
  patient_ref: string;
  specificity: string;
  clinically_significant: boolean;
  first_identified_on: string | null;
  source: string;
  active: boolean;
}

export interface AntibodyWorkup {
  workup_id: string;
  patient_ref: string;
  method: string;
  panel_reactions: { cell: string; reaction: string }[];
  identified_specificities: string[];
  ruled_out: string[];
  conclusion: string;
}

export interface SpecialRequirement {
  id: number;
  patient_ref: string;
  requirement_code: string;
  specificity: string | null;
  active: boolean;
}

export interface Reservation {
  reservation_id: string;
  unit_id: string;
  patient_ref: string;
  status: string;
  expires_at: string | null;
  release_reason: string | null;
}

export interface Preparation {
  prep_id: string;
  unit_id: string;
  prep_kind: string;
  product_type: string;
  status: string;
  next_step: string | null;
  expected_steps: { code: string; display: string; done: boolean }[];
  steps: { step: string; at: string; recorded_by: string }[];
  prepared_at: string | null;
  expires_at: string | null;
}

export interface ReactionStepState {
  code: string;
  display: string;
  done: boolean;
  result: string | null;
  recorded_by: string | null;
}

export interface ReactionWorkup {
  workup_id: string;
  unit_id: string;
  patient_ref: string;
  status: string;
  steps: ReactionStepState[];
  next_step: string | null;
  severity: string | null;
  imputability: string | null;
  haemovigilance_report_id: string | null;
}

export interface WastageReport {
  items: { disposition_id: string; unit_id: string; disposition: string;
           reason_code: string | null; created_at: string }[];
  total: number;
  by_reason: Record<string, number>;
  by_disposition: Record<string, number>;
  period: { from: string | null; to: string | null };
}

const post = (client: ApiClient, path: string, body: unknown) =>
  client.request(path, { method: "POST", body: JSON.stringify(body) });

export const getTransfusionConfig = (c: ApiClient) =>
  c.request<TransfusionConfig>("/lab/transfusion/config");
export const listTsSpecimens = (c: ApiClient) =>
  c.request<{ items: TsSpecimen[]; total: number; expired: number; valid: number }>(
    "/lab/transfusion/ts-specimens");
export const registerTsSpecimen = (c: ApiClient, body: unknown) =>
  post(c, "/lab/transfusion/ts-specimens", body);
export const listHistorical = (c: ApiClient, patientRef: string) =>
  c.request<{ items: HistoricalAntibody[]; total: number }>(
    `/lab/transfusion/historical-antibodies?patient_ref=${encodeURIComponent(patientRef)}`);
export const listWorkups = (c: ApiClient, patientRef: string) =>
  c.request<{ items: AntibodyWorkup[]; total: number }>(
    `/lab/transfusion/antibody-workups?patient_ref=${encodeURIComponent(patientRef)}`);
export const createAntibodyWorkup = (c: ApiClient, body: unknown) =>
  post(c, "/lab/transfusion/antibody-workups", body);
export const listRequirements = (c: ApiClient, patientRef: string) =>
  c.request<{ items: SpecialRequirement[]; total: number }>(
    `/lab/transfusion/special-requirements?patient_ref=${encodeURIComponent(patientRef)}`);
export const createRequirement = (c: ApiClient, body: unknown) =>
  post(c, "/lab/transfusion/special-requirements", body);
export const deactivateRequirement = (c: ApiClient, id: number, body: unknown) =>
  post(c, `/lab/transfusion/special-requirements/${id}/deactivate`, body);
export const createUnitAttribute = (c: ApiClient, body: unknown) =>
  post(c, "/lab/transfusion/unit-attributes", body);
export const listReservations = (c: ApiClient) =>
  c.request<{ items: Reservation[]; total: number; active: number }>(
    "/lab/transfusion/reservations");
export const createReservation = (c: ApiClient, body: unknown) =>
  post(c, "/lab/transfusion/reservations", body);
export const releaseReservation = (c: ApiClient, id: string, body: unknown) =>
  post(c, `/lab/transfusion/reservations/${encodeURIComponent(id)}/release`, body);
export const listPreparations = (c: ApiClient) =>
  c.request<{ items: Preparation[]; total: number }>("/lab/transfusion/preparations");
export const createPreparation = (c: ApiClient, body: unknown) =>
  post(c, "/lab/transfusion/preparations", body);
export const recordPrepStep = (c: ApiClient, id: string, body: unknown) =>
  post(c, `/lab/transfusion/preparations/${encodeURIComponent(id)}/steps`, body);
export const recordDisposition = (c: ApiClient, body: unknown) =>
  post(c, "/lab/transfusion/dispositions", body);
export const getWastageReport = (c: ApiClient) =>
  c.request<WastageReport>("/lab/transfusion/wastage-report");
export const listReactionWorkups = (c: ApiClient) =>
  c.request<{ items: ReactionWorkup[]; total: number }>(
    "/lab/transfusion/reaction-workups");
export const createReactionWorkup = (c: ApiClient, body: unknown) =>
  post(c, "/lab/transfusion/reaction-workups", body);
export const recordReactionStep = (c: ApiClient, id: string, body: unknown) =>
  post(c, `/lab/transfusion/reaction-workups/${encodeURIComponent(id)}/steps`, body);
export const gradeReactionWorkup = (c: ApiClient, id: string, body: unknown) =>
  post(c, `/lab/transfusion/reaction-workups/${encodeURIComponent(id)}/grading`, body);
export const fileHaemovigilance = (c: ApiClient, id: string, body: unknown) =>
  post(c, `/lab/transfusion/reaction-workups/${encodeURIComponent(id)}/haemovigilance-report`, body);
