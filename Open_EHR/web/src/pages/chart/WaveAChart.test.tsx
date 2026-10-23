import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import type {
  AllergyIntolerance,
  MedicationSigVocabulary,
  OrderTypesResponse,
} from "../../api/types";
import { fixtureMar, fixturePatient } from "../../test/fixtures";
import { ChartPage } from "../ChartPage";
import { AllergiesTab } from "./AllergiesTab";
import { MarTab } from "./MarTab";
import { OrdersTab } from "./OrdersTab";

afterEach(cleanup);

// The medication_sig payload below is the REAL backend response shape
// (GET /orders/types -> medication_sig), captured from
// backend/app/orders.py default_medication_sig_payload() — not a
// hand-invented type. The backend test
// (test_wave_a_chart.test_sig_vocabulary_is_published_terminology)
// pins the same codes/timesPerDay server-side.
const SIG_VOCAB: MedicationSigVocabulary = {
  name: "OpenCare medication sig vocabulary (routes / frequencies / dose units)",
  system: "urn:opencare:medication-sig",
  routes: [
    { code: "oral", display: "Oral", active: true },
    { code: "intravenous", display: "Intravenous", active: true },
    { code: "intramuscular", display: "Intramuscular", active: true },
    { code: "subcutaneous", display: "Subcutaneous", active: true },
    { code: "inhaled", display: "Inhaled", active: true },
    { code: "topical", display: "Topical", active: true },
    { code: "rectal", display: "Rectal", active: true },
    { code: "sublingual", display: "Sublingual", active: true },
  ],
  frequencies: [
    { code: "once-daily", display: "Once daily", timesPerDay: 1, active: true, repeat: { frequency: 1, period: 1, periodUnit: "d" } },
    { code: "twice-daily", display: "Twice daily", timesPerDay: 2, active: true, repeat: { frequency: 2, period: 1, periodUnit: "d" } },
    { code: "three-times-daily", display: "Three times daily", timesPerDay: 3, active: true, repeat: { frequency: 3, period: 1, periodUnit: "d" } },
    { code: "four-times-daily", display: "Four times daily", timesPerDay: 4, active: true, repeat: { frequency: 4, period: 1, periodUnit: "d" } },
    { code: "every-6-hours", display: "Every 6 hours", timesPerDay: 4, active: true, repeat: { frequency: 1, period: 6, periodUnit: "h" } },
    { code: "every-8-hours", display: "Every 8 hours", timesPerDay: 3, active: true, repeat: { frequency: 1, period: 8, periodUnit: "h" } },
    { code: "every-12-hours", display: "Every 12 hours", timesPerDay: 2, active: true, repeat: { frequency: 1, period: 12, periodUnit: "h" } },
    { code: "as-needed", display: "As needed (PRN)", timesPerDay: 0, active: true },
  ],
  units: ["mg", "mcg", "g", "mL", "units", "tablets", "drops", "puffs"].map(
    (code) => ({ code, display: code, active: true }),
  ),
};

const ORDER_TYPES: OrderTypesResponse = {
  order_types: {
    name: "Synthetic order types",
    concepts: [
      { code: "medication", display: "Medication", resourceType: "MedicationRequest", requiredFields: ["code"] },
      { code: "procedure", display: "Procedure", resourceType: "ServiceRequest", requiredFields: ["code"] },
    ],
  },
  medication_sig: SIG_VOCAB,
  medication_sig_version: 1,
};

const PENICILLIN: AllergyIntolerance = {
  resourceType: "AllergyIntolerance",
  id: "alg-pat-1-pen",
  clinicalStatus: { coding: [{ code: "active" }] },
  verificationStatus: { coding: [{ code: "confirmed" }] },
  type: "allergy",
  category: ["medication"],
  criticality: "high",
  code: { coding: [{ code: "penicillin", display: "Penicillin" }], text: "Penicillin" },
  patient: { reference: "Patient/pat-1" },
  recordedDate: "2026-10-19T10:00:00Z",
  reaction: [{ manifestation: [{ text: "Anaphylaxis" }], severity: "severe" }],
};

function ordersStub(overrides: Record<string, unknown> = {}) {
  return {
    orderTypes: vi.fn(async () => ORDER_TYPES),
    orderSets: vi.fn(async () => ({ sets: [], total: 0 })),
    orders: vi.fn(async () => ({ items: [], total: 0 })),
    allergies: vi.fn(async () => [PENICILLIN]),
    placeOrder: vi.fn(async () => ({ order: { resourceType: "MedicationRequest", id: "o-1" }, order_type: "medication", warnings: [] })),
    placeOrderSet: vi.fn(async () => ({ orders: [], total: 0, warnings: [] })),
    transitionOrder: vi.fn(async () => ({})),
    ...overrides,
  } as unknown as ApiClient & Record<string, ReturnType<typeof vi.fn>>;
}

async function openMedicationForm(client: ApiClient) {
  render(<OrdersTab client={client} patientId="pat-1" canPlace />);
  await screen.findByText(/Allergies:/);
  fireEvent.change(screen.getByLabelText("Order type"), { target: { value: "medication" } });
  fireEvent.change(screen.getByLabelText("Code"), { target: { value: "amox-500" } });
  fireEvent.change(screen.getByLabelText("Display (optional)"), { target: { value: "Amoxicillin" } });
}

describe("CHART-B3 sig builder (OrdersTab)", () => {
  it("renders governed routes / frequencies / units from the API payload", async () => {
    const client = ordersStub();
    await openMedicationForm(client);
    expect(screen.getByRole("option", { name: "Intravenous" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "Sublingual" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "Three times daily" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "Every 6 hours" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "As needed (PRN)" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "mcg" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "puffs" })).toBeTruthy();
  });

  it("shows the patient's active allergies above the order form", async () => {
    const client = ordersStub();
    render(<OrdersTab client={client} patientId="pat-1" canPlace />);
    expect(await screen.findByText(/Penicillin/)).toBeTruthy();
  });

  it("validation errors render inline and nothing is placed", async () => {
    const client = ordersStub();
    await openMedicationForm(client);
    fireEvent.click(screen.getByRole("button", { name: "Place order" }));
    expect(await screen.findByText("Dose must be greater than 0")).toBeTruthy();
    expect(screen.getByText("Dose unit is required")).toBeTruthy();
    expect(screen.getByText("Route is required")).toBeTruthy();
    expect(screen.getByText("Frequency is required")).toBeTruthy();
    expect(client.placeOrder).not.toHaveBeenCalled();

    // PRN without a reason is refused inline too.
    fireEvent.change(screen.getByLabelText(/^Dose$/), { target: { value: "500" } });
    fireEvent.change(screen.getByLabelText("Dose unit"), { target: { value: "mg" } });
    fireEvent.change(screen.getByLabelText("Route"), { target: { value: "oral" } });
    fireEvent.change(screen.getByLabelText("Frequency"), { target: { value: "as-needed" } });
    fireEvent.click(screen.getByLabelText(/PRN \(as needed\)/));
    fireEvent.click(screen.getByRole("button", { name: "Place order" }));
    expect(await screen.findByText("PRN reason is required when PRN is selected")).toBeTruthy();
    expect(client.placeOrder).not.toHaveBeenCalled();
  });

  it("composes the exact FHIR dosageInstruction and live sig text", async () => {
    const client = ordersStub();
    await openMedicationForm(client);
    fireEvent.change(screen.getByLabelText(/^Dose$/), { target: { value: "500" } });
    fireEvent.change(screen.getByLabelText("Dose unit"), { target: { value: "mg" } });
    fireEvent.change(screen.getByLabelText("Route"), { target: { value: "oral" } });
    fireEvent.change(screen.getByLabelText("Frequency"), { target: { value: "three-times-daily" } });
    fireEvent.change(screen.getByLabelText("Duration (optional)"), { target: { value: "7" } });

    expect(
      await screen.findByText("Sig: Amoxicillin 500 mg oral three times daily for 7 days"),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Place order" }));
    await waitFor(() => expect(client.placeOrder).toHaveBeenCalledTimes(1));
    const arg = (client.placeOrder as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(arg.dosageInstruction).toEqual([
      {
        text: "Amoxicillin 500 mg oral three times daily for 7 days",
        doseAndRate: [{ doseQuantity: { value: 500, unit: "mg", code: "mg" } }],
        route: {
          coding: [{ system: "urn:opencare:medication-sig", code: "oral", display: "Oral" }],
          text: "Oral",
        },
        timing: {
          repeat: {
            frequency: 3,
            period: 1,
            periodUnit: "d",
            boundsDuration: { value: 7, unit: "d", code: "d" },
          },
        },
      },
    ]);
  });

  it("non-medication orders keep the plain form (no sig builder)", async () => {
    const client = ordersStub();
    render(<OrdersTab client={client} patientId="pat-1" canPlace />);
    await screen.findByText(/Allergies:/);
    expect(screen.queryByLabelText(/^Dose$/)).toBeNull();
  });
});

describe("CHART-B2 allergies tab", () => {
  function allergyStub() {
    return {
      createAllergy: vi.fn(async (body: Record<string, unknown>) => body),
      updateAllergy: vi.fn(async (body: Record<string, unknown>) => body),
    } as unknown as ApiClient & Record<string, ReturnType<typeof vi.fn>>;
  }

  it("lists substance, criticality, reaction and statuses", async () => {
    const client = allergyStub();
    render(
      <AllergiesTab client={client} patientId="pat-1" canWrite allergies={[PENICILLIN]} />,
    );
    expect(screen.getByText(/Penicillin/)).toBeTruthy();
    expect(screen.getByText(/criticality high/)).toBeTruthy();
    expect(screen.getByText(/Anaphylaxis/)).toBeTruthy();
    expect(screen.getByText(/clinical: active/)).toBeTruthy();
    expect(screen.getByText(/verification: confirmed/)).toBeTruthy();
  });

  it("read-only roles see the data without add/edit controls", async () => {
    const client = allergyStub();
    render(
      <AllergiesTab client={client} patientId="pat-1" canWrite={false} allergies={[PENICILLIN]} />,
    );
    expect(screen.getByText(/Penicillin/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Add allergy" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Save allergy/ })).toBeNull();
  });

  it("empty list renders the not-recorded posture and Record NKDA creates the NKDA resource", async () => {
    const client = allergyStub();
    render(<AllergiesTab client={client} patientId="pat-1" canWrite allergies={[]} />);
    expect(screen.getByText(/Allergies not recorded/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Record NKDA" }));
    await waitFor(() => expect(client.createAllergy).toHaveBeenCalledTimes(1));
    const body = (client.createAllergy as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(body.code.coding[0]).toEqual({
      system: "urn:opencare:allergy",
      code: "no-known-allergy",
      display: "No known allergies",
    });
    expect(body.patient).toEqual({ reference: "Patient/pat-1" });
    expect(await screen.findByText(/No known allergies \(NKDA\)/)).toBeTruthy();
  });

  it("add form validates the substance code/display inline", async () => {
    const client = allergyStub();
    render(<AllergiesTab client={client} patientId="pat-1" canWrite allergies={[]} />);
    fireEvent.click(screen.getByRole("button", { name: "Add allergy" }));
    expect(await screen.findByText("Substance code is required")).toBeTruthy();
    expect(screen.getByText("Substance display is required")).toBeTruthy();
    expect(client.createAllergy).not.toHaveBeenCalled();
  });

  it("edit saves clinical/verification status transitions under the same id", async () => {
    const client = allergyStub();
    render(
      <AllergiesTab client={client} patientId="pat-1" canWrite allergies={[PENICILLIN]} />,
    );
    fireEvent.change(screen.getByLabelText(/Clinical status for Penicillin/), {
      target: { value: "resolved" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Save allergy/ }));
    await waitFor(() => expect(client.updateAllergy).toHaveBeenCalledTimes(1));
    const body = (client.updateAllergy as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(body.id).toBe("alg-pat-1-pen");
    expect(body.clinicalStatus.coding[0].code).toBe("resolved");
  });
});

describe("CHART-B2 chart banner + CHART-B3 MAR sig", () => {
  it("ChartPage banner names the active allergy substances", async () => {
    const client = {
      get: vi.fn(async () => fixturePatient),
      search: vi.fn(async (type: string) =>
        type === "AllergyIntolerance" ? [PENICILLIN] : [],
      ),
    } as unknown as ApiClient;
    render(<ChartPage client={client} patientId="pat-1" role="clinician" />);
    await screen.findByRole("heading", { name: /Synthetic Sample Testworth/ });
    expect(await screen.findByText("Allergies: Penicillin")).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Allergies" })).toBeTruthy();
  });

  it("MarTab shows the order's sig text on the MAR row", async () => {
    const client = {
      marDue: vi.fn(async () => fixtureMar),
      bcmaAdminister: vi.fn(async () => ({})),
      patientDispenses: vi.fn(async () => []),
      orders: vi.fn(async () => ({
        items: [
          {
            resourceType: "MedicationRequest",
            id: "ord-lisinopril",
            status: "signed",
            intent: "order",
            medicationCodeableConcept: { coding: [{ code: "1049221", display: "Lisinopril" }] },
            subject: { reference: "Patient/pat-1" },
            dosageInstruction: [
              { text: "Lisinopril 10 mg oral once daily" },
            ],
          },
        ],
        total: 1,
      })),
    } as unknown as ApiClient;
    render(<MarTab client={client} patientId="pat-1" />);
    // Both fixture doses belong to the same order, so the sig shows
    // on each MAR row for that order.
    expect(
      (await screen.findAllByText("Lisinopril 10 mg oral once daily")).length,
    ).toBe(2);
  });
});
