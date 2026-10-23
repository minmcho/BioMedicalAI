import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import {
  fixtureAnaesthesiaForm,
  fixtureRooms,
  fixtureSurgeryBoard,
  fixtureSurgeryCaseRecord,
} from "../test/fixtures";
import { SurgeryBoardPage } from "./SurgeryBoardPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// ---- Phase 15 (E) fixtures: shapes copied from backend/app/surgery_depth.py

const fixtureRequest = {
  request_id: "req-1",
  patient_ref: "Patient/pat-1",
  procedure_code: "27447",
  procedure_display: "Total knee arthroplasty",
  surgeon: "dr-smith",
  urgency: "emergent",
  urgency_rank: 1,
  requested_window: { start: null, end: null },
  status: "requested",
  decline_reason: null,
  case_id: null,
  created_by: "dev-nurse",
  decided_by: null,
  created_at: "2026-10-19T09:00:00Z",
  wait_minutes: 120,
  wait_hours: 2,
};

const fixtureWaitlistItem = {
  ...fixtureRequest,
  request_id: "req-2",
  procedure_code: "27130",
  procedure_display: "Total hip arthroplasty",
  urgency: "urgent",
  urgency_rank: 2,
  status: "approved",
};

const fixtureUtilisation = {
  date: "2026-10-20",
  weekday: "tuesday",
  rooms: [
    {
      room: "OR-1",
      display: "Operating Room 1",
      cases: 1,
      scheduled_minutes: 120,
      block_minutes: 480,
      in_block_minutes: 120,
      non_block_minutes: 0,
      utilisation_pct: 25,
      blocks: [
        {
          block_code: "blk-or1-mon-ortho",
          surgeon: "dr-smith",
          service: "orthopaedics",
          start: "2026-10-20T08:00:00+00:00",
          end: "2026-10-20T16:00:00+00:00",
          block_minutes: 480,
          scheduled_minutes: 120,
          utilisation_pct: 25,
        },
      ],
      turnover: { gaps: [], total_gap_minutes: 0, count: 0 },
    },
  ],
};

const fixtureCard = {
  card_id: "card-1",
  surgeon: "dr-smith",
  procedure_code: "27447",
  items: [
    { kind: "implant", code: "knee-implant", display: "Knee implant", qty: 3 },
    { kind: "supply", code: "suture-2-0", display: "Suture 2-0", qty: 2 },
  ],
  created_by: "dev-clinician",
  created_at: "2026-10-19T09:00:00Z",
};

const fixtureLot = {
  item_code: "knee-implant",
  display: "Knee implant",
  lot_number: "LOT-9",
  serial: null,
  expiry_date: "2027-01-01",
  quantity: 1,
  days_to_expiry: 400,
  expired: false,
};

const fixturePacuPayload = {
  name: "PACU discharge scoring (modified Aldrete, site config)",
  concepts: [
    { code: "activity", display: "Activity — moves extremities", max: 2, active: true },
    { code: "respiration", display: "Respiration", max: 2, active: true },
    { code: "circulation", display: "Circulation — BP vs pre-anaesthetic", max: 2, active: true },
    { code: "consciousness", display: "Consciousness", max: 2, active: true },
    { code: "oxygen-saturation", display: "Oxygen saturation", max: 2, active: true },
  ],
  dischargeThreshold: 9,
  mustPass: [
    { code: "respiration", minimum: 1 },
    { code: "oxygen-saturation", minimum: 1 },
  ],
};

const OVERSELL_DETAIL =
  "Implant 'knee-implant': pick needs 3 but only 1 in-date units are "
  + "on hand — pick refused (oversell)";

function depthRequest() {
  return vi.fn(async (path: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (path === "/surgery/requests") {
      return { items: [fixtureRequest], total: 1 };
    }
    if (path === "/surgery/waitlist") {
      return { items: [fixtureWaitlistItem], total: 1, generated_at: "2026-10-20T09:00:00Z" };
    }
    if (path.startsWith("/surgery/utilisation")) return fixtureUtilisation;
    if (path === "/surgery/preference-cards") {
      return { items: [fixtureCard], total: 1 };
    }
    if (path === "/surgery/implants/lots") {
      return { items: [fixtureLot], total: 1 };
    }
    if (path === "/surgery/cases/case-1/pick-list" && method === "POST") {
      throw new ApiError(409, OVERSELL_DETAIL);
    }
    if (path === "/surgery/trays") {
      return { items: [], total: 0, states: ["in-use", "decontamination", "assembly", "sterilising", "sterile", "issued"], shelf_life_days: 30 };
    }
    if (path === "/surgery/cases/case-1/pacu-scores") {
      return { items: [], total: 0, latest: null };
    }
    if (path === "/surgery/cases/case-1/pacu-score" && method === "POST") {
      const body = JSON.parse(String(init?.body ?? "{}")) as {
        scores: Record<string, number>;
      };
      return {
        score_id: "pacu-1",
        case_id: "case-1",
        scores: body.scores,
        total: 10,
        threshold: 9,
        must_pass_failures: [],
        passes: true,
        discharge_ready: true,
        recorded_by: "dev-clinician",
        recorded_at: "2026-10-20T12:00:00Z",
      };
    }
    if (path === "/surgery/outcomes/ssi") {
      return {
        feed: "surgical-site-infection surveillance",
        purpose: "Infection-prevention surveillance feed.",
        items: [],
        total: 0,
      };
    }
    throw new Error(`unexpected request: ${method} ${path}`);
  });
}

function stubDepthClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    surgeryBoard: vi.fn(async () => fixtureSurgeryBoard),
    surgeryRooms: vi.fn(async () => fixtureRooms.rooms),
    scheduleCase: vi.fn(async () => ({})),
    transitionCase: vi.fn(async () => ({})),
    rescheduleCase: vi.fn(async () => ({})),
    surgeryCaseRecord: vi.fn(async () => fixtureSurgeryCaseRecord),
    submitPreopChecklist: vi.fn(async () => ({})),
    submitAnaesthesia: vi.fn(async () => ({})),
    submitAnaesthesiaVitals: vi.fn(async () => ({})),
    submitSafetyChecklist: vi.fn(async () => ({
      case: null,
      section: "sign-in",
      safety_checklist: { "sign-in": true, "time-out": false, "sign-out": false },
    })),
    request: depthRequest(),
    effectiveConfig: vi.fn(async (_kind: string, key: string) => {
      const payload =
        key === "pacu-scoring" ? fixturePacuPayload
        : key === "or-blocks" ? {
            name: "Operating-room block templates",
            blocks: [
              {
                code: "blk-or1-mon-ortho", surgeon: "dr-smith",
                service: "orthopaedics", room: "OR-1", weekday: "monday",
                start: "08:00", end: "16:00",
              },
            ],
          }
        : key === "surgery-urgency" ? {
            concepts: [
              { code: "emergent", display: "Emergent", rank: 1, active: true },
              { code: "urgent", display: "Urgent", rank: 2, active: true },
              { code: "elective", display: "Elective", rank: 3, active: true },
            ],
          }
        : key === "sterile-processing" ? {
            states: ["in-use", "decontamination", "assembly", "sterilising", "sterile", "issued"],
            shelfLifeDays: 30,
            cycleTypes: [
              { code: "steam-134", display: "Steam 134C pre-vacuum", temperatureC: 134, minMinutes: 18 },
            ],
          }
        : key === "surgery-complications" ? {
            statuses: ["recovered", "recovering", "complication", "readmitted", "deceased"],
            concepts: [{ code: "sepsis", display: "Sepsis", active: true }],
          }
        : key === "surgery-flow" ? {}
        : fixtureAnaesthesiaForm;
      return { kind: _kind, key, version: 1, status: "published", payload };
    }),
    ...overrides,
  } as unknown as ApiClient;
}

describe("SurgeryBoardPage — Phase 15 depth", () => {
  it("requests tab renders requests with the urgency badge and the waitlist", async () => {
    render(<SurgeryBoardPage client={stubDepthClient()} role="clinician" />);
    await screen.findByText("Total knee arthroplasty");
    fireEvent.click(screen.getByRole("tab", { name: "Requests & waitlist" }));
    // The request appears in the master list with its urgency as a
    // badge (and again in the selected detail, so there are two).
    expect((await screen.findAllByText("emergent")).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("urgent")).toBeTruthy();
    expect(screen.getByText(/Waitlist — approved, unscheduled \(1\)/)).toBeTruthy();
    // Master-detail: the first request is selected, showing its detail.
    expect(await screen.findByText(/Surgeon dr-smith/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Approve request" })).toBeTruthy();
  });

  it("blocks tab renders block templates and server utilisation figures", async () => {
    render(<SurgeryBoardPage client={stubDepthClient()} role="clinician" />);
    await screen.findByText("Total knee arthroplasty");
    fireEvent.click(screen.getByRole("tab", { name: "Blocks & utilisation" }));
    expect(await screen.findByText(/Scheduled 120 min/)).toBeTruthy();
    expect(screen.getByText(/Utilisation 25%/)).toBeTruthy();
    expect(screen.getByText(/In-block 120 min/)).toBeTruthy();
    expect(screen.getAllByText(/blk-or1-mon-ortho/).length).toBeGreaterThan(0);
  });

  it("preference pick oversell 409 is surfaced verbatim", async () => {
    render(<SurgeryBoardPage client={stubDepthClient()} role="clinician" />);
    await screen.findByText("Total knee arthroplasty");
    fireEvent.click(screen.getByRole("tab", { name: "Preference cards" }));
    expect(await screen.findByText(/Lot LOT-9/)).toBeTruthy();
    expect(screen.getByText(/expires 2027-01-01/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Pick list case id"), {
      target: { value: "case-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Generate pick list" }));
    expect(await screen.findByText(new RegExp("pick refused \\(oversell\\)"))).toBeTruthy();
    expect(screen.getByText(new RegExp("pick needs 3 but only 1"))).toBeTruthy();
  });

  it("PACU score submits the five criteria and shows the server total + pass", async () => {
    const client = stubDepthClient();
    render(<SurgeryBoardPage client={client} role="clinician" />);
    await screen.findByText("Total knee arthroplasty");
    fireEvent.click(screen.getByRole("button", { name: "PACU & outcome" }));
    const labels = [
      "PACU Activity — moves extremities",
      "PACU Respiration",
      "PACU Circulation — BP vs pre-anaesthetic",
      "PACU Consciousness",
      "PACU Oxygen saturation",
    ];
    for (const label of labels) {
      fireEvent.change(await screen.findByLabelText(label), { target: { value: "2" } });
    }
    fireEvent.click(screen.getByRole("button", { name: "Record PACU score" }));
    expect(await screen.findByText(/Latest PACU total: 10 \(threshold 9\)/)).toBeTruthy();
    expect(screen.getByText(/Passes — discharge ready/)).toBeTruthy();
    const request = client.request as unknown as ReturnType<typeof vi.fn>;
    const pacuCall = request.mock.calls.find(
      (c: unknown[]) => c[0] === "/surgery/cases/case-1/pacu-score",
    );
    expect(pacuCall).toBeTruthy();
    const body = JSON.parse(String((pacuCall?.[1] as RequestInit).body));
    expect(body.scores).toEqual({
      activity: 2,
      respiration: 2,
      circulation: 2,
      consciousness: 2,
      "oxygen-saturation": 2,
    });
  });

  it("surfaces the PACU gate 409 detail when the pacu → done transition is refused", async () => {
    const pacuBoard = {
      ...fixtureSurgeryBoard,
      rooms: fixtureSurgeryBoard.rooms.map((room) => ({
        ...room,
        cases: room.cases.map((entry) => ({
          ...entry,
          case: { ...entry.case, status: "pacu" },
          state: "pacu",
        })),
      })),
    };
    const client = stubDepthClient({
      surgeryBoard: vi.fn(async () => pacuBoard),
      transitionCase: vi.fn(async () => {
        throw new ApiError(
          409,
          "PACU discharge gate: case case-1 cannot move from 'pacu' to "
          + "'done' — no PACU score recorded (missing PACU score; record "
          + "one via POST /surgery/cases/case-1/pacu-score)",
        );
      }),
    });
    render(<SurgeryBoardPage client={client} role="clinician" />);
    await screen.findByText("Total knee arthroplasty");
    fireEvent.click(screen.getByRole("button", { name: "done" }));
    expect(await screen.findByText(/PACU discharge gate/)).toBeTruthy();
    expect(screen.getByText(/missing PACU score/)).toBeTruthy();
  });
});
