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

// Phase 12: the care-continuum client surface (app/continuum.py).
// These tests pin the wire mapping — paths, verbs and snake_case
// bodies — the same way pharmacyOps.test.ts pins Phase 11.
describe("ApiClient care-continuum operations (Phase 12)", () => {
  it("continuumErBoard reads the enriched ER board", async () => {
    const { client, fetchFn } = clientReturning({ counts: { total: 2 } });
    const board = await client.continuumErBoard();
    expect(board.counts.total).toBe(2);
    expect(lastCall(fetchFn).url).toBe("http://localhost:8000/continuum/er/board");
  });

  it("setDiversion posts status + surge level for the banner", async () => {
    const { client, fetchFn } = clientReturning({ diversion: "diversion-ed" });
    await client.setDiversion({ status: "diversion-ed", surgeLevel: "surge", reason: "no ICU beds" });
    const { url, init, body } = lastCall(fetchFn);
    expect(url).toBe("http://localhost:8000/continuum/er/diversion");
    expect(init.method).toBe("POST");
    expect(body).toEqual({
      status: "diversion-ed", surge_level: "surge", reason: "no ICU beds",
    });
  });

  it("startActivation + milestone + close hit the activation endpoints", async () => {
    const { client, fetchFn } = clientReturning({ id: "act-1" });
    await client.startActivation("enc-1", "stemi");
    let call = lastCall(fetchFn);
    expect(call.url).toBe("http://localhost:8000/continuum/er/activations");
    expect(call.body).toEqual({ encounter_id: "enc-1", pathway: "stemi" });
    await client.recordActivationMilestone("act-1", "ecg");
    call = lastCall(fetchFn);
    expect(call.url).toBe("http://localhost:8000/continuum/er/activations/act-1/milestone");
    expect(call.body).toEqual({ code: "ecg" });
    await client.closeActivation("act-1", "pci-done");
    call = lastCall(fetchFn);
    expect(call.url).toBe("http://localhost:8000/continuum/er/activations/act-1/close");
    expect(call.body).toEqual({ outcome: "pci-done" });
  });

  it("recordTransferOut posts governed-form answers to the EMTALA gate", async () => {
    const { client, fetchFn } = clientReturning({ transfer_response_id: "qr-1" });
    const answers = [{ linkId: "receiving-facility", answer: [{ valueString: "County" }] }];
    await client.recordTransferOut("enc-9", answers);
    const { url, body } = lastCall(fetchFn);
    expect(url).toBe("http://localhost:8000/continuum/er/encounters/enc-9/transfer-out");
    expect(body).toEqual({ answers });
  });

  it("assignNurse posts encounter, nurse and acuity", async () => {
    const { client, fetchFn } = clientReturning({ id: 1 });
    await client.assignNurse({ encounterId: "enc-2", nurse: "nurse-jane", acuity: 4 });
    const { url, body } = lastCall(fetchFn);
    expect(url).toBe("http://localhost:8000/continuum/inpatient/assignments");
    expect(body).toEqual({ encounter_id: "enc-2", nurse: "nurse-jane", acuity: 4 });
  });

  it("clinicBook posts patient/session/date and surfaces overbooked", async () => {
    const { client, fetchFn } = clientReturning({
      appointment: { resourceType: "Appointment", id: "a-1" },
      start: "2026-10-20T09:00:00Z", overbooked: true,
    });
    const out = await client.clinicBook({ patient: "pat-1", session: "am-clinic", date: "2026-10-20" });
    expect(out.overbooked).toBe(true);
    const { url, body } = lastCall(fetchFn);
    expect(url).toBe("http://localhost:8000/continuum/clinic/book");
    expect(body).toEqual({ patient: "pat-1", session: "am-clinic", date: "2026-10-20" });
  });

  it("noShowRisks unwraps items for the date", async () => {
    const { client, fetchFn } = clientReturning({
      items: [{ appointment_id: "a-1", high_risk: true }], total: 1,
    });
    const items = await client.noShowRisks("2026-10-20");
    expect(items).toHaveLength(1);
    expect(lastCall(fetchFn).url).toBe(
      "http://localhost:8000/continuum/clinic/no-show-risks?date=2026-10-20",
    );
  });

  it("decideTransfer posts the decision and decline reason", async () => {
    const { client, fetchFn } = clientReturning({ id: "tr-1", status: "declined" });
    await client.decideTransfer("tr-1", "decline", "no ICU capacity");
    const { url, body } = lastCall(fetchFn);
    expect(url).toBe("http://localhost:8000/continuum/command/transfers/tr-1/decide");
    expect(body).toEqual({ decision: "decline", reason: "no ICU capacity" });
  });

  it("commandAssignBed posts the bed code", async () => {
    const { client, fetchFn } = clientReturning({ event: { event_type: "A01" } });
    await client.commandAssignBed("enc-3", "MS-101A");
    const { url, body } = lastCall(fetchFn);
    expect(url).toBe("http://localhost:8000/continuum/command/bed-requests/enc-3/assign");
    expect(body).toEqual({ bed: "MS-101A" });
  });

  it("fileSafetyEvent maps camelCase input to the wire body", async () => {
    const { client, fetchFn } = clientReturning({ id: "sev-1", reporter: "confidential" });
    await client.fileSafetyEvent({
      description: "Near miss", category: "medication",
      harmLevel: "near-miss", unit: "unit-icu", confidential: true,
    });
    const { url, body } = lastCall(fetchFn);
    expect(url).toBe("http://localhost:8000/continuum/safety/events");
    expect(body).toEqual({
      description: "Near miss", category: "medication",
      harm_level: "near-miss", unit: "unit-icu",
      patient: undefined, confidential: true,
    });
  });

  it("triageSafetyEvent posts status/harm/rca fields", async () => {
    const { client, fetchFn } = clientReturning({ id: "sev-1", status: "closed" });
    await client.triageSafetyEvent("sev-1", { status: "closed", note: "RCA complete" });
    const { url, body } = lastCall(fetchFn);
    expect(url).toBe("http://localhost:8000/continuum/safety/events/sev-1/triage");
    expect(body.status).toBe("closed");
    expect(body.note).toBe("RCA complete");
  });

  it("recordAftercareCall posts the scripted outcome", async () => {
    const { client, fetchFn } = clientReturning({ id: 1 });
    await client.recordAftercareCall({
      encounterId: "enc-4", outcome: "reached-well", reached: true,
    });
    const { url, body } = lastCall(fetchFn);
    expect(url).toBe("http://localhost:8000/continuum/aftercare/calls");
    expect(body).toEqual({
      encounter_id: "enc-4", outcome: "reached-well", reached: true, note: undefined,
    });
  });
});
