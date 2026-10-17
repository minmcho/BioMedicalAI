import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "./client";

// Request-shaping tests for the Phase 6B lab client methods (benches +
// operations + post-analytical): correct URLs, methods and JSON bodies
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

function lastCall(fetchFn: ReturnType<typeof vi.fn>) {
  const call = fetchFn.mock.calls[fetchFn.mock.calls.length - 1] as unknown as [
    string,
    RequestInit,
  ];
  return { url: call[0], init: call[1] };
}

describe("lab client Phase 6B request shaping", () => {
  it("reads the collection worklist and recollects a rejected order", async () => {
    const { client, fetchFn } = stubClient(async () => jsonResponse({}));
    await client.collectionWorklist();
    expect(lastCall(fetchFn).url).toBe("http://localhost:8000/lab/collection-worklist");
    await client.recollectLabOrder("ord-1");
    const { url, init } = lastCall(fetchFn);
    expect(url).toBe("http://localhost:8000/lab/orders/ord-1/recollect");
    expect(init.method).toBe("POST");
  });

  it("amends a report with the mandatory reason + optional corrections", async () => {
    const { client, fetchFn } = stubClient(async () => jsonResponse({}, 201));
    await client.amendLabReport("rep-1", {
      reason: "Transcription error",
      results: [{ code: "2345-7", value: 95 }],
    });
    const { url, init } = lastCall(fetchFn);
    expect(url).toBe("http://localhost:8000/lab/reports/rep-1/amend");
    expect(JSON.parse(init.body as string)).toEqual({
      reason: "Transcription error",
      results: [{ code: "2345-7", value: 95 }],
    });
  });

  it("shapes the microbiology bench calls", async () => {
    const { client, fetchFn } = stubClient(async () => jsonResponse({}));
    await client.microTerminology();
    expect(lastCall(fetchFn).url).toBe("http://localhost:8000/lab/micro/terminology");
    await client.microPreliminary("ord-1", "Gram-negative rods seen");
    let call = lastCall(fetchFn);
    expect(call.url).toBe("http://localhost:8000/lab/micro/orders/ord-1/preliminary");
    expect(JSON.parse(call.init.body as string)).toEqual({
      text: "Gram-negative rods seen",
    });
    await client.microOrganism("ord-1", "escherichia-coli");
    call = lastCall(fetchFn);
    expect(call.url).toBe("http://localhost:8000/lab/micro/orders/ord-1/organism");
    expect(JSON.parse(call.init.body as string)).toEqual({
      organism_code: "escherichia-coli",
    });
    await client.microSusceptibility("ord-1", [
      { antibiotic: "ciprofloxacin", interpretation: "S" },
    ]);
    call = lastCall(fetchFn);
    expect(call.url).toBe(
      "http://localhost:8000/lab/micro/orders/ord-1/susceptibility",
    );
    await client.microFinalize("ord-1");
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/lab/micro/orders/ord-1/finalize",
    );
  });

  it("shapes the blood-bank calls, including the reaction hold clear", async () => {
    const { client, fetchFn } = stubClient(async () => jsonResponse({}));
    await client.createBloodUnit({
      unit_id: "SYN-U1", product_type: "rbc", blood_group: "O POS",
      expiry_date: "2099-01-01",
    });
    let call = lastCall(fetchFn);
    expect(call.url).toBe("http://localhost:8000/lab/blood-bank/units");
    await client.issueBloodUnit("SYN-U1", "ord-1");
    call = lastCall(fetchFn);
    expect(call.url).toBe("http://localhost:8000/lab/blood-bank/units/SYN-U1/issue");
    expect(JSON.parse(call.init.body as string)).toEqual({ order_id: "ord-1" });
    await client.recordTransfusion({ unit_id: "SYN-U1" });
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/lab/blood-bank/transfusions",
    );
    await client.reportTransfusionReaction("SYN-U1", "Fever and rigors");
    call = lastCall(fetchFn);
    expect(call.url).toBe(
      "http://localhost:8000/lab/blood-bank/units/SYN-U1/reaction",
    );
    expect(JSON.parse(call.init.body as string)).toEqual({
      symptoms: "Fever and rigors",
    });
    await client.clearBloodHold("pat-1");
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/lab/blood-bank/holds/pat-1/clear",
    );
  });

  it("shapes pathology, EQA, instruments, Levey-Jennings and TAT calls", async () => {
    const { client, fetchFn } = stubClient(async () => jsonResponse({}));
    await client.pathologySynopticForm();
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/lab/pathology/synoptic-form",
    );
    await client.pathologyReport("ord-1", [
      { linkId: "specimenType", answer: [{ valueString: "Colon biopsy" }] },
    ]);
    let call = lastCall(fetchFn);
    expect(call.url).toBe("http://localhost:8000/lab/pathology/orders/ord-1/report");
    expect(JSON.parse(call.init.body as string)).toEqual({
      answers: [
        { linkId: "specimenType", answer: [{ valueString: "Colon biopsy" }] },
      ],
    });
    await client.pathologySign("rep-1");
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/lab/pathology/reports/rep-1/sign",
    );
    await client.createEqa({
      event: "SYN-EQA-1", analyte_code: "2345-7", reported_value: 95,
      acceptable_low: 90, acceptable_high: 110,
    });
    expect(lastCall(fetchFn).url).toBe("http://localhost:8000/lab/eqa");
    await client.createInstrument({
      instrument_id: "SYN-AN-01", name: "Analyser", bench: "chemistry",
    });
    expect(lastCall(fetchFn).url).toBe("http://localhost:8000/lab/instruments");
    await client.setInstrumentStatus("SYN-AN-01", "down");
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/lab/instruments/SYN-AN-01/status",
    );
    await client.leveyJennings("2823-3", "SYN-QC-LOT-1");
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/lab/qc/levey-jennings?analyte_code=2823-3&control_material=SYN-QC-LOT-1",
    );
    await client.tatStats();
    expect(lastCall(fetchFn).url).toBe("http://localhost:8000/lab/tat");
  });

  it("sends qualitative result values and the ingest instrument id", async () => {
    const { client, fetchFn } = stubClient(async () => jsonResponse({}));
    await client.enterLabResults("ord-1", [
      { code: "5792-7", value: "Negative" },
      { code: "5803-2", value: 6.0 },
    ]);
    let call = lastCall(fetchFn);
    expect(JSON.parse(call.init.body as string)).toEqual({
      results: [
        { code: "5792-7", value: "Negative" },
        { code: "5803-2", value: 6.0 },
      ],
    });
    await client.ingestLab("csv", "order_id,...", "SYN-AN-01");
    call = lastCall(fetchFn);
    expect(JSON.parse(call.init.body as string)).toEqual({
      format: "csv",
      content: "order_id,...",
      instrument_id: "SYN-AN-01",
    });
  });

  it("sends the QC level and the coded rejection criterion", async () => {
    const { client, fetchFn } = stubClient(async () => jsonResponse({}));
    await client.createQcRun({
      analyte_code: "2823-3", control_material: "SYN-QC-LOT-1",
      expected_low: 3.0, expected_high: 5.5, measured_value: 4.25,
      level: "L1",
    });
    let call = lastCall(fetchFn);
    expect(JSON.parse(call.init.body as string).level).toBe("L1");
    await client.transitionLabOrder("ord-1", "rejected", {
      reason: "Visible haemolysis",
      reason_code: "hemolysed",
    });
    call = lastCall(fetchFn);
    expect(JSON.parse(call.init.body as string)).toEqual({
      to_status: "rejected",
      reason: "Visible haemolysis",
      reason_code: "hemolysed",
    });
  });
});
