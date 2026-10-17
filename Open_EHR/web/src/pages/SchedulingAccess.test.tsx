import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { SchedulingPage } from "./SchedulingPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// Phase 7 access sections (app.access): referral triage queue,
// authorisations, eligibility (labelled local-config), waitlist
// offer/accept. Stub client = network boundary for unit tests only.

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    search: vi.fn(async () => []),
    payers: vi.fn(async () => ({
      payers: {
        concepts: [
          { code: "synthetic-payer", display: "Synthetic Payer" },
          { code: "synthetic-community-health-plan", display: "Synthetic Community Health Plan" },
        ],
      },
      version: 3,
    })),
    referralQueue: vi.fn(async () => [
      {
        resourceType: "ServiceRequest",
        id: "ref-1",
        status: "received",
        code: {
          coding: [{ code: "cardiology", display: "Cardiology referral" }],
          text: "Cardiology referral",
        },
        subject: { reference: "Patient/pat-1" },
        referringOrganization: "Synthetic Referring Clinic",
        taskId: "task-1",
      },
    ]),
    createReferral: vi.fn(),
    transitionReferral: vi.fn(async () => ({})),
    authorisations: vi.fn(async () => [
      {
        id: "auth-1", auth_number: "AUTH-123", patient: "Patient/pat-1",
        payer: "synthetic-payer", service_code: "imaging",
        status: "requested", approved_units: 2, used_units: 0,
      },
    ]),
    createAuthorisation: vi.fn(),
    transitionAuthorisation: vi.fn(async () => ({})),
    checkEligibility: vi.fn(async () => ({
      patient: "Patient/pat-1", payer: "synthetic-payer",
      payer_display: "Synthetic Payer", service_code: "imaging",
      service_date: null, covered: true, copay: 25,
      coinsurance_percent: 20, basis: "local-config",
      disclaimer:
        "Local configuration check only: this is a deterministic evaluation of the published payer configuration. No live payer network (EDI 270/271) was contacted.",
      checked_at: "2026-01-10T12:00:00Z", checked_by: "dev-clinician",
      recorded_id: "elig-1",
    })),
    waitlist: vi.fn(async () => [
      {
        id: "wl-1", patient: "Patient/pat-1", slot_type: "follow-up",
        priority: 10, status: "offered", offered_appointment_id: "appt-9",
        joined_at: "2026-01-10T11:00:00Z",
      },
    ]),
    joinWaitlist: vi.fn(),
    offerWaitlistSlot: vi.fn(),
    acceptWaitlistOffer: vi.fn(async () => ({
      entry: {}, appointment: { id: "appt-new" },
    })),
    transitionWaitlist: vi.fn(),
    transitionAppointment: vi.fn(),
    ...overrides,
  } as unknown as ApiClient;
}

describe("SchedulingPage Phase 7 access sections", () => {
  it("lists the triage queue and transitions a referral", async () => {
    const client = stubClient();
    render(<SchedulingPage client={client} role="clinician" />);
    expect(await screen.findByText("Referrals — triage queue")).toBeTruthy();
    expect(await screen.findByText("Cardiology referral")).toBeTruthy();
    expect(screen.getByText("Synthetic Referring Clinic")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "triaged" }));
    await waitFor(() => {
      expect(client.transitionReferral).toHaveBeenCalledWith("ref-1", "triaged");
    });
  });

  it("approves a requested authorisation", async () => {
    const client = stubClient();
    render(<SchedulingPage client={client} role="clinician" />);
    expect(await screen.findByText("AUTH-123")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => {
      expect(client.transitionAuthorisation).toHaveBeenCalledWith("auth-1", "approved");
    });
  });

  it("runs an eligibility check and shows the local-config label", async () => {
    const client = stubClient();
    render(<SchedulingPage client={client} role="clinician" />);
    expect(await screen.findByText("Eligibility check")).toBeTruthy();
    expect(screen.getByText(/no live payer connection \(no EDI 270\/271\)/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Eligibility patient id"), {
      target: { value: "pat-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Check eligibility" }));
    await waitFor(() => {
      expect(client.checkEligibility).toHaveBeenCalled();
    });
    expect(await screen.findByText(/Covered/)).toBeTruthy();
    expect(screen.getByText(/basis: local-config/)).toBeTruthy();
    expect(screen.getByText(/No live payer network \(EDI 270\/271\) was contacted/)).toBeTruthy();
  });

  it("accepts an offered waitlist slot and books the appointment", async () => {
    const client = stubClient();
    render(<SchedulingPage client={client} role="clinician" />);
    expect(await screen.findByText("Waitlist")).toBeTruthy();
    expect(await screen.findByText("offered")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Accept start for wl-1"), {
      target: { value: "2026-01-12T09:00" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Accept & book" }));
    await waitFor(() => {
      expect(client.acceptWaitlistOffer).toHaveBeenCalledWith(
        "wl-1",
        expect.objectContaining({ start: expect.any(String) }),
      );
    });
  });

  it("hides access sections from roles without access.read", async () => {
    const client = stubClient();
    render(<SchedulingPage client={client} role="patient" />);
    expect(await screen.findByText("Scheduling")).toBeTruthy();
    expect(screen.queryByText("Referrals — triage queue")).toBeNull();
    expect(screen.queryByText("Waitlist")).toBeNull();
  });
});
