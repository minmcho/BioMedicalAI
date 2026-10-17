import { useState } from "react";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/client";
import type {
  ConfigKind,
  ModulePack,
  PackInstallation,
  StudioConfigObject,
} from "../api/types";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";
import { AuditTab } from "./studio/AuditTab";
import { BrandingTab } from "./studio/BrandingTab";
import { FormsTab } from "./studio/FormsTab";
import { CONFIG_KINDS, configActions, groupConfigs, nextAction } from "./studio/helpers";
import { IntegrationTab } from "./studio/IntegrationTab";
import { NavigationTab } from "./studio/NavigationTab";
import { OverviewTab } from "./studio/OverviewTab";
import { PermissionsTab } from "./studio/PermissionsTab";
import { TerminologyTab } from "./studio/TerminologyTab";
import { WorkflowsTab } from "./studio/WorkflowsTab";

// Re-exported for existing imports (api/studioConfig.test.ts and the
// Studio editor shell); the implementations live in ./studio/helpers.
export { CONFIG_KINDS, configActions, groupConfigs, nextAction };
export type { ConfigGroup } from "./studio/helpers";

interface StudioData {
  packs: ModulePack[];
  installations: PackInstallation[];
}

interface Props {
  client: ApiClient;
}

/** Module Packs tab — the System Studio module-pack lifecycle:
 *  packs install as designer drafts and advance only through the separate
 *  approve / attach / enable steps, via the real /studio endpoints. */
function StudioPacksSection({ client }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const { data, loading, error, reload } = useAsync<StudioData>(async () => {
    const [packs, installations] = await Promise.all([
      client.listPacks(),
      client.listInstallations(),
    ]);
    return { packs, installations };
  }, [client]);

  async function run(key: string, fn: () => Promise<unknown>) {
    setBusy(key);
    setActionError(null);
    try {
      await fn();
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.detail : "Action failed.");
    } finally {
      setBusy(null);
    }
  }

  if (loading) return <Loading label="Loading packs…" />;
  if (error) return <ErrorView error={error} />;
  if (!data) return <Empty label="No pack data." />;

  const installationByPack = new Map<number, PackInstallation>(
    data.installations.map((i) => [i.pack_id, i]),
  );

  return (
    <section>
      <p className="hint">
        Module packs install as designer drafts. A pack becomes usable only through the
        separate approve → attach → enable steps; declared endpoints stay disabled in the
        manifest until then.
      </p>
      {actionError ? (
        <div className="state state-error" role="alert">
          <strong>Action failed.</strong> {actionError}
        </div>
      ) : null}

      {data.packs.length === 0 ? (
        <Empty label="No packs available on the server." />
      ) : (
        <div className="pack-list">
          {data.packs.map((pack) => {
            const inst = installationByPack.get(pack.id);
            const action = inst ? nextAction(inst.status) : null;
            return (
              <article key={pack.id} className="card pack">
                <header>
                  <h3>
                    {pack.name} <span className="pack-version">v{pack.version}</span>
                  </h3>
                  <span className="badge">{inst ? inst.status : "not installed"}</span>
                </header>
                {pack.manifest.description ? <p>{pack.manifest.description}</p> : null}

                {(pack.manifest.modules ?? []).length > 0 ? (
                  <p className="pack-detail">
                    Modules: {(pack.manifest.modules ?? []).map((m) => m.name).join(", ")}
                  </p>
                ) : null}
                {(pack.manifest.api_endpoints ?? []).length > 0 ? (
                  <p className="pack-detail">
                    Endpoints:{" "}
                    {(pack.manifest.api_endpoints ?? [])
                      .map((e) => `${e.method} ${e.path}${e.enabled ? "" : " (disabled)"}`)
                      .join(", ")}
                  </p>
                ) : null}

                {!inst ? (
                  <button
                    type="button"
                    disabled={busy === `install-${pack.id}`}
                    onClick={() => run(`install-${pack.id}`, () => client.installPack(pack.name))}
                  >
                    Install as draft
                  </button>
                ) : action ? (
                  <button
                    type="button"
                    disabled={busy === `inst-${inst.id}`}
                    onClick={() =>
                      run(`inst-${inst.id}`, () => client.installationAction(inst.id, action))
                    }
                  >
                    {action[0].toUpperCase() + action.slice(1)}
                  </button>
                ) : (
                  <span className="hint">Disabled — terminal for this installation.</span>
                )}
                {inst ? <p className="pack-detail">Installed by {inst.installed_by}</p> : null}
              </article>
            );
          })}
        </div>
      )}

      {data.installations.length > 0 ? (
        <>
          <h3>Installations</h3>
          <ul>
            {data.installations.map((i) => (
              <li key={i.id}>
                {i.pack_name} v{i.version} — {i.status} (by {i.installed_by})
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Advanced tab — the raw-JSON config editor (all kinds/keys, versions,
// diff-free payload view, rollback). Kept at full capability: the visual
// tabs cover the common kinds; anything else (cohort definitions, novel
// payload shapes) is edited here.
// ---------------------------------------------------------------------------

/** Studio configuration — versioned config objects with the draft →
 *  publish lifecycle, driven entirely through the real /studio/config
 *  endpoints. Published versions are immutable: "New draft" copies a
 *  payload into the editor, and saving creates the next draft version. */
export function StudioConfigSection({ client }: Props) {
  const [kindFilter, setKindFilter] = useState<ConfigKind | "all">("all");
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [viewingId, setViewingId] = useState<number | null>(null);

  // Draft editor state. When `editing` is set, saving PUTs to that draft
  // version; otherwise saving POSTs a new draft version for kind/key.
  const [editKind, setEditKind] = useState<ConfigKind>("form");
  const [editKey, setEditKey] = useState("");
  const [editText, setEditText] = useState("{}");
  const [editing, setEditing] = useState<{ version: number } | null>(null);
  const [editError, setEditError] = useState<string | null>(null);

  const { data, loading, error, reload } = useAsync<StudioConfigObject[]>(
    () => client.listConfigs(),
    [client],
  );

  async function run(key: string, fn: () => Promise<unknown>) {
    setBusy(key);
    setActionError(null);
    try {
      await fn();
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.detail : "Action failed.");
    } finally {
      setBusy(null);
    }
  }

  function loadIntoEditor(cfg: StudioConfigObject) {
    setEditKind(cfg.kind);
    setEditKey(cfg.key);
    setEditText(JSON.stringify(cfg.payload, null, 2));
    setEditing(cfg.status === "draft" ? { version: cfg.version } : null);
    setEditError(null);
  }

  async function saveDraft() {
    setEditError(null);
    let payload: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(editText);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error("Payload must be a JSON object.");
      }
      payload = parsed as Record<string, unknown>;
    } catch (err) {
      setEditError(err instanceof Error ? err.message : "Invalid JSON.");
      return;
    }
    if (!editKey.trim()) {
      setEditError("A config key is required.");
      return;
    }
    setBusy("save-draft");
    try {
      if (editing) {
        await client.updateConfigDraft(editKind, editKey.trim(), editing.version, payload);
      } else {
        await client.createConfigDraft(editKind, editKey.trim(), payload);
      }
      setEditing(null);
      reload();
    } catch (err) {
      setEditError(err instanceof ApiError ? err.detail : "Save failed.");
    } finally {
      setBusy(null);
    }
  }

  if (loading) return <Loading label="Loading configuration…" />;
  if (error) return <ErrorView error={error} />;
  if (!data) return <Empty label="No configuration data." />;

  const groups = groupConfigs(data).filter(
    (g) => kindFilter === "all" || g.kind === kindFilter,
  );

  return (
    <section>
      <p className="hint">
        Raw JSON editing for any config kind and key — including kinds without a visual
        editor (cohort) and payload shapes the visual tabs do not model. Same governance:
        drafts are designer-only, publishing supersedes, published versions are immutable.
      </p>
      {actionError ? (
        <div className="state state-error" role="alert">
          <strong>Action failed.</strong> {actionError}
        </div>
      ) : null}

      <p>
        <label>
          Kind{" "}
          <select
            value={kindFilter}
            onChange={(e) => setKindFilter(e.target.value as ConfigKind | "all")}
          >
            <option value="all">all</option>
            {CONFIG_KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
      </p>

      {groups.length === 0 ? (
        <Empty label="No configuration objects yet. Create a draft below." />
      ) : (
        <div className="pack-list">
          {groups.map((group) => {
            const latest = group.versions[group.versions.length - 1];
            const published = group.versions.find((v) => v.status === "published");
            return (
              <article key={`${group.kind}/${group.key}`} className="card pack">
                <header>
                  <h3>
                    {group.kind}/{group.key}
                  </h3>
                  <span className="badge">
                    {published ? `effective: v${published.version}` : "nothing published"}
                  </span>
                </header>
                <ul className="config-versions">
                  {group.versions.map((v) => (
                    <li key={v.id}>
                      <span>
                        v{v.version} — {v.status} (by {v.updated_by || v.created_by})
                      </span>{" "}
                      <button type="button" onClick={() => setViewingId(viewingId === v.id ? null : v.id)}>
                        {viewingId === v.id ? "Hide" : "View"}
                      </button>{" "}
                      <button type="button" onClick={() => loadIntoEditor(v)}>
                        {v.status === "draft" ? "Edit draft" : "New draft from this"}
                      </button>{" "}
                      {configActions(v.status).includes("publish") ? (
                        <button
                          type="button"
                          disabled={busy === `pub-${v.id}`}
                          onClick={() => run(`pub-${v.id}`, () => client.publishConfig(v.kind, v.key, v.version))}
                        >
                          Publish
                        </button>
                      ) : null}{" "}
                      {configActions(v.status).includes("disable") ? (
                        <button
                          type="button"
                          disabled={busy === `dis-${v.id}`}
                          onClick={() => run(`dis-${v.id}`, () => client.disableConfig(v.kind, v.key, v.version))}
                        >
                          Disable
                        </button>
                      ) : null}{" "}
                      {configActions(v.status).includes("rollback") ? (
                        <button
                          type="button"
                          disabled={busy === `rb-${v.id}`}
                          onClick={() => run(`rb-${v.id}`, () => client.rollbackConfig(v.kind, v.key, v.version))}
                        >
                          Roll back to v{v.version}
                        </button>
                      ) : null}
                      {viewingId === v.id ? (
                        <pre className="config-payload">{JSON.stringify(v.payload, null, 2)}</pre>
                      ) : null}
                    </li>
                  ))}
                </ul>
                <span className="hint">Latest version: v{latest.version}</span>
              </article>
            );
          })}
        </div>
      )}

      <h3>{editing ? `Edit draft (${editKind}/${editKey} v${editing.version})` : "Create draft"}</h3>
      {editError ? (
        <div className="state state-error" role="alert">
          <strong>Cannot save.</strong> {editError}
        </div>
      ) : null}
      <div className="config-editor">
        <label>
          Kind{" "}
          <select
            value={editKind}
            disabled={editing !== null}
            onChange={(e) => setEditKind(e.target.value as ConfigKind)}
          >
            {CONFIG_KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>{" "}
        <label>
          Key{" "}
          <input
            value={editKey}
            disabled={editing !== null}
            onChange={(e) => setEditKey(e.target.value)}
            placeholder="e.g. intake"
          />
        </label>
        <textarea
          aria-label="Config payload JSON"
          value={editText}
          onChange={(e) => setEditText(e.target.value)}
          rows={10}
          spellCheck={false}
        />
        <p>
          <button type="button" disabled={busy === "save-draft"} onClick={saveDraft}>
            {editing ? "Save draft" : "Create draft"}
          </button>{" "}
          {editing ? (
            <button
              type="button"
              onClick={() => {
                setEditing(null);
                setEditText("{}");
              }}
            >
              Cancel edit
            </button>
          ) : null}
        </p>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// System Studio — the tabbed visual module
// ---------------------------------------------------------------------------

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "packs", label: "Module packs" },
  { key: "forms", label: "Forms designer" },
  { key: "workflows", label: "Workflows" },
  { key: "terminology", label: "Terminology" },
  { key: "navigation", label: "Navigation" },
  { key: "branding", label: "Branding" },
  { key: "permissions", label: "Permissions" },
  { key: "integration", label: "Integration" },
  { key: "advanced", label: "Advanced (JSON)" },
  { key: "audit", label: "Audit" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

export function StudioPage({ client }: Props) {
  const [tab, setTab] = useState<TabKey>("overview");
  return (
    <>
      <h2>System Studio</h2>
      <p className="hint">
        The customization and future-configuration layer: everything a deploying
        organisation changes — module packs, forms, workflows, terminology, navigation,
        branding, permissions, integrations — is versioned configuration data with a
        draft → publish lifecycle and a hash-chained audit trail. Drafts change
        nothing until a human publishes them; once published, configuration is
        live: the API reads workflows, terminology, permissions and integrations
        from the published versions on every request, and the web shell reads
        navigation, branding and permissions at sign-in.
      </p>
      <div className="studio-tabs" role="tablist" aria-label="System Studio sections">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            className={tab === t.key ? "studio-tab active" : "studio-tab"}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "overview" && <OverviewTab client={client} />}
      {tab === "packs" && <StudioPacksSection client={client} />}
      {tab === "forms" && <FormsTab client={client} />}
      {tab === "workflows" && <WorkflowsTab client={client} />}
      {tab === "terminology" && <TerminologyTab client={client} />}
      {tab === "navigation" && <NavigationTab client={client} />}
      {tab === "branding" && <BrandingTab client={client} />}
      {tab === "permissions" && <PermissionsTab client={client} />}
      {tab === "integration" && <IntegrationTab client={client} />}
      {tab === "advanced" && <StudioConfigSection client={client} />}
      {tab === "audit" && <AuditTab client={client} />}
    </>
  );
}
