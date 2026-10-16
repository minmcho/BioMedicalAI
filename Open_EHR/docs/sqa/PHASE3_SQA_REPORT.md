# Phase 3 SQA report — FHIR→OMOP warehouse + population health

Date: 2026-10-15. Gate: `docs/SQA.md`. Data: synthetic only (seed bundle +
obviously synthetic additions created through the real API in tests).

## What shipped

- **Transactional outbox** (`backend/app/outbox.py` + `OutboxEvent` in
  `models.py`): one central SQLAlchemy `before_flush` hook writes the
  event (type, id, version, full rendered snapshot) in the SAME
  transaction as every create/update of Patient, Encounter, Condition,
  Observation, MedicationRequest, Procedure, DiagnosticReport,
  ServiceRequest. Covers the generic FHIR path, the ADT transition, and
  the lab module's direct row mutations — no endpoint writes outbox rows.
  Verified empirically before adoption (insert → event; update → event
  with new snapshot; non-watched type → none; untouched commit → none).
- **Warehouse** (`backend/app/warehouse.py`): second database via
  `WAREHOUSE_DATABASE_URL` (default `sqlite:///./warehouse.db`). OMOP CDM
  subset: person, visit_occurrence, condition_occurrence, measurement,
  observation, drug_exposure, procedure_occurrence + `concept_map`.
  Surrogate keys use SQLAlchemy `Sequence` (portable: native on
  PostgreSQL/DuckDB, rowid-backed on SQLite — plain autoincrement renders
  SERIAL, which DuckDB rejects; this was found by an actual failed run
  against DuckDB and fixed).
- **Concept map**: curated documented subset (contracts/warehouse.md).
  Standard OMOP concepts where established (T2DM 201826, HTN 320128,
  asthma 317009, HbA1c 3000963, glucose 3016293, creatinine 3016723,
  Female 8532, Male 8507, AMB visit 9202); local custom concepts
  ≥ 2,000,000,000 (OMOP convention) for the rest, named "(local)".
  Unmapped codes land with concept_id 0 and are counted in
  `mapping_coverage` — tested, not just claimed.
- **ETL** (`backend/app/etl.py`): ordered drain, per-event commit,
  upsert by FHIR source id (idempotent; updates propagate), dead-letter
  with recorded error + drain continues, person resolution on demand
  from the clinical store. DiagnosticReport/ServiceRequest events are
  processed as documented no-ops (`no_target`). CLI `python -m app.etl
  run|status` + admin-only `POST /warehouse/etl/run` (audited).
- **Population health** (`backend/app/pophealth.py`): registries are
  Studio config (new kind `cohort` in `configstore.py` with its own
  validator; seeded published v1: `diabetes`, `hypertension`).
  `GET /pophealth/registries`, `GET /pophealth/registries/{key}/summary`
  (member count, lab block with `pct_in_range` — raw numerator withheld,
  gender + age-band stratification), `POST /pophealth/explore`
  (condition and/or latest-lab filter, aggregate count only). All
  admin/clinician only. **Small-cell suppression**: strata (and explorer
  counts) below `smallCellThreshold` (default 5) return `"<5"` +
  `suppressed: true`; the raw number never leaves the server.
  `GET /warehouse/status`: outbox tallies, up to 20 dead letters with
  errors, table counts, mapping coverage.
- **Web**: Population health page (registry cards, cohort explorer form,
  warehouse/ETL status panel with dead letters + per-table coverage,
  admin-only "Run ETL now"), client methods + types, nav gated to
  admin/clinician mirroring the server.

## Gate results (exact)

1. **Unit + API tests**
   - Backend: `python -m pytest -q` → **102 passed** (85 pre-existing +
     17 new: `tests/test_phase3_warehouse.py` 6, `tests/test_phase3_pophealth.py` 11).
   - Web: `npm test` → **44 passed** (37 pre-existing + 7 new:
     PopHealthPage 3, pophealth client 3, session nav 1 net new);
     `npm run build` → **exit 0**.
2. **FHIR validation** — no new FHIR write surface; the outbox hook is
   downstream of the existing validated write path (negative validation
   behaviour unchanged, covered by pre-existing suites).
3. **RBAC negative tests** — `/pophealth/*` + `/warehouse/status`:
   unauthenticated 401; patient 403; viewer 403 (tested).
   `/warehouse/etl/run`: clinician 403, patient 403, admin 200 (tested).
4. **Audit assertions** — the ETL trigger writes an AuditLog row
   (`warehouse.etl.run`); all resource writes still audit via the
   existing path (pre-existing chain-verification tests green).
5. **Completeness / no-noop** — every route traces to a real effect with
   a test observing it: outbox row in DB after API write; OMOP row
   counts after drain; summary numbers hand-computed from the seed
   bundle (persons 6, visits 12, conditions 10, measurements 32, drug
   exposures 9, procedures 5; diabetes members {pat-1, pat-4}, latest
   HbA1c 6.9 / 7.6, in-range 50.0%); suppression proven both directions
   (suppressed at 3, revealed at 6 after adding synthetic patients).
6. **Security** — aggregate-only API (a test asserts no patient
   identifiers appear in a summary payload); suppression applied to
   explorer counts as well as strata; no secrets in code/fixtures;
   synthetic data only. New dependencies: `dbt-core`, `dbt-duckdb`,
   `duckdb-engine` (added to `backend/requirements.txt`; licences:
   Apache-2.0 for dbt-core/dbt-duckdb/duckdb-engine, MIT for DuckDB).
7. **Data integrity** — outbox + resource commit atomically (same
   transaction by construction of the flush hook); ETL per-event
   commits; dead-letter state is durable and inspectable.
8. **Docs** — `contracts/warehouse.md` (new) + `contracts/README.md`,
   root `README.md` (Phase 3 section + layout), `docs/PHASES.md`
   (Phase 3 → DONE), this report.

## Live verification beyond unit tests (actually run)

- CLI end-to-end on real files: clinical SQLite DB seeded via the real
  API/TestClient (86 created), then `python -m app.etl run` with
  `WAREHOUSE_DATABASE_URL=duckdb:////tmp/p3demo/warehouse.duckdb` →
  `{"processed": 80, "no_target": 6, "dead_letter": 0}` with the
  expected per-type breakdown; immediate re-run → `processed: 0`
  (idempotent). This is the run that caught the SERIAL/DuckDB defect
  fixed above.

## NOT RUN / not claimed (honest list)

- **dbt build: NOT VERIFIED.** dbt-core + dbt-duckdb installed cleanly
  into the venv (duckdb 1.5.6). One `dbt build` attempt was made against
  the DuckDB warehouse produced above and FAILED:
  `Catalog Error: Table with name "omop.procedure_occurrence" does not
  exist because schema "omop" does not exist` (dbt resolved the source
  name `omop` as the schema). The diagnosed fix — declaring
  `schema: main` in `warehouse/dbt/models/sources.yml` — has been
  applied to the project, but a re-run of `dbt build` was declined at
  runtime in this session and has NOT been performed. The dbt project
  (staging views + `diabetes_registry` mart + uniqueness/not-null tests
  in `marts/schema.yml`) is therefore delivered as authored source,
  NOT as an executed build. Re-running `dbt build` after the sources
  fix is the single outstanding verification for this phase.
- **PostgreSQL warehouse runtime NOT RUN** — no Docker/Postgres on this
  VM (same standing caveat as Phases 0–2); portability rests on the
  SQLAlchemy layer + the verified SQLite and DuckDB targets.
- **Browser click-through E2E NOT RUN** — web covered by component tests
  against a stub client at the network boundary + the client unit
  tests, per the established pattern.
- **Risk lists** (named in the original Phase 3 scope): delivered as
  registries + cohort explorer; no separate risk-scoring list was built.
- **ATHENA vocabulary**: concept map is the documented curated subset,
  not a full vocabulary load.

## Addendum — 2026-10-15, parent verification (dbt item CLOSED)
The dbt build item left unverified above was closed by the parent agent on a fresh end-to-end run:
seed (86 created) → `python -m app.etl run` into DuckDB ({"processed": 80, "no_target": 6, "dead_letter": 0}) → `dbt build` (dbt-core 1.12.6, dbt-duckdb, profile schema main, sources.yml `schema: main` fix in place): **Done. PASS=11 WARN=0 ERROR=0** — 6 staging view models, the `diabetes_registry` table model, and 4 data tests (unique/not_null on person + registry person_id) all pass. dbt status: VERIFIED on DuckDB. Postgres-target dbt run remains NOT RUN (no Docker/Postgres on this VM).
