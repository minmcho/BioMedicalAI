import { describe, expect, it } from "vitest";
import { canAccess, homePageFor, navItemsFor } from "./session";
import type { Role } from "../api/types";

const ALL_ROLES: Role[] = ["admin", "configurator", "clinician", "viewer", "patient"];

describe("role-based nav", () => {
  it("patient sees only the portal", () => {
    expect(navItemsFor("patient").map((i) => i.key)).toEqual(["portal"]);
    expect(homePageFor("patient")).toBe("portal");
  });

  it("clinician sees patients, inbox and scheduling — but not Studio", () => {
    const keys = navItemsFor("clinician").map((i) => i.key);
    expect(keys).toEqual(["patients", "inbox", "scheduling"]);
    expect(canAccess("clinician", "studio")).toBe(false);
  });

  it("admin and configurator see everything staff-side including Studio", () => {
    for (const role of ["admin", "configurator"] as Role[]) {
      expect(navItemsFor(role).map((i) => i.key)).toEqual([
        "patients",
        "inbox",
        "scheduling",
        "studio",
      ]);
      expect(homePageFor(role)).toBe("patients");
    }
  });

  it("viewer is read-only: patients only, no inbox/scheduling/studio/portal", () => {
    expect(navItemsFor("viewer").map((i) => i.key)).toEqual(["patients"]);
    expect(canAccess("viewer", "inbox")).toBe(false);
    expect(canAccess("viewer", "scheduling")).toBe(false);
    expect(canAccess("viewer", "studio")).toBe(false);
  });

  it("no staff role can open the patient portal page", () => {
    for (const role of ALL_ROLES.filter((r) => r !== "patient")) {
      expect(canAccess(role, "portal")).toBe(false);
    }
  });
});
