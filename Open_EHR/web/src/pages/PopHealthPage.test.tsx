import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import {
  fixtureDiabetesSummary,
  fixtureRegistryList,
  fixtureWarehouseStatus,
} from "../test/fixtures";
import { PopHealthPage } from "./PopHealthPage";

// See LabPage.test.tsx: no vitest globals here, so RTL auto-cleanup does
// not run — clean up explicitly.
afterEach(cleanup);

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    registries: vi.fn(async () => fixtureRegistryList),
    registrySummary: vi.fn(async () => fixtureDiabetesSummary),
    warehouseStatus: vi.fn(async () => fixtureWarehouseStatus),
    exploreCohort: vi.fn(async () => ({
      filters: { conditionCode: "38341003", labCode: null, comparator: null, threshold: null },
      count: "<5",
      suppressed: true,
    })),
    runEtl: vi.fn(async () => ({ processed: 3, no_target: 0, dead_letter: 0, by_type: {} })),
    ...overrides,
  } as unknown as ApiClient;
}

describe("PopHealthPage", () => {
  it("renders registry cards with aggregates, suppressed strata, and warehouse/ETL status", async () => {
    render(<PopHealthPage client={stubClient()} role="clinician" />);

    // registry card: name first (from the list), then the summary line
    // once its own request lands: member count + in-range percentage.
    expect(await screen.findByText("Diabetes registry")).toBeTruthy();
    expect(await screen.findByText(/50% in range/)).toBeTruthy();
    expect(screen.getByText(/10 with a current result/)).toBeTruthy();
    expect(screen.getByText(/members/)).toBeTruthy();
    // strata: raw count for the large stratum, suppressed marker for the small one
    expect(screen.getByText("female")).toBeTruthy();
    expect(screen.getByText("suppressed")).toBeTruthy();
    // warehouse panel: outbox tallies, coverage, dead letter detail
    expect(screen.getByText(/dead-lettered/)).toBeTruthy();
    expect(screen.getByText(/98\.6%/)).toBeTruthy();
    expect(screen.getByText(/Observation\/obs-poison/)).toBeTruthy();
    // a clinician sees the hint instead of the admin-only ETL button
    expect(screen.getByText(/ETL runs are triggered by an administrator/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Run ETL now" })).toBeNull();
  });

  it("explorer submits the filters and renders the suppressed aggregate", async () => {
    const client = stubClient();
    render(<PopHealthPage client={client} role="clinician" />);
    await screen.findByText("Diabetes registry");

    fireEvent.change(screen.getByPlaceholderText("e.g. 44054006"), {
      target: { value: "38341003" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Count cohort" }));

    await waitFor(() => expect(client.exploreCohort).toHaveBeenCalled());
    expect(client.exploreCohort).toHaveBeenCalledWith({ conditionCode: "38341003" });
    expect(await screen.findByText(/<5 \(suppressed\)/)).toBeTruthy();
  });

  it("admin can trigger an ETL run and sees the outcome", async () => {
    const client = stubClient();
    render(<PopHealthPage client={client} role="admin" />);
    await screen.findByText("Diabetes registry");

    fireEvent.click(screen.getByRole("button", { name: "Run ETL now" }));
    await waitFor(() => expect(client.runEtl).toHaveBeenCalled());
    expect(await screen.findByText(/ETL run complete: 3 events processed/)).toBeTruthy();
  });
});
