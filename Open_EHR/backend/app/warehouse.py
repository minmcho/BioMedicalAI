"""Analytics warehouse — OMOP CDM-shaped tables in a SECOND database
(Phase 3).

The warehouse is deliberately a separate database from the clinical store:
`WAREHOUSE_DATABASE_URL` (default `sqlite:///./warehouse.db`; PostgreSQL
or DuckDB URLs work through the same SQLAlchemy models). It holds an OMOP
CDM v5.4 *subset* — person, visit_occurrence, condition_occurrence,
measurement, observation, drug_exposure, procedure_occurrence — plus the
project's own `concept_map`.

Concept mapping honesty rules:
- Every OMOP row keeps its source code (`*_source_value`) and the
  source-code identity, so nothing is lost when a code is unmapped.
- `concept_map` is a small CURATED, DOCUMENTED subset (contracts/
  warehouse.md): the codes used by the synthetic seed bundle and the
  Studio lab catalog, plus gender/visit-class maps. Where the standard
  OMOP concept is well established it is used (e.g. Type 2 diabetes
  SNOMED 44054006 -> 201826; HbA1c LOINC 4548-4 -> 3000963; Female ->
  8532). Everything else is assigned a LOCAL custom concept id in the
  >= 2,000,000,000 range, which is the OMOP convention for site-specific
  concepts (a production deployment replaces this table with the full
  ATHENA vocabulary load).
- An unmapped code lands with concept_id 0 ("No matching concept", the
  OMOP convention) and is COUNTED in the mapping-coverage statistic
  (app.warehouse.mapping_coverage) — never silently dropped.
"""
import os
from datetime import date, datetime, time, timezone

from sqlalchemy import (Date, DateTime, Float, ForeignKey, Integer, Sequence,
                        String, UniqueConstraint, create_engine)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class WarehouseBase(DeclarativeBase):
    pass


def _pk(sequence: str):
    """Surrogate PK that autoincrements portably: SQLAlchemy Sequence —
    native on PostgreSQL/DuckDB, rowid-backed no-op on SQLite. (Plain
    Integer autoincrement renders SERIAL, which DuckDB rejects, and
    IDENTITY columns are not implemented by the DuckDB dialect.)"""
    return mapped_column(Integer, Sequence(sequence), primary_key=True)


# ---------------------------------------------------------------------------
# OMOP CDM subset
# ---------------------------------------------------------------------------

class Person(WarehouseBase):
    __tablename__ = "person"
    person_id: Mapped[int] = _pk("person_id_seq")
    gender_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gender_source_value: Mapped[str | None] = mapped_column(String(50), nullable=True)
    gender_source_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    year_of_birth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    month_of_birth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    day_of_birth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birth_datetime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    race_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ethnicity_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    person_source_value: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    # The FHIR Patient id this row came from (upsert key).
    source_patient_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)


class VisitOccurrence(WarehouseBase):
    __tablename__ = "visit_occurrence"
    visit_occurrence_id: Mapped[int] = _pk("visit_occurrence_id_seq")
    person_id: Mapped[int] = mapped_column(ForeignKey("person.person_id"), nullable=False, index=True)
    visit_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    visit_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    visit_start_datetime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    visit_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    visit_end_datetime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    visit_type_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    visit_source_value: Mapped[str | None] = mapped_column(String(120), nullable=True)
    visit_source_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_encounter_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)


class ConditionOccurrence(WarehouseBase):
    __tablename__ = "condition_occurrence"
    condition_occurrence_id: Mapped[int] = _pk("condition_occurrence_id_seq")
    person_id: Mapped[int] = mapped_column(ForeignKey("person.person_id"), nullable=False, index=True)
    condition_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    condition_source_value: Mapped[str | None] = mapped_column(String(120), nullable=True)
    condition_source_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    condition_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    condition_start_datetime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    condition_type_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    visit_occurrence_id: Mapped[int | None] = mapped_column(
        ForeignKey("visit_occurrence.visit_occurrence_id"), nullable=True)
    source_condition_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)


class Measurement(WarehouseBase):
    __tablename__ = "measurement"
    measurement_id: Mapped[int] = _pk("measurement_id_seq")
    person_id: Mapped[int] = mapped_column(ForeignKey("person.person_id"), nullable=False, index=True)
    measurement_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    measurement_source_value: Mapped[str | None] = mapped_column(String(120), nullable=True)
    measurement_source_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    measurement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    measurement_datetime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    measurement_type_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    value_as_number: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unit_source_value: Mapped[str | None] = mapped_column(String(60), nullable=True)
    visit_occurrence_id: Mapped[int | None] = mapped_column(
        ForeignKey("visit_occurrence.visit_occurrence_id"), nullable=True)
    source_observation_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)


class ObservationOmop(WarehouseBase):
    """OMOP `observation` — non-numeric FHIR Observations land here."""
    __tablename__ = "observation"
    observation_id: Mapped[int] = _pk("observation_id_seq")
    person_id: Mapped[int] = mapped_column(ForeignKey("person.person_id"), nullable=False, index=True)
    observation_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observation_source_value: Mapped[str | None] = mapped_column(String(120), nullable=True)
    observation_source_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    observation_datetime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    observation_type_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    value_as_string: Mapped[str | None] = mapped_column(String(500), nullable=True)
    value_as_number: Mapped[float | None] = mapped_column(Float, nullable=True)
    visit_occurrence_id: Mapped[int | None] = mapped_column(
        ForeignKey("visit_occurrence.visit_occurrence_id"), nullable=True)
    source_observation_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)


class DrugExposure(WarehouseBase):
    __tablename__ = "drug_exposure"
    drug_exposure_id: Mapped[int] = _pk("drug_exposure_id_seq")
    person_id: Mapped[int] = mapped_column(ForeignKey("person.person_id"), nullable=False, index=True)
    drug_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    drug_source_value: Mapped[str | None] = mapped_column(String(120), nullable=True)
    drug_source_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    drug_exposure_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    drug_exposure_start_datetime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    drug_exposure_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    drug_type_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    visit_occurrence_id: Mapped[int | None] = mapped_column(
        ForeignKey("visit_occurrence.visit_occurrence_id"), nullable=True)
    source_medication_request_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)


class ProcedureOccurrence(WarehouseBase):
    __tablename__ = "procedure_occurrence"
    procedure_occurrence_id: Mapped[int] = _pk("procedure_occurrence_id_seq")
    person_id: Mapped[int] = mapped_column(ForeignKey("person.person_id"), nullable=False, index=True)
    procedure_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    procedure_source_value: Mapped[str | None] = mapped_column(String(120), nullable=True)
    procedure_source_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    procedure_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    procedure_datetime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    procedure_type_concept_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    visit_occurrence_id: Mapped[int | None] = mapped_column(
        ForeignKey("visit_occurrence.visit_occurrence_id"), nullable=True)
    source_procedure_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)


class ConceptMap(WarehouseBase):
    """Source code/system -> OMOP concept id (curated subset, see docstring)."""
    __tablename__ = "concept_map"
    __table_args__ = (UniqueConstraint("source_system", "source_code"),)
    id: Mapped[int] = _pk("concept_map_id_seq")
    source_system: Mapped[str] = mapped_column(String(200), nullable=False)
    source_code: Mapped[str] = mapped_column(String(120), nullable=False)
    source_display: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    vocabulary_id: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    target_concept_id: Mapped[int] = mapped_column(Integer, nullable=False)
    target_concept_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")


# ---------------------------------------------------------------------------
# Concept map seed — DOCUMENTED subset (contracts/warehouse.md)
#
# Standard OMOP concepts (well-established ATHENA ids) are used where
# known; every other entry is a LOCAL custom concept in the >= 2e9 range,
# the OMOP convention for site-specific concepts.
# ---------------------------------------------------------------------------

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
CPT = "http://www.ama-assn.org/go/cpt"
ACTCODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
GENDER_SYSTEM = "urn:opencare:fhir-administrative-gender"

# (system, code, display, vocabulary, target_concept_id, target_name)
CONCEPT_MAP_SEED: list[tuple[str, str, str, str, int, str]] = [
    # Gender (standard OMOP Gender concepts)
    (GENDER_SYSTEM, "female", "Female", "Gender", 8532, "FEMALE"),
    (GENDER_SYSTEM, "male", "Male", "Gender", 8507, "MALE"),
    (GENDER_SYSTEM, "other", "Other", "Gender", 0, "No matching concept"),
    (GENDER_SYSTEM, "unknown", "Unknown", "Gender", 0, "No matching concept"),
    # Visit class (standard OMOP Visit concept)
    (ACTCODE, "AMB", "Ambulatory", "Visit", 9202, "Outpatient Visit"),
    (ACTCODE, "EMER", "Emergency", "Visit", 9203, "Emergency Room Visit"),
    (ACTCODE, "IMP", "Inpatient", "Visit", 9201, "Inpatient Visit"),
    # Conditions (SNOMED) — standard concepts where established
    (SNOMED, "44054006", "Type 2 diabetes mellitus", "SNOMED", 201826, "Type 2 diabetes mellitus"),
    (SNOMED, "38341003", "Hypertensive disorder", "SNOMED", 320128, "Hypertensive disorder"),
    (SNOMED, "195967001", "Asthma", "SNOMED", 317009, "Asthma"),
    (SNOMED, "55822004", "Hyperlipidemia", "SNOMED", 2000000001, "Hyperlipidemia (local)"),
    (SNOMED, "239873007", "Knee osteoarthritis", "SNOMED", 2000000002, "Osteoarthritis of knee (local)"),
    (SNOMED, "48694002", "Anxiety disorder", "SNOMED", 2000000003, "Anxiety disorder (local)"),
    (SNOMED, "410429000", "Cardiac arrhythmia", "SNOMED", 2000000004, "Cardiac arrhythmia (local)"),
    # Encounter types (SNOMED) — carried as visit_source_concept_id
    (SNOMED, "185349003", "Encounter for check up", "SNOMED", 2000000005, "Encounter for check up (local)"),
    (SNOMED, "390906007", "Follow-up encounter", "SNOMED", 2000000006, "Follow-up encounter (local)"),
    # Labs (LOINC) — standard concepts where established
    (LOINC, "4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "LOINC", 3000963,
     "Hemoglobin A1c/Hemoglobin.total in Blood"),
    (LOINC, "2345-7", "Glucose [Mass/volume] in Serum or Plasma", "LOINC", 3016293,
     "Glucose [Mass/volume] in Serum or Plasma"),
    (LOINC, "2160-0", "Creatinine [Mass/volume] in Serum or Plasma", "LOINC", 3016723,
     "Creatinine [Mass/volume] in Serum or Plasma"),
    (LOINC, "13457-7", "LDL cholesterol [Mass/volume] in Serum or Plasma (calculated)",
     "LOINC", 2000000007, "LDL cholesterol (local)"),
    (LOINC, "14682-9", "Creatinine [Moles/volume] in Serum or Plasma", "LOINC", 2000000008,
     "Creatinine [Moles/volume] (local)"),
    # Lab catalog analytes (Studio lab catalog, Phase 2)
    (LOINC, "718-7", "Hemoglobin [Mass/volume] in Blood", "LOINC", 2000000021, "Hemoglobin (local)"),
    (LOINC, "4544-3", "Hematocrit [Volume Fraction] of Blood by Automated count", "LOINC",
     2000000022, "Hematocrit (local)"),
    (LOINC, "6690-2", "Leukocytes [#/volume] in Blood by Automated count", "LOINC",
     2000000023, "Leukocytes (local)"),
    (LOINC, "777-3", "Platelets [#/volume] in Blood by Automated count", "LOINC",
     2000000024, "Platelets (local)"),
    (LOINC, "3094-0", "Urea nitrogen [Mass/volume] in Serum or Plasma", "LOINC",
     2000000025, "Urea nitrogen (local)"),
    (LOINC, "2951-2", "Sodium [Moles/volume] in Serum or Plasma", "LOINC",
     2000000026, "Sodium (local)"),
    (LOINC, "2823-3", "Potassium [Moles/volume] in Serum or Plasma", "LOINC",
     2000000027, "Potassium (local)"),
    # Drugs (RxNorm) — local custom concepts (ATHENA load replaces in production)
    (RXNORM, "1049221", "Lisinopril 10 MG Oral Tablet", "RxNorm", 2000000010, "Lisinopril 10 MG Oral Tablet (local)"),
    (RXNORM, "847232", "Metformin hydrochloride 500 MG Oral Tablet", "RxNorm", 2000000011,
     "Metformin hydrochloride 500 MG Oral Tablet (local)"),
    (RXNORM, "245314", "Albuterol 0.09 MG/ACTUAT Metered Dose Inhaler", "RxNorm", 2000000012,
     "Albuterol 0.09 MG/ACTUAT Metered Dose Inhaler (local)"),
    (RXNORM, "1044910", "Atorvastatin 20 MG Oral Tablet", "RxNorm", 2000000013,
     "Atorvastatin 20 MG Oral Tablet (local)"),
    (RXNORM, "1048467", "Sertraline 50 MG Oral Tablet", "RxNorm", 2000000014,
     "Sertraline 50 MG Oral Tablet (local)"),
    (RXNORM, "1351886", "Apixaban 5 MG Oral Tablet", "RxNorm", 2000000015, "Apixaban 5 MG Oral Tablet (local)"),
    # Procedures (CPT) — local custom concepts
    (CPT, "71046", "Chest X-ray, 2 views", "CPT", 2000000016, "Chest X-ray, 2 views (local)"),
    (CPT, "94640", "Inhalation therapy administration", "CPT", 2000000017, "Inhalation therapy administration (local)"),
    (CPT, "20610", "Arthrocentesis, knee", "CPT", 2000000018, "Arthrocentesis, knee (local)"),
    (CPT, "90791", "Psychiatric diagnostic evaluation", "CPT", 2000000019, "Psychiatric diagnostic evaluation (local)"),
    (CPT, "93005", "Electrocardiogram", "CPT", 2000000020, "Electrocardiogram (local)"),
]


# ---------------------------------------------------------------------------
# Engine / session management (keyed by URL so tests can point the
# warehouse at a throwaway file through the env var)
# ---------------------------------------------------------------------------

def default_warehouse_url() -> str:
    return os.environ.get("WAREHOUSE_DATABASE_URL", "sqlite:///./warehouse.db")


_ENGINES: dict[str, object] = {}


def warehouse_engine(database_url: str | None = None):
    url = database_url or default_warehouse_url()
    engine = _ENGINES.get(url)
    if engine is None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        engine = create_engine(url, connect_args=connect_args, future=True)
        WarehouseBase.metadata.create_all(engine)
        _ENGINES[url] = engine
    return engine


def warehouse_session_factory(database_url: str | None = None):
    engine = warehouse_engine(database_url)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def ensure_concept_map(session) -> int:
    """Seed the concept map idempotently. Returns rows inserted."""
    from sqlalchemy import select

    inserted = 0
    for system, code, display, vocab, target_id, target_name in CONCEPT_MAP_SEED:
        exists = session.scalars(
            select(ConceptMap).where(ConceptMap.source_system == system,
                                     ConceptMap.source_code == code)).first()
        if exists is None:
            session.add(ConceptMap(source_system=system, source_code=code,
                                   source_display=display, vocabulary_id=vocab,
                                   target_concept_id=target_id,
                                   target_concept_name=target_name))
            inserted += 1
    session.commit()
    return inserted


def lookup_concept(session, system: str | None, code: str | None) -> int:
    """Target OMOP concept id for a source code, 0 when unmapped."""
    if not system or not code:
        return 0
    from sqlalchemy import select

    row = session.scalars(
        select(ConceptMap).where(ConceptMap.source_system == system,
                                 ConceptMap.source_code == code)).first()
    return row.target_concept_id if row is not None else 0


# ---------------------------------------------------------------------------
# Small FHIR datetime/date helpers (naive UTC, matching app.fhir)
# ---------------------------------------------------------------------------

def parse_fhir_datetime(raw) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            return None
        return datetime.combine(d, time.min)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def parse_fhir_date(raw) -> date | None:
    dt = parse_fhir_datetime(raw)
    return dt.date() if dt is not None else None


# ---------------------------------------------------------------------------
# Warehouse statistics (status endpoint + coverage accounting)
# ---------------------------------------------------------------------------

# (table name, model, concept column) for mapping-coverage accounting.
_COVERAGE_TABLES = (
    ("person", Person, "gender_concept_id"),
    ("visit_occurrence", VisitOccurrence, "visit_concept_id"),
    ("condition_occurrence", ConditionOccurrence, "condition_concept_id"),
    ("measurement", Measurement, "measurement_concept_id"),
    ("observation", ObservationOmop, "observation_concept_id"),
    ("drug_exposure", DrugExposure, "drug_concept_id"),
    ("procedure_occurrence", ProcedureOccurrence, "procedure_concept_id"),
)


def table_counts(session) -> dict:
    from sqlalchemy import func, select

    return {name: session.scalar(select(func.count()).select_from(model)) or 0
            for name, model, _ in _COVERAGE_TABLES}


def mapping_coverage(session) -> dict:
    """Per-table + overall share of rows whose concept id is mapped (!= 0).
    Unmapped rows are counted, never hidden."""
    from sqlalchemy import func, select

    per_table: dict[str, dict] = {}
    total = mapped_total = 0
    for name, model, column_name in _COVERAGE_TABLES:
        column = getattr(model, column_name)
        rows = session.scalar(select(func.count()).select_from(model)) or 0
        mapped = session.scalar(
            select(func.count()).select_from(model).where(column != 0)) or 0
        per_table[name] = {
            "rows": rows, "mapped": mapped, "unmapped": rows - mapped,
            "coverage_pct": round(100.0 * mapped / rows, 1) if rows else None,
        }
        total += rows
        mapped_total += mapped
    return {
        "tables": per_table,
        "total_rows": total,
        "mapped_rows": mapped_total,
        "unmapped_rows": total - mapped_total,
        "coverage_pct": round(100.0 * mapped_total / total, 1) if total else None,
    }
