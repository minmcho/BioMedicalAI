import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type {
  InboxResultDetail,
  InboxResultItem,
  InboxSummary,
} from "../api/inbox";
import { fixtureMessageThreads, fixtureThreadDetail } from "../test/fixtures";
import { InboxPage } from "./InboxPage";

// Phase 15 (G): the Results tab is the cross-patient clinician
// worklist over /inbox (stubbed here through client.request, keyed by
// path); the Messages tab preserves the Phase 9 secure-message
// threads (the tests at the bottom are carried over from the previous
// InboxPage test file, adapted only to open the Messages tab first).
afterEach(cleanup);

const summaryFixture: InboxSummary = {
  results: {
    unacknowledged: 2,
    critical_unacknowledged: 1,
    module: "inbox",
    nav_hint: { module: "command", label: "Care continuum" },
  },
  continuum: {
    open_tasks: 3,
    breached: 1,
    module: "command",
    nav_hint: { module: "inbox", label: "Results inbox" },
  },
  generated_at: "2026-01-05T12:00:00Z",
};

function worklistItems(acked: boolean): InboxResultItem[] {
  return [
    {
      result_ref: "DiagnosticReport/rep-crit",
      result_type: "DiagnosticReport",
      result_id: "rep-crit",
      patient_ref: "Patient/pat-2",
      kind: "lab",
      title: "Basic metabolic panel",
      code: "51990-0",
      effective_at: "2026-01-05T10:00:00Z",
      flag: "critical",
      status: "final",
      observation_count: 1,
      acknowledged: acked,
      acknowledged_at: acked ? "2026-01-05T11:00:00Z" : null,
      acknowledged_by: acked ? "dr-clinician" : null,
    },
    {
      result_ref: "DiagnosticReport/rep-glu",
      result_type: "DiagnosticReport",
      result_id: "rep-glu",
      patient_ref: "Patient/pat-1",
      kind: "lab",
      title: "Glucose panel",
      code: "2345-7",
      effective_at: "2026-01-05T09:00:00Z",
      flag: "abnormal",
      status: "final",
      observation_count: 1,
      acknowledged: false,
      acknowledged_at: null,
      acknowledged_by: null,
    },
    {
      result_ref: "DiagnosticReport/rep-ct",
      result_type: "DiagnosticReport",
      result_id: "rep-ct",
      patient_ref: "Patient/pat-2",
      kind: "imaging",
      title: "CT head report",
      code: "CT-HEAD",
      effective_at: "2026-01-05T08:00:00Z",
      flag: null,
      status: "final",
      observation_count: 0,
      acknowledged: true,
      acknowledged_at: "2026-01-05T08:30:00Z",
      acknowledged_by: "dr-clinician",
    },
  ];
}

function detailFixture(acked: boolean): InboxResultDetail {
  const item = worklistItems(acked)[0];
  return {
    ...item,
    report: {
      resourceType: "DiagnosticReport",
      id: "rep-crit",
      status: "final",
    },
    conclusion: "Potassium critically high — repeat sample advised.",
    observations: [
      {
        resourceType: "Observation",
        id: "obs-k",
        status: "final",
        code: {
          coding: [{ system: "http://loinc.org", code: "2823-3", display: "Potassium" }],
          text: "Potassium",
        },
        effectiveDateTime: "2026-01-05T10:00:00Z",
        valueQuantity: { value: 6.4, unit: "mmol/L" },
        interpretation: [
          {
            coding: [
              {
                system: "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                code: "HH",
                display: "HH",
              },
            ],
            text: "HH",
          },
        ],
        referenceRange: [{ low: { value: 3.5, unit: "mmol/L" }, high: { value: 5.1, unit: "mmol/L" } }],
      } as InboxResultDetail["observations"][number],
    ],
    patient_summary: {
      patient_ref: "Patient/pat-2",
      found: true,
      id: "pat-2",
      name: "Pat Two",
      gender: "female",
      birth_date: "1980-01-01",
    },
    encounter_context: {
      encounter_ref: "Encounter/enc-1",
      found: true,
      id: "enc-1",
      status: "in-progress",
      class_code: "IMP",
      attending_staff_id: "staff-1",
    },
    trend: [
      {
        observation_id: "obs-k-old",
        code: "2823-3",
        display: "Potassium",
        value: 4.2,
        unit: "mmol/L",
        interpretation: "N",
        effective_at: "2026-01-01T09:00:00Z",
      },
      {
        observation_id: "obs-k",
        code: "2823-3",
        display: "Potassium",
        value: 6.4,
        unit: "mmol/L",
        interpretation: "HH",
        effective_at: "2026-01-05T10:00:00Z",
      },
    ],
  };
}

/** Stateful request stub: the acknowledge POST flips the acked flag
 *  so subsequent list/detail reads reflect the new state, exactly as
 *  the real endpoints behave. List reads honour kind/flag/ack query
 *  params so filter changes are observable. */
function makeRequest() {
  let acked = false;
  return vi.fn(async (path: string, init?: RequestInit) => {
    if (path === "/inbox/summary") return summaryFixture;
    if (path === "/inbox/results/lab/rep-crit/acknowledge") {
      expect(init?.method).toBe("POST");
      acked = true;
      return {
        result_ref: "DiagnosticReport/rep-crit",
        kind: "lab",
        result_id: "rep-crit",
        patient_ref: "Patient/pat-2",
        acknowledged_by: "dr-clinician",
        acknowledged_at: "2026-01-05T11:00:00Z",
        note: null,
        already_acknowledged: false,
      };
    }
    if (path === "/inbox/results/lab/rep-crit") return detailFixture(acked);
    if (path.startsWith("/inbox/results")) {
      const query = path.includes("?") ? new URLSearchParams(path.split("?")[1]) : null;
      let items = worklistItems(acked);
      const kind = query?.get("kind");
      const flag = query?.get("flag");
      const ack = query?.get("ack");
      if (kind) items = items.filter((i) => i.kind === kind);
      if (flag === "none") items = items.filter((i) => i.flag === null);
      else if (flag) items = items.filter((i) => i.flag === flag);
      if (ack === "acknowledged") items = items.filter((i) => i.acknowledged);
      if (ack === "unacknowledged") items = items.filter((i) => !i.acknowledged);
      return {
        items,
        total: items.length,
        page_size: 50,
        limit: 50,
        truncated: false,
        scope: query?.get("scope") ?? "all",
        filters: { kind, flag, ack: ack ?? "all", patient: null },
      };
    }
    throw new Error(`Unexpected request: ${path}`);
  });
}

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    request: makeRequest(),
    messageInbox: vi.fn(async () => ({
      items: fixtureMessageThreads,
      total: fixtureMessageThreads.length,
      patient: "Patient/pat-1",
    })),
    messageThread: vi.fn(async () => fixtureThreadDetail),
    replyToThread: vi.fn(async () => ({})),
    markThreadRead: vi.fn(async () => ({ thread_id: "thread-1", marked: 2 })),
    sendMessage: vi.fn(async () => ({})),
    ...overrides,
  } as unknown as ApiClient;
}

async function openMessagesTab() {
  fireEvent.click(await screen.findByRole("tab", { name: "Messages" }));
}

describe("InboxPage results worklist (Phase 15 G)", () => {
  it("renders the cross-patient worklist from /inbox/results, severity first as returned", async () => {
    const client = stubClient();
    render(<InboxPage client={client} />);
    expect(await screen.findByText("Basic metabolic panel")).toBeTruthy();
    expect(screen.getByText("Glucose panel")).toBeTruthy();
    expect(screen.getByText("CT head report")).toBeTruthy();
    // Flag badges are text + colour (StatusBadge), ack state is text.
    expect(screen.getAllByText("Critical").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Abnormal").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Unacknowledged").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Patient\/pat-2/).length).toBeGreaterThan(0);
    await waitFor(() =>
      expect(client.request).toHaveBeenCalledWith("/inbox/results"),
    );
  });

  it("renders the summary strip with results + continuum counts and the Command Center affordance", async () => {
    const onNavigate = vi.fn();
    render(<InboxPage client={stubClient()} onNavigate={onNavigate} />);
    expect(
      await screen.findByText("2 unacknowledged results · 1 critical unacknowledged"),
    ).toBeTruthy();
    expect(screen.getByText("Care continuum: 3 open tasks · 1 breached")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open Command Center inbox" }));
    expect(onNavigate).toHaveBeenCalledWith("command");
  });

  it("changing a filter re-queries the worklist with the new filter", async () => {
    const client = stubClient();
    render(<InboxPage client={client} />);
    await screen.findByText("Basic metabolic panel");
    fireEvent.change(screen.getByLabelText("Kind"), { target: { value: "imaging" } });
    await waitFor(() =>
      expect(client.request).toHaveBeenCalledWith("/inbox/results?kind=imaging"),
    );
    expect(await screen.findByText("CT head report")).toBeTruthy();
    expect(screen.queryByText("Basic metabolic panel")).toBeNull();
  });

  it("selecting a result renders its detail: values, reference range, conclusion, patient, encounter, trend", async () => {
    const onOpenChart = vi.fn();
    render(<InboxPage client={stubClient()} onOpenChart={onOpenChart} />);
    fireEvent.click(await screen.findByRole("button", { name: "Basic metabolic panel" }));
    expect(
      await screen.findByText("Potassium critically high — repeat sample advised."),
    ).toBeTruthy();
    // The current value appears in both the observation and the trend.
    expect(screen.getAllByText("6.4 mmol/L").length).toBeGreaterThan(0);
    expect(screen.getByText("Reference range: 3.5–5.1 mmol/L")).toBeTruthy();
    expect(screen.getByText(/Pat Two/)).toBeTruthy();
    expect(screen.getByText(/Encounter\/enc-1/)).toBeTruthy();
    // Trend carries the prior same-code result.
    expect(screen.getByText("4.2 mmol/L")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open chart" }));
    expect(onOpenChart).toHaveBeenCalledWith("pat-2");
  });

  it("acknowledge calls the endpoint and updates the displayed state", async () => {
    const client = stubClient();
    render(<InboxPage client={client} />);
    fireEvent.click(await screen.findByRole("button", { name: "Basic metabolic panel" }));
    await screen.findByText("Potassium critically high — repeat sample advised.");
    fireEvent.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() =>
      expect(client.request).toHaveBeenCalledWith(
        "/inbox/results/lab/rep-crit/acknowledge",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    expect(await screen.findByText("Acknowledged by dr-clinician.")).toBeTruthy();
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: "Acknowledge" }) as HTMLButtonElement).disabled,
      ).toBe(true),
    );
  });

  it("follow-up task, message patient and escalate each call their endpoint and show the outcome", async () => {
    const request = vi.fn(async (path: string) => {
      if (path === "/inbox/summary") return summaryFixture;
      if (path === "/inbox/results/lab/rep-crit") return detailFixture(false);
      if (path.startsWith("/inbox/results/lab/rep-crit/follow-up-task"))
        return { task: { resourceType: "Task", id: "task-9" }, result_ref: "DiagnosticReport/rep-crit" };
      if (path.startsWith("/inbox/results/lab/rep-crit/message-patient"))
        return { message: { resourceType: "Communication", id: "msg-9" }, thread_id: "thread-9", result_ref: "DiagnosticReport/rep-crit" };
      if (path.startsWith("/inbox/results/lab/rep-crit/escalate"))
        return { task: { resourceType: "Task", id: "task-10" }, audience: "charge-nurse", result_ref: "DiagnosticReport/rep-crit" };
      if (path.startsWith("/inbox/results"))
        return {
          items: worklistItems(false),
          total: 3,
          page_size: 50,
          limit: 50,
          truncated: false,
          scope: "all",
          filters: { kind: null, flag: null, ack: "all", patient: null },
        };
      throw new Error(`Unexpected request: ${path}`);
    });
    const client = stubClient({ request });
    render(<InboxPage client={client} />);
    fireEvent.click(await screen.findByRole("button", { name: "Basic metabolic panel" }));
    await screen.findByText("Potassium critically high — repeat sample advised.");

    fireEvent.change(screen.getByLabelText("Follow-up task description"), {
      target: { value: "Repeat BMP in 4 hours" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create follow-up task" }));
    expect(await screen.findByText("Follow-up task created (task-9).")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Message to patient about this result"), {
      target: { value: "Your potassium result needs a repeat test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Message patient" }));
    expect(
      await screen.findByText("Message sent to patient (thread thread-9)."),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Escalate" }));
    expect(await screen.findByText("Escalated to charge-nurse.")).toBeTruthy();
    expect(request).toHaveBeenCalledWith(
      "/inbox/results/lab/rep-crit/escalate",
      expect.objectContaining({ method: "POST" }),
    );
  });
});

describe("InboxPage (Phase 9 messages, preserved)", () => {
  it("renders the patient's message threads with unread counts", async () => {
    render(<InboxPage client={stubClient()} />);
    await openMessagesTab();
    expect(await screen.findByText("Message thread")).toBeTruthy();
    expect(screen.getByText(/1 unread/)).toBeTruthy();
    expect(screen.getByText("Your result is ready")).toBeTruthy();
  });

  it("opening a thread marks it read and shows the conversation", async () => {
    const client = stubClient();
    render(<InboxPage client={client} />);
    await openMessagesTab();
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));
    await waitFor(() =>
      expect(client.markThreadRead).toHaveBeenCalledWith("thread-1"),
    );
    expect(await screen.findByText("Question about my lab")).toBeTruthy();
  });

  it("staff can reply in the thread and start a new thread", async () => {
    const client = stubClient();
    render(<InboxPage client={client} />);
    await openMessagesTab();
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));
    await screen.findByText("Question about my lab");
    fireEvent.change(screen.getByLabelText("Reply"), {
      target: { value: "It posted this morning" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send reply" }));
    await waitFor(() =>
      expect(client.replyToThread).toHaveBeenCalledWith(
        "thread-1",
        "It posted this morning",
      ),
    );

    fireEvent.change(screen.getByLabelText("New message to patient"), {
      target: { value: "Care-plan update" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() =>
      expect(client.sendMessage).toHaveBeenCalledWith("pat-1", "Care-plan update"),
    );
  });
});
