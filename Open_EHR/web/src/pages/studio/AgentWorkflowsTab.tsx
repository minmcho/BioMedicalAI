import { useState } from "react";
import type { ApiClient } from "../../api/client";
import { ApiError } from "../../api/client";
import type { ConfigKind, StudioConfigObject } from "../../api/types";
import { RichTextEditor } from "../../components/RichTextEditor";
import { htmlToText } from "../../components/richtext";
import { StatusBadge, type StatusTone } from "../../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

// Agent workflows tab (Phase 16, WS-K1) — supervised agent workflows
// (backend/app/agent_workflows.py). A workflow is a governed agentflow
// config whose steps are ONLY agents and human review gates: agents
// draft, a reviewer approves / edits / rejects, and a reject
// TERMINATES the chain (downstream steps are skipped, never run).
// Failures are recorded on the run with the server's reason verbatim —
// nothing is fabricated in place of a failed step.

export interface AgentflowStepDef {
  type: string;
  agentKey?: string;
  role?: string;
  instructions?: string;
}

export interface AgentflowDef {
  key: string;
  version: number;
  status: string;
  description: string | null;
  steps: AgentflowStepDef[];
  valid: boolean;
}

export interface WorkflowStepRecord {
  index: number;
  type: string;
  agent_key: string | null;
  role: string | null;
  instructions: string | null;
  status: string;
  agent_run_id: string | null;
  input: string | null;
  output: string | null;
  review_decision: string | null;
  review_note: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
}

export interface WorkflowRunRecord {
  run_id: string;
  key: string;
  version: number;
  status: string;
  patient_ref: string | null;
  actor: string;
  input: string;
  input_hash: string;
  output: string | null;
  failure_reason: string | null;
  current_step: number | null;
  steps: WorkflowStepRecord[];
  created_at: string | null;
  updated_at: string | null;
}

const AGENTFLOW_KIND = "agentflow" as ConfigKind;

export function runStatusTone(status: string): StatusTone {
  switch (status) {
    case "completed":
      return "ok";
    case "awaiting-review":
      return "warn";
    case "failed":
    case "terminated":
      return "bad";
    case "running":
      return "info";
    default:
      return "neutral";
  }
}

export function runStatusLabel(status: string): string {
  return status === "awaiting-review" ? "Awaiting review" : status;
}

export function stepStatusTone(status: string): StatusTone {
  switch (status) {
    case "completed":
    case "approved":
    case "edited":
      return "ok";
    case "awaiting-review":
      return "warn";
    case "failed":
    case "rejected":
      return "bad";
    case "running":
      return "info";
    default:
      return "neutral";
  }
}

function errText(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err);
}

export function AgentWorkflowsTab({ client }: { client: ApiClient }) {
  const [reloadKey, setReloadKey] = useState(0);
  const flows = useAsync<{ workflows: AgentflowDef[] }>(
    () => client.request("/agent-workflows"),
    [client, reloadKey],
  );
  const runs = useAsync<WorkflowRunRecord[]>(
    () => client.request("/agent-workflows/runs"),
    [client, reloadKey],
  );
  const drafts = useAsync<StudioConfigObject[]>(
    () => client.listConfigs(AGENTFLOW_KIND),
    [client, reloadKey],
  );

  const [notice, setNotice] = useState<string | null>(null);
  const [flowKey, setFlowKey] = useState("");
  const [input, setInput] = useState("");
  const [patientRef, setPatientRef] = useState("");
  const [runError, setRunError] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [reviewNote, setReviewNote] = useState("");
  const [editedOutput, setEditedOutput] = useState("");
  const [reviewError, setReviewError] = useState<string | null>(null);

  if (flows.loading) return <Loading label="Loading agent workflows…" />;
  if (flows.error) return <ErrorView error={flows.error} />;

  const published = flows.data?.workflows ?? [];
  const selectedFlow = flowKey || published[0]?.key || "";
  const runList = runs.data ?? [];
  const selectedRun =
    runList.find((r) => r.run_id === selectedRunId) ?? runList[0] ?? null;
  const parkedStep =
    selectedRun?.status === "awaiting-review"
      ? selectedRun.steps.find((s) => s.status === "awaiting-review")
      : undefined;

  const draftByKey = new Map<string, StudioConfigObject[]>();
  for (const c of drafts.data ?? []) {
    if (c.status !== "draft") continue;
    const list = draftByKey.get(c.key) ?? [];
    list.push(c);
    draftByKey.set(c.key, list);
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

  async function doRun() {
    setRunError(null);
    try {
      const run = await client.request<WorkflowRunRecord>(
        `/agent-workflows/${encodeURIComponent(selectedFlow)}/run`,
        {
          method: "POST",
          body: JSON.stringify({
            input,
            patientRef: patientRef.trim() || undefined,
          }),
        },
      );
      setSelectedRunId(run.run_id);
      setNotice(`Run ${run.run_id.slice(0, 8)}… started — status: ${runStatusLabel(run.status)}.`);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setRunError(errText(err));
    }
  }

  async function doReview(decision: "approve" | "reject" | "edit") {
    if (!selectedRun) return;
    setReviewError(null);
    // Edit with an untouched textarea substitutes the reviewed text
    // itself (the reviewer opened the editor to change it; an empty
    // box never silently sends an empty output — the server 422s).
    let effectiveEdit: string | undefined;
    if (decision === "edit") {
      effectiveEdit = htmlToText(editedOutput).trim()
        ? editedOutput
        : (parkedStep?.input ?? "");
      if (!htmlToText(effectiveEdit).trim()) {
        setReviewError("Edited output is required for an edit decision.");
        return;
      }
    }
    try {
      const updated = await client.request<WorkflowRunRecord>(
        `/agent-workflows/runs/${selectedRun.run_id}/review`,
        {
          method: "POST",
          body: JSON.stringify({
            decision,
            note: reviewNote,
            editedOutput: effectiveEdit,
          }),
        },
      );
      setSelectedRunId(updated.run_id);
      setReviewNote("");
      setNotice(
        decision === "reject"
          ? `Run ${updated.run_id.slice(0, 8)}… terminated by rejection.`
          : `Review recorded (${decision}) — run status: ${runStatusLabel(updated.status)}.`,
      );
      setReloadKey((k) => k + 1);
    } catch (err) {
      setReviewError(errText(err));
    }
  }

  return (
    <div>
      <p className="hint">
        Supervised workflows chain governed agents behind human review
        gates. The step vocabulary is deliberately only <strong>agent</strong>{" "}
        and <strong>review</strong> — no step writes clinical data. A run
        parks at a review step until a reviewer holding the declared role
        approves, edits, or rejects it; rejecting terminates the run and
        downstream agents never run.
      </p>
      {notice && <p className="hint" role="status">{notice}</p>}

      <h3>Workflow definitions (published)</h3>
      {published.length === 0 ? (
        <Empty label="No published agentflow definitions yet. Publish one from a draft below (or the Advanced JSON tab) — drafts never run." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Workflow</th>
              <th>Version</th>
              <th>Steps</th>
              <th>Valid</th>
            </tr>
          </thead>
          <tbody>
            {published.map((f) => (
              <tr key={f.key}>
                <td>
                  <strong>{f.key}</strong>
                  {f.description ? (
                    <>
                      <br />
                      <span className="hint">{f.description}</span>
                    </>
                  ) : null}
                </td>
                <td>v{f.version} (published)</td>
                <td>
                  {f.steps.map((s, i) =>
                    s.type === "review"
                      ? `${i + 1}. review (role: ${s.role ?? "?"})`
                      : `${i + 1}. agent ${s.agentKey ?? "?"}`,
                  ).join(" → ") || "—"}
                </td>
                <td>
                  {f.valid ? (
                    <StatusBadge tone="ok">valid</StatusBadge>
                  ) : (
                    <StatusBadge tone="bad">invalid</StatusBadge>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h4>Draft definitions</h4>
      {drafts.loading ? (
        <Loading label="Loading drafts…" />
      ) : drafts.error ? (
        <p className="error-note" role="alert">
          Draft agentflow configs could not be loaded: {errText(drafts.error)}
        </p>
      ) : draftByKey.size === 0 ? (
        <p className="hint">No draft agentflow definitions.</p>
      ) : (
        <ul>
          {[...draftByKey.entries()].map(([key, versions]) =>
            versions.map((v) => (
              <li key={v.id}>
                <strong>{key}</strong> draft v{v.version}{" "}
                <button
                  type="button"
                  onClick={() =>
                    void act(
                      () => client.publishConfig(AGENTFLOW_KIND, key, v.version),
                      `Published ${key} v${v.version}.`,
                    )
                  }
                >
                  Publish v{v.version}
                </button>
              </li>
            )),
          )}
        </ul>
      )}

      <h3>Run a workflow</h3>
      {published.length === 0 ? (
        <p className="hint">
          Nothing to run: no agentflow definition is published. Publishing
          is a governed Studio action — until then runs are impossible, by
          design.
        </p>
      ) : (
        <div>
          <p>
            <label>
              Workflow{" "}
              <select
                aria-label="Workflow to run"
                value={selectedFlow}
                onChange={(e) => setFlowKey(e.target.value)}
              >
                {published.map((f) => (
                  <option key={f.key} value={f.key}>
                    {f.key}
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
              ariaLabel="Workflow input"
              value={input}
              onChange={setInput}
              minRows={4}
            />
          </div>
          <p className="hint">
            Input for the first step (e.g. the encounter transcript or context)
          </p>
          <p>
            <button
              type="button"
              disabled={!htmlToText(input).trim() || !selectedFlow}
              onClick={() => void doRun()}
            >
              Run workflow
            </button>
          </p>
          {runError && (
            <p className="error-note" role="alert">
              {runError}
            </p>
          )}
        </div>
      )}

      <h3>Workflow runs</h3>
      {runs.loading ? (
        <Loading label="Loading runs…" />
      ) : runs.error ? (
        <ErrorView error={runs.error} />
      ) : runList.length === 0 ? (
        <Empty label="No workflow runs recorded yet." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Run</th>
              <th>Workflow</th>
              <th>Actor</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {runList.map((r) => (
              <tr key={r.run_id}>
                <td>
                  <code title={r.input_hash}>{r.run_id.slice(0, 8)}…</code>
                </td>
                <td>
                  {r.key} v{r.version}
                </td>
                <td>{r.actor}</td>
                <td>
                  <StatusBadge tone={runStatusTone(r.status)}>
                    {runStatusLabel(r.status)}
                  </StatusBadge>
                </td>
                <td>
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedRunId(r.run_id);
                      setReviewError(null);
                      setEditedOutput("");
                      setReviewNote("");
                    }}
                  >
                    View
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selectedRun && (
        <div>
          <h4>
            Run {selectedRun.run_id.slice(0, 8)}… — {selectedRun.key} v
            {selectedRun.version}{" "}
            <StatusBadge tone={runStatusTone(selectedRun.status)}>
              {runStatusLabel(selectedRun.status)}
            </StatusBadge>
          </h4>
          {selectedRun.status === "failed" && (
            <p className="error-note" role="alert">
              Run failed: {selectedRun.failure_reason ?? "no reason recorded"}
            </p>
          )}
          {selectedRun.status === "terminated" && (
            <p className="error-note" role="alert">
              Terminated — a reviewer rejected a step output. Downstream
              steps were skipped and their agents were never invoked.
            </p>
          )}
          {selectedRun.output && (
            <div>
              <h5>Final output (draft)</h5>
              <pre>{selectedRun.output}</pre>
            </div>
          )}
          <table className="studio-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Step</th>
                <th>Status</th>
                <th>Output</th>
                <th>Review</th>
              </tr>
            </thead>
            <tbody>
              {selectedRun.steps.map((s) => (
                <tr key={s.index}>
                  <td>{s.index + 1}</td>
                  <td>
                    {s.type === "review" ? (
                      <>
                        review (role: {s.role ?? "?"})
                        {s.instructions ? (
                          <>
                            <br />
                            <span className="hint">{s.instructions}</span>
                          </>
                        ) : null}
                      </>
                    ) : (
                      <>agent {s.agent_key ?? "?"}</>
                    )}
                  </td>
                  <td>
                    <StatusBadge tone={stepStatusTone(s.status)}>
                      {s.status === "awaiting-review" ? "Awaiting review" : s.status}
                    </StatusBadge>
                  </td>
                  <td>
                    {s.output ? <pre>{s.output}</pre> : (s.input ?? "—")}
                  </td>
                  <td>
                    {s.review_decision
                      ? `${s.review_decision} by ${s.reviewed_by ?? "?"}${s.review_note ? ` — ${s.review_note}` : ""}`
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {parkedStep && selectedRun.status === "awaiting-review" && (
            <div>
              <h5>
                Review step {parkedStep.index + 1} — Awaiting review (role:{" "}
                {parkedStep.role ?? "?"})
              </h5>
              <p className="hint">
                The text below is what the previous step produced. Approve
                passes it on unchanged, Edit &amp; resume substitutes your
                edited text, and Reject terminates the run.
              </p>
              <pre>{parkedStep.input ?? ""}</pre>
              <p>
                <label>
                  Review note{" "}
                  <input
                    aria-label="Review note"
                    value={reviewNote}
                    onChange={(e) => setReviewNote(e.target.value)}
                    size={48}
                    placeholder="Why this decision (recorded in the audit)"
                  />
                </label>
              </p>
              <div>
                <RichTextEditor
                  ariaLabel="Edited output"
                  value={editedOutput}
                  onChange={setEditedOutput}
                  minRows={4}
                />
              </div>
              <p className="hint">
                Edited output — used only by Edit &amp; resume (defaults to
                the step text when opened)
              </p>
              {reviewError && (
                <p className="error-note" role="alert">
                  {reviewError}
                </p>
              )}
              <p>
                <button
                  type="button"
                  onClick={() => void doReview("approve")}
                >
                  Approve
                </button>{" "}
                <button
                  type="button"
                  onClick={() => void doReview("edit")}
                >
                  Edit &amp; resume
                </button>{" "}
                <button
                  type="button"
                  onClick={() => void doReview("reject")}
                >
                  Reject
                </button>
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
