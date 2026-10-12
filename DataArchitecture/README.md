# Data Architecture — FHIR -> OMOP + dbt (gap-closer project)

Why this exists: across 36 real FDE / Data Architect / AI-ML postings checked 2026-10-12, the three recurring gaps were dbt+Snowflake, FHIR->OMOP, and Databricks lakehouse. This one project addresses the first two honestly, on public/synthetic data only.

Plan:
1. Generate synthetic FHIR R4 resources (Synthea).
2. Map to OMOP CDM tables (person, visit_occurrence, condition_occurrence, measurement, drug_exposure) with documented vocabulary mappings (LOINC, SNOMED CT, RxNorm concepts).
3. Build it as a dbt project (models, tests, docs) running on DuckDB locally, Snowflake-compatible SQL.
4. Governance artifact: data-quality checks, lineage, access/audit notes — drawing on LIMS + Agentic_App validation/governance work.
5. README with architecture diagram and trade-offs (warehouse vs lakehouse, Databricks note).

No real PHI. No clinical claims.
