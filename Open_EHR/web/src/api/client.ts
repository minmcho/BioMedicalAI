import * as realtimeModule from "./realtime";
import type {
  AdcRestockItem,
  AdjustStockResult,
  Appointment,
  AuditEntry,
  BarcodeParseResult,
  BcmaOutcome,
  BloodHold,
  BloodUnit,
  Bundle,
  Claim,
  CollectionWorklist,
  CompoundBatch,
  CompoundingQueueResponse,
  ConfigConcept,
  ConfigKind,
  CriticalNotification,
  CsBookResponse,
  CsCount,
  CsWasteResult,
  DiagnosticReport,
  DurQueueResponse,
  EqaRecord,
  FormDefinition,
  InstrumentLogEntry,
  InteractionCheckResult,
  InteractionStatus,
  InventoryWorklist,
  LabInstrument,
  LeveyJennings,
  MedLabelPayload,
  MicroTerminology,
  PatientDispenseItem,
  PharmacyInventoryResponse,
  PurchaseOrder,
  TatStats,
  TraceabilityEntry,
  VerificationOutcome,
  VerificationQueueItem,
  WristbandPayload,
  DispenseOutcome,
  DispenseQueueItem,
  EdAdmitOutcome,
  EdBoard,
  Encounter,
  FormularyResponse,
  MedicationAdministration,
  EtlRunStats,
  ExploreInput,
  ExploreResult,
  FhirResource,
  LabCatalogResponse,
  LabIngestOutcome,
  LabOrderDetail,
  LabResultInput,
  LabResultsOutcome,
  LabTransitionOutcome,
  ClinicalNote,
  FlowsheetView,
  AdmissionDetail,
  AdmitOutcome,
  AdtEventsResponse,
  BedEntry,
  CarePlanResponse,
  Census,
  DischargePlan,
  DispositionsResponse,
  ExpectedDischargesResponse,
  LocationsResponse,
  QuestionnaireResponse,
  SurgeryCaseRecord,
  Task,
  MarDueResponse,
  ModalitiesResponse,
  ModulePack,
  NotesListResponse,
  NoteTypesResponse,
  OrdersListResponse,
  OrderSetsResponse,
  AllergyIntolerance,
  DosageInstruction,
  OrderTypesResponse,
  PackInstallation,
  PlaceOrderOutcome,
  PlaceOrderSetOutcome,
  PlacedOrder,
  QuestionnaireAnswerItem,
  QcRun,
  QcStatus,
  RadiologyStudiesResponse,
  RadiologyStudyDetail,
  RadiologyStudyItem,
  RegistryListEntry,
  RegistrySummary,
  QualityMeasureEntry,
  QualityMeasureResult,
  CohortSnapshot,
  CohortSnapshotsResponse,
  CohortExport,
  MessageThreadSummary,
  MessageThreadDetail,
  PortalMessage,
  TelehealthWorkspace,
  SelfSchedulingConfig,
  AppointmentRequestOutcome,
  RoomsResponse,
  ServiceRequest,
  Specimen,
  StockItem,
  StudioAction,
  StudioConfigObject,
  SurgeryBoard,
  SurgeryCaseEntry,
  SurgeryCaseOutcome,
  VerificationQueue,
  WarehouseStatus,
  CodingQueueItem,
  Activation,
  AssignmentBoard,
  BoardingBoard,
  CallbackQueue,
  CallQueueItem,
  CareGapItem,
  ClinicDay,
  ClinicSessionState,
  ClinicSessionTemplate,
  CommandOverview,
  ContinuumInbox,
  DischargeReadiness,
  ErOpsBoard,
  EvsItem,
  HahBoard,
  NoShowRiskItem,
  ReadinessItem,
  ReadmissionRiskItem,
  ReferralLoopItem,
  SafetyEventDetail,
  SafetyEventItem,
  SafetySummary,
  TransferRequestItem,
  TransportItem,
  CodingRecord,
  DenialItem,
  DenialsResponse,
  EligibilityCheckResult,
  PatientPaymentRecord,
  PatientStatement,
  InteropEndpointsResponse,
  InteropMessagesResponse,
  InteropDeadLettersResponse,
  InteropMessage,
  DispatchStats,
  PayersResponse,
  PriorAuth,
  Referral,
  ReferralDetail,
  RemittanceBatchSummary,
  RemittanceExceptionItem,
  RemittanceIngestResult,
  WaitlistAcceptResult,
  WaitlistEntry,
  WaitlistOfferResult,
  // Phase 13 (people & data platform)
  CredentialWorklist,
  DownloadedFile,
  ExportDataset,
  HomeDashboard,
  ImportProfile,
  ImportValidation,
  OnCallNow,
  ReportDefinition,
  RosterShift,
  StaffCredential,
  StaffMember,
  StaffUpdate,
  WorkforceConfig,
  // Front Door pass (accounts, registration, unit-first home)
  AccountCreated,
  AccountInput,
  AccountUpdate,
  DuplicateCandidate,
  FrontdeskToday,
  HomeCard,
  HomeCardsResponse,
  LocationDraftOp,
  LocationsDraftResult,
  LocationsDraftStatus,
  MyPatients,
  MyUnitPatients,
  Patient,
  RegistrationInput,
  RegistrationResult,
  UserAccount,
  // Phase 14 (devices, IoT, lake & pipelines)
  DedupCandidate,
  DeviceItem,
  DeviceReading,
  DevicesConfigResponse,
  DevicesResponse,
  FeedsStatus,
  LakeEntry,
  LakeResponse,
  LineageResponse,
  PipelineDeadLetter,
  PipelineDefinition,
  PipelineRun,
  QuarantineItem,
  LabGovernance,
  LabTrends,
  PoctCompetency,
  PoctDevices,
  PoctOverview,
  PoctQcRecord,
  SendoutManifest,
  SendoutQueue,
  SendoutTat,
  StewardshipDuplicates,
} from "./types";

// API client for the real OpenCare backend. There is deliberately no mock
// mode: every method performs an actual HTTP call to VITE_API_BASE.

export class ApiError extends Error {
  status: number;
  detail: string;
  /** The parsed error body, when the server sent structured JSON —
   *  e.g. the registration 409 carries the duplicate candidates the
   *  UI must show before the user can confirm. */
  body?: unknown;
  constructor(status: number, detail: string, body?: unknown) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.body = body;
  }

  get isPermissionDenied(): boolean {
    return this.status === 403;
  }
  get isNotFound(): boolean {
    return this.status === 404;
  }
}

export interface SearchParams {
  patient?: string;
  practitioner?: string;
  start?: string;
  end?: string;
}

export function buildSearchPath(resourceType: string, params: SearchParams = {}): string {
  const qs = new URLSearchParams();
  if (params.patient) qs.set("patient", params.patient);
  if (params.practitioner) qs.set("practitioner", params.practitioner);
  if (params.start) qs.set("start", params.start);
  if (params.end) qs.set("end", params.end);
  const query = qs.toString();
  return `/fhir/${resourceType}${query ? `?${query}` : ""}`;
}

export function unwrapBundle<T extends FhirResource>(bundle: Bundle<T>): T[] {
  return (bundle.entry ?? []).map((e) => e.resource);
}

type FetchFn = typeof fetch;

// ---- Depth pass (Workstream 2) response shapes ----
// Declared here (not in api/types) because this block is the depth
// pass's own client surface: the WHO checklist status the surgery
// board payload carries per case, and the continuum rounds list.

/** Per-section WHO surgical safety checklist completion for one case. */
export interface SafetyChecklistStatus {
  "sign-in": boolean;
  "time-out": boolean;
  "sign-out": boolean;
}

/** One patient on the interdisciplinary rounds list. */
export interface RoundsListItem {
  encounter_id: string;
  patient_ref: string | null;
  bed: string | null;
  reason?: string | null;
  nurse: string | null;
  acuity: number | null;
  target_date: string | null;
  disposition: string | null;
  barriers: string[] | string | null;
  rounds_checklist_today: boolean;
}

export interface RoundsList {
  unit: string;
  items: RoundsListItem[];
  total: number;
}

// ---- Analysis drill-down (Home & Module UX pass, WS4b) ----
// Shapes copied from the REAL backend payloads in
// backend/app/indicators.py: the published definition payload
// ({name, title?, source, thresholds?, target?, direction, unit?}),
// evaluate_indicator's output, and GET /indicators/{key}/records
// ({key, definition, value, records, records_supported, reason}).

/** A published indicator's source binding (exactly one key set per type). */
export interface IndicatorSource {
  type: "measure" | "cohort" | "named" | string;
  measure_key?: string;
  cohort_key?: string;
  name?: string;
}

/** The published indicator definition payload. */
export interface IndicatorDefinition {
  name: string;
  title?: string;
  source: IndicatorSource;
  thresholds?: { warn?: number; critical?: number };
  target?: number | null;
  direction: "higher-better" | "lower-better" | string;
  unit?: string;
}

/** One row of GET /indicators/definitions ({items, total}). */
export interface IndicatorDefinitionEntry {
  kind: string;
  key: string;
  version: number;
  status: string;
  payload: IndicatorDefinition;
  created_by?: string;
  updated_by?: string;
  created_at?: string | null;
  updated_at?: string | null;
}

/** evaluate_indicator output — also GET /indicators/{key}/value and
 *  the `value` field of the records payload. */
export interface IndicatorValue {
  key: string;
  value: number | null;
  unit: string | null;
  target: number | null;
  status: "ok" | "warn" | "critical" | "unknown" | string;
  evaluated_at: string;
  source: IndicatorSource;
  detail: Record<string, unknown>;
}

/** One underlying record behind an indicator number. `patient_id` is
 *  the chart patient id when the record is patient-linked, else null. */
export interface IndicatorRecord {
  id: string;
  label: string;
  detail: string;
  at: string | null;
  patient_id: string | null;
}

/** GET /indicators/{key}/records — definition + value + records.
 *  `reason` is null for ordinary count sources, an explanatory note
 *  for numerator-side listings, and the honest explanation when
 *  records_supported is false (records is then empty — never faked). */
export interface IndicatorRecordsResponse {
  key: string;
  definition: IndicatorDefinition;
  value: IndicatorValue;
  records: IndicatorRecord[];
  records_supported: boolean;
  reason: string | null;
}

// ---- Inventory & Materials (/inventory) ----
// Shapes follow the WS-A backend contract for backend/app/inventory.py
// exactly: taxonomy / overview aggregates, the item master, lot-level
// stock rows, the append-only movement ledger, PAR levels,
// requisitions, cycle counts, case draw, implant trace and the
// scan / RFID identification surface. Money is integer cents
// end-to-end; the page converts to dollars only for display/input.

export interface InvTaxonomyOption {
  code: string;
  label: string;
}

export interface InvClassPolicy {
  containerTracked?: boolean;
  openStabilityDays?: number | null;
  initialStage?: string;
  verificationRequired?: boolean;
  verificationValidityDays?: number | null;
}

export interface InvTaxonomySubcategory extends InvTaxonomyOption {
  classPolicy?: InvClassPolicy | null;
}

export interface InvTaxonomyCategory {
  code: string;
  label: string;
  subcategories: InvTaxonomySubcategory[];
  classPolicy?: InvClassPolicy | null;
}

export interface InvTaxonomy {
  categories: InvTaxonomyCategory[];
  reasonCodes: InvTaxonomyOption[];
  quarantineReasonCodes?: InvTaxonomyOption[];
  recallReasonCodes?: InvTaxonomyOption[];
  disposalReasonCodes?: InvTaxonomyOption[];
  expiryWarningDays: number;
}

export interface InvUomLevel {
  code: string;
  containsNext: number;
  barcode?: string | null;
}

export interface InvKitComponent {
  sku: string;
  qty: number;
}

export interface InvKitCompositionEntry {
  sku: string;
  lot_number?: string | null;
  expiry_date?: string | null;
  qty?: number;
  qty_per_kit?: number;
}

export interface InvOverview {
  active_items: number;
  stock_locations: number;
  on_hand_units: number;
  low_stock_lines: number;
  expiring_lines: number;
  expired_lines: number;
  movements_today: number;
  stock_value_cents: number;
  uncosted_lots: number;
}

export interface InvItem {
  sku: string;
  name: string;
  category: string;
  subcategory: string;
  uom: string;
  structure?: "simple" | "kit" | string;
  base_uom?: string | null;
  uom_ladder?: InvUomLevel[];
  kit_components?: InvKitComponent[];
  kit_mode?: "track-whole" | "break-on-receipt" | string | null;
  hazard_class?: string | null;
  sds_ref?: string | null;
  lot_tracked: boolean;
  serial_tracked: boolean;
  expiry_tracked: boolean;
  unit_cost_cents: number | null;
  formulary_code?: string | null;
  asset_key?: string | null;
  active: boolean;
}

export interface InvItemInput {
  sku: string;
  name: string;
  category: string;
  subcategory: string;
  uom: string;
  structure?: "simple" | "kit" | string;
  base_uom?: string | null;
  uom_ladder?: InvUomLevel[];
  kit_components?: InvKitComponent[];
  kit_mode?: "track-whole" | "break-on-receipt" | string | null;
  hazard_class?: string | null;
  sds_ref?: string | null;
  lot_tracked: boolean;
  serial_tracked: boolean;
  expiry_tracked: boolean;
  unit_cost_cents: number | null;
  formulary_code?: string | null;
  asset_key?: string | null;
  active?: boolean;
}

export interface InvStockRow {
  sku: string;
  name?: string;
  category?: string;
  location_code: string;
  lot_number: string | null;
  serial: string | null;
  expiry_date: string | null;
  quantity: number;
  unit_cost_cents: number | null;
  value_cents: number | null;
  stage?: string;
  verified_by?: string | null;
  verification_method?: string | null;
  verification_result?: string | null;
  verified_on?: string | null;
  verification_note?: string | null;
  composition?: InvKitCompositionEntry[];
  recall_blocked?: boolean;
  blocked_by?: Array<{ sku: string; lot_number?: string | null }>;
}

export interface InvParLevel {
  sku: string;
  name?: string;
  location_code: string;
  par_level: number;
  par_unit_level?: string | null;
  par_qty?: number | null;
}

export interface InvContainer {
  container_code: string;
  sku: string;
  location_code: string;
  lot_number: string | null;
  serial: string | null;
  quantity: number;
  stage: string;
  opened_at?: string | null;
  opened_by?: string | null;
  in_use_expiry_date?: string | null;
  effective_expiry?: string | null;
  lot_stage?: string | null;
  disposal_reason_code?: string | null;
  disposed_by?: string | null;
  disposed_at?: string | null;
}

export interface InvKitResult {
  sku: string;
  location_code: string;
  quantity: number;
  movements?: InvMovement[];
  lot?: InvStockRow;
  kit_lot?: InvStockRow;
  lots?: InvStockRow[];
  composition?: InvKitCompositionEntry[];
}

export interface InvMovement {
  id?: number | string;
  sku: string;
  name?: string;
  event?: string;
  location_code?: string | null;
  from_location?: string | null;
  to_location?: string | null;
  quantity?: number;
  quantity_delta?: number;
  lot_number?: string | null;
  serial?: string | null;
  expiry_date?: string | null;
  unit_cost_cents?: number | null;
  value_cents?: number | null;
  reference?: string | null;
  patient_ref?: string | null;
  reason_code?: string | null;
  note?: string | null;
  actor?: string | null;
  created_at?: string | null;
  unit_level?: string | null;
  container_code?: string | null;
}

export interface InvRequisitionLine {
  sku: string;
  quantity: number;
  issued?: number;
}

export interface InvRequisition {
  id?: string;
  req_id?: string;
  from_location_code: string;
  to_location_code: string;
  status: string;
  lines: InvRequisitionLine[];
  reference?: string | null;
  created_at?: string | null;
  created_by?: string | null;
}

export interface InvCountLine {
  sku: string;
  name?: string;
  lot_number?: string | null;
  serial?: string | null;
  expected?: number;
  counted?: number | null;
  variance?: number | null;
}

export interface InvCount {
  id?: string;
  count_id?: string;
  location_code: string;
  status: string;
  lines: InvCountLine[];
  created_at?: string | null;
  created_by?: string | null;
  completed_at?: string | null;
}

export interface InvCaseIssueLine {
  sku: string;
  qty: number;
  allocations: Array<Record<string, unknown>>;
}

export interface InvCaseIssueResult {
  case_id: string;
  card_id: string;
  lines: InvCaseIssueLine[];
}

export interface InvScanResult {
  resolved: boolean;
  via: string | null;
  item: InvItem | null;
  lot?: Record<string, unknown> | null;
  stock?: InvStockRow[] | Record<string, unknown> | null;
}

export interface InvLowStockRow {
  sku: string;
  name: string;
  location_code: string;
  on_hand: number;
  par_level: number;
  deficit: number;
}

export interface InvExpiryRow {
  sku: string;
  name: string;
  location_code: string;
  lot_number: string | null;
  expiry_date: string | null;
  quantity: number;
  days_to_expiry: number | null;
  expired: boolean;
}

export interface InvImplantTraceRow {
  sku?: string;
  name?: string;
  lot_number?: string | null;
  serial?: string | null;
  patient_ref?: string | null;
  case_id?: string | null;
  location_code?: string | null;
  event?: string;
  quantity?: number;
  actor?: string | null;
  created_at?: string | null;
  [key: string]: unknown;
}

/** List endpoints in the inventory surface return either a bare array
 *  or an {items} envelope depending on the route; unwrap both. */
function invList<T>(body: unknown): T[] {
  if (Array.isArray(body)) return body as T[];
  if (body && typeof body === "object" && Array.isArray((body as { items?: unknown }).items)) {
    return (body as { items: T[] }).items;
  }
  return [];
}

function invQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    qs.set(key, String(value));
  }
  const s = qs.toString();
  return s ? `?${s}` : "";
}

export class ApiClient {
  private baseUrl: string;
  private getToken: () => string | null;
  private fetchFn: FetchFn;

  constructor(baseUrl: string, getToken: () => string | null, fetchFn: FetchFn = fetch) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.getToken = getToken;
    // Wrap the fetch implementation so it is always invoked as a free
    // function. The browser's native fetch throws "Illegal invocation"
    // when called with any receiver other than Window — storing it and
    // calling it as this.fetchFn(...) breaks every request in a real
    // browser (test doubles don't enforce the receiver, which is how
    // this shipped). The wrapper keeps custom implementations intact.
    this.fetchFn = (url: RequestInfo | URL, init?: RequestInit) => fetchFn(url, init);
  }

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    const token = this.getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    if (init.body) headers.set("Content-Type", "application/json");
    let response: Response;
    try {
      response = await this.fetchFn(`${this.baseUrl}${path}`, { ...init, headers });
    } catch {
      throw new ApiError(0, `Cannot reach the OpenCare API at ${this.baseUrl}. Is the backend running?`);
    }
    const text = await response.text();
    let data: unknown = undefined;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = undefined;
      }
    }
    if (!response.ok) {
      const detail =
        (data as { detail?: string } | undefined)?.detail ??
        `${response.status} ${response.statusText}`;
      throw new ApiError(response.status, detail, data);
    }
    return data as T;
  }

  health(): Promise<{ status: string; service: string }> {
    return this.request("/health");
  }

  /** Raw-text GET (the 837 export is an EDI text file, not JSON). Errors
   *  surface as ApiError with the server's JSON detail when present. */
  async requestText(path: string): Promise<string> {
    const headers = new Headers();
    const token = this.getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    let response: Response;
    try {
      response = await this.fetchFn(`${this.baseUrl}${path}`, { headers });
    } catch {
      throw new ApiError(0, `Cannot reach the OpenCare API at ${this.baseUrl}. Is the backend running?`);
    }
    const text = await response.text();
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        detail = (JSON.parse(text) as { detail?: string }).detail ?? detail;
      } catch {
        // non-JSON error body — keep the status line
      }
      throw new ApiError(response.status, detail);
    }
    return text;
  }

  /** Binary GET/POST download (Phase 13: report PDFs/CSVs, printable
   *  documents, dataset exports). Returns the blob plus the server's
   *  filename (Content-Disposition) and row count header when sent.
   *  Errors surface as ApiError with the server's JSON detail. */
  async requestBlob(
    path: string,
    init: RequestInit = {},
  ): Promise<DownloadedFile> {
    const headers = new Headers(init.headers);
    const token = this.getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    if (init.body) headers.set("Content-Type", "application/json");
    let response: Response;
    try {
      response = await this.fetchFn(`${this.baseUrl}${path}`, {
        ...init,
        headers,
      });
    } catch {
      throw new ApiError(0, `Cannot reach the OpenCare API at ${this.baseUrl}. Is the backend running?`);
    }
    if (!response.ok) {
      const text = await response.text();
      let detail = `${response.status} ${response.statusText}`;
      try {
        const parsed = JSON.parse(text) as {
          detail?: string | { message?: string };
        };
        if (typeof parsed.detail === "string") detail = parsed.detail;
        else if (parsed.detail?.message) detail = parsed.detail.message;
      } catch {
        // non-JSON error body — keep the status line
      }
      throw new ApiError(response.status, detail);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") ?? "";
    const match = /filename="?([^";]+)"?/.exec(disposition);
    const rowsHeader =
      response.headers.get("X-Export-Rows") ??
      response.headers.get("X-Report-Rows");
    return {
      blob,
      filename: match ? match[1] : "download",
      rows: rowsHeader ? Number(rowsHeader) : null,
    };
  }

  /** GET /fhir/{Type} search — returns the resources inside the Bundle. */
  async search<T extends FhirResource>(resourceType: string, params: SearchParams = {}): Promise<T[]> {
    const bundle = await this.request<Bundle<T>>(buildSearchPath(resourceType, params));
    return unwrapBundle(bundle);
  }

  get<T extends FhirResource>(resourceType: string, id: string): Promise<T> {
    return this.request(`/fhir/${resourceType}/${encodeURIComponent(id)}`);
  }

  /** POST /fhir/{Type} — create a resource through the generic FHIR
   *  surface (server-validated; e.g. a QuestionnaireResponse is only
   *  accepted against a published Studio form). */
  create<T extends FhirResource>(resourceType: string, body: Record<string, unknown>): Promise<T> {
    return this.request(`/fhir/${resourceType}`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  /** GET /fhir/inbox?patient=... — staff roles only on the server. */
  async inbox(patient: string, status?: string): Promise<FhirResource[]> {
    const qs = new URLSearchParams({ patient });
    if (status) qs.set("status", status);
    const bundle = await this.request<Bundle>(`/fhir/inbox?${qs.toString()}`);
    return unwrapBundle(bundle);
  }

  /** GET /portal/my-results — patient role only on the server. */
  async myResults(): Promise<FhirResource[]> {
    const bundle = await this.request<Bundle>("/portal/my-results");
    return unwrapBundle(bundle);
  }

  /** POST /fhir/Appointment/{id}/transition { to_status } */
  transitionAppointment(id: string, toStatus: string): Promise<Appointment> {
    return this.request(`/fhir/Appointment/${encodeURIComponent(id)}/transition`, {
      method: "POST",
      body: JSON.stringify({ to_status: toStatus }),
    });
  }

  // ---- Phase 2: Lab LIS (/lab) ----
  // Orders are catalog-validated server-side; results are drafts until a
  // clinician verifies and then releases the report (two separate calls).

  /** GET /lab/catalog — published Studio lab catalog + workflow config. */
  labCatalog(): Promise<LabCatalogResponse> {
    return this.request("/lab/catalog");
  }

  /** GET /lab/orders[?status=][&bench=] — the order queue (staff
   *  roles). `bench` filters to one bench's worklist (Depth pass:
   *  the core benches share the spine, so their worklist is this
   *  queue filtered by the catalog bench of each order's code). */
  async labOrders(status?: string, bench?: string): Promise<ServiceRequest[]> {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (bench) params.set("bench", bench);
    const qs = params.toString() ? `?${params.toString()}` : "";
    const bundle = await this.request<Bundle<ServiceRequest>>(`/lab/orders${qs}`);
    return unwrapBundle(bundle);
  }

  /** POST /lab/orders — place a catalog-validated order. */
  createLabOrder(input: { code: string; patientId: string; id?: string }): Promise<ServiceRequest> {
    return this.request("/lab/orders", {
      method: "POST",
      body: JSON.stringify({
        ...(input.id ? { id: input.id } : {}),
        code: input.code,
        subject: { reference: `Patient/${input.patientId}` },
      }),
    });
  }

  /** GET /lab/orders/{id} — order + specimen + reports. */
  labOrderDetail(id: string): Promise<LabOrderDetail> {
    return this.request(`/lab/orders/${encodeURIComponent(id)}`);
  }

  /** POST /lab/orders/{id}/transition { to_status, reason?, reason_code?, specimen_type? } */
  transitionLabOrder(
    id: string,
    toStatus: string,
    extra: { reason?: string; reason_code?: string; specimen_type?: string } = {},
  ): Promise<LabTransitionOutcome> {
    return this.request(`/lab/orders/${encodeURIComponent(id)}/transition`, {
      method: "POST",
      body: JSON.stringify({ to_status: toStatus, ...extra }),
    });
  }

  /** GET /lab/collection-worklist — orders in "ordered" state grouped by
   *  patient + collection location, sorted by collection priority from
   *  the published catalog (Phase 6B pre-analytical). */
  collectionWorklist(): Promise<CollectionWorklist> {
    return this.request("/lab/collection-worklist");
  }

  /** POST /lab/orders/{id}/recollect — rejected orders only: back to
   *  "ordered" with the rejection history preserved (Phase 6B). */
  recollectLabOrder(id: string): Promise<LabTransitionOutcome> {
    return this.request(`/lab/orders/${encodeURIComponent(id)}/recollect`, {
      method: "POST",
    });
  }

  /** POST /lab/reports/{id}/amend { reason, results? } — amend a
   *  RELEASED report with a mandatory reason: a new version (FHIR
   *  status "amended") supersedes it; the original is preserved and
   *  linked, never rewritten (Phase 6B). */
  amendLabReport(
    id: string,
    input: { reason: string; results?: LabResultInput[] },
  ): Promise<{
    report: DiagnosticReport;
    order: ServiceRequest;
    original_report_id: string;
  }> {
    return this.request(`/lab/reports/${encodeURIComponent(id)}/amend`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /lab/orders/{id}/results — manual result entry (draft results). */
  enterLabResults(id: string, results: LabResultInput[]): Promise<LabResultsOutcome> {
    return this.request(`/lab/orders/${encodeURIComponent(id)}/results`, {
      method: "POST",
      body: JSON.stringify({ results }),
    });
  }

  /** POST /lab/ingest { format: "hl7" | "csv", content, instrument_id? }
   *  — analyser ingest. When named, the instrument must exist in the
   *  registry (server records it as report metadata). */
  ingestLab(
    format: "hl7" | "csv",
    content: string,
    instrumentId?: string,
  ): Promise<LabIngestOutcome & { instrument_id?: string | null }> {
    return this.request("/lab/ingest", {
      method: "POST",
      body: JSON.stringify({
        format,
        content,
        ...(instrumentId ? { instrument_id: instrumentId } : {}),
      }),
    });
  }

  /** GET /lab/specimens[?status=] — specimen tracking list. */
  async labSpecimens(status?: string): Promise<Specimen[]> {
    const qs = status ? `?status=${encodeURIComponent(status)}` : "";
    const bundle = await this.request<Bundle<Specimen>>(`/lab/specimens${qs}`);
    return unwrapBundle(bundle);
  }

  /** GET /lab/verification-queue — drafts awaiting verify/release, with
   *  critical flags and live QC-block state per report. */
  verificationQueue(): Promise<VerificationQueue> {
    return this.request("/lab/verification-queue");
  }

  /** POST /lab/reports/{id}/verify — clinical verification (step 1). */
  verifyLabReport(id: string): Promise<{ report: DiagnosticReport; order: ServiceRequest }> {
    return this.request(`/lab/reports/${encodeURIComponent(id)}/verify`, { method: "POST" });
  }

  /** POST /lab/reports/{id}/release — release to inbox/portal (step 2). */
  releaseLabReport(id: string): Promise<{ report: DiagnosticReport; order: ServiceRequest }> {
    return this.request(`/lab/reports/${encodeURIComponent(id)}/release`, { method: "POST" });
  }

  /** GET /lab/critical-reports — released reports containing a critical
   *  value, each with its notification state (staff roles). */
  criticalReports(): Promise<VerificationQueue> {
    return this.request("/lab/critical-reports");
  }

  /** POST /lab/reports/{id}/notify { notified_to, read_back } — record
   *  the critical-value phone notification. Read-back is mandatory on
   *  the server (422 without it); non-critical reports are a 400. */
  notifyLabReport(
    id: string,
    input: { notifiedTo: string; readBack: boolean },
  ): Promise<{ report_id: string; notification: CriticalNotification }> {
    return this.request(`/lab/reports/${encodeURIComponent(id)}/notify`, {
      method: "POST",
      body: JSON.stringify({ notified_to: input.notifiedTo, read_back: input.readBack }),
    });
  }

  /** POST /lab/reports/{id}/acknowledge — the receiving clinician
   *  confirms a notified critical report (409 before notification). */
  acknowledgeLabReport(
    id: string,
  ): Promise<{ report_id: string; notification: CriticalNotification }> {
    return this.request(`/lab/reports/${encodeURIComponent(id)}/acknowledge`, {
      method: "POST",
    });
  }

  /** GET /lab/qc[?analyte_code=] — QC run history, newest first. */
  qcRuns(analyteCode?: string): Promise<QcRun[]> {
    const qs = analyteCode ? `?analyte_code=${encodeURIComponent(analyteCode)}` : "";
    return this.request(`/lab/qc${qs}`);
  }

  /** POST /lab/qc — record a QC run; pass/fail + Westgard-style flags
   *  are computed server-side (Phase 6B QC programme). */
  createQcRun(input: {
    analyte_code: string;
    control_material: string;
    expected_low: number;
    expected_high: number;
    measured_value: number;
    unit?: string;
    level?: string;
  }): Promise<QcRun> {
    return this.request("/lab/qc", { method: "POST", body: JSON.stringify(input) });
  }

  /** GET /lab/qc/levey-jennings — run series + server-computed mean/SD
   *  for a Levey-Jennings chart (Phase 6B). */
  leveyJennings(analyteCode: string, controlMaterial: string): Promise<LeveyJennings> {
    return this.request(
      `/lab/qc/levey-jennings?analyte_code=${encodeURIComponent(analyteCode)}&control_material=${encodeURIComponent(controlMaterial)}`,
    );
  }

  // ---- Phase 6B: microbiology bench ----
  microTerminology(): Promise<MicroTerminology> {
    return this.request("/lab/micro/terminology");
  }

  microPreliminary(
    orderId: string,
    text: string,
  ): Promise<{ report: DiagnosticReport; order: ServiceRequest }> {
    return this.request(`/lab/micro/orders/${encodeURIComponent(orderId)}/preliminary`, {
      method: "POST",
      body: JSON.stringify({ text }),
    });
  }

  microOrganism(
    orderId: string,
    organismCode: string,
  ): Promise<{ report: DiagnosticReport; order: ServiceRequest }> {
    return this.request(`/lab/micro/orders/${encodeURIComponent(orderId)}/organism`, {
      method: "POST",
      body: JSON.stringify({ organism_code: organismCode }),
    });
  }

  microSusceptibility(
    orderId: string,
    results: Array<{ antibiotic: string; interpretation: string }>,
  ): Promise<{ report: DiagnosticReport; order: ServiceRequest; observations: string[] }> {
    return this.request(`/lab/micro/orders/${encodeURIComponent(orderId)}/susceptibility`, {
      method: "POST",
      body: JSON.stringify({ results }),
    });
  }

  microFinalize(
    orderId: string,
  ): Promise<{ report: DiagnosticReport; order: ServiceRequest }> {
    return this.request(`/lab/micro/orders/${encodeURIComponent(orderId)}/finalize`, {
      method: "POST",
    });
  }

  // ---- Phase 6B: blood bank / transfusion ----
  bloodUnits(status?: string): Promise<BloodUnit[]> {
    const qs = status ? `?status=${encodeURIComponent(status)}` : "";
    return this.request(`/lab/blood-bank/units${qs}`);
  }

  createBloodUnit(input: {
    unit_id: string;
    product_type: string;
    blood_group: string;
    expiry_date: string;
  }): Promise<BloodUnit> {
    return this.request("/lab/blood-bank/units", { method: "POST", body: JSON.stringify(input) });
  }

  issueBloodUnit(unitId: string, orderId: string): Promise<BloodUnit> {
    return this.request(`/lab/blood-bank/units/${encodeURIComponent(unitId)}/issue`, {
      method: "POST",
      body: JSON.stringify({ order_id: orderId }),
    });
  }

  returnBloodUnit(unitId: string, transfused: boolean): Promise<BloodUnit> {
    return this.request(`/lab/blood-bank/units/${encodeURIComponent(unitId)}/return`, {
      method: "POST",
      body: JSON.stringify({ transfused }),
    });
  }

  recordTransfusion(input: {
    unit_id: string;
    performed_at?: string;
  }): Promise<{ transfusion: FhirResource; unit: BloodUnit }> {
    return this.request("/lab/blood-bank/transfusions", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  reportTransfusionReaction(
    unitId: string,
    symptoms: string,
  ): Promise<{ unit: BloodUnit; hold: BloodHold | null; transfusion_id: string | null }> {
    return this.request(`/lab/blood-bank/units/${encodeURIComponent(unitId)}/reaction`, {
      method: "POST",
      body: JSON.stringify({ symptoms }),
    });
  }

  bloodHolds(): Promise<BloodHold[]> {
    return this.request("/lab/blood-bank/holds");
  }

  clearBloodHold(patientId: string): Promise<BloodHold> {
    return this.request(`/lab/blood-bank/holds/${encodeURIComponent(patientId)}/clear`, {
      method: "POST",
    });
  }

  // ---- Phase 6B: anatomic pathology ----
  pathologySynopticForm(): Promise<{ form: FormDefinition; version: number }> {
    return this.request("/lab/pathology/synoptic-form");
  }

  pathologyReport(
    orderId: string,
    answers: QuestionnaireAnswerItem[],
  ): Promise<{ report: DiagnosticReport; order: ServiceRequest; questionnaire_response: unknown }> {
    return this.request(`/lab/pathology/orders/${encodeURIComponent(orderId)}/report`, {
      method: "POST",
      body: JSON.stringify({ answers }),
    });
  }

  pathologySign(
    reportId: string,
  ): Promise<{ report: DiagnosticReport; order: ServiceRequest }> {
    return this.request(`/lab/pathology/reports/${encodeURIComponent(reportId)}/sign`, {
      method: "POST",
    });
  }

  // ---- Phase 6B: laboratory operations ----
  eqaRecords(): Promise<EqaRecord[]> {
    return this.request("/lab/eqa");
  }

  createEqa(input: {
    event: string;
    analyte_code: string;
    reported_value: number;
    acceptable_low: number;
    acceptable_high: number;
  }): Promise<EqaRecord> {
    return this.request("/lab/eqa", { method: "POST", body: JSON.stringify(input) });
  }

  instruments(): Promise<LabInstrument[]> {
    return this.request("/lab/instruments");
  }

  createInstrument(input: {
    instrument_id: string;
    name: string;
    bench?: string;
    status?: string;
  }): Promise<LabInstrument> {
    return this.request("/lab/instruments", { method: "POST", body: JSON.stringify(input) });
  }

  setInstrumentStatus(instrumentId: string, status: string): Promise<LabInstrument> {
    return this.request(`/lab/instruments/${encodeURIComponent(instrumentId)}/status`, {
      method: "POST",
      body: JSON.stringify({ status }),
    });
  }

  instrumentLog(instrumentId: string): Promise<InstrumentLogEntry[]> {
    return this.request(`/lab/instruments/${encodeURIComponent(instrumentId)}/log`);
  }

  addInstrumentLog(
    instrumentId: string,
    input: { kind: string; note?: string },
  ): Promise<InstrumentLogEntry> {
    return this.request(`/lab/instruments/${encodeURIComponent(instrumentId)}/log`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** GET /lab/tat — per-bench/per-analyte turnaround percentiles,
   *  live-computed from custody timestamps (Phase 6B). */
  tatStats(): Promise<TatStats> {
    return this.request("/lab/tat");
  }

  /** GET /lab/qc/status?analyte_code= — is this analyte QC-blocked? */
  qcStatus(analyteCode: string): Promise<QcStatus> {
    return this.request(`/lab/qc/status?analyte_code=${encodeURIComponent(analyteCode)}`);
  }

  // ---- Depth pass: virology trends, send-outs, POCT programme,
  // post-analytical governance ----

  /** GET /lab/trends — a patient's RELEASED result series for one
   *  analyte (viral-load trends; drafts never appear). */
  labTrends(patientId: string, code: string): Promise<LabTrends> {
    return this.request(
      `/lab/trends?patient_id=${encodeURIComponent(patientId)}&code=${encodeURIComponent(code)}`,
    );
  }

  /** GET /lab/governance — auto-verify state + rules, reflex rules,
   *  delta checks and the stewardship window, read from the
   *  published catalog (editing stays in Studio). */
  labGovernance(): Promise<LabGovernance> {
    return this.request("/lab/governance");
  }

  /** GET /lab/stewardship/duplicates — patient + analyte pairs with
   *  more than one unfinished order inside the duplicate window. */
  labStewardshipDuplicates(): Promise<StewardshipDuplicates> {
    return this.request("/lab/stewardship/duplicates");
  }

  /** GET /lab/sendouts — orders routed to a reference laboratory,
   *  with their send-out state. */
  labSendouts(): Promise<SendoutQueue> {
    return this.request("/lab/sendouts");
  }

  /** POST /lab/sendouts/dispatch — ship collected send-out orders
   *  on one manifest to one reference laboratory. */
  labSendoutDispatch(orderIds: string[], trackingRef?: string): Promise<SendoutManifest> {
    return this.request("/lab/sendouts/dispatch", {
      method: "POST",
      body: JSON.stringify({
        order_ids: orderIds,
        ...(trackingRef ? { tracking_ref: trackingRef } : {}),
      }),
    });
  }

  /** POST /lab/sendouts/{id}/receive-result — file the reference
   *  laboratory's returned result as a draft for verification. */
  labSendoutReceiveResult(
    orderId: string,
    results: LabResultInput[],
  ): Promise<LabResultsOutcome> {
    return this.request(`/lab/sendouts/${encodeURIComponent(orderId)}/receive-result`, {
      method: "POST",
      body: JSON.stringify({ results }),
    });
  }

  /** GET /lab/sendouts/tat — turnaround by reference laboratory. */
  labSendoutTat(): Promise<SendoutTat> {
    return this.request("/lab/sendouts/tat");
  }

  /** GET /lab/pathology/cytology-form — the published cytology form. */
  pathologyCytologyForm(): Promise<{ form: FormDefinition; version: number }> {
    return this.request("/lab/pathology/cytology-form");
  }

  /** POST /lab/pathology/orders/{id}/cytology-report — store the
   *  cytology draft report (answers validated by the form machinery). */
  pathologyCytologyReport(
    orderId: string,
    answers: QuestionnaireAnswerItem[],
  ): Promise<{
    report: DiagnosticReport;
    order: ServiceRequest;
    questionnaire_response: FhirResource;
  }> {
    return this.request(`/lab/pathology/orders/${encodeURIComponent(orderId)}/cytology-report`, {
      method: "POST",
      body: JSON.stringify({ answers }),
    });
  }

  /** GET /lab/poct/devices — programme devices from the Phase 14
   *  registry with today's QC state. */
  labPoctDevices(): Promise<PoctDevices> {
    return this.request("/lab/poct/devices");
  }

  /** GET /lab/poct/qc — recent POCT QC records. */
  async labPoctQc(deviceKey?: string): Promise<PoctQcRecord[]> {
    const qs = deviceKey ? `?device_key=${encodeURIComponent(deviceKey)}` : "";
    const body = await this.request<{ items: PoctQcRecord[] }>(`/lab/poct/qc${qs}`);
    return body.items;
  }

  /** POST /lab/poct/qc — record one QC run on a POCT device. */
  labPoctRecordQc(input: {
    device_key: string;
    level: string;
    result: string;
  }): Promise<{ qc: PoctQcRecord; qc_current: boolean }> {
    return this.request("/lab/poct/qc", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** GET /lab/poct/competency — competency records with computed
   *  currency (status + expiry from the published validity window). */
  async labPoctCompetency(staffId?: string): Promise<PoctCompetency[]> {
    const qs = staffId ? `?staff_id=${encodeURIComponent(staffId)}` : "";
    const body = await this.request<{ items: PoctCompetency[] }>(
      `/lab/poct/competency${qs}`,
    );
    return body.items;
  }

  /** POST /lab/poct/competency — record an operator assessment. */
  labPoctRecordCompetency(input: {
    staff_id: string;
    test_code: string;
    method: string;
    assessed_on: string;
  }): Promise<{ competency: PoctCompetency }> {
    return this.request("/lab/poct/competency", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** GET /lab/poct/overview — programme state: enforcement label,
   *  devices due QC, expiring/expired competencies. */
  labPoctOverview(): Promise<PoctOverview> {
    return this.request("/lab/poct/overview");
  }

  // ---- Studio packs ----
  listPacks(): Promise<ModulePack[]> {
    return this.request("/studio/packs");
  }

  listInstallations(): Promise<PackInstallation[]> {
    return this.request("/studio/installations");
  }

  installPack(name: string): Promise<PackInstallation> {
    return this.request(`/studio/packs/${encodeURIComponent(name)}/install`, { method: "POST" });
  }

  installationAction(id: number, action: StudioAction): Promise<PackInstallation> {
    return this.request(`/studio/installations/${id}/${action}`, { method: "POST" });
  }

  /** GET /studio/audit?limit= — most recent hash-chained audit entries,
   *  newest first (admin/configurator only on the server). */
  listAudit(limit = 50): Promise<AuditEntry[]> {
    return this.request(`/studio/audit?limit=${limit}`);
  }

  // ---- Studio configuration (customization engine) ----
  // Versioned config objects: create a draft, edit the draft, publish
  // (superseding the previous published version), disable, roll back.
  // Published versions are immutable on the server; editing one means
  // creating a new draft.

  /** GET /studio/config[?kind=] — every version visible to the caller. */
  listConfigs(kind?: ConfigKind): Promise<StudioConfigObject[]> {
    const qs = kind ? `?kind=${encodeURIComponent(kind)}` : "";
    return this.request(`/studio/config${qs}`);
  }

  /** GET /studio/config/{kind}/{key} — full version history of one object. */
  configHistory(kind: ConfigKind, key: string): Promise<StudioConfigObject[]> {
    return this.request(
      `/studio/config/${encodeURIComponent(kind)}/${encodeURIComponent(key)}`,
    );
  }

  /** GET /studio/config/{kind}/effective — published objects of a kind. */
  effectiveConfigs(kind: ConfigKind): Promise<StudioConfigObject[]> {
    return this.request(`/studio/config/${encodeURIComponent(kind)}/effective`);
  }

  /** GET /studio/config/{kind}/effective/{key} — the one published
   *  object for a key (404 when nothing is published). This is the
   *  runtime-consumption read the web shell uses at sign-in. */
  effectiveConfig(kind: ConfigKind, key: string): Promise<StudioConfigObject> {
    return this.request(
      `/studio/config/${encodeURIComponent(kind)}/effective/${encodeURIComponent(key)}`,
    );
  }

  /** POST /studio/config/{kind}/{key} — create the next version as a draft. */
  createConfigDraft(
    kind: ConfigKind,
    key: string,
    payload: Record<string, unknown>,
  ): Promise<StudioConfigObject> {
    return this.request(
      `/studio/config/${encodeURIComponent(kind)}/${encodeURIComponent(key)}`,
      { method: "POST", body: JSON.stringify(payload) },
    );
  }

  /** PUT /studio/config/{kind}/{key}/versions/{n} — replace a draft payload. */
  updateConfigDraft(
    kind: ConfigKind,
    key: string,
    version: number,
    payload: Record<string, unknown>,
  ): Promise<StudioConfigObject> {
    return this.request(
      `/studio/config/${encodeURIComponent(kind)}/${encodeURIComponent(key)}/versions/${version}`,
      { method: "PUT", body: JSON.stringify(payload) },
    );
  }

  publishConfig(kind: ConfigKind, key: string, version: number): Promise<StudioConfigObject> {
    return this.request(
      `/studio/config/${encodeURIComponent(kind)}/${encodeURIComponent(key)}/versions/${version}/publish`,
      { method: "POST" },
    );
  }

  disableConfig(kind: ConfigKind, key: string, version: number): Promise<StudioConfigObject> {
    return this.request(
      `/studio/config/${encodeURIComponent(kind)}/${encodeURIComponent(key)}/versions/${version}/disable`,
      { method: "POST" },
    );
  }

  /** POST /studio/config/{kind}/{key}/rollback {version} — republish an old
   *  version's payload as a new published version. */
  rollbackConfig(kind: ConfigKind, key: string, version: number): Promise<StudioConfigObject> {
    return this.request(
      `/studio/config/${encodeURIComponent(kind)}/${encodeURIComponent(key)}/rollback`,
      { method: "POST", body: JSON.stringify({ version }) },
    );
  }

  // ---- Phase 3: population health + warehouse (/pophealth, /warehouse) ----
  // Aggregate-only analytics over the OMOP warehouse. Small counts come
  // back suppressed ("<5") from the server; there is no patient-level
  // endpoint in this surface at all.

  /** GET /pophealth/registries — published Studio cohort definitions. */
  registries(): Promise<RegistryListEntry[]> {
    return this.request("/pophealth/registries");
  }

  /** GET /pophealth/registries/{key}/summary — aggregate registry summary. */
  registrySummary(key: string): Promise<RegistrySummary> {
    return this.request(`/pophealth/registries/${encodeURIComponent(key)}/summary`);
  }

  /** POST /pophealth/explore — ad-hoc cohort count (aggregate only). */
  exploreCohort(input: ExploreInput): Promise<ExploreResult> {
    return this.request("/pophealth/explore", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** GET /warehouse/status — outbox tallies, dead letters, table counts,
   *  mapping coverage. */
  warehouseStatus(): Promise<WarehouseStatus> {
    return this.request("/warehouse/status");
  }

  /** POST /warehouse/etl/run — trigger an outbox drain (pophealth.run
   *  capability; admin by default). */
  runEtl(): Promise<EtlRunStats> {
    return this.request("/warehouse/etl/run", { method: "POST" });
  }

  // ---- Phase 4A: billing (/billing) ----
  // Charge capture + claim lifecycle + 837 export. Prices and totals are
  // computed server-side from the published Studio fee schedule.

  /** POST /billing/claims/generate { encounter_id } — build a draft claim
   *  from an encounter (billing.manage: admin/configurator). */
  generateClaim(encounterId: string): Promise<Claim> {
    return this.request("/billing/claims/generate", {
      method: "POST",
      body: JSON.stringify({ encounter_id: encounterId }),
    });
  }

  /** GET /billing/claims[?status=][?patient=] — patient callers only ever
   *  receive their own claims (server-enforced). */
  async claims(params: { status?: string; patient?: string } = {}): Promise<Claim[]> {
    const qs = new URLSearchParams();
    if (params.status) qs.set("status", params.status);
    if (params.patient) qs.set("patient", params.patient);
    const query = qs.toString();
    const bundle = await this.request<Bundle<Claim>>(
      `/billing/claims${query ? `?${query}` : ""}`,
    );
    return unwrapBundle(bundle);
  }

  /** GET /billing/claims/{id} */
  getClaim(id: string): Promise<Claim> {
    return this.request(`/billing/claims/${encodeURIComponent(id)}`);
  }

  /** POST /billing/claims/{id}/transition { to_status, note? } — the
   *  'appealed' target is the one-time appeal and requires a note. */
  transitionClaim(id: string, toStatus: string, note?: string): Promise<Claim> {
    return this.request(`/billing/claims/${encodeURIComponent(id)}/transition`, {
      method: "POST",
      body: JSON.stringify({ to_status: toStatus, ...(note ? { note } : {}) }),
    });
  }

  /** GET /billing/claims/{id}/export/837 — raw EDI text (submitted claims
   *  only; a draft is a 409 on the server). */
  export837(id: string): Promise<string> {
    return this.requestText(`/billing/claims/${encodeURIComponent(id)}/export/837`);
  }

  // ---- Phase 7: access (/access) — referrals, prior authorisations,
  // eligibility, waitlist. Payers/services/slot types and the three
  // lifecycles are Studio configuration on the server. ----

  /** GET /access/payers — the published payer terminology (eligibility
   *  figures + auth-required services ride on the concepts). */
  payers(): Promise<PayersResponse> {
    return this.request("/access/payers");
  }

  /** POST /access/referrals — inbound referral intake (access.work). */
  createReferral(input: {
    patient: string;
    service_code: string;
    referring_organization: string;
    referring_provider?: string;
    reason?: string;
    payer?: string;
    priority?: string;
    note?: string;
  }): Promise<{ referral: Referral; task: unknown }> {
    return this.request("/access/referrals", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** GET /access/referrals/queue — referrals awaiting triage. */
  async referralQueue(): Promise<Referral[]> {
    const res = await this.request<{ items: Referral[] }>("/access/referrals/queue");
    return res.items;
  }

  /** GET /access/referrals[?status=][?patient=] */
  async referrals(params: { status?: string; patient?: string } = {}): Promise<Referral[]> {
    const qs = new URLSearchParams();
    if (params.status) qs.set("status", params.status);
    if (params.patient) qs.set("patient", params.patient);
    const query = qs.toString();
    const res = await this.request<{ items: Referral[] }>(
      `/access/referrals${query ? `?${query}` : ""}`);
    return res.items;
  }

  /** GET /access/referrals/{id} — referral + triage task + linked auths. */
  getReferral(id: string): Promise<ReferralDetail> {
    return this.request(`/access/referrals/${encodeURIComponent(id)}`);
  }

  /** POST /access/referrals/{id}/transition { to_status, note? } */
  transitionReferral(id: string, toStatus: string, note?: string): Promise<Referral> {
    return this.request(`/access/referrals/${encodeURIComponent(id)}/transition`, {
      method: "POST",
      body: JSON.stringify({ to_status: toStatus, ...(note ? { note } : {}) }),
    });
  }

  /** POST /access/authorisations — open a prior-auth request. */
  createAuthorisation(input: {
    patient: string;
    payer: string;
    service_code: string;
    referral_id?: string;
    approved_units?: number;
    valid_from?: string;
    valid_to?: string;
    auth_number?: string;
    note?: string;
  }): Promise<PriorAuth> {
    return this.request("/access/authorisations", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** GET /access/authorisations[?patient=][?status=][?service_code=] */
  async authorisations(params: {
    patient?: string;
    status?: string;
    service_code?: string;
  } = {}): Promise<PriorAuth[]> {
    const qs = new URLSearchParams();
    if (params.patient) qs.set("patient", params.patient);
    if (params.status) qs.set("status", params.status);
    if (params.service_code) qs.set("service_code", params.service_code);
    const query = qs.toString();
    const res = await this.request<{ items: PriorAuth[] }>(
      `/access/authorisations${query ? `?${query}` : ""}`);
    return res.items;
  }

  /** POST /access/authorisations/{id}/transition — decision data
   *  (auth_number, approved_units, validity) may ride along. */
  transitionAuthorisation(
    id: string,
    toStatus: string,
    extra: Record<string, unknown> = {},
  ): Promise<PriorAuth> {
    return this.request(`/access/authorisations/${encodeURIComponent(id)}/transition`, {
      method: "POST",
      body: JSON.stringify({ to_status: toStatus, ...extra }),
    });
  }

  /** POST /access/eligibility/check — a RECORDED local-config
   *  eligibility evaluation (no live payer connection exists). */
  checkEligibility(input: {
    patient: string;
    payer: string;
    service_code?: string;
    service_date?: string;
  }): Promise<EligibilityCheckResult> {
    return this.request("/access/eligibility/check", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** GET /access/eligibility/checks[?patient=] — recorded checks. */
  async eligibilityChecks(patient?: string): Promise<EligibilityCheckResult[]> {
    const query = patient ? `?patient=${encodeURIComponent(patient)}` : "";
    const res = await this.request<{ items: EligibilityCheckResult[] }>(
      `/access/eligibility/checks${query}`);
    return res.items;
  }

  /** POST /access/waitlist — request a slot type (staff for any
   *  patient; a patient caller only for themselves). */
  joinWaitlist(input: {
    patient: string;
    slot_type: string;
    priority?: number;
    note?: string;
  }): Promise<WaitlistEntry> {
    return this.request("/access/waitlist", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** GET /access/waitlist[?status=][?patient=][?slot_type=] — queue
   *  order (priority, then oldest) is the server's. */
  async waitlist(params: {
    status?: string;
    patient?: string;
    slot_type?: string;
  } = {}): Promise<WaitlistEntry[]> {
    const qs = new URLSearchParams();
    if (params.status) qs.set("status", params.status);
    if (params.patient) qs.set("patient", params.patient);
    if (params.slot_type) qs.set("slot_type", params.slot_type);
    const query = qs.toString();
    const res = await this.request<{ items: WaitlistEntry[] }>(
      `/access/waitlist${query ? `?${query}` : ""}`);
    return res.items;
  }

  /** POST /access/waitlist/offers { appointment_id } — offer the
   *  freed (cancelled) slot to the first matching waiting entry. */
  offerWaitlistSlot(appointmentId: string): Promise<WaitlistOfferResult> {
    return this.request("/access/waitlist/offers", {
      method: "POST",
      body: JSON.stringify({ appointment_id: appointmentId }),
    });
  }

  /** POST /access/waitlist/{id}/transition { to_status } */
  transitionWaitlist(id: string, toStatus: string): Promise<WaitlistEntry> {
    return this.request(`/access/waitlist/${encodeURIComponent(id)}/transition`, {
      method: "POST",
      body: JSON.stringify({ to_status: toStatus }),
    });
  }

  /** POST /access/waitlist/{id}/accept { start, end?, practitioner? } —
   *  books a real Appointment and marks the entry booked. */
  acceptWaitlistOffer(
    id: string,
    input: { start: string; end?: string; practitioner?: string },
  ): Promise<WaitlistAcceptResult> {
    return this.request(`/access/waitlist/${encodeURIComponent(id)}/accept`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  // ---- Phase 7: revenue completion (/billing, app.revenue) — HIM
  // coding, 835 remittance, denials, statements + payments. ----

  /** GET /billing/coding/queue — finished / signed-note encounters
   *  with no coding record yet. */
  async codingQueue(): Promise<CodingQueueItem[]> {
    const res = await this.request<{ items: CodingQueueItem[] }>("/billing/coding/queue");
    return res.items;
  }

  /** POST /billing/coding/{encounterId} { diagnoses, procedures } —
   *  codes validate against the published ICD-10/CPT value sets. */
  assignCodes(
    encounterId: string,
    input: { diagnoses: Array<{ code: string }>; procedures?: Array<{ code: string }> },
  ): Promise<CodingRecord> {
    return this.request(`/billing/coding/${encodeURIComponent(encounterId)}`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** PUT /billing/coding/{encounterId} — amend an existing record. */
  amendCodes(
    encounterId: string,
    input: { diagnoses: Array<{ code: string }>; procedures?: Array<{ code: string }> },
  ): Promise<CodingRecord> {
    return this.request(`/billing/coding/${encodeURIComponent(encounterId)}`, {
      method: "PUT",
      body: JSON.stringify(input),
    });
  }

  /** GET /billing/coding/{encounterId} */
  getCodes(encounterId: string): Promise<CodingRecord> {
    return this.request(`/billing/coding/${encodeURIComponent(encounterId)}`);
  }

  /** POST /billing/remittances — ingest one X12 835 file as raw text.
   *  A malformed file is a 422 and posts nothing; unpostable CLP loops
   *  come back as batch exceptions, never silently dropped. */
  async ingest835(text: string): Promise<RemittanceIngestResult> {
    const headers = new Headers();
    const token = this.getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    headers.set("Content-Type", "text/plain");
    let response: Response;
    try {
      response = await this.fetchFn(`${this.baseUrl}/billing/remittances`, {
        method: "POST",
        headers,
        body: text,
      });
    } catch {
      throw new ApiError(0, `Cannot reach the OpenCare API at ${this.baseUrl}. Is the backend running?`);
    }
    const raw = await response.text();
    let data: unknown = undefined;
    if (raw) {
      try {
        data = JSON.parse(raw);
      } catch {
        data = undefined;
      }
    }
    if (!response.ok) {
      const detail =
        (data as { detail?: string } | undefined)?.detail ??
        `${response.status} ${response.statusText}`;
      throw new ApiError(response.status, detail);
    }
    return data as RemittanceIngestResult;
  }

  /** GET /billing/remittances — ingested batch history. */
  async remittanceBatches(): Promise<RemittanceBatchSummary[]> {
    const res = await this.request<{ items: RemittanceBatchSummary[] }>(
      "/billing/remittances");
    return res.items;
  }

  /** GET /billing/remittances/exceptions — unpostable CLP loops. */
  async remittanceExceptions(): Promise<RemittanceExceptionItem[]> {
    const res = await this.request<{ items: RemittanceExceptionItem[] }>(
      "/billing/remittances/exceptions");
    return res.items;
  }

  /** GET /billing/denials[?include_resolved=] — the denials
   *  workqueue (denied / partially-paid claims with their reasons). */
  denials(includeResolved = false): Promise<DenialsResponse> {
    const query = includeResolved ? "?include_resolved=true" : "";
    return this.request(`/billing/denials${query}`);
  }

  /** POST /billing/denials/{claimId}/assign { assignee } */
  assignDenial(claimId: string, assignee: string): Promise<DenialItem> {
    return this.request(`/billing/denials/${encodeURIComponent(claimId)}/assign`, {
      method: "POST",
      body: JSON.stringify({ assignee }),
    });
  }

  /** POST /billing/denials/{claimId}/note { note } */
  noteDenial(claimId: string, note: string): Promise<DenialItem> {
    return this.request(`/billing/denials/${encodeURIComponent(claimId)}/note`, {
      method: "POST",
      body: JSON.stringify({ note }),
    });
  }

  /** POST /billing/denials/{claimId}/resolve { outcome, outcome_claim_id? } */
  resolveDenial(
    claimId: string,
    outcome: string,
    outcomeClaimId?: string,
  ): Promise<DenialItem> {
    return this.request(`/billing/denials/${encodeURIComponent(claimId)}/resolve`, {
      method: "POST",
      body: JSON.stringify({
        outcome,
        ...(outcomeClaimId ? { outcome_claim_id: outcomeClaimId } : {}),
      }),
    });
  }

  /** POST /billing/claims/{id}/rebill — a NEW corrected claim linked
   *  to the denied/partially-paid original (which is preserved). */
  rebillClaim(id: string): Promise<Claim> {
    return this.request(`/billing/claims/${encodeURIComponent(id)}/rebill`, {
      method: "POST",
    });
  }

  /** GET /billing/statements/{patient} — per-claim money view +
   *  totals (a patient caller only ever gets their own). */
  statement(patient: string): Promise<PatientStatement> {
    return this.request(`/billing/statements/${encodeURIComponent(patient)}`);
  }

  /** POST /billing/claims/{id}/patient-payments { amount, method } —
   *  RECORDS a payment the practice took (no payment gateway). */
  recordPatientPayment(
    claimId: string,
    amount: number,
    method: string,
  ): Promise<PatientPaymentRecord> {
    return this.request(
      `/billing/claims/${encodeURIComponent(claimId)}/patient-payments`,
      { method: "POST", body: JSON.stringify({ amount, method }) });
  }

  /** GET /billing/claims/{id}/patient-payments */
  async patientPayments(claimId: string): Promise<PatientPaymentRecord[]> {
    const res = await this.request<{ items: PatientPaymentRecord[] }>(
      `/billing/claims/${encodeURIComponent(claimId)}/patient-payments`);
    return res.items;
  }

  // ---- Phase 4A: pharmacy (/pharmacy) ----
  // Formulary + stock come from the published Studio formulary config and
  // the server-side stock counters; BCMA administer enforces the
  // right-patient / right-medication / right-dispense checks server-side.

  /** GET /pharmacy/formulary — published Studio formulary. */
  formulary(): Promise<FormularyResponse> {
    return this.request("/pharmacy/formulary");
  }

  /** GET /pharmacy/stock — stock levels per formulary medication. */
  async stock(): Promise<StockItem[]> {
    const out = await this.request<{ items: StockItem[] }>("/pharmacy/stock");
    return out.items;
  }

  /** GET /pharmacy/dispense-queue — active requests awaiting dispensing. */
  async dispenseQueue(): Promise<DispenseQueueItem[]> {
    const out = await this.request<{ items: DispenseQueueItem[]; total: number }>(
      "/pharmacy/dispense-queue",
    );
    return out.items;
  }

  /** POST /pharmacy/dispense { medication_request_id, quantity? } */
  dispense(medicationRequestId: string, quantity?: number): Promise<DispenseOutcome> {
    return this.request("/pharmacy/dispense", {
      method: "POST",
      body: JSON.stringify({
        medication_request_id: medicationRequestId,
        ...(quantity ? { quantity } : {}),
      }),
    });
  }

  /** POST /pharmacy/administer — BCMA-lite scan { wristband_id,
   *  medication_code, dispense_id }; a failed right is a 422 naming it. */
  administer(input: {
    wristbandId: string;
    medicationCode: string;
    dispenseId: string;
  }): Promise<MedicationAdministration> {
    return this.request("/pharmacy/administer", {
      method: "POST",
      body: JSON.stringify({
        wristband_id: input.wristbandId,
        medication_code: input.medicationCode,
        dispense_id: input.dispenseId,
      }),
    });
  }

  // ---- Phase 11: pharmacy department depth (/pharmacy, app/pharmacy_ops) ----
  // Lot-level inventory + PAR worklists, DSCSA procurement, ADC restock,
  // controlled substances, compounding, the verification + DUR queues,
  // barcode payloads and the full five-rights BCMA scan. All thresholds
  // (PAR, SLA, BUD, prefixes) are server-side Studio config.

  /** GET /pharmacy/verification-queue — signed/active orders awaiting
   *  pharmacist verification, priority-ordered with SLA posture. */
  async pharmacyVerificationQueue(): Promise<VerificationQueueItem[]> {
    const out = await this.request<{ items: VerificationQueueItem[] }>(
      "/pharmacy/verification-queue",
    );
    return out.items;
  }

  /** POST /pharmacy/verification/{orderId}/verify — restricted items
   *  need restrictionAcknowledged; non-formulary items are refused. */
  verifyOrder(
    orderId: string,
    input: { restrictionAcknowledged?: boolean; note?: string } = {},
  ): Promise<VerificationOutcome> {
    return this.request(
      `/pharmacy/verification/${encodeURIComponent(orderId)}/verify`,
      {
        method: "POST",
        body: JSON.stringify({
          ...(input.restrictionAcknowledged
            ? { restriction_acknowledged: true }
            : {}),
          ...(input.note ? { note: input.note } : {}),
        }),
      },
    );
  }

  /** GET /pharmacy/dur-queue — orders carrying DUR signals, plus the
   *  interaction-checking status (inert without a knowledge source). */
  durQueue(): Promise<DurQueueResponse> {
    return this.request("/pharmacy/dur-queue");
  }

  /** GET /pharmacy/interactions/status — the integration point's state. */
  interactionStatus(): Promise<InteractionStatus> {
    return this.request("/pharmacy/interactions/status");
  }

  /** POST /pharmacy/interactions/check — pair-check medication codes;
   *  with no knowledge source configured this reports no-source and
   *  checks zero pairs (never a fabricated "no interactions"). */
  interactionCheck(medicationCodes: string[]): Promise<InteractionCheckResult> {
    return this.request("/pharmacy/interactions/check", {
      method: "POST",
      body: JSON.stringify({ medication_codes: medicationCodes }),
    });
  }

  /** GET /pharmacy/inventory — lots by medication + location. */
  inventory(): Promise<PharmacyInventoryResponse> {
    return this.request("/pharmacy/inventory");
  }

  /** GET /pharmacy/inventory/worklist — low-stock (vs PAR/reorder) and
   *  expiring/expired lot worklists. */
  inventoryWorklist(): Promise<InventoryWorklist> {
    return this.request("/pharmacy/inventory/worklist");
  }

  /** POST /pharmacy/inventory/adjust — audited lot adjustment; the
   *  reason is required and the lot can never go negative. */
  adjustStock(input: {
    medicationCode: string;
    location: string;
    lotNumber: string;
    delta: number;
    reason: string;
  }): Promise<AdjustStockResult> {
    return this.request("/pharmacy/inventory/adjust", {
      method: "POST",
      body: JSON.stringify({
        medication_code: input.medicationCode,
        location: input.location,
        lot_number: input.lotNumber,
        delta: input.delta,
        reason: input.reason,
      }),
    });
  }

  /** GET /pharmacy/procurement/orders — purchase orders, newest first. */
  async purchaseOrders(): Promise<PurchaseOrder[]> {
    const out = await this.request<{ items: PurchaseOrder[] }>(
      "/pharmacy/procurement/orders",
    );
    return out.items;
  }

  /** POST /pharmacy/procurement/orders — create a PO (status ordered). */
  createPurchaseOrder(input: {
    supplier: string;
    lines: Array<{ medicationCode: string; quantity: number }>;
    note?: string;
  }): Promise<PurchaseOrder> {
    return this.request("/pharmacy/procurement/orders", {
      method: "POST",
      body: JSON.stringify({
        supplier: input.supplier,
        lines: input.lines.map((l) => ({
          medication_code: l.medicationCode,
          quantity: l.quantity,
        })),
        ...(input.note ? { note: input.note } : {}),
      }),
    });
  }

  /** POST /pharmacy/procurement/orders/{poId}/receive — receiving
   *  captures DSCSA lot/expiry/serials per line into the ledger;
   *  validated in full before anything is written. */
  receivePurchaseOrder(
    poId: string,
    input: {
      location?: string;
      lines: Array<{
        medicationCode: string;
        quantity: number;
        lotNumber: string;
        expiryDate: string;
        serials?: string[];
      }>;
    },
  ): Promise<PurchaseOrder> {
    return this.request(
      `/pharmacy/procurement/orders/${encodeURIComponent(poId)}/receive`,
      {
        method: "POST",
        body: JSON.stringify({
          ...(input.location ? { location: input.location } : {}),
          lines: input.lines.map((l) => ({
            medication_code: l.medicationCode,
            quantity: l.quantity,
            lot_number: l.lotNumber,
            expiry_date: l.expiryDate,
            ...(l.serials ? { serials: l.serials } : {}),
          })),
        }),
      },
    );
  }

  /** GET /pharmacy/procurement/traceability — the DSCSA ledger. */
  async traceability(): Promise<TraceabilityEntry[]> {
    const out = await this.request<{ items: TraceabilityEntry[] }>(
      "/pharmacy/procurement/traceability",
    );
    return out.items;
  }

  /** GET /pharmacy/adc/restock-list?location= — PAR top-up list for one
   *  dispensing-cabinet location, with recent dispense demand. */
  async adcRestockList(location: string): Promise<AdcRestockItem[]> {
    const out = await this.request<{ items: AdcRestockItem[] }>(
      `/pharmacy/adc/restock-list?location=${encodeURIComponent(location)}`,
    );
    return out.items;
  }

  /** GET /pharmacy/cs/book — the controlled-substance count book:
   *  expected balances, unresolved discrepancies, recent waste. */
  csBook(): Promise<CsBookResponse> {
    return this.request("/pharmacy/cs/book");
  }

  /** POST /pharmacy/cs/counts — two-person shift count (expected vs
   *  counted); a variance opens a discrepancy on the book. */
  csCount(input: {
    medicationCode: string;
    location: string;
    countedQuantity: number;
    witness: string;
  }): Promise<CsCount> {
    return this.request("/pharmacy/cs/counts", {
      method: "POST",
      body: JSON.stringify({
        medication_code: input.medicationCode,
        location: input.location,
        counted_quantity: input.countedQuantity,
        witness: input.witness,
      }),
    });
  }

  /** POST /pharmacy/cs/counts/{countId}/resolve — a discrepancy must be
   *  resolved with a reason before the book balances again. */
  csResolve(countId: string, reason: string): Promise<CsCount> {
    return this.request(
      `/pharmacy/cs/counts/${encodeURIComponent(countId)}/resolve`,
      { method: "POST", body: JSON.stringify({ reason }) },
    );
  }

  /** POST /pharmacy/cs/waste — witnessed waste; the witness cannot be
   *  the person recording it. */
  csWaste(input: {
    medicationCode: string;
    location: string;
    lotNumber: string;
    quantity: number;
    reason: string;
    witness: string;
  }): Promise<CsWasteResult> {
    return this.request("/pharmacy/cs/waste", {
      method: "POST",
      body: JSON.stringify({
        medication_code: input.medicationCode,
        location: input.location,
        lot_number: input.lotNumber,
        quantity: input.quantity,
        reason: input.reason,
        witness: input.witness,
      }),
    });
  }

  /** GET /pharmacy/compounding/queue — batches awaiting verification,
   *  plus the published compounding categories (BUD config). */
  compoundingQueue(): Promise<CompoundingQueueResponse> {
    return this.request("/pharmacy/compounding/queue");
  }

  /** POST /pharmacy/compounding/batches — create a batch record; the
   *  beyond-use datetime is computed server-side from category config. */
  createCompoundBatch(input: {
    preparationName: string;
    category: string;
    storage: string;
    patientRef?: string;
    ingredients: Array<{
      medicationCode?: string;
      name?: string;
      quantity?: string;
      lotNumber: string;
    }>;
  }): Promise<CompoundBatch> {
    return this.request("/pharmacy/compounding/batches", {
      method: "POST",
      body: JSON.stringify({
        preparation_name: input.preparationName,
        category: input.category,
        storage: input.storage,
        ...(input.patientRef ? { patient_ref: input.patientRef } : {}),
        ingredients: input.ingredients.map((i) => ({
          ...(i.medicationCode ? { medication_code: i.medicationCode } : {}),
          ...(i.name ? { name: i.name } : {}),
          ...(i.quantity ? { quantity: i.quantity } : {}),
          lot_number: i.lotNumber,
        })),
      }),
    });
  }

  /** POST /pharmacy/compounding/batches/{batchId}/verify — the verifier
   *  cannot be the preparer. */
  verifyCompoundBatch(batchId: string): Promise<CompoundBatch> {
    return this.request(
      `/pharmacy/compounding/batches/${encodeURIComponent(batchId)}/verify`,
      { method: "POST", body: JSON.stringify({}) },
    );
  }

  /** POST /pharmacy/barcodes/wristband — wristband payload for a patient
   *  (patient id + DOB check digits, from the governed standard). */
  barcodeWristband(patientId: string): Promise<WristbandPayload> {
    return this.request("/pharmacy/barcodes/wristband", {
      method: "POST",
      body: JSON.stringify({ patient_id: patientId }),
    });
  }

  /** POST /pharmacy/barcodes/med-label — unit-dose label payload for a
   *  dispense (dispense id + allocated lot). */
  barcodeMedLabel(dispenseId: string): Promise<MedLabelPayload> {
    return this.request("/pharmacy/barcodes/med-label", {
      method: "POST",
      body: JSON.stringify({ dispense_id: dispenseId }),
    });
  }

  /** POST /pharmacy/barcodes/parse — parse + validate any payload. */
  barcodeParse(payload: string): Promise<BarcodeParseResult> {
    return this.request("/pharmacy/barcodes/parse", {
      method: "POST",
      body: JSON.stringify({ payload }),
    });
  }

  /** POST /pharmacy/bcma/administer — the full five-rights scan.
   *  Patient/medication/dispense mismatches are absolute hard stops;
   *  dose/route/time failures need an override reason, which is
   *  audited and stamped on the administration. */
  bcmaAdminister(input: {
    wristbandPayload: string;
    medLabelPayload: string;
    overrideReason?: string;
  }): Promise<BcmaOutcome> {
    return this.request("/pharmacy/bcma/administer", {
      method: "POST",
      body: JSON.stringify({
        wristband_payload: input.wristbandPayload,
        med_label_payload: input.medLabelPayload,
        ...(input.overrideReason
          ? { override_reason: input.overrideReason }
          : {}),
      }),
    });
  }

  /** GET /pharmacy/patients/{patientId}/dispenses — a patient's
   *  dispense history with allocated lots (MAR simulator feed). */
  async patientDispenses(patientId: string): Promise<PatientDispenseItem[]> {
    const out = await this.request<{ items: PatientDispenseItem[] }>(
      `/pharmacy/patients/${encodeURIComponent(patientId)}/dispenses`,
    );
    return out.items;
  }

  // ---- Phase 4B: ED board (/ed) ----
  // Tracking states + transitions come from the published Studio
  // workflow config (ed-flow) on the server; wait times are computed
  // server-side from the recorded arrival time.

  /** GET /ed/board — census grouped by tracking state, with waits. */
  edBoard(): Promise<EdBoard> {
    return this.request("/ed/board");
  }

  /** POST /ed/arrivals — register an ED arrival (waiting state). */
  edArrive(input: {
    patientId: string;
    chiefComplaint?: string;
    arrivedAt?: string;
  }): Promise<Encounter> {
    return this.request("/ed/arrivals", {
      method: "POST",
      body: JSON.stringify({
        patient: input.patientId,
        ...(input.chiefComplaint ? { chief_complaint: input.chiefComplaint } : {}),
        ...(input.arrivedAt ? { arrived_at: input.arrivedAt } : {}),
      }),
    });
  }

  /** POST /ed/encounters/{id}/transition — triage requires the ESI
   *  acuity (1-5); departed/admitted are governed (admit/depart calls). */
  edTransition(id: string, toStatus: string, acuity?: number): Promise<Encounter> {
    return this.request(`/ed/encounters/${encodeURIComponent(id)}/transition`, {
      method: "POST",
      body: JSON.stringify({
        to_status: toStatus,
        ...(acuity !== undefined ? { acuity } : {}),
      }),
    });
  }

  /** POST /ed/encounters/{id}/admit — creates the linked inpatient
   *  encounter and closes the ED encounter as admitted. */
  edAdmit(id: string, ward?: string): Promise<EdAdmitOutcome> {
    return this.request(`/ed/encounters/${encodeURIComponent(id)}/admit`, {
      method: "POST",
      body: JSON.stringify(ward ? { ward } : {}),
    });
  }

  /** POST /ed/encounters/{id}/depart — discharge/depart the ED visit. */
  edDepart(id: string, note?: string): Promise<{ ed_encounter: Encounter }> {
    return this.request(`/ed/encounters/${encodeURIComponent(id)}/depart`, {
      method: "POST",
      body: JSON.stringify(note ? { note } : {}),
    });
  }

  // ---- Phase 4B: radiology (/radiology, /dicomweb) ----
  // Studies are metadata-only (no pixel data in v1). The viewer URL is
  // built server-side from the published Studio integration config
  // (dicom-viewer -> viewer.baseUrl); the viewer itself is external.

  /** GET /radiology/modalities — published Studio modality list. */
  radiologyModalities(): Promise<ModalitiesResponse> {
    return this.request("/radiology/modalities");
  }

  /** GET /radiology/studies — study list with report status + viewer
   *  link per study. */
  async radiologyStudies(): Promise<RadiologyStudyItem[]> {
    const out = await this.request<RadiologyStudiesResponse>("/radiology/studies");
    return out.items;
  }

  /** GET /radiology/studies/{id} — study detail with its reports. */
  radiologyStudy(id: string): Promise<RadiologyStudyDetail> {
    return this.request(`/radiology/studies/${encodeURIComponent(id)}`);
  }

  /** POST /radiology/studies — create an ImagingStudy from a
   *  ServiceRequest; the modality is validated against Studio config. */
  createStudy(input: {
    serviceRequestId: string;
    modality: string;
    description?: string;
  }): Promise<RadiologyStudyItem> {
    return this.request("/radiology/studies", {
      method: "POST",
      body: JSON.stringify({
        service_request_id: input.serviceRequestId,
        modality: input.modality,
        ...(input.description ? { description: input.description } : {}),
      }),
    });
  }

  /** POST /radiology/studies/{id}/report — draft the imaging report
   *  (preliminary; one open draft per study). */
  createImagingReport(studyId: string, conclusion: string): Promise<DiagnosticReport> {
    return this.request(`/radiology/studies/${encodeURIComponent(studyId)}/report`, {
      method: "POST",
      body: JSON.stringify({ conclusion }),
    });
  }

  /** POST /radiology/reports/{id}/verify — radiologist sign-off
   *  (preliminary -> final; radiology.verify capability server-side). */
  verifyImagingReport(reportId: string): Promise<DiagnosticReport> {
    return this.request(`/radiology/reports/${encodeURIComponent(reportId)}/verify`, {
      method: "POST",
    });
  }

  // ---- Phase 4B: surgery board (/surgery) ----
  // Rooms are published Studio terminology config (or-rooms); the room
  // double-booking rule is enforced server-side on create + reschedule.

  /** GET /surgery/rooms — configured operating rooms. */
  async surgeryRooms(): Promise<ConfigConcept[]> {
    const out = await this.request<RoomsResponse>("/surgery/rooms");
    return out.rooms;
  }

  /** GET /surgery/board?date=YYYY-MM-DD — the day's cases by room. */
  surgeryBoard(date?: string): Promise<SurgeryBoard> {
    const qs = date ? `?date=${encodeURIComponent(date)}` : "";
    return this.request(`/surgery/board${qs}`);
  }

  /** POST /surgery/cases — schedule a case (409 when the room overlaps
   *  an existing case). */
  scheduleCase(input: {
    patientId: string;
    code: string;
    display?: string;
    room: string;
    start: string;
    end: string;
    surgeon?: string;
  }): Promise<SurgeryCaseOutcome> {
    return this.request("/surgery/cases", {
      method: "POST",
      body: JSON.stringify({
        patient_id: input.patientId,
        code: input.code,
        ...(input.display ? { display: input.display } : {}),
        room: input.room,
        start: input.start,
        end: input.end,
        ...(input.surgeon ? { surgeon: input.surgeon } : {}),
      }),
    });
  }

  /** POST /surgery/cases/{id}/transition { to_status } */
  transitionCase(id: string, toStatus: string): Promise<SurgeryCaseOutcome> {
    return this.request(`/surgery/cases/${encodeURIComponent(id)}/transition`, {
      method: "POST",
      body: JSON.stringify({ to_status: toStatus }),
    });
  }

  /** POST /surgery/cases/{id}/reschedule { start, end } — the overlap
   *  rule is enforced again on the move (409). */
  rescheduleCase(id: string, start: string, end: string): Promise<SurgeryCaseOutcome> {
    return this.request(`/surgery/cases/${encodeURIComponent(id)}/reschedule`, {
      method: "POST",
      body: JSON.stringify({ start, end }),
    });
  }

  // ---- Phase 5: general orders (/orders) ----
  // Order types and order sets are published Studio config; the
  // lifecycle is the published workflow/order-flow machine on the
  // server. Allergy conflicts are a 422 naming the allergen unless an
  // override reason is supplied (recorded on the order server-side).

  /** GET /orders/types — the published order-type catalogue. */
  orderTypes(): Promise<OrderTypesResponse> {
    return this.request("/orders/types");
  }

  /** GET /orders/sets — the published order sets. */
  orderSets(): Promise<OrderSetsResponse> {
    return this.request("/orders/sets");
  }

  /** POST /orders — place one order (starts in draft). */
  placeOrder(input: {
    type: string;
    code: string;
    display?: string;
    patient: string;
    performerRole?: string;
    modality?: string;
    encounter?: string;
    instructions?: string;
    allergyOverrideReason?: string;
    dosageInstruction?: DosageInstruction[];
    dosage_text?: string;
  }): Promise<PlaceOrderOutcome> {
    return this.request("/orders", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /orders/from-set — expand a published order set into
   *  individual draft orders (validated as a whole server-side). */
  placeOrderSet(input: { set: string; patient: string }): Promise<PlaceOrderSetOutcome> {
    return this.request("/orders/from-set", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** GET /orders?patient= — the patient's orders (all states). */
  orders(patient: string, params: { type?: string; status?: string } = {}): Promise<OrdersListResponse> {
    const qs = new URLSearchParams({ patient });
    if (params.type) qs.set("type", params.type);
    if (params.status) qs.set("status", params.status);
    return this.request(`/orders?${qs.toString()}`);
  }

  /** POST /orders/{id}/transition { to_status, ... } — sign / activate /
   *  complete / discontinue / cancel through the published workflow. */
  transitionOrder(
    id: string,
    toStatus: string,
    extra: { allergyOverrideReason?: string } = {},
  ): Promise<PlacedOrder> {
    return this.request(`/orders/${encodeURIComponent(id)}/transition`, {
      method: "POST",
      body: JSON.stringify({ to_status: toStatus, ...extra }),
    });
  }

  /** GET /orders/mar/due — the computed MAR for a window: scheduled
   *  doses from signed/active medication orders, classified given /
   *  due / missed / scheduled against BCMA administration records. */
  marDue(patient: string, from: string, to: string): Promise<MarDueResponse> {
    const qs = new URLSearchParams({ patient, from, to });
    return this.request(`/orders/mar/due?${qs.toString()}`);
  }

  // ---- Wave A (CHART-B2): chart allergies ----
  // AllergyIntolerance is a first-class resource on the generic FHIR
  // surface: list/create ride search()/create(); the server validates
  // code + patient.reference. There is no generic PUT for this type —
  // an edit re-POSTs the full resource under its existing id, which
  // the FHIR create path upserts (version increments, body replaces;
  // pinned by backend/tests/test_wave_a_chart.py).

  /** GET /fhir/AllergyIntolerance?patient= — the patient's allergy
   *  list (the single source the chart banner, the allergies tab,
   *  the diet tray-check and DUR all read). */
  allergies(patient: string): Promise<AllergyIntolerance[]> {
    return this.search<AllergyIntolerance>("AllergyIntolerance", { patient });
  }

  /** POST /fhir/AllergyIntolerance — record a new allergy (or NKDA). */
  createAllergy(body: Record<string, unknown>): Promise<AllergyIntolerance> {
    return this.create<AllergyIntolerance>("AllergyIntolerance", body);
  }

  /** Edit an existing allergy (status transitions, verification):
   *  re-POST under the same id — see the upsert note above. */
  updateAllergy(body: Record<string, unknown>): Promise<AllergyIntolerance> {
    return this.create<AllergyIntolerance>("AllergyIntolerance", body);
  }

  // ---- Phase 5: clinical documentation (/notes, /flowsheets) ----

  /** GET /notes/types — the published note-type catalogue. */
  noteTypes(): Promise<NoteTypesResponse> {
    return this.request("/notes/types");
  }

  /** POST /notes — author a note (preliminary). Template answers are
   *  validated server-side against the type's published form. */
  createNote(input: {
    patient: string;
    noteType: string;
    text?: string;
    encounter?: string;
    answers?: QuestionnaireAnswerItem[];
  }): Promise<ClinicalNote> {
    return this.request("/notes", {
      method: "POST",
      body: JSON.stringify({
        patient: input.patient,
        note_type: input.noteType,
        ...(input.text ? { text: input.text } : {}),
        ...(input.encounter ? { encounter: input.encounter } : {}),
        ...(input.answers ? { answers: input.answers } : {}),
      }),
    });
  }

  /** GET /notes?patient= — the patient's notes, newest first. */
  notes(patient: string): Promise<NotesListResponse> {
    const qs = new URLSearchParams({ patient });
    return this.request(`/notes?${qs.toString()}`);
  }

  /** GET /notes/{id} */
  getNote(id: string): Promise<ClinicalNote> {
    return this.request(`/notes/${encodeURIComponent(id)}`);
  }

  /** POST /notes/{id}/sign — preliminary → final (409 if not a draft). */
  signNote(id: string): Promise<ClinicalNote> {
    return this.request(`/notes/${encodeURIComponent(id)}/sign`, { method: "POST" });
  }

  /** POST /notes/{id}/amend — creates a NEW linked DocumentReference;
   *  the original is never modified server-side. */
  amendNote(
    id: string,
    input: { text?: string; answers?: QuestionnaireAnswerItem[] },
  ): Promise<ClinicalNote> {
    return this.request(`/notes/${encodeURIComponent(id)}/amend`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /flowsheets — record one flowsheet entry (a
   *  QuestionnaireResponse against the published form). */
  submitFlowsheet(input: {
    patient: string;
    form: string;
    answers: QuestionnaireAnswerItem[];
  }): Promise<FhirResource> {
    return this.request("/flowsheets", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** GET /flowsheets/{form}?patient= — time-series rows for the grid. */
  flowsheetView(formKey: string, patient: string): Promise<FlowsheetView> {
    const qs = new URLSearchParams({ patient });
    return this.request(`/flowsheets/${encodeURIComponent(formKey)}?${qs.toString()}`);
  }

  // ---- Phase 6: inpatient operations (/inpatient) ----
  // Locations, dispositions, ADT labels and the checklist /
  // anaesthesia forms are published Studio config; beds carry live
  // status server-side and every movement writes an ADT event.

  /** GET /inpatient/locations — the config hierarchy + live bed status. */
  inpatientLocations(): Promise<LocationsResponse> {
    return this.request("/inpatient/locations");
  }

  /** GET /inpatient/census — per-unit counts + occupancy + today's
   *  expected discharges. */
  inpatientCensus(): Promise<Census> {
    return this.request("/inpatient/census");
  }

  /** GET /inpatient/beds — patient-per-bed listing. */
  inpatientBeds(): Promise<{ beds: BedEntry[]; total: number }> {
    return this.request("/inpatient/beds");
  }

  /** POST /inpatient/beds/{code}/status — housekeeping / bed-management
   *  flip (never occupancy; an occupied bed is a 409). */
  setBedStatus(code: string, status: string): Promise<{ bed: BedEntry }> {
    return this.request(`/inpatient/beds/${encodeURIComponent(code)}/status`, {
      method: "POST",
      body: JSON.stringify({ status }),
    });
  }

  /** POST /inpatient/admissions — admit a patient into a bed (A01). */
  admitPatient(input: { patient: string; bed: string; reason?: string }): Promise<AdmitOutcome> {
    return this.request("/inpatient/admissions", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /inpatient/encounters/{id}/assign-bed — bed for an admission
   *  created elsewhere (e.g. the ED board's admit). */
  assignBed(encounterId: string, bed: string): Promise<AdmitOutcome> {
    return this.request(
      `/inpatient/encounters/${encodeURIComponent(encounterId)}/assign-bed`,
      { method: "POST", body: JSON.stringify({ bed }) },
    );
  }

  /** POST /inpatient/encounters/{id}/transfer — move beds (A02; the
   *  old bed goes to cleaning). */
  transferPatient(encounterId: string, bed: string): Promise<AdmitOutcome> {
    return this.request(
      `/inpatient/encounters/${encodeURIComponent(encounterId)}/transfer`,
      { method: "POST", body: JSON.stringify({ bed }) },
    );
  }

  /** POST /inpatient/encounters/{id}/discharge — close the encounter
   *  through the inpatient workflow + release the bed (A03). */
  dischargePatient(
    encounterId: string,
    disposition?: string,
  ): Promise<{ encounter: Encounter; bed_code_released?: string | null; disposition?: string | null }> {
    return this.request(
      `/inpatient/encounters/${encodeURIComponent(encounterId)}/discharge`,
      { method: "POST", body: JSON.stringify(disposition ? { disposition } : {}) },
    );
  }

  /** GET /inpatient/encounters/{id} — one admission's detail. */
  admissionDetail(encounterId: string): Promise<AdmissionDetail> {
    return this.request(`/inpatient/encounters/${encodeURIComponent(encounterId)}`);
  }

  /** GET /inpatient/patients/{id}/admission — the current admission. */
  patientAdmission(patientId: string): Promise<{ admission: AdmissionDetail | null }> {
    return this.request(
      `/inpatient/patients/${encodeURIComponent(patientId)}/admission`,
    );
  }

  /** GET /inpatient/adt-events — the ADT movement log. */
  adtEvents(params?: { encounterId?: string; patient?: string }): Promise<AdtEventsResponse> {
    const qs = new URLSearchParams();
    if (params?.encounterId) qs.set("encounter_id", params.encounterId);
    if (params?.patient) qs.set("patient", params.patient);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return this.request(`/inpatient/adt-events${suffix}`);
  }

  /** GET /inpatient/discharge-dispositions — the published value set. */
  dischargeDispositions(): Promise<DispositionsResponse> {
    return this.request("/inpatient/discharge-dispositions");
  }

  /** POST /inpatient/encounters/{id}/discharge-plan — record the plan
   *  (target date / disposition / checklist linkage; writes an A08). */
  setDischargePlan(
    encounterId: string,
    plan: DischargePlan & { checklistResponseId?: string },
  ): Promise<{ encounter_id: string; discharge_plan: DischargePlan }> {
    return this.request(
      `/inpatient/encounters/${encodeURIComponent(encounterId)}/discharge-plan`,
      { method: "POST", body: JSON.stringify(plan) },
    );
  }

  /** GET /inpatient/expected-discharges?date= — the planning feed. */
  expectedDischarges(date?: string): Promise<ExpectedDischargesResponse> {
    const qs = date ? `?date=${encodeURIComponent(date)}` : "";
    return this.request(`/inpatient/expected-discharges${qs}`);
  }

  /** POST /inpatient/encounters/{id}/care-plan — add a case-management item. */
  addCarePlanItem(encounterId: string, description: string): Promise<{ task: Task }> {
    return this.request(
      `/inpatient/encounters/${encodeURIComponent(encounterId)}/care-plan`,
      { method: "POST", body: JSON.stringify({ description }) },
    );
  }

  /** GET /inpatient/encounters/{id}/care-plan — the item list. */
  carePlan(encounterId: string): Promise<CarePlanResponse> {
    return this.request(
      `/inpatient/encounters/${encodeURIComponent(encounterId)}/care-plan`,
    );
  }

  /** POST /inpatient/care-plan/{taskId}/transition — advance an item. */
  transitionCarePlanItem(taskId: string, to: string): Promise<{ task: Task }> {
    return this.request(
      `/inpatient/care-plan/${encodeURIComponent(taskId)}/transition`,
      { method: "POST", body: JSON.stringify({ to }) },
    );
  }

  /** POST /inpatient/surgery-cases/{id}/preop-checklist — submit the
   *  published pre-op checklist form for a case. */
  submitPreopChecklist(
    caseId: string,
    answers: QuestionnaireAnswerItem[],
  ): Promise<{ case: SurgeryCaseEntry; response: QuestionnaireResponse }> {
    return this.request(
      `/inpatient/surgery-cases/${encodeURIComponent(caseId)}/preop-checklist`,
      { method: "POST", body: JSON.stringify({ answers }) },
    );
  }

  /** POST /inpatient/surgery-cases/{id}/anaesthesia — submit the
   *  published anaesthesia record form for a case. */
  submitAnaesthesia(
    caseId: string,
    answers: QuestionnaireAnswerItem[],
  ): Promise<{ case: SurgeryCaseEntry; response: QuestionnaireResponse }> {
    return this.request(
      `/inpatient/surgery-cases/${encodeURIComponent(caseId)}/anaesthesia`,
      { method: "POST", body: JSON.stringify({ answers }) },
    );
  }

  /** POST /inpatient/surgery-cases/{id}/anaesthesia-vitals — one
   *  reading on the case's anaesthesia vitals flowsheet. */
  submitAnaesthesiaVitals(
    caseId: string,
    answers: QuestionnaireAnswerItem[],
  ): Promise<{ case_id: string; response: QuestionnaireResponse; recorded_at?: string }> {
    return this.request(
      `/inpatient/surgery-cases/${encodeURIComponent(caseId)}/anaesthesia-vitals`,
      { method: "POST", body: JSON.stringify({ answers }) },
    );
  }

  /** GET /inpatient/surgery-cases/{id}/record — timeline + pre-op +
   *  anaesthesia record + vitals series for one case. */
  surgeryCaseRecord(caseId: string): Promise<SurgeryCaseRecord> {
    return this.request(
      `/inpatient/surgery-cases/${encodeURIComponent(caseId)}/record`,
    );
  }

  // ---------------------------------------------------------------------
  // Phase 8 — Interoperability (interface engine admin)
  // ---------------------------------------------------------------------

  /** GET /interop/endpoints — effective endpoint config (published
   *  Studio integration/hl7-endpoints) + per-endpoint queue tallies. */
  interopEndpoints(): Promise<InteropEndpointsResponse> {
    return this.request("/interop/endpoints");
  }

  /** GET /interop/messages — the unified interface log. */
  interopMessages(params: {
    direction?: string;
    type?: string;
    status?: string;
    patient?: string;
    limit?: number;
  } = {}): Promise<InteropMessagesResponse> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") query.set(key, String(value));
    }
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return this.request(`/interop/messages${suffix}`);
  }

  /** GET /interop/dead-letters — exhausted messages with history. */
  interopDeadLetters(): Promise<InteropDeadLettersResponse> {
    return this.request("/interop/dead-letters");
  }

  /** POST /interop/dead-letters/{id}/replay — re-queue one dead
   *  letter (attempt history preserved server-side). */
  replayDeadLetter(id: number): Promise<InteropMessage> {
    return this.request(`/interop/dead-letters/${id}/replay`, { method: "POST" });
  }

  /** POST /interop/dispatch — run one dispatcher pass now. */
  dispatchInterop(): Promise<{ stats: DispatchStats }> {
    return this.request("/interop/dispatch", { method: "POST" });
  }

  // ---------------------------------------------------------------------
  // Phase 9 — Quality measures (/quality), research workbench
  // (/pophealth snapshots + export), messaging (/messages, /portal),
  // telehealth + self-scheduling (/portal, /engagement)
  // ---------------------------------------------------------------------

  /** GET /quality/measures — published Studio measure configs. */
  qualityMeasures(): Promise<{ measures: QualityMeasureEntry[]; total: number }> {
    return this.request("/quality/measures");
  }

  /** GET /quality/measures/{key}/result — denominator/numerator/rate
   *  with small-cell suppression applied server-side. */
  qualityMeasureResult(key: string): Promise<QualityMeasureResult> {
    return this.request(`/quality/measures/${encodeURIComponent(key)}/result`);
  }

  /** GET /pophealth/registries/{key}/snapshots — snapshot time series. */
  registrySnapshots(key: string): Promise<CohortSnapshotsResponse> {
    return this.request(
      `/pophealth/registries/${encodeURIComponent(key)}/snapshots`,
    );
  }

  /** POST /pophealth/registries/{key}/snapshots — record the current
   *  member count (pophealth.run; admin by default). */
  takeRegistrySnapshot(key: string): Promise<CohortSnapshot> {
    return this.request(
      `/pophealth/registries/${encodeURIComponent(key)}/snapshots`,
      { method: "POST" },
    );
  }

  /** GET /pophealth/registries/{key}/export — de-identified aggregate
   *  export (pophealth.export capability; audited server-side). */
  exportRegistry(key: string): Promise<CohortExport> {
    return this.request(
      `/pophealth/registries/${encodeURIComponent(key)}/export`,
    );
  }

  /** GET /messages/inbox?patient= — staff thread summaries for one
   *  patient (messages.use capability). */
  messageInbox(patient: string): Promise<{
    items: MessageThreadSummary[];
    total: number;
    patient: string;
  }> {
    return this.request(
      `/messages/inbox?patient=${encodeURIComponent(patient)}`,
    );
  }

  /** GET /messages/threads/{id} — one thread's messages. Patients get
   *  a 404 for another patient's thread (non-disclosure). */
  messageThread(threadId: string): Promise<MessageThreadDetail> {
    return this.request(`/messages/threads/${encodeURIComponent(threadId)}`);
  }

  /** POST /messages { patient, text } — staff starts a thread. */
  sendMessage(patient: string, text: string): Promise<PortalMessage> {
    return this.request("/messages", {
      method: "POST",
      body: JSON.stringify({ patient, text }),
    });
  }

  /** POST /messages/threads/{id}/reply { text }. */
  replyToThread(threadId: string, text: string): Promise<PortalMessage> {
    return this.request(
      `/messages/threads/${encodeURIComponent(threadId)}/reply`,
      { method: "POST", body: JSON.stringify({ text }) },
    );
  }

  /** POST /messages/threads/{id}/read — read receipt for the caller. */
  markThreadRead(threadId: string): Promise<{ thread_id: string; marked: number }> {
    return this.request(
      `/messages/threads/${encodeURIComponent(threadId)}/read`,
      { method: "POST" },
    );
  }

  /** GET /messages/unread — staff unread total across threads. */
  messageUnread(): Promise<{ unread: number }> {
    return this.request("/messages/unread");
  }

  /** GET /portal/my-messages — the patient's own thread summaries. */
  myMessages(): Promise<{ items: MessageThreadSummary[]; total: number }> {
    return this.request("/portal/my-messages");
  }

  /** POST /portal/messages { text } — patient starts a thread (the
   *  subject is always the caller's own record, server-side). */
  sendPortalMessage(text: string): Promise<PortalMessage> {
    return this.request("/portal/messages", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
  }

  /** GET /portal/my-appointments — the patient's own appointments. */
  myAppointments(): Promise<{ items: FhirResource[]; total: number }> {
    return this.request("/portal/my-appointments");
  }

  /** GET /portal/self-scheduling/config — auto-confirm flag + the
   *  published slot types a request may name. */
  selfSchedulingConfig(): Promise<SelfSchedulingConfig> {
    return this.request("/portal/self-scheduling/config");
  }

  /** POST /portal/appointments/request — request an appointment from
   *  a published slot type; proposed (staff-confirm) or booked
   *  (autoConfirm published config). */
  requestAppointment(input: {
    slot_type: string;
    start: string;
    reason?: string;
  }): Promise<AppointmentRequestOutcome> {
    return this.request("/portal/appointments/request", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** GET /portal/telehealth/appointments/{id}/workspace — the
   *  patient's telehealth visit workspace (join URL is the org's
   *  configured provider link — data, not an OpenCare video call). */
  telehealthWorkspace(appointmentId: string): Promise<TelehealthWorkspace> {
    return this.request(
      `/portal/telehealth/appointments/${encodeURIComponent(appointmentId)}/workspace`,
    );
  }

  /** POST /portal/telehealth/appointments/{id}/intake — submit the
   *  published pre-visit intake form's answers. */
  submitTelehealthIntake(
    appointmentId: string,
    answers: QuestionnaireAnswerItem[],
  ): Promise<FhirResource> {
    return this.request(
      `/portal/telehealth/appointments/${encodeURIComponent(appointmentId)}/intake`,
      { method: "POST", body: JSON.stringify({ answers }) },
    );
  }

  /** GET /engagement/telehealth/appointments/{id}/workspace — staff
   *  view of the same workspace. */
  staffTelehealthWorkspace(appointmentId: string): Promise<TelehealthWorkspace> {
    return this.request(
      `/engagement/telehealth/appointments/${encodeURIComponent(appointmentId)}/workspace`,
    );
  }

  // ---- Phase 12: care-continuum operations (/continuum, app/continuum) ----
  // ER operations, inpatient operations, outpatient operations,
  // aftercare, the Capacity Command Center and safety operations. All
  // thresholds/timers/templates come from the published
  // terminology/continuum-ops config on the server.

  /** GET /continuum/er/board — the Phase 4B ED board enriched with
   *  per-ESI target timers (config), split-flow queues and the
   *  diversion/surge banner. */
  continuumErBoard(): Promise<ErOpsBoard> {
    return this.request("/continuum/er/board");
  }

  /** GET /continuum/er/boarding — admitted patients waiting for beds. */
  continuumBoarding(): Promise<BoardingBoard> {
    return this.request("/continuum/er/boarding");
  }

  /** POST /continuum/er/diversion — set the facility diversion state
   *  and surge level (config-driven states; audited server-side). */
  setDiversion(input: { status: string; surgeLevel?: string; reason?: string }): Promise<{
    diversion: string; surge_level: string; updated_by: string;
  }> {
    return this.request("/continuum/er/diversion", {
      method: "POST",
      body: JSON.stringify({
        status: input.status,
        ...(input.surgeLevel ? { surge_level: input.surgeLevel } : {}),
        ...(input.reason ? { reason: input.reason } : {}),
      }),
    });
  }

  /** POST /continuum/er/activations — start a STEMI/stroke/trauma
   *  activation timer (pathway targets copied from config). */
  startActivation(encounterId: string, pathway: string): Promise<Activation> {
    return this.request("/continuum/er/activations", {
      method: "POST",
      body: JSON.stringify({ encounter_id: encounterId, pathway }),
    });
  }

  /** GET /continuum/er/activations?status=open */
  async listActivations(status = "open"): Promise<Activation[]> {
    const out = await this.request<{ items: Activation[] }>(
      `/continuum/er/activations?status=${encodeURIComponent(status)}`,
    );
    return out.items;
  }

  /** POST /continuum/er/activations/{id}/milestone */
  recordActivationMilestone(id: string, code: string): Promise<Activation> {
    return this.request(
      `/continuum/er/activations/${encodeURIComponent(id)}/milestone`,
      { method: "POST", body: JSON.stringify({ code }) },
    );
  }

  /** POST /continuum/er/activations/{id}/close */
  closeActivation(id: string, outcome?: string): Promise<Activation> {
    return this.request(
      `/continuum/er/activations/${encodeURIComponent(id)}/close`,
      { method: "POST", body: JSON.stringify({ outcome }) },
    );
  }

  /** POST /continuum/er/encounters/{id}/lwbs — capture a
   *  left-without-being-seen departure from waiting/triage. */
  markLwbs(encounterId: string, note?: string): Promise<unknown> {
    return this.request(
      `/continuum/er/encounters/${encodeURIComponent(encounterId)}/lwbs`,
      { method: "POST", body: JSON.stringify({ note }) },
    );
  }

  /** POST /continuum/er/encounters/{id}/mse — record the EMTALA
   *  medical screening exam (governed form answers). */
  recordMse(encounterId: string, answers: QuestionnaireAnswerItem[]): Promise<unknown> {
    return this.request(
      `/continuum/er/encounters/${encodeURIComponent(encounterId)}/mse`,
      { method: "POST", body: JSON.stringify({ answers }) },
    );
  }

  /** POST /continuum/er/encounters/{id}/transfer-out — EMTALA transfer;
   *  the server refuses (422) without an MSE on file and a complete
   *  transfer certification. */
  recordTransferOut(encounterId: string, answers: QuestionnaireAnswerItem[]): Promise<unknown> {
    return this.request(
      `/continuum/er/encounters/${encodeURIComponent(encounterId)}/transfer-out`,
      { method: "POST", body: JSON.stringify({ answers }) },
    );
  }

  /** GET /continuum/er/callbacks — results-callback queue for departed
   *  ED patients whose results are still pending. */
  callbackQueue(): Promise<CallbackQueue> {
    return this.request("/continuum/er/callbacks");
  }

  /** POST /continuum/er/callbacks/{reportId}/complete */
  completeCallback(reportId: string, outcome: string, note?: string): Promise<unknown> {
    return this.request(
      `/continuum/er/callbacks/${encodeURIComponent(reportId)}/complete`,
      { method: "POST", body: JSON.stringify({ outcome, note }) },
    );
  }

  /** GET /continuum/inpatient/assignments — acuity-based nurse
   *  assignments with per-nurse loads and overload flags. */
  nurseAssignments(unit?: string): Promise<AssignmentBoard> {
    const qs = unit ? `?unit=${encodeURIComponent(unit)}` : "";
    return this.request(`/continuum/inpatient/assignments${qs}`);
  }

  /** POST /continuum/inpatient/assignments */
  assignNurse(input: { encounterId: string; nurse: string; acuity: number }): Promise<unknown> {
    return this.request("/continuum/inpatient/assignments", {
      method: "POST",
      body: JSON.stringify({
        encounter_id: input.encounterId, nurse: input.nurse, acuity: input.acuity,
      }),
    });
  }

  /** GET /continuum/inpatient/discharge-readiness — the readiness
   *  board behind the published discharge-readiness dashboard. */
  dischargeReadiness(): Promise<DischargeReadiness> {
    return this.request("/continuum/inpatient/discharge-readiness");
  }

  /** POST /continuum/inpatient/handoffs — an I-PASS handoff record
   *  (governed form; all five sections required). */
  recordHandoff(encounterId: string, answers: QuestionnaireAnswerItem[]): Promise<unknown> {
    return this.request("/continuum/inpatient/handoffs", {
      method: "POST",
      body: JSON.stringify({ encounter_id: encounterId, answers }),
    });
  }

  /** GET /continuum/inpatient/evs — EVS bed-turnaround tasks. */
  async evsTasks(): Promise<EvsItem[]> {
    const out = await this.request<{ items: EvsItem[] }>("/continuum/inpatient/evs");
    return out.items;
  }

  /** POST /continuum/inpatient/evs/{taskId}/complete */
  completeEvsTask(taskId: string): Promise<unknown> {
    return this.request(
      `/continuum/inpatient/evs/${encodeURIComponent(taskId)}/complete`,
      { method: "POST", body: JSON.stringify({}) },
    );
  }

  /** GET /continuum/clinic/templates — session templates from config. */
  async clinicTemplates(): Promise<ClinicSessionTemplate[]> {
    const out = await this.request<{ items: ClinicSessionTemplate[] }>(
      "/continuum/clinic/templates",
    );
    return out.items;
  }

  /** GET /continuum/clinic/day?date= — one clinic day: sessions with
   *  booked/overbooked counts plus the rooming queue. */
  clinicDay(date: string): Promise<ClinicDay> {
    return this.request(`/continuum/clinic/day?date=${encodeURIComponent(date)}`);
  }

  /** POST /continuum/clinic/book — book into a session template;
   *  overbooking beyond the configured limit is refused (409). */
  clinicBook(input: { patient: string; session: string; date: string }): Promise<{
    appointment: Appointment; start: string; overbooked: boolean;
  }> {
    return this.request("/continuum/clinic/book", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /continuum/clinic/appointments/{id}/check-in — mark a
   *  booked patient arrived (UX audit F-1). Accepts the front
   *  desk's registration.work or continuum.work server-side. */
  clinicCheckIn(appointmentId: string): Promise<{
    appointment_id: string; status: string; patient: string | null;
  }> {
    return this.request(
      `/continuum/clinic/appointments/${encodeURIComponent(appointmentId)}/check-in`,
      { method: "POST", body: JSON.stringify({}) },
    );
  }

  /** POST /continuum/clinic/appointments/{id}/room */
  roomAppointment(appointmentId: string, room: string): Promise<unknown> {
    return this.request(
      `/continuum/clinic/appointments/${encodeURIComponent(appointmentId)}/room`,
      { method: "POST", body: JSON.stringify({ room }) },
    );
  }

  /** GET /continuum/clinic/no-show-risks?date= — deterministic
   *  no-show flags from each patient's own visit history. */
  async noShowRisks(date: string): Promise<NoShowRiskItem[]> {
    const out = await this.request<{ items: NoShowRiskItem[] }>(
      `/continuum/clinic/no-show-risks?date=${encodeURIComponent(date)}`,
    );
    return out.items;
  }

  /** GET /continuum/referrals/loop — referral loop-closure stages
   *  with ageing/stale flags. */
  async referralLoop(): Promise<ReferralLoopItem[]> {
    const out = await this.request<{ items: ReferralLoopItem[] }>(
      "/continuum/referrals/loop",
    );
    return out.items;
  }

  /** POST /continuum/referrals/{id}/report-back — close the loop once
   *  the consultation report is back. */
  referralReportBack(referralId: string, note?: string): Promise<unknown> {
    return this.request(
      `/continuum/referrals/${encodeURIComponent(referralId)}/report-back`,
      { method: "POST", body: JSON.stringify({ note }) },
    );
  }

  /** GET /continuum/inbox — the asynchronous inbox across tasks,
   *  messages, results and callbacks with per-type SLA breach flags. */
  continuumInbox(type?: string): Promise<ContinuumInbox> {
    const qs = type ? `?type=${encodeURIComponent(type)}` : "";
    return this.request(`/continuum/inbox${qs}`);
  }

  /** POST /continuum/tasks/{taskId}/transition — work an inbox task. */
  transitionContinuumTask(taskId: string, toStatus: string): Promise<unknown> {
    return this.request(
      `/continuum/tasks/${encodeURIComponent(taskId)}/transition`,
      { method: "POST", body: JSON.stringify({ to_status: toStatus }) },
    );
  }

  /** GET /continuum/panels/gaps — panel care-gap worklist computed
   *  from registry/quality data. */
  async careGaps(): Promise<CareGapItem[]> {
    const out = await this.request<{ items: CareGapItem[] }>("/continuum/panels/gaps");
    return out.items;
  }

  /** POST /continuum/panels/gaps/outreach — generate (idempotently)
   *  the outreach task for one patient's care gap. */
  careGapOutreach(gapCode: string, patient: string): Promise<unknown> {
    return this.request("/continuum/panels/gaps/outreach", {
      method: "POST",
      body: JSON.stringify({ gap_code: gapCode, patient }),
    });
  }

  /** POST /continuum/aftercare/teach-back — discharge teach-back
   *  sign-off (governed form). */
  recordTeachBack(encounterId: string, answers: QuestionnaireAnswerItem[]): Promise<unknown> {
    return this.request("/continuum/aftercare/teach-back", {
      method: "POST",
      body: JSON.stringify({ encounter_id: encounterId, answers }),
    });
  }

  /** GET /continuum/aftercare/readiness/{encounterId} */
  aftercareReadiness(encounterId: string): Promise<ReadinessItem> {
    return this.request(
      `/continuum/aftercare/readiness/${encodeURIComponent(encounterId)}`,
    );
  }

  /** GET /continuum/aftercare/calls — the 48–72h post-discharge call
   *  queue (window from config). */
  async aftercareCalls(): Promise<CallQueueItem[]> {
    const out = await this.request<{ items: CallQueueItem[] }>(
      "/continuum/aftercare/calls",
    );
    return out.items;
  }

  /** POST /continuum/aftercare/calls — record one call's scripted
   *  outcome (outcomes list from config). */
  recordAftercareCall(input: {
    encounterId: string; outcome: string; reached?: boolean; note?: string;
  }): Promise<unknown> {
    return this.request("/continuum/aftercare/calls", {
      method: "POST",
      body: JSON.stringify({
        encounter_id: input.encounterId, outcome: input.outcome,
        reached: input.reached, note: input.note,
      }),
    });
  }

  /** GET /continuum/aftercare/readmission-risks — deterministic,
   *  config-weighted scoring; labelled a triage aid server-side. */
  async readmissionRisks(): Promise<{ items: ReadmissionRiskItem[]; basis: string }> {
    return this.request("/continuum/aftercare/readmission-risks");
  }

  /** GET /continuum/aftercare/hah — hospital-at-home census board
   *  (workflow only; no device feeds — those are Phase 14). */
  hospitalAtHome(): Promise<HahBoard> {
    return this.request("/continuum/aftercare/hah");
  }

  /** GET /continuum/command/overview — the Capacity Command Center
   *  roll-up, all from live module data. */
  commandOverview(): Promise<CommandOverview> {
    return this.request("/continuum/command/overview");
  }

  /** Phase 17 (BD-69): subscribe to command-centre realtime fan-out
   *  (SSE stream with automatic polling fallback — see
   *  ./realtime.ts). Returns the connection; call close() on unmount. */
  connectCommandRealtime(callbacks: {
    onEvent: (event: import("./realtime").RealtimeEvent) => void;
    onMode?: (mode: import("./realtime").RealtimeMode) => void;
    pollIntervalMs?: number;
  }): import("./realtime").RealtimeConnection {
    const { connectRealtime } = realtimeModule;
    return connectRealtime({
      baseUrl: this.baseUrl,
      getToken: this.getToken,
      fetchFn: this.fetchFn,
      channel: "command",
      pollIntervalMs: callbacks.pollIntervalMs,
      onEvent: callbacks.onEvent,
      onMode: callbacks.onMode,
    });
  }

  /** POST /continuum/command/bed-requests/{encounterId}/assign */
  commandAssignBed(encounterId: string, bed: string): Promise<unknown> {
    return this.request(
      `/continuum/command/bed-requests/${encodeURIComponent(encounterId)}/assign`,
      { method: "POST", body: JSON.stringify({ bed }) },
    );
  }

  /** GET /continuum/command/transfers — transfer-centre requests. */
  async transferRequests(status?: string): Promise<TransferRequestItem[]> {
    const qs = status ? `?status=${encodeURIComponent(status)}` : "";
    const out = await this.request<{ items: TransferRequestItem[] }>(
      `/continuum/command/transfers${qs}`,
    );
    return out.items;
  }

  /** POST /continuum/command/transfers — intake a new external
   *  transfer request. */
  createTransferRequest(input: {
    patientName: string; referringFacility: string; reason: string;
    patientAge?: number; patientSex?: string; serviceLine?: string;
    urgency?: string; contact?: string;
  }): Promise<TransferRequestItem> {
    return this.request("/continuum/command/transfers", {
      method: "POST",
      body: JSON.stringify({
        patient_name: input.patientName,
        referring_facility: input.referringFacility,
        reason: input.reason,
        patient_age: input.patientAge,
        patient_sex: input.patientSex,
        service_line: input.serviceLine,
        urgency: input.urgency,
        contact: input.contact,
      }),
    });
  }

  /** POST /continuum/command/transfers/{id}/decide — accept creates
   *  an admission task; decline requires a reason (server-enforced). */
  decideTransfer(id: string, decision: "accept" | "decline", reason?: string): Promise<TransferRequestItem> {
    return this.request(
      `/continuum/command/transfers/${encodeURIComponent(id)}/decide`,
      { method: "POST", body: JSON.stringify({ decision, reason }) },
    );
  }

  /** GET /continuum/command/transport — transport dispatch list. */
  async transportRequests(): Promise<TransportItem[]> {
    const out = await this.request<{ items: TransportItem[] }>(
      "/continuum/command/transport",
    );
    return out.items;
  }

  /** POST /continuum/command/transport/{id}/transition */
  transitionTransport(id: string, toStatus: string, transporter?: string): Promise<TransportItem> {
    return this.request(
      `/continuum/command/transport/${encodeURIComponent(id)}/transition`,
      { method: "POST", body: JSON.stringify({ to_status: toStatus, transporter }) },
    );
  }

  /** POST /continuum/safety/events — file a safety event (any staff
   *  role; the confidential flag shields the reporter in listings). */
  fileSafetyEvent(input: {
    description: string; category?: string; harmLevel?: string;
    unit?: string; patient?: string; confidential?: boolean;
  }): Promise<SafetyEventItem> {
    return this.request("/continuum/safety/events", {
      method: "POST",
      body: JSON.stringify({
        description: input.description,
        category: input.category,
        harm_level: input.harmLevel,
        unit: input.unit,
        patient: input.patient,
        confidential: input.confidential,
      }),
    });
  }

  /** GET /continuum/safety/events — the triage queue (safety.review). */
  async safetyEvents(status?: string): Promise<SafetyEventItem[]> {
    const qs = status ? `?status=${encodeURIComponent(status)}` : "";
    const out = await this.request<{ items: SafetyEventItem[] }>(
      `/continuum/safety/events${qs}`,
    );
    return out.items;
  }

  /** GET /continuum/safety/events/{id} — detail with the two-way
   *  reviewer↔reporter follow-up thread. */
  safetyEvent(id: string): Promise<SafetyEventDetail> {
    return this.request(`/continuum/safety/events/${encodeURIComponent(id)}`);
  }

  /** POST /continuum/safety/events/{id}/messages */
  safetyEventMessage(id: string, body: string): Promise<unknown> {
    return this.request(
      `/continuum/safety/events/${encodeURIComponent(id)}/messages`,
      { method: "POST", body: JSON.stringify({ body }) },
    );
  }

  /** POST /continuum/safety/events/{id}/triage */
  triageSafetyEvent(id: string, input: {
    status?: string; harmLevel?: string; rcaRequired?: boolean; note?: string;
  }): Promise<SafetyEventItem> {
    return this.request(
      `/continuum/safety/events/${encodeURIComponent(id)}/triage`,
      {
        method: "POST",
        body: JSON.stringify({
          status: input.status, harm_level: input.harmLevel,
          rca_required: input.rcaRequired, note: input.note,
        }),
      },
    );
  }

  /** GET /continuum/safety/rca — the RCA workqueue. */
  async safetyRcaQueue(): Promise<SafetyEventItem[]> {
    const out = await this.request<{ items: SafetyEventItem[] }>(
      "/continuum/safety/rca",
    );
    return out.items;
  }

  /** GET /continuum/safety/summary — unit safety summary (the data
   *  behind the published unit-safety dashboard). */
  safetySummary(unit?: string): Promise<SafetySummary> {
    const qs = unit ? `?unit=${encodeURIComponent(unit)}` : "";
    return this.request(`/continuum/safety/summary${qs}`);
  }

  // ---- Depth pass (Workstream 2): clinical journeys deepened ----
  // The three governed additions of the depth pass: the WHO surgical
  // safety checklist on the surgery board, the interdisciplinary
  // rounds checklist on the unit board, and discharge medication
  // reconciliation feeding the readiness board. All three are
  // published Studio forms on the server; required items are enforced
  // server-side and refusals surface as ApiError details in the pages.

  /** POST /surgery/cases/{id}/safety-checklist — record ONE section
   *  (sign-in | time-out | sign-out) of the WHO checklist for a case.
   *  A 409 from a later transition names the missing section. */
  submitSafetyChecklist(
    caseId: string,
    section: "sign-in" | "time-out" | "sign-out",
    answers: QuestionnaireAnswerItem[],
  ): Promise<{
    case: SurgeryCaseEntry | null;
    section: string;
    safety_checklist: SafetyChecklistStatus;
  }> {
    return this.request(
      `/surgery/cases/${encodeURIComponent(caseId)}/safety-checklist`,
      { method: "POST", body: JSON.stringify({ section, answers }) },
    );
  }

  /** GET /continuum/inpatient/rounds?unit= — the structured rounds
   *  list for one unit, each item carrying rounds_checklist_today. */
  inpatientRounds(unit: string): Promise<RoundsList> {
    return this.request(
      `/continuum/inpatient/rounds?unit=${encodeURIComponent(unit)}`,
    );
  }

  /** POST /continuum/inpatient/rounds-checklist — record the
   *  interdisciplinary rounds checklist for an active admission. */
  submitRoundsChecklist(
    encounterId: string,
    answers: QuestionnaireAnswerItem[],
  ): Promise<unknown> {
    return this.request("/continuum/inpatient/rounds-checklist", {
      method: "POST",
      body: JSON.stringify({ encounter_id: encounterId, answers }),
    });
  }

  /** POST /continuum/inpatient/discharge-med-rec — record the
   *  discharge medication reconciliation for an admission (the
   *  readiness board's fourth element). */
  submitDischargeMedRec(
    encounterId: string,
    answers: QuestionnaireAnswerItem[],
  ): Promise<unknown> {
    return this.request("/continuum/inpatient/discharge-med-rec", {
      method: "POST",
      body: JSON.stringify({ encounter_id: encounterId, answers }),
    });
  }

  // ---- Phase 13: people & data platform (/people, /reports) ----

  /** GET /people/config — the published workforce config (roles,
   *  units, shift templates, credential types, expiry windows). */
  workforceConfig(): Promise<WorkforceConfig> {
    return this.request("/people/config");
  }

  /** GET /people/staff — the staff directory. Placement filters
   *  (Organisation pass) are server-side query params; only the
   *  filters actually set are sent. */
  async listStaff(
    params: {
      unit?: string;
      region?: string;
      location?: string;
      department?: string;
    } = {},
  ): Promise<StaffMember[]> {
    const qs = new URLSearchParams();
    if (params.unit) qs.set("unit", params.unit);
    if (params.region) qs.set("region", params.region);
    if (params.location) qs.set("location", params.location);
    if (params.department) qs.set("department", params.department);
    const query = qs.toString();
    const out = await this.request<{ items: StaffMember[] }>(
      `/people/staff${query ? `?${query}` : ""}`,
    );
    return out.items;
  }

  /** GET /people/credentials/worklist — expired + expiring credentials
   *  inside the config worklist window, soonest first. */
  credentialWorklist(): Promise<CredentialWorklist> {
    return this.request("/people/credentials/worklist");
  }

  /** GET /people/credentials — every credential with computed status. */
  async listCredentials(): Promise<StaffCredential[]> {
    const out = await this.request<{ items: StaffCredential[] }>(
      "/people/credentials",
    );
    return out.items;
  }

  /** POST /people/credentials/scan — evaluate the expiry rules over
   *  the alert window (idempotent; raises renewal Tasks). */
  scanCredentials(): Promise<{ scanned: number; alerted: number; alert_window_days: number }> {
    return this.request("/people/credentials/scan", { method: "POST" });
  }

  /** GET /people/roster — shift instances (one day when `date` set). */
  async listRoster(date?: string): Promise<RosterShift[]> {
    const qs = date
      ? `?date_from=${encodeURIComponent(date)}&date_to=${encodeURIComponent(date)}`
      : "";
    const out = await this.request<{ items: RosterShift[] }>(
      `/people/roster${qs}`,
    );
    return out.items;
  }

  /** GET /people/on-call/now — who is on call per service line. */
  onCallNow(): Promise<OnCallNow> {
    return this.request("/people/on-call/now");
  }

  /** GET /people/home-dashboard — the signed-in user's audience
   *  dashboard (published config), or dashboard: null to fall back. */
  homeDashboard(): Promise<HomeDashboard> {
    return this.request("/people/home-dashboard");
  }

  /** GET /home/cards — live Home card indicators (Home & Module UX
   *  pass). One entry per staff module with a real provider; modules
   *  without a provider are absent. Accepts either the wrapped
   *  { cards: [...] } shape or a bare array, defensively. */
  async homeCards(): Promise<HomeCard[]> {
    const res = await this.request<HomeCardsResponse | HomeCard[]>("/home/cards");
    if (Array.isArray(res)) return res;
    return Array.isArray(res?.cards) ? res.cards : [];
  }

  /** GET /indicators/{key}/records — the analysis drill-down: the
   *  published definition, the evaluated value, and the underlying
   *  records behind the number (or records_supported=false + an
   *  honest reason for aggregate warehouse sources). */
  indicatorRecords(key: string): Promise<IndicatorRecordsResponse> {
    return this.request(`/indicators/${encodeURIComponent(key)}/records`);
  }

  // ---- Front Door pass: accounts, registration, unit-first home ----

  /** PUT /people/staff/{id} — edit a directory entry; the server
   *  validates against the published workforce config like create. */
  updateStaff(staffId: string, patch: StaffUpdate): Promise<StaffMember> {
    return this.request(`/people/staff/${encodeURIComponent(staffId)}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    });
  }

  /** GET /accounts — user accounts (admin: accounts.manage). */
  async listAccounts(): Promise<UserAccount[]> {
    const out = await this.request<{ items: UserAccount[] }>("/accounts");
    return out.items;
  }

  /** POST /accounts — create an account. In dev mode the response
   *  carries the one-time dev token; it is never readable again. */
  createAccount(input: AccountInput): Promise<AccountCreated> {
    return this.request("/accounts", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** PUT /accounts/{id} — update an account (username immutable). */
  updateAccount(accountId: number, patch: AccountUpdate): Promise<UserAccount> {
    return this.request(`/accounts/${accountId}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    });
  }

  /** POST /accounts/{id}/disable — a disabled account's sign-in
   *  stops authenticating immediately. */
  disableAccount(accountId: number): Promise<UserAccount> {
    return this.request(`/accounts/${accountId}/disable`, { method: "POST" });
  }

  /** POST /accounts/{id}/enable — reverse a disable. */
  enableAccount(accountId: number): Promise<UserAccount> {
    return this.request(`/accounts/${accountId}/enable`, { method: "POST" });
  }

  /** PUT /fhir/Patient/{id} — full-resource replace (the one FHIR
   *  update route; last-write-wins, audited with changed-field
   *  names). Entered-in-error is an update with active: false. */
  updatePatient(id: string, body: Record<string, unknown>): Promise<Patient> {
    return this.request(`/fhir/Patient/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  /** POST /registration/patients/check-duplicates — candidate
   *  records matching name + date of birth. */
  async patientDuplicateCheck(input: {
    family: string;
    given: string;
    birthDate: string;
  }): Promise<DuplicateCandidate[]> {
    const out = await this.request<{ candidates: DuplicateCandidate[] }>(
      "/registration/patients/check-duplicates",
      { method: "POST", body: JSON.stringify(input) },
    );
    return out.candidates;
  }

  /** POST /registration/patients — register a patient (MRN assigned
   *  from the published format). A name+DOB match without
   *  confirm_duplicate is a 409 whose ApiError.body carries the
   *  candidates. */
  registerPatient(input: RegistrationInput): Promise<RegistrationResult> {
    return this.request("/registration/patients", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** GET /people/my-unit-patients — active admissions in the
   *  caller's own unit (unit: null when the caller has none). */
  myUnitPatients(): Promise<MyUnitPatients> {
    return this.request("/people/my-unit-patients");
  }

  /** GET /people/my-patients — active admissions where the caller
   *  is the attending of record. */
  myPatients(): Promise<MyPatients> {
    return this.request("/people/my-patients");
  }

  /** GET /registration/frontdesk/today — the front-desk day: today's
   *  appointments, arrivals awaiting check-in/rooming, the open
   *  waitlist and today's registrations. */
  frontdeskToday(): Promise<FrontdeskToday> {
    return this.request("/registration/frontdesk/today");
  }

  /** GET /inpatient/locations/draft-status — which locations
   *  version is live and whether a draft is pending publish. */
  locationsDraftStatus(): Promise<LocationsDraftStatus> {
    return this.request("/inpatient/locations/draft-status");
  }

  /** POST /inpatient/locations/draft — compose unit/bed edits into
   *  the governed locations DRAFT. Nothing goes live until a human
   *  publishes the draft in System Studio. */
  composeLocationsDraft(operations: LocationDraftOp[]): Promise<LocationsDraftResult> {
    return this.request("/inpatient/locations/draft", {
      method: "POST",
      body: JSON.stringify({ operations }),
    });
  }

  /** GET /reports/catalogue — published report definitions. */
  async reportsCatalogue(): Promise<ReportDefinition[]> {
    const out = await this.request<{ items: ReportDefinition[] }>(
      "/reports/catalogue",
    );
    return out.items;
  }

  /** POST /reports/{key}/run — run a catalogue report and download
   *  the rendered file (PDF or CSV). Generation is audited server-side. */
  runReport(
    key: string,
    format: string,
    params: Record<string, string | number> = {},
  ): Promise<DownloadedFile> {
    return this.requestBlob(`/reports/${encodeURIComponent(key)}/run`, {
      method: "POST",
      body: JSON.stringify({ format, params }),
    });
  }

  /** GET /reports/documents/invoice/{claimId} — printable invoice PDF. */
  printInvoice(claimId: string): Promise<DownloadedFile> {
    return this.requestBlob(
      `/reports/documents/invoice/${encodeURIComponent(claimId)}`,
    );
  }

  /** GET /reports/documents/receipt/{paymentId} — payment receipt PDF. */
  printReceipt(paymentId: string): Promise<DownloadedFile> {
    return this.requestBlob(
      `/reports/documents/receipt/${encodeURIComponent(paymentId)}`,
    );
  }

  /** GET /reports/documents/statement/{patient} — patient statement PDF. */
  printStatement(patient: string): Promise<DownloadedFile> {
    return this.requestBlob(
      `/reports/documents/statement/${encodeURIComponent(patient)}`,
    );
  }

  /** GET /reports/documents/superbill/{encounterId} — superbill PDF
   *  (422 when the encounter has no HIM coding record). */
  printSuperbill(encounterId: string): Promise<DownloadedFile> {
    return this.requestBlob(
      `/reports/documents/superbill/${encodeURIComponent(encounterId)}`,
    );
  }

  /** GET /reports/exports/datasets — the named exportable datasets. */
  async exportDatasets(): Promise<ExportDataset[]> {
    const out = await this.request<{ items: ExportDataset[] }>(
      "/reports/exports/datasets",
    );
    return out.items;
  }

  /** POST /reports/exports — run a named dataset export (audited;
   *  above the config row threshold a reason is required). */
  runExport(input: {
    dataset: string;
    format?: string;
    reason?: string;
  }): Promise<DownloadedFile> {
    return this.requestBlob("/reports/exports", {
      method: "POST",
      body: JSON.stringify({
        dataset: input.dataset,
        format: input.format ?? "csv",
        reason: input.reason,
      }),
    });
  }

  /** GET /reports/imports/profiles — published mapping profiles. */
  async importProfiles(): Promise<ImportProfile[]> {
    const out = await this.request<{ items: ImportProfile[] }>(
      "/reports/imports/profiles",
    );
    return out.items;
  }

  /** POST /reports/imports/validate — full error report; writes nothing. */
  validateImport(profile: string, csv: string): Promise<ImportValidation> {
    return this.request("/reports/imports/validate", {
      method: "POST",
      body: JSON.stringify({ profile, csv }),
    });
  }

  /** POST /reports/imports/commit — all-or-nothing import. */
  commitImport(
    profile: string,
    csv: string,
  ): Promise<{ imported: number; entity_type: string; profile: string }> {
    return this.request("/reports/imports/commit", {
      method: "POST",
      body: JSON.stringify({ profile, csv }),
    });
  }

  // ---- Phase 14: devices, IoT, data lake & pipelines ----

  /** GET /devices — the device registry with computed calibration
   *  state and gate block reasons. Feeds are labelled simulated. */
  devices(): Promise<DevicesResponse> {
    return this.request("/devices");
  }

  /** GET /devices/config — published device config + MQTT configured. */
  devicesConfig(): Promise<DevicesConfigResponse> {
    return this.request("/devices/config");
  }

  /** GET /devices/calibration-worklist — overdue + due-soon devices. */
  calibrationWorklist(): Promise<{ items: DeviceItem[]; total: number }> {
    return this.request("/devices/calibration-worklist");
  }

  /** POST /devices/{key}/calibration — record a calibration; the due
   *  date is computed server-side from the published interval. */
  recordCalibration(
    deviceKey: string,
    performedAt: string,
  ): Promise<DeviceItem> {
    return this.request(`/devices/${encodeURIComponent(deviceKey)}/calibration`, {
      method: "POST",
      body: JSON.stringify({ performed_at: performedAt }),
    });
  }

  /** GET /devices/feeds/status — honest feed state (MQTT off when no
   *  broker is configured; the MLLP gate policy as published). */
  feedsStatus(): Promise<FeedsStatus> {
    return this.request("/devices/feeds/status");
  }

  /** GET /devices/readings — curated device readings. */
  async deviceReadings(params: {
    device_key?: string;
    excursion?: boolean;
  } = {}): Promise<DeviceReading[]> {
    const qs = new URLSearchParams();
    if (params.device_key) qs.set("device_key", params.device_key);
    if (params.excursion) qs.set("excursion", "true");
    const query = qs.toString();
    const out = await this.request<{ items: DeviceReading[] }>(
      `/devices/readings${query ? `?${query}` : ""}`,
    );
    return out.items;
  }

  /** GET /devices/lake — the append-only raw zone (summaries). */
  lakeEntries(params: {
    payload_type?: string;
    disposition?: string;
    device_key?: string;
  } = {}): Promise<LakeResponse> {
    const qs = new URLSearchParams();
    if (params.payload_type) qs.set("payload_type", params.payload_type);
    if (params.disposition) qs.set("disposition", params.disposition);
    if (params.device_key) qs.set("device_key", params.device_key);
    const query = qs.toString();
    return this.request(`/devices/lake${query ? `?${query}` : ""}`);
  }

  /** GET /devices/lake/{id} — one raw entry with its full payload. */
  lakeEntry(id: number): Promise<LakeEntry> {
    return this.request(`/devices/lake/${id}`);
  }

  /** GET /devices/lineage/{type}/{id} — where a curated value came from. */
  lineage(resourceType: string, resourceId: string): Promise<LineageResponse> {
    return this.request(
      `/devices/lineage/${encodeURIComponent(resourceType)}/${encodeURIComponent(resourceId)}`,
    );
  }

  /** GET /devices/quarantine — held items (device ingest + pipelines). */
  async quarantine(status = "held"): Promise<QuarantineItem[]> {
    const out = await this.request<{ items: QuarantineItem[] }>(
      `/devices/quarantine?status=${encodeURIComponent(status)}`,
    );
    return out.items;
  }

  /** POST /devices/quarantine/{id}/release — release a hold (audited). */
  releaseQuarantine(
    id: number,
    note?: string,
  ): Promise<{ quarantine: QuarantineItem; outcome: Record<string, unknown> }> {
    return this.request(`/devices/quarantine/${id}/release`, {
      method: "POST",
      body: JSON.stringify({ note }),
    });
  }

  /** POST /devices/quarantine/{id}/reject — reject a hold; the note is
   *  required server-side (422 without it). */
  rejectQuarantine(
    id: number,
    note: string,
  ): Promise<{ quarantine: QuarantineItem }> {
    return this.request(`/devices/quarantine/${id}/reject`, {
      method: "POST",
      body: JSON.stringify({ note }),
    });
  }

  /** GET /pipelines/definitions — governed pipeline definitions. */
  async pipelineDefinitions(): Promise<PipelineDefinition[]> {
    const out = await this.request<{ items: PipelineDefinition[] }>(
      "/pipelines/definitions",
    );
    return out.items;
  }

  /** POST /pipelines/{key}/run — start a run (pipelines.run). */
  runPipeline(
    key: string,
    window: { window_start?: string; window_end?: string } = {},
  ): Promise<PipelineRun> {
    return this.request(`/pipelines/${encodeURIComponent(key)}/run`, {
      method: "POST",
      body: JSON.stringify(window),
    });
  }

  /** GET /pipelines/runs — run history with per-step status. */
  async pipelineRuns(pipelineKey?: string): Promise<PipelineRun[]> {
    const query = pipelineKey
      ? `?pipeline_key=${encodeURIComponent(pipelineKey)}`
      : "";
    const out = await this.request<{ items: PipelineRun[] }>(
      `/pipelines/runs${query}`,
    );
    return out.items;
  }

  /** GET /pipelines/dead-letters — failed steps awaiting replay. */
  async pipelineDeadLetters(): Promise<PipelineDeadLetter[]> {
    const out = await this.request<{ items: PipelineDeadLetter[] }>(
      "/pipelines/dead-letters",
    );
    return out.items;
  }

  /** POST /pipelines/dead-letters/{id}/replay — fresh run of the
   *  letter's pipeline over the same scope. (Named apart from the
   *  Phase 8 interop replayDeadLetter.) */
  replayPipelineDeadLetter(id: number): Promise<PipelineRun> {
    return this.request(`/pipelines/dead-letters/${id}/replay`, {
      method: "POST",
      body: JSON.stringify({}),
    });
  }

  /** GET /pipelines/dedup/candidates — MPI duplicate candidates for
   *  human review (the scan never merges). */
  async dedupCandidates(status?: string): Promise<DedupCandidate[]> {
    const query = status ? `?status=${encodeURIComponent(status)}` : "";
    const out = await this.request<{ items: DedupCandidate[] }>(
      `/pipelines/dedup/candidates${query}`,
    );
    return out.items;
  }

  /** POST /pipelines/dedup/candidates/{id}/review — record the human
   *  decision; the response states merge_performed: false. */
  reviewDedupCandidate(
    id: number,
    decision: "not-duplicates" | "confirmed-duplicate",
    note?: string,
  ): Promise<{ candidate: DedupCandidate; merge_performed: boolean }> {
    return this.request(`/pipelines/dedup/candidates/${id}/review`, {
      method: "POST",
      body: JSON.stringify({ decision, note }),
    });
  }

  // ---- Inventory & Materials (/inventory, WS-A backend) ----
  // Reads ride inventory.read (staff incl. viewer); item-master / PAR
  // / count writes ride inventory.manage and every stock movement
  // rides inventory.move (admin / configurator / clinician) — the
  // server enforces both; errors surface as ApiError {detail}.

  /** GET /inventory/taxonomy — governed categories/subcategories,
   *  adjustment reason codes and the expiry warning window. */
  inventoryTaxonomy(): Promise<InvTaxonomy> {
    return this.request("/inventory/taxonomy");
  }

  /** GET /inventory/overview — headline aggregates for the strip. */
  inventoryOverview(): Promise<InvOverview> {
    return this.request("/inventory/overview");
  }

  /** GET /inventory/items[?category&subcategory&active&q] */
  async inventoryItems(params: {
    category?: string;
    subcategory?: string;
    active?: boolean;
    q?: string;
  } = {}): Promise<InvItem[]> {
    const body = await this.request<unknown>(
      `/inventory/items${invQuery({
        category: params.category,
        subcategory: params.subcategory,
        active: params.active === undefined ? undefined : String(params.active),
        q: params.q,
      })}`,
    );
    return invList<InvItem>(body);
  }

  /** GET /inventory/items/{sku} */
  inventoryItem(sku: string): Promise<InvItem> {
    return this.request(`/inventory/items/${encodeURIComponent(sku)}`);
  }

  /** POST /inventory/items — create an item-master record. */
  createInventoryItem(input: InvItemInput): Promise<InvItem> {
    return this.request("/inventory/items", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** PUT /inventory/items/{sku} — replace an item-master record. */
  updateInventoryItem(sku: string, input: InvItemInput): Promise<InvItem> {
    return this.request(`/inventory/items/${encodeURIComponent(sku)}`, {
      method: "PUT",
      body: JSON.stringify(input),
    });
  }

  /** GET /inventory/stock[?sku&location_code&category] — lot rows
   *  with the effective unit cost and line value from the server. */
  async inventoryStock(params: {
    sku?: string;
    location_code?: string;
    category?: string;
  } = {}): Promise<InvStockRow[]> {
    const body = await this.request<unknown>(
      `/inventory/stock${invQuery(params)}`,
    );
    return invList<InvStockRow>(body);
  }

  /** GET /inventory/par-levels */
  async inventoryParLevels(): Promise<InvParLevel[]> {
    const body = await this.request<unknown>("/inventory/par-levels");
    return invList<InvParLevel>(body);
  }

  /** PUT /inventory/par-levels {sku, location_code, par_level, unit_level?} */
  setInventoryParLevel(input: {
    sku: string;
    location_code: string;
    par_level: number;
    unit_level?: string;
  }): Promise<InvParLevel> {
    return this.request("/inventory/par-levels", {
      method: "PUT",
      body: JSON.stringify(input),
    });
  }

  /** GET /inventory/movements[?sku&location_code&event&reference&patient_ref]
   *  — the append-only ledger, newest first (server order). */
  async inventoryMovements(params: {
    sku?: string;
    location_code?: string;
    event?: string;
    reference?: string;
    patient_ref?: string;
  } = {}): Promise<InvMovement[]> {
    const body = await this.request<unknown>(
      `/inventory/movements${invQuery(params)}`,
    );
    return invList<InvMovement>(body);
  }

  /** POST /inventory/movements/receive */
  inventoryReceive(input: {
    sku: string;
    location_code: string;
    quantity: number;
    lot_number?: string;
    serial?: string;
    expiry_date?: string;
    unit_cost_cents?: number;
    reference?: string;
    unit_level?: string;
    containers?: Array<{ container_code?: string; quantity: number }>;
    container_count?: number;
    component_lots?: Array<{
      sku: string;
      lot_number?: string;
      expiry_date?: string;
    }>;
  }): Promise<InvMovement> {
    return this.request("/inventory/movements/receive", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /inventory/movements/issue — FEFO allocation server-side. */
  inventoryIssue(input: {
    sku: string;
    location_code: string;
    quantity: number;
    lot_number?: string;
    reference?: string;
    unit_level?: string;
  }): Promise<InvMovement> {
    return this.request("/inventory/movements/issue", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /inventory/movements/transfer */
  inventoryTransfer(input: {
    sku: string;
    from_location: string;
    to_location: string;
    quantity: number;
    lot_number?: string;
    unit_level?: string;
  }): Promise<InvMovement> {
    return this.request("/inventory/movements/transfer", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /inventory/movements/adjust — reason_code from the taxonomy;
   *  a note is required server-side when the reason is "other". */
  inventoryAdjust(input: {
    sku: string;
    location_code: string;
    lot_number?: string;
    serial?: string;
    quantity_delta: number;
    reason_code: string;
    note?: string;
    unit_level?: string;
  }): Promise<InvMovement> {
    return this.request("/inventory/movements/adjust", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /inventory/requisitions */
  createInventoryRequisition(input: {
    from_location_code: string;
    to_location_code: string;
    lines: Array<{ sku: string; quantity: number }>;
    reference?: string;
  }): Promise<InvRequisition> {
    return this.request("/inventory/requisitions", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** GET /inventory/requisitions */
  async inventoryRequisitions(): Promise<InvRequisition[]> {
    const body = await this.request<unknown>("/inventory/requisitions");
    return invList<InvRequisition>(body);
  }

  /** GET /inventory/requisitions/{req_id} */
  inventoryRequisition(reqId: string): Promise<InvRequisition> {
    return this.request(`/inventory/requisitions/${encodeURIComponent(reqId)}`);
  }

  /** POST /inventory/requisitions/{req_id}/issue {lines?} — issues
   *  the remaining quantities when no lines are given. */
  issueInventoryRequisition(
    reqId: string,
    lines?: Array<{ sku: string; quantity: number }>,
  ): Promise<InvRequisition> {
    return this.request(
      `/inventory/requisitions/${encodeURIComponent(reqId)}/issue`,
      {
        method: "POST",
        body: JSON.stringify(lines ? { lines } : {}),
      },
    );
  }

  /** POST /inventory/requisitions/{req_id}/cancel */
  cancelInventoryRequisition(reqId: string): Promise<InvRequisition> {
    return this.request(
      `/inventory/requisitions/${encodeURIComponent(reqId)}/cancel`,
      { method: "POST", body: JSON.stringify({}) },
    );
  }

  /** POST /inventory/counts {location_code} — start a cycle count. */
  startInventoryCount(locationCode: string): Promise<InvCount> {
    return this.request("/inventory/counts", {
      method: "POST",
      body: JSON.stringify({ location_code: locationCode }),
    });
  }

  /** GET /inventory/counts */
  async inventoryCounts(): Promise<InvCount[]> {
    const body = await this.request<unknown>("/inventory/counts");
    return invList<InvCount>(body);
  }

  /** GET /inventory/counts/{count_id} */
  inventoryCount(countId: string): Promise<InvCount> {
    return this.request(`/inventory/counts/${encodeURIComponent(countId)}`);
  }

  /** POST /inventory/counts/{count_id}/complete {lines, reason_code?}
   *  — a reason is required server-side when any variance is non-zero. */
  completeInventoryCount(
    countId: string,
    input: {
      lines: Array<{
        sku: string;
        lot_number?: string | null;
        serial?: string | null;
        counted: number;
      }>;
      reason_code?: string;
    },
  ): Promise<InvCount> {
    return this.request(
      `/inventory/counts/${encodeURIComponent(countId)}/complete`,
      { method: "POST", body: JSON.stringify(input) },
    );
  }

  /** POST /inventory/counts/{count_id}/cancel */
  cancelInventoryCount(countId: string): Promise<InvCount> {
    return this.request(
      `/inventory/counts/${encodeURIComponent(countId)}/cancel`,
      { method: "POST", body: JSON.stringify({}) },
    );
  }

  /** POST /inventory/case-issues — draw a case card's bill of
   *  materials from a location (FEFO, server-side). */
  inventoryCaseIssue(input: {
    card_id: string;
    case_id: string;
    location_code: string;
    patient_ref?: string;
  }): Promise<InvCaseIssueResult> {
    return this.request("/inventory/case-issues", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** GET /inventory/case-issues[?case_id=] */
  async inventoryCaseIssues(caseId?: string): Promise<InvCaseIssueResult[]> {
    const body = await this.request<unknown>(
      `/inventory/case-issues${invQuery({ case_id: caseId })}`,
    );
    return invList<InvCaseIssueResult>(body);
  }

  /** GET /inventory/implant-trace[?lot_number&patient_ref&sku] —
   *  lot -> patient/case and patient -> lots in one row shape. */
  async inventoryImplantTrace(params: {
    lot_number?: string;
    patient_ref?: string;
    sku?: string;
  } = {}): Promise<InvImplantTraceRow[]> {
    const body = await this.request<unknown>(
      `/inventory/implant-trace${invQuery(params)}`,
    );
    return invList<InvImplantTraceRow>(body);
  }

  /** POST /inventory/scan {value, context?, location_code?} —
   *  resolve a scanned/typed identifier to item + lot + stock. */
  inventoryScan(input: {
    value: string;
    context?: string;
    location_code?: string;
  }): Promise<InvScanResult> {
    return this.request("/inventory/scan", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /inventory/rfid-events {epc, reader_id?, zone?, context,
   *  location_code?} — record one (simulated) reader event. */
  inventoryRfidEvent(input: {
    epc: string;
    reader_id?: string;
    zone?: string;
    context: string;
    location_code?: string;
  }): Promise<Record<string, unknown>> {
    return this.request("/inventory/rfid-events", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** GET /inventory/worklists/low-stock[?location_code&category] */
  async inventoryLowStock(params: {
    location_code?: string;
    category?: string;
  } = {}): Promise<InvLowStockRow[]> {
    const body = await this.request<unknown>(
      `/inventory/worklists/low-stock${invQuery(params)}`,
    );
    return invList<InvLowStockRow>(body);
  }

  /** GET /inventory/worklists/expiry[?location_code&category] */
  async inventoryExpiry(params: {
    location_code?: string;
    category?: string;
  } = {}): Promise<InvExpiryRow[]> {
    const body = await this.request<unknown>(
      `/inventory/worklists/expiry${invQuery(params)}`,
    );
    return invList<InvExpiryRow>(body);
  }

  /** POST /inventory/lots/verify — stamp a lot verification record. */
  inventoryLotVerify(input: {
    sku: string;
    location_code: string;
    lot_number: string;
    serial?: string;
    method: string;
    result: string;
    note?: string;
  }): Promise<InvStockRow> {
    return this.request("/inventory/lots/verify", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /inventory/lots/stage — transition a lot through the stage machine. */
  inventoryLotStage(input: {
    sku: string;
    location_code: string;
    lot_number: string;
    serial?: string;
    to_stage: string;
    reason_code?: string;
    note?: string;
  }): Promise<InvStockRow> {
    return this.request("/inventory/lots/stage", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** GET /inventory/containers[?sku&location_code&lot_number&stage] */
  async inventoryContainers(params: {
    sku?: string;
    location_code?: string;
    lot_number?: string;
    stage?: string;
  } = {}): Promise<InvContainer[]> {
    const body = await this.request<unknown>(
      `/inventory/containers${invQuery(params)}`,
    );
    return invList<InvContainer>(body);
  }

  /** POST /inventory/containers/open */
  inventoryContainerOpen(input: {
    container_code: string;
  }): Promise<InvContainer> {
    return this.request("/inventory/containers/open", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /inventory/containers/consume */
  inventoryContainerConsume(input: {
    container_code: string;
    quantity: number;
  }): Promise<InvContainer> {
    return this.request("/inventory/containers/consume", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /inventory/containers/dispose */
  inventoryContainerDispose(input: {
    container_code: string;
    reason_code: string;
    note?: string;
  }): Promise<InvContainer> {
    return this.request("/inventory/containers/dispose", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /inventory/containers/transfer — move a whole container. */
  inventoryContainerTransfer(input: {
    container_code: string;
    to_location_code: string;
  }): Promise<{ container: InvContainer; movements: InvMovement[] }> {
    return this.request("/inventory/containers/transfer", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /inventory/kits/open */
  inventoryKitOpen(input: {
    sku: string;
    location_code: string;
    lot_number?: string;
    quantity: number;
  }): Promise<InvKitResult> {
    return this.request("/inventory/kits/open", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /inventory/kits/assemble */
  inventoryKitAssemble(input: {
    sku: string;
    location_code: string;
    quantity: number;
    lot_number?: string;
  }): Promise<InvKitResult> {
    return this.request("/inventory/kits/assemble", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** POST /inventory/kits/disassemble */
  inventoryKitDisassemble(input: {
    sku: string;
    location_code: string;
    lot_number?: string;
    quantity: number;
  }): Promise<InvKitResult> {
    return this.request("/inventory/kits/disassemble", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }
}

// Same-origin by default: in dev, the Vite proxy (vite.config.ts) forwards
// the API prefixes to the backend; in the Docker deployment, nginx does.
// Cross-origin browser calls were the source of a live CORS failure, so an
// absolute base is now strictly opt-in via VITE_API_BASE (e.g. a hosted
// deployment where the API has its own origin + CORS configured).
export const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
