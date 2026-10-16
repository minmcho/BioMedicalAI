import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "./client";

// Request-shaping tests for the Phase 4A billing + pharmacy client
// methods: correct URLs, methods and JSON bodies against a stubbed fetch.
// No network is touched.
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubClient(handler: (url: string, init?: RequestInit) => Promise<Response>) {
  const fetchFn = vi.fn(handler);
  const client = new ApiClient("http://localhost:8000", () => "admin-token", fetchFn as never);
  return { client, fetchFn };
}

describe("billing client request shaping", () => {
  it("generates a claim from an encounter id", async () => {
    const { client, fetchFn } = stubClient(async () =>
      jsonResponse({ resourceType: "Claim", id: "c1", status: "draft" }, 201),
    );
    await client.generateClaim("enc-1");
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/billing/claims/generate");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ encounter_id: "enc-1" });
  });

  it("lists claims with status/patient filters and unwraps the Bundle", async () => {
    const { client, fetchFn } = stubClient(async () =>
      jsonResponse({
        resourceType: "Bundle", type: "searchset", total: 1,
        entry: [{ resource: { resourceType: "Claim", id: "c1", status: "ready" } }],
      }),
    );
    const claims = await client.claims({ status: "ready", patient: "pat-1" });
    expect(claims.map((c) => c.id)).toEqual(["c1"]);
    const [url] = fetchFn.mock.calls[0] as unknown as [string];
    expect(url).toBe("http://localhost:8000/billing/claims?status=ready&patient=pat-1");
  });

  it("posts lifecycle transitions as { to_status } and appeals with a note", async () => {
    const { client, fetchFn } = stubClient(async () =>
      jsonResponse({ resourceType: "Claim", id: "c1" }),
    );
    await client.transitionClaim("c1", "submitted");
    let [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/billing/claims/c1/transition");
    expect(JSON.parse(init.body as string)).toEqual({ to_status: "submitted" });

    await client.transitionClaim("c1", "appealed", "Records attached.");
    [url, init] = fetchFn.mock.calls[1] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      to_status: "appealed",
      note: "Records attached.",
    });
  });

  it("downloads the 837 export as raw text", async () => {
    const edi = "ISA*00*~\nGS*HC*~\n";
    const { client, fetchFn } = stubClient(async () =>
      new Response(edi, { status: 200, headers: { "Content-Type": "text/plain" } }),
    );
    const text = await client.export837("c1");
    expect(text).toBe(edi);
    const [url] = fetchFn.mock.calls[0] as unknown as [string];
    expect(url).toBe("http://localhost:8000/billing/claims/c1/export/837");
  });

  it("maps a 409 draft-export error to ApiError with the server detail", async () => {
    const { client } = stubClient(async () =>
      jsonResponse({ detail: "Claim c1 is in status 'draft'; an 837 export requires a submitted claim" }, 409),
    );
    await expect(client.export837("c1")).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
    });
    await expect(client.export837("c1")).rejects.toThrow(/requires a submitted claim/);
    expect(ApiError).toBeDefined();
  });
});

describe("pharmacy client request shaping", () => {
  it("reads the dispense queue and stock from their endpoints", async () => {
    const { client, fetchFn } = stubClient(async (url) => {
      if (url.endsWith("/pharmacy/dispense-queue")) {
        return jsonResponse({ items: [{ request: { id: "m1" } }], total: 1 });
      }
      return jsonResponse({ items: [{ code: "1049221", quantity: 120 }] });
    });
    const queue = await client.dispenseQueue();
    expect(queue).toHaveLength(1);
    const stock = await client.stock();
    expect(stock[0].code).toBe("1049221");
    const urls = fetchFn.mock.calls.map((c) => (c as unknown as [string])[0]);
    expect(urls).toEqual([
      "http://localhost:8000/pharmacy/dispense-queue",
      "http://localhost:8000/pharmacy/stock",
    ]);
  });

  it("posts dispenses as { medication_request_id, quantity }", async () => {
    const { client, fetchFn } = stubClient(async () =>
      jsonResponse({ dispense: { id: "d1" }, medication_request: {}, stock_after: 110 }, 201),
    );
    const outcome = await client.dispense("med-1", 10);
    expect(outcome.stock_after).toBe(110);
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/pharmacy/dispense");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      medication_request_id: "med-1",
      quantity: 10,
    });
  });

  it("posts BCMA scans in the server field names", async () => {
    const { client, fetchFn } = stubClient(async () =>
      jsonResponse({ resourceType: "MedicationAdministration", id: "a1" }, 201),
    );
    await client.administer({ wristbandId: "pat-1", medicationCode: "1049221", dispenseId: "d1" });
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/pharmacy/administer");
    expect(JSON.parse(init.body as string)).toEqual({
      wristband_id: "pat-1",
      medication_code: "1049221",
      dispense_id: "d1",
    });
  });
});
