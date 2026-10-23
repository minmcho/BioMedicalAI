import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import { InventoryPage } from "./InventoryPage";

afterEach(cleanup);

const TAXONOMY = {
  categories: [
    {
      code: "surgical-supplies",
      label: "Surgical supplies",
      subcategories: [{ code: "sutures-staples", label: "Sutures & staples" }],
    },
    {
      code: "lab-reagents",
      label: "Lab reagents",
      classPolicy: {
        containerTracked: true,
        openStabilityDays: 30,
        initialStage: "quarantine",
        verificationRequired: true,
      },
      subcategories: [{ code: "chemistry", label: "Chemistry" }],
    },
  ],
  reasonCodes: [
    { code: "damaged", label: "Damaged" },
    { code: "other", label: "Other (note required)" },
  ],
  quarantineReasonCodes: [{ code: "suspect-quality", label: "Suspect quality" }],
  recallReasonCodes: [{ code: "mfr-recall", label: "Manufacturer recall" }],
  disposalReasonCodes: [
    { code: "expired-disposal", label: "Expired — dispose" },
    { code: "damaged-disposal", label: "Damaged — dispose" },
  ],
  expiryWarningDays: 90,
};

const OVERVIEW = {
  active_items: 3,
  stock_locations: 2,
  on_hand_units: 250,
  low_stock_lines: 0,
  expiring_lines: 0,
  expired_lines: 0,
  movements_today: 5,
  stock_value_cents: 1000,
  uncosted_lots: 0,
};

const ITEMS = [
  {
    sku: "SUT-001",
    name: "Suture pack (synthetic)",
    category: "surgical-supplies",
    subcategory: "sutures-staples",
    uom: "each",
    structure: "simple",
    base_uom: "each",
    uom_ladder: [],
    kit_components: [],
    kit_mode: null,
    hazard_class: null,
    sds_ref: null,
    lot_tracked: true,
    serial_tracked: false,
    expiry_tracked: true,
    unit_cost_cents: 1250,
    formulary_code: null,
    asset_key: null,
    active: true,
  },
  {
    sku: "KIT-001",
    name: "Procedure kit (synthetic)",
    category: "surgical-supplies",
    subcategory: "sutures-staples",
    uom: "kit",
    structure: "kit",
    base_uom: "kit",
    uom_ladder: [
      { code: "case", containsNext: 4 },
      { code: "kit", containsNext: 1 },
    ],
    kit_components: [{ sku: "SUT-001", qty: 2 }],
    kit_mode: "track-whole",
    hazard_class: null,
    sds_ref: null,
    lot_tracked: true,
    serial_tracked: false,
    expiry_tracked: true,
    unit_cost_cents: 5000,
    formulary_code: null,
    asset_key: null,
    active: true,
  },
  {
    sku: "REAG-001",
    name: "Chemistry reagent (synthetic)",
    category: "lab-reagents",
    subcategory: "chemistry",
    uom: "vial",
    structure: "simple",
    base_uom: "vial",
    uom_ladder: [
      { code: "case", containsNext: 10 },
      { code: "box", containsNext: 10 },
      { code: "vial", containsNext: 1 },
    ],
    kit_components: [],
    kit_mode: null,
    hazard_class: "irritant",
    sds_ref: "SDS-REAG-001",
    lot_tracked: true,
    serial_tracked: false,
    expiry_tracked: true,
    unit_cost_cents: 900,
    formulary_code: null,
    asset_key: null,
    active: true,
  },
];

const STOCK = [
  {
    sku: "REAG-001",
    name: "Chemistry reagent (synthetic)",
    location_code: "LOC-MAIN",
    lot_number: "LOT-Q",
    serial: null,
    expiry_date: "2027-06-01",
    quantity: 8,
    unit_cost_cents: 900,
    value_cents: 7200,
    stage: "quarantine",
    verified_by: null,
    verification_method: null,
    verification_result: null,
    verified_on: null,
    verification_note: null,
    composition: [],
    recall_blocked: false,
    blocked_by: [],
  },
  {
    sku: "KIT-001",
    name: "Procedure kit (synthetic)",
    location_code: "LOC-MAIN",
    lot_number: "LOT-K1",
    serial: null,
    expiry_date: "2027-03-01",
    quantity: 3,
    unit_cost_cents: 5000,
    value_cents: 15000,
    stage: "released",
    verified_by: "tech1",
    verification_method: "visual",
    verification_result: "pass",
    verified_on: "2026-10-01",
    verification_note: null,
    composition: [{ sku: "SUT-001", lot_number: "LOT-BAD", qty: 6 }],
    recall_blocked: true,
    blocked_by: [{ sku: "SUT-001", lot_number: "LOT-BAD" }],
  },
];

const CONTAINERS = [
  {
    container_code: "CNT-1",
    sku: "REAG-001",
    location_code: "LOC-MAIN",
    lot_number: "LOT-Q",
    serial: null,
    quantity: 5,
    stage: "released",
    opened_at: null,
    opened_by: null,
    in_use_expiry_date: null,
    effective_expiry: "2027-01-01",
    lot_stage: "quarantine",
    disposal_reason_code: null,
    disposed_by: null,
    disposed_at: null,
  },
  {
    container_code: "CNT-2",
    sku: "REAG-001",
    location_code: "LOC-MAIN",
    lot_number: "LOT-Q",
    serial: null,
    quantity: 3,
    stage: "in_use",
    opened_at: "2026-10-01T00:00:00Z",
    opened_by: "tech1",
    in_use_expiry_date: "2026-12-01",
    effective_expiry: "2026-12-01",
    lot_stage: "quarantine",
    disposal_reason_code: null,
    disposed_by: null,
    disposed_at: null,
  },
];

const ORG_TREE = {
  version: 1,
  regions: [
    {
      code: "REG-1",
      display: "Region One",
      locations: [
        {
          code: "LOC-MAIN",
          display: "Main Hospital",
          departments: [
            {
              code: "DEPT-STORES",
              display: "Central Stores",
              units: [{ code: "UNIT-STORES", display: "Main Stores", bed_count: 0 }],
            },
          ],
        },
      ],
    },
  ],
};

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    request: vi.fn(async (path: string) => {
      if (path === "/org-hierarchy") return ORG_TREE;
      throw new ApiError(404, `No stub for ${path}`);
    }),
    inventoryTaxonomy: vi.fn(async () => TAXONOMY),
    inventoryOverview: vi.fn(async () => OVERVIEW),
    inventoryItems: vi.fn(async () => ITEMS),
    inventoryStock: vi.fn(async () => STOCK),
    inventoryParLevels: vi.fn(async () => [
      {
        sku: "REAG-001",
        name: "Chemistry reagent (synthetic)",
        location_code: "LOC-MAIN",
        par_level: 100,
        par_unit_level: "case",
        par_qty: 1,
      },
    ]),
    inventoryMovements: vi.fn(async () => [
      {
        id: 7,
        sku: "KIT-001",
        event: "kit_open",
        location_code: "LOC-MAIN",
        quantity: -1,
        lot_number: "LOT-K1",
        unit_level: "case",
        container_code: "CNT-9",
        created_at: "2026-10-21T10:00:00Z",
      },
    ]),
    inventoryReceive: vi.fn(async () => ({ id: 42, sku: "REAG-001", quantity: 10 })),
    inventoryIssue: vi.fn(async () => ({ id: 43 })),
    inventoryTransfer: vi.fn(async () => ({ id: 44 })),
    inventoryAdjust: vi.fn(async () => ({ id: 45 })),
    inventoryScan: vi.fn(async () => ({ resolved: false, via: null, item: null, lot: null, stock: null })),
    inventoryRequisitions: vi.fn(async () => []),
    createInventoryRequisition: vi.fn(async () => ({})),
    inventoryCounts: vi.fn(async () => []),
    inventoryCount: vi.fn(async () => null),
    inventoryLowStock: vi.fn(async () => []),
    inventoryExpiry: vi.fn(async () => []),
    inventoryImplantTrace: vi.fn(async () => []),
    inventoryCaseIssue: vi.fn(async () => ({ case_id: "CASE-1", card_id: "CARD-1", lines: [] })),
    inventoryRfidEvent: vi.fn(async () => ({})),
    createInventoryItem: vi.fn(async () => ({})),
    updateInventoryItem: vi.fn(async () => ({})),
    setInventoryParLevel: vi.fn(async () => ({})),
    inventoryLotVerify: vi.fn(async () => ({})),
    inventoryLotStage: vi.fn(async () => ({})),
    inventoryContainers: vi.fn(async () => CONTAINERS),
    inventoryContainerOpen: vi.fn(async () => ({})),
    inventoryContainerConsume: vi.fn(async () => ({})),
    inventoryContainerDispose: vi.fn(async () => ({})),
    inventoryContainerTransfer: vi.fn(async () => ({ container: CONTAINERS[0], movements: [] })),
    inventoryKitOpen: vi.fn(async () => ({})),
    inventoryKitAssemble: vi.fn(async () => ({})),
    inventoryKitDisassemble: vi.fn(async () => ({})),
    ...overrides,
  } as unknown as ApiClient;
}

async function openStockTab(client: ApiClient) {
  render(<InventoryPage client={client} role="clinician" />);
  await screen.findByText("Inventory & Materials");
  fireEvent.click(screen.getByRole("tab", { name: "Stock" }));
  await screen.findByText("LOT-Q");
}

describe("InventoryPage depth", () => {
  it("item form renders ladder + kit editors for a kit item and submits structure/ladder/components", async () => {
    const client = stubClient();
    render(<InventoryPage client={client} role="clinician" />);
    expect(await screen.findByText("Procedure kit (synthetic)")).toBeTruthy();
    // List shows the structure badge, ladder summary and kit summary.
    expect(screen.getAllByText("kit").length).toBeGreaterThan(0);
    expect(screen.getByText("case → kit")).toBeTruthy();
    expect(screen.getByText(/track-whole · 1 component/)).toBeTruthy();
    expect(screen.getByText("case → box → vial")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Edit KIT-001" }));
    expect((screen.getByLabelText("Item structure") as HTMLSelectElement).value).toBe("kit");
    expect((screen.getByLabelText("Kit mode") as HTMLSelectElement).value).toBe("track-whole");
    expect((screen.getByLabelText("Ladder level 1 code") as HTMLInputElement).value).toBe("case");
    expect((screen.getByLabelText("Ladder level 2 code") as HTMLInputElement).value).toBe("kit");
    expect((screen.getByLabelText("Kit component 1") as HTMLSelectElement).value).toBe("SUT-001");
    expect((screen.getByLabelText("Kit component 1 quantity") as HTMLInputElement).value).toBe("2");

    fireEvent.click(screen.getByRole("button", { name: "Save item" }));
    await waitFor(() => {
      expect(client.updateInventoryItem).toHaveBeenCalledWith(
        "KIT-001",
        expect.objectContaining({
          structure: "kit",
          kit_mode: "track-whole",
          kit_components: [{ sku: "SUT-001", qty: 2 }],
          uom_ladder: [
            { code: "case", containsNext: 4 },
            { code: "kit", containsNext: 1 },
          ],
        }),
      );
    });
  });

  it("stock panel renders stage chip + verification state and a stage action offers the governed reason list", async () => {
    const client = stubClient();
    await openStockTab(client);
    expect(screen.getByText("quarantine")).toBeTruthy();
    expect(screen.getByText("not verified")).toBeTruthy();
    expect(screen.getByText("recall-blocked")).toBeTruthy();
    expect(screen.getByText(/SUT-001\/LOT-BAD/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Change stage LOT-Q" }));
    fireEvent.change(screen.getByLabelText("Target stage for LOT-Q"), {
      target: { value: "recalled" },
    });
    const reason = screen.getByLabelText("Stage reason for LOT-Q") as HTMLSelectElement;
    expect(Array.from(reason.options).map((o) => o.textContent)).toContain("Manufacturer recall");
    fireEvent.change(reason, { target: { value: "mfr-recall" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply stage change for LOT-Q" }));
    await waitFor(() => {
      expect(client.inventoryLotStage).toHaveBeenCalledWith(
        expect.objectContaining({
          sku: "REAG-001",
          location_code: "LOC-MAIN",
          lot_number: "LOT-Q",
          to_stage: "recalled",
          reason_code: "mfr-recall",
        }),
      );
    });
  });

  it("verify form submits method/result", async () => {
    const client = stubClient();
    await openStockTab(client);
    fireEvent.click(screen.getByRole("button", { name: "Verify lot LOT-Q" }));
    fireEvent.change(screen.getByLabelText("Verify method for LOT-Q"), {
      target: { value: "lot-to-lot comparison" },
    });
    fireEvent.change(screen.getByLabelText("Verify result for LOT-Q"), {
      target: { value: "pass" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Submit verification for LOT-Q" }));
    await waitFor(() => {
      expect(client.inventoryLotVerify).toHaveBeenCalledWith(
        expect.objectContaining({
          sku: "REAG-001",
          location_code: "LOC-MAIN",
          lot_number: "LOT-Q",
          method: "lot-to-lot comparison",
          result: "pass",
        }),
      );
    });
  });

  it("containers list renders effective expiry + stage-gated actions", async () => {
    const client = stubClient();
    await openStockTab(client);
    fireEvent.click(screen.getByRole("button", { name: "View containers for LOT-Q" }));
    expect(await screen.findByText("CNT-1")).toBeTruthy();
    expect(screen.getByText("CNT-2")).toBeTruthy();
    expect(screen.getByText("2027-01-01")).toBeTruthy();
    expect(screen.getByText(/2026-12-01 \(in-use\)/)).toBeTruthy();
    const consumeSealed = screen.getByRole("button", { name: "Consume from container CNT-1" }) as HTMLButtonElement;
    expect(consumeSealed.disabled).toBe(true);
    expect(screen.getByText(/Consume needs an opened container/)).toBeTruthy();
    const consumeOpen = screen.getByRole("button", { name: "Consume from container CNT-2" }) as HTMLButtonElement;
    expect(consumeOpen.disabled).toBe(false);
    const openSealed = screen.getByRole("button", { name: "Open container CNT-1" }) as HTMLButtonElement;
    expect(openSealed.disabled).toBe(false);
  });

  it("kit assemble form submits", async () => {
    const client = stubClient();
    await openStockTab(client);
    fireEvent.change(screen.getByLabelText("Kit operation item"), {
      target: { value: "KIT-001" },
    });
    fireEvent.change(screen.getByLabelText("Kit operation location"), {
      target: { value: "LOC-MAIN" },
    });
    fireEvent.change(screen.getByLabelText("Kit operation quantity"), {
      target: { value: "2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Assemble kit" }));
    await waitFor(() => {
      expect(client.inventoryKitAssemble).toHaveBeenCalledWith(
        expect.objectContaining({
          sku: "KIT-001",
          location_code: "LOC-MAIN",
          quantity: 2,
        }),
      );
    });
  });

  it("PAR form sends unit_level and the PAR list shows the entered level", async () => {
    const client = stubClient();
    await openStockTab(client);
    expect(screen.getByText("1 case")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("PAR item"), {
      target: { value: "REAG-001" },
    });
    fireEvent.change(screen.getByLabelText("PAR location"), {
      target: { value: "LOC-MAIN" },
    });
    fireEvent.change(screen.getByLabelText("PAR level"), {
      target: { value: "2" },
    });
    fireEvent.change(screen.getByLabelText("PAR unit level"), {
      target: { value: "case" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save PAR level" }));
    await waitFor(() => {
      expect(client.setInventoryParLevel).toHaveBeenCalledWith(
        expect.objectContaining({
          sku: "REAG-001",
          location_code: "LOC-MAIN",
          par_level: 2,
          unit_level: "case",
        }),
      );
    });
  });

  it("movements show unit_level + container_code and the event filter includes the new events", async () => {
    const client = stubClient();
    render(<InventoryPage client={client} role="clinician" />);
    await screen.findByText("Inventory & Materials");
    fireEvent.click(screen.getByRole("tab", { name: "Movements" }));
    expect(await screen.findByText("kit_open")).toBeTruthy();
    expect(screen.getByText("CNT-9")).toBeTruthy();
    const filter = screen.getByLabelText("Movement event filter") as HTMLSelectElement;
    const values = Array.from(filter.options).map((o) => o.value);
    expect(values).toContain("kit_assemble");
    expect(values).toContain("stage_transition");
    expect(values).toContain("container_consume");
  });
});
