select
    condition_occurrence_id,
    person_id,
    condition_concept_id,
    condition_source_value,
    condition_start_date,
    condition_start_datetime,
    visit_occurrence_id,
    source_condition_id
from {{ source('omop', 'condition_occurrence') }}
