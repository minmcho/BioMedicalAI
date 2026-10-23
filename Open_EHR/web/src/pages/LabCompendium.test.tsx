import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LabCompendium, type CompendiumApi } from "./LabCompendium";

// Lab Depth D1 + D2 web surface: directory browser (per-test
// attributes, partitioned ranges, TAT targets with declared clocks)
// + bench list with workload counts and the handover panel. The
// stub api stands in for the network boundary in a unit test only —
// shipped code always uses the real ApiClient.request.

afterEach(cleanup);

const DIRECTORY = {
  total: 2,
  tests: [
    {
      code: "2345-7",
      display: "Glucose [Mass/volume] in Serum or Plasma",
      active: true,
      bench: "chemistry",
      panel: false,
      components: [],
      unit: "mg/dL",
      qualitative: false,
      reference_range: { low: 70, high: 99, unit: "mg/dL" },
      reference_range_partitions: null,
      directory: {
        performingSection: "Chemistry",
        performingSite: "Central laboratory",
        method: "Hexokinase (enzymatic)",
        specimen: {
          container: "Serum separator tube",
          specimenType: "Serum",
          minimumVolumeMl: 3.5,
        },
        availability: { mode: "24-7" },
        tatTargets: [
          { priority: "stat", clock: "order-to-result", targetMinutes: 60 },
          { priority: "routine", clock: "receipt-to-result", targetMinutes: 180 },
        ],
      },
    },
    {
      code: "718-7",
      display: "Hemoglobin [Mass/volume] in Blood",
      active: true,
      bench: "haematology",
      panel: false,
      components: [],
      unit: "g/dL",
      qualitative: false,
      reference_range: { low: 12.0, high: 15.5, criticalLow: 7.0, criticalHigh: 20.0, unit: "g/dL" },
      reference_range_partitions: [
        { label: "Adult male", sex: "male", ageMinYears: 18, ageMaxYears: null, low: 13.0, high: 17.0 },
        { label: "Adult female", sex: "female", ageMinYears: 18, ageMaxYears: null, low: 12.0, high: 15.5 },
      ],
      directory: {
        performingSection: "Haematology",
        performingSite: "Central laboratory",
        method: "Automated haematology analyser (SLS photometry)",
        specimen: {
          container: "EDTA tube (lavender top)",
          specimenType: "Whole blood",
          minimumVolumeMl: 2.0,
        },
        availability: { mode: "24-7" },
        tatTargets: [
          { priority: "stat", clock: "order-to-result", targetMinutes: 60 },
        ],
      },
    },
  ],
};

const BENCHES = {
  total: 2,
  benches: [
    {
      code: "haematology",
      name: "Haematology bench",
      section: "Core laboratory",
      location: "facility-main",
      supervisor_staff_id: "staff-202",
      hours: { days: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], opensAt: "00:00", closesAt: "23:59" },
      grant_code: "lab.bench.haematology",
      caller_has_grant: false,
      grants: [],
    },
    {
      code: "chemistry",
      name: "Chemistry bench",
      section: "Core laboratory",
      location: "facility-main",
      supervisor_staff_id: "staff-201",
      hours: { days: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], opensAt: "00:00", closesAt: "23:59" },
      grant_code: "lab.bench.chemistry",
      caller_has_grant: true,
      grants: [
        { grant_id: "grant-1", staff_id: "dev-clinician", granted_by: "dev-admin", granted_at: null, note: "" },
      ],
    },
  ],
};

const WORKLOAD = {
  bench: "chemistry",
  counts_by_status: { ordered: 2, received: 1 },
  open_total: 3,
  open_items: [
    { order_id: "ord-1", status: "ordered", code: "2345-7", patient_ref: "Patient/pat-1", priority: "stat", authored_on: null },
  ],
};

const HANDOVER = {
  bench: "chemistry",
  open_items: WORKLOAD.open_items,
  open_total: 3,
  counts_by_status: { ordered: 2, received: 1 },
  notes: [
    {
      note_id: "handover-1",
      bench_code: "chemistry",
      note: "Analyser 2 QC due at 06:00",
      author: "dev-clinician",
      status: "open",
      acknowledged_by: null,
      acknowledged_at: null,
      created_at: null,
    },
  ],
  open_notes: 1,
};

function stubApi() {
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  const api: CompendiumApi = {
    request: vi.fn(async (path: string, init?: RequestInit): Promise<unknown> => {
      calls.push({ path, init });
      if (path === "/lab/compendium/directory") return DIRECTORY;
      if (path === "/lab/compendium/benches") return BENCHES;
      if (path === "/lab/compendium/benches/chemistry/workload") return WORKLOAD;
      if (path === "/lab/compendium/benches/chemistry/handover" && !init?.method)
        return HANDOVER;
      if (path === "/lab/compendium/benches/chemistry/handover" && init?.method === "POST")
        return { note_id: "handover-2", bench_code: "chemistry", note: "x", author: "dev-clinician", status: "open" };
      if (path.endsWith("/acknowledge"))
        return { note_id: "handover-1", bench_code: "chemistry", note: "x", author: "dev-clinician", status: "acknowledged" };
      throw new Error(`Unexpected path ${path}`);
    }) as unknown as CompendiumApi["request"],
  };
  return { api, calls };
}

describe("LabCompendium", () => {
  it("renders directory attributes, TAT targets and partitioned ranges", async () => {
    const { api } = stubApi();
    render(<LabCompendium api={api} />);
    expect(
      (await screen.findAllByText(/Glucose \[Mass\/volume\]/)).length,
    ).toBeGreaterThan(0);
    // Glucose is selected first: its directory attributes show.
    expect(screen.getByText("Serum separator tube")).toBeTruthy();
    expect(screen.getByText("Hexokinase (enzymatic)")).toBeTruthy();
    expect(screen.getByText("order-to-result")).toBeTruthy();
    expect(screen.getByText("receipt-to-result")).toBeTruthy();
    // Switch to haemoglobin: partitioned ranges render.
    fireEvent.click(screen.getByRole("button", { name: /Hemoglobin/ }));
    expect(await screen.findByText("Adult male")).toBeTruthy();
    expect(screen.getByText("Adult female")).toBeTruthy();
    expect(screen.getByText("13 – 17 g/dL")).toBeTruthy();
    // Bench filter narrows the list to the haematology test only.
    fireEvent.change(screen.getByLabelText("Filter by bench"), {
      target: { value: "haematology" },
    });
    expect(screen.queryByText(/Glucose \[Mass\/volume\]/)).toBeNull();
  });

  it("shows bench workload counts and the handover panel", async () => {
    const { api } = stubApi();
    render(<LabCompendium api={api} />);
    expect(await screen.findByText(/Chemistry bench \(chemistry\)/)).toBeTruthy();
    fireEvent.change(await screen.findByLabelText("Select bench"), {
      target: { value: "chemistry" },
    });
    expect(await screen.findByText("Workload — chemistry")).toBeTruthy();
    expect(screen.getByText("Open items: 3")).toBeTruthy();
    expect(screen.getByText("ordered: 2")).toBeTruthy();
    expect(screen.getByText(/Analyser 2 QC due/)).toBeTruthy();
    expect(screen.getByText(/Your bench grant \(lab\.bench\.chemistry\): held/)).toBeTruthy();
  });

  it("validates the handover note inline and submits a real one", async () => {
    const { api, calls } = stubApi();
    render(<LabCompendium api={api} />);
    fireEvent.change(await screen.findByLabelText("Select bench"), {
      target: { value: "chemistry" },
    });
    expect(await screen.findByText("Workload — chemistry")).toBeTruthy();
    // Empty note: inline error, no POST issued.
    fireEvent.click(screen.getByRole("button", { name: "Add handover note" }));
    expect(await screen.findByText(/empty note hands over nothing/)).toBeTruthy();
    expect(
      calls.filter(
        (c) => c.path.endsWith("/handover") && c.init?.method === "POST",
      ),
    ).toHaveLength(0);
    // A real note posts to the bench handover endpoint.
    fireEvent.change(screen.getByLabelText("Handover note"), {
      target: { value: "Night shift: rerun QC on analyser 1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add handover note" }));
    await waitFor(() =>
      expect(
        calls.filter(
          (c) => c.path.endsWith("/handover") && c.init?.method === "POST",
        ),
      ).toHaveLength(1),
    );
    const posted = calls.find(
      (c) => c.path.endsWith("/handover") && c.init?.method === "POST",
    );
    expect(JSON.parse(String(posted?.init?.body))).toEqual({
      note: "Night shift: rerun QC on analyser 1",
    });
  });

  it("acknowledges an open handover note", async () => {
    const { api, calls } = stubApi();
    render(<LabCompendium api={api} />);
    fireEvent.change(await screen.findByLabelText("Select bench"), {
      target: { value: "chemistry" },
    });
    expect(await screen.findByText(/Analyser 2 QC due/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() =>
      expect(
        calls.some((c) => c.path.endsWith("/handover/handover-1/acknowledge")),
      ).toBe(true),
    );
  });
});
