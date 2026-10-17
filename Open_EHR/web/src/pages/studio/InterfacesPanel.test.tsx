import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import type {
  InteropEndpointsResponse,
  InteropMessage,
} from "../../api/types";
import { InterfacesPanel } from "./InterfacesPanel";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// Phase 8: the interface-engine panel under the Studio Integration
// tab — endpoint status from the published config, the message log,
// dispatcher runs, and dead-letter replay. Stub client only (the
// network boundary); the backend behaviour is covered by the Python
// MLLP socket suites.

const endpointsFixture: InteropEndpointsResponse = {
  config: { kind: "integration", key: "hl7-endpoints", version: 2 },
  facility: { sendingApplication: "OPENCARE", sendingFacility: "OPENCARE HOSPITAL" },
  retry: { maxAttempts: 3, backoffSeconds: 30 },
  endpoints: [
    {
      name: "downstream-ehr", host: "127.0.0.1", port: 2575,
      messageTypes: ["ADT", "ORM", "ORU"], enabled: true,
      queue: { pending: 2, sent: 5, dead_letter: 1 },
    },
    {
      name: "public-health", host: "127.0.0.1", port: 2576,
      messageTypes: ["ORU"], enabled: false,
      queue: { pending: 0, sent: 0, dead_letter: 0 },
    },
  ],
};

const deadLetter: InteropMessage = {
  direction: "outbound", id: 7, message_type: "ADT^A01", status: "dead_letter",
  patient_ref: "Patient/pat-1", endpoint: "downstream-ehr", attempts: 3,
  replay_count: 0, last_error: "ConnectionRefusedError: refused",
  created_at: "2026-10-17T08:00:00Z",
};

const recentMessage: InteropMessage = {
  direction: "outbound", id: 8, message_type: "ORU^R01", status: "sent",
  patient_ref: "Patient/pat-2", endpoint: "downstream-ehr",
  created_at: "2026-10-17T09:00:00Z",
};

function stubClient(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    interopEndpoints: vi.fn(async () => endpointsFixture),
    interopMessages: vi.fn(async () => ({ items: [recentMessage], count: 1 })),
    interopDeadLetters: vi.fn(async () => ({ items: [deadLetter], count: 1 })),
    dispatchInterop: vi.fn(async () => ({
      stats: { attempted: 2, sent: 2, failed: 0, dead_lettered: 0, skipped: 0 },
    })),
    replayDeadLetter: vi.fn(async () => ({ ...deadLetter, status: "pending" })),
    ...overrides,
  } as unknown as ApiClient;
}

describe("InterfacesPanel", () => {
  it("renders endpoint status, queues, dead letters and the log", async () => {
    render(<InterfacesPanel client={stubClient()} />);
    // The endpoint name recurs across the three tables (endpoints,
    // dead letters, message log) — assert presence, not uniqueness.
    expect((await screen.findAllByText("downstream-ehr")).length)
      .toBeGreaterThanOrEqual(3);
    expect(screen.getByText("public-health")).toBeTruthy();
    expect(screen.getByText("enabled")).toBeTruthy();
    expect(screen.getByText("disabled")).toBeTruthy();
    expect(screen.getByText("2 / 5 / 1")).toBeTruthy();
    expect(screen.getByText("ADT^A01")).toBeTruthy();
    expect(screen.getByText("ORU^R01")).toBeTruthy();
    expect(screen.getByText("ConnectionRefusedError: refused")).toBeTruthy();
  });

  it("runs a dispatcher pass and reports the stats", async () => {
    const client = stubClient();
    render(<InterfacesPanel client={client} />);
    await screen.findAllByText("downstream-ehr");
    fireEvent.click(screen.getByText("Run dispatcher pass"));
    await waitFor(() =>
      expect(screen.getByText(/2 attempted, 2 sent/)).toBeTruthy(),
    );
    expect(client.dispatchInterop).toHaveBeenCalledTimes(1);
  });

  it("replays a dead letter", async () => {
    const client = stubClient();
    render(<InterfacesPanel client={client} />);
    await screen.findAllByText("downstream-ehr");
    fireEvent.click(screen.getByText("Replay"));
    await waitFor(() =>
      expect(client.replayDeadLetter).toHaveBeenCalledWith(7),
    );
  });
});
