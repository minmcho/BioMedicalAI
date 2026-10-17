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
