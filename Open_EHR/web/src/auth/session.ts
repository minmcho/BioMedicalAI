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
  // Front Door pass: the seeded front-desk account — a viewer-role
  // sign-in whose staff-directory entry (front-desk job role) lands
  // it on the front-desk audience home.
  { role: "viewer", token: "frontdesk-token", username: "dev-frontdesk", label: "Front desk (registration)" },
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
  | "home"
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
  | "beds"
  | "command"
  | "er-ops"
  | "unit-board"
  | "clinic"
  | "aftercare"
  | "safety"
  | "support"
  | "inventory"
  | "workforce"
  | "teams"
  | "reports"
  | "imports"
  | "devices"
  | "data-platform"
  | "federation"
  | "research"
  | "analytics"
  | "documents"
  | "studio"
  | "portal";

export function canAccess(role: Role, page: PageKey): boolean {
  switch (page) {
    case "home":
      // Phase 13: the role-aware landing (audience dashboard) is a
      // staff page; patients land on the portal instead.
      return role !== "patient";
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
    case "beds":
      // The Phase 4B boards and the Phase 6 bed board are staff-only
      // server-side (patients get 403); board reads include the
      // viewer, and the write actions inside each page are role-gated
      // there and enforced again server-side by capability (ed.work /
      // radiology.work / radiology.verify / surgery.work /
      // inpatient.work).
      return role === "admin" || role === "configurator" || role === "clinician" || role === "viewer";
    case "command":
    case "er-ops":
    case "unit-board":
    case "clinic":
    case "aftercare":
      // Phase 12 care-continuum operations: continuum.read is staff-wide
      // server-side (patients 403); the work actions inside each page
      // follow continuum.work and are enforced again server-side.
      return role === "admin" || role === "configurator" || role === "clinician" || role === "viewer";
    case "safety":
      // The safety triage queue is safety.review server-side (admin /
      // configurator / clinician); any staff role can FILE an event
      // from the page's report form, enforced by safety.report.
      return role === "admin" || role === "configurator" || role === "clinician";
    case "support":
      // Phase 15: support.read is staff-wide server-side (patients
      // 403); write actions inside the page follow support.work.
      return role === "admin" || role === "configurator" || role === "clinician" || role === "viewer";
    case "inventory":
      // Inventory & Materials pass: inventory.read is staff-wide
      // server-side (patients 403); write actions inside the page
      // follow inventory.manage / inventory.move.
      return role === "admin" || role === "configurator" || role === "clinician" || role === "viewer";
    case "workforce":
    case "teams":
    case "reports":
      // Phase 13: workforce.read / reports.run are staff-wide
      // server-side (patients 403); write actions inside the pages
      // follow workforce.work / data.export / data.import.
      // Teams & Panels pass: teams follows teams.read (staff-wide);
      // work actions inside follow teams.work, enforced server-side.
      return role === "admin" || role === "configurator" || role === "clinician" || role === "viewer";
    case "imports":
      // The import centre is data.import server-side: admin +
      // configurator only.
      return role === "admin" || role === "configurator";
    case "devices":
    case "data-platform":
      // Phase 14: devices.read is staff-wide server-side (patients
      // 403); registry writes follow devices.manage and pipeline runs
      // follow pipelines.run, enforced again server-side.
      return role === "admin" || role === "configurator" || role === "clinician" || role === "viewer";
    case "federation":
    case "research":
      // Phase 16: federation.read / research.read are staff-wide
      // server-side (patients 403); the work actions inside the pages
      // follow federation.work / research.work (+ esign.sign /
      // genomics.work on the research page), enforced server-side.
      return role === "admin" || role === "configurator" || role === "clinician" || role === "viewer";
    case "analytics":
    case "documents":
      // Phase 16: analytics.use / intake.work exclude the viewer by
      // default server-side (row-level surfaces), so the viewer gets
      // no nav entry either.
      return role === "admin" || role === "configurator" || role === "clinician";
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
  beds: "inpatient.read",
  // Phase 12: the continuum pages follow continuum.read; the safety
  // triage page follows safety.review (filing stays open to staff via
  // safety.report, checked inside the page).
  command: "continuum.read",
  "er-ops": "continuum.read",
  "unit-board": "continuum.read",
  clinic: "continuum.read",
  aftercare: "continuum.read",
  safety: "safety.review",
  // Phase 15: the support services page follows support.read.
  support: "support.read",
  // Inventory & Materials pass: the inventory page follows
  // inventory.read (staff-wide incl. viewer).
  inventory: "inventory.read",
  // Phase 13: workforce follows workforce.read, reports follows
  // reports.run, the import centre follows data.import.
  workforce: "workforce.read",
  teams: "teams.read",
  reports: "reports.run",
  imports: "data.import",
  // Phase 14: both device/data pages ride devices.read (registry,
  // lake, quarantine and pipeline reads are one read surface;
  // pipelines.run gates the run buttons inside the page).
  devices: "devices.read",
  "data-platform": "devices.read",
  // Phase 16: federation / research pages follow their read
  // capabilities (viewer included); analytics follows analytics.use
  // and documents follows intake.work (viewer excluded by default) —
  // the seeded navigation roles match these gates exactly
  // (backend tests/test_nav_seed_consistency.py pins the pairing).
  federation: "federation.read",
  research: "research.read",
  analytics: "analytics.use",
  documents: "intake.work",
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
  if (page === "patients" || page === "chart" || page === "home") return role !== "patient";
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

export interface NavSection {
  label: string;
  section: string;
}

export interface NavItem {
  key: PageKey;
  label: string;
  group?: string;
  sections?: NavSection[];
  /** Home & Module UX pass: one-line purpose shown on the Home card
   *  menu (published nav payload field, built-in fallback below). */
  purpose?: string;
  /** Home & Module UX pass: icon name for the Home card menu
   *  (published nav payload field, built-in fallback below). */
  icon?: string;
}

// Depth pass (Workstream 1): built-in fallback groups mirror the
// backend seed in configstore._DEFAULT_NAV_MODULES exactly, and the
// lab/pharmacy deep-link sections mirror _DEFAULT_NAV_SECTIONS.
export const LAB_NAV_SECTIONS: NavSection[] = [
  { label: "Orders & Results", section: "orders" },
  { label: "Microbiology", section: "microbiology" },
  { label: "Virology", section: "virology" },
  { label: "Blood Bank", section: "blood-bank" },
  { label: "Pathology", section: "pathology" },
  { label: "QC & EQA", section: "qc" },
  // Phase 15: Test Manager / Panel Manager tabs + the specimen
  // chain-of-custody view, mirroring _DEFAULT_NAV_SECTIONS.
  { label: "Test Catalogue", section: "test-catalogue" },
  { label: "Panels", section: "panels" },
  { label: "Specimens", section: "specimens" },
  { label: "Send-outs", section: "sendouts" },
  { label: "POCT", section: "poct" },
];

export const COMMAND_NAV_SECTIONS: NavSection[] = [
  { label: "Desks", section: "desks" },
  { label: "Access line", section: "access-line" },
];

export const DEVICES_NAV_SECTIONS: NavSection[] = [
  { label: "RFID", section: "rfid" },
  // Phase 16: HTM work orders + preventive maintenance ride the
  // Devices module as a section (Phase 14 device spine).
  { label: "Maintenance", section: "maintenance" },
];

export const PHARMACY_NAV_SECTIONS: NavSection[] = [
  { label: "Verification", section: "verification" },
  { label: "Inventory & procurement", section: "inventory" },
  { label: "Controlled substances", section: "controlled" },
  { label: "Compounding", section: "compounding" },
  { label: "BCMA", section: "bcma" },
];

// Home & Module UX pass: the built-in fallback mirrors the backend
// seed in configstore._DEFAULT_NAV_MODULES after the NAV REGROUP
// (PROGRESS ledger, fixed contract): 10 groups of 2–6 modules (spec
// N-2). Group labels only changed — per-module roles and the
// order-within-group follow the ledger table; the array itself is in
// group order so navItemsFor / resolveNavItems keep a stable,
// group-contiguous order. Every item also carries the `purpose` +
// `icon` fallbacks the Home card menu renders when a published
// payload predates those fields (mirrors the backend seed purposes).
const NAV: Array<NavItem & { pages: PageKey }> = [
  // Patient Care
  { key: "patients", label: "Patients", pages: "patients", group: "Patient Care", purpose: "Find, register and open patient records", icon: "users" },
  { key: "inbox", label: "Results inbox", pages: "inbox", group: "Patient Care", purpose: "Review results and messages awaiting action", icon: "inbox" },
  { key: "portal", label: "My results", pages: "portal", group: "Patient Care", purpose: "Your results, messages and appointments", icon: "heart" },
  // Access & Revenue
  { key: "scheduling", label: "Scheduling", pages: "scheduling", group: "Access & Revenue", purpose: "Book, move and manage appointments", icon: "calendar" },
  { key: "billing", label: "Billing", pages: "billing", group: "Access & Revenue", purpose: "Claims, payments and patient statements", icon: "receipt" },
  // Clinical
  { key: "ed", label: "ED board", pages: "ed", group: "Clinical", purpose: "Track emergency department patients live", icon: "siren" },
  { key: "radiology", label: "Radiology", pages: "radiology", group: "Clinical", purpose: "Imaging orders, studies and reports", icon: "scan" },
  { key: "surgery", label: "Surgery board", pages: "surgery", group: "Clinical", purpose: "Operating lists and perioperative flow", icon: "scalpel" },
  // Diagnostics & Pharmacy
  { key: "lab", label: "Laboratory", pages: "lab", group: "Diagnostics & Pharmacy", sections: LAB_NAV_SECTIONS, purpose: "Laboratory orders, results and benches", icon: "flask" },
  { key: "pharmacy", label: "Pharmacy", pages: "pharmacy", group: "Diagnostics & Pharmacy", sections: PHARMACY_NAV_SECTIONS, purpose: "Verification, dispensing and stock", icon: "pill" },
  // Operations
  { key: "beds", label: "Bed board", pages: "beds", group: "Operations", purpose: "Bed state, admissions and transfers", icon: "bed" },
  { key: "command", label: "Command Center", pages: "command", group: "Operations", sections: COMMAND_NAV_SECTIONS, purpose: "House-wide flow and capacity oversight", icon: "command" },
  { key: "er-ops", label: "ER operations", pages: "er-ops", group: "Operations", purpose: "Emergency operations and boarding", icon: "activity" },
  { key: "unit-board", label: "Unit board", pages: "unit-board", group: "Operations", purpose: "Unit census, rounds and discharge readiness", icon: "board" },
  { key: "clinic", label: "Clinic day", pages: "clinic", group: "Operations", purpose: "Today's clinics, arrivals and rooming", icon: "stethoscope" },
  { key: "aftercare", label: "Aftercare", pages: "aftercare", group: "Operations", purpose: "Follow-up, referrals and care transitions", icon: "route" },
  // Safety & Support
  { key: "safety", label: "Safety", pages: "safety", group: "Safety & Support", purpose: "Safety events, triage and follow-up", icon: "shield" },
  { key: "support", label: "Support services", pages: "support", group: "Safety & Support", purpose: "Support services and hospital operations", icon: "lifebuoy" },
  { key: "inventory", label: "Inventory & Materials", pages: "inventory", group: "Safety & Support", purpose: "Stock, supplies, implants and materials across locations", icon: "boxes" },
  // People & Teams
  { key: "workforce", label: "Workforce", pages: "workforce", group: "People & Teams", purpose: "Staff directory, rosters and credentials", icon: "id-badge" },
  { key: "teams", label: "Teams", pages: "teams", group: "People & Teams", purpose: "Care teams, panels and coverage", icon: "people" },
  // Insights
  { key: "pophealth", label: "Population health", pages: "pophealth", group: "Insights", purpose: "Registries and population cohorts", icon: "chart" },
  { key: "reports", label: "Reports", pages: "reports", group: "Insights", purpose: "Run reports and export documents", icon: "file" },
  { key: "analytics", label: "Analytics", pages: "analytics", group: "Insights", purpose: "Governed measures, views and indicators", icon: "graph" },
  // Data Platform
  { key: "devices", label: "Devices", pages: "devices", group: "Data Platform", sections: DEVICES_NAV_SECTIONS, purpose: "Device registry, feeds and maintenance", icon: "device" },
  { key: "data-platform", label: "Data platform", pages: "data-platform", group: "Data Platform", purpose: "Data lake, pipelines and data quality", icon: "database" },
  { key: "federation", label: "Federation", pages: "federation", group: "Data Platform", purpose: "Peer exchange and record reconciliation", icon: "network" },
  { key: "research", label: "Research", pages: "research", group: "Data Platform", purpose: "Studies, cohorts and research operations", icon: "microscope" },
  { key: "documents", label: "Documents", pages: "documents", group: "Data Platform", purpose: "Document intake and processing worklist", icon: "folder" },
  // System
  { key: "studio", label: "System Studio", pages: "studio", group: "System", purpose: "Configure modules, navigation and branding", icon: "sliders" },
  { key: "imports", label: "Import centre", pages: "imports", group: "System", purpose: "Import data files and review mappings", icon: "upload" },
];

export function navItemsFor(role: Role): NavItem[] {
  return NAV.filter((item) => canAccess(role, item.pages)).map(
    ({ key, label, group, sections, purpose, icon }) => ({
      key,
      label,
      ...(group !== undefined ? { group } : {}),
      ...(sections !== undefined ? { sections } : {}),
      ...(purpose !== undefined ? { purpose } : {}),
      ...(icon !== undefined ? { icon } : {}),
    }),
  );
}

function parseNavSections(raw: unknown): NavSection[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const out: NavSection[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== "object") continue;
    const e = entry as { label?: unknown; section?: unknown };
    if (typeof e.label === "string" && e.label.trim()
        && typeof e.section === "string" && e.section.trim()) {
      out.push({ label: e.label, section: e.section });
    }
  }
  return out.length > 0 ? out : undefined;
}

/** Resolve the shell navigation from the published navigation config
 *  (kind=navigation, key=main) merged with the permissions matrix.
 *  A missing/malformed payload falls back to the built-in NAV, so the
 *  shell is never broken by a bad config; the matrix ALWAYS applies
 *  (with the built-in defaults it reproduces navItemsFor exactly).
 *  Depth pass: items also carry `group`/`sections` — parsed from the
 *  payload when present, otherwise the built-in fallback's values, so
 *  an older published payload (no group/sections) still groups. */
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
      ({ key, label, group, sections, purpose, icon }) => ({
        key,
        label,
        ...(group !== undefined ? { group } : {}),
        ...(sections !== undefined ? { sections } : {}),
        ...(purpose !== undefined ? { purpose } : {}),
        ...(icon !== undefined ? { icon } : {}),
      }),
    );
  }
  const resolved: Array<NavItem & { order: number; index: number }> = [];
  payloadItems.forEach((raw, index) => {
    if (!raw || typeof raw !== "object") return;
    const item = raw as {
      module?: unknown; label?: unknown; roles?: unknown;
      order?: unknown; visible?: unknown; group?: unknown; sections?: unknown;
      purpose?: unknown; icon?: unknown;
    };
    if (typeof item.module !== "string") return;
    const builtin = known.get(item.module);
    if (!builtin) return; // unknown module — the shell has no such page
    if (item.visible === false) return;
    if (Array.isArray(item.roles) && !item.roles.includes(role)) return;
    if (!canAccessWith(role, builtin.pages, matrix)) return;
    const group =
      typeof item.group === "string" && item.group.trim()
        ? item.group
        : builtin.group;
    const sections = parseNavSections(item.sections) ?? builtin.sections;
    // Home & Module UX pass: purpose/icon ride the payload when a
    // site published them; older payloads fall back to the built-in
    // mirror so the card menu never renders a blank purpose line.
    const purpose =
      typeof item.purpose === "string" && item.purpose.trim()
        ? item.purpose
        : builtin.purpose;
    const icon =
      typeof item.icon === "string" && item.icon.trim()
        ? item.icon
        : builtin.icon;
    resolved.push({
      key: builtin.key,
      label:
        typeof item.label === "string" && item.label.trim()
          ? item.label
          : builtin.label,
      ...(group !== undefined ? { group } : {}),
      ...(sections !== undefined ? { sections } : {}),
      ...(purpose !== undefined ? { purpose } : {}),
      ...(icon !== undefined ? { icon } : {}),
      order: typeof item.order === "number" ? item.order : index + 1,
      index,
    });
  });
  resolved.sort((a, b) => a.order - b.order || a.index - b.index);
  return resolved.map(({ key, label, group, sections, purpose, icon }) => ({
    key,
    label,
    ...(group !== undefined ? { group } : {}),
    ...(sections !== undefined ? { sections } : {}),
    ...(purpose !== undefined ? { purpose } : {}),
    ...(icon !== undefined ? { icon } : {}),
  }));
}

export const NAV_GROUP_ORDER: Array<string | null> = [
  "Patient Care",
  "Access & Revenue",
  "Clinical",
  "Diagnostics & Pharmacy",
  "Operations",
  "Safety & Support",
  "People & Teams",
  "Insights",
  "Data Platform",
  "System",
];

/** Group resolved nav items for the shell: known groups in
 *  NAV_GROUP_ORDER, unknown groups after in first-seen order, and
 *  ungrouped items in a leading null group. Item order inside each
 *  group is the resolved nav order. */
export function resolveNavGroups(
  role: Role,
  navPayload: unknown,
  matrix: PermissionMatrix,
): Array<{ group: string | null; items: NavItem[] }> {
  const items = resolveNavItems(role, navPayload, matrix);
  const byGroup = new Map<string | null, NavItem[]>();
  const firstSeen: Array<string | null> = [];
  for (const item of items) {
    const g = item.group ?? null;
    if (!byGroup.has(g)) {
      byGroup.set(g, []);
      firstSeen.push(g);
    }
    byGroup.get(g)!.push(item);
  }
  const ordered: Array<{ group: string | null; items: NavItem[] }> = [];
  if (byGroup.has(null)) {
    ordered.push({ group: null, items: byGroup.get(null)! });
  }
  for (const g of NAV_GROUP_ORDER) {
    if (g !== null && byGroup.has(g)) {
      ordered.push({ group: g, items: byGroup.get(g)! });
    }
  }
  for (const g of firstSeen) {
    if (g === null) continue;
    if ((NAV_GROUP_ORDER as Array<string | null>).includes(g)) continue;
    ordered.push({ group: g, items: byGroup.get(g)! });
  }
  return ordered;
}

// ---- Job profiles (Home & Module UX pass) ----
// The published navigation config key "job-profiles" maps a caller's
// job role (people.py catalogue: physician, nurse, pharmacist, …) to
// the module keys that role's Home emphasises. The effective menu is
// the role/permissions resolution INTERSECTED with the profile when
// the caller's job role is known — and the intersection NEVER empties
// the menu: an empty result falls back to the unfiltered role menu
// (ledger contract). Parsing is defensive like every other runtime
// config read: a malformed payload yields no profiles, i.e. no
// filtering, never a broken menu.

export type JobProfiles = Record<string, string[]>;

export function parseJobProfiles(payload: unknown): JobProfiles {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return {};
  }
  const profiles = (payload as { profiles?: unknown }).profiles;
  if (!profiles || typeof profiles !== "object" || Array.isArray(profiles)) {
    return {};
  }
  const known = new Set<string>(NAV.map((item) => item.key as string));
  const out: JobProfiles = {};
  for (const [jobRole, raw] of Object.entries(profiles as Record<string, unknown>)) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) continue;
    const modules = (raw as { modules?: unknown }).modules;
    if (!Array.isArray(modules)) continue;
    const keys = modules.filter(
      (m): m is string => typeof m === "string" && known.has(m),
    );
    if (keys.length > 0) out[jobRole] = keys;
  }
  return out;
}

/** Intersect resolved nav groups with a job profile. Returns the
 *  groups unchanged when the job role is unknown, has no profile, or
 *  the intersection would empty the menu. Group order and
 *  within-group order are preserved; emptied groups drop out.
 *  Operator roles (admin / configurator) are EXEMPT: they administer
 *  the whole system, so a staff-record link (the demo directory
 *  links the admin sign-in to an executive) must never narrow their
 *  menu — the backend applies the same exemption in
 *  configstore.effective_modules. */
export function applyJobProfile(
  groups: Array<{ group: string | null; items: NavItem[] }>,
  profiles: JobProfiles | null | undefined,
  jobRole: string | null | undefined,
  role?: Role | null | undefined,
): Array<{ group: string | null; items: NavItem[] }> {
  if (role === "admin" || role === "configurator") return groups;
  if (!profiles || !jobRole) return groups;
  const allowed = profiles[jobRole];
  if (!allowed || allowed.length === 0) return groups;
  const allowedSet = new Set(allowed);
  const filtered = groups
    .map(({ group, items }) => ({
      group,
      items: items.filter((item) => allowedSet.has(item.key as string)),
    }))
    .filter(({ items }) => items.length > 0);
  const total = filtered.reduce((n, g) => n + g.items.length, 0);
  return total > 0 ? filtered : groups;
}

export function homePageFor(role: Role): PageKey {
  // Phase 13: staff land on Home — their published audience dashboard
  // (clinician / nurse / unit manager / department head / executive),
  // falling back to the patient list inside the page when no dashboard
  // is published for their audience. Patients land on the portal.
  return role === "patient" ? "portal" : "home";
}
