import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { fixtureDispenseQueue, fixtureFormulary, fixtureStock } from "../test/fixtures";
import { PharmacyPage } from "./PharmacyPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    dispenseQueue: vi.fn(async () => fixtureDispenseQueue),
    stock: vi.fn(async () => fixtureStock),
    formulary: vi.fn(async () => fixtureFormulary),
    dispense: vi.fn(async () => ({
      dispense: { resourceType: "MedicationDispense", id: "disp-1" },
      medication_request: fixtureDispenseQueue[0].request,
      stock_after: 119,
    })),
    administer: vi.fn(async () => ({
      resourceType: "MedicationAdministration",
      id: "adm-1",
      status: "completed",
      medicationCodeableConcept: { text: "Lisinopril 10 MG Oral Tablet" },
      subject: { reference: "Patient/pat-1" },
      effectiveDateTime: "2026-01-15T11:00:00Z",
    })),
    ...overrides,
  } as unknown as ApiClient;
}

describe("PharmacyPage", () => {
  it("renders the dispense queue with live stock and the stock table", async () => {
    render(<PharmacyPage client={stubClient()} role="clinician" />);
    expect(await screen.findByText("med-1")).toBeTruthy();
    expect(screen.getByText("120 on hand")).toBeTruthy();
    expect(screen.getByText("Lisinopril 10 MG Oral Tablet (synthetic)")).toBeTruthy();
    expect(screen.getByText(/terminology\/formulary v1/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Dispense" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Administer" })).toBeTruthy();
  });

  it("dispenses through the client and pre-fills the BCMA scan form from the outcome", async () => {
    const client = stubClient();
    render(<PharmacyPage client={client} role="clinician" />);
    fireEvent.click(await screen.findByRole("button", { name: "Dispense" }));
    await waitFor(() => {
      expect(client.dispense).toHaveBeenCalledWith("med-1", 1);
    });
    await waitFor(() => {
      expect((screen.getByLabelText("Dispense id") as HTMLInputElement).value).toBe("disp-1");
    });
    expect((screen.getByLabelText("Wristband id") as HTMLInputElement).value).toBe("pat-1");
    expect((screen.getByLabelText("Medication code") as HTMLInputElement).value).toBe("1049221");
  });

  it("administers through the client from the scan form", async () => {
    const client = stubClient();
    render(<PharmacyPage client={client} role="clinician" />);
    await screen.findByText("med-1");
    fireEvent.change(screen.getByLabelText("Wristband id"), { target: { value: "pat-1" } });
    fireEvent.change(screen.getByLabelText("Medication code"), { target: { value: "1049221" } });
    fireEvent.change(screen.getByLabelText("Dispense id"), { target: { value: "disp-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Administer" }));
    await waitFor(() => {
      expect(client.administer).toHaveBeenCalledWith({
        wristbandId: "pat-1",
        medicationCode: "1049221",
        dispenseId: "disp-1",
      });
    });
    expect(await screen.findByText(/Administered:/)).toBeTruthy();
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
    fireEvent.click(await screen.findByRole("button", { name: "Dispense" }));
    expect(await screen.findByText(/Insufficient stock/)).toBeTruthy();
  });
});
