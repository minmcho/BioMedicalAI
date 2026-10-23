import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { HomePage } from "./HomePage";
import { ImportPage } from "./ImportPage";
import { ReportsPage } from "./ReportsPage";
import { WorkforcePage } from "./WorkforcePage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// jsdom has no object-URL downloads; the report/document actions call
// saveDownloadedFile after the client resolves — stub the URL pair so
// the success path runs end to end.
Object.defineProperty(URL, "createObjectURL", {
  value: () => "blob:test",
  configurable: true,
});
Object.defineProperty(URL, "revokeObjectURL", {
  value: () => undefined,
  configurable: true,
});

// Phase 13 pages: workforce, reports/print, import centre and the
// role-aware home landing — rendered from the /people + /reports API
// surface through stub clients, the Phase 12 page-test pattern.

function workforceClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    listStaff: vi.fn(async () => [
      {
        staff_id: "staff-001", username: "dev-clinician",
        full_name: "Maya Chen, MD", job_role: "physician",
        unit: "unit-medsurg", service_line: "medicine", status: "active",
        audience: "clinician", credential_count: 4,
      },
    ]),
    credentialWorklist: vi.fn(async () => ({
      items: [
        {
          credential_id: "cred-009", staff_id: "staff-004",
          staff_name: "Tom Becker, RN", type: "rn-licence",
          number: "SYN-RN-1004", issued_on: "2024-01-01",
          expires_on: "2026-10-13", days_to_expiry: -5, status: "expired",
          alert_window: true,
        },
      ],
      total: 1, expired: 1, alert_window_days: 30, worklist_window_days: 90,
    })),
    listRoster: vi.fn(async () => [
      {
        shift_id: "shift-1", staff_id: "staff-001",
        staff_name: "Maya Chen, MD", unit: "unit-medsurg",
        date: "2026-10-18", template: "day",
        starts_at: "2026-10-18T07:00:00+00:00",
        ends_at: "2026-10-18T19:00:00+00:00", status: "scheduled",
      },
    ]),
    onCallNow: vi.fn(async () => ({
      now: "2026-10-18T12:00:00Z",
      services: [
        {
          service_line: "medicine",
          primary: {
            staff_id: "staff-001", staff_name: "Maya Chen, MD",
            role_on_call: "primary",
            starts_at: "2026-10-12T00:00:00Z", ends_at: "2026-10-19T00:00:00Z",
          },
          backup: null,
        },
        { service_line: "surgery", primary: null, backup: null },
      ],
      total: 2, covered: 1,
    })),
    scanCredentials: vi.fn(async () => ({
      scanned: 27, alerted: 7, alert_window_days: 30,
    })),
    // Front Door pass: the page reads the published workforce config
    // on mount (staff-edit + account selects) and, for admins, the
    // account list for the Accounts section.
    workforceConfig: vi.fn(async () => ({
      units: [{ code: "unit-medsurg", display: "Medical-Surgical Unit", serviceLine: "medicine" }],
      serviceLines: [{ code: "medicine", display: "Medicine" }],
      concepts: [{ code: "physician", display: "Physician", active: true }],
      shiftTemplates: [{ code: "day", display: "Day", start: "07:00", end: "19:00" }],
      credentialTypes: [{ code: "rn-licence", display: "RN licence" }],
      expiry: { worklistWindowDays: 90, alertWindowDays: 30 },
    })),
    listAccounts: vi.fn(async () => []),
    ...overrides,
  } as unknown as ApiClient;
}

describe("WorkforcePage (Phase 13)", () => {
  it("renders on-call, the expiry worklist, roster and directory", async () => {
    // Adjusted for the depth-pass Tabs rollout (Workstream 1): the
    // stacked sections now live behind tabs (Directory default), so
    // the test clicks through the tabs — assertions unchanged.
    render(<WorkforcePage client={workforceClient()} role="clinician" />);
    expect(await screen.findByText("Maya Chen, MD")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Credentials & licences" }));
    expect(await screen.findByText("Tom Becker, RN")).toBeTruthy();
    expect(screen.getByText("expired · alert window")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "On-call" }));
    expect(screen.getByText("uncovered")).toBeTruthy();
    expect(screen.getByText(/1 of 2 service lines covered/)).toBeTruthy();
    // Clinicians (no workforce.work) get no scan button.
    fireEvent.click(screen.getByRole("tab", { name: "Credentials & licences" }));
    expect(screen.queryByText("Run credential expiry scan")).toBeNull();
  });

  it("admin can run the credential scan and sees the outcome", async () => {
    // Adjusted for the depth-pass Tabs rollout: the scan lives in the
    // Credentials & licences tab.
    const client = workforceClient();
    render(<WorkforcePage client={client} role="admin" />);
    fireEvent.click(await screen.findByRole("tab", { name: "Credentials & licences" }));
    const button = await screen.findByText("Run credential expiry scan");
    fireEvent.click(button);
    expect(
      await screen.findByText(/27 credentials scanned, 7 renewal alert/),
    ).toBeTruthy();
    expect(client.scanCredentials).toHaveBeenCalled();
  });
});

function reportsClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    reportsCatalogue: vi.fn(async () => [
      {
        key: "credential-expiry", title: "Credential expiry",
        description: "Credentials expiring inside a window",
        source: "credential-expiry",
        parameters: [
          { name: "window_days", type: "number", label: "Window (days)", default: 90 },
        ],
        formats: ["pdf", "csv"], version: 1,
      },
    ]),
    exportDatasets: vi.fn(async () => [
      { key: "patients", title: "Patients", description: "Patient list" },
    ]),
    runReport: vi.fn(async () => ({
      blob: new Blob(["%PDF"], { type: "application/pdf" }),
      filename: "credential-expiry.pdf", rows: 7,
    })),
    printInvoice: vi.fn(async () => ({
      blob: new Blob(["%PDF"]), filename: "invoice-claim-1.pdf", rows: null,
    })),
    printReceipt: vi.fn(async () => ({
      blob: new Blob(["%PDF"]), filename: "receipt.pdf", rows: null,
    })),
    printStatement: vi.fn(async () => ({
      blob: new Blob(["%PDF"]), filename: "statement.pdf", rows: null,
    })),
    printSuperbill: vi.fn(async () => ({
      blob: new Blob(["%PDF"]), filename: "superbill.pdf", rows: null,
    })),
    runExport: vi.fn(async () => ({
      blob: new Blob(["id\n"]), filename: "patients.csv", rows: 2,
    })),
    ...overrides,
  } as unknown as ApiClient;
}

describe("ReportsPage (Phase 13)", () => {
  it("renders the catalogue with its parameter form and runs a report", async () => {
    const client = reportsClient();
    render(<ReportsPage client={client} role="admin" />);
    expect(await screen.findByText("Credential expiry")).toBeTruthy();
    expect(screen.getByLabelText("Window (days)")).toBeTruthy();
    fireEvent.click(screen.getByText("Run & download"));
    await waitFor(() => expect(client.runReport).toHaveBeenCalled());
    expect(client.runReport).toHaveBeenCalledWith(
      "credential-expiry", "pdf", { window_days: 90 },
    );
  });

  it("export centre is capability-gated: hidden for the clinician", async () => {
    render(<ReportsPage client={reportsClient()} role="clinician" />);
    expect(await screen.findByText("Report catalogue")).toBeTruthy();
    expect(screen.queryByText("Export centre")).toBeNull();
  });

  it("admin sees the export centre and can export a dataset", async () => {
    // Adjusted for the depth-pass Tabs rollout: the export centre
    // lives behind the Export tab.
    const client = reportsClient();
    render(<ReportsPage client={client} role="admin" />);
    fireEvent.click(await screen.findByRole("tab", { name: "Export" }));
    expect(await screen.findByText("Export centre")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Export" }));
    await waitFor(() => expect(client.runExport).toHaveBeenCalled());
  });
});

function importClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    importProfiles: vi.fn(async () => [
      {
        key: "patient-intake", entity_type: "patients",
        description: "Intake spreadsheet", columns: ["MRN", "Last Name"],
        version: 1,
      },
    ]),
    validateImport: vi.fn(async () => ({
      profile: "patient-intake", entity_type: "patients", rows_total: 2,
      valid: false,
      errors: [{ row: 3, field: "family_name", reason: "family_name is required" }],
      preview: [],
    })),
    commitImport: vi.fn(async () => ({
      imported: 2, entity_type: "patients", profile: "patient-intake",
    })),
    ...overrides,
  } as unknown as ApiClient;
}

describe("ImportPage (Phase 13)", () => {
  it("validation shows the full error report and blocks commit", async () => {
    const client = importClient();
    render(<ImportPage client={client} role="admin" />);
    expect(await screen.findByText(/patient-intake \(patients\)/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("CSV content"), {
      target: { value: "MRN,Last Name\npat-1,Doe\npat-2,\n" },
    });
    fireEvent.click(screen.getByText("Validate"));
    expect(await screen.findByText("family_name is required")).toBeTruthy();
    expect(screen.getByText(/nothing will import/)).toBeTruthy();
    const commit = screen.getByText("Commit import") as HTMLButtonElement;
    expect(commit.disabled).toBe(true);
    expect(client.commitImport).not.toHaveBeenCalled();
  });

  it("a valid file enables commit and reports the imported count", async () => {
    const client = importClient({
      validateImport: vi.fn(async () => ({
        profile: "patient-intake", entity_type: "patients", rows_total: 2,
        valid: true, errors: [], preview: [],
      })),
    });
    render(<ImportPage client={client} role="admin" />);
    expect(await screen.findByText(/patient-intake \(patients\)/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("CSV content"), {
      target: { value: "MRN,Last Name\npat-1,Doe\n" },
    });
    fireEvent.click(screen.getByText("Validate"));
    expect(await screen.findByText(/ready to commit/)).toBeTruthy();
    fireEvent.click(screen.getByText("Commit import"));
    expect(await screen.findByText(/Imported 2 patients record/)).toBeTruthy();
  });
});

describe("HomePage (Phase 13)", () => {
  it("renders the audience dashboard when one is published", async () => {
    const client = {
      homeDashboard: vi.fn(async () => ({
        audience: "executive", basis: "directory", staff_id: "staff-011",
        dashboard_key: "audience-executive",
        dashboard: {
          title: "Executive overview",
          widgets: [
            {
              id: "w1", title: "Occupied", endpoint: "/people/analytics/overview",
              display: "big-number", field: "occupied",
            },
          ],
        },
      })),
      request: vi.fn(async () => ({ occupied: 9, total: 1 })),
      // Front Door pass: HomePage also probes the caller's own work
      // lists on mount; an executive with neither gets empty payloads.
      myPatients: vi.fn(async () => ({ staff_id: "staff-011", items: [], total: 0 })),
      myUnitPatients: vi.fn(async () => ({ unit: null, items: [], total: 0 })),
    } as unknown as ApiClient;
    render(<HomePage client={client} fallback={<p>fallback home</p>} />);
    expect(await screen.findByText("Executive overview")).toBeTruthy();
    expect(await screen.findByText("9")).toBeTruthy();
    expect(screen.queryByText("fallback home")).toBeNull();
  });

  it("falls back to the ordinary home when no dashboard is published", async () => {
    const client = {
      homeDashboard: vi.fn(async () => ({
        audience: "clinician", basis: "role-map", staff_id: null,
        dashboard_key: null, dashboard: null,
      })),
      myPatients: vi.fn(async () => ({ staff_id: null, items: [], total: 0 })),
      myUnitPatients: vi.fn(async () => ({ unit: null, items: [], total: 0 })),
    } as unknown as ApiClient;
    render(<HomePage client={client} fallback={<p>fallback home</p>} />);
    expect(await screen.findByText("fallback home")).toBeTruthy();
  });
});
