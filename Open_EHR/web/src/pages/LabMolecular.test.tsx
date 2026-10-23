import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { LabMolecular } from "./LabMolecular";

// LAB-D14 web surface with a stub client at the network boundary
// (synthetic fixture data only — shipped code uses the real
// ApiClient).

afterEach(cleanup);

const OPT = (code: string, display: string, extra: object = {}) =>
  ({ code, display, active: true, ...extra });

const CONFIG = {
  config: { kind: "terminology", key: "lab-molecular", version: 1 },
  assays: [{
    code: "94500-6", display: "SARS-CoV-2 RNA by NAA", active: true,
    targets: [{ code: "sars-cov-2", display: "SARS-CoV-2 RNA",
                detectedMaxCt: 37, inconclusiveMaxCt: 40 }],
    requiresInternalControl: true, requiresExtractionControl: true,
    requiresPositiveControl: true, maxBatchSize: 24,
    runWindows: ["08:00", "14:00"],
  }],
  areas: [OPT("clean-pre-amp", "Clean / pre-amplification area", { order: 0 }),
          OPT("specimen-extraction", "Extraction", { order: 1 }),
          OPT("post-amp-dirty", "Post-amplification area", { order: 2 })],
  ntc_positive_max_ct: 40,
  repeat_options: [OPT("extraction", "Repeat from extraction"),
                   OPT("amplification", "Repeat from amplification")],
  decontamination_methods: [
    OPT("sodium-hypochlorite", "10% sodium hypochlorite")],
};

const BATCH = {
  id: 1, assay_code: "94500-6", instrument_id: "MOL-INST-1",
  operator: "tech", status: "open",
  samples: [{ order_id: "ord-1", specimen_id: "spec-1",
              patient_ref: "Patient/pat-1" }],
  sample_count: 1,
  area_stamps: [{ area: "clean-pre-amp", at: "2026-10-22T00:00:00Z",
                  by: "tech", flagged: false }],
  workflow_flagged: false, workflow_flag_reason: null,
  created_by: "tech", created_at: "2026-10-22T00:00:00Z",
};

function runRow(overrides: object = {}) {
  return {
    id: 1, batch_id: 1, assay_code: "94500-6",
    instrument_id: "MOL-INST-1", operator: "tech", status: "open",
    ntc: { status: "unresolved", ct: null },
    positive_control: { status: null, ct: null },
    invalidated_reason: null, invalidated_by: null, invalidated_at: null,
    repeat_of_run_id: null, repeat_from: null, repeat_run_id: null,
    released_by: null, released_at: null,
    gate: { can_release: false,
            blockers: ["NTC gate: run 1 cannot release any result while "
                       + "its no-template control status is UNRESOLVED"] },
    sample_count: 1,
    samples: [{
      id: 1, run_id: 1, order_id: "ord-1", specimen_id: "spec-1",
      patient_ref: "Patient/pat-1", ct_values: { "sars-cov-2": 25 },
      target_interpretations: { "sars-cov-2": "detected" },
      internal_control: { status: "passed", ct: 30 },
      extraction_control: { status: "passed", ct: 29 },
      interpretation: "Detected", discarded_interpretation: null,
      validity: "valid", validity_reason: null, report_id: null,
      resulted_by: "tech", resulted_at: "2026-10-22T00:00:00Z",
    }],
    batch: BATCH,
    ...overrides,
  };
}

const INVALIDATED = runRow({
  id: 2, status: "invalidated",
  ntc: { status: "positive", ct: 22.5 },
  invalidated_reason: "NTC positive: the no-template control amplified "
    + "(Ct 22.5) — the run is contaminated and INVALID; every result "
    + "in run 2 is discarded",
  gate: { can_release: false,
          blockers: ["Run 2 is INVALIDATED (NTC positive); its results "
                     + "are discarded and can never be released"] },
  samples: [{
    id: 2, run_id: 2, order_id: "ord-2", specimen_id: "spec-2",
    patient_ref: "Patient/pat-2", ct_values: { "sars-cov-2": 25 },
    target_interpretations: { "sars-cov-2": "detected" },
    internal_control: { status: "passed", ct: 30 },
    extraction_control: { status: "passed", ct: 29 },
    interpretation: null, discarded_interpretation: "Detected",
    validity: "invalidated",
    validity_reason: "NTC positive — result discarded",
    report_id: null, resulted_by: "tech",
    resulted_at: "2026-10-22T00:00:00Z",
  }],
});

const EVENT = {
  id: 7, trigger: "ntc-positive", status: "open", note: "NTC positive",
  source_run_id: 2, affected_run_ids: [2], affected_batch_ids: [1],
  instruments: ["MOL-INST-1"], areas: ["post-amp-dirty"],
  decontamination: null, raised_by: "tech",
  created_at: "2026-10-22T00:00:00Z", cleared_by: null, cleared_at: null,
};

function stubClient() {
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    void init;
    if (path === "/lab/molecular/config") return CONFIG;
    if (path === "/lab/molecular/batches") return { items: [BATCH], total: 1 };
    if (path === "/lab/molecular/runs") {
      return { items: [runRow(), INVALIDATED], total: 2 };
    }
    if (path === "/lab/molecular/contamination-events") {
      return { items: [EVENT], total: 1 };
    }
    if (path === "/lab/molecular/runs/1") return runRow();
    if (path === "/lab/molecular/runs/2") return INVALIDATED;
    if (path === "/lab/molecular/runs/1/controls") {
      return runRow({ ntc: { status: "negative", ct: null } });
    }
    return {};
  });
  return { client: { request } as unknown as ApiClient, request };
}

describe("LabMolecular (LAB-D14)", () => {
  it("renders the governed assay config with Ct bands and the board", async () => {
    const { client } = stubClient();
    render(<LabMolecular client={client} />);
    // the assay name appears in the config table AND in the
    // create-batch select — both are correct renderings
    await waitFor(() => expect(
      screen.getAllByText(/SARS-CoV-2 RNA by NAA/).length)
      .toBeGreaterThan(0));
    expect(screen.getByText(/detected ≤37, inconclusive ≤40/)).toBeTruthy();
    expect(screen.getByText(/NTC positivity ceiling Ct 40/)).toBeTruthy();
    // the batch label appears on the board AND in the create-run
    // batch select — both are correct renderings
    expect(screen.getAllByText(/Batch 1 — 94500-6/).length)
      .toBeGreaterThan(0);
  });

  it("shows the unresolved NTC prominently and disables release with its blocker", async () => {
    const { client } = stubClient();
    render(<LabMolecular client={client} />);
    await waitFor(() =>
      screen.getByRole("button", { name: "Open run 1" }));
    fireEvent.click(screen.getByRole("button", { name: "Open run 1" }));
    await waitFor(() =>
      screen.getByText(/NTC status: UNRESOLVED/));
    const release = screen.getByRole("button",
      { name: "Release run 1 results" }) as HTMLButtonElement;
    expect(release.disabled).toBe(true);
    expect(screen.getByText(/Release disabled: NTC gate/)).toBeTruthy();
    expect(screen.getByText("Per-sample validity")).toBeTruthy();
  });

  it("records run controls with a null NTC Ct for a true negative", async () => {
    const { client, request } = stubClient();
    render(<LabMolecular client={client} />);
    await waitFor(() =>
      screen.getByRole("button", { name: "Open run 1" }));
    fireEvent.click(screen.getByRole("button", { name: "Open run 1" }));
    await waitFor(() => screen.getByLabelText("NTC did not amplify"));
    fireEvent.click(screen.getByLabelText("NTC did not amplify"));
    fireEvent.click(screen.getByRole("button",
      { name: "Record run controls" }));
    await waitFor(() => {
      const call = request.mock.calls.find(
        ([path]) => String(path).includes("/runs/1/controls"));
      expect(call).toBeTruthy();
      const body = JSON.parse(String(call?.[1]?.body));
      expect(body.ntc_ct).toBeNull();
      expect(body.positive_control_status).toBe("passed");
    });
  });

  it("an invalidated run LOOKS invalidated: banner, discarded result, repeat workflow", async () => {
    const { client } = stubClient();
    render(<LabMolecular client={client} />);
    await waitFor(() =>
      screen.getByRole("button", { name: "Open run 2" }));
    fireEvent.click(screen.getByRole("button", { name: "Open run 2" }));
    await waitFor(() =>
      screen.getByText(/ALL RESULTS DISCARDED/));
    expect(screen.getByText(/\(DISCARDED\)/)).toBeTruthy();
    expect(screen.getByText("Per-sample validity")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Repeat run 2" }))
      .toBeTruthy();
    // controls are closed on an invalidated run
    const controls = screen.getByRole("button",
      { name: "Record run controls" }) as HTMLButtonElement;
    expect(controls.disabled).toBe(true);
  });

  it("contamination surface lists the blast radius and gates decontamination on a method", async () => {
    const { client } = stubClient();
    render(<LabMolecular client={client} />);
    await waitFor(() => screen.getByText(/Event 7/));
    expect(screen.getByText(/affected runs 2/)).toBeTruthy();
    const decon = screen.getByRole("button",
      { name: "Record decontamination 7" }) as HTMLButtonElement;
    expect(decon.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Decontamination method 7"),
                     { target: { value: "sodium-hypochlorite" } });
    expect(decon.disabled).toBe(false);
    // raising an event requires a scope
    const raise = screen.getByRole("button",
      { name: "Raise event" }) as HTMLButtonElement;
    expect(raise.disabled).toBe(true);
  });
});
