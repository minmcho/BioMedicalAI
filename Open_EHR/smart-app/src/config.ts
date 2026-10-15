/**
 * SMART app configuration.
 *
 * Values come from (in priority order): localStorage overrides saved by the
 * settings form, Vite env vars (VITE_SMART_*), then the defaults below.
 * The default FHIR service URL is the public sandbox endpoint operated by
 * Epic for app developers — a factual integration target, used only as a
 * default `iss` for standalone launches; EHR launches carry their own iss.
 */

export interface SmartConfig {
  clientId: string;
  scope: string;
  fhirServiceUrl: string;
  /** Where the authorization server returns the browser: the app root
   *  (fhirclient resolves a relative "." to the directory of the launch
   *  page, i.e. index.html's directory). */
  redirectUri: string;
}

export const EPIC_SANDBOX_FHIR_URL =
  "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4";

/**
 * SMART App Launch v2 granular scopes (`.rs` = read + search) plus the
 * launch-context scopes. If a given EHR registration grants different scope
 * strings, edit them in the settings form — they are stored verbatim.
 */
export const DEFAULT_SCOPE = [
  "launch/patient",
  "openid",
  "fhirUser",
  "patient/Patient.rs",
  "patient/Condition.rs",
  "patient/MedicationRequest.rs",
  "patient/Observation.rs",
].join(" ");

const LS_KEY = "opencare.smart.config";

export function loadSmartConfig(): SmartConfig {
  let stored: Partial<SmartConfig> = {};
  try {
    stored = JSON.parse(localStorage.getItem(LS_KEY) ?? "{}") as Partial<SmartConfig>;
  } catch {
    stored = {};
  }
  const env = import.meta.env as Record<string, string | undefined>;
  return {
    clientId: stored.clientId ?? env.VITE_SMART_CLIENT_ID ?? "",
    scope: stored.scope ?? env.VITE_SMART_SCOPE ?? DEFAULT_SCOPE,
    fhirServiceUrl: stored.fhirServiceUrl ?? env.VITE_SMART_FHIR_URL ?? EPIC_SANDBOX_FHIR_URL,
    redirectUri: stored.redirectUri ?? env.VITE_SMART_REDIRECT_URI ?? ".",
  };
}

export function saveSmartConfig(cfg: SmartConfig): void {
  localStorage.setItem(LS_KEY, JSON.stringify(cfg));
}
