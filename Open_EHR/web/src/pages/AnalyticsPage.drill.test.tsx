import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  ApiClient,
  IndicatorRecordsResponse,
} from "../api/client";
import { AnalyticsPage } from "./AnalyticsPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// WS4b analysis drill-down: value -> definition -> records -> record.
// Fixtures copy the REAL backend shapes (backend/app/indicators.py):
// GET /indicators/definitions returns {items: [_out rows], total};
// GET /indicators/{key}/value returns evaluate_indicator's output;
// GET /indicators/{key}/records returns {key, definition, value,
// records: [{id, label, detail, at, patient_id}], records_supported,
// reason}.

const CRITICAL_DEFINITION = {
  name: "Critical reports awaiting closure",
  title: "Critical reports awaiting closure",
  source: { type: "named", name: "lab.critical-open" },
  direction: "lower-better",
  thresholds: { warn: 1, critical: 5 },
  unit: "reports",
};

const CRITICAL_VALUE = {
  key: "operations-critical-open",
  value: 2,
  unit: "reports",
  target: null,
  status: "warn",
  evaluated_at: "2026-10-19T17:00:00+00:00",
  source: { type: "named", name: "lab.critical-open" },
  detail: { named: "lab.critical-open" },
};

const CRITICAL_RECORDS: IndicatorRecordsResponse = {
  key: "operations-critical-open",
  definition: CRITICAL_DEFINITION as IndicatorRecordsResponse["definition"],
  value: CRITICAL_VALUE as IndicatorRecordsResponse["value"],
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
      label: "Sodium — unknown patient",
      detail: "Report rep-2 · order ord-2 · order status released · critical value",
      at: "2026-10-19T11:00:00+00:00",
      patient_id: null,
    },
  ],
  records_supported: true,
  reason: null,
};

const MEASURE_DEFINITION = {
  name: "Diabetes control rate",
  source: { type: "measure", measure_key: "diabetes-control" },
  direction: "higher-better",
  unit: "%",
};

const MEASURE_VALUE = {
  key: "diabetes-control-rate",
  value: 42.5,
  unit: "%",
  target: null,
  status: "ok",
  evaluated_at: "2026-10-19T17:00:00+00:00",
  source: { type: "measure", measure_key: "diabetes-control" },
  detail: {
    measure: "diabetes-control",
    numerator: 17,
    denominator: 40,
    suppressed: false,
    as_of: "2026-10-19",
  },
};

const MEASURE_RECORDS: IndicatorRecordsResponse = {
  key: "diabetes-control-rate",
  definition: MEASURE_DEFINITION as IndicatorRecordsResponse["definition"],
  value: MEASURE_VALUE as IndicatorRecordsResponse["value"],
  records: [],
  records_supported: false,
  reason:
    "Value is an aggregate warehouse measure ('diabetes-control') computed over the OMOP warehouse; individual records are explored via Analytics query (POST /analytics/query), not enumerated here.",
};

function definitionEntry(key: string, payload: unknown, version = 1) {
  return {
    kind: "indicator",
    key,
    version,
    status: "published",
    payload,
    created_by: "dev-configurator",
    updated_by: "dev-configurator",
    created_at: "2026-10-19T00:00:00+00:00",
    updated_at: "2026-10-19T00:00:00+00:00",
  };
}

function stubClient() {
  const recordsByKey: Record<string, IndicatorRecordsResponse> = {
    "operations-critical-open": CRITICAL_RECORDS,
    "diabetes-control-rate": MEASURE_RECORDS,
  };
  const indicatorRecords = vi.fn(async (key: string) => {
    const found = recordsByKey[key];
    if (!found) throw new Error(`no records fixture for ${key}`);
    return found;
  });
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
            definitionEntry("operations-critical-open", CRITICAL_DEFINITION),
            definitionEntry("diabetes-control-rate", MEASURE_DEFINITION),
          ],
          total: 2,
        };
      }
      if (path === "/indicators/operations-critical-open/value") return CRITICAL_VALUE;
      if (path === "/indicators/diabetes-control-rate/value") return MEASURE_VALUE;
      throw new Error(`Unexpected request: ${path}`);
    }),
    indicatorRecords,
  } as unknown as ApiClient;
  return { client, indicatorRecords };
}

describe("AnalyticsPage indicator drill (WS4b)", () => {
  it("drills value -> definition -> records -> patient chart in 1 click", async () => {
    const { client, indicatorRecords } = stubClient();
    const onOpenChart = vi.fn();
    render(<AnalyticsPage client={client} role="clinician" onOpenChart={onOpenChart} />);

    fireEvent.click(screen.getByRole("tab", { name: "Indicators" }));
    // The published indicators browser lists both indicators.
    expect(await screen.findByText("Critical reports awaiting closure")).toBeTruthy();
    expect(screen.getByText("Diabetes control rate")).toBeTruthy();

    fireEvent.click(screen.getAllByText("View indicator")[0]);

    // Value, big, with unit + status as text+tone badge.
    expect(await screen.findByText("warn")).toBeTruthy();
    expect(screen.getByText(/Evaluated at 2026-10-19/)).toBeTruthy();
    // Definition rendered readably: name, source, direction, thresholds.
    expect(screen.getByText(/Named source lab\.critical-open/)).toBeTruthy();
    expect(screen.getByText(/lower-better \(lower is better\)/)).toBeTruthy();
    expect(screen.getByText(/Warn at 1 · Critical at 5/)).toBeTruthy();

    fireEvent.click(screen.getByText("View underlying records"));
    expect(indicatorRecords).toHaveBeenCalledWith("operations-critical-open");
    // Count check: header N equals the reported value (2) from the payload.
    expect(await screen.findByText(/2 records — matches the reported value 2/)).toBeTruthy();
    expect(screen.getByText(/Report rep-1 · order ord-1/)).toBeTruthy();

    // Patient-linked row is a button opening that chart in 1 click.
    fireEvent.click(screen.getByRole("button", { name: "Potassium — Synth pat-1" }));
    expect(onOpenChart).toHaveBeenCalledTimes(1);
    expect(onOpenChart).toHaveBeenCalledWith("pat-1");
    // The non-patient row renders as text, not a button.
    expect(
      screen.queryByRole("button", { name: "Sodium — unknown patient" }),
    ).toBeNull();
    expect(screen.getByText("Sodium — unknown patient")).toBeTruthy();
  });

  it("shows the backend reason plainly for an unsupported source, with no table", async () => {
    const { client } = stubClient();
    render(<AnalyticsPage client={client} role="clinician" onOpenChart={() => {}} />);

    fireEvent.click(screen.getByRole("tab", { name: "Indicators" }));
    expect(await screen.findByText("Diabetes control rate")).toBeTruthy();
    fireEvent.click(screen.getAllByText("View indicator")[1]);
    expect(await screen.findByText(/Measure diabetes-control/)).toBeTruthy();

    fireEvent.click(screen.getByText("View underlying records"));
    expect(
      await screen.findByText(/aggregate warehouse measure \('diabetes-control'\)/),
    ).toBeTruthy();
    // No fake records table for an unsupported source.
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText(/records — matches/)).toBeNull();
  });
});
