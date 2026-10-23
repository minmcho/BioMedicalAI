import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import { SupportPage } from "./SupportPage";

// Phase 15 support page: HaH + supply + diet tray-check hard stop +
// GME violations, rendered from the /support API surface through a
// stubbed client.request (the standalone api/support.ts pattern).
afterEach(cleanup);

const ALLERGY_DETAIL =
  "Allergy alert: tray item 'peanut sauce' matches this patient's recorded allergy to Peanut (code peanut); the tray was refused at assembly";

function stubClient(): ApiClient {
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === "/support/hah/bindings") return { items: [{ id: "b1", binding_id: "hahb-1", patient_ref: "Patient/pat-1", device_id: "dev-1", device_key: "dev-1", device_name: "Home monitor", status: "active", breach_count: 1, breaches: [], created_at: null }], total: 1 };
    if (path === "/support/hah/readings") return { items: [{ id: "r1", reading_id: "hahr-1", binding_id: "hahb-1", patient_ref: "Patient/pat-1", reading_type: "spo2", value: 88, unit: "%", at: "2026-10-18T10:00:00+00:00", in_range: false, breach: "low", task_id: "t1" }], total: 1, breaches: 1 };
    if (path === "/support/hah/dispatches") return { items: [{ id: "d1", dispatch_id: "hahd-1", patient_ref: "Patient/pat-1", dispatch_type: "rapid-response", status: "requested", assignee: null, note: null, sla_minutes: 30, due_at: null }], total: 1 };
    if (path === "/support/hah/kits") return { items: [{ id: "k1", kit_id: "kit-1", kit_type: "standard-hah", contents: ["tablet-monitor"], status: "issued", patient_ref: "Patient/pat-1", expected_return_at: "2026-10-01T00:00:00+00:00", overdue: true }], total: 1 };
    if (path === "/support/supplies/catalogue") return { catalogue: { name: "Supplies", concepts: [{ code: "gloves-nitrile", display: "Nitrile gloves", unit: "box", category: "ppe", active: true }] }, version: 1 };
    if (path === "/support/supplies/stock") return { items: [{ item_code: "gloves-nitrile", quantity: 42 }], total: 1 };
    if (path === "/support/supplies/par") return { items: [{ unit: "ICU", item_code: "gloves-nitrile", par_level: 10, on_hand: 3, below_par: true }], total: 1 };
    if (path === "/support/requisitions") return { items: [{ id: "req-1", unit: "ICU", item_code: "gloves-nitrile", quantity: 5, case_ref: null, status: "requested", requested_by: "nurse-1", approved_by: null, reject_reason: null, created_at: null }], total: 1 };
    if (path === "/support/diet/orders" && init?.method === "POST") throw new Error("unused");
    if (path === "/support/diet/orders") return { items: [{ id: "diet-1", patient: "Patient/pat-1", diet_type: "regular", texture: null, notes: null, unit: "ICU", status: "active", tray_checked: false, tray_items: null, tray_checked_by: null }], total: 1 };
    if (path === "/support/diet/census") return { items: [{ unit: "ICU", diet_type: "regular", unit_source: "order", count: 2 }], total_orders: 2 };
    if (path.startsWith("/support/diet/orders/") && path.endsWith("/tray-check")) throw new ApiError(422, ALLERGY_DETAIL);
    if (path === "/support/gme/duty-hours") return { items: [{ id: "log-1", trainee_staff_id: "trainee-1", start: "2026-10-17T08:00:00+00:00", end: "2026-10-18T10:00:00+00:00", hours: 26 }], total: 1 };
    if (path === "/support/gme/violations") return { items: [{ trainee: "trainee-1", rule: "max-continuous-hours", actual: 26, limit: 24, log_id: "log-1" }], total: 1, limits: { max_weekly: 80, max_continuous: 24, min_rest: 8 } };
    if (path === "/support/gme/rotations") return { items: [{ id: "rot-1", trainee_staff_id: "trainee-1", service_line: "Cardiology", start_date: "2026-10-01", end_date: "2026-10-31" }], total: 1 };
    return { items: [], total: 0 };
  });
  return {
    request,
    effectiveConfig: vi.fn(async () => ({ payload: { concepts: [{ code: "regular", display: "Regular", active: true }] } })),
  } as unknown as ApiClient;
}

describe("SupportPage (Phase 15)", () => {
  it("renders Hospital at Home bindings, breach readings, dispatch and kits", async () => {
    render(<SupportPage client={stubClient()} role="clinician" />);
    expect(await screen.findByText("Home monitor")).toBeTruthy();
    expect(screen.getByText("1 breach(es)")).toBeTruthy();
    expect(screen.getByText(/breach low/)).toBeTruthy();
    expect(screen.getByText("rapid-response")).toBeTruthy();
    expect(screen.getByText("kit-1")).toBeTruthy();
    expect(screen.getByText("overdue")).toBeTruthy();
  });

  it("renders supply catalogue, below-PAR badge and requisition actions", async () => {
    render(<SupportPage client={stubClient()} role="clinician" />);
    fireEvent.click(await screen.findByRole("tab", { name: "Supply chain" }));
    expect((await screen.findAllByText("Nitrile gloves")).length).toBeGreaterThan(0);
    expect(screen.getByText("below PAR")).toBeTruthy();
    expect(screen.getByText("42")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy();
  });

  it("surfaces the diet tray-check allergy hard-stop verbatim", async () => {
    render(<SupportPage client={stubClient()} role="clinician" />);
    fireEvent.click(await screen.findByRole("tab", { name: "Diet office" }));
    const input = await screen.findByLabelText("Tray items for order diet-1");
    fireEvent.change(input, { target: { value: "peanut sauce" } });
    fireEvent.click(screen.getByRole("button", { name: "Tray check" }));
    expect(await screen.findByText(new RegExp(ALLERGY_DETAIL.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")))).toBeTruthy();
    // Census by unit/diet also renders.
    expect(screen.getByText("Meal census (by unit / diet)")).toBeTruthy();
  });

  it("renders GME violations with actual vs limit", async () => {
    render(<SupportPage client={stubClient()} role="clinician" />);
    fireEvent.click(await screen.findByRole("tab", { name: "GME" }));
    expect(await screen.findByText("max-continuous-hours")).toBeTruthy();
    expect(screen.getByText("26h actual")).toBeTruthy();
    expect(screen.getByText("24h limit")).toBeTruthy();
    expect(screen.getByText("Cardiology")).toBeTruthy();
  });
});
