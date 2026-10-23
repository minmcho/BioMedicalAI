import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import { ApiError } from "../../api/client";
import type { StudioConfigObject } from "../../api/types";
import { AdkTab } from "./AdkTab";
import { ComplianceTab } from "./ComplianceTab";
import { DashboardsTab } from "./DashboardsTab";

// Phase 10 tabs: agents (ADK), compliance, dashboards-as-config. The
// stub client answers at the same boundary the page tests use; the
// honesty paths (runtime down, no model) are asserted verbatim.

afterEach(cleanup);

function cfg(
  kind: StudioConfigObject["kind"],
  key: string,
  payload: Record<string, unknown>,
  version = 1,
  status: StudioConfigObject["status"] = "draft",
): StudioConfigObject {
  return {
    id: version * 7 + key.length, kind, key, version, status, payload,
    created_by: "system", updated_by: "system",
    created_at: "2026-10-17T00:00:00Z", updated_at: "2026-10-17T00:00:00Z",
  };
}

const AGENT_DRAFT = cfg("agent", "lab-result-summariser", {
  description: "Drafts plain-language lab summaries",
  model: "", instructions: "Summarise.", tools: ["opencare_api_call"],
  apiAllowlist: ["/lab", "/fhir"],
  modalities: { text: true, image: false, audio: false },
});

describe("Agents (ADK) tab", () => {
  it("shows draft agents, the unreachable-runtime notice, and publishes", async () => {
    const publishConfig = vi.fn(async () => ({}));
    const client = {
      listConfigs: vi.fn(async () => [AGENT_DRAFT]),
      publishConfig,
      request: vi.fn(async (path: string) => {
        if (path === "/agents") {
          return {
            agents: [],
            runtime: { url: "http://127.0.0.1:8100", reachable: false,
                       detail: "ConnectError" },
          };
        }
        if (path === "/agents/runs") return [];
        throw new Error(`unexpected ${path}`);
      }),
    } as unknown as ApiClient;
    render(<AdkTab client={client} />);
    expect(await screen.findByText("lab-result-summariser")).toBeTruthy();
    expect(screen.getByText(/draft only/)).toBeTruthy();
    expect(screen.getByText(/no model route — runs are honestly off/)).toBeTruthy();
    expect(screen.getByText(/Agent runtime unreachable/)).toBeTruthy();
    // Nothing is runnable until published AND enabled.
    expect(screen.getByText(/No agent is enabled/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Publish v1" }));
    await waitFor(() =>
      expect(publishConfig).toHaveBeenCalledWith(
        "agent", "lab-result-summariser", 1));
  });

  it("runs an enabled agent and shows the honest no-model 503 verbatim", async () => {
    const client = {
      listConfigs: vi.fn(async () => [
        { ...AGENT_DRAFT, status: "published" as const },
      ]),
      publishConfig: vi.fn(async () => ({})),
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path === "/agents") {
          return {
            agents: [{
              key: "lab-result-summariser", version: 1,
              description: "", model: "", model_configured: false,
              modalities: { text: true }, enabled: true,
            }],
            runtime: { url: "http://127.0.0.1:8100", reachable: true,
                       model_configured: false },
          };
        }
        if (path === "/agents/runs") return [];
        if (path === "/agents/lab-result-summariser/run") {
          throw new ApiError(
            503,
            "No model configured: set a model on the agent config or " +
              "AGENT_MODEL on the ADK runtime. No answer was generated.",
          );
        }
        throw new Error(`unexpected ${path} ${init?.method ?? ""}`);
      }),
    } as unknown as ApiClient;
    render(<AdkTab client={client} />);
    const prompt = await screen.findByLabelText("Prompt");
    // WS2b: the prompt is a rich-text editor (contentEditable) — drive
    // it the way a browser reports edits (innerHTML + input event).
    prompt.innerHTML = "Summarise the BMP";
    fireEvent.input(prompt);
    fireEvent.click(screen.getByRole("button", { name: "Run agent" }));
    expect(await screen.findByText(/No model configured/)).toBeTruthy();
    expect(screen.getByText(/No answer was generated/)).toBeTruthy();
  });

  it("shows a successful run as a pending draft and records disposition", async () => {
    const calls: Array<{ path: string; body?: string }> = [];
    const client = {
      listConfigs: vi.fn(async () => [
        { ...AGENT_DRAFT, status: "published" as const },
      ]),
      publishConfig: vi.fn(async () => ({})),
      request: vi.fn(async (path: string, init?: RequestInit) => {
        calls.push({ path, body: init?.body as string | undefined });
        if (path === "/agents") {
          return {
            agents: [{
              key: "lab-result-summariser", version: 1, description: "",
              model: "ollama_chat/llama3.2:3b", model_configured: true,
              modalities: { text: true }, enabled: true,
            }],
            runtime: { url: "http://127.0.0.1:8100", reachable: true,
                       model_configured: true },
          };
        }
        if (path === "/agents/runs") {
          return [{
            run_id: "run-1234567890", agent: "lab-result-summariser",
            version: 1, actor: "dev-clinician", patient_ref: null,
            input_hash: "ab".repeat(32), output: "DRAFT: all normal.",
            model: "ollama_chat/llama3.2:3b", disposition: "pending",
            disposition_by: null, created_at: "2026-10-17T00:00:00Z",
          }];
        }
        if (path === "/agents/lab-result-summariser/run") {
          return {
            run_id: "run-1234567890", agent: "lab-result-summariser",
            version: 1, actor: "dev-clinician", patient_ref: null,
            input_hash: "ab".repeat(32), output: "DRAFT: all normal.",
            model: "ollama_chat/llama3.2:3b", disposition: "pending",
            disposition_by: null, created_at: "2026-10-17T00:00:00Z",
          };
        }
        if (path === "/agents/runs/run-1234567890/disposition") return {};
        throw new Error(`unexpected ${path}`);
      }),
    } as unknown as ApiClient;
    render(<AdkTab client={client} />);
    const prompt = await screen.findByLabelText("Prompt");
    // WS2b: rich-text editor — see the note in the 503 test above.
    prompt.innerHTML = "Summarise";
    fireEvent.input(prompt);
    fireEvent.click(screen.getByRole("button", { name: "Run agent" }));
    expect(await screen.findByText("DRAFT: all normal.")).toBeTruthy();
    expect(screen.getByText(/pending human disposition/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() =>
      expect(calls.some(
        (c) => c.path === "/agents/runs/run-1234567890/disposition" &&
               (c.body ?? "").includes("accepted"))).toBe(true));
  });
});

const REQUIREMENT = {
  id: 2, body: "HIPAA", citation: "45 CFR 164.312(a)(2)(ii)",
  title: "Emergency access procedure (break-the-glass)",
  control: "Break-glass openings are time-boxed and 100% reviewed",
  implementation_ref: "app.privacy", evidence_source: "audit:breakglass.open",
  created_by: "system", updated_by: "system",
};

const POLICY = {
  duration_minutes: 60, justification_window_hours: 48,
  review_expectation: "100% — every opening reviewed, never sampled",
  source: "built-in defaults (no regulatory_pack published)",
};

describe("Compliance tab", () => {
  it("renders the register, the break-glass policy and the review queue", async () => {
    const calls: string[] = [];
    const client = {
      listConfigs: vi.fn(async () => [
        cfg("regulatory_pack", "hipaa-baseline", { requirements: [] }),
      ]),
      publishConfig: vi.fn(async () => ({})),
      request: vi.fn(async (path: string) => {
        calls.push(path);
        if (path === "/compliance/requirements") {
          return { items: [REQUIREMENT], total: 1, bodies: ["HIPAA"] };
        }
        if (path === "/privacy/review-queue") {
          return {
            items: [{
              access_id: "bg-abcdef123456", patient_ref: "Patient/pat-1",
              actor: "dev-clinician", reason: "code blue",
              opened_at: "2026-10-17T00:00:00Z",
              expires_at: "2026-10-17T01:00:00Z", active: true,
              justification_due_at: "2026-10-19T00:00:00Z",
              overdue: false, reviewed: false, review_outcome: null,
            }],
            total: 1, overdue: 0, policy: POLICY,
          };
        }
        if (path === "/compliance/requirements/2/evidence") {
          return {
            requirement_id: 2, source: "audit:breakglass.open",
            kind: "audit", total: 1,
            entries: [{
              action: "breakglass.open", entity: "Patient/pat-1",
              actor: "dev-clinician", created_at: "2026-10-17T00:00:00Z",
            }],
          };
        }
        if (path.startsWith("/privacy/review-queue/")) return {};
        throw new Error(`unexpected ${path}`);
      }),
    } as unknown as ApiClient;
    render(<ComplianceTab client={client} />);
    expect(await screen.findByText("45 CFR 164.312(a)(2)(ii)")).toBeTruthy();
    expect(screen.getByText(/60 minutes/)).toBeTruthy();
    expect(screen.getByText(/100% — every opening reviewed/)).toBeTruthy();
    expect(screen.getByText("code blue")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "View evidence" }));
    expect(await screen.findByText(/breakglass\.open/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Justified" }));
    await waitFor(() =>
      expect(calls.some(
        (p) => p === "/privacy/review-queue/bg-abcdef123456/review")).toBe(true));
  });
});

describe("Dashboards tab", () => {
  it("renders a published dashboard config's widgets from live endpoints", async () => {
    const dashboard = cfg("dashboard", "operations-overview", {
      title: "Operations overview",
      widgets: [
        { id: "census", title: "Patients in beds",
          endpoint: "/inpatient/census", display: "total" },
        { id: "crits", title: "Critical reports",
          endpoint: "/lab/critical-reports", display: "total" },
        { id: "broken", title: "Broken widget",
          endpoint: "/lab/verification-queue", display: "total" },
      ],
    }, 1, "published");
    const client = {
      listConfigs: vi.fn(async () => [dashboard]),
      effectiveConfig: vi.fn(async () => dashboard),
      request: vi.fn(async (path: string) => {
        if (path === "/inpatient/census") return { total: 7, items: [] };
        if (path === "/lab/critical-reports") return { total: 2, items: [] };
        throw new ApiError(503, "downtime");
      }),
    } as unknown as ApiClient;
    render(<DashboardsTab client={client} />);
    expect(await screen.findByText("Operations overview")).toBeTruthy();
    expect(await screen.findByText("7")).toBeTruthy();
    expect(await screen.findByText("2")).toBeTruthy();
    // A failing widget degrades alone; the dashboard still renders.
    expect(await screen.findByText("unavailable")).toBeTruthy();
  });
});
