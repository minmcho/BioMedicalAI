import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import { LabPage } from "./LabPage";
import { PanelsTab, SpecimensTab, TestCatalogueTab } from "./LabManagerTabs";

// Phase 15 (scope B): Test Catalogue / Panels manager tabs (draft-only
// compose on the governed catalogue, laboratory-director 403 state)
// and the Specimens aliquot + chain-of-custody tab. The stub client
// stands in for the network boundary in a unit test only — shipped
// code always uses the real ApiClient; the standalone labDepth module
// calls client.request / client.requestBlob, which is what is stubbed.

afterEach(cleanup);

const CATALOG = {
  catalog_version: 3,
  config: {
    kind: "terminology",
    key: "lab-catalog",
    publish_in: "System Studio -> Terminology -> lab-catalog",
  },
  tests: [
    {
      code: "2823-3",
      display: "Potassium",
      active: true,
      bench: "chemistry",
      unit: "mmol/L",
      qualitative: false,
      aliquot_rules: { count: 2, volumeMl: 5, container: "serum separator tube" },
      aliquot_rules_source: "catalog",
      specimen_requirements: {
        specimen_type: "Blood specimen",
        container: "serum separator tube",
        required_aliquots: 2,
        volume_ml: 5,
      },
      reflex_rules: [],
      panel: false,
    },
  ],
  panels: [
    {
      code: "BMP",
      display: "Basic metabolic panel",
      active: true,
      bench: "chemistry",
      panel: true,
      components: [{ code: "2823-3", display: "Potassium" }],
      component_codes: ["2823-3"],
      reflex_rules: [
        {
          id: "reflex-bmp-1",
          triggerCode: "2823-3",
          when: "below-low",
          addCode: "ECG",
          note: "",
        },
      ],
      aliquot_rules: { count: 1, volumeMl: 5, container: "standard tube" },
      aliquot_rules_source: "catalog",
      specimen_requirements: {
        specimen_type: "Blood specimen",
        container: "standard tube",
        required_aliquots: 1,
        volume_ml: 5,
      },
    },
  ],
  reflex_rules: [],
  draft: null,
};

const BREAK_REASON =
  "Missing predecessor event 'transport' before 'receipt' in specimen " +
  "spec-1-a1's custody chain";

const PARENT_NODE = {
  specimen_key: "spec-1",
  specimen_id: "spec-1",
  entity_ref: "Specimen/spec-1",
  parent_key: null,
  root_key: "spec-1",
  derivation_type: "primary",
  sequence: 0,
  volume_ml: 10,
  remaining_volume_ml: 8,
  container: "standard tube",
  status: "collected",
  order_id: "ord-1",
  patient_ref: "Patient/pat-1",
  organism_code: null,
  barcode: "OCSP1|spec-1|primary-0|42",
  custody: [
    {
      id: 1, specimen_key: "spec-1", action: "collection", actor: "nurse-1",
      location: "Ward 4", note: null, is_break: false, break_reason: null,
      at: "2026-10-18T08:00:00Z",
    },
  ],
};

const CHILD_NODE = {
  specimen_key: "spec-1-a1",
  specimen_id: "spec-1-a1",
  entity_ref: "Specimen/spec-1-a1",
  parent_key: "spec-1",
  root_key: "spec-1",
  derivation_type: "aliquot",
  sequence: 1,
  volume_ml: 2,
  remaining_volume_ml: 2,
  container: "standard tube",
  status: "collected",
  order_id: "ord-1",
  patient_ref: "Patient/pat-1",
  organism_code: null,
  barcode: "OCSP1|spec-1-a1|aliquot-1|43",
  custody: [
    {
      id: 2, specimen_key: "spec-1-a1", action: "receipt", actor: "tech-1",
      location: "Central lab", note: null, is_break: true,
      break_reason: BREAK_REASON, at: "2026-10-18T09:00:00Z",
    },
  ],
};

const CHAIN = {
  root_key: "spec-1",
  requested_member: "spec-1",
  members: [PARENT_NODE, CHILD_NODE],
  total: 2,
  tree: { ...PARENT_NODE, children: [{ ...CHILD_NODE, children: [] }] },
  breaks: [{ ...CHILD_NODE.custody[0] }],
};

const BARCODES = {
  items: [
    {
      value: "OCSP1|spec-1|primary-0|42", kind: "primary",
      entity_ref: "Specimen/spec-1", specimen_key: "spec-1",
    },
    {
      value: "OCSP1|spec-1-a1|aliquot-1|43", kind: "aliquot",
      entity_ref: "Specimen/spec-1-a1", specimen_key: "spec-1-a1",
    },
  ],
  total: 2,
};

const CUSTODY_BREAKS = {
  items: [
    {
      root_key: "spec-1", specimen_key: "spec-1-a1", action: "receipt",
      reason: BREAK_REASON, actor: "tech-1", location: "Central lab",
      at: "2026-10-18T09:00:00Z",
    },
  ],
  total: 1,
  families: ["spec-1"],
  family_count: 1,
};

type RequestFn = (path: string, init?: RequestInit) => Promise<unknown>;

function makeRequest(overrides?: Partial<Record<string, RequestFn>>): RequestFn {
  const routes: Record<string, RequestFn> = {
    "/lab/manager/catalog": async () => CATALOG,
    "/lab/manager/tests/draft": async () => ({
      draft: {
        kind: "terminology", key: "lab-catalog", version: 4,
        status: "draft",
        publish_via: "POST /studio/config/terminology/lab-catalog/versions/4/publish",
      },
      test: { code: "NEW-1", display: "New test" },
    }),
    "/lab/manager/panels/draft": async () => ({
      draft: {
        kind: "terminology", key: "lab-catalog", version: 4,
        status: "draft",
        publish_via: "POST /studio/config/terminology/lab-catalog/versions/4/publish",
      },
      panel: { code: "NP", display: "New panel" },
    }),
    "/lab/specimen-barcodes": async () => BARCODES,
    "/lab/specimens/custody-breaks": async () => CUSTODY_BREAKS,
    "/lab/specimens/scan": async () => ({
      event: { id: 3, action: "transport", is_break: false },
      specimen: PARENT_NODE,
    }),
    ...overrides,
  };
  return async (path: string) => {
    if (routes[path]) return routes[path](path);
    if (path.startsWith("/lab/specimens/") && path.endsWith("/chain")) {
      return CHAIN;
    }
    if (path.includes("/derive")) {
      return { parent: PARENT_NODE, children: [CHILD_NODE], total: 1 };
    }
    if (path.includes("/add-tests")) {
      return {
        addon: {
          id: 1, specimen_key: "spec-1", test_code: "2823-3",
          required_volume_ml: 5, order_id: "ord-2",
        },
        specimen: PARENT_NODE,
      };
    }
    throw new Error(`unstubbed request path: ${path}`);
  };
}

function stubClient(requestImpl?: RequestFn): ApiClient {
  return {
    request: vi.fn(requestImpl ?? makeRequest()),
    requestBlob: vi.fn(async () => ({
      blob: new Blob(["%PDF"], { type: "application/pdf" }),
      filename: "label-specimen.pdf",
      rows: null,
    })),
    // LabPage's own top-level loaders (used by the mounting test).
    labCatalog: vi.fn(async () => ({
      catalog: { name: "Catalog", concepts: [], rejectionCriteria: [] },
      catalog_version: 1,
      workflow: { states: [], transitions: [] },
      workflow_version: 1,
    })),
    labOrders: vi.fn(async () => []),
    verificationQueue: vi.fn(async () => ({ items: [], total: 0 })),
    labSpecimens: vi.fn(async () => []),
    qcRuns: vi.fn(async () => []),
    criticalReports: vi.fn(async () => ({ items: [], total: 0 })),
  } as unknown as ApiClient;
}

function submitFormOf(buttonName: string) {
  const button = screen.getByRole("button", { name: buttonName });
  const form = button.closest("form");
  if (!form) throw new Error(`button ${buttonName} is not in a form`);
  fireEvent.submit(form);
}

describe("Phase 15 LabPage manager + specimens tabs", () => {
  it("test catalogue: renders tests with specimen/container and aliquot rules", async () => {
    render(<TestCatalogueTab client={stubClient()} />);
    expect(await screen.findByText("Potassium")).toBeTruthy();
    expect(screen.getByText("2823-3")).toBeTruthy();
    expect(
      screen.getByText("Blood specimen / serum separator tube"),
    ).toBeTruthy();
    expect(
      screen.getByText("2 × 5 mL (serum separator tube)"),
    ).toBeTruthy();
    expect(screen.getByText(/Draft-only: saving composes/)).toBeTruthy();
  });

  it("test catalogue: draft-compose submit calls the draft endpoint", async () => {
    const client = stubClient();
    render(<TestCatalogueTab client={client} />);
    await screen.findByText("Potassium");
    fireEvent.click(screen.getByRole("button", { name: "Propose change" }));
    fireEvent.change(screen.getByLabelText("Test code"), {
      target: { value: "NEW-1" },
    });
    fireEvent.change(screen.getByLabelText("Display name"), {
      target: { value: "New test" },
    });
    submitFormOf("Save draft change");
    await waitFor(() => {
      const calls = (client.request as ReturnType<typeof vi.fn>).mock.calls;
      const call = calls.find((c) => c[0] === "/lab/manager/tests/draft");
      expect(call).toBeTruthy();
      const body = JSON.parse((call?.[1] as RequestInit).body as string);
      expect(body.code).toBe("NEW-1");
      expect(body.display).toBe("New test");
      expect(body.aliquot_rules).toMatchObject({ count: 1, volumeMl: 5 });
    });
    expect(
      await screen.findByText(/Draft change saved onto catalogue draft v4/),
    ).toBeTruthy();
  });

  it("test catalogue: a 403 renders the laboratory-director read-only state", async () => {
    const request = makeRequest({
      "/lab/manager/tests/draft": async () => {
        throw new ApiError(
          403,
          "Catalogue composition requires the laboratory director job role ('lab-director')",
        );
      },
    });
    render(<TestCatalogueTab client={stubClient(request)} />);
    await screen.findByText("Potassium");
    fireEvent.click(screen.getByRole("button", { name: "Propose change" }));
    fireEvent.change(screen.getByLabelText("Test code"), {
      target: { value: "NEW-1" },
    });
    fireEvent.change(screen.getByLabelText("Display name"), {
      target: { value: "New test" },
    });
    submitFormOf("Save draft change");
    expect(
      await screen.findByText(/Read-only — laboratory director approval required/),
    ).toBeTruthy();
    expect(screen.getByText(/laboratory director job role/)).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Save draft change" }),
    ).toBeNull();
  });

  it("panels: renders panel definitions with components, reflex and aliquots", async () => {
    render(<PanelsTab client={stubClient()} />);
    expect(await screen.findByText(/Basic metabolic panel \(BMP\)/)).toBeTruthy();
    expect(screen.getByText(/Components: Potassium/)).toBeTruthy();
    expect(
      screen.getByText(/2823-3 \(below-low\) → ECG/),
    ).toBeTruthy();
    expect(
      screen.getByText(/Required aliquots: 1 × 5 mL \(standard tube\)/),
    ).toBeTruthy();
  });

  it("panels: LabPage mounts the Panels tab for the panels section", async () => {
    render(<LabPage client={stubClient()} role="clinician" section="panels" />);
    expect(await screen.findByText(/Basic metabolic panel \(BMP\)/)).toBeTruthy();
  });

  it("specimens: renders the family chain (parent + child) with custody events", async () => {
    render(<SpecimensTab client={stubClient()} />);
    // Master list + tree both name the members; the parent's own
    // custody timeline is shown for the default selection.
    expect((await screen.findAllByText(/spec-1-a1/)).length).toBeGreaterThan(0);
    expect((await screen.findAllByText(/spec-1/)).length).toBeGreaterThan(0);
    expect((await screen.findAllByText("collection")).length).toBeGreaterThan(0);
    expect(screen.getByText(/Family root spec-1 · 2 member\(s\)/)).toBeTruthy();
    expect(screen.getByText(/Custody timeline — spec-1/)).toBeTruthy();

    // Selecting the aliquot shows its custody timeline.
    fireEvent.click(
      screen.getByRole("button", { name: "View specimen spec-1-a1" }),
    );
    expect((await screen.findAllByText("receipt")).length).toBeGreaterThan(0);
    expect(
      await screen.findByText(/Custody timeline — spec-1-a1/),
    ).toBeTruthy();
  });

  it("specimens: custody-break flag is visible on the worklist and timeline", async () => {
    render(<SpecimensTab client={stubClient()} />);
    // Worklist above the list carries the flagged break with its reason.
    expect((await screen.findAllByText("Custody break")).length).toBeGreaterThan(0);
    expect(screen.getAllByText(new RegExp("Missing predecessor event")).length)
      .toBeGreaterThan(0);
    // And the child's own timeline flags the break event too.
    fireEvent.click(
      await screen.findByRole("button", { name: "View specimen spec-1-a1" }),
    );
    expect(
      await screen.findByText(/Custody timeline — spec-1-a1/),
    ).toBeTruthy();
    expect(screen.getAllByText("Custody break").length).toBeGreaterThan(1);
  });

  it("specimens: recording a scan event calls the scan endpoint", async () => {
    const client = stubClient();
    render(<SpecimensTab client={client} />);
    await screen.findByText(/Family root spec-1/);
    fireEvent.change(screen.getByLabelText("Custody action"), {
      target: { value: "transport" },
    });
    submitFormOf("Record scan event");
    await waitFor(() => {
      const calls = (client.request as ReturnType<typeof vi.fn>).mock.calls;
      const call = calls.find((c) => c[0] === "/lab/specimens/scan");
      expect(call).toBeTruthy();
      const body = JSON.parse((call?.[1] as RequestInit).body as string);
      expect(body.identifier).toBe("spec-1");
      expect(body.action).toBe("transport");
    });
  });
});
