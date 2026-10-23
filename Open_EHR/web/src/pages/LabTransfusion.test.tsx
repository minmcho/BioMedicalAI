import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { LabTransfusion } from "./LabTransfusion";

// LAB-D13 web surface with a stub client at the network boundary
// (synthetic fixture data only — shipped code uses the real
// ApiClient).

afterEach(cleanup);

const OPT = (code: string, display: string, extra: object = {}) =>
  ({ code, display, active: true, ...extra });

const CONFIG = {
  config: { kind: "terminology", key: "transfusion-service" },
  standard_validity_hours: 72,
  extended_validity_days: 28,
  extended_lookback_days: 90,
  reservation_expiry_hours: 72,
  post_thaw_expiry_hours: { plasma: 24, cryoprecipitate: 6, platelets: 24, rbc: 24 },
  special_requirements: [OPT("irradiated", "Irradiated"),
                         OPT("antigen-negative", "Antigen-negative")],
  unit_attributes: [OPT("irradiated", "Irradiated"),
                    OPT("antigen-negative", "Antigen-negative")],
  antibody_id_methods: [OPT("antibody-panel", "Antibody identification panel")],
  prep_kinds: [OPT("thaw-plasma", "Thaw plasma",
                   { products: ["plasma"],
                     steps: ["thaw-started", "thaw-completed",
                             "visual-inspection", "label-verified"] })],
  prep_step_displays: {},
  dispositions: [OPT("wasted", "Wasted"), OPT("discarded", "Discarded")],
  wastage_reason_codes: [OPT("expired-before-use", "Expired before use")],
  severity_grades: [OPT("severe", "Severe")],
  imputability_grades: [OPT("probable", "Probable")],
  reaction_steps: [
    { code: "clerical-check", display: "Clerical check" },
    { code: "dat", display: "Direct antiglobulin test (DAT)" },
  ],
};

function stubClient() {
  let workupGraded = false;
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    void init;
    if (path === "/lab/transfusion/config") return CONFIG;
    if (path === "/lab/transfusion/ts-specimens") {
      return { items: [
        { specimen_id: "ts-1", patient_ref: "Patient/pat-1",
          order_id: null, screen_observation_id: "obs-1",
          collected_at: "2026-10-19T08:00:00Z",
          validity_kind: "standard", extended_requested: false,
          extended_granted: false, extended_denial_reason: null,
          expires_at: "2026-10-22T08:00:00Z", status: "expired" },
        { specimen_id: "ts-2", patient_ref: "Patient/pat-1",
          order_id: null, screen_observation_id: "obs-2",
          collected_at: "2026-10-22T08:00:00Z",
          validity_kind: "presurgical-extended", extended_requested: true,
          extended_granted: false,
          extended_denial_reason: "transfusion within the governed 90-day lookback window",
          expires_at: "2026-10-25T08:00:00Z", status: "valid" },
      ], total: 2, expired: 1, valid: 1 };
    }
    if (path === "/lab/transfusion/reservations") {
      return { items: [
        { reservation_id: "res-1", unit_id: "U-1",
          patient_ref: "Patient/pat-1", status: "active",
          expires_at: "2026-10-25T08:00:00Z", release_reason: null },
      ], total: 1, active: 1 };
    }
    if (path === "/lab/transfusion/preparations") {
      return { items: [
        { prep_id: "prep-1", unit_id: "U-2", prep_kind: "thaw-plasma",
          product_type: "plasma", status: "expired", next_step: null,
          expected_steps: [], steps: [],
          prepared_at: "2026-10-21T08:00:00Z",
          expires_at: "2026-10-22T08:00:00Z" },
      ], total: 1 };
    }
    if (path === "/lab/transfusion/reaction-workups") {
      return { items: [
        { workup_id: "rxn-1", unit_id: "U-3", patient_ref: "Patient/pat-1",
          status: "open", next_step: null,
          steps: [
            { code: "clerical-check", display: "Clerical check",
              done: true, result: "No clerical error", recorded_by: "tech" },
            { code: "dat", display: "Direct antiglobulin test (DAT)",
              done: true, result: "DAT negative", recorded_by: "tech" },
          ],
          severity: workupGraded ? "severe" : null,
          imputability: workupGraded ? "probable" : null,
          haemovigilance_report_id: null },
      ], total: 1 };
    }
    if (path.includes("/grading")) {
      workupGraded = true;
      return {};
    }
    if (path === "/lab/transfusion/wastage-report") {
      return { items: [], total: 2,
               by_reason: { "expired-before-use": 2 },
               by_disposition: { wasted: 2 },
               period: { from: null, to: null } };
    }
    if (path.includes("historical-antibodies")) {
      return { items: [
        { id: 1, patient_ref: "Patient/pat-1", specificity: "anti-E",
          clinically_significant: true, first_identified_on: "2026-09-01",
          source: "antibody-identification-workup", active: true },
      ], total: 1 };
    }
    if (path.includes("antibody-workups")) return { items: [], total: 0 };
    if (path.includes("special-requirements")) {
      return { items: [
        { id: 7, patient_ref: "Patient/pat-1",
          requirement_code: "irradiated", specificity: null, active: true },
      ], total: 1 };
    }
    return {};
  });
  return { client: { request } as unknown as ApiClient, request };
}

async function loadPatient() {
  await waitFor(() => screen.getByLabelText("Patient reference"));
  fireEvent.change(screen.getByLabelText("Patient reference"),
                   { target: { value: "Patient/pat-1" } });
  fireEvent.click(screen.getByRole("button",
    { name: "Load patient transfusion record" }));
  await waitFor(() => screen.getByText(/Historical antibody:/));
}

describe("LabTransfusion (LAB-D13)", () => {
  it("renders the T&S board with EXPIRED visible and the extended denial reason", async () => {
    const { client } = stubClient();
    render(<LabTransfusion client={client} />);
    await waitFor(() => screen.getByText("EXPIRED"));
    expect(screen.getByText(/extended denied: transfusion within/)).toBeTruthy();
    expect(screen.getByText(/post-thaw expiry 2026-10-22/)).toBeTruthy();
  });

  it("shows the patient historical antibody record and standing requirement", async () => {
    const { client } = stubClient();
    render(<LabTransfusion client={client} />);
    await loadPatient();
    expect(screen.getByText(/anti-E/)).toBeTruthy();
    expect(screen.getByText(/the issue gate consults this record/)).toBeTruthy();
    expect(screen.getAllByText(/irradiated/).length).toBeGreaterThan(0);
  });

  it("reservation release is disabled without a reason, enabled with one", async () => {
    const { client, request } = stubClient();
    render(<LabTransfusion client={client} />);
    await waitFor(() =>
      expect(screen.getAllByText(/res-1/).length).toBeGreaterThan(0));
    const release = screen.getByRole("button",
      { name: "Release reservation res-1" }) as HTMLButtonElement;
    expect(release.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Reservation release reason"),
                     { target: { value: "No longer needed" } });
    expect(release.disabled).toBe(false);
    fireEvent.click(release);
    await waitFor(() => {
      const call = request.mock.calls.find(
        ([path]) => String(path).includes("/reservations/res-1/release"));
      expect(call).toBeTruthy();
      expect(JSON.parse(String(call?.[1]?.body))).toEqual(
        { reason: "No longer needed" });
    });
  });

  it("reaction workup checklist shows steps and grading posts both grades", async () => {
    const { client, request } = stubClient();
    render(<LabTransfusion client={client} />);
    await waitFor(() => screen.getByText(/No clerical error/));
    fireEvent.change(screen.getByLabelText("Severity rxn-1"),
                     { target: { value: "severe" } });
    fireEvent.change(screen.getByLabelText("Imputability rxn-1"),
                     { target: { value: "probable" } });
    fireEvent.click(screen.getByRole("button", { name: "Grade workup" }));
    await waitFor(() => {
      const call = request.mock.calls.find(
        ([path]) => String(path).includes("/reaction-workups/rxn-1/grading"));
      expect(call).toBeTruthy();
      expect(JSON.parse(String(call?.[1]?.body))).toEqual(
        { severity: "severe", imputability: "probable" });
    });
  });

  it("wastage totals render by reason", async () => {
    const { client } = stubClient();
    render(<LabTransfusion client={client} />);
    await waitFor(() =>
      screen.getByText(/Wasted\/discarded\/expired total: 2/));
    expect(screen.getByText(/expired-before-use: 2/)).toBeTruthy();
  });
});
