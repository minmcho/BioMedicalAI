// Report designer client (Phase 15, BD-32 + BD-30) — standalone
// client module for the visual report designer in System Studio.
//
// Backend sources of truth (read before changing any shape here):
// - GET /designer/datasets[/{name}] — backend/app/platform_depth.py:
//   the palette schema (dataset keys/titles/descriptions, field names
//   with inferred types, filter ops, formats). Built on the report
//   engine's registry — there is NO SQL anywhere in this surface and
//   the server refuses a definition carrying a `sql`/`query` key (422).
// - /report-engine/* — backend/app/report_engine.py: the governed
//   definition machinery. Definitions are Studio config of kind
//   `report-definition`: create draft (POST /report-engine/definitions/
//   {key}) -> human publishes -> run (POST /report-engine/{key}/run,
//   formats json/csv/pdf). Runs execute the PUBLISHED version only.
// - GET /platform-depth/dashboard-indicators — backend/app/
//   platform_depth.py dashboard_trace(): the BD-30 trace of which
//   dashboard widgets resolve through a published indicator and which
//   stay on a live endpoint (with the reason).
//
// VALIDATION NOTE (honest): the backend exposes no validate-only
// endpoint. Server validation (app.report_engine.
// validate_report_definition) runs inside the draft create/update
// endpoints BEFORE any row is written, so an invalid definition is
// refused 422 and persists nothing. `validateReportDefinition` below
// therefore performs server validation through the draft endpoint:
// success yields the governed draft; a 422 surfaces the server's
// detail verbatim and nothing was saved.
import type { ApiClient } from "./client";
import type { DownloadedFile } from "./types";

export interface DesignerField {
  name: string;
  type: string;
}

export interface DesignerDatasetSummary {
  key: string;
  title: string;
  description: string;
  field_count: number;
  fields: DesignerField[];
}

export interface DesignerDatasetDetail {
  key: string;
  title: string;
  description: string;
  fields: DesignerField[];
  filter_ops: string[];
  formats: string[];
}

export interface DesignerDatasetsResponse {
  items: DesignerDatasetSummary[];
  total: number;
}

/** One filter row of a report definition: field + op + value. For op
 *  `in` the server requires `value` to be a list. */
export interface ReportFilter {
  field: string;
  op: string;
  value: unknown;
}

export interface ReportDefinitionPayload {
  title: string;
  dataset: string;
  fields: string[];
  filters: ReportFilter[];
  grouping: string[];
  formats: string[];
}

/** The report-engine definition object (app.report_engine._out). */
export interface ReportDefinitionObject {
  kind: string;
  key: string;
  version: number;
  status: string;
  payload: ReportDefinitionPayload;
  created_by: string;
  updated_by: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface ReportDefinitionHistory {
  key: string;
  versions: ReportDefinitionObject[];
  published: ReportDefinitionObject | null;
}

export interface ReportRunResult {
  key: string;
  version: number;
  title: string;
  dataset: string;
  format: string;
  row_count: number;
  fields: string[];
  rows: Array<Record<string, unknown>>;
  groups: Array<Record<string, unknown>> | null;
  run: {
    id: number;
    definition_key: string;
    definition_version: number;
    actor: string;
    row_count: number;
    format: string;
    ran_at: string | null;
  };
}

export interface DashboardWidgetTrace {
  id: string | null;
  title: string | null;
  disposition: "indicator" | "live-endpoint";
  indicator: string | null;
  applied: boolean;
  endpoint: string | null;
  reason?: string;
}

export interface DashboardTrace {
  key: string;
  version: number;
  title: string | null;
  widgets: DashboardWidgetTrace[];
}

export interface DashboardIndicatorsTrace {
  dashboards: DashboardTrace[];
  total: number;
  widgets_total: number;
  widgets_on_indicators: number;
  widgets_on_live_endpoints: number;
}

/** Built-in fallback for the engine's filter ops / formats, identical
 *  to app.report_engine FILTER_OPS / FORMATS — used only until the
 *  dataset detail (which carries the server's own lists) loads. */
export const FALLBACK_FILTER_OPS = ["eq", "ne", "in", "gte", "lte", "contains"];
export const FALLBACK_FORMATS = ["json", "csv", "pdf"];

/** GET /designer/datasets — the palette catalogue. */
export function listDesignerDatasets(
  client: ApiClient,
): Promise<DesignerDatasetsResponse> {
  return client.request<DesignerDatasetsResponse>("/designer/datasets");
}

/** GET /designer/datasets/{name} — one dataset's palette schema,
 *  including the server's filter ops and formats. */
export function getDesignerDataset(
  client: ApiClient,
  name: string,
): Promise<DesignerDatasetDetail> {
  return client.request<DesignerDatasetDetail>(
    `/designer/datasets/${encodeURIComponent(name)}`,
  );
}

/** POST /report-engine/definitions/{key} — create the governed DRAFT
 *  (server-validated; 409 when a draft already exists for the key). */
export function createReportDraft(
  client: ApiClient,
  key: string,
  definition: ReportDefinitionPayload,
): Promise<ReportDefinitionObject> {
  return client.request<ReportDefinitionObject>(
    `/report-engine/definitions/${encodeURIComponent(key)}`,
    { method: "POST", body: JSON.stringify(definition) },
  );
}

/** PUT /report-engine/definitions/{key}/versions/{version} — replace
 *  a DRAFT version's payload (server-validated; published versions
 *  are immutable and refuse with 409). */
export function updateReportDraft(
  client: ApiClient,
  key: string,
  version: number,
  definition: ReportDefinitionPayload,
): Promise<ReportDefinitionObject> {
  return client.request<ReportDefinitionObject>(
    `/report-engine/definitions/${encodeURIComponent(key)}/versions/${version}`,
    { method: "PUT", body: JSON.stringify(definition) },
  );
}

/** GET /report-engine/definitions/{key} — version history + the
 *  published version (used to adopt an existing draft after a 409). */
export function getReportDefinition(
  client: ApiClient,
  key: string,
): Promise<ReportDefinitionHistory> {
  return client.request<ReportDefinitionHistory>(
    `/report-engine/definitions/${encodeURIComponent(key)}`,
  );
}

/** Server validation of a definition (see the module note: the
 *  backend validates inside the draft endpoints, before persisting).
 *  With `draftVersion`, validation runs through the draft update;
 *  otherwise through draft creation. Resolves with the draft object
 *  on success; rejects with the server's ApiError (detail verbatim)
 *  on a 422. */
export function validateReportDefinition(
  client: ApiClient,
  key: string,
  definition: ReportDefinitionPayload,
  draftVersion: number | null,
): Promise<ReportDefinitionObject> {
  if (draftVersion !== null) {
    return updateReportDraft(client, key, draftVersion, definition);
  }
  return createReportDraft(client, key, definition);
}

/** POST /report-engine/{key}/run {format:"json"} — run the PUBLISHED
 *  definition and return its rows (404 when nothing is published
 *  under the key: drafts never run). */
export function runReportPreview(
  client: ApiClient,
  key: string,
): Promise<ReportRunResult> {
  return client.request<ReportRunResult>(
    `/report-engine/${encodeURIComponent(key)}/run`,
    { method: "POST", body: JSON.stringify({ format: "json" }) },
  );
}

/** POST /report-engine/{key}/run {format} as a download (CSV/PDF) —
 *  rides ApiClient.requestBlob, the existing download helper path. */
export function downloadReportRun(
  client: ApiClient,
  key: string,
  format: "csv" | "pdf",
): Promise<DownloadedFile> {
  return client.requestBlob(`/report-engine/${encodeURIComponent(key)}/run`, {
    method: "POST",
    body: JSON.stringify({ format }),
  });
}

/** GET /platform-depth/dashboard-indicators — the BD-30 trace. */
export function dashboardIndicators(
  client: ApiClient,
): Promise<DashboardIndicatorsTrace> {
  return client.request<DashboardIndicatorsTrace>(
    "/platform-depth/dashboard-indicators",
  );
}
