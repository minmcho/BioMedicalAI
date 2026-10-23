import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { fixtureCensus, fixtureEdBoard } from "../test/fixtures";
import { AftercarePage } from "./AftercarePage";
import { BedBoardPage } from "./BedBoardPage";
import { EdBoardPage } from "./EdBoardPage";
import { pharmacyLandingTab } from "./PharmacyPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// Home & Module UX pass (WS5b): ModuleHeader adoption smoke — every
// adopted page renders exactly ONE <h2> (its ModuleHeader title) and
// the purpose line, per spec P-1. BedBoard/EdBoard/Aftercare stand in
// for the cluster; the remaining adopted pages share the same
// component and are covered by their own suites' heading assertions.

function headings(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll("h2")).map(
    (h) => h.textContent ?? "",
  );
}

describe("ModuleHeader adoption (WS5b)", () => {
  it("EdBoardPage: one h2, the module title, and its purpose line", async () => {
    const client = {
      edBoard: vi.fn(async () => fixtureEdBoard),
    } as unknown as ApiClient;
    const { container } = render(<EdBoardPage client={client} role="viewer" />);
    expect(await screen.findByText(/Chest pain/)).toBeTruthy();
    expect(headings(container)).toEqual(["Emergency department board"]);
    expect(screen.getByText(/Live ED tracking/)).toBeTruthy();
  });

  it("AftercarePage: one h2, the module title, and its purpose line", async () => {
    const client = {
      aftercareCalls: vi.fn(async () => []),
      readmissionRisks: vi.fn(async () => ({ items: [], basis: "test basis" })),
      hospitalAtHome: vi.fn(async () => ({
        total: 0, census: [], eligible: [], note: "workflow only",
      })),
    } as unknown as ApiClient;
    const { container } = render(<AftercarePage client={client} role="viewer" />);
    expect(
      await screen.findByText(/No post-discharge calls outstanding/),
    ).toBeTruthy();
    expect(headings(container)).toEqual(["Aftercare"]);
    expect(screen.getByText(/Post-discharge follow-up:/)).toBeTruthy();
  });

  it("BedBoardPage: one h2, the module title, and its purpose line", async () => {
    const client = {
      inpatientCensus: vi.fn(async () => fixtureCensus),
      inpatientLocations: vi.fn(async () => ({ locations: [], version: 1 })),
      locationsDraftStatus: vi.fn(async () => ({ published_version: 1, draft: null })),
      request: vi.fn(async () => ({ version: 1, regions: [] })),
    } as unknown as ApiClient;
    const { container } = render(<BedBoardPage client={client} role="viewer" />);
    expect(await screen.findByTestId("census-totals")).toBeTruthy();
    expect(headings(container)).toEqual(["Bed board"]);
    expect(screen.getByText(/Live bed state by unit/)).toBeTruthy();
  });
});

describe("pharmacyLandingTab (N-9 rule)", () => {
  it("verification-capable roles land on the verification queue", () => {
    expect(pharmacyLandingTab(true)).toBe("verification");
  });

  it("all other roles keep the dispense default", () => {
    expect(pharmacyLandingTab(false)).toBe("dispense");
  });
});
