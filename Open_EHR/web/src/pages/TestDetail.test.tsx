import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import {
  fixtureLabCatalog,
  fixtureLabOrder,
  fixtureQcRun,
  fixtureSpecimen,
  fixtureVerificationItem,
} from "../test/fixtures";
import { LabPage } from "./LabPage";

// Fixture shapes copied from the REAL backend serializer:
// GET /lab/orders/{id}/test-detail in backend/app/lab.py `test_detail`
// (order / steps / specimen / aliquots via lab_specimens._node_out /
// results / report / acknowledgements via _notification_out / custody),
// with values mirroring backend/tests/test_lab_test_detail.py's full
// plated journey (potassium 6.4, HH, delta previous 4.0, acknowledged).
afterEach(cleanup);

const PLATED_DETAIL = {
  order: {
    id: "ord-lab-1", status: "released", code: "2823-3",
    display: "Potassium (synthetic)", patient_ref: "Patient/pat-1",
    authored_on: "2026-01-15T09:00:00Z", requester: "Dr Synthetic",
    priority: "routine",
  },
  steps: [
    { key: "ordered", label: "Ordered", state: "done", at: "2026-01-15T09:00:00Z", by: "Dr Synthetic" },
    { key: "collected", label: "Collected", state: "done", at: "2026-01-15T09:05:00Z", by: "dev-clinician" },
    { key: "received", label: "Received", state: "done", at: "2026-01-15T09:10:00Z", by: "dev-clinician" },
    { key: "plated", label: "Plated", state: "done", at: "2026-01-15T09:12:00Z", by: "dev-clinician", plate_id: 1 },
    { key: "in_analysis", label: "In analysis", state: "done", at: "2026-01-15T09:15:00Z", by: "dev-clinician" },
    { key: "analysed", label: "Analysed", state: "done", at: "2026-01-15T09:20:00Z", by: "dev-clinician" },
    { key: "verified", label: "Verified", state: "done", at: "2026-01-15T09:25:00Z", by: "dev-clinician" },
    { key: "resulted", label: "Resulted", state: "done", at: "2026-01-15T09:30:00Z", by: "dev-clinician" },
  ],
  specimen: {
    id: "spec-1", status: "resulted", accession: "ACC-20260115-0001",
    type: "Blood specimen", subject_ref: "Patient/pat-1",
    collected_at: "2026-01-15T09:05:00Z", received_at: "2026-01-15T09:10:00Z",
    in_analysis_at: "2026-01-15T09:15:00Z",
  },
  aliquots: [
    {
      specimen_key: "spec-1-aliquot-1", specimen_id: "spec-1-aliquot-1",
      entity_ref: "Specimen/spec-1-aliquot-1", parent_key: "spec-1",
      root_key: "spec-1", derivation_type: "aliquot", sequence: 1,
      volume_ml: 2.0, remaining_volume_ml: 2.0, container: "SST",
      status: "available", order_id: "ord-lab-1", patient_ref: "Patient/pat-1",
      organism_code: null, barcode: "OCSP1|spec-1-aliquot-1", custody: [],
    },
  ],
  results: [
    {
      analyte: "2823-3", code: "2823-3", display: "Potassium (synthetic)",
      observation_id: "obs-lab-1", value: 6.4, unit: "mmol/L",
      reference_range: { low: 3.5, high: 5.1, unit: "mmol/L" },
      interpretation: "HH", interpretation_text: "Critical high",
      delta: {
        code: "2823-3", previous_value: 4.0, current_value: 6.4,
        change: 2.4, change_pct: 60.0, previous_at: "2026-01-14T09:00:00Z",
        window_hours: 72,
      },
      critical: true, status: "final",
    },
  ],
  report: {
    report_id: "rep-lab-1", status: "final", verified_by: "dev-clinician",
    verified_at: "2026-01-15T09:25:00Z", released_by: "dev-clinician",
    released_at: "2026-01-15T09:30:00Z", effective_at: "2026-01-15T09:20:00Z",
    critical: true,
  },
  acknowledgements: [
    {
      report_id: "rep-lab-1", state: "acknowledged",
      notified_to: "Dr Synthetic (attending)", read_back: true,
      notified_by: "dev-clinician", notified_at: "2026-01-15T09:35:00Z",
      acknowledged_by: "dev-clinician", acknowledged_at: "2026-01-15T09:40:00Z",
    },
  ],
  custody: {
    ordered_at: "2026-01-15T09:00:00Z", collected_at: "2026-01-15T09:05:00Z",
    received_at: "2026-01-15T09:10:00Z", in_analysis_at: "2026-01-15T09:15:00Z",
  },
};

const NON_PLATED_DETAIL = {
  ...PLATED_DETAIL,
  order: { ...PLATED_DETAIL.order, status: "resulted" },
  steps: [
    { key: "ordered", label: "Ordered", state: "done", at: "2026-01-15T09:00:00Z", by: "Dr Synthetic" },
    { key: "collected", label: "Collected", state: "done", at: "2026-01-15T09:05:00Z", by: "dev-clinician" },
    { key: "received", label: "Received", state: "done", at: "2026-01-15T09:10:00Z", by: "dev-clinician" },
    { key: "in_analysis", label: "In analysis", state: "done", at: "2026-01-15T09:15:00Z", by: "dev-clinician" },
    { key: "analysed", label: "Analysed", state: "done", at: "2026-01-15T09:20:00Z", by: "dev-clinician" },
    { key: "verified", label: "Verified", state: "current", at: null, by: null },
    { key: "resulted", label: "Resulted", state: "pending", at: null, by: null },
  ],
  aliquots: [],
  results: [
    {
      ...PLATED_DETAIL.results[0],
      value: 95, unit: "mg/dL", analyte: "2345-7", code: "2345-7",
      display: "Glucose (synthetic)",
      reference_range: { low: 70.0, high: 99.0, unit: "mg/dL" },
      interpretation: "N", interpretation_text: "Normal",
      delta: null, critical: false, status: "preliminary",
    },
  ],
  report: {
    report_id: "rep-lab-1", status: "preliminary", verified_by: null,
    verified_at: null, released_by: null, released_at: null,
    effective_at: "2026-01-15T09:20:00Z", critical: false,
  },
  acknowledgements: [],
};

function stubClient(detail: unknown): ApiClient {
  return {
    labCatalog: vi.fn(async () => fixtureLabCatalog),
    labOrders: vi.fn(async () => [fixtureLabOrder]),
    verificationQueue: vi.fn(async () => ({ items: [fixtureVerificationItem], total: 1 })),
    labSpecimens: vi.fn(async () => [fixtureSpecimen]),
    qcRuns: vi.fn(async () => [fixtureQcRun]),
    request: vi.fn(async (path: string) => {
      if (path === "/lab/orders/ord-lab-1/test-detail") return detail;
      throw new Error(`unexpected request ${path}`);
    }),
  } as unknown as ApiClient;
}

async function openDetail(detail: unknown) {
  render(<LabPage client={stubClient(detail)} role="clinician" />);
  expect(await screen.findByText("ord-lab-1")).toBeTruthy();
  fireEvent.click(screen.getAllByRole("button", { name: "Test detail" })[0]);
  expect(await screen.findByText(/Test detail — order ord-lab-1/)).toBeTruthy();
}

describe("LabPage Test detail (WS3b)", () => {
  it("renders the plated step, delta + critical badges as text, report stamps and the ack trail", async () => {
    await openDetail(PLATED_DETAIL);
    // stepper steps incl. plated, with Done text states
    expect(screen.getAllByText("Plated").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText(/\(Done\)/).length).toBe(8);
    expect(screen.getByText(/plate 1/)).toBeTruthy();
    // specimen + aliquot lineage
    expect(screen.getByText(/ACC-20260115-0001/)).toBeTruthy();
    expect(screen.getByText(/spec-1-aliquot-1/)).toBeTruthy();
    // results: value, range, interpretation badge text, delta with prior, critical badge
    expect(screen.getByText(/6\.4 mmol\/L/)).toBeTruthy();
    expect(screen.getByText(/3\.5–5\.1 mmol\/L/)).toBeTruthy();
    expect(screen.getByText("Critical high")).toBeTruthy();
    expect(screen.getByText(/Δ 2823-3: prior 4 → 6\.4/)).toBeTruthy();
    expect(screen.getByText("CRITICAL")).toBeTruthy();
    // report stamps
    expect(screen.getByText(/verified by dev-clinician/)).toBeTruthy();
    expect(screen.getByText(/released by dev-clinician/)).toBeTruthy();
    // acknowledgement trail: notified / acknowledged by + at
    expect(screen.getByText(/State: acknowledged/)).toBeTruthy();
    expect(screen.getByText(/notified Dr Synthetic \(attending\) by dev-clinician/)).toBeTruthy();
    expect(screen.getByText(/acknowledged by dev-clinician/)).toBeTruthy();
  });

  it("omits the plated step when the payload has none, and shows pending/current states + no delta", async () => {
    await openDetail(NON_PLATED_DETAIL);
    expect(screen.queryByText("Plated")).toBeNull();
    expect(screen.getAllByText(/\(Done\)/).length).toBe(5);
    expect(screen.getByText(/\(Current\)/)).toBeTruthy();
    expect(screen.getByText(/\(Pending\)/)).toBeTruthy();
    expect(screen.getByText("No delta flag")).toBeTruthy();
    expect(screen.getByText("Not critical")).toBeTruthy();
    expect(screen.getByText("No aliquots.")).toBeTruthy();
    expect(screen.getByText("No acknowledgements.")).toBeTruthy();
    expect(screen.getByText(/not yet verified/)).toBeTruthy();
  });
});
