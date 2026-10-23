import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { parsePermissions } from "./studio/models";
import { resolveNavGroups } from "../auth/session";
import { HomePage } from "./HomePage";

// Home & Module UX pass: the card-menu Home. Cards lead the page
// (one per module from the governed resolution), live indicators
// come from client.homeCards(), and the existing role content
// (quick actions, My patients / My unit, dashboard) stays below.

afterEach(cleanup);

const MATRIX = parsePermissions({});
const CLINICIAN_GROUPS = resolveNavGroups("clinician", null, MATRIX);
const PATIENT_GROUPS = resolveNavGroups("patient", null, MATRIX);

function cardClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    homeDashboard: vi.fn(async () => ({
      audience: "clinician",
      basis: "staff",
      staff_id: "staff-001",
      dashboard_key: "k",
      dashboard: { key: "k", title: "Home", widgets: [] },
    })),
    myPatients: vi.fn(async () => ({ staff_id: "staff-001", items: [], total: 0 })),
    myUnitPatients: vi.fn(async () => ({
      unit: null,
      unit_display: null,
      items: [],
      total: 0,
    })),
    homeCards: vi.fn(async () => [
      {
        module: "lab",
        value: 3,
        label: "critical results open",
        status_text: "Needs attention",
        tone: "bad",
        timer_text: "Oldest 42m",
      },
      {
        module: "beds",
        value: "92%",
        label: "occupancy",
        status_text: "Busy",
        tone: "warn",
      },
    ]),
    ...overrides,
  } as unknown as ApiClient;
}

describe("HomePage card menu", () => {
  it("leads with one card per resolved module, grouped by the regrouped labels", async () => {
    render(
      <HomePage
        client={cardClient()}
        fallback={<p>fallback</p>}
        onNavigate={() => {}}
        navGroups={CLINICIAN_GROUPS}
      />,
    );
    // Group headings (regroup contract) + a card per clinician module.
    expect(await screen.findByText("Diagnostics & Pharmacy")).toBeTruthy();
    expect(screen.getByText("Patient Care")).toBeTruthy();
    expect(screen.getByText("Insights")).toBeTruthy();
    const cards = document.querySelectorAll(".module-card");
    const expected = CLINICIAN_GROUPS.reduce((n, g) => n + g.items.length, 0);
    expect(cards.length).toBe(expected);
    // Exactly one <h2> on Home (P-1), and it is the page heading.
    expect(document.querySelectorAll("h2").length).toBe(1);
    expect(document.querySelector("h2")?.textContent).toBe("Home");
  });

  it("hero strip shows org + date and own-work chips only from loaded payloads", async () => {
    const client = cardClient({
      myPatients: vi.fn(async () => ({
        staff_id: "staff-001",
        items: [{ encounter_id: "e1" }, { encounter_id: "e2" }],
        total: 2,
      })),
      myUnitPatients: vi.fn(async () => ({
        unit: "unit-medsurg",
        unit_display: "Med-Surg",
        items: [{ encounter_id: "e1" }],
        total: 7,
      })),
    });
    render(
      <HomePage
        client={client}
        fallback={<p>fallback</p>}
        onNavigate={() => {}}
        navGroups={CLINICIAN_GROUPS}
      />,
    );
    // Chips carry counts from the my-patients / my-unit payloads.
    expect(await screen.findByText("My patients")).toBeTruthy();
    expect(screen.getByText("My unit · Med-Surg")).toBeTruthy();
    expect(screen.getByText("7")).toBeTruthy();
    // The greeting is the org name at display size, with the
    // audience label from the home payload in the meta line.
    const hero = document.querySelector(".home-hero");
    expect(hero?.textContent).toContain("OpenCare");
    expect(hero?.textContent).toContain("Clinician");
    // Group identity rides the card + group wrapper (data-group).
    expect(
      document
        .querySelector('[data-module="lab"]')
        ?.getAttribute("data-group"),
    ).toBe("Diagnostics & Pharmacy");
  });

  it("shows the live indicator on provider modules and purpose + Open elsewhere", async () => {
    render(
      <HomePage
        client={cardClient()}
        fallback={<p>fallback</p>}
        onNavigate={() => {}}
        navGroups={CLINICIAN_GROUPS}
      />,
    );
    expect(await screen.findByText("critical results open")).toBeTruthy();
    expect(screen.getByText("Needs attention")).toBeTruthy();
    expect(screen.getByText("Oldest 42m")).toBeTruthy();
    expect(screen.getByText("92%")).toBeTruthy();
    // Pharmacy has no provider entry in the stub: purpose + Open.
    const pharmacy = document.querySelector('[data-module="pharmacy"]');
    expect(pharmacy?.textContent).toContain("Verification, dispensing and stock");
    expect(pharmacy?.textContent).toContain("Open →");
    expect(pharmacy?.textContent).not.toContain("critical results open");
  });

  it("a failing /home/cards degrades to purpose-only cards (no error, no invented numbers)", async () => {
    const client = cardClient({
      homeCards: vi.fn(async () => {
        throw new Error("endpoint not deployed yet");
      }),
    });
    render(
      <HomePage
        client={client}
        fallback={<p>fallback</p>}
        onNavigate={() => {}}
        navGroups={CLINICIAN_GROUPS}
      />,
    );
    const lab = await screen.findByRole("button", { name: "Open Laboratory" });
    expect(lab.textContent).toContain("Open →");
    expect(screen.queryByText("critical results open")).toBeNull();
  });

  it("clicking a card navigates to its module in one action", async () => {
    const onNavigate = vi.fn();
    render(
      <HomePage
        client={cardClient()}
        fallback={<p>fallback</p>}
        onNavigate={onNavigate}
        navGroups={CLINICIAN_GROUPS}
      />,
    );
    // Wait for the settled tree (dashboard loaded) before clicking —
    // clicking a node from the transient loading branch would miss
    // React's root-delegated events after the tree swap.
    await screen.findByText("My patients — I am the attending");
    fireEvent.click(screen.getByRole("button", { name: "Open Pharmacy" }));
    expect(onNavigate).toHaveBeenCalledTimes(1);
    expect(onNavigate).toHaveBeenCalledWith("pharmacy");
  });

  it("keeps the existing role content below the cards", async () => {
    render(
      <HomePage
        client={cardClient()}
        fallback={<p>fallback</p>}
        onNavigate={() => {}}
        onOpenChart={() => {}}
        navGroups={CLINICIAN_GROUPS}
      />,
    );
    // My Patients (clinician audience, always rendered) follows the cards.
    expect(
      await screen.findByText("My patients — I am the attending"),
    ).toBeTruthy();
    const cardsEl = document.querySelector(".home-cards");
    const listEl = screen.getByText("My patients — I am the attending");
    expect(
      cardsEl && listEl
        ? cardsEl.compareDocumentPosition(listEl) & Node.DOCUMENT_POSITION_FOLLOWING
        : 0,
    ).toBeTruthy();
  });

  it("patient groups render the portal + billing cards", async () => {
    render(
      <HomePage
        client={cardClient()}
        fallback={<p>fallback</p>}
        onNavigate={() => {}}
        navGroups={PATIENT_GROUPS}
      />,
    );
    expect(await screen.findByRole("button", { name: "Open My results" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open Billing" })).toBeTruthy();
    expect(document.querySelectorAll(".module-card").length).toBe(2);
  });

  it("without navGroups the page behaves exactly as before (no card menu)", async () => {
    render(<HomePage client={cardClient()} fallback={<p>fallback</p>} />);
    expect(
      await screen.findByText("My patients — I am the attending"),
    ).toBeTruthy();
    expect(document.querySelector(".home-cards")).toBeNull();
  });
});
