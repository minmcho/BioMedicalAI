-- Diabetes registry mart: one row per person with Type 2 diabetes
-- (OMOP concept 201826, SNOMED source 44054006), with their latest HbA1c
-- (LOINC 4548-4 / OMOP concept 3000963) and whether it is in target
-- (< 7.0%). Mirrors the /pophealth diabetes registry definition, which
-- is Studio cohort config (kind='cohort', key='diabetes').
with diabetic_persons as (
    select distinct person_id
    from {{ ref('stg_condition_occurrence') }}
    where condition_concept_id = 201826
       or condition_source_value = '44054006'
),

ranked_a1c as (
    select
        person_id,
        value_as_number,
        measurement_datetime,
        row_number() over (
            partition by person_id
            order by measurement_datetime desc nulls last, measurement_id desc
        ) as rn
    from {{ ref('stg_measurement') }}
    where measurement_concept_id = 3000963
       or measurement_source_value = '4548-4'
),

latest_a1c as (
    select person_id, value_as_number, measurement_datetime
    from ranked_a1c
    where rn = 1
)

select
    p.person_id,
    p.source_patient_id,
    p.gender_source_value,
    p.year_of_birth,
    a.value_as_number as latest_hba1c,
    a.measurement_datetime as latest_hba1c_at,
    case
        when a.value_as_number is null then null
        when a.value_as_number < 7.0 then true
        else false
    end as hba1c_in_target
from diabetic_persons d
inner join {{ ref('stg_person') }} p on p.person_id = d.person_id
left join latest_a1c a on a.person_id = p.person_id
