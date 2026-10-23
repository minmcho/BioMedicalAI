import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { LabMicro } from "./LabMicro";

// LAB-D12 web surface with a stub client at the network boundary
// (synthetic fixture data only — shipped code uses the real
// ApiClient).

afterEach(cleanup);

const OPT = (code: string, display: string, extra: object = {}) =>
  ({ code, display, active: true, ...extra });

const CONFIG = {
  vocab: {
    gramMorphologies: [OPT("gram-negative-rods", "Gram-negative rods"),
                       OPT("none-seen", "No organisms seen")],
    quantities: [OPT("moderate", "Moderate")],
    cellBands: [OPT("10-25", "10-25"), OPT("<10", "<10")],
    adequacyValues: [OPT("adequate", "Adequate")],
    media: [OPT("blood-agar", "Blood agar")],
    streakPatterns: [OPT("four-quadrant", "Four-quadrant streak")],
    atmospheres: [OPT("co2", "5-10% CO2")],
    cultureTypes: [OPT("blood", "Blood culture"), OPT("urine", "Urine culture")],
    bottleTypes: [OPT("aerobic", "Aerobic bottle")],
    idMethods: [OPT("maldi-tof", "MALDI-TOF mass spectrometry",
                    { carriesConfidence: true, minConfidence: 75 }),
                OPT("colony-morphology", "Colony morphology",
                    { carriesConfidence: false })],
    antibiogramMinIsolates: 30,
    antibiogramPeriodDays: 365,
  },
  organisms: [OPT("escherichia-coli", "Escherichia coli")],
  organisms_version: 1,
  antibiotics: [OPT("ciprofloxacin", "Ciprofloxacin"),
                OPT("meropenem", "Meropenem")],
  antibiotics_version: 1,
  breakpoint_tables: [{ standard: "CLSI", version: "M100-34", entries: [] }],
  breakpoints_version: 1,
  expert_rules: [],
  selective_rules: [],
};

function workup(view: string, criticalState = "pending") {
  const suppressedEntry = view === "released"
    ? { antibiotic: "meropenem", antibiotic_display: "Meropenem",
        suppressed: true,
        suppression_reason: "Cascade: carbapenems reported only when first-line agents are resistant" }
    : { antibiotic: "meropenem", antibiotic_display: "Meropenem",
        mic: 0.5, zone_mm: null, standard: "CLSI", version: "M100-34",
        computed_interpretation: "S", expert_traces: [],
        final_interpretation: "S", suppressed: true,
        suppression_reason: "Cascade: carbapenems reported only when first-line agents are resistant" };
  return {
    workup: {
      order_id: "ord-1", patient_ref: "Patient/pat-1",
      culture_type: "blood",
      plating: { media: ["blood-agar"], streak_pattern: "four-quadrant",
                 incubation: { temperature_c: 35, atmosphere: "co2",
                               duration_hours: 24 },
                 started_at: "2026-10-21T00:00:00Z", recorded_by: "tech" },
      gram_stain: { morphology: "gram-negative-rods", quantity: "moderate",
                    wbc: "10-25", epithelial_cells: "<10",
                    adequacy: "adequate", organisms_present: true },
      critical_call: { state: criticalState, notified_to: null,
                       read_back: null },
      colony: { bottle: { set_id: "SET-1", bottle_type: "aerobic",
                          time_to_positivity_hours: 11.5 } },
      isolates: [{
        isolate_id: "iso-1", organism_code: "escherichia-coli",
        organism_display: "Escherichia coli", id_method: "maldi-tof",
        id_confidence: 98, id_below_threshold: false,
        ast: [
          { antibiotic: "ciprofloxacin", antibiotic_display: "Ciprofloxacin",
            mic: 1, zone_mm: null, standard: "CLSI", version: "M100-35",
            computed_interpretation: "S",
            expert_traces: [{ rule_code: "test-inducible-rule",
                              reason: "pattern implies resistance",
                              from_interpretation: "S",
                              to_interpretation: "R" }],
            final_interpretation: "R", suppressed: false },
          suppressedEntry,
        ],
      }],
      mdro_alerts: [],
      finalized_at: null,
      view,
    },
    order_status: "susceptibility-reported",
  };
}

function stubClient() {
  let criticalState = "pending";
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === "/lab/micro-depth/config") return CONFIG;
    if (path.includes("/gram-stain/notify")) {
      criticalState = "notified";
      return {};
    }
    if (path.includes("/workup?view=released")) {
      return workup("released", criticalState);
    }
    if (path.includes("/workup?view=lab")) {
      return workup("lab", criticalState);
    }
    if (path.startsWith("/lab/micro-depth/antibiogram")) {
      return {
        items: [
          { organism: "escherichia-coli", organism_display: "Escherichia coli",
            antibiotic: "ciprofloxacin", isolates: 3, susceptible: 2,
            pct_s: 66.7, suppressed: false, suppression_reason: null },
          { organism: "klebsiella-pneumoniae", antibiotic: "meropenem",
            isolates: 2, susceptible: 1, pct_s: null, suppressed: true,
            suppression_reason: "Below the minimum isolate count (30) for a published %S (CLSI M39 pattern)" },
        ],
        total: 2, period: { from: "2025-10-21", to: "2026-10-21" },
        min_isolates: 30, governed_min_isolates: 30,
      };
    }
    void init;
    return {};
  });
  return { client: { request } as unknown as ApiClient, request };
}

async function loadWorkup() {
  await waitFor(() => screen.getByLabelText("Culture order ID"));
  fireEvent.change(screen.getByLabelText("Culture order ID"),
                   { target: { value: "ord-1" } });
  fireEvent.click(screen.getByRole("button", { name: "Load workup" }));
  await waitFor(() => screen.getByText(/ID by maldi-tof/));
}

describe("LabMicro (LAB-D12)", () => {
  it("renders the workup: gram stain, colony bottle, isolate AST with expert trace and suppression", async () => {
    const { client } = stubClient();
    render(<LabMicro client={client} />);
    await loadWorkup();
    expect(screen.getByText(/gram-negative-rods, moderate/)).toBeTruthy();
    expect(screen.getByText(/Bottle set SET-1/)).toBeTruthy();
    expect(screen.getByText(/test-inducible-rule: S → R/)).toBeTruthy();
    expect(screen.getByText(/Suppressed from the released set:/)).toBeTruthy();
  });

  it("critical call requires a named receiver and read-back, then notifies", async () => {
    const { client, request } = stubClient();
    render(<LabMicro client={client} />);
    await loadWorkup();
    const notify = screen.getByRole("button", { name: "Record critical call" });
    expect((notify as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Notified to"),
                     { target: { value: "Dr Synthetic" } });
    fireEvent.click(screen.getByLabelText("Read back"));
    expect((notify as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(notify);
    await waitFor(() => {
      const call = request.mock.calls.find(
        ([path]) => String(path).includes("/gram-stain/notify"));
      expect(call).toBeTruthy();
      expect(JSON.parse(String(call?.[1]?.body))).toEqual(
        { notified_to: "Dr Synthetic", read_back: true });
    });
  });

  it("released view hides suppressed values but keeps the released set", async () => {
    const { client } = stubClient();
    render(<LabMicro client={client} />);
    await loadWorkup();
    fireEvent.click(screen.getByRole("button", { name: "Released set" }));
    await waitFor(() =>
      expect(screen.getByText(/Cascade: carbapenems reported/)).toBeTruthy());
    // ciprofloxacin (released) still shows its final interpretation
    expect(screen.getAllByText("R").length).toBeGreaterThan(0);
  });

  it("antibiogram shows %S and minimum-count suppression", async () => {
    const { client } = stubClient();
    render(<LabMicro client={client} />);
    await waitFor(() =>
      screen.getByRole("button", { name: "Load antibiogram" }));
    fireEvent.click(screen.getByRole("button", { name: "Load antibiogram" }));
    await waitFor(() => screen.getByText("66.7%"));
    expect(screen.getByText(/Below the minimum isolate count/)).toBeTruthy();
  });

  it("ID confidence is disabled with a reason for methods that carry none", async () => {
    const { client } = stubClient();
    render(<LabMicro client={client} />);
    await loadWorkup();
    fireEvent.change(screen.getByLabelText("ID method"),
                     { target: { value: "colony-morphology" } });
    const confidence = screen.getByLabelText("ID confidence") as HTMLInputElement;
    expect(confidence.disabled).toBe(true);
    expect(screen.getByText(/does not carry a confidence score/)).toBeTruthy();
  });
});
