/**
 * Phase 17 (BD-69) — realtime client: subscribe + fallback.
 *
 * The primary path consumes the SSE stream via fetch/ReadableStream;
 * when the stream is refused the client must fall back to polling
 * /realtime/events and keep delivering the same events in order.
 */
import { describe, expect, it, vi } from "vitest";

import { connectRealtime, parseSseBuffer, type RealtimeEvent } from "./realtime";

const EVENT: RealtimeEvent = {
  id: 3,
  channel: "command",
  type: "board.changed",
  payload: { source: "adt", event_type: "A01" },
  ts: "2026-10-19T00:00:00Z",
};

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

describe("parseSseBuffer", () => {
  it("extracts complete data frames and keeps the partial tail", () => {
    const wire = `: connected\n\ndata: ${JSON.stringify(EVENT)}\n\ndata: {"id":4`;
    const { events, rest } = parseSseBuffer(wire);
    expect(events).toHaveLength(1);
    expect(events[0].id).toBe(3);
    expect(rest).toContain('"id":4');
  });
});

describe("connectRealtime", () => {
  it("delivers events over the SSE stream and reports stream mode", async () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          encoder.encode(`id: 3\nevent: board.changed\ndata: ${JSON.stringify(EVENT)}\n\n`),
        );
        // Stay open (a live stream); the client closes via abort.
      },
    });
    const fetchFn = vi.fn(async () =>
      new Response(stream, { status: 200 }),
    ) as unknown as typeof fetch;
    const seen: RealtimeEvent[] = [];
    const modes: string[] = [];
    const conn = connectRealtime({
      baseUrl: "http://api.test",
      getToken: () => "clinician-token",
      fetchFn,
      onEvent: (e) => seen.push(e),
      onMode: (m) => modes.push(m),
    });
    await sleep(50);
    expect(seen.map((e) => e.id)).toEqual([3]);
    expect(modes).toContain("stream");
    expect(conn.mode()).toBe("stream");
    // The bearer token rode the stream request (EventSource cannot).
    const init = (fetchFn as unknown as ReturnType<typeof vi.fn>).mock
      .calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>)["Authorization"]).toBe(
      "Bearer clinician-token",
    );
    conn.close();
  });

  it("falls back to polling when the stream is refused", async () => {
    let calls = 0;
    const fetchFn = vi.fn(async (url: unknown) => {
      calls += 1;
      const u = String(url);
      if (u.includes("/realtime/stream")) {
        return new Response("nope", { status: 503 });
      }
      if (u.includes("/realtime/events")) {
        return new Response(
          JSON.stringify({ channel: "command", events: [EVENT], latest_id: 3 }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`unexpected ${u}`);
    }) as unknown as typeof fetch;
    const seen: RealtimeEvent[] = [];
    const modes: string[] = [];
    const conn = connectRealtime({
      baseUrl: "http://api.test",
      getToken: () => "clinician-token",
      fetchFn,
      pollIntervalMs: 10,
      onEvent: (e) => seen.push(e),
      onMode: (m) => modes.push(m),
    });
    await sleep(80);
    expect(modes).toContain("polling");
    expect(conn.mode()).toBe("polling");
    expect(seen.map((e) => e.id)).toEqual([3]); // delivered once (id dedupe)
    expect(calls).toBeGreaterThan(2); // stream attempt + repeated polls
    conn.close();
  });
});
