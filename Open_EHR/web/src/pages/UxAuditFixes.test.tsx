import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { Session } from "../auth/session";
import { RuntimeConfigProvider } from "../auth/runtimeConfig";
import { ClinicDayPage } from "./ClinicDayPage";
import { HomePage } from "./HomePage";

// RTL auto-cleanup does not run in this repo's vitest setup.
afterEach(cleanup);

// UX audit fixes F-1 (front-desk check-in) and F-2 (Home's lists
// open the chart). Backend behaviour is pinned in
// backend/tests/test_ux_checkin.py; these pin the web surfaces.

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

function homeClient(): ApiClient {
  return {
    homeDashboard: vi.fn(async () => ({
      audience: "clinician",
      basis: "staff",
      staff_id: "staff-001",
      dashboard_key: "k",
      dashboard: { key: "k", title: "Home", widgets: [] },
    })),
    myPatients: vi.fn(async () => ({
      staff_id: "staff-001",
      items: [HOME_ITEM],
      total: 1,
    })),
    myUnitPatients: vi.fn(async () => ({
      unit: null,
      unit_display: null,
      items: [],
      total: 0,
    })),
    request: vi.fn(async () => ({})),
  } as unknown as ApiClient;
}

describe("F-2 — Home's patient lists open the chart", () => {
  it("a My Patients row opens the chart in one click", async () => {
    const onOpenChart = vi.fn();
    render(
      <HomePage
        client={homeClient()}
        fallback={<p>fallback</p>}
        onOpenChart={onOpenChart}
      />,
    );
    const opener = await screen.findByRole("button", {
      name: "Open chart for Ada Lovelace",
    });
    fireEvent.click(opener);
    expect(onOpenChart).toHaveBeenCalledWith("pat-1");
  });

  it("without an opener the list stays display-only (portal-safe)", async () => {
    render(<HomePage client={homeClient()} fallback={<p>fallback</p>} />);
    expect(await screen.findByText(/Ada Lovelace/)).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: /Open chart/ }),
    ).toBeNull();
  });
});

const VIEWER_SESSION: Session = {
  token: "viewer-token",
  role: "viewer",
  username: "dev-viewer",
};

function clinicClient(checkIn: ReturnType<typeof vi.fn>): ApiClient {
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
    clinicCheckIn: checkIn,
  } as unknown as ApiClient;
}

describe("F-1 — front-desk check-in on the Clinic Day board", () => {
  it("renders the arrivals worklist with step 3 current and checks in", async () => {
    const checkIn = vi.fn(async () => ({
      appointment_id: "appt-1", status: "arrived",
      patient: "Patient/pat-1",
    }));
    const client = clinicClient(checkIn);
    render(
      <RuntimeConfigProvider client={client} session={VIEWER_SESSION}>
        <ClinicDayPage client={client} role="viewer" />
      </RuntimeConfigProvider>,
    );
    expect(
      await screen.findByText("Arrivals — awaiting check-in (1)"),
    ).toBeTruthy();
    // The step strip's third step renders as current HERE — the
    // screen the front desk actually works (F-1's dead end, closed).
    expect(screen.getByText("3 · Check in (current)")).toBeTruthy();
    // The front-desk role (registration.work in the default matrix)
    // sees the Check in action on its own board.
    fireEvent.click(screen.getByRole("button", { name: "Check in" }));
    expect(await screen.findByText("Patient checked in — done.")).toBeTruthy();
    expect(checkIn).toHaveBeenCalledWith("appt-1");
  });
});
