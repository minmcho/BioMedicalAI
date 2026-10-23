import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { VerificationItem } from "../api/types";
import {
  fixtureLabCatalog,
  fixtureLabOrder,
  fixtureQcRun,
  fixtureSpecimen,
  fixtureVerificationItem,
} from "../test/fixtures";
import { LabPage } from "./LabPage";

// Critical-value notification UI (realignment group E): released
// critical reports list their notification state; permitted roles can
// record the phone notification (read-back required) and acknowledge.

afterEach(cleanup);

const RELEASED_CRITICAL: VerificationItem = {
  ...fixtureVerificationItem,
  report: { ...fixtureVerificationItem.report, id: "rep-crit-1", status: "final" },
  order_id: "ord-lab-9",
  order_status: "released",
  stage: "released",
  notification: {
    state: "pending",
    notified_to: null,
    read_back: null,
    notified_by: null,
    notified_at: null,
    acknowledged_by: null,
    acknowledged_at: null,
  },
};

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    labCatalog: vi.fn(async () => fixtureLabCatalog),
    labOrders: vi.fn(async () => [fixtureLabOrder]),
    verificationQueue: vi.fn(async () => ({ items: [fixtureVerificationItem], total: 1 })),
    labSpecimens: vi.fn(async () => [fixtureSpecimen]),
    qcRuns: vi.fn(async () => [fixtureQcRun]),
    criticalReports: vi.fn(async () => ({ items: [RELEASED_CRITICAL], total: 1 })),
    notifyLabReport: vi.fn(async () => ({
      report_id: "rep-crit-1",
      notification: { ...RELEASED_CRITICAL.notification, state: "notified" },
    })),
    acknowledgeLabReport: vi.fn(async () => ({
      report_id: "rep-crit-1",
      notification: { ...RELEASED_CRITICAL.notification, state: "acknowledged" },
    })),
    verifyLabReport: vi.fn(async () => ({})),
    releaseLabReport: vi.fn(async () => ({})),
    transitionLabOrder: vi.fn(async () => ({})),
    createLabOrder: vi.fn(async () => fixtureLabOrder),
    enterLabResults: vi.fn(async () => ({})),
    ingestLab: vi.fn(async () => ({ ingested: [], report_count: 0, result_count: 0 })),
    createQcRun: vi.fn(async () => fixtureQcRun),
    ...overrides,
  } as unknown as ApiClient;
}

// Depth pass: the lab page renders one tab panel at a time; the
// critical-results section lives on the Verification tab.
function clickTab(name: string) {
  fireEvent.click(screen.getByRole("tab", { name }));
}

describe("LabPage critical results", () => {
  it("shows the pending notification state in the queue and the critical list", async () => {
    render(<LabPage client={stubClient()} role="clinician" />);
    clickTab("Verification");
    expect(await screen.findByText("Critical results")).toBeTruthy();
    expect(screen.getAllByText(/Notification: pending/).length).toBeGreaterThanOrEqual(1);
    expect(
      screen.getByRole("button", { name: "Record notification" }),
    ).toBeTruthy();
  });

  it("records a notification only with a named recipient and read-back", async () => {
    const client = stubClient();
    render(<LabPage client={client} role="clinician" />);
    clickTab("Verification");
    const button = await screen.findByRole("button", { name: "Record notification" });
    expect((button as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(
      screen.getByLabelText("Notified person for report rep-crit-1"),
      { target: { value: "Dr A (attending)" } },
    );
    expect((button as HTMLButtonElement).disabled).toBe(true); // still no read-back
    fireEvent.click(screen.getByLabelText("Read-back confirmed for report rep-crit-1"));
    expect((button as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(button);
    await waitFor(() =>
      expect(client.notifyLabReport).toHaveBeenCalledWith("rep-crit-1", {
        notifiedTo: "Dr A (attending)",
        readBack: true,
      }),
    );
  });

  it("offers acknowledgement once notified, with the audit details shown", async () => {
    const notified: VerificationItem = {
      ...RELEASED_CRITICAL,
      notification: {
        state: "notified",
        notified_to: "Dr A (attending)",
        read_back: true,
        notified_by: "dev-clinician",
        notified_at: "2026-01-15T11:00:00Z",
        acknowledged_by: null,
        acknowledged_at: null,
      },
    };
    const client = stubClient({
      criticalReports: vi.fn(async () => ({ items: [notified], total: 1 })),
    });
    render(<LabPage client={client} role="clinician" />);
    clickTab("Verification");
    expect(await screen.findByText(/notified Dr A \(attending\) by dev-clinician/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() =>
      expect(client.acknowledgeLabReport).toHaveBeenCalledWith("rep-crit-1"),
    );
  });

  it("viewers see the state but no notification actions", async () => {
    render(<LabPage client={stubClient()} role="viewer" />);
    clickTab("Verification");
    expect(await screen.findByText("Critical results")).toBeTruthy();
    expect(screen.getByText(/Notification is clinician\/admin only/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Record notification" })).toBeNull();
  });
});
