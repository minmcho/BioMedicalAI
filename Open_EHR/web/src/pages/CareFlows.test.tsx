import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { CommandCenterPage } from "./CommandCenterPage";
import { EConsultsPanel, EpisodesPanel } from "./CareFlows";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// Phase 15 care-flow surfaces: the standalone flows client functions go
// through ApiClient.request, so the stub routes by path + method and
// pins the exact backend shapes from app.support_ops (/support),
// app.care_coordination (/coord) and app.infection_prevention.

const desksFixture = [
  { code: "bed-management", display: "Bed management desk", roles: ["admin", "clinician"], huddleCadence: "every-4-hours" },
  { code: "access-transfer", display: "Access & transfer desk", roles: ["admin"], huddleCadence: "twice-daily" },
];

const supportConfigFixture = {
  "command-desks": {
    desks: desksFixture,
    triageAuthorityRole: "physician",
    triageAuthorityDisplay: "ICU triage authority (HTRIP-equivalent)",
    triageDecisions: ["admit-icu", "defer", "decline"],
  },
  "access-line": {
    referralSources: [{ code: "referring-hospital", display: "Referring hospital" }],
    sources: ["referring-hospital"],
    dispositions: ["advice", "transfer-requested", "clinic-referral", "other"],
  },
};

const staffingFixture = [{
  id: "cmds-1", staffing_id: "cmds-1", desk: "bed-management",
  staff_id: "staff-1", staff_username: "maya", staff_name: "Maya Chen",
  starts_at: "2026-10-18T07:00:00Z", ends_at: "2026-10-18T19:00:00Z",
}];

const accessCallFixture = {
  id: "acc-1", call_id: "acc-1", caller_name: "Dr Referrer",
  caller_org: "County General", referral_source: "referring-hospital",
  patient_ref: "Patient/pat-7", patient_name: "Pat Seven",
  patient_summary: "Needs an ICU bed", disposition: "transfer-requested",
  transfer_request_id: "tr-existing-1",
  transfer_linkage: { note: "Queued intake record referencing the Phase 12 TransferRequest" },
};

const openEConsult = {
  id: "ec-1", econsult_id: "ec-1", patient_ref: "Patient/pat-1",
  requester: "dr-a", specialty: "cardiology", question: "Chest pain workup?",
  chart_refs: [], state: "open", sla_hours: 24, sla_breached: false,
  answer_text: null, needs_visit: null, referral_ref: null,
};

const answeredEConsult = {
  ...openEConsult, id: "ec-2", econsult_id: "ec-2", state: "answered",
  answer_text: "Start metoprolol and review in clinic",
  answered_by: "dr-spec", needs_visit: true,
  referral_id: "sr-1", referral_ref: "ServiceRequest/sr-1",
};

const episodeFixture = {
  id: "ep-1", episode_id: "ep-1", patient_ref: "Patient/pat-1",
  coordinator_username: "maya", coordinator_staff_id: "staff-1",
  title: "Complex cardiac case", condition: "Heart failure",
  status: "active", links: [],
};

const assemblyFixture = {
  episode: episodeFixture,
  coordinator: { username: "maya", staff_id: "staff-1", name: "Maya Chen", role_label: "Coordinating physician" },
  contributions: [],
  contributions_by_specialty: {
    cardiology: [{
      id: "c-1", contribution_id: "c-1", episode_id: "ep-1",
      specialty: "cardiology", author: "dr-card",
      summary: "Start beta blocker and titrate", linked_refs: [],
    }],
    endocrinology: [{
      id: "c-2", contribution_id: "c-2", episode_id: "ep-1",
      specialty: "endocrinology", author: "dr-endo",
      summary: "Tighten glycaemic control", linked_refs: [],
    }],
  },
  links: [{ ref: "Encounter/enc-1", reference: "Encounter/enc-1", type: "encounter" }],
};

const overviewFixture = {
  generated_at: "2026-10-18T12:00:00Z",
  census: { units: [], totals: {}, boarding: 0 },
  boarding: { items: [], total: 0 },
  boarding_count: 0, ed_boarding_count: 0,
  projection: { projected_occupied_end_of_day: 0, basis: "test basis" },
  discharges_today: { total: 0, before_noon: 0 },
  diversion: "open", surge_level: "normal",
};

function flowClient(econsults: unknown[] = [openEConsult]) {
  const request = vi.fn(async (path: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    if (path === "/support/config") return supportConfigFixture;
    if (path === "/support/command/desks") {
      return { items: desksFixture, total: desksFixture.length, triage_authority_role: "physician" };
    }
    if (path === "/support/command/staffing") return { items: staffingFixture, total: 1 };
    if (path === "/support/command/huddles") return { items: [], total: 0 };
    if (path === "/support/command/triage-decisions" && method === "GET") {
      return { items: [], total: 0 };
    }
    if (path === "/support/command/triage-decisions") {
      return { id: "cmdt-1", decision_id: "cmdt-1", authority_role: "admin" };
    }
    if (path === "/support/access-line/calls" && method === "GET") {
      return { items: [accessCallFixture], total: 1 };
    }
    if (path === "/support/access-line/calls") {
      return { ...accessCallFixture, id: "acc-2", call_id: "acc-2", transfer_request_id: "tr-999" };
    }
    if (path === "/coord/econsults" && method === "GET") {
      return { items: econsults, total: econsults.length };
    }
    if (path === "/coord/econsults/scan") {
      return { created: [], created_count: 0, evaluated_patients: 0, triggers: [] };
    }
    if (path.endsWith("/answer")) {
      return { ...openEConsult, state: "answered", needs_visit: true, referral_ref: "ServiceRequest/sr-1" };
    }
    if (path.endsWith("/close")) return { ...answeredEConsult, state: "closed" };
    if (path === "/coord/episodes") return { items: [episodeFixture], total: 1 };
    if (path.endsWith("/assembly")) return assemblyFixture;
    throw new Error(`unexpected request ${method} ${path}`);
  });
  const client = {
    request,
    commandOverview: vi.fn(async () => overviewFixture),
    transferRequests: vi.fn(async () => []),
    transportRequests: vi.fn(async () => []),
  } as unknown as ApiClient;
  return { client, request };
}

describe("Command desks (BD-13)", () => {
  it("renders desks + roster and disables triage for a non-authority role with an explanation", async () => {
    const { client } = flowClient();
    render(<CommandCenterPage client={client} role="clinician" section="desks" />);
    expect(await screen.findByText("Maya Chen")).toBeTruthy();
    expect(screen.getAllByText(/Bed management desk/).length).toBeGreaterThan(0);
    expect(screen.getByText(/not the triage authority/)).toBeTruthy();
    const button = screen.getByRole("button", { name: "Record triage decision" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it("lets an admin record a triage decision through /support", async () => {
    const { client, request } = flowClient();
    render(<CommandCenterPage client={client} role="admin" section="desks" />);
    await screen.findByText("Maya Chen");
    const button = screen.getByRole("button", { name: "Record triage decision" });
    expect((button as HTMLButtonElement).disabled).toBe(false);
    fireEvent.change(screen.getByLabelText("Triage patient"), {
      target: { value: "Patient/pat-9" },
    });
    fireEvent.click(button);
    await waitFor(() => {
      const call = request.mock.calls.find(
        ([p, i]) => p === "/support/command/triage-decisions" && i?.method === "POST",
      );
      expect(call).toBeTruthy();
      expect(JSON.parse(String(call?.[1]?.body ?? "{}"))).toEqual(
        expect.objectContaining({ patient_ref: "Patient/pat-9", decision: "admit-icu" }),
      );
    });
  });
});

describe("Access line (BD-14)", () => {
  it("shows the created Phase 12 TransferRequest id for transfer-requested calls", async () => {
    const { client } = flowClient();
    render(<CommandCenterPage client={client} role="clinician" section="access-line" />);
    // The seeded log row already carries its TransferRequest id.
    expect(await screen.findByText(/tr-existing-1/)).toBeTruthy();
    // Log a new transfer-requested call: the created id is surfaced.
    fireEvent.change(screen.getByLabelText("Caller name"), { target: { value: "Dr New" } });
    fireEvent.change(screen.getByLabelText("Call summary"), {
      target: { value: "STEMI transfer please" },
    });
    fireEvent.change(screen.getByLabelText("Call disposition"), {
      target: { value: "transfer-requested" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Log call" }));
    expect(await screen.findByText(/tr-999/)).toBeTruthy();
    expect(screen.getAllByText(/Phase 12 TransferRequest/).length).toBeGreaterThan(0);
  });
});

describe("eConsults (BD-27)", () => {
  it("answers an open eConsult in chart with the needs-visit flag", async () => {
    const { client, request } = flowClient();
    render(<EConsultsPanel client={client} role="clinician" />);
    expect(await screen.findByText("Chest pain workup?")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Advice for ec-1"), {
      target: { value: "Troponin trend is reassuring" },
    });
    fireEvent.click(screen.getByLabelText("Needs visit for ec-1"));
    fireEvent.click(screen.getByRole("button", { name: "Answer eConsult" }));
    await waitFor(() => {
      const call = request.mock.calls.find(([p]) => p === "/coord/econsults/ec-1/answer");
      expect(call).toBeTruthy();
      expect(JSON.parse(String(call?.[1]?.body ?? "{}"))).toEqual(
        expect.objectContaining({ advice: "Troponin trend is reassuring", needs_visit: true }),
      );
    });
    expect(await screen.findByText(/eConsult answered — done/)).toBeTruthy();
  });

  it("shows the needs-visit referral on an answered eConsult and closes it", async () => {
    const { client, request } = flowClient([answeredEConsult]);
    render(<EConsultsPanel client={client} role="clinician" />);
    expect(await screen.findByText(/Needs visit — referral ServiceRequest\/sr-1/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close eConsult" }));
    await waitFor(() => {
      expect(
        request.mock.calls.some(([p]) => p === "/coord/econsults/ec-2/close"),
      ).toBe(true);
    });
  });
});

describe("Episodes (BD-25)", () => {
  it("renders the assembly view with contributions grouped by specialty", async () => {
    const { client } = flowClient();
    render(<EpisodesPanel client={client} role="clinician" />);
    expect(await screen.findByText("Complex cardiac case")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "View assembly" }));
    expect(await screen.findByText(/Assembly — Complex cardiac case/)).toBeTruthy();
    expect(screen.getByText("Start beta blocker and titrate")).toBeTruthy();
    expect(screen.getByText("Tighten glycaemic control")).toBeTruthy();
    expect(screen.getByText(/Maya Chen/)).toBeTruthy();
    expect(screen.getByText(/Encounter\/enc-1/)).toBeTruthy();
  });
});
