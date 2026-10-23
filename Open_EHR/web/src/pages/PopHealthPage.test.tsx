import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import {
  fixtureCohortExport,
  fixtureCohortSnapshots,
  fixtureDiabetesSummary,
  fixtureMeasureResultSuppressed,
  fixtureMeasureResultVisible,
  fixtureQualityMeasures,
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
    qualityMeasures: vi.fn(async () => ({
      measures: fixtureQualityMeasures,
      total: fixtureQualityMeasures.length,
    })),
    qualityMeasureResult: vi.fn(async (key: string) =>
      key === "asthma-review"
        ? fixtureMeasureResultSuppressed
        : fixtureMeasureResultVisible,
    ),
    registrySnapshots: vi.fn(async () => fixtureCohortSnapshots),
    takeRegistrySnapshot: vi.fn(async () => fixtureCohortSnapshots.snapshots[1]),
    exportRegistry: vi.fn(async () => fixtureCohortExport),
    ...overrides,
  } as unknown as ApiClient;
}


// UX pass: measures / explorer / warehouse are tabs now — open the
// tab under test first; every assertion below is unchanged.
async function openTab(name: string) {
  fireEvent.click(await screen.findByRole("tab", { name }));
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
    // warehouse panel (its own tab now): outbox tallies, coverage,
    // dead letter detail
    await openTab("Warehouse & ETL");
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
    await openTab("Cohort explorer");

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

  it("quality measures render rates and honest suppressed states", async () => {
    render(<PopHealthPage client={stubClient()} role="clinician" />);
    await openTab("Quality measures");
    // visible measure: rate + numerator/denominator
    expect(await screen.findByText(/60%/)).toBeTruthy();
    expect(screen.getByText(/6 of 10 persons/)).toBeTruthy();
    // suppressed measure: no rate, explicit suppression explanation
    expect(await screen.findByText(/a rate would reveal the suppressed numerator/)).toBeTruthy();
    expect(screen.getByText("Diabetes — HbA1c control (< 8.0)")).toBeTruthy();
    expect(screen.getByText("Asthma — annual review")).toBeTruthy();
  });

  it("registry snapshots render, and snapshot/export actions are capability-shaped", async () => {
    const client = stubClient();
    render(<PopHealthPage client={client} role="clinician" />);
    await screen.findByText("Diabetes registry");
    // snapshot history renders with counts + definition version
    expect(await screen.findByText(/10 in cohort · definition v1/)).toBeTruthy();
    expect(screen.getByText(/12 in cohort · definition v1/)).toBeTruthy();
    // a clinician holds neither pophealth.run nor pophealth.export in
    // the default matrix, so neither action button renders
    expect(screen.queryByRole("button", { name: "Take snapshot" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Export aggregates" })).toBeNull();
  });

  it("admin can snapshot and export a registry", async () => {
    // jsdom has no URL.createObjectURL; stub it so the download link
    // the page builds for the export can render.
    (URL as unknown as { createObjectURL: unknown }).createObjectURL =
      vi.fn(() => "blob:mock-export");
    const client = stubClient();
    render(<PopHealthPage client={client} role="admin" />);
    await screen.findByText("Diabetes registry");

    fireEvent.click(await screen.findByRole("button", { name: "Take snapshot" }));
    await waitFor(() => expect(client.takeRegistrySnapshot).toHaveBeenCalledWith("diabetes"));

    fireEvent.click(screen.getByRole("button", { name: "Export aggregates" }));
    await waitFor(() => expect(client.exportRegistry).toHaveBeenCalledWith("diabetes"));
    expect(await screen.findByText(/No patient-level rows exist in this export/)).toBeTruthy();
    expect(screen.getByText(/Download export JSON/)).toBeTruthy();
  });
});
