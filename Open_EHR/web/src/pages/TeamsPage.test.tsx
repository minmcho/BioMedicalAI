import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { TeamsPage } from "./TeamsPage";

// Teams & Panels pass: the Teams page is rendered from the /teams
// surface through a stub client — standalone client modules are
// stubbed through client.request, keyed by path (the InboxPage test
// pattern). No network, no live backend.
afterEach(cleanup);

const CONFIG = {
  teams: {
    teamTypes: [
      { code: "inpatient", display: "Inpatient" },
      { code: "primary-care", display: "Primary care" },
    ],
    teamRoles: [
      { code: "lead", display: "Lead" },
      { code: "member", display: "Member" },
    ],
    panelThresholds: { maxWeighted: 100, minWeighted: 10 },
    panelWeights: {},
  },
  privileges: {},
  jobCatalogue: { roles: [] },
};

const TEAMS = {
  items: [
    {
      team_id: "team-1", name: "Med-Surg Team", team_type: "inpatient",
      unit: "unit-medsurg", service_line: "medicine", status: "active",
      member_count: 1, active_patient_count: 2,
      placement: {
        region: { code: "region-main", display: "OpenCare Region" },
        location: { code: "facility-main", display: "OpenCare Hospital" },
        department: { code: "dept-medicine", display: "Medicine" },
        unit: { code: "unit-medsurg", display: "Medical-Surgical Unit" },
      },
    },
    {
      team_id: "team-2", name: "Primary Care Team", team_type: "primary-care",
      unit: null, service_line: "medicine", status: "active",
      member_count: 1, active_patient_count: 120,
    },
  ],
  total: 2,
};

const PANEL = {
  team: TEAMS.items[0],
  members: [{ staff_id: "staff-001", staff_name: "Maya Chen, MD", role_in_team: "lead" }],
  patients: [
    {
      assignment_id: "asg-1", patient_ref: "Patient/pat-1",
      patient_name: "Alice Example", role: "primary", state: "active",
      encounter_id: "enc-1", bed: "MS-12", attending_name: "Maya Chen, MD",
      primary_nurse: "Tom Becker, RN", via_coverage: false,
    },
    {
      assignment_id: "asg-2", patient_ref: "Patient/pat-2",
      patient_name: "Bob Example", role: "consulting", state: "active",
      encounter_id: "enc-2", bed: "MS-13", attending_name: "Maya Chen, MD",
      primary_nurse: "Tom Becker, RN", via_coverage: true,
    },
  ],
  pending_inbound: [
    {
      assignment_id: "asg-pending-1", patient_ref: "Patient/pat-9",
      patient_name: "Carol Pending", role: "primary",
      handoff: "Stable, needs a bed", requested_by: "dr-x",
      created_at: "2026-10-19T08:00:00Z",
    },
  ],
  unassigned: [
    {
      encounter_id: "enc-9", patient_ref: "Patient/pat-10",
      patient_name: "Dan Unassigned", bed: "MS-14", unit: "unit-medsurg",
    },
  ],
  coverage_in: [],
  coverage_out: [
    {
      coverage_id: "cov-1", covered_staff_id: "staff-001",
      covered_team_id: "team-1", covering_staff_id: "staff-002",
      covering_team_id: "team-1", starts_at: "2026-10-20T07:00:00Z",
      ends_at: "2026-10-21T07:00:00Z", reason: "PTO", status: "active",
    },
  ],
  todays_assignments: [],
};

const PANELS = {
  pcps: [
    {
      pcp_staff_id: "staff-001", pcp_name: "Maya Chen, MD",
      team_id: "team-2", panel_size: 120, weighted_size: 150, capacity: 100,
    },
  ],
  teams: [],
  coverage: { patients_total: 200, empanelled: 150, unempanelled: 50, coverage_pct: 75 },
  unempanelled: ["Patient/pat-77"],
};

const STAFF = [
  { staff_id: "staff-001", full_name: "Maya Chen, MD", job_role: "physician", unit: "unit-medsurg", service_line: "medicine", status: "active" },
  { staff_id: "staff-002", full_name: "Tom Becker, RN", job_role: "nurse", unit: "unit-medsurg", service_line: "medicine", status: "active" },
];

function makeRequest() {
  return vi.fn(async (path: string, init?: RequestInit) => {
    if (path === "/teams/config") return CONFIG;
    if (path === "/teams" && (!init || init.method === undefined)) return TEAMS;
    if (path === "/teams/team-1/panel") return PANEL;
    if (path === "/teams/team-1") {
      return {
        ...TEAMS.items[0],
        members: [
          { membership_id: "mem-1", staff_id: "staff-001", staff_name: "Maya Chen, MD", role_in_team: "lead", status: "active" },
        ],
      };
    }
    if (path === "/teams/panels") return PANELS;
    if (path === "/teams/my-teams") {
      return { staff_id: null, teams: [], panel: null, coverage_giving: [], coverage_receiving: [] };
    }
    if (path === "/teams/unassigned" || path.startsWith("/teams/unassigned?")) {
      return { items: PANEL.unassigned, total: 1 };
    }
    if (path === "/teams/assignments?state=requested") return { items: [], total: 0 };
    if (path === "/teams/assignments/asg-pending-1/accept") {
      expect(init?.method).toBe("POST");
      return { assignment_id: "asg-pending-1", state: "active" };
    }
    if (path === "/people/staff?job_role=physician") return { items: [STAFF[0]] };
    if (path === "/people/staff") return { items: STAFF };
    if (path === "/people/config") {
      return { units: [{ code: "unit-medsurg", display: "Medical-Surgical Unit" }] };
    }
    throw new Error(`Unexpected request: ${path}`);
  });
}

function stubClient(): ApiClient {
  return { request: makeRequest() } as unknown as ApiClient;
}

describe("TeamsPage — team panel tab", () => {
  it("renders the selected team's patients with Primary/Consulting badges and the coverage flag", async () => {
    render(<TeamsPage client={stubClient()} role="clinician" />);
    expect(await screen.findByText("Alice Example")).toBeTruthy();
    expect(screen.getByText("Bob Example")).toBeTruthy();
    expect(screen.getAllByText("Primary").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Consulting").length).toBeGreaterThan(0);
    // Bob is covered via coverage; Alice is on her own team. ("Coverage"
    // also appears as the table header and section heading.)
    expect(screen.getAllByText("Coverage").length).toBeGreaterThan(1);
    expect(screen.getByText("Own team")).toBeTruthy();
    expect(screen.getByText("MS-12")).toBeTruthy();
    // The unassigned queue for the team's unit is visible too.
    expect(screen.getByText("Dan Unassigned")).toBeTruthy();
    expect(screen.getByText("Assign to this team")).toBeTruthy();
  });

  it("shows the team's placement line from its placement object", async () => {
    render(<TeamsPage client={stubClient()} role="clinician" />);
    expect(await screen.findByText("Alice Example")).toBeTruthy();
    expect(
      screen.getByText(
        "Placement: Medical-Surgical Unit · Medicine · OpenCare Hospital · OpenCare Region",
      ),
    ).toBeTruthy();
  });

  it("pending inbound Accept fires POST /teams/assignments/{id}/accept", async () => {
    const client = stubClient();
    render(<TeamsPage client={client} role="clinician" />);
    expect(await screen.findByText("Carol Pending")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() =>
      expect(client.request).toHaveBeenCalledWith(
        "/teams/assignments/asg-pending-1/accept",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    expect(await screen.findByText("Assignment accepted.")).toBeTruthy();
  });
});

describe("TeamsPage — panels tab", () => {
  it("shows coverage numbers and the PCP capacity badge", async () => {
    render(<TeamsPage client={stubClient()} role="clinician" />);
    fireEvent.click(await screen.findByRole("tab", { name: "Panels" }));
    expect(await screen.findByText("Over capacity")).toBeTruthy();
    expect(screen.getByText(/150 of 200 patients empanelled \(75%\)/)).toBeTruthy();
    expect(screen.getByText("Patient/pat-77")).toBeTruthy();
    // PCP row: name, weighted size over the numeric capacity (150 > 100).
    // ("150" also appears as the empanelled big number.)
    expect(screen.getByText("Maya Chen, MD")).toBeTruthy();
    expect(screen.getAllByText("150").length).toBeGreaterThan(0);
  });
});

describe("TeamsPage — my teams tab", () => {
  it("shows the honest empty state when the caller has no staff link", async () => {
    render(<TeamsPage client={stubClient()} role="viewer" />);
    fireEvent.click(await screen.findByRole("tab", { name: "My teams" }));
    expect(
      await screen.findByText(/not linked to a staff record/),
    ).toBeTruthy();
  });
});
