import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import {
  fixtureLabCatalog,
  fixtureLabOrder,
  fixtureQcRun,
  fixtureSpecimen,
  fixtureVerificationItem,
} from "../test/fixtures";
import { LabPage } from "./LabPage";

// This repo's vitest setup has no globals, so RTL auto-cleanup does not
// run — clean up explicitly or earlier trees leak into later queries.
afterEach(cleanup);

// Component render test with fixture (synthetic) lab data. The stub client
// stands in for the network boundary in a unit test only — shipped code
// always uses the real ApiClient.
function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    labCatalog: vi.fn(async () => fixtureLabCatalog),
    labOrders: vi.fn(async () => [fixtureLabOrder]),
    verificationQueue: vi.fn(async () => ({ items: [fixtureVerificationItem], total: 1 })),
    labSpecimens: vi.fn(async () => [fixtureSpecimen]),
    qcRuns: vi.fn(async () => [fixtureQcRun]),
    verifyLabReport: vi.fn(async () => ({})),
    releaseLabReport: vi.fn(async () => ({})),
    transitionLabOrder: vi.fn(async () => ({})),
    createLabOrder: vi.fn(async () => fixtureLabOrder),
    enterLabResults: vi.fn(async () => ({})),
    ingestLab: vi.fn(async () => ({ ingested: [], report_count: 0, result_count: 0 })),
    createQcRun: vi.fn(async () => fixtureQcRun),
    ...overrides,
  } as unknown as ApiClient;
}

// Depth pass: the lab page renders one tab panel at a time (the
// shared Tabs component), so section assertions open their tab first.
function clickTab(name: string) {
  fireEvent.click(screen.getByRole("tab", { name }));
}

describe("LabPage", () => {
  it("renders the order queue, verification queue with critical + QC-block state, specimens and QC runs", async () => {
    render(<LabPage client={stubClient()} role="clinician" />);

    // order queue (the Orders & Results tab is the default panel)
    expect(await screen.findByText("ord-lab-1")).toBeTruthy();
    // the panel name shows in the order row and the order-form select —
    // both are the same honest label
    expect(screen.getAllByText(/Basic metabolic panel \(synthetic\)/).length)
      .toBeGreaterThanOrEqual(2);
    // verification queue: stage, critical flag, QC block, result line
    clickTab("Verification");
    expect(await screen.findByText(/awaiting verification/)).toBeTruthy();
    expect(screen.getByText("CRITICAL")).toBeTruthy();
    expect(screen.getByText(/QC blocked: 2823-3/)).toBeTruthy();
    expect(screen.getByText(/Potassium \(synthetic\): 6.4 mmol\/L \[HH\]/)).toBeTruthy();
    // the queue's Verify button is disabled while the QC gate is active
    const verify = screen.getByRole("button", { name: "Verify" }) as HTMLButtonElement;
    expect(verify.disabled).toBe(true);
    // specimen tracking shows the accession id (Specimens tab)
    clickTab("Specimens");
    expect(await screen.findByText("ACC-20260115-0001")).toBeTruthy();
    // QC section: the failed run renders FAIL and the gate banner names the analyte
    clickTab("QC & EQA");
    expect(await screen.findByText("FAIL")).toBeTruthy();
    expect(screen.getByText(/QC gate active/)).toBeTruthy();
  });

  it("calls verify through the real client method when the report is not QC-blocked", async () => {
    const unblocked = { ...fixtureVerificationItem, qc_blocked: [] };
    const client = stubClient({
      verificationQueue: vi.fn(async () => ({ items: [unblocked], total: 1 })),
      qcRuns: vi.fn(async () => []),
    });
    render(<LabPage client={client} role="clinician" />);
    clickTab("Verification");
    const verify = await screen.findByRole("button", { name: "Verify" });
    fireEvent.click(verify);
    await waitFor(() => {
      expect(client.verifyLabReport).toHaveBeenCalledWith("rep-lab-1");
    });
  });

  it("hides write controls from the viewer role (server enforces regardless)", async () => {
    render(<LabPage client={stubClient()} role="viewer" />);
    expect(await screen.findByText("ord-lab-1")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Place order" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Verify" })).toBeNull();
    clickTab("Verification");
    expect(await screen.findByText(/Verification is clinician\/admin only/)).toBeTruthy();
  });
});
