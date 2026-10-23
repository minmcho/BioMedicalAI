import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import { InventoryPage } from "./InventoryPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

const TAXONOMY = {
  categories: [
    {
      code: "surgical-supplies",
      label: "Surgical supplies",
      subcategories: [{ code: "sutures-staples", label: "Sutures & staples" }],
    },
    {
      code: "implants",
      label: "Implants",
      subcategories: [{ code: "orthopedic-implants", label: "Orthopaedic implants" }],
    },
  ],
  reasonCodes: [
    { code: "damaged", label: "Damaged" },
    { code: "other", label: "Other (note required)" },
  ],
  expiryWarningDays: 90,
};

const OVERVIEW = {
  active_items: 3,
  stock_locations: 2,
  on_hand_units: 250,
  low_stock_lines: 1,
  expiring_lines: 2,
  expired_lines: 1,
  movements_today: 5,
  stock_value_cents: 123456,
  uncosted_lots: 1,
};

const ITEMS = [
  {
    sku: "SUT-001",
    name: "Suture pack (synthetic)",
    category: "surgical-supplies",
    subcategory: "sutures-staples",
    uom: "each",
    lot_tracked: true,
    serial_tracked: false,
    expiry_tracked: true,
    unit_cost_cents: 1250,
    formulary_code: null,
    asset_key: null,
    active: true,
  },
  {
    sku: "IMP-001",
    name: "Hip implant (synthetic)",
    category: "implants",
    subcategory: "orthopedic-implants",
    uom: "each",
    lot_tracked: true,
    serial_tracked: true,
    expiry_tracked: true,
    unit_cost_cents: 50000,
    formulary_code: null,
    asset_key: null,
    active: true,
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
    inventoryStock: vi.fn(async () => [
      {
        sku: "SUT-001",
        name: "Suture pack (synthetic)",
        location_code: "LOC-MAIN",
        lot_number: "LOT-1",
        serial: null,
        expiry_date: "2027-06-01",
        quantity: 40,
        unit_cost_cents: 1250,
        value_cents: 50000,
      },
    ]),
    inventoryParLevels: vi.fn(async () => [
      { sku: "SUT-001", name: "Suture pack (synthetic)", location_code: "LOC-MAIN", par_level: 10 },
    ]),
    inventoryMovements: vi.fn(async () => []),
    inventoryReceive: vi.fn(async () => ({
      id: 42,
      sku: "SUT-001",
      location_code: "LOC-MAIN",
      quantity: 10,
      lot_number: "LOT-9",
    })),
    inventoryIssue: vi.fn(async () => ({ id: 43, sku: "SUT-001", quantity: -1 })),
    inventoryTransfer: vi.fn(async () => ({ id: 44 })),
    inventoryAdjust: vi.fn(async () => ({ id: 45 })),
    inventoryScan: vi.fn(async () => ({
      resolved: true,
      via: "sku",
      item: ITEMS[0],
      lot: { lot_number: "LOT-9", expiry_date: "2027-06-01" },
      stock: null,
    })),
    inventoryRequisitions: vi.fn(async () => []),
    createInventoryRequisition: vi.fn(async () => ({})),
    inventoryCounts: vi.fn(async () => []),
    inventoryCount: vi.fn(async () => null),
    inventoryLowStock: vi.fn(async () => [
      {
        sku: "SUT-001",
        name: "Suture pack (synthetic)",
        location_code: "LOC-MAIN",
        on_hand: 2,
        par_level: 10,
        deficit: 8,
      },
    ]),
    inventoryExpiry: vi.fn(async () => []),
    inventoryImplantTrace: vi.fn(async () => []),
    inventoryCaseIssue: vi.fn(async () => ({ case_id: "CASE-1", card_id: "CARD-1", lines: [] })),
    inventoryRfidEvent: vi.fn(async () => ({})),
    ...overrides,
  } as unknown as ApiClient;
}

describe("InventoryPage", () => {
  it("renders the overview strip from the mocked aggregates", async () => {
    render(<InventoryPage client={stubClient()} role="clinician" />);
    expect(await screen.findByText("Inventory & Materials")).toBeTruthy();
    expect(await screen.findByText("$1,234.56")).toBeTruthy();
    expect(screen.getByText("Active items")).toBeTruthy();
    expect(screen.getByText("250")).toBeTruthy();
    expect(screen.getByText("Movements today")).toBeTruthy();
    expect(screen.getByText("needs reorder")).toBeTruthy();
    expect(screen.getByText("expired")).toBeTruthy();
  });

  it("renders all six tabs", async () => {
    render(<InventoryPage client={stubClient()} role="clinician" />);
    await screen.findByText("$1,234.56");
    for (const label of ["Items", "Stock", "Movements", "Receiving & Issue", "Counts", "Worklists"]) {
      expect(screen.getByRole("tab", { name: label })).toBeTruthy();
    }
  });

  it("lists mocked items with category filter options from the taxonomy", async () => {
    render(<InventoryPage client={stubClient()} role="clinician" />);
    expect(await screen.findByText("Suture pack (synthetic)")).toBeTruthy();
    expect(screen.getByText("Hip implant (synthetic)")).toBeTruthy();
    const categoryFilter = screen.getByLabelText("Filter by category") as HTMLSelectElement;
    const options = Array.from(categoryFilter.options).map((o) => o.textContent);
    expect(options).toContain("Surgical supplies");
    expect(options).toContain("Implants");
    // The item form's category select is taxonomy-fed too.
    const formCategory = screen.getByLabelText("Item category") as HTMLSelectElement;
    expect(Array.from(formCategory.options).map((o) => o.textContent)).toContain("Implants");
    expect(screen.getByRole("button", { name: "Create item" })).toBeTruthy();
  });

  it("receive stepper submits and shows the created movement", async () => {
    const client = stubClient();
    render(<InventoryPage client={client} role="clinician" />);
    await screen.findByText("$1,234.56");
    fireEvent.click(screen.getByRole("tab", { name: "Receiving & Issue" }));
    fireEvent.change(await screen.findByLabelText("Receive item"), {
      target: { value: "SUT-001" },
    });
    fireEvent.change(screen.getByLabelText("Receiving location"), {
      target: { value: "LOC-MAIN" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fireEvent.change(await screen.findByLabelText("Receive quantity"), {
      target: { value: "10" },
    });
    fireEvent.change(screen.getByLabelText("Receive lot number"), {
      target: { value: "LOT-9" },
    });
    fireEvent.change(screen.getByLabelText("Receive expiry date"), {
      target: { value: "2027-06-01" },
    });
    const submitButtons = screen.getAllByRole("button", { name: "Receive stock" });
    fireEvent.click(submitButtons[submitButtons.length - 1]);
    await waitFor(() => {
      expect(client.inventoryReceive).toHaveBeenCalledWith(
        expect.objectContaining({
          sku: "SUT-001",
          location_code: "LOC-MAIN",
          quantity: 10,
          lot_number: "LOT-9",
          expiry_date: "2027-06-01",
        }),
      );
    });
    expect(await screen.findByText(/Received 10 × SUT-001 into LOC-MAIN/)).toBeTruthy();
    expect(screen.getByText(/movement #42/)).toBeTruthy();
  });

  it("surfaces a 409 from issue inline next to the form", async () => {
    const client = stubClient({
      inventoryIssue: vi.fn(async () => {
        throw new ApiError(409, "Insufficient stock for 'SUT-001' at LOC-MAIN: 0 on hand, 5 requested");
      }),
    });
    render(<InventoryPage client={client} role="clinician" />);
    await screen.findByText("$1,234.56");
    fireEvent.click(screen.getByRole("tab", { name: "Receiving & Issue" }));
    fireEvent.change(await screen.findByLabelText("Movement item"), {
      target: { value: "SUT-001" },
    });
    fireEvent.change(screen.getByLabelText("Issuing location"), {
      target: { value: "LOC-MAIN" },
    });
    fireEvent.change(screen.getByLabelText("Issue quantity"), {
      target: { value: "5" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Issue stock" }));
    expect(await screen.findByText(/Insufficient stock for 'SUT-001'/)).toBeTruthy();
  });

  it("renders low-stock worklist rows with their deficit", async () => {
    render(<InventoryPage client={stubClient()} role="clinician" />);
    await screen.findByText("$1,234.56");
    fireEvent.click(screen.getByRole("tab", { name: "Worklists" }));
    expect(await screen.findByText("deficit 8")).toBeTruthy();
    expect(screen.getByText("Low stock — below PAR")).toBeTruthy();
  });

  it("viewer sees read-only inventory with no receive or item-create controls", async () => {
    render(<InventoryPage client={stubClient()} role="viewer" />);
    expect(await screen.findByText("Suture pack (synthetic)")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Create item" })).toBeNull();
    // The header's Receive stock primary action is present but
    // disabled with its reason, and the Receiving tab hides its forms.
    const receiveButtons = screen.getAllByRole("button", { name: "Receive stock" });
    expect(receiveButtons.every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
    fireEvent.click(screen.getByRole("tab", { name: "Receiving & Issue" }));
    expect((await screen.findAllByText(/read-only/)).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Issue stock" })).toBeNull();
  });
});
