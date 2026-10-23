import { useState } from "react";
import type { ApiClient } from "../../api/client";
import { ApiError } from "../../api/client";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

/** AI Providers tab — the governed AI provider registry (Studio
 *  parity row 4). Provider definitions are versioned declarations
 *  served by /ai-providers: draft → publish, and only PUBLISHED
 *  providers appear in the effective panel. A definition names its
 *  API key by REFERENCE only — the name of an environment variable
 *  on the server — so no secret value is ever entered, stored or
 *  shown here; /ai-providers/effective reports only whether the
 *  referenced variable is present (keyConfigured), never its value. */

interface ProviderDefinition {
  key: string;
  version: number;
  status: string;
  payload: Record<string, unknown>;
}

interface EffectiveProvider {
  key: string;
  name?: string;
  type?: string;
  model?: string;
  baseUrl?: string;
  base_url?: string;
  department?: string;
  enabled?: boolean;
  keyConfigured?: boolean | null;
}

/** The definitions endpoint answers {items:[…]}; tolerate a plain
 *  array as well so the tab survives either wire shape. */
function asItems<T>(raw: unknown): T[] {
  if (Array.isArray(raw)) return raw as T[];
  if (
    raw &&
    typeof raw === "object" &&
    Array.isArray((raw as { items?: unknown }).items)
  ) {
    return (raw as { items: T[] }).items;
  }
  return [];
}

function payloadStr(payload: Record<string, unknown>, ...keys: string[]): string {
  for (const k of keys) {
    const v = payload[k];
    if (typeof v === "string" && v) return v;
  }
  return "";
}

function keyConfiguredLabel(value: boolean | null | undefined): string {
  if (value === true) return "Yes";
  if (value === false) return "No";
  return "Not required";
}

const PROVIDER_TYPES = [
  "ollama",
  "openai-compatible",
  "anthropic",
  "google",
  "azure-openai",
];

export function AiProvidersTab({ client }: { client: ApiClient }) {
  const [reloadKey, setReloadKey] = useState(0);
  const definitionsRaw = useAsync<unknown>(
    () => client.request("/ai-providers/definitions"),
    [client, reloadKey],
  );
  const effectiveRaw = useAsync<unknown>(
    () => client.request("/ai-providers/effective"),
    [client, reloadKey],
  );
  const [notice, setNotice] = useState<string | null>(null);
  const [form, setForm] = useState({
    key: "",
    name: "",
    type: "ollama",
    model: "",
    baseUrl: "",
    department: "",
    apiKeyRef: "",
    enabled: true,
  });

  if (definitionsRaw.loading || effectiveRaw.loading) {
    return <Loading label="Loading AI providers…" />;
  }
  if (definitionsRaw.error) return <ErrorView error={definitionsRaw.error} />;
  if (effectiveRaw.error) return <ErrorView error={effectiveRaw.error} />;

  const definitions = asItems<ProviderDefinition>(definitionsRaw.data);
  const effective = asItems<EffectiveProvider>(effectiveRaw.data);
  const effectiveNote =
    effectiveRaw.data &&
    typeof effectiveRaw.data === "object" &&
    !Array.isArray(effectiveRaw.data) &&
    typeof (effectiveRaw.data as { note?: unknown }).note === "string"
      ? ((effectiveRaw.data as { note: string }).note as string)
      : null;

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

  async function createDraft() {
    const key = form.key.trim();
    if (!key) return;
    await act(
      () =>
        client.request(`/ai-providers/definitions/${encodeURIComponent(key)}`, {
          method: "POST",
          body: JSON.stringify({
            key,
            name: form.name.trim(),
            type: form.type,
            model: form.model.trim(),
            baseUrl: form.baseUrl.trim(),
            department: form.department.trim(),
            apiKeyRef: form.apiKeyRef.trim(),
            enabled: form.enabled,
          }),
        }),
      `Draft created for provider ${key}. Publish it to make it effective.`,
    );
  }

  return (
    <div>
      <p className="hint">
        AI providers are governed declarations: model route, provider type and
        per-department default, versioned like every Studio config. Publishing
        makes a provider effective; nothing here stores a secret. API keys are
        never entered here — apiKeyRef names an environment variable on the
        server, and the effective panel reports only whether that variable is
        set.
      </p>
      {notice && (
        <p className="hint" role="status">
          {notice}
        </p>
      )}

      <h3>Provider definitions</h3>
      {definitions.length === 0 ? (
        <Empty label="No AI provider definitions yet." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Key</th>
              <th>Name</th>
              <th>Type</th>
              <th>Model</th>
              <th>Department</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {definitions.map((d) => (
              <tr key={`${d.key}-v${d.version}`}>
                <td>
                  <strong>{d.key}</strong>
                  <br />
                  <span className="hint">v{d.version}</span>
                </td>
                <td>{payloadStr(d.payload, "name") || d.key}</td>
                <td>{payloadStr(d.payload, "type", "provider_type") || "—"}</td>
                <td>
                  {payloadStr(d.payload, "model") ? (
                    <code>{payloadStr(d.payload, "model")}</code>
                  ) : (
                    "—"
                  )}
                </td>
                <td>{payloadStr(d.payload, "department") || "—"}</td>
                <td>
                  <span className="badge">{d.status}</span>
                </td>
                <td>
                  {d.status === "draft" && (
                    <button
                      type="button"
                      onClick={() =>
                        void act(
                          () =>
                            client.request(
                              `/ai-providers/definitions/${encodeURIComponent(d.key)}/versions/${d.version}/publish`,
                              { method: "POST" },
                            ),
                          `Published ${d.key} v${d.version}.`,
                        )
                      }
                    >
                      Publish v{d.version}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Create provider draft</h3>
      <div className="inline-form">
        <label className="field">
          Key{" "}
          <input
            aria-label="Provider key"
            value={form.key}
            onChange={(e) => setForm({ ...form, key: e.target.value })}
            placeholder="e.g. local-ollama"
          />
        </label>
        <label className="field">
          Name{" "}
          <input
            aria-label="Provider name"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="e.g. Local Ollama"
          />
        </label>
        <label className="field">
          Type{" "}
          <select
            aria-label="Provider type"
            value={form.type}
            onChange={(e) => setForm({ ...form, type: e.target.value })}
          >
            {PROVIDER_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Model{" "}
          <input
            aria-label="Provider model"
            value={form.model}
            onChange={(e) => setForm({ ...form, model: e.target.value })}
            placeholder="e.g. llama3.2:3b"
          />
        </label>
        <label className="field">
          Base URL{" "}
          <input
            aria-label="Provider base URL"
            value={form.baseUrl}
            onChange={(e) => setForm({ ...form, baseUrl: e.target.value })}
            placeholder="e.g. http://127.0.0.1:11434"
          />
        </label>
        <label className="field">
          Department{" "}
          <input
            aria-label="Provider department"
            value={form.department}
            onChange={(e) => setForm({ ...form, department: e.target.value })}
            placeholder="e.g. Radiology"
          />
        </label>
        <label className="field">
          API key reference{" "}
          <input
            aria-label="Provider API key reference"
            value={form.apiKeyRef}
            onChange={(e) => setForm({ ...form, apiKeyRef: e.target.value })}
            placeholder="e.g. OPENAI_API_KEY"
          />
        </label>
        <label className="field">
          <span>Enabled</span>
          <input
            aria-label="Provider enabled"
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
          />
        </label>
        <button
          type="button"
          disabled={!form.key.trim()}
          onClick={() => void createDraft()}
        >
          Create draft
        </button>
      </div>
      <p className="hint">
        API keys are never entered here — apiKeyRef names an environment
        variable on the server.
      </p>

      <h3>Effective providers</h3>
      {effectiveNote && <p className="hint">{effectiveNote}</p>}
      {effective.length === 0 ? (
        <Empty label="No published providers are effective yet." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Key</th>
              <th>Name</th>
              <th>Type</th>
              <th>Model</th>
              <th>Base URL</th>
              <th>Department</th>
              <th>State</th>
              <th>API key configured</th>
            </tr>
          </thead>
          <tbody>
            {effective.map((p) => (
              <tr key={p.key}>
                <td>
                  <strong>{p.key}</strong>
                </td>
                <td>{p.name || p.key}</td>
                <td>{p.type || "—"}</td>
                <td>{p.model ? <code>{p.model}</code> : "—"}</td>
                <td>{p.baseUrl || p.base_url || "—"}</td>
                <td>{p.department || "—"}</td>
                <td>
                  {p.enabled === undefined ? (
                    "—"
                  ) : p.enabled ? (
                    <span className="badge">enabled</span>
                  ) : (
                    "disabled"
                  )}
                </td>
                <td>{keyConfiguredLabel(p.keyConfigured)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
