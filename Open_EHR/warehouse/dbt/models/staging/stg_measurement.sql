select
    measurement_id,
    person_id,
    measurement_concept_id,
    measurement_source_value,
    measurement_date,
    measurement_datetime,
    value_as_number,
    unit_source_value,
    visit_occurrence_id,
    source_observation_id
from {{ source('omop', 'measurement') }}
