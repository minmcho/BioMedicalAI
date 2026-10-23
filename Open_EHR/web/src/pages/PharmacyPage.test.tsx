import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { fixtureDispenseQueue, fixtureFormulary, fixtureStock } from "../test/fixtures";
import { PharmacyPage } from "./PharmacyPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// Wave A (PHARM-B1): the dispense queue annotates verification state;
// the fixture order is currently verified so Dispense is enabled.
const verifiedQueue = fixtureDispenseQueue.map((item) => ({
  ...item,
  verification: {
    verified: true,
    verified_by: "dev-clinician",
    verified_at: "2026-01-15T10:00:00Z",
    state: "current",
    reason: "current verification on file",
  },
}));

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    dispenseQueue: vi.fn(async () => verifiedQueue),
    stock: vi.fn(async () => fixtureStock),
    formulary: vi.fn(async () => fixtureFormulary),
    // UX pass (N-9): clinicians land on the Verification tab, whose
    // panel calls these on mount — the stub serves empty queues.
    pharmacyVerificationQueue: vi.fn(async () => []),
    durQueue: vi.fn(async () => ({
      items: [],
      interaction_checking: { status: "no-source", message: "Not configured in this stub." },
    })),
    dispense: vi.fn(async () => ({
      dispense: { resourceType: "MedicationDispense", id: "disp-1" },
      medication_request: fixtureDispenseQueue[0].request,
      stock_after: 119,
    })),
    // Wave A (PHARM-M2): med-admin is the five-rights BCMA scan; the
    // page fills real payloads from the barcode endpoints after a
    // dispense, and administers through bcmaAdminister.
    barcodeWristband: vi.fn(async () => ({
      payload: "OCWB1|pat-1|19620314|42",
      patient_id: "pat-1",
      dob: "1962-03-14",
    })),
    barcodeMedLabel: vi.fn(async () => ({
      payload: "OCML1|disp-1|SEED-1049221|07",
      dispense_id: "disp-1",
      lot: "SEED-1049221",
    })),
    bcmaAdminister: vi.fn(async () => ({
      administration: {
        resourceType: "MedicationAdministration",
        id: "adm-1",
        status: "completed",
        medicationCodeableConcept: { text: "Lisinopril 10 MG Oral Tablet" },
        subject: { reference: "Patient/pat-1" },
        effectiveDateTime: "2026-01-15T11:00:00Z",
      },
      bcma: {
        checks: [
          { right: "right-patient", result: "pass", detail: "matches" },
        ],
        override: null,
      },
    })),
    ...overrides,
  } as unknown as ApiClient;
}

// UX pass (N-9): a clinician holds pharmacy.verify, so the page now
// lands on the Verification tab; the dispense tests open the
// Dispense & stock tab first (the journey assertions are unchanged).
async function openDispenseTab() {
  fireEvent.click(await screen.findByRole("tab", { name: "Dispense & stock" }));
}

describe("PharmacyPage", () => {
  it("renders the dispense queue with live stock and the stock table", async () => {
    render(<PharmacyPage client={stubClient()} role="clinician" />);
    await openDispenseTab();
    expect(await screen.findByText("med-1")).toBeTruthy();
    expect(screen.getByText("120 on hand")).toBeTruthy();
    expect(screen.getByText("Lisinopril 10 MG Oral Tablet (synthetic)")).toBeTruthy();
    expect(screen.getByText(/terminology\/formulary v1/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Dispense" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Administer" })).toBeTruthy();
    // Wave A: the current verification is shown on the queue row.
    expect(screen.getByText(/verified by dev-clinician/)).toBeTruthy();
  });

  it("dispenses through the client and pre-fills the BCMA scan form from the outcome", async () => {
    const client = stubClient();
    render(<PharmacyPage client={client} role="clinician" />);
    await openDispenseTab();
    fireEvent.click(await screen.findByRole("button", { name: "Dispense" }));
    await waitFor(() => {
      expect(client.dispense).toHaveBeenCalledWith("med-1", 1);
    });
    // The scan form is filled with real barcode payloads for the
    // dispense just made (wristband for the patient, label for the
    // dispense), not typed ids.
    await waitFor(() => {
      expect((screen.getByLabelText("Wristband payload") as HTMLInputElement).value).toBe("OCWB1|pat-1|19620314|42");
    });
    expect((screen.getByLabelText("Medication label payload") as HTMLInputElement).value).toBe("OCML1|disp-1|SEED-1049221|07");
    expect(client.barcodeWristband).toHaveBeenCalledWith("pat-1");
    expect(client.barcodeMedLabel).toHaveBeenCalledWith("disp-1");
  });

  it("administers through the BCMA scan from the scan form", async () => {
    const client = stubClient();
    render(<PharmacyPage client={client} role="clinician" />);
    await openDispenseTab();
    await screen.findByText("med-1");
    fireEvent.change(screen.getByLabelText("Wristband payload"), { target: { value: "OCWB1|pat-1|19620314|42" } });
    fireEvent.change(screen.getByLabelText("Medication label payload"), { target: { value: "OCML1|disp-1|SEED-1049221|07" } });
    fireEvent.click(screen.getByRole("button", { name: "Administer" }));
    await waitFor(() => {
      expect(client.bcmaAdminister).toHaveBeenCalledWith({
        wristbandPayload: "OCWB1|pat-1|19620314|42",
        medLabelPayload: "OCML1|disp-1|SEED-1049221|07",
      });
    });
    expect(await screen.findByText(/Administered:/)).toBeTruthy();
  });

  it("blocks dispensing with the reason shown when verification is not current", async () => {
    const unverifiedQueue = fixtureDispenseQueue.map((item) => ({
      ...item,
      verification: {
        verified: false,
        verified_by: null,
        verified_at: null,
        state: "none",
        reason: "no verification on file",
      },
    }));
    const client = stubClient({
      dispenseQueue: vi.fn(async () => unverifiedQueue),
    });
    render(<PharmacyPage client={client} role="clinician" />);
    await openDispenseTab();
    await screen.findByText("med-1");
    const button = screen.getByRole("button", { name: "Dispense" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getAllByText(/no verification on file/).length).toBeGreaterThan(0);
  });

  it("viewer sees queue and stock but no dispense or administer actions", async () => {
    render(<PharmacyPage client={stubClient()} role="viewer" />);
    expect(await screen.findByText("med-1")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Dispense" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Administer" })).toBeNull();
  });

  it("surfaces a server refusal (oversell 409) as an inline error", async () => {
    const { ApiError } = await import("../api/client");
    const client = stubClient({
      dispense: vi.fn(async () => {
        throw new ApiError(409, "Insufficient stock for '1049221': 0 on hand, 1 requested");
      }),
    });
    render(<PharmacyPage client={client} role="clinician" />);
    await openDispenseTab();
    fireEvent.click(await screen.findByRole("button", { name: "Dispense" }));
    expect(await screen.findByText(/Insufficient stock/)).toBeTruthy();
  });
});
