// Structured editor models for the System Studio visual tabs.
//
// Each config kind gets a parse (payload -> editor model) and serialize
// (editor model -> payload) pair. The rule throughout: fields the visual
// editor does not model are PRESERVED verbatim (collected into `rest`
// bags), so a round-trip through a visual tab never silently drops data
// that belongs to the owning module — the same discipline the backend
// validators use ("extra fields are data for the owning module").
//
// Payload shapes mirror backend/app/configstore.py validators exactly.

import type { Role } from "../../api/types";

export const ALL_ROLES: Role[] = ["admin", "configurator", "clinician", "patient", "viewer"];

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

/** "admin, clinician" <-> ["admin", "clinician"] for compact roles inputs. */
export function rolesToText(roles: string[]): string {
  return roles.join(", ");
}

export function rolesFromText(text: string): string[] {
  return text
    .split(",")
    .map((r) => r.trim())
    .filter(Boolean);
}

// ---------------------------------------------------------------------------
// Forms — FHIR Questionnaire (configstore.validate_form)
// ---------------------------------------------------------------------------

/** Item types offered by the designer (a subset of the backend's
 *  FORM_ITEM_TYPES); an existing item of any other type keeps its type
 *  as an extra option in the editor's select. */
export const FORM_TYPE_OPTIONS = ["string", "decimal", "integer", "boolean", "date", "choice"];

export interface FormOption {
  code: string;
  display: string;
}

export interface FormItemModel {
  linkId: string;
  text: string;
  type: string;
  required: boolean;
  options: FormOption[];
  /** Unmodelled item fields (e.g. `valueSet`) preserved verbatim. */
  rest: Record<string, unknown>;
}

export interface FormModel {
  title: string;
  items: FormItemModel[];
  /** Unmodelled top-level fields preserved verbatim. */
  rest: Record<string, unknown>;
}

export function parseForm(payload: Record<string, unknown>): FormModel {
  const { item, title, resourceType: _rt, ...rest } = payload;
  const items: FormItemModel[] = (Array.isArray(item) ? item : []).map((raw) => {
    const obj = isObj(raw) ? raw : {};
    const { linkId, text, type, required, answerOption, ...itemRest } = obj;
    const options: FormOption[] = (Array.isArray(answerOption) ? answerOption : []).map(
      (opt) => {
        const o = isObj(opt) ? opt : {};
        const coding = isObj(o.valueCoding) ? o.valueCoding : {};
        return { code: str(coding.code), display: str(coding.display) };
      },
    );
    return {
      linkId: str(linkId),
      text: str(text),
      type: str(type) || "string",
      required: required === true,
      options,
      rest: itemRest,
    };
  });
  return { title: str(title), items, rest };
}

export function serializeForm(model: FormModel): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    ...model.rest,
    resourceType: "Questionnaire",
    item: model.items.map((item) => {
      const out: Record<string, unknown> = {
        ...item.rest,
        linkId: item.linkId,
        text: item.text,
        type: item.type,
        required: item.required,
      };
      if (item.type === "choice" && item.options.length > 0) {
        out.answerOption = item.options.map((o) => ({
          valueCoding: { code: o.code, display: o.display || o.code },
        }));
      }
      return out;
    }),
  };
  if (model.title) payload.title = model.title;
  return payload;
}

export function emptyForm(): FormModel {
  return {
    title: "",
    items: [{ linkId: "q1", text: "", type: "string", required: false, options: [], rest: {} }],
    rest: {},
  };
}

// ---------------------------------------------------------------------------
// Workflows — state machines (configstore.validate_workflow)
// ---------------------------------------------------------------------------

export interface WorkflowStateModel {
  name: string;
  initial: boolean;
  terminal: boolean;
}

export interface WorkflowTransitionModel {
  from: string;
  to: string;
  /** Stored under the payload's `action` field (the transition label). */
  action: string;
  roles: string[];
  rest: Record<string, unknown>;
}

export interface WorkflowModel {
  states: WorkflowStateModel[];
  transitions: WorkflowTransitionModel[];
  rest: Record<string, unknown>;
}

export function parseWorkflow(payload: Record<string, unknown>): WorkflowModel {
  const { states, transitions, initial, terminal, ...rest } = payload;
  const terminalSet = new Set(strList(terminal));
  return {
    states: strList(states).map((name) => ({
      name,
      initial: initial === name,
      terminal: terminalSet.has(name),
    })),
    transitions: (Array.isArray(transitions) ? transitions : []).map((raw) => {
      const t = isObj(raw) ? raw : {};
      const { from, to, action, roles, ...tRest } = t;
      return {
        from: str(from),
        to: str(to),
        action: str(action),
        roles: strList(roles),
        rest: tRest,
      };
    }),
    rest,
  };
}

export function serializeWorkflow(model: WorkflowModel): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    ...model.rest,
    states: model.states.map((s) => s.name),
    transitions: model.transitions.map((t) => {
      const out: Record<string, unknown> = { ...t.rest, from: t.from, to: t.to };
      if (t.action) out.action = t.action;
      if (t.roles.length > 0) out.roles = [...t.roles];
      return out;
    }),
  };
  const initial = model.states.find((s) => s.initial);
  if (initial) payload.initial = initial.name;
  const terminal = model.states.filter((s) => s.terminal).map((s) => s.name);
  if (terminal.length > 0) payload.terminal = terminal;
  return payload;
}

export function emptyWorkflow(): WorkflowModel {
  return {
    states: [
      { name: "open", initial: true, terminal: false },
      { name: "done", initial: false, terminal: true },
    ],
    transitions: [{ from: "open", to: "done", action: "", roles: [], rest: {} }],
    rest: {},
  };
}

// ---------------------------------------------------------------------------
// Terminology — concept lists (configstore.validate_terminology).
// Matches the lab catalog shape (backend/app/lab.py): each concept carries
// `unit` at top level AND inside referenceRange; the editor edits one unit
// and serializes both, exactly like the seed data.
// ---------------------------------------------------------------------------

export interface ConceptModel {
  code: string;
  display: string;
  active: boolean;
  unit: string;
  low: string;
  high: string;
  criticalLow: string;
  criticalHigh: string;
  /** Unmodelled concept fields (e.g. a panel's `components`) preserved. */
  rest: Record<string, unknown>;
}

export interface TerminologyModel {
  name: string;
  system: string;
  concepts: ConceptModel[];
  rest: Record<string, unknown>;
}

function numToText(v: unknown): string {
  return typeof v === "number" ? String(v) : "";
}

export function parseTerminology(payload: Record<string, unknown>): TerminologyModel {
  const { concepts, name, system, ...rest } = payload;
  return {
    name: str(name),
    system: str(system),
    concepts: (Array.isArray(concepts) ? concepts : []).map((raw) => {
      const c = isObj(raw) ? raw : {};
      const { code, display, active, unit, referenceRange, ...cRest } = c;
      const rng = isObj(referenceRange) ? referenceRange : {};
      const { unit: rangeUnit, ...rangeRest } = rng;
      return {
        code: str(code),
        display: str(display),
        active: active !== false,
        unit: str(unit) || str(rangeUnit),
        low: numToText(rng.low),
        high: numToText(rng.high),
        criticalLow: numToText(rng.criticalLow),
        criticalHigh: numToText(rng.criticalHigh),
        rest: { ...cRest, rangeRest },
      };
    }),
    rest,
  };
}

/** Returns an error message, or null when every numeric field parses. */
export function terminologyError(model: TerminologyModel): string | null {
  for (const c of model.concepts) {
    for (const [label, value] of [
      ["low", c.low],
      ["high", c.high],
      ["critical low", c.criticalLow],
      ["critical high", c.criticalHigh],
    ] as const) {
      if (value.trim() !== "" && Number.isNaN(Number(value))) {
        return `Concept ${c.code || "?"}: reference ${label} "${value}" is not a number.`;
      }
    }
  }
  return null;
}

export function serializeTerminology(model: TerminologyModel): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    ...model.rest,
    concepts: model.concepts.map((c) => {
      const { rangeRest, ...conceptRest } = c.rest;
      const out: Record<string, unknown> = {
        ...conceptRest,
        code: c.code,
        display: c.display,
        active: c.active,
      };
      const rng: Record<string, unknown> = { ...(isObj(rangeRest) ? rangeRest : {}) };
      const put = (field: string, value: string) => {
        if (value.trim() !== "" && !Number.isNaN(Number(value))) rng[field] = Number(value);
      };
      put("low", c.low);
      put("high", c.high);
      put("criticalLow", c.criticalLow);
      put("criticalHigh", c.criticalHigh);
      if (c.unit) {
        out.unit = c.unit;
        rng.unit = c.unit;
      }
      if (Object.keys(rng).length > 0) out.referenceRange = rng;
      return out;
    }),
  };
  if (model.name) payload.name = model.name;
  if (model.system) payload.system = model.system;
  return payload;
}

export function emptyTerminology(): TerminologyModel {
  return {
    name: "",
    system: "",
    concepts: [
      { code: "", display: "", active: true, unit: "", low: "", high: "",
        criticalLow: "", criticalHigh: "", rest: {} },
    ],
    rest: {},
  };
}

// ---------------------------------------------------------------------------
// Navigation — modules only (configstore.validate_navigation, HTTP 400 rule)
// ---------------------------------------------------------------------------

export interface NavItemModel {
  module: string;
  label: string;
  roles: string[];
  visible: boolean;
  rest: Record<string, unknown>;
}

export interface NavigationModel {
  items: NavItemModel[];
  rest: Record<string, unknown>;
}

export function parseNavigation(payload: Record<string, unknown>): NavigationModel {
  const { items, ...rest } = payload;
  return {
    items: (Array.isArray(items) ? items : []).map((raw) => {
      const it = isObj(raw) ? raw : {};
      const { module, label, roles, visible, order: _order, ...iRest } = it;
      return {
        module: str(module),
        label: str(label),
        roles: strList(roles),
        visible: visible !== false,
        rest: iRest,
      };
    }),
    rest,
  };
}

export function serializeNavigation(model: NavigationModel): Record<string, unknown> {
  return {
    ...model.rest,
    // Row position IS the order — writing it explicitly keeps the visual
    // order and the stored order identical.
    items: model.items.map((it, i) => ({
      ...it.rest,
      module: it.module,
      label: it.label,
      ...(it.roles.length > 0 ? { roles: [...it.roles] } : {}),
      order: i + 1,
      visible: it.visible,
    })),
  };
}

export function emptyNavigation(): NavigationModel {
  return { items: [{ module: "", label: "", roles: [], visible: true, rest: {} }], rest: {} };
}

// ---------------------------------------------------------------------------
// Branding — org profile (configstore.validate_branding)
// ---------------------------------------------------------------------------

export interface BrandingModel {
  orgName: string;
  portalTitle: string;
  tagline: string;
  primaryColor: string;
  logoUrl: string;
  supportContact: string;
  /** Rich Design Elevation: the draft's theme.groupColors edits —
   *  canonical nav group label -> #RRGGBB, holding ONLY the groups
   *  the payload itself carries (published or edited here). The
   *  editor displays the EFFECTIVE colour (this map, else the
   *  built-in default from auth/runtimeConfig) per group; groups
   *  absent from the map keep rendering in their default, so an
   *  untouched editor never changes the payload's theme shape. */
  groupColors: Record<string, string>;
  rest: Record<string, unknown>;
}

const HEX_COLOR = /^#[0-9a-fA-F]{6}$/;

export function parseBranding(payload: Record<string, unknown>): BrandingModel {
  const { orgName, portalTitle, tagline, primaryColor, logoUrl, supportContact, ...rest } =
    payload;
  const theme = isObj(payload.theme) ? payload.theme : {};
  const rawGroupColors = isObj(theme.groupColors) ? theme.groupColors : {};
  const groupColors: Record<string, string> = {};
  for (const [label, value] of Object.entries(rawGroupColors)) {
    if (typeof value === "string" && HEX_COLOR.test(value)) {
      groupColors[label] = value;
    }
  }
  return {
    orgName: str(orgName),
    portalTitle: str(portalTitle),
    tagline: str(tagline),
    primaryColor: str(primaryColor) || "#1a7f37",
    logoUrl: str(logoUrl),
    supportContact: str(supportContact),
    groupColors,
    rest,
  };
}

export function serializeBranding(model: BrandingModel): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    ...model.rest,
    orgName: model.orgName,
    portalTitle: model.portalTitle,
  };
  if (model.tagline) payload.tagline = model.tagline;
  if (model.primaryColor) payload.primaryColor = model.primaryColor;
  if (model.logoUrl) payload.logoUrl = model.logoUrl;
  if (model.supportContact) payload.supportContact = model.supportContact;
  // Theme rides in `rest`; re-apply the groupColors edits on top of
  // whatever theme the payload already carried (other theme fields
  // are preserved verbatim). An empty edit map removes the key, so
  // a payload that never had groupColors stays without it.
  const theme: Record<string, unknown> = {
    ...(isObj(model.rest.theme) ? model.rest.theme : {}),
  };
  if (Object.keys(model.groupColors).length > 0) {
    theme.groupColors = model.groupColors;
  } else {
    delete theme.groupColors;
  }
  if (Object.keys(theme).length > 0) {
    payload.theme = theme;
  } else {
    delete payload.theme;
  }
  return payload;
}

export function emptyBranding(): BrandingModel {
  return {
    orgName: "",
    portalTitle: "",
    tagline: "",
    primaryColor: "#1a7f37",
    logoUrl: "",
    supportContact: "",
    groupColors: {},
    rest: {},
  };
}

// ---------------------------------------------------------------------------
// Permissions — the role/capability matrix (configstore: kind=permissions,
// key=role-matrix). The server merges the published payload over
// CAPABILITY_DEFAULTS; this mirror of the defaults is what the matrix
// starts from, exactly like session.ts mirrors the server's role gates.
// ---------------------------------------------------------------------------

export const CAPABILITY_DEFAULTS: Record<string, Role[]> = {
  "fhir.write": ["admin", "configurator", "clinician"],
  "fhir.inbox": ["admin", "configurator", "clinician"],
  "studio.manage": ["admin", "configurator"],
  "studio.read": ["admin", "configurator", "clinician", "viewer"],
  "lab.order": ["admin", "configurator", "clinician"],
  "lab.work": ["admin", "configurator", "clinician"],
  "lab.verify": ["admin", "clinician"],
  "lab.read": ["admin", "configurator", "clinician", "viewer"],
  "pharmacy.read": ["admin", "configurator", "clinician", "viewer"],
  "boards.read": ["admin", "configurator", "clinician", "viewer"],
  "pophealth.view": ["admin", "clinician"],
  "pophealth.run": ["admin"],
  "billing.view": ["admin", "configurator", "clinician"],
  "billing.manage": ["admin", "configurator"],
  "pharmacy.dispense": ["admin", "configurator", "clinician"],
  "pharmacy.administer": ["admin", "clinician"],
  // Phase 11 (pharmacy department depth) — mirrors configstore.py
  // CAPABILITY_DEFAULTS exactly, like every row above.
  "pharmacy.verify": ["admin", "clinician"],
  "pharmacy.inventory": ["admin", "configurator", "clinician"],
  "pharmacy.compound": ["admin", "configurator", "clinician"],
  "pharmacy.cs": ["admin", "clinician"],
  "ed.work": ["admin", "configurator", "clinician"],
  "radiology.work": ["admin", "configurator", "clinician"],
  "radiology.verify": ["admin", "clinician"],
  "surgery.work": ["admin", "configurator", "clinician"],
  // Phase 5 (orders + documentation) — mirrors configstore.py
  // CAPABILITY_DEFAULTS exactly, like every row above.
  "orders.read": ["admin", "configurator", "clinician", "viewer"],
  "orders.place": ["admin", "configurator", "clinician"],
  "notes.read": ["admin", "configurator", "clinician", "viewer"],
  "notes.write": ["admin", "configurator", "clinician"],
  // Phase 6 (inpatient operations) — mirrors configstore.py
  // CAPABILITY_DEFAULTS exactly, like every row above.
  "inpatient.read": ["admin", "configurator", "clinician", "viewer"],
  "inpatient.work": ["admin", "configurator", "clinician"],
  // Phase 7 (access & revenue cycle) — mirrors configstore.py
  // CAPABILITY_DEFAULTS exactly, like every row above.
  "access.read": ["admin", "configurator", "clinician", "viewer"],
  "access.work": ["admin", "configurator", "clinician"],
  "coding.read": ["admin", "configurator", "clinician"],
  "coding.work": ["admin", "configurator"],
  // Phase 8 (interoperability) — mirrors configstore.py
  // CAPABILITY_DEFAULTS exactly, like every row above.
  "interop.read": ["admin", "configurator", "clinician", "viewer"],
  "interop.manage": ["admin", "configurator"],
  "interop.export": ["admin", "configurator"],
  // Phase 9 (engagement + research) — mirrors configstore.py
  // CAPABILITY_DEFAULTS exactly, like every row above.
  "messages.use": ["admin", "configurator", "clinician"],
  "portal.self-schedule": ["admin", "clinician", "patient"],
  "pophealth.export": ["admin", "configurator"],
  // Phase 10 (agents + privacy) — mirrors configstore.py
  // CAPABILITY_DEFAULTS exactly. The break-glass pair is split on
  // purpose: opening roles != reviewing roles (separation of duties).
  "agents.view": ["admin", "configurator", "clinician"],
  "agents.run": ["admin", "configurator", "clinician"],
  "privacy.breakglass": ["admin", "clinician"],
  "privacy.review": ["admin", "configurator"],
  // Phase 12 (care-continuum operations) — mirrors configstore.py
  // CAPABILITY_DEFAULTS exactly, like every row above. safety.report
  // is every staff role (anyone can file); safety.review is the
  // safety team's triage queue.
  "continuum.read": ["admin", "configurator", "clinician", "viewer"],
  "continuum.work": ["admin", "configurator", "clinician"],
  "safety.report": ["admin", "configurator", "clinician", "viewer"],
  "safety.review": ["admin", "configurator", "clinician"],
  // Phase 13 (people & data platform) — mirrors configstore.py
  // CAPABILITY_DEFAULTS exactly, like every row above.
  "workforce.read": ["admin", "configurator", "clinician", "viewer"],
  "workforce.work": ["admin", "configurator"],
  "reports.run": ["admin", "configurator", "clinician", "viewer"],
  "data.export": ["admin", "configurator"],
  "data.import": ["admin", "configurator"],
  // Phase 14 (devices, IoT, lake & pipelines) — mirrors configstore.py
  // CAPABILITY_DEFAULTS exactly, like every row above.
  "devices.read": ["admin", "configurator", "clinician", "viewer"],
  "devices.manage": ["admin", "configurator", "clinician"],
  "pipelines.run": ["admin", "configurator"],
  // Front Door pass (accounts & registration) — mirrors
  // configstore.py CAPABILITY_DEFAULTS exactly, like every row above.
  "accounts.manage": ["admin"],
  "registration.work": ["admin", "configurator", "clinician", "viewer"],
  // Phase 16 (Intelligence & Research) — mirrors configstore.py
  // CAPABILITY_DEFAULTS exactly, like every row above. mcp.use
  // includes the patient role ONLY through the module's published
  // patient-scope guardrails; rag.search and analytics.use
  // deliberately exclude viewer (row-level surfaces).
  "federation.read": ["admin", "configurator", "clinician", "viewer"],
  "federation.work": ["admin", "configurator", "clinician"],
  "directories.read": ["admin", "configurator", "clinician", "viewer"],
  "directories.sync": ["admin", "configurator"],
  "agents.validate": ["admin", "configurator", "clinician"],
  "innovation.manage": ["admin", "configurator", "clinician"],
  "rag.search": ["admin", "configurator", "clinician"],
  "mcp.use": ["admin", "configurator", "clinician", "viewer", "patient"],
  "a2a.use": ["admin", "configurator", "clinician"],
  "genomics.read": ["admin", "configurator", "clinician", "viewer"],
  "genomics.work": ["admin", "configurator", "clinician"],
  "research.read": ["admin", "configurator", "clinician", "viewer"],
  "research.work": ["admin", "configurator", "clinician"],
  "esign.sign": ["admin", "configurator", "clinician"],
  "empi.steward": ["admin", "configurator", "clinician"],
  "intake.work": ["admin", "configurator", "clinician"],
  "analytics.use": ["admin", "configurator", "clinician"],
  "maintenance.read": ["admin", "configurator", "clinician", "viewer"],
  "maintenance.work": ["admin", "configurator", "clinician"],
  // Workforce, Teams & Panels pass — mirrors configstore.py
  // CAPABILITY_DEFAULTS exactly, like every row above. teams.read is
  // staff-wide; teams.work covers assignment / coverage / panel work
  // (clinicians included — they accept and cover); workforce.onboard
  // is the HR onboarding workflow (admin + configurator).
  "teams.read": ["admin", "configurator", "clinician", "viewer"],
  "teams.work": ["admin", "configurator", "clinician"],
  "workforce.onboard": ["admin", "configurator"],
};

/** capability -> role -> allowed (fully resolved matrix). */
export type PermissionMatrix = Record<string, Record<Role, boolean>>;

export function parsePermissions(payload: Record<string, unknown>): PermissionMatrix {
  const matrix: PermissionMatrix = {};
  const overrides = isObj(payload.capabilities) ? payload.capabilities : {};
  for (const capability of Object.keys(CAPABILITY_DEFAULTS)) {
    const row = {} as Record<Role, boolean>;
    const capOverride = isObj(overrides[capability]) ? overrides[capability] : {};
    for (const role of ALL_ROLES) {
      row[role] =
        typeof capOverride[role] === "boolean"
          ? (capOverride[role] as boolean)
          : CAPABILITY_DEFAULTS[capability].includes(role);
    }
    matrix[capability] = row;
  }
  return matrix;
}

export function serializePermissions(matrix: PermissionMatrix): Record<string, unknown> {
  const capabilities: Record<string, Record<string, boolean>> = {};
  for (const [capability, row] of Object.entries(matrix)) {
    capabilities[capability] = Object.fromEntries(
      ALL_ROLES.map((role) => [role, row[role] === true]),
    );
  }
  return { capabilities };
}

export function emptyPermissions(): PermissionMatrix {
  return parsePermissions({});
}

// ---------------------------------------------------------------------------
// Integration — flat key/value rows over the payload's scalar leaves
// (configstore.validate_integration; v1 shape: {viewer: {baseUrl, name}}).
// Non-scalar values are preserved untouched in `rest`.
// ---------------------------------------------------------------------------

export interface IntegrationRow {
  path: string;
  value: string;
}

export interface IntegrationModel {
  rows: IntegrationRow[];
  rest: Record<string, unknown>;
}

export function parseIntegration(payload: Record<string, unknown>): IntegrationModel {
  const rows: IntegrationRow[] = [];
  const rest: Record<string, unknown> = {};
  for (const [section, value] of Object.entries(payload)) {
    if (isObj(value)) {
      const sectionRest: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(value)) {
        if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") {
          rows.push({ path: `${section}.${k}`, value: String(v) });
        } else {
          sectionRest[k] = v;
        }
      }
      if (Object.keys(sectionRest).length > 0) rest[section] = sectionRest;
    } else if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      rows.push({ path: section, value: String(value) });
    } else {
      rest[section] = value;
    }
  }
  return { rows, rest };
}

export function serializeIntegration(model: IntegrationModel): Record<string, unknown> {
  const payload: Record<string, unknown> = JSON.parse(JSON.stringify(model.rest)) as Record<
    string,
    unknown
  >;
  for (const row of model.rows) {
    const path = row.path.trim();
    if (!path) continue;
    const dot = path.indexOf(".");
    if (dot === -1) {
      payload[path] = row.value;
    } else {
      const section = path.slice(0, dot);
      const key = path.slice(dot + 1);
      const existing = isObj(payload[section]) ? payload[section] : {};
      payload[section] = { ...existing, [key]: row.value };
    }
  }
  return payload;
}

export function emptyIntegration(): IntegrationModel {
  return { rows: [{ path: "viewer.baseUrl", value: "" }], rest: {} };
}
