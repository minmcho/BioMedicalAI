import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { PortalPage } from "./PortalPage";

// Phase 15 closure (BD-28): the portal "My virtual visits" section —
// the patient's own visits list (GET /portal/telehealth/visits) with
// the governed outcome form on a completed visit.
afterEach(cleanup);

const OUTCOME_FORM = {
  title: "Telehealth visit outcome",
  item: [
    { linkId: "outcome-summary", text: "How did the visit go?", type: "text", required: true },
    { linkId: "goals-met", text: "Were your goals met?", type: "boolean", required: true },
    { linkId: "follow-up-needed", text: "Follow-up needed?", type: "boolean" },
  ],
};

const VISIT = {
  id: "1",
  visit_id: "visit-1",
  appointment_id: "appt-1",
  appointment_ref: "Appointment/appt-1",
  patient: "Patient/pat-1",
  patient_ref: "Patient/pat-1",
  service_line: "general",
  status: "completed",
  state: "completed",
  interpreter_request_id: null,
  interpreter_display: null,
  timestamps: {},
  state_history: [],
  outcome_response_id: null,
  outcome_submitted: false,
  created_by: "clinician",
  created_at: null,
  updated_at: null,
};

function stubClient() {
  const request = vi.fn(async (path: string, init?: { method?: string }) => {
    if (path === "/portal/telehealth/visits") {
      return { items: [VISIT], total: 1, patient: "Patient/pat-1" };
    }
    if (path === "/portal/telehealth/visits/visit-1/outcome" && init?.method === "POST") {
      return { visit: { ...VISIT, outcome_submitted: true }, outcome: {}, form: "telehealth-outcome" };
    }
    throw new Error(`unexpected request ${path}`);
  });
  return {
    request,
    myResults: vi.fn(async () => []),
    search: vi.fn(async () => []),
    effectiveConfig: vi.fn(async () => ({ payload: OUTCOME_FORM })),
    // The portal's other sections (Phase 9) — minimal empty fixtures.
    myMessages: vi.fn(async () => ({ items: [], total: 0 })),
    messageThread: vi.fn(async () => ({ items: [] })),
    sendPortalMessage: vi.fn(async () => ({})),
    replyToThread: vi.fn(async () => ({})),
    markThreadRead: vi.fn(async () => ({ thread_id: "t", marked: 0 })),
    myAppointments: vi.fn(async () => ({ items: [], total: 0 })),
    selfSchedulingConfig: vi.fn(async () => ({ auto_confirm: false, slot_types: [] })),
    requestAppointment: vi.fn(async () => ({})),
    telehealthWorkspace: vi.fn(async () => ({})),
    submitTelehealthIntake: vi.fn(async () => ({})),
  } as unknown as ApiClient & { request: ReturnType<typeof vi.fn> };
}


// UX pass: virtual visits live on the Appointments & visits tab —
// open it first; every assertion below is unchanged.
async function openVisitsTab() {
  fireEvent.click(await screen.findByRole("tab", { name: "Appointments & visits" }));
}

describe("PortalPage — My virtual visits (BD-28 closure)", () => {
  it("lists the patient's visits and submits the governed outcome form", async () => {
    const client = stubClient();
    render(<PortalPage client={client} />);
    await openVisitsTab();
    expect(await screen.findByText("My virtual visits")).toBeTruthy();
    expect(await screen.findByText("Virtual visit visit-1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Submit outcome" }));
    // The governed form renders (fetched via effectiveConfig).
    const summary = await screen.findByLabelText(/How did the visit go\?/);
    fireEvent.change(summary, { target: { value: "Felt much better" } });
    const goals = screen.getByLabelText(/Were your goals met\?/);
    fireEvent.click(goals);
    // Two "Submit outcome" buttons now exist (the row toggle + the
    // form's submit); the submit is the last one.
    const submits = screen.getAllByRole("button", { name: "Submit outcome" });
    fireEvent.click(submits[submits.length - 1]);
    await waitFor(() =>
      expect(client.request).toHaveBeenCalledWith(
        "/portal/telehealth/visits/visit-1/outcome",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    expect(await screen.findByText(/Outcome submitted/)).toBeTruthy();
  });

  it("a visit with an outcome already submitted offers no second outcome", async () => {
    const client = stubClient();
    (client.request as ReturnType<typeof vi.fn>).mockImplementation(
      async (path: string) => {
        if (path === "/portal/telehealth/visits") {
          return {
            items: [{ ...VISIT, outcome_submitted: true, outcome_response_id: "qr-1" }],
            total: 1,
            patient: "Patient/pat-1",
          };
        }
        throw new Error(`unexpected request ${path}`);
      },
    );
    render(<PortalPage client={client} />);
    await openVisitsTab();
    expect(await screen.findByText(/outcome submitted/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Submit outcome" })).toBeNull();
  });
});
