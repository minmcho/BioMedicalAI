import { useState } from "react";
import type { ApiClient } from "../../api/client";
import { ApiError } from "../../api/client";
import type { ConfigKind, ModulePack, StudioConfigObject } from "../../api/types";
import { useAsync } from "../../hooks";

// Pack Builder (Studio parity row 28) — compose a module pack in the
// UI and create it as a designer draft through the existing pack API
// (POST /studio/packs, backend/app/studio.py create_pack): body
// {name, version, manifest}. The manifest carries a description,
// endpoint DECLARATIONS and an artefact list over existing governed
// config. Endpoint declarations are born disabled — the client has no
// dedicated create-pack wrapper, so creation goes through the same
// ApiClient.request transport StudioPage's pack methods (listPacks /
// installPack / installationAction) are built on; if a createPack
// wrapper is present on the client it is preferred. Enablement stays
// a separate governed step, exactly as the backend enforces (a
// manifest claiming enabled=true is refused 422).
//
// The coordinator mounts this into StudioPage's packs tab; this file
// deliberately does not touch StudioPage.

/** Config kinds the client can list via listConfigs — the artefact
 *  picker's vocabulary (mirrors api/types ConfigKind). */
const ARTEFACT_KINDS: ConfigKind[] = [
  "form",
  "workflow",
  "terminology",
  "navigation",
  "branding",
  "permissions",
  "cohort",
  "integration",
  "orderset",
  "agent",
  "rule",
  "dashboard",
  "regulatory_pack",
  "pipeline",
];

const HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"];

interface EndpointRow {
  method: string;
  path: string;
}

interface Artefact {
  kind: ConfigKind;
  key: string;
}

type PackClient = ApiClient & {
  createPack?: (body: Record<string, unknown>) => Promise<ModulePack>;
};

export function PackBuilder({ client }: { client: ApiClient }) {
  const [name, setName] = useState("");
  const [version, setVersion] = useState("1.0.0");
  const [description, setDescription] = useState("");
  const [endpoints, setEndpoints] = useState<EndpointRow[]>([]);
  const [artefactKind, setArtefactKind] = useState<ConfigKind>("form");
  const [artefactKey, setArtefactKey] = useState("");
  const [artefacts, setArtefacts] = useState<Artefact[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<ModulePack | null>(null);

  const configs = useAsync<StudioConfigObject[]>(
    () => client.listConfigs(artefactKind),
    [client, artefactKind],
  );
  const availableKeys = [...new Set((configs.data ?? []).map((c) => c.key))].sort();
  const effectiveArtefactKey = artefactKey || availableKeys[0] || "";

  const setEndpoint = (index: number, patch: Partial<EndpointRow>) =>
    setEndpoints((rows) => rows.map((r, i) => (i === index ? { ...r, ...patch } : r)));

  function addArtefact() {
    if (!effectiveArtefactKey) return;
    const next = { kind: artefactKind, key: effectiveArtefactKey };
    setArtefacts((list) =>
      list.some((a) => a.kind === next.kind && a.key === next.key) ? list : [...list, next],
    );
  }

  async function create() {
    setBusy(true);
    setError(null);
    setCreated(null);
    try {
      if (!name.trim()) {
        setError("A pack name is required.");
        return;
      }
      if (!version.trim()) {
        setError("A pack version is required.");
        return;
      }
      const declared = endpoints.filter((e) => e.path.trim());
      const body: Record<string, unknown> = {
        name: name.trim(),
        version: version.trim(),
        manifest: {
          description: description.trim(),
          // Locked: every declared endpoint ships disabled.
          api_endpoints: declared.map((e) => ({
            method: e.method,
            path: e.path.trim(),
            enabled: false,
          })),
          artefacts: artefacts.map((a) => ({ kind: a.kind, key: a.key })),
        },
      };
      const packClient = client as PackClient;
      const pack = packClient.createPack
        ? await packClient.createPack(body)
        : await client.request<ModulePack>("/studio/packs", {
            method: "POST",
            body: JSON.stringify(body),
          });
      setCreated(pack);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Pack creation failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="studio-editor">
      <h3>Pack builder</h3>
      <p className="hint">
        Compose a module pack from a manifest, endpoint declarations and existing
        governed config artefacts, and create it as a draft pack — no file editing.
      </p>
      {error ? (
        <div className="state state-error" role="alert">
          <strong>Action failed.</strong> {error}
        </div>
      ) : null}
      {created ? (
        <div className="state state-ok" role="status">
          <p>
            Created draft pack <strong>{created.name}</strong> v{created.version}.
          </p>
          <p className="hint">
            Next: install → approve → attach → enable in Module packs.
          </p>
        </div>
      ) : null}

      <p>
        <label>
          Pack name{" "}
          <input
            aria-label="Pack name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. solo-practice-profile"
          />
        </label>{" "}
        <label>
          Pack version{" "}
          <input
            aria-label="Pack version"
            value={version}
            onChange={(e) => setVersion(e.target.value)}
          />
        </label>{" "}
        <label>
          Pack description{" "}
          <input
            aria-label="Pack description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
      </p>

      <h4>Endpoint declarations</h4>
      <p className="hint">
        🔒 Endpoints ship disabled — enablement is a separate governed step.
      </p>
      {endpoints.map((row, i) => (
        <p key={i} className="studio-row">
          <label>
            Method{" "}
            <select
              aria-label={`Endpoint ${i + 1} method`}
              value={row.method}
              onChange={(e) => setEndpoint(i, { method: e.target.value })}
            >
              {HTTP_METHODS.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>{" "}
          <label>
            Path{" "}
            <input
              aria-label={`Endpoint ${i + 1} path`}
              value={row.path}
              placeholder="/example/path"
              onChange={(e) => setEndpoint(i, { path: e.target.value })}
            />
          </label>{" "}
          <label>
            <input type="checkbox" aria-label={`Endpoint ${i + 1} enabled`} checked={false} disabled />{" "}
            enabled: false (locked)
          </label>{" "}
          <button
            type="button"
            aria-label={`Remove endpoint ${i + 1}`}
            onClick={() => setEndpoints((rows) => rows.filter((_, j) => j !== i))}
          >
            ✕
          </button>
        </p>
      ))}
      <p>
        <button
          type="button"
          onClick={() => setEndpoints((rows) => [...rows, { method: "GET", path: "" }])}
        >
          Add endpoint
        </button>
      </p>

      <h4>Artefacts (governed config)</h4>
      <p>
        <label>
          Artefact kind{" "}
          <select
            aria-label="Artefact kind"
            value={artefactKind}
            onChange={(e) => {
              setArtefactKind(e.target.value as ConfigKind);
              setArtefactKey("");
            }}
          >
            {ARTEFACT_KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>{" "}
        <label>
          Artefact key{" "}
          <select
            aria-label="Artefact key"
            value={effectiveArtefactKey}
            onChange={(e) => setArtefactKey(e.target.value)}
          >
            {availableKeys.length === 0 ? (
              <option value="">(none available)</option>
            ) : (
              availableKeys.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))
            )}
          </select>
        </label>{" "}
        <button type="button" disabled={!effectiveArtefactKey} onClick={addArtefact}>
          Add artefact
        </button>
      </p>
      {artefacts.length > 0 ? (
        <ul>
          {artefacts.map((a, i) => (
            <li key={`${a.kind}/${a.key}`}>
              {a.kind}/{a.key}{" "}
              <button
                type="button"
                aria-label={`Remove artefact ${a.kind}/${a.key}`}
                onClick={() => setArtefacts((list) => list.filter((_, j) => j !== i))}
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="hint">No artefacts added yet.</p>
      )}

      <p className="studio-actions">
        <button type="button" disabled={busy} onClick={create}>
          Create draft pack
        </button>
      </p>
    </div>
  );
}
