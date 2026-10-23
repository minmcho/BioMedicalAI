import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import {
  fixtureCensus,
  fixtureDischargeChecklistForm,
} from "../test/fixtures";
import { BedBoardPage } from "./BedBoardPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    inpatientCensus: vi.fn(async () => fixtureCensus),
    effectiveConfig: vi.fn(async () => ({
      kind: "form",
      key: "discharge-checklist",
      version: 1,
      status: "published",
      payload: fixtureDischargeChecklistForm,
    })),
    setBedStatus: vi.fn(async (code: string, status: string) => ({
      bed: { code, status },
    })),
    admitPatient: vi.fn(async () => ({})),
    transferPatient: vi.fn(async () => ({})),
    dischargePatient: vi.fn(async () => ({})),
    setDischargePlan: vi.fn(async () => ({})),
    create: vi.fn(async () => ({ resourceType: "QuestionnaireResponse", id: "qr-1" })),
    // Front Door pass: the Units & Beds manager reads the published
    // locations config and the locations draft status on mount.
    inpatientLocations: vi.fn(async () => ({
      locations: [
        { code: "facility-main", display: "OpenCare Hospital", type: "facility", active: true },
        { code: "dept-medicine", display: "Medicine", type: "department", parent: "facility-main", active: true },
        { code: "unit-medsurg", display: "Medical-Surgical Unit", type: "unit", parent: "dept-medicine", active: true },
        { code: "MS-101A", display: "MS 101 A", type: "bed", parent: "unit-medsurg", active: true },
      ],
      version: 3,
      bed_statuses: ["available", "occupied", "cleaning"],
    })),
    // Organisation hierarchy (Placement pass): GET /org-hierarchy,
    // fetched through client.request by api/org.
    request: vi.fn(async (path: string) => {
      if (path === "/org-hierarchy") {
        return {
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
                    {
                      code: "dept-critical-care", display: "Critical Care",
                      units: [
                        { code: "unit-icu", display: "Intensive Care Unit", bed_count: 6 },
                      ],
                    },
                  ],
                },
              ],
            },
          ],
        };
      }
      throw new Error(`Unexpected request: ${path}`);
    }),
    locationsDraftStatus: vi.fn(async () => ({ published_version: 3, draft: null })),
    composeLocationsDraft: vi.fn(async () => ({
      version: 4, draft: true, operations: 1,
      note: "Draft only — nothing is live until published in System Studio.",
    })),
    ...overrides,
  } as unknown as ApiClient;
}

describe("BedBoardPage", () => {
  it("renders units as sections with bed cards, statuses and census totals", async () => {
    render(<BedBoardPage client={stubClient()} role="clinician" />);
    expect(await screen.findByText(/Medical-Surgical Unit — 1\/3 occupied/)).toBeTruthy();
    expect(screen.getByText(/Intensive Care Unit — 1\/1 occupied/)).toBeTruthy();
    // MS-101A appears both as a bed card and in the expected-
    // discharges list; statuses repeat across beds.
    expect(screen.getAllByText("MS-101A").length).toBeGreaterThan(0);
    expect(screen.getAllByText("occupied").length).toBeGreaterThan(0);
    expect(screen.getAllByText("cleaning").length).toBeGreaterThan(0);
    expect(screen.getByTestId("census-totals").textContent).toContain("occupancy 50%");
    // The occupied card names the patient and the length of stay.
    expect(screen.getByText(/Patient Patient\/pat-1 · LOS 2 d/)).toBeTruthy();
  });

  it("shows today's expected discharges from the census feed", async () => {
    render(<BedBoardPage client={stubClient()} role="clinician" />);
    expect(await screen.findByText(/Expected discharges today \(1\)/)).toBeTruthy();
    expect(screen.getByText(/disposition home/)).toBeTruthy();
  });

  it("housekeeping flips a cleaning bed to available through the client", async () => {
    const client = stubClient();
    render(<BedBoardPage client={client} role="clinician" />);
    await screen.findByText(/Medical-Surgical Unit — 1\/3 occupied/);
    fireEvent.click(screen.getByRole("button", { name: "Mark available" }));
    await waitFor(() => {
      expect(client.setBedStatus).toHaveBeenCalledWith("MS-101B", "available");
    });
  });

  it("admits a patient into a selected available bed", async () => {
    const client = stubClient();
    render(<BedBoardPage client={client} role="clinician" />);
    await screen.findByText(/Medical-Surgical Unit — 1\/3 occupied/);
    fireEvent.change(screen.getByLabelText("Admit patient id"), {
      target: { value: "pat-9" },
    });
    fireEvent.change(screen.getByLabelText("Admit bed"), {
      target: { value: "MS-102A" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Admit" }));
    await waitFor(() => {
      expect(client.admitPatient).toHaveBeenCalledWith({
        patient: "pat-9",
        bed: "MS-102A",
      });
    });
  });

  it("surfaces an occupied-bed conflict (409) as an inline error", async () => {
    const client = stubClient({
      admitPatient: vi.fn(async () => {
        throw new ApiError(409, "Bed MS-102A is 'occupied' and cannot take a patient");
      }),
    });
    render(<BedBoardPage client={client} role="clinician" />);
    await screen.findByText(/Medical-Surgical Unit — 1\/3 occupied/);
    fireEvent.change(screen.getByLabelText("Admit patient id"), {
      target: { value: "pat-9" },
    });
    fireEvent.change(screen.getByLabelText("Admit bed"), {
      target: { value: "MS-102A" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Admit" }));
    expect(await screen.findByText(/cannot take a patient/)).toBeTruthy();
  });

  it("transfers a patient to the chosen bed through the client", async () => {
    const client = stubClient();
    render(<BedBoardPage client={client} role="clinician" />);
    await screen.findByText(/Medical-Surgical Unit — 1\/3 occupied/);
    fireEvent.change(screen.getByLabelText("Transfer target for MS-101A"), {
      target: { value: "MS-102A" },
    });
    // Two occupied beds render two Transfer buttons; MS-101A's is first.
    fireEvent.click(screen.getAllByRole("button", { name: "Transfer" })[0]);
    await waitFor(() => {
      expect(client.transferPatient).toHaveBeenCalledWith("enc-1", "MS-102A");
    });
  });

  it("discharge panel renders the published checklist form and submits plan + discharge", async () => {
    const client = stubClient();
    render(<BedBoardPage client={client} role="clinician" />);
    await screen.findByText(/Medical-Surgical Unit — 1\/3 occupied/);
    fireEvent.click(screen.getAllByRole("button", { name: "Discharge…" })[0]);
    // FormRenderer renders the published form's fields (no title).
    expect(await screen.findByLabelText(/Discharge disposition/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/Discharge disposition/), {
      target: { value: "home" },
    });
    fireEvent.change(screen.getByLabelText(/Target discharge date/), {
      target: { value: "2026-10-20" },
    });
    fireEvent.click(screen.getByLabelText(/Medications reconciled/));
    fireEvent.click(screen.getByRole("button", {
      name: "Submit checklist & discharge",
    }));
    await waitFor(() => {
      expect(client.create).toHaveBeenCalledWith(
        "QuestionnaireResponse",
        expect.objectContaining({
          questionnaire: "Questionnaire/discharge-checklist",
        }),
      );
      expect(client.setDischargePlan).toHaveBeenCalledWith(
        "enc-1",
        expect.objectContaining({
          checklistResponseId: "qr-1",
          disposition: "home",
          targetDate: "2026-10-20",
        }),
      );
      expect(client.dischargePatient).toHaveBeenCalledWith("enc-1", "home");
    });
  });

  // UX pass (spec P-7): the Units & beds manager is its own tab now;
  // the manager tests open it first (assertions unchanged).
  async function openManagerTab() {
    fireEvent.click(await screen.findByRole("tab", { name: "Units & beds manager" }));
  }

  it("renders the four-level org hierarchy tree with bed counts", async () => {
    render(<BedBoardPage client={stubClient()} role="clinician" />);
    await openManagerTab();
    const tree = await screen.findByLabelText("Organisation hierarchy tree");
    expect(tree.textContent).toContain("OpenCare Region");
    expect(tree.textContent).toContain("(region)");
    expect(tree.textContent).toContain("OpenCare Hospital");
    expect(tree.textContent).toContain("(location)");
    expect(tree.textContent).toContain("Medicine");
    expect(tree.textContent).toContain("Critical Care");
    expect(tree.textContent).toContain("(department)");
    expect(tree.textContent).toContain("Medical-Surgical Unit — 12 beds");
    expect(tree.textContent).toContain("Intensive Care Unit — 6 beds");
  });

  it("add-department composes the right locations draft operation", async () => {
    const client = stubClient();
    render(<BedBoardPage client={client} role="clinician" />);
    await openManagerTab();
    await screen.findByLabelText("New department code");
    fireEvent.change(screen.getByLabelText("New department code"), {
      target: { value: "dept-surgery" },
    });
    fireEvent.change(screen.getByLabelText("New department name"), {
      target: { value: "Surgery" },
    });
    fireEvent.change(screen.getByLabelText("New department location"), {
      target: { value: "facility-main" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add department to draft" }));
    await waitFor(() => {
      expect(client.composeLocationsDraft).toHaveBeenCalledWith([
        { op: "add-department", code: "dept-surgery", display: "Surgery", location: "facility-main" },
      ]);
    });
    expect(await screen.findByText(/Nothing is live yet/)).toBeTruthy();
  });

  it("add-region composes an add-region draft operation", async () => {
    const client = stubClient();
    render(<BedBoardPage client={client} role="clinician" />);
    await openManagerTab();
    await screen.findByLabelText("New region code");
    fireEvent.change(screen.getByLabelText("New region code"), {
      target: { value: "region-north" },
    });
    fireEvent.change(screen.getByLabelText("New region name"), {
      target: { value: "North Region" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add region to draft" }));
    await waitFor(() => {
      expect(client.composeLocationsDraft).toHaveBeenCalledWith([
        { op: "add-region", code: "region-north", display: "North Region" },
      ]);
    });
  });

  it("viewer sees the board but no work actions", async () => {
    render(<BedBoardPage client={stubClient()} role="viewer" />);
    expect(
      (await screen.findAllByText("MS-101A")).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Admit" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Mark available" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Transfer" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Discharge…" })).toBeNull();
  });
});
