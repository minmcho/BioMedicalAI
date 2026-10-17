import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import type { ApiClient } from "../api/client";
import { parsePermissions, type PermissionMatrix } from "../pages/studio/models";
import type { Session } from "./session";

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
}

export const DEFAULT_BRANDING: Branding = {
  orgName: "OpenCare",
  portalTitle: "My results",
  primaryColor: "#0b6e4f",
};

export interface RuntimeConfig {
  branding: Branding;
  /** Raw published navigation payload (null = use the built-in NAV). */
  navPayload: Record<string, unknown> | null;
  permissions: PermissionMatrix;
}

export const DEFAULT_RUNTIME_CONFIG: RuntimeConfig = {
  branding: DEFAULT_BRANDING,
  navPayload: null,
  permissions: parsePermissions({}),
};

const RuntimeConfigContext = createContext<RuntimeConfig>(DEFAULT_RUNTIME_CONFIG);

export function useRuntimeConfig(): RuntimeConfig {
  return useContext(RuntimeConfigContext);
}

const HEX_COLOR = /^#[0-9a-fA-F]{6}$/;

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
  };
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
