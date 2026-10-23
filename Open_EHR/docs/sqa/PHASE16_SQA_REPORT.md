# Phase 16 SQA Report — Intelligence & Research

**Status: BUILT IN STAGING — coordinator gate below; nothing in this
report counts until the parent agent re-runs the full gate
independently.** Synthetic data only; no real PHI anywhere in this
phase. No GitHub push, no Mac sync (release held until after Phase 17
and the final rigid gate).

Phase 16 delivers the intelligence layer on the Phase 10 governed
agent substrate: supervised agent workflows, long-context RAG with
citations, the agent validation/change-control harness (BD-41), MCP +
full A2A as governed Studio configuration, a radiology vision-model
drafting assistant over Phase 15's DICOM pixel pipeline, genomics
with pharmacogenomic DUR checks, research & R&D operations with
Part 11 e-signature, EMPI merge governance, document intake, the
Phase 16 gateway legs (public health, clearinghouse, EDI/eRx),
federated exchange, governed analytics with an OMOP vocabulary
store, device maintenance/HTM, and the Rust terminology step —
plus all fifteen Phase 16 burn-down items (BD-36…BD-50) from the
Pre-Release Burndown Plan.

## Gate table

| Suite | Result |
|---|---|
| Backend pytest (10 sequential alphabetical chunks, per-chunk private basetemps) | **1,071 passed, 2 skipped, 0 failed** (Phase 15: 897 + 4 skipped; the 2 skips are the MariaDB legs of `test_platform_db_live` — its PostgreSQL legs ran LIVE, see below) |
| BD-45 live pgvector (phase-local PostgreSQL 16.2 + pgvector 0.8.0, `OPENCARE_TEST_PG_URL` set) | **PASSED live** — standalone run and again inside gate chunk 06 (98 passed, no skip): mirror table created by the app, raw `<->` neighbour asserted, API reports `store == "pgvector"` |
| Platform DB live (PostgreSQL legs of `test_platform_db_live`) | **PASSED live** in gate chunk 09 (conformance subset + dbplatform governance against the phase-local instance + sibling warehouse DB) |
| Web vitest | 410 passed / 60 files (Phase 15: 389; +21 = 9 Studio-tab + 12 module-page tests) |
| Web build (`npm run build` = tsc + vite build) | exit 0 |
| ADK service pytest (own venv) | 8 passed |
| Go (`go test ./...`) | ok — all packages (edge, gateway, hl7msg) |
| Rust (`cargo test`) | 43 passed (lib 16 incl. 7 terminology, de-id CLI 4, de-id integration 20, term CLI 3) |
| Backup/restore drill (re-run incl. Phase 16 tables) | **PASS** — row counts match across 167 tables (18 of them Phase 16's), audit hash chain verifies on the restored database |
| Browser E2E | NOT RUN (standing VM limitation — hosted tooling refuses loopback; Min's Air click-through is the acting browser gate) |

Pre-gate (coordinator, sequential ≤5-file batches before the full
gate): all 18 Phase 16 test files plus the directly affected
existing suites (Phase 10 agents/compliance, nav-seed consistency,
Phase 11 pharmacy ops) pass after the registered adjustments
below; every failure encountered on the way — and its cause — is
in the adjusted-tests register and incidents. The full-suite gate
above is the gate of record.

## Per-BD-item disposition (BD-36…BD-50)

- **BD-36 — Federated record location + query (TEFCA-shaped):
  ADDRESSED (interface-complete).** `app.federation`: peers are
  published `integration/federation` config; record location +
  consented summary query across instances, proven by a real
  two-uvicorn-subprocess cross-instance test with hash-chained audit
  asserted in BOTH databases. Consent is enforced on the RESPONDING
  side (deny wins; the audit entry commits before the 403 returns).
  Live TEFCA network participation: NOT RUN (external; waiver
  candidate per the burndown plan, not waived).
- **BD-37 — External record staging + reconciliation: ADDRESSED.**
  `mode=references` is data-behind-glass (no identifiers, no clinical
  payload); staged ExternalRecords reconcile accept-new /
  reject / supersede — accept writes through `fhir.create_resource`
  with a provenance extension and refuses overwrite (409).
- **BD-38 — Public-health reporting gateway: ADDRESSED to the
  interface.** Aggregate extracts compute from live warehouse data
  (measure- and cohort-sourced definitions; `contains_patient_data:
  false`); submission follows the interop outbox pattern
  (append-only attempts, governed retry budget, dead-letter); with
  no gateway URL published the state is `queued-for-gateway`,
  never claimed sent. Production submission: NOT RUN (external
  onboarding).
- **BD-39 — Clearinghouse submission + 999/277 acks: ADDRESSED to
  the interface.** The submitted payload is `billing.render_837p`,
  byte-identical to the export endpoint (test-asserted); 999 (AK9)
  and 277CA (STC) acknowledgements parse to accepted/rejected and
  are recorded on the claim + audit; transport failure is never
  recorded as a rejection. Live clearinghouse: NOT RUN (external).
- **BD-40 — X12 270/271 eligibility + NCPDP SCRIPT: ADDRESSED to
  the interface.** `build_270` (005010X279A1, SE count reconciles) /
  `parse_271` land eligibility in the Phase 7 table with
  `basis: "payer-edi"` — test-proven side-by-side never conflated
  with `local-config`; unconfigured endpoint → 503 with nothing
  recorded. NewRx XML is built well-formed and Status/Error
  responses parsed. Live payer endpoints + Surescripts
  certification: NOT RUN (external).
- **BD-41 — AI model validation + change control (DSI): ADDRESSED.**
  `app.agent_validation`: governed evaluation datasets (hashed,
  audited), recorded-mode scoring (harness mechanics without a
  model), live mode honest-503 with no model; enablement of any
  agent now requires a CURRENT validation record matching the exact
  published version + model route (wired into `agents.enable_agent`);
  seeded grandfathered baselines cover the two Phase 10 seed
  agents; PCCP-style change control with an exceptions list;
  drift reporting against the validation baseline.
- **BD-42 — CTMS coverage analysis + billing grids: ADDRESSED.**
  `app.research_ops`: coverage determinations
  (research/clinical/mixed, append-only) gate study activation —
  activation refuses with the exact undetermined-procedure list;
  billing grids separate research-billed from standard-of-care
  lines with exact totals and a released, audited grid artefact
  (export payload; grid lines are study-level and patient-less, so
  no draft claim lines are fabricated — rationale in the module).
- **BD-43 — Innovation pipeline: ADDRESSED with one named partial.**
  Intake → triage → pilot → exercise → evaluation → decision proven
  end-to-end. Pilot authorisation substitutes for global agent
  enablement INSIDE pilot scope/window only (enablement state is
  never written — test-asserted). PARTIAL: cohort-scoped pilot
  exercise is implemented but NOT test-proven (the per-test
  warehouse is empty, so the endpoint honestly 403s); the unit-scoped
  path is proven.
- **BD-44 — OMOP vocabulary store (ATHENA-shaped): ADDRESSED with
  one named partial.** Warehouse gains VocabConcept /
  VocabRelationship; the loader is format-complete and the corpus
  proof loads 21,000 concepts + 5,000 relationships with exact
  coverage against the concept-map baseline; reload is idempotent.
  PARTIAL: a real ATHENA bundle download is NOT RUN (account-gated
  external download, GB-scale).
- **BD-45 — pgvector-backed retrieval: ADDRESSED, live-verified.**
  On PostgreSQL the RAG store is a pgvector table and search is
  served by `<->` (responses name the store actually used);
  the env-gated conformance test PASSED live against the
  phase-local PostgreSQL 16.2 + pgvector 0.8.0 instance (see gate
  table). Evidence is a phase-local instance, not the production
  deployment — stated plainly. On SQLite/dev the store is the
  local-JSON mirror with identical API.
- **BD-46 — 21 CFR Part 11 e-signature: ADDRESSED.**
  `app.esignature`: signatures bind signer + meaning + timestamp to
  a sha256 snapshot hash of the exact record state; signing a record
  that changed since the signer's snapshot is refused 409; the
  manifestation is served (`GET /esign/manifest/...`); protocol
  approval and coverage determinations consume signatures. The
  `cfr-part-11` regulatory pack is a labelled DRAFT (legal
  attestation out of scope).
- **BD-47 — EMPI merge/unmerge governance: ADDRESSED.**
  `app.empi`: candidates from the Phase 14 dedup table + manual
  pairs; execute requires confirm + a second steward (proposer
  deciding their own request → 403); snapshots + cross-reference
  are written BEFORE the merge in the same transaction; unmerge
  restores body AND version byte-identically; the cross-reference
  is retained after unmerge. Enforcement reads the published
  `integration/empi` switches; no auto-merge path exists.
- **BD-48 — Document intake (fax/document classification + filing):
  ADDRESSED to the interface.** Bytes live in the tiering cold store
  (`cold://<sha256>`, byte-exact); classification is a governed
  draft agent's SUGGESTION; confirm-to-file creates a real
  DocumentReference with an intake-provenance extension, and filing
  to a patient different from the suggestion requires a reason
  (422 otherwise); unfile marks entered-in-error and deletes
  nothing. Live fax hardware/carriers: NOT RUN.
- **BD-49 — FHIR directories (Practitioner/Organization/Consent):
  ADDRESSED.** FHIR SUPPORTED += Practitioner/Organization/Consent
  with per-type validation; `app.directories` seeds published
  `integration/directories` and syncs create-or-UPDATE keyed by
  identifier (seeded data → 19 Practitioners / 3 Organizations);
  dry-run writes no resources; every sync incl. dry-run is
  hash-chain audited.
- **BD-50 — Rust terminology service in the pipeline engine:
  ADDRESSED with stated limits.** `params.engine: core|rust` is
  governed pipeline config; parity is proven by real pipeline runs
  over both engines (identical extracts; row errors identical,
  order-insensitive); a bogus binary fails closed with a recorded
  run error and no fallback. Rust suite: 43 tests. Limits: the
  `row.system` branch is covered by Rust unit tests only (no
  governed extract emits a `system` field today); release build +
  throughput benchmark NOT RUN — no performance number is claimed.

## Scopes B–I

- **B — Supervised agent workflows: DONE.** New governed config kind
  `agentflow`; the step vocabulary is exactly {agent, review} — any
  other step type is refused 422 at validation AND at run start (no
  run record exists). Agent steps execute only through the Phase 10
  substrate (`app.agents`); a review step parks the run
  `awaiting-review`; reject TERMINATES the run (downstream agent
  transport-call count asserted zero); honest-off records the run
  failed with the verbatim reason and no fabricated output. No code
  path writes clinical data.
- **C — Long-context RAG with citations: DONE.** `app.rag` over
  chart chunks + published `knowledge` documents; every result
  carries its citation (source type/id/title/snippet); retrieval is
  audit-logged with the query sha256 (raw query text is never
  written); `/rag/search` works with no model; `/rag/answer` is a
  governed agent run and returns the honest 503 with no model. The
  default embedder `local-hash-v1` is labelled in code and in every
  response as a lexical stand-in, NOT a semantic model; an endpoint
  embedder is used only when a published ai-provider config names
  one, and failures fall back labelled, never silently.
- **D — Ambient / chart-search / deep-research agents: DONE as
  governed drafts.** `ambient-scribe` (audio modality),
  `chart-search`, `deep-research` (maxSteps 16) are DRAFT agent
  configs seeded through the same draft → publish → enable
  governance as every agent; a DRAFT `agentflow`
  (`supervised-note-drafting`) chains them behind a human review
  step. Live model run: NOT RUN.
- **E — MCP + full A2A as governed Studio config: DONE.**
  `mcp_server` is a governed config kind whose validator refuses
  credential-shaped fields and credential-embedded URLs (422 before
  storage); server tools are seeded in published
  `integration/mcp-tools` ALL DISABLED with capability guardrails
  evaluated before any HTTP; every call is audited without raw
  payloads. A2A: the AgentCard is built from published + ENABLED
  agents only; task lifecycle + `message/stream` SSE replay of
  recorded transitions; delegation headers accepted only from
  admin/configurator and recorded on the task + audit. Conformance
  is tested against the protocol surface in-process; a live external
  MCP/A2A peer is NOT RUN.
- **F — Radiology vision-model assist: DONE (drafts only).**
  Governed DRAFT agent `radiology-vision-assistant` over the
  Phase 15 pixel pipeline (SHA-256-verified cold-store objects,
  pydicom conversion; unconvertible frames reported skipped with
  reasons, never invented; no convertible frames → 422). Drafts
  carry findings/impression/protocol/triage/measurements + the prior
  study's report context; accept creates a PRELIMINARY report
  through radiology's own functions — the module contains no
  final/verified status write (test + grep asserted). Honest-off
  writes no draft row. Live vision-model run: NOT RUN.
- **G — Genomics: DONE.** Variants are FHIR Observations (HL7
  Genomics Reporting IG shape; no profile conformance claimed);
  PGx rules are published `terminology/pharmacogenomics` config
  (seeded rules labelled EXAMPLE) evaluated through the pharmacy
  DUR pattern: findings ride the verification queue beside
  monitoring blockers/warnings, and an `avoid` finding refuses
  verification (422, naming the finding) BEFORE the formulary
  refusals — precedence decision recorded under incidents. NO
  variant-calling pipeline exists or is implied (by design).
- **H — Research & R&D ops: DONE.** Studies/procedures/coverage/
  billing grids (BD-42), Part 11 e-signature (BD-46), and the GCP
  (ICH E6) + GLP (21 CFR 58 / OECD) regulatory packs seeded as
  labelled DRAFTS; six register entries added to the compliance
  register (27 → 33). Legal attestation: out of scope, packs are
  drafts.
- **I — Maintenance / HTM: DONE.** Work orders + preventive
  maintenance on the Phase 14 device registry (no second registry):
  PM schedules are published `terminology/maintenance` config; PM
  completion advances the device's maintenance-due; downtime orders
  drive the device status and restore it only when no other
  downtime order is open.

## Configuration coverage (configure, don't fork)

Every item names its Studio surface; an item without one would be
incomplete by the standing rule.

| Item | Studio surface |
|---|---|
| BD-36/37 federation | Published config `integration/federation` (peers, modes); capabilities federation.read/work |
| BD-38 public health | Published config `integration/public-health` (gateway URL + extract definitions) |
| BD-39 clearinghouse | Published config `integration/clearinghouse` |
| BD-40 EDI/eRx | Published config `integration/edi` (payer test endpoints); basis tagged `payer-edi` |
| BD-41 validation | Published config `integration/agent-validation` (require switch, validity days, thresholds); datasets + change control as governed records; `agents.validate` |
| BD-42 CTMS | Study records via API; GCP pack parameters govern cadence (below) |
| BD-43 innovation | Published config `integration/innovation` (pilot bounds); `innovation.manage` |
| BD-44 vocabulary | Published config `integration/vocabulary` (directory); kind `analytics_view` for views |
| BD-45 RAG | Kind `knowledge` (documents); agent `rag-answerer` draft; `rag.search` |
| BD-46 e-signature | DRAFT regulatory pack `cfr-part-11` (requireMeaning, manifestFields) |
| BD-47 EMPI | Published config `integration/empi` (second-steward + confirm switches); `empi.steward` |
| BD-48 intake | Published terminology `terminology/intake-doc-types`; DRAFT agent `document-intake-classifier`; `intake.work` |
| BD-49 directories | Published config `integration/directories`; capabilities directories.read/sync |
| BD-50 Rust term | Pipeline config `params.engine` (core|rust) on governed pipeline definitions |
| Scope B workflows | Kind `agentflow` (draft → publish); runs ride agents.view/agents.run |
| Scope D agents | Kind `agent` drafts: ambient-scribe, chart-search, deep-research |
| Scope E MCP/A2A | Kind `mcp_server`; published `integration/mcp-tools` (all disabled); published `integration/a2a`; capabilities mcp.use/a2a.use |
| Scope F imaging | Kind `agent` draft `radiology-vision-assistant`; rides radiology.work/boards.read |
| Scope G genomics | Published terminology `terminology/pharmacogenomics` (EXAMPLE rules); capabilities genomics.read/work |
| Scope H research | DRAFT regulatory packs `gcp-ich-e6`, `glp-21cfr58-oecd`, `cfr-part-11`; capabilities research.read/work, esign.sign |
| Scope I maintenance | Published terminology `terminology/maintenance` (PM intervals per device type); capabilities maintenance.read/work |
| Navigation | `navigation/main` seed += federation, research, analytics, documents (Data & Platform); regrouping reaches existing deployments on republish (standing precedent) |

## Compliance coverage

- Register: +6 entries (ICH E6(R2) 8.1 essential documents,
  ICH E6(R2) 5.18 monitoring, 21 CFR 58.195 raw-data retention,
  21 CFR 58.29 QA audits, 21 CFR 11.50 signature manifestations,
  21 CFR 11.70 signature/record linking) → 33 entries on a fresh DB.
  §170.315(b)(11) DSI: the BD-41 harness is its operating evidence
  (validation records + change control, audit-chained).
- `validate_regulatory_pack` now accepts flat list-of-scalars
  parameter values (the GCP pack's essentialDocuments and the
  Part 11 pack's manifestFields are lists); nested objects and
  nulls remain refused.
- Every Phase 16 mutation is hash-chain audited in the same
  transaction (federation, gateway, agentflow, validation,
  innovation, RAG retrieval, MCP calls, A2A tasks, imaging drafts,
  genomics, research, e-signature, EMPI, intake, analytics,
  maintenance, directory syncs incl. dry-runs).
- Consent: federation queries are consent-gated on the responding
  side (BD-36); document intake files only on human confirmation;
  agents draft, humans dispose — no autonomous clinical action
  exists in any Phase 16 module (agentflow step vocabulary,
  imaging accept path, pilot exercise scope all enforce this in
  code, not policy text).

## Adjusted-tests register

No assertion was weakened silently. Every adjustment below only
ADDS the BD-41 enablement prerequisite (a current validation
record) that Phase 16 itself mandates; the original assertions are
unchanged.

1. `tests/test_phase10_agents.py` — 4 tests
   (`test_run_modality_gate`,
   `test_run_runtime_unreachable_is_honest_503`,
   `test_run_no_model_is_honest_503`,
   `test_run_records_draft_and_disposition`): shared helper
   `_validate_for_enable` mints the record via a one-case
   recorded-mode dataset run before the enable POST. Reason: BD-41
   gate (C3's pre-delivery analysis named exactly these four; the
   two seeded agents pass unadjusted on their grandfathered
   baseline records; two further tests pass incidentally because
   their assertions are capability 403s that fire first).
2. `tests/test_phase16_agent_workflows.py` — helper + 4 enable
   sites (chain-a, chain-b, modelless-a, down-a). Written in
   parallel with the gate.
3. `tests/test_phase16_doc_intake.py` — the classifier
   publish+enable helper mints the record between publish and
   enable.
4. `tests/test_phase16_imaging_assist.py` — `_enable_vision_agent`
   mints the record between publish and enable.
5. `tests/test_phase16_rag.py` — 3 rag-answerer enable sites.
6. `tests/test_phase16_mcp_a2a.py` — 6 a2a-worker enable sites.
7. `tests/test_nav_seed_consistency.py` — MODULE_GATES extended
   with the four Phase 16 modules and their page gates
   (federation.read / research.read / analytics.use / intake.work),
   transcribed from session.ts PAGE_CAPABILITY as wired by the
   coordinator; the test's own coverage assertion required the
   extension (it fails loudly on unregistered modules by design).
8. Coordinator-authored `tests/test_phase16_pgx_wiring.py` (new,
   3 tests) proves the F1 → pharmacy wiring end-to-end (queue
   findings, avoid refusal, flag advisory + clean control).
9. `web/src/pages/Phase16Pages.test.tsx` — three test-layer
   repairs, no assertion weakened: (a) the Federation tests'
   `findByText("peer-a")` was ambiguous (the string renders both
   as the peer `<option>` and in the results area) and
   race-dependent — replaced with a role query for the option;
   (b) the EMPI confirm-label regex carried a stray space
   (`Patient/ b2`) that can never match the rendered label —
   corrected to `Patient/b2`; (c) the imaging panel's
   no-finalise assertion used a bare text query that matched the
   panel's own hint copy ("there is no finalise control here") —
   replaced with the button-role query that expresses the actual
   guarantee (no finalise/verify/final CONTROL exists).
10. Web snapshot growth for the four new modules (expectations
    extended, in position, with per-phase comments in the
    established style): `web/src/auth/session.test.ts` per-role
    nav key lists (clinician/admin/configurator + federation,
    research, analytics, documents; viewer + federation,
    research only); `web/src/auth/runtimeConfig.test.tsx`
    SEED_NAV fixture += the four modules at orders 24–27 exactly
    as the backend seeds them (studio/portal renumbered 28/29);
    `web/src/pages/studio/models.test.ts` capability count
    58 → 77 with the nineteen Phase 16 capabilities enumerated in
    the comment.
11. `web/src/auth/session.ts` `canAccess` — a SOURCE completion,
    not a test change: the static role switch had no cases for
    the four new PageKeys, so the static nav and the
    published-config resolver disagreed (the runtimeConfig test
    caught it: resolver returned 28 items, static nav 24). Cases
    added mirroring the server capability defaults
    (federation/research staff-wide; analytics/documents
    admin/configurator/clinician).
12. `tests/test_depth_nav.py` — EXPECTED_GROUPS extended with
    the four Phase 16 modules (all "Data & Platform") and
    DEVICES_SECTIONS with the Phase 16 Maintenance section,
    matching the seeded navigation payload's legitimate growth
    (the full-suite gate's chunk 01 caught both pins; the
    sections pin surfaced on the chunk's re-run).
13. `tests/test_frontdoor.py` —
    `test_register_gains_unique_user_identification`: register
    total pin 27 → 33 (the six GCP/GLP/Part 11 seed entries are
    the legitimate growth; the test's HIPAA-specific assertions
    are unchanged). Same adjustment class as Phase 12's
    register growth.
14. `tests/test_phase13_reports.py` —
    `test_phase13_register_entries`: register total pin
    27 → 33, with the test's growth-narration comment extended
    by the Phase 16 sentence (the citation-specific assertions
    are unchanged). Caught by the full-suite gate's chunk 03.
15. `services/rust/tests/term_cli.rs` — the shared `run()`
    helper's stdin write no longer unwraps: the usage-error
    cases exit before stdin is read, so the write races the
    child's exit (the gate run lost the race — BrokenPipe panic
    in the harness, not a CLI defect; the child's fast usage
    exit is the behaviour under test). Assertions (exit code +
    stderr via wait_with_output) unchanged.
16. `tests/test_realignment.py` — the seeded-navigation module
    list extended with the four Phase 16 modules in seed
    position (the test's own comment documents this per-phase
    extension pattern) and the derived count pin 24 → 28.
    Caught by the full-suite gate's chunk 09.

## Incidents (recorded, not buried)

- **BD-41 gate vs parallel suites:** the enablement gate landed at
  integration and refused enables in five sibling suites written
  before it existed (28 BD-41 refusals across three pre-gate
  batches, every one traced to the gate's own 409 message; three
  further failures in the same batches were the PGx-precedence
  finding below). Fixed at the test layer with the registered
  adjustment above — the gate itself was never relaxed.
- **BD-45 live-only defect (rag.py):** the first live pgvector
  run failed where the SQLite mirror path never could:
  `_pg_search`'s `:pref` parameter raised
  `psycopg.errors.AmbiguousParameter` because the `IS NULL`
  disjunct gives the parameter no inferable type under
  server-side binding. Fixed in `app/rag.py` with
  `CAST(:pref AS VARCHAR)` (comment in code); the live test then
  passed. Recorded because it is exactly the class of defect the
  live leg exists to catch — and because it validates the
  decision to run BD-45 live rather than accept the mirror.
- **PGx precedence:** the first wiring placed the PGx check after
  verify_order's formulary refusals; the seeded EXAMPLE-rule drugs
  are not on the seeded formulary, so the genomic stop was masked.
  The check now runs before the formulary refusals (documented in
  code); formulary admission in the wiring test uses the governed
  publish path.
- **C3 hook syntax:** the validation child's prescribed one-line
  hook was a Python SyntaxError as written (compound statement
  after a semicolon); the coordinator rewrote it as equivalent
  multi-line code with identical semantics and message text.
- **/tmp exhaustion:** the first pre-gate attempt ran 18 files in
  one invocation; the 512 MB tmpfs filled mid-run and the run was
  discarded as tainted evidence and re-run in ≤5-file batches with
  per-batch basetemps — the standing gate method exists for
  exactly this reason.
- **VM restart:** the VM was replaced mid-phase; /home/postgres and
  the postgres user were wiped and rebuilt (recipe in the
  coordinator's ledger). A runtime completion delivery also
  reported one test run's result incorrectly during the phase;
  all gate evidence in this report was read from the coordinator's
  own session logs / redirected files.
- **Toolchains wiped with the VM (gate logistics):** the Go and
  Rust toolchains lived outside the workspace and did not
  survive the replacement. They were reinstalled INTO the
  workspace (Go 1.27.2 at `~/workspace/tools/go`, Rust via
  rustup at `~/workspace/tools/cargo` + `tools/rustup`) and the
  suites then ran green; the versions differ from the old VM's
  (unrecorded) ones, which is noted for the parent's re-run.
  On the first cargo run the `term_cli` harness lost a stdin
  race (see adjusted-tests item 15); after the harness fix the
  full cargo suite passed 43/43.
- **VM replacement #2 (during the full gate):** the VM was
  replaced AGAIN while backend gate chunk 06 was in flight —
  /tmp (and the first gate's logs) wiped, the phase-local
  PostgreSQL instance wiped with it. Chunk 06's outcome is
  UNKNOWN and was discarded, not inferred. Chunks 02/04/05 had
  completed and been fully observed before the wipe and no
  backend code changed during the gate, so their results stand;
  chunks 01/03 (whose failures had already been diagnosed and
  fixed) and 06–10 were re-run with logs kept in the workspace,
  and PostgreSQL was rebuilt from the workspace-persisted
  pgserver tree before the re-run.
- **PostgreSQL provisioning:** the VM's apt path was unusable
  (multi-hour apt-get update, package lists without postgresql-16);
  the phase-local instance was provisioned from the pip `pgserver`
  wheel (PostgreSQL 16.2) + pgvector 0.8.0 compiled from source
  against it. This is test infrastructure, not a product change.
- **G2 order interaction:** during development, doc-intake-first
  ordering was observed to break a Phase 10 agents test. On the
  integrated tree the coordinator ran BOTH orders in one process;
  both pass. Recorded as not reproducing post-integration.

## NOT RUN

- Live LLM run of any agent/RAG/workflow (no model endpoint
  configured in this environment) — honest-off paths are what is
  proven, per the phase evidence standard.
- Live vision-model run (Scope F) — same basis.
- Live TEFCA network participation (BD-36); live public-health
  gateway submission (BD-38); live clearinghouse (BD-39); live
  payer EDI endpoints + Surescripts certification (BD-40);
  live external MCP server / A2A peer (Scope E); live fax
  hardware/carriers (BD-48); real ATHENA vocabulary bundle (BD-44).
- AMR/genomic variant-calling pipeline — out of scope by design
  (Scope G); nothing was built or implied.
- Rust terminology release build + throughput benchmark (BD-50) —
  no performance claim is made.
- Browser E2E (standing VM limitation, see gate table).
- Legal attestation of the GCP/GLP/Part 11 packs — out of scope;
  the packs are labelled DRAFTS.

## Named partials

- **BD-43:** cohort-scoped pilot exercise implemented, not
  test-proven (unit-scoped path proven).
- **BD-44:** real ATHENA bundle not downloaded (loader
  format-complete; corpus proof on generated data).
- **BD-45:** live evidence is a phase-local PostgreSQL instance
  stood up for this gate, not the production deployment.
- **BD-50:** `row.system` parity rests on Rust unit tests (no
  governed extract emits a `system` field).
- **EMPI surface:** delivered as an API + a Data Platform tab in
  web wave 2 (coordinator decision; no separate nav module).

---

## Parent re-verification addendum (2026-10-19, parent-run)

The parent re-ran the full gate independently on the final tree: backend as 10 sequential alphabetical chunks with per-chunk private basetemps, then web, build, ADK, Go and Cargo, strictly sequential (toolchains at `~/workspace/tools/` after the VM replacements). Artefacts: `~/workspace/p16gate/`.

| Suite | Parent result |
|---|---|
| Backend `pytest tests/` (10 chunks) | **1,068 passed, 5 skipped, 0 failed** |
| Web vitest | **410 passed** |
| Web `npm run build` | **exit 0** |
| ADK | **8 passed** |
| Go `go test ./...` | **ok (exit 0)** |
| Cargo `cargo test` | **43 passed** |

**Reconciliation with the coordinator's 1,071 + 2:** totals are identical (1,073 tests). The difference is exactly 3 tests — the live-PostgreSQL legs of `tests/test_platform_db_live.py` (chunk_08) and the live pgvector test in `tests/test_phase16_rag_pgvector.py` (chunk_06). They PASSED live in the coordinator's gate against the phase-local PostgreSQL 16.2 + pgvector 0.8.0 instance, and SKIPPED in the parent's run because no such server was running on the parent's machine at gate time. No other test differs. The coordinator's live evidence for those 3 legs stands as reported; **the parent's own live-PostgreSQL/pgvector run remains NOT RUN** and is carried into Phase 17's live-stack evidence scope (the final gate will require it run or explicitly waived by name).

Named partials stand as reported (BD-43 pilot not test-proven; BD-44 ATHENA bundle NOT RUN — loader proven on generated data; BD-45 evidence on a phase-local PG instance; BD-50 parity caveats). Live LLM and vision-model runs remain NOT RUN — honest-off behaviour is what is verified.

**Phase 16 is parent-verified** (with the live-leg caveat above).
