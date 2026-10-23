import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import { ApiError } from "../../api/client";
import type { StudioConfigObject } from "../../api/types";
import { AgentWorkflowsTab } from "./AgentWorkflowsTab";
import { InnovationTab } from "./InnovationTab";
import { KnowledgeTab } from "./KnowledgeTab";
import { McpA2aTab } from "./McpA2aTab";
import { ValidationTab } from "./ValidationTab";

// Phase 16 Studio tabs (WS-K1): supervised agent workflows, the
// validation harness, MCP + A2A, knowledge/RAG and the innovation
// pipeline. Stub clients pin the CONTRACT shapes from the backend
// modules; the honesty paths (parked review, reject terminates, live
// 503 verbatim, RAG answer honestly off) are asserted exactly.

afterEach(cleanup);

function cfg(
  kind: string,
  key: string,
  payload: Record<string, unknown>,
  version = 1,
  status: StudioConfigObject["status"] = "draft",
): StudioConfigObject {
  return {
    id: version * 7 + key.length,
    kind: kind as StudioConfigObject["kind"],
    key, version, status, payload,
    created_by: "system", updated_by: "system",
    created_at: "2026-10-19T00:00:00Z", updated_at: "2026-10-19T00:00:00Z",
  };
}

// ---------------------------------------------------------------------
// Agent workflows
// ---------------------------------------------------------------------

const FLOW = {
  key: "supervised-note-drafting",
  version: 1,
  status: "published",
  description: "Scribe drafts, clinician reviews, coder drafts.",
  steps: [
    { type: "agent", agentKey: "ambient-scribe" },
    { type: "review", role: "clinician", instructions: "Review the draft." },
    { type: "agent", agentKey: "note-coding-assistant" },
  ],
  valid: true,
};

function stepRecord(overrides: Record<string, unknown>) {
  return {
    index: 0, type: "agent", agent_key: null, role: null,
    instructions: null, status: "pending", agent_run_id: null,
    input: null, output: null, review_decision: null,
    review_note: null, reviewed_by: null, reviewed_at: null,
    ...overrides,
  };
}

const PARKED_RUN = {
  run_id: "run-parked-1234",
  key: "supervised-note-drafting", version: 1,
  status: "awaiting-review", patient_ref: null, actor: "dev-clinician",
  input: "Encounter transcript (synthetic).", input_hash: "ab".repeat(32),
  output: null, failure_reason: null, current_step: 1,
  steps: [
    stepRecord({ index: 0, type: "agent", agent_key: "ambient-scribe",
      status: "completed", agent_run_id: "ar-1",
      input: "Encounter transcript (synthetic).", output: "Draft note text." }),
    stepRecord({ index: 1, type: "review", role: "clinician",
      instructions: "Review the draft.", status: "awaiting-review",
      input: "Draft note text." }),
    stepRecord({ index: 2, type: "agent", agent_key: "note-coding-assistant" }),
  ],
  created_at: "2026-10-19T00:00:00Z", updated_at: "2026-10-19T00:00:00Z",
};

const COMPLETED_RUN = {
  ...PARKED_RUN,
  status: "completed",
  output: "Coding suggestions (draft).",
  steps: [
    PARKED_RUN.steps[0],
    stepRecord({ index: 1, type: "review", role: "clinician",
      instructions: "Review the draft.", status: "approved",
      input: "Draft note text.", output: "Draft note text.",
      review_decision: "approve", review_note: "Facts check out",
      reviewed_by: "dev-clinician" }),
    stepRecord({ index: 2, type: "agent", agent_key: "note-coding-assistant",
      status: "completed", agent_run_id: "ar-2",
      input: "Draft note text.", output: "Coding suggestions (draft)." }),
  ],
};

const TERMINATED_RUN = {
  ...PARKED_RUN,
  status: "terminated",
  steps: [
    PARKED_RUN.steps[0],
    stepRecord({ index: 1, type: "review", role: "clinician",
      instructions: "Review the draft.", status: "rejected",
      input: "Draft note text.", review_decision: "reject",
      review_note: "Unsupported finding", reviewed_by: "dev-clinician" }),
    stepRecord({ index: 2, type: "agent", agent_key: "note-coding-assistant",
      status: "skipped" }),
  ],
};

const FAILED_RUN = {
  ...PARKED_RUN,
  run_id: "run-failed-9876",
  status: "failed",
  failure_reason:
    "Agent runtime unavailable at http://127.0.0.1:8100 — start the ADK service. No answer was generated.",
  steps: [
    stepRecord({ index: 0, type: "agent", agent_key: "ambient-scribe",
      status: "failed", input: "Encounter transcript (synthetic)." }),
    stepRecord({ index: 1, type: "review", role: "clinician", status: "skipped" }),
    stepRecord({ index: 2, type: "agent", agent_key: "note-coding-assistant", status: "skipped" }),
  ],
};

describe("AgentWorkflowsTab", () => {
  it("publishes a draft, runs a flow, parks at review, and approves to completion", async () => {
    const calls: Array<{ path: string; body?: string }> = [];
    let runsList: unknown[] = [];
    const publishConfig = vi.fn(async () => ({}));
    const client = {
      listConfigs: vi.fn(async () => [
        cfg("agentflow", "supervised-note-drafting", FLOW, 1, "draft"),
      ]),
      publishConfig,
      request: vi.fn(async (path: string, init?: RequestInit) => {
        calls.push({ path, body: init?.body as string | undefined });
        if (path === "/agent-workflows") return { workflows: [FLOW] };
        if (path === "/agent-workflows/runs") return runsList;
        if (path === "/agent-workflows/supervised-note-drafting/run") {
          runsList = [PARKED_RUN];
          return PARKED_RUN;
        }
        if (path === "/agent-workflows/runs/run-parked-1234/review") {
          runsList = [COMPLETED_RUN];
          return COMPLETED_RUN;
        }
        throw new Error(`unexpected ${path}`);
      }),
    } as unknown as ApiClient;
    render(<AgentWorkflowsTab client={client} />);
    expect((await screen.findAllByText("supervised-note-drafting")).length)
      .toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/1\. agent ambient-scribe/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Publish v1" }));
    await waitFor(() =>
      expect(publishConfig).toHaveBeenCalledWith(
        "agentflow", "supervised-note-drafting", 1));
    // The publish triggers a full reload (the tab gates on the flows
    // load); wait for the post-reload render before touching the form.
    expect(await screen.findByText("Published supervised-note-drafting v1.")).toBeTruthy();

    const input = screen.getByLabelText("Workflow input");
    // WS2b: the workflow input is a rich-text editor (contentEditable)
    // — drive it the way a browser reports edits (innerHTML + input).
    input.innerHTML = "Encounter transcript (synthetic).";
    fireEvent.input(input);
    fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
    // The run parks: park state is visible as text, with the reviewed
    // text on screen.
    expect((await screen.findAllByText("Awaiting review")).length)
      .toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Draft note text.").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/Review step 2 — Awaiting review \(role: clinician\)/)).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Review note"), {
      target: { value: "Facts check out" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() =>
      expect(calls.some(
        (c) => c.path === "/agent-workflows/runs/run-parked-1234/review" &&
               (c.body ?? "").includes('"approve"') &&
               (c.body ?? "").includes("Facts check out"))).toBe(true));
    expect((await screen.findAllByText("Coding suggestions (draft).")).length)
      .toBeGreaterThanOrEqual(1);
    expect(screen.queryByText(/Terminated — a reviewer rejected/)).toBeNull();
  });

  it("reject terminates the run and shows downstream steps skipped", async () => {
    let runsList: unknown[] = [PARKED_RUN];
    const client = {
      listConfigs: vi.fn(async () => []),
      publishConfig: vi.fn(async () => ({})),
      request: vi.fn(async (path: string) => {
        if (path === "/agent-workflows") return { workflows: [FLOW] };
        if (path === "/agent-workflows/runs") return runsList;
        if (path === "/agent-workflows/runs/run-parked-1234/review") {
          runsList = [TERMINATED_RUN];
          return TERMINATED_RUN;
        }
        throw new Error(`unexpected ${path}`);
      }),
    } as unknown as ApiClient;
    render(<AgentWorkflowsTab client={client} />);
    expect((await screen.findAllByText("Awaiting review")).length)
      .toBeGreaterThanOrEqual(1);
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(await screen.findByText(/Terminated — a reviewer rejected a step output/)).toBeTruthy();
    expect(screen.getByText("skipped")).toBeTruthy();
    expect(screen.getByText(/reject by dev-clinician — Unsupported finding/)).toBeTruthy();
  });

  it("renders a failed run's honest reason verbatim", async () => {
    const client = {
      listConfigs: vi.fn(async () => []),
      publishConfig: vi.fn(async () => ({})),
      request: vi.fn(async (path: string) => {
        if (path === "/agent-workflows") return { workflows: [FLOW] };
        if (path === "/agent-workflows/runs") return [FAILED_RUN];
        throw new Error(`unexpected ${path}`);
      }),
    } as unknown as ApiClient;
    render(<AgentWorkflowsTab client={client} />);
    expect(await screen.findByText(/Agent runtime unavailable at http:\/\/127\.0\.0\.1:8100/)).toBeTruthy();
    expect(screen.getByText(/No answer was generated/)).toBeTruthy();
  });
});

// ---------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------

const VAL_CONFIG = {
  requireValidationForEnable: true, validationValidityDays: 180,
  sliceDisparityThreshold: 0.2, driftRejectRateThreshold: 0.2,
  driftOutputLengthChangeThreshold: 0.5,
  notes: "Seeded governance defaults.",
};

const VAL_STATUS = {
  agentKey: "note-coding-assistant", status: "current", required: true,
  publishedVersion: 1, modelRoute: "",
  record: {
    id: 1, recordKey: "rec-1", agentKey: "note-coding-assistant",
    agentConfigVersion: 1, modelRoute: "", datasetKey: "baseline-seeded-agents",
    passRate: 1, issuedAt: "2026-10-19T00:00:00Z",
    expiresAt: "2027-04-17T00:00:00Z", issuedBy: "system",
  },
  reason: "Validation record rec-1 is current until 2027-04-17T00:00:00Z",
};

const VAL_DATASET = {
  id: 1, key: "baseline-seeded-agents", name: "Baseline seeded agents",
  description: "Synthetic baseline", caseCount: 2,
  casesHash: "f".repeat(64), createdBy: "system",
  createdAt: "2026-10-19T00:00:00Z",
};

function validationClient(runImpl?: (path: string, init?: RequestInit) => unknown) {
  const posts: Array<{ path: string; body?: string }> = [];
  const client = {
    request: vi.fn(async (path: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posts.push({ path, body: init.body as string | undefined });
        if (runImpl) return runImpl(path, init);
        throw new Error(`unexpected POST ${path}`);
      }
      if (path === "/agent-validation/config") return VAL_CONFIG;
      if (path === "/agent-validation/datasets") return { items: [VAL_DATASET], total: 1 };
      if (path === "/agent-validation/runs") {
        return {
          items: [{
            id: 1, runKey: "rk-1", datasetKey: "baseline-seeded-agents",
            agentKey: "note-coding-assistant", agentConfigVersion: 1,
            modelRoute: "", mode: "recorded", totalCases: 2, passedCases: 2,
            passRate: 1, disparityFlags: [], actor: "system",
            createdAt: "2026-10-19T00:00:00Z",
          }],
          total: 1,
        };
      }
      if (path === "/agent-validation/validations") {
        return { items: [VAL_STATUS.record], total: 1 };
      }
      if (path === "/agent-validation/change-control") {
        return {
          items: [{
            id: 1, recordKey: "cc-1", agentKey: "note-coding-assistant",
            fromVersion: 0, toVersion: 1,
            intendedChange: "Grandfathered baseline for the seeded v1",
            validationPlan: "baseline-seeded-agents", resultRef: "",
            status: "approved", approver: "system", createdBy: "system",
            createdAt: "2026-10-19T00:00:00Z", approvedAt: "2026-10-19T00:00:00Z",
          }],
          total: 1,
        };
      }
      if (path === "/agent-validation/exceptions") {
        return {
          items: [{
            agentKey: "rogue-agent", version: 2, modelRoute: "",
            reason: "Published agent 'rogue-agent' v2 has no change-control record; a version change without PCCP change control is an exception under BD-41",
          }],
          total: 1,
        };
      }
      if (path === "/agents") {
        return { agents: [{ key: "note-coding-assistant", version: 1, enabled: false }] };
      }
      if (path === "/agent-validation/agents/note-coding-assistant/status") return VAL_STATUS;
      throw new Error(`unexpected ${path}`);
    }),
  } as unknown as ApiClient;
  return { client, posts };
}

describe("ValidationTab", () => {
  it("renders per-agent status, datasets, records and exceptions", async () => {
    const { client } = validationClient();
    render(<ValidationTab client={client} />);
    expect((await screen.findAllByText("baseline-seeded-agents")).length)
      .toBeGreaterThanOrEqual(1);
    expect(await screen.findByText("current")).toBeTruthy();
    expect(screen.getByText(/Validation record rec-1 is current until/)).toBeTruthy();
    expect(screen.getByText(/validity 180 days/)).toBeTruthy();
    expect(screen.getAllByText("100%").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/Published agent 'rogue-agent' v2 has no change-control record/)).toBeTruthy();
    expect(screen.getByText("exception")).toBeTruthy();
  });

  it("shows the client-side JSON error and never posts a malformed dataset", async () => {
    const { client, posts } = validationClient();
    render(<ValidationTab client={client} />);
    expect((await screen.findAllByText("baseline-seeded-agents")).length)
      .toBeGreaterThanOrEqual(1);
    fireEvent.change(screen.getByLabelText("Dataset cases JSON"), {
      target: { value: "{not json" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create dataset" }));
    expect(await screen.findByText(/Cases are not valid JSON/)).toBeTruthy();
    expect(posts.length).toBe(0);
  });

  it("surfaces a live run's honest 503 verbatim and records nothing", async () => {
    const { client } = validationClient((path) => {
      if (path === "/agent-validation/datasets/1/run") {
        throw new ApiError(
          503,
          "No model configured: set a model on the agent config or AGENT_MODEL on the ADK runtime. No answer was generated.",
        );
      }
      throw new Error(`unexpected POST ${path}`);
    });
    render(<ValidationTab client={client} />);
    expect((await screen.findAllByText("baseline-seeded-agents")).length)
      .toBeGreaterThanOrEqual(1);
    fireEvent.change(screen.getByLabelText("Run mode"), { target: { value: "live" } });
    fireEvent.click(screen.getByRole("button", { name: "Run dataset" }));
    expect(await screen.findByText(/No model configured: set a model on the agent config/)).toBeTruthy();
    expect(screen.getByText(/No answer was generated/)).toBeTruthy();
  });
});

// ---------------------------------------------------------------------
// MCP + A2A
// ---------------------------------------------------------------------

const MCP_TOOLS_CFG = cfg("integration", "mcp-tools", {
  tools: [
    { name: "patient_summary", description: "Read-only patient summary",
      requiredCapability: "notes.read", patientScoped: true, enabled: false },
    { name: "lab_results_recent", description: "Recent lab results",
      requiredCapability: "lab.read", patientScoped: true, enabled: false },
  ],
}, 1, "published");

const AGENT_CARD = {
  name: "OpenCare", description: "OpenCare A2A node",
  url: "http://127.0.0.1:8000/a2a/rpc", version: "1.0.0",
  protocolVersion: "0.3.0",
  capabilities: { streaming: true, pushNotifications: false },
  skills: [{
    id: "note-coding-assistant", name: "note-coding-assistant",
    description: "Drafts coding suggestions from a reviewed note",
    inputModes: ["text"], outputModes: ["text"],
  }],
};

const A2A_TASK = {
  id: "task-abcdef123456", contextId: "task-abcdef123456", kind: "task",
  agentKey: "note-coding-assistant", status: "completed",
  statusMessage: "Draft recorded for disposition", patientRef: null,
  actor: "dev-clinician", onBehalfOf: null, history: [], artifacts: [],
  agentRunId: "ar-9", createdAt: "2026-10-19T00:00:00Z",
  updatedAt: "2026-10-19T00:00:00Z",
  streamUrl: "/a2a/tasks/task-abcdef123456/stream",
};

describe("McpA2aTab", () => {
  it("renders servers, the disabled-by-default registry, the card, and runs a task lifecycle", async () => {
    const calls: string[] = [];
    const client = {
      listConfigs: vi.fn(async () => []),
      createConfigDraft: vi.fn(async () => ({})),
      updateConfigDraft: vi.fn(async () => ({})),
      publishConfig: vi.fn(async () => ({})),
      disableConfig: vi.fn(async () => ({})),
      rollbackConfig: vi.fn(async () => ({})),
      effectiveConfig: vi.fn(async () => MCP_TOOLS_CFG),
      requestText: vi.fn(async () =>
        ": opencare a2a task stream\n\nevent: task-status\ndata: {\"id\": \"task-abcdef123456\", \"status\": \"completed\"}\n\n"),
      request: vi.fn(async (path: string, init?: RequestInit) => {
        calls.push(`${init?.method ?? "GET"} ${path}`);
        if (path === "/mcp/servers") {
          return {
            servers: [{
              key: "ext-lab", version: 1, name: "External lab MCP",
              transport: "http", url: "http://127.0.0.1:9000/mcp",
              toolAllowlist: ["lookup"], tools: [{ name: "lookup" }],
              enabled: false,
            }],
            total: 1,
          };
        }
        if (path === "/mcp/servers/ext-lab/enable") return {};
        if (path === "/.well-known/agent-card.json") return AGENT_CARD;
        if (path === "/a2a/rpc") {
          const body = JSON.parse(init?.body as string) as { method: string };
          if (body.method === "message/stream") {
            return { jsonrpc: "2.0", id: 1, result: A2A_TASK };
          }
          throw new Error(`unexpected rpc ${body.method}`);
        }
        if (path === "/a2a/tasks/task-abcdef123456") return A2A_TASK;
        throw new Error(`unexpected ${path}`);
      }),
    } as unknown as ApiClient;
    render(<McpA2aTab client={client} />);
    expect(await screen.findByText(/External lab MCP/)).toBeTruthy();
    expect(screen.getByText("not enabled")).toBeTruthy();
    expect(await screen.findByText("patient_summary")).toBeTruthy();
    expect(screen.getAllByText("disabled").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/All seeded tools start disabled/)).toBeTruthy();
    expect(await screen.findByText("Drafts coding suggestions from a reviewed note")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Enable" }));
    await waitFor(() =>
      expect(calls.some((c) => c === "POST /mcp/servers/ext-lab/enable")).toBe(true));

    // WS2b: the A2A message is a rich-text editor (contentEditable).
    const a2aMessage = screen.getByLabelText("A2A message");
    a2aMessage.innerHTML = "Draft coding suggestions for this note";
    fireEvent.input(a2aMessage);
    fireEvent.click(screen.getByRole("button", { name: "Send task (message/stream)" }));
    expect(await screen.findByText("completed")).toBeTruthy();
    expect(screen.getByText("Draft recorded for disposition")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "View stream" }));
    expect(await screen.findByText(/opencare a2a task stream/)).toBeTruthy();
  });
});

// ---------------------------------------------------------------------
// Knowledge / RAG
// ---------------------------------------------------------------------

const RAG_STATUS = {
  chunks: 12, store: "local-json", embedder: "local-hash-v1",
  embedder_kind: "local-hash",
  embedder_note: "local-hash-v1 is a deterministic lexical hashing embedder — a stand-in, NOT a semantic model",
  pgvector_available: false, answer_agent: "rag-answerer",
};

const RAG_SEARCH = {
  results: [{
    chunk_id: "chunk-1", sourceType: "knowledge",
    sourceId: "knowledge/sepsis-pathway", title: "Sepsis pathway summary",
    patient_ref: null, text: "Give fluids early and reassess.",
    score: 0.42,
    citation: {
      sourceType: "knowledge", sourceId: "knowledge/sepsis-pathway",
      title: "Sepsis pathway summary", snippet: "Give fluids early and reassess.",
    },
  }],
  citations: [{
    sourceType: "knowledge", sourceId: "knowledge/sepsis-pathway",
    title: "Sepsis pathway summary", snippet: "Give fluids early and reassess.",
  }],
  total: 1, store: "local-json", embedder: "local-hash-v1",
  embedder_kind: "local-hash", patient_ref: null,
};

describe("KnowledgeTab", () => {
  it("shows the honest status line, search results with citations, and the answer 503 verbatim", async () => {
    const client = {
      listConfigs: vi.fn(async () => []),
      createConfigDraft: vi.fn(async () => ({})),
      updateConfigDraft: vi.fn(async () => ({})),
      publishConfig: vi.fn(async () => ({})),
      disableConfig: vi.fn(async () => ({})),
      rollbackConfig: vi.fn(async () => ({})),
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path === "/rag/status") return RAG_STATUS;
        if (path === "/rag/search") return RAG_SEARCH;
        if (path === "/rag/answer") {
          throw new ApiError(
            503,
            "No RAG answer agent is published: agent 'rag-answerer' has no published version, so no model can answer. Retrieval still works via POST /rag/search. No answer was generated.",
          );
        }
        throw new Error(`unexpected ${path} ${init?.method ?? ""}`);
      }),
    } as unknown as ApiClient;
    render(<KnowledgeTab client={client} />);
    expect(await screen.findByText(/NOT a semantic model/)).toBeTruthy();
    expect(screen.getByText(/chunks indexed:/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("RAG query"), {
      target: { value: "sepsis fluids" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search (retrieval only)" }));
    expect(await screen.findByText("Give fluids early and reassess.")).toBeTruthy();
    expect(screen.getByText(/Citation: Sepsis pathway summary/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Answer with citations" }));
    expect(await screen.findByText(/No RAG answer agent is published/)).toBeTruthy();
    expect(screen.getByText(/No answer was generated/)).toBeTruthy();
  });
});

// ---------------------------------------------------------------------
// Innovation
// ---------------------------------------------------------------------

const PILOT_EVALUATED = {
  id: 7, intake_id: 2, capability_type: "agent",
  capability_key: "chart-search", scope: { unit: "MS" },
  starts_on: "2026-10-01", ends_on: "2026-11-30", status: "evaluated",
  outcome: { summary: "Worked in scope" }, outcome_by: "dev-admin",
  created_by: "dev-admin",
};

const PILOT_EVALUATION = {
  pilot: PILOT_EVALUATED,
  intake: { id: 2, title: "Chart search pilot", capability_type: "agent",
    capability_key: "chart-search", status: "evaluated" },
  exercises: {
    total: 3, succeeded: 2, refused: 1,
    refusals: [{ patient: "Patient/pat-9", reason: "Patient/pat-9 is not in the pilot scope", at: "2026-10-19T00:00:00Z" }],
    patients: ["Patient/pat-1"],
  },
  runs: { total: 2, run_ids: ["r1", "r2"], dispositions: { pending: 2 } },
  outcome: { summary: "Worked in scope" },
  decisions: [],
  handoffs: [],
};

describe("InnovationTab", () => {
  it("walks triage → approve, shows pilot scope/window, evaluation refusals, and the scale hand-off", async () => {
    const calls: Array<{ path: string; body?: string }> = [];
    // Immutable state replacement: the tab only ever sees a status
    // change after a real refetch, exactly like the live API — a
    // shared mutated object would let assertions race the reload.
    let intakeState = {
      id: 1, title: "Ambient scribe pilot", description: "Trial the scribe",
      capability_type: "agent", capability_key: "ambient-scribe",
      proposer: "dev-admin", status: "proposed", triage_notes: "",
      triaged_by: "", created_at: "2026-10-19T00:00:00Z",
    };
    const client = {
      request: vi.fn(async (path: string, init?: RequestInit) => {
        calls.push({ path, body: init?.body as string | undefined });
        if (path === "/innovation/intakes") {
          if (init?.method === "POST") return intakeState;
          return { items: [intakeState], total: 1 };
        }
        if (path === "/innovation/intakes/1/triage") {
          intakeState = {
            ...intakeState, status: "triaged",
            triage_notes: "Worth a scoped pilot", triaged_by: "dev-admin",
          };
          return intakeState;
        }
        if (path === "/innovation/intakes/1/approve-pilot") {
          intakeState = { ...intakeState, status: "pilot-approved" };
          return intakeState;
        }
        if (path === "/innovation/pilots") return { items: [PILOT_EVALUATED], total: 1 };
        if (path === "/innovation/pilots/7/evaluation") return PILOT_EVALUATION;
        if (path === "/innovation/pilots/7/decision") {
          return {
            id: 3, pilot_id: 7, decision: "scale",
            rationale: "Evidence is sufficient", decided_by: "dev-admin",
            created_at: "2026-10-19T00:00:00Z", pilot_status: "decided",
            intake_status: "decided",
            handoff: {
              id: 1, pilot_id: 7, decision_id: 3, capability_type: "agent",
              capability_key: "chart-search",
              required_step: "Enable the agent via POST /agents/chart-search/enable",
              status: "pending-human-action", created_by: "dev-admin",
              created_at: "2026-10-19T00:00:00Z",
            },
          };
        }
        throw new Error(`unexpected ${path}`);
      }),
    } as unknown as ApiClient;
    render(<InnovationTab client={client} />);
    expect(await screen.findByText("Ambient scribe pilot")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Manage" }));
    // Lifecycle is visible as a stepper with the current step named.
    expect(screen.getByText("Proposed")).toBeTruthy();
    expect(screen.getByText("(Current)")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Triage notes"), {
      target: { value: "Worth a scoped pilot" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Triage" }));
    expect(await screen.findByRole("button", { name: "Approve pilot" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Approve pilot" }));
    expect(await screen.findByText("Scope the pilot")).toBeTruthy();

    // Pilot detail: scope + window shown, evaluation with refusals.
    expect(screen.getByText("unit: MS")).toBeTruthy();
    expect(screen.getByText("2026-10-01 → 2026-11-30")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    fireEvent.click(screen.getByRole("button", { name: "Load evaluation" }));
    expect(await screen.findByText(/is not in the pilot scope/)).toBeTruthy();
    expect(screen.getByText(/Exercises: 2 succeeded, 1 refused/)).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Decision rationale"), {
      target: { value: "Evidence is sufficient" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Record decision" }));
    expect(await screen.findByText(/Scale hand-off created \(pending-human-action\)/)).toBeTruthy();
    expect(screen.getByText(/POST \/agents\/chart-search\/enable/)).toBeTruthy();
    expect(calls.some(
      (c) => c.path === "/innovation/pilots/7/decision" &&
             (c.body ?? "").includes('"scale"'))).toBe(true);
  });
});
