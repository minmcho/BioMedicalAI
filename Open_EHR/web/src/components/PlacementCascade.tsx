import { ApiError } from "../api/client";
import {
  departmentsFor,
  locationsFor,
  unitsFor,
  type OrgTree,
  type PlacementCodes,
} from "../api/org";

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

interface Props {
  tree: OrgTree | null;
  treeError?: unknown;
  value: PlacementCodes;
  onChange: (next: PlacementCodes) => void;
  /** Prefix for aria labels, e.g. "Onboarding" → "Onboarding region". */
  labelPrefix: string;
  /** Required mode: labels carry * and placeholders say "Select…";
   *  filter mode: the empty option reads "All …". */
  required?: boolean;
}

/** Cascading placement selects (Organisation & Registration
 *  Placement pass): Region → Location → Department → Unit, each
 *  fed by GET /org-hierarchy and filtered by the selection above.
 *  A select is disabled until its parent is chosen, with the reason
 *  shown as a hint — never a silently empty select. Changing a
 *  parent clears its descendants. */
export function PlacementCascade({
  tree,
  treeError,
  value,
  onChange,
  labelPrefix,
  required = false,
}: Props) {
  const star = required ? " *" : "";
  const regions = tree?.regions ?? [];
  const locations = locationsFor(tree, value.region);
  const departments = departmentsFor(tree, value.region, value.location);
  const units = unitsFor(tree, value.region, value.location, value.department);

  const treeUnavailable = tree == null;
  const regionHint = treeError
    ? `Organisation hierarchy unavailable: ${errorText(treeError)}`
    : treeUnavailable
      ? "Loading organisation hierarchy…"
      : regions.length === 0
        ? "No regions in the published organisation hierarchy."
        : null;

  return (
    <>
      <label className="field">
        Region{star}
        <select
          aria-label={`${labelPrefix} region`}
          value={value.region}
          disabled={treeUnavailable || regions.length === 0}
          onChange={(e) =>
            onChange({ region: e.target.value, location: "", department: "", unit: "" })
          }
        >
          <option value="">{required ? "Select a region" : "All regions"}</option>
          {regions.map((r) => (
            <option key={r.code} value={r.code}>{r.display ?? r.code}</option>
          ))}
        </select>
        {regionHint && <span className="field-error">{regionHint}</span>}
      </label>
      <label className="field">
        Location{star}
        <select
          aria-label={`${labelPrefix} location`}
          value={value.location}
          disabled={!value.region || locations.length === 0}
          onChange={(e) =>
            onChange({ ...value, location: e.target.value, department: "", unit: "" })
          }
        >
          <option value="">{required ? "Select a location" : "All locations"}</option>
          {locations.map((l) => (
            <option key={l.code} value={l.code}>{l.display ?? l.code}</option>
          ))}
        </select>
        {!value.region ? (
          <span className="field-error">Select a region first</span>
        ) : locations.length === 0 ? (
          <span className="field-error">No locations under the selected region.</span>
        ) : null}
      </label>
      <label className="field">
        Department{star}
        <select
          aria-label={`${labelPrefix} department`}
          value={value.department}
          disabled={!value.location || departments.length === 0}
          onChange={(e) =>
            onChange({ ...value, department: e.target.value, unit: "" })
          }
        >
          <option value="">{required ? "Select a department" : "All departments"}</option>
          {departments.map((d) => (
            <option key={d.code} value={d.code}>{d.display ?? d.code}</option>
          ))}
        </select>
        {!value.location ? (
          <span className="field-error">Select a location first</span>
        ) : departments.length === 0 ? (
          <span className="field-error">No departments under the selected location.</span>
        ) : null}
      </label>
      <label className="field">
        Unit{star}
        <select
          aria-label={`${labelPrefix} unit`}
          value={value.unit}
          disabled={!value.department || units.length === 0}
          onChange={(e) => onChange({ ...value, unit: e.target.value })}
        >
          <option value="">{required ? "Select a unit" : "All units"}</option>
          {units.map((u) => (
            <option key={u.code} value={u.code}>{u.display ?? u.code}</option>
          ))}
        </select>
        {!value.department ? (
          <span className="field-error">Select a department first</span>
        ) : units.length === 0 ? (
          <span className="field-error">No units under the selected department.</span>
        ) : null}
      </label>
    </>
  );
}
