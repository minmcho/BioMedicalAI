# OpenCare Go services — MLLP gateway & edge node

Go tier for OpenCare EHR, built with **Go 1.27.2**
(`go version go1.27.2 linux/amd64`, toolchain at
`~/workspace/toolchains/go` on the build VM; module
`opencare/services`, `go 1.27`, no third-party dependencies — standard
library only).

## Where this tier sits (read this first)

- The **Python MLLP listener remains the in-core listener**:
  `backend/app/mllp_server.py` runs in/next to the FastAPI process and
  calls `app.interop.process_inbound` directly against the core
  database.
- The **Go gateway is the edge/gateway tier in front of the same
  raw-zone contract**. It does not replace the Python listener, keeps
  no curated store of its own, and invents no endpoints.
- Both Go binaries deliver into the core through the **existing**
  offline store-and-forward contract implemented by
  `queue_offline_batch` in `backend/app/devices.py`:

  ```
  POST {CORE_URL}/devices/offline/batches
  Authorization: Bearer <OPENCARE_GATEWAY_TOKEN>    (sent only when set)
  Content-Type: application/json

  {"items":[{"kind":"hl7","payload":"<raw HL7 text>"},
            {"kind":"observation","payload":{ ... }},
            {"kind":"reading","payload":{ ... }}]}
  ```

  Success is **exactly HTTP 201**. The core lands each item in its raw
  zone with disposition `held-offline`; the core's pipeline replay step
  curates held items later. `kind` values are the core's own set:
  `hl7` (payload = raw HL7 text, a JSON string), `observation` and
  `reading` (payload = a JSON object). Note the core only accepts
  batches while its published `integration/offline` Studio config is
  enabled; otherwise it answers 409, which these binaries treat like
  any other failure: items stay queued/spooled and are retried.

## Binaries

### `cmd/mllp-gateway` — MLLP listener + device gateway

Terminates MLLP from analysers/devices at a site and forwards every
accepted message to the core contract above.

- MLLP framing: `VT (0x0B) … FS (0x1C) CR (0x0D)`, multiple/pipelined
  frames per connection supported.
- Parses MSH far enough to build a proper HL7 ACK: `MSA-1` is
  `AA`/`AE`/`AR`, `MSA-2` echoes the original `MSH-10` control id, and
  an `ERR` segment rides along for `AE`/`AR` (mirrors
  `backend/app/hl7.py build_ack`).
- Max frame size guard: **4 MB**, mirroring the Python listener's
  `MAX_FRAME_BYTES`; an oversize frame gets `AR` and the connection is
  closed.
- Health: separate HTTP listener, `GET /health` →
  `{"status": "ok", "spooled": <count>, "core_reachable": <bool>,
  "service": "mllp-gateway"}`. `core_reachable` is a transport-level
  probe for reporting only; delivery decisions are made solely by the
  batch POST's 201.

Environment configuration:

| Variable | Default | Meaning |
|---|---|---|
| `MLLP_ADDR` | `:2575` | MLLP TCP listen address |
| `GATEWAY_HEALTH_ADDR` | `:8081` | health HTTP listen address |
| `GATEWAY_SPOOL_DIR` | `./mllp-spool` | durable on-disk spool directory |
| `CORE_URL` | `http://127.0.0.1:8000` | FastAPI core base URL |
| `OPENCARE_GATEWAY_TOKEN` | _(unset)_ | Bearer token for the core contract; configuration only, never hardcoded |

Flags (`-mllp-addr`, `-health-addr`, `-spool-dir`, `-core-url`)
override the environment. The token is environment-only by design.

### `cmd/edge-node` — store-and-forward node for a clinic site

Captures items at a site with intermittent connectivity and syncs
them to the same core contract.

- Capture surface (local HTTP):
  - `POST /capture` with `{"kind": "hl7"|"observation"|"reading",
    "payload": <string for hl7, object for observation/reading>}` →
    `201 {"id": "<uuid>", "seq": <n>}`. Validation follows the core
    contract: an `hl7` payload must be a string; an
    `observation`/`reading` payload must be a JSON object (a string
    that itself parses as an object is accepted and unwrapped).
  - `GET /status` → `{"pending", "synced", "core_reachable",
    "last_sync", "queue_dir", "service"}`.
  - `GET /health` → `{"status": "ok", "service": "edge-node"}`.
- Queue: `EDGE_QUEUE_DIR` holds one JSON envelope file per item —
  `{id (uuid), seq, received_at, kind, payload}` — named by
  zero-padded sequence. Writes are atomic (tmp file + fsync + rename +
  directory fsync). The sequence high-water mark is persisted
  (`seq.state`, also atomic) and recovered by scanning envelopes, so
  **receive order survives restarts**.
- Sync loop: pending items are POSTed in receive order, batched at up
  to **50 per POST**. An item is marked synced **only after** its
  batch receives a 201; marking = moving its envelope into the
  queue dir's `synced/` subdir. On any failure the pass stops at that
  batch, remaining items stay pending in order, and the loop retries
  with backoff (1 s doubling to 30 s; idle interval 5 s).
- One-shot mode (cron-style):

  ```
  edge-node sync-once
  # exit 0: queue empty / everything synced
  # exit 1: sync failed, items remain pending, or a
  #         configuration / queue error
  ```

Environment configuration:

| Variable | Default | Meaning |
|---|---|---|
| `EDGE_ADDR` | `:8082` | capture HTTP listen address |
| `EDGE_QUEUE_DIR` | `./edge-queue` | queue directory |
| `CORE_URL` | `http://127.0.0.1:8000` | FastAPI core base URL |
| `OPENCARE_GATEWAY_TOKEN` | _(unset)_ | Bearer token for the core contract |

## ACK semantics (mllp-gateway)

- **AA only after delivery is guaranteed**: every well-formed message
  is *first* durably spooled (fsynced envelope on disk), then a drain
  in receive order is attempted. AA is returned only once the payload
  has been **forwarded** (core answered 201; spool entry removed) **or
  is durably spooled**. An AA'd message is never lost by the gateway.
  If the spool write itself fails, the ACK is **AE**, never AA.
- **AR for malformed frames**: no complete MLLP frame, a frame over
  the 4 MB guard, or an unparseable MSH (not starting with MSH, or no
  MSH-9/MSH-10). AR'd payloads are neither forwarded nor spooled as
  valid HL7.
- Spooled messages are retried by a background drain loop with
  backoff (200 ms doubling to 5 s), **in receive order**, until the
  core answers 201.

## Delivery semantics (both binaries) — at-least-once, honestly

- An item leaves the local queue/spool **only after the core's 201**
  for the batch containing it (gateway: envelope deleted; edge node:
  envelope moved to `synced/`).
- On failure (core down, non-201, timeout) the batch stops, order is
  kept, and everything not yet confirmed stays pending for retry.
  **Nothing is ever dropped silently.**
- A crash between the core's 201 commit and the local mark/remove can
  therefore **re-send an item**: delivery is at-least-once, not
  exactly-once. This is safe by design — the core's raw zone accepts
  these batches as held, immutable raw entries that are replayed and
  governed downstream, never auto-curated, so a duplicate is a
  duplicate raw record, not a duplicated clinical write.

## Layout

```
go.mod
cmd/mllp-gateway/main.go      MLLP gateway binary (flags/env, signals)
cmd/edge-node/main.go         edge node binary (serve | sync-once)
internal/hl7msg/              MLLP framing, MSH parsing, ACK build/parse
internal/coreclient/          the one client for the core contract
internal/queue/               shared durable envelope queue (atomic,
                              sequence-persisted, synced/ marking)
internal/gateway/             gateway: TCP/MLLP server, spool drain,
                              health endpoint
internal/edge/                edge node: capture HTTP, sync engine
```

## Build & test

Hermetic: tests use an `httptest` stub implementing the
offline-batches contract and real localhost TCP for MLLP — no real
core, no network beyond localhost.

```sh
export PATH=$HOME/workspace/toolchains/go/bin:$PATH   # or call go by full path
cd services/go
go build ./...
go vet ./...
go test ./...
```

On the build VM, `/tmp` is a small tmpfs: point `TMPDIR`, `GOPATH`
and `GOCACHE` at a workspace directory for downloads/builds.
