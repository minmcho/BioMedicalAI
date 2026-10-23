import { Fragment, useState } from "react";
import type { ApiClient } from "../../api/client";
import { ApiError } from "../../api/client";
import type {
  AgentRunRecord,
  AgentsResponse,
  CaiAgentGovernanceStatus,
  StudioConfigObject,
} from "../../api/types";
import { RichTextEditor } from "../../components/RichTextEditor";
import { htmlToText } from "../../components/richtext";
import { StatusBadge } from "../../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";
import { AgentDisclosure } from "./ValidationTab";

function errText(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err);
}

/** Lazily loaded governance status for one agent (C-AI): risk tier,
 *  validation-record state, validation-statement state and the
 *  runtime gate, exactly as the server reports them. */
function AgentGovernancePanel({
  client,
  agentKey,
}: {
  client: ApiClient;
  agentKey: string;
}) {
  const status = useAsync<CaiAgentGovernanceStatus>(
    () =>
      client.request(
        `/ai-governance/agents/${encodeURIComponent(agentKey)}/status`,
      ),
    [client, agentKey],
  );
  if (status.loading) return <Loading label="Loading governance status…" />;
  if (status.error) return <ErrorView error={status.error} />;
  const s = status.data;
  if (!s) return <Empty label="No governance status returned." />;
  const gov = (s.governance ?? {}) as Record<string, unknown>;
  const recordState =
    s.validation?.status ??
    (typeof gov.recordStatus === "string" ? gov.recordStatus : null);
  return (
    <div>
      <p role="status">
        Governance: tier {s.tier ?? "not recorded"} ·{" "}
        {s.killSwitchActive ? "kill switch ACTIVE · " : ""}
        {s.enabled ? "enabled" : "not enabled"} · statement:{" "}
        {s.statement?.status ?? "missing"}
        {s.statement?.statementId ? ` (${s.statement.statementId})` : ""}
        {s.statement?.reason ? ` — ${s.statement.reason}` : ""} · record:{" "}
        {recordState ?? "missing"}
        {s.validation?.reason ? ` — ${s.validation.reason}` : ""}
      </p>
      {s.runtimeGate && (
        <p className="hint">
          Runtime gate: {s.runtimeGate.allowed ? "allowed" : "refused"} —{" "}
          {s.runtimeGate.reason}
        </p>
      )}
    </div>
  );
}

/** "Report output" (AI-G9): file an agent incident against one run.
 *  The created Safety event id is shown back to the reporter. */
function RunReportForm({
  client,
  runId,
}: {
  client: ApiClient;
  runId: string;
}) {
  const [description, setDescription] = useState("");
  const [harmLevel, setHarmLevel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  async function submit() {
    setError(null);
    setResult(null);
    try {
      const out = await client.request<{
        eventId: string;
        linkedEventIds?: string[];
      }>(`/ai-governance/runs/${encodeURIComponent(runId)}/report`, {
        method: "POST",
        body: JSON.stringify({
          description: description.trim(),
          ...(harmLevel ? { harmLevel } : {}),
        }),
      });
      setResult(
        `Report filed: event ${out.eventId}` +
          (out.linkedEventIds && out.linkedEventIds.length > 0
            ? ` (also linked: ${out.linkedEventIds.join(", ")})`
            : ""),
      );
    } catch (err) {
      setError(errText(err));
    }
  }

  return (
    <div>
      <p>
        <label>
          What is wrong with this output?{" "}
          <input
            aria-label={`Report description for run ${runId}`}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            size={56}
          />
        </label>{" "}
        <label>
          Harm level (optional){" "}
          <select
            aria-label={`Report harm level for run ${runId}`}
            value={harmLevel}
            onChange={(e) => setHarmLevel(e.target.value)}
          >
            <option value="">not specified</option>
            <option value="none">none</option>
            <option value="minor">minor</option>
            <option value="moderate">moderate</option>
            <option value="severe">severe</option>
          </select>
        </label>{" "}
        <button
          type="button"
          disabled={!description.trim()}
          onClick={() => void submit()}
        >
          File report
        </button>
      </p>
      {error && (
        <p className="error-note" role="alert">
          {error}
        </p>
      )}
      {result && (
        <p className="hint" role="status">
          {result}
        </p>
      )}
    </div>
  );
}

/** Agents (ADK) tab — Studio-managed multimodal agents (Phase 10,
 *  Track A). An agent is governed config (kind=agent): draft →
 *  publish (versions immutable) → separately ENABLE before it can
 *  run, mirroring the pack lifecycle. Runs are drafted by the model
 *  and disposed by a human; with no model route configured the
 *  runtime answers an honest 503 and this tab shows it verbatim —
 *  a fabricated answer is never presented as a run. */
export function AdkTab({ client }: { client: ApiClient }) {
  const [reloadKey, setReloadKey] = useState(0);
  const configs = useAsync<StudioConfigObject[]>(
    () => client.listConfigs("agent"),
    [client, reloadKey],
  );
  const roster = useAsync<AgentsResponse>(
    () => client.request<AgentsResponse>("/agents"),
    [client, reloadKey],
  );
  const runs = useAsync<AgentRunRecord[]>(
    () => client.request<AgentRunRecord[]>("/agents/runs"),
    [client, reloadKey],
  );
  const [notice, setNotice] = useState<string | null>(null);
  const [runAgent, setRunAgent] = useState("");
  const [prompt, setPrompt] = useState("");
  const [patientRef, setPatientRef] = useState("");
  const [runOutput, setRunOutput] = useState<string | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [govOpenKey, setGovOpenKey] = useState<string | null>(null);
  const [disclosureKey, setDisclosureKey] = useState<string | null>(null);
  const [reportRunId, setReportRunId] = useState<string | null>(null);

  if (configs.loading || roster.loading) return <Loading label="Loading agents…" />;
  if (configs.error) return <ErrorView error={configs.error} />;
  if (roster.error) return <ErrorView error={roster.error} />;

  const all = configs.data ?? [];
  const byKey = new Map<string, StudioConfigObject[]>();
  for (const c of all) {
    const list = byKey.get(c.key) ?? [];
    list.push(c);
    byKey.set(c.key, list);
  }
  const enabledByKey = new Map(
    (roster.data?.agents ?? []).map((a) => [a.key, a]),
  );
  const enabledKeys = (roster.data?.agents ?? [])
    .filter((a) => a.enabled)
    .map((a) => a.key);
  const selectedAgent = runAgent || enabledKeys[0] || "";

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

  async function doRun() {
    setRunOutput(null);
    setRunError(null);
    try {
      const run = await client.request<AgentRunRecord>(
        `/agents/${encodeURIComponent(selectedAgent)}/run`,
        {
          method: "POST",
          body: JSON.stringify({
            prompt,
            patient_ref: patientRef.trim() || null,
          }),
        },
      );
      setRunOutput(run.output);
      setReloadKey((k) => k + 1);
    } catch (err) {
      // The honest-off states (runtime down, no model configured)
      // arrive here as the server's own words — shown verbatim.
      setRunError(err instanceof ApiError ? err.message : String(err));
    }
  }

  return (
    <div>
      <p className="hint">
        Agents are configuration, not code: model route, system prompt, tool
        allow-list and modalities are a versioned Studio config. Publishing
        makes a version immutable; a separate enable step makes it runnable.
        Every run is written to the hash-chained audit with its input hash,
        and its output stays a <strong>draft</strong> until a human accepts
        it. Agents never act on their own.
      </p>
      {roster.data && !roster.data.runtime.reachable && (
        <p className="error-note" role="alert">
          Agent runtime unreachable at {roster.data.runtime.url}
          {roster.data.runtime.detail ? ` — ${roster.data.runtime.detail}` : ""}.
          Runs will return an honest “runtime unavailable” error; nothing is
          fabricated in its place.
        </p>
      )}
      {notice && <p className="hint" role="status">{notice}</p>}

      <h3>Agent definitions</h3>
      {byKey.size === 0 ? (
        <Empty label="No agent configs yet." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Agent</th>
              <th>Versions</th>
              <th>Model route</th>
              <th>State</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {[...byKey.entries()].map(([key, versions]) => {
              const sorted = [...versions].sort((a, b) => b.version - a.version);
              const latest = sorted[0];
              const published = sorted.find((v) => v.status === "published");
              const draft = sorted.find((v) => v.status === "draft");
              const rosterEntry = enabledByKey.get(key);
              const model = String(
                (published ?? latest).payload.model ?? "",
              );
              const riskTier =
                rosterEntry?.risk_tier ?? rosterEntry?.riskTier ?? null;
              const govSummary = rosterEntry?.governance ?? null;
              return (
                <Fragment key={key}>
                <tr>
                  <td>
                    <strong>{key}</strong>
                    <br />
                    <span className="hint">
                      {String((published ?? latest).payload.description ?? "")}
                    </span>
                    <br />
                    {riskTier ? (
                      <StatusBadge tone="warn">Risk tier {riskTier}</StatusBadge>
                    ) : (
                      <span className="hint">
                        Risk tier not on the roster — load Governance status to see it
                      </span>
                    )}
                    {govSummary && (
                      <span className="hint">
                        {" "}· statement: {govSummary.statementStatus ?? "missing"}
                        {" "}· record: {govSummary.recordStatus ?? "missing"}
                      </span>
                    )}
                  </td>
                  <td>
                    {sorted.map((v) => `v${v.version} (${v.status})`).join(", ")}
                  </td>
                  <td>
                    {model ? (
                      <code>{model}</code>
                    ) : (
                      <span className="hint">no model route — runs are honestly off</span>
                    )}
                  </td>
                  <td>
                    {published
                      ? rosterEntry?.enabled
                        ? "published · enabled"
                        : "published · not enabled"
                      : "draft only"}
                  </td>
                  <td>
                    {draft && (
                      <button
                        type="button"
                        onClick={() =>
                          void act(
                            () => client.publishConfig("agent", key, draft.version),
                            `Published ${key} v${draft.version}.`,
                          )
                        }
                      >
                        Publish v{draft.version}
                      </button>
                    )}{" "}
                    {published && !rosterEntry?.enabled && (
                      <button
                        type="button"
                        onClick={() =>
                          void act(
                            () =>
                              client.request(`/agents/${encodeURIComponent(key)}/enable`, {
                                method: "POST",
                              }),
                            `Enabled ${key}.`,
                          )
                        }
                      >
                        Enable
                      </button>
                    )}{" "}
                    {published && rosterEntry?.enabled && (
                      <button
                        type="button"
                        onClick={() =>
                          void act(
                            () =>
                              client.request(`/agents/${encodeURIComponent(key)}/disable`, {
                                method: "POST",
                              }),
                            `Disabled ${key}.`,
                          )
                        }
                      >
                        Disable
                      </button>
                    )}{" "}
                    <button
                      type="button"
                      onClick={() =>
                        setGovOpenKey(govOpenKey === key ? null : key)
                      }
                    >
                      Governance status
                    </button>{" "}
                    <button
                      type="button"
                      onClick={() =>
                        setDisclosureKey(disclosureKey === key ? null : key)
                      }
                    >
                      Source attributes
                    </button>
                  </td>
                </tr>
                {govOpenKey === key && (
                  <tr>
                    <td colSpan={5}>
                      <AgentGovernancePanel client={client} agentKey={key} />
                    </td>
                  </tr>
                )}
                {disclosureKey === key && (
                  <tr>
                    <td colSpan={5}>
                      <h4>Source attributes — {key}</h4>
                      <AgentDisclosure client={client} agentKey={key} />
                    </td>
                  </tr>
                )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}

      <h3>Run an agent</h3>
      {enabledKeys.length === 0 ? (
        <p className="hint">
          No agent is enabled. Publish an agent config and enable it above —
          until then there is nothing to run, by design.
        </p>
      ) : (
        <div>
          <p>
            <label>
              Agent{" "}
              <select
                aria-label="Agent to run"
                value={selectedAgent}
                onChange={(e) => setRunAgent(e.target.value)}
              >
                {enabledKeys.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </label>{" "}
            <label>
              Patient reference{" "}
              <input
                aria-label="Patient reference"
                value={patientRef}
                onChange={(e) => setPatientRef(e.target.value)}
                placeholder="Patient/…"
              />
            </label>
          </p>
          <div>
            <RichTextEditor
              ariaLabel="Prompt"
              value={prompt}
              onChange={setPrompt}
              minRows={4}
            />
          </div>
          <p className="hint">What should the agent draft?</p>
          <p>
            <button
              type="button"
              disabled={!htmlToText(prompt).trim()}
              onClick={() => void doRun()}
            >
              Run agent
            </button>
          </p>
          {runError && (
            <p className="error-note" role="alert">
              {runError}
            </p>
          )}
          {runOutput && (
            <div>
              <h4>Draft output (pending human disposition)</h4>
              <pre>{runOutput}</pre>
            </div>
          )}
        </div>
      )}

      <h3>Recent runs</h3>
      {runs.loading ? (
        <Loading label="Loading runs…" />
      ) : (runs.data ?? []).length === 0 ? (
        <Empty label="No agent runs recorded yet." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Run</th>
              <th>Agent</th>
              <th>Actor</th>
              <th>Disposition</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {(runs.data ?? []).map((r) => (
              <Fragment key={r.run_id}>
              <tr>
                <td>
                  <code title={r.input_hash}>{r.run_id.slice(0, 8)}…</code>
                </td>
                <td>
                  {r.agent} v{r.version}
                </td>
                <td>{r.actor}</td>
                <td>
                  {r.disposition}
                  {r.disposition_by ? ` by ${r.disposition_by}` : ""}
                </td>
                <td>
                  {r.disposition === "pending" && (
                    <>
                      <button
                        type="button"
                        onClick={() =>
                          void act(
                            () =>
                              client.request(
                                `/agents/runs/${r.run_id}/disposition`,
                                {
                                  method: "POST",
                                  body: JSON.stringify({ disposition: "accepted" }),
                                },
                              ),
                            `Run ${r.run_id.slice(0, 8)} accepted.`,
                          )
                        }
                      >
                        Accept
                      </button>{" "}
                      <button
                        type="button"
                        onClick={() =>
                          void act(
                            () =>
                              client.request(
                                `/agents/runs/${r.run_id}/disposition`,
                                {
                                  method: "POST",
                                  body: JSON.stringify({ disposition: "rejected" }),
                                },
                              ),
                            `Run ${r.run_id.slice(0, 8)} rejected.`,
                          )
                        }
                      >
                        Reject
                      </button>
                    </>
                  )}{" "}
                  <button
                    type="button"
                    onClick={() =>
                      setReportRunId(reportRunId === r.run_id ? null : r.run_id)
                    }
                  >
                    Report output
                  </button>
                </td>
              </tr>
              {reportRunId === r.run_id && (
                <tr>
                  <td colSpan={5}>
                    <RunReportForm client={client} runId={r.run_id} />
                  </td>
                </tr>
              )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
