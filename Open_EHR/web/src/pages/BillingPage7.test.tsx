import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { Claim, DenialItem, PatientStatement } from "../api/types";
import { BillingPage } from "./BillingPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// Phase 7 sections (app.revenue): HIM coding queue, 835 remittance,
// denials workqueue, patient statement + recorded payments. The stub
// client stands in for the network boundary in unit tests only.

const deniedClaim: Claim & { financials: DenialItem["claim"]["financials"] } = {
  resourceType: "Claim",
  id: "claim-denied",
  status: "denied",
  patient: { reference: "Patient/pat-3" },
  total: { value: 52, currency: "USD" },
  item: [],
  appealCount: 0,
  statusHistory: [],
  financials: {
    total: 52, paid: 0, adjustments: 0, patient_responsibility: 0,
    patient_paid: 0, patient_due: 0, balance: 52,
    remittance_count: 1, last_outcome: "denied",
  },
};

const denialItem: DenialItem = {
  claim: deniedClaim,
  kind: "denied",
  reasons: [
    {
      group: "CO", reason: "197", amount: 52, level: "claim",
      line_code: null, category: "authorisation",
      display: "Precertification/authorization/notification absent",
      check_number: "CHK-DENY",
    },
  ],
  work: { assignee: null, notes: [], outcome: null, outcome_claim_id: null },
};

const statementFixture: PatientStatement = {
  patient: "Patient/pat-1",
  generated_at: "2026-01-10T12:00:00Z",
  claims: [
    {
      claim_id: "claim-partial", status: "partially-paid",
      total: 240, paid: 180, adjustments: 30,
      patient_responsibility: 20, patient_paid: 0, patient_due: 20,
      balance: 10, remittance_count: 1, last_outcome: "paid",
    },
  ],
  totals: {
    total: 240, paid: 180, adjustments: 30, patient_responsibility: 20,
    patient_paid: 0, patient_due: 20, balance: 10,
  },
  disclaimer:
    "Payments recorded here are ledger entries for payments the practice has taken; this system has no card processor or payment gateway.",
};

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    claims: vi.fn(async () => []),
    getClaim: vi.fn(async () => deniedClaim),
    generateClaim: vi.fn(),
    transitionClaim: vi.fn(),
    export837: vi.fn(async () => ""),
    codingQueue: vi.fn(async () => [
      {
        encounter_id: "enc-1", patient: "Patient/pat-1",
        encounter_status: "finished", signed_notes: 1, reason: "finished",
      },
    ]),
    assignCodes: vi.fn(async () => ({
      encounter_id: "enc-1", patient: "Patient/pat-1",
      diagnoses: [{ code: "E11.9" }], procedures: [], coded_by: "dev-admin",
    })),
    remittanceBatches: vi.fn(async () => []),
    remittanceExceptions: vi.fn(async () => []),
    ingest835: vi.fn(async () => ({
      batch: {
        id: "batch-1", check_number: "CHK-778899", payer: "SYNTHETIC PAYER",
        total_amount: 302, posted: 3, exceptions: 2,
        received_by: "dev-admin",
      },
      posted: [],
      exceptions: [],
    })),
    denials: vi.fn(async () => ({
      items: [denialItem], total: 1,
      outcomes: ["appealed", "rebilled", "written-off", "closed"],
    })),
    assignDenial: vi.fn(async () => denialItem),
    noteDenial: vi.fn(async () => denialItem),
    resolveDenial: vi.fn(async () => denialItem),
    rebillClaim: vi.fn(async () => ({ ...deniedClaim, id: "claim-corrected", status: "draft" })),
    statement: vi.fn(async () => statementFixture),
    recordPatientPayment: vi.fn(async () => ({
      id: "pay-1", claim_id: "claim-partial", patient: "Patient/pat-1",
      amount: 12.5, method: "card", recorded_by: "dev-patient",
    })),
    ...overrides,
  } as unknown as ApiClient;
}

describe("BillingPage Phase 7 sections", () => {
  it("assigns codes from the HIM coding queue through the client", async () => {
    const client = stubClient();
    render(<BillingPage client={client} role="admin" />);
    expect(await screen.findByText("HIM coding queue")).toBeTruthy();
    expect(await screen.findByText("enc-1")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Diagnosis codes for enc-1"), {
      target: { value: "E11.9, I10" },
    });
    fireEvent.change(screen.getByLabelText("Procedure codes for enc-1"), {
      target: { value: "99214" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Assign codes" }));
    await waitFor(() => {
      expect(client.assignCodes).toHaveBeenCalledWith("enc-1", {
        diagnoses: [{ code: "E11.9" }, { code: "I10" }],
        procedures: [{ code: "99214" }],
      });
    });
  });

  it("posts an 835 remittance and reports posted + exception counts", async () => {
    const client = stubClient();
    render(<BillingPage client={client} role="admin" />);
    expect(await screen.findByText("Remittance (X12 835)")).toBeTruthy();
    expect(screen.getByText(/no clearinghouse connection exists/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("835 remittance text"), {
      target: { value: "ST*835*0001~\nSE*2*0001~\n" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Post 835 remittance" }));
    await waitFor(() => {
      expect(client.ingest835).toHaveBeenCalled();
    });
    expect(await screen.findByText(/3 posted, 2 exception\(s\)/)).toBeTruthy();
  });

  it("renders denial reasons and routes rebill + resolve through the client", async () => {
    const client = stubClient();
    render(<BillingPage client={client} role="admin" />);
    expect(await screen.findByText("Denials workqueue")).toBeTruthy();
    expect(await screen.findByText("claim-denied")).toBeTruthy();
    expect(screen.getByText(/CO\/197 \(authorisation\)/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Rebill (corrected claim)" }));
    await waitFor(() => {
      expect(client.rebillClaim).toHaveBeenCalledWith("claim-denied");
    });
    fireEvent.change(screen.getByLabelText("Outcome for claim-denied"), {
      target: { value: "rebilled" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Resolve" }));
    await waitFor(() => {
      expect(client.resolveDenial).toHaveBeenCalledWith(
        "claim-denied", "rebilled", "claim-corrected");
    });
  });

  it("shows the patient their statement and records a payment", async () => {
    const client = stubClient({
      claims: vi.fn(async () => [
        { resourceType: "Claim", id: "claim-mine", patient: { reference: "Patient/pat-1" } },
      ]),
    });
    render(<BillingPage client={client} role="patient" />);
    expect(await screen.findByText("Your statement")).toBeTruthy();
    expect(await screen.findByText("claim-partial")).toBeTruthy();
    expect(screen.getByText(/Payments recorded here are ledger entries/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Payment amount for claim-partial"), {
      target: { value: "12.50" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Record payment" }));
    await waitFor(() => {
      expect(client.recordPatientPayment).toHaveBeenCalledWith(
        "claim-partial", 12.5, "card");
    });
    // Patients never see the staff workqueues.
    expect(screen.queryByText("Denials workqueue")).toBeNull();
    expect(screen.queryByText("HIM coding queue")).toBeNull();
  });
});
