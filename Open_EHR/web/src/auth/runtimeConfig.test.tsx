import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { Role } from "../api/types";
import { parsePermissions } from "../pages/studio/models";
import {
  parseBranding,
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
    { module: "scheduling", label: "Scheduling", roles: ["admin", "configurator", "clinician"], order: 3, visible: true },
    { module: "lab", label: "Laboratory", roles: ["admin", "configurator", "clinician", "viewer"], order: 4, visible: true },
    { module: "pophealth", label: "Population health", roles: ["admin", "clinician"], order: 5, visible: true },
    { module: "billing", label: "Billing", roles: ["admin", "configurator", "clinician", "patient"], order: 6, visible: true },
    { module: "pharmacy", label: "Pharmacy", roles: ["admin", "configurator", "clinician", "viewer"], order: 7, visible: true },
    { module: "ed", label: "ED board", roles: ["admin", "configurator", "clinician", "viewer"], order: 8, visible: true },
    { module: "radiology", label: "Radiology", roles: ["admin", "configurator", "clinician", "viewer"], order: 9, visible: true },
    { module: "surgery", label: "Surgery board", roles: ["admin", "configurator", "clinician", "viewer"], order: 10, visible: true },
    { module: "studio", label: "System Studio", roles: ["admin", "configurator"], order: 11, visible: true },
    { module: "portal", label: "My results", roles: ["patient"], order: 12, visible: true },
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
      "billing", "pharmacy", "ed", "radiology", "surgery", "studio", "portal",
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
    ).toEqual({ orgName: "Mercy General", portalTitle: "Patient portal", primaryColor: "#123abc" });
  });

  it("falls back per-field on malformed values", () => {
    expect(parseBranding({ orgName: "  ", primaryColor: "blue" })).toEqual({
      orgName: "OpenCare",
      portalTitle: "My results",
      primaryColor: "#0b6e4f",
    });
    expect(parseBranding(null).orgName).toBe("OpenCare");
    expect(parseBranding("junk").primaryColor).toBe("#0b6e4f");
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
    });
  });
});
