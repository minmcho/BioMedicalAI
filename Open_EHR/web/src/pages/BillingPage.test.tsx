import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { fixtureClaim } from "../test/fixtures";
import { BillingPage } from "./BillingPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// Component render test with fixture (synthetic) claim data. The stub
// client stands in for the network boundary in a unit test only — shipped
// code always uses the real ApiClient.
function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    claims: vi.fn(async () => [fixtureClaim]),
    getClaim: vi.fn(async () => fixtureClaim),
    generateClaim: vi.fn(async () => fixtureClaim),
    transitionClaim: vi.fn(async () => fixtureClaim),
    export837: vi.fn(async () => "ISA*00*~\nCLM*claim-1*100.00~\n"),
    // Phase 7 sections mount alongside the claims UI; the stub serves
    // their empty states so these Phase 4A tests keep asserting the
    // claims behaviour they were written for.
    codingQueue: vi.fn(async () => []),
    assignCodes: vi.fn(async () => ({
      encounter_id: "enc-1", patient: "Patient/pat-1",
      diagnoses: [], procedures: [], coded_by: "dev-admin",
    })),
    remittanceBatches: vi.fn(async () => []),
    remittanceExceptions: vi.fn(async () => []),
    ingest835: vi.fn(async () => ({
      batch: {
        id: "batch-1", check_number: "CHK-1", payer: "Synthetic Payer",
        total_amount: 0, posted: 0, exceptions: 0, received_by: "dev-admin",
      },
      posted: [],
      exceptions: [],
    })),
    denials: vi.fn(async () => ({
      items: [], total: 0,
      outcomes: ["appealed", "rebilled", "written-off", "closed"],
    })),
    assignDenial: vi.fn(),
    noteDenial: vi.fn(),
    resolveDenial: vi.fn(),
    rebillClaim: vi.fn(async () => fixtureClaim),
    statement: vi.fn(async () => ({
      patient: "Patient/pat-1", generated_at: "2026-01-10T12:00:00Z",
      claims: [],
      totals: {
        total: 0, paid: 0, adjustments: 0, patient_responsibility: 0,
        patient_paid: 0, patient_due: 0, balance: 0,
      },
      disclaimer: "Payments recorded here are ledger entries.",
    })),
    recordPatientPayment: vi.fn(),
    ...overrides,
  } as unknown as ApiClient;
}

describe("BillingPage", () => {
  it("renders the claims list, then the detail with line items, totals, the unpriced flag and history", async () => {
    render(<BillingPage client={stubClient()} role="admin" />);
    expect(await screen.findByText("claim-1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "View" }));

    // line items: priced lines show their prices, the unscheduled code is
    // flagged unpriced (charged at 0, never dropped)
    expect(await screen.findByText("Electrocardiogram")).toBeTruthy();
    expect(screen.getByText("Synthetic unscheduled test")).toBeTruthy();
    // "unpriced" appears in the explainer hint AND as the line's flag cell
    expect(screen.getAllByText("unpriced").length).toBeGreaterThanOrEqual(2);
    // the total shows in both the list row and the detail header
    expect(screen.getAllByText("USD 100.00").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/Diagnoses:/)).toBeTruthy();
    expect(screen.getByText(/draft → ready/)).toBeTruthy();
    // a submitted claim can be marked paid/denied and exported
    expect(screen.getByRole("button", { name: "Record paid" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Download 837 export" })).toBeTruthy();
  });

  it("runs a lifecycle transition through the client when a manager clicks it", async () => {
    const client = stubClient();
    render(<BillingPage client={client} role="admin" />);
    fireEvent.click(await screen.findByRole("button", { name: "View" }));
    fireEvent.click(await screen.findByRole("button", { name: "Record paid" }));
    await waitFor(() => {
      expect(client.transitionClaim).toHaveBeenCalledWith("claim-1", "paid");
    });
  });

  it("generates a claim from an encounter id through the client", async () => {
    const client = stubClient();
    render(<BillingPage client={client} role="configurator" />);
    fireEvent.change(screen.getByLabelText("Encounter id"), {
      target: { value: "enc-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Generate claim from encounter" }));
    await waitFor(() => {
      expect(client.generateClaim).toHaveBeenCalledWith("enc-1");
    });
  });

  it("patients see their claims read-only: no generate form, no lifecycle buttons", async () => {
    render(<BillingPage client={stubClient()} role="patient" />);
    expect(await screen.findByText("claim-1")).toBeTruthy();
    expect(screen.getByText(/your claims/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Generate claim from encounter" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "View" }));
    expect(await screen.findByText("Electrocardiogram")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Record paid" })).toBeNull();
  });

  it("shows an honest empty state when there are no claims", async () => {
    const client = stubClient({ claims: vi.fn(async () => []) });
    render(<BillingPage client={client} role="clinician" />);
    expect(await screen.findByText("No claims in this view.")).toBeTruthy();
  });
});
