import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "./client";

// Request-shaping tests for the Phase 2 lab client methods: correct URLs,
// methods and JSON bodies against a stubbed fetch. No network is touched.
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

describe("lab client request shaping", () => {
  it("lists orders with the status filter and unwraps the Bundle", async () => {
    const { client, fetchFn } = stubClient(async () =>
      jsonResponse({
        resourceType: "Bundle", type: "searchset", total: 1,
        entry: [{ resource: { resourceType: "ServiceRequest", id: "o1", status: "ordered" } }],
      }),
    );
    const orders = await client.labOrders("ordered");
    expect(orders.map((o) => o.id)).toEqual(["o1"]);
    const [url] = fetchFn.mock.calls[0] as unknown as [string];
    expect(url).toBe("http://localhost:8000/lab/orders?status=ordered");
  });

  it("places an order as { code, subject.reference } JSON", async () => {
    const { client, fetchFn } = stubClient(async () =>
      jsonResponse({ resourceType: "ServiceRequest", id: "o2", status: "ordered" }, 201),
    );
    await client.createLabOrder({ code: "58410-2", patientId: "pat-1" });
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/lab/orders");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      code: "58410-2",
      subject: { reference: "Patient/pat-1" },
    });
  });

  it("posts transitions as { to_status, reason } to the order transition endpoint", async () => {
    const { client, fetchFn } = stubClient(async () => jsonResponse({ order: {}, specimen: null }));
    await client.transitionLabOrder("o1", "rejected", { reason: "Haemolysed" });
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/lab/orders/o1/transition");
    expect(JSON.parse(init.body as string)).toEqual({
      to_status: "rejected",
      reason: "Haemolysed",
    });
  });

  it("posts result entry and ingest payloads in the server shapes", async () => {
    const { client, fetchFn } = stubClient(async () => jsonResponse({}));
    await client.enterLabResults("o1", [{ code: "2823-3", value: 4.4 }]);
    let [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/lab/orders/o1/results");
    expect(JSON.parse(init.body as string)).toEqual({
      results: [{ code: "2823-3", value: 4.4 }],
    });

    await client.ingestLab("hl7", "MSH|^~\\&|...");
    [url, init] = fetchFn.mock.calls[1] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/lab/ingest");
    expect(JSON.parse(init.body as string)).toEqual({
      format: "hl7",
      content: "MSH|^~\\&|...",
    });
  });

  it("hits the verify/release endpoints and builds QC URLs", async () => {
    const { client, fetchFn } = stubClient(async () => jsonResponse({}));
    await client.verifyLabReport("rep-1");
    await client.releaseLabReport("rep-1");
    await client.qcRuns("2823-3");
    await client.qcStatus("2823-3");
    const urls = fetchFn.mock.calls.map((c) => (c as unknown as [string])[0]);
    expect(urls).toEqual([
      "http://localhost:8000/lab/reports/rep-1/verify",
      "http://localhost:8000/lab/reports/rep-1/release",
      "http://localhost:8000/lab/qc?analyte_code=2823-3",
      "http://localhost:8000/lab/qc/status?analyte_code=2823-3",
    ]);
  });
});
