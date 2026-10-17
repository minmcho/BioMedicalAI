import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError, buildSearchPath, unwrapBundle } from "./client";
import type { Bundle, Patient } from "./types";
import { fixturePatient } from "../test/fixtures";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("buildSearchPath", () => {
  it("builds a bare search path without params", () => {
    expect(buildSearchPath("Patient")).toBe("/fhir/Patient");
  });

  it("encodes patient, practitioner and date-range params", () => {
    const path = buildSearchPath("Appointment", {
      practitioner: "Practitioner/dr-1",
      start: "2026-02-01T00:00:00.000Z",
      end: "2026-02-28T00:00:00.000Z",
    });
    expect(path).toContain("/fhir/Appointment?");
    expect(path).toContain("practitioner=Practitioner%2Fdr-1");
    expect(path).toContain("start=2026-02-01T00%3A00%3A00.000Z");
    expect(path).toContain("end=2026-02-28T00%3A00%3A00.000Z");
  });

  it("includes the patient param", () => {
    expect(buildSearchPath("Observation", { patient: "pat-1" })).toBe(
      "/fhir/Observation?patient=pat-1",
    );
  });
});

describe("unwrapBundle", () => {
  it("returns the resources inside a Bundle", () => {
    const bundle: Bundle<Patient> = {
      resourceType: "Bundle",
      type: "searchset",
      total: 1,
      entry: [{ resource: fixturePatient }],
    };
    expect(unwrapBundle(bundle)).toEqual([fixturePatient]);
  });

  it("returns an empty array for a Bundle without entries", () => {
    expect(unwrapBundle({ resourceType: "Bundle", type: "searchset" })).toEqual([]);
  });
});

describe("ApiClient request shaping", () => {
  it("sends the bearer token and hits the configured base URL", async () => {
    const fetchFn = vi.fn(async () =>
      jsonResponse({ resourceType: "Bundle", type: "searchset", total: 0, entry: [] }),
    );
    const client = new ApiClient("http://api.example:8000/", () => "clinician-token", fetchFn as never);
    await client.search("Patient");
    expect(fetchFn).toHaveBeenCalledTimes(1);
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://api.example:8000/fhir/Patient");
    const headers = new Headers(init.headers);
    expect(headers.get("Authorization")).toBe("Bearer clinician-token");
  });

  it("posts transitions as { to_status } JSON to the transition endpoint", async () => {
    const fetchFn = vi.fn(async () =>
      jsonResponse({ resourceType: "Appointment", id: "appt-1", status: "arrived" }),
    );
    const client = new ApiClient("http://localhost:8000", () => "clinician-token", fetchFn as never);
    const updated = await client.transitionAppointment("appt-1", "arrived");
    expect(updated.status).toBe("arrived");
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/fhir/Appointment/appt-1/transition");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ to_status: "arrived" });
  });

  it("maps a 403 response to an ApiError marked permission-denied", async () => {
    const fetchFn = vi.fn(async () =>
      jsonResponse({ detail: "Role viewer may not perform this action" }, 403),
    );
    const client = new ApiClient("http://localhost:8000", () => "viewer-token", fetchFn as never);
    await expect(client.inbox("pat-1")).rejects.toMatchObject({
      status: 403,
      detail: "Role viewer may not perform this action",
    });
    const err = await client.inbox("pat-1").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).isPermissionDenied).toBe(true);
  });

  it("maps a network failure to status 0 with an honest message", async () => {
    const fetchFn = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    const client = new ApiClient("http://localhost:8000", () => "clinician-token", fetchFn as never);
    const err = await client.search("Patient").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(0);
  });

  it("builds the inbox URL with the patient param", async () => {
    const fetchFn = vi.fn(async () =>
      jsonResponse({ resourceType: "Bundle", type: "searchset", entry: [] }),
    );
    const client = new ApiClient("http://localhost:8000", () => "clinician-token", fetchFn as never);
    await client.inbox("pat-1");
    const [url] = fetchFn.mock.calls[0] as unknown as [string];
    expect(url).toBe("http://localhost:8000/fhir/inbox?patient=pat-1");
  });

  it("fetches a single effective config object for runtime consumption", async () => {
    const fetchFn = vi.fn(async () =>
      jsonResponse({ kind: "branding", key: "default", payload: { orgName: "Mercy" } }),
    );
    const client = new ApiClient("http://localhost:8000", () => "clinician-token", fetchFn as never);
    const cfg = await client.effectiveConfig("branding", "default");
    expect(cfg.payload.orgName).toBe("Mercy");
    const [url] = fetchFn.mock.calls[0] as unknown as [string];
    expect(url).toBe("http://localhost:8000/studio/config/branding/effective/default");
  });

  it("posts the critical-value notification with read-back, and acknowledgement", async () => {
    const fetchFn = vi.fn(async () =>
      jsonResponse({ report_id: "rep-1", notification: { state: "notified" } }),
    );
    const client = new ApiClient("http://localhost:8000", () => "clinician-token", fetchFn as never);
    await client.notifyLabReport("rep-1", { notifiedTo: "Dr A", readBack: true });
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/lab/reports/rep-1/notify");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      notified_to: "Dr A",
      read_back: true,
    });
    await client.acknowledgeLabReport("rep-1");
    const [ackUrl] = fetchFn.mock.calls[1] as unknown as [string];
    expect(ackUrl).toBe("http://localhost:8000/lab/reports/rep-1/acknowledge");
  });
});

describe("ApiClient Phase 5 (orders, notes, flowsheets, MAR)", () => {
  function stubClient(body: unknown = {}) {
    const fetchFn = vi.fn(async () => jsonResponse(body));
    const client = new ApiClient("http://localhost:8000", () => "clinician-token", fetchFn as never);
    return { client, fetchFn };
  }

  it("places an order with the placement body unchanged", async () => {
    const { client, fetchFn } = stubClient({ order: { id: "o-1" }, warnings: [] });
    await client.placeOrder({
      type: "consult",
      code: "cardiology-consult",
      patient: "pat-1",
      performerRole: "clinician",
    });
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/orders");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      type: "consult",
      code: "cardiology-consult",
      patient: "pat-1",
      performerRole: "clinician",
    });
  });

  it("expands an order set and transitions orders via { to_status }", async () => {
    const { client, fetchFn } = stubClient({});
    await client.placeOrderSet({ set: "admission-general", patient: "pat-1" });
    let [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/orders/from-set");
    expect(JSON.parse(init.body as string)).toEqual({
      set: "admission-general",
      patient: "pat-1",
    });
    await client.transitionOrder("ord-1", "signed");
    [url, init] = fetchFn.mock.calls[1] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/orders/ord-1/transition");
    expect(JSON.parse(init.body as string)).toEqual({ to_status: "signed" });
  });

  it("creates a note with snake_case note_type and template answers", async () => {
    const { client, fetchFn } = stubClient({});
    const answers = [
      { linkId: "assessment", answer: [{ valueString: "Stable" }] },
    ];
    await client.createNote({ patient: "pat-1", noteType: "progress", answers });
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/notes");
    expect(JSON.parse(init.body as string)).toEqual({
      patient: "pat-1",
      note_type: "progress",
      answers,
    });
  });

  it("signs and amends notes on their dedicated endpoints", async () => {
    const { client, fetchFn } = stubClient({});
    await client.signNote("note-1");
    let [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/notes/note-1/sign");
    expect(init.method).toBe("POST");
    await client.amendNote("note-1", { text: "Correction" });
    [url, init] = fetchFn.mock.calls[1] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/notes/note-1/amend");
    expect(JSON.parse(init.body as string)).toEqual({ text: "Correction" });
  });

  it("queries the MAR window and the flowsheet view with patient scoping", async () => {
    const { client, fetchFn } = stubClient({});
    await client.marDue("pat-1", "2026-10-15T00:00:00Z", "2026-10-17T00:00:00Z");
    let [url] = fetchFn.mock.calls[0] as unknown as [string];
    expect(url).toContain("/orders/mar/due?");
    expect(url).toContain("patient=pat-1");
    expect(url).toContain("from=2026-10-15T00%3A00%3A00Z");
    await client.flowsheetView("flowsheet-vitals", "pat-1");
    [url] = fetchFn.mock.calls[1] as unknown as [string];
    expect(url).toBe("http://localhost:8000/flowsheets/flowsheet-vitals?patient=pat-1");
  });
});
