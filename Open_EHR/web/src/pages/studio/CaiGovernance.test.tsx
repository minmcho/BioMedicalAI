import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import { ApiError } from "../../api/client";
import type { StudioConfigObject } from "../../api/types";
import { AdkTab } from "./AdkTab";
import { ValidationTab } from "./ValidationTab";

// C-AI clinical AI governance (W3 web): tier matrix, the validation
// statement register (four-eyes approval, verbatim 422/403 refusals),
// the expiry worklist, the kill switch, HTI-1 disclosure and the
// per-run "Report output" incident path. Stub clients pin the
// /ai-governance CONTRACT shapes; the backend is built in parallel.

afterEach(cleanup);

const AGENT = "note-coding-assistant";

const TIERS = {
  tiers: {
    T1: {
      minOverallPass: 0.9, minSliceFloor: 0.85, maxSliceDisparity: 0.2,
      statementValidityMonths: 12, approvals: ["governance_approver"],
      monitoringCadence: "Monthly drift report",
    },
    T2: {
      minOverallPass: 0.95, minSliceFloor: 0.9, maxSliceDisparity: 0.1,
      statementValidityMonths: 6,
      approvals: ["clinical_reviewer", "governance_approver"],
      monitoringCadence: "Monthly drift + disposition review",
    },
    T3: {
      minOverallPass: 0.98, minSliceFloor: 0.95, maxSliceDisparity: 0.05,
      statementValidityMonths: 6,
      approvals: ["clinical_reviewer", "governance_approver", "clinical_owner"],
      monitoringCadence: "Continuous drift watch",
    },
  },
  source: "governed config integration/agent-validation",
};

const DISCLOSURE = {
  agentKey: AGENT,
  attributes: [
    { attribute: "Intended use", value: "Draft progress notes for clinician review", source: "statement field 5" },
    { attribute: "Funding source", value: "not recorded", source: "statement administrative field" },
    { attribute: "External validation status", value: "not recorded", source: "statement field 10" },
  ],
  generatedFrom: { statementId: "VS-1" },
};

function statement(overrides: Record<string, unknown> = {}) {
  return {
    statementId: "VS-1",
    version: 1,
    agentKey: AGENT,
    agentConfigVersion: 1,
    modelRoute: "",
    riskTier: "T2",
    owner: "Dr Owner",
    status: "pending",
    author: "builder-a",
    fields: { intendedUse: "Draft progress notes for clinician review" },
    approvals: [] as unknown[],
    reviewBy: "2027-01-01",
    approvedAt: null,
    currency: { status: "stale", reason: "Awaiting approval" },
    ...overrides,
  };
}

const VAL_STATUS = {
  agentKey: AGENT, status: "current", required: true,
  publishedVersion: 1, modelRoute: "", record: null,
  reason: "Validation record is current",
};

interface GovOptions {
  statements?: unknown[];
  worklist?: unknown[];
  post?: (path: string, init?: RequestInit) => unknown;
}

function governanceClient(opts: GovOptions = {}) {
  const posts: Array<{ path: string; body?: string }> = [];
  let killActive = false;
  const client = {
    request: vi.fn(async (path: string, init?: RequestInit) => {
      if (init?.method === "POST" || init?.method === "PUT") {
        posts.push({ path, body: init.body as string | undefined });
        if (path === "/ai-governance/kill-switch") {
          killActive = true;
          return { active: true };
        }
        if (path === "/ai-governance/kill-switch/release") {
          killActive = false;
          return { active: false };
        }
        if (opts.post) return opts.post(path, init);
        throw new Error(`unexpected ${init.method} ${path}`);
      }
      // Existing validation-harness surface.
      if (path === "/agent-validation/config") {
        return {
          requireValidationForEnable: true, validationValidityDays: 180,
          sliceDisparityThreshold: 0.2, driftRejectRateThreshold: 0.2,
          driftOutputLengthChangeThreshold: 0.5,
        };
      }
      if (path === "/agent-validation/datasets") return { items: [], total: 0 };
      if (path === "/agent-validation/runs") return { items: [], total: 0 };
      if (path === "/agent-validation/validations") return { items: [], total: 0 };
      if (path === "/agent-validation/change-control") return { items: [], total: 0 };
      if (path === "/agent-validation/exceptions") return { items: [], total: 0 };
      if (path === "/agents") {
        return { agents: [{ key: AGENT, version: 1, enabled: false }] };
      }
      if (path === `/agent-validation/agents/${AGENT}/status`) return VAL_STATUS;
      // C-AI governance surface.
      if (path === "/ai-governance/tier-requirements") return TIERS;
      if (path === `/ai-governance/statements?agent=${AGENT}`) {
        return { items: opts.statements ?? [statement()], total: (opts.statements ?? [statement()]).length };
      }
      if (path === "/ai-governance/worklist") {
        return { items: opts.worklist ?? [], total: (opts.worklist ?? []).length };
      }
      if (path === "/ai-governance/kill-switch") {
        return killActive
          ? {
              active: true, reason: "Fabricated output reported",
              engagedBy: "governance-lead", engagedAt: "2026-10-21T00:00:00Z",
            }
          : { active: false, reason: null, engagedBy: null, engagedAt: null };
      }
      if (path === "/ai-governance/incidents") {
        return {
          items: [{
            eventId: "evt-9", reporter: "dev-clinician",
            category: "ai-agent", harmLevel: "minor",
            description: "Draft invented a potassium value not in the input",
            status: "reported", createdAt: "2026-10-21T00:00:00Z",
          }],
          total: 1,
        };
      }
      if (path === `/ai-governance/agents/${AGENT}/disclosure`) return DISCLOSURE;
      throw new Error(`unexpected ${path}`);
    }),
  } as unknown as ApiClient;
  return { client, posts };
}

describe("ValidationTab — C-AI governance", () => {
  it("renders the tier-requirements matrix from the endpoint", async () => {
    const { client } = governanceClient();
    render(<ValidationTab client={client} />);
    expect(await screen.findByText("Risk tiers")).toBeTruthy();
    expect(await screen.findByText("Continuous drift watch")).toBeTruthy();
    expect(screen.getByText("Monthly drift + disposition review")).toBeTruthy();
    expect(screen.getByText(/clinical_reviewer, governance_approver, clinical_owner/)).toBeTruthy();
    expect(screen.getByText(/governed config integration\/agent-validation/)).toBeTruthy();
    expect(screen.getAllByText("6 months").length).toBeGreaterThanOrEqual(1);
  });

  it("lists statements for the selected agent with currency shown", async () => {
    const { client } = governanceClient({
      statements: [
        statement({
          statementId: "VS-7", status: "approved",
          currency: { status: "expired", reason: "Review-by date passed" },
          approvals: [{
            approver: "rev-a", kind: "clinical_reviewer",
            conditions: "Medicine service only", decidedAt: "2026-10-01",
          }],
        }),
      ],
    });
    render(<ValidationTab client={client} />);
    expect(await screen.findByText("VS-7")).toBeTruthy();
    expect(screen.getByText("expired")).toBeTruthy();
    expect(screen.getByText(/Review-by date passed/)).toBeTruthy();
    expect(screen.getByText(/Medicine service only/)).toBeTruthy();
    // The draft editor seeds the five locked prohibited-use codes.
    expect(screen.getByDisplayValue("no-autonomous-diagnosis")).toBeTruthy();
    expect(screen.getByDisplayValue("no-unlisted-route-tool-modality")).toBeTruthy();
    // Zero-tolerance classes 1 and 2 are locked on.
    expect((screen.getByLabelText("Zero-tolerance class 1") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("Zero-tolerance class 1") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText("Zero-tolerance class 2") as HTMLInputElement).checked).toBe(true);
  });

  it("surfaces a submit 422 completeness refusal verbatim", async () => {
    const draft = statement({ statementId: "VS-3", status: "draft" });
    const { client, posts } = governanceClient({
      statements: [draft],
      post: (path) => {
        if (path === "/ai-governance/statements/VS-3") return draft;
        if (path === "/ai-governance/statements/VS-3/submit") {
          throw new ApiError(
            422,
            "Statement is incomplete: missing fields: monitoringPlan, changeControlPlan, reviewByRequested",
          );
        }
        throw new Error(`unexpected POST ${path}`);
      },
    });
    render(<ValidationTab client={client} />);
    expect(await screen.findByText("VS-3")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Edit draft" }));
    fireEvent.click(screen.getByRole("button", { name: "Submit for approval" }));
    expect(
      await screen.findByText(/Statement is incomplete: missing fields: monitoringPlan, changeControlPlan, reviewByRequested/),
    ).toBeTruthy();
    await waitFor(() =>
      expect(posts.some((p) => p.path === "/ai-governance/statements/VS-3/submit")).toBe(true));
  });

  it("surfaces an approve 403 four-eyes refusal verbatim", async () => {
    const { client } = governanceClient({
      post: (path) => {
        if (path === "/ai-governance/statements/VS-1/approve") {
          throw new ApiError(
            403,
            "Four-eyes refusal: the approver must not be the statement author (builder-a)",
          );
        }
        throw new Error(`unexpected POST ${path}`);
      },
    });
    render(<ValidationTab client={client} />);
    expect(await screen.findByText("VS-1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(
      await screen.findByText(/Four-eyes refusal: the approver must not be the statement author/),
    ).toBeTruthy();
  });

  it("kill switch requires a reason, confirms, then shows the active state", async () => {
    const { client, posts } = governanceClient();
    render(<ValidationTab client={client} />);
    expect(await screen.findByText(/Kill switch is inactive/)).toBeTruthy();
    expect(screen.getByText(/disables ALL agents immediately/)).toBeTruthy();
    const engage = screen.getByRole("button", { name: "Engage kill switch" }) as HTMLButtonElement;
    expect(engage.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Kill switch reason"), {
      target: { value: "Fabricated output reported" },
    });
    expect(engage.disabled).toBe(false);
    fireEvent.click(engage);
    fireEvent.click(
      screen.getByRole("button", { name: "Confirm engage — disable all agents" }),
    );
    expect(await screen.findByText(/Kill switch is\s+ACTIVE/)).toBeTruthy();
    expect(screen.getByText(/Engaged by governance-lead/)).toBeTruthy();
    expect(screen.getByText(/does not re-enable any agent/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Release kill switch" })).toBeTruthy();
    await waitFor(() =>
      expect(posts.some(
        (p) => p.path === "/ai-governance/kill-switch" &&
               (p.body ?? "").includes("Fabricated output reported"))).toBe(true));
  });

  it("renders the expiry worklist with the overdue state distinct", async () => {
    const { client } = governanceClient({
      worklist: [{
        agentKey: AGENT, owner: "Dr Owner", tier: "T2",
        statementStatus: "approved", statementDaysToExpiry: -3,
        recordStatus: "current", recordDaysToExpiry: 40,
        dueState: "overdue",
      }],
    });
    render(<ValidationTab client={client} />);
    expect(await screen.findByText("overdue")).toBeTruthy();
    expect(screen.getByText(/approved \(-3 days\)/)).toBeTruthy();
    expect(screen.getByText(/current \(40 days\)/)).toBeTruthy();
    const row = screen.getByText("overdue").closest("tr");
    expect(row?.style.background).toBeTruthy();
  });

  it("renders the disclosure attributes including 'not recorded' gaps as-is", async () => {
    const { client } = governanceClient();
    render(<ValidationTab client={client} />);
    expect(
      await screen.findByText("Draft progress notes for clinician review"),
    ).toBeTruthy();
    expect(screen.getAllByText("not recorded").length).toBe(2);
    expect(screen.getByText("Funding source")).toBeTruthy();
    // Incidents list rides the same render.
    expect(await screen.findByText("evt-9")).toBeTruthy();
    expect(screen.getByText(/Draft invented a potassium value/)).toBeTruthy();
  });
});

// ---------------------------------------------------------------------
// AdkTab — governance badge, disclosure, report output
// ---------------------------------------------------------------------

function cfg(
  key: string,
  payload: Record<string, unknown>,
): StudioConfigObject {
  return {
    id: 7, kind: "agent", key, version: 1, status: "published", payload,
    created_by: "system", updated_by: "system",
    created_at: "2026-10-19T00:00:00Z", updated_at: "2026-10-19T00:00:00Z",
  };
}

const RUN = {
  run_id: "run-1234567890",
  agent: AGENT, version: 1, actor: "dev-clinician",
  patient_ref: null, input_hash: "ab".repeat(32),
  output: "Draft note text.", model: null,
  disposition: "pending", disposition_by: null,
  created_at: "2026-10-19T00:00:00Z",
};

function adkClient() {
  const posts: Array<{ path: string; body?: string }> = [];
  const client = {
    listConfigs: vi.fn(async () => [
      cfg(AGENT, { description: "Drafts notes", model: "" }),
    ]),
    request: vi.fn(async (path: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posts.push({ path, body: init.body as string | undefined });
        if (path === "/ai-governance/runs/run-1234567890/report") {
          return { eventId: "evt-42", linkedEventIds: ["evt-9"] };
        }
        throw new Error(`unexpected POST ${path}`);
      }
      if (path === "/agents") {
        return {
          agents: [{
            key: AGENT, version: 1, description: "Drafts notes",
            model: "", model_configured: false,
            modalities: { text: true }, enabled: false,
            risk_tier: "T2",
            governance: { statementStatus: "approved", recordStatus: "current" },
          }],
          runtime: { url: "http://127.0.0.1:8100", reachable: true },
        };
      }
      if (path === "/agents/runs") return [RUN];
      if (path === `/ai-governance/agents/${AGENT}/status`) {
        return {
          agentKey: AGENT, tier: "T2", killSwitchActive: false,
          enabled: false,
          validation: { status: "current", reason: "Record current" },
          statement: { status: "approved", statementId: "VS-1", reason: "Statement current" },
          governance: {},
          runtimeGate: { allowed: false, reason: "Agent is not enabled" },
        };
      }
      if (path === `/ai-governance/agents/${AGENT}/disclosure`) return DISCLOSURE;
      throw new Error(`unexpected ${path}`);
    }),
  } as unknown as ApiClient;
  return { client, posts };
}

describe("AdkTab — C-AI governance", () => {
  it("shows the roster risk tier, governance status, and source attributes", async () => {
    const { client } = adkClient();
    render(<AdkTab client={client} />);
    expect(await screen.findByText("Risk tier T2")).toBeTruthy();
    expect(screen.getByText(/statement: approved/)).toBeTruthy();
    // Empty model route stays honestly off — no runnable claim.
    expect(screen.getByText(/no model route — runs are honestly off/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Governance status" }));
    expect(await screen.findByText(/statement: approved \(VS-1\)/)).toBeTruthy();
    expect(screen.getByText(/Runtime gate: refused — Agent is not enabled/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Source attributes" }));
    expect(await screen.findByText("Funding source")).toBeTruthy();
    expect(screen.getAllByText("not recorded").length).toBeGreaterThanOrEqual(1);
  });

  it("report output posts to the governance incident path and shows the event id", async () => {
    const { client, posts } = adkClient();
    render(<AdkTab client={client} />);
    expect(await screen.findByText("Report output")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Report output" }));
    fireEvent.change(
      screen.getByLabelText("Report description for run run-1234567890"),
      { target: { value: "Invented a lab value" } },
    );
    fireEvent.change(
      screen.getByLabelText("Report harm level for run run-1234567890"),
      { target: { value: "minor" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "File report" }));
    expect(await screen.findByText(/Report filed: event evt-42/)).toBeTruthy();
    expect(screen.getByText(/also linked: evt-9/)).toBeTruthy();
    await waitFor(() =>
      expect(posts.some(
        (p) => p.path === "/ai-governance/runs/run-1234567890/report" &&
               (p.body ?? "").includes("Invented a lab value") &&
               (p.body ?? "").includes('"minor"'))).toBe(true));
  });
});
