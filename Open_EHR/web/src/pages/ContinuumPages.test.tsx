import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { navItemsFor } from "../auth/session";
import { AftercarePage } from "./AftercarePage";
import { CommandCenterPage } from "./CommandCenterPage";
import { ErOpsPage } from "./ErOpsPage";
import { SafetyPage } from "./SafetyPage";
import { UnitBoardPage } from "./UnitBoardPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// Phase 12 pages: each renders from the /continuum API surface through
// the client; these stubs pin the rendering + the write interactions.

const overviewFixture = {
  generated_at: "2026-10-18T12:00:00Z",
  census: {
    units: [{
      unit: "unit-medsurg", display: "Medical-Surgical", beds_total: 12,
      occupied: 5, available: 6, cleaning: 1, reserved: 0, occupancy_pct: 41.7,
    }],
    totals: { beds_total: 18, occupied: 5, available: 12, cleaning: 1, reserved: 0 },
    boarding: 1,
  },
  ed: { counts: { total: 3 } },
  boarding: {
    items: [{
      encounter_id: "enc-inp-1", patient_ref: "Patient/pat-1", source: "ed",
      origin_encounter_id: "enc-ed-1", requested_unit: null,
      admitted_at: "2026-10-18T11:00:00Z", wait_minutes: 42,
    }],
    total: 1, ed_total: 1, oldest_wait_minutes: 42,
  },
  boarding_count: 1,
  ed_boarding_count: 1,
  bed_requests: 1,
  expected_discharges_today: 2,
  projection: {
    occupied_now: 5, boarding: 1, accepted_transfers: 0,
    expected_discharges_today: 2, projected_occupied_end_of_day: 4,
    basis: "deterministic test basis",
  },
  discharges_today: { total: 3, before_noon: 1 },
  transport_open: 0,
  diversion: "open",
  surge_level: "normal",
};

function commandClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    commandOverview: vi.fn(async () => overviewFixture),
    transferRequests: vi.fn(async () => []),
    transportRequests: vi.fn(async () => []),
    commandAssignBed: vi.fn(async () => ({})),
    decideTransfer: vi.fn(async () => ({})),
    setDiversion: vi.fn(async () => ({})),
    ...overrides,
  } as unknown as ApiClient;
}

describe("CommandCenterPage (Phase 12)", () => {
  it("renders census, projection and the boarding queue from live data", async () => {
    render(<CommandCenterPage client={commandClient()} role="clinician" />);
    expect(await screen.findByText(/Medical-Surgical/)).toBeTruthy();
    expect(screen.getByText(/Projected occupied end of day/)).toBeTruthy();
    expect(screen.getByText(/waiting 42 min/)).toBeTruthy();
    expect(screen.getByText(/1 before noon/)).toBeTruthy();
  });

  it("assigns a bed through the command client", async () => {
    const client = commandClient();
    render(<CommandCenterPage client={client} role="clinician" />);
    const input = await screen.findByLabelText(/Bed for enc-inp-1/);
    fireEvent.change(input, { target: { value: "MS-101A" } });
    fireEvent.click(screen.getByRole("button", { name: "Assign bed" }));
    await waitFor(() => {
      expect(client.commandAssignBed).toHaveBeenCalledWith("enc-inp-1", "MS-101A");
    });
  });

  it("does not crash when the overview payload has no boarding object", async () => {
    // UX pass contract pin: the real payload carries boarding as an
    // object (see overviewFixture, which mirrors continuum.py), but
    // the page must degrade to the flat boarding_count — never throw.
    const { boarding: _boarding, ...withoutBoarding } = overviewFixture;
    const client = commandClient({
      commandOverview: vi.fn(async () => withoutBoarding),
    });
    render(<CommandCenterPage client={client} role="clinician" />);
    expect(await screen.findByText(/Medical-Surgical/)).toBeTruthy();
    expect(screen.getByText(/Bed requests \(1\)/)).toBeTruthy();
  });
});

const erBoardFixture = {
  generated_at: "2026-10-18T12:00:00Z",
  groups: {},
  entries: [],
  queues: {
    fast_track: [{
      encounter: { resourceType: "Encounter", id: "enc-ed-9", subject: { reference: "Patient/pat-9" } },
      patient_ref: "Patient/pat-9", arrived_at: "2026-10-18T11:30:00Z",
      wait_minutes: 25, acuity: 5, chief_complaint: "Sprained ankle",
      state: "triage", target_minutes: 120, target_breached: false,
      queue: "fast-track",
    }],
    main: [],
  },
  counts: {
    waiting: 0, triage: 1, "in-treatment": 0, disposition: 0,
    total: 1, fast_track: 1, target_breaches: 0,
  },
  diversion: "open",
  surge_level: "normal",
};

function erClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    continuumErBoard: vi.fn(async () => erBoardFixture),
    // The page reads its activation pathways from the published
    // terminology/continuum-ops config (the same payload the
    // server validates against) — the stub serves it.
    effectiveConfig: vi.fn(async () => ({
      payload: {
        activationPathways: [
          { code: "stemi", display: "STEMI activation" },
          { code: "stroke", display: "Stroke activation" },
          { code: "trauma", display: "Trauma activation" },
        ],
      },
    })),
    listActivations: vi.fn(async () => []),
    callbackQueue: vi.fn(async () => ({ items: [], completed: [], total: 0, breached: 0 })),
    startActivation: vi.fn(async () => ({})),
    markLwbs: vi.fn(async () => ({})),
    ...overrides,
  } as unknown as ApiClient;
}

describe("ErOpsPage (Phase 12)", () => {
  it("renders the fast-track queue with ESI targets", async () => {
    render(<ErOpsPage client={erClient()} role="clinician" />);
    expect(await screen.findByText(/Sprained ankle/)).toBeTruthy();
    expect(screen.getByText(/target 120 min/)).toBeTruthy();
    expect(screen.getByText(/Fast track \(1\)/)).toBeTruthy();
  });

  it("starts an activation for the selected encounter", async () => {
    const client = erClient();
    render(<ErOpsPage client={client} role="clinician" />);
    fireEvent.click(await screen.findByRole("button", { name: "Start activation" }));
    await waitFor(() => {
      expect(client.startActivation).toHaveBeenCalledWith("enc-ed-9", "stemi");
    });
  });
});

function safetyClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    safetyEvents: vi.fn(async () => [{
      id: "sev-1", reporter: "confidential", patient_ref: null,
      unit: "unit-icu", category: "fall", harm_level: "permanent",
      description: "Patient fall with injury", confidential: true,
      status: "reported", rca_required: true, rca_status: "open",
      occurred_at: "2026-10-18T10:00:00Z", created_at: "2026-10-18T10:05:00Z",
    }]),
    safetyRcaQueue: vi.fn(async () => []),
    safetySummary: vi.fn(async () => ({
      total: 1, open: 1, rca_open: 1, by_status: {}, by_harm: {},
      by_category: {}, by_unit: {},
    })),
    safetyEvent: vi.fn(async () => null),
    fileSafetyEvent: vi.fn(async () => ({})),
    triageSafetyEvent: vi.fn(async () => ({})),
    ...overrides,
  } as unknown as ApiClient;
}

describe("SafetyPage (Phase 12)", () => {
  it("shows the shielded reporter and the RCA flag in the triage queue", async () => {
    render(<SafetyPage client={safetyClient()} role="clinician" />);
    expect(await screen.findByText(/Patient fall with injury/)).toBeTruthy();
    expect(screen.getByText(/reporter: confidential/)).toBeTruthy();
    expect(screen.getAllByText(/RCA open/).length).toBeGreaterThan(0);
  });

  it("files an event through the report form", async () => {
    const client = safetyClient();
    render(<SafetyPage client={client} role="viewer" />);
    fireEvent.change(screen.getByLabelText("Event description"), {
      target: { value: "Wrong-dose near miss" },
    });
    fireEvent.click(screen.getByRole("button", { name: "File event" }));
    await waitFor(() => {
      expect(client.fileSafetyEvent).toHaveBeenCalledWith(
        expect.objectContaining({ description: "Wrong-dose near miss", confidential: true }),
      );
    });
  });
});

// Depth pass (Workstream 2): UnitBoardPage rounds checklist + the
// readiness med-rec chip, and the AftercarePage med-rec form. The
// stubs pin the new client surface (inpatientRounds /
// submitRoundsChecklist / submitDischargeMedRec).

function unitClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    nurseAssignments: vi.fn(async () => ({
      items: [], nurses: [], unassigned: [], total: 0,
    })),
    dischargeReadiness: vi.fn(async () => ({
      items: [{
        encounter_id: "enc-1", patient_ref: "Patient/pat-1",
        unit: "unit-medsurg", bed: "MS-101A", target_date: null,
        disposition: null, barriers: null, checklist_done: false,
        teachback_done: false, followup_booked: false,
        medrec_done: false, missing: ["teach-back"], ready: false,
      }],
      total: 1, ready: 0,
    })),
    evsTasks: vi.fn(async () => []),
    inpatientRounds: vi.fn(async () => ({
      unit: "unit-medsurg",
      items: [{
        encounter_id: "enc-1", patient_ref: "Patient/pat-1",
        bed: "MS-101A", nurse: "nurse-jane", acuity: 3,
        target_date: null, disposition: null, barriers: [],
        rounds_checklist_today: false,
      }],
      total: 1,
    })),
    submitRoundsChecklist: vi.fn(async () => ({})),
    recordHandoff: vi.fn(async () => ({})),
    ...overrides,
  } as unknown as ApiClient;
}

describe("UnitBoardPage (depth pass: rounds + med-rec)", () => {
  // UX pass (spec P-7): rounds and readiness are separate tabs now;
  // the chip tests open the owning tab first (assertions unchanged).
  it("shows Rounds today and Med-rec labelled chips per patient", async () => {
    render(<UnitBoardPage client={unitClient()} role="clinician" />);
    fireEvent.click(await screen.findByRole("tab", { name: "Rounds" }));
    expect(await screen.findByText(/Rounds today: Pending/)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Discharge readiness" }));
    expect(await screen.findByText(/Med-rec: Pending/)).toBeTruthy();
  });

  it("submits the rounds checklist for the patient", async () => {
    const client = unitClient();
    render(<UnitBoardPage client={client} role="clinician" />);
    fireEvent.click(await screen.findByRole("tab", { name: "Rounds" }));
    await screen.findByText(/Rounds today: Pending/);
    fireEvent.click(screen.getByRole("button", { name: "Rounds checklist" }));
    fireEvent.change(screen.getByLabelText(/Overnight events for enc-1/), {
      target: { value: "Quiet night" },
    });
    fireEvent.click(screen.getByLabelText(/Lines and devices reviewed for enc-1/));
    fireEvent.click(screen.getByRole("button", { name: "Submit rounds checklist" }));
    await waitFor(() => {
      expect(client.submitRoundsChecklist).toHaveBeenCalledWith(
        "enc-1",
        expect.arrayContaining([
          expect.objectContaining({ linkId: "overnight-events" }),
          expect.objectContaining({ linkId: "lines-devices-reviewed" }),
        ]),
      );
    });
  });
});

function aftercareClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    aftercareCalls: vi.fn(async () => []),
    readmissionRisks: vi.fn(async () => ({ items: [], basis: "deterministic test basis" })),
    hospitalAtHome: vi.fn(async () => ({
      total: 0, census: [], eligible: [], note: "workflow only",
    })),
    recordAftercareCall: vi.fn(async () => ({})),
    submitDischargeMedRec: vi.fn(async () => ({})),
    ...overrides,
  } as unknown as ApiClient;
}

describe("AftercarePage (depth pass: medication reconciliation)", () => {
  it("records a medication reconciliation through the client", async () => {
    const client = aftercareClient();
    render(<AftercarePage client={client} role="clinician" />);
    // UX pass (spec P-7): med-rec is its own tab now.
    fireEvent.click(await screen.findByRole("tab", { name: "Medication reconciliation" }));
    expect(await screen.findByText(/Discharge medication reconciliation/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Med-rec encounter id"), {
      target: { value: "enc-9" },
    });
    fireEvent.change(screen.getByLabelText("Medication changes"), {
      target: { value: "Metformin held" },
    });
    fireEvent.click(screen.getByLabelText("Home medications reviewed"));
    fireEvent.click(screen.getByLabelText("Allergies rechecked"));
    fireEvent.click(screen.getByRole("button", {
      name: "Record medication reconciliation",
    }));
    await waitFor(() => {
      expect(client.submitDischargeMedRec).toHaveBeenCalledWith(
        "enc-9",
        expect.arrayContaining([
          expect.objectContaining({ linkId: "medication-changes" }),
          expect.objectContaining({ linkId: "home-medications-reviewed" }),
        ]),
      );
    });
  });
});

describe("Phase 12 navigation", () => {
  it("staff see the six continuum pages; patients see none", () => {
    const clinicianKeys = navItemsFor("clinician").map((i) => i.key);
    for (const key of ["command", "er-ops", "unit-board", "clinic", "aftercare", "safety"]) {
      expect(clinicianKeys).toContain(key);
    }
    const viewerKeys = navItemsFor("viewer").map((i) => i.key);
    expect(viewerKeys).toContain("command");
    expect(viewerKeys).not.toContain("safety"); // triage is safety.review
    const patientKeys = navItemsFor("patient").map((i) => i.key);
    expect(patientKeys).not.toContain("command");
    expect(patientKeys).not.toContain("safety");
  });
});
