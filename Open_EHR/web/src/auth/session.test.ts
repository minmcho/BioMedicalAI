import { describe, expect, it } from "vitest";
import {
  applyJobProfile,
  canAccess,
  homePageFor,
  NAV_GROUP_ORDER,
  navItemsFor,
  parseJobProfiles,
  resolveNavGroups,
} from "./session";
import { parsePermissions } from "../pages/studio/models";
import type { Role } from "../api/types";

const ALL_ROLES: Role[] = ["admin", "configurator", "clinician", "viewer", "patient"];
const DEFAULT_MATRIX = parsePermissions({});

// Home & Module UX pass (NAV REGROUP): the built-in NAV is in the
// ledger's group order — Patient Care, Access & Revenue, Clinical,
// Diagnostics & Pharmacy, Operations, Safety & Support, People &
// Teams, Insights, Data Platform, System — so per-role key lists
// below follow that order (module membership per role is unchanged).

describe("role-based nav", () => {
  it("patient sees the portal plus their own claims (billing), nothing staff-side", () => {
    expect(navItemsFor("patient").map((i) => i.key)).toEqual(["portal", "billing"]);
    expect(homePageFor("patient")).toBe("portal");
    expect(canAccess("patient", "billing")).toBe(true);
    expect(canAccess("patient", "pharmacy")).toBe(false);
    expect(canAccess("patient", "patients")).toBe(false);
  });

  it("clinician sees patients, inbox, scheduling, lab, population health, billing, pharmacy and the specialty boards — but not Studio", () => {
    const keys = navItemsFor("clinician").map((i) => i.key);
    expect(keys).toEqual([
      "patients", "inbox",
      // Access & Revenue
      "scheduling", "billing",
      // Clinical
      "ed", "radiology", "surgery",
      // Diagnostics & Pharmacy
      "lab", "pharmacy",
      // Operations
      "beds", "command", "er-ops", "unit-board", "clinic", "aftercare",
      // Safety & Support (safety triage + support services)
      "safety", "support", "inventory",
      // People & Teams
      "workforce", "teams",
      // Insights
      "pophealth", "reports", "analytics",
      // Data Platform
      "devices", "data-platform", "federation", "research", "documents",
    ]);
    expect(canAccess("clinician", "studio")).toBe(false);
    expect(canAccess("clinician", "imports")).toBe(false);
    expect(homePageFor("clinician")).toBe("home");
    expect(canAccess("clinician", "pophealth")).toBe(true);
    expect(canAccess("clinician", "billing")).toBe(true);
  });

  it("admin sees everything staff-side including Studio and population health", () => {
    expect(navItemsFor("admin").map((i) => i.key)).toEqual([
      "patients", "inbox",
      "scheduling", "billing",
      "ed", "radiology", "surgery",
      "lab", "pharmacy",
      "beds", "command", "er-ops", "unit-board", "clinic", "aftercare",
      "safety", "support", "inventory",
      "workforce", "teams",
      "pophealth", "reports", "analytics",
      "devices", "data-platform", "federation", "research", "documents",
      // System
      "studio", "imports",
    ]);
    expect(homePageFor("admin")).toBe("home");
  });

  it("configurator sees the staff pages + Studio, but not population health (admin/clinician server-side)", () => {
    expect(navItemsFor("configurator").map((i) => i.key)).toEqual([
      "patients", "inbox",
      "scheduling", "billing",
      "ed", "radiology", "surgery",
      "lab", "pharmacy",
      "beds", "command", "er-ops", "unit-board", "clinic", "aftercare",
      "safety", "support", "inventory",
      "workforce", "teams",
      "reports", "analytics",
      "devices", "data-platform", "federation", "research", "documents",
      "studio", "imports",
    ]);
    expect(canAccess("configurator", "pophealth")).toBe(false);
    expect(homePageFor("configurator")).toBe("home");
  });

  it("viewer is read-only: patients + lab + pharmacy + the specialty boards, no inbox/scheduling/studio/portal/billing", () => {
    expect(navItemsFor("viewer").map((i) => i.key)).toEqual([
      "patients",
      // Clinical
      "ed", "radiology", "surgery",
      // Diagnostics & Pharmacy
      "lab", "pharmacy",
      // Operations: continuum reads include the viewer; the safety
      // triage page does not (safety.review is the safety team's).
      "beds", "command", "er-ops", "unit-board", "clinic", "aftercare",
      // Safety & Support: support + inventory reads include the viewer.
      "support", "inventory",
      // People & Teams: workforce + teams reads include the viewer.
      "workforce", "teams",
      // Insights: reports runs include the viewer.
      "reports",
      // Data Platform: devices/data-platform + federation/research
      // reads include the viewer; analytics + documents do not
      // (analytics.use / intake.work exclude the viewer by default).
      "devices", "data-platform", "federation", "research",
    ]);
    expect(canAccess("viewer", "ed")).toBe(true);
    expect(canAccess("viewer", "radiology")).toBe(true);
    expect(canAccess("viewer", "surgery")).toBe(true);
    expect(canAccess("patient", "ed")).toBe(false);
    expect(canAccess("patient", "radiology")).toBe(false);
    expect(canAccess("patient", "surgery")).toBe(false);
    // Phase 6: the bed board is staff-only, like the other boards.
    expect(canAccess("viewer", "beds")).toBe(true);
    expect(canAccess("patient", "beds")).toBe(false);
    expect(canAccess("viewer", "inbox")).toBe(false);
    expect(canAccess("viewer", "scheduling")).toBe(false);
    expect(canAccess("viewer", "studio")).toBe(false);
    expect(canAccess("viewer", "pophealth")).toBe(false);
    // billing is denied to the viewer server-side by default
    // (billing.view capability), so no nav entry either.
    expect(canAccess("viewer", "billing")).toBe(false);
    // lab access for the viewer is read-only: the /lab queues are
    // staff-readable server-side, patients are 403'd there.
    expect(canAccess("viewer", "lab")).toBe(true);
    expect(canAccess("patient", "lab")).toBe(false);
    expect(canAccess("viewer", "pharmacy")).toBe(true);
  });

  it("no staff role can open the patient portal page", () => {
    for (const role of ALL_ROLES.filter((r) => r !== "patient")) {
      expect(canAccess(role, "portal")).toBe(false);
    }
  });
});

describe("nav regroup (Home & Module UX pass)", () => {
  it("NAV_GROUP_ORDER is the ledger's 10-group order", () => {
    expect(NAV_GROUP_ORDER).toEqual([
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
    ]);
  });

  it("admin resolves all 10 groups with the ledger's membership and sizes (N-2: 2–6 per group)", () => {
    const groups = resolveNavGroups("admin", null, DEFAULT_MATRIX);
    expect(groups.map((g) => g.group)).toEqual([...NAV_GROUP_ORDER]);
    const byGroup = new Map(groups.map((g) => [g.group, g.items.map((i) => i.key)]));
    expect(byGroup.get("Patient Care")).toEqual(["patients", "inbox"]);
    expect(byGroup.get("Access & Revenue")).toEqual(["scheduling", "billing"]);
    expect(byGroup.get("Clinical")).toEqual(["ed", "radiology", "surgery"]);
    expect(byGroup.get("Diagnostics & Pharmacy")).toEqual(["lab", "pharmacy"]);
    expect(byGroup.get("Operations")).toEqual([
      "beds", "command", "er-ops", "unit-board", "clinic", "aftercare",
    ]);
    expect(byGroup.get("Safety & Support")).toEqual(["safety", "support", "inventory"]);
    expect(byGroup.get("People & Teams")).toEqual(["workforce", "teams"]);
    expect(byGroup.get("Insights")).toEqual(["pophealth", "reports", "analytics"]);
    expect(byGroup.get("Data Platform")).toEqual([
      "devices", "data-platform", "federation", "research", "documents",
    ]);
    expect(byGroup.get("System")).toEqual(["studio", "imports"]);
    for (const g of groups) {
      expect(g.items.length).toBeGreaterThanOrEqual(2);
      expect(g.items.length).toBeLessThanOrEqual(6);
    }
    const total = groups.reduce((n, g) => n + g.items.length, 0);
    expect(total).toBe(30); // every staff module; portal is patient-only
  });

  it("patient resolves portal + billing under their regrouped labels", () => {
    const groups = resolveNavGroups("patient", null, DEFAULT_MATRIX);
    expect(groups.map((g) => [g.group, g.items.map((i) => i.key)])).toEqual([
      ["Patient Care", ["portal"]],
      ["Access & Revenue", ["billing"]],
    ]);
  });

  it("every module carries a purpose line and an icon name (card-menu fallbacks)", () => {
    for (const role of ALL_ROLES) {
      for (const item of navItemsFor(role)) {
        expect(item.purpose, `${role}/${item.key} purpose`).toBeTruthy();
        expect(item.icon, `${role}/${item.key} icon`).toBeTruthy();
      }
    }
    const all = navItemsFor("admin").concat(navItemsFor("patient"));
    expect(new Set(all.map((i) => i.key)).size).toBe(31);
  });
});

describe("job profiles (Home & Module UX pass)", () => {
  const groups = () => resolveNavGroups("clinician", null, DEFAULT_MATRIX);

  it("parseJobProfiles keeps known modules and drops junk", () => {
    expect(
      parseJobProfiles({
        profiles: {
          nurse: { modules: ["beds", "unit-board", "teleport", 42] },
          broken: { modules: "nope" },
          empty: { modules: [] },
        },
      }),
    ).toEqual({ nurse: ["beds", "unit-board"] });
    expect(parseJobProfiles(null)).toEqual({});
    expect(parseJobProfiles({ profiles: "nope" })).toEqual({});
  });

  it("applyJobProfile intersects when the job role is known, preserving group order", () => {
    const filtered = applyJobProfile(
      groups(),
      { nurse: ["unit-board", "beds", "patients"] },
      "nurse",
    );
    expect(filtered.map((g) => [g.group, g.items.map((i) => i.key)])).toEqual([
      ["Patient Care", ["patients"]],
      ["Operations", ["beds", "unit-board"]],
    ]);
  });

  it("applyJobProfile never filters operator roles, even with a matching profile", () => {
    // The demo directory links the admin sign-in to an executive
    // staff record; without the exemption the executive profile
    // (6 modules) hid Laboratory, Pharmacy, Surgery and Studio
    // from the administrator's card Home (live defect).
    const adminGroups = resolveNavGroups("admin", null, DEFAULT_MATRIX);
    expect(
      applyJobProfile(adminGroups, { executive: ["command", "beds"] }, "executive", "admin"),
    ).toBe(adminGroups);
    const cfgGroups = resolveNavGroups("configurator", null, DEFAULT_MATRIX);
    expect(
      applyJobProfile(cfgGroups, { "department-head": ["command"] }, "department-head", "configurator"),
    ).toBe(cfgGroups);
    // A staff role with the same profile + job role still intersects.
    const staff = applyJobProfile(
      groups(),
      { nurse: ["unit-board", "beds", "patients"] },
      "nurse",
      "clinician",
    );
    expect(staff.map((g) => g.items.map((i) => i.key)).flat()).toEqual([
      "patients",
      "beds",
      "unit-board",
    ]);
  });

  it("applyJobProfile is a no-op for unknown roles and NEVER empties the menu", () => {
    const base = groups();
    expect(applyJobProfile(base, { nurse: ["beds"] }, null)).toBe(base);
    expect(applyJobProfile(base, { nurse: ["beds"] }, "executive")).toBe(base);
    expect(applyJobProfile(base, {}, "nurse")).toBe(base);
    // A profile naming only modules this role cannot see intersects
    // to nothing — the role menu stands instead of an empty Home.
    expect(applyJobProfile(base, { nurse: ["studio", "imports"] }, "nurse")).toBe(base);
  });
});
