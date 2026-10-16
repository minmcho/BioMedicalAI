import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "./client";

// Phase 3 client methods: exact paths + payloads for /pophealth and
// /warehouse, exercised against a stub fetch (unit-test boundary only —
// shipped code always uses the real client).
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function clientWith(fetchFn: typeof fetch): ApiClient {
  return new ApiClient("http://localhost:8000", () => "clinician-token", fetchFn);
}

describe("pophealth client methods", () => {
  it("registries + summary hit the /pophealth paths", async () => {
    const fetchFn = vi.fn(async () => jsonResponse([]));
    const client = clientWith(fetchFn as unknown as typeof fetch);
    await client.registries();
    await client.registrySummary("diabetes");
    const urls = (fetchFn.mock.calls as unknown as Array<[string]>).map((c) => c[0]);
    expect(urls[0]).toContain("/pophealth/registries");
    expect(urls[1]).toContain("/pophealth/registries/diabetes/summary");
  });

  it("exploreCohort POSTs the filter payload", async () => {
    const fetchFn = vi.fn(async () =>
      jsonResponse({ filters: {}, count: "<5", suppressed: true }),
    );
    const client = clientWith(fetchFn as unknown as typeof fetch);
    const result = await client.exploreCohort({
      conditionCode: "44054006",
      labCode: "4548-4",
      comparator: ">",
      threshold: 7.0,
    });
    const [, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      conditionCode: "44054006",
      labCode: "4548-4",
      comparator: ">",
      threshold: 7.0,
    });
    expect(result.suppressed).toBe(true);
  });

  it("warehouseStatus + runEtl hit the /warehouse paths", async () => {
    const fetchFn = vi.fn(async () => jsonResponse({}));
    const client = clientWith(fetchFn as unknown as typeof fetch);
    await client.warehouseStatus();
    await client.runEtl();
    const calls = fetchFn.mock.calls as unknown as Array<[string, RequestInit]>;
    expect(calls[0][0]).toContain("/warehouse/status");
    expect(calls[1][0]).toContain("/warehouse/etl/run");
    expect(calls[1][1].method).toBe("POST");
  });
});
