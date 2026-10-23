import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import { LabReagentsQc } from "./LabReagentsQc";

afterEach(cleanup);

const RUN = {
  id: 7,
  sku: "REAG-001",
  location_code: "facility-main",
  lot_number: "RL-1",
  analyte_code: "2823-3",
  status: "comparing",
  decision: null,
  decided_by: null,
  criteria: { minSamples: 3, maxBiasPct: 10, maxBiasAbs: null, source: "byAnalyte", label: "GOVERNED DEFAULTS — director to set" },
  comparison: {
    meets_criteria: true,
    reasons: [],
    sample_count: 3,
    min_samples: 3,
    mean_bias: 0.03,
    per_pair: [
      { sample_ref: "sample-1", old_value: 4.0, new_value: 4.1, bias: 0.1, bias_pct: 2.5, within_criteria: true },
    ],
  },
  inventory_lot: { stage: "quarantine", verification_result: null },
};

const CONTROL_LOT = {
  id: 3,
  lot_key: "CTL-1",
  analyte_code: "2823-3",
  level: "L1",
  state: "collecting",
  required_runs: 20,
  collected_runs: 12,
  runs_remaining: 8,
  established_mean: null,
  established_sd: null,
  usable_as_patient_qc: false,
};

const CAL = {
  id: 5,
  instrument_id: "AN-1",
  kind: "calibration",
  passed: true,
  due_date: "2027-01-01",
  registry_due: null,
  failure_reasons: [],
  performed_at: "2026-10-21T00:00:00Z",
  performed_by: "clinician",
};

function stubClient(): ApiClient {
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === "/lab/reagent-verifications") {
      if (init?.method === "POST") return { ...RUN, status: "collecting" };
      return { items: [RUN], total: 1 };
    }
    if (path === "/lab/control-lots") return { items: [CONTROL_LOT], total: 1 };
    if (path === "/lab/calibrations") return { items: [CAL], total: 1 };
    if (path === "/inventory/stock") {
      return {
        items: [
          { sku: "REAG-001", location_code: "facility-main", lot_number: "RL-1", stage: "quarantine", quantity: 100 },
        ],
      };
    }
    if (path === "/lab/instruments") {
      return [{ instrument_id: "AN-1", name: "Analyser 1", bench: "chemistry", status: "active" }];
    }
    if (path.startsWith("/lab/qc/instrument-status")) {
      return {
        instrument_id: "AN-1",
        path_locked: false,
        calibration: { status: "ok", source: "device-registry", due: "2027-01-01" },
        items: [
          {
            analyte_code: "2823-3",
            reagent_lot_key: "REAG-001|facility-main|RL-1",
            shift: "day",
            path_locked: false,
            latest: { measured_value: 4.0, passed: true, flags: [], control_material: "CTL-1" },
          },
        ],
      };
    }
    throw new ApiError(404, `No stub for ${path}`);
  });
  return {
    request,
    labCatalog: vi.fn(async () => ({
      catalog: {
        concepts: [
          { code: "2823-3", display: "Potassium", active: true },
          { code: "51990-0", display: "BMP panel", active: true, panel: true },
        ],
      },
      catalog_version: 1,
      workflow: null,
      workflow_version: null,
    })),
  } as unknown as ApiClient;
}

describe("LabReagentsQc (Lab Depth W4)", () => {
  it("states the no-emergency-bypass JIT position", async () => {
    render(<LabReagentsQc client={stubClient()} />);
    expect(await screen.findByText(/No emergency bypass/)).toBeTruthy();
  });

  it("renders a verification run with its computed comparison and governed criteria", async () => {
    render(<LabReagentsQc client={stubClient()} />);
    const open = await screen.findByRole("button", { name: /REAG-001 \/ RL-1/ });
    fireEvent.click(open);
    expect(await screen.findByText(/MEETS the director-configured criteria/)).toBeTruthy();
    expect(screen.getByText(/GOVERNED DEFAULTS/)).toBeTruthy();
    expect(screen.getByText(/within criteria/)).toBeTruthy();
    // Decision controls are live for an undecided run.
    expect((screen.getByRole("button", { name: "Sign acceptance" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("shows the control-lot onboarding board with runs remaining and gated actions", async () => {
    render(<LabReagentsQc client={stubClient()} />);
    expect(await screen.findByText("CTL-1")).toBeTruthy();
    expect(screen.getByText(/12 of 20 collected \(8 remaining\)/)).toBeTruthy();
    expect(screen.getByText(/refuses patient-QC use/)).toBeTruthy();
    const establish = screen.getByRole("button", { name: "Establish mean/SD" }) as HTMLButtonElement;
    expect(establish.disabled).toBe(true);
    expect(screen.getByText(/8 more establishment run\(s\) required first/)).toBeTruthy();
  });

  it("shows QC status by instrument, lot and shift plus calibration due dates", async () => {
    render(<LabReagentsQc client={stubClient()} />);
    const select = await screen.findByLabelText("QC instrument");
    fireEvent.change(select, { target: { value: "AN-1" } });
    await waitFor(() => expect(screen.getByText("REAG-001|facility-main|RL-1")).toBeTruthy());
    expect(screen.getByText("day")).toBeTruthy();
    expect(screen.getByText(/truth: device-registry/)).toBeTruthy();
    // Calibration list carries the due date.
    expect(await screen.findByText("2027-01-01")).toBeTruthy();
  });

  it("disables lot selection with a reason when inventory has no lots", async () => {
    const client = stubClient();
    (client.request as ReturnType<typeof vi.fn>).mockImplementation(async (path: string) => {
      if (path === "/inventory/stock") return { items: [] };
      if (path === "/lab/reagent-verifications") return { items: [], total: 0 };
      if (path === "/lab/control-lots") return { items: [], total: 0 };
      if (path === "/lab/calibrations") return { items: [], total: 0 };
      if (path === "/lab/instruments") return [];
      throw new ApiError(404, `No stub for ${path}`);
    });
    render(<LabReagentsQc client={client} />);
    const lotSelect = await screen.findByLabelText("Inventory lot");
    expect((lotSelect as HTMLSelectElement).disabled).toBe(true);
    expect(screen.getByText("No lots in inventory stock")).toBeTruthy();
  });
});
