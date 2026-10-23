import { useEffect, useRef, useState } from "react";
import { patientName } from "./api/display";
import type { Patient } from "./api/types";
import type { ApiClient } from "./api/client";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { SessionLockOverlay, useIdleLock } from "./auth/IdleLock";
import {
  DowntimeBanner,
  groupColorSlug,
  useRuntimeConfig,
} from "./auth/runtimeConfig";
import {
  applyJobProfile,
  canAccessWith,
  homePageFor,
  navItemsFor,
  resolveNavGroups,
  type NavItem,
  type PageKey,
} from "./auth/session";
import { BillingPage } from "./pages/BillingPage";
import { ChartPage } from "./pages/ChartPage";
import { EdBoardPage } from "./pages/EdBoardPage";
import { InboxPage } from "./pages/InboxPage";
import { LabPage } from "./pages/LabPage";
import { LoginPage } from "./pages/LoginPage";
import { PatientsPage } from "./pages/PatientsPage";
import { PharmacyPage } from "./pages/PharmacyPage";
import { PopHealthPage } from "./pages/PopHealthPage";
import { PortalPage } from "./pages/PortalPage";
import { RadiologyPage } from "./pages/RadiologyPage";
import { SchedulingPage } from "./pages/SchedulingPage";
import { StudioPage } from "./pages/StudioPage";
import { SurgeryBoardPage } from "./pages/SurgeryBoardPage";
import { BedBoardPage } from "./pages/BedBoardPage";
import { CommandCenterPage } from "./pages/CommandCenterPage";
import { ErOpsPage } from "./pages/ErOpsPage";
import { UnitBoardPage } from "./pages/UnitBoardPage";
import { ClinicDayPage } from "./pages/ClinicDayPage";
import { AftercarePage } from "./pages/AftercarePage";
import { SafetyPage } from "./pages/SafetyPage";
import { SupportPage } from "./pages/SupportPage";
import { HomePage } from "./pages/HomePage";
import { WorkforcePage } from "./pages/WorkforcePage";
import { TeamsPage } from "./pages/TeamsPage";
import { ReportsPage } from "./pages/ReportsPage";
import { ImportPage } from "./pages/ImportPage";
import { DevicesPage } from "./pages/DevicesPage";
import { DataPlatformPage } from "./pages/DataPlatformPage";
import { FederationPage } from "./pages/FederationPage";
import { ResearchPage } from "./pages/ResearchPage";
import { AnalyticsPage } from "./pages/AnalyticsPage";
import { DocumentsPage } from "./pages/DocumentsPage";
import { InventoryPage } from "./pages/InventoryPage";

/** Board pages get the wide layout (Home & Module UX pass): the
 *  980px content cap is lifted for these operational boards via the
 *  `content-wide` class on <main>. */
export const BOARD_PAGES: ReadonlySet<PageKey> = new Set<PageKey>([
  "command",
  "beds",
  "ed",
  "unit-board",
  "data-platform",
  "lab",
]);

/** Every module's built-in label/group, for breadcrumbs on pages the
 *  caller's resolved nav may not list (e.g. the chart). */
const ALL_NAV_ITEMS: NavItem[] = [
  ...navItemsFor("admin"),
  ...navItemsFor("patient"),
];
const NAV_ITEM_BY_KEY = new Map(ALL_NAV_ITEMS.map((i) => [i.key as string, i]));

/** Global patient search (Home & Module UX pass): staff only.
 *  Queries the SAME patient search the Patients page uses
 *  (client.search("Patient") + the same name/id/identifier match) at
 *  ≥2 characters; Enter or a click opens the chart in one action.
 *  Pressing "/" anywhere outside a form field focuses the input. */
export function GlobalPatientSearch({
  client,
  onOpenChart,
}: {
  client: ApiClient;
  onOpenChart: (patientId: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Patient[]>([]);
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/") return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName;
      if (
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        tag === "SELECT" ||
        target?.isContentEditable
      ) {
        return;
      }
      e.preventDefault();
      inputRef.current?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    const q = query.trim();
    if (q.length < 2) {
      setResults([]);
      setOpen(false);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const all = await client.search<Patient>("Patient");
          if (cancelled) return;
          const needle = q.toLowerCase();
          const matched = all
            .filter(
              (p) =>
                patientName(p).toLowerCase().includes(needle) ||
                (p.id ?? "").toLowerCase().includes(needle) ||
                (p.identifier ?? []).some((i) =>
                  (i.value ?? "").toLowerCase().includes(needle),
                ),
            )
            .slice(0, 8);
          setResults(matched);
          setOpen(true);
        } catch {
          if (!cancelled) {
            setResults([]);
            setOpen(false);
          }
        }
      })();
    }, 200);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [query, client]);

  const choose = (patient: Patient) => {
    if (!patient.id) return;
    setOpen(false);
    setQuery("");
    onOpenChart(patient.id);
  };

  return (
    <div
      className="topbar-search"
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
          setOpen(false);
        }
      }}
    >
      <label className="topbar-search-label" htmlFor="global-patient-search">
        Patient search
      </label>
      <input
        id="global-patient-search"
        ref={inputRef}
        type="search"
        role="combobox"
        aria-expanded={open}
        aria-controls="global-patient-search-results"
        aria-label="Search patients"
        placeholder="Search patients — name, MRN or id ( / )"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onFocus={() => {
          if (results.length > 0) setOpen(true);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && open && results.length > 0) {
            e.preventDefault();
            choose(results[0]);
          }
          if (e.key === "Escape") setOpen(false);
        }}
      />
      {open && (
        <ul
          className="topbar-search-results"
          id="global-patient-search-results"
          role="listbox"
          aria-label="Patient search results"
        >
          {results.length === 0 ? (
            <li className="topbar-search-empty" role="option" aria-selected="false">
              No patients match “{query.trim()}”.
            </li>
          ) : (
            results.map((p) => {
              const mrn = p.identifier?.find((i) => i.value)?.value;
              return (
                <li key={p.id ?? patientName(p)} role="option" aria-selected="false">
                  <button
                    type="button"
                    className="topbar-search-result"
                    onClick={() => choose(p)}
                  >
                    <span className="patient-name">{patientName(p)}</span>
                    <span className="patient-meta">{mrn ?? p.id}</span>
                  </button>
                </li>
              );
            })
          )}
        </ul>
      )}
    </div>
  );
}

/** The slim sticky top bar (Home & Module UX pass) — the sidebar's
 *  replacement as the primary navigation surface: brand, Home,
 *  global patient search (staff), identity + sign out. Modules are
 *  reached from the Home card menu (tier 1) and breadcrumbs. */
export function TopBar({
  orgName,
  username,
  role,
  isPatient,
  client,
  onHome,
  onOpenChart,
  onSignOut,
}: {
  orgName: string;
  username: string;
  role: string;
  isPatient: boolean;
  client: ApiClient;
  onHome: () => void;
  onOpenChart: (patientId: string) => void;
  onSignOut: () => void;
}) {
  return (
    <header className="topbar">
      {/* Rich Design Elevation (§8.4): brand block — accent tile
          with the org initial + the org name in display weight. */}
      <span className="topbar-brand">
        <span className="topbar-brand-tile" aria-hidden="true">
          {orgName.charAt(0)}
        </span>
        <span className="brand">{orgName}</span>
      </span>
      <button
        type="button"
        className="topbar-home"
        onClick={onHome}
        aria-label="Go to Home"
      >
        Home
      </button>
      {!isPatient && (
        <GlobalPatientSearch client={client} onOpenChart={onOpenChart} />
      )}
      <span className="topbar-user">
        <span className="topbar-username">{username}</span>
        <span className="badge">{role}</span>
        <button type="button" className="topbar-signout" onClick={onSignOut}>
          Sign out
        </button>
      </span>
    </header>
  );
}

/** Breadcrumb bar under the top bar: Home › {group} › {module label};
 *  the chart reads Home › Patients › {patient id}. */
export function BreadcrumbBar({
  page,
  chartPatientId,
  items,
  onHome,
  onNavigate,
}: {
  page: PageKey;
  chartPatientId: string | null;
  items: NavItem[];
  onHome: () => void;
  onNavigate: (page: PageKey) => void;
}) {
  const crumbs: Array<{
    label: string;
    page?: PageKey;
    current?: boolean;
    group?: boolean;
  }> = [];
  // Rich Design Elevation: the bar carries the current module's
  // group identity (data-group) and the group crumb gets a colour
  // dot — the group NAME remains the text carrier.
  let currentGroup: string | undefined;
  if (page === "home") {
    crumbs.push({ label: "Home", current: true });
  } else if (page === "chart") {
    const patientsItem =
      items.find((i) => i.key === "patients") ?? NAV_ITEM_BY_KEY.get("patients");
    if (patientsItem?.group) {
      crumbs.push({ label: patientsItem.group, group: true });
      currentGroup = patientsItem.group;
    }
    crumbs.push({ label: "Patients", page: "patients" });
    crumbs.push({ label: chartPatientId ?? "Chart", current: true });
  } else {
    const item =
      items.find((i) => i.key === page) ?? NAV_ITEM_BY_KEY.get(page as string);
    if (item?.group) {
      crumbs.push({ label: item.group, group: true });
      currentGroup = item.group;
    }
    crumbs.push({ label: item?.label ?? page, current: true });
  }
  return (
    <nav
      className="breadcrumb-bar"
      aria-label="Breadcrumb"
      data-group={currentGroup ?? undefined}
    >
      <ol className="breadcrumb-list">
        <li className="breadcrumb-item">
          {page === "home" ? (
            <span aria-current="page">Home</span>
          ) : (
            <button type="button" className="breadcrumb-link" onClick={onHome}>
              Home
            </button>
          )}
        </li>
        {crumbs
          .filter((c) => !(page === "home" && c.label === "Home"))
          .map((crumb, i) => (
            <li className="breadcrumb-item" key={`${crumb.label}-${i}`}>
              <span className="breadcrumb-sep" aria-hidden="true">
                ›
              </span>
              {crumb.current ? (
                <span aria-current="page">{crumb.label}</span>
              ) : crumb.page ? (
                <button
                  type="button"
                  className="breadcrumb-link"
                  onClick={() => onNavigate(crumb.page as PageKey)}
                >
                  {crumb.label}
                </button>
              ) : crumb.group ? (
                <span className="breadcrumb-group">{crumb.label}</span>
              ) : (
                <span>{crumb.label}</span>
              )}
            </li>
          ))}
      </ol>
    </nav>
  );
}

function Shell() {
  const { session, client, signOut } = useAuth();
  const runtime = useRuntimeConfig();
  // Phase 17 (BD-54 / CMP-33): idle-lock enforced from the published
  // session-security config. The lock is UI-only inside the grace
  // window (the server session is untouched); past grace the hook
  // fires signOut. Tap-and-go re-entry re-verifies the server session
  // via GET /security/session before unlocking.
  const idleLock = useIdleLock(runtime.sessionSecurity, session !== null, signOut);
  const handleReenter = async () => {
    try {
      await client.request("/security/session");
      idleLock.reenter();
    } catch {
      signOut();
    }
  };
  const [page, setPage] = useState<PageKey>("home");
  // Depth pass (Workstream 1): deep-link section for pages that expose
  // named sub-views (LabPage / PharmacyPage section contract). With
  // the sidebar retired (Home & Module UX pass), sections are the
  // module's own tier-3 tabs; this state still carries a section when
  // a page's own tab strip reports one.
  const [pageSection, setPageSection] = useState<{ page: PageKey; section: string } | null>(null);
  const [chartPatientId, setChartPatientId] = useState<string | null>(null);
  // Front-desk flow: a patient registered on the Patients page is
  // handed to the Clinic Day booking form (register → book).
  const [bookingPatient, setBookingPatient] = useState("");
  // Home & Module UX pass (job profiles): the caller's job role from
  // the staff directory, resolved by username like the home
  // dashboard's audience resolution. Unknown (no directory entry,
  // fetch failure, patient) = null = the profile filter is a no-op.
  const [jobRole, setJobRole] = useState<string | null>(null);

  useEffect(() => {
    if (!session || session.role === "patient") {
      setJobRole(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const staff = await client.listStaff();
        if (cancelled) return;
        const me = staff.find((s) => s.username === session.username);
        setJobRole(me?.job_role ?? null);
      } catch {
        if (!cancelled) setJobRole(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, session]);

  // Land each role on its home page and never sit on a page the role
  // cannot access (route guarding) — access comes from the published
  // permissions matrix merged over the defaults, so a site override
  // moves the guard and the nav together.
  useEffect(() => {
    if (session && !canAccessWith(session.role, page, runtime.permissions)) {
      setPage(homePageFor(session.role));
    }
  }, [session, page, runtime.permissions]);

  // The published branding drives the shell's CSS variables: accent
  // plus the Home & Module UX theme (density → spacing multiplier,
  // corner → radius, statusColors → status vars, fontScale → base
  // font-size). All values were validated by parseBrandingTheme.
  useEffect(() => {
    const root = document.documentElement;
    const theme = runtime.branding.theme;
    root.style.setProperty("--accent", runtime.branding.primaryColor);
    root.style.setProperty(
      "--density-scale",
      theme.density === "compact" ? "0.8" : "1",
    );
    root.style.setProperty(
      "--radius",
      theme.corner === "sharp" ? "3px" : "10px",
    );
    root.style.setProperty("--status-ok", theme.statusColors.ok);
    root.style.setProperty("--status-warn", theme.statusColors.warn);
    root.style.setProperty("--status-bad", theme.statusColors.bad);
    root.style.setProperty("--status-info", theme.statusColors.info);
    // Rich Design Elevation: the status TINTS now follow the
    // published status colours (derived at 8% via color-mix) —
    // closing the gap where -bg vars stayed frozen to the default
    // palette under a custom theme. And the group identity vars are
    // set from the effective groupColors map (published value or
    // built-in default per group), so [data-group] surfaces pick
    // up a site's recolouring with no code change.
    const tint = (hex: string) => `color-mix(in srgb, ${hex} 8%, white)`;
    root.style.setProperty("--status-ok-bg", tint(theme.statusColors.ok));
    root.style.setProperty("--status-warn-bg", tint(theme.statusColors.warn));
    root.style.setProperty("--status-bad-bg", tint(theme.statusColors.bad));
    root.style.setProperty("--status-info-bg", tint(theme.statusColors.info));
    for (const [label, color] of Object.entries(theme.groupColors)) {
      root.style.setProperty(`--group-${groupColorSlug(label)}`, color);
    }
    root.style.fontSize = theme.fontScale === "large" ? "112.5%" : "";
  }, [runtime.branding]);

  if (!session) return <LoginPage />;

  const navGroups = resolveNavGroups(session.role, runtime.navPayload, runtime.permissions);
  // Job profiles: role/permissions resolution ∩ profile when the
  // caller's job role is known; never empties the menu (session.ts).
  const effectiveGroups = applyJobProfile(navGroups, runtime.jobProfiles, jobRole, session.role);
  const flatItems = effectiveGroups.flatMap((g) => g.items);
  const openChart = (id: string) => {
    setChartPatientId(id);
    setPage("chart");
  };
  const goHome = () => setPage(homePageFor(session.role));

  return (
    <div className="layout">
      <TopBar
        orgName={runtime.branding.orgName}
        username={session.username}
        role={session.role}
        isPatient={session.role === "patient"}
        client={client}
        onHome={goHome}
        onOpenChart={openChart}
        onSignOut={signOut}
      />
      <BreadcrumbBar
        page={page}
        chartPatientId={chartPatientId}
        items={flatItems}
        onHome={goHome}
        onNavigate={(target) => setPage(target)}
      />
      <main className={BOARD_PAGES.has(page) ? "content content-wide" : "content"}>
        <DowntimeBanner />
        {idleLock.locked && (
          <SessionLockOverlay
            phase={idleLock.phase}
            username={session.username}
            onReenter={() => void handleReenter()}
            onSignOut={signOut}
          />
        )}
        {page === "home" && (
          <HomePage
            client={client}
            navGroups={effectiveGroups}
            onNavigate={(target) => setPage(target)}
            onOpenChart={openChart}
            fallback={
              <PatientsPage
                client={client}
                role={session.role}
                onOpenChart={openChart}
                onRegistered={(result) => {
                  setBookingPatient(result.id ?? "");
                  setPage("clinic");
                }}
              />
            }
          />
        )}
        {page === "patients" && (
          <PatientsPage
            client={client}
            role={session.role}
            onOpenChart={openChart}
            onRegistered={(result) => {
              setBookingPatient(result.id ?? "");
              setPage("clinic");
            }}
          />
        )}
        {page === "chart" &&
          (chartPatientId ? (
            <>
              <button type="button" className="linklike" onClick={() => setPage("patients")}>
                ← Back to patients
              </button>
              <ChartPage client={client} patientId={chartPatientId} role={session.role} />
            </>
          ) : (
            <PatientsPage
              client={client}
              role={session.role}
              onOpenChart={openChart}
            />
          ))}
        {page === "inbox" && (
          <InboxPage
            client={client}
            role={session.role}
            onNavigate={(target) => setPage(target as PageKey)}
            onOpenChart={openChart}
          />
        )}
        {page === "scheduling" && <SchedulingPage client={client} role={session.role} />}
        {page === "lab" && (
          <LabPage
            client={client}
            role={session.role}
            section={pageSection && pageSection.page === "lab" ? pageSection.section : undefined}
            onSectionChange={(s) => setPageSection({ page: "lab", section: s })}
          />
        )}
        {page === "pophealth" && <PopHealthPage client={client} role={session.role} />}
        {page === "billing" && <BillingPage client={client} role={session.role} />}
        {page === "pharmacy" && (
          <PharmacyPage
            client={client}
            role={session.role}
            section={pageSection && pageSection.page === "pharmacy" ? pageSection.section : undefined}
            onSectionChange={(s) => setPageSection({ page: "pharmacy", section: s })}
          />
        )}
        {page === "ed" && <EdBoardPage client={client} role={session.role} />}
        {page === "radiology" && <RadiologyPage client={client} role={session.role} />}
        {page === "surgery" && <SurgeryBoardPage client={client} role={session.role} />}
        {page === "beds" && <BedBoardPage client={client} role={session.role} />}
        {page === "command" && (
          <CommandCenterPage
            client={client}
            role={session.role}
            section={pageSection && pageSection.page === "command" ? pageSection.section : undefined}
            onSectionChange={(s) => setPageSection({ page: "command", section: s })}
          />
        )}
        {page === "er-ops" && <ErOpsPage client={client} role={session.role} />}
        {page === "unit-board" && <UnitBoardPage client={client} role={session.role} />}
        {page === "clinic" && (
          <ClinicDayPage
            client={client}
            role={session.role}
            initialPatient={bookingPatient || undefined}
          />
        )}
        {page === "aftercare" && <AftercarePage client={client} role={session.role} />}
        {page === "safety" && <SafetyPage client={client} role={session.role} />}
        {page === "support" && (
          <SupportPage
            client={client}
            role={session.role}
            section={pageSection && pageSection.page === "support" ? pageSection.section : undefined}
            onSectionChange={(s) => setPageSection({ page: "support", section: s })}
          />
        )}
        {page === "inventory" && <InventoryPage client={client} role={session.role} />}
        {page === "workforce" && <WorkforcePage client={client} role={session.role} />}
        {page === "teams" && <TeamsPage client={client} role={session.role} />}
        {page === "reports" && <ReportsPage client={client} role={session.role} />}
        {page === "imports" && <ImportPage client={client} role={session.role} />}
        {page === "devices" && (
          <DevicesPage
            client={client}
            role={session.role}
            section={pageSection && pageSection.page === "devices" ? pageSection.section : undefined}
          />
        )}
        {page === "data-platform" && <DataPlatformPage client={client} role={session.role} />}
        {page === "federation" && <FederationPage client={client} />}
        {page === "research" && <ResearchPage client={client} />}
        {page === "analytics" && (
          <AnalyticsPage
            client={client}
            onOpenChart={(id) => {
              setChartPatientId(id);
              setPage("chart");
            }}
          />
        )}
        {page === "documents" && <DocumentsPage client={client} />}
        {page === "studio" && <StudioPage client={client} />}
        {page === "portal" && <PortalPage client={client} />}
      </main>
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Shell />
    </AuthProvider>
  );
}
