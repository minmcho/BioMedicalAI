select
    drug_exposure_id,
    person_id,
    drug_concept_id,
    drug_source_value,
    drug_exposure_start_date,
    drug_exposure_start_datetime,
    source_medication_request_id
from {{ source('omop', 'drug_exposure') }}
