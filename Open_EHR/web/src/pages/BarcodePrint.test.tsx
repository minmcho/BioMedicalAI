import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import { saveDownloadedFile } from "../api/download";
import {
  fixtureCondition,
  fixtureDispenseQueue,
  fixtureEncounter,
  fixtureFormulary,
  fixtureMedication,
  fixtureObservation,
  fixturePatient,
  fixtureProcedure,
  fixtureReport,
  fixtureStock,
} from "../test/fixtures";
import { ChartPage } from "./ChartPage";
import { PharmacyPage } from "./PharmacyPage";

// The print path under test is: page button -> api/barcodes.ts ->
// client.requestBlob (the server label PDF) -> saveDownloadedFile
// (the browser download/print path report PDFs already use). Mock
// only the last hop so the blob hand-off is asserted, not performed.
vi.mock("../api/download", () => ({ saveDownloadedFile: vi.fn() }));

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);
beforeEach(() => {
  vi.mocked(saveDownloadedFile).mockClear();
});

const LABEL_FILE = {
  blob: new Blob(["%PDF-label"], { type: "application/pdf" }),
  filename: "label-wristband.pdf",
  rows: null,
};

function chartClient(overrides: Record<string, unknown> = {}): ApiClient {
  const byType: Record<string, unknown[]> = {
    Condition: [fixtureCondition],
    MedicationRequest: [fixtureMedication],
    Observation: [fixtureObservation],
    DiagnosticReport: [fixtureReport],
    Procedure: [fixtureProcedure],
    Encounter: [fixtureEncounter],
  };
  return {
    get: vi.fn(async () => fixturePatient),
    search: vi.fn(async (type: string) => byType[type] ?? []),
    requestBlob: vi.fn(async () => LABEL_FILE),
    ...overrides,
  } as unknown as ApiClient;
}

function pharmacyClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    dispenseQueue: vi.fn(async () => fixtureDispenseQueue),
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
    administer: vi.fn(async () => ({
      resourceType: "MedicationAdministration",
      id: "adm-1",
      status: "completed",
      medicationCodeableConcept: { text: "Lisinopril 10 MG Oral Tablet" },
      subject: { reference: "Patient/pat-1" },
      effectiveDateTime: "2026-01-15T11:00:00Z",
    })),
    requestBlob: vi.fn(async () => ({
      ...LABEL_FILE,
      filename: "label-medication.pdf",
    })),
    ...overrides,
  } as unknown as ApiClient;
}

describe("Barcode label printing (Phase 15, scope H)", () => {
  it("chart: Print wristband fetches the wristband label for Patient/<id> and hands the blob to the download path", async () => {
    const client = chartClient();
    render(<ChartPage client={client} patientId="pat-1" />);

    fireEvent.click(
      await screen.findByRole("button", { name: /Print wristband/ }),
    );

    await waitFor(() => {
      expect(client.requestBlob).toHaveBeenCalledWith(
        "/barcodes/labels/wristband/Patient/pat-1",
      );
    });
    await waitFor(() => {
      expect(saveDownloadedFile).toHaveBeenCalledWith(LABEL_FILE);
    });
    expect(await screen.findByText(/Wristband label downloaded/)).toBeTruthy();
  });

  it("chart: a 404 label fetch surfaces the server detail inline, never a silent no-op", async () => {
    const client = chartClient({
      requestBlob: vi.fn(async () => {
        throw new ApiError(404, "Patient/pat-1 not found");
      }),
    });
    render(<ChartPage client={client} patientId="pat-1" />);

    fireEvent.click(
      await screen.findByRole("button", { name: /Print wristband/ }),
    );

    expect(
      await screen.findByRole("alert"),
    ).toBeTruthy();
    expect(
      await screen.findByText(/Print wristband failed: Patient\/pat-1 not found/),
    ).toBeTruthy();
    expect(saveDownloadedFile).not.toHaveBeenCalled();
  });

  it("pharmacy: Print med label appears for the dispensed medication and fetches the medication label for MedicationDispense/<id>", async () => {
    const client = pharmacyClient();
    render(<PharmacyPage client={client} role="clinician" />);
    // UX pass (N-9): clinicians land on Verification; open Dispense.
    fireEvent.click(await screen.findByRole("tab", { name: "Dispense & stock" }));

    // No dispense selected yet — no label action.
    expect(await screen.findByText("med-1")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Print med label/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Dispense" }));

    fireEvent.click(
      await screen.findByRole("button", { name: /Print med label/ }),
    );

    await waitFor(() => {
      expect(client.requestBlob).toHaveBeenCalledWith(
        "/barcodes/labels/medication/MedicationDispense/disp-1",
      );
    });
    await waitFor(() => {
      expect(saveDownloadedFile).toHaveBeenCalledWith(
        expect.objectContaining({ filename: "label-medication.pdf" }),
      );
    });
    expect(await screen.findByText(/Medication label downloaded/)).toBeTruthy();
  });

  it("pharmacy: a 404 med-label fetch surfaces the server detail inline", async () => {
    const client = pharmacyClient({
      requestBlob: vi.fn(async () => {
        throw new ApiError(
          404,
          "No dispense or lot 'disp-1' for a medication label",
        );
      }),
    });
    render(<PharmacyPage client={client} role="clinician" />);
    // UX pass (N-9): clinicians land on Verification; open Dispense.
    fireEvent.click(await screen.findByRole("tab", { name: "Dispense & stock" }));

    fireEvent.click(await screen.findByRole("button", { name: "Dispense" }));
    fireEvent.click(
      await screen.findByRole("button", { name: /Print med label/ }),
    );

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(
      await screen.findByText(/Print med label failed:.*No dispense or lot 'disp-1'/),
    ).toBeTruthy();
    expect(saveDownloadedFile).not.toHaveBeenCalled();
  });
});
