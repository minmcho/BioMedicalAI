import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import {
  fixtureMessageThreads,
  fixtureTelehealthWorkspace,
  fixtureThreadDetail,
} from "../test/fixtures";
import { PortalPage } from "./PortalPage";

// Phase 9 portal sections: secure messaging, my appointments +
// self-scheduling, and the telehealth workspace. The stub client is
// the unit-test boundary; server-side scoping is proven by the
// backend suite (test_phase9_engagement.py).
afterEach(cleanup);

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    myResults: vi.fn(async () => []),
    search: vi.fn(async () => []),
    myMessages: vi.fn(async () => ({
      items: fixtureMessageThreads,
      total: fixtureMessageThreads.length,
    })),
    messageThread: vi.fn(async () => fixtureThreadDetail),
    sendPortalMessage: vi.fn(async () => ({})),
    replyToThread: vi.fn(async () => ({})),
    markThreadRead: vi.fn(async () => ({ thread_id: "thread-1", marked: 1 })),
    myAppointments: vi.fn(async () => ({
      items: [fixtureTelehealthWorkspace.appointment],
      total: 1,
    })),
    selfSchedulingConfig: vi.fn(async () => ({
      auto_confirm: false,
      slot_types: ["new-patient", "follow-up", "procedure", "telehealth"],
    })),
    requestAppointment: vi.fn(async () => ({
      appointment: { resourceType: "Appointment", id: "appt-new", status: "proposed" },
      auto_confirmed: false,
      status: "proposed",
    })),
    telehealthWorkspace: vi.fn(async () => fixtureTelehealthWorkspace),
    submitTelehealthIntake: vi.fn(async () => ({})),
    ...overrides,
  } as unknown as ApiClient;
}

describe("PortalPage (Phase 9 sections)", () => {
  it("renders message threads with unread badges and opens a thread", async () => {
    const client = stubClient();
    render(<PortalPage client={client} />);
    expect(await screen.findByText("Message thread")).toBeTruthy();
    expect(screen.getByText(/1 unread/)).toBeTruthy();
    expect(screen.getByText("Your result is ready")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(await screen.findByText("Question about my lab")).toBeTruthy();
    await waitFor(() => expect(client.messageThread).toHaveBeenCalledWith("thread-1"));
  });

  it("sends a new message to the care team", async () => {
    const client = stubClient();
    render(<PortalPage client={client} />);
    await screen.findByText("Message thread");
    fireEvent.change(screen.getByLabelText(/New message to your care team/), {
      target: { value: "When will my result post?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() =>
      expect(client.sendPortalMessage).toHaveBeenCalledWith(
        "When will my result post?",
      ),
    );
  });

  it("telehealth workspace shows the configured join link and the honest disclaimer", async () => {
    const client = stubClient();
    render(<PortalPage client={client} />);
    fireEvent.click(await screen.findByRole("button", { name: "Visit workspace" }));
    // The disclaimer renders VERBATIM: OpenCare does not provide video.
    expect(
      await screen.findByText(/OpenCare does not provide video/),
    ).toBeTruthy();
    const join = screen.getByRole("link", { name: "Join visit" });
    expect(join.getAttribute("href")).toBe(
      "https://video.example.invalid/opencare/appt-tele-1",
    );
    expect(screen.getByText(/Synthetic Video \(configured provider\)/)).toBeTruthy();
    // The published intake form renders through FormRenderer
    // (required items carry the " *" marker in their labels).
    expect(screen.getByText(/Chief concern/)).toBeTruthy();
    expect(screen.getByText(/Camera and microphone working/)).toBeTruthy();
  });

  it("self-scheduling request reports the staff-confirm default honestly", async () => {
    const client = stubClient();
    render(<PortalPage client={client} />);
    await screen.findByText(/Requests are confirmed by the care team/);
    fireEvent.change(screen.getByLabelText(/Preferred start/), {
      target: { value: "2026-11-05T09:00" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Request appointment" }));
    await waitFor(() => expect(client.requestAppointment).toHaveBeenCalled());
    expect(
      await screen.findByText(/Requested — the practice will confirm/),
    ).toBeTruthy();
  });
});
