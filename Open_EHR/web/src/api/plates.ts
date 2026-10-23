// UX pass (WS3b) — virtual plates client (backend/app/lab_plates.py).
// Types below are copied from the backend serializers in that file:
//   _well_out  -> PlateWell
//   _qc        -> PlateQc
//   _run_out   -> PlateRun (detail adds `wells`)
//   templates  -> default_plate_templates_payload concepts, served as
//                 published Studio terminology config key
//                 "lab-plate-templates" (GET /studio/config/
//                 terminology/effective/lab-plate-templates); a concept
//                 is {code, display, active, assay, format, wells:
//                 [{position, role, expected?}]}.
// Endpoint shapes: POST /lab/plates -> PlateRun (detail),
// GET /lab/plates -> {items: PlateRun[], total},
// GET /lab/plates/{id} -> PlateRun (detail),
// POST /lab/plates/{id}/assign -> {plate: PlateRun, well: PlateWell},
// POST /lab/plates/{id}/results -> {plate: PlateRun, processed: [...]},
// POST /lab/plates/{id}/complete -> PlateRun (detail).
import type { ApiClient } from "./client";

export type PlateWellRole =
  | "control-pos"
  | "control-neg"
  | "calibrator"
  | "blank"
  | "patient"
  | "replicate";

export type PlateExpected =
  | { low: number; high: number }
  | { result: string };

export interface PlateTemplateWell {
  position: string;
  role: PlateWellRole | string;
  expected?: PlateExpected;
}

export interface PlateTemplate {
  code: string;
  display: string;
  active?: boolean;
  assay?: string;
  format?: string;
  wells: PlateTemplateWell[];
}

export interface PlateWell {
  position: string;
  role: string;
  expected: PlateExpected | null;
  specimen_id: string | null;
  order_id: string | null;
  result_value: unknown;
  result_status: string; // "pending" | "passed" | "failed" | "resulted"
  assigned_by: string | null;
  assigned_at: string | null;
  resulted_by: string | null;
  resulted_at: string | null;
}

export interface PlateQc {
  controls_total: number;
  controls_passed: number;
  controls_failed: number;
  gate_open: boolean;
}

export interface PlateRun {
  id: number;
  template_code: string;
  assay: string;
  format: string;
  status: string; // "open" | "complete"
  created_by: string;
  created_at: string | null;
  completed_by: string | null;
  completed_at: string | null;
  qc: PlateQc;
  wells?: PlateWell[];
}

export interface PlateList {
  items: PlateRun[];
  total: number;
}

export interface PlateAssignOutcome {
  plate: PlateRun;
  well: PlateWell;
}

export interface PlateProcessedEntry {
  position: string;
  role: string;
  result_status: string;
  value: unknown;
  order_id?: string;
  report_id?: string;
}

export interface PlateResultsOutcome {
  plate: PlateRun;
  processed: PlateProcessedEntry[];
}

export interface PlateResultEntry {
  position: string;
  value: string | number;
  code?: string;
}

export const PLATE_TEMPLATES_KEY = "lab-plate-templates";

/** Published plate templates from the governed terminology config
 *  (terminology/lab-plate-templates). Throws when nothing is
 *  published (the backend 503/404 path) so the caller can disable
 *  the picker with the reason shown. */
export async function getPlateTemplates(
  client: ApiClient,
): Promise<PlateTemplate[]> {
  const cfg =
    typeof client.effectiveConfig === "function"
      ? await client.effectiveConfig("terminology", PLATE_TEMPLATES_KEY)
      : await client.request(
          `/studio/config/terminology/effective/${PLATE_TEMPLATES_KEY}`,
        );
  const payload = (cfg as { payload?: Record<string, unknown> })?.payload ?? {};
  const concepts = Array.isArray(payload.concepts) ? payload.concepts : [];
  return concepts.filter(
    (c): c is PlateTemplate =>
      typeof c === "object" && c !== null && typeof (c as PlateTemplate).code === "string",
  );
}

export function listPlateRuns(client: ApiClient): Promise<PlateList> {
  return client.request("/lab/plates");
}

export function getPlateRun(client: ApiClient, plateId: number): Promise<PlateRun> {
  return client.request(`/lab/plates/${plateId}`);
}

export function createPlateRun(
  client: ApiClient,
  templateCode: string,
): Promise<PlateRun> {
  return client.request("/lab/plates", {
    method: "POST",
    body: JSON.stringify({ template_code: templateCode }),
  });
}

export function assignPlateSpecimen(
  client: ApiClient,
  plateId: number,
  input: { specimen_ref: string; position?: string },
): Promise<PlateAssignOutcome> {
  return client.request(`/lab/plates/${plateId}/assign`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function postPlateResults(
  client: ApiClient,
  plateId: number,
  results: PlateResultEntry[],
): Promise<PlateResultsOutcome> {
  return client.request(`/lab/plates/${plateId}/results`, {
    method: "POST",
    body: JSON.stringify({ results }),
  });
}

export function completePlateRun(
  client: ApiClient,
  plateId: number,
): Promise<PlateRun> {
  return client.request(`/lab/plates/${plateId}/complete`, { method: "POST" });
}

/** Parse a bulk paste box of "position,value" lines (one per line;
 *  a value may itself contain commas only in the qualitative case —
 *  everything after the first comma is the value). Blank lines and
 *  lines starting with # are ignored. Returns entries plus the raw
 *  text of any malformed lines so the UI can name them inline. */
export function parseBulkPlateResults(text: string): {
  entries: PlateResultEntry[];
  malformed: string[];
} {
  const entries: PlateResultEntry[] = [];
  const malformed: string[] = [];
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const comma = line.indexOf(",");
    if (comma <= 0) {
      malformed.push(rawLine);
      continue;
    }
    const position = line.slice(0, comma).trim();
    const rawValue = line.slice(comma + 1).trim();
    if (!position || rawValue === "") {
      malformed.push(rawLine);
      continue;
    }
    const numeric = Number(rawValue);
    entries.push({
      position,
      value: Number.isNaN(numeric) ? rawValue : numeric,
    });
  }
  return { entries, malformed };
}

/** Rows × columns for a plate's well grid, derived from the real
 *  well positions (e.g. chem-96 -> 8 rows A–H × 12 columns). */
export function plateGridShape(wells: Array<{ position: string }>): {
  rows: string[];
  columns: number[];
} {
  const rowSet = new Set<string>();
  let maxCol = 0;
  for (const w of wells) {
    const match = /^([A-Za-z]+)(\d+)$/.exec(w.position.trim());
    if (!match) continue;
    rowSet.add(match[1].toUpperCase());
    maxCol = Math.max(maxCol, Number(match[2]));
  }
  const rows = [...rowSet].sort();
  const columns = Array.from({ length: maxCol }, (_, i) => i + 1);
  return { rows, columns };
}

export const CONTROL_ROLES = ["control-pos", "control-neg", "calibrator", "blank"];
export const PATIENT_ROLES = ["patient", "replicate"];

export function isControlRole(role: string): boolean {
  return CONTROL_ROLES.includes(role);
}
export function isPatientRole(role: string): boolean {
  return PATIENT_ROLES.includes(role);
}
