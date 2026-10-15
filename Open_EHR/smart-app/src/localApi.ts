/**
 * Client for the OpenCare core backend's FHIR API, used by the app's
 * local/demo mode. Auth is the backend's dev bearer-token mode (see
 * backend/app/security.py) — for local development against synthetic seed
 * data only, never a production credential scheme.
 */
import {
  bundleResources,
  groupLabTrends,
  summarize,
  type FhirResource,
  type PatientSummary,
  type TrendSeries,
} from "./fhirShape";

export interface LocalSummary {
  summary: PatientSummary;
  trends: TrendSeries[];
}

export class LocalFhirClient {
  constructor(
    private readonly baseUrl: string,
    private readonly token: string,
  ) {}

  private async getJson(path: string): Promise<unknown> {
    const res = await fetch(`${this.baseUrl.replace(/\/$/, "")}${path}`, {
      headers: { Authorization: `Bearer ${this.token}` },
    });
    if (!res.ok) {
      let detail = "";
      try {
        detail = String((await res.json())?.detail ?? "");
      } catch {
        /* keep the status-only message */
      }
      throw new Error(`Backend ${res.status}${detail ? `: ${detail}` : ""} (${path})`);
    }
    return res.json();
  }

  async listPatients(): Promise<FhirResource[]> {
    return bundleResources(await this.getJson("/fhir/Patient"));
  }

  async getPatientSummary(patientId: string): Promise<LocalSummary> {
    const q = `patient=${encodeURIComponent(patientId)}`;
    const [patient, conditions, medications, observations] = await Promise.all([
      this.getJson(`/fhir/Patient/${encodeURIComponent(patientId)}`) as Promise<FhirResource>,
      this.getJson(`/fhir/Condition?${q}`).then(bundleResources),
      this.getJson(`/fhir/MedicationRequest?${q}`).then(bundleResources),
      this.getJson(`/fhir/Observation?${q}`).then(bundleResources),
    ]);
    return {
      summary: summarize(patient, conditions, medications),
      trends: groupLabTrends(observations),
    };
  }
}
