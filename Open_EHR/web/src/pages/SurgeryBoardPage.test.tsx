import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import type { FormDefinition } from "../api/types";
import {
  fixtureAnaesthesiaForm,
  fixtureRooms,
  fixtureSurgeryBoard,
  fixtureSurgeryCaseRecord,
} from "../test/fixtures";
import { SurgeryBoardPage } from "./SurgeryBoardPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    surgeryBoard: vi.fn(async () => fixtureSurgeryBoard),
    surgeryRooms: vi.fn(async () => fixtureRooms.rooms),
    scheduleCase: vi.fn(async () => ({
      case: fixtureSurgeryBoard.rooms[0].cases[0].case,
      appointment: fixtureSurgeryBoard.rooms[0].cases[0].appointment,
      room: "OR-1",
      state: "scheduled",
    })),
    transitionCase: vi.fn(async () => ({
      case: { ...fixtureSurgeryBoard.rooms[0].cases[0].case, status: "pre-op" },
      appointment: fixtureSurgeryBoard.rooms[0].cases[0].appointment,
      room: "OR-1",
      state: "pre-op",
    })),
    rescheduleCase: vi.fn(async () => ({
      case: fixtureSurgeryBoard.rooms[0].cases[0].case,
      appointment: fixtureSurgeryBoard.rooms[0].cases[0].appointment,
      room: "OR-1",
      state: "scheduled",
    })),
    surgeryCaseRecord: vi.fn(async () => fixtureSurgeryCaseRecord),
    effectiveConfig: vi.fn(async () => ({
      kind: "form",
      key: "anaesthesia-record",
      version: 1,
      status: "published",
      payload: fixtureAnaesthesiaForm,
    })),
    submitPreopChecklist: vi.fn(async () => ({})),
    submitAnaesthesia: vi.fn(async () => ({})),
    submitAnaesthesiaVitals: vi.fn(async () => ({})),
    // Depth pass (Workstream 2): WHO safety checklist surface.
    submitSafetyChecklist: vi.fn(async () => ({
      case: null,
      section: "sign-in",
      safety_checklist: { "sign-in": true, "time-out": false, "sign-out": false },
    })),
    ...overrides,
  } as unknown as ApiClient;
}

// Depth pass (Workstream 2): the governed WHO checklist form as the
// server seeds it — three section groups of required booleans.
const fixtureSafetyForm: FormDefinition = {
  resourceType: "Questionnaire",
  title: "WHO surgical safety checklist",
  item: [
    {
      linkId: "sign-in", text: "Sign In", type: "group",
      item: [
        { linkId: "signin-identity-site-procedure", text: "Patient identity, site and procedure confirmed", type: "boolean", required: true },
        { linkId: "signin-consent", text: "Consent confirmed", type: "boolean", required: true },
        { linkId: "signin-anaesthesia-check", text: "Anaesthesia safety check complete", type: "boolean", required: true },
        { linkId: "signin-allergies", text: "Allergies reviewed", type: "boolean", required: true },
        { linkId: "signin-airway-risk", text: "Airway / aspiration risk assessed", type: "boolean", required: true },
      ],
    },
    {
      linkId: "time-out", text: "Time Out", type: "group",
      item: [
        { linkId: "timeout-introductions", text: "Team members introduced by name and role", type: "boolean", required: true },
      ],
    },
    {
      linkId: "sign-out", text: "Sign Out", type: "group",
      item: [
        { linkId: "signout-procedure-recorded", text: "Name of the procedure recorded", type: "boolean", required: true },
      ],
    },
  ],
};

function safetyClient(overrides: Record<string, unknown> = {}): ApiClient {
  return stubClient({
    effectiveConfig: vi.fn(async (_kind: string, key: string) => ({
      kind: "form",
      key,
      version: 1,
      status: "published",
      payload: key === "surgical-safety-checklist"
        ? fixtureSafetyForm
        : fixtureAnaesthesiaForm,
    })),
    ...overrides,
  });
}

describe("SurgeryBoardPage", () => {
  it("renders the day grouped by room with case states and next-step actions", async () => {
    render(<SurgeryBoardPage client={stubClient()} role="clinician" />);
    expect(await screen.findByText("Total knee arthroplasty")).toBeTruthy();
    expect(screen.getByText(/OR-1 — Operating Room 1 \(1\)/)).toBeTruthy();
    expect(screen.getByText(/OR-2 — Operating Room 2 \(0\)/)).toBeTruthy();
    expect(screen.getByText("scheduled")).toBeTruthy();
    expect(screen.getByRole("button", { name: "pre-op" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "cancelled" })).toBeTruthy();
  });

  it("advances a case through the client", async () => {
    const client = stubClient();
    render(<SurgeryBoardPage client={client} role="clinician" />);
    fireEvent.click(await screen.findByRole("button", { name: "pre-op" }));
    await waitFor(() => {
      expect(client.transitionCase).toHaveBeenCalledWith("case-1", "pre-op");
    });
  });

  it("schedules a case through the client", async () => {
    const client = stubClient();
    render(<SurgeryBoardPage client={client} role="clinician" />);
    await screen.findByText("Total knee arthroplasty");
    fireEvent.change(screen.getByLabelText("Case patient id"), { target: { value: "pat-9" } });
    fireEvent.change(screen.getByLabelText("Procedure code"), { target: { value: "27130" } });
    fireEvent.change(screen.getByLabelText("Case start"), { target: { value: "2026-10-20T13:00" } });
    fireEvent.change(screen.getByLabelText("Case end"), { target: { value: "2026-10-20T15:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Schedule case" }));
    await waitFor(() => {
      expect(client.scheduleCase).toHaveBeenCalledWith(expect.objectContaining({
        patientId: "pat-9", code: "27130", room: "OR-1",
      }));
    });
  });

  it("surfaces a room double-booking conflict (409) as an inline error", async () => {
    const client = stubClient({
      scheduleCase: vi.fn(async () => {
        throw new ApiError(409, "Room OR-1 is already booked over that time (Appointment/appt-1)");
      }),
    });
    render(<SurgeryBoardPage client={client} role="clinician" />);
    await screen.findByText("Total knee arthroplasty");
    fireEvent.change(screen.getByLabelText("Case patient id"), { target: { value: "pat-9" } });
    fireEvent.change(screen.getByLabelText("Procedure code"), { target: { value: "27130" } });
    fireEvent.change(screen.getByLabelText("Case start"), { target: { value: "2026-10-20T09:00" } });
    fireEvent.change(screen.getByLabelText("Case end"), { target: { value: "2026-10-20T11:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Schedule case" }));
    expect(await screen.findByText(/already booked over that time/)).toBeTruthy();
  });

  it("viewer sees the board but no scheduling or transition actions", async () => {
    render(<SurgeryBoardPage client={stubClient()} role="viewer" />);
    expect(await screen.findByText("Total knee arthroplasty")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Schedule case" })).toBeNull();
    expect(screen.queryByRole("button", { name: "pre-op" })).toBeNull();
  });

  it("renders the case timeline from the board entry (Phase 6)", async () => {
    render(<SurgeryBoardPage client={stubClient()} role="clinician" />);
    expect(await screen.findByText(/Timeline: scheduled/)).toBeTruthy();
  });

  it("case record panel shows pre-op + vitals and submits the anaesthesia form", async () => {
    const client = stubClient();
    render(<SurgeryBoardPage client={client} role="clinician" />);
    await screen.findByText("Total knee arthroplasty");
    fireEvent.click(screen.getByRole("button", { name: "Case record" }));
    expect(await screen.findByText(/Pre-op checklist completed/)).toBeTruthy();
    expect(screen.getByText(/Anaesthesia vitals \(1\)/)).toBeTruthy();
    expect(screen.getByText(/systolic: 118/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Anaesthesia record" }));
    expect(await screen.findByLabelText(/Anaesthesia type/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/Anaesthesia type/), {
      target: { value: "general" },
    });
    fireEvent.change(screen.getByLabelText(/Airway/), {
      target: { value: "ett" },
    });
    fireEvent.change(screen.getByLabelText(/Agents administered/), {
      target: { value: "Propofol, sevoflurane" },
    });
    fireEvent.click(screen.getByRole("button", {
      name: "Submit Anaesthesia record",
    }));
    await waitFor(() => {
      expect(client.submitAnaesthesia).toHaveBeenCalledWith(
        "case-1",
        expect.any(Array),
      );
    });
  });

  // Depth pass (Workstream 2): WHO safety checklist panel — labelled
  // Done/Pending per section, the next section's governed form, and
  // the gate's 409 surfacing inline on transition.
  it("safety panel shows pending sections and records Sign In via the form", async () => {
    const client = safetyClient();
    render(<SurgeryBoardPage client={client} role="clinician" />);
    await screen.findByText("Total knee arthroplasty");
    fireEvent.click(screen.getByRole("button", { name: "Safety checklist" }));
    expect(await screen.findByText(/WHO surgical safety checklist/)).toBeTruthy();
    expect(screen.getByText("Sign In:")).toBeTruthy();
    expect(screen.getAllByText("Pending").length).toBe(3);
    fireEvent.click(screen.getByRole("button", { name: "Record Sign In" }));
    expect(await screen.findByLabelText(/Patient identity/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText(/Patient identity/));
    fireEvent.click(screen.getByLabelText(/Consent confirmed/));
    fireEvent.click(screen.getByRole("button", { name: "Submit Sign In" }));
    await waitFor(() => {
      expect(client.submitSafetyChecklist).toHaveBeenCalledWith(
        "case-1",
        "sign-in",
        expect.arrayContaining([
          expect.objectContaining({ linkId: "signin-consent" }),
        ]),
      );
    });
    expect(await screen.findByText("Done")).toBeTruthy();
  });

  it("surfaces the safety gate 409 inline when a transition is refused", async () => {
    const preopBoard = {
      ...fixtureSurgeryBoard,
      rooms: fixtureSurgeryBoard.rooms.map((room) => ({
        ...room,
        cases: room.cases.map((entry) => ({
          ...entry,
          case: { ...entry.case, status: "pre-op" },
          state: "pre-op",
          safety_checklist: { "sign-in": true, "time-out": false, "sign-out": false },
        })),
      })),
    };
    const client = safetyClient({
      surgeryBoard: vi.fn(async () => preopBoard),
      transitionCase: vi.fn(async () => {
        throw new ApiError(
          409,
          "Surgical safety checklist gate: case case-1 cannot move to "
          + "'in-op' until the 'time-out' section of the WHO surgical "
          + "safety checklist is recorded (missing section: time-out)",
        );
      }),
    });
    render(<SurgeryBoardPage client={client} role="clinician" />);
    await screen.findByText("Total knee arthroplasty");
    fireEvent.click(screen.getByRole("button", { name: "in-op" }));
    expect(await screen.findByText(/missing section: time-out/)).toBeTruthy();
  });
});
