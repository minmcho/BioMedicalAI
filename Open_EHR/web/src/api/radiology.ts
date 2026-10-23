// Phase 15 radiology department client.
// Source of truth: backend/app/radiology.py (the department: RIS
// worklist, DICOM ingest/archive + DICOMweb retrieval, MWL,
// protocoling + screening, dose, structured reports + addenda,
// critical-result closed loop, TAT stats) layered over the Phase 4B
// board in backend/app/boards.py (study creation, study detail,
// modalities, plain report drafts, report verification — those routes
// are unchanged and are called here, not forked). Types live here,
// not in api/types.ts, per the Phase 15 web brief. Every function
// takes the ApiClient as its first argument and calls
// client.request(...).

import type { ApiClient } from "./client";
import type {
  DiagnosticReport,
  FormDefinition,
  ModalitiesResponse,
  QuestionnaireAnswerItem,
  RadiologyStudyDetail,
  RadiologyStudyItem,
  StudioConfigObject,
} from "./types";

export interface ListResponse<T> {
  items: T[];
  total: number;
}

// ---- RIS worklist (GET /radiology/worklist) ----

export interface RadScreeningEntry {
  responseId?: string;
  formVersion?: number;
  completedBy?: string;
  completedAt?: string;
}

export interface RadWorklistItem {
  study_id: string;
  study_uid: string | null;
  patient_ref: string;
  modality: string;
  description: string | null;
  priority: string;
  tat_target_minutes: number | null;
  state: string;
  protocol_code: string | null;
  scheduled_at: string | null;
  station_ae: string | null;
  ordered_at: string | null;
  screening: Record<string, RadScreeningEntry>;
  report: { id: string; status?: string } | null;
  state_history: Array<{ state: string; at?: string; by?: string }>;
}

export interface WorklistFilters {
  state?: string;
  priority?: string;
  modality?: string;
}

export function getWorklist(
  client: ApiClient,
  filters: WorklistFilters = {},
): Promise<ListResponse<RadWorklistItem>> {
  const params = new URLSearchParams();
  if (filters.state) params.set("state", filters.state);
  if (filters.priority) params.set("priority", filters.priority);
  if (filters.modality) params.set("modality", filters.modality);
  const qs = params.toString();
  return client.request(`/radiology/worklist${qs ? `?${qs}` : ""}`);
}

export function transitionWorklist(
  client: ApiClient,
  studyId: string,
  toStatus: string,
): Promise<RadWorklistItem> {
  return client.request(
    `/radiology/worklist/${encodeURIComponent(studyId)}/transition`,
    { method: "POST", body: JSON.stringify({ to_status: toStatus }) },
  );
}

export function assignProtocol(
  client: ApiClient,
  studyId: string,
  protocolCode: string,
): Promise<RadWorklistItem> {
  return client.request(
    `/radiology/worklist/${encodeURIComponent(studyId)}/protocol`,
    { method: "POST", body: JSON.stringify({ protocol_code: protocolCode }) },
  );
}

export function submitScreening(
  client: ApiClient,
  studyId: string,
  formKey: string,
  answers: QuestionnaireAnswerItem[],
): Promise<{ study_id: string; response: unknown; screening: Record<string, RadScreeningEntry> }> {
  return client.request(
    `/radiology/worklist/${encodeURIComponent(studyId)}/screening/${encodeURIComponent(formKey)}`,
    { method: "POST", body: JSON.stringify({ answers }) },
  );
}

// ---- Governed config the department reads (Studio published) ----

export interface RadProtocol {
  code: string;
  display?: string;
  active?: boolean;
  modality?: string;
  contrast?: boolean;
  screeningForms?: string[];
}

/** Protocols from the published terminology/imaging-protocols config. */
export async function getProtocols(client: ApiClient): Promise<RadProtocol[]> {
  const cfg = await client.request<StudioConfigObject>(
    "/studio/config/terminology/effective/imaging-protocols",
  );
  const concepts = (cfg.payload as { concepts?: unknown })?.concepts;
  return Array.isArray(concepts) ? (concepts as RadProtocol[]) : [];
}

export interface RadOps {
  doseQuantities?: Record<string, Array<{ code: string; unit?: string }>>;
  criticalSlaMinutes?: number;
  [key: string]: unknown;
}

/** The published terminology/radiology-ops payload (dose quantities…). */
export async function getOps(client: ApiClient): Promise<RadOps> {
  const cfg = await client.request<StudioConfigObject>(
    "/studio/config/terminology/effective/radiology-ops",
  );
  return (cfg.payload ?? {}) as RadOps;
}

/** One published governed form (kind=form) — screening + report templates. */
export async function getPublishedForm(
  client: ApiClient,
  key: string,
): Promise<{ form: FormDefinition; version: number }> {
  const cfg = await client.request<StudioConfigObject>(
    `/studio/config/form/effective/${encodeURIComponent(key)}`,
  );
  return { form: cfg.payload as unknown as FormDefinition, version: cfg.version };
}

// ---- Boards board surface (unchanged Phase 4B routes) ----

export function getModalities(client: ApiClient): Promise<ModalitiesResponse> {
  return client.request("/radiology/modalities");
}

export function getStudyDetail(client: ApiClient, id: string): Promise<RadiologyStudyDetail> {
  return client.request(`/radiology/studies/${encodeURIComponent(id)}`);
}

export function createStudy(
  client: ApiClient,
  input: { serviceRequestId: string; modality: string },
): Promise<RadiologyStudyItem> {
  return client.request("/radiology/studies", {
    method: "POST",
    body: JSON.stringify({
      service_request_id: input.serviceRequestId,
      modality: input.modality,
    }),
  });
}

export function createImagingReport(
  client: ApiClient,
  studyId: string,
  conclusion: string,
): Promise<DiagnosticReport> {
  return client.request(`/radiology/studies/${encodeURIComponent(studyId)}/report`, {
    method: "POST",
    body: JSON.stringify({ conclusion }),
  });
}

export function verifyImagingReport(client: ApiClient, reportId: string): Promise<DiagnosticReport> {
  return client.request(`/radiology/reports/${encodeURIComponent(reportId)}/verify`, {
    method: "POST",
  });
}

// ---- DICOMweb archive metadata (GET /radiology/dicomweb/...) ----

/** DICOM JSON object: tag -> { vr, Value }. */
export type DicomJson = Record<string, { vr?: string; Value?: unknown[] }>;

export function getSeries(client: ApiClient, studyUid: string): Promise<DicomJson[]> {
  return client.request(
    `/radiology/dicomweb/studies/${encodeURIComponent(studyUid)}/series`,
  );
}

export function getInstances(
  client: ApiClient,
  studyUid: string,
  seriesUid: string,
): Promise<DicomJson[]> {
  return client.request(
    `/radiology/dicomweb/studies/${encodeURIComponent(studyUid)}/series/${encodeURIComponent(seriesUid)}/instances`,
  );
}

// ---- DICOM ingest (POST /radiology/dicom/ingest) ----

export interface DicomIngestResult {
  id: number;
  study_id: string;
  study_uid: string;
  series_uid: string;
  sop_uid: string;
  sop_class_uid: string;
  modality: string;
  patient_id: string;
  series_number: number | null;
  instance_number: number | null;
  cold_ref: string;
  content_hash: string;
  size_bytes: number;
  already_archived: boolean;
}

/** Ingest ONE DICOM object. The backend contract is a JSON body
 *  {dicom_base64} — not multipart — and ingest is for ordered studies:
 *  the object's Study Instance UID must match an existing ImagingStudy
 *  and its PatientID must match that study's patient (422 otherwise). */
export function ingestDicom(
  client: ApiClient,
  dicomBase64: string,
): Promise<DicomIngestResult> {
  return client.request("/radiology/dicom/ingest", {
    method: "POST",
    body: JSON.stringify({ dicom_base64: dicomBase64 }),
  });
}

// ---- DICOM Modality Worklist (GET /radiology/mwl) ----

/** One MWL entry: DICOM JSON (tag -> {vr, Value}), PS3.18. The
 *  Scheduled Procedure Step sequence lives under tag 00400100 and
 *  carries the modality, station AE title and scheduled date/time. */
export type MwlEntry = DicomJson;

export function getModalityWorklist(client: ApiClient): Promise<MwlEntry[]> {
  return client.request("/radiology/mwl");
}

// ---- Structured reporting + addenda ----

export function structuredReport(
  client: ApiClient,
  studyId: string,
  template: string,
  answers: QuestionnaireAnswerItem[],
): Promise<DiagnosticReport & { questionnaireResponseId: string; template: string }> {
  return client.request(
    `/radiology/studies/${encodeURIComponent(studyId)}/structured-report`,
    { method: "POST", body: JSON.stringify({ template, answers }) },
  );
}

export function addendum(
  client: ApiClient,
  reportId: string,
  conclusion: string,
): Promise<DiagnosticReport> {
  return client.request(`/radiology/reports/${encodeURIComponent(reportId)}/addendum`, {
    method: "POST",
    body: JSON.stringify({ conclusion }),
  });
}

// ---- Critical results closed loop ----

export interface RadCritical {
  report_id: string;
  study_id: string;
  patient_ref: string;
  ordering_clinician: string;
  state: string; // notified | read-back | closed
  task_id: string | null;
  flagged_by: string;
  flagged_at: string | null;
  sla_minutes: number;
  overdue: boolean;
  minutes_overdue: number | null;
  readback_by: string | null;
  readback_text: string | null;
  readback_at: string | null;
  closed_by: string | null;
  closed_at: string | null;
}

export function listCritical(client: ApiClient): Promise<ListResponse<RadCritical>> {
  return client.request("/radiology/critical");
}

export function flagCritical(client: ApiClient, reportId: string): Promise<RadCritical> {
  return client.request(`/radiology/reports/${encodeURIComponent(reportId)}/critical`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function readBackCritical(
  client: ApiClient,
  reportId: string,
  reader: string,
  readBackText: string,
): Promise<RadCritical> {
  return client.request(
    `/radiology/reports/${encodeURIComponent(reportId)}/critical/read-back`,
    { method: "POST", body: JSON.stringify({ reader, read_back_text: readBackText }) },
  );
}

export function closeCritical(client: ApiClient, reportId: string): Promise<RadCritical> {
  return client.request(`/radiology/reports/${encodeURIComponent(reportId)}/critical/close`, {
    method: "POST",
  });
}

export function scanCritical(
  client: ApiClient,
): Promise<{ scanned: number; overdue: number; escalated: unknown[]; total: number }> {
  return client.request("/radiology/critical/scan", { method: "POST" });
}

// ---- Radiation dose ----

export interface RadDoseRecord {
  id: number;
  study_id: string;
  patient_ref?: string;
  modality: string;
  quantity: string;
  value: number;
  unit: string;
  recorded_by: string;
  recorded_at: string | null;
}

export interface RadDoseHistory {
  patient_ref: string;
  items: RadDoseRecord[];
  total: number;
  totals: Array<{ quantity: string; unit: string; total: number; count: number }>;
}

export function recordDose(
  client: ApiClient,
  studyId: string,
  quantity: string,
  value: number,
): Promise<RadDoseRecord> {
  return client.request(`/radiology/worklist/${encodeURIComponent(studyId)}/dose`, {
    method: "POST",
    body: JSON.stringify({ quantity, value }),
  });
}

export function doseHistory(client: ApiClient, patientId: string): Promise<RadDoseHistory> {
  return client.request(
    `/radiology/patients/${encodeURIComponent(patientId)}/dose-history`,
  );
}

// ---- TAT stats (GET /radiology/stats/tat) ----

export interface RadTatBucket {
  target_minutes: number | null;
  studies: number;
  breaches: number;
  total_minutes: number;
  avg_minutes: number | null;
}

export interface RadTatStats {
  generated_at: string;
  total: number;
  breaches: number;
  unreported_stat: number;
  by_priority: Record<string, RadTatBucket>;
}

export function tatStats(client: ApiClient): Promise<RadTatStats> {
  return client.request("/radiology/stats/tat");
}
