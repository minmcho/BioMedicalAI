import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import { LabTat } from "./LabTat";

afterEach(cleanup);

const CONFIG = {
  config: { kind: "terminology", key: "lab-tat", version: 1, publish_in: "System Studio -> Terminology -> lab-tat" },
  warning_fraction: 0.8,
  escalation: { supervisorAfterBreachMinutes: 0, managerAfterBreachMinutes: 60, labManagerDisplay: "Laboratory manager (on-call)" },
  critical_notify: { windowMinutes: 30, supervisorAfterBreachMinutes: 0, managerAfterBreachMinutes: 30 },
  governed_defaults_label: "GOVERNED DEFAULTS — laboratory director to set; NOT a universal standard.",
};

const BOARD = {
  generated_at: "2026-10-22T00:00:00Z",
  warning_fraction: 0.8,
  clocks: ["order-to-result", "receipt-to-result"],
  benches: [
    {
      bench: "chemistry",
      approaching: [
        {
          order_id: "ord-1", code: "2345-7", display: "Glucose", bench: "chemistry",
          priority: "routine", order_status: "received", clock: "order-to-result",
          clock_label: "order → result", target_minutes: 240, start_at: null,
          due_at: "2026-10-22T04:00:00Z", elapsed_minutes: 200, fraction: 0.83,
          minutes_past_breach: 0, state: "approaching", specimen_id: "sp-1",
        },
      ],
      breached: [
        {
          order_id: "ord-1", code: "2345-7", display: "Glucose", bench: "chemistry",
          priority: "routine", order_status: "received", clock: "receipt-to-result",
          clock_label: "receipt → result", target_minutes: 180, start_at: null,
          due_at: "2026-10-22T03:00:00Z", elapsed_minutes: 190, fraction: 1.06,
          minutes_past_breach: 10, state: "breached", specimen_id: "sp-1",
        },
      ],
      awaiting_receipt: [],
      within_count: 3,
    },
  ],
  unmonitored: [
    {
      order_id: "ord-9", code: "883-9", bench: "blood-bank", priority: "routine",
      monitored: false,
      reason: "no TAT target published for this test in the lab directory (terminology/lab-catalog directory.tatTargets)",
    },
  ],
  unmonitored_count: 1,
};

const CHASE = {
  items: [
    {
      report_id: "rep-1", order_id: "ord-2", bench: "chemistry", code: "2823-3",
      released_at: "2026-10-22T00:00:00Z", notify_window_minutes: 30,
      elapsed_minutes: 45, minutes_past_window: 15, chase_state: "breached",
      notification: { state: "pending", notified_to: null, notified_at: null },
    },
  ],
  total: 1,
  unnotified: 1,
};

const ESCALATIONS = {
  items: [
    {
      escalation_id: "esc-1", kind: "tat", subject_key: "ord-1", bench_code: "chemistry",
      level: 1, level_name: "supervisor", target_role: "bench-supervisor",
      target_staff_id: "staff-201", clock: "receipt-to-result", task_id: "task-1",
      created_at: "2026-10-22T00:10:00Z", detail: { summary: "breached" },
    },
  ],
  total: 1,
};

const REFLEX = {
  items: [
    {
      firing_id: "rfx-1", rule_id: "tsh-low-add-ft4", source_order_id: "ord-3",
      trigger_code: "3016-3", trigger_value: "0.2", trigger_kind: "quantitative",
      add_code: "3024-7", status: "fired", refusal_reason: null,
      specimen_key: "sp-1", required_volume_ml: 5, remaining_before_ml: 10,
      addon_order_id: "ord-4", reflex_billing: true, cascade_step: 1,
      parent_firing_id: null, recollect_task_id: null,
    },
    {
      firing_id: "rfx-2", rule_id: "tsh-low-add-ft4", source_order_id: "ord-5",
      trigger_code: "3016-3", trigger_value: "0.1", trigger_kind: "quantitative",
      add_code: "3024-7", status: "refused-insufficient-volume",
      refusal_reason: "Insufficient volume for reflex 'tsh-low-add-ft4' add-on '3024-7': requires 5.0 mL per its aliquot rules, specimen sp-2 has 4.0 mL remaining",
      specimen_key: "sp-2", required_volume_ml: 5, remaining_before_ml: 4,
      addon_order_id: null, reflex_billing: false, cascade_step: 1,
      parent_firing_id: null, recollect_task_id: null,
    },
  ],
  total: 2,
  refused: 1,
};

function stubClient() {
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === "/lab/tat/config") return CONFIG;
    if (path === "/lab/tat/board") return BOARD;
    if (path === "/lab/tat/critical-chase") return CHASE;
    if (path === "/lab/tat/escalations") return ESCALATIONS;
    if (path === "/lab/tat/reflex") return REFLEX;
    if (path === "/lab/tat/scan" && init?.method === "POST") return { fired: [], fired_count: 2, scanned_at: "2026-10-22T00:00:00Z" };
    if (path === "/lab/tat/reflex-firings/rfx-1/begin" && init?.method === "POST") {
      return { firing: { ...REFLEX.items[0], status: "in-analysis" }, order: { id: "ord-4", status: "received" } };
    }
    if (path === "/lab/tat/reflex-firings/rfx-2/request-recollect" && init?.method === "POST") {
      return { firing: { ...REFLEX.items[1], status: "recollect-requested", recollect_task_id: "task-9" }, task_id: "task-9" };
    }
    throw new ApiError(404, `No stub for ${path}`);
  });
  return { request } as unknown as ApiClient;
}

describe("LabTat (Lab Depth W5)", () => {
  it("shows the governed config with its defaults label", async () => {
    render(<LabTat client={stubClient()} />);
    expect(await screen.findByText(/terminology\/lab-tat/)).toBeTruthy();
    expect(screen.getByText(/GOVERNED DEFAULTS/)).toBeTruthy();
    expect(screen.getByText(/critical notify window 30 min/)).toBeTruthy();
  });

  it("renders per-bench breached and approaching lists with DECLARED clocks kept separate", async () => {
    render(<LabTat client={stubClient()} />);
    expect(await screen.findByText(/1 breached, 1 approaching/)).toBeTruthy();
    // The same order appears once per declared clock, labelled.
    expect(screen.getByText(/receipt → result \(receipt-to-result\)/)).toBeTruthy();
    expect(screen.getByText(/order → result \(order-to-result\)/)).toBeTruthy();
    expect(screen.getByText(/breached \(\+10 min\)/)).toBeTruthy();
  });

  it("names unmonitored tests with the reason instead of hiding them", async () => {
    render(<LabTat client={stubClient()} />);
    expect(await screen.findByText(/Unmonitored \(1\)/)).toBeTruthy();
    expect(screen.getByText(/no TAT target published for this test/)).toBeTruthy();
  });

  it("shows the critical chase with elapsed vs the notify window", async () => {
    render(<LabTat client={stubClient()} />);
    expect(await screen.findByText("rep-1")).toBeTruthy();
    expect(screen.getByText(/\+15 min past window/)).toBeTruthy();
    expect(screen.getByText(/1 un-notified of 1/)).toBeTruthy();
  });

  it("shows the escalation log with level and notified target", async () => {
    render(<LabTat client={stubClient()} />);
    expect(await screen.findByText(/Level 1 — supervisor/)).toBeTruthy();
    expect(screen.getByText(/bench-supervisor \(staff-201\)/)).toBeTruthy();
  });

  it("shows reflex firings with trigger, specimen, billing flag — and refusals with reasons", async () => {
    render(<LabTat client={stubClient()} />);
    expect(await screen.findByText(/3016-3 = 0.2 \(quantitative\)/)).toBeTruthy();
    expect(screen.getByText("reflex-billable")).toBeTruthy();
    expect(screen.getAllByText(/step 1/).length).toBe(2);
    expect(screen.getByText(/Insufficient volume for reflex/)).toBeTruthy();
  });

  it("runs the escalation scan and reports the fired count", async () => {
    const client = stubClient();
    render(<LabTat client={client} />);
    const button = await screen.findByRole("button", { name: "Run escalation scan" });
    fireEvent.click(button);
    expect(await screen.findByText(/2 new escalation\(s\) fired/)).toBeTruthy();
  });

  it("begins bench work on a fired reflex and requests recollect for a refusal", async () => {
    const client = stubClient();
    render(<LabTat client={client} />);
    const begin = await screen.findByRole("button", { name: "Begin bench work" });
    fireEvent.click(begin);
    await waitFor(() => expect(client.request).toHaveBeenCalledWith(
      "/lab/tat/reflex-firings/rfx-1/begin", expect.objectContaining({ method: "POST" })));
    const recollect = screen.getByRole("button", { name: "Request recollect" });
    fireEvent.click(recollect);
    await waitFor(() => expect(client.request).toHaveBeenCalledWith(
      "/lab/tat/reflex-firings/rfx-2/request-recollect", expect.objectContaining({ method: "POST" })));
  });
});
