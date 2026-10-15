import type {
  Appointment,
  Bundle,
  FhirResource,
  ModulePack,
  PackInstallation,
  StudioAction,
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
}

export const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ?? "http://localhost:8000";
