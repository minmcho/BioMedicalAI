// Organisation hierarchy client (Organisation & Registration
// Placement pass). The hierarchy is governed Studio configuration
// (terminology/locations) exposed read-only at GET /org-hierarchy as
// a four-level tree: region → location (facility) → department →
// unit. Types live here, not in api/types.ts, per the domain-client
// pattern (see api/teams.ts). Errors are FastAPI {detail} surfaced
// by ApiClient as ApiError — callers show err.detail inline.

import type { ApiClient } from "./client";
import type { PlacementView } from "./types";

export type { PlacementView };

export interface OrgUnit {
  code: string;
  display: string;
  bed_count: number;
}

export interface OrgDepartment {
  code: string;
  display: string;
  units: OrgUnit[];
}

export interface OrgLocation {
  code: string;
  display: string;
  departments: OrgDepartment[];
}

export interface OrgRegion {
  code: string;
  display: string;
  locations: OrgLocation[];
}

export interface OrgTree {
  version: number | null;
  regions: OrgRegion[];
}

/** The four placement codes a registration carries (flat fields). */
export interface PlacementCodes {
  region: string;
  location: string;
  department: string;
  unit: string;
}

export const EMPTY_PLACEMENT: PlacementCodes = {
  region: "",
  location: "",
  department: "",
  unit: "",
};

/** GET /org-hierarchy — the published region → location →
 *  department → unit tree. */
export async function getOrgTree(client: ApiClient): Promise<OrgTree> {
  return client.request<OrgTree>("/org-hierarchy");
}

// ---------------------------------------------------------------------------
// Cascading option getters: each level's options are the children
// of the selection above it. Unknown parent codes yield no options
// (the selects stay disabled with their hint, never silently empty).
// ---------------------------------------------------------------------------

export function locationsFor(tree: OrgTree | null, regionCode: string): OrgLocation[] {
  if (!tree || !regionCode) return [];
  return tree.regions.find((r) => r.code === regionCode)?.locations ?? [];
}

export function departmentsFor(
  tree: OrgTree | null,
  regionCode: string,
  locationCode: string,
): OrgDepartment[] {
  return (
    locationsFor(tree, regionCode).find((l) => l.code === locationCode)
      ?.departments ?? []
  );
}

export function unitsFor(
  tree: OrgTree | null,
  regionCode: string,
  locationCode: string,
  departmentCode: string,
): OrgUnit[] {
  return (
    departmentsFor(tree, regionCode, locationCode).find(
      (d) => d.code === departmentCode,
    )?.units ?? []
  );
}

/** Flatten the tree to one row per unit, carrying its full ancestor
 *  chain — used to resolve flat staff placement codes to displays. */
export interface FlatPlacement {
  region: OrgRegion;
  location: OrgLocation;
  department: OrgDepartment;
  unit: OrgUnit;
}

export function flattenPlacements(tree: OrgTree | null): FlatPlacement[] {
  if (!tree) return [];
  const out: FlatPlacement[] = [];
  for (const region of tree.regions) {
    for (const location of region.locations) {
      for (const department of location.departments) {
        for (const unit of department.units) {
          out.push({ region, location, department, unit });
        }
      }
    }
  }
  return out;
}

/** Display for any hierarchy code, searched across all four levels;
 *  undefined when the code is not in the published tree (callers
 *  fall back to the raw code). */
export function displayForCode(tree: OrgTree | null, code: string): string | undefined {
  if (!tree || !code) return undefined;
  for (const region of tree.regions) {
    if (region.code === code) return region.display;
    for (const location of region.locations) {
      if (location.code === code) return location.display;
      for (const department of location.departments) {
        if (department.code === code) return department.display;
        for (const unit of department.units) {
          if (unit.code === code) return unit.display;
        }
      }
    }
  }
  return undefined;
}

/** One-line placement from a server placement view (accounts,
 *  teams): "Unit · Department · Location · Region", skipping
 *  unplaced levels; empty string when nothing is placed. */
export function formatPlacementView(placement: PlacementView | null | undefined): string {
  if (!placement) return "";
  return [placement.unit, placement.department, placement.location, placement.region]
    .filter((p): p is { code: string; display?: string } => p != null)
    .map((p) => p.display ?? p.code)
    .join(" · ");
}
