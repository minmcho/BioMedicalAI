import type {
  ConfigKind,
  InstallationStatus,
  StudioAction,
  StudioConfigObject,
} from "../../api/types";

// Shared Studio helpers (moved out of StudioPage.tsx when the visual
// module landed; StudioPage re-exports them so existing imports hold).

/** Next lifecycle action for an installation, following the server's
 *  state machine: installed_draft → approved → attached → enabled, and
 *  enabled → disabled. */
export function nextAction(status: InstallationStatus): StudioAction | null {
  switch (status) {
    case "installed_draft":
      return "approve";
    case "approved":
      return "attach";
    case "attached":
      return "enable";
    case "enabled":
      return "disable";
    case "disabled":
      return null;
  }
}

export const CONFIG_KINDS: ConfigKind[] = [
  "form",
  "workflow",
  "terminology",
  "navigation",
  "branding",
  "permissions",
  "cohort",
  "integration",
];

export interface ConfigGroup {
  kind: ConfigKind;
  key: string;
  versions: StudioConfigObject[]; // ascending by version
}

/** Group a flat config list into objects with their version history. */
export function groupConfigs(configs: StudioConfigObject[]): ConfigGroup[] {
  const byId = new Map<string, ConfigGroup>();
  for (const cfg of configs) {
    const id = `${cfg.kind}/${cfg.key}`;
    const group = byId.get(id) ?? { kind: cfg.kind, key: cfg.key, versions: [] };
    group.versions.push(cfg);
    byId.set(id, group);
  }
  const groups = [...byId.values()];
  for (const g of groups) g.versions.sort((a, b) => a.version - b.version);
  groups.sort((a, b) => `${a.kind}/${a.key}`.localeCompare(`${b.kind}/${b.key}`));
  return groups;
}

/** Actions the server allows for a config version in a given status. */
export function configActions(status: StudioConfigObject["status"]): string[] {
  switch (status) {
    case "draft":
      return ["edit", "publish"];
    case "published":
      return ["disable"];
    case "disabled":
      return ["publish", "rollback"];
    case "superseded":
      return ["rollback"];
  }
}
