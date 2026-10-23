import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, IndicatorRecordsResponse } from "../api/client";
import type { DashboardConfig } from "../api/types";
import { DashboardGrid } from "./DashboardView";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// WS4b dashboard drill: an indicator-backed widget (the rewired form
// backend platform_depth produces: endpoint /indicators/{key}/value,
// field "value", plus the carried `indicator` key) is clickable and
// opens the definition + records drill; a non-indicator widget has
// no drill affordance at all. Records fixture copies the REAL
// GET /indicators/{key}/records shape (backend/app/indicators.py).

const RECORDS: IndicatorRecordsResponse = {
  key: "operations-critical-open",
  definition: {
    name: "Critical reports awaiting closure",
    title: "Critical reports awaiting closure",
    source: { type: "named", name: "lab.critical-open" },
    direction: "lower-better",
    thresholds: { warn: 1, critical: 5 },
    unit: "reports",
  },
  value: {
    key: "operations-critical-open",
    value: 2,
    unit: "reports",
    target: null,
    status: "warn",
    evaluated_at: "2026-10-19T17:00:00+00:00",
    source: { type: "named", name: "lab.critical-open" },
    detail: { named: "lab.critical-open" },
  },
  records: [
    {
      id: "rep-1",
      label: "Potassium — Synth pat-1",
      detail:
        "Report rep-1 · order ord-1 · order status released · critical value",
      at: "2026-10-19T10:00:00+00:00",
      patient_id: "pat-1",
    },
    {
      id: "rep-2",
      label: "Sodium — Synth pat-2",
      detail: "Report rep-2 · order ord-2 · order status released · critical value",
      at: "2026-10-19T11:00:00+00:00",
      patient_id: "pat-2",
    },
  ],
  records_supported: true,
  reason: null,
};

const CONFIG = {
  title: "Ops",
  widgets: [
    {
      id: "crit",
      title: "Critical reports awaiting closure",
      endpoint: "/indicators/operations-critical-open/value",
      display: "value",
      field: "value",
      indicator: "operations-critical-open",
    },
    {
      id: "beds",
      title: "Occupied beds",
      endpoint: "/people/analytics/overview",
      display: "big-number",
      field: "occupied",
    },
    {
      // Indicator derived from the endpoint alone (no carried key).
      id: "queue",
      title: "Lab reports in verification",
      endpoint: "/indicators/operations-verification-queue/value",
      display: "value",
      field: "value",
    },
  ],
} as unknown as DashboardConfig;

function stubClient() {
  return {
    request: vi.fn(async (path: string) => {
      if (path === "/indicators/operations-critical-open/value") {
        return { value: 2, unit: "reports", status: "warn" };
      }
      if (path === "/indicators/operations-verification-queue/value") {
        return { value: 0, unit: "reports", status: "ok" };
      }
      if (path === "/people/analytics/overview") return { occupied: 7 };
      throw new Error(`Unexpected request: ${path}`);
    }),
    indicatorRecords: vi.fn(async (key: string) =>
      key === "operations-critical-open"
        ? RECORDS
        : { ...RECORDS, key, records: [], value: { ...RECORDS.value, key, value: 0 } },
    ),
  } as unknown as ApiClient;
}

describe("DashboardView indicator drill (WS4b)", () => {
  it("indicator widget click opens the drill with records; patient rows open the chart", async () => {
    const onOpenChart = vi.fn();
    render(
      <DashboardGrid client={stubClient()} config={CONFIG} onOpenChart={onOpenChart} />,
    );
    expect(await screen.findByText("7")).toBeTruthy();
    // Exactly the two indicator widgets carry the drill affordance.
    expect(screen.getAllByText("View underlying records")).toHaveLength(2);

    fireEvent.click(screen.getAllByText("View underlying records")[0]);
    // Definition summary + count header (N == reported value 2).
    expect(await screen.findByText(/Named source lab\.critical-open/)).toBeTruthy();
    expect(screen.getByText(/2 records — matches the reported value 2/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Potassium — Synth pat-1" }));
    expect(onOpenChart).toHaveBeenCalledTimes(1);
    expect(onOpenChart).toHaveBeenCalledWith("pat-1");
  });

  it("non-indicator widget has no drill affordance", async () => {
    render(<DashboardGrid client={stubClient()} config={CONFIG} />);
    expect(await screen.findByText("Occupied beds")).toBeTruthy();
    // The big-number widget renders its value and endpoint only; the
    // only drill buttons in the grid belong to the indicator widgets.
    const drillButtons = screen.getAllByText("View underlying records");
    expect(drillButtons).toHaveLength(2);
    expect(screen.queryByText(/Hide underlying records/)).toBeNull();
  });

  it("without a chart opener from the host, patient rows render as text", async () => {
    render(<DashboardGrid client={stubClient()} config={CONFIG} />);
    fireEvent.click((await screen.findAllByText("View underlying records"))[0]);
    expect(await screen.findByText(/2 records/)).toBeTruthy();
    expect(screen.getByText("Potassium — Synth pat-1")).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Potassium — Synth pat-1" }),
    ).toBeNull();
  });
});
