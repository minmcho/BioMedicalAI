import type { ReactNode } from "react";
import type { ApiClient } from "../api/client";
import type {
  HomeCard,
  HomeDashboard,
  HomePatientItem,
  MyPatients,
  MyUnitPatients,
} from "../api/types";
import type { NavItem, PageKey } from "../auth/session";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { FrontDeskSteps } from "../components/PatientForms";
import { DashboardGrid } from "../components/DashboardView";
import { ModuleCard } from "../components/ModuleCard";
import { SkeletonCards } from "../components/Skeleton";
import { Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

export interface HomeNavGroup {
  group: string | null;
  items: NavItem[];
}

/** The card menu that leads Home (Home & Module UX pass): one card
 *  per module from the SAME governed resolution the shell uses
 *  (resolveNavGroups — published nav payload + permissions, regrouped
 *  labels, job-profile filter applied by the caller). A card carries
 *  its live indicator only when GET /home/cards returned a provider
 *  entry for that module; otherwise purpose + "Open" only. */
function CardMenu({
  groups,
  cards,
  onOpen,
}: {
  groups: HomeNavGroup[];
  cards: HomeCard[];
  onOpen: (key: PageKey) => void;
}) {
  const byModule = new Map(cards.map((c) => [c.module, c]));
  return (
    <section aria-label="Modules" className="home-cards">
      {groups.map(({ group, items }) => (
        <div
          key={group ?? "__ungrouped"}
          className="home-card-group"
          data-group={group ?? undefined}
        >
          {group !== null && (
            <h3 className="home-card-group-label">
              {/* Rich Design Elevation (§8.2): colour dot + hairline
                  rule — the group name stays the text carrier. */}
              <span className="home-group-dot" aria-hidden="true" />
              {group}
            </h3>
          )}
          <div className="module-card-grid">
            {items.map((item) => (
              <ModuleCard
                key={item.key}
                item={item}
                indicator={byModule.get(item.key as string)}
                onOpen={onOpen}
              />
            ))}
          </div>
        </div>
      ))}
    </section>
  );
}

/** A unit-first patient list: the work the signed-in person owns,
 *  before any aggregate widget. Readiness is text + badge, never
 *  colour alone. */
function PatientList({
  title,
  items,
  emptyLabel,
  onOpenChart,
}: {
  title: string;
  items: HomePatientItem[];
  emptyLabel: string;
  /** UX audit F-2: a row opens the patient's chart in one click —
   *  the unit-first home must start work, not just inform. */
  onOpenChart?: (patientId: string) => void;
}) {
  return (
    <section aria-label={title}>
      <h3>{title}</h3>
      {items.length === 0 ? (
        <p className="hint">{emptyLabel}</p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Patient</th>
              <th scope="col">Bed</th>
              <th scope="col">Attending</th>
              <th scope="col">Admitted</th>
              <th scope="col">Discharge readiness</th>
            </tr>
          </thead>
          <tbody>
            {items.map((i) => (
              <tr key={i.encounter_id}>
                <td>
                  {onOpenChart && i.patient_id ? (
                    <button
                      type="button"
                      className="linklike"
                      aria-label={`Open chart for ${i.patient_name ?? i.patient_id}`}
                      onClick={() => onOpenChart(i.patient_id as string)}
                    >
                      {i.patient_name ?? i.patient_ref ?? "—"}
                      {` (${i.patient_id})`}
                    </button>
                  ) : (
                    <>
                      {i.patient_name ?? i.patient_ref ?? "—"}
                      {i.patient_id ? ` (${i.patient_id})` : ""}
                    </>
                  )}
                </td>
                <td>{i.bed ?? "—"}</td>
                <td>{i.attending_name ?? "—"}</td>
                <td>
                  {i.admitted_at
                    ? new Date(i.admitted_at).toLocaleDateString()
                    : "—"}
                  {i.los_days != null ? ` · day ${i.los_days + 1}` : ""}
                </td>
                <td>
                  {i.discharge_ready ? (
                    <span className="badge">Ready for discharge</span>
                  ) : (
                    <span className="badge badge-warn">Not ready</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

/** Home — the role-aware landing. The server resolves the caller's
 *  audience and published dashboard; this pass leads the page with
 *  the caller's OWN work first (Phase 13 dashboards stay, below):
 *  front desk gets its workflow quick actions; a clinician gets My
 *  Patients (always — an empty list is information, not an error),
 *  then their unit's patients; nurse and unit-manager audiences get
 *  their unit's patients first. Callers with no unit and no
 *  attending list see the dashboard exactly as before. */
export function HomePage({
  client,
  fallback,
  onNavigate,
  onOpenChart,
  navGroups,
}: {
  client: ApiClient;
  fallback: ReactNode;
  onNavigate?: (page: PageKey) => void;
  onOpenChart?: (patientId: string) => void;
  /** Home & Module UX pass: resolved card-menu groups from the shell
   *  (resolveNavGroups + job-profile filter). Absent in legacy/test
   *  renders — the page then behaves exactly as before. */
  navGroups?: HomeNavGroup[];
}) {
  const home = useAsync<HomeDashboard>(
    () => client.homeDashboard(),
    [client],
  );
  const mine = useAsync<MyPatients>(() => client.myPatients(), [client]);
  const unit = useAsync<MyUnitPatients>(
    () => client.myUnitPatients(),
    [client],
  );
  // Live card indicators: a failed/absent /home/cards (endpoint not
  // yet deployed, patient role, offline) degrades to purpose-only
  // cards — never an error surface, never invented numbers.
  const cards = useAsync<HomeCard[]>(
    async () => {
      if (typeof client.homeCards !== "function") return [];
      try {
        return await client.homeCards();
      } catch {
        return [];
      }
    },
    [client],
  );

  const { branding } = useRuntimeConfig();

  // Rich Design Elevation (§8.3): the hero / summary strip — chrome,
  // not content, so it renders in every branch (including the
  // dashboard-fallback branch). It carries ONLY data this page
  // already knows: the org name from branding, today's date (long
  // form), the caller's audience label from the home payload, and
  // own-work chips from the my-patients / my-unit payloads this
  // page already loads. A count that is not in an already-loaded
  // payload produces no chip — nothing is invented, no new fetch.
  const audienceRaw =
    home.data && typeof home.data.audience === "string"
      ? home.data.audience
      : null;
  const audienceLabel = audienceRaw
    ? audienceRaw
        .replace(/-/g, " ")
        .replace(/^./, (c) => c.toUpperCase())
    : null;
  const heroChips: Array<{ label: string; value: number }> = [];
  if (mine.data) {
    heroChips.push({
      label: "My patients",
      value:
        typeof mine.data.total === "number"
          ? mine.data.total
          : mine.data.items.length,
    });
  }
  if (unit.data?.unit) {
    heroChips.push({
      label: unit.data.unit_display
        ? `My unit · ${unit.data.unit_display}`
        : "My unit census",
      value:
        typeof unit.data.total === "number"
          ? unit.data.total
          : unit.data.items.length,
    });
  }
  const hero = (
    <section className="home-hero" aria-label="Welcome">
      <p className="home-hero-greeting">{branding.orgName}</p>
      <p className="home-hero-meta">
        {new Date().toLocaleDateString(undefined, {
          weekday: "long",
          month: "long",
          day: "numeric",
          year: "numeric",
        })}
        {audienceLabel ? ` · ${audienceLabel}` : ""}
      </p>
      {heroChips.length > 0 && (
        <div className="home-hero-chips">
          {heroChips.map((chip) => (
            <span key={chip.label} className="home-hero-chip">
              {chip.label} <strong className="num">{chip.value}</strong>
            </span>
          ))}
        </div>
      )}
    </section>
  );

  const showCards =
    !!navGroups && navGroups.length > 0 && typeof onNavigate === "function";
  // P-1: Home carries exactly one <h2>. When the fallback (the
  // Patients page, which brings its own <h2>) renders below, the card
  // menu leads without adding a second one.
  const fallbackWillRender =
    (!!home.data && !home.data.dashboard) || !!home.error;
  const cardMenu = showCards ? (
    <>
      {!fallbackWillRender && <h2>Home</h2>}
      {home.loading ? (
        // §8.14: while Home resolves, the card grid's structure is
        // already known — mirror it with skeleton cards (the
        // skeleton carries the same status label for AT).
        <SkeletonCards
          count={navGroups.reduce((n, g) => n + g.items.length, 0)}
          label="Loading your home…"
        />
      ) : (
        <CardMenu
          groups={navGroups}
          cards={cards.data ?? []}
          onOpen={(key) => onNavigate?.(key)}
        />
      )}
    </>
  ) : null;

  if (home.loading) {
    return (
      <>
        {hero}
        {cardMenu}
        {!showCards && <Loading label="Loading your home…" />}
      </>
    );
  }
  if (home.error || !home.data) {
    return (
      <>
        {hero}
        {cardMenu}
        {fallback}
      </>
    );
  }

  const audience = home.data.audience;
  const sections: ReactNode[] = [];

  if (audience === "front-desk") {
    sections.push(
      <section key="frontdesk-actions" aria-label="Front desk quick actions">
        <h3>Front desk — start here</h3>
        <FrontDeskSteps current="register" />
        <div className="row-actions">
          <button type="button" onClick={() => onNavigate?.("patients")}>
            Register a new patient
          </button>
          <button type="button" onClick={() => onNavigate?.("clinic")}>
            Book an appointment
          </button>
        </div>
        <p className="hint">
          Register first, then book — the booking form opens with the
          new patient filled in. When the patient arrives, check-in
          (mark arrived) is under Arrivals on the Clinic Day board,
          and the clinical team rooms them from there.
        </p>
      </section>,
    );
  }

  if (audience === "clinician" && mine.data) {
    sections.push(
      <PatientList
        key="my-patients"
        title="My patients — I am the attending"
        onOpenChart={onOpenChart}
        items={mine.data.items}
        emptyLabel="You are not the attending of record on any current admission."
      />,
    );
  }

  const unitFirst = audience === "nurse" || audience === "unit-manager";
  if ((unitFirst || audience === "clinician") && unit.data?.unit) {
    sections.push(
      <PatientList
        key="my-unit"
        title={`My unit — ${unit.data.unit_display ?? unit.data.unit}`}
        onOpenChart={onOpenChart}
        items={unit.data.items}
        emptyLabel="No patients are currently admitted on your unit."
      />,
    );
  } else if (unitFirst && unit.data && !unit.data.unit) {
    // A nurse/manager whose directory entry names no unit: say so
    // plainly rather than silently skipping the section.
    sections.push(
      <p key="no-unit" className="hint">
        Your staff-directory entry names no unit, so there is no unit
        list to lead with — the house dashboard follows.
      </p>,
    );
  }

  return (
    <div>
      {hero}
      {cardMenu}
      {sections}
      {home.data.dashboard ? (
        <>
          <p className="hint">
            Your {audience ?? "staff"} dashboard — a published Studio
            config over live operational data. Compose or republish
            dashboards in System Studio → Dashboards.
          </p>
          <DashboardGrid
            client={client}
            config={home.data.dashboard}
            onOpenChart={onOpenChart}
          />
        </>
      ) : (
        fallback
      )}
    </div>
  );
}
