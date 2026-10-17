import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { fixtureMessageThreads, fixtureThreadDetail } from "../test/fixtures";
import { InboxPage } from "./InboxPage";

// Phase 9: the staff inbox carries the patient's secure-message
// threads next to their results (same FHIR Communication threads the
// patient sees in the portal).
afterEach(cleanup);

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    inbox: vi.fn(async () => []),
    messageInbox: vi.fn(async () => ({
      items: fixtureMessageThreads,
      total: fixtureMessageThreads.length,
      patient: "Patient/pat-1",
    })),
    messageThread: vi.fn(async () => fixtureThreadDetail),
    replyToThread: vi.fn(async () => ({})),
    markThreadRead: vi.fn(async () => ({ thread_id: "thread-1", marked: 2 })),
    sendMessage: vi.fn(async () => ({})),
    ...overrides,
  } as unknown as ApiClient;
}

describe("InboxPage (Phase 9 messages)", () => {
  it("renders the patient's message threads with unread counts", async () => {
    render(<InboxPage client={stubClient()} />);
    expect(await screen.findByText("Message thread")).toBeTruthy();
    expect(screen.getByText(/1 unread/)).toBeTruthy();
    expect(screen.getByText("Your result is ready")).toBeTruthy();
  });

  it("opening a thread marks it read and shows the conversation", async () => {
    const client = stubClient();
    render(<InboxPage client={client} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));
    await waitFor(() =>
      expect(client.markThreadRead).toHaveBeenCalledWith("thread-1"),
    );
    expect(await screen.findByText("Question about my lab")).toBeTruthy();
  });

  it("staff can reply in the thread and start a new thread", async () => {
    const client = stubClient();
    render(<InboxPage client={client} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));
    await screen.findByText("Question about my lab");
    fireEvent.change(screen.getByLabelText("Reply"), {
      target: { value: "It posted this morning" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send reply" }));
    await waitFor(() =>
      expect(client.replyToThread).toHaveBeenCalledWith(
        "thread-1",
        "It posted this morning",
      ),
    );

    fireEvent.change(screen.getByLabelText("New message to patient"), {
      target: { value: "Care-plan update" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() =>
      expect(client.sendMessage).toHaveBeenCalledWith("pat-1", "Care-plan update"),
    );
  });
});
