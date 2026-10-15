import { useState } from "react";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/client";
import type { InstallationStatus, ModulePack, PackInstallation, StudioAction } from "../api/types";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface StudioData {
  packs: ModulePack[];
  installations: PackInstallation[];
}

interface Props {
  client: ApiClient;
}

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

/** Studio packs — the System Studio module-pack lifecycle in the web UI:
 *  packs install as designer drafts and advance only through the separate
 *  approve / attach / enable steps, via the real /studio endpoints. */
export function StudioPage({ client }: Props) {
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
      <h2>Studio packs</h2>
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
