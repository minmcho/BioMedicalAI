import type {
  Appointment,
  AuditEntry,
  Bundle,
  Claim,
  ConfigConcept,
  ConfigKind,
  DiagnosticReport,
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
  ModalitiesResponse,
  ModulePack,
  PackInstallation,
  QcRun,
  QcStatus,
  RadiologyStudiesResponse,
  RadiologyStudyDetail,
  RadiologyStudyItem,
  RegistryListEntry,
  RegistrySummary,
  RoomsResponse,
  ServiceRequest,
  Specimen,
  StockItem,
  StudioAction,
  StudioConfigObject,
  SurgeryBoard,
  SurgeryCaseOutcome,
  VerificationQueue,
  WarehouseStatus,
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

  /** POST /lab/orders/{id}/transition { to_status, reason?, specimen_type? } */
  transitionLabOrder(
    id: string,
    toStatus: string,
    extra: { reason?: string; specimen_type?: string } = {},
  ): Promise<LabTransitionOutcome> {
    return this.request(`/lab/orders/${encodeURIComponent(id)}/transition`, {
      method: "POST",
      body: JSON.stringify({ to_status: toStatus, ...extra }),
    });
  }

  /** POST /lab/orders/{id}/results — manual result entry (draft results). */
  enterLabResults(id: string, results: LabResultInput[]): Promise<LabResultsOutcome> {
    return this.request(`/lab/orders/${encodeURIComponent(id)}/results`, {
      method: "POST",
      body: JSON.stringify({ results }),
    });
  }

  /** POST /lab/ingest { format: "hl7" | "csv", content } — analyser ingest. */
  ingestLab(format: "hl7" | "csv", content: string): Promise<LabIngestOutcome> {
    return this.request("/lab/ingest", {
      method: "POST",
      body: JSON.stringify({ format, content }),
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

  /** GET /lab/qc[?analyte_code=] — QC run history, newest first. */
  qcRuns(analyteCode?: string): Promise<QcRun[]> {
    const qs = analyteCode ? `?analyte_code=${encodeURIComponent(analyteCode)}` : "";
    return this.request(`/lab/qc${qs}`);
  }

  /** POST /lab/qc — record a QC run; pass/fail is computed server-side. */
  createQcRun(input: {
    analyte_code: string;
    control_material: string;
    expected_low: number;
    expected_high: number;
    measured_value: number;
    unit?: string;
  }): Promise<QcRun> {
    return this.request("/lab/qc", { method: "POST", body: JSON.stringify(input) });
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

  /** POST /warehouse/etl/run — trigger an outbox drain (admin only). */
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
}

// Same-origin by default: in dev, the Vite proxy (vite.config.ts) forwards
// the API prefixes to the backend; in the Docker deployment, nginx does.
// Cross-origin browser calls were the source of a live CORS failure, so an
// absolute base is now strictly opt-in via VITE_API_BASE (e.g. a hosted
// deployment where the API has its own origin + CORS configured).
export const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
