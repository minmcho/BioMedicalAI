import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { Role } from "../api/types";
import { parsePermissions } from "../pages/studio/models";
import {
  DEFAULT_GROUP_COLORS,
  DEFAULT_THEME,
  DowntimeBanner,
  groupColorSlug,
  parseBranding,
  parseBrandingTheme,
  parseDowntime,
  RuntimeConfigProvider,
  useRuntimeConfig,
  type RuntimeConfig,
} from "./runtimeConfig";
import {
  canAccess,
  canAccessWith,
  navItemsFor,
  resolveNavItems,
  type Session,
} from "./session";

// Realignment: the shell consumes the published navigation / branding /
// permissions config at sign-in, with built-in defaults as the fallback.
// These tests pin the resolution rules (no network: the provider is fed
// a stub client at the same boundary the page tests use).

afterEach(cleanup);

const ALL_ROLES: Role[] = ["admin", "configurator", "clinician", "viewer", "patient"];
const DEFAULT_MATRIX = parsePermissions({});

const SEED_NAV = {
  items: [
    { module: "patients", label: "Patients", roles: ["admin", "configurator", "clinician", "viewer"], order: 1, visible: true },
    { module: "inbox", label: "Results inbox", roles: ["admin", "configurator", "clinician"], order: 2, visible: true },
    { module: "scheduling", label: "Scheduling", roles: ["admin", "configurator", "clinician"], order: 4, visible: true },
    { module: "lab", label: "Laboratory", roles: ["admin", "configurator", "clinician", "viewer"], order: 9, visible: true },
    { module: "pophealth", label: "Population health", roles: ["admin", "clinician"], order: 22, visible: true },
    { module: "billing", label: "Billing", roles: ["admin", "configurator", "clinician", "patient"], order: 5, visible: true },
    { module: "pharmacy", label: "Pharmacy", roles: ["admin", "configurator", "clinician", "viewer"], order: 10, visible: true },
    { module: "ed", label: "ED board", roles: ["admin", "configurator", "clinician", "viewer"], order: 6, visible: true },
    { module: "radiology", label: "Radiology", roles: ["admin", "configurator", "clinician", "viewer"], order: 7, visible: true },
    { module: "surgery", label: "Surgery board", roles: ["admin", "configurator", "clinician", "viewer"], order: 8, visible: true },
    { module: "beds", label: "Bed board", roles: ["admin", "configurator", "clinician", "viewer"], order: 11, visible: true },
    // Phase 12: the care-continuum operations modules, seeded before
    // studio/portal exactly as configstore._default_nav_payload does.
    { module: "command", label: "Command Center", roles: ["admin", "configurator", "clinician", "viewer"], order: 12, visible: true },
    { module: "er-ops", label: "ER operations", roles: ["admin", "configurator", "clinician", "viewer"], order: 13, visible: true },
    { module: "unit-board", label: "Unit board", roles: ["admin", "configurator", "clinician", "viewer"], order: 14, visible: true },
    { module: "clinic", label: "Clinic day", roles: ["admin", "configurator", "clinician", "viewer"], order: 15, visible: true },
    { module: "aftercare", label: "Aftercare", roles: ["admin", "configurator", "clinician", "viewer"], order: 16, visible: true },
    { module: "safety", label: "Safety", roles: ["admin", "configurator", "clinician", "viewer"], order: 17, visible: true },
    // Phase 15: support services, staff-wide (support.read), seeded
    // after safety exactly as configstore seeds it.
    { module: "support", label: "Support services", roles: ["admin", "configurator", "clinician", "viewer"], order: 18, visible: true },
    // Inventory & Materials pass: inventory (inventory.read,
    // staff-wide), seeded after support exactly as configstore seeds
    // it; every later module's order shifts by one.
    { module: "inventory", label: "Inventory & Materials", roles: ["admin", "configurator", "clinician", "viewer"], order: 19, visible: true },
    // Phase 13: workforce + reports are staff-wide (their read
    // capabilities include the viewer); the import centre is
    // admin/configurator only (data.import), exactly as
    // configstore._default_nav_payload seeds them.
    { module: "workforce", label: "Workforce", roles: ["admin", "configurator", "clinician", "viewer"], order: 20, visible: true },
    // Teams & Panels pass: teams (teams.read, staff-wide), seeded
    // beside workforce in Operations, exactly as the built-in NAV does.
    { module: "teams", label: "Teams", roles: ["admin", "configurator", "clinician", "viewer"], order: 21, visible: true },
    { module: "reports", label: "Reports", roles: ["admin", "configurator", "clinician", "viewer"], order: 23, visible: true },
    { module: "imports", label: "Import centre", roles: ["admin", "configurator"], order: 31, visible: true },
    // Phase 14: devices + data platform, staff-wide (devices.read),
    // seeded before studio exactly as configstore seeds them.
    { module: "devices", label: "Devices", roles: ["admin", "configurator", "clinician", "viewer"], order: 25, visible: true },
    { module: "data-platform", label: "Data platform", roles: ["admin", "configurator", "clinician", "viewer"], order: 26, visible: true },
    // Phase 16: federation + research (staff-wide reads), analytics +
    // documents (work capabilities, viewer excluded), seeded after
    // data-platform exactly as configstore seeds them.
    { module: "federation", label: "Federation", roles: ["admin", "configurator", "clinician", "viewer"], order: 27, visible: true },
    { module: "research", label: "Research", roles: ["admin", "configurator", "clinician", "viewer"], order: 28, visible: true },
    { module: "analytics", label: "Analytics", roles: ["admin", "configurator", "clinician"], order: 24, visible: true },
    { module: "documents", label: "Documents", roles: ["admin", "configurator", "clinician"], order: 29, visible: true },
    { module: "studio", label: "System Studio", roles: ["admin", "configurator"], order: 30, visible: true },
    { module: "portal", label: "My results", roles: ["patient"], order: 3, visible: true },
  ],
};

describe("resolveNavItems", () => {
  it("with no payload and the default matrix, reproduces the legacy nav for every role", () => {
    for (const role of ALL_ROLES) {
      expect(resolveNavItems(role, null, DEFAULT_MATRIX)).toEqual(navItemsFor(role));
      expect(resolveNavItems(role, undefined, DEFAULT_MATRIX)).toEqual(navItemsFor(role));
      expect(resolveNavItems(role, { items: "nope" }, DEFAULT_MATRIX)).toEqual(navItemsFor(role));
    }
  });

  it("with the seeded payload, reproduces the legacy nav for every role", () => {
    for (const role of ALL_ROLES) {
      expect(resolveNavItems(role, SEED_NAV, DEFAULT_MATRIX)).toEqual(navItemsFor(role));
    }
  });

  it("a published nav without a module removes it from the shell", () => {
    const payload = {
      items: SEED_NAV.items.filter((i) => i.module !== "lab"),
    };
    const keys = resolveNavItems("clinician", payload, DEFAULT_MATRIX).map((i) => i.key);
    expect(keys).not.toContain("lab");
    expect(keys).toContain("pharmacy");
  });

  it("honours order, labels, visibility and per-item roles; unknown modules are ignored", () => {
    const payload = {
      items: [
        { module: "pharmacy", label: "Dispensary", order: 1 },
        { module: "patients", label: "Patients", order: 2 },
        { module: "teleport", label: "Teleport", order: 3 },
        { module: "lab", label: "Laboratory", order: 4, visible: false },
        { module: "studio", label: "System Studio", order: 5, roles: ["admin"] },
      ],
    };
    const items = resolveNavItems("configurator", payload, DEFAULT_MATRIX);
    expect(items.map((i) => i.key)).toEqual(["pharmacy", "patients"]);
    expect(items[0].label).toBe("Dispensary");
    // The same payload for an admin includes Studio (roles + capability).
    expect(
      resolveNavItems("admin", payload, DEFAULT_MATRIX).map((i) => i.key),
    ).toEqual(["pharmacy", "patients", "studio"]);
  });
});

describe("canAccessWith", () => {
  it("matches the legacy canAccess for every role/page pair under the default matrix", () => {
    const pages = [
      "patients", "chart", "inbox", "scheduling", "lab", "pophealth",
      "billing", "pharmacy", "ed", "radiology", "surgery", "beds", "studio", "portal",
    ] as const;
    for (const role of ALL_ROLES) {
      for (const page of pages) {
        expect(canAccessWith(role, page, DEFAULT_MATRIX)).toBe(canAccess(role, page));
      }
    }
  });

  it("a published matrix flips access exactly like the server", () => {
    const matrix = parsePermissions({
      capabilities: {
        "lab.read": { viewer: false },
        "pophealth.view": { configurator: true },
        "studio.read": { clinician: true },
      },
    });
    expect(canAccessWith("viewer", "lab", matrix)).toBe(false);
    expect(canAccessWith("configurator", "pophealth", matrix)).toBe(true);
    // Studio page access follows studio.manage, NOT studio.read:
    // granting studio.read alone does not open the Studio page.
    expect(canAccessWith("clinician", "studio", matrix)).toBe(false);
    // Role-based pages are untouched by the matrix.
    expect(canAccessWith("patient", "billing", matrix)).toBe(true);
    expect(canAccessWith("patient", "portal", matrix)).toBe(true);
    expect(canAccessWith("patient", "lab", matrix)).toBe(false);
  });
});

describe("parseBranding", () => {
  it("applies a valid published branding payload", () => {
    expect(
      parseBranding({ orgName: "Mercy General", portalTitle: "Patient portal", primaryColor: "#123abc" }),
    ).toEqual({
      orgName: "Mercy General",
      portalTitle: "Patient portal",
      primaryColor: "#123abc",
      theme: DEFAULT_THEME,
    });
  });

  it("falls back per-field on malformed values", () => {
    expect(parseBranding({ orgName: "  ", primaryColor: "blue" })).toEqual({
      orgName: "OpenCare",
      portalTitle: "My results",
      primaryColor: "#0b6e4f",
      theme: DEFAULT_THEME,
    });
    expect(parseBranding(null).orgName).toBe("OpenCare");
    expect(parseBranding("junk").primaryColor).toBe("#0b6e4f");
  });

  it("carries a published theme through parseBranding", () => {
    const branding = parseBranding({
      orgName: "Mercy General",
      portalTitle: "Portal",
      primaryColor: "#123abc",
      theme: { density: "compact", corner: "sharp", fontScale: "large" },
    });
    expect(branding.theme.density).toBe("compact");
    expect(branding.theme.corner).toBe("sharp");
    expect(branding.theme.fontScale).toBe("large");
    expect(branding.theme.statusColors).toEqual(DEFAULT_THEME.statusColors);
  });
});

// Home & Module UX pass: the branding theme object (all optional) —
// defaults reproduce the modernized current look; every field is
// validated independently and bad values fall back per-field.
describe("parseBrandingTheme", () => {
  it("defaults when the theme is absent or not an object", () => {
    expect(parseBrandingTheme(undefined)).toEqual(DEFAULT_THEME);
    expect(parseBrandingTheme(null)).toEqual(DEFAULT_THEME);
    expect(parseBrandingTheme("compact")).toEqual(DEFAULT_THEME);
    expect(parseBrandingTheme({})).toEqual(DEFAULT_THEME);
  });

  it("applies valid overrides, including per-status colours", () => {
    expect(
      parseBrandingTheme({
        density: "compact",
        corner: "sharp",
        fontScale: "large",
        statusColors: { ok: "#00aa55", bad: "#cc0000" },
      }),
    ).toEqual({
      density: "compact",
      corner: "sharp",
      fontScale: "large",
      statusColors: {
        ok: "#00aa55",
        warn: DEFAULT_THEME.statusColors.warn,
        bad: "#cc0000",
        info: DEFAULT_THEME.statusColors.info,
      },
      groupColors: DEFAULT_THEME.groupColors,
    });
  });

  it("rejects bad enum values and malformed colours to defaults", () => {
    expect(
      parseBrandingTheme({
        density: "roomy",
        corner: "round",
        fontScale: "huge",
        statusColors: { ok: "green", warn: "#12345", bad: 42, info: "#1d4e7e" },
      }),
    ).toEqual({
      density: "comfortable",
      corner: "rounded",
      fontScale: "standard",
      statusColors: { ...DEFAULT_THEME.statusColors, info: "#1d4e7e" },
      groupColors: DEFAULT_THEME.groupColors,
    });
  });

  // Rich Design Elevation pass: theme.groupColors (optional,
  // partial) — per-group fallback to the built-in palette; unknown
  // keys ignored client-side; malformed values fall back per group.
  it("applies published group colours per group with default fallback", () => {
    const theme = parseBrandingTheme({
      groupColors: { Clinical: "#123456", "Patient Care": "#0B6E4F" },
    });
    expect(theme.groupColors.Clinical).toBe("#123456");
    expect(theme.groupColors["Patient Care"]).toBe("#0B6E4F");
    expect(theme.groupColors.System).toBe(DEFAULT_GROUP_COLORS.System);
    expect(Object.keys(theme.groupColors)).toHaveLength(10);
  });

  it("ignores unknown group keys and malformed group colours", () => {
    const theme = parseBrandingTheme({
      groupColors: { "Ward Nine": "#123456", Clinical: "blue", Insights: 42 },
    });
    expect(theme.groupColors).toEqual(DEFAULT_GROUP_COLORS);
    expect(
      parseBrandingTheme({ groupColors: ["#123456"] }).groupColors,
    ).toEqual(DEFAULT_GROUP_COLORS);
  });
});

describe("groupColorSlug", () => {
  it("slugs canonical group labels to their CSS var names", () => {
    expect(groupColorSlug("Patient Care")).toBe("patient-care");
    expect(groupColorSlug("Access & Revenue")).toBe("access-revenue");
    expect(groupColorSlug("Diagnostics & Pharmacy")).toBe(
      "diagnostics-pharmacy",
    );
    expect(groupColorSlug("Safety & Support")).toBe("safety-support");
    expect(groupColorSlug("Data Platform")).toBe("data-platform");
    expect(groupColorSlug("System")).toBe("system");
  });
});

describe("nav purpose/icon (Home & Module UX pass)", () => {
  it("a published payload's purpose/icon win; absent fields fall back to the built-ins", () => {
    const payload = {
      items: [
        { module: "lab", label: "Laboratory", order: 1, purpose: "Custom lab purpose", icon: "custom-flask" },
        { module: "patients", label: "Patients", order: 2 },
      ],
    };
    const items = resolveNavItems("clinician", payload, DEFAULT_MATRIX);
    const lab = items.find((i) => i.key === "lab");
    expect(lab?.purpose).toBe("Custom lab purpose");
    expect(lab?.icon).toBe("custom-flask");
    const patients = items.find((i) => i.key === "patients");
    expect(patients?.purpose).toBe("Find, register and open patient records");
    expect(patients?.icon).toBe("users");
  });
});

// ---- Provider ----

function Probe({ onConfig }: { onConfig: (c: RuntimeConfig) => void }) {
  const config = useRuntimeConfig();
  onConfig(config);
  return <span>{config.branding.orgName}</span>;
}

const SESSION: Session = { token: "clinician-token", role: "clinician", username: "dev-clinician" };

function stubClient(handlers: Record<string, unknown>): ApiClient {
  return handlers as unknown as ApiClient;
}

describe("RuntimeConfigProvider", () => {
  it("applies the published branding once fetched", async () => {
    const seen: RuntimeConfig[] = [];
    const client = stubClient({
      effectiveConfig: vi.fn(async (kind: string) => {
        if (kind === "branding") {
          return { payload: { orgName: "Mercy General", portalTitle: "Portal", primaryColor: "#123abc" } };
        }
        throw new Error("not published");
      }),
    });
    render(
      <RuntimeConfigProvider client={client} session={SESSION}>
        <Probe onConfig={(c) => seen.push(c)} />
      </RuntimeConfigProvider>,
    );
    expect(await screen.findByText("Mercy General")).toBeTruthy();
    const last = seen[seen.length - 1];
    expect(last.branding.primaryColor).toBe("#123abc");
    // The failed nav/permissions fetches fell back to the defaults.
    expect(last.navPayload).toBeNull();
    expect(last.permissions["lab.read"].viewer).toBe(true);
  });

  it("keeps the built-in defaults when every fetch fails (offline API)", async () => {
    const seen: RuntimeConfig[] = [];
    const client = stubClient({
      effectiveConfig: vi.fn(async () => {
        throw new Error("offline");
      }),
    });
    render(
      <RuntimeConfigProvider client={client} session={SESSION}>
        <Probe onConfig={(c) => seen.push(c)} />
      </RuntimeConfigProvider>,
    );
    await waitFor(() => expect(seen.length).toBeGreaterThan(0));
    expect(screen.getByText("OpenCare")).toBeTruthy();
    const last = seen[seen.length - 1];
    expect(last.branding).toEqual({
      orgName: "OpenCare",
      portalTitle: "My results",
      primaryColor: "#0b6e4f",
      theme: DEFAULT_THEME,
    });
    // The failed job-profiles fetch leaves the profiles empty (no
    // filtering) rather than guessing a menu.
    expect(last.jobProfiles).toEqual({});
  });

  it("exposes the published job profiles (navigation/job-profiles) parsed", async () => {
    const seen: RuntimeConfig[] = [];
    const client = stubClient({
      effectiveConfig: vi.fn(async (kind: string, key: string) => {
        if (kind === "navigation" && key === "job-profiles") {
          return {
            payload: {
              profiles: { nurse: { modules: ["beds", "unit-board", "teleport"] } },
            },
          };
        }
        throw new Error("not published");
      }),
    });
    render(
      <RuntimeConfigProvider client={client} session={SESSION}>
        <Probe onConfig={(c) => seen.push(c)} />
      </RuntimeConfigProvider>,
    );
    await waitFor(() =>
      expect(seen[seen.length - 1]?.jobProfiles).toEqual({
        nurse: ["beds", "unit-board"],
      }),
    );
  });
});

// Phase 10 (Track K): the downtime read-only flag is published config
// (integration/downtime); the shell fetches it with the same
// fallback discipline — any failure leaves the mode OFF.
describe("downtime (Phase 10)", () => {
  it("parseDowntime reads the published payload strictly", () => {
    expect(parseDowntime({ readOnly: true, message: "upgrade window" }))
      .toEqual({ readOnly: true, message: "upgrade window" });
    expect(parseDowntime({ readOnly: false })).toEqual({ readOnly: false, message: "" });
    expect(parseDowntime({ readOnly: "yes" }).readOnly).toBe(false);
    expect(parseDowntime(null)).toEqual({ readOnly: false, message: "" });
    expect(parseDowntime(undefined)).toEqual({ readOnly: false, message: "" });
  });

  it("provider exposes the published downtime and the banner renders it", async () => {
    const client = stubClient({
      effectiveConfig: vi.fn(async (kind: string, key: string) => {
        if (kind === "integration" && key === "downtime") {
          return { payload: { readOnly: true, message: "EHR upgrade 02:00–04:00" } };
        }
        throw new Error("not published");
      }),
    });
    render(
      <RuntimeConfigProvider client={client} session={SESSION}>
        <DowntimeBanner />
      </RuntimeConfigProvider>,
    );
    expect(await screen.findByText(/Read-only mode/)).toBeTruthy();
    expect(screen.getByText(/EHR upgrade 02:00–04:00/)).toBeTruthy();
  });

  it("no banner when downtime is off or the fetch fails", async () => {
    const client = stubClient({
      effectiveConfig: vi.fn(async () => {
        throw new Error("offline");
      }),
    });
    const { container } = render(
      <RuntimeConfigProvider client={client} session={SESSION}>
        <DowntimeBanner />
      </RuntimeConfigProvider>,
    );
    await waitFor(() => expect(container.querySelector(".downtime-banner")).toBeNull());
    expect(screen.queryByText(/Read-only mode/)).toBeNull();
  });
});
