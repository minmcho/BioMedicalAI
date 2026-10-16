select
    procedure_occurrence_id,
    person_id,
    procedure_concept_id,
    procedure_source_value,
    procedure_date,
    procedure_datetime,
    source_procedure_id
from {{ source('omop', 'procedure_occurrence') }}
