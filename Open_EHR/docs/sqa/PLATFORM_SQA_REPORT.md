# Platform SQA Report — Platform Pass (Go + Rust tiers, DB platform, tiering, protocols, report engine, indicators, design system, Studio full parity, RTM dispositions, UX audit fixes)

**Date:** 2026-10-18
**Scope:** The Platform pass, answering the user's 2026-10-18 directives
(data architecture completed: hot/cold tiering + edge store-and-forward;
"backend need not only FastAPI but Go and Rust stack" — the locked
multi-language stack made real; the Studio database platform — adapters,
schema designer, transformer workbench; SOP/protocol framework for all
modules; the generalised report engine; the indicator library; the
written design-system spec). Nine workstreams: (1) Go + Rust toolchains,
(2) Go MLLP gateway + edge node (`services/go/`), (3) Rust
de-identification pipeline step (`services/rust/`), (4) database
adapters + Studio DB platform with live Postgres/MariaDB verification,
(5) hot/cold tiering, (6) SOP/protocol framework with a laboratory
proof, (7) report engine, (8) indicator library, (9) design-system
specification (`docs/DESIGN_SYSTEM.md`).
**Staging only** — not published to GitHub, not synced to any Mac.
**Synthetic data only.** The parked `agents/` draft and
`docs/AGENTS.md` were excluded throughout, per standing rule.
**Coordination note:** this pass ran while an Air sync was transferring
the tree; builders created new files only, and every edit to an
existing file was deferred to a pending-integration list and applied
by the coordinator after the parent signalled "sync closed"
(2026-10-18). Section "Integration edits applied" lists them all.
**Scope additions after the original nine workstreams** (all
2026-10-18, all in this report): **Workstream 10 — Studio Full
Parity** (Min: "System Studio need fully implementation like
Agentic_App System Studio") with the parity gap map as its first
deliverable (`docs/STUDIO_PARITY.md`); the **RTM findings**
dispositions (STU-04 pack-enablement route gating built, STU-16
gap map, gate table finalised on the enlarged tree); and the
**Navigation & UX audit fixes** F-1/F-2/F-3 (check-in, Home lists,
Safety seed drift) with the design system's §2A aligned to the
landed UX spec. The gate table below is the FINAL combined gate
over everything in this report.

## Gate results (all re-run by the coordinator on the final tree, sequentially)

| Suite | Result |
|---|---|
| Backend `pytest tests/` | **695 passed, 4 skipped** (549 baseline + 146 new: 110 WS1–9 + 36 WS10/RTM/UX) — FINAL combined gate over the enlarged tree: two sequential half-suite runs on pruned /tmp, junit artefacts `~/workspace/gatePLAT2_A.xml` (366 tests, 0 failures/errors/skipped) + `~/workspace/gatePLAT2_B.xml` (333 tests = 329 passed + 4 skipped, 0 failures/errors); each artefact's counts verified against its console summary. The 4 skips are the env-gated live-engine tests (next row). (The interim WS1–9 gate — 659 passed + 4 skipped, artefacts `gatePLAT_A/B.xml` — was real on its tree and is superseded by this one.) |
| Live DB conformance `tests/test_platform_db_live.py` (env-set run) | **4 passed** — coordinator's run against real PostgreSQL 16.15 and MariaDB 10.11.14 (`OPENCARE_TEST_PG_URL` / `OPENCARE_TEST_MARIADB_URL`, throwaway local test roles; warehouse side-databases `<db>_wh` provisioned), with `tests/test_platform_db.py` at 51 passed in the same session. In the default gate above, the same 4 tests skip with the env-gate reason |
| Web `npm test` (vitest) | **322 passed, 47 files** (299 baseline + 23 new across the three new WS10/UX test files) |
| Web `npm run build` | **clean** (exit 0; only the pre-existing chunk-size advisory) |
| ADK service `pytest` | **8 passed** (unchanged) |
| Go `go test ./...` (services/go) | **11 passed** (edge 4, gateway 4, hl7msg 3; all packages `ok`) — go1.27.2 |
| Rust `cargo test` (services/rust) | **33 passed** (9 unit + 20 integration + 4 CLI; 5 suites, all `ok`) — rustc/cargo 1.99.1 |
| `/tmp` pruned before every full-suite run (standing environment rule) | done |
| Suites run sequentially, never overlapped | done |

New tests: backend `tests/test_platform_db.py` (51 collected),
`tests/test_platform_db_live.py` (4 collected, env-gated),
`tests/test_platform_reports.py` (12),
`tests/test_platform_indicators.py` (9),
`tests/test_platform_protocols.py` (16),
`tests/test_platform_tiering.py` (13),
`tests/test_platform_deid_pipeline.py` (9, coordinator-written for
the integration edits); WS10/RTM/UX: `tests/test_studio_ai_providers.py`
(11), `tests/test_studio_ops.py` (10), `tests/test_pack_gate.py`
(6), `tests/test_nav_seed_consistency.py` (4),
`tests/test_ux_checkin.py` (5); web
`web/src/pages/studio/StudioParityTabs.test.tsx`,
`web/src/pages/studio/DesignerCompletions.test.tsx`,
`web/src/pages/UxAuditFixes.test.tsx` (23 tests across the three);
Go tests in `services/go/internal/{edge, gateway,hl7msg}` (11);
Rust tests in `services/rust` (33).

Existing tests adjusted — one in WS1–9, a legitimate growth
change, commented in code, no assertion weakened:

- `tests/test_phase14_pipelines.py::test_seeded_pipeline_definitions`
  — the seeded pipeline key set grows by `deidentify-export`
  (platform pass), with its ships-disabled assertion added beside
  the existing `lake-replay` one.

In the WS10/RTM/UX round **no existing test file was modified**:
the gate arithmetic closes exactly (backend +36 = the five new
files' collected counts; web +23 tests in exactly +3 files).

Compliance register: **27 (unchanged)**. Capabilities: **58
(unchanged)** — every new route gates on an existing capability.

## Toolchains (Workstream 1)

| Toolchain | Version | Location |
|---|---|---|
| Go | **go1.27.2 linux/amd64** (tarball SHA-256 verified against go.dev) | `~/workspace/toolchains/go` |
| Rust | **rustc 1.99.1 / cargo 1.99.1** (rustup; `RUSTUP_HOME`/`CARGO_HOME` under `~/workspace/toolchains`) | `~/workspace/toolchains/rustup`, `~/workspace/toolchains/cargo` |

Both install outside the repo tree; all builds ran with `TMPDIR` on
workspace disk (the VM `/tmp` is a 512 MB tmpfs).

## Workstream 2 — Go services (`services/go/`)

Module `opencare/services` (go 1.27, zero third-party dependencies —
standard library only). Two binaries:

- **`cmd/mllp-gateway`** — a real MLLP listener (default `:2575`,
  health on `:8081` via `GATEWAY_HEALTH_ADDR`) that forwards HL7 v2
  payloads into the core's EXISTING raw-zone contract —
  `POST {CORE_URL}/devices/offline/batches` with a Bearer token from
  the `OPENCARE_GATEWAY_TOKEN` environment variable (never hardcoded),
  body `{"items":[{"kind":"hl7","payload":...}]}`, success = HTTP 201
  exactly. It invents no second store and no new core endpoint.
  Semantics: the payload is written to a durable on-disk spool
  (atomic tmp+rename+fsync envelopes) BEFORE any ACK decision when the
  core is unreachable; AA is returned only after a forward-201 or a
  durable spool; AE on spool failure; AR for malformed frames
  (>4 MB guard mirroring the Python `MAX_FRAME_BYTES`); ordered drain
  with 200 ms→5 s backoff when the core returns. If the core answers
  409 (published `integration/offline` config disabled), the payload
  stays queued and drains once an operator enables it.
- **`cmd/edge-node`** — a store-and-forward edge node (`serve` +
  `sync-once`, exit 0 = queue drained / 1 = failed-or-pending) with its
  own durable on-disk queue; captures payloads over HTTP while the
  server/network is down and syncs them **in receive order** to the
  same core offline-batch contract; an envelope is marked synced only
  after a core 201. Delivery is **at-least-once, stated honestly**: a
  crash between the core's commit and the local mark can re-send, and
  the core's raw zone tolerates duplicates by design.

Tests: **11/11 pass** (`go test ./...`: edge 4, gateway 4, hl7msg 3),
all hermetic — an httptest stub core plus a real localhost TCP MLLP
round-trip (an ORU^R01 arrives byte-identical at the stub core and the
sender receives AA); core-down → spool → ordered drain; restart
persistence; malformed frame → AR. `gofmt`/`go vet`/`go build` clean;
`-race` passed for the gateway and edge packages.

Relationship to the existing listener, stated plainly: the Python
asyncio MLLP listener (`app/mllp_server.py`) remains the in-core
listener; the Go services are the edge/gateway tier speaking the same
raw-zone contract. Packaging: `services/go/Dockerfile` (multi-stage,
new file) + `mllp-gateway` and `edge-node` services in
`docker-compose.yml` (coordinator integration edit; the edge node is
behind the `edge` compose profile because it belongs at a site edge,
not in the central stack). **Docker is not available on this VM, so
the compose definitions are YAML-validated only — NOT RUN.**

## Workstream 3 — Rust de-identification (`services/rust/`)

Crate `opencare-deid` (library + CLI `deid`/`check`; stdin→stdout or
`--in`/`--out`; `--record-key`; `--format auto|json|text`; exit codes
0/1/2/3/4; `check` emits counts-only JSON for data-quality gating).

**Method statement (precise, no certification invented):** rule-based
removal/generalisation in the shape of the HIPAA Safe Harbor method,
45 CFR 164.514(b)(2), for the categories implementable over text/JSON,
driven by a config file (`examples/default-config.json`): email,
phone, fax, URL, validated IPv4, SSN shape, dates (ISO/US/long-form,
validated — generalised to year-only by default), ZIP → first 3
digits + `00`, labelled identifiers and site custom regex patterns;
a JSON walker applies per-key category transforms plus a free-text
pass; document structure and clinical values are preserved. A second
date mode, `shift`, performs deterministic per-record-key date
shifting (FNV-1a + splitmix64 over salt+key, bounded by
`shift_max_days`) — a limited-dataset technique, explicitly **not**
Safe Harbor. **This is not an expert-certification determination and
not a compliance guarantee** (stated verbatim in the crate README and
library docs). Declared NOT implemented: IPv6, non-US formats,
ages->89 aggregation, photographs/biometrics (not applicable to
text/JSON). Honest limits: unlabelled names/cities in prose are not
regex-detectable; FHIR `identifier[].value` needs a site custom
pattern.

Tests: **33/33 pass** (`cargo test`: 9 unit + 20 integration + 4 CLI),
including a release-binary smoke proving shift determinism (same
record key → identical output, both dates shifted −32 d; a different
key → +105 d).

**Pipeline integration (coordinator-applied):** `deidentify` joined
the governed pipeline step vocabulary (`configstore.
PIPELINE_STEP_TYPES`, params validated: mandatory `command`,
optional `config` / `recordKeyField` / `timeoutSeconds`), and
`app/pipelines.py` gained `_step_deidentify`, which invokes the
binary once per dataset row and **fails closed** — a missing binary,
non-zero exit, timeout, or non-JSON output raises into the runner's
existing retry/dead-letter machinery; rows are never passed through
un-deidentified. A seeded pipeline `deidentify-export`
(extract-table → deidentify) ships **disabled**, because the binary
must be installed on the app PATH first. New coordinator tests:
`tests/test_platform_deid_pipeline.py` (9) — vocabulary governance
(missing command → 422), seeded-disabled 409, unit transformation
with the real binary (identifiers out, structure preserved),
fail-closed without binary and on non-zero exit, and an end-to-end
runner pass through the real binary.

## Workstream 4 — Database adapters + Studio DB platform

New module `backend/app/dbplatform.py`, router `/dbplatform`
(reads `studio.read`, writes `studio.manage` — existing capabilities
only). Three governed config kinds, registered by the coordinator:
`dbplatform` (connection profiles), `dbplatform-schema` (deployment
schema extensions), `dbplatform-transform` (transformer/export
profiles) — all draft→publish over the shared StudioConfig store with
hash-chained audit on transitions, mirroring `studio_config`.

- **Adapter matrix** (`ENGINES`, served at `GET /dbplatform/engines`):
  per-engine driver, SQLAlchemy dialect, URL example (credential-free),
  capability matrix (vector search, JSON behaviour, identifier case,
  transactional DDL) and a per-engine `verification` field (table
  below).
- **Connection profiles:** host/engine/adapter/vault-key-NAME only.
  The validator refuses any credential-shaped field in the payload
  (422) — credentials are referenced by vault key name, never stored
  in config. There is no "test connection" endpoint in v1: profile
  validation is structural, and a profile goes live only when an
  operator points `DATABASE_URL` / `WAREHOUSE_DATABASE_URL` at the
  engine (the two existing env seams in `app/db.py` /
  `app/warehouse.py` are the runtime switch; the platform governs and
  documents them, it does not hot-swap live connections).
- **Canonical DDL export:** `GET /dbplatform/ddl/{engine}` compiles
  `CreateTable` for every canonical table (operational `app.db.Base`
  + warehouse `WarehouseBase`, `variant=app|warehouse|both`) against
  each engine's SQLAlchemy dialect — test-asserted for all six SQL
  engines; unknown engine 422; `mongodb-ferretdb` has no SQL DDL
  (422, by design).
- **Schema designer:** `GET /dbplatform/schema` read model over the
  canonical schema; governed draft extensions (kind
  `dbplatform-schema`) may add **deployment-extension columns only** —
  any alter/drop of a canonical clinical/FHIR table is refused 422.
  The clinical FHIR schema itself is not user-editable; that boundary
  is the design, stated in the module docstring.
- **Transformer workbench:** `POST /dbplatform/transform/preview`
  applies the governed Phase 13 `MAPPING_TRANSFORMS` vocabulary to
  sample rows and commits nothing; export profiles are governed
  drafts (kind `dbplatform-transform`).

### Per-engine verification states

| Engine | Verification | Evidence |
|---|---|---|
| PostgreSQL 16.15 | **live-verified (conformance subset)** | Real server on this VM; conformance subset passed (coordinator re-run: see below) |
| SQLite | **live-verified (baseline suite)** | The whole backend suite runs on it every gate |
| MariaDB 10.11.14 | **live-verified (conformance subset)** | apt-installed on this VM; same subset passed |
| MySQL | dialect-only — **live NOT RUN** | No MySQL server in the verification environment (MariaDB is the live-verified MySQL-family engine); DDL compiles, dialect imports |
| MSSQL | dialect-only — **live NOT RUN** | No SQL Server instance available; DDL compiles for the full canonical schema, dialect imports |
| Oracle | dialect-only — **live NOT RUN** | No Oracle instance available; DDL compiles, dialect imports |
| MongoDB via FerretDB | **live-verified (document-store smoke only)** | FerretDB **v2.7.0** binary (`~/workspace/toolchains/ferretdb`, `--handler=sqlite`, state in the toolchains dir) ran; a pymongo 4.19.0 smoke (ping, insert_one, find_one, update_one) passed. NOT an app-engine claim: the application has **no MongoDB adapter seam** — the clinical store is SQLAlchemy-only; the matrix entry exists so a deployment can plan document storage beside the SQL store, never instead of it |

The **conformance subset** (defined in
`tests/test_platform_db_live.py`, parameterised over both live
engines, env-gated by `OPENCARE_TEST_PG_URL` /
`OPENCARE_TEST_MARIADB_URL`; absent env → skip, stated plainly):
boot `create_app` on the live URL → health route → dev-token auth →
FHIR Patient create + read → Studio draft→publish round-trip →
offline-batch HL7 item accepted (published `integration/offline`
enabled) → audit `verify_chain` → warehouse ETL drain into the
same-engine warehouse database (`<db>_wh`) asserting an OMOP row.
Plus dbplatform governance exercised on the live engine.
Coordinator's independent re-run: unit file **51 passed**; live file
**4 passed** (subset ×2 engines + governance ×2 engines). In the
default gate (env unset) the live tests skip — 4 skipped, with the
live numbers quoted here from the coordinator's own env-set run.
Stated exclusions (inherited from the Phase 14 SQA §9 pattern): the
subset does not re-prove flowsheets/MAR step-level role enforcement
or the shipped-disabled lake-replay 409 on the live engines — those
remain SQLite-proven. One implementation wart, disclosed: on this
SQLAlchemy version a `mariadb+pymysql://` URL only resolves after a
one-time dialect registration in `app.dbplatform` (`app.db` stays
untouched); MariaDB DDL also compiles through that registered
dialect. MSSQL DDL compiles JSON columns to `NVARCHAR(MAX)` via a
module-local type variant (no pre-2025 native JSON type).
Drivers installed into the project venv by the builder and added to
`backend/requirements.txt` by the coordinator: `psycopg[binary]`
3.3.6 (already listed), `PyMySQL` 1.2.3, `pymongo` 4.19.0
(smoke-only, commented as such).

## Workstream 5 — Hot/cold tiering

New module `backend/app/tiering.py`, router `/tiering`. Policy =
published Studio config kind `integration`, key `tiering`:
`{enabled (default false), hot_window_days (default 90), classes[]}`
(per-payload-type overrides, `*` wildcard) — publishable through the
existing Studio API; no new config kind was needed. The cold store
sits behind a `ColdStore` protocol (`put`/`get`/`exists`) — an
**S3-shaped interface with a local filesystem implementation**
(`FilesystemColdStore`, content-addressed by SHA-256 with a JSON
sidecar, rooted at env `OPENCARE_COLD_DIR`), labelled as such: it is
NOT S3 and no object-storage service is claimed. The job moves
lake raw-zone payloads past the hot window: cold write +
`LakeLineageLink` (`link_kind "tiered-cold"`,
`curated_ref "cold://<sha256>"`) + `payload_text` emptied with
`content_hash` UNTOUCHED + `append_audit("lake.tiering.moved")`.
**Nothing is deleted**; retrieval `GET /tiering/cold/{id}` verifies
the SHA-256 before serving (mismatch → 500; unverified bytes are
never served); `GET /tiering/status` (`devices.read`);
`POST /tiering/run` (`pipelines.run`; 409 when the policy is
disabled). Idempotent; runs are audited. **No daemon is claimed** —
the only trigger is the operator endpoint. Tests:
`tests/test_platform_tiering.py`, **13/13 pass** (default-disabled
policy, disabled no-op + 409, old-moves/young-untouched, verified
retrieval, retrieval 404s, tamper → 500, class overrides both
directions, wildcard class, idempotency, audit rows, status counts,
role gates).

## Workstream 6 — SOP / protocol framework

New module `backend/app/protocols.py`, router `/protocols`, config
kind `protocol` (registered by the coordinator in
`configstore.CONFIG_KINDS`/`VALIDATORS` via a lazy-import wrapper —
`app.protocols` imports `configstore`, so the validator resolves
lazily). A definition carries steps with per-step role, checkpoint
(a governed form key), timer (`timer_minutes`) and escalation
thresholds. Execution keeps its own tables (`ProtocolInstance`,
`ProtocolStepRecord`) on the shared Base; timers/escalation posture
is **computed at read** (`due_at`, `breached`, `elapsed_minutes`,
applicable escalation = highest exceeded `after_minutes` threshold)
— **no daemon is claimed**. Endpoints: definition draft create
(409 if a draft exists) / draft-only edit / publish (prior published
→ superseded) / history / published read; instance start (404 no
published definition; 409 duplicate active instance for the same
protocol + module_ref); advance (409 not-current/not-active; **403
when the caller's role ≠ the step's declared role and the caller is
not admin** — a per-step role check inside the capability gate;
422 when a checkpoint `formKey` is declared but no form submission
accompanies the advance); escalate (409 when none due; a second
escalation 409s); abandon (starter-or-admin); `GET
/protocols/coverage` computed live from published definitions.
Capabilities are existing only: definitions `studio.manage`; reads,
coverage and instance reads `studio.read`; start/advance/escalate/
abandon `fhir.write` (justified in the module docstring). Audits:
`protocols.definition.create/.publish`,
`protocols.instance.start/.advance/.complete`, escalate/abandon.

**Proof SOP — laboratory:** `lab-critical-result-callback`
(module `laboratory`): notify (clinician, 30-minute timer,
checkpoint `critical-result-readback`) → readback (clinician,
checkpoint) → document (clinician) → verify-closure
(**configurator** — included specifically so the wrong-role test
proves the per-step check: a clinician passes the `fhir.write`
capability gate yet receives 403 naming the configurator role);
escalation `{after_minutes: 45, to_role: admin, action:
"page-on-call"}`. The framework **seeds nothing**: the proof exists
in the test suite (`tests/test_platform_protocols.py`, **16/16
pass**), and a fresh deployment has zero published protocols until a
site publishes one — per-module coverage below is stated on that
basis. (Repo token note: the configurator dev token is
`config-token`.)

### Per-module SOP coverage

| Module | Governed `protocol` definition? | Basis |
|---|---|---|
| Laboratory | **YES — test-proven only** | `lab-critical-result-callback` is created + published through the API and executed end-to-end in `tests/test_platform_protocols.py`; it is NOT seeded, so a fresh deployment starts with none |
| Pharmacy | NO | Per-module machinery only (verification work centre, BCMA five-rights, controlled-substance counts) — no governed protocol definition |
| ED / continuum | NO | ESI timers, EMTALA gate, activation workflows as module code + config — no governed protocol definition |
| Inpatient | NO | Discharge checklist, I-PASS, acuity rules as module machinery — no governed protocol definition |
| Orders | NO | Order sets + per-transition workflow roles — not protocol definitions |
| Revenue | NO | Denial/claim state machines — not protocol definitions |
| Workforce | NO | Credential-expiry worklist + rules engine — not protocol definitions |
| Devices | NO | Calibration posture + quarantine policy config — not protocol definitions |
| Engagement | NO | Teach-back + follow-up workflows as module machinery — not protocol definitions |

The §11 final-gate criterion (a published definition per module) is
therefore met today only by laboratory, and only via a site's own
publish action. Cuts, with reasons: no disable/rollback endpoints for
definitions (the generic Studio API provides them now that the kind
is registered); no `skipped` transition route (the status value
exists; no skip operation was specified).

## Workstream 7 — Report engine

New module `backend/app/report_engine.py`, router `/report-engine`,
kind `report-definition` (registered by the coordinator). A
definition names a dataset + fields + filters + grouping +
formats + optional schedule + distribution. **Hard rule, enforced by
a recursive payload scan: any `sql`/`query` key → 422; the dataset
must name an existing server-defined dataset from `app.reports`
(`EXPORT_DATASETS`); fields/filters/grouping validate against the
dataset's actual output keys; unknown dataset/field/op → 422. No
SQL path exists at all.** Governance mirrors Studio (draft→publish,
published immutable, supersedes, audited
`report-engine.definition.*`; governance `studio.manage`, reads
`studio.read`, runs `reports.run`). A run applies filters
(eq/ne/in/gte/lte/contains) → projection → grouping (groups carry
`{key, count, rows}`), records an in-module `ReportRun` row +
`append_audit("report-engine.run")`, and renders json / csv
(`reports._csv_text`) / pdf (`reports.build_pdf` + the published
`integration/print` template; 100-row render cap, noted in the PDF).
The only run parameter is `as_of`, recorded on the artefact — it
does **not** time-travel (datasets are current-state builders), and
the module says so. Distribution v1 = run-record artefacts only;
any other distribution type → 422. **Scheduling:** definitions may
carry a 5-field cron string (documented subset: `*`, `*/n`, values,
comma lists, ranges; day-of-week 0 and 7 = Sunday; range-with-step
and month/day names NOT supported); `POST /report-engine/run-due`
executes definitions whose cron matches the evaluated minute and
that have no scheduler run yet for that minute (actor `"scheduler"`).
**No daemon is claimed** — an operator or external cron invokes the
endpoint (the Phase 14 pattern).

### Phase 13 mapping (`phase13_mapping()`, `GET /report-engine/phase13-mapping`)

All five named Phase 13 reports were assessed for faithful
re-expression as engine definitions. **Verdict: 0 of 5 expressible —
none seeded, deliberately** (approximating them would have been
fabrication):

| Query | Expressible | Why not |
|---|---|---|
| census-by-unit | NO | Aggregates live bed state per unit (occupied/available/cleaning/blocked + computed occupancy %). No export dataset carries Location/bed rows; engine grouping counts rows — it cannot derive per-status buckets or the ratio |
| claims-summary | NO | Groups claims by status and SUMS remittance financials (`revenue.claim_financials`). `billing-lines` carries only per-line net amounts — no claim status, no remittance fields; grouping counts, never sums money |
| credential-expiry | NO | Computes `days_to_expiry` vs today + status from the published worklist window over `StaffCredential`. No dataset carries credentials; the time-derived fields exist in no dataset |
| safety-events | NO | Reads `SafetyEvent` rows; no export dataset carries safety events |
| payments-summary | NO | Groups `PatientPayment` by method and SUMS amounts; no payments dataset exists (billing-lines are claim lines, not payments), and grouping counts rather than sums |

The five remain served by `app.reports` exactly as before; the
engine generalises the catalogue for dataset-shaped reports.
Tests: `tests/test_platform_reports.py`, **12/12 pass**.

## Workstream 8 — Indicator library

New module `backend/app/indicators.py`, router `/indicators`, kind
`indicator` (registered by the coordinator). A definition:
`{name, title, source, thresholds{warn, critical}, target,
direction (higher-better|lower-better), unit}`, where source is
`{type: measure, measure_key}` (computed by `app.quality` over the
warehouse), `{type: cohort, cohort_key}` (`app.pophealth` registry
summary), or `{type: named, name}` from the in-module registry —
`census.current`, `lab.pending-verification`, `lab.critical-open`,
`safety.open-events` — each computed by the owning service's own
code path (no duplicated logic; no named source was dropped).
`GET /indicators/{key}/value` computes from LIVE data and returns
`{key, value, unit, target, status (ok|warn|critical|unknown),
evaluated_at, source}`. **Honesty rule:** `unknown` (value null) is
returned only for missing source data — no published measure/cohort
config, suppressed counts, or a zero denominator; a genuine 0 count
is reported as the fact 0. Thresholds drive status; target is
reported, not status-driving. Governance mirrors Studio
(`studio.manage` / `studio.read`, audited `indicator.definition.*`).
`default_indicator_payloads()` mirrors the seeded
`operations-overview` dashboard widget-for-widget — `operations-
census` → `census.current`, `operations-critical-open` →
`lab.critical-open` (warn 1 / critical 5), `operations-verification-
queue` → `lab.pending-verification` (warn 5 / critical 10) —
**un-mirrorable KPIs: none**. The payloads ship as publishable
defaults under the module's governance (auto-seeding published
indicators was left as a parent decision, per the builder's
handoff); the existing dashboards are NOT rewired (existing files)
— the library is the shared source going forward. Tests:
`tests/test_platform_indicators.py`, **9/9 pass** (including a
measure value of 60.0 computed from a seeded warehouse, census
moving 0→1→2 across real admission calls, and ghost-source →
unknown with reason).

## Workstream 9 — Design-system specification

`docs/DESIGN_SYSTEM.md` (new file) documents the presentation layer
as it exists in code: 7 principles; the three-tier organisation
(grouped menu → sub-menus → in-panel tabs, published-config-driven,
modules-only rule); the token set from `styles.css :root` (`--ink
#17222b`, `--muted #5b6b76`, `--accent #0b6e4f`
[branding-overridable], `--border #d6dee3`, error tones) plus the
supporting literals (page `#f6f8f9`, washes, warn/critical/info
pairs); geometry (card radius 10 px, buttons 8 px, inputs 6 px,
pills 999 px, content max 980 px, sidebar 220 px, collapse <720 px);
the shared components — Tabs (only the active panel renders),
Stepper (label + state WORD, never colour alone), StatusBadge
(tones ok/warn/bad/info/neutral), StatusViews (Loading / Empty-with-
reason / ErrorView specialising 403/401/status-0), DashboardView
(big-number/line/bar/gauge/table, hand-rolled SVG; **gauge semantics
higher-is-better ONLY**), FormRenderer (FHIR Questionnaire shape);
the panel patterns (`.page-header` = title + ONE primary action,
cards, tables, `.field`/`.inline-form`/`.row-actions`, primary vs
secondary vs `.linklike` buttons, `.downtime-banner`,
`.studio-color-chip`); 9 conformance rules for new work; and the
deliberate non-goals (no CSS framework, no chart library, light
theme only, print = server PDF on governed templates). The visual
report designer remains unbuilt (the report engine renders governed
definitions to screen JSON + PDF; there is no drag-and-drop report
designer, and none is claimed).

## Workstream 10 — Studio full parity (Min's directive, 2026-10-18)

**Deliverable 1 — the parity gap map** (`docs/STUDIO_PARITY.md`,
RTM STU-16): all 40 Agentic_App Studio surfaces (shell + 39
components) mapped to OpenCare status with a verdict and a reason
per row, under Min's three standing adaptation rules (no arbitrary
code execution in the clinical runtime; SaaS billing/licence not
applicable to an EHR Studio; business modules map to clinical
equivalents). Verdict tally: **BUILD 13 · ADAPT 14 · EXCLUDE 8 ·
DEFER 5**.

**Deliverable 2 — the builds.** Backend: `app/ai_providers.py`
(kind `ai-provider`, router `/ai-providers`, 11 tests) — provider
registry whose payloads reference API keys by env-var NAME only
(any secret-shaped field refused 422; `/ai-providers/effective`
reports `keyConfigured` as a boolean, never a value; no live model
call is made anywhere). `app/studio_ops.py` (router `/studio-ops`,
10 tests) — approvals inbox aggregating real pending items (a
boot smoke returned a real seeded draft awaiting publish),
organisation read model over the real locations tree + staff
directory + accounts, data-views registry over governed datasets,
and validators for kinds `endpoint` and `tool` (declarations only,
born disabled, no code execution). Config kinds 23 → **26**.
Web: six new Studio tabs (AI providers, Approvals, Organisation,
Data views, Database, Registry) taking StudioPage from 14 → **20
tabs in 4 groups** (new Platform group), the Pack Builder
(composes a draft pack through the existing pack API), and the
designer completions (form designer palette aligned to the
backend validator + FormRenderer vocabulary, workflow canvas view
over the same draft model, dashboard widget authoring). All six
new `/ai-providers` + `/studio-ops` boot-smoke routes returned
200 with real data on the integrated app.

**Studio-parity coverage list** (every gap-map row, outcome):
BUILD→built this pass: AiProviders (row 4), Approvals inbox (5),
AutomationCanvas→workflow canvas view (7), DataViews registry
(15), DesignEditors via widget+form designer completions (16),
Endpoints→`endpoint` declarations (18), Forms designer completion
(19), Integrations→MCP/tool registry surface (21, surface only),
Migrations→Database tab over WS4 schema/DDL (26), Modules→Pack
Builder completion (28), Organization (30), Tools→`tool`
declarations registry (37), Widgets→widget designer (39).
ADAPT→the clinical adaptation is the standing answer: shell (1),
ADK (2), Actions→governed rules (3), Automations→rules/pipelines/
outbox (8), Behaviors→workflow/form/registration primitives (9),
BusinessIntel→indicators/analytics/report engine (12), Designer
host (17), Instruments→Phase 14 device registry + lake (20),
Metrics→indicator library (25), Plugins→governed packs (32;
URL-fetch installs excluded, supply-chain), ServerDatabase→WS4 DB
platform without .env/password handling (34), StreamsJobs→lake +
pipeline jobs (35), Transformations→mapping kind + WS4 workbench
(38), Workflows (40).
EXCLUDE (with reason): Billing (10, SaaS), BusinessModule shell
(13), License (22, SaaS), MarketEntry (24, business-specific),
Notebooks (29, Rule 1), Packages (31, Rule 1), Scripts (33,
Rule 1), Terminal (36, Rule 1).
DEFER (named, never dropped): StudioAssistant chat (6, Phase 16
live-model wiring), StudioBlocks (11) + CanvasEditor (14,
Studio-completion block before Phase 15, re-evaluate against the
design system), MLOps (23) + MLOps Platforms (27, Phase 16
research scope).

## RTM findings disposition

- **STU-16 (gap map missing)** — closed: the map exists (above);
  the RTM predated it.
- **Gate-table placeholder** — closed: the table at the head of
  this report is the final combined gate over the enlarged tree
  (artefacts `gatePLAT2_A.xml` / `gatePLAT2_B.xml`), replacing the
  interim WS1–9 table.
- **STU-04 (pack-enablement route gating homeless)** — **mechanism
  BUILT in this pass** (`app/pack_gate.py`, 6 tests in
  `tests/test_pack_gate.py`). Semantics: opt-in and OFF by default
  (published `integration/pack-gating` = `{"enabled": true}` turns
  it on); when on, a request matching a pack-declared
  `(method, path)` is refused 403 unless at least one declaring
  pack has an `enabled` installation (ANY-enabled rule, so shared
  core endpoints are not held hostage by every specialty pack);
  `{param}` segments match one segment; the 403 names the
  declaring pack(s). Exempt: `/health` and everything under
  `/studio` — the operator escape hatch (Studio stays reachable
  to enable a pack or publish the gate back off). The gate is
  governance enforcement, not a security boundary: auth and
  capability checks still run inside routes, and an internal gate
  failure fails open (the downtime-guard precedent) so a guard
  bug cannot take the API down. What remains — the default-ON
  rollout posture, release/DR drills, and Air click-through
  proof — is owned by **Phase 17** (production readiness; its
  charter is burning down the NOT RUN list on the real stack).
  STU-04 does not close homeless.

## UX audit fixes (Navigation & UX audit, 2026-10-18)

- **F-1 — front-desk check-in was a dead end. FIXED.**
  `POST /continuum/clinic/appointments/{id}/check-in` (booked →
  arrived, 409 otherwise, audited) gated by `continuum.work` OR
  `registration.work` — the same one-step widening booking already
  uses; rooming stays `continuum.work`-only (front-desk room
  attempt is 403, test-proven). The clinic-day view now returns
  the day's booked appointments as `arrivals`, and ClinicDayPage
  renders an Arrivals worklist whose `FrontDeskSteps` strip shows
  step 3 as current — the strip is honest for the first time; the
  Home hint that pointed at the unopenable Scheduling page now
  names the Clinic Day board. Tests: backend
  `tests/test_ux_checkin.py` (5), web `UxAuditFixes.test.tsx`.
- **F-2 — Home's My Patients / My Unit lists were display-only.
  FIXED.** `PatientList` rows open the chart (1 click) via an
  `onOpenChart` callback the shell now passes to HomePage (the
  same handler PatientsPage uses); without an opener the lists
  stay display-only (portal-safe default). Web tests pin both.
- **F-3 / S-10 — Safety navigation seed drift. FIXED.** The
  configstore navigation seed promised Safety to `_STAFF`
  (including viewer) while the page gate is `safety.review`
  (viewer excluded). The gate is the correct artefact — the
  triage queue is the safety team's; filing stays staff-wide via
  `safety.report` — so the seed now names exactly the
  `safety.review` holders (`_WRITE_STAFF`), with the reasoning at
  the seed. Mechanism: `tests/test_nav_seed_consistency.py` (4
  tests) asserts, for **every** seeded module, that each seed
  role passes the module's page gate under `CAPABILITY_DEFAULTS`,
  that the test's gate map covers exactly the seeded module set
  (a new module without a gate entry fails loudly), and that the
  Safety seed equals the `safety.review` holder set exactly.
  Safety was the only drift the test found.

**Navigation contradictions** (§2A of the design system vs the
shell as built — stated, not smoothed over):

1. Ungrouped modules lead the sidebar: `resolveNavGroups` emits
   the null/ungrouped bucket before Patient Care, contradicting
   N4's group-scan order. Reachable only if a site publishes an
   ungrouped module; the seed groups all 24 modules.
2. Studio tabs are not role-filtered for visibility: any holder
   of Studio access sees all 20 tabs (actions remain
   capability-gated) — a partial N2 exception inside Studio.
3. No global shell search (spec N-7): patient find lives in Home
   and Patients only. Named gap; S-7, Phase 15.
4. Group-size rule (spec N-2, adopted in the §2A addendum):
   Operations holds 8 modules; Laboratory, Pharmacy and System
   Studio hold 1 each. Regrouping is config (S-11, Phase 15).
5. No place persistence (spec N-8): state-based shell — refresh
   loses the place; no shareable links (S-13, Phase 15).
6. Landing tabs are fixed per page, not per role (spec N-9):
   Pharmacy lands every role on Dispense & stock (S-15, Phase
   15); sidebar sections and page tabs are not 1:1 in Laboratory
   and Pharmacy (spec N-5; S-6, Phase 15).
7. Home renders no `<h2>` title (spec P-1; S-9, named a gate fix
   in the spec itself).

**S-deferrals by number** (the spec's S-1…S-15; Min ratifies):
S-1, S-2, S-10 — **built here** (as F-1/F-2/F-3). S-3 job-role
profiles — design adopted in the DESIGN_SYSTEM §2A addendum;
enforcement build Phase 15 (the spec's own phasing; the mechanism
does not fall out of the Studio parity work — it belongs to the
auth/session layer). S-4 shared Tabs/Stepper migration —
deferred to the Studio-completion block before Phase 15: the
design system already names the shared components normative, and
migrating ChartPage/StudioPage tab internals inside a
gate-closing pass risks the most-tested surfaces for no
behavioural gain. S-5, S-6, S-7, S-11, S-12, S-13, S-15 — Phase 15
(per the spec's own phase column). S-8 tone sweep — tones are
defined in the design system (WS9); the all-boards sweep is the
gate check the spec names. S-9 — gate fix (spec's own label).
S-14 wide boards — the rule is adopted in the §2A addendum; the
layout build is Phase 15.

**Click budgets after the fixes** (spec §B3 counting, from
sign-in; code-path arithmetic — browser E2E remains NOT RUN):
front-desk register → book → check-in **6, completable by the
front-desk role end-to-end** (budget ≤ 6 ✓); chart from a Home
list **1** (budget 1 ✓); nurse home → chart → MAR administer
**5** (budget ≤ 5 ✓); clinician home → chart → order signed
**5** by the same arithmetic (budget ≤ 5 — previously 6 via the
re-find the F-2 fix removes); pharmacist landing → verification
queue still **3** (budget ≤ 2 ✗ — F-10, owned by S-15).

## RTM cross-reference — the 38 UNMET-and-unscheduled requirements

The RTM counts 38 UNMET-and-unscheduled requirements
programme-wide (TEFCA, jurisdiction packs, blood-bank lookback,
KMS rotation, snooping analytics, log redaction, idle lock, the
cross-cutting ops block, and others). **This pass closes none of
them, and no workstream in it silently absorbs one.** The nearest
adjacencies, stated so the RTM does not have to guess: the Rust
de-identification step is privacy-adjacent but is not snooping
analytics, log redaction, KMS rotation or idle lock; the DB
platform's credential law (key names only, never values) is
security-adjacent but is not KMS rotation or any other Track K
item; pack-enablement gating was STU-04, dispositioned above on
its own line — it is not one of the 38 disclaimed here.

## Configuration coverage (configure-don't-fork)

Every platform capability is governed Studio configuration or a
documented environment seam; no site difference requires a fork:

- **Tiering policy** — published config kind `integration`, key
  `tiering` (enabled flag, hot window, per-class overrides);
  disabled by default; publishable today through the Studio API.
- **Protocol definitions** — kind `protocol` (registered this pass):
  steps, per-step roles, checkpoints (governed form keys), timers,
  escalations; draft→publish, published immutable, publish
  supersedes. Sites publish their own SOPs; none are seeded.
- **Report definitions** — kind `report-definition` (registered):
  dataset/fields/filters/grouping/formats/schedule/distribution,
  all validated against the server-defined dataset catalogue.
- **Indicator definitions** — kind `indicator` (registered):
  source (measure/cohort/named), thresholds, target, direction,
  unit; the operations-overview KPIs ship as publishable defaults.
- **DB connection profiles** — kind `dbplatform` (registered):
  engine/host/adapter + vault key NAME only; credential-shaped
  fields refused 422.
- **Schema extensions** — kind `dbplatform-schema` (registered):
  additive deployment-extension columns only; canonical clinical
  tables cannot be altered or dropped (422).
- **Transformer/export profiles** — kind `dbplatform-transform`
  (registered), reusing the governed Phase 13 mapping-transform
  vocabulary.
- **Pipeline de-identification** — the `deidentify` step joined the
  governed pipeline step vocabulary; its rule set is a config file
  carried in the step params (`config`, `recordKeyField`); the
  seeded `deidentify-export` pipeline ships disabled.
- **Go services** — environment configuration only (`CORE_URL`,
  `MLLP_ADDR`, `GATEWAY_HEALTH_ADDR`, `GATEWAY_SPOOL_DIR`,
  `EDGE_ADDR`, `EDGE_QUEUE_DIR`, token via
  `OPENCARE_GATEWAY_TOKEN`); they consume the core's published
  `integration/offline` config — when it is disabled the core
  answers 409 and the gateway keeps payloads spooled.
- **Runtime engine choice** — the pre-existing env seams
  `DATABASE_URL` / `WAREHOUSE_DATABASE_URL` /
  `OPENCARE_COLD_DIR`; the DB platform governs profiles and
  documents the matrix around those seams (no hot-swap claimed).
- **Design system** — tokens are the existing CSS custom
  properties; `--accent` remains branding-config-overridable, as
  before.
- **AI providers (WS10)** — kind `ai-provider` (registered):
  provider type, base URL, model, department default, key
  referenced by env-var NAME only; draft→publish governance;
  nothing calls a model in this pass.
- **Endpoint + tool declarations (WS10)** — kinds `endpoint` and
  `tool` (registered): declarations only, born disabled; enabling
  a declaration is a governance marker — it creates no route and
  executes nothing (stated in the Registry UI).
- **Pack gating (STU-04)** — published config kind `integration`,
  key `pack-gating` (`{"enabled": true}`); ships OFF; the gate's
  on/off posture is itself governed configuration.
- **Navigation seed (UX F-3)** — the Safety module's seeded roles
  now match its page gate (`safety.review` holders); a site's
  published navigation remains free to differ — the consistency
  test pins the SEED, and the shell's AND of nav roles +
  permissions matrix remains the runtime rule.

## Compliance coverage

- **Compliance register: 27 requirements — unchanged.** No platform
  workstream adds a register row; the register is clinical/regulatory
  scope and nothing in this pass claims a new regulatory function.
- **HIPAA Safe Harbor (45 CFR 164.514(b)(2))** — the Rust de-id
  implements the *shape* of the Safe Harbor method for text/JSON
  categories and says so; it is explicitly NOT an expert
  determination and NOT a compliance guarantee (crate README +
  library docs + this report). Date shifting is labelled a
  limited-dataset technique, not Safe Harbor.
- **Audit** — every new governed surface writes the hash-chained
  audit: tiering moves (`lake.tiering.moved`), protocol definition
  and instance transitions, report runs (`report-engine.run`) and
  definition transitions, indicator definition transitions,
  dbplatform draft/publish transitions. The live conformance subset
  re-verified `verify_chain` on PostgreSQL and MariaDB.
- **Credentials** — no credential is stored in any config payload
  (dbplatform validator refuses credential-shaped fields, tested);
  the Go token comes from the environment; the live-test database
  passwords existed only in shell environment variables, never in
  files.
- **Capabilities: 58 — unchanged.** Every new route group gates on
  existing capabilities (`studio.read`/`studio.manage`,
  `devices.read`, `pipelines.run`, `fhir.write`, `reports.run`);
  no capability was invented, so the web capability mirror is
  untouched.

## Integration edits applied (coordinator, after "sync closed")

Builders created new files only during the sync window; the
following edits to existing files were listed as pending by the
builders (or found necessary by the coordinator) and applied after
the parent lifted the coexistence rule. Each is listed with its
reason:

1. `backend/app/configstore.py` — registered kinds `protocol`,
   `report-definition`, `indicator`, `dbplatform`,
   `dbplatform-schema`, `dbplatform-transform` in `CONFIG_KINDS`
   and `VALIDATORS` (lazy-import wrappers; the owning modules
   import `configstore`, so top-level imports would cycle). Added
   `deidentify` to `PIPELINE_STEP_TYPES` + its params validation.
2. `backend/app/main.py` — imported and mounted the five new
   routers (`protocols`, `tiering`, `report_engine`, `indicators`,
   `dbplatform`). NOTE: an initial coordinator edit also called a
   `protocols.ensure_protocols_defaults(...)` seed that does not
   exist — the framework seeds nothing by design; the call was
   removed before any gate ran (see Incidents).
3. `backend/app/pipelines.py` — `_step_deidentify` + `_STEP_IMPLS`
   entry, `import subprocess`, seeded `deidentify-export` pipeline
   (disabled), seed docstring count four → five.
4. `backend/tests/test_phase14_pipelines.py` — the seeded-pipeline
   key-set assertion grew by `deidentify-export` (+ its
   ships-disabled assertion), commented as a platform-pass change;
   no assertion weakened.
5. `web/vite.config.ts`, `web/nginx.conf`, `web/nginx.tls.conf` —
   proxy prefixes for `/protocols`, `/tiering`, `/report-engine`,
   `/indicators`, `/dbplatform` (the Phase 9 defect pattern: route
   groups missing from a proxy list fail silently in proxy mode).
   The TLS template's list was additionally behind `nginx.conf`
   (missing `continuum`, `people`, `reports`, `devices`,
   `pipelines`, `accounts`, `registration`); it was brought to full
   parity — a pre-existing lag found and fixed during integration.
6. `docker-compose.yml` + `services/go/Dockerfile` (new) —
   `mllp-gateway` service (host port 2576, spool volume) and
   `edge-node` service (behind the `edge` profile) for the Go tier.
   YAML-validated only; Docker NOT RUN on this VM.
7. `backend/requirements.txt` — added `PyMySQL` (MySQL/MariaDB
   dialects) and `pymongo` (FerretDB smoke only), with comments;
   `psycopg[binary]` was already listed.
8. `docs/PHASES.md` — platform-pass entry added (coordinator).
9. `docs/DESIGN_SYSTEM.md`, `services/go/**`, `services/rust/**`,
   the five new backend modules and seven new test files — new
   files, created during the pass.

**WS10 / STU-04 / UX additions** (same pass, after the RTM and UX
findings landed):

10. `backend/app/configstore.py` — registered kinds `ai-provider`,
    `endpoint`, `tool` (total config kinds 23 → 26) with
    lazy-import validator wrappers; Safety navigation seed roles
    `_STAFF` → `_WRITE_STAFF` (UX F-3, with the reasoning at the
    seed).
11. `backend/app/main.py` — mounted the `ai_providers` and
    `studio_ops` routers; added the `pack_gate_guard` middleware
    (after `downtime_guard`); `create_app` now loads pack
    manifests at seed time (`studio.load_pack_manifests`) — the
    root-cause fix in Incidents.
12. `web/vite.config.ts`, `web/nginx.conf`, `web/nginx.tls.conf` —
    proxy prefixes for `/ai-providers` and `/studio-ops`, all
    three files (the parity rule from item 5).
13. `web/src/api/types.ts` + `web/src/pages/studio/helpers.ts` —
    `ConfigKind` / `CONFIG_KINDS` extended with `ai-provider`,
    `endpoint`, `tool`. `web/src/pages/StudioPage.tsx` — six new
    tabs wired (20 total, new Platform group) + `PackBuilder`
    mounted in the packs section. New files:
    `web/src/pages/studio/{AiProvidersTab, ApprovalsTab,
    OrganisationTab, DataViewsTab, DatabaseTab, RegistryTab,
    PackBuilder}.tsx` + two test files; designer completions
    edited `FormsTab.tsx`, `WorkflowsTab.tsx`, `DashboardsTab.tsx`
    in place.
14. UX fixes — `backend/app/continuum.py` (check-in endpoint +
    clinic-day `arrivals`), `web/src/api/client.ts`
    (`clinicCheckIn`), `web/src/api/types.ts` (`ClinicDay.arrivals`),
    `web/src/pages/ClinicDayPage.tsx` (Arrivals section),
    `web/src/pages/HomePage.tsx` (chart opener + hint correction),
    `web/src/App.tsx` (passes `onOpenChart` to HomePage). New
    tests: `backend/tests/test_pack_gate.py`,
    `backend/tests/test_studio_ai_providers.py`,
    `backend/tests/test_studio_ops.py`,
    `backend/tests/test_nav_seed_consistency.py`,
    `backend/tests/test_ux_checkin.py`,
    `web/src/pages/UxAuditFixes.test.tsx`; new modules
    `backend/app/ai_providers.py`, `backend/app/studio_ops.py`,
    `backend/app/pack_gate.py`; new doc `docs/STUDIO_PARITY.md`.

## NOT RUN list (platform pass)

- **Docker / docker compose** — no Docker on this VM; the new
  compose services and the Rust/Go images are YAML/static artefacts
  only. (The binaries themselves are test-verified natively.)
- **MySQL live** — dialect-only (no server in the verification
  environment; MariaDB live-verified as the MySQL-family engine).
- **MSSQL live, Oracle live** — dialect-only; no instances exist
  here. DDL compilation + dialect imports are test-asserted.
- **FerretDB as an application engine** — NOT RUN by design: no
  Mongo adapter seam exists; only a document-store smoke was run.
- **S3 / MinIO cold store** — NOT RUN; the cold tier ships a
  filesystem implementation behind an S3-shaped interface.
- **Live LLM / agent runs** — unchanged standing gap; no model
  endpoint exists anywhere in the environment.
- **Live Keycloak OIDC, live TLS serving, live Epic SMART launch**
  — unchanged standing gaps (templates/config only).
- **Browser click-through E2E** — blocked on this VM (loopback
  policy); the Air remains the browser gate. WS10's six new Studio
  tabs, the Pack Builder, the designer completions and the Clinic
  Day arrivals section are unit-test-proven and build-clean, not
  browser-proven; the UX click budgets in this report are
  code-path arithmetic for the same reason.
- **Mobile compiled builds** — unchanged (source only; sandbox
  `other_tcp` block + CI Workflows-permission 403 stand).
- **Pack gate default-ON rollout** — the STU-04 mechanism is built
  and test-proven but ships OFF (opt-in published config); the
  default-on posture, release/DR drills and Air proof are Phase
  17's (see the RTM disposition). Pack endpoint declarations
  still do not create routes.
- **Live model execution for AI providers / StudioAssistant /
  MCP** — Phase 16 by Min's placement directive; this pass ships
  the governed config + surfaces only (honest-off).

## Incidents

- **Coordinator's first combined DB run failed on test-database
  setup, not code:** the live conformance subset derives a
  same-engine warehouse database (`<db>_wh`) beside the test
  database; the coordinator's throwaway test role/database grants
  covered only the primary database (MariaDB 1044 + the Postgres
  equivalent). After creating/granting the `_wh` databases the live
  file passed 4/4. The builder's own run had provisioned both.
- **A coordinator main.py edit briefly referenced a non-existent
  seed function** (`protocols.ensure_protocols_defaults`): the
  protocols framework seeds nothing by design. Caught by the first
  integration smoke (AttributeError at `create_app`), removed, and
  re-verified — no gate ran against the broken state.
- **The DB builder's original full-file run** (started before its
  own fixes landed, under sibling-suite load) delivered 5 failed /
  43 passed / 3 errors. The builder accounted for every item: the
  PostgreSQL/MariaDB DDL failures were a real dialect-loading bug
  (`sqlalchemy.dialects` has no `postgresql`/`mariadb` attribute
  for `getattr` — fixed by resolving dialects through
  `create_mock_engine`); the transform-preview failure traced to a
  stray inherited `engines.py` carrying an unrelated `StudioError`
  class (the module's canonical class lives once in
  `configstore.py`; the stray file was deleted — it is absent from
  the final tree and nothing imports it); one failure was a
  test-authorship bug (a config key the module never had), corrected
  in the test; the 3 errors were the run starting against a live
  server before the fixes landed. The coordinator's clean re-run of
  the same files on the final tree — 51 passed (unit) + 4 passed
  (live) — is the deciding evidence quoted in this report.
- **Protocols suite duration:** 16 tests take ~472 s because every
  test boots a fully seeded `create_app` (~26 s each on this VM);
  the same effect makes the whole platform test block slow. Not a
  failure — recorded so the gate timings are not mistaken for
  hangs.
- **Pack gate's first run failed 4 of 6 — a boot-order root
  cause, not a gate bug:** the gate's config read and declaration
  matching were correct, but a fresh `create_app` database held
  zero `ModulePack` rows: pack manifests were only loaded lazily
  when a `/studio` handler first called
  `studio.load_pack_manifests(session)`. Fixed by loading
  manifests in the `create_app` seeding block (the loader is
  idempotent, upserts by name+version): pack manifests are
  reference data and now exist from boot. After the fix the file
  passes 6/6. Recorded because the lazy load would have made the
  gate silently inert in any deployment that enabled it before
  anyone opened Studio.
- **Check-in tests' first run failed on a fixture assumption:**
  the shared test app seeds no `pat-1`, so the booking helper 404'd
  ("Patient/pat-1 not found") — a test-authorship error, fixed by
  creating the patient through the FHIR API in the helper (the
  Phase 12 pattern). The endpoint was never implicated; the
  corrected file passes 5/5.


---

## Parent addendum — independent re-verification (2026-10-18, evening)

The parent re-ran every suite from staging, sequentially, on its own
invocations (backend as two halves over the 57 test files; junit
`~/workspace/pPLAT_parent_A.xml` / `pPLAT_parent_B.xml`; logs
`pPLAT_parent_*.log`):

| Suite | Coordinator | Parent re-run |
|---|---|---|
| Backend half A (30 files) | 366 passed | **366 passed** |
| Backend half B (27 files) | 329 passed, 4 skipped | **328 passed, 4 skipped, 1 FAILED → fixed, see below** |
| Web (vitest) | 322 passed | **322 passed** |
| Web build (`tsc --noEmit && vite build`) | clean | **clean (exit 0)** |
| ADK | 8 passed | **8 passed** |
| Go (`go test ./...`) | 11 tests | **all packages ok** |
| Rust (`cargo test`) | 33 passed | **33 passed, 0 failed** |

**The one failure — a timezone-window test flaw, found and fixed.**
`tests/test_phase6_inpatient.py::test_discharge_checklist_form_submit_and_expected_discharges`
failed on the parent's half-B run (19:52 PDT) after passing on the
coordinator's afternoon run. Root cause, proven before any change:
the test pinned `TODAY = date.today()` (local zone) while the server
defaults its date parameters to UTC today (`/inpatient/expected-discharges`
documents "defaults to today, UTC"; `app.inpatient._now` is
`datetime.now(timezone.utc)`). Between 17:00 and 24:00 PDT the local
and UTC dates differ, and the default-date leg of the test then
queries a different day than the discharge plan it just wrote. Proof:
the identical test passes under `TZ=UTC` (1 passed, 1.13 s). The
product behaviour is as documented — this was a test-authorship flaw,
invisible in same-date hours. Fix (no assertion weakened): the test
now computes `TODAY = datetime.now(timezone.utc).date()`, with the
reason recorded in a comment at the change. The full file re-ran
**15/15 passed inside the mismatch window** (20:00 PDT), bringing the
parent backend total to **695 passed, 4 skipped** — the coordinator's
numbers, now reproducible at any hour.

**Parent NOT RUN (stated plainly):** the 4 env-gated live-engine
tests (`tests/test_platform_db_live.py` — PostgreSQL + MariaDB
conformance/governance) were NOT re-run by the parent: the database
servers the coordinator used were installed on a VM instance that
has since been replaced, and no server binaries or data directories
survive on this machine. They skip by design without
`OPENCARE_TEST_PG_URL` / `OPENCARE_TEST_MARIADB_URL`. The
coordinator's live evidence (4 passed against PostgreSQL 16.15 +
MariaDB 10.11.14) stands as reported in this SQA; the parent's own
live-engine run remains outstanding and belongs to Phase 17's
live-stack evidence.

**Verdict:** the platform pass is parent-verified on the counts
above, with the one test flaw repaired in staging
(`backend/tests/test_phase6_inpatient.py` — a test-only change; it
post-dates the Air Run 6 sync and rides the next one). The standing
NOT RUN list in this report is otherwise unchanged.
