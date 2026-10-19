# Lakehouse MVP — FHIR → medallion → OMOP-shaped gold

Why this exists: across the verified target postings, production
Databricks/Snowflake is the one hard gap — named explicitly by Teladoc
JR21143 (Principal Data & AI Platform Engineer, DataOps, $200k–$230k
base, closes Nov 3). The amended prep plan freezes a linkable v0 on
**Sunday, Nov 1** so the Teladoc application (submit by **Monday,
Nov 2**) can link to it. This folder is that proof, built in the order
that makes the freeze cheap:

1. **`medallion.py`** — the transform logic in dependency-free Python:
   bronze (land as-is + ingestion metadata), silver (validate FHIR
   HbA1c Observations, LOINC 4548-4, drop entered-in-error/cancelled,
   normalise to an OMOP measurement shape, deduplicate), gold (one row
   per patient: latest HbA1c, band, counts, date range). Synthetic data
   only; no PHI, no clinical claims.
2. **`test_medallion.py`** — 5 tests, stdlib unittest (also runs under
   pytest): `python3 -m unittest test_medallion -v`
3. **`databricks_notebook.py`** — the same rules in Spark/Delta with one
   MLflow run, in Databricks notebook-source format, written for
   Databricks Free Edition (Delta + MLflow work there; Unity Catalog
   and the model registry are limited, so it uses plain Delta paths).

## Freeze checklist (Nov 1)

- [ ] Local tests green (done at scaffold time — re-run before freeze)
- [ ] Import `databricks_notebook.py` into a Free Edition workspace
- [ ] Run end-to-end on the synthetic sample; silver + gold Delta
      tables written
- [ ] One MLflow run logged (`lakehouse-mvp-v0`) — copy its URL here: _
- [ ] Link the run + this README in the Teladoc application

## Trade-offs, stated plainly

- Logic-first, platform-second: the rules are proven locally where
  iteration is free; Databricks is the execution target, not the
  development environment. The risk is drift between the two files —
  mitigated by keeping the notebook a thin port and the module the
  source of truth.
- HbA1c only, one resource type: deliberately the smallest slice that
  exercises ingest → validation → dedupe → aggregation, matching the
  FHIR→OMOP mapping notes in the parent folder. Widening comes after
  the freeze, not before it.
