import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { AnalyticsPage } from "./AnalyticsPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// Wave A item 9 (user-reported defect): "View indicator" looked dead —
// the detail card rendered AFTER the full published-indicators list,
// viewports below the click, with no scroll, focus move, or visible
// selected state. These tests pin the fix: the detail renders ABOVE
// the list, scrollIntoView fires on selection, the heading takes
// focus, and the clicked row/button show the selected state.
// Fixtures copy the drill test's real backend shapes.

const DEFINITION = {
  name: "Critical reports awaiting closure",
  title: "Critical reports awaiting closure",
  source: { type: "named", name: "lab.critical-open" },
  direction: "lower-better",
  thresholds: { warn: 1, critical: 5 },
  unit: "reports",
};

const VALUE = {
  key: "operations-critical-open",
  value: 2,
  unit: "reports",
  target: null,
  status: "warn",
  evaluated_at: "2026-10-19T17:00:00+00:00",
  source: { type: "named", name: "lab.critical-open" },
  detail: { named: "lab.critical-open" },
};

const SECOND_DEFINITION = {
  name: "Diabetes control rate",
  source: { type: "measure", measure_key: "diabetes-control" },
  direction: "higher-better",
  unit: "%",
};

const SECOND_VALUE = {
  key: "diabetes-control-rate",
  value: 42.5,
  unit: "%",
  target: null,
  status: "ok",
  evaluated_at: "2026-10-19T17:00:00+00:00",
  source: { type: "measure", measure_key: "diabetes-control" },
  detail: { measure: "diabetes-control" },
};

function definitionEntry(key: string, payload: unknown) {
  return {
    kind: "indicator",
    key,
    version: 1,
    status: "published",
    payload,
    created_by: "dev-configurator",
    updated_by: "dev-configurator",
    created_at: "2026-10-19T00:00:00+00:00",
    updated_at: "2026-10-19T00:00:00+00:00",
  };
}

function stubClient() {
  const client = {
    request: vi.fn(async (path: string) => {
      if (path === "/analytics/datasets") return { items: [], total: 0 };
      if (path === "/analytics/vocabulary/coverage") {
        return {
          vocab_concept_total: 0,
          vocab_relationship_total: 0,
          by_vocabulary: [],
          by_domain: [],
          curated_concept_map_count: 12,
          concept_map_baseline: { table: "concept_map", count: 12, note: "x" },
        };
      }
      if (path === "/studio/config/analytics_view/effective") return [];
      if (path === "/indicators/definitions") {
        return {
          items: [
            definitionEntry("operations-critical-open", DEFINITION),
            definitionEntry("diabetes-control-rate", SECOND_DEFINITION),
          ],
          total: 2,
        };
      }
      if (path === "/indicators/operations-critical-open/value") return VALUE;
      if (path === "/indicators/diabetes-control-rate/value") return SECOND_VALUE;
      throw new Error(`Unexpected request: ${path}`);
    }),
    indicatorRecords: vi.fn(async () => {
      throw new Error("not used in this test file");
    }),
  } as unknown as ApiClient;
  return client;
}

const scrollSpy = vi.fn();

beforeEach(() => {
  scrollSpy.mockClear();
  Element.prototype.scrollIntoView = scrollSpy;
});

describe("AnalyticsPage indicator selection visibility (Wave A item 9)", () => {
  it("renders the detail ABOVE the list, scrolls to it, focuses its heading, and marks the row selected", async () => {
    const client = stubClient();
    const { container } = render(
      <AnalyticsPage client={client} role="clinician" onOpenChart={() => {}} />,
    );

    fireEvent.click(screen.getByRole("tab", { name: "Indicators" }));
    const buttons = await screen.findAllByText("View indicator");
    expect(buttons).toHaveLength(2);
    // No detail card before any selection.
    expect(container.querySelector(".indicator-detail")).toBeNull();
    expect(scrollSpy).not.toHaveBeenCalled();

    fireEvent.click(buttons[0]);

    // The detail loaded (value badge proves the fetch resolved).
    expect(await screen.findByText("warn")).toBeTruthy();
    const detail = container.querySelector(".indicator-detail");
    expect(detail).not.toBeNull();

    // Position: the detail card precedes the "Published indicators"
    // list card in document order.
    const listHeading = screen.getByText("Published indicators");
    expect(
      detail!.compareDocumentPosition(listHeading) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    // Scroll + focus: scrollIntoView fired, and the detail heading is
    // the focused element (keyboard users land on the result).
    expect(scrollSpy).toHaveBeenCalled();
    const heading = detail!.querySelector("h3");
    expect(heading).not.toBeNull();
    expect(document.activeElement).toBe(heading);

    // Selected state: the clicked button is pressed, the other is not,
    // and its row carries the selected treatment class.
    const after = screen.getAllByText("View indicator");
    expect(after[0].getAttribute("aria-pressed")).toBe("true");
    expect(after[1].getAttribute("aria-pressed")).toBe("false");
    expect(after[0].closest("li")?.className).toContain("result-row-selected");
    expect(after[1].closest("li")?.className).not.toContain(
      "result-row-selected",
    );
  });

  it("moves the selection (and the scroll) when a second indicator is picked", async () => {
    const client = stubClient();
    render(
      <AnalyticsPage client={client} role="clinician" onOpenChart={() => {}} />,
    );

    fireEvent.click(screen.getByRole("tab", { name: "Indicators" }));
    const buttons = await screen.findAllByText("View indicator");
    fireEvent.click(buttons[0]);
    expect(await screen.findByText("warn")).toBeTruthy();
    const callsAfterFirst = scrollSpy.mock.calls.length;
    expect(callsAfterFirst).toBeGreaterThan(0);

    fireEvent.click(screen.getAllByText("View indicator")[1]);
    expect(await screen.findByText("ok")).toBeTruthy();
    expect(scrollSpy.mock.calls.length).toBeGreaterThan(callsAfterFirst);
    const after = screen.getAllByText("View indicator");
    expect(after[0].getAttribute("aria-pressed")).toBe("false");
    expect(after[1].getAttribute("aria-pressed")).toBe("true");
  });
});
