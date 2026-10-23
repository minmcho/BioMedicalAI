import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { OnboardingPanel } from "./OnboardingPanel";

// Onboarding (Workforce, Teams & Panels pass): rendered standalone
// from the /onboarding surface through a stub client.request, keyed
// by path (the InboxPage test pattern). The Workforce page mounts
// this same panel as its Onboarding tab.
afterEach(cleanup);

const CONFIG = {
  teams: {
    teamTypes: [{ code: "inpatient", display: "Inpatient" }],
    teamRoles: [{ code: "member", display: "Member" }],
    panelThresholds: { maxWeighted: 100 },
    panelWeights: {},
  },
  privileges: {},
  jobCatalogue: {
    roles: [
      {
        jobRole: "nurse", display: "Nurse", clinical: true,
        requiredCredentials: ["rn-licence", "bls"],
        defaultAccountRole: "clinician", privilegeTemplate: "nurse-core",
      },
    ],
  },
};

const STAGES = ["intake", "credentialing", "provisioning", "orientation", "complete"];

// Organisation hierarchy (Placement pass): GET /org-hierarchy tree.
const ORG_TREE = {
  version: 5,
  regions: [
    {
      code: "region-main", display: "OpenCare Region",
      locations: [
        {
          code: "facility-main", display: "OpenCare Hospital",
          departments: [
            {
              code: "dept-medicine", display: "Medicine",
              units: [
                { code: "unit-medsurg", display: "Medical-Surgical Unit", bed_count: 12 },
                { code: "unit-stepdown", display: "Step-down Unit", bed_count: 4 },
              ],
            },
            {
              code: "dept-critical-care", display: "Critical Care",
              units: [{ code: "unit-icu", display: "Intensive Care Unit", bed_count: 6 }],
            },
          ],
        },
      ],
    },
  ],
};

let lastCreateBody: Record<string, unknown> | null = null;

function detailFixture(blockers: string[], stage = "credentialing", safetyDone = false) {
  return {
    record: { onboarding_id: "ob-1", staff_id: "staff-009", employee_id: "E-9", stage },
    staff: {
      staff_id: "staff-009", full_name: "New Nurse, RN", job_role: "nurse",
      unit: "unit-medsurg", service_line: "medicine", status: "onboarding",
    },
    credentials: [
      { type: "rn-licence", number: "RN-1009", expires_on: "2027-01-01", status: "active" },
    ],
    privileges: [{ privilege_code: "med-admin", service_line: "medicine" }],
    account: null,
    orientation: {
      form_key: "orientation",
      items: [
        { linkId: "safety", text: "Safety training", required: true, completed: safetyDone },
        { linkId: "hipaa", text: "HIPAA training", required: true, completed: true },
      ],
    },
    blockers,
    stages: STAGES,
  };
}

function makeRequest(blockers: string[]) {
  let advanced = false;
  let safetyDone = false;
  return vi.fn(async (path: string, init?: RequestInit) => {
    if (path === "/teams/config") return CONFIG;
    if (path === "/org-hierarchy") return ORG_TREE;
    if (path === "/onboarding" && init?.method === "POST") {
      lastCreateBody = JSON.parse(String(init.body ?? "{}")) as Record<string, unknown>;
      return detailFixture(blockers);
    }
    if (path === "/onboarding") {
      return {
        items: [
          {
            record: { onboarding_id: "ob-1", staff_id: "staff-009", stage: advanced ? "provisioning" : "credentialing" },
            staff_name: "New Nurse, RN", job_role: "nurse",
            blockers_count: blockers.length,
          },
        ],
        total: 1,
      };
    }
    if (path === "/onboarding/ob-1/advance") {
      expect(init?.method).toBe("POST");
      advanced = true;
      return { ...detailFixture([], "provisioning", safetyDone), dev_token: "dev-token-once-123" };
    }
    if (path === "/onboarding/ob-1/orientation/safety/complete") {
      expect(init?.method).toBe("POST");
      safetyDone = true;
      return {};
    }
    if (path === "/onboarding/ob-1") {
      return detailFixture(advanced ? [] : blockers, advanced ? "provisioning" : "credentialing", safetyDone);
    }
    throw new Error(`Unexpected request: ${path}`);
  });
}

function stubClient(blockers: string[]): ApiClient {
  return { request: makeRequest(blockers) } as unknown as ApiClient;
}

describe("OnboardingPanel", () => {
  it("renders the stage stepper, blockers and a disabled Advance with its reason", async () => {
    render(<OnboardingPanel client={stubClient(["BLS certificate missing"])} role="admin" />);
    expect(await screen.findByText("New Nurse, RN")).toBeTruthy();
    // Detail loads async: blockers + stepper arrive with it.
    expect(await screen.findByText("BLS certificate missing")).toBeTruthy();
    // Stepper: exactly one step is marked Current (credentialing).
    expect(screen.getByText("(Current)")).toBeTruthy();
    expect(screen.getByText("(Done)")).toBeTruthy();
    const advance = screen.getByRole("button", { name: "Advance stage" }) as HTMLButtonElement;
    expect(advance.disabled).toBe(true);
    expect(screen.getByText(/Cannot advance: 1 blocker\(s\) remain/)).toBeTruthy();
    // Credentials, privileges and orientation render from the detail.
    expect(screen.getByText("RN-1009")).toBeTruthy();
    expect(screen.getByText("med-admin · medicine")).toBeTruthy();
    expect(screen.getByText("Safety training")).toBeTruthy();
  });

  it("Advance calls through and shows the one-time dev token with its warning", async () => {
    const client = stubClient([]);
    render(<OnboardingPanel client={client} role="admin" />);
    expect(await screen.findByText("New Nurse, RN")).toBeTruthy();
    expect(await screen.findByText(/No blockers/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Advance stage" }));
    await waitFor(() =>
      expect(client.request).toHaveBeenCalledWith(
        "/onboarding/ob-1/advance",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    expect(await screen.findByText("dev-token-once-123")).toBeTruthy();
    expect(screen.getByText(/shown.*once/)).toBeTruthy();
  });

  it("cascading placement selects populate from the tree, gate submit, and submit all four fields", async () => {
    lastCreateBody = null;
    const client = stubClient([]);
    render(<OnboardingPanel client={client} role="admin" />);
    expect(await screen.findByText("New Nurse, RN")).toBeTruthy();

    const region = screen.getByLabelText("Onboarding region") as HTMLSelectElement;
    await waitFor(() => expect(region.disabled).toBe(false));
    // Descendants start disabled, with the reason shown.
    const location = screen.getByLabelText("Onboarding location") as HTMLSelectElement;
    expect(location.disabled).toBe(true);
    expect(screen.getByText("Select a region first")).toBeTruthy();
    const submit = screen.getByRole("button", { name: "Start onboarding" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    expect(screen.getByText(/placement\s+is required to start onboarding/)).toBeTruthy();

    fireEvent.change(region, { target: { value: "region-main" } });
    expect(location.disabled).toBe(false);
    fireEvent.change(location, { target: { value: "facility-main" } });
    const department = screen.getByLabelText("Onboarding department") as HTMLSelectElement;
    expect(department.disabled).toBe(false);
    fireEvent.change(department, { target: { value: "dept-medicine" } });
    const unit = screen.getByLabelText("Onboarding unit") as HTMLSelectElement;
    expect(unit.disabled).toBe(false);
    fireEvent.change(unit, { target: { value: "unit-medsurg" } });

    fireEvent.change(screen.getByLabelText("Onboarding full name"), {
      target: { value: "New Nurse, RN" },
    });
    fireEvent.change(screen.getByLabelText("Onboarding job role"), {
      target: { value: "nurse" },
    });
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);
    await waitFor(() => expect(lastCreateBody).not.toBeNull());
    expect(lastCreateBody).toMatchObject({
      region: "region-main",
      location: "facility-main",
      department: "dept-medicine",
      unit: "unit-medsurg",
    });
  });

  it("changing a placement parent clears its descendants", async () => {
    render(<OnboardingPanel client={stubClient([])} role="admin" />);
    expect(await screen.findByText("New Nurse, RN")).toBeTruthy();
    const region = screen.getByLabelText("Onboarding region") as HTMLSelectElement;
    await waitFor(() => expect(region.disabled).toBe(false));
    fireEvent.change(region, { target: { value: "region-main" } });
    fireEvent.change(screen.getByLabelText("Onboarding location"), {
      target: { value: "facility-main" },
    });
    fireEvent.change(screen.getByLabelText("Onboarding department"), {
      target: { value: "dept-medicine" },
    });
    fireEvent.change(screen.getByLabelText("Onboarding unit"), {
      target: { value: "unit-medsurg" },
    });
    // Re-picking the region clears location/department/unit.
    fireEvent.change(region, { target: { value: "" } });
    expect((screen.getByLabelText("Onboarding location") as HTMLSelectElement).value).toBe("");
    expect((screen.getByLabelText("Onboarding department") as HTMLSelectElement).value).toBe("");
    expect((screen.getByLabelText("Onboarding unit") as HTMLSelectElement).value).toBe("");
    expect(
      (screen.getByRole("button", { name: "Start onboarding" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("orientation Complete fires the complete endpoint for that item", async () => {
    const client = stubClient([]);
    render(<OnboardingPanel client={client} role="admin" />);
    expect(await screen.findByText("Safety training")).toBeTruthy();
    const completes = screen.getAllByRole("button", { name: "Complete" });
    fireEvent.click(completes[0]);
    await waitFor(() =>
      expect(client.request).toHaveBeenCalledWith(
        "/onboarding/ob-1/orientation/safety/complete",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });
});
