import type { ApiClient } from "../../api/client";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

/** Organisation tab — a READ MODEL of the real org structure
 *  (Studio parity row 30): the published locations tree
 *  (facilities → units → beds), the staff directory and user
 *  accounts, aggregated by /studio-ops/organisation. Nothing is
 *  edited here: edits stay in the owning governed surfaces (the
 *  locations draft, people and accounts), which this tab lists —
 *  linked, not duplicated. */

interface OrgUnit {
  name?: string;
  code?: string;
  display?: string;
  beds?: number | unknown[];
  bed_count?: number;
  beds_total?: number;
  bedCount?: number;
}

interface OrgFacility {
  name?: string;
  code?: string;
  display?: string;
  units?: OrgUnit[];
  children?: OrgUnit[];
}

interface OrganisationResponse {
  facilities?: unknown;
  totals?: Record<string, unknown>;
  staff?: unknown;
  accounts?: unknown;
  edit_surfaces?: unknown;
  notes?: unknown;
}

function asArray(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  if (
    value &&
    typeof value === "object" &&
    Array.isArray((value as { items?: unknown }).items)
  ) {
    return (value as { items: unknown[] }).items;
  }
  return [];
}

function countOf(value: unknown): number | null {
  if (typeof value === "number") return value;
  if (Array.isArray(value)) return value.length;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    if (typeof record.total === "number") return record.total;
    if (typeof record.count === "number") return record.count;
    if (Array.isArray(record.items)) return record.items.length;
  }
  return null;
}

function bedCountOf(unit: OrgUnit): number | null {
  if (typeof unit.beds === "number") return unit.beds;
  if (Array.isArray(unit.beds)) return unit.beds.length;
  if (typeof unit.bed_count === "number") return unit.bed_count;
  if (typeof unit.beds_total === "number") return unit.beds_total;
  if (typeof unit.bedCount === "number") return unit.bedCount;
  return null;
}

function nameOf(record: { name?: string; display?: string; code?: string }): string {
  return record.name || record.display || record.code || "—";
}

function surfaceLabel(surface: unknown): string {
  if (typeof surface === "string") return surface;
  if (surface && typeof surface === "object") {
    const record = surface as Record<string, unknown>;
    for (const key of ["label", "name", "surface", "title", "path"]) {
      if (typeof record[key] === "string") return record[key] as string;
    }
  }
  return String(surface);
}

export function OrganisationTab({ client }: { client: ApiClient }) {
  const org = useAsync<OrganisationResponse>(
    () => client.request<OrganisationResponse>("/studio-ops/organisation"),
    [client],
  );

  if (org.loading) return <Loading label="Loading organisation…" />;
  if (org.error) return <ErrorView error={org.error} />;
  if (!org.data) return <Empty label="No organisation data." />;

  const data = org.data;
  const facilities = asArray(data.facilities) as OrgFacility[];
  const unitsOf = (facility: OrgFacility): OrgUnit[] =>
    (facility.units ?? facility.children ?? []) as OrgUnit[];
  const unitsTotal = facilities.reduce((sum, f) => sum + unitsOf(f).length, 0);
  const bedsTotal = facilities.reduce(
    (sum, f) =>
      sum + unitsOf(f).reduce((s, u) => s + (bedCountOf(u) ?? 0), 0),
    0,
  );

  const totals = data.totals ?? {};
  const cards: Array<{ label: string; value: number | null }> = [
    {
      label: "Facilities",
      value: countOf(totals.facilities) ?? facilities.length,
    },
    { label: "Units", value: countOf(totals.units) ?? unitsTotal },
    { label: "Beds", value: countOf(totals.beds) ?? bedsTotal },
    {
      label: "Staff",
      value: countOf(totals.staff) ?? countOf(data.staff),
    },
    {
      label: "Accounts",
      value: countOf(totals.accounts) ?? countOf(data.accounts),
    },
  ];

  const editSurfaces = asArray(data.edit_surfaces);
  const notes = asArray(data.notes)
    .filter((n): n is string => typeof n === "string")
    .concat(typeof data.notes === "string" ? [data.notes] : []);

  return (
    <div>
      <p className="hint">
        The organisation as it actually is — aggregated from the published
        locations tree, the staff directory and user accounts. This is a read
        model: structure is edited in the owning surfaces below, never here.
      </p>

      <div className="studio-overview">
        {cards.map((card) => (
          <section key={card.label} className="card">
            <h3>{card.label}</h3>
            <p className="studio-org-name">{card.value ?? "—"}</p>
          </section>
        ))}
      </div>

      <h3>Facilities and units</h3>
      {facilities.length === 0 ? (
        <Empty label="No facilities in the published locations config." />
      ) : (
        <ul>
          {facilities.map((facility, fi) => (
            <li key={`${nameOf(facility)}-${fi}`}>
              <strong>{nameOf(facility)}</strong>
              {unitsOf(facility).length > 0 ? (
                <ul>
                  {unitsOf(facility).map((unit, ui) => {
                    const beds = bedCountOf(unit);
                    return (
                      <li key={`${nameOf(unit)}-${ui}`}>
                        {nameOf(unit)} —{" "}
                        {beds === null
                          ? "beds: —"
                          : `${beds} ${beds === 1 ? "bed" : "beds"}`}
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <span className="hint"> — no units</span>
              )}
            </li>
          ))}
        </ul>
      )}

      <h3>Where edits happen</h3>
      <p className="hint">
        Edits happen in the owning surfaces — this tab links them, it does not
        duplicate them.
      </p>
      {editSurfaces.length === 0 ? (
        <Empty label="No edit surfaces reported." />
      ) : (
        <ul>
          {editSurfaces.map((surface, i) => (
            <li key={i}>{surfaceLabel(surface)}</li>
          ))}
        </ul>
      )}
      {notes.map((note, i) => (
        <p key={i} className="hint">
          {note}
        </p>
      ))}
    </div>
  );
}
