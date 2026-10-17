import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "./client";

// Phase 9 client methods: exact paths + payloads for /quality,
// /pophealth snapshots+export, /messages, /portal, /engagement —
// exercised against a stub fetch (unit-test boundary only).
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function clientWith(fetchFn: typeof fetch): ApiClient {
  return new ApiClient("http://localhost:8000", () => "clinician-token", fetchFn);
}

function calls(fetchFn: ReturnType<typeof vi.fn>) {
  return fetchFn.mock.calls as unknown as Array<[string, RequestInit]>;
}

describe("phase 9 client methods", () => {
  it("quality measures + result hit the /quality paths", async () => {
    const fetchFn = vi.fn(async () => jsonResponse({}));
    const client = clientWith(fetchFn as unknown as typeof fetch);
    await client.qualityMeasures();
    await client.qualityMeasureResult("hba1c-control");
    const urls = calls(fetchFn).map((c) => c[0]);
    expect(urls[0]).toContain("/quality/measures");
    expect(urls[1]).toContain("/quality/measures/hba1c-control/result");
  });

  it("snapshots list + take, and the aggregate export path", async () => {
    const fetchFn = vi.fn(async () => jsonResponse({}));
    const client = clientWith(fetchFn as unknown as typeof fetch);
    await client.registrySnapshots("diabetes");
    await client.takeRegistrySnapshot("diabetes");
    await client.exportRegistry("diabetes");
    const cs = calls(fetchFn);
    expect(cs[0][0]).toContain("/pophealth/registries/diabetes/snapshots");
    expect(cs[1][0]).toContain("/pophealth/registries/diabetes/snapshots");
    expect(cs[1][1].method).toBe("POST");
    expect(cs[2][0]).toContain("/pophealth/registries/diabetes/export");
  });

  it("staff messaging: inbox, send, reply, read, unread", async () => {
    const fetchFn = vi.fn(async () => jsonResponse({}));
    const client = clientWith(fetchFn as unknown as typeof fetch);
    await client.messageInbox("pat-1");
    await client.sendMessage("pat-1", "Care-plan update");
    await client.messageThread("thread-1");
    await client.replyToThread("thread-1", "Thanks");
    await client.markThreadRead("thread-1");
    await client.messageUnread();
    const cs = calls(fetchFn);
    expect(cs[0][0]).toContain("/messages/inbox?patient=pat-1");
    expect(cs[1][0]).toContain("/messages");
    expect(JSON.parse(String(cs[1][1].body))).toEqual({
      patient: "pat-1",
      text: "Care-plan update",
    });
    expect(cs[2][0]).toContain("/messages/threads/thread-1");
    expect(cs[3][0]).toContain("/messages/threads/thread-1/reply");
    expect(JSON.parse(String(cs[3][1].body))).toEqual({ text: "Thanks" });
    expect(cs[4][0]).toContain("/messages/threads/thread-1/read");
    expect(cs[4][1].method).toBe("POST");
    expect(cs[5][0]).toContain("/messages/unread");
  });

  it("portal: my messages, send, appointments, self-scheduling", async () => {
    const fetchFn = vi.fn(async () => jsonResponse({}));
    const client = clientWith(fetchFn as unknown as typeof fetch);
    await client.myMessages();
    await client.sendPortalMessage("Question about my lab");
    await client.myAppointments();
    await client.selfSchedulingConfig();
    await client.requestAppointment({
      slot_type: "follow-up",
      start: "2026-11-05T09:00:00.000Z",
    });
    const cs = calls(fetchFn);
    expect(cs[0][0]).toContain("/portal/my-messages");
    expect(cs[1][0]).toContain("/portal/messages");
    expect(JSON.parse(String(cs[1][1].body))).toEqual({
      text: "Question about my lab",
    });
    expect(cs[2][0]).toContain("/portal/my-appointments");
    expect(cs[3][0]).toContain("/portal/self-scheduling/config");
    expect(cs[4][0]).toContain("/portal/appointments/request");
    expect(JSON.parse(String(cs[4][1].body))).toEqual({
      slot_type: "follow-up",
      start: "2026-11-05T09:00:00.000Z",
    });
  });

  it("telehealth workspace + intake paths (portal and staff)", async () => {
    const fetchFn = vi.fn(async () => jsonResponse({}));
    const client = clientWith(fetchFn as unknown as typeof fetch);
    await client.telehealthWorkspace("appt-1");
    await client.submitTelehealthIntake("appt-1", [
      { linkId: "chief-concern", answer: [{ valueString: "Check-up" }] },
    ]);
    await client.staffTelehealthWorkspace("appt-1");
    const cs = calls(fetchFn);
    expect(cs[0][0]).toContain("/portal/telehealth/appointments/appt-1/workspace");
    expect(cs[1][0]).toContain("/portal/telehealth/appointments/appt-1/intake");
    expect(cs[1][1].method).toBe("POST");
    expect(JSON.parse(String(cs[1][1].body))).toEqual({
      answers: [{ linkId: "chief-concern", answer: [{ valueString: "Check-up" }] }],
    });
    expect(cs[2][0]).toContain("/engagement/telehealth/appointments/appt-1/workspace");
  });
});
