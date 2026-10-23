// Phase 15 laboratory depth client (scope B — Laboratory completion).
// The single source of truth is the backend: backend/app/lab_depth.py
// (Test Manager + Panel Manager over the GOVERNED catalogue,
// terminology/lab-catalog), backend/app/lab_specimens.py (specimen
// aliquot derivation + family chain of custody) and
// backend/app/barcodes.py (label printing). Types for these payloads
// live here, not in api/types.ts.
//
// Draft compose NEVER mutates the published catalogue: the POSTs
// below write onto the catalogue DRAFT only, and publishing stays in
// System Studio. Compose is authorised server-side for the
// laboratory-director job role (published terminology/lab-governance)
// or admin/configurator — a 403 is an expected, renderable state.
import type { ApiClient } from "./client";
import type { DownloadedFile } from "./types";

export interface AliquotRules {
  count: number;
  volumeMl: number;
  container: string;
}

export interface SpecimenRequirements {
  specimen_type?: string | null;
  container?: string | null;
  required_aliquots?: number | null;
  volume_ml?: number | null;
}

export interface ReflexRule {
  id?: string;
  triggerCode?: string;
  when?: string;
  addCode?: string;
  note?: string | null;
}

export interface LabManagerTest {
  code: string;
  display?: string | null;
  active?: boolean;
  bench?: string | null;
  unit?: string | null;
  qualitative?: boolean;
  values?: string[] | null;
  reference_range?: unknown;
  aliquot_rules?: AliquotRules;
  aliquot_rules_source?: string;
  specimen_requirements?: SpecimenRequirements;
  reflex_rules?: ReflexRule[];
  panel?: boolean;
}

export interface LabManagerPanel {
  code: string;
  display?: string | null;
  active?: boolean;
  bench?: string | null;
  panel: true;
  components: Array<LabManagerTest | { code: string; display?: string | null; missing?: boolean }>;
  component_codes: string[];
  reflex_rules?: ReflexRule[];
  aliquot_rules?: AliquotRules;
  aliquot_rules_source?: string;
  specimen_requirements?: SpecimenRequirements;
}

export interface ManagerCatalog {
  catalog_version: number | null;
  config: { kind: string; key: string; publish_in: string };
  tests: LabManagerTest[];
  panels: LabManagerPanel[];
  reflex_rules: ReflexRule[];
  draft: { version: number; updated_by: string } | null;
}

export interface DraftComposeOutcome {
  draft: {
    kind: string;
    key: string;
    version: number;
    status: string;
    publish_via: string;
  };
  test?: Record<string, unknown>;
  panel?: Record<string, unknown>;
  reflex_rules_added?: ReflexRule[];
}

export interface ComposeTestInput {
  code: string;
  display: string;
  bench?: string;
  unit?: string;
  specimen_type?: string;
  container?: string;
  aliquot_rules?: AliquotRules;
}

export interface ComposePanelInput {
  code: string;
  display: string;
  components: string[];
  bench?: string;
  specimen_type?: string;
  aliquot_rules?: AliquotRules;
  reflex_rules?: ReflexRule[];
}

/** GET /lab/manager/catalog — published catalogue enriched with
 *  aliquot rules, panel composition, reflex rules and specimen /
 *  container requirements, plus the open draft's summary. */
export function getManagerCatalog(client: ApiClient): Promise<ManagerCatalog> {
  return client.request("/lab/manager/catalog");
}

/** POST /lab/manager/tests/draft — compose a test change onto the
 *  catalogue DRAFT only (403 for non-director, non-admin callers). */
export function composeTestDraft(
  client: ApiClient,
  input: ComposeTestInput,
): Promise<DraftComposeOutcome> {
  return client.request("/lab/manager/tests/draft", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** POST /lab/manager/panels/draft — compose a panel definition onto
 *  the catalogue DRAFT only (same authorisation as tests). */
export function composePanelDraft(
  client: ApiClient,
  input: ComposePanelInput,
): Promise<DraftComposeOutcome> {
  return client.request("/lab/manager/panels/draft", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

// ---------------------------------------------------------------------------
// Specimens: aliquot derivation, family chain of custody, add-on tests
// (backend/app/lab_specimens.py)
// ---------------------------------------------------------------------------

export interface SpecimenCustodyEvent {
  id: number;
  specimen_key: string;
  action: string;
  actor: string;
  location?: string | null;
  note?: string | null;
  is_break: boolean;
  break_reason?: string | null;
  at?: string | null;
}

export interface SpecimenNode {
  specimen_key: string;
  specimen_id: string;
  entity_ref: string;
  parent_key: string | null;
  root_key: string;
  derivation_type: string;
  sequence: number;
  volume_ml: number;
  remaining_volume_ml: number;
  container: string;
  status: string;
  order_id?: string | null;
  patient_ref?: string | null;
  organism_code?: string | null;
  barcode?: string | null;
  custody: SpecimenCustodyEvent[];
  children?: SpecimenNode[];
}

export interface SpecimenChain {
  root_key: string;
  requested_member: string;
  members: SpecimenNode[];
  total: number;
  tree: SpecimenNode;
  breaks: Array<SpecimenCustodyEvent & { specimen_key: string }>;
}

export interface CustodyBreakItem {
  root_key: string;
  specimen_key: string;
  action: string;
  reason?: string | null;
  actor: string;
  location?: string | null;
  at?: string | null;
}

export interface CustodyBreaks {
  items: CustodyBreakItem[];
  total: number;
  families: string[];
  family_count: number;
}

export interface SpecimenBarcodeItem {
  value: string;
  kind: string;
  entity_ref: string;
  specimen_key: string;
}

/** GET /lab/specimen-barcodes — every registered specimen/aliquot
 *  barcode (the Specimens tab's master list source). */
export function listSpecimenBarcodes(
  client: ApiClient,
): Promise<{ items: SpecimenBarcodeItem[]; total: number }> {
  return client.request("/lab/specimen-barcodes");
}

/** GET /lab/specimens/{identifier}/chain — the whole family tree
 *  (parent → children) with every member's custody timeline,
 *  resolvable from ANY member by id or barcode value. */
export function getSpecimenChain(
  client: ApiClient,
  identifier: string,
): Promise<SpecimenChain> {
  return client.request(
    `/lab/specimens/${encodeURIComponent(identifier)}/chain`,
  );
}

/** GET /lab/specimens/custody-breaks — the custody-break worklist. */
export function listCustodyBreaks(client: ApiClient): Promise<CustodyBreaks> {
  return client.request("/lab/specimens/custody-breaks");
}

/** POST /lab/specimens/{id}/derive — derive aliquots / splits /
 *  subcultures / isolates, each a child specimen with its own
 *  barcode, linked to the parent. */
export function deriveSpecimen(
  client: ApiClient,
  specimenId: string,
  input: {
    type: string;
    count: number;
    volume_ml: number;
    container?: string;
    organism_code?: string;
  },
): Promise<{ parent: SpecimenNode; children: SpecimenNode[]; total: number }> {
  return client.request(
    `/lab/specimens/${encodeURIComponent(specimenId)}/derive`,
    { method: "POST", body: JSON.stringify(input) },
  );
}

/** POST /lab/specimens/scan — append one custody scan event to a
 *  family member (identified by specimen id or barcode value). A
 *  scan whose required predecessor is missing is recorded AND
 *  flagged as a custody break by the server. */
export function scanSpecimen(
  client: ApiClient,
  input: { identifier: string; action: string; location?: string; note?: string },
): Promise<{ event: SpecimenCustodyEvent; specimen: SpecimenNode }> {
  return client.request("/lab/specimens/scan", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** POST /lab/specimens/{id}/add-tests — add one test onto an
 *  existing specimen (server validates status + remaining volume
 *  against the test's catalogue aliquot rules). */
export function addTestToSpecimen(
  client: ApiClient,
  specimenId: string,
  testCode: string,
): Promise<{
  addon: {
    id: number;
    specimen_key: string;
    test_code: string;
    required_volume_ml: number;
    order_id: string;
  };
  specimen: SpecimenNode;
}> {
  return client.request(
    `/lab/specimens/${encodeURIComponent(specimenId)}/add-tests`,
    { method: "POST", body: JSON.stringify({ test_code: testCode }) },
  );
}

// ---------------------------------------------------------------------------
// Label printing (backend/app/barcodes.py)
// ---------------------------------------------------------------------------

/** GET /barcodes/labels/{kind}/{entity_ref} — a print-ready label
 *  PDF for one specimen/aliquot (kind "specimen" | "aliquot"). */
export function printSpecimenLabel(
  client: ApiClient,
  kind: "specimen" | "aliquot",
  entityRef: string,
): Promise<DownloadedFile> {
  return client.requestBlob(`/barcodes/labels/${kind}/${entityRef}`);
}

/** GET /barcodes/labels/blood/{entity_ref} — the ISBT 128 blood-unit
 *  label PDF for one unit. */
export function printBloodLabel(
  client: ApiClient,
  entityRef: string,
): Promise<DownloadedFile> {
  return client.requestBlob(`/barcodes/labels/blood/${entityRef}`);
}

// ---------------------------------------------------------------------------
// Test detail (UX pass WS3b) — GET /lab/orders/{order_id}/test-detail
// (backend/app/lab.py `test_detail`). Types copied from that endpoint's
// return dict: order / steps / specimen / aliquots (_node_out from
// lab_specimens.py, reused as SpecimenNode above) / results / report /
// acknowledgements (_notification_out) / custody (_custody).
// ---------------------------------------------------------------------------

export interface LabTestDetailStep {
  key: string; // ordered|collected|received|plated|in_analysis|analysed|verified|resulted
  label: string;
  state: "done" | "current" | "pending" | string;
  at: string | null;
  by?: string | null;
  plate_id?: number;
}

export interface LabTestDetailOrder {
  id: string;
  status: string | null;
  code: string | null;
  display: string | null;
  patient_ref: string | null;
  authored_on: string | null;
  requester: string | null;
  priority: string | null;
}

export interface LabTestDetailSpecimen {
  id: string;
  status: string | null;
  accession: string | null;
  type: string | null;
  subject_ref: string | null;
  collected_at: string | null;
  received_at: string | null;
  in_analysis_at: string | null;
}

export interface LabTestDetailDelta {
  code: string;
  previous_value: number;
  current_value: number;
  change: number;
  change_pct: number | null;
  previous_at: string | null;
  window_hours: number | null;
}

export interface LabTestDetailResult {
  analyte: string | null;
  code: string | null;
  display: string | null;
  observation_id: string;
  value: string | number | null;
  unit: string | null;
  reference_range: { low: number | null; high: number | null; unit: string | null } | null;
  interpretation: string | null;
  interpretation_text: string | null;
  delta: LabTestDetailDelta | null;
  critical: boolean;
  status: string | null;
}

export interface LabTestDetailReport {
  report_id: string;
  status: string | null;
  verified_by: string | null;
  verified_at: string | null;
  released_by: string | null;
  released_at: string | null;
  effective_at: string | null;
  critical: boolean;
}

export interface LabTestDetailAcknowledgement {
  report_id: string;
  state: string; // pending | notified | acknowledged | not-required
  notified_to: string | null;
  read_back: boolean | null;
  notified_by: string | null;
  notified_at: string | null;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
}

export interface LabTestDetail {
  order: LabTestDetailOrder;
  steps: LabTestDetailStep[];
  specimen: LabTestDetailSpecimen | null;
  aliquots: SpecimenNode[];
  results: LabTestDetailResult[];
  report: LabTestDetailReport | null;
  acknowledgements: LabTestDetailAcknowledgement[];
  custody: {
    ordered_at: string | null;
    collected_at: string | null;
    received_at: string | null;
    in_analysis_at: string | null;
  };
}

/** GET /lab/orders/{order_id}/test-detail — the per-test drill-down. */
export function getLabTestDetail(
  client: ApiClient,
  orderId: string,
): Promise<LabTestDetail> {
  return client.request(
    `/lab/orders/${encodeURIComponent(orderId)}/test-detail`,
  );
}
