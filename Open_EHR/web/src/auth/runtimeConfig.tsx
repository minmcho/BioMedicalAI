import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import type { ApiClient } from "../api/client";
import { parsePermissions, type PermissionMatrix } from "../pages/studio/models";
import {
  DEFAULT_SESSION_SECURITY,
  parseSessionSecurity,
  type SessionSecurityConfig,
} from "./sessionSecurity";
import {
  NAV_GROUP_ORDER,
  parseJobProfiles,
  type JobProfiles,
  type Session,
} from "./session";

// Runtime consumption of published Studio configuration (realignment).
// At sign-in the shell fetches the three published configs it renders
// from — navigation/main, branding/default, permissions/role-matrix —
// and every surface below reads them through useRuntimeConfig().
//
// Fallback discipline: each fetch is guarded independently and any
// failure (offline API, 403 for the patient role, 404 when a site has
// disabled a seed) leaves the built-in default in place, so the shell
// renders exactly as it did before the realignment. Pages rendered
// without the provider (unit tests) get the defaults too.

export interface Branding {
  orgName: string;
  portalTitle: string;
  primaryColor: string;
  /** Home & Module UX pass: optional visual theme (branding payload
   *  extension, ledger contract). All fields defaulted by
   *  parseBrandingTheme — a payload without a theme renders exactly
   *  the modernized default look. */
  theme: BrandingTheme;
}

export type ThemeDensity = "comfortable" | "compact";
export type ThemeCorner = "rounded" | "sharp";
export type ThemeFontScale = "standard" | "large";

export interface BrandingTheme {
  density: ThemeDensity;
  corner: ThemeCorner;
  statusColors: { ok: string; warn: string; bad: string; info: string };
  fontScale: ThemeFontScale;
  /** Rich Design Elevation pass: the EFFECTIVE group identity
   *  colours — canonical nav group label -> #RRGGBB. Always fully
   *  populated by parseBrandingTheme (published value per group,
   *  else the built-in default), so consumers never fall back. */
  groupColors: Record<string, string>;
}

/** Rich Design Elevation pass: the built-in group identity palette
 *  (DESIGN_SPEC §3.2 — every value measured WCAG AA on white and on
 *  its own 10% tint). Keys are the canonical nav group labels
 *  (NAV_GROUP_ORDER). A published theme.groupColors overrides per
 *  group; the defaults ARE the design, so a payload without the
 *  extension renders the full elevated look. */
export const DEFAULT_GROUP_COLORS: Record<string, string> = {
  "Patient Care": "#0b6e4f",
  "Access & Revenue": "#8a5a13",
  Clinical: "#1d4e7e",
  "Diagnostics & Pharmacy": "#5f4b9e",
  Operations: "#0e6b7a",
  "Safety & Support": "#8e2a4e",
  "People & Teams": "#4a4e9c",
  Insights: "#5c6618",
  "Data Platform": "#41586b",
  System: "#5d4037",
};

/** CSS custom-property slug for a group label ("Diagnostics &
 *  Pharmacy" -> "diagnostics-pharmacy"). The App theme effect sets
 *  --group-<slug> from the effective map; styles.css maps
 *  [data-group="<label>"] onto the same slug. */
export function groupColorSlug(label: string): string {
  return label
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export const DEFAULT_THEME: BrandingTheme = {
  density: "comfortable",
  corner: "rounded",
  statusColors: { ok: "#0b6e4f", warn: "#9a6700", bad: "#b42318", info: "#1d4e7e" },
  fontScale: "standard",
  groupColors: DEFAULT_GROUP_COLORS,
};

export const DEFAULT_BRANDING: Branding = {
  orgName: "OpenCare",
  portalTitle: "My results",
  primaryColor: "#0b6e4f",
  theme: DEFAULT_THEME,
};

export interface RuntimeConfig {
  branding: Branding;
  /** Raw published navigation payload (null = use the built-in NAV). */
  navPayload: Record<string, unknown> | null;
  permissions: PermissionMatrix;
  /** Phase 10 (Track K): the published downtime read-only flag
   *  (integration/downtime). Off unless a published config turns it
   *  on — any fetch failure leaves it off, matching the backend,
   *  whose guard also fails open for reads. */
  downtime: DowntimeState;
  /** Phase 17 (BD-54 / CMP-33): the published session-security policy
   *  (integration/session-security) — idle-lock + tap-and-go re-entry
   *  timers enforced by the shell (auth/IdleLock). Off unless a
   *  published config turns it on; any fetch failure leaves it off. */
  sessionSecurity: SessionSecurityConfig;
  /** Home & Module UX pass: the published job-profiles payload
   *  (navigation/job-profiles) parsed to job role -> module keys.
   *  Empty when nothing is published (or the fetch fails) — the shell
   *  then shows the unfiltered role menu. */
  jobProfiles: JobProfiles;
}

export interface DowntimeState {
  readOnly: boolean;
  message: string;
}

export const DEFAULT_DOWNTIME: DowntimeState = { readOnly: false, message: "" };

export const DEFAULT_RUNTIME_CONFIG: RuntimeConfig = {
  branding: DEFAULT_BRANDING,
  navPayload: null,
  permissions: parsePermissions({}),
  downtime: DEFAULT_DOWNTIME,
  sessionSecurity: DEFAULT_SESSION_SECURITY,
  jobProfiles: {},
};

export { parseSessionSecurity };

const RuntimeConfigContext = createContext<RuntimeConfig>(DEFAULT_RUNTIME_CONFIG);

export function useRuntimeConfig(): RuntimeConfig {
  return useContext(RuntimeConfigContext);
}

const HEX_COLOR = /^#[0-9a-fA-F]{6}$/;

function hexOr(value: unknown, fallback: string): string {
  return typeof value === "string" && HEX_COLOR.test(value) ? value : fallback;
}

/** Parse the optional branding `theme` object (Home & Module UX
 *  pass). Every field is validated independently: an unknown enum
 *  value or a malformed colour falls back to that field's default, so
 *  a bad published theme can never break the shell's appearance. */
export function parseBrandingTheme(raw: unknown): BrandingTheme {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return DEFAULT_THEME;
  }
  const t = raw as Record<string, unknown>;
  const sc =
    t.statusColors && typeof t.statusColors === "object" && !Array.isArray(t.statusColors)
      ? (t.statusColors as Record<string, unknown>)
      : {};
  // Group colours: read per canonical group label with the same
  // hexOr fallback discipline (unknown keys ignored, malformed or
  // missing values fall back to that group's built-in default).
  const gc =
    t.groupColors && typeof t.groupColors === "object" && !Array.isArray(t.groupColors)
      ? (t.groupColors as Record<string, unknown>)
      : {};
  const groupColors: Record<string, string> = {};
  for (const label of NAV_GROUP_ORDER) {
    if (typeof label === "string") {
      groupColors[label] = hexOr(gc[label], DEFAULT_GROUP_COLORS[label]);
    }
  }
  return {
    density: t.density === "compact" ? "compact" : "comfortable",
    corner: t.corner === "sharp" ? "sharp" : "rounded",
    statusColors: {
      ok: hexOr(sc.ok, DEFAULT_THEME.statusColors.ok),
      warn: hexOr(sc.warn, DEFAULT_THEME.statusColors.warn),
      bad: hexOr(sc.bad, DEFAULT_THEME.statusColors.bad),
      info: hexOr(sc.info, DEFAULT_THEME.statusColors.info),
    },
    fontScale: t.fontScale === "large" ? "large" : "standard",
    groupColors,
  };
}

export function parseBranding(payload: unknown): Branding {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return DEFAULT_BRANDING;
  }
  const p = payload as Record<string, unknown>;
  return {
    orgName:
      typeof p.orgName === "string" && p.orgName.trim()
        ? p.orgName
        : DEFAULT_BRANDING.orgName,
    portalTitle:
      typeof p.portalTitle === "string" && p.portalTitle.trim()
        ? p.portalTitle
        : DEFAULT_BRANDING.portalTitle,
    primaryColor:
      typeof p.primaryColor === "string" && HEX_COLOR.test(p.primaryColor)
        ? p.primaryColor
        : DEFAULT_BRANDING.primaryColor,
    theme: parseBrandingTheme(p.theme),
  };
}

export function parseDowntime(payload: unknown): DowntimeState {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return DEFAULT_DOWNTIME;
  }
  const p = payload as Record<string, unknown>;
  return {
    readOnly: p.readOnly === true,
    message: typeof p.message === "string" ? p.message : "",
  };
}

/** The read-only banner the shell renders while a downtime is in
 *  effect (Phase 10, Track K). Writes are refused server-side with a
 *  503; this banner is how staff learn why before they try. */
export function DowntimeBanner() {
  const { downtime } = useRuntimeConfig();
  if (!downtime.readOnly) return null;
  return (
    <div className="downtime-banner" role="alert">
      <strong>Read-only mode.</strong> The system is in a scheduled downtime
      {downtime.message ? <> — {downtime.message}</> : ""}. Records can be
      viewed; changes are paused and will be refused until the downtime ends.
    </div>
  );
}

export function RuntimeConfigProvider({
  client,
  session,
  children,
}: {
  client: ApiClient;
  session: Session | null;
  children: ReactNode;
}) {
  const [config, setConfig] = useState<RuntimeConfig>(DEFAULT_RUNTIME_CONFIG);
  const token = session?.token ?? null;

  useEffect(() => {
    if (!session) {
      setConfig(DEFAULT_RUNTIME_CONFIG);
      return;
    }
    let cancelled = false;
    void (async () => {
      const next: RuntimeConfig = {
        branding: DEFAULT_BRANDING,
        navPayload: null,
        permissions: parsePermissions({}),
        downtime: DEFAULT_DOWNTIME,
        sessionSecurity: DEFAULT_SESSION_SECURITY,
        jobProfiles: {},
      };
      try {
        const nav = await client.effectiveConfig("navigation", "main");
        if (nav?.payload && typeof nav.payload === "object") {
          next.navPayload = nav.payload;
        }
      } catch {
        // fall back to the built-in navigation
      }
      try {
        // Home & Module UX pass: job profiles ride the SAME governed
        // navigation kind (key "job-profiles") through this existing
        // runtime-config path. Guarded like every fetch here: a 404
        // (nothing published yet) leaves {} = no filtering, never a
        // wrong menu.
        const jobProfiles = await client.effectiveConfig(
          "navigation",
          "job-profiles",
        );
        next.jobProfiles = parseJobProfiles(jobProfiles?.payload);
      } catch {
        // no published job profiles: the role menu stands unfiltered
      }
      try {
        const branding = await client.effectiveConfig("branding", "default");
        next.branding = parseBranding(branding?.payload);
      } catch {
        // fall back to the built-in branding
      }
      try {
        const permissions = await client.effectiveConfig(
          "permissions",
          "role-matrix",
        );
        next.permissions = parsePermissions(permissions?.payload ?? {});
      } catch {
        // patients get a 403 here by design; defaults apply
      }
      try {
        const downtime = await client.effectiveConfig("integration", "downtime");
        next.downtime = parseDowntime(downtime?.payload);
      } catch {
        // no published downtime config (or no read access): mode stays off
      }
      try {
        const sessionSecurity = await client.effectiveConfig(
          "integration",
          "session-security",
        );
        next.sessionSecurity = parseSessionSecurity(sessionSecurity?.payload);
      } catch {
        // no published session-security config: idle lock stays off
      }
      if (!cancelled) setConfig(next);
    })();
    return () => {
      cancelled = true;
    };
  }, [client, session, token]);

  return (
    <RuntimeConfigContext.Provider value={config}>
      {children}
    </RuntimeConfigContext.Provider>
  );
}
