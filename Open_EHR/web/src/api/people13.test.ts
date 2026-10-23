import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "./client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function clientReturning(body: unknown) {
  const fetchFn = vi.fn(async () => jsonResponse(body));
  const client = new ApiClient(
    "http://localhost:8000",
    () => "admin-token",
    fetchFn as never,
  );
  return { client, fetchFn };
}

function lastCall(fetchFn: ReturnType<typeof vi.fn>) {
  const [url, init] = fetchFn.mock.calls[fetchFn.mock.calls.length - 1] as [
    string,
    RequestInit,
  ];
  return { url, init, body: init.body ? JSON.parse(init.body as string) : null };
}

// Phase 13: the people & data platform client surface (/people,
// /reports in app/people.py + app/reports.py). These tests pin the
// wire mapping the same way continuum.test.ts pins Phase 12.
describe("ApiClient people & data platform (Phase 13)", () => {
  it("listStaff unwraps the directory items", async () => {
    const { client, fetchFn } = clientReturning({
      items: [{ staff_id: "staff-001", full_name: "Maya Chen, MD" }],
      total: 1,
    });
    const staff = await client.listStaff();
    expect(staff).toHaveLength(1);
    expect(lastCall(fetchFn).url).toBe("http://localhost:8000/people/staff");
  });

  it("credentialWorklist + scan hit the workforce endpoints", async () => {
    const { client, fetchFn } = clientReturning({
      items: [],
      total: 0,
      expired: 0,
      alert_window_days: 30,
      worklist_window_days: 90,
      scanned: 27,
      alerted: 7,
    });
    const wl = await client.credentialWorklist();
    expect(wl.worklist_window_days).toBe(90);
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/people/credentials/worklist",
    );
    const scan = await client.scanCredentials();
    expect(scan.alerted).toBe(7);
    const call = lastCall(fetchFn);
    expect(call.url).toBe("http://localhost:8000/people/credentials/scan");
    expect(call.init.method).toBe("POST");
  });

  it("listRoster pins one day via date_from/date_to", async () => {
    const { client, fetchFn } = clientReturning({ items: [], total: 0 });
    await client.listRoster("2026-10-18");
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/people/roster?date_from=2026-10-18&date_to=2026-10-18",
    );
  });

  it("onCallNow + homeDashboard read their endpoints", async () => {
    const { client, fetchFn } = clientReturning({
      services: [],
      total: 6,
      covered: 5,
      audience: "executive",
      dashboard: null,
    });
    const onCall = await client.onCallNow();
    expect(onCall.covered).toBe(5);
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/people/on-call/now",
    );
    const home = await client.homeDashboard();
    expect(home.audience).toBe("executive");
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/people/home-dashboard",
    );
  });

  it("runReport posts format + params and parses the download headers", async () => {
    const fetchFn = vi.fn(
      async () =>
        new Response("%PDF-fake", {
          status: 200,
          headers: {
            "Content-Type": "application/pdf",
            "Content-Disposition": 'attachment; filename="credential-expiry.pdf"',
            "X-Report-Rows": "7",
          },
        }),
    );
    const client = new ApiClient(
      "http://localhost:8000",
      () => "admin-token",
      fetchFn as never,
    );
    const file = await client.runReport("credential-expiry", "pdf", {
      window_days: 30,
    });
    const call = lastCall(fetchFn);
    expect(call.url).toBe("http://localhost:8000/reports/credential-expiry/run");
    expect(call.init.method).toBe("POST");
    expect(call.body).toEqual({ format: "pdf", params: { window_days: 30 } });
    expect(file.filename).toBe("credential-expiry.pdf");
    expect(file.rows).toBe(7);
    expect(file.blob.size).toBe("%PDF-fake".length);
  });

  it("document printers hit the audited document endpoints", async () => {
    const fetchFn = vi.fn(
      async () =>
        new Response("%PDF", {
          status: 200,
          headers: { "Content-Type": "application/pdf" },
        }),
    );
    const client = new ApiClient(
      "http://localhost:8000",
      () => "admin-token",
      fetchFn as never,
    );
    await client.printInvoice("claim-1");
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/reports/documents/invoice/claim-1",
    );
    await client.printReceipt("pay-1");
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/reports/documents/receipt/pay-1",
    );
    await client.printStatement("pat-1");
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/reports/documents/statement/pat-1",
    );
    await client.printSuperbill("enc-1");
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/reports/documents/superbill/enc-1",
    );
  });

  it("runExport posts dataset/format/reason; a 422 surfaces the reason rule", async () => {
    const { client, fetchFn } = clientReturning({});
    // requestBlob path: swap in a CSV response.
    const csvFetch = vi.fn(
      async () =>
        new Response("id\npat-1\n", {
          status: 200,
          headers: {
            "Content-Type": "text/csv",
            "Content-Disposition": 'attachment; filename="patients.csv"',
            "X-Export-Rows": "1",
          },
        }),
    );
    const csvClient = new ApiClient(
      "http://localhost:8000",
      () => "admin-token",
      csvFetch as never,
    );
    const file = await csvClient.runExport({
      dataset: "patients",
      format: "csv",
      reason: "Registry reconciliation",
    });
    const call = lastCall(csvFetch);
    expect(call.url).toBe("http://localhost:8000/reports/exports");
    expect(call.body).toEqual({
      dataset: "patients",
      format: "csv",
      reason: "Registry reconciliation",
    });
    expect(file.rows).toBe(1);
    void client;
    void fetchFn;

    const denied = new ApiClient(
      "http://localhost:8000",
      () => "admin-token",
      vi.fn(async () =>
        jsonResponse(
          { detail: "Export of 600 rows exceeds the configured threshold; a reason is required" },
          422,
        ),
      ) as never,
    );
    await expect(denied.runExport({ dataset: "patients" })).rejects.toThrow(
      /reason is required/,
    );
  });

  it("import validate/commit post profile + csv verbatim", async () => {
    const { client, fetchFn } = clientReturning({
      profile: "patient-intake",
      entity_type: "patients",
      rows_total: 2,
      valid: true,
      errors: [],
      preview: [],
      imported: 2,
    });
    const csv = "MRN,Last Name\npat-1,Doe\n";
    const validation = await client.validateImport("patient-intake", csv);
    expect(validation.valid).toBe(true);
    let call = lastCall(fetchFn);
    expect(call.url).toBe("http://localhost:8000/reports/imports/validate");
    expect(call.body).toEqual({ profile: "patient-intake", csv });
    const committed = await client.commitImport("patient-intake", csv);
    expect(committed.imported).toBe(2);
    call = lastCall(fetchFn);
    expect(call.url).toBe("http://localhost:8000/reports/imports/commit");
  });
});
