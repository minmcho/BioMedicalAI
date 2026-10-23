// FHIR-lite types matching the OpenCare backend contract
// (contracts/fhir-resources.md). Resources are open JSON objects; these
// interfaces cover the fields the web app actually reads.

export type Role = "admin" | "configurator" | "clinician" | "viewer" | "patient";

export interface CodeableConcept {
  text?: string;
  coding?: Array<{ system?: string; code?: string; display?: string }>;
}

export interface Reference {
  reference?: string;
  display?: string;
}

export interface FhirResource {
  resourceType: string;
  id?: string;
  meta?: { versionId?: string; profile?: string[] };
  [key: string]: unknown;
}

export interface Patient extends FhirResource {
  resourceType: "Patient";
  name?: Array<{ family?: string; given?: string[]; text?: string }>;
  identifier?: Array<{ system?: string; value?: string }>;
  gender?: string;
  birthDate?: string;
}

export interface Observation extends FhirResource {
  resourceType: "Observation";
  status?: string;
  code?: CodeableConcept;
  subject?: Reference;
  effectiveDateTime?: string;
  valueQuantity?: { value?: number; unit?: string };
  valueString?: string;
  valueCodeableConcept?: CodeableConcept;
  interpretation?: CodeableConcept[];
  basedOn?: Reference[];
  specimen?: Reference;
}

export interface Condition extends FhirResource {
  resourceType: "Condition";
  code?: CodeableConcept;
  subject?: Reference;
  clinicalStatus?: CodeableConcept;
}

export interface MedicationRequest extends FhirResource {
  resourceType: "MedicationRequest";
  status?: string;
  intent?: string;
  medicationCodeableConcept?: CodeableConcept;
  medicationReference?: Reference;
  subject?: Reference;
  dosageInstruction?: DosageInstruction[];
}

/** FHIR Dosage — the structured sig shape the chart sig builder
 *  emits (Wave A, CHART-B3). dose_quantity is read by the backend
 *  from doseAndRate[0].doseQuantity and the timing from
 *  timing.repeat (frequency per period periodUnit), exactly the
 *  shapes app.pharmacy.dose_quantity_of / doses_per_day_of parse. */
export interface DosageInstruction {
  text?: string;
  doseAndRate?: Array<{
    doseQuantity?: { value?: number; unit?: string; code?: string };
  }>;
  doseQuantity?: { value?: number; unit?: string; code?: string };
  route?: CodeableConcept;
  timing?: {
    repeat?: {
      frequency?: number;
      period?: number;
      periodUnit?: string;
      boundsDuration?: { value?: number; unit?: string; code?: string };
    };
    code?: CodeableConcept;
  };
  asNeededBoolean?: boolean;
  asNeededCodeableConcept?: CodeableConcept;
}

/** AllergyIntolerance — first-class FHIR resource (app.fhir).
 *  The chart allergies tab + banner read/write this shape; the
 *  NKDA record is one whose code coding carries code
 *  "no-known-allergy" (see AllergiesTab). */
export interface AllergyIntolerance extends FhirResource {
  resourceType: "AllergyIntolerance";
  clinicalStatus?: CodeableConcept;
  verificationStatus?: CodeableConcept;
  type?: string;
  category?: string[];
  criticality?: string;
  code?: CodeableConcept;
  patient?: Reference;
  recordedDate?: string;
  recorder?: Reference;
  onsetString?: string;
  reaction?: Array<{
    manifestation?: CodeableConcept[];
    description?: string;
    severity?: string;
  }>;
  note?: Array<{ text?: string }>;
}

export interface DiagnosticReport extends FhirResource {
  resourceType: "DiagnosticReport";
  status?: string;
  code?: CodeableConcept;
  subject?: Reference;
  effectiveDateTime?: string;
  conclusion?: string;
  basedOn?: Reference[];
  result?: Reference[];
  issued?: string;
}

export interface Procedure extends FhirResource {
  resourceType: "Procedure";
  status?: string;
  code?: CodeableConcept;
  subject?: Reference;
  performedDateTime?: string;
}

export interface Task extends FhirResource {
  resourceType: "Task";
  status?: string;
  intent?: string;
  description?: string;
  for?: Reference;
  encounter?: Reference;
  authoredOn?: string;
}

export interface QuestionnaireResponse extends FhirResource {
  resourceType: "QuestionnaireResponse";
  status?: string;
  questionnaire?: string;
  subject?: Reference;
  encounter?: Reference;
  authored?: string;
  item?: QuestionnaireAnswerItem[];
}

export interface Encounter extends FhirResource {
  resourceType: "Encounter";
  status?: string;
  class?: { code?: string; display?: string };
  subject?: Reference;
  period?: { start?: string; end?: string };
}

export interface AppointmentParticipant {
  actor?: Reference;
  status?: string;
  type?: CodeableConcept[];
}

export interface Appointment extends FhirResource {
  resourceType: "Appointment";
  status?: string;
  start?: string;
  end?: string;
  description?: string;
  participant?: AppointmentParticipant[];
}

export interface Bundle<T extends FhirResource = FhirResource> {
  resourceType: "Bundle";
  type: string;
  total?: number;
  entry?: Array<{ resource: T }>;
}

// ---- Phase 2: Lab LIS shapes — backend/app/lab.py ----
// The lab catalog and workflow are Studio configuration served by
// GET /lab/catalog; the rest mirror the /lab endpoints' JSON.

export interface ServiceRequest extends FhirResource {
  resourceType: "ServiceRequest";
  status?: string;
  intent?: string;
  code?: CodeableConcept;
  subject?: Reference;
  encounter?: Reference;
  specimen?: Reference[];
  authoredOn?: string;
  statusReason?: CodeableConcept;
}

export interface Specimen extends FhirResource {
  resourceType: "Specimen";
  status?: string;
  accessionIdentifier?: { system?: string; value?: string };
  type?: CodeableConcept;
  subject?: Reference;
  request?: Reference[];
  receivedDateTime?: string;
}

export interface LabReferenceRange {
  low?: number;
  high?: number;
  criticalLow?: number;
  criticalHigh?: number;
  unit?: string;
}

export interface LabDeltaCheck {
  kind: "percent" | "absolute";
  value: number;
}

export interface LabConceptAutoVerify {
  low: number;
  high: number;
  delta?: LabDeltaCheck;
}

export interface LabConcept {
  code: string;
  display: string;
  active?: boolean;
  unit?: string;
  panel?: boolean;
  components?: string[];
  referenceRange?: LabReferenceRange;
  /** Phase 6B — bench routing (catalog config): microbiology /
   *  pathology orders are worked on their own bench sections. */
  bench?: string;
  /** Phase 6B — qualitative concepts accept coded text values. */
  qualitative?: boolean;
  values?: string[];
  /** Depth pass — cytology concepts on the pathology bench report
   *  with the published cytology form, not the surgical synoptic. */
  cytology?: boolean;
  deltaCheck?: LabDeltaCheck;
  autoVerify?: LabConceptAutoVerify;
}

export interface LabCatalogPayload {
  name?: string;
  system?: string;
  concepts: LabConcept[];
  /** Phase 6B configuration blocks (all optional; absent blocks mean
   *  the feature is off under the published catalog). */
  rejectionCriteria?: Array<{ code: string; display: string; active?: boolean }>;
  collection?: { priorityOrder?: string[] };
  reflexRules?: Array<{
    id: string; analyte: string; when: string; threshold: number;
    addOrder: string; display?: string;
  }>;
  autoVerify?: { enabled?: boolean };
  bloodProducts?: Array<{ code: string; display: string }>;
  qc?: {
    blockOnFailedLatest?: boolean;
    blockOnMissingQc?: boolean;
    freshnessHours?: number | null;
    programme?: Record<string, unknown>;
  };
}

export interface WorkflowTransition {
  from: string;
  to: string;
  action?: string;
  /** Optional per-transition role restriction (Studio workflow config);
   *  absent = any role the endpoint otherwise allows. */
  roles?: string[];
}

export interface LabWorkflowPayload {
  states: string[];
  transitions: WorkflowTransition[];
  /** States reachable only through dedicated endpoints (server-enforced
   *  governance); the order queue offers no bare-transition buttons for
   *  them. */
  governedStates?: string[];
  specimenStates?: string[];
}

export interface LabCatalogResponse {
  catalog: LabCatalogPayload | null;
  catalog_version: number | null;
  workflow: LabWorkflowPayload | null;
  workflow_version: number | null;
  /** Phase 6B bench workflows (microbiology / pathology). */
  micro_workflow?: LabWorkflowPayload | null;
  micro_workflow_version?: number | null;
  pathology_workflow?: LabWorkflowPayload | null;
  pathology_workflow_version?: number | null;
}

/** Specimen chain-of-custody timestamps (Phase 6B): when the specimen
 *  was collected, received at the bench, and taken into analysis. */
export interface LabCustody {
  collected_at: string | null;
  received_at: string | null;
  in_analysis_at: string | null;
}

export interface LabOrderDetail {
  order: ServiceRequest;
  specimen: Specimen | null;
  reports: DiagnosticReport[];
  custody?: LabCustody;
}

export interface LabTransitionOutcome {
  order: ServiceRequest;
  specimen: Specimen | null;
}

export interface LabResultInput {
  code: string;
  /** A number for numeric concepts, a coded text value for concepts
   *  flagged qualitative in the published catalog (Phase 6B). */
  value: number | string;
  unit?: string;
}

export interface LabResultsOutcome {
  report: DiagnosticReport;
  observations: Observation[];
  critical: boolean;
}

export interface LabIngestOutcome {
  ingested: Array<{
    order_id: string;
    report_id: string;
    result_count: number;
    critical: boolean;
  }>;
  report_count: number;
  result_count: number;
}

export interface QcRun {
  id: number;
  analyte_code: string;
  control_material: string;
  expected_low: number;
  expected_high: number;
  measured_value: number;
  unit: string | null;
  passed: boolean;
  performed_by: string;
  created_at: string | null;
  /** Phase 6B QC programme: control level + Westgard-style flags +
   *  programme z-score, computed server-side at run creation. */
  level?: string | null;
  flags?: string[];
  z_score?: number | null;
}

export interface QcBlocker {
  analyte_code: string;
  /** The latest QC run for the analyte, or null when the block is a
   *  missing run (the catalog's QC policy decides which reasons block). */
  qc_run: QcRun | null;
  reason?: "failed-latest" | "missing-qc" | "stale-qc";
}

/** Critical-value notification state for a lab report (released
 *  critical reports only; tracked server-side, never inside the FHIR
 *  report body). */
export interface CriticalNotification {
  state: "not-required" | "pending" | "notified" | "acknowledged";
  notified_to: string | null;
  read_back: boolean | null;
  notified_by: string | null;
  notified_at: string | null;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
}

export interface VerificationResult {
  observation_id: string;
  code: string | null;
  display?: string;
  value?: number;
  /** Qualitative (coded text) value, Phase 6B. */
  value_text?: string;
  unit?: string;
  interpretation: string | null;
  status?: string;
}

/** A delta-check flag on a verification-queue result (Phase 6B):
 *  informational — it never blocks verification or release. */
export interface LabDeltaFlag {
  code: string;
  previous_value: number;
  current_value: number;
  change: number;
  change_pct: number | null;
}

export interface LabAmendmentInfo {
  /** "DiagnosticReport/<id>" this report amends, when it is a successor. */
  amends: string | null;
  /** "DiagnosticReport/<id>" that amended this report, when superseded. */
  amended_by: string | null;
  reason: string | null;
}

export interface VerificationItem {
  report: DiagnosticReport;
  order_id: string | null;
  order_status: string | null;
  stage: string;
  critical: boolean;
  qc_blocked: QcBlocker[];
  notification: CriticalNotification;
  results: VerificationResult[];
  /** Phase 6B: delta-check flags + amendment state. */
  delta_flags?: LabDeltaFlag[];
  amended?: boolean;
  amendment?: LabAmendmentInfo | null;
}

export interface VerificationQueue {
  items: VerificationItem[];
  total: number;
}

export interface QcStatus {
  analyte_code: string;
  blocked: boolean;
  latest: QcRun | null;
}

// ---- Phase 6B: laboratory benches + operations —
// backend/app/lab_benches.py + the Phase 6B additions in lab.py ----

export interface CollectionWorklistOrder {
  order_id: string;
  code: string | null;
  display: string | null;
  priority: string;
  authored_on: string | null;
  custody: LabCustody & { ordered_at: string | null };
}

export interface CollectionWorklist {
  total_orders: number;
  groups: Array<{
    patient_ref: string;
    location: string;
    highest_priority: string;
    orders: CollectionWorklistOrder[];
  }>;
}

export interface MicroTerminology {
  organisms: { concepts?: Array<{ code: string; display: string; active?: boolean }> } | null;
  organisms_version: number | null;
  antibiotics: { concepts?: Array<{ code: string; display: string; active?: boolean }> } | null;
  antibiotics_version: number | null;
}

export interface BloodUnit {
  unit_id: string;
  product_type: string;
  blood_group: string;
  expiry_date: string;
  status: string;
  patient_ref: string | null;
  order_id: string | null;
  issued_at: string | null;
  created_at: string | null;
}

export interface BloodHold {
  id: number;
  patient_ref: string;
  unit_id: string;
  reason: string;
  active: boolean;
  created_by?: string | null;
  created_at?: string | null;
  cleared_by?: string | null;
  cleared_at?: string | null;
}

export interface EqaRecord {
  id: number;
  event: string;
  analyte_code: string;
  reported_value: number;
  acceptable_low: number;
  acceptable_high: number;
  passed: boolean;
  recorded_by: string;
  created_at: string | null;
}

export interface LabInstrument {
  instrument_id: string;
  name: string;
  bench: string;
  status: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface InstrumentLogEntry {
  id: number;
  instrument_id: string;
  kind: string;
  note: string;
  performed_by: string;
  created_at: string | null;
}

export interface LeveyJennings {
  analyte_code: string;
  control_material: string;
  count: number;
  mean: number | null;
  sd: number | null;
  limits: {
    plus_2sd: number; minus_2sd: number; plus_3sd: number; minus_3sd: number;
  } | null;
  runs: Array<{
    id: number;
    measured_value: number;
    level: string | null;
    flags: string[];
    passed: boolean;
    z_score: number | null;
    created_at: string | null;
  }>;
}

export interface TatBucket {
  count: number;
  order_to_release_hours: { p50: number | null; p90: number | null };
  received_to_release_hours: { p50: number | null; p90: number | null };
}

export interface TatStats {
  generated_at: string;
  source: string;
  benches: Record<string, TatBucket>;
  analytes: Record<string, TatBucket>;
}

// Studio (module-pack lifecycle) shapes — backend/app/studio.py
export interface PackManifest {
  name: string;
  version: string;
  description?: string;
  modules?: Array<{ id: string; name: string; enabled: boolean }>;
  forms?: Array<{ id: string; name: string; fields: string[] }>;
  api_endpoints?: Array<{ method: string; path: string; enabled: boolean }>;
  [key: string]: unknown;
}

export interface ModulePack {
  id: number;
  name: string;
  version: string;
  manifest: PackManifest;
}

export type InstallationStatus =
  | "installed_draft"
  | "approved"
  | "attached"
  | "enabled"
  | "disabled";

export interface PackInstallation {
  id: number;
  pack_id: number;
  pack_name: string;
  version: string;
  status: InstallationStatus;
  installed_by: string;
}

export type StudioAction = "approve" | "attach" | "enable" | "disable";

// One entry of the hash-chained audit log — GET /studio/audit
// (backend/app/studio.py). Covers pack lifecycle, Studio config writes
// and clinical writes in a single chain.
export interface AuditEntry {
  id: number;
  created_at: string | null;
  actor: string;
  action: string;
  entity: string;
  entry_hash: string;
  prev_hash: string;
}

// Studio customization engine (versioned configuration) —
// backend/app/studio_config.py + app/configstore.py
export type ConfigKind =
  | "form"
  | "workflow"
  | "terminology"
  | "navigation"
  | "branding"
  | "permissions"
  | "cohort"
  | "integration"
  | "orderset"
  // Phase 10 — Studio-managed agents, rules/automation, dashboards and
  // regulatory packs are governed config kinds like everything above.
  | "agent"
  | "rule"
  | "dashboard"
  | "regulatory_pack"
  // Phase 14 — pipeline definitions are a governed config kind.
  | "pipeline"
  // Platform pass WS10 (Studio full parity) — AI provider registry
  // and the endpoint/tool declaration registries.
  | "ai-provider"
  | "endpoint"
  | "tool"
  // Phase 16 — supervised agent workflows, governed knowledge
  // documents, MCP server registrations and analytics views.
  | "agentflow"
  | "knowledge"
  | "mcp_server"
  | "analytics_view";

export type ConfigStatus = "draft" | "published" | "superseded" | "disabled";

export interface StudioConfigObject {
  id: number;
  kind: ConfigKind;
  key: string;
  version: number;
  status: ConfigStatus;
  payload: Record<string, unknown>;
  created_by: string;
  updated_by: string;
  created_at: string | null;
  updated_at: string | null;
}

// ---- Phase 3: population health + warehouse — backend/app/pophealth.py ----
// Aggregate-only shapes: a stratum count is either a number or the
// suppression marker "<5" (the raw small count never leaves the server).

export interface CohortLabRule {
  system?: string;
  code: string;
  comparator: "<" | "<=" | ">" | ">=" | "==";
  threshold: number;
  windowDays?: number;
}

export interface CohortDefinition {
  name: string;
  description?: string;
  conditionCodes?: Array<{ system?: string; code: string }>;
  lab?: CohortLabRule;
  smallCellThreshold?: number;
  [key: string]: unknown;
}

export interface RegistryListEntry {
  key: string;
  name: string | null;
  definition: CohortDefinition;
}

export interface StratumCell {
  value: string;
  count: number | string; // number, or "<5" when suppressed
  suppressed: boolean;
}

export interface RegistrySummary {
  registry: string;
  name: string | null;
  member_count: number;
  lab: {
    code: string;
    comparator: string;
    threshold: number;
    window_days: number | null;
    members_with_current_result: number;
    pct_in_range: number | null;
  } | null;
  stratification: {
    by_gender: StratumCell[];
    by_age_band: StratumCell[];
    as_of: string;
  };
  small_cell_threshold: number;
}

export interface ExploreInput {
  conditionCode?: string;
  labCode?: string;
  comparator?: "<" | "<=" | ">" | ">=" | "==";
  threshold?: number;
}

export interface ExploreResult {
  filters: {
    conditionCode: string | null;
    labCode: string | null;
    comparator: string | null;
    threshold: number | null;
  };
  count: number | string; // number, or "<5" when suppressed
  suppressed: boolean;
}

export interface CoverageTable {
  rows: number;
  mapped: number;
  unmapped: number;
  coverage_pct: number | null;
}

export interface WarehouseStatus {
  outbox: { pending: number; processed: number; dead_letter: number };
  dead_letters: Array<{
    event_id: number;
    resource: string;
    version: number;
    attempts: number;
    error: string | null;
  }>;
  warehouse: Record<string, number>;
  mapping_coverage: {
    tables: Record<string, CoverageTable>;
    total_rows: number;
    mapped_rows: number;
    unmapped_rows: number;
    coverage_pct: number | null;
  };
}

export interface EtlRunStats {
  processed: number;
  no_target: number;
  dead_letter: number;
  by_type: Record<string, number>;
}

// ---- Phase 4A: billing / claims — backend/app/billing.py ----
// Claims are FHIR Claim resources. Line prices come from the published
// Studio fee schedule; a line whose code is not scheduled is priced 0 and
// carries priced=false (never dropped). Lifecycle: draft -> ready ->
// submitted -> paid / denied -> appealed (once, with a note) -> submitted.

export interface Money {
  value?: number;
  currency?: string;
}

export interface ClaimLineExtension {
  url: string;
  valueBoolean?: boolean;
  valueString?: string;
  valueInteger?: number;
}

export interface ClaimItem {
  sequence?: number;
  productOrService?: CodeableConcept;
  quantity?: { value?: number };
  unitPrice?: Money;
  net?: Money;
  extension?: ClaimLineExtension[];
}

export interface ClaimHistoryEntry {
  from: string | null;
  to: string;
  at: string;
  by: string;
  note?: string | null;
}

export interface Claim extends FhirResource {
  resourceType: "Claim";
  status?: string;
  use?: string;
  patient?: Reference;
  encounter?: Reference[];
  provider?: Reference;
  insurer?: Reference;
  created?: string;
  diagnosis?: Array<{ sequence?: number; diagnosisCodeableConcept?: CodeableConcept }>;
  item?: ClaimItem[];
  total?: Money;
  /** Parsed server-side from the claim's extension (API responses only). */
  appealCount?: number;
  /** Parsed server-side from supportingInfo (API responses only). */
  statusHistory?: ClaimHistoryEntry[];
}

// ---- Phase 4A: pharmacy — backend/app/pharmacy.py ----

export interface MedicationDispense extends FhirResource {
  resourceType: "MedicationDispense";
  status?: string;
  medicationCodeableConcept?: CodeableConcept;
  subject?: Reference;
  authorizingPrescription?: Reference[];
  quantity?: { value?: number; unit?: string };
  whenHandedOver?: string;
}

export interface MedicationAdministration extends FhirResource {
  resourceType: "MedicationAdministration";
  status?: string;
  medicationCodeableConcept?: CodeableConcept;
  subject?: Reference;
  effectiveDateTime?: string;
  request?: Reference;
}

export interface StockItem {
  code: string;
  display?: string;
  system?: string;
  active: boolean;
  stock_tracked: boolean;
  quantity: number | null;
}

export interface DispenseQueueItem {
  request: MedicationRequest;
  medication_code: string | null;
  in_formulary: boolean;
  stock_tracked: boolean | null;
  stock_quantity: number | null;
}

export interface DispenseOutcome {
  dispense: MedicationDispense;
  medication_request: MedicationRequest;
  stock_after: number | null;
}

export interface FormularyResponse {
  formulary: {
    name?: string;
    system?: string;
    concepts: Array<{
      code: string;
      display: string;
      active?: boolean;
      stockTracked?: boolean;
      initialStock?: number;
    }>;
  } | null;
  version: number | null;
}

// ---- Phase 11: pharmacy department depth — backend/app/pharmacy_ops.py ----
// Inventory by lot + location with PAR levels, procurement with DSCSA
// traceability, controlled substances, sterile compounding, the
// pharmacist verification + DUR queues, barcode payloads and the
// five-rights BCMA scan. Thresholds (PAR, SLA minutes, BUD hours,
// barcode prefixes) are published pharmacy-ops Studio config on the
// server; these interfaces mirror the endpoints' JSON.

export interface PharmacyLocation {
  code: string;
  label: string;
}

export interface PharmacyLotItem {
  medication_code: string;
  location: string;
  lot_number: string;
  expiry_date: string | null;
  quantity: number;
  days_to_expiry: number | null;
  expired: boolean;
  expiring_soon: boolean;
  par_level: number | null;
  reorder_point: number | null;
}

export interface PharmacyInventoryResponse {
  items: PharmacyLotItem[];
  locations: PharmacyLocation[];
  total: number;
}

export interface LowStockItem {
  medication_code: string;
  location: string;
  on_hand: number;
  par_level: number | null;
  reorder_point: number | null;
  suggested_order: number | null;
}

export interface InventoryWorklist {
  low_stock: LowStockItem[];
  expiring: PharmacyLotItem[];
  expired: PharmacyLotItem[];
  expiry_warning_days: number;
}

export interface AdjustStockResult {
  lot: PharmacyLotItem;
  stock_after: number | null;
}

export interface PurchaseOrderLine {
  medication_code: string;
  quantity: number;
  received_quantity: number;
}

export interface PurchaseOrder {
  po_id: string;
  supplier: string;
  status: string;
  lines: PurchaseOrderLine[];
  note: string | null;
  created_by: string;
  created_at: string | null;
}

export interface TraceabilityEntry {
  entry_id: string;
  event: string;
  medication_code: string;
  lot_number: string | null;
  serial: string | null;
  expiry_date: string | null;
  quantity: number | null;
  reference: string | null;
  detail: { serials?: string[]; supplier?: string; location?: string };
  actor: string;
  created_at: string | null;
}

export interface AdcRestockItem {
  medication_code: string;
  display: string | null;
  location: string;
  on_hand: number;
  par_level: number;
  suggested_quantity: number;
  recent_dispenses: number;
}

export interface RenalView {
  crcl_ml_min: number | null;
  threshold_ml_min: number | null;
  flagged: boolean;
}

export interface VerificationQueueItem {
  order_id: string;
  patient_ref: string;
  medication_code: string | null;
  medication_display: string | null;
  priority: string | null;
  status: string | null;
  authored_on: string | null;
  wait_minutes: number | null;
  sla_minutes: number | null;
  sla_breached: boolean;
  formulary_status: string;
  restriction_note: string | null;
  in_formulary: boolean;
  allergy_conflict: { allergy_id: string; display: string } | null;
  monitoring_blockers: Array<{ problem: string }>;
  monitoring_warnings: Array<{ problem: string }>;
  renal: RenalView;
}

export interface RuleFiring {
  rule: string;
  version: number;
  event: string;
  action: string;
  detail: Record<string, unknown>;
}

export interface VerificationOutcome {
  verification: {
    order_id: string;
    medication_code: string | null;
    verified_by: string;
    formulary_status: string;
    restriction_acknowledged: boolean;
    crcl_ml_min: number | null;
  };
  rule_firings: RuleFiring[];
}

export interface DurSignal {
  type: string;
  detail: string;
}

export interface DurQueueItem {
  order_id: string;
  patient_ref: string;
  medication_code: string | null;
  medication_display: string | null;
  signals: DurSignal[];
}

export interface InteractionStatus {
  status: string;
  configured: boolean;
  source: string | null;
  message: string;
}

export interface DurQueueResponse {
  items: DurQueueItem[];
  total: number;
  interaction_checking: InteractionStatus;
}

export interface InteractionCheckResult {
  status: string;
  configured: boolean;
  source: string | null;
  message: string;
  medication_codes: string[];
  pairs_checked: number;
  findings: unknown[];
}

export interface CsBookItem {
  medication_code: string;
  display: string | null;
  schedule: string | null;
  location: string;
  expected: number;
  unresolved_discrepancies: number;
  balanced: boolean;
  last_count: CsCount | null;
}

export interface CsWasteRecord {
  waste_id: string;
  medication_code: string;
  location: string;
  lot_number: string;
  quantity: number;
  reason: string;
  wasted_by: string;
  witness: string;
  created_at: string | null;
}

export interface CsBookResponse {
  items: CsBookItem[];
  open_discrepancies: CsCount[];
  recent_waste: CsWasteRecord[];
}

export interface CsCount {
  count_id: string;
  medication_code: string;
  location: string;
  expected: number;
  counted: number;
  variance: number;
  counter: string;
  witness: string;
  status: string;
  resolution_reason: string | null;
  resolved_by: string | null;
  created_at: string | null;
}

export interface CsWasteResult {
  waste_id: string;
  medication_code: string;
  location: string;
  lot_number: string;
  quantity: number;
  lot_after: number;
}

export interface CompoundIngredient {
  medication_code?: string;
  name?: string;
  quantity?: string;
  lot_number: string;
}

export interface CompoundBatch {
  batch_id: string;
  preparation_name: string;
  category: string;
  storage: string;
  compounded_at: string | null;
  beyond_use_at: string | null;
  ingredients: CompoundIngredient[];
  patient_ref: string | null;
  preparer: string;
  verifier: string | null;
  verified_at: string | null;
  status: string;
}

export interface CompoundingQueueResponse {
  items: CompoundBatch[];
  total: number;
  categories: Record<string, { label?: string; budHours?: Record<string, number> }>;
}

export interface WristbandPayload {
  payload: string;
  patient_id: string;
  dob: string;
}

export interface MedLabelPayload {
  payload: string;
  dispense_id: string;
  lot: string;
}

export interface BarcodeParseResult {
  type: string;
  patient_id?: string;
  dob?: string;
  dispense_id?: string;
  lot?: string;
}

export interface BcmaCheck {
  right: string;
  result: string;
  detail: string;
}

export interface BcmaOutcome {
  administration: MedicationAdministration;
  bcma: {
    checks: BcmaCheck[];
    override: { reason: string; rights: string[] } | null;
  };
}

export interface PatientDispenseItem {
  dispense_id: string;
  status: string | null;
  medication_code: string | null;
  medication_display: string | null;
  lot_number: string | null;
  quantity: number | null;
  when_handed_over: string | null;
  request_ref: string | null;
}

// ---- Phase 4B: specialty boards — backend/app/boards.py ----
// ED tracking states, surgery case states and the room/modality lists are
// Studio configuration on the server; these interfaces mirror the board
// endpoints' JSON.

export type EdState = "waiting" | "triage" | "in-treatment" | "disposition";

export interface EdBoardEntry {
  encounter: Encounter;
  patient_ref?: string | null;
  arrived_at: string;
  wait_minutes: number;
  acuity?: number | null;
  chief_complaint?: string | null;
  state?: string;
}

export interface EdBoard {
  generated_at: string;
  counts: Record<string, number>;
  groups: Record<string, EdBoardEntry[]>;
}

export interface EdAdmitOutcome {
  ed_encounter: Encounter;
  inpatient_encounter: Encounter;
}

export interface ImagingStudy extends FhirResource {
  resourceType: "ImagingStudy";
  status?: string;
  identifier?: Array<{ system?: string; value?: string }>;
  modality?: Array<{ system?: string; code?: string; display?: string }>;
  subject?: Reference;
  basedOn?: Reference[];
  started?: string;
  description?: string;
  numberOfSeries?: number;
  numberOfInstances?: number;
}

export interface ConfigConcept {
  code: string;
  display?: string;
  active?: boolean;
}

export interface ModalitiesResponse {
  modalities: ConfigConcept[];
  version?: number | null;
}

export interface RadiologyStudyItem {
  study: ImagingStudy;
  study_uid?: string | null;
  viewer_url?: string | null;
  report?: { id: string; status?: string } | null;
  /** Always false in v1: studies are metadata-only, no pixel data. */
  has_images: boolean;
}

export interface RadiologyStudiesResponse {
  items: RadiologyStudyItem[];
  total: number;
}

export interface RadiologyStudyDetail extends RadiologyStudyItem {
  reports: DiagnosticReport[];
}

export interface RoomsResponse {
  rooms: ConfigConcept[];
  version?: number | null;
}

export interface CaseTimelineEvent {
  event: string;
  state?: string;
  at?: string;
  by?: string;
}

export interface SurgeryCaseEntry {
  case: Procedure;
  appointment: Appointment;
  room?: string | null;
  state?: string;
  start?: string;
  end?: string;
  patient_ref?: string | null;
  /** Phase 6: server-timestamped perioperative timeline. */
  timeline?: CaseTimelineEvent[];
}

export interface SurgeryBoardRoom {
  code: string;
  display?: string | null;
  cases: SurgeryCaseEntry[];
}

export interface SurgeryBoard {
  date: string;
  rooms: SurgeryBoardRoom[];
  total: number;
}

export interface SurgeryCaseOutcome {
  case: Procedure;
  appointment: Appointment;
  room?: string | null;
  state: string;
}

// ---- Phase 5: orders, documentation, MAR — backend/app/orders.py +
// app/documentation.py. Order types / note types / order sets are
// published Studio config read at runtime; the shapes below mirror the
// config payloads, not code constants. ----

export interface OrderTypeConcept {
  code: string;
  display?: string;
  resourceType?: string;
  requiredFields?: string[];
  timingDefaults?: { frequencyHours?: number; windowHours?: number };
  active?: boolean;
}

export interface OrderTypesResponse {
  order_types: { name?: string; system?: string; concepts: OrderTypeConcept[] };
  version?: number | null;
  workflow?: unknown;
  workflow_version?: number | null;
  /** Wave A (CHART-B3): published terminology/medication-sig payload
   *  from GET /orders/types — the real backend response shape. */
  medication_sig?: MedicationSigVocabulary | null;
  medication_sig_version?: number | null;
}

/** Governed sig vocabulary (terminology/medication-sig). Frequencies
 *  carry timesPerDay plus the FHIR timing.repeat triple the backend
 *  dose maths parses; as-needed has timesPerDay 0 and no repeat. */
export interface SigVocabConcept {
  code: string;
  display?: string;
  active?: boolean;
}

export interface SigFrequency extends SigVocabConcept {
  timesPerDay: number;
  repeat?: { frequency: number; period: number; periodUnit: string };
}

export interface MedicationSigVocabulary {
  name?: string;
  system?: string;
  routes: SigVocabConcept[];
  frequencies: SigFrequency[];
  units: SigVocabConcept[];
}

export interface OrderSetItem {
  type: string;
  code: string;
  display?: string;
}

export interface OrderSet {
  key: string;
  name?: string;
  orders: OrderSetItem[];
}

export interface OrderSetsResponse {
  sets: OrderSet[];
  total: number;
}

export type PlacedOrder = MedicationRequest | ServiceRequest;

export interface PlaceOrderOutcome {
  order: PlacedOrder;
  order_type: string;
  warnings: string[];
}

export interface PlaceOrderSetOutcome {
  orders: PlacedOrder[];
  total: number;
  warnings: string[];
}

export interface OrdersListResponse {
  items: PlacedOrder[];
  total: number;
}

export type MarDoseStatus = "given" | "due" | "missed" | "scheduled";

export interface MarDose {
  order_id: string;
  medication_code?: string | null;
  medication_display?: string | null;
  due_at: string;
  window_start: string;
  window_end: string;
  administration_id: string | null;
  status: MarDoseStatus;
}

export interface MarDueResponse {
  patient: string;
  from: string;
  to: string;
  doses: MarDose[];
  administrations: { id: string; order_id: string | null; effective_at?: string }[];
  summary: Record<MarDoseStatus, number>;
}

// Notes: DocumentReference + the Composition-lite content the API
// decorates onto the resource (noteType / noteContent are API-added,
// not stored FHIR fields).

export interface NoteContent {
  templateForm: string | null;
  questionnaireResponseId: string | null;
  sections: unknown[];
  text: string | null;
}

export interface ClinicalNote extends FhirResource {
  resourceType: "DocumentReference";
  status?: string;
  docStatus?: string;
  type?: { coding?: { system?: string; code?: string; display?: string }[]; text?: string };
  subject?: { reference?: string };
  date?: string;
  author?: { display?: string }[];
  relatesTo?: { code?: string; target?: { reference?: string } }[];
  noteType?: string;
  noteContent?: NoteContent;
}

export interface NotesListResponse {
  items: ClinicalNote[];
  total: number;
}

export interface NoteTypeConcept {
  code: string;
  display?: string;
  templateForm?: string;
  active?: boolean;
}

export interface NoteTypesResponse {
  note_types: { name?: string; concepts: NoteTypeConcept[] };
}

// Published form (Questionnaire) definitions — Studio config kind=form.

export interface FormAnswerOption {
  valueCoding?: { system?: string; code?: string; display?: string };
  valueString?: string;
}

export interface FormItem {
  linkId: string;
  text?: string;
  type: string;
  required?: boolean;
  /** UX pass: a "text" item carrying "rich": true in its governed
   *  form definition renders the shared rich-text editor instead
   *  of a plain textarea (seeded for note-template prose). */
  rich?: boolean;
  item?: FormItem[];
  answerOption?: FormAnswerOption[];
}

export interface FormDefinition {
  resourceType?: string;
  title?: string;
  item?: FormItem[];
}

/** One QuestionnaireResponse item as the API accepts it. */
export interface QuestionnaireAnswerItem {
  linkId: string;
  answer?: Record<string, unknown>[];
  item?: QuestionnaireAnswerItem[];
}

export interface FlowsheetAnswerValue {
  value: unknown;
  unit?: string | null;
}

export interface FlowsheetEntry {
  id: string;
  authored?: string | null;
  answers: Record<string, FlowsheetAnswerValue>;
}

export interface FlowsheetView {
  form: { title?: string; items: { linkId: string; text?: string; type?: string; required?: boolean }[] };
  entries: FlowsheetEntry[];
  total: number;
  patient: string;
}

// ---- Phase 6: inpatient operations — backend/app/inpatient.py.
// Locations, bed statuses, dispositions and ADT event labels are
// published Studio config read at runtime; the shapes below mirror
// the config payloads + live bed state, not code constants. ----

export interface LocationItem {
  code: string;
  display?: string | null;
  type: "facility" | "unit" | "room" | "bed" | string;
  parent?: string | null;
  active: boolean;
  status?: string | null;
  patient_ref?: string | null;
  encounter_id?: string | null;
}

export interface LocationsResponse {
  locations: LocationItem[];
  version?: number | null;
  bed_statuses: string[];
}

export interface BedEntry {
  code: string;
  display?: string | null;
  room?: string | null;
  bed?: string | null;
  unit?: string | null;
  unit_display?: string | null;
  status: string;
  patient_ref?: string | null;
  encounter_id?: string | null;
  admitted_at?: string | null;
  los_days?: number | null;
}

export interface CensusUnit {
  code: string;
  display?: string | null;
  beds_total: number;
  available: number;
  occupied: number;
  cleaning: number;
  blocked: number;
  occupancy_pct: number;
  beds: BedEntry[];
  [status: string]: unknown;
}

export interface ExpectedDischarge {
  encounter_id: string;
  patient_ref?: string | null;
  bed_code?: string | null;
  target_date?: string | null;
  disposition?: string | null;
  barriers?: string | null;
  admitted_at?: string | null;
  los_days?: number | null;
}

export interface ExpectedDischargesResponse {
  date: string;
  discharges: ExpectedDischarge[];
  total: number;
}

export interface Census {
  generated_at: string;
  units: CensusUnit[];
  totals: {
    beds_total: number;
    available: number;
    occupied: number;
    cleaning: number;
    blocked: number;
    occupancy_pct: number;
    [status: string]: unknown;
  };
  expected_discharges_today: ExpectedDischargesResponse;
}

export interface AdtEvent {
  id: number;
  event_type: string;
  event_display?: string | null;
  encounter_id?: string | null;
  patient_ref?: string | null;
  from_location?: string | null;
  to_location?: string | null;
  actor?: string | null;
  detail: Record<string, unknown>;
  at?: string | null;
}

export interface AdtEventsResponse {
  events: AdtEvent[];
  total: number;
}

export interface DischargePlan {
  targetDate?: string;
  disposition?: string;
  barriers?: string;
  checklistResponseId?: string;
  updatedBy?: string;
  updatedAt?: string;
}

export interface AdmissionDetail {
  encounter: Encounter;
  bed_code?: string | null;
  bed?: BedEntry | null;
  admitted_at?: string | null;
  los_days?: number | null;
  discharge_plan?: DischargePlan | null;
}

export interface AdmitOutcome {
  encounter: Encounter;
  bed: BedEntry;
  event: AdtEvent;
}

export interface DispositionsResponse {
  dispositions: ConfigConcept[];
  version?: number | null;
}

export interface CarePlanResponse {
  tasks: Task[];
  total: number;
}

export interface SurgeryCaseRecord {
  case: SurgeryCaseEntry;
  preop_checklist?: {
    meta: Record<string, unknown>;
    response?: QuestionnaireResponse | null;
  } | null;
  anaesthesia?: {
    meta: Record<string, unknown> & { summary?: Record<string, unknown> };
    response?: QuestionnaireResponse | null;
  } | null;
  anaesthesia_vitals: FlowsheetView & { form_key: string };
}

// ---- Phase 7: access (app.access) — referrals, authorisations,
// eligibility, waitlist. Payers / services / slot types and the three
// lifecycles are Studio configuration on the server; these interfaces
// mirror the /access endpoints' JSON. ----

export interface Referral extends FhirResource {
  resourceType: "ServiceRequest";
  status?: string;
  code?: CodeableConcept;
  subject?: Reference;
  requester?: { display?: string };
  taskId?: string | null;
  referringOrganization?: string | null;
  payer?: string | null;
}

export interface ReferralDetail {
  referral: Referral;
  task: Task | null;
  authorisations: PriorAuth[];
}

export interface PriorAuth {
  id: string;
  auth_number: string;
  patient: string;
  payer: string;
  service_code: string;
  referral_id?: string | null;
  status: string;
  approved_units?: number | null;
  used_units: number;
  valid_from?: string | null;
  valid_to?: string | null;
  note?: string | null;
  created_by?: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface PayersResponse {
  payers: {
    name?: string;
    system?: string;
    concepts: Array<{
      code: string;
      display: string;
      active?: boolean;
      eligibility?: { copay?: number; coinsurancePercent?: number };
      authRequiredServices?: string[];
    }>;
  } | null;
  version: number | null;
}

/** An eligibility determination — ALWAYS a local configuration check
 *  (basis "local-config"); this system has no live payer connection. */
export interface EligibilityCheckResult {
  id: string;
  patient: string;
  payer: string;
  payer_display?: string;
  service_code?: string | null;
  service_date: string;
  covered: boolean;
  reason: string;
  copay?: number | null;
  coinsurance_percent?: number | null;
  basis: string;
  disclaimer: string;
  checked_by: string;
  checked_at: string;
}

export interface WaitlistEntry {
  id: string;
  patient: string;
  slot_type: string;
  priority: number;
  status: string;
  note?: string | null;
  offered_appointment_id?: string | null;
  offered_at?: string | null;
  appointment_id?: string | null;
  created_by?: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface WaitlistOfferResult {
  entry: WaitlistEntry | null;
  appointment_id: string;
  slot_type: string;
  reason?: string;
}

export interface WaitlistAcceptResult {
  entry: WaitlistEntry;
  appointment: Appointment;
}

// ---- Phase 7: revenue completion (app.revenue) — HIM/coding, 835
// remittance, denials workqueue, statements + recorded payments. ----

export interface CodingCode {
  system?: string;
  code: string;
  display?: string;
}

export interface CodingRecord {
  encounter_id: string;
  patient: string;
  diagnoses: CodingCode[];
  procedures: CodingCode[];
  coded_by: string;
  coded_at?: string | null;
  updated_at?: string | null;
}

export interface CodingQueueItem {
  encounter_id: string;
  patient?: string | null;
  encounter_status?: string;
  period_start?: string | null;
  period_end?: string | null;
  signed_notes: number;
  reason: string;
}

export interface RemittanceBatchSummary {
  id: string;
  check_number: string;
  payer: string;
  total_amount: number;
  posted: number;
  exceptions: number;
  received_by: string;
  received_at?: string | null;
}

export interface RemittancePostedLine {
  claim_id: string;
  outcome: string;
  from_status?: string;
  to_status: string;
  paid: number;
  patient_responsibility: number;
  balance_after: number;
}

export interface RemittanceExceptionItem {
  claim_ref: string;
  reason: string;
  detail?: Record<string, unknown>;
  batch_id?: string;
  recorded_at?: string | null;
}

export interface RemittanceIngestResult {
  batch: RemittanceBatchSummary;
  posted: RemittancePostedLine[];
  exceptions: RemittanceExceptionItem[];
}

export interface ClaimFinancials {
  total: number;
  paid: number;
  adjustments: number;
  patient_responsibility: number;
  patient_paid: number;
  patient_due: number;
  balance: number;
  remittance_count: number;
  last_outcome?: string | null;
}

export interface DenialReason {
  group?: string;
  reason?: string;
  amount?: number;
  level?: string;
  line_code?: string | null;
  category: string;
  display?: string | null;
  check_number?: string;
}

export interface DenialWorkState {
  assignee?: string | null;
  notes: Array<{ at: string; by: string; text: string }>;
  outcome?: string | null;
  outcome_claim_id?: string | null;
  updated_by?: string | null;
}

export interface DenialItem {
  claim: Claim & { financials: ClaimFinancials };
  kind: string;
  reasons: DenialReason[];
  work: DenialWorkState;
}

export interface DenialsResponse {
  items: DenialItem[];
  total: number;
  outcomes: string[];
}

export interface StatementClaimLine extends ClaimFinancials {
  claim_id: string;
  status?: string;
  created?: string;
}

export interface PatientStatement {
  patient: string;
  generated_at: string;
  claims: StatementClaimLine[];
  totals: {
    total: number;
    paid: number;
    adjustments: number;
    patient_responsibility: number;
    patient_paid: number;
    patient_due: number;
    balance: number;
  };
  disclaimer: string;
}

export interface PatientPaymentRecord {
  id: string;
  claim_id: string;
  patient: string;
  amount: number;
  method: string;
  recorded_by: string;
  recorded_at?: string | null;
  disclaimer?: string;
}

// ---------------------------------------------------------------------------
// Phase 8 — Interoperability (interface engine admin)
// ---------------------------------------------------------------------------

export interface InteropEndpoint {
  name: string;
  host: string;
  port: number;
  messageTypes: string[];
  enabled: boolean;
  receivingApplication?: string;
  receivingFacility?: string;
  queue: { pending: number; sent: number; dead_letter: number };
}

export interface InteropEndpointsResponse {
  config: { kind: string; key: string; version: number | null };
  facility: { sendingApplication: string; sendingFacility: string };
  retry: { maxAttempts: number; backoffSeconds: number };
  endpoints: InteropEndpoint[];
}

export interface InteropMessage {
  direction: "inbound" | "outbound";
  id: number;
  message_type: string;
  status: string;
  patient_ref?: string | null;
  endpoint?: string;
  peer?: string;
  control_id?: string;
  ack_code?: string | null;
  last_error?: string | null;
  detail?: string;
  attempts?: number;
  replay_count?: number;
  source?: { kind: string; id: string };
  created_at?: string | null;
  sent_at?: string | null;
  raw?: string;
  attempt_history?: {
    attempt_no: number;
    outcome: string;
    detail: string;
    at?: string | null;
  }[];
}

export interface InteropMessagesResponse {
  items: InteropMessage[];
  count: number;
}

export interface InteropDeadLettersResponse {
  items: InteropMessage[];
  count: number;
}

export interface DispatchStats {
  attempted: number;
  sent: number;
  failed: number;
  dead_lettered: number;
  skipped: number;
}

// ---- Phase 9: quality measures (app.quality) — measures are published
// Studio config (kind `measure`) evaluated by the Phase 3 pophealth
// evaluator over the warehouse. Counts arrive as numbers, or "<5" when
// small-cell suppressed; the rate is null whenever either count is
// suppressed (a rate would leak the hidden numerator).

export interface QualityMeasureEntry {
  key: string;
  name: string | null;
  title: string | null;
  description: string | null;
  definition_version: number;
  small_cell_threshold: number;
  definition: Record<string, unknown>;
}

export interface QualityMeasureResult {
  measure: string;
  name: string | null;
  title: string | null;
  description: string | null;
  denominator: number | string; // number, or "<5" when suppressed
  numerator: number | string; // number, or "<5" when suppressed
  rate: number | null;
  suppressed: boolean;
  small_cell_threshold: number;
  as_of: string;
}

// ---- Phase 9: research cohort workbench (app.pophealth additions) ----
// Snapshots pin a registry's member count at a moment; the export is
// de-identified aggregates ONLY (definition + summary + notice) — the
// API has no patient-level export, by design.

export interface CohortSnapshot {
  id: number;
  cohort_key: string;
  member_count: number;
  definition_version: number;
  taken_by: string;
  taken_at: string | null;
}

export interface CohortSnapshotsResponse {
  cohort: string;
  snapshots: CohortSnapshot[];
  total: number;
}

export interface CohortExport {
  registry: string;
  definition: CohortDefinition;
  definition_version: number;
  summary: RegistrySummary;
  exported_by: string;
  notice: string;
}

// ---- Phase 9: secure messaging (app.engagement) — FHIR Communication
// threads per patient. A decorated message is the Communication render
// plus threadId / senderRole / text / read / mine conveniences.

export interface PortalMessage {
  id: string;
  resourceType: "Communication";
  status?: string;
  sent?: string;
  threadId: string;
  senderRole: string;
  text: string;
  read: boolean;
  mine: boolean;
  [key: string]: unknown;
}

export interface MessageThreadSummary {
  thread_id: string;
  patient: string | null;
  topic: string | null;
  message_count: number;
  unread_count: number;
  last_message_at: string | null;
  last_text: string | null;
  last_sender_role: string | null;
}

export interface MessageThreadDetail {
  thread_id: string;
  patient: string | null;
  messages: PortalMessage[];
  total: number;
}

// ---- Phase 9: telehealth + self-scheduling (app.engagement) ----
// The workspace's join_url is rendered from the organisation's
// PUBLISHED provider template — configuration data. OpenCare records
// the visit; it does not provide video (the disclaimer says so).

export interface TelehealthWorkspace {
  appointment: FhirResource;
  patient: string | null;
  provider_name: string | null;
  join_url: string | null;
  disclaimer: string | null;
  intake_form: string;
  intake_form_definition: FormDefinition | null;
  intake_submitted: boolean;
  intake_response_id: string | null;
  note_type: string;
}

export interface SelfSchedulingConfig {
  auto_confirm: boolean;
  slot_types: string[];
}

export interface AppointmentRequestOutcome {
  appointment: FhirResource;
  auto_confirmed: boolean;
  status: string;
}

// ---- Phase 10: agents (app.agents), rules (app.rules), compliance
// ---- (app.compliance), privacy / break-the-glass (app.privacy) ----
// Agents are governed Studio config (kind=agent). A published agent
// still cannot run until separately enabled; with no model route the
// runtime answers an honest 503 — the UI surfaces that verbatim.

export interface AgentRosterEntry {
  key: string;
  version: number;
  description: string;
  model: string;
  model_configured: boolean;
  modalities: { text?: boolean; image?: boolean; audio?: boolean };
  enabled: boolean;
  /** C-AI governance (backend/app/ai_governance.py): rows gain these
   *  over time — always optional, code defensively. `risk_tier` is the
   *  contract spelling; `riskTier` is accepted as a fallback. */
  risk_tier?: string | null;
  riskTier?: string | null;
  governance?: {
    statementStatus?: string | null;
    recordStatus?: string | null;
    [key: string]: unknown;
  } | null;
}

export interface AgentsResponse {
  agents: AgentRosterEntry[];
  runtime: {
    url: string;
    reachable: boolean;
    model_configured?: boolean;
    detail?: string;
  };
}

export interface AgentRunRecord {
  run_id: string;
  agent: string;
  version: number;
  actor: string;
  patient_ref: string | null;
  input_hash: string;
  output: string;
  model: string | null;
  disposition: "pending" | "accepted" | "rejected";
  disposition_by: string | null;
  created_at: string | null;
}

// ---- C-AI: clinical AI governance (/ai-governance) ----
// Contract shapes for the governance surface in the Studio
// Validation + ADK tabs. Statement `fields` is the §4.1 template of
// the AI governance framework, kept open (index signature) because
// the register stores the human-authored document as submitted.

export type CaiRiskTier = "T1" | "T2" | "T3";

export interface CaiTierRequirement {
  minOverallPass: number;
  minSliceFloor: number;
  maxSliceDisparity: number;
  statementValidityMonths: number;
  approvals: string[];
  monitoringCadence: string;
}

export interface CaiTierRequirements {
  tiers: Partial<Record<CaiRiskTier, CaiTierRequirement>> & Record<string, CaiTierRequirement>;
  source?: string;
}

export interface CaiStatementApproval {
  approver: string;
  kind: string;
  conditions?: string | null;
  decidedAt?: string | null;
}

export interface CaiStatementCurrency {
  status: "current" | "expired" | "suspended" | "stale" | "missing" | string;
  reason: string;
}

export interface CaiStatement {
  statementId: string;
  version: number;
  agentKey: string;
  agentConfigVersion?: number | null;
  modelRoute?: string | null;
  riskTier: string;
  owner: string;
  status: string;
  author?: string;
  fields: Record<string, unknown>;
  approvals: CaiStatementApproval[];
  reviewBy?: string | null;
  approvedAt?: string | null;
  currency: CaiStatementCurrency;
}

export interface CaiAgentGovernanceStatus {
  agentKey: string;
  tier?: string | null;
  killSwitchActive?: boolean;
  enabled?: boolean;
  validation?: AgentValidationStatusLike | null;
  statement?: { status: string; statementId?: string | null; reason?: string } | null;
  governance?: Record<string, unknown> | null;
  runtimeGate?: { allowed: boolean; reason: string } | null;
}

/** The existing agent-validation status shape, referenced loosely
 *  from the governance status payload. */
export interface AgentValidationStatusLike {
  status?: string;
  reason?: string;
  [key: string]: unknown;
}

export interface CaiDisclosure {
  agentKey: string;
  attributes: Array<{ attribute: string; value: string; source: string }>;
  generatedFrom?: Record<string, unknown>;
}

export interface CaiWorklistItem {
  agentKey: string;
  owner: string;
  tier: string;
  statementStatus: string;
  statementDaysToExpiry: number | null;
  recordStatus: string;
  recordDaysToExpiry: number | null;
  dueState: string;
}

export interface CaiKillSwitch {
  active: boolean;
  reason?: string | null;
  engagedBy?: string | null;
  engagedAt?: string | null;
}

export interface CaiIncident {
  eventId: string;
  reporter: string;
  category: string;
  harmLevel: string;
  description: string;
  status: string;
  createdAt: string | null;
}

export interface ComplianceRequirement {
  id: number;
  body: string;
  citation: string;
  title: string;
  control: string;
  implementation_ref: string;
  evidence_source: string;
  created_by: string;
  updated_by: string;
}

export interface ComplianceEvidence {
  requirement_id: number;
  source: string;
  kind: "audit" | "config" | "reference" | "none";
  entries?: Array<Record<string, unknown>>;
  total?: number;
  config?: { kind: string; key: string; version: number; status: string } | null;
  note?: string;
}

export interface BreakGlassPolicy {
  duration_minutes: number;
  justification_window_hours: number;
  review_expectation: string;
  source: string;
}

export interface BreakGlassItem {
  access_id: string;
  patient_ref: string;
  actor: string;
  reason: string;
  opened_at: string | null;
  expires_at: string | null;
  active: boolean;
  justification_due_at: string | null;
  overdue: boolean;
  reviewed: boolean;
  review_outcome: string | null;
}

export interface ReviewQueueResponse {
  items: BreakGlassItem[];
  total: number;
  overdue: number;
  policy: BreakGlassPolicy;
}

export interface RulesRegistry {
  events: string[];
  actions: string[];
  signal_ops: string[];
}

/** A published `dashboard` config payload (kind=dashboard). Widgets
 *  reference existing read endpoints; the generic view renders them.
 *  Phase 13 extends the renderer with chart displays (line / bar /
 *  gauge / table / big-number) alongside the Phase 10 total / count /
 *  value: chart widgets read the endpoint's `items` list (or a FHIR
 *  Bundle's `entry`) and plot `series` fields against `labelField`;
 *  gauge thresholds are HIGHER-IS-BETTER (value >= warn is fine,
 *  below critical is critical). */
export interface DashboardWidgetConfig {
  id: string;
  title: string;
  endpoint: string;
  display:
    | "total"
    | "count"
    | "value"
    | "big-number"
    | "line"
    | "bar"
    | "gauge"
    | "table";
  field?: string;
  series?: Array<{ field: string; label?: string }>;
  labelField?: string;
  columns?: Array<{ field: string; title: string }>;
  max?: number;
  thresholds?: { warn?: number; critical?: number };
  unit?: string;
}

export interface DashboardConfig {
  title: string;
  widgets: DashboardWidgetConfig[];
  /** Phase 13: the audience this dashboard was composed for
   *  (clinician / nurse / unit-manager / department-head / executive). */
  audience?: string;
  roles?: string[];
}

// ---- Phase 12: care-continuum operations (/continuum) ----
// Every threshold these shapes carry (ESI targets, SLA minutes, risk
// weights) is computed server-side from the published
// terminology/continuum-ops config; the client only renders it.

export interface ContinuumErEntry {
  encounter: FhirResource;
  patient_ref: string | null;
  arrived_at: string;
  wait_minutes: number;
  acuity: number | null;
  chief_complaint: string | null;
  state: string;
  target_minutes: number | null;
  target_breached: boolean;
  queue: "fast-track" | "main";
}

export interface ErOpsBoard {
  generated_at: string;
  groups: Record<string, ContinuumErEntry[]>;
  entries: ContinuumErEntry[];
  queues: { fast_track: ContinuumErEntry[]; main: ContinuumErEntry[] };
  counts: {
    waiting: number; triage: number; "in-treatment": number;
    disposition: number; total: number; fast_track: number;
    target_breaches: number;
  };
  diversion: string;
  surge_level: string;
}

export interface BoardingItem {
  encounter_id: string;
  patient_ref: string | null;
  source: "ed" | "direct";
  origin_encounter_id: string | null;
  requested_unit: string | null;
  admitted_at: string | null;
  wait_minutes: number;
}

export interface BoardingBoard {
  items: BoardingItem[];
  total: number;
  ed_total: number;
  oldest_wait_minutes: number;
}

export interface ActivationMilestone {
  code: string;
  display: string;
  target_minutes: number;
  recorded_at: string | null;
  elapsed_minutes: number | null;
  breached: boolean;
}

export interface Activation {
  id: string;
  encounter_id: string;
  patient_ref: string | null;
  pathway: string;
  pathway_display: string;
  target_minutes: number;
  milestones: ActivationMilestone[];
  status: "open" | "closed";
  outcome: string | null;
  started_by: string;
  started_at: string;
  closed_at: string | null;
  elapsed_minutes: number;
  target_breached: boolean;
}

export interface CallbackItem {
  report_id: string;
  patient_ref: string | null;
  code_text: string | null;
  status: string;
  age_minutes: number;
  sla_minutes: number;
  breached: boolean;
  order_released: boolean;
}

export interface CallbackQueue {
  items: CallbackItem[];
  completed: Array<Record<string, unknown>>;
  total: number;
  breached: number;
}

export interface NurseLoad {
  nurse: string;
  patients: number;
  acuity_load: number;
  overloaded: boolean;
}

export interface AssignmentBoard {
  items: Array<{
    encounter_id: string; patient_ref: string | null; unit: string | null;
    bed: string | null; nurse: string; acuity: number;
  }>;
  nurses: NurseLoad[];
  unassigned: Array<{ encounter_id: string; patient_ref: string | null;
                      unit: string | null; bed: string | null }>;
  total: number;
  max_acuity_per_nurse: number;
}

export interface ReadinessItem {
  encounter_id: string;
  patient_ref: string | null;
  unit: string | null;
  bed: string | null;
  target_date: string | null;
  disposition: string | null;
  barriers: string | null;
  checklist_done: boolean;
  teachback_done: boolean;
  followup_booked: boolean;
  missing: string[];
  ready: boolean;
}

export interface DischargeReadiness {
  items: ReadinessItem[];
  total: number;
  ready: number;
}

export interface EvsItem {
  id: string;
  bed_code: string;
  encounter_id: string | null;
  status: "open" | "in-progress" | "completed";
  released_at: string;
  started_at: string | null;
  completed_at: string | null;
  completed_by: string | null;
  waiting_minutes: number;
  turnaround_minutes: number | null;
}

export interface ClinicSessionState {
  code: string;
  display: string;
  start_time: string;
  slot_minutes: number;
  capacity: number;
  overbook_limit: number;
  booked: number;
  overbooked: number;
}

/** A session template as published in terminology/continuum-ops
 *  (the templates endpoint returns the config documents themselves). */
export interface ClinicSessionTemplate {
  code: string;
  display: string;
  startTime: string;
  slotMinutes: number;
  capacity: number;
  overbookLimit: number;
}

export interface ClinicDay {
  date: string;
  sessions: ClinicSessionState[];
  appointments: FhirResource[];
  /** The day's booked appointments awaiting check-in (UX audit
   *  F-1) — the front desk's check-in worklist. */
  arrivals: Array<{
    appointment_id: string; patient_ref: string | null;
    start: string | null; status: string; session: string | null;
  }>;
  rooming_queue: Array<{
    appointment_id: string; patient_ref: string | null;
    session: string | null; room: string | null; roomed_at: string | null;
  }>;
}

export interface NoShowRiskItem {
  appointment_id: string;
  patient_ref: string | null;
  history_total: number;
  history_missed: number;
  history_attended: number;
  miss_rate: number | null;
  high_risk: boolean;
  basis: string;
}

export interface ReferralLoopItem {
  id: string;
  patient_ref: string | null;
  service_code: string | null;
  status: string;
  stage: "sent" | "scheduled" | "awaiting-report" | "closed";
  age_days: number;
  stale: boolean;
  report_back_at: string | null;
}

export interface ContinuumInboxItem {
  type: "task" | "message" | "result" | "callback";
  id: string;
  title: string;
  patient_ref: string | null;
  created_at: string | null;
  age_minutes: number;
  sla_minutes: number | null;
  breached: boolean;
  task_id?: string;
  report_id?: string;
}

export interface ContinuumInbox {
  items: ContinuumInboxItem[];
  total: number;
  breached: number;
  counts: Record<string, number>;
}

export interface CareGapItem {
  gap_code: string;
  display: string;
  registry: string;
  patient_ref: string;
  reason: "missing" | "stale" | "out-of-range";
  latest_value: number | null;
  latest_at: string | null;
  outreach_task_id: string | null;
}

export interface CallQueueItem {
  encounter_id: string;
  patient_ref: string | null;
  discharged_at: string;
  hours_since_discharge: number;
  state: "scheduled" | "due" | "overdue";
  window: { start_hours: number; end_hours: number };
}

export interface ReadmissionRiskItem {
  encounter_id: string;
  patient_ref: string | null;
  discharged_at: string | null;
  score: number;
  factors: Record<string, number>;
}

export interface HahBoard {
  eligible: string[];
  census: Array<{
    encounter_id: string; patient_ref: string | null;
    status: string; open_visit_tasks: number;
  }>;
  total: number;
  note: string;
}

export interface CommandOverview {
  generated_at: string;
  census: {
    units: Array<{ code: string; display: string; beds_total: number;
                   occupied: number; available: number; cleaning: number;
                   blocked: number; occupancy_pct: number;
                   beds: unknown[] }>;
    totals: { beds_total: number; occupied: number; available: number;
              cleaning: number; blocked: number; occupancy_pct: number };
  };
  ed: { counts: Record<string, number> };
  boarding: BoardingBoard;
  boarding_count: number;
  ed_boarding_count: number;
  oldest_boarding_minutes: number | null;
  bed_requests: number;
  accepted_transfers: number;
  expected_discharges_today: {
    date: string; discharges: Array<Record<string, unknown>>; total: number;
  };
  projection: {
    projected_occupied_end_of_day: number;
    projected_occupancy_pct: number;
    basis: string;
  };
  discharges_today: { total: number; before_noon: number };
  transport_open: number;
  diversion: string;
  surge_level: string;
  scope?: {
    region: string | null; location: string | null;
    department: string | null; unit: string | null;
    unit_codes: string[];
  };
}

export interface TransferRequestItem {
  id: string;
  patient_name: string;
  patient_age: number | null;
  patient_sex: string | null;
  referring_facility: string;
  referring_clinician: string | null;
  reason: string;
  service_line: string | null;
  urgency: string;
  contact: string | null;
  status: "pending" | "accepted" | "declined";
  decision_reason: string | null;
  decided_by: string | null;
  decided_at: string | null;
  admission_task_id: string | null;
  created_by: string;
  created_at: string;
}

export interface TransportItem {
  id: string;
  patient_ref: string | null;
  from_location: string;
  to_location: string;
  priority: string;
  reason: string | null;
  status: "requested" | "assigned" | "in-transit" | "completed" | "cancelled";
  transporter: string | null;
  requested_by: string;
  created_at: string;
  updated_at: string;
}

export interface SafetyEventItem {
  id: string;
  reporter: string;
  patient_ref: string | null;
  unit: string | null;
  category: string;
  harm_level: string;
  description: string;
  confidential: boolean;
  status: "reported" | "triaged" | "in-review" | "closed";
  rca_required: boolean;
  rca_status: "open" | "closed" | null;
  occurred_at: string;
  created_at: string;
}

export interface SafetyEventDetail extends SafetyEventItem {
  messages: Array<{
    id: number; author: string; author_role: "reporter" | "reviewer";
    body: string; created_at: string;
  }>;
}

export interface SafetySummary {
  total: number;
  open: number;
  rca_open: number;
  by_status: Record<string, number>;
  by_harm: Record<string, number>;
  by_category: Record<string, number>;
  by_unit: Record<string, number>;
}

// ---- Phase 13: people & data platform (/people, /reports) ----
// Workforce windows, shift templates, dashboard audiences and the
// export threshold are all published Studio config server-side; the
// client renders what the server computed.

/** One level of a resolved placement (Organisation & Registration
 *  Placement pass): the code plus its published display. */
export interface PlacementRef {
  code: string;
  display?: string;
}

export interface PlacementView {
  region: PlacementRef | null;
  location: PlacementRef | null;
  department: PlacementRef | null;
  unit: PlacementRef | null;
}

export interface StaffMember {
  staff_id: string;
  username: string | null;
  full_name: string;
  job_role: string;
  unit: string | null;
  /** Flat placement codes (Organisation pass); unit stays the
   *  long-standing field above. */
  region?: string | null;
  location?: string | null;
  department?: string | null;
  service_line: string | null;
  status: string;
  audience: string | null;
  email?: string | null;
  phone?: string | null;
  credential_count?: number;
}

/** PUT /people/staff/{id} — every field optional; the server
 *  validates against the published workforce config like create. */
export interface StaffUpdate {
  full_name?: string;
  job_role?: string;
  unit?: string | null;
  service_line?: string | null;
  status?: string;
  email?: string | null;
  phone?: string | null;
  audience?: string | null;
}

export interface StaffCredential {
  credential_id: string;
  staff_id: string;
  staff_name?: string;
  type: string;
  number: string | null;
  issued_on: string | null;
  expires_on: string;
  days_to_expiry: number | null;
  status: "valid" | "expiring" | "expired" | "unknown";
  alert_window?: boolean;
}

export interface CredentialWorklist {
  items: StaffCredential[];
  total: number;
  expired: number;
  alert_window_days: number;
  worklist_window_days: number;
}

export interface RosterShift {
  shift_id: string;
  staff_id: string;
  staff_name?: string;
  unit: string;
  date: string;
  template: string;
  starts_at: string;
  ends_at: string;
  status: string;
}

export interface OnCallEntry {
  staff_id: string;
  staff_name: string;
  role_on_call: string;
  starts_at: string;
  ends_at: string;
}

export interface OnCallNow {
  now: string;
  services: Array<{
    service_line: string;
    primary: OnCallEntry | null;
    backup: OnCallEntry | null;
  }>;
  total: number;
  covered: number;
}

export interface HomeDashboard {
  audience: string | null;
  basis: string;
  staff_id: string | null;
  dashboard_key: string | null;
  dashboard: DashboardConfig | null;
}

// ---- Home & Module UX pass: GET /home/cards (backend app/home_cards.py) ----
// One live indicator per staff module that has a REAL provider.
// Modules without a provider are ABSENT from the response — the Home
// card then shows purpose + "Open" only, never an invented number.
// Status is always text (status_text) + tone, never colour alone.

export type HomeCardTone = "ok" | "warn" | "bad" | "info" | "neutral";

export interface HomeCard {
  module: string;
  value: string | number;
  label: string;
  status_text: string;
  tone: HomeCardTone;
  timer_text?: string;
}

export interface HomeCardsResponse {
  cards: HomeCard[];
}

export interface WorkforceConfig {
  units: Array<{ code: string; display?: string; serviceLine?: string; minOnShift?: number }>;
  serviceLines: Array<{ code: string; display?: string }>;
  concepts?: Array<{ code: string; display?: string; active?: boolean }>;
  shiftTemplates: Array<{ code: string; display?: string; start: string; end: string }>;
  credentialTypes: Array<{ code: string; display?: string }> | string[];
  expiry: { worklistWindowDays: number; alertWindowDays: number };
  audienceDashboards?: Record<string, string>;
  audienceByRole?: Record<string, string>;
  [key: string]: unknown;
}

// ---- Front Door pass: accounts, registration, unit-first home ----
// Accounts govern identity, role, unit and status. Passwords are NOT
// part of an account: production sign-in credentials live in the
// identity provider (OIDC/Keycloak); in dev mode the server issues a
// one-time dev token at creation (returned once, stored hashed).

export interface UserAccount {
  account_id: number;
  username: string;
  full_name: string;
  role: Role;
  unit: string | null;
  service_line: string | null;
  email: string | null;
  phone: string | null;
  staff_id: string | null;
  patient_ref: string | null;
  status: "active" | "disabled" | string;
  /** Resolved org placement (Organisation pass); null when the
   *  account is not placed. */
  placement?: PlacementView | null;
  has_dev_token: boolean;
  created_by: string | null;
  updated_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AccountInput {
  username: string;
  full_name: string;
  role: Role;
  unit?: string | null;
  service_line?: string | null;
  email?: string | null;
  phone?: string | null;
  staff_id?: string | null;
  patient_ref?: string | null;
}

export interface AccountUpdate {
  full_name?: string;
  role?: Role;
  unit?: string | null;
  service_line?: string | null;
  email?: string | null;
  phone?: string | null;
  staff_id?: string | null;
  patient_ref?: string | null;
}

/** POST /accounts response: the account plus, in dev mode only, the
 *  one-time dev token (never readable again afterwards). */
export interface AccountCreated extends UserAccount {
  dev_token?: string;
  dev_token_note?: string;
}

export interface RegistrationInput {
  family: string;
  given: string;
  birthDate: string;
  gender: string;
  phone?: string;
  email?: string;
  identifiers?: Array<{ system: string; value: string }>;
  address?: {
    line?: string;
    city?: string;
    state?: string;
    postalCode?: string;
    country?: string;
  };
  emergency_contact?: { name?: string; phone?: string };
  confirm_duplicate?: boolean;
}

export interface DuplicateCandidate {
  id: string;
  name: string;
  birthDate?: string;
  gender?: string;
  active: boolean;
  identifiers: Array<{ system?: string; value?: string }>;
}

/** POST /registration/patients: the created FHIR Patient body plus
 *  the assigned MRN and whether duplicates were confirmed. */
export interface RegistrationResult extends Patient {
  mrn: string;
  duplicates_confirmed: boolean;
}

export interface DuplicateConflict {
  detail: string;
  candidates: DuplicateCandidate[];
}

/** One row of the unit-first home lists (my unit / my patients). */
export interface HomePatientItem {
  encounter_id: string;
  patient_ref: string | null;
  patient_id: string | null;
  patient_name: string | null;
  bed: string | null;
  attending_staff_id: string | null;
  attending_name: string | null;
  admitted_at: string | null;
  los_days: number | null;
  discharge_ready: boolean;
}

export interface MyUnitPatients {
  unit: string | null;
  unit_display: string | null;
  items: HomePatientItem[];
  total: number;
}

export interface MyPatients {
  staff_id: string | null;
  items: HomePatientItem[];
  total: number;
}

export interface FrontdeskTodayItem {
  appointment_id: string;
  start: string | null;
  patient_ref: string | null;
  patient_name: string | null;
  session: string | null;
  status: string | null;
  roomed: boolean;
}

export interface FrontdeskToday {
  date: string;
  appointments_total: number;
  awaiting_checkin: number;
  awaiting_room: number;
  waitlist_open: number;
  registrations_today: number;
  items: FrontdeskTodayItem[];
}

export interface LocationsDraftStatus {
  published_version: number | null;
  draft: { version: number; created_by: string | null; updated_by: string | null } | null;
}

export type LocationDraftOp =
  | { op: "add-region"; code: string; display: string }
  | { op: "add-department"; code: string; display: string; location: string }
  | { op: "add-unit"; code: string; display: string; department?: string }
  | { op: "add-bed"; code: string; display: string; unit: string; room?: string; bed?: string }
  | { op: "rename"; code: string; display: string }
  | { op: "retire"; code: string };

export interface LocationsDraftResult {
  version: number;
  status: string;
  operations_applied: number;
  note: string;
}

export interface ReportDefinition {
  key: string;
  title: string;
  description?: string;
  source: string;
  parameters: Array<{
    name: string;
    type: "string" | "number" | "date" | "choice";
    required?: boolean;
    default?: string | number;
    choices?: string[];
    label?: string;
  }>;
  formats: string[];
  version: number;
}

export interface ExportDataset {
  key: string;
  title: string;
  description: string;
}

export interface ImportProfile {
  key: string;
  entity_type: string;
  description?: string;
  columns: string[];
  version: number;
}

export interface ImportValidation {
  profile: string;
  entity_type: string;
  rows_total: number;
  valid: boolean;
  errors: Array<{ row: number; field: string; reason: string }>;
  preview: Array<Record<string, unknown>>;
}

/** A downloaded file from the report/document/export endpoints. */
export interface DownloadedFile {
  blob: Blob;
  filename: string;
  rows: number | null;
}

// ---------------------------------------------------------------------------
// Phase 14 — devices, IoT, data lake & pipelines (backend app/devices.py +
// app/pipelines.py + app/lake.py). Every feed in this deployment is
// simulated or recorded; the `feed_label`/`source_label` fields carry
// that label from the server and the pages render it verbatim.
// ---------------------------------------------------------------------------

export interface DeviceCalibration {
  status: "ok" | "due-soon" | "overdue" | "unknown" | "not-required";
  due: string | null;
  days_remaining: number | null;
}

export interface DeviceItem {
  device_key: string;
  name: string;
  device_type: string;
  location: string;
  serial: string;
  status: string;
  hl7_sender: string | null;
  mqtt_topic_key: string | null;
  calibration_due: string | null;
  maintenance_due: string | null;
  last_calibrated_at: string | null;
  calibration: DeviceCalibration;
  blocked_reason: string | null;
  feed_label: string;
}

export interface DevicesResponse {
  items: DeviceItem[];
  total: number;
  overdue: number;
}

export interface DeviceTypeConfig {
  type: string;
  label?: string;
  calibrationIntervalDays?: number | null;
  maintenanceIntervalDays?: number | null;
  ranges?: Record<string, { low?: number; high?: number; unit?: string }>;
  ownerRole?: string;
}

export interface DevicesConfigResponse {
  devices: {
    deviceTypes?: DeviceTypeConfig[];
    mllpGate?: {
      enabled?: boolean;
      onUnknownDevice?: string;
      onCalibrationOverdue?: string;
      senderMap?: Record<string, string>;
    };
    vitalsThresholds?: Record<string, { low?: number; high?: number; unit?: string }>;
    calibrationWorklistWindowDays?: number;
    [key: string]: unknown;
  };
  mqtt_configured: boolean;
  feed_label: string;
}

export interface FeedsStatus {
  feed_label: string;
  note: string;
  devices: { total: number; overdue: number; blocked: number };
  mllp_gate: {
    enabled: boolean;
    on_unknown_device: string | null;
    on_calibration_overdue: string | null;
  };
  mqtt: {
    configured: boolean;
    state: string;
    broker_url?: string | null;
    counts?: Record<string, number>;
    note?: string;
  };
  lake_entries: number;
  quarantine_held: number;
  dead_letters: number;
}

export interface DeviceReading {
  id: number;
  device_key: string;
  metric: string;
  value: number;
  unit: string;
  recorded_at: string | null;
  received_at: string | null;
  source: string;
  source_label: string;
  excursion: boolean;
  raw_entry_id: number | null;
}

export interface LakeEntry {
  id: number;
  payload_type: string;
  content_hash: string;
  source: string;
  source_label: string;
  device_key: string | null;
  interface: string | null;
  patient_ref: string | null;
  disposition: string;
  supersedes_id: number | null;
  received_at: string | null;
  payload_bytes: number;
  payload?: string;
  curated_links?: Array<{ curated_ref: string; link_kind: string }>;
}

export interface LakeResponse {
  items: LakeEntry[];
  total_entries: number;
  by_disposition: Record<string, number>;
}

export interface LineageResponse {
  curated_ref: string;
  total: number;
  raw_entries: Array<LakeEntry & {
    link_kind?: string;
    device?: { device_key: string; name?: string; device_type?: string } | null;
  }>;
}

export interface QuarantineItem {
  id: number;
  source: string;
  reason: string;
  detail: Record<string, unknown>;
  raw_entry_id: number | null;
  pipeline_run_id: string | null;
  status: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  created_at: string | null;
}

export interface PipelineDefinition {
  key: string;
  version: number;
  name: string | null;
  description: string | null;
  source: string | null;
  schedule: string | null;
  enabled: boolean;
  steps: Array<{ type: string; name?: string; checks: number }>;
  last_run: { run_id: string; status: string } | null;
}

export interface PipelineStepRun {
  step_index: number;
  step_type: string;
  status: string;
  attempts: number;
  rows_in: number;
  rows_out: number;
  idempotency_key: string;
  detail: Record<string, unknown>;
  error: string | null;
}

export interface PipelineRun {
  run_id: string;
  pipeline_key: string;
  pipeline_version: number;
  trigger: string;
  window_start: string | null;
  window_end: string | null;
  status: string;
  actor: string;
  detail: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
  steps: PipelineStepRun[];
}

export interface PipelineDeadLetter {
  id: number;
  pipeline_key: string;
  run_id: string;
  step_index: number;
  step_type: string;
  error: string;
  detail: Record<string, unknown>;
  status: string;
  created_at: string | null;
}

export interface DedupCandidate {
  id: number;
  patient_a: string;
  patient_b: string;
  score: number;
  reasons: Record<string, string>;
  status: string;
  reviewed_by: string | null;
  review_note: string | null;
  created_at: string | null;
}


// ---------------------------------------------------------------------------
// Depth pass — laboratory completion (virology trends, send-outs,
// POCT programme, post-analytical governance)
// ---------------------------------------------------------------------------

export interface LabTrendPoint {
  observation_id: string;
  value: number;
  unit?: string | null;
  effective?: string | null;
  log10?: number | null;
}

export interface LabTrends {
  patient: string;
  code: string;
  display?: string;
  bench?: string;
  points: LabTrendPoint[];
  count: number;
}

export interface SendoutLabRef {
  code?: string;
  display?: string;
  tat_hours?: number | null;
}

export interface SendoutItem {
  order_id: string;
  order_status?: string;
  code: string;
  display?: string;
  patient?: string;
  reference_lab: SendoutLabRef;
  sendout: {
    status: string;
    lab?: string;
    manifest_id?: string;
    dispatched_at?: string;
    returned_at?: string;
    tracking_ref?: string | null;
  };
}

export interface SendoutQueue {
  items: SendoutItem[];
  total: number;
  laboratories: Array<{
    code?: string;
    display?: string;
    tests?: string[];
    tatHours?: number;
  }>;
}

export interface SendoutManifest {
  manifest: {
    id: string;
    lab?: string;
    display?: string;
    dispatched_at: string;
    tracking_ref?: string | null;
    orders: string[];
  };
}

export interface SendoutTat {
  laboratories: Array<{
    lab?: string;
    display?: string;
    tat_hours?: number | null;
    resulted: number;
    in_flight: number;
    avg_hours?: number | null;
    within_tat_pct?: number | null;
  }>;
  generated_at: string;
  source: string;
}

export interface PoctDeviceItem {
  device_key: string;
  name: string;
  device_type: string;
  location?: string;
  status: string;
  qc_today: Record<string, string | null>;
  qc_current: boolean;
}

export interface PoctDevices {
  programme_configured: boolean;
  device_types: string[];
  items: PoctDeviceItem[];
  total: number;
}

export interface PoctQcRecord {
  id: number;
  device_key: string;
  level: string;
  result: string;
  operator: string;
  recorded_at?: string | null;
}

export interface PoctCompetency {
  id: number;
  staff_id: string;
  test_code: string;
  method: string;
  assessed_on: string;
  assessor: string;
  status: string;
  expires_on?: string | null;
  recorded_at?: string | null;
}

export interface PoctOverview {
  programme_configured: boolean;
  config: { kind: string; key: string; edit_in: string };
  enforcement: {
    block_uncompetent_operator: boolean;
    block_missing_daily_qc: boolean;
    state_label: string;
  };
  devices_total: number;
  devices_due_qc: string[];
  qc_levels: string[];
  competency_expiring: PoctCompetency[];
  competency_expired: PoctCompetency[];
}

export interface LabGovernance {
  config: { kind: string; key: string; version: number; edit_in: string };
  auto_verify: {
    enabled: boolean;
    state_label: string;
    rules: Array<{
      code: string;
      display?: string;
      low?: number | null;
      high?: number | null;
    }>;
    recent_auto_verified: Array<{
      report_id: string;
      order_id?: string;
      rule_ids: string[];
      at?: string | null;
    }>;
  };
  reflex_rules: Array<{
    id?: string;
    triggerCode?: string;
    when?: string;
    matchValue?: string;
    addCode?: string;
    note?: string;
  }>;
  delta_checks: Array<{
    code: string;
    display?: string;
    config: { absolute?: number; pct?: number; windowHours?: number };
  }>;
  stewardship: { duplicateCheckEnabled?: boolean; duplicateWindowHours?: number };
}

export interface StewardshipDuplicates {
  window_hours?: number | null;
  enabled: boolean;
  groups: Array<{
    patient: string;
    code: string;
    display?: string;
    orders: Array<{
      order_id: string;
      status?: string;
      authored_on?: string;
    }>;
  }>;
  total: number;
}
