import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "./client";

// Request-shaping tests for the Phase 4B specialty-board client methods
// (ED / radiology / surgery): correct URLs, methods and JSON bodies
// against a stubbed fetch. No network is touched.
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubClient(handler: (url: string, init?: RequestInit) => Promise<Response>) {
  const fetchFn = vi.fn(handler);
  const client = new ApiClient("http://localhost:8000", () => "clinician-token", fetchFn as never);
  return { client, fetchFn };
}

describe("ED board client", () => {
  it("fetches the board and posts arrivals", async () => {
    const { client, fetchFn } = stubClient(async () =>
      jsonResponse({ generated_at: "t", counts: { total: 0 }, groups: {} }));
    await client.edBoard();
    let [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/ed/board");
    expect(init.method ?? "GET").toBe("GET");

    await client.edArrive({ patientId: "pat-1", chiefComplaint: "Chest pain" });
    [url, init] = fetchFn.mock.calls[1] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/ed/arrivals");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      patient: "pat-1", chief_complaint: "Chest pain",
    });
  });

  it("posts triage with the ESI acuity and governed exits via their endpoints", async () => {
    const { client, fetchFn } = stubClient(async () =>
      jsonResponse({ resourceType: "Encounter", id: "enc-1" }));
    await client.edTransition("enc-1", "triage", 3);
    let [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/ed/encounters/enc-1/transition");
    expect(JSON.parse(init.body as string)).toEqual({ to_status: "triage", acuity: 3 });

    await client.edAdmit("enc-1");
    [url] = fetchFn.mock.calls[1] as unknown as [string];
    expect(url).toBe("http://localhost:8000/ed/encounters/enc-1/admit");

    await client.edDepart("enc-1");
    [url] = fetchFn.mock.calls[2] as unknown as [string];
    expect(url).toBe("http://localhost:8000/ed/encounters/enc-1/depart");
  });
});

describe("radiology client", () => {
  it("creates a study from an order and unwraps the studies list", async () => {
    const { client, fetchFn } = stubClient(async (url) => {
      if (url.endsWith("/radiology/studies") && !url.includes("?")) {
        return jsonResponse({ items: [], total: 0 });
      }
      return jsonResponse({ study: { resourceType: "ImagingStudy", id: "s1" }, has_images: false }, 201);
    });
    await client.createStudy({ serviceRequestId: "sr-1", modality: "CT" });
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/radiology/studies");
    expect(JSON.parse(init.body as string)).toEqual({
      service_request_id: "sr-1", modality: "CT",
    });
    const items = await client.radiologyStudies();
    expect(items).toEqual([]);
  });

  it("drafts and verifies imaging reports", async () => {
    const { client, fetchFn } = stubClient(async () =>
      jsonResponse({ resourceType: "DiagnosticReport", id: "r1", status: "preliminary" }, 201));
    await client.createImagingReport("s1", "No acute findings.");
    let [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/radiology/studies/s1/report");
    expect(JSON.parse(init.body as string)).toEqual({ conclusion: "No acute findings." });

    await client.verifyImagingReport("r1");
    [url, init] = fetchFn.mock.calls[1] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/radiology/reports/r1/verify");
    expect(init.method).toBe("POST");
  });
});

describe("surgery board client", () => {
  it("fetches the board for a date and schedules a case", async () => {
    const { client, fetchFn } = stubClient(async () =>
      jsonResponse({ date: "2026-10-20", rooms: [], total: 0 }));
    await client.surgeryBoard("2026-10-20");
    let [url] = fetchFn.mock.calls[0] as unknown as [string];
    expect(url).toBe("http://localhost:8000/surgery/board?date=2026-10-20");

    await client.scheduleCase({
      patientId: "pat-1", code: "27447", room: "OR-1",
      start: "2026-10-20T08:00:00.000Z", end: "2026-10-20T10:00:00.000Z",
    });
    let init: RequestInit;
    [url, init] = fetchFn.mock.calls[1] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/surgery/cases");
    expect(JSON.parse(init.body as string)).toEqual({
      patient_id: "pat-1", code: "27447", room: "OR-1",
      start: "2026-10-20T08:00:00.000Z", end: "2026-10-20T10:00:00.000Z",
    });
  });

  it("transitions and reschedules cases; a room conflict surfaces as ApiError 409", async () => {
    const { client, fetchFn } = stubClient(async (url) => {
      if (url.includes("/reschedule")) {
        return jsonResponse(
          { detail: "Room OR-1 is already booked over that time" }, 409);
      }
      return jsonResponse({ case: {}, appointment: {}, state: "pre-op" });
    });
    await client.transitionCase("case-1", "pre-op");
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/surgery/cases/case-1/transition");
    expect(JSON.parse(init.body as string)).toEqual({ to_status: "pre-op" });

    await expect(
      client.rescheduleCase("case-1", "2026-10-20T09:00:00Z", "2026-10-20T11:00:00Z"),
    ).rejects.toMatchObject({ status: 409 });
    await expect(
      client.rescheduleCase("case-1", "2026-10-20T09:00:00Z", "2026-10-20T11:00:00Z"),
    ).rejects.toBeInstanceOf(ApiError);
  });
});
