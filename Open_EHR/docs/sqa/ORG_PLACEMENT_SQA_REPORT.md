# Organisation & Registration Placement — SQA Report

**Pass:** Organisation & Registration Placement (Min's directive,
2026-10-19: "registration user with department, region, unit,
location").
**Date:** 2026-10-19. **Staging only** — not published, not synced.
Synthetic data only. The parked `agents/` folder and `docs/AGENTS.md`
were not touched.

## What was built

Every registered user/employee now carries an organisational
placement at four governed levels — REGION → LOCATION (facility) →
DEPARTMENT → UNIT — and the hierarchy itself is governed
configuration data in the published `terminology/locations` config.

1. **Governed hierarchy (WS1).** The locations concept model gained
   two concept types — `region` (no parent) and `department`
   (parent = facility) — so the chain is region → facility (the
   LOCATION level; the pre-existing concept type name is kept so
   every Phase 6+ consumer is untouched) → department → unit → bed.
   The Units & Beds manager composes the new levels through the same
   draft → publish governance (new ops `add-region`,
   `add-department`; `add-unit` accepts a `department`; retiring ANY
   concept with active children now refuses 409). The Studio
   organisation read model renders the four levels (regions,
   departments per facility, staff rollups by every placement
   level) while keeping its existing facility/unit shape.
2. **Hierarchy service + read API (WS1).** `backend/app/
   org_hierarchy.py` is the ONE resolver: `validate_placement`
   (422s naming the missing level, the unknown code, or the broken
   link — including non-adjacent pairs), `placement_for_unit`,
   `placement_view`, `resolve_scope`, and `GET /org-hierarchy`
   (the nested tree for cascading selects; gated on
   `workforce.read`). Placement columns (`region`, `location`,
   `department`) were added to `StaffMember` and `UserAccount`;
   pre-existing databases get them through an inspector-guarded
   additive `ALTER TABLE` at startup (`ensure_org_columns`) and a
   governed hierarchy migration (`extend_locations_hierarchy` —
   publishes a NEW version of a legacy locations config in the
   four-level shape, system actor, audited) plus placement backfill
   (`ensure_org_placement`, one audit row when it changes anything).
3. **Registration placement (WS2).** Onboarding create REQUIRES a
   complete valid placement (422 naming the gap, validated before
   anything is written); the StaffMember is written with all four
   levels; stage progression blocks (409, existing blocker
   mechanism) while a record's placement is incomplete or no longer
   valid, naming the missing/invalid level; the account stage copies
   the placement onto the provisioned `UserAccount`. People
   create/update validate the effective placement chain when
   placement fields are touched; the staff list filters by region /
   location / department. Accounts inherit placement from a linked
   staff record, display it (codes + displays), and revalidate on
   edit (422 naming the broken link). Patient registration accepts
   an optional `registration_location`, validated as an active
   facility concept, stored as the Patient extension
   `urn:opencare:registration-location`, audited and echoed.
4. **Filters & rollups (WS3).** The workforce analytics handlers
   that carried the `?unit=` scope now also accept `region_id` /
   `location_id` / `department_id` (resolved through the one
   resolver; unscoped responses byte-identical). The command
   overview accepts the same scope: census units + totals are
   restricted to the scope and the projection is recomputed from
   scoped totals, with a `scope` block echoed (ED / boarding /
   transport remain facility-wide — they are not unit-attributed).
   Team outputs and the team panel carry the team's placement view.
   The one staff-based report (`credential-expiry`) accepts the
   four placement parameters through its governed parameter schema
   and filters rows by the holder's placement.
5. **UI (WS4).** The onboarding form's four cascading selects
   (parent-filtered, disabled-with-reason until the parent is
   chosen, submit blocked with the reason visible until the chain
   is complete, server 422s inline); workforce directory placement
   filters + placement rendering; accounts placement column; the
   Units & Beds manager's four-level tree + add-region /
   add-department draft forms; the team panel's placement line.

## Seed mapping (demo org migration)

Recorded as required: the seeded demo organisation, previously
facility → unit, now sits in the four-level hierarchy. The bed
census is unchanged (18 beds: 12 med-surg + 6 ICU).

| Region | Location (facility) | Department | Unit | Beds |
|---|---|---|---|---|
| region-main "OpenCare Region" | facility-main "OpenCare Hospital" | dept-medicine "Medicine" | unit-medsurg "Medical-Surgical Unit" | 12 |
| region-main | facility-main | dept-critical-care "Critical Care" | unit-icu "Intensive Care Unit" | 6 |
| region-main | facility-main | dept-emergency "Emergency Medicine" | ed "Emergency Department" | 0 |
| region-main | facility-main | dept-ambulatory "Ambulatory Care" | clinic "Outpatient Clinic" | 0 |

`ed` and `clinic` are bed-less unit concepts that already existed
in the published workforce config (app.people) and now exist in the
locations hierarchy too, so a placement can name them. The same
mapping drives `extend_locations_hierarchy` for pre-existing
deployments; a unit the mapping does not know is reparented to a
"General" department with the literal code `dept-general` under
its own facility (a facility-suffixed code is used only if a
`dept-general` already exists under a different facility), never
dropped. (Corrected post-gate: an earlier draft of this report
named the code `dept-general-<facility>`; the landed resolver
uses the contract's literal `dept-general`, a fix contributed by
the WS1 verification worker and confirmed on disk.)

Backfill at first boot after this pass: every seeded staff member
and account whose unit resolves gains its placement from the chain
(the demo directory's units all resolve); rows whose unit does not
resolve are left unplaced and counted in the `org.placement.
backfill` audit row.

## Configuration-coverage list

Per the standing rule (configure, don't fork; every item names its
Studio surface):

- **The hierarchy itself** — published `terminology/locations`
  config data (existing kind/key; the model gained the `region` and
  `department` concept types). Maintained through the Units & Beds
  manager (draft → publish) or Studio directly. **No new config
  kind was needed, and none was created.**
- **Placement requirement** — enforced in code at the onboarding
  boundary (it is a registration rule, not site data); WHAT a valid
  placement is comes entirely from the published hierarchy above.
- **Report parameters** — the `credential-expiry` report definition
  (existing `report` config kind) now DECLARES the four placement
  parameters in its governed parameter schema; undeclared
  parameters are still refused 422. Caveat: report definitions are
  seed-once governed config — a pre-existing deployment's published
  v1 lacks the new parameters (placement params 422 as "unknown
  parameter") until the definition is republished through Studio.
- Everything else (resolver, filters, rollups, UI) reads the
  published hierarchy; no site-specific organisation exists in code
  beyond the labelled demo seed + demo migration mapping above.

## Compliance coverage

No new compliance-register rows: this pass changes organisational
master data and its governance, not a regulated workflow. The
placement writes ride the existing hash-chained audit (onboarding,
people, accounts and registration audit payloads carry the
placement fields they set), and the hierarchy migration writes the
standard Studio create/publish audit rows.

## Gate (coordinator-run, this environment)

Live legs ON (workspace live stack: PostgreSQL 16.15 + pgvector,
MariaDB 10.11.14, MinIO, Keycloak 26 — started via
`~/workspace/live-stack/` scripts). Backend run as 20 sequential
byte-balanced chunks with per-chunk private basetemps on /tmp,
deleted between chunks.

- **Backend: 1,285 passed, 0 failed, 0 skipped** (1,285 collected).
  Main sequence: 1,274 passed / 5 failed / 6 skipped; every failure
  and skip was then resolved by a targeted rerun on the final tree
  (below), bringing the effective result to a clean sweep. New
  org-placement tests: 46 (hierarchy 19, registration 16, rollups
  11 — per-file counts re-verified on disk post-gate). Growth vs
  the Phase 17 close (1,239): +46 net (46 new org tests; the
  directories behaviour fix changed no test counts).
- **Web: 440 passed** (64 files; +8 vs Phase 17's 432 — the org UI
  tests). **Build: exit 0.**
- **ADK: 8 passed. Go: all packages ok (5 test-bearing). Cargo: 43
  passed.**
- Failure/skip resolution detail: chunk_08's 4 failures were the
  Phase 16 directory-sync Organization counts (see register item
  3) — rerun of `test_phase16_directories.py` on the fixed tree:
  8 passed. Chunk_13's 1 failure
  (`test_config_selects_s3_when_published`) was the sandbox egress
  proxy intercepting the loopback boto3 call — rerun with proxy
  variables stripped: the tiering file passes 6/6, and the live
  MinIO drill (its 1 skip) passes with the stack env supplied.
  Chunk_12's 5 skips were the live Keycloak OIDC tests, whose
  `OPENCARE_TEST_OIDC_*` variables the chunk harness did not
  carry — rerun of `test_phase17_oidc.py` with the stack
  credentials mapped: 14 passed, live against Keycloak 26.
- The gate survived two VM restarts (chunks are resumable from the
  recorded summary; no chunk result was carried forward without a
  completed run behind it).

## Adjusted-tests register

Test-only reconciliations; no assertion was weakened or deleted
anywhere. Behaviour-changing fixture updates are listed with their
reason.

1. `backend/tests/test_workforce_onboarding.py` — the `_create`
   helper now adds a valid placement to every create payload
   (region-main / facility-main / the department matching the
   unit), and `test_viewer_and_clinician_cannot_onboard`'s
   configurator success post now carries unit-icu + its full
   placement. Reason: onboarding create now REQUIRES a complete
   valid placement (this pass's directive). Assertions unchanged.
2. `backend/tests/test_phase13_reports.py` —
   `test_catalogue_seeded_and_runs_csv_and_pdf`: the census-by-unit
   `x-report-rows` expectation 2 → 4, with an explanatory comment.
   Reason: the locations seed now carries 4 unit concepts
   (unit-medsurg, unit-icu, ed, clinic), so the report — which
   lists every unit — emits 4 rows. The report's semantics are
   unchanged; the pin recorded the old seed's unit count.
3. `backend/tests/test_phase16_directories.py` + a behaviour fix
   in `backend/app/directories.py`. The FHIR directory sync minted
   Organizations only for facility/unit concepts; under the
   four-level hierarchy a unit's `partOf` would have pointed at
   its department — a resource the sync never created (a dangling
   FHIR reference, found at this pass's gate: 4 tests failed on
   the Organization counts, 5 actual vs 3 pinned). Fix (product
   code): the sync now mirrors the WHOLE hierarchy —
   `ORGANIZATION_LOCATION_TYPES` covers region / facility /
   department / unit, so every `partOf` resolves and the
   Organization tree matches the governed hierarchy; the type
   mapping labels regions and facilities `prov` (Healthcare
   Provider) and departments/units `dept` (Hospital
   Department). Test reconciliations (all in this one file): the
   Organization count pins 3 → 10 (1 region + 1 facility + 4
   departments + 4 units) across status/sync/dry-run/idempotency
   assertions; the republish test's post-add pins 4 → 11; and
   `test_sync_creates_then_idempotent`'s unit-icu `partOf`
   expectation updated from `Organization/facility-main` to
   `Organization/dept-critical-care` — a semantics update, not a
   weakening: partOf now names the unit's actual parent, which
   the hierarchy defines as its department (the assertion is
   exactly as strict, against the new parent).

## Design decisions & interpretations (recorded)

- `people.update_staff`: hierarchy validation triggers when an edit
  touches region/location/department and then chain-checks the full
  effective placement (stored + new). A unit-only edit keeps the
  legacy workforce-config check alone — the pre-existing front-door
  contract (a bare unit move succeeds) would otherwise break with
  no fixture-side fix short of weakening that test.
- `people.py` reaches the resolver through thin local wrappers
  (`_validate_placement`, `_placement_view`, `_resolve_scope`,
  `_concept_level`) so onboarding/accounts share one call shape.
- Team placement is DERIVED at read time from the team's unit (no
  new storage); a unit that does not resolve yields placement
  `None`, stated honestly rather than an all-None view.
- Governed analytics (`app.analytics`, OMOP warehouse datasets):
  NOT BUILT for placement — the warehouse tables carry no
  org-placement columns (their `unit_*` columns are measurement
  units); placement filtering there would need an ETL/warehouse
  change outside this pass. The placement filter surface is the
  workforce list + workforce analytics + command overview +
  credential-expiry report, as built.
- Command overview scoping covers the census + projection; ED,
  boarding and transport figures stay facility-wide (they are not
  unit-attributed anywhere in the system today).

## Build-process note (for the record)

This pass's first fan-out round reported completions whose
artefacts had not yet landed on disk when the coordinator checked;
the workstreams' background processes were in fact still running
and their files landed over the following hour, interleaved with
the coordinator's own WS1 build. The coordinator reconciled the
union (including removing a duplicated placement-column block the
overlap produced in `models.py`), discarded every unverified claim,
and re-ran the full gate personally. The totals above are the
coordinator's own runs on the final tree; nothing in this report
rests on a workstream's self-report.

## NOT RUN / named partials

- **Real-browser E2E** — standing VM limitation (the Air remains
  the browser gate); the web suite + build are the evidence here.
- Pre-existing deployments' report-definition republish for the
  credential-expiry placement parameters (operator action in
  Studio; fresh installs are seeded with them) — see the
  configuration-coverage caveat.
- Everything on the standing NOT RUN list (Docker/Kubernetes
  runtime, live LLM, Epic SMART live launch, physical devices,
  clearinghouse/Surescripts live legs, mobile compiled builds) is
  unchanged by this pass.

## Parent verification addendum (parent-run, 2026-10-19)

The coordinator's totals were reproduced by the parent's own runs
against the live stack (PostgreSQL 16.15 + pgvector, MariaDB
10.11.14, MinIO, Keycloak 26). Gate artefacts: `~/workspace/
orgverify/` (summary.txt, per-chunk logs, run scripts).

- **Backend: 1,285 passed / 0 failed / 0 skipped** (20 sequential
  chunks). The live Keycloak OIDC legs passed inside the main
  sequence with the stack credentials mapped. One in-chunk skip
  (the live MinIO drill in `tests/test_phase17_tiering_s3.py`)
  traced to an env-name mismatch — the test gates on
  `OPENCARE_TEST_S3_ENDPOINT`, the stack credentials name it
  `OPENCARE_S3_ENDPOINT` — and the file rerun with the mapping
  passed 7/7, drills included.
- **Web: 440 passed; build exit 0. ADK: 8 passed. Go: 5 packages
  ok. Cargo: 43 passed.**

Adjusted-tests register — parent addition:

4. `backend/tests/test_org_placement_rollups.py` — `OVERVIEW_KEYS`
   gains `"boarding"`. Reason: the parent's Command Center hotfix
   (2026-10-19, fixing the live white-page bug Min reported) added
   the `boarding` object to `command_overview` after this pass's
   WS3 pin was written, and the coordinator's gate chunk for this
   file ran against the pre-hotfix module. The exact-key-set
   assertion is unchanged in strictness; the set now names the
   complete contract, whose `boarding` contents are pinned in
   `test_phase12_continuum.py`.

Gate incidents: two VM restarts killed parent gate runs mid-flight;
chunks resumed from the recorded summary each time, and no chunk
result was carried forward without a completed run behind it.
