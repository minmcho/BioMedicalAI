# Phase 8 SQA Report — Live Interoperability

**Date:** 2026-10-17 · **Scope:** roadmap §3 Phase 8 + §2 interoperability row
(HL7 v2 live feeds over MLLP, FHIR bulk export, public-health reporting) under
the §6 rule (configure, don't fork). **Staging only** — not published to
GitHub, not synced to any Mac. Synthetic data only.

## Gate results (all re-run at delivery)

- **Backend: 297 passed** (273 baseline + 24 new: `tests/test_phase8_interop.py`
  15, `tests/test_phase8_mllp.py` 9) — zero regressions.
- **Web: 187 passed** (184 baseline + 3 new in
  `src/pages/studio/InterfacesPanel.test.tsx`) — zero regressions.
- **`npm run build`: clean.**
- **Live smoke (real processes, not TestClient):** API + the standalone
  `python -m app.mllp_server` process on one SQLite database — ORU^R01 over a
  real socket → `MSA|AA`, results landed as drafts (order `resulted`, report
  `preliminary`); then a Studio-published endpoint + admission + dispatcher
  pass delivered a well-formed ADT^A01 to a separate downstream socket
  listener (outbound row `sent`, ack `AA`, 1 attempt). The smoke also caught
  and fixed a real defect (below).

## What landed, in task order

### 1. One shared HL7 module (`backend/app/hl7.py`, new)

The Phase 2 ORU reader (`parse_hl7_oru` + field/component helpers) moved
**verbatim** from `app.lab` into `app.hl7`; `app.lab` re-exports it, so every
Phase 2 import and test is untouched. The module grew: a generic segment
parser, HL7 escaping, DTM/date formatting, builders, ACK build/parse, and
MLLP framing (VT … FS CR, `extract_frame` for stream reassembly). There is
exactly one HL7 implementation in the codebase.

Builders produce HL7 **v2.5.1**: MSH/PID/PV1 (+EVN) for ADT, PID from the
FHIR Patient (PID-3 first repetition = the OpenCare patient resource id —
the identifier this system's own ingest matches on; MRN as second
repetition), PV1-3/PV1-6 composed as unit^room^bed from the published
locations terminology, PV1-2 from the Encounter class (both stored shapes —
bare Coding and CodeableConcept — tolerated, matching
`app.inpatient._class_code`).

### 2. Outbound generation — interface outbox, in-transaction

Hooks at the three event sites queue `Hl7OutboundMessage` rows **in the
same transaction as the event** (one row per enabled, subscribed endpoint;
the raw text is built per endpoint because MSH-5/6 differ):

- `app.inpatient._record_event` → ADT^A01/A02/A03/A08 (`enqueue_adt_event`);
- `app.orders` sign transition → ORM^O01 (`enqueue_signed_order`) — drafts
  queue nothing, signing queues exactly one per endpoint;
- `app.lab._release_now` **and** the Phase 6B bench release points
  (`micro_finalize`, `pathology_sign`) → ORU^R01 (`enqueue_released_report`) —
  resulted/verified queue nothing; release queues.

Atomicity is test-proven, not asserted: a test writes an ADT event through
`_record_event` in a bare session, observes the queue row, rolls back, and
finds **both** gone. With no enabled endpoint configured (the seeded
default), events queue nothing and all pre-Phase-8 behaviour is unchanged —
the full 273-test baseline passes unmodified.

### 3. MLLP transport (`backend/app/mllp_server.py`, new)

A real asyncio TCP listener speaking MLLP framing, runnable standalone
(`python -m app.mllp_server --port 2575`) and registered as the `mllp`
service in `docker-compose.yml`. The outbound client (`send_mllp`) is a
plain socket client with timeout; both directions are exercised over **real
localhost sockets** in the test suite (sockets verified available in this
environment before the suite was written — nothing is mocked at the socket
layer).

**Language choice, stated plainly:** the roadmap names the Go tier for
this listener. There is **no Go toolchain on this machine and no Go
skeleton anywhere in the repo**, so the listener is Python/asyncio in the
existing backend codebase. The wire behaviour (framing, ACK semantics,
ingest routing) is the deliverable; `process_inbound` is
transport-agnostic if the listener is ever re-homed to Go.

Inbound routing (`app.interop.process_inbound`), every message recorded in
`hl7_inbound_messages` with its disposition + audited:

- **ORU^R01** → the EXISTING lab ingest core (`app.lab.ingest_hl7_groups`,
  extracted from the `/lab/ingest` handler — one code path for HTTP and
  wire): drafts only, preliminary + unreleased, verification/release remain
  human steps. Parse/validation failures → **AE + ERR**.
- **ADT^A01/A02/A03/A08** → recorded as an **external event only**; the
  census is never mutated by inbound ADT. Conflicts are refused with
  **AR + ERR**: A01 for a patient with an active inpatient (IMP,
  in-progress) admission; A03 with no active admission; unknown patient
  (PID-3 resolved by resource id or any identifier value).
- Anything else → AE (unsupported type); non-HL7 → AR + ERR. A poison
  message still gets an ACK (defensive catch + rollback in the processor).

**Defect found by the live smoke and fixed:** the listener originally
imported `app.main` (whose module level builds the HTTP app and races
`create_all` against a concurrently booting API on a fresh database). The
listener now builds its own engine/session factory via `app.db`, runs the
same idempotent seed chain as `create_app`, and tolerates losing the
create race to the API process (schema ownership stays with the API).

### 4. Dispatcher, dead letters, replay

`dispatch_pending` drains due `pending` rows: AA → `sent`; AE/AR or any
transport error (refused/timeout/closed-without-ACK) → failed attempt with
the reason kept; attempts accrue in append-only `Hl7SendAttempt` rows;
exhausting the configured max attempts → `dead_letter`. Backoff is
exponential from the published `retry.backoffSeconds` (`next_attempt_at`).
An endpoint disabled after queueing is skipped (a re-enable revives the
row); an endpoint removed/unsubscribed dead-letters loudly rather than
vanishing. `POST /interop/dispatch` runs one pass (the production shape is
a scheduler invoking the same function).

Replay (`POST /interop/dead-letters/{id}/replay`, `interop.manage`):
re-queues with `attempt_base` moved to the preserved attempt count — the
message gets a fresh budget and its **history is never rewritten**
(test-proven: 2 recorded NAK attempts survive replay; delivery then
succeeds as attempt 3).

### 5. Interface admin API + web

`GET /interop/endpoints` (effective published config + per-endpoint queue
tallies), `GET /interop/messages` (unified log; filters: direction, type,
status, patient), per-message detail (raw text + attempt history),
`GET /interop/dead-letters`, replay, dispatch. **No write path for
endpoint configuration exists here** — endpoints are edited through the
Studio draft → publish flow only (test asserts the 404/405 and that the
Studio validator refuses a malformed endpoint, e.g. port 99999 → 422).

Web: the Studio **Integration tab** gains an **Interfaces panel**
(`src/pages/studio/InterfacesPanel.tsx`): endpoint status + queue tallies,
dead letters with Replay buttons, dispatcher-pass button with stats, and
the recent message log. `/interop` added to the Vite dev proxy and the
nginx proxy. New pack manifest `backend/packs/interop.pack.json` v1.0.0
(all endpoints/modules `enabled: false`, per governance).

### 6. FHIR bulk export (`$export`-style)

`POST /interop/$export` → **202** + job id + status URL; generation runs
on a background thread (or inline with `?wait=true` — the documented
small-mode; the job shape is identical). NDJSON per resource type
(Patient, Encounter, Observation, Condition, MedicationRequest — other
types 422) is stored in `bulk_export_files` and served only from
`GET /interop/export-jobs/{id}/files/{type}`. Posture:

- gated by the new **`interop.export`** capability (default
  admin/configurator; clinician/viewer/patient get 403 — test-proven);
- the job/status surface carries **counts and file links only, never
  patient data** (test asserts no names/MRNs appear in the status body);
- job creation, completion and **every download** are audit-logged;
- lines are the resources exactly as the FHIR API renders them
  (test parses every NDJSON line back as FHIR and matches counts/ids).

### 7. Public-health reporting (ELR)

A released report whose resulted analyte matches the published
`terminology/reportable-conditions` list — LOINC code + a `valueCondition`
(`any` / `above` threshold / `positive` qualitative) — additionally queues
an ELR-variant ORU^R01 (NTE naming the condition) addressed **only** to the
endpoint named `public-health`. The general results feed explicitly
excludes that endpoint, so a reportable result is never double-sent in two
shapes. Test-proven: potassium 6.4 against a published `above 6.0` trigger
queues exactly one ELR; 4.4 queues none; glucose never queues one.

**Immunisation (VXU) reporting is NOT built, deliberately:** this system
has no Immunization resource and no vaccine-administration model anywhere
in its data (the BCMA `MedicationAdministration` records are medication,
not vaccination). Fabricating a vaccine model to emit VXUs would invent
clinical data structures the rest of the system does not have. ELR is the
public-health scope of this phase; VXU waits for a real immunisation
model (a Phase 9 candidate alongside the specialty packs that need one).

## §6 configuration-coverage list

Everything site-specific in this phase is published Studio configuration,
seeded as governed v1 and changeable by draft → publish with no code change:

| Behaviour | Configuration |
|---|---|
| Which endpoints exist, host/port, subscribed message types, enabled flag, receiving application/facility | `integration/hl7-endpoints` → `endpoints[]` |
| Facility identity on every outbound MSH (sending application/facility) | `integration/hl7-endpoints` → `facility` |
| Retry budget + backoff base for the dispatcher | `integration/hl7-endpoints` → `retry` |
| ELR trigger list: LOINC codes, conditions, value conditions | `terminology/reportable-conditions` |
| PV1 location composition (units/rooms/beds) | `terminology/locations` (Phase 6, reused) |
| Who may view the interface log / dispatch / replay / export | capabilities `interop.read` / `interop.manage` / `interop.export` in `permissions/role-matrix` (new defaults in `app.configstore.CAPABILITY_DEFAULTS`, overridable by publish) |

Left coded, with justification: the HL7 segment layouts and MLLP framing
(the wire standard itself — no site legitimately reconfigures segment
order); the inbound ADT conflict RULES (patient-safety invariants — the
census is never written by an external feed); the set of exportable
resource types (a code-level allowlist is the safe default for a
record-level extract); the `public-health` endpoint NAME as the ELR
address (the routing convention — its address/enabled state is config).

## Mechanism changes to existing behaviour (with justification)

1. **`app.lab`**: the ORU parser moved verbatim to `app.hl7` (re-exported
   from `app.lab`); the `/lab/ingest` handler's per-group draft creation
   was extracted to `lab.ingest_hl7_groups` so the MLLP listener routes
   through the identical path. No behaviour change — the full Phase 2/6B
   lab suites pass unmodified.
2. **Four one-line lazy-import hooks** (`inpatient._record_event`,
   `orders` sign transition, `lab._release_now`, `lab_benches`
   micro-finalize + pathology-sign): each queues outbound messages in the
   event's transaction. With the seeded (disabled) endpoints they queue
   nothing; all pre-existing suites pass unweakened.
3. **`app.configstore.validate_integration`** extended: `facility`,
   `retry` and `endpoints` sections are validated when present (unknown
   sections still pass through, per the kind's documented contract).
4. **`app.main`**: `ensure_interop_defaults` added to the seed chain,
   `interop.router` included, `/health` phase → "8". Seeded configs are
   published v1 like every other module's.
5. Encounter `class` is read tolerantly (bare Coding **or**
   CodeableConcept) in `app.hl7.pv1_segment` and
   `app.interop._encounter_class_code` — found by test: stored inpatient
   encounters carry the FHIR R4 bare-Coding shape.

## Design notes (recorded, not hidden)

- **Queue rows are per (message × endpoint)**, created at event time from
  the then-published config. Enabling an endpoint does NOT retro-queue
  past events (test-proven) — queueing is part of the event's
  transaction, by design; a site that needs history replay uses bulk
  export, not the live feed.
- **The interface user**: inbound ingest acts as a dedicated
  `interface-engine` user row (clinician role) so wire-originated drafts
  carry an honest actor in the audit chain, distinct from any human.
- **Bulk export is record-level by design** (that is what FHIR Bulk Data
  is) — unlike the aggregate-only population-health surfaces. The
  compensating controls are the capability gate, the counts-only status
  surface, and full audit. Unreleased lab observations export in their
  stored state (their `status` says preliminary); the export is a
  system-to-system extract of the record as held, not a patient-facing
  view, so the portal visibility tag is not re-applied.
- SQLite returns timezone-naive datetimes; `dispatch_pending` normalises
  before comparing backoff times (the `app.inpatient._aware` convention).

## NOT RUN / not claimed

- **Live external endpoints** — no real downstream EHR or public-health
  gateway exists in this environment; all delivery was proven against
  localhost socket peers (test doubles + a live smoke listener), never
  against an external system.
- **Browser click-through E2E** — NOT RUN (standing gap; the web panel is
  covered by component tests with a stub client at the network boundary).
- **Postgres runtime** — NOT RUN (no Docker/Postgres in this environment;
  all suites run on SQLite, the codebase's portable layer; the
  `docker-compose.yml` `mllp` service is defined but not booted here).
- **Carequality-pattern document retrieval** and **device-data ingest**
  (roadmap §3 Phase 8 mentions both) — not built in this phase; the §2
  inventory row's live-feed/export/reporting core is what landed.
- **VXU immunisation reporting** — not built (no immunisation data model
  exists; see §7 above).
