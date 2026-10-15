import type { Role } from "../api/types";

// Session handling + role capabilities. The dev accounts below are the
// backend's seeded dev tokens (backend/app/security.py DEFAULT_DEV_TOKENS).
// They are labelled "dev sign-in" in the UI and exist for local development
// only; production uses AUTH_MODE=oidc on the server with Keycloak-issued
// tokens.

export interface Session {
  token: string;
  role: Role;
  username: string;
}

export interface DevAccount {
  role: Role;
  token: string;
  username: string;
  label: string;
}

export const DEV_ACCOUNTS: DevAccount[] = [
  { role: "admin", token: "admin-token", username: "dev-admin", label: "Administrator" },
  { role: "configurator", token: "config-token", username: "dev-configurator", label: "Configurator" },
  { role: "clinician", token: "clinician-token", username: "dev-clinician", label: "Clinician" },
  { role: "viewer", token: "viewer-token", username: "dev-viewer", label: "Viewer (read-only)" },
  { role: "patient", token: "patient-token", username: "dev-patient", label: "Patient (portal)" },
];

/** "dev" unless VITE_AUTH_MODE says otherwise. In oidc mode the sign-in
 *  screen must not fake an SSO flow — see LoginPage. */
export const AUTH_MODE: string =
  (import.meta.env.VITE_AUTH_MODE as string | undefined) ?? "dev";

const STORAGE_KEY = "opencare.session";

export function loadSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Session;
    if (!parsed.token || !parsed.role) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function saveSession(session: Session | null): void {
  try {
    if (session) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session));
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // sessionStorage unavailable (private mode) — session stays in memory.
  }
}

// ---- Role capabilities (mirror the server's role gates) ----

export type PageKey = "patients" | "chart" | "inbox" | "scheduling" | "studio" | "portal";

export function canAccess(role: Role, page: PageKey): boolean {
  switch (page) {
    case "patients":
    case "chart":
      // FHIR search/read allow any authenticated staff role server-side.
      return role === "admin" || role === "configurator" || role === "clinician" || role === "viewer";
    case "inbox":
    case "scheduling":
      // /fhir/inbox is staff-only server-side; scheduling transitions are
      // write-role only, so viewers are kept out of the page entirely.
      return role === "admin" || role === "configurator" || role === "clinician";
    case "studio":
      // Pack list is readable by staff, but install/lifecycle actions are
      // admin/configurator only — the page is theirs.
      return role === "admin" || role === "configurator";
    case "portal":
      return role === "patient";
  }
}

export interface NavItem {
  key: PageKey;
  label: string;
}

const NAV: Array<NavItem & { pages: PageKey }> = [
  { key: "patients", label: "Patients", pages: "patients" },
  { key: "inbox", label: "Results inbox", pages: "inbox" },
  { key: "scheduling", label: "Scheduling", pages: "scheduling" },
  { key: "studio", label: "Studio packs", pages: "studio" },
  { key: "portal", label: "My results", pages: "portal" },
];

export function navItemsFor(role: Role): NavItem[] {
  return NAV.filter((item) => canAccess(role, item.pages)).map(({ key, label }) => ({ key, label }));
}

export function homePageFor(role: Role): PageKey {
  return role === "patient" ? "portal" : "patients";
}
