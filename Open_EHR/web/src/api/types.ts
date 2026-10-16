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

export interface LabConcept {
  code: string;
  display: string;
  active?: boolean;
  unit?: string;
  panel?: boolean;
  components?: string[];
  referenceRange?: LabReferenceRange;
}

export interface LabCatalogPayload {
  name?: string;
  system?: string;
  concepts: LabConcept[];
}

export interface LabWorkflowPayload {
  states: string[];
  transitions: Array<{ from: string; to: string; action?: string }>;
}

export interface LabCatalogResponse {
  catalog: LabCatalogPayload | null;
  catalog_version: number | null;
  workflow: LabWorkflowPayload | null;
  workflow_version: number | null;
}

export interface LabOrderDetail {
  order: ServiceRequest;
  specimen: Specimen | null;
  reports: DiagnosticReport[];
}

export interface LabTransitionOutcome {
  order: ServiceRequest;
  specimen: Specimen | null;
}

export interface LabResultInput {
  code: string;
  value: number;
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
}

export interface QcBlocker {
  analyte_code: string;
  qc_run: QcRun;
}

export interface VerificationResult {
  observation_id: string;
  code: string | null;
  display?: string;
  value?: number;
  unit?: string;
  interpretation: string | null;
  status?: string;
}

export interface VerificationItem {
  report: DiagnosticReport;
  order_id: string | null;
  order_status: string | null;
  stage: string;
  critical: boolean;
  qc_blocked: QcBlocker[];
  results: VerificationResult[];
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
  | "integration";

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

export interface SurgeryCaseEntry {
  case: Procedure;
  appointment: Appointment;
  room?: string | null;
  state?: string;
  start?: string;
  end?: string;
  patient_ref?: string | null;
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
