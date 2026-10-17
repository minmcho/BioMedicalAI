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
  | "orderset";

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
