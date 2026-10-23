import { useState } from "react";
import type { ApiClient } from "../../api/client";
import { ApiError } from "../../api/client";
import type { StudioConfigObject } from "../../api/types";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

/** Registry tab — governed DECLARATIONS for endpoints and tools
 *  (Studio parity rows 18 + 37). Both are ordinary Studio config
 *  kinds with the standard draft → publish lifecycle, adapted
 *  under Rule 1 (no arbitrary code execution in the clinical
 *  runtime): an endpoint declaration records method, path,
 *  capability gate and backing, and is BORN DISABLED; a tool
 *  declaration records a kind from the governed vocabulary and a
 *  config reference. Neither declaration creates anything live —
 *  the honesty lines under each section say so in the UI, rather
 *  than letting a registry row imply a running route or tool. */

// NOTE(coordinator): "endpoint" and "tool" are not yet in the
// ConfigKind union in api/types.ts, so the typed client methods
// reject them at compile time. Until the union is extended (the
// coordinator's wiring step), the calls go through this minimal
// local shape — the runtime calls are exactly the ones
// ConfigEditorShell uses for every other kind.
interface AnyKindClient {
  listConfigs: (kind: string) => Promise<StudioConfigObject[]>;
  createConfigDraft: (
    kind: string,
    key: string,
    payload: Record<string, unknown>,
  ) => Promise<StudioConfigObject>;
  publishConfig: (
    kind: string,
    key: string,
    version: number,
  ) => Promise<StudioConfigObject>;
}

const ENDPOINT_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"];
const ENDPOINT_BACKINGS = [
  { value: "service", label: "Existing service" },
  { value: "fhir", label: "FHIR resource" },
  { value: "external", label: "External system" },
  { value: "none", label: "None — declaration only" },
];
const TOOL_KINDS = ["rest", "webhook", "mcp", "pipeline", "rule"];

function payloadStr(payload: Record<string, unknown>, key: string): string {
  const value = payload[key];
  return typeof value === "string" && value ? value : "—";
}

export function RegistryTab({ client }: { client: ApiClient }) {
  const anyClient = client as unknown as AnyKindClient;
  const [reloadKey, setReloadKey] = useState(0);
  const endpoints = useAsync<StudioConfigObject[]>(
    () => anyClient.listConfigs("endpoint"),
    [client, reloadKey],
  );
  const tools = useAsync<StudioConfigObject[]>(
    () => anyClient.listConfigs("tool"),
    [client, reloadKey],
  );
  const [notice, setNotice] = useState<string | null>(null);
  const [endpointForm, setEndpointForm] = useState({
    key: "",
    title: "",
    method: "GET",
    path: "",
    capability: "",
    backing: "service",
  });
  const [toolForm, setToolForm] = useState({
    key: "",
    title: "",
    kind: "rest",
    configRef: "",
  });

  if (endpoints.loading || tools.loading) {
    return <Loading label="Loading registries…" />;
  }
  if (endpoints.error) return <ErrorView error={endpoints.error} />;
  if (tools.error) return <ErrorView error={tools.error} />;

  const endpointRows = endpoints.data ?? [];
  const toolRows = tools.data ?? [];

  async function act(fn: () => Promise<unknown>, ok: string) {
    setNotice(null);
    try {
      await fn();
      setNotice(ok);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setNotice(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function createEndpoint() {
    const key = endpointForm.key.trim();
    if (!key) return;
    await act(
      () =>
        anyClient.createConfigDraft("endpoint", key, {
          title: endpointForm.title.trim(),
          method: endpointForm.method,
          path: endpointForm.path.trim(),
          capability: endpointForm.capability.trim(),
          backing: endpointForm.backing,
          enabled: false,
        }),
      `Endpoint declaration ${key} drafted (disabled).`,
    );
  }

  async function createTool() {
    const key = toolForm.key.trim();
    if (!key) return;
    await act(
      () =>
        anyClient.createConfigDraft("tool", key, {
          title: toolForm.title.trim(),
          kind: toolForm.kind,
          configRef: toolForm.configRef.trim(),
          enabled: false,
        }),
      `Tool declaration ${key} drafted (disabled).`,
    );
  }

  function publishButton(kind: string, row: StudioConfigObject) {
    if (row.status !== "draft") return null;
    return (
      <button
        type="button"
        onClick={() =>
          void act(
            () => anyClient.publishConfig(kind, row.key, row.version),
            `Published ${kind}/${row.key} v${row.version}.`,
          )
        }
      >
        Publish v{row.version}
      </button>
    );
  }

  return (
    <div>
      <p className="hint">
        Endpoint and tool DECLARATIONS under the same draft → publish
        governance as every Studio kind. Declarations are born disabled and
        create nothing live by themselves.
      </p>
      {notice && (
        <p className="hint" role="status">
          {notice}
        </p>
      )}

      <h3>Endpoints</h3>
      {endpointRows.length === 0 ? (
        <Empty label="No endpoint declarations yet." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Key</th>
              <th>Title</th>
              <th>Method</th>
              <th>Path</th>
              <th>Capability</th>
              <th>Backing</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {endpointRows.map((row) => (
              <tr key={row.id}>
                <td>
                  <strong>{row.key}</strong>
                  <br />
                  <span className="hint">v{row.version}</span>
                </td>
                <td>{payloadStr(row.payload, "title")}</td>
                <td>{payloadStr(row.payload, "method")}</td>
                <td>
                  <code>{payloadStr(row.payload, "path")}</code>
                </td>
                <td>{payloadStr(row.payload, "capability")}</td>
                <td>{payloadStr(row.payload, "backing")}</td>
                <td>
                  <span className="badge">{row.status}</span>
                </td>
                <td>{publishButton("endpoint", row)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h4>Declare an endpoint</h4>
      <div className="inline-form">
        <label className="field">
          Key{" "}
          <input
            aria-label="Endpoint key"
            value={endpointForm.key}
            onChange={(e) =>
              setEndpointForm({ ...endpointForm, key: e.target.value })
            }
            placeholder="e.g. patient-intake-api"
          />
        </label>
        <label className="field">
          Title{" "}
          <input
            aria-label="Endpoint title"
            value={endpointForm.title}
            onChange={(e) =>
              setEndpointForm({ ...endpointForm, title: e.target.value })
            }
            placeholder="e.g. Patient intake API"
          />
        </label>
        <label className="field">
          Method{" "}
          <select
            aria-label="Endpoint method"
            value={endpointForm.method}
            onChange={(e) =>
              setEndpointForm({ ...endpointForm, method: e.target.value })
            }
          >
            {ENDPOINT_METHODS.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Path{" "}
          <input
            aria-label="Endpoint path"
            value={endpointForm.path}
            onChange={(e) =>
              setEndpointForm({ ...endpointForm, path: e.target.value })
            }
            placeholder="e.g. /intake/patients"
          />
        </label>
        <label className="field">
          Capability{" "}
          <input
            aria-label="Endpoint capability"
            value={endpointForm.capability}
            onChange={(e) =>
              setEndpointForm({ ...endpointForm, capability: e.target.value })
            }
            placeholder="e.g. intake.work"
          />
        </label>
        <label className="field">
          Backing{" "}
          <select
            aria-label="Endpoint backing"
            value={endpointForm.backing}
            onChange={(e) =>
              setEndpointForm({ ...endpointForm, backing: e.target.value })
            }
          >
            {ENDPOINT_BACKINGS.map((b) => (
              <option key={b.value} value={b.value}>
                {b.label}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          disabled={!endpointForm.key.trim()}
          onClick={() => void createEndpoint()}
        >
          Create draft
        </button>
      </div>
      <p className="hint">
        New endpoint declarations are born disabled. A declaration does not
        create a live route; enablement is a governance marker (see
        docs/STUDIO_PARITY.md).
      </p>

      <h3>Tools</h3>
      {toolRows.length === 0 ? (
        <Empty label="No tool declarations yet." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Key</th>
              <th>Title</th>
              <th>Kind</th>
              <th>Config reference</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {toolRows.map((row) => (
              <tr key={row.id}>
                <td>
                  <strong>{row.key}</strong>
                  <br />
                  <span className="hint">v{row.version}</span>
                </td>
                <td>{payloadStr(row.payload, "title")}</td>
                <td>{payloadStr(row.payload, "kind")}</td>
                <td>{payloadStr(row.payload, "configRef")}</td>
                <td>
                  <span className="badge">{row.status}</span>
                </td>
                <td>{publishButton("tool", row)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h4>Declare a tool</h4>
      <div className="inline-form">
        <label className="field">
          Key{" "}
          <input
            aria-label="Tool key"
            value={toolForm.key}
            onChange={(e) => setToolForm({ ...toolForm, key: e.target.value })}
            placeholder="e.g. fhir-lookup"
          />
        </label>
        <label className="field">
          Title{" "}
          <input
            aria-label="Tool title"
            value={toolForm.title}
            onChange={(e) => setToolForm({ ...toolForm, title: e.target.value })}
            placeholder="e.g. FHIR lookup tool"
          />
        </label>
        <label className="field">
          Kind{" "}
          <select
            aria-label="Tool kind"
            value={toolForm.kind}
            onChange={(e) => setToolForm({ ...toolForm, kind: e.target.value })}
          >
            {TOOL_KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Config reference{" "}
          <input
            aria-label="Tool config reference"
            value={toolForm.configRef}
            onChange={(e) =>
              setToolForm({ ...toolForm, configRef: e.target.value })
            }
            placeholder="e.g. integration/fhir-main"
          />
        </label>
        <button
          type="button"
          disabled={!toolForm.key.trim()}
          onClick={() => void createTool()}
        >
          Create draft
        </button>
      </div>
      <p className="hint">
        Tool execution lands with MCP in Phase 16; this registry governs
        declarations.
      </p>
    </div>
  );
}
