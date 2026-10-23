import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { DashboardConfig } from "../api/types";
import { DashboardGrid } from "./DashboardView";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// Phase 13 visualisation: the shared dashboard renderer draws chart
// widgets (line / bar / gauge / table / big-number) from the widget's
// data endpoint + config. Hand-rolled SVG, no chart library — these
// tests pin the SVG output against fixture endpoint data.

const CONFIG: DashboardConfig = {
  title: "Test dashboard",
  widgets: [
    {
      id: "big",
      title: "Occupied beds",
      endpoint: "/people/analytics/overview",
      display: "big-number",
      field: "occupied",
    },
    {
      id: "gauge",
      title: "Roster coverage",
      endpoint: "/people/analytics/coverage",
      display: "gauge",
      field: "overall_pct",
      max: 100,
      thresholds: { warn: 90, critical: 75 },
      unit: "%",
    },
    {
      id: "line",
      title: "Census trend",
      endpoint: "/people/analytics/census-series?days=3",
      display: "line",
      labelField: "date",
      series: [
        { field: "admissions", label: "Admissions" },
        { field: "census", label: "Census" },
      ],
    },
    {
      id: "bar",
      title: "Unit occupancy",
      endpoint: "/people/analytics/units",
      display: "bar",
      labelField: "unit",
      series: [{ field: "occupied", label: "Occupied" }],
    },
    {
      id: "table",
      title: "Units table",
      endpoint: "/people/analytics/units",
      display: "table",
      columns: [
        { field: "unit", title: "Unit" },
        { field: "occupied", title: "Occupied" },
      ],
    },
    {
      id: "broken",
      title: "Broken widget",
      endpoint: "/people/analytics/broken",
      display: "total",
    },
  ],
};

const DATA: Record<string, unknown> = {
  "/people/analytics/overview": { occupied: 7, total: 3 },
  "/people/analytics/coverage": { overall_pct: 82 },
  "/people/analytics/census-series?days=3": {
    items: [
      { date: "2026-10-16", admissions: 2, discharges: 1, census: 4 },
      { date: "2026-10-17", admissions: 3, discharges: 2, census: 5 },
      { date: "2026-10-18", admissions: 1, discharges: 0, census: 6 },
    ],
  },
  "/people/analytics/units": {
    items: [
      { unit: "unit-medsurg", occupied: 5, beds_total: 12 },
      { unit: "unit-icu", occupied: 2, beds_total: 6 },
    ],
    total: 2,
  },
};

function stubClient(): ApiClient {
  return {
    request: vi.fn(async (path: string) => {
      if (path === "/people/analytics/broken") {
        throw new Error("boom");
      }
      return DATA[path];
    }),
  } as unknown as ApiClient;
}

describe("DashboardView chart widgets (Phase 13)", () => {
  it("renders big-number, gauge, line, bar and table widgets from endpoint data", async () => {
    const { container } = render(
      <DashboardGrid client={stubClient()} config={CONFIG} />,
    );
    // big-number
    expect(await screen.findByText("7")).toBeTruthy();
    // gauge value + unit (rendered as "82 %" in one value block)
    expect(
      await screen.findByText((content) => content.startsWith("82")),
    ).toBeTruthy();
    const gaugeSvg = container.querySelector(
      'svg[aria-label="Roster coverage: 82 of 100"]',
    );
    expect(gaugeSvg).toBeTruthy();
    // 82 is below warn=90 but above critical=75 -> warning colour fill.
    const fill = gaugeSvg?.querySelectorAll("rect")[1];
    expect(fill?.getAttribute("fill")).toBe("#b25f00");
    // line chart: two polylines (one per series)
    const lineSvg = container.querySelector(
      'svg[aria-label="Census trend line chart"]',
    );
    expect(lineSvg?.querySelectorAll("polyline")).toHaveLength(2);
    // bar chart: one rect per item for the single series
    const barSvg = container.querySelector(
      'svg[aria-label="Unit occupancy bar chart"]',
    );
    expect(barSvg?.querySelectorAll("rect")).toHaveLength(2);
    // table widget renders the configured columns (the bar chart also
    // labels unit-icu, so it appears more than once in the document)
    expect(screen.getByText("unit-medsurg")).toBeTruthy();
    expect(screen.getAllByText("unit-icu").length).toBeGreaterThan(0);
  });

  it("a failing widget degrades alone; the rest still render", async () => {
    render(<DashboardGrid client={stubClient()} config={CONFIG} />);
    expect(await screen.findByText("unavailable")).toBeTruthy();
    expect(screen.getByText("Broken widget")).toBeTruthy();
    expect(screen.getByText("Occupied beds")).toBeTruthy();
  });

  it("gauge at/above the warn threshold uses the on-target colour", async () => {
    const client = {
      request: vi.fn(async () => ({ overall_pct: 95 })),
    } as unknown as ApiClient;
    const { container } = render(
      <DashboardGrid
        client={client}
        config={{
          title: "G",
          widgets: [
            {
              id: "g",
              title: "Coverage",
              endpoint: "/x",
              display: "gauge",
              field: "overall_pct",
              max: 100,
              thresholds: { warn: 90, critical: 75 },
            },
          ],
        }}
      />,
    );
    expect(await screen.findByText("95")).toBeTruthy();
    const fill = container.querySelectorAll("svg rect")[1];
    expect(fill?.getAttribute("fill")).toBe("#0b6e4f");
  });
});
