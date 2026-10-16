# Warehouse + population health contracts (Phase 3)

Single source of truth for the analytics surface: the transactional
outbox, the OMOP warehouse, the ETL, the dbt project, and the
aggregate-only population-health API.

## Transactional outbox — `outbox_events` (clinical DB)

Written by ONE central hook (`backend/app/outbox.py`, a SQLAlchemy
`before_flush` listener) in the same transaction as every clinical
resource create/update — never by endpoint code.

| Column | Meaning |
|---|---|
| `id` | Monotonic drain order |
| `resource_type`, `resource_id` | FHIR resource identity |
| `version` | Resource `meta.versionId` at write time |
| `payload` | Full rendered-resource snapshot the ETL transforms |
| `status` | `pending` → `processed`, or `dead_letter` on transform failure |
| `error` | Failure text for dead letters (visible via `/warehouse/status`) |
| `attempts`, `created_at`, `processed_at` | Drain bookkeeping |

Watched types: Patient, Encounter, Condition, Observation,
MedicationRequest, Procedure, DiagnosticReport, ServiceRequest.

## Warehouse — OMOP CDM subset (second database)

`WAREHOUSE_DATABASE_URL` selects the warehouse (default
`sqlite:///./warehouse.db`; PostgreSQL and DuckDB URLs work through the
same SQLAlchemy models in `backend/app/warehouse.py`).

Tables: `person`, `visit_occurrence`, `condition_occurrence`,
`measurement`, `observation`, `drug_exposure`, `procedure_occurrence`,
plus `concept_map`. Every clinical row carries its FHIR source id
(`source_patient_id`, `source_encounter_id`, `source_condition_id`,
`source_observation_id`, `source_medication_request_id`,
`source_procedure_id`) — the ETL upserts by it, and every row keeps its
`*_source_value` (the original code) next to the mapped concept id.

Transforms (`backend/app/etl.py`): Patient→person (gender via concept
map; birth date → year/month/day + datetime), Encounter→visit_occurrence
(class → visit concept; type → visit source concept), Condition→
condition_occurrence, Observation→measurement when `valueQuantity` is
numeric else OMOP `observation`, MedicationRequest→drug_exposure,
Procedure→procedure_occurrence. DiagnosticReport and ServiceRequest have
no target table in this CDM subset: their events are marked processed as
documented no-ops (`no_target` in ETL stats) — present in the outbox,
never silently dropped.

### Concept mapping

`concept_map(source_system, source_code) → target_concept_id` is a
small, curated, documented subset covering the codes used by the
synthetic seed bundle and the Studio lab catalog, plus gender and
visit-class maps. Standard OMOP concepts are used where well established
(T2DM SNOMED 44054006→201826, hypertension 38341003→320128, asthma
195967001→317009, HbA1c LOINC 4548-4→3000963, glucose 2345-7→3016293,
creatinine 2160-0→3016723, Female→8532, Male→8507, ambulatory visit
AMB→9202). All other entries are LOCAL custom concepts in the
≥ 2,000,000,000 range — the OMOP convention for site-specific concepts —
and are named "(local)" in the map. A production deployment replaces this
table with the full ATHENA vocabulary load. Unmapped codes land with
concept_id **0** ("No matching concept") and are counted in the
mapping-coverage statistic — never dropped.

### ETL

- CLI: `python -m app.etl run` (drain) / `python -m app.etl status`
  (JSON tallies). HTTP: `POST /warehouse/etl/run` (admin only, audited).
- Drains pending events in id order; per-event commit; a failure
  dead-letters that event (error recorded) and the drain continues.
- Idempotent: upserts by source id; a re-run with no new events changes
  nothing; an updated resource updates its OMOP row in place.
- Surrogate keys use SQLAlchemy `Sequence` (portable across
  SQLite/PostgreSQL/DuckDB).

## dbt project — `warehouse/dbt/`

Staging views over the OMOP tables (`stg_person`, `stg_visit_occurrence`,
`stg_condition_occurrence`, `stg_measurement`, `stg_drug_exposure`,
`stg_procedure_occurrence`) + the `diabetes_registry` mart (one row per
person with T2DM: latest HbA1c + in-target flag at < 7.0%, mirroring the
Studio `cohort/diabetes` definition). Profiles: `profiles.yml.example`
(DuckDB local target, PostgreSQL prod target). Sources are declared in
`models/sources.yml` against the warehouse database's `main` schema.

## Population health API — aggregate only

All endpoints require role **admin** or **clinician** (patient/viewer →
403, unauthenticated → 401). No endpoint returns patient-level rows.

| Route | Behaviour |
|---|---|
| `GET /pophealth/registries` | Published Studio `cohort` config definitions |
| `GET /pophealth/registries/{key}/summary` | Member count; lab block (code, comparator, threshold, window, members with a current result, `pct_in_range` — the raw in-range numerator is deliberately not exposed); stratification by gender + age band with small-cell suppression |
| `POST /pophealth/explore` | Body `{conditionCode?, labCode?, comparator?, threshold?}` (at least one filter; lab requires a numeric threshold). Persons matching ALL filters; lab filter uses each person's LATEST measurement. Response is the aggregate count, itself small-cell suppressed |
| `GET /warehouse/status` | Outbox tallies, up to 20 dead letters with errors, warehouse table counts, mapping coverage (per table + overall) |
| `POST /warehouse/etl/run` | Admin only; drains the outbox; returns `{processed, no_target, dead_letter, by_type}`; audited |

**Small-cell suppression:** any stratum count below the cohort's
`smallCellThreshold` (default 5, part of the Studio cohort definition) is
returned as the string `"<5"` with `suppressed: true`. The raw number
never leaves the server. Age bands are 0-17, 18-44, 45-64, 65-74, 75+,
computed as of the latest activity date in the warehouse (data-relative,
so results are reproducible for a given dataset).

**Registry definitions are Studio configuration** (kind `cohort`,
draft → publish like every other kind):

```json
{
  "name": "Diabetes registry",
  "conditionCodes": [{"system": "http://snomed.info/sct", "code": "44054006"}],
  "lab": {"system": "http://loinc.org", "code": "4548-4",
          "comparator": "<", "threshold": 7.0, "windowDays": 365},
  "smallCellThreshold": 5
}
```

`windowDays` is evaluated against the latest measurement date of that
lab code in the warehouse (data-relative). Seeded published v1 registries:
`diabetes`, `hypertension`.
