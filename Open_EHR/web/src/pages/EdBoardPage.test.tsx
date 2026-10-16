import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { fixtureEdBoard } from "../test/fixtures";
import { EdBoardPage } from "./EdBoardPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    edBoard: vi.fn(async () => fixtureEdBoard),
    edArrive: vi.fn(async () => ({ resourceType: "Encounter", id: "enc-new" })),
    edTransition: vi.fn(async () => ({ resourceType: "Encounter", id: "enc-ed-1" })),
    edAdmit: vi.fn(async () => ({
      ed_encounter: { resourceType: "Encounter", id: "enc-ed-3", status: "admitted" },
      inpatient_encounter: { resourceType: "Encounter", id: "enc-inp-1", status: "in-progress" },
    })),
    edDepart: vi.fn(async () => ({
      ed_encounter: { resourceType: "Encounter", id: "enc-ed-3", status: "departed" },
    })),
    ...overrides,
  } as unknown as ApiClient;
}

describe("EdBoardPage", () => {
  it("renders the census grouped by state with acuity badges and server wait times", async () => {
    render(<EdBoardPage client={stubClient()} role="clinician" />);
    expect(await screen.findByText(/Chest pain/)).toBeTruthy();
    expect(screen.getByText("wait 85 min")).toBeTruthy();
    expect(screen.getByText("ESI 2")).toBeTruthy();
    expect(screen.getByText(/Waiting \(1\)/)).toBeTruthy();
    expect(screen.getByText(/Disposition \(1\)/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Triage" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Admit" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Depart" })).toBeTruthy();
  });

  it("triages with the selected ESI acuity through the client", async () => {
    const client = stubClient();
    render(<EdBoardPage client={client} role="clinician" />);
    fireEvent.click(await screen.findByRole("button", { name: "Triage" }));
    await waitFor(() => {
      expect(client.edTransition).toHaveBeenCalledWith("enc-ed-1", "triage", 3);
    });
  });

  it("admits from disposition through the governed endpoint", async () => {
    const client = stubClient();
    render(<EdBoardPage client={client} role="admin" />);
    fireEvent.click(await screen.findByRole("button", { name: "Admit" }));
    await waitFor(() => {
      expect(client.edAdmit).toHaveBeenCalledWith("enc-ed-3");
    });
  });

  it("viewer sees the census but no actions and no arrival form", async () => {
    render(<EdBoardPage client={stubClient()} role="viewer" />);
    expect(await screen.findByText(/Chest pain/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Triage" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Admit" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Register arrival" })).toBeNull();
  });
});
