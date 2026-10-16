import { describe, expect, it } from "vitest";
import { canAccess, homePageFor, navItemsFor } from "./session";
import type { Role } from "../api/types";

const ALL_ROLES: Role[] = ["admin", "configurator", "clinician", "viewer", "patient"];

describe("role-based nav", () => {
  it("patient sees the portal plus their own claims (billing), nothing staff-side", () => {
    expect(navItemsFor("patient").map((i) => i.key)).toEqual(["billing", "portal"]);
    expect(homePageFor("patient")).toBe("portal");
    expect(canAccess("patient", "billing")).toBe(true);
    expect(canAccess("patient", "pharmacy")).toBe(false);
    expect(canAccess("patient", "patients")).toBe(false);
  });

  it("clinician sees patients, inbox, scheduling, lab, population health, billing, pharmacy and the specialty boards — but not Studio", () => {
    const keys = navItemsFor("clinician").map((i) => i.key);
    expect(keys).toEqual([
      "patients", "inbox", "scheduling", "lab", "pophealth", "billing", "pharmacy",
      "ed", "radiology", "surgery",
    ]);
    expect(canAccess("clinician", "studio")).toBe(false);
    expect(canAccess("clinician", "pophealth")).toBe(true);
    expect(canAccess("clinician", "billing")).toBe(true);
  });

  it("admin sees everything staff-side including Studio and population health", () => {
    expect(navItemsFor("admin").map((i) => i.key)).toEqual([
      "patients",
      "inbox",
      "scheduling",
      "lab",
      "pophealth",
      "billing",
      "pharmacy",
      "ed",
      "radiology",
      "surgery",
      "studio",
    ]);
    expect(homePageFor("admin")).toBe("patients");
  });

  it("configurator sees the staff pages + Studio, but not population health (admin/clinician server-side)", () => {
    expect(navItemsFor("configurator").map((i) => i.key)).toEqual([
      "patients",
      "inbox",
      "scheduling",
      "lab",
      "billing",
      "pharmacy",
      "ed",
      "radiology",
      "surgery",
      "studio",
    ]);
    expect(canAccess("configurator", "pophealth")).toBe(false);
    expect(homePageFor("configurator")).toBe("patients");
  });

  it("viewer is read-only: patients + lab + pharmacy + the specialty boards, no inbox/scheduling/studio/portal/billing", () => {
    expect(navItemsFor("viewer").map((i) => i.key)).toEqual([
      "patients", "lab", "pharmacy", "ed", "radiology", "surgery",
    ]);
    expect(canAccess("viewer", "ed")).toBe(true);
    expect(canAccess("viewer", "radiology")).toBe(true);
    expect(canAccess("viewer", "surgery")).toBe(true);
    expect(canAccess("patient", "ed")).toBe(false);
    expect(canAccess("patient", "radiology")).toBe(false);
    expect(canAccess("patient", "surgery")).toBe(false);
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
