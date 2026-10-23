import { useState } from "react";
import type { ApiClient } from "../../api/client";
import { ApiError } from "../../api/client";
import type { ConfigKind, StudioConfigObject } from "../../api/types";
import { RichTextEditor } from "../../components/RichTextEditor";
import { htmlToText } from "../../components/richtext";
import { StatusBadge, type StatusTone } from "../../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";
import { ConfigEditorShell } from "./ConfigEditorShell";

// MCP & A2A tab (Phase 16, WS-K1) — protocol surfaces as governed
// configuration (backend/app/mcp_a2a.py). External MCP servers are a
// Studio config kind (mcp_server): drafted and published like any
// other config, then ENABLED operationally through the module's
// audited flip — publishing alone never makes a server callable, and
// credentials are refused in config payloads (names of credential
// fields are rejected by the server validator). This node's own MCP
// tool registry is seeded with every tool DISABLED. The A2A surface
// shows the public AgentCard and the JSON-RPC task lifecycle; the
// companion SSE stream replays a task's recorded status transitions.

const MCP_SERVER_KIND = "mcp_server" as ConfigKind;

export interface McpToolDecl {
  name: string;
  requiredCapability: string;
}

export interface McpServerModel {
  name: string;
  url: string;
  allowlistText: string;
  tools: McpToolDecl[];
}

export function parseMcpServer(payload: Record<string, unknown>): McpServerModel {
  const allowlist = Array.isArray(payload.toolAllowlist)
    ? payload.toolAllowlist.filter((t): t is string => typeof t === "string")
    : [];
  const tools = Array.isArray(payload.tools)
    ? payload.tools
        .filter((t): t is Record<string, unknown> => typeof t === "object" && t !== null)
        .map((t) => ({
          name: String(t.name ?? ""),
          requiredCapability: String(t.requiredCapability ?? ""),
        }))
    : [];
  return {
    name: String(payload.name ?? ""),
    url: String(payload.url ?? ""),
    allowlistText: allowlist.join(", "),
    tools,
  };
}

export function serializeMcpServer(model: McpServerModel): Record<string, unknown> {
  return {
    name: model.name.trim(),
    url: model.url.trim(),
    transport: "http",
    toolAllowlist: model.allowlistText
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean),
    tools: model.tools
      .filter((t) => t.name.trim())
      .map((t) => ({
        name: t.name.trim(),
        ...(t.requiredCapability.trim()
          ? { requiredCapability: t.requiredCapability.trim() }
          : {}),
      })),
  };
}

export function emptyMcpServer(): McpServerModel {
  return { name: "", url: "", allowlistText: "", tools: [] };
}

export function validateMcpServer(model: McpServerModel): string | null {
  if (!model.name.trim()) return "Name is required.";
  if (!/^https?:\/\//.test(model.url.trim()))
    return "URL is required and must start with http:// or https:// (no embedded credentials).";
  return null;
}

function McpServerEditor({
  model,
  setModel,
}: {
  model: McpServerModel;
  setModel: (m: McpServerModel) => void;
}) {
  return (
    <div className="studio-editor">
      <p className="hint">
        An external MCP server definition. Only tools named in the
        allowlist can ever be called through this node, and credential
        fields (apiKey, token, secret, …) are refused by the server —
        credentials never live in config. Publishing a definition does
        not enable it; enabling is the separate audited action in the
        server list below.
      </p>
      <p className="studio-row">
        <label>
          Name{" "}
          <input
            aria-label="MCP server name"
            value={model.name}
            onChange={(e) => setModel({ ...model, name: e.target.value })}
            size={32}
          />
        </label>{" "}
        <label>
          URL{" "}
          <input
            aria-label="MCP server URL"
            value={model.url}
            onChange={(e) => setModel({ ...model, url: e.target.value })}
            size={44}
            placeholder="http://127.0.0.1:9000/mcp"
          />
        </label>
      </p>
      <p className="studio-row">
        <label>
          Tool allowlist (comma-separated){" "}
          <input
            aria-label="Tool allowlist"
            value={model.allowlistText}
            onChange={(e) => setModel({ ...model, allowlistText: e.target.value })}
            size={56}
            placeholder="empty = no tools callable"
          />
        </label>
      </p>
      <h5>Declared tools</h5>
      {model.tools.map((tool, i) => (
        <p key={i} className="studio-row">
          <label>
            Tool name{" "}
            <input
              aria-label={`Declared tool ${i + 1} name`}
              value={tool.name}
              onChange={(e) =>
                setModel({
                  ...model,
                  tools: model.tools.map((t, j) => (j === i ? { ...t, name: e.target.value } : t)),
                })
              }
            />
          </label>{" "}
          <label>
            Required capability{" "}
            <input
              aria-label={`Declared tool ${i + 1} required capability`}
              value={tool.requiredCapability}
              onChange={(e) =>
                setModel({
                  ...model,
                  tools: model.tools.map((t, j) =>
                    j === i ? { ...t, requiredCapability: e.target.value } : t,
                  ),
                })
              }
              placeholder="optional"
            />
          </label>{" "}
          <button
            type="button"
            aria-label={`Remove declared tool ${i + 1}`}
            onClick={() => setModel({ ...model, tools: model.tools.filter((_, j) => j !== i) })}
          >
            ✕
          </button>
        </p>
      ))}
      <p>
        <button
          type="button"
          onClick={() =>
            setModel({ ...model, tools: [...model.tools, { name: "", requiredCapability: "" }] })
          }
        >
          Add declared tool
        </button>
      </p>
    </div>
  );
}

export interface McpServerPublic {
  key: string;
  version: number;
  name: string | null;
  transport: string;
  url: string | null;
  toolAllowlist: string[];
  tools: McpToolDecl[];
  enabled: boolean;
}

export interface RegistryTool {
  name: string;
  description: string;
  requiredCapability: string;
  patientScoped: boolean;
  enabled: boolean;
}

export interface AgentCard {
  name: string;
  description: string;
  url: string;
  version: string;
  protocolVersion: string;
  capabilities: { streaming: boolean; pushNotifications: boolean };
  skills: Array<{
    id: string;
    name: string;
    description: string;
    inputModes: string[];
    outputModes: string[];
  }>;
}

export interface A2aTask {
  id: string;
  contextId: string;
  kind: string;
  agentKey: string;
  status: string;
  statusMessage: string | null;
  patientRef: string | null;
  actor: string;
  onBehalfOf: string | null;
  history: unknown[];
  artifacts: unknown[];
  agentRunId: string | null;
  createdAt: string | null;
  updatedAt: string | null;
  streamUrl?: string;
}

interface RpcEnvelope<T> {
  jsonrpc: string;
  id: unknown;
  result?: T;
  error?: { code: number; message: string };
}

const TERMINAL_TASK_STATES = ["completed", "failed", "canceled"];

export function taskStatusTone(status: string): StatusTone {
  switch (status) {
    case "completed":
      return "ok";
    case "failed":
    case "canceled":
      return "bad";
    case "working":
      return "info";
    case "submitted":
      return "warn";
    default:
      return "neutral";
  }
}

function errText(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err);
}

export function McpA2aTab({ client }: { client: ApiClient }) {
  const [reloadKey, setReloadKey] = useState(0);
  const servers = useAsync<{ servers: McpServerPublic[]; total: number }>(
    () => client.request("/mcp/servers"),
    [client, reloadKey],
  );
  const registry = useAsync<StudioConfigObject>(
    () => client.effectiveConfig("integration", "mcp-tools"),
    [client, reloadKey],
  );
  const card = useAsync<AgentCard>(
    () => client.request("/.well-known/agent-card.json"),
    [client],
  );

  const [notice, setNotice] = useState<string | null>(null);
  const [upstreamTools, setUpstreamTools] = useState<{ server: string; tools: unknown[] } | null>(null);
  const [upstreamError, setUpstreamError] = useState<string | null>(null);

  // A2A task state is session-local: the protocol surface has no task
  // list endpoint, so the tab tracks the tasks it created or looked up.
  const [tasks, setTasks] = useState<A2aTask[]>([]);
  const [taskAgent, setTaskAgent] = useState("");
  const [taskMessage, setTaskMessage] = useState("");
  const [taskPatientRef, setTaskPatientRef] = useState("");
  const [taskError, setTaskError] = useState<string | null>(null);
  const [lookupId, setLookupId] = useState("");
  const [streamText, setStreamText] = useState<{ id: string; text: string } | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);

  const registryTools: RegistryTool[] =
    registry.data && Array.isArray(registry.data.payload.tools)
      ? (registry.data.payload.tools as RegistryTool[])
      : [];
  const cardSkills = card.data?.skills ?? [];
  const selectedTaskAgent = taskAgent || cardSkills[0]?.id || "";

  function upsertTask(task: A2aTask) {
    setTasks((prev) => {
      const rest = prev.filter((t) => t.id !== task.id);
      return [task, ...rest];
    });
  }

  async function act(fn: () => Promise<unknown>, ok: string) {
    setNotice(null);
    try {
      await fn();
      setNotice(ok);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setNotice(errText(err));
    }
  }

  async function viewUpstreamTools(key: string) {
    setUpstreamError(null);
    setUpstreamTools(null);
    try {
      const out = await client.request<{ server: string; tools: unknown[] }>(
        `/mcp/servers/${encodeURIComponent(key)}/tools`,
      );
      setUpstreamTools(out);
    } catch (err) {
      setUpstreamError(errText(err));
    }
  }

  async function rpc<T>(method: string, params: Record<string, unknown>): Promise<T> {
    const envelope = await client.request<RpcEnvelope<T>>("/a2a/rpc", {
      method: "POST",
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    });
    if (envelope.error) {
      throw new Error(`A2A error ${envelope.error.code}: ${envelope.error.message}`);
    }
    return envelope.result as T;
  }

  async function sendTask() {
    setTaskError(null);
    try {
      const task = await rpc<A2aTask>("message/stream", {
        agentKey: selectedTaskAgent,
        message: taskMessage,
        patientRef: taskPatientRef.trim() || undefined,
      });
      upsertTask(task);
      setNotice(`A2A task ${task.id.slice(0, 8)}… settled as '${task.status}'.`);
    } catch (err) {
      setTaskError(err instanceof Error ? err.message : String(err));
    }
  }

  async function lookupTask() {
    setTaskError(null);
    try {
      const task = await client.request<A2aTask>(
        `/a2a/tasks/${encodeURIComponent(lookupId.trim())}`,
      );
      upsertTask(task);
    } catch (err) {
      setTaskError(errText(err));
    }
  }

  async function refreshTask(id: string) {
    try {
      upsertTask(await client.request<A2aTask>(`/a2a/tasks/${encodeURIComponent(id)}`));
    } catch (err) {
      setTaskError(errText(err));
    }
  }

  async function cancelTask(id: string) {
    setTaskError(null);
    try {
      upsertTask(await rpc<A2aTask>("tasks/cancel", { id }));
    } catch (err) {
      setTaskError(err instanceof Error ? err.message : String(err));
    }
  }

  async function viewStream(task: A2aTask) {
    setStreamError(null);
    setStreamText(null);
    try {
      const text = await client.requestText(
        task.streamUrl ?? `/a2a/tasks/${encodeURIComponent(task.id)}/stream`,
      );
      setStreamText({ id: task.id, text });
    } catch (err) {
      setStreamError(errText(err));
    }
  }

  return (
    <div>
      <p className="hint">
        MCP servers and the A2A node are governed configuration, not
        code paths: definitions are drafted and published in Studio,
        operational enablement is a separate audited flip, and this
        node's own MCP tools ship disabled until a site publishes the
        registry with a tool enabled.
      </p>
      {notice && <p className="hint" role="status">{notice}</p>}

      <h3>MCP server definitions</h3>
      <ConfigEditorShell<McpServerModel>
        client={client}
        kind={MCP_SERVER_KIND}
        newKeyHint="e.g. external-lab-mcp"
        parse={parseMcpServer}
        serialize={serializeMcpServer}
        empty={emptyMcpServer}
        validate={validateMcpServer}
      >
        {(model, setModel) => <McpServerEditor model={model} setModel={setModel} />}
      </ConfigEditorShell>

      <h3>Published MCP servers</h3>
      {servers.loading ? (
        <Loading label="Loading MCP servers…" />
      ) : servers.error ? (
        <ErrorView error={servers.error} />
      ) : (servers.data?.servers ?? []).length === 0 ? (
        <Empty label="No published MCP server definitions. Publish one above — it still will not be callable until enabled." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Server</th>
              <th>URL</th>
              <th>Allowlist</th>
              <th>State</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {(servers.data?.servers ?? []).map((s) => (
              <tr key={s.key}>
                <td>
                  <strong>{s.key}</strong>
                  {s.name ? (
                    <>
                      <br />
                      <span className="hint">{s.name} · v{s.version}</span>
                    </>
                  ) : null}
                </td>
                <td>{s.url ? <code>{s.url}</code> : "—"}</td>
                <td>
                  {s.toolAllowlist.length > 0
                    ? s.toolAllowlist.join(", ")
                    : "none — no tools callable"}
                </td>
                <td>
                  <StatusBadge tone={s.enabled ? "ok" : "neutral"}>
                    {s.enabled ? "enabled" : "not enabled"}
                  </StatusBadge>
                </td>
                <td>
                  {s.enabled ? (
                    <button
                      type="button"
                      onClick={() =>
                        void act(
                          () =>
                            client.request(`/mcp/servers/${encodeURIComponent(s.key)}/disable`, {
                              method: "POST",
                            }),
                          `MCP server ${s.key} disabled.`,
                        )
                      }
                    >
                      Disable
                    </button>
                  ) : (
                    <button
                      type="button"
                      onClick={() =>
                        void act(
                          () =>
                            client.request(`/mcp/servers/${encodeURIComponent(s.key)}/enable`, {
                              method: "POST",
                            }),
                          `MCP server ${s.key} enabled.`,
                        )
                      }
                    >
                      Enable
                    </button>
                  )}{" "}
                  <button type="button" onClick={() => void viewUpstreamTools(s.key)}>
                    View upstream tools
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {upstreamError && (
        <p className="error-note" role="alert">
          {upstreamError}
        </p>
      )}
      {upstreamTools && (
        <div>
          <h4>Upstream tools — {upstreamTools.server}</h4>
          {upstreamTools.tools.length === 0 ? (
            <Empty label="The upstream server reported no tools." />
          ) : (
            <ul>
              {upstreamTools.tools.map((t, i) => (
                <li key={i}>
                  <code>
                    {typeof t === "object" && t !== null && "name" in t
                      ? String((t as { name: unknown }).name)
                      : JSON.stringify(t)}
                  </code>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <h3>Tool registry (this node as an MCP server)</h3>
      {registry.loading ? (
        <Loading label="Loading tool registry…" />
      ) : registry.error ? (
        <p className="hint">
          No published integration/mcp-tools registry is in effect:{" "}
          {errText(registry.error)}
        </p>
      ) : registryTools.length === 0 ? (
        <Empty label="The published registry declares no tools." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Tool</th>
              <th>Description</th>
              <th>Required capability</th>
              <th>Patient-scoped</th>
              <th>State</th>
            </tr>
          </thead>
          <tbody>
            {registryTools.map((t) => (
              <tr key={t.name}>
                <td><code>{t.name}</code></td>
                <td>{t.description}</td>
                <td><code>{t.requiredCapability}</code></td>
                <td>{t.patientScoped ? "yes" : "no"}</td>
                <td>
                  <StatusBadge tone={t.enabled ? "ok" : "neutral"}>
                    {t.enabled ? "enabled" : "disabled"}
                  </StatusBadge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="hint">
        All seeded tools start disabled. A site enables a tool only by
        publishing a new version of integration/mcp-tools with that
        tool's enabled flag set — there is no per-tool toggle here.
      </p>

      <h3>A2A agent card</h3>
      {card.loading ? (
        <Loading label="Loading agent card…" />
      ) : card.error ? (
        <ErrorView error={card.error} />
      ) : card.data ? (
        <div>
          <p>
            <strong>{card.data.name}</strong> — {card.data.description}
            <br />
            <span className="hint">
              {card.data.url} · version {card.data.version} · A2A protocol{" "}
              {card.data.protocolVersion} · streaming{" "}
              {card.data.capabilities.streaming ? "supported" : "not supported"}
            </span>
          </p>
          {cardSkills.length === 0 ? (
            <p className="hint">
              The card advertises no skills: skills appear only for
              agents that are published AND enabled.
            </p>
          ) : (
            <table className="studio-table">
              <thead>
                <tr>
                  <th>Skill (agent)</th>
                  <th>Description</th>
                  <th>Input modes</th>
                  <th>Output modes</th>
                </tr>
              </thead>
              <tbody>
                {cardSkills.map((s) => (
                  <tr key={s.id}>
                    <td><code>{s.id}</code></td>
                    <td>{s.description}</td>
                    <td>{s.inputModes.join(", ")}</td>
                    <td>{s.outputModes.join(", ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : null}

      <h3>A2A tasks</h3>
      {cardSkills.length === 0 ? (
        <p className="hint">
          Sending a task is unavailable: the agent card has no skills,
          because no agent is published and enabled. Enable an agent
          first — the task form appears here when a skill exists.
        </p>
      ) : (
        <div>
          <p>
            <label>
              Agent{" "}
              <select
                aria-label="A2A agent"
                value={selectedTaskAgent}
                onChange={(e) => setTaskAgent(e.target.value)}
              >
                {cardSkills.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.id}
                  </option>
                ))}
              </select>
            </label>{" "}
            <label>
              Patient reference{" "}
              <input
                aria-label="A2A patient reference"
                value={taskPatientRef}
                onChange={(e) => setTaskPatientRef(e.target.value)}
                placeholder="Patient/…"
              />
            </label>
          </p>
          <div>
            <RichTextEditor
              ariaLabel="A2A message"
              value={taskMessage}
              onChange={setTaskMessage}
              minRows={3}
            />
          </div>
          <p className="hint">Message for the agent</p>
          <p>
            <button
              type="button"
              disabled={!htmlToText(taskMessage).trim() || !selectedTaskAgent}
              onClick={() => void sendTask()}
            >
              Send task (message/stream)
            </button>
          </p>
        </div>
      )}
      <p>
        <label>
          Task ID{" "}
          <input
            aria-label="A2A task ID lookup"
            value={lookupId}
            onChange={(e) => setLookupId(e.target.value)}
            size={40}
          />
        </label>{" "}
        <button
          type="button"
          disabled={!lookupId.trim()}
          onClick={() => void lookupTask()}
        >
          Look up task
        </button>
      </p>
      {taskError && (
        <p className="error-note" role="alert">
          {taskError}
        </p>
      )}
      {tasks.length === 0 ? (
        <Empty label="No A2A tasks in this session yet — send one above or look one up by ID. (The protocol surface has no task-list endpoint; this list tracks this session's tasks.)" />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Task</th>
              <th>Agent</th>
              <th>Status</th>
              <th>Status message</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((t) => (
              <tr key={t.id}>
                <td><code title={t.id}>{t.id.slice(0, 8)}…</code></td>
                <td>{t.agentKey}</td>
                <td>
                  <StatusBadge tone={taskStatusTone(t.status)}>{t.status}</StatusBadge>
                </td>
                <td>{t.statusMessage ?? "—"}</td>
                <td>
                  <button type="button" onClick={() => void refreshTask(t.id)}>
                    Refresh
                  </button>{" "}
                  <button type="button" onClick={() => void viewStream(t)}>
                    View stream
                  </button>{" "}
                  {!TERMINAL_TASK_STATES.includes(t.status) && (
                    <button type="button" onClick={() => void cancelTask(t.id)}>
                      Cancel
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {streamError && (
        <p className="error-note" role="alert">
          {streamError}
        </p>
      )}
      {streamText && (
        <div>
          <h4>Stream replay — task {streamText.id.slice(0, 8)}…</h4>
          <p className="hint">
            The companion SSE stream replays the task's recorded status
            transitions (event: task-status), then the full task
            (event: task):
          </p>
          <pre>{streamText.text}</pre>
        </div>
      )}
    </div>
  );
}
