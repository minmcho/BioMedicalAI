# FHIR R4 → OMOP CDM — Mapping Notes (Week 2 draft scaffold)

Status: DRAFT prepared for Min's Week 2 learning pass (Mon Oct 19 – Sun Oct 25,
Bootcamp/PREP_3_MONTHS.md). Reference mappings below are the standard
FHIR→OMOP correspondences; the "Decisions for Min" section at the end is
the part that is actually his — review, correct, and commit the reviewed
version. Synthetic/public data only (Synthea); no PHI.

Purpose: this is the design input for the Week 3 lakehouse MVP
(Synthea FHIR JSON → bronze → silver OMOP-shaped Delta tables) and the
Sun Nov 1 linkable v0 freeze that the Teladoc application links.

## Core resource → table map

| FHIR R4 resource | OMOP CDM table | Key fields | Notes |
|---|---|---|---|
| Patient | person | gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id | OMOP stores year_of_birth, not full birthDate (de-identification by design). Gender: FHIR `male/female` → OMOP Gender concepts 8507/8532. |
| Encounter | visit_occurrence | visit_concept_id, visit_start_datetime, visit_end_datetime | Classify from Encounter.class: inpatient 9201, outpatient 9202, emergency 9203. One Encounter = one visit; do not split. |
| Condition | condition_occurrence | condition_concept_id (SNOMED CT), condition_start_datetime | Map from Condition.code; Synthea codes conditions in SNOMED CT already. onsetDateTime → start; abatement → end. |
| Observation (laboratory) | measurement | measurement_concept_id (LOINC), value_as_number, unit_concept_id, measurement_datetime | Only lab Observations go here. Filter on Observation.category = laboratory. LOINC from Observation.code. |
| Observation (vital signs) | measurement (or observation) | measurement_concept_id (LOINC), value_as_number | Convention varies; vitals as measurement is the common choice — see Decisions. |
| Observation (social history, etc.) | observation | observation_concept_id, value_as_* | The residual category: anything observational that is not a measurement, condition, or procedure. |
| MedicationRequest | drug_exposure | drug_concept_id (RxNorm), drug_exposure_start_datetime, quantity / days_supply | Map from medicationCodeableConcept (RxNorm). authoredOn is NOT the start date — use the intended dispense/exposure window when present, else document the fallback. |
| Procedure | procedure_occurrence | procedure_concept_id (SNOMED CT), procedure_datetime | Synthea procedures are SNOMED-coded. |
| Immunization | drug_exposure | drug_concept_id (CVX → RxNorm-mapped vaccine concept) | OMOP has no vaccine table; vaccines live in drug_exposure. Easy to forget; Synthea exports Immunization resources. |

## Vocabulary rules (the part that bites)

- OMOP concept_ids are vocabulary-specific: a SNOMED code is not a valid
  measurement concept, a LOINC code is not a valid condition concept.
  Keep `*_source_value` (the original FHIR code) alongside every mapped
  concept_id so the mapping is auditable and reversible.
- Synthea is convenient precisely because it already emits SNOMED CT
  (conditions/procedures), LOINC (labs), and RxNorm (medications) —
  the three vocabularies OMOP wants. Real-world FHIR feeds often emit
  local codes instead; that gap is a mapping project of its own, and
  the lakehouse README should say so.
- Unmapped codes: land them with concept_id = 0 plus the source value,
  never drop the row silently. A v0 freeze metric worth publishing:
  % of rows per table with concept_id = 0.

## Datetime and timezone handling

- FHIR datetimes may carry offsets; OMOP datetime fields are naive.
  Rule for the MVP: convert to UTC, store naive, document the rule in
  the bronze→silver notebook — do not let each notebook improvise.
- Date-only FHIR values (no time) → midnight, flagged as date-only in
  bronze so silver consumers know the precision they actually have.

## Bronze → silver shape (feeds Week 3)

- Bronze: raw Synthea JSON, one row per resource, plus resourceType,
  resource id, and the raw code + system pairs extracted — nothing
  interpreted yet.
- Silver: one Delta table per OMOP table above, concept mapping applied,
  datetimes normalised per the rules above, concept_id = 0 rows kept
  and countable.
- Gold (Week 4): one summary table (e.g. patients per condition with
  small-cell suppression) + one MLflow run — the v0 freeze on Nov 1
  needs only silver + one gold summary + README, per the plan.

## Decisions for Min (fill in during the Week 2 pass)

1. Vital signs: measurement table (recommended above) or observation?
2. MedicationRequest start-date fallback when no exposure window is
   present: authoredOn, or exclude from drug_exposure?
3. Which Synthea module subset for the MVP patient cohort — full
   population export, or a small fixed cohort (e.g. 100 patients) so
   the Nov 1 freeze stays reviewable?
4. Race/ethnicity: Synthea emits US Core extensions — map to OMOP
   Race/Ethnicity concepts, or carry as source values only for v0?
