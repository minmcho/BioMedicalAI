import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type {
  LabCatalogResponse,
  ServiceRequest,
  VerificationItem,
} from "../api/types";
import {
  fixtureLabOrder,
  fixtureQcRun,
  fixtureSpecimen,
  fixtureVerificationItem,
} from "../test/fixtures";
import { LabPage } from "./LabPage";

// Phase 6B LabPage sections: collection worklist, microbiology bench,
// blood bank, pathology, operations — plus delta/amended rendering in
// the verification queue and the recollection action. Fixture
// (synthetic) data only; the stub client stands in for the network
// boundary in unit tests — shipped code uses the real ApiClient.
afterEach(cleanup);

const catalog6b: LabCatalogResponse = {
  catalog: {
    name: "Synthetic lab catalog",
    concepts: [
      {
        code: "2823-3", display: "Potassium (synthetic)", active: true,
        unit: "mmol/L",
        referenceRange: { low: 3.5, high: 5.1, unit: "mmol/L" },
      },
      {
        code: "51990-0", display: "Basic metabolic panel (synthetic)",
        active: true, panel: true, components: ["2823-3"],
      },
      {
        code: "87040-1", display: "Blood culture (synthetic)",
        active: true, bench: "microbiology",
      },
      {
        code: "11529-5", display: "Surgical pathology (synthetic)",
        active: true, bench: "pathology",
      },
      {
        code: "883-9", display: "ABO group (synthetic)", active: true,
        bench: "blood-bank", qualitative: true,
        values: ["A POS", "O POS", "O NEG"],
      },
    ],
    rejectionCriteria: [
      { code: "hemolysed", display: "Haemolysed specimen" },
      { code: "clotted", display: "Clotted specimen" },
    ],
    bloodProducts: [{ code: "rbc", display: "Red blood cells" }],
  },
  catalog_version: 3,
  workflow: {
    states: ["ordered", "collected", "received", "in-process", "rejected"],
    transitions: [
      { from: "ordered", to: "collected" },
      { from: "collected", to: "received" },
      { from: "collected", to: "rejected" },
    ],
    governedStates: ["resulted", "verified", "released"],
  },
  workflow_version: 1,
};

const microOrder: ServiceRequest = {
  resourceType: "ServiceRequest",
  id: "ord-micro-1",
  status: "plated",
  intent: "order",
  code: { text: "Blood culture (synthetic)", coding: [{ code: "87040-1" }] },
  subject: { reference: "Patient/pat-1" },
};

const pathologyOrder: ServiceRequest = {
  resourceType: "ServiceRequest",
  id: "ord-path-1",
  status: "histology",
  intent: "order",
  code: { text: "Surgical pathology (synthetic)", coding: [{ code: "11529-5" }] },
  subject: { reference: "Patient/pat-1" },
};

const rejectedOrder: ServiceRequest = {
  resourceType: "ServiceRequest",
  id: "ord-rej-1",
  status: "rejected",
  intent: "order",
  code: { text: "Basic metabolic panel (synthetic)", coding: [{ code: "51990-0" }] },
  subject: { reference: "Patient/pat-1" },
  statusReason: { text: "Haemolysed specimen: Visible haemolysis" },
};

const deltaItem: VerificationItem = {
  ...fixtureVerificationItem,
  report: { ...fixtureVerificationItem.report, id: "rep-delta-1" },
  order_id: "ord-delta-1",
  critical: false,
  qc_blocked: [],
  delta_flags: [
    {
      code: "2823-3", previous_value: 4.0, current_value: 5.0,
      change: 1.0, change_pct: 25.0,
    },
  ],
};

const amendedItem: VerificationItem = {
  ...fixtureVerificationItem,
  report: {
    ...fixtureVerificationItem.report,
    id: "rep-amended-1",
    status: "amended",
  },
  order_id: "ord-amended-1",
  stage: "released",
  critical: false,
  qc_blocked: [],
  amended: true,
  amendment: {
    amends: "DiagnosticReport/rep-orig-1",
    amended_by: null,
    reason: "Transcription error corrected",
  },
};

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    labCatalog: vi.fn(async () => catalog6b),
    labOrders: vi.fn(async () => [
      fixtureLabOrder, microOrder, pathologyOrder, rejectedOrder,
    ]),
    verificationQueue: vi.fn(async () => ({
      items: [deltaItem, amendedItem],
      total: 2,
    })),
    labSpecimens: vi.fn(async () => [fixtureSpecimen]),
    qcRuns: vi.fn(async () => [fixtureQcRun]),
    criticalReports: vi.fn(async () => ({ items: [], total: 0 })),
    verifyLabReport: vi.fn(async () => ({})),
    releaseLabReport: vi.fn(async () => ({})),
    transitionLabOrder: vi.fn(async () => ({})),
    createLabOrder: vi.fn(async () => fixtureLabOrder),
    enterLabResults: vi.fn(async () => ({})),
    ingestLab: vi.fn(async () => ({ ingested: [], report_count: 0, result_count: 0 })),
    createQcRun: vi.fn(async () => fixtureQcRun),
    collectionWorklist: vi.fn(async () => ({
      total_orders: 1,
      groups: [
        {
          patient_ref: "Patient/pat-1",
          location: "Encounter/enc-a",
          highest_priority: "stat",
          orders: [
            {
              order_id: "ord-lab-1", code: "51990-0",
              display: "Basic metabolic panel (synthetic)",
              priority: "stat", authored_on: "2026-01-15T09:00:00Z",
              custody: {
                ordered_at: "2026-01-15T09:00:00Z", collected_at: null,
                received_at: null, in_analysis_at: null,
              },
            },
          ],
        },
      ],
    })),
    recollectLabOrder: vi.fn(async () => ({})),
    amendLabReport: vi.fn(async () => ({})),
    microTerminology: vi.fn(async () => ({
      organisms: {
        concepts: [
          { code: "escherichia-coli", display: "Escherichia coli" },
          { code: "no-growth", display: "No growth" },
        ],
      },
      organisms_version: 1,
      antibiotics: {
        concepts: [{ code: "ciprofloxacin", display: "Ciprofloxacin" }],
      },
      antibiotics_version: 1,
    })),
    microPreliminary: vi.fn(async () => ({})),
    microOrganism: vi.fn(async () => ({})),
    microSusceptibility: vi.fn(async () => ({})),
    microFinalize: vi.fn(async () => ({})),
    bloodUnits: vi.fn(async () => [
      {
        unit_id: "SYN-U1", product_type: "rbc", blood_group: "O POS",
        expiry_date: "2099-01-01", status: "available",
        patient_ref: null, order_id: null, issued_at: null,
        created_at: "2026-01-15T09:00:00Z",
      },
    ]),
    bloodHolds: vi.fn(async () => [
      {
        id: 1, patient_ref: "Patient/pat-9", unit_id: "SYN-U0",
        reason: "Fever and rigors", active: true,
      },
    ]),
    createBloodUnit: vi.fn(async () => ({})),
    issueBloodUnit: vi.fn(async () => ({})),
    returnBloodUnit: vi.fn(async () => ({})),
    recordTransfusion: vi.fn(async () => ({})),
    reportTransfusionReaction: vi.fn(async () => ({})),
    clearBloodHold: vi.fn(async () => ({})),
    pathologySynopticForm: vi.fn(async () => ({
      form: {
        title: "Synoptic",
        item: [
          { linkId: "specimenType", text: "Specimen type", type: "string", required: true },
        ],
      },
      version: 1,
    })),
    pathologyReport: vi.fn(async () => ({})),
    pathologySign: vi.fn(async () => ({})),
    eqaRecords: vi.fn(async () => [
      {
        id: 1, event: "SYN-EQA-2026-Q3", analyte_code: "2823-3",
        reported_value: 4.1, acceptable_low: 3.9, acceptable_high: 4.3,
        passed: true, recorded_by: "dev-clinician",
        created_at: "2026-01-15T09:00:00Z",
      },
    ]),
    createEqa: vi.fn(async () => ({})),
    instruments: vi.fn(async () => [
      {
        instrument_id: "SYN-AN-01", name: "Synthetic chemistry analyser",
        bench: "chemistry", status: "active",
        created_at: "2026-01-15T09:00:00Z", updated_at: null,
      },
    ]),
    createInstrument: vi.fn(async () => ({})),
    setInstrumentStatus: vi.fn(async () => ({})),
    instrumentLog: vi.fn(async () => []),
    addInstrumentLog: vi.fn(async () => ({})),
    leveyJennings: vi.fn(async () => ({
      analyte_code: "2823-3", control_material: "SYN-QC-LOT-1", count: 0,
      mean: null, sd: null, limits: null, runs: [],
    })),
    tatStats: vi.fn(async () => ({
      generated_at: "2026-01-15T10:00:00Z",
      source: "live-query",
      benches: {
        chemistry: {
          count: 3,
          order_to_release_hours: { p50: 4.0, p90: 10.0 },
          received_to_release_hours: { p50: 3.0, p90: 9.0 },
        },
      },
      analytes: {},
    })),
    ...overrides,
  } as unknown as ApiClient;
}

// Depth pass: the lab page renders one tab panel at a time (the
// shared Tabs component), so bench assertions open their tab first.
function clickTab(name: string) {
  fireEvent.click(screen.getByRole("tab", { name }));
}

describe("LabPage Phase 6B", () => {
  it("renders the collection worklist, benches and operations sections", async () => {
    render(<LabPage client={stubClient()} role="clinician" />);

    // collection worklist (Specimens tab)
    clickTab("Specimens");
    expect(await screen.findByText("Collection worklist")).toBeTruthy();
    // worklist row: stat priority from the grouped payload
    expect(screen.getByText("stat")).toBeTruthy();
    // microbiology bench: the plated culture offers preliminary reporting
    clickTab("Microbiology");
    expect(
      await screen.findByRole("button", { name: "Preliminary report" }),
    ).toBeTruthy();
    expect(screen.getByText("ord-micro-1", { exact: false })).toBeTruthy();
    // blood bank: unit inventory + the active-hold banner
    clickTab("Blood Bank");
    expect(await screen.findByText("Blood bank")).toBeTruthy();
    expect(screen.getByText("SYN-U1")).toBeTruthy();
    expect(screen.getByText(/Transfusion hold/)).toBeTruthy();
    // pathology bench: the histology case offers the synoptic report
    clickTab("Pathology");
    expect(await screen.findByText("Anatomic pathology")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Write synoptic report" }),
    ).toBeTruthy();
    // operations (QC & EQA tab): TAT bench row, EQA record, instrument registry
    clickTab("QC & EQA");
    expect(await screen.findByText("Laboratory operations")).toBeTruthy();
    expect(screen.getByText("SYN-EQA-2026-Q3")).toBeTruthy();
    expect(screen.getByText("SYN-AN-01")).toBeTruthy();
    // "chemistry" is both the TAT bench row and the instrument's bench
    expect(screen.getAllByText("chemistry").length).toBeGreaterThanOrEqual(2);
  });

  it("shows delta flags and the amended state in the verification queue", async () => {
    render(<LabPage client={stubClient()} role="clinician" />);
    clickTab("Verification");
    expect(await screen.findByText(/Δ 2823-3: 4 → 5 \(\+25\.0%\)/)).toBeTruthy();
    expect(screen.getByText(/AMENDED — Transcription error corrected/)).toBeTruthy();
  });

  it("bench orders stay out of the standard order queue", async () => {
    render(<LabPage client={stubClient()} role="clinician" />);
    // the micro + pathology orders appear in their bench sections; the
    // order-queue table lists only standard-spine orders
    expect((await screen.findAllByText("ord-lab-1")).length).toBeGreaterThanOrEqual(1);
    screen.getByText("Order queue");
    // the order-queue table is the one whose first header cell is "Order"
    // (the collection-worklist table above it starts with "Patient")
    const tables = Array.from(document.querySelectorAll("table"));
    const table = tables.find(
      (t) => t.querySelector("th")?.textContent === "Order",
    );
    expect(table?.textContent).not.toContain("ord-micro-1");
    expect(table?.textContent).not.toContain("ord-path-1");
    expect(table?.textContent).toContain("ord-rej-1");
  });

  it("requests recollection for a rejected order through the client", async () => {
    const client = stubClient();
    render(<LabPage client={client} role="clinician" />);
    const button = await screen.findByRole("button", {
      name: "Request recollection",
    });
    fireEvent.click(button);
    await waitFor(() => {
      expect(client.recollectLabOrder).toHaveBeenCalledWith("ord-rej-1");
    });
  });

  it("records a preliminary culture result through the client", async () => {
    const client = stubClient();
    render(<LabPage client={client} role="clinician" />);
    clickTab("Microbiology");
    const input = await screen.findByLabelText(
      "Preliminary finding for order ord-micro-1",
    );
    fireEvent.change(input, { target: { value: "Gram-negative rods seen" } });
    fireEvent.click(screen.getByRole("button", { name: "Preliminary report" }));
    await waitFor(() => {
      expect(client.microPreliminary).toHaveBeenCalledWith(
        "ord-micro-1",
        "Gram-negative rods seen",
      );
    });
  });

  it("amends a released report only with a reason", async () => {
    const client = stubClient();
    render(<LabPage client={client} role="clinician" />);
    clickTab("Verification");
    const input = await screen.findByLabelText(
      "Amendment reason for report rep-amended-1",
    );
    const button = screen.getByRole("button", { name: "Amend report" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.change(input, { target: { value: "Wrong tube labelled" } });
    expect(button.disabled).toBe(false);
    fireEvent.click(button);
    await waitFor(() => {
      expect(client.amendLabReport).toHaveBeenCalledWith("rep-amended-1", {
        reason: "Wrong tube labelled",
      });
    });
  });
});
