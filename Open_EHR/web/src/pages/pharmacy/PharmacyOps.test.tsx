import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import {
  fixtureDispenseQueue,
  fixtureFormulary,
  fixtureStock,
} from "../../test/fixtures";
import { PharmacyPage } from "../PharmacyPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

const verificationItem = {
  order_id: "ord-apix",
  patient_ref: "Patient/pat-1",
  medication_code: "1351886",
  medication_display: "Apixaban 5 MG Oral Tablet",
  priority: "routine",
  status: "active",
  authored_on: "2026-10-18T08:00:00Z",
  wait_minutes: 42,
  sla_minutes: 480,
  sla_breached: false,
  formulary_status: "restricted",
  restriction_note: "Anticoagulation stewardship: confirm indication + renal function.",
  in_formulary: true,
  allergy_conflict: null,
  monitoring_blockers: [],
  monitoring_warnings: [],
  renal: { crcl_ml_min: 55, threshold_ml_min: 30, flagged: false },
};

const durResponse = {
  items: [
    {
      order_id: "ord-lis",
      patient_ref: "Patient/pat-1",
      medication_code: "1049221",
      medication_display: "Lisinopril 10 MG Oral Tablet",
      signals: [
        { type: "allergy", detail: "Recorded allergy to Lisinopril" },
        { type: "duplicate-therapy", detail: "1 other open order(s)" },
      ],
    },
  ],
  total: 1,
  interaction_checking: {
    status: "no-source",
    configured: false,
    source: null,
    message: "No drug-interaction knowledge source is configured.",
  },
};

const csBook = {
  items: [
    {
      medication_code: "1049625",
      display: "Oxycodone hydrochloride 5 MG Oral Tablet",
      schedule: "II",
      location: "main-pharmacy",
      expected: 40,
      unresolved_discrepancies: 1,
      balanced: false,
      last_count: null,
    },
  ],
  open_discrepancies: [
    {
      count_id: "csc-1",
      medication_code: "1049625",
      location: "main-pharmacy",
      expected: 40,
      counted: 38,
      variance: -2,
      counter: "dev-clinician",
      witness: "nurse-2",
      status: "discrepancy",
      resolution_reason: null,
      resolved_by: null,
      created_at: "2026-10-18T08:00:00Z",
    },
  ],
  recent_waste: [],
};

const compoundQueue = {
  items: [
    {
      batch_id: "cpd-1",
      preparation_name: "Synthetic IV admixture",
      category: "category-1",
      storage: "refrigerated",
      compounded_at: "2026-10-18T08:00:00Z",
      beyond_use_at: "2026-10-19T08:00:00Z",
      ingredients: [{ name: "Bulk powder", quantity: "10 mg", lot_number: "ING-1" }],
      patient_ref: null,
      preparer: "dev-clinician",
      verifier: null,
      verified_at: null,
      status: "in_preparation",
    },
  ],
  total: 1,
  categories: {
    "category-1": {
      label: "Category 1 (default BUDs)",
      budHours: { room: 12, refrigerated: 24, frozen: 1080 },
    },
  },
};

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    dispenseQueue: vi.fn(async () => fixtureDispenseQueue),
    stock: vi.fn(async () => fixtureStock),
    formulary: vi.fn(async () => fixtureFormulary),
    dispense: vi.fn(),
    administer: vi.fn(),
    pharmacyVerificationQueue: vi.fn(async () => [verificationItem]),
    verifyOrder: vi.fn(async () => ({
      verification: {
        order_id: "ord-apix",
        medication_code: "1351886",
        verified_by: "dev-clinician",
        formulary_status: "restricted",
        restriction_acknowledged: true,
        crcl_ml_min: 55,
      },
      rule_firings: [],
    })),
    durQueue: vi.fn(async () => durResponse),
    inventory: vi.fn(async () => ({
      items: [
        {
          medication_code: "1049221",
          location: "main-pharmacy",
          lot_number: "SEED-1049221",
          expiry_date: "2027-10-18",
          quantity: 120,
          days_to_expiry: 365,
          expired: false,
          expiring_soon: false,
          par_level: 100,
          reorder_point: 40,
        },
      ],
      locations: [
        { code: "main-pharmacy", label: "Main pharmacy" },
        { code: "adc-icu", label: "ADC — ICU" },
      ],
      total: 1,
    })),
    inventoryWorklist: vi.fn(async () => ({
      low_stock: [
        {
          medication_code: "1044910",
          location: "main-pharmacy",
          on_hand: 25,
          par_level: 80,
          reorder_point: 30,
          suggested_order: 55,
        },
      ],
      expiring: [],
      expired: [],
      expiry_warning_days: 30,
    })),
    purchaseOrders: vi.fn(async () => []),
    traceability: vi.fn(async () => []),
    adcRestockList: vi.fn(async () => [
      {
        medication_code: "1049221",
        display: "Lisinopril 10 MG Oral Tablet",
        location: "adc-icu",
        on_hand: 0,
        par_level: 20,
        suggested_quantity: 20,
        recent_dispenses: 3,
      },
    ]),
    csBook: vi.fn(async () => csBook),
    csResolve: vi.fn(async () => ({ ...csBook.open_discrepancies[0], status: "resolved" })),
    csCount: vi.fn(),
    csWaste: vi.fn(),
    compoundingQueue: vi.fn(async () => compoundQueue),
    createCompoundBatch: vi.fn(),
    verifyCompoundBatch: vi.fn(async () => ({
      ...compoundQueue.items[0],
      status: "verified",
      verifier: "dev-admin",
    })),
    ...overrides,
  } as unknown as ApiClient;
}

async function openTab(name: string) {
  fireEvent.click(await screen.findByRole("tab", { name }));
}

describe("PharmacyPage work centre (Phase 11)", () => {
  it("verification tab: restricted order needs acknowledgement before verify", async () => {
    const client = stubClient();
    render(<PharmacyPage client={client} role="clinician" />);
    await openTab("Verification & DUR");
    expect(await screen.findByText("ord-apix")).toBeTruthy();
    expect(screen.getByText(/Anticoagulation stewardship/)).toBeTruthy();
    const verify = screen.getByRole("button", { name: "Verify" });
    expect((verify as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(
      screen.getByLabelText("Acknowledge restriction for ord-apix"),
    );
    expect((verify as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(verify);
    await waitFor(() => {
      expect(client.verifyOrder).toHaveBeenCalledWith("ord-apix", {
        restrictionAcknowledged: true,
      });
    });
    // DUR section: honest interaction status + the allergy signal.
    expect(screen.getByText(/no-source/)).toBeTruthy();
    expect(
      screen.getByText(/No drug-interaction knowledge source is configured/),
    ).toBeTruthy();
    expect(screen.getByText(/Recorded allergy to Lisinopril/)).toBeTruthy();
  });

  it("inventory tab: worklist, lots and ADC restock render from the client", async () => {
    const client = stubClient();
    render(<PharmacyPage client={client} role="clinician" />);
    await openTab("Inventory & procurement");
    expect(await screen.findByText("1044910")).toBeTruthy();
    expect(screen.getByText("55")).toBeTruthy(); // suggested order to PAR
    expect(screen.getByText("SEED-1049221")).toBeTruthy();
    expect(client.adcRestockList).toHaveBeenCalledWith("adc-icu");
    expect(await screen.findByText("Lisinopril 10 MG Oral Tablet")).toBeTruthy();
  });

  it("controlled substances tab: unbalanced book and discrepancy resolution", async () => {
    const client = stubClient();
    render(<PharmacyPage client={client} role="clinician" />);
    await openTab("Controlled substances");
    expect(await screen.findByText("DOES NOT BALANCE")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Resolution reason for csc-1"), {
      target: { value: "Two tablets wasted, waste record filed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Resolve" }));
    await waitFor(() => {
      expect(client.csResolve).toHaveBeenCalledWith(
        "csc-1",
        "Two tablets wasted, waste record filed",
      );
    });
  });

  it("compounding tab: batch shows config BUD and can be verified", async () => {
    const client = stubClient();
    render(<PharmacyPage client={client} role="clinician" />);
    await openTab("Compounding");
    expect(await screen.findByText("cpd-1")).toBeTruthy();
    expect(screen.getByText(/2026-10-19 08:00/)).toBeTruthy();
    expect(screen.getByText(/BUD from config: 24 h/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    await waitFor(() => {
      expect(client.verifyCompoundBatch).toHaveBeenCalledWith("cpd-1");
    });
  });

  // UX pass (spec N-9 / F-10) supersedes the old fixed default: the
  // landing tab is now the entering role's primary queue.
  it("landing: a verification-capable role (clinician) lands on Verification & DUR", async () => {
    const client = stubClient();
    render(<PharmacyPage client={client} role="clinician" />);
    expect(await screen.findByText("ord-apix")).toBeTruthy();
    expect(client.pharmacyVerificationQueue).toHaveBeenCalled();
    expect(screen.queryByText("med-1")).toBeNull();
    expect(
      screen.getByRole("tab", { name: "Verification & DUR" }).getAttribute("aria-selected"),
    ).toBe("true");
  });

  it("landing: a role without pharmacy.verify (viewer) still lands on Dispense & stock", async () => {
    const client = stubClient();
    render(<PharmacyPage client={client} role="viewer" />);
    expect(await screen.findByText("med-1")).toBeTruthy();
    expect(client.pharmacyVerificationQueue).not.toHaveBeenCalled();
  });

  it("landing: an explicit deep-link section wins over the role default", async () => {
    const client = stubClient();
    render(<PharmacyPage client={client} role="clinician" section="inventory" />);
    expect(await screen.findByText("1044910")).toBeTruthy();
    expect(
      screen.getByRole("tab", { name: "Inventory & procurement" }).getAttribute("aria-selected"),
    ).toBe("true");
  });
});
