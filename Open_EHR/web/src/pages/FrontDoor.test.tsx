import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import {
  isRealDate,
  RegisterPatientForm,
  validatePatientForm,
  EMPTY_PATIENT_FORM,
} from "../components/PatientForms";
import { BedBoardPage } from "./BedBoardPage";
import { HomePage } from "./HomePage";
import { PatientsPage } from "./PatientsPage";
import { WorkforcePage } from "./WorkforcePage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

const VALID_FORM = {
  ...EMPTY_PATIENT_FORM,
  family: "Lovelace",
  given: "Ada",
  birthDate: "1990-05-04",
  gender: "female",
};

describe("patient form typed validation (quality bar)", () => {
  it("accepts a well-formed form", () => {
    expect(validatePatientForm(VALID_FORM)).toEqual({});
  });

  it("rejects an impossible calendar date and a future DOB", () => {
    expect(isRealDate("2026-02-30")).toBe(false);
    expect(isRealDate("1990-05-04")).toBe(true);
    const badDay = validatePatientForm({ ...VALID_FORM, birthDate: "2026-02-30" });
    expect(badDay.birthDate).toMatch(/real calendar date/);
    const future = validatePatientForm({ ...VALID_FORM, birthDate: "2999-01-01" });
    expect(future.birthDate).toMatch(/future/);
  });

  it("rejects bad email/phone formats and missing required fields", () => {
    const errs = validatePatientForm({
      ...VALID_FORM,
      family: " ",
      email: "ada@",
      phone: "call me",
      emergencyPhone: "+1 555-7777",
      emergencyName: "",
    });
    expect(errs.family).toMatch(/required/);
    expect(errs.email).toMatch(/email address/);
    expect(errs.phone).toBeTruthy();
    expect(errs.emergencyName).toMatch(/required/);
  });
});

function registerClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    patientDuplicateCheck: vi.fn(async () => []),
    registerPatient: vi.fn(async () => ({
      resourceType: "Patient",
      id: "pat-new",
      name: [{ family: "Lovelace", given: ["Ada"] }],
      mrn: "MRN-000001",
      duplicates_confirmed: false,
    })),
    search: vi.fn(async () => []),
    ...overrides,
  } as unknown as ApiClient;
}

function fillRegistration() {
  fireEvent.change(screen.getByLabelText("Family name *"), {
    target: { value: "Lovelace" },
  });
  fireEvent.change(screen.getByLabelText("Given name *"), {
    target: { value: "Ada" },
  });
  fireEvent.change(screen.getByLabelText("Date of birth *"), {
    target: { value: "1990-05-04" },
  });
}

describe("RegisterPatientForm", () => {
  it("blocks a bad email inline and never calls the API", async () => {
    const client = registerClient();
    render(<RegisterPatientForm client={client} />);
    fillRegistration();
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "not-an-email" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Register patient" }));
    expect(await screen.findByText(/does not look like an email/)).toBeTruthy();
    expect(client.registerPatient).not.toHaveBeenCalled();
    expect(client.patientDuplicateCheck).not.toHaveBeenCalled();
  });

  it("warns on duplicates and registers only on explicit confirm", async () => {
    const candidate = {
      id: "pat-1",
      name: "Ada Lovelace",
      birthDate: "1990-05-04",
      active: true,
      identifiers: [{ system: "urn:opencare:mrn", value: "MRN-000001" }],
    };
    const client = registerClient({
      patientDuplicateCheck: vi.fn(async () => [candidate]),
    });
    render(<RegisterPatientForm client={client} />);
    fillRegistration();
    fireEvent.click(screen.getByRole("button", { name: "Register patient" }));
    expect(await screen.findByText(/Possible duplicate/)).toBeTruthy();
    expect(screen.getByText(/Ada Lovelace · born 1990-05-04/)).toBeTruthy();
    expect(client.registerPatient).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole("button", { name: /Register anyway/ }),
    );
    await waitFor(() => {
      expect(client.registerPatient).toHaveBeenCalledWith(
        expect.objectContaining({ confirm_duplicate: true }),
      );
    });
  });

  it("surfaces a server 409's candidates the same way", async () => {
    const conflict = new ApiError(409, "Possible duplicate registration", {
      detail: "Possible duplicate registration",
      candidates: [
        { id: "pat-9", name: "Ada Lovelace", birthDate: "1990-05-04", active: true, identifiers: [] },
      ],
    });
    const client = registerClient({
      patientDuplicateCheck: vi.fn(async () => []),
      registerPatient: vi.fn(async () => {
        throw conflict;
      }),
    });
    render(<RegisterPatientForm client={client} />);
    fillRegistration();
    fireEvent.click(screen.getByRole("button", { name: "Register patient" }));
    expect(await screen.findByText(/Possible duplicate/)).toBeTruthy();
    expect(screen.getByText(/pat-9/)).toBeTruthy();
  });
});

describe("PatientsPage", () => {
  it("shows the register panel to registration roles and hides it from patients", async () => {
    const client = registerClient();
    const { unmount } = render(
      <PatientsPage client={client} role="viewer" onOpenChart={() => {}} />,
    );
    expect(await screen.findByText("Register a new patient")).toBeTruthy();
    expect(screen.getByText(/1 · Register/)).toBeTruthy();
    unmount();
    render(<PatientsPage client={client} role="patient" onOpenChart={() => {}} />);
    await screen.findByText("Patients");
    expect(screen.queryByText("Register a new patient")).toBeNull();
  });
});

const WORKFORCE = {
  units: [{ code: "unit-medsurg", display: "Medical-Surgical" }],
  serviceLines: [{ code: "medicine", display: "Medicine" }],
  concepts: [{ code: "nurse", display: "Nurse" }],
  shiftTemplates: [],
  credentialTypes: [],
  expiry: { worklistWindowDays: 90, alertWindowDays: 30 },
};

// Organisation hierarchy (Placement pass): GET /org-hierarchy tree,
// fetched through client.request by api/org.
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
              ],
            },
          ],
        },
      ],
    },
  ],
};

const PLACEMENT_VIEW = {
  region: { code: "region-main", display: "OpenCare Region" },
  location: { code: "facility-main", display: "OpenCare Hospital" },
  department: { code: "dept-medicine", display: "Medicine" },
  unit: { code: "unit-medsurg", display: "Medical-Surgical Unit" },
};

function workforceClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    request: vi.fn(async (path: string) => {
      if (path === "/org-hierarchy") return ORG_TREE;
      throw new Error(`Unexpected request: ${path}`);
    }),
    listStaff: vi.fn(async () => []),
    credentialWorklist: vi.fn(async () => ({
      items: [], total: 0, expired: 0,
      alert_window_days: 30, worklist_window_days: 90,
    })),
    listRoster: vi.fn(async () => []),
    onCallNow: vi.fn(async () => ({ now: "", services: [], total: 0, covered: 0 })),
    workforceConfig: vi.fn(async () => WORKFORCE),
    listAccounts: vi.fn(async () => []),
    createAccount: vi.fn(async () => ({
      account_id: 7,
      username: "j.osei",
      full_name: "Jordan Osei",
      role: "viewer",
      unit: null,
      service_line: null,
      email: null,
      phone: null,
      staff_id: null,
      patient_ref: null,
      status: "active",
      has_dev_token: true,
      created_by: "dev-admin",
      updated_by: "dev-admin",
      created_at: null,
      updated_at: null,
      dev_token: "acct-j.osei-deadbeef",
      dev_token_note: "Dev-mode sign-in token — shown once, store it now.",
    })),
    ...overrides,
  } as unknown as ApiClient;
}

describe("WorkforcePage accounts", () => {
  it("creates an account and shows the one-time dev token", async () => {
    const client = workforceClient();
    render(<WorkforcePage client={client} role="admin" />);
    // Depth-pass Tabs rollout (Workstream 1): accounts live behind
    // the Accounts tab — click it before the original assertions.
    fireEvent.click(await screen.findByRole("tab", { name: "Accounts" }));
    expect(await screen.findByText("User accounts")).toBeTruthy();
    expect(
      screen.getByText(/Passwords are not stored here/),
    ).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Account username"), {
      target: { value: "j.osei" },
    });
    fireEvent.change(screen.getByLabelText("Account full name"), {
      target: { value: "Jordan Osei" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByText(/acct-j\.osei-deadbeef/)).toBeTruthy();
    expect(screen.getByText(/shown/)).toBeTruthy();
    expect(client.createAccount).toHaveBeenCalledWith(
      expect.objectContaining({ username: "j.osei", role: "viewer" }),
    );
  });

  it("blocks a spaced username inline without calling the API", async () => {
    const client = workforceClient();
    render(<WorkforcePage client={client} role="admin" />);
    // Depth-pass Tabs rollout: accounts live behind the Accounts tab.
    fireEvent.click(await screen.findByRole("tab", { name: "Accounts" }));
    await screen.findByText("User accounts");
    fireEvent.change(screen.getByLabelText("Account username"), {
      target: { value: "has space" },
    });
    fireEvent.change(screen.getByLabelText("Account full name"), {
      target: { value: "Space Case" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByText(/no spaces/)).toBeTruthy();
    expect(client.createAccount).not.toHaveBeenCalled();
  });

  it("disables a select whose config source is empty, with the reason", async () => {
    const client = workforceClient({
      workforceConfig: vi.fn(async () => ({
        ...WORKFORCE,
        units: [],
        serviceLines: [],
      })),
    });
    render(<WorkforcePage client={client} role="admin" />);
    // Depth-pass Tabs rollout: accounts live behind the Accounts tab.
    fireEvent.click(await screen.findByRole("tab", { name: "Accounts" }));
    await screen.findByText("User accounts");
    const unitSelect = screen.getByLabelText("Unit") as HTMLSelectElement;
    expect(unitSelect.disabled).toBe(true);
    expect(
      screen.getAllByText(/No units in the published workforce config/).length,
    ).toBeGreaterThan(0);
  });

  it("hides the accounts section from non-admin roles", async () => {
    const client = workforceClient();
    render(<WorkforcePage client={client} role="clinician" />);
    await screen.findByText("Workforce");
    expect(screen.queryByText("User accounts")).toBeNull();
  });
});

const HOME_ITEM = {
  encounter_id: "enc-1",
  patient_ref: "Patient/pat-1",
  patient_id: "pat-1",
  patient_name: "Ada Lovelace",
  bed: "MS-101A",
  attending_staff_id: "staff-001",
  attending_name: "Maya Chen, MD",
  admitted_at: "2026-10-17T08:00:00Z",
  los_days: 1,
  discharge_ready: false,
};

function homeClient(audience: string): ApiClient {
  return {
    homeDashboard: vi.fn(async () => ({
      audience,
      basis: "staff",
      staff_id: "staff-001",
      dashboard_key: "k",
      dashboard: { key: "k", title: "Home", widgets: [] },
    })),
    myPatients: vi.fn(async () => ({
      staff_id: "staff-001",
      items: audience === "clinician" ? [HOME_ITEM] : [],
      total: audience === "clinician" ? 1 : 0,
    })),
    myUnitPatients: vi.fn(async () => ({
      unit: "unit-medsurg",
      unit_display: "Medical-Surgical",
      items: [HOME_ITEM],
      total: 1,
    })),
    request: vi.fn(async () => ({})),
  } as unknown as ApiClient;
}

describe("WorkforcePage placement (Organisation pass)", () => {
  const placedStaff = [
    {
      staff_id: "staff-001", username: null, full_name: "Maya Chen, MD",
      job_role: "physician", unit: "unit-medsurg",
      region: "region-main", location: "facility-main",
      department: "dept-medicine", service_line: "medicine",
      status: "active", audience: null, credential_count: 3,
    },
  ];
  const accountBase = {
    full_name: "Maya Chen, MD", role: "clinician", unit: "unit-medsurg",
    service_line: "medicine", email: null, phone: null,
    staff_id: "staff-001", patient_ref: null, status: "active",
    has_dev_token: false, created_by: "dev-admin", updated_by: "dev-admin",
    created_at: null, updated_at: null,
  };

  it("renders the staff placement (displays resolved from the tree) and sends placement filters", async () => {
    const client = workforceClient({
      listStaff: vi.fn(async () => placedStaff),
    });
    render(<WorkforcePage client={client} role="admin" />);
    expect(
      await screen.findAllByText(
        /Placement: Medical-Surgical Unit · Medicine · OpenCare Hospital · OpenCare Region/,
      ),
    ).toBeTruthy();
    // The region filter cascades and reaches the staff fetch as a
    // query param (client.listStaff argument).
    const region = screen.getByLabelText("Staff filter region") as HTMLSelectElement;
    await waitFor(() => expect(region.disabled).toBe(false));
    fireEvent.change(region, { target: { value: "region-main" } });
    await waitFor(() =>
      expect(client.listStaff).toHaveBeenCalledWith(
        expect.objectContaining({ region: "region-main" }),
      ),
    );
  });

  it("shows account placement in the accounts table and 'Not placed' when null", async () => {
    const client = workforceClient({
      listAccounts: vi.fn(async () => [
        { ...accountBase, account_id: 1, username: "m.chen", placement: PLACEMENT_VIEW },
        { ...accountBase, account_id: 2, username: "un.placed", placement: null },
      ]),
    });
    render(<WorkforcePage client={client} role="admin" />);
    fireEvent.click(await screen.findByRole("tab", { name: "Accounts" }));
    expect(await screen.findByText("User accounts")).toBeTruthy();
    expect(
      screen.getByText(
        "Medical-Surgical Unit · Medicine · OpenCare Hospital · OpenCare Region",
      ),
    ).toBeTruthy();
    expect(screen.getByText("Not placed")).toBeTruthy();
  });
});

describe("HomePage unit-first sections", () => {
  it("leads the clinician home with My Patients, then the unit list", async () => {
    render(
      <HomePage client={homeClient("clinician")} fallback={<p>fallback</p>} />,
    );
    expect(
      await screen.findByText("My patients — I am the attending"),
    ).toBeTruthy();
    expect(screen.getByText(/My unit — Medical-Surgical/)).toBeTruthy();
    expect(screen.getAllByText(/Ada Lovelace/).length).toBe(2);
    expect(screen.getAllByText("Not ready").length).toBeGreaterThan(0);
  });

  it("gives the front desk its workflow quick actions", async () => {
    const onNavigate = vi.fn();
    render(
      <HomePage
        client={homeClient("front-desk")}
        fallback={<p>fallback</p>}
        onNavigate={onNavigate}
      />,
    );
    expect(await screen.findByText("Front desk — start here")).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: "Register a new patient" }),
    );
    expect(onNavigate).toHaveBeenCalledWith("patients");
    fireEvent.click(
      screen.getByRole("button", { name: "Book an appointment" }),
    );
    expect(onNavigate).toHaveBeenCalledWith("clinic");
  });

  it("leads the nurse home with the unit list only", async () => {
    render(<HomePage client={homeClient("nurse")} fallback={<p>fallback</p>} />);
    expect(await screen.findByText(/My unit — Medical-Surgical/)).toBeTruthy();
    expect(screen.queryByText("My patients — I am the attending")).toBeNull();
  });
});

describe("BedBoardPage units & beds manager", () => {
  function bedClient(overrides: Record<string, unknown> = {}): ApiClient {
    return {
      inpatientCensus: vi.fn(async () => ({
        units: [],
        totals: {
          beds_total: 0, occupied: 0, available: 0, cleaning: 0,
          blocked: 0, occupancy_pct: 0,
        },
        expected_discharges_today: { total: 0, discharges: [] },
      })),
      inpatientLocations: vi.fn(async () => ({
        locations: [
          { code: "unit-medsurg", display: "Medical-Surgical", type: "unit", parent: "facility-main", active: true },
          { code: "MS-101A", display: "Med-Surg 101A", type: "bed", parent: "unit-medsurg", active: true },
        ],
        version: 3,
        bed_statuses: ["available"],
      })),
      locationsDraftStatus: vi.fn(async () => ({
        published_version: 3,
        draft: { version: 4, created_by: "dev-admin", updated_by: "dev-admin" },
      })),
      composeLocationsDraft: vi.fn(async () => ({
        version: 4,
        status: "draft",
        operations_applied: 1,
        note: "Draft only — nothing changes live until this version is published in System Studio.",
      })),
      effectiveConfig: vi.fn(async () => {
        throw new Error("no form");
      }),
      ...overrides,
    } as unknown as ApiClient;
  }

  it("labels the pending draft and composes an add-bed into it", async () => {
    const client = bedClient();
    render(<BedBoardPage client={client} role="clinician" />);
    // UX pass (spec P-7): the manager is a tab on BedBoardPage now.
    fireEvent.click(await screen.findByRole("tab", { name: "Units & beds manager" }));
    expect(await screen.findByText(/Draft pending publish/)).toBeTruthy();
    expect(screen.getByText(/still shows published v3/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("New bed code"), {
      target: { value: "MS-107A" },
    });
    fireEvent.change(screen.getByLabelText("New bed name"), {
      target: { value: "Med-Surg 107A" },
    });
    fireEvent.change(screen.getByLabelText("New bed unit"), {
      target: { value: "unit-medsurg" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add bed to draft" }));
    await waitFor(() => {
      expect(client.composeLocationsDraft).toHaveBeenCalledWith([
        expect.objectContaining({ op: "add-bed", code: "MS-107A", unit: "unit-medsurg" }),
      ]);
    });
    expect(await screen.findByText(/Nothing is live yet/)).toBeTruthy();
  });

  it("rejects a malformed bed code inline without calling the API", async () => {
    const client = bedClient();
    render(<BedBoardPage client={client} role="clinician" />);
    // UX pass (spec P-7): the manager is a tab on BedBoardPage now.
    fireEvent.click(await screen.findByRole("tab", { name: "Units & beds manager" }));
    await screen.findByText(/Draft pending publish/);
    fireEvent.change(screen.getByLabelText("New bed code"), {
      target: { value: "bad code!" },
    });
    fireEvent.change(screen.getByLabelText("New bed name"), {
      target: { value: "Nowhere" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add bed to draft" }));
    expect(await screen.findByText(/Codes are letters, digits and dashes/)).toBeTruthy();
    expect(client.composeLocationsDraft).not.toHaveBeenCalled();
  });
});
