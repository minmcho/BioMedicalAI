# Phase 14 SQA Report — Devices, IoT, Data Lake & Pipelines

**Date:** 2026-10-18
**Scope:** PHASE10_ROADMAP Track H (device registry, ingestion, raw-zone
lake, offline-first deployment mode) and Track I (pipelines as Studio
config: idempotency, retry/dead-letter, backfill/replay, DQ quarantine,
terminology normalisation, MPI dedup candidates), with the Track J
register additions for equipment maintenance and analyser calibration.
**Staging only** — not published to GitHub, not synced to any Mac.
**Synthetic data only.** Every device feed in this deployment is
simulated or recorded, and is labelled `source_label: "simulated"` in
the data and in the UI — no live device link is claimed anywhere.

## Gate results (all re-run on the final tree, sequentially)

| Suite | Result |
|---|---|
| Backend `pytest tests/` | **492 passed** (455 baseline + 37 new Phase 14 tests) — deciding gate: two sequential half-suite runs on pruned /tmp, junitxml artefacts `~/workspace/gateA.xml` (249 tests, 0 failures/errors) + `~/workspace/gateB.xml` (243 tests, 0/0); see the environment note in Incidents for why a single plain run no longer completes on this VM |
| ADK service `pytest tests/` | **8 passed** (unchanged) |
| Web `npm test` | **274 passed, 42 files** (266 baseline + 8 new) |
| Web `npm run build` | **clean** (tsc + vite) |
| `/tmp` pruned before every full-suite run (standing environment rule) | done |
| Suites run sequentially, never overlapped (Phase 13 lesson) | done |

New backend tests: `tests/test_phase14_devices.py` (20),
`tests/test_phase14_pipelines.py` (11), `tests/test_phase14_mqtt.py` (6).
New web tests: `src/pages/Phase14Pages.test.tsx` (8), plus extensions to
`session.test.ts` / `runtimeConfig.test.tsx` nav expectations and the
`models.test.ts` capability count.

Existing tests adjusted, each for a Phase 14 mechanism change, in the
established Phase 6/11/12/13 precedent (mechanism snapshots extended,
assertions kept): `test_realignment.py` seeded-nav snapshot 22 → 24
modules (+devices, +data-platform) and the nav-derived count 21 → 23;
`test_phase10_rules.py` rules-registry event set +2
(`device.temperature.excursion`, `device.vitals.breach`);
`test_phase13_reports.py` compliance register total 24 → 26.

## Delivered scope

### 1. Device registry + calibration gating (`backend/app/devices.py`, router `/devices`)

- **Devices are real data** (`Device`): key, type, location, serial,
  status, HL7 sender binding, MQTT topic key, calibration/maintenance
  due dates. Seeded: 6 synthetic devices (chemistry + haematology
  analysers, ICU vitals monitor, pharmacy + lab fridge sensors, ICU
  pump).
- **Everything variable is published Studio config**
  (`terminology/devices`): device types with calibration intervals
  (analyser 180d, vitals monitor / fridge sensor / pump 365d), fridge
  temperature ranges, vitals thresholds per LOINC code, the
  calibration worklist window (30d), the blocking statuses, and the
  MLLP gate policy. Registry writes validate against it (unknown type
  → 422; duplicate key or sender → 409).
- **Calibration due dates are computed, never hand-typed:** recording
  a calibration sets due = performed_at + the published interval for
  the device type. Posture is computed at read (ok / due-soon /
  overdue / unknown / not-required); the **calibration worklist** is
  the HTM queue (overdue + inside-window, worst first).
- **The gate:** a device blocks ingestion when its status is a
  published blocking status (out-of-service, retired) or its
  calibration is overdue. What "blocked" means is the published
  policy's choice — see §2.

### 2. Ingestion paths (all audited, all raw-payload-first)

Every inbound payload is written to the raw-zone lake BEFORE curation,
so nothing is ever silently dropped — including refusals.

**(a) HL7 ORU over the Phase 8 MLLP machinery.** `interop.process_inbound`'s
ORU branch now passes a device gate (`devices.evaluate_oru_gate`):
the MSH-3 sender resolves through the config `senderMap`, then
`Device.hl7_sender`. Outcomes, all test-proven end-to-end through
`/devices/simulate/oru` (which runs the real `process_inbound`):
- known sender, device clear → AA; results land as DRAFT lab reports
  through the existing Phase 2/6B lab machinery, stamped with the raw
  entry and lineage-linked to it; the draft carries the instrument id.
- unknown sender (gate policy `onUnknownDevice: dead-letter`) → AR,
  the raw entry is dispositioned `dead-letter`, the inbound message is
  recorded with status `dead-letter` — visible, never dropped.
- calibration-overdue device, policy `block` → AR naming the device
  and its due date; nothing is ingested (verification queue stays
  empty — asserted).
- policy `quarantine` → AA with the message HELD (`DataQuarantine`,
  source `device-ingest`); releasing the hold after the calibration is
  repaired re-ingests the held message into a real draft.
- gate disabled by republishing the config → Phase 8 behaviour is
  restored exactly (flip-proven).

**(b) FHIR Observation ingestion** (`POST /devices/ingest/observation`):
device reference required (422 without), the device must be
registered, the patient must exist. Blocked device → 409 in block
mode, **202 + quarantine hold** in quarantine mode (the observation is
NOT created while held — asserted via a 404 read). Values are checked
against the published vitals thresholds; a breach fires the rules
event `device.vitals.breach`, and the seeded published rule
`vitals-threshold-breach` creates a review Task (proven).

**(c) MQTT adapter** (`MqttAdapter`, paho-mqtt 2.1.0): broker URL,
credentials and topic prefix come from `integration/mqtt` config,
which is NOT seeded — with no broker configured the adapter is
honestly off and `GET /devices/mqtt/status` says `state: "off"`.
Topic convention `{prefix}{device_key}/readings`; the message handler
is transport-free and is proven with recorded payloads
(`/devices/mqtt/test-message`): recorded / unknown-device dead-letter /
malformed dead-letter / foreign-topic ignored. **A live localhost
broker round-trip is also proven in the test suite**: amqtt 0.12.1
(open source, installed in the backend venv for the suite) serves an
ephemeral-port broker; the adapter connects, subscribes, and two
published fridge readings land as curated readings, the excursion
crossing the rules engine into a Task. That live test caught a real
bug — paho v2 `on_connect` delivers a `ReasonCode` object, not an int;
the adapter now normalises it (fixed before this gate).

**(d) Fridge / temperature readings** (`POST /devices/ingest/reading`,
also the MQTT payload shape): each reading is checked against its
device type's published range. An excursion fires
`device.temperature.excursion` **only on the transition into
excursion** (sustained out-of-range readings do not re-fire; returning
in-range re-arms) — the seeded published rule
`fridge-temperature-excursion` raises the cold-chain Task to the
config-named owner role (pharmacy). Transition semantics are
test-proven.

### 3. Data lake — raw zone (`backend/app/lake.py`)

- **Zone model (documented in the module):** raw → curated. The raw
  zone is the append-only `LakeRawEntry` table set in the existing
  database (no object-storage infrastructure exists in this
  deployment, and none was invented): payload type, full payload text,
  SHA-256 content hash, received timestamp, source + `simulated`
  label, source device/interface, patient reference, and a
  disposition (received / dead-letter / blocked / quarantined /
  held-offline). Corrections are new entries via `supersedes_id` —
  **there are no update or delete endpoints** (PUT/PATCH/DELETE return
  405; test-proven).
- **Lineage:** curated records link to their raw entry
  (`LakeLineageLink`, idempotent per pair). Lab drafts carry the
  raw-entry id as a FHIR extension; observations carry it as an
  extension too. `GET /devices/lineage/{resourceType}/{id}` answers
  "where did this value come from" with the raw payload, the source
  device, and the interface.
- Storage note: payloads live in the operational database by design
  for this phase; the zone model (immutable raw + links) is the
  contract a future object-store backing would keep.

### 4. Pipelines as Studio config (`backend/app/pipelines.py`, router `/pipelines`)

- **New governed config kind `pipeline`** with a real validator:
  ordered steps from a governed vocabulary (`extract-table`,
  `terminology-normalise`, `dedup-scan`, `load-warehouse`,
  `replay-lake`), per-step DQ checks from a governed set
  (`row-count-floor`, `null-rate-ceiling`, `value-set-membership`),
  extract targets restricted to a named server-side allow-list,
  optional cron schedule string, `enabled` flag. Anything outside the
  vocabulary → 422 at draft creation (test-proven).
- **Runner semantics, all test-proven:** steps execute in order and
  commit progressively; each step carries an **idempotency key**
  (pipeline@version : window : index : type) — re-running the same
  scope REUSES the succeeded step row and reports it in
  `detail.reused_step_ids` instead of re-executing; failure retries
  per the definition (`retry.maxAttempts`, capped backoff), and on
  exhaustion the run is `failed`, the step lands in the **dead-letter**
  table with its error, and `POST /pipelines/dead-letters/{id}/replay`
  starts a fresh run over the same scope (the full loop — poisoned
  offline reading → fail → dead-letter → repair → replay → curated —
  is one test).
- **Data-quality gates quarantine, they do not pass bad data:** a
  failed check marks the step + run `quarantined`, snapshots the
  batch into a `DataQuarantine` hold, and the data does NOT proceed.
  Release (audited, from the shared quarantine surface) resumes the
  run from the held step with the held dataset and executes the
  remaining steps; reject requires a note (422 without) and stops the
  run permanently.
- **Terminology normalisation** maps local codes through the
  published `terminology/code-map`; unmapped codes are REPORTED in the
  step detail (`unmapped_codes: ["XYZ"]` in tests), never silently
  passed.
- **MPI dedup scan:** deterministic weighted matching from the
  published `terminology/mpi-matching` (DOB 40, exact name 40,
  family-only 20, phone 20; threshold 70). Pairs at/above threshold
  become **candidates for human review**; existing pairs are never
  duplicated or re-opened. Review records the human decision; the
  response states `merge_performed: false` — **merging is explicitly
  out of scope here** and remains a human act through the patient
  machinery. Patients are byte-untouched by a scan (asserted).
- **Seeded definitions:** `nightly-warehouse-load` (load-warehouse
  step runs the real Phase 3 ETL into the warehouse DB — proven
  against a temp warehouse), `terminology-normalisation`,
  `mpi-dedup-scan`, and `lake-replay` (**enabled: false** — backfill
  ships disabled, and running a disabled pipeline is a 409).
- **Backfill/replay:** the `replay-lake` step reprocesses raw-zone
  entries by payload type; backfill mode skips entries already linked
  to a curated record (no double-curation — proven), offline mode
  processes only `held-offline` entries; HL7 entries are re-checked
  against the device gate at replay time.

### 5. Offline-first — designed deployment mode (per task §5)

Offline-first store-and-forward is a **designed deployment mode of
the lake/pipeline edge**, not a separate sync engine: the mechanism
is exactly the raw zone + replay built above. A site (or a ward
collector) running with the published `integration/offline` config
enabled accepts ingestion batches at `POST /devices/offline/batches`
(readings, observations, HL7 text); each item is stored as a raw
entry dispositioned `held-offline` and NOTHING is curated yet. When
connectivity returns, a `replay-lake` pipeline run in `offline` mode
curates the held entries through the same gates as live ingestion
(device gate included — an entry from a since-blocked device fails
loudly into retry/dead-letter rather than slipping through). Turning
the config off makes the batch endpoint refuse with 409 (proven).
No second store, no merge protocol, no conflict-resolution fiction:
the raw zone is the queue.

### 6. Web + plumbing

- **Devices page:** registry with computed calibration posture and
  gate block reasons, calibration worklist, honest feed status
  (gate policy as published; MQTT state verbatim — "off" with the
  no-broker explanation), recent readings with excursion flags and
  source labels; "Record calibration" for `devices.manage` roles.
- **Data platform page:** lake browser (disposition summary, payload
  viewer with curated links), lineage lookup, pipeline definitions
  with run history (per-step status + row counts), quarantine with
  release/reject, dead letters with replay, dedup candidates with
  review — run/review actions hidden from roles the server would 403.
- `/devices` + `/pipelines` added to BOTH proxy lists
  (`web/vite.config.ts`, `web/nginx.conf`) — the Phase 9/10 lesson.
- Capabilities registered in lockstep (backend `CAPABILITY_DEFAULTS`
  + web mirror, 53 → 56): `devices.read` (staff incl. viewer),
  `devices.manage` (admin/configurator/clinician), `pipelines.run`
  (admin/configurator). Nav modules `devices` + `data-platform`
  seeded through published navigation config (22 → 24 modules).

## Configuration-coverage list (standing rule: configure, don't fork)

Everything a site would vary is published Studio configuration;
changing any of it needs no code:

- Device types, calibration + maintenance intervals, per-type result
  ranges and their owner roles, blocking statuses, worklist window —
  `terminology/devices`.
- The MLLP device gate itself: enabled flag, sender → device map,
  unknown-device behaviour, overdue behaviour (`block` vs
  `quarantine`) — same document, republish flips behaviour (proven).
- Vitals breach thresholds per LOINC code — same document.
- MQTT broker URL / credentials / topic prefix — `integration/mqtt`
  (absent = adapter off).
- Offline store-and-forward on/off + mode — `integration/offline`.
- Pipeline definitions incl. steps, checks, retry, schedule, enabled
  — kind `pipeline`; terminology mappings — `terminology/code-map`;
  MPI matching weights + threshold — `terminology/mpi-matching`.
- Alert behaviour for excursions and breaches — the Phase 10 rules
  engine (kind `rule`), seeded published, editable like any rule.
- Who can see/run/manage any of it — the permissions matrix
  (`devices.read` / `devices.manage` / `pipelines.run` defaults).

## Compliance-coverage list (Principle 6: regulation is configuration)

- **Register +2 (24 → 26), seeded with evidence links:**
  The Joint Commission **EC.02.04.03** (medical equipment inspection,
  testing and maintenance — the device registry, computed calibration
  posture and the worklist are the operating mechanism) and CLIA
  **42 CFR 493.1253** (analyser calibration / calibration verification
  — the gate that refuses or quarantines results from an overdue
  analyser is the enforcement point).
- Device results are never accepted silently from an unregistered or
  out-of-calibration source: unknown → dead-letter with reason,
  overdue → block or quarantine per published policy, every outcome
  written to the hash-chained audit (`devices.*`, `pipelines.*`
  actions) and the raw payload retained immutably for reconstruction.
- Quarantine decisions (release/reject) require an identified actor,
  are timestamped, and reject requires a recorded reason — the
  data-quality hold is a governed disposition, not a log line.
- MPI dedup is advisory-only by design: no automated merge of patient
  records exists in this phase, so no record-integrity regulation is
  put at risk by the matching heuristic.

## NOT RUN / gaps (honest list)

- **Real physical devices** — none exist in this environment; all
  feeds are simulated/recorded and labelled as such.
- **External / production MQTT broker** — NOT RUN. The adapter is
  proven against a real open-source broker on localhost (in-suite)
  and with recorded payloads; no external broker was contacted.
- **ASTM E1381/E1394 analyser links** — NOT implemented. The analyser
  path in this system is HL7 ORU over the Phase 8 MLLP listener
  (stated plainly, as with the Phase 8 Go note); ASTM is future work,
  not a hidden stub.
- **Live MLLP socket test for the gate** — the gate is proven through
  `process_inbound` (the exact function the MLLP server calls) plus
  the Phase 8 socket tests; no new socket-level run was added.
- **Pipeline scheduling** — definitions carry a cron string, but no
  scheduler daemon runs them in this phase; runs are triggered via
  the API/UI. The string is governed data awaiting the Phase 10-style
  scheduler, not a claim that jobs fire.
- **Rust de-identification step** (roadmap Track I mentions it) — not
  built; no de-id step type exists in the vocabulary, and none is
  faked.
- **Browser click-through E2E** — NOT RUN (VM limitation, standing;
  not retried per instructions). The Air remains the browser gate.
- Standing NOT RUN list unchanged: live LLM run, Postgres/Docker
  runtime, live Keycloak, live TLS serving, live Epic SMART launch,
  pack-enablement route gating.

## Incidents / notes from this build

- The live-broker test caught a real adapter bug (paho v2
  `ReasonCode` vs int) that recorded-payload tests could not have;
  fixed and re-proven before the gate.
- The seeded terminology payloads (`devices`, `code-map`,
  `mpi-matching`) initially failed re-publish through the governed
  API because `validate_terminology` requires a `concepts` list; all
  three now carry their value set as `concepts`, following the
  Phase 12/13 pattern, so a site can republish them (the gate-policy
  flip test depends on exactly this).
- **Environment (important for re-verification):** the backend suite
  has outgrown this VM's 512 MB /tmp tmpfs. Each test's retained
  basetemp is ≈1.2 MB, so 492 tests ≈ 590 MB: a single plain
  `pytest tests/` run fills /tmp near the end and the
  alphabetically-late files (first seen in `test_studio_config.py`)
  fail at FIXTURE SETUP with disk errors — observed counts varied
  (246, then 46 errors) with resident /tmp content, while the same
  files pass standalone (test_studio_config.py: 22/22). At 455 tests
  (Phase 13) the single run still fit; at 492 it does not. Two
  aggravators were found and dealt with during this gate: backgrounded
  suite processes outlived their completion notifications and
  OVERLAPPED (the Phase 13 corruption mechanism — all instances were
  killed and `pgrep pytest` verified at 0 before the deciding runs),
  and a disk-backed basetemp (`TMPDIR` on workspace disk) proved too
  I/O-slow on this VM to finish in reasonable time. **Deciding gate:**
  two sequential half-suite runs (20 + 21 test files) on pruned /tmp,
  each ≈285 MB of basetemp: 249 passed + 243 passed, both exit 0,
  junitxml artefacts `~/workspace/gateA.xml` / `gateB.xml`, logs
  `~/workspace/gateA.log` / `gateB.log`. Re-verification on this VM
  should use the same halves approach (or a fast disk basetemp) and
  confirm no other pytest instance is alive first.

---

## Parent verification addendum (2026-10-18, independently re-run)

- Backend suite re-run by the parent agent in two sequential halves on
  pruned /tmp (parent split: 21 + 20 files, different from the build's
  20 + 21): **261 passed** (exit 0, log `~/workspace/p14_parent_A.log`)
  + **231 passed** (exit 0, log `~/workspace/p14_parent_B.log`) =
  **492 passed, 0 failures** — independently confirms the build's
  deciding gate (249 + 243). The build's first "492" notification-based
  claim and this confirmation agree on the total; the halves method is
  now the standing gate on this VM (single run no longer fits the
  512 MB /tmp at ~492 tests).
- Web suite parent re-run: **274 passed** (42 files); `npm run build`
  clean. ADK suite parent re-run: **8 passed**. Spot checks:
  `backend/app/devices.py`, `backend/app/lake.py`,
  `backend/app/pipelines.py` present; `/devices` + `/pipelines` in
  both proxy configs; PHASES.md Phase 14 row present; builder gate
  artefacts `~/workspace/gateA.xml` / `gateB.xml` on disk (249 / 243).
- Final verified counts for Phase 14: **backend 492, web 274, ADK 8,
  build clean.**
