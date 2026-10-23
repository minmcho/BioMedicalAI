import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { LabPreanalytical } from "./LabPreanalytical";

// LAB-D3/D4/D5 web surface with a stub client at the network boundary
// (synthetic fixture data only — shipped code uses the real ApiClient).

afterEach(cleanup);

const CONFIG = {
  config_key: "terminology/lab-preanalytical",
  receipt_checklist: [
    { code: "label-legible", display: "Label legible and matches requisition", criterionCode: "mislabelled", required: true },
    { code: "no-haemolysis", display: "No invalidating haemolysis", criterionCode: "hemolysed", required: true },
  ],
  transport_rules: [
    { specimenType: "Blood specimen", tubeEligible: true, neverTube: false, temperatureCondition: "ambient", maxTransitMinutes: 240 },
    { specimenType: "Sputum specimen", tubeEligible: false, neverTube: true, temperatureCondition: "ambient", maxTransitMinutes: 120 },
  ],
  default_transport_rule: null,
  storage_locations: [
    { code: "fridge-retained-a", display: "Retained fridge A", parentCode: "retained-store", temperatureCondition: "refrigerated", deviceKey: "fridge-lab-1" },
  ],
  retention_schedule: [{ specimenType: "Blood specimen", holdDays: 7 }],
  default_hold_days: 7,
  retrieval_purposes: ["addon", "repeat", "delta-lookup"],
  sendout_return_reasons: [{ code: "temperature-excursion", display: "Temperature excursion in transit" }],
};

const QUEUE_ITEM = {
  order_id: "ord-1", code: "58410-2", display: "CBC", bench_code: "haematology",
  specimen_id: "spec-1", specimen_type: "Blood specimen",
  collection_id: "ACC-20261022-0001", accession: null,
  patient_ref: "Patient/pat-1", collected_at: "2026-10-22T01:00:00+00:00",
  returned_from_sendout: false, return_reason_code: null, return_reason_display: null,
};

function makeClient(overrides: Record<string, unknown> = {}) {
  const state = { received: false, packed: false };
  const request = vi.fn(async (path: string, init?: { method?: string; body?: string }) => {
    const method = init?.method ?? "GET";
    if (path === "/lab/preanalytical/config") return CONFIG;
    if (path === "/lab/preanalytical/receiving-queue") {
      return { items: state.received ? [] : [QUEUE_ITEM], total: state.received ? 0 : 1 };
    }
    if (path === "/lab/preanalytical/receive" && method === "POST") {
      state.received = true;
      const body = JSON.parse(init?.body ?? "{}");
      const failed = (body.checklist ?? []).find((c: { passed: boolean }) => !c.passed);
      if (failed) {
        return { receipt: { id: 1, specimen_key: "spec-1", bench_code: "haematology", collection_id: "ACC-20261022-0001", accession: null, status: "rejected", rejection_criterion: "hemolysed", transport_exceptions: [] }, rejected: true, rejection: { criterion_code: "hemolysed", failed_items: [failed.code] } };
      }
      return { receipt: { id: 1, specimen_key: "spec-1", bench_code: "haematology", collection_id: "ACC-20261022-0001", accession: "ACC-20261022-0007", status: "accepted", rejection_criterion: null, transport_exceptions: [] }, rejected: false, accession: "ACC-20261022-0007", collection_id: "ACC-20261022-0001" };
    }
    if (path === "/lab/preanalytical/transport-exceptions") {
      return { items: [{ id: 1, specimen_key: "spec-9", method: "pneumatic-tube", temperature_condition: "ambient", transit_minutes: 30, exceptions: [{ code: "never-tube", detail: "Sputum specimen is on the governed never-tube list" }], actor: "tech", at: null }], total: 1 };
    }
    if (path === "/lab/preanalytical/storage/board") {
      return { stored: [{ id: 1, specimen_key: "spec-2", location_code: "fridge-retained-a", status: "stored", stored_by: "tech", stored_at: "2026-10-20T00:00:00+00:00", disposal_due: "2026-10-27T00:00:00+00:00", disposal_due_date: "2026-10-27", disposed_by: null, disposal_reason: null, excursion_flags: [] }], due_for_disposal: [], disposed: [], excursions: [], as_of: "2026-10-22" };
    }
    if (path === "/lab/preanalytical/sendouts/contracts") {
      return { laboratories: [
        { lab_code: "ref-regional", display: "Regional reference laboratory", tat_hours: 120, performing_lab: { name: "Regional Reference Laboratory (synthetic)" }, tests: [{ code: "SO-METANEPH", display: "Plasma free metanephrines", stability: { hours: 72, condition: "refrigerated" }, shipping_temperature: "refrigerated", split_before_transport: "Separate plasma", required_attachments: ["requisition-form"], report_window_hours: 120, classification: "FDA-cleared" }] },
        { lab_code: "ref-specialty", display: "National specialty reference laboratory", tat_hours: 240, performing_lab: { name: "National Specialty Reference Laboratory (synthetic)" }, tests: [{ code: "SO-VLCFA", display: "Very long-chain fatty acids", stability: { hours: 168, condition: "frozen" }, shipping_temperature: "frozen", split_before_transport: "Separate serum", required_attachments: ["requisition-form"], report_window_hours: 240, classification: "LDT" }] },
      ] };
    }
    if (path === "/lab/sendouts") {
      return { items: [{ order_id: "ord-9", code: "SO-METANEPH", display: "Plasma free metanephrines", sendout: { status: "awaiting-dispatch" }, reference_lab: { code: "ref-regional", display: "Regional reference laboratory", tat_hours: 120 } }] };
    }
    if (path === "/lab/preanalytical/sendouts/chase-list") {
      return { items: [{ order_id: "ord-7", code: "SO-VLCFA", lab_code: "ref-specialty", dispatched_at: "2026-10-01T00:00:00+00:00", expected_by: "2026-10-11T00:00:00+00:00", overdue_hours: 264, report_window_hours: 240, classification: "LDT" }], total: 1 };
    }
    if (path === "/lab/preanalytical/sendouts/pack" && method === "POST") {
      state.packed = true;
      return { manifest: { manifest_key: "man-1", lab_code: "ref-regional", order_ids: ["ord-9"], shipping_temperature: "refrigerated", split_instructions: { "SO-METANEPH": "Separate plasma" }, attachments: {}, status: "packed", expected_report_by: null, dispatched_at: null } };
    }
    if (path === "/lab/preanalytical/sendouts/manifests/man-1/dispatch" && method === "POST") {
      return { manifest: { manifest_key: "man-1", lab_code: "ref-regional", order_ids: ["ord-9"], shipping_temperature: "refrigerated", split_instructions: {}, attachments: {}, status: "dispatched", expected_report_by: "2026-10-27T00:00:00+00:00", dispatched_at: "2026-10-22T00:00:00+00:00" } };
    }
    if (overrides[path] !== undefined) return overrides[path];
    throw new Error(`unstubbed request: ${method} ${path}`);
  });
  return { client: { request } as unknown as ApiClient, request, state };
}

describe("LabPreanalytical", () => {
  it("renders the receiving queue with the collection ID and no accession yet", async () => {
    const { client } = makeClient();
    render(<LabPreanalytical client={client} />);
    expect(await screen.findByText("CBC")).toBeTruthy();
    expect(screen.getByText("ACC-20261022-0001")).toBeTruthy();
    expect(screen.getByText("minted at receipt")).toBeTruthy();
    expect(screen.getByText(/NEVER TUBE/)).toBeTruthy();
  });

  it("accessions at receipt through the governed checklist", async () => {
    const { client, request } = makeClient();
    render(<LabPreanalytical client={client} />);
    fireEvent.click(await screen.findByText("Receive"));
    fireEvent.click(await screen.findByText(/Scan to receive/));
    await waitFor(() => expect(screen.getByText(/Accessioned at receipt: ACC-20261022-0007/)).toBeTruthy());
    const receiveCall = request.mock.calls.find((c) => c[0] === "/lab/preanalytical/receive");
    const body = JSON.parse(receiveCall?.[1]?.body ?? "{}");
    expect(body.bench_code).toBe("haematology");
    expect(body.checklist).toHaveLength(2);
  });

  it("shows the coded rejection when a checklist item fails", async () => {
    const { client } = makeClient();
    render(<LabPreanalytical client={client} />);
    fireEvent.click(await screen.findByText("Receive"));
    const boxes = screen.getAllByRole("checkbox");
    fireEvent.click(boxes[1]); // no-haemolysis -> fail
    fireEvent.click(await screen.findByText(/Scan to receive/));
    await waitFor(() => expect(screen.getByText(/Rejected at receipt — coded criterion: hemolysed/)).toBeTruthy());
  });

  it("shows transport exceptions, storage board, contracts, and the chase list", async () => {
    const { client } = makeClient();
    render(<LabPreanalytical client={client} />);
    expect(await screen.findByText(/never-tube — Sputum specimen/)).toBeTruthy();
    expect(screen.getByText("fridge-retained-a")).toBeTruthy();
    expect(screen.getByText("FDA-cleared")).toBeTruthy();
    expect(screen.getAllByText("LDT").length).toBeGreaterThan(0);
    expect(screen.getByText(/264 h overdue/)).toBeTruthy();
    expect(screen.getByText(/no live sensor feed is claimed/)).toBeTruthy();
  });

  it("packs and dispatches a send-out through the contract gate", async () => {
    const { client, request } = makeClient();
    render(<LabPreanalytical client={client} />);
    const selects = await screen.findAllByRole("combobox");
    const orderSelect = selects.find((s) => s.innerHTML.includes("ord-9") || s.innerHTML.includes("Plasma free"));
    expect(orderSelect).toBeTruthy();
    fireEvent.change(orderSelect!, { target: { value: "ord-9" } });
    const attachment = await screen.findByLabelText(/requisition-form/);
    fireEvent.click(attachment);
    fireEvent.click(screen.getByText(/Pack & dispatch/));
    await waitFor(() => expect(request.mock.calls.some(
      (c) => c[0] === "/lab/preanalytical/sendouts/manifests/man-1/dispatch")).toBe(true));
    const packCall = request.mock.calls.find((c) => c[0] === "/lab/preanalytical/sendouts/pack");
    const packBody = JSON.parse(packCall?.[1]?.body ?? "{}");
    expect(packBody.attachments["ord-9"]).toEqual(["requisition-form"]);
  });
});
