import type {
  Appointment,
  AuditEntry,
  BloodHold,
  BloodUnit,
  Bundle,
  Claim,
  CollectionWorklist,
  ConfigConcept,
  ConfigKind,
  CriticalNotification,
  DiagnosticReport,
  EqaRecord,
  FormDefinition,
  InstrumentLogEntry,
  LabInstrument,
  LeveyJennings,
  MicroTerminology,
  TatStats,
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
} from "./types";

// API client for the real OpenCare backend. There is deliberately no mock
// mode: every method performs an actual HTTP call to VITE_API_BASE.

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
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

export class ApiClient {
  private baseUrl: string;
  private getToken: () => string | null;
  private fetchFn: FetchFn;

  constructor(baseUrl: string, getToken: () => string | null, fetchFn: FetchFn = fetch) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.getToken = getToken;
    this.fetchFn = fetchFn;
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
      throw new ApiError(response.status, detail);
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

  /** GET /lab/orders[?status=] — the order queue (staff roles). */
  async labOrders(status?: string): Promise<ServiceRequest[]> {
    const qs = status ? `?status=${encodeURIComponent(status)}` : "";
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
    dosageInstruction?: Record<string, unknown>[];
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
}

// Same-origin by default: in dev, the Vite proxy (vite.config.ts) forwards
// the API prefixes to the backend; in the Docker deployment, nginx does.
// Cross-origin browser calls were the source of a live CORS failure, so an
// absolute base is now strictly opt-in via VITE_API_BASE (e.g. a hosted
// deployment where the API has its own origin + CORS configured).
export const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
