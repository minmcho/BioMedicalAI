import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { LabPage } from "./LabPage";

// Depth pass sections (virology, send-outs, POCT, governance) with
// fixture (synthetic) data. The stub client stands in for the network
// boundary in a unit test only — shipped code always uses the real
// ApiClient.

afterEach(cleanup);

const CATALOG = {
  catalog: {
    name: "Depth test catalog",
    concepts: [
      { code: "20447-9", display: "HIV-1 RNA viral load", bench: "virology", unit: "copies/mL" },
      { code: "94500-6", display: "SARS-CoV-2 NAAT", bench: "virology", qualitative: true },
      { code: "SO-METANEPH", display: "Plasma metanephrines (send-out)", bench: "sendout", unit: "nmol/L" },
      { code: "2823-3", display: "Potassium (synthetic)", bench: "chemistry", unit: "mmol/L" },
    ],
    rejectionCriteria: [],
  },
  catalog_version: 1,
  workflow: {
    states: [
      "ordered", "collected", "received", "in-process",
      "resulted", "verified", "released",
    ],
    transitions: [],
  },
  workflow_version: 1,
};

const VIRO_ORDER = {
  resourceType: "ServiceRequest",
  id: "ord-viro-1",
  status: "released",
  code: { coding: [{ code: "20447-9", display: "HIV-1 RNA viral load" }] },
  subject: { reference: "Patient/pat-1" },
};

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    labCatalog: vi.fn(async () => CATALOG),
    labOrders: vi.fn(async () => [VIRO_ORDER]),
    verificationQueue: vi.fn(async () => ({ items: [], total: 0 })),
    labSpecimens: vi.fn(async () => []),
    qcRuns: vi.fn(async () => []),
    criticalReports: vi.fn(async () => ({ items: [], total: 0 })),
    labTrends: vi.fn(async () => ({
      patient: "pat-1",
      code: "20447-9",
      display: "HIV-1 RNA viral load",
      unit: "copies/mL",
      points: [
        {
          observation_id: "obs-1", value: 45200, unit: "copies/mL",
          effective: "2026-01-10T09:00:00Z", log10: 4.66,
        },
      ],
      count: 1,
    })),
    labSendouts: vi.fn(async () => ({
      items: [
        {
          order_id: "ord-so-1", code: "SO-METANEPH",
          display: "Plasma metanephrines (send-out)", patient: "pat-1",
          order_status: "collected",
          reference_lab: {
            code: "ref-regional", display: "Regional Reference Laboratory",
            tat_hours: 120,
          },
          sendout: {
            status: "awaiting-dispatch", manifest_id: null,
            dispatched_at: null, tracking_ref: null, returned_at: null,
            resulted_report_id: null,
          },
        },
        {
          order_id: "ord-so-2", code: "SO-METANEPH",
          display: "Plasma metanephrines (send-out)", patient: "pat-2",
          order_status: "in-process",
          reference_lab: {
            code: "ref-regional", display: "Regional Reference Laboratory",
            tat_hours: 120,
          },
          sendout: {
            status: "dispatched", manifest_id: "man-1",
            dispatched_at: "2026-01-14T08:00:00Z", tracking_ref: "TRK-1",
            returned_at: null, resulted_report_id: null,
          },
        },
      ],
      total: 2,
    })),
    labSendoutDispatch: vi.fn(async () => ({ manifest_id: "man-2" })),
    labSendoutReceiveResult: vi.fn(async () => ({})),
    labSendoutTat: vi.fn(async () => ({
      laboratories: [
        {
          lab: "ref-regional", display: "Regional Reference Laboratory",
          tat_hours: 120, resulted: 4, in_flight: 1, avg_hours: 96.5,
          within_tat_pct: 75.0,
        },
      ],
    })),
    labPoctOverview: vi.fn(async () => ({
      programme: { device_types: ["poct-analyser"] },
      enforcement: {
        block_uncompetent_operator: false,
        block_missing_daily_qc: false,
        state_label:
          "TRACKING ONLY — enforcement is OFF in the published poct-programme config.",
      },
      devices_due_qc: ["poc-1"],
      expiring_soon: [],
      expired: [],
      competency_validity_months: 12,
      qc_levels: ["L1", "L2"],
    })),
    labPoctDevices: vi.fn(async () => ({
      items: [
        {
          device_key: "poc-1", name: "Ward glucose meter",
          type: "poct-analyser", status: "active", location: "Ward 4",
          qc_today: { L1: null, L2: null }, qc_current: false,
        },
      ],
      total: 1,
    })),
    labPoctRecordQc: vi.fn(async () => ({})),
    labPoctCompetency: vi.fn(async () => [
      {
        id: 1, staff_id: "staff-001", test_code: "2823-3",
        method: "Direct observation", assessed_on: "2026-01-01",
        assessed_by: "dev-clinician", status: "current",
        expires_on: "2027-01-01",
      },
    ]),
    labPoctRecordCompetency: vi.fn(async () => ({})),
    labGovernance: vi.fn(async () => ({
      auto_verify: {
        enabled: false,
        state_label:
          "Auto-verification is DISABLED in the published catalog configuration.",
        rules: [
          { code: "2823-3", display: "Potassium (synthetic)", low: 3.5, high: 5.1 },
        ],
        recent_auto_verified: [],
      },
      reflex_rules: [
        {
          id: "hiv-reactive-add-diff", triggerCode: "HIV-AGAB",
          when: "qualitative-match", matchValue: "Reactive",
          addCode: "HIV-DIFF", note: null,
        },
      ],
      delta_checks: [
        {
          code: "14682-9", display: "Creatinine (synthetic)",
          config: { absolute: 26.5, windowHours: 72 },
        },
      ],
      stewardship: { duplicateCheckEnabled: true, duplicateWindowHours: 24 },
      config: { version: 3, edit_in: "System Studio → Laboratory catalog" },
    })),
    labStewardshipDuplicates: vi.fn(async () => ({
      window_hours: 24, groups: [], total: 0,
    })),
    ...overrides,
  } as unknown as ApiClient;
}

function clickTab(name: string) {
  fireEvent.click(screen.getByRole("tab", { name }));
}

function submitFormOf(buttonName: string) {
  const button = screen.getByRole("button", { name: buttonName });
  const form = button.closest("form");
  if (!form) throw new Error(`button ${buttonName} is not in a form`);
  fireEvent.submit(form);
}

describe("LabPage depth sections", () => {
  it("virology: lists the bench orders and loads a viral-load trend", async () => {
    const client = stubClient();
    render(<LabPage client={client} role="clinician" />);
    clickTab("Virology");
    expect(await screen.findByText("ord-viro-1")).toBeTruthy();
    submitFormOf("Load trend");
    expect(await screen.findByText("45200")).toBeTruthy();
    expect(screen.getByText("4.66")).toBeTruthy();
    expect(client.labTrends).toHaveBeenCalledWith("pat-1", "20447-9");
  });

  it("send-outs: dispatches a collected order and files a returned result", async () => {
    const client = stubClient();
    render(<LabPage client={client} role="clinician" />);
    clickTab("Send-outs");
    expect(await screen.findByText("Awaiting dispatch")).toBeTruthy();
    fireEvent.click(screen.getByLabelText(/Select order ord-so-1/));
    submitFormOf("Dispatch selected to Regional Reference Laboratory");
    await waitFor(() =>
      expect(client.labSendoutDispatch).toHaveBeenCalledWith(
        ["ord-so-1"], undefined,
      ),
    );
    // the dispatched order accepts its returned value (findByRole
    // waits out the section's refetch after the dispatch)
    const fileButton = await screen.findByRole("button", {
      name: "File returned result",
    });
    fireEvent.change(
      screen.getByLabelText("Returned value for order ord-so-2"),
      { target: { value: "3.2" } },
    );
    fireEvent.click(fileButton);
    await waitFor(() =>
      expect(client.labSendoutReceiveResult).toHaveBeenCalledWith("ord-so-2", [
        { code: "SO-METANEPH", value: 3.2 },
      ]),
    );
    // TAT by laboratory renders from its endpoint
    expect(screen.getByText("75%")).toBeTruthy();
  });

  it("poct: shows the tracking-only state, QC due, and records QC + competency", async () => {
    const client = stubClient();
    render(<LabPage client={client} role="clinician" />);
    clickTab("POCT");
    expect(await screen.findByText(/TRACKING ONLY/)).toBeTruthy();
    expect(screen.getByText("QC due")).toBeTruthy();
    expect(screen.getByText("staff-001")).toBeTruthy();

    submitFormOf("Record QC run");
    await waitFor(() =>
      expect(client.labPoctRecordQc).toHaveBeenCalledWith({
        device_key: "poc-1", level: "L1", result: "pass",
      }),
    );

    fireEvent.change(screen.getByLabelText("Staff id"), {
      target: { value: "staff-9" },
    });
    fireEvent.change(screen.getByLabelText("Test"), {
      target: { value: "2823-3" },
    });
    fireEvent.change(screen.getByLabelText("Assessment method"), {
      target: { value: "Direct observation" },
    });
    fireEvent.change(screen.getByLabelText("Assessed on"), {
      target: { value: "2026-01-15" },
    });
    submitFormOf("Record competency");
    await waitFor(() =>
      expect(client.labPoctRecordCompetency).toHaveBeenCalledWith({
        staff_id: "staff-9", test_code: "2823-3",
        method: "Direct observation", assessed_on: "2026-01-15",
      }),
    );
  });

  it("governance: auto-verify off state, reflex rules and delta windows are visible", async () => {
    render(<LabPage client={stubClient()} role="clinician" />);
    clickTab("Governance");
    expect(await screen.findByText("Auto-verify DISABLED")).toBeTruthy();
    expect(screen.getByText("hiv-reactive-add-diff")).toBeTruthy();
    expect(screen.getByText("72 h")).toBeTruthy();
    expect(
      screen.getByText("No duplicate orders inside the window."),
    ).toBeTruthy();
  });
});
