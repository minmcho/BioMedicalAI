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

export type PageKey =
  | "patients"
  | "chart"
  | "inbox"
  | "scheduling"
  | "lab"
  | "pophealth"
  | "billing"
  | "pharmacy"
  | "ed"
  | "radiology"
  | "surgery"
  | "studio"
  | "portal";

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
    case "lab":
      // The /lab queues are staff-readable including the viewer (the
      // server 403s patients); write/verify buttons inside the page are
      // role-gated there and enforced again server-side.
      return role === "admin" || role === "configurator" || role === "clinician" || role === "viewer";
    case "pophealth":
      // /pophealth + /warehouse/status are admin/clinician on the server
      // (aggregate analytics + ETL operations); other staff roles get 403.
      return role === "admin" || role === "clinician";
    case "billing":
      // billing.view on the server: admin/configurator/clinician, plus a
      // patient sees only their own claims (read-only). The viewer is
      // denied server-side by default, so it gets no nav entry either.
      return role === "admin" || role === "configurator" || role === "clinician" || role === "patient";
    case "pharmacy":
      // /pharmacy reads are staff-wide server-side (patients 403);
      // dispense/administer actions inside the page are role-gated there
      // and enforced again server-side by capability.
      return role === "admin" || role === "configurator" || role === "clinician" || role === "viewer";
    case "ed":
    case "radiology":
    case "surgery":
      // The Phase 4B boards are staff-only server-side (patients get
      // 403); board reads include the viewer, and the write actions
      // inside each page are role-gated there and enforced again
      // server-side by capability (ed.work / radiology.work /
      // radiology.verify / surgery.work).
      return role === "admin" || role === "configurator" || role === "clinician" || role === "viewer";
    case "studio":
      // Pack list is readable by staff, but install/lifecycle actions are
      // admin/configurator only — the page is theirs.
      return role === "admin" || role === "configurator";
    case "portal":
      return role === "patient";
  }
}

// ---- Runtime-config-aware access (realignment) ----
// The shell resolves access from the PUBLISHED permissions matrix
// (merged over the built-in defaults by the caller) so the UI and the
// server agree after a site publishes an override. Each page maps to
// the capability that gates its API surface server-side; where no
// capability exists (FHIR reads are token-scoped; the portal is the
// patient's own record) the rule stays role-based, exactly as the
// server enforces it.

import type { PermissionMatrix } from "../pages/studio/models";

export const PAGE_CAPABILITY: Partial<Record<PageKey, string>> = {
  inbox: "fhir.inbox",
  scheduling: "fhir.write",
  lab: "lab.read",
  pophealth: "pophealth.view",
  pharmacy: "pharmacy.read",
  ed: "boards.read",
  radiology: "boards.read",
  surgery: "boards.read",
  // The Studio page is the configurators' surface: its write actions
  // are studio.manage on the server, so page access follows it too
  // (NOT studio.read, which staff roles hold for config reads).
  studio: "studio.manage",
};

export function canAccessWith(
  role: Role,
  page: PageKey,
  matrix: PermissionMatrix,
): boolean {
  if (page === "portal") return role === "patient";
  if (page === "patients" || page === "chart") return role !== "patient";
  if (page === "billing") {
    // billing.view for staff; a patient sees only their own claims.
    if (role === "patient") return true;
    const row = matrix["billing.view"];
    return row ? row[role] === true : canAccess(role, page);
  }
  const capability = PAGE_CAPABILITY[page];
  if (capability) {
    const row = matrix[capability];
    if (row) return row[role] === true;
  }
  return canAccess(role, page);
}

export interface NavItem {
  key: PageKey;
  label: string;
}

const NAV: Array<NavItem & { pages: PageKey }> = [
  { key: "patients", label: "Patients", pages: "patients" },
  { key: "inbox", label: "Results inbox", pages: "inbox" },
  { key: "scheduling", label: "Scheduling", pages: "scheduling" },
  { key: "lab", label: "Laboratory", pages: "lab" },
  { key: "pophealth", label: "Population health", pages: "pophealth" },
  { key: "billing", label: "Billing", pages: "billing" },
  { key: "pharmacy", label: "Pharmacy", pages: "pharmacy" },
  { key: "ed", label: "ED board", pages: "ed" },
  { key: "radiology", label: "Radiology", pages: "radiology" },
  { key: "surgery", label: "Surgery board", pages: "surgery" },
  { key: "studio", label: "System Studio", pages: "studio" },
  { key: "portal", label: "My results", pages: "portal" },
];

export function navItemsFor(role: Role): NavItem[] {
  return NAV.filter((item) => canAccess(role, item.pages)).map(({ key, label }) => ({ key, label }));
}

/** Resolve the shell navigation from the published navigation config
 *  (kind=navigation, key=main) merged with the permissions matrix.
 *  A missing/malformed payload falls back to the built-in NAV, so the
 *  shell is never broken by a bad config; the matrix ALWAYS applies
 *  (with the built-in defaults it reproduces navItemsFor exactly). */
export function resolveNavItems(
  role: Role,
  navPayload: unknown,
  matrix: PermissionMatrix,
): NavItem[] {
  const known = new Map(NAV.map((item) => [item.key as string, item]));
  const payloadItems =
    navPayload && typeof navPayload === "object" && !Array.isArray(navPayload)
      ? (navPayload as { items?: unknown }).items
      : undefined;
  if (!Array.isArray(payloadItems)) {
    return NAV.filter((item) => canAccessWith(role, item.pages, matrix)).map(
      ({ key, label }) => ({ key, label }),
    );
  }
  const resolved: Array<NavItem & { order: number; index: number }> = [];
  payloadItems.forEach((raw, index) => {
    if (!raw || typeof raw !== "object") return;
    const item = raw as {
      module?: unknown; label?: unknown; roles?: unknown;
      order?: unknown; visible?: unknown;
    };
    if (typeof item.module !== "string") return;
    const builtin = known.get(item.module);
    if (!builtin) return; // unknown module — the shell has no such page
    if (item.visible === false) return;
    if (Array.isArray(item.roles) && !item.roles.includes(role)) return;
    if (!canAccessWith(role, builtin.pages, matrix)) return;
    resolved.push({
      key: builtin.key,
      label:
        typeof item.label === "string" && item.label.trim()
          ? item.label
          : builtin.label,
      order: typeof item.order === "number" ? item.order : index + 1,
      index,
    });
  });
  resolved.sort((a, b) => a.order - b.order || a.index - b.index);
  return resolved.map(({ key, label }) => ({ key, label }));
}

export function homePageFor(role: Role): PageKey {
  return role === "patient" ? "portal" : "patients";
}
