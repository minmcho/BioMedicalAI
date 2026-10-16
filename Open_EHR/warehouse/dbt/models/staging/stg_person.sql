select
    person_id,
    gender_concept_id,
    gender_source_value,
    year_of_birth,
    month_of_birth,
    day_of_birth,
    person_source_value,
    source_patient_id
from {{ source('omop', 'person') }}
