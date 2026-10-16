select
    visit_occurrence_id,
    person_id,
    visit_concept_id,
    visit_source_value,
    visit_start_date,
    visit_start_datetime,
    visit_end_date,
    visit_end_datetime,
    source_encounter_id
from {{ source('omop', 'visit_occurrence') }}
