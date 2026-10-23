/**
 * Realtime fan-out client — Phase 17 (BD-69).
 *
 * Subscribes to the backend's command channel (app.realtime):
 * primary path is the SSE stream at GET /realtime/stream, consumed
 * through fetch + ReadableStream because EventSource cannot send the
 * bearer token. When the stream cannot be held (network drop, a
 * proxy that buffers SSE, an environment without ReadableStream),
 * the client AUTOMATICALLY falls back to polling the identical
 * buffered events at GET /realtime/events — both paths deliver the
 * same event objects, in id order, with replay from the last seen
 * id. The current mode is reported via onMode so the UI can show
 * "Live" vs "Polling" honestly.
 */

export interface RealtimeEvent {
  id: number;
  channel: string;
  type: string;
  payload: Record<string, unknown>;
  ts: string;
}

export type RealtimeMode = "stream" | "polling";

export interface RealtimeOptions {
  baseUrl: string;
  getToken: () => string | null;
  fetchFn?: typeof fetch;
  channel?: string;
  pollIntervalMs?: number;
  onEvent: (event: RealtimeEvent) => void;
  onMode?: (mode: RealtimeMode) => void;
}

export interface RealtimeConnection {
  close: () => void;
  mode: () => RealtimeMode;
}

/** Parse one SSE buffer into complete events + the remainder. */
export function parseSseBuffer(buffer: string): {
  events: RealtimeEvent[];
  rest: string;
} {
  const events: RealtimeEvent[] = [];
  const blocks = buffer.split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    for (const line of block.split("\n")) {
      if (line.startsWith("data:")) {
        try {
          events.push(JSON.parse(line.slice(5).trim()) as RealtimeEvent);
        } catch {
          // A malformed frame is skipped, never fatal to the stream.
        }
      }
    }
  }
  return { events, rest };
}

export function connectRealtime(opts: RealtimeOptions): RealtimeConnection {
  const fetchFn = opts.fetchFn ?? fetch;
  const channel = opts.channel ?? "command";
  const pollIntervalMs = opts.pollIntervalMs ?? 5000;
  const base = opts.baseUrl.replace(/\/$/, "");
  let closed = false;
  let mode: RealtimeMode = "polling";
  let lastId = 0;
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  const controller = new AbortController();

  const headers = (): Record<string, string> => {
    const h: Record<string, string> = {};
    const token = opts.getToken();
    if (token) h["Authorization"] = `Bearer ${token}`;
    return h;
  };

  const deliver = (event: RealtimeEvent) => {
    if (event.id > lastId) {
      lastId = event.id;
      opts.onEvent(event);
    }
  };

  const setMode = (m: RealtimeMode) => {
    mode = m;
    opts.onMode?.(m);
  };

  const pollOnce = async () => {
    const resp = await fetchFn(
      `${base}/realtime/events?channel=${encodeURIComponent(channel)}&since=${lastId}`,
      { headers: headers() },
    );
    if (!resp.ok) throw new Error(`poll failed: ${resp.status}`);
    const body = (await resp.json()) as { events: RealtimeEvent[] };
    for (const event of body.events ?? []) deliver(event);
  };

  const pollLoop = async () => {
    if (closed) return;
    try {
      await pollOnce();
    } catch {
      // Polling errors are transient by design: keep the cadence.
    }
    if (!closed) pollTimer = setTimeout(pollLoop, pollIntervalMs);
  };

  const startPolling = () => {
    setMode("polling");
    void pollLoop();
  };

  const startStream = async () => {
    try {
      const resp = await fetchFn(
        `${base}/realtime/stream?channel=${encodeURIComponent(channel)}&since=${lastId}`,
        { headers: headers(), signal: controller.signal },
      );
      if (!resp.ok || !resp.body) throw new Error(`stream failed: ${resp.status}`);
      setMode("stream");
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parsed = parseSseBuffer(buffer);
        buffer = parsed.rest;
        for (const event of parsed.events) deliver(event);
      }
      if (!closed) startPolling(); // stream ended: fall back
    } catch {
      if (!closed) startPolling(); // stream refused: fall back
    }
  };

  void startStream();

  return {
    close: () => {
      closed = true;
      controller.abort();
      if (pollTimer) clearTimeout(pollTimer);
    },
    mode: () => mode,
  };
}
