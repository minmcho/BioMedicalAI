import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type ApiClient } from "../api/client";
import { LabAnalyser } from "./LabAnalyser";

afterEach(cleanup);

const INSTRUMENTS = {
  items: [
    {
      instrument_id: "AN-1", name: "Analyser 1", bench: "chemistry",
      in_lab_registry: true, in_device_registry: true,
      lab_status: "active", device_status: "maintenance",
      effective_status: "in-maintenance", divergent: true,
      ingest_blocked: true,
      block_reason: "instrument 'AN-1' is in-maintenance (lab registry status 'active'; device registry status 'maintenance')",
    },
    {
      instrument_id: "AN-2", name: "Analyser 2", bench: "haematology",
      in_lab_registry: true, in_device_registry: false,
      lab_status: "active", device_status: null,
      effective_status: "in-service", divergent: false,
      ingest_blocked: false, block_reason: null,
    },
  ],
  total: 2, divergent: 1, ingest_blocked: 1,
};

const QUARANTINE = {
  items: [
    {
      quarantine_id: "liq-abc123", instrument_id: "AN-1", source_format: "hl7",
      reason: "instrument 'AN-1' is in-maintenance; results are quarantined, never written",
      effective_status: "in-maintenance", status: "held",
      payload: { kind: "groups" }, decided_by: null, reprocessed_report_ids: [],
      created_at: "2026-10-22T00:00:00Z",
    },
  ],
  total: 1, held: 1,
};

const AV_STATUS = {
  config: { kind: "terminology", key: "lab-autoverify", published_version: 2 },
  benches: [
    { bench: "chemistry", state: "live", rule_set_version: 2, rule_count: 5, enabled: true },
    { bench: "haematology", state: "unpublished", rule_set_version: null, rule_count: 0, enabled: false },
  ],
};

const HOLDS = {
  items: [
    {
      id: 9, report_id: "rep-9", order_id: "ord-9", bench: "chemistry",
      status: "recorded",
      hold_reasons: ["chem-glucose-amr: 2345-7 value 700 is outside the analytical measurement range (AMR 20–600)"],
      trace: [
        { rule_id: "qc-gate", type: "qc", outcome: "pass", reason: "no QC blocker" },
        { rule_id: "chem-glucose-amr", type: "amr", outcome: "hold", reason: "2345-7 value 700 is outside AMR" },
      ],
    },
  ],
  total: 1,
};

const RATES = {
  window_days: 30,
  items: [
    { bench: "chemistry", released: 2, held: 2, total: 4, rate: 0.5 },
    { bench: "haematology", released: 0, held: 0, total: 0, rate: null },
  ],
};

const RERUNS = {
  items: [
    { item_id: "rerun-1", order_id: "ord-1", analyte_code: "718-7", instrument_id: "AN-2", flags: ["DIL"], status: "pending" },
  ],
  total: 1,
};

function stubClient() {
  const calls: Array<{ path: string; method?: string; body?: string }> = [];
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    calls.push({ path, method: init?.method, body: init?.body as string | undefined });
    if (path === "/lab/analyser/instruments") return INSTRUMENTS;
    if (path === "/lab/analyser/quarantine") {
      if (init?.method === "POST") return {};
      return QUARANTINE;
    }
    if (path.includes("/quarantine/")) return { ...QUARANTINE.items[0], status: "rejected" };
    if (path === "/lab/analyser/rerun-queue") return RERUNS;
    if (path === "/lab/autoverify/status") return AV_STATUS;
    if (path === "/lab/autoverify/holds") return HOLDS;
    if (path.startsWith("/lab/autoverify/rates")) return RATES;
    if (path.startsWith("/lab/analyser/worklist/")) {
      return {
        instrument_id: "AN-2", bench: "haematology", note: "Worklist = orders on the bench.",
        items: [{ order_id: "ord-wl", code: "58410-2", status: "received", patient_ref: "Patient/pat-1", accession: "ACC-1" }],
        rerun_items: RERUNS.items,
      };
    }
    return {};
  });
  return { client: { request } as unknown as ApiClient, calls };
}

describe("LabAnalyser", () => {
  it("shows the reconciled registries, quarantine, bench rule-set states, holds with traces, rerun queue and AV rates", async () => {
    const { client } = stubClient();
    render(<LabAnalyser client={client} />);
    // reconciled instrument view: divergence + effective status named
    expect(await screen.findByText(/DIVERGENT — registries disagree/)).toBeTruthy();
    expect(screen.getAllByText(/in-maintenance/).length).toBeGreaterThan(0);
    // quarantine held batch with its named reason
    expect(await screen.findByText(/liq-abc123/)).toBeTruthy();
    expect(screen.getByText(/results are quarantined, never written/)).toBeTruthy();
    // reprocess disabled with the reason while the instrument is blocked
    const reprocess = screen.getByRole("button", { name: "Reprocess batch" }) as HTMLButtonElement;
    expect(reprocess.disabled).toBe(true);
    expect(screen.getByText(/Reprocessing is disabled: the instrument is still not in service/)).toBeTruthy();
    // bench rule-set states: live vs unpublished
    expect(await screen.findByText(/LIVE — auto-release under rules/)).toBeTruthy();
    expect(screen.getByText(/Unpublished — nothing auto-releases/)).toBeTruthy();
    // hold with named reason + rule trace
    expect(await screen.findByText(/chem-glucose-amr: 2345-7 value 700/)).toBeTruthy();
    expect(screen.getByText(/chem-glucose-amr \(amr\): hold/)).toBeTruthy();
    // rerun queue item
    expect(await screen.findByText(/rerun-1/)).toBeTruthy();
    // AV rate math rendered
    expect(await screen.findByText("50.0%")).toBeTruthy();
    expect(screen.getByText(/— \(no evaluations\)/)).toBeTruthy();
  });

  it("rejects a quarantined batch with its note, and loads a worklist", async () => {
    const { client, calls } = stubClient();
    render(<LabAnalyser client={client} />);
    const note = await screen.findByLabelText("Note for liq-abc123");
    fireEvent.change(note, { target: { value: "Specimens recollected" } });
    fireEvent.click(screen.getByRole("button", { name: "Reject batch" }));
    await waitFor(() => {
      expect(calls.some((c) => c.path.includes("/quarantine/liq-abc123/reject")
        && c.body?.includes("Specimens recollected"))).toBe(true);
    });
    // worklist for AN-2
    const select = screen.getByLabelText("Worklist instrument");
    fireEvent.change(select, { target: { value: "AN-2" } });
    expect(await screen.findByText(/ord-wl/)).toBeTruthy();
    expect(screen.getByText(/ACC-1/)).toBeTruthy();
  });
});
