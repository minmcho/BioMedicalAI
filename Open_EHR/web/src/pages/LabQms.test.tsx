import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { LabQms } from "./LabQms";

afterEach(cleanup);

const PROFILE = {
  profile_mode: "enforce",
  profile_config: { kind: "terminology", key: "poct-deployment-profile", edit_in: "System Studio -> Terminology -> poct-deployment-profile" },
  programme_enforcement: { blockUncompetentOperator: false, blockMissingDailyQc: false },
  effective_enforcement: { block_uncompetent_operator: true, block_missing_daily_qc: true },
  enforced_at: "POCT result entry (app.lab._create_results)",
  external_leg: "EXTERNAL LEG — physical device lockout is NOT performed by this product: OpenCare enforces POCT policy at its own result-entry surface and records what each device was configured with.",
};

const DEVICE_CONFIG = {
  items: [{
    id: 1, device_key: "poc-glu-1", reagent_lot: "RL-2026-01", control_lot: "CTL-9",
    qc_ranges: { L1: { low: 40, high: 70 }, L2: { low: 200, high: 260 } },
    effective_from: "2026-10-22", effective_to: null, note: "", recorded_by: "dev-clinician", current: true,
  }],
  total: 1, external_leg: PROFILE.external_leg,
};

const QC_REVIEW = {
  devices: [{
    device_key: "poc-glu-1", qc_current: false, levels_today: { L1: "pass", L2: "fail" },
    runs_total: 3, fails_total: 1,
    current_config: DEVICE_CONFIG.items[0],
  }],
  operators: [{ operator: "dev-clinician", runs: 3, passes: 2, fails: 1, devices: ["poc-glu-1"] }],
  reviews: [{ id: 1, device_key: "poc-glu-1", staff_id: null, comment: "L2 failed on review", corrective_action: "Repeat L2 with fresh control", reviewer: "lab-manager" }],
  external_leg: PROFILE.external_leg,
};

const BOARD = {
  records: [{
    id: 1, staff_id: "staff-001", staff_name: "Maya Chen, MD", test_system: "2345-7",
    status: "current", expires_on: "2027-10-22", days_to_expiry: 365, missing_elements: [],
  }],
  total: 1, worklist: { total: 0, expired: 0, incomplete: 0 },
};

const GATING = {
  tests: [
    { code: "2345-7", display: "Glucose", gated: true, required_test_system: "2345-7", state_label: "GATED — current CLIA competency for '2345-7' required at result entry and verification" },
    { code: "2823-3", display: "Potassium", gated: false, required_test_system: null, state_label: "UNGATED — no competency mapping published for this test; entry and verification do not check competency" },
  ],
  gated_count: 1, ungated_count: 1,
  config: { kind: "terminology", key: "lab-competency-gating", edit_in: "System Studio" },
};

const EQA = {
  enrolments: [{ enrolment_id: "eqa-enrol-1", programme: "PT-2026-Q1", status: "enrolled" }],
  total: 1, live_exchange: "NOT RUN — fixture-defined format only",
  pt_events: [{
    record_id: 5, event: "PT-2026-Q1", analyte_code: "2823-3", reported_value: 9.9,
    acceptable_low: 3.5, acceptable_high: 4.5, passed: false,
    corrective_action_nc: null, closure: null, closeable: false,
  }],
};

const NCS = {
  items: [{
    nc_id: "lab-nc-abc", source_type: "qc-failure", source_ref: "qc-run-1",
    title: "QC failure on chemistry bench", severity: "major", owner: "staff-008",
    corrective_action: "", status: "assigned", safety_event_id: "sev-1",
    closure_state: "closure-pending-wave-c",
    closure_label: "INTAKE ONLY — closure rides Wave C.",
  }],
  total: 1,
  closure_label: "INTAKE ONLY — nonconformance closure (four-eyes review, effectiveness check, formal close) rides Wave C and is not available in this surface; records carry intake, assignment and status visibility only.",
};

const SOP = {
  sop_id: "sop-1", sop_code: "SOP-GLU-01", title: "Glucose testing procedure", version: 2,
  effective_date: "2025-01-01", review_due_date: "2026-01-01", status: "current",
  review_overdue: true, acknowledgements: 1,
};
const SOPS = { items: [SOP], total: 1, overdue_reviews: [SOP] };

const AUDITS = {
  items: [{
    audit_id: "lab-audit-1", title: "Q3 POCT site audit", checklist_template: "poct-site-tracer",
    checklist_items: ["Daily QC recorded"], scheduled_for: "2026-10-22", auditor: "staff-008",
    tracer_ref: "Patient/pat-1", status: "in-progress",
    findings: [{ id: 1, finding: "Daily QC missing", severity: "major", nonconformance_id: "lab-nc-abc" }],
  }],
  total: 1,
  checklist_templates: [{ code: "poct-site-tracer", display: "POCT site tracer" }],
};

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  const data: Record<string, unknown> = {
    "/lab/qms/poct/profile": PROFILE,
    "/lab/qms/poct/device-config": DEVICE_CONFIG,
    "/lab/qms/poct/qc-review": QC_REVIEW,
    "/lab/poct/devices": { items: [{ device_key: "poc-glu-1", name: "Synthetic POCT glucose meter" }], total: 1 },
    "/lab/qms/competency/board": BOARD,
    "/lab/qms/competency/gating": GATING,
    "/lab/qms/lookback": { items: [], total: 0 },
    "/lab/qms/deviations": { items: [], total: 0 },
    "/lab/qms/eqa": EQA,
    "/lab/qms/nonconformances": NCS,
    "/lab/qms/sops": SOPS,
    "/lab/qms/audits": AUDITS,
    ...overrides,
  };
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === "POST") return { ok: true };
    if (path in data) return data[path];
    throw new Error(`unstubbed path ${path}`);
  });
  return { request } as unknown as ApiClient;
}

function byText(substr: string) {
  return (_: string, el: Element | null) =>
    (el?.textContent ?? "").includes(substr) && el?.tagName === "P";
}

describe("LabQms (W9: LAB-D15 + LAB-D16)", () => {
  it("shows the deployment profile, effective enforcement and the external-leg bound", async () => {
    render(<LabQms client={stubClient()} />);
    expect(await screen.findByText(/POCT deployment profile \(LAB-D15\)/)).toBeTruthy();
    expect(screen.getByText(/Profile mode:/)).toBeTruthy();
    expect(screen.getByText(/EXTERNAL LEG — physical device lockout is NOT performed/)).toBeTruthy();
    expect(screen.getAllByText(/RL-2026-01/).length).toBeGreaterThan(0);
  });

  it("shows QC review aggregates with the corrective action", async () => {
    render(<LabQms client={stubClient()} />);
    expect(await screen.findByText(/QC NOT current today/)).toBeTruthy();
    expect(screen.getByText(/Repeat L2 with fresh control/)).toBeTruthy();
    expect(screen.getByText(byText("Operator dev-clinician: 3 QC run(s), 2 pass, 1 fail"))).toBeTruthy();
  });

  it("labels gated and ungated tests honestly on the competency board", async () => {
    render(<LabQms client={stubClient()} />);
    expect(await screen.findByText(/GATED — current CLIA competency for '2345-7' required/)).toBeTruthy();
    expect(screen.getByText(/UNGATED — no competency mapping published for this test/)).toBeTruthy();
    expect(screen.getByText(/nothing is silently gated or silently ungated/)).toBeTruthy();
    expect(screen.getByText(/Maya Chen, MD — 2345-7:/)).toBeTruthy();
  });

  it("shows the PT failure rule and refuses-by-name error surface on close", async () => {
    const client = stubClient();
    const request = client.request as unknown as ReturnType<typeof vi.fn>;
    request.mockImplementation(async (path: string, init?: RequestInit) => {
      if (init?.method === "POST" && path === "/lab/qms/pt-events/5/close") {
        throw new Error("PT failure corrective action required: PT record 5 (event 'PT-2026-Q1', analyte '2823-3') FAILED and no corrective action is on file");
      }
      if (init?.method === "POST") return { ok: true };
      const data: Record<string, unknown> = {
        "/lab/qms/poct/profile": PROFILE, "/lab/qms/poct/device-config": DEVICE_CONFIG,
        "/lab/qms/poct/qc-review": QC_REVIEW,
        "/lab/poct/devices": { items: [{ device_key: "poc-glu-1", name: "Meter" }], total: 1 },
        "/lab/qms/competency/board": BOARD, "/lab/qms/competency/gating": GATING,
        "/lab/qms/lookback": { items: [], total: 0 }, "/lab/qms/deviations": { items: [], total: 0 },
        "/lab/qms/eqa": EQA, "/lab/qms/nonconformances": NCS,
        "/lab/qms/sops": SOPS, "/lab/qms/audits": AUDITS,
      };
      if (path in data) return data[path];
      throw new Error(`unstubbed path ${path}`);
    });
    render(<LabQms client={client} />);
    expect(await screen.findByText(/a failed PT event requires an opened corrective action/)).toBeTruthy();
    expect(screen.getByText(/no corrective action on file, NOT closeable/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close PT event" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText(/PT failure corrective action required/)).toBeTruthy();
  });

  it("shows nonconformance intake with the Wave C closure label, SOP overdue reviews and audit findings", async () => {
    render(<LabQms client={stubClient()} />);
    expect(await screen.findByText(/INTAKE ONLY — nonconformance closure/)).toBeTruthy();
    expect(screen.getByText(byText("lab-nc-abc — [qc-failure] QC failure on chemistry bench"))).toBeTruthy();
    expect(screen.getByText(/closure: closure-pending-wave-c/)).toBeTruthy();
    expect(screen.getByText(/REVIEW OVERDUE/)).toBeTruthy();
    expect(screen.getByText(/raised nonconformance lab-nc-abc/)).toBeTruthy();
    await waitFor(() => expect(screen.getByText(byText("SOP-GLU-01 v2 — Glucose testing procedure"))).toBeTruthy());
  });
});
