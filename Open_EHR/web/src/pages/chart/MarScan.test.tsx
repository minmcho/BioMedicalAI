import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../../api/client";
import { fixtureMar } from "../../test/fixtures";
import { MarTab } from "./MarTab";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

const bcmaOutcome = {
  administration: {
    resourceType: "MedicationAdministration",
    id: "adm-bcma-1",
    status: "completed",
  },
  bcma: {
    checks: [
      { right: "right-patient", result: "pass", detail: "Wristband matches" },
      { right: "right-medication", result: "pass", detail: "Dispensed = ordered" },
      { right: "right-dose", result: "pass", detail: "1 tablet" },
      { right: "right-time", result: "pass", detail: "Inside the MAR window" },
    ],
    override: null,
  },
};

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    marDue: vi.fn(async () => fixtureMar),
    bcmaAdminister: vi.fn(async () => bcmaOutcome),
    patientDispenses: vi.fn(async () => [
      {
        dispense_id: "disp-1",
        status: "completed",
        medication_code: "1049221",
        medication_display: "Lisinopril 10 MG Oral Tablet",
        lot_number: "SEED-1049221",
        quantity: 1,
        when_handed_over: "2026-10-16T07:30:00Z",
        request_ref: "MedicationRequest/ord-lisinopril",
      },
    ]),
    barcodeWristband: vi.fn(async () => ({
      payload: "OCWB1|pat-1|19620314|10",
      patient_id: "pat-1",
      dob: "1962-03-14",
    })),
    barcodeMedLabel: vi.fn(async () => ({
      payload: "OCML1|disp-1|SEED-1049221|55",
      dispense_id: "disp-1",
      lot: "SEED-1049221",
    })),
    ...overrides,
  } as unknown as ApiClient;
}

describe("MarTab BCMA scan (Phase 11)", () => {
  it("administers from scanned payloads and shows the five-rights results", async () => {
    const client = stubClient();
    render(<MarTab client={client} patientId="pat-1" />);
    await screen.findByText(/Given 1 · Due 1/);
    fireEvent.click(screen.getByRole("button", { name: "Scan to administer" }));
    fireEvent.change(screen.getByLabelText("Wristband payload"), {
      target: { value: "OCWB1|pat-1|19620314|10" },
    });
    fireEvent.change(screen.getByLabelText("Medication label payload"), {
      target: { value: "OCML1|disp-1|SEED-1049221|55" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Administer scan" }));
    await waitFor(() => {
      expect(client.bcmaAdminister).toHaveBeenCalledWith({
        wristbandPayload: "OCWB1|pat-1|19620314|10",
        medLabelPayload: "OCML1|disp-1|SEED-1049221|55",
      });
    });
    expect(await screen.findByText(/right-patient/)).toBeTruthy();
    expect(screen.getByText(/adm-bcma-1/)).toBeTruthy();
  });

  it("surfaces a hard-stop refusal as an alert and records nothing", async () => {
    const client = stubClient({
      bcmaAdminister: vi.fn(async () => {
        throw new ApiError(
          422,
          "BCMA hard stop — right-patient failed: wristband patient does not match the dispense",
        );
      }),
    });
    render(<MarTab client={client} patientId="pat-1" />);
    await screen.findByText(/Given 1 · Due 1/);
    fireEvent.click(screen.getByRole("button", { name: "Scan to administer" }));
    fireEvent.change(screen.getByLabelText("Wristband payload"), {
      target: { value: "OCWB1|pat-2|19750707|99" },
    });
    fireEvent.change(screen.getByLabelText("Medication label payload"), {
      target: { value: "OCML1|disp-1|SEED-1049221|55" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Administer scan" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/right-patient failed/);
  });

  it("simulator loads dispenses lazily and fills the scan fields", async () => {
    const client = stubClient();
    render(<MarTab client={client} patientId="pat-1" />);
    await screen.findByText(/Given 1 · Due 1/);
    // Nothing simulator-related is fetched until the panel is opened.
    expect(client.patientDispenses).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Scan to administer" }));
    expect(client.patientDispenses).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Scan simulator" }));
    await waitFor(() => {
      expect(client.patientDispenses).toHaveBeenCalledWith("pat-1");
    });
    expect(
      await screen.findByText(/Scan simulator \(development\/testing only\)/),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Fill scan fields" }));
    await waitFor(() => {
      expect(
        (screen.getByLabelText("Wristband payload") as HTMLInputElement).value,
      ).toBe("OCWB1|pat-1|19620314|10");
      expect(
        (screen.getByLabelText("Medication label payload") as HTMLInputElement)
          .value,
      ).toBe("OCML1|disp-1|SEED-1049221|55");
    });
    expect(client.barcodeWristband).toHaveBeenCalledWith("pat-1");
    expect(client.barcodeMedLabel).toHaveBeenCalledWith("disp-1");
  });
});
