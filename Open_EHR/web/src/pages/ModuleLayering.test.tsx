import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { Session } from "../auth/session";
import { RuntimeConfigProvider } from "../auth/runtimeConfig";
import {
  fixtureCondition,
  fixtureEncounter,
  fixtureMedication,
  fixtureObservation,
  fixturePatient,
  fixtureProcedure,
  fixtureReport,
} from "../test/fixtures";
import { ChartPage } from "./ChartPage";
import { ClinicDayPage } from "./ClinicDayPage";
import { PatientsPage } from "./PatientsPage";
import { PopHealthPage } from "./PopHealthPage";
import { PortalPage } from "./PortalPage";
import { SchedulingPage } from "./SchedulingPage";

// UX pass (WS5a) — module layering: ModuleHeader adoption (one h2,
// purpose, one primary action), stacked sections converted to the
// shared Tabs, and the front-desk landing-tab rule on Clinic day.
// RTL auto-cleanup does not run in this repo's vitest setup.
afterEach(cleanup);

function patientsClient(): ApiClient {
  return {
    search: vi.fn(async () => []),
    patientDuplicateCheck: vi.fn(async () => []),
    registerPatient: vi.fn(async () => ({ id: "pat-new" })),
    effectiveConfig: vi.fn(async () => {
      throw new Error("not published");
    }),
    request: vi.fn(async () => ({})),
  } as unknown as ApiClient;
}

describe("ModuleHeader adoption", () => {
  it("Patients: one h2, a purpose line, and Start registration for registration roles only", async () => {
    const { container, unmount } = render(
      <PatientsPage client={patientsClient()} role="viewer" onOpenChart={() => {}} />,
    );
    expect(await screen.findByText("Register a new patient")).toBeTruthy();
    expect(container.querySelectorAll("h2")).toHaveLength(1);
    expect(
      screen.getByText(/Find a patient record, register a new patient/),
    ).toBeTruthy();
    // The header primary is wired: clicking it focuses the
    // registration section's first control (no throw, no dead button).
    fireEvent.click(screen.getByRole("button", { name: "Start registration" }));
    unmount();

    const second = render(
      <PatientsPage client={patientsClient()} role="patient" onOpenChart={() => {}} />,
    );
    await screen.findByText("Patients");
    expect(
      screen.queryByRole("button", { name: "Start registration" }),
    ).toBeNull();
    expect(second.container.querySelectorAll("h2")).toHaveLength(1);
  });

  it("Chart: the tab bar is the shared Tabs (no hand-rolled studio-tabs) under one h2", async () => {
    const byType: Record<string, unknown[]> = {
      Condition: [fixtureCondition],
      MedicationRequest: [fixtureMedication],
      Observation: [fixtureObservation],
      DiagnosticReport: [fixtureReport],
      Procedure: [fixtureProcedure],
      Encounter: [fixtureEncounter],
    };
    const client = {
      get: vi.fn(async () => fixturePatient),
      search: vi.fn(async (type: string) => byType[type] ?? []),
    } as unknown as ApiClient;
    const { container } = render(<ChartPage client={client} patientId="pat-1" />);
    expect(
      await screen.findByRole("heading", { name: /Synthetic Sample Testworth/ }),
    ).toBeTruthy();
    expect(container.querySelectorAll("h2")).toHaveLength(1);
    expect(container.querySelector(".tabs-bar")).toBeTruthy();
    expect(container.querySelector(".studio-tabs")).toBeNull();
    // Print wristband is the header primary action now.
    expect(
      screen.getByRole("button", { name: /Print wristband/ }),
    ).toBeTruthy();
  });

  it("Portal: header primary switches to the Appointments & visits tab", async () => {
    const client = {
      myResults: vi.fn(async () => []),
      search: vi.fn(async () => []),
      myMessages: vi.fn(async () => ({ items: [], total: 0 })),
      messageThread: vi.fn(async () => ({ messages: [] })),
      sendPortalMessage: vi.fn(async () => ({})),
      replyToThread: vi.fn(async () => ({})),
      markThreadRead: vi.fn(async () => ({})),
      myAppointments: vi.fn(async () => ({ items: [], total: 0 })),
      selfSchedulingConfig: vi.fn(async () => ({
        auto_confirm: false,
        slot_types: ["follow-up"],
      })),
      requestAppointment: vi.fn(async () => ({})),
      telehealthWorkspace: vi.fn(async () => ({})),
      submitTelehealthIntake: vi.fn(async () => ({})),
    } as unknown as ApiClient;
    const { container } = render(<PortalPage client={client} />);
    expect(await screen.findByText("No results yet.")).toBeTruthy();
    expect(container.querySelectorAll("h2")).toHaveLength(1);
    fireEvent.click(
      screen.getByRole("button", { name: "Request an appointment" }),
    );
    expect(await screen.findByText("My appointments")).toBeTruthy();
    expect(await screen.findByText("My virtual visits")).toBeTruthy();
  });
});

const VIEWER_SESSION: Session = {
  token: "test-token",
  role: "viewer",
  username: "dev-viewer",
};

function clinicClient(): ApiClient {
  return {
    effectiveConfig: vi.fn(async () => {
      throw new Error("not published");
    }),
    clinicDay: vi.fn(async () => ({
      date: "2026-10-18",
      sessions: [{
        code: "am-clinic", display: "Morning clinic",
        start_time: "09:00", booked: 1, capacity: 10, overbooked: 0,
      }],
      appointments: [],
      arrivals: [{
        appointment_id: "appt-1", patient_ref: "Patient/pat-1",
        start: "2026-10-18T09:00:00Z", status: "booked",
        session: "am-clinic",
      }],
      rooming_queue: [],
    })),
    noShowRisks: vi.fn(async () => []),
    referralLoop: vi.fn(async () => []),
    careGaps: vi.fn(async () => []),
    continuumInbox: vi.fn(async () => ({ items: [], total: 0, breached: 0 })),
    clinicCheckIn: vi.fn(async () => ({
      appointment_id: "appt-1", status: "arrived", patient: "Patient/pat-1",
    })),
    clinicBook: vi.fn(async () => ({})),
  } as unknown as ApiClient;
}

describe("ClinicDayPage tabs (P-7 + landing = your queue)", () => {
  it("lands on Arrivals & rooming (the check-in queue) by default", async () => {
    const client = clinicClient();
    render(
      <RuntimeConfigProvider client={client} session={VIEWER_SESSION}>
        <ClinicDayPage client={client} role="viewer" />
      </RuntimeConfigProvider>,
    );
    expect(
      await screen.findByText("Arrivals — awaiting check-in (1)"),
    ).toBeTruthy();
    // Sessions are one tab away, with the booking form intact.
    fireEvent.click(screen.getByRole("tab", { name: "Sessions & booking" }));
    expect(await screen.findByText("Book into a session")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Sessions" })).toBeTruthy();
    expect(screen.getByLabelText("Booking session")).toBeTruthy();
  });

  it("a registration handoff lands on Sessions & booking with the patient pre-filled", async () => {
    const client = clinicClient();
    render(
      <RuntimeConfigProvider client={client} session={VIEWER_SESSION}>
        <ClinicDayPage client={client} role="viewer" initialPatient="pat-9" />
      </RuntimeConfigProvider>,
    );
    expect(await screen.findByText("Book into a session")).toBeTruthy();
    const input = screen.getByLabelText("Booking patient id") as HTMLInputElement;
    expect(input.value).toBe("pat-9");
  });

  it("booking with an empty patient id shows an inline error and never calls the API", async () => {
    const client = clinicClient();
    render(
      <RuntimeConfigProvider client={client} session={VIEWER_SESSION}>
        <ClinicDayPage client={client} role="viewer" />
      </RuntimeConfigProvider>,
    );
    await screen.findByText("Arrivals — awaiting check-in (1)");
    fireEvent.click(screen.getByRole("tab", { name: "Sessions & booking" }));
    await screen.findByText("Book into a session");
    fireEvent.click(screen.getByRole("button", { name: "Book" }));
    expect(
      await screen.findByText("Patient id is required to book an appointment."),
    ).toBeTruthy();
    expect(client.clinicBook).not.toHaveBeenCalled();
  });
});

describe("SchedulingPage / PopHealthPage tab smoke", () => {
  it("Scheduling: a role without access.read gets only the Appointments tab", async () => {
    const client = {
      search: vi.fn(async () => []),
      effectiveConfig: vi.fn(async () => {
        throw new Error("not published");
      }),
    } as unknown as ApiClient;
    render(<SchedulingPage client={client} role="patient" />);
    expect(await screen.findByRole("tab", { name: "Appointments" })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: "Referrals" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "Waitlist" })).toBeNull();
  });

  it("PopHealth: tabs switch panels and the explorer validates the lab threshold inline", async () => {
    const client = {
      registries: vi.fn(async () => []),
      qualityMeasures: vi.fn(async () => ({ measures: [], total: 0 })),
      warehouseStatus: vi.fn(async () => ({
        outbox: { pending: 0, processed: 0, dead_letter: 0 },
        mapping_coverage: {
          coverage_pct: null, mapped_rows: 0, total_rows: 0,
          unmapped_rows: 0, tables: {},
        },
        warehouse: {},
        dead_letters: [],
      })),
      exploreCohort: vi.fn(async () => ({ count: 0, suppressed: false })),
    } as unknown as ApiClient;
    render(<PopHealthPage client={client} role="clinician" />);
    expect(
      await screen.findByText(/No published cohort registries/),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Cohort explorer" }));
    fireEvent.change(screen.getByPlaceholderText("e.g. 4548-4"), {
      target: { value: "4548-4" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Count cohort" }));
    expect(
      await screen.findByText("A threshold is required when a lab code is given."),
    ).toBeTruthy();
    expect(client.exploreCohort).not.toHaveBeenCalled();
  });
});
