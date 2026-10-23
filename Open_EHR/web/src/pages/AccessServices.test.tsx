import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import {
  listTelehealthVisits,
  portalTelehealthVisit,
  submitTelehealthOutcome,
  telehealthServices,
  type URCase,
} from "../api/accessServices";
import {
  fixtureMessageThreads,
  fixtureTelehealthWorkspace,
  fixtureThreadDetail,
} from "../test/fixtures";
import { BillingPage } from "./BillingPage";
import { PortalPage } from "./PortalPage";
import { URPanel } from "./UrPanel";

// Phase 15 access services (BD-21/22/23/28, backend/app/access_services.py).
// The stub client stands in for the network boundary in unit tests only;
// standalone client modules are stubbed through client.request, keyed by
// path, following the InboxPage.test.tsx pattern.
afterEach(cleanup);

const DISCLAIMER =
  "Test-mode payment provider only: this records the payment in " +
  "the revenue ledger (app.revenue PatientPayment). No live card " +
  "processor or payment gateway is integrated.";

const criteriaPayload = {
  name: "Utilisation review criteria (SITE-FIXTURE example set only)",
  notice:
    "SITE-FIXTURE EXAMPLE ONLY — these are locally invented example criteria for testing the review workflow.",
  concepts: [
    {
      code: "site-fixture-med-nec-01",
      display: "SITE-FIXTURE example: medical-necessity review",
      active: true,
      description:
        "SITE-FIXTURE example criteria set for local testing only — not licensed InterQual/MCG content.",
      requiredEvidence: ["diagnosis-documented", "clinical-notes"],
      fixture: true,
    },
  ],
};

function urCase(overrides: Partial<URCase> = {}): URCase {
  return {
    id: "case-1",
    case_id: "case-1",
    patient: "Patient/pat-1",
    patient_ref: "Patient/pat-1",
    encounter_ref: "Encounter/enc-1",
    encounter_id: "enc-1",
    claim_ref: "Claim/claim-1",
    claim_id: "claim-1",
    status: "in-review",
    criteria_code: null,
    criteria_set: null,
    evidence_checked: [],
    first_reviewer: null,
    advisor_reviewer: null,
    advisor_decision: null,
    advisor_review_required: false,
    history: [{ from: null, to: "pending", by: "intake", at: "2026-01-05T09:00:00Z" }],
    note: null,
    created_by: "intake",
    created_at: "2026-01-05T09:00:00Z",
    updated_at: "2026-01-05T09:00:00Z",
    ...overrides,
  };
}

const denialCase = urCase({
  id: "case-denial",
  case_id: "case-denial",
  status: "denial-risk",
  encounter_ref: "Encounter/enc-9",
  encounter_id: "enc-9",
  claim_ref: "Claim/claim-9",
  claim_id: "claim-9",
  criteria_code: "site-fixture-med-nec-01",
  criteria_set: "site-fixture-med-nec-01",
  evidence_checked: ["diagnosis-documented"],
  first_reviewer: "nurse-reviewer",
  advisor_review_required: true,
});

/** Stateful UR request stub: reviews mutate the case store, and the
 *  advisor endpoint rejects with the server's separation 409 (the
 *  advisor must differ from the first reviewer). */
function makeUrRequest() {
  const store = new Map<string, URCase>([
    ["case-1", urCase()],
    ["case-denial", { ...denialCase }],
  ]);
  return vi.fn(async (path: string, init?: RequestInit) => {
    if (path === "/ur/denial-risk") {
      const items = [...store.values()].filter((c) => c.status === "denial-risk");
      return {
        items,
        total: items.length,
        join_keys: ["claim_id", "claim_ref", "encounter_ref", "patient_ref"],
        joins_with: "GET /billing/denials (app.revenue denials workqueue)",
        note: "Join feed only — denial workqueue state (assignee, notes, outcomes) lives in app.revenue and is not duplicated here.",
      };
    }
    if (path === "/ur/cases" || path.startsWith("/ur/cases?")) {
      const items = [...store.values()];
      return { items, total: items.length };
    }
    const advisor = path.match(/^\/ur\/cases\/(.+)\/advisor-review$/);
    if (advisor) {
      expect(init?.method).toBe("POST");
      throw new ApiError(
        409,
        "The physician advisor must differ from the first reviewer ('nurse-reviewer') — a denial-risk case cannot be advisor-reviewed by the person who marked it",
      );
    }
    const review = path.match(/^\/ur\/cases\/(.+)\/review$/);
    if (review) {
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      const current = store.get(review[1]);
      if (!current) throw new ApiError(404, "UR case not found");
      const updated: URCase = {
        ...current,
        status: String(body.to_status),
        criteria_code: (body.criteria_code as string) ?? current.criteria_code,
        evidence_checked: (body.evidence_checked as string[]) ?? current.evidence_checked,
        first_reviewer: current.first_reviewer ?? "nurse-reviewer",
      };
      store.set(review[1], updated);
      return updated;
    }
    const detail = path.match(/^\/ur\/cases\/(.+)$/);
    if (detail) {
      const found = store.get(detail[1]);
      if (!found) throw new ApiError(404, "UR case not found");
      return found;
    }
    throw new Error(`Unexpected request: ${path}`);
  });
}

function urStubClient(): ApiClient {
  return {
    request: makeUrRequest(),
    effectiveConfig: vi.fn(async () => ({ payload: criteriaPayload })),
  } as unknown as ApiClient;
}

describe("URPanel (BD-21)", () => {
  it("renders the worklist, the denial-risk feed strip and the site-fixture criteria label", async () => {
    render(<URPanel client={urStubClient()} role="clinician" />);
    // Worklist rows from GET /ur/cases.
    expect(await screen.findByText("Encounter/enc-1")).toBeTruthy();
    expect(screen.getByText("Claim/claim-9")).toBeTruthy();
    expect(screen.getAllByText("denial-risk").length).toBeGreaterThan(0);
    // The denial-risk strip is a FEED into the existing denials
    // workqueue, never a second queue.
    expect(
      screen.getByText(/feed into that queue, not a separate queue/),
    ).toBeTruthy();
    expect(screen.getByText(/GET \/billing\/denials/)).toBeTruthy();
    // Criteria are labelled as site-fixture examples wherever shown.
    expect(
      screen.getAllByText(/Site-fixture example criteria — sites publish their licensed criteria/)
        .length,
    ).toBeGreaterThan(0);
  });

  it("records a first review with criteria + evidence through the client", async () => {
    const client = urStubClient();
    render(<URPanel client={client} role="clinician" />);
    await screen.findByText("Encounter/enc-1");
    const viewButtons = screen.getAllByRole("button", { name: "View case" });
    fireEvent.click(viewButtons[0]);
    expect(await screen.findByText(/History:/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Criteria set"), {
      target: { value: "site-fixture-med-nec-01" },
    });
    fireEvent.click(await screen.findByLabelText("Evidence diagnosis-documented"));
    fireEvent.click(screen.getByRole("button", { name: "Record review" }));
    await waitFor(() =>
      expect(client.request).toHaveBeenCalledWith(
        "/ur/cases/case-1/review",
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining('"to_status":"approved"'),
        }),
      ),
    );
    const reviewCall = (client.request as ReturnType<typeof vi.fn>).mock.calls.find(
      (c: unknown[]) => c[0] === "/ur/cases/case-1/review",
    );
    expect(String(reviewCall?.[1]?.body)).toContain("site-fixture-med-nec-01");
    expect(String(reviewCall?.[1]?.body)).toContain("diagnosis-documented");
    expect(await screen.findByText("Record review — done.")).toBeTruthy();
  });

  it("surfaces the advisor-separation 409 when the advisor is the first reviewer", async () => {
    render(<URPanel client={urStubClient()} role="clinician" />);
    await screen.findByText("Claim/claim-9");
    const viewButtons = screen.getAllByRole("button", { name: "View case" });
    fireEvent.click(viewButtons[1]);
    expect(
      await screen.findByText(/Physician advisor review \(required\)/),
    ).toBeTruthy();
    expect(screen.getByText(/must differ from the first reviewer/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Submit advisor review" }));
    expect(
      await screen.findByText(
        /\(409\) The physician advisor must differ from the first reviewer/,
      ),
    ).toBeTruthy();
  });

  it("mounts as the Utilisation review tab in BillingPage", async () => {
    const client = {
      request: makeUrRequest(),
      effectiveConfig: vi.fn(async () => ({ payload: criteriaPayload })),
      claims: vi.fn(async () => []),
      getClaim: vi.fn(),
      generateClaim: vi.fn(),
      transitionClaim: vi.fn(),
      export837: vi.fn(async () => ""),
      codingQueue: vi.fn(async () => []),
      assignCodes: vi.fn(),
      remittanceBatches: vi.fn(async () => []),
      remittanceExceptions: vi.fn(async () => []),
      ingest835: vi.fn(),
      denials: vi.fn(async () => ({ items: [], total: 0, outcomes: [] })),
      assignDenial: vi.fn(),
      noteDenial: vi.fn(),
      resolveDenial: vi.fn(),
      rebillClaim: vi.fn(),
      statement: vi.fn(async () => ({
        patient: "Patient/pat-1",
        generated_at: "2026-01-10T12:00:00Z",
        claims: [],
        totals: {
          total: 0, paid: 0, adjustments: 0, patient_responsibility: 0,
          patient_paid: 0, patient_due: 0, balance: 0,
        },
        disclaimer: "Payments recorded here are ledger entries.",
      })),
      recordPatientPayment: vi.fn(),
    } as unknown as ApiClient;
    render(<BillingPage client={client} role="admin" />);
    expect(await screen.findByText("No claims in this view.")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Utilisation review" }));
    expect(await screen.findByText("UR worklist")).toBeTruthy();
    expect(await screen.findByText("Encounter/enc-1")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Portal: bill pay (BD-22) + proxy access (BD-23)
// ---------------------------------------------------------------------------

const balanceFixture = {
  patient: "Patient/pat-1",
  balance: 20,
  patient_due: 20,
  patient_paid: 0,
  patient_responsibility: 20,
  totals: {
    total: 240, paid: 180, adjustments: 30, patient_responsibility: 20,
    patient_paid: 0, patient_due: 20, balance: 10,
  },
  claims: [
    { claim_id: "claim-partial", status: "partially-paid", patient_due: 20 },
  ],
  provider_mode: "test",
  disclaimer: DISCLAIMER,
  revenue_disclaimer: null,
};

const proxyForGrant = {
  id: "grant-9",
  grant_id: "grant-9",
  patient: "Patient/pat-2",
  patient_ref: "Patient/pat-2",
  proxy_username: "pat-1-user",
  scopes: ["results", "schedule"],
  granted_by: "pat-2-user",
  grant_source: "patient",
  status: "active",
  expired: false,
  effective_status: "active",
  expires_at: null,
  created_at: "2026-01-05T09:00:00Z",
};

function makePortalRequest() {
  let myGrants: unknown[] = [];
  return vi.fn(async (path: string, init?: RequestInit) => {
    if (path === "/portal/billing/balance") return balanceFixture;
    if (path === "/portal/billing/payments") {
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      return {
        receipt_id: "pay-1",
        payment_id: "pay-1",
        claim_id: body.claim_id ?? "claim-partial",
        patient: "Patient/pat-1",
        amount: body.amount,
        method: body.method ?? "card",
        provider: "test-mode",
        provider_mode: "test",
        mode: "test",
        live: false,
        patient_due_after: 0,
        balance_after: 0,
        recorded_by: "pat-1-user",
        disclaimer: DISCLAIMER,
      };
    }
    if (path === "/portal/proxy/grants" && init?.method === "POST") {
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      const grant = {
        id: "grant-1",
        grant_id: "grant-1",
        patient: "Patient/pat-1",
        patient_ref: "Patient/pat-1",
        proxy_username: body.proxy_username,
        scopes: body.scopes,
        granted_by: "pat-1-user",
        grant_source: "patient",
        status: "active",
        expired: false,
        effective_status: "active",
        expires_at: null,
        created_at: "2026-01-05T09:00:00Z",
      };
      myGrants = [grant];
      return grant;
    }
    if (path === "/portal/proxy/grants") return { items: myGrants, total: myGrants.length };
    if (path === "/portal/proxy/patients") {
      return { items: [proxyForGrant], total: 1, proxy: "pat-1-user" };
    }
    if (path.endsWith("/results")) {
      return {
        resourceType: "Bundle",
        type: "searchset",
        total: 1,
        entry: [
          {
            resource: {
              resourceType: "Observation",
              id: "obs-1",
              status: "final",
              code: {
                coding: [{ system: "http://loinc.org", code: "2823-3", display: "Potassium" }],
                text: "Potassium",
              },
              effectiveDateTime: "2026-01-05T10:00:00Z",
              valueQuantity: { value: 4.5, unit: "mmol/L" },
            },
          },
        ],
        patient: "Patient/pat-2",
        on_behalf_of: "Patient/pat-2",
        proxy: "pat-1-user",
      };
    }
    if (path.endsWith("/appointments") || path.endsWith("/messages")) {
      return { items: [], total: 0, patient: "Patient/pat-2", on_behalf_of: "Patient/pat-2", proxy: "pat-1-user" };
    }
    throw new Error(`Unexpected request: ${path}`);
  });
}

function portalStubClient(): ApiClient {
  return {
    request: makePortalRequest(),
    effectiveConfig: vi.fn(async () => ({ payload: criteriaPayload })),
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
      slot_types: ["follow-up", "telehealth"],
    })),
    requestAppointment: vi.fn(async () => ({
      appointment: { resourceType: "Appointment", id: "appt-new", status: "proposed" },
      auto_confirmed: false,
      status: "proposed",
    })),
    telehealthWorkspace: vi.fn(async () => fixtureTelehealthWorkspace),
    submitTelehealthIntake: vi.fn(async () => ({})),
  } as unknown as ApiClient;
}


// UX pass: bill-pay and proxy access are portal tabs now — open the
// tab under test first; every assertion below is unchanged.
async function openPortalTab(name: string) {
  fireEvent.click(await screen.findByRole("tab", { name }));
}

describe("PortalPage access services (BD-22 / BD-23)", () => {
  it("renders the bill-pay and proxy sections", async () => {
    render(<PortalPage client={portalStubClient()} />);
    await openPortalTab("Pay a bill");
    expect(
      await screen.findByRole("heading", { name: "Pay a bill" }),
    ).toBeTruthy();
    expect(screen.getByText(/Test mode — no live payment processor is integrated/)).toBeTruthy();
    await openPortalTab("Proxies");
    expect(await screen.findByText("My proxies")).toBeTruthy();
    expect(screen.getByText("I am a proxy for")).toBeTruthy();
  });

  it("bill pay: balance → payment → receipt, with test-mode labels and the verbatim disclaimer", async () => {
    const client = portalStubClient();
    render(<PortalPage client={client} />);
    await openPortalTab("Pay a bill");
    expect(
      await screen.findByRole("heading", { name: "Pay a bill" }),
    ).toBeTruthy();
    // Balance from GET /portal/billing/balance.
    expect(await screen.findByText("USD 20.00")).toBeTruthy();
    expect(screen.getByText(/provider mode: test \(live: false\)/)).toBeTruthy();
    // The server disclaimer renders verbatim where payment is offered.
    expect(screen.getAllByText(DISCLAIMER).length).toBeGreaterThan(0);
    fireEvent.change(screen.getByLabelText("Payment amount"), {
      target: { value: "20" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Pay bill (test mode)" }));
    await waitFor(() =>
      expect(client.request).toHaveBeenCalledWith(
        "/portal/billing/payments",
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining('"amount":20'),
        }),
      ),
    );
    // Receipt: provider "test-mode", live: false, disclaimer verbatim.
    expect(await screen.findByText("Payment receipt")).toBeTruthy();
    // "provider: test-mode" / "live: false" appear both in the
    // section hint and on the receipt — both must carry the label.
    expect(screen.getAllByText(/provider: test-mode/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/live: false/).length).toBeGreaterThan(0);
  });

  it("proxy: creates a grant for a named proxy with chosen scopes", async () => {
    const client = portalStubClient();
    render(<PortalPage client={client} />);
    await openPortalTab("Proxies");
    expect(await screen.findByText("My proxies")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Proxy username"), {
      target: { value: "family-member" },
    });
    fireEvent.click(screen.getByLabelText("Proxy scope results"));
    fireEvent.click(screen.getByRole("button", { name: "Grant proxy access" }));
    await waitFor(() =>
      expect(client.request).toHaveBeenCalledWith(
        "/portal/proxy/grants",
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining('"proxy_username":"family-member"'),
        }),
      ),
    );
    const createCall = (client.request as ReturnType<typeof vi.fn>).mock.calls.find(
      (c: unknown[]) => c[0] === "/portal/proxy/grants"
        && (c[1] as { method?: string } | undefined)?.method === "POST",
    );
    expect(String(createCall?.[1]?.body)).toContain('"schedule"');
    expect(String(createCall?.[1]?.body)).toContain('"results"');
    expect(
      await screen.findByText(/Proxy access granted to family-member/),
    ).toBeTruthy();
    expect(await screen.findByText("family-member")).toBeTruthy();
  });

  it("proxy: scoped results view for a patient the caller proxies for", async () => {
    const client = portalStubClient();
    render(<PortalPage client={client} />);
    await openPortalTab("Proxies");
    expect(await screen.findByText("I am a proxy for")).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "View as proxy" }));
    expect(await screen.findByText(/Proxy view — Patient\/pat-2/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "View released results" }));
    expect(await screen.findByText(/Potassium/)).toBeTruthy();
    await waitFor(() =>
      expect(client.request).toHaveBeenCalledWith(
        "/portal/proxy/patients/Patient%2Fpat-2/results",
      ),
    );
  });
});

// ---------------------------------------------------------------------------
// BD-28 telehealth client wiring (module level — the portal has no
// Phase 15 visit surface to mount the outcome UI on; see report)
// ---------------------------------------------------------------------------

describe("accessServices telehealth client (BD-28)", () => {
  it("calls the services, visits, portal workspace and outcome endpoints", async () => {
    const request = vi.fn(async (path: string, init?: RequestInit) => {
      void init;
      return { path };
    });
    const client = { request } as unknown as ApiClient;
    await telehealthServices(client);
    await listTelehealthVisits(client, { status: "waiting" });
    await portalTelehealthVisit(client, "visit-1");
    await submitTelehealthOutcome(client, "visit-1", [
      { linkId: "outcome-summary", answer: [{ valueString: "Went well" }] },
    ]);
    expect(request).toHaveBeenCalledWith("/telehealth/services");
    expect(request).toHaveBeenCalledWith("/telehealth/visits?status=waiting");
    expect(request).toHaveBeenCalledWith("/portal/telehealth/visits/visit-1");
    expect(request).toHaveBeenCalledWith(
      "/portal/telehealth/visits/visit-1/outcome",
      expect.objectContaining({
        method: "POST",
        body: expect.stringContaining('"outcome-summary"'),
      }),
    );
  });
});
