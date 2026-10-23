import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import {
  fixtureLabCatalog,
  fixtureLabOrder,
  fixtureQcRun,
  fixtureSpecimen,
  fixtureVerificationItem,
} from "../test/fixtures";
import { LabPage } from "./LabPage";

// Fixture shapes copied from the REAL backend serializers:
// - templates: backend/app/lab_plates.py default_plate_templates_payload()
//   + _chem_96_wells() (row A QC wells verbatim; remaining wells patient),
//   served inside a StudioConfigObject {payload:{name, concepts:[...]}}
//   (GET /studio/config/terminology/effective/lab-plate-templates).
// - run/well/qc: lab_plates.py _run_out() / _well_out() / _qc().
// - 409 detail strings: lab_plates.py plate_results() gate message.
// The stub below stands in for the network boundary only (unit test);
// shipped code always uses the real ApiClient + api/plates.ts.

afterEach(cleanup);

const CHEM_ROW_A: Record<string, { role: string; expected: { low: number; high: number } }> = {
  A1: { role: "control-pos", expected: { low: 90.0, high: 110.0 } },
  A2: { role: "control-pos", expected: { low: 180.0, high: 220.0 } },
  A3: { role: "control-neg", expected: { low: 0.0, high: 5.0 } },
  A4: { role: "control-neg", expected: { low: 0.0, high: 5.0 } },
  A5: { role: "calibrator", expected: { low: 95.0, high: 105.0 } },
  A6: { role: "calibrator", expected: { low: 190.0, high: 210.0 } },
  A7: { role: "calibrator", expected: { low: 45.0, high: 55.0 } },
  A8: { role: "calibrator", expected: { low: 280.0, high: 320.0 } },
  A9: { role: "blank", expected: { low: 0.0, high: 2.0 } },
  A10: { role: "blank", expected: { low: 0.0, high: 2.0 } },
  A11: { role: "blank", expected: { low: 0.0, high: 2.0 } },
  A12: { role: "control-pos", expected: { low: 90.0, high: 110.0 } },
};

interface TemplateWellFixture {
  position: string;
  role: string;
  expected?: { low: number; high: number } | { result: string };
}

function chem96Wells(): TemplateWellFixture[] {
  const wells: TemplateWellFixture[] = [];
  for (const row of "ABCDEFGH") {
    for (let col = 1; col <= 12; col++) {
      const position = `${row}${col}`;
      if (position in CHEM_ROW_A) {
        wells.push({ position, ...CHEM_ROW_A[position] });
      } else {
        wells.push({ position, role: "patient" });
      }
    }
  }
  return wells;
}

const CONTROL_ROLES = ["control-pos", "control-neg", "calibrator", "blank"];

interface FakeWell {
  position: string;
  role: string;
  expected: { low: number; high: number } | { result: string } | null;
  specimen_id: string | null;
  order_id: string | null;
  result_value: unknown;
  result_status: string;
  assigned_by: string | null;
  assigned_at: string | null;
  resulted_by: string | null;
  resulted_at: string | null;
}

function qcOf(wells: FakeWell[]) {
  const controls = wells.filter((w) => CONTROL_ROLES.includes(w.role));
  const passed = controls.filter((w) => w.result_status === "passed").length;
  const failed = controls.filter((w) => w.result_status === "failed").length;
  const resulted = controls.filter((w) =>
    ["passed", "failed"].includes(w.result_status)).length;
  return {
    controls_total: controls.length,
    controls_passed: passed,
    controls_failed: failed,
    gate_open: resulted === controls.length && failed === 0,
  };
}

function makePlateServer() {
  let run: Record<string, unknown> & { wells: FakeWell[] } | null = null;
  const runOut = (detail: boolean) => {
    if (!run) throw new ApiError(404, "Plate run 1 not found");
    const { wells, ...rest } = run;
    return { ...rest, qc: qcOf(wells), ...(detail ? { wells } : {}) };
  };
  const request = vi.fn(async (path: string, init: RequestInit = {}) => {
    const method = (init.method ?? "GET").toUpperCase();
    const body = init.body ? JSON.parse(String(init.body)) : {};
    if (path === "/lab/plates" && method === "GET") {
      return run ? { items: [runOut(false)], total: 1 } : { items: [], total: 0 };
    }
    if (path === "/lab/plates" && method === "POST") {
      const wells: FakeWell[] = chem96Wells().map((w) => ({
        position: w.position,
        role: w.role,
        expected: w.expected ?? null,
        specimen_id: null,
        order_id: null,
        result_value: null,
        result_status: "pending",
        assigned_by: null,
        assigned_at: null,
        resulted_by: null,
        resulted_at: null,
      }));
      run = {
        id: 1, template_code: "chem-96", assay: "chemistry", format: "96-well",
        status: "open", created_by: "dev-clinician",
        created_at: "2026-01-15T09:00:00Z", completed_by: null,
        completed_at: null, wells,
      };
      return runOut(true);
    }
    if (path === "/lab/plates/1" && method === "GET") return runOut(true);
    if (path === "/lab/plates/1/assign") {
      const wells = run!.wells;
      const well = body.position
        ? wells.find((w) => w.position === body.position)
        : wells.find((w) => w.role === "patient" && w.specimen_id === null);
      if (!well) throw new ApiError(409, "Plate 1 has no empty patient well");
      well.specimen_id = "spec-1";
      well.order_id = "ord-lab-1";
      well.assigned_by = "dev-clinician";
      well.assigned_at = "2026-01-15T09:10:00Z";
      return { plate: runOut(true), well };
    }
    if (path === "/lab/plates/1/results") {
      const wells = run!.wells;
      const processed = [];
      // controls first, as the backend does
      const entries = [...body.results].sort((a: { position: string }) => {
        const w = wells.find((x) => x.position === a.position);
        return w && CONTROL_ROLES.includes(w.role) ? -1 : 1;
      });
      for (const entry of entries) {
        const well = wells.find((w) => w.position === entry.position)!;
        if (CONTROL_ROLES.includes(well.role)) {
          const exp = well.expected as { low: number; high: number };
          const ok = typeof entry.value === "number" &&
            entry.value >= exp.low && entry.value <= exp.high;
          well.result_value = entry.value;
          well.result_status = ok ? "passed" : "failed";
          processed.push({ position: well.position, role: well.role, result_status: well.result_status, value: entry.value });
        } else {
          if (!qcOf(wells).gate_open) {
            throw new ApiError(409,
              `Plate 1 QC gate is closed: patient well ${well.position} cannot be resulted until every control/calibrator/blank well has resulted and none has failed (QC gate)`);
          }
          well.result_value = entry.value;
          well.result_status = "resulted";
          processed.push({ position: well.position, role: well.role, result_status: "resulted", value: entry.value, order_id: well.order_id, report_id: "rep-lab-1" });
        }
      }
      return { plate: runOut(true), processed };
    }
    if (path === "/lab/plates/1/complete") {
      run!.status = "complete";
      run!.completed_by = "dev-clinician";
      run!.completed_at = "2026-01-15T11:00:00Z";
      return runOut(true);
    }
    throw new Error(`unexpected request ${method} ${path}`);
  });
  return { request };
}

function stubClient(server: ReturnType<typeof makePlateServer>): ApiClient {
  return {
    labCatalog: vi.fn(async () => fixtureLabCatalog),
    labOrders: vi.fn(async () => [fixtureLabOrder]),
    verificationQueue: vi.fn(async () => ({ items: [fixtureVerificationItem], total: 1 })),
    labSpecimens: vi.fn(async () => [fixtureSpecimen]),
    qcRuns: vi.fn(async () => [fixtureQcRun]),
    effectiveConfig: vi.fn(async () => ({
      id: 1, kind: "terminology", key: "lab-plate-templates", version: 1,
      status: "published",
      payload: {
        name: "OpenCare laboratory plate templates",
        concepts: [
          { code: "chem-96", display: "Chemistry 96-well plate", active: true, assay: "chemistry", format: "96-well", wells: chem96Wells() },
          { code: "micro-48", display: "Microbiology 48-well plate", active: true, assay: "microbiology", format: "48-well", wells: [{ position: "A1", role: "control-pos", expected: { result: "Positive" } }] },
        ],
      },
      created_by: "system", updated_by: "system", created_at: null, updated_at: null,
    })),
    request: server.request,
  } as unknown as ApiClient;
}

const PASSING_CONTROLS_BULK = [
  "A1,100", "A2,200", "A3,2", "A4,2", "A5,100", "A6,200",
  "A7,50", "A8,300", "A9,1", "A10,1", "A11,1", "A12,100",
].join("\n");

describe("LabPage Plates (WS3b)", () => {
  it("create run renders the well grid; gate closed blocks patient results with the reason; passing controls open the gate", async () => {
    const server = makePlateServer();
    render(<LabPage client={stubClient(server)} role="clinician" />);
    fireEvent.click(screen.getByRole("tab", { name: "Plates" }));

    // template picker is populated from the governed terminology payload
    expect(await screen.findByRole("option", { name: /Chemistry 96-well plate/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Create plate run" }));

    // run detail: format-aware grid (8 rows x 12 columns from chem-96)
    expect(await screen.findByText(/grid 8 rows × 12 columns/)).toBeTruthy();
    expect(screen.getByText("A1")).toBeTruthy();
    expect(screen.getByText("H12")).toBeTruthy();
    expect(screen.getAllByText("control-pos").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("patient").length).toBeGreaterThanOrEqual(1);

    // QC panel: totals + closed gate as text
    expect(screen.getByText(/Controls: 12 total · 0 passed · 0 failed/)).toBeTruthy();
    expect(screen.getByText("QC gate closed — controls outstanding/failed")).toBeTruthy();

    // assign a specimen -> first free patient well B1
    fireEvent.change(screen.getByLabelText("Specimen barcode or id"), { target: { value: "spec-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Assign specimen" }));
    expect(await screen.findByText("Specimen: spec-1")).toBeTruthy();

    // gate closed: the patient well's result control is blocked with the reason
    const b1Button = screen.getByRole("button", { name: "Result well B1" }) as HTMLButtonElement;
    expect(b1Button.disabled).toBe(true);
    expect(screen.getAllByText("Blocked: QC gate closed").length).toBeGreaterThanOrEqual(1);
    // ...and a bulk patient entry surfaces the server 409 naming the gate
    fireEvent.change(screen.getByLabelText("Bulk plate results"), { target: { value: "B1,95" } });
    fireEvent.click(screen.getByRole("button", { name: "Import bulk results" }));
    expect(await screen.findByText(/QC gate is closed: patient well B1 cannot be resulted/)).toBeTruthy();

    // completion is blocked while B1 is assigned but unresulted, with the reason
    const complete = screen.getByRole("button", { name: "Complete plate run" }) as HTMLButtonElement;
    expect(complete.disabled).toBe(true);
    expect(screen.getByText(/Cannot complete: assigned patient well\(s\) B1/)).toBeTruthy();

    // passing controls via bulk import -> gate opens (text)
    fireEvent.change(screen.getByLabelText("Bulk plate results"), { target: { value: PASSING_CONTROLS_BULK } });
    fireEvent.click(screen.getByRole("button", { name: "Import bulk results" }));
    expect(await screen.findByText("QC gate open — patient wells can result")).toBeTruthy();
    expect(screen.getByText(/Controls: 12 total · 12 passed · 0 failed/)).toBeTruthy();

    // per-well patient result now lands
    fireEvent.change(screen.getByLabelText("Result value for well B1"), { target: { value: "95" } });
    fireEvent.click(screen.getByRole("button", { name: "Result well B1" }));
    await waitFor(() => {
      expect(screen.getByText(/Last well result: B1: resulted/)).toBeTruthy();
    });

    // completion now succeeds
    const completeNow = screen.getByRole("button", { name: "Complete plate run" }) as HTMLButtonElement;
    expect(completeNow.disabled).toBe(false);
    fireEvent.click(completeNow);
    expect(await screen.findByText("This plate run is complete.")).toBeTruthy();
    // 30s: this journey re-renders a 96-well grid many times in jsdom.
  }, 30000);

  it("shows the template picker disabled with the reason when no templates are published", async () => {
    const server = makePlateServer();
    const client = stubClient(server);
    (client.effectiveConfig as ReturnType<typeof vi.fn>).mockRejectedValue(
      new ApiError(404, "No published lab plate templates"),
    );
    render(<LabPage client={client} role="clinician" />);
    fireEvent.click(screen.getByRole("tab", { name: "Plates" }));
    expect(await screen.findByText(/Plate templates unavailable/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Create plate run" })).toBeNull();
  });
});
