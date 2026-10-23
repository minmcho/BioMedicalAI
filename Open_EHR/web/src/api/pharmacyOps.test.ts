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
    () => "clinician-token",
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

// Phase 11: the pharmacy-ops client surface (app/pharmacy_ops.py).
// These tests pin the wire mapping — paths, verbs and snake_case bodies —
// the same way client.test.ts pins the earlier phases.
describe("ApiClient pharmacy operations (Phase 11)", () => {
  it("pharmacyVerificationQueue unwraps items from /pharmacy/verification-queue", async () => {
    const { client, fetchFn } = clientReturning({
      items: [{ order_id: "ord-1" }],
      total: 1,
    });
    const items = await client.pharmacyVerificationQueue();
    expect(items).toHaveLength(1);
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/pharmacy/verification-queue",
    );
  });

  it("verifyOrder posts the restriction acknowledgement", async () => {
    const { client, fetchFn } = clientReturning({
      verification: { order_id: "ord-1" },
      rule_firings: [],
    });
    await client.verifyOrder("ord-1", { restrictionAcknowledged: true });
    const { url, init, body } = lastCall(fetchFn);
    expect(url).toBe("http://localhost:8000/pharmacy/verification/ord-1/verify");
    expect(init.method).toBe("POST");
    expect(body).toEqual({ restriction_acknowledged: true });
  });

  it("receivePurchaseOrder maps lines with DSCSA serials", async () => {
    const { client, fetchFn } = clientReturning({ po_id: "po-1", status: "received" });
    await client.receivePurchaseOrder("po-1", {
      location: "adc-icu",
      lines: [
        {
          medicationCode: "1049221",
          quantity: 2,
          lotNumber: "LOT-A",
          expiryDate: "2028-01-01",
          serials: ["SN-1", "SN-2"],
        },
      ],
    });
    const { url, body } = lastCall(fetchFn);
    expect(url).toBe(
      "http://localhost:8000/pharmacy/procurement/orders/po-1/receive",
    );
    expect(body).toEqual({
      location: "adc-icu",
      lines: [
        {
          medication_code: "1049221",
          quantity: 2,
          lot_number: "LOT-A",
          expiry_date: "2028-01-01",
          serials: ["SN-1", "SN-2"],
        },
      ],
    });
  });

  it("csCount and csResolve hit the count-book endpoints", async () => {
    const { client, fetchFn } = clientReturning({ count_id: "csc-1" });
    await client.csCount({
      medicationCode: "1049625",
      location: "main-pharmacy",
      countedQuantity: 38,
      witness: "nurse-2",
    });
    let call = lastCall(fetchFn);
    expect(call.url).toBe("http://localhost:8000/pharmacy/cs/counts");
    expect(call.body).toEqual({
      medication_code: "1049625",
      location: "main-pharmacy",
      counted_quantity: 38,
      witness: "nurse-2",
    });
    await client.csResolve("csc-1", "Two tablets wasted, record filed");
    call = lastCall(fetchFn);
    expect(call.url).toBe(
      "http://localhost:8000/pharmacy/cs/counts/csc-1/resolve",
    );
    expect(call.body).toEqual({ reason: "Two tablets wasted, record filed" });
  });

  it("bcmaAdminister posts payloads and includes the override only when given", async () => {
    const { client, fetchFn } = clientReturning({
      administration: { id: "adm-1" },
      bcma: { checks: [], override: null },
    });
    await client.bcmaAdminister({
      wristbandPayload: "OCWB1|pat-1|19620314|10",
      medLabelPayload: "OCML1|disp-1|LOT-A|55",
    });
    let call = lastCall(fetchFn);
    expect(call.url).toBe("http://localhost:8000/pharmacy/bcma/administer");
    expect(call.body).toEqual({
      wristband_payload: "OCWB1|pat-1|19620314|10",
      med_label_payload: "OCML1|disp-1|LOT-A|55",
    });
    await client.bcmaAdminister({
      wristbandPayload: "OCWB1|pat-1|19620314|10",
      medLabelPayload: "OCML1|disp-1|LOT-A|55",
      overrideReason: "Dose delayed by procedure",
    });
    call = lastCall(fetchFn);
    expect(call.body.override_reason).toBe("Dose delayed by procedure");
  });

  it("patientDispenses unwraps the patient's dispense history", async () => {
    const { client, fetchFn } = clientReturning({
      items: [{ dispense_id: "disp-1", lot_number: "LOT-A" }],
      total: 1,
    });
    const items = await client.patientDispenses("pat-1");
    expect(items[0].lot_number).toBe("LOT-A");
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/pharmacy/patients/pat-1/dispenses",
    );
  });

  it("interactionCheck reports the request body verbatim", async () => {
    const { client, fetchFn } = clientReturning({
      status: "no-source",
      pairs_checked: 0,
      findings: [],
    });
    const out = await client.interactionCheck(["11951", "1049221"]);
    expect(out.status).toBe("no-source");
    expect(lastCall(fetchFn).body).toEqual({
      medication_codes: ["11951", "1049221"],
    });
  });

  it("createCompoundBatch maps ingredients with lot capture", async () => {
    const { client, fetchFn } = clientReturning({ batch_id: "cpd-1" });
    await client.createCompoundBatch({
      preparationName: "Synthetic IV admixture",
      category: "category-1",
      storage: "refrigerated",
      ingredients: [{ name: "Bulk powder", quantity: "10 mg", lotNumber: "ING-1" }],
    });
    const { url, body } = lastCall(fetchFn);
    expect(url).toBe("http://localhost:8000/pharmacy/compounding/batches");
    expect(body).toEqual({
      preparation_name: "Synthetic IV admixture",
      category: "category-1",
      storage: "refrigerated",
      ingredients: [
        { name: "Bulk powder", quantity: "10 mg", lot_number: "ING-1" },
      ],
    });
  });
});
