"""FHIR -> OMOP ETL worker (Phase 3).

Drains the transactional outbox (app.outbox) in id order and upserts each
clinical resource snapshot into the OMOP warehouse (app.warehouse):

    Patient           -> person
    Encounter         -> visit_occurrence
    Condition         -> condition_occurrence
    Observation       -> measurement (numeric valueQuantity)
                         or observation (string/coded values)
    MedicationRequest -> drug_exposure
    Procedure         -> procedure_occurrence
    DiagnosticReport, ServiceRequest -> no OMOP target table in this CDM
                         subset: the event is marked processed as a
                         documented no-op ("no_target"), never dropped.

Properties:
- IDEMPOTENT: re-running with no new events changes nothing. Every OMOP
  row carries its FHIR source id and transforms UPSERT by that id, so
  reprocessing an updated resource updates its OMOP row in place.
- FAULT-TOLERANT: a transform failure on one event records the error on
  that event (status='dead_letter', attempts incremented) and the drain
  CONTINUES with the next event. Dead letters are visible via
  GET /warehouse/status.
- Person resolution: child resources resolve their OMOP person through
  the warehouse person row for the FHIR patient id; if that row does not
  exist yet (event order is not guaranteed across resource types), the
  Patient resource is read from the clinical DB and transformed on
  demand. A resource naming a patient that does not exist at all is a
  transform failure (dead letter), not a silent skip.

CLI:
    python -m app.etl run     # drain pending events (DATABASE_URL /
                              # WAREHOUSE_DATABASE_URL env select the DBs)
    python -m app.etl status  # outbox + warehouse tallies as JSON
"""
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import warehouse as wh
from .models import FhirResource, OutboxEvent

NO_TARGET_TYPES = ("DiagnosticReport", "ServiceRequest")


# ---------------------------------------------------------------------------
# Small FHIR accessors (payloads are stored resource snapshots)
# ---------------------------------------------------------------------------

def _first_coding(codeable) -> tuple[str | None, str | None, str | None]:
    if not isinstance(codeable, dict):
        return None, None, None
    codings = codeable.get("coding") or []
    for c in codings:
        if isinstance(c, dict) and c.get("code"):
            return c.get("system"), c.get("code"), c.get("display")
    return None, None, codeable.get("text")


def _ref_id(reference, prefix: str) -> str | None:
    if isinstance(reference, dict):
        reference = reference.get("reference")
    if isinstance(reference, str) and reference.startswith(prefix):
        return reference[len(prefix):]
    return None


def _patient_id_of(body: dict) -> str:
    pid = _ref_id(body.get("subject"), "Patient/")
    if pid is None and body.get("resourceType") == "Appointment":
        for p in body.get("participant") or []:
            pid = _ref_id((p or {}).get("actor"), "Patient/")
            if pid:
                break
    if pid is None:
        raise ValueError(f"{body.get('resourceType')} has no Patient subject reference")
    return pid


def _visit_id_for(wh_session: Session, body: dict) -> int | None:
    enc_id = _ref_id(body.get("encounter"), "Encounter/")
    if enc_id is None:
        return None
    row = wh_session.scalars(
        select(wh.VisitOccurrence).where(
            wh.VisitOccurrence.source_encounter_id == enc_id)).first()
    return row.visit_occurrence_id if row is not None else None


# ---------------------------------------------------------------------------
# Transforms (each upserts by FHIR source id)
# ---------------------------------------------------------------------------

def _upsert(wh_session: Session, model, source_column, source_id: str):
    row = wh_session.scalars(
        select(model).where(source_column == source_id)).first()
    if row is None:
        row = model()
        setattr(row, source_column.name, source_id)
        wh_session.add(row)
    return row


def _ensure_person(clin: Session, wh_session: Session, patient_id: str) -> wh.Person:
    row = wh_session.scalars(
        select(wh.Person).where(wh.Person.source_patient_id == patient_id)).first()
    if row is not None:
        return row
    resource = clin.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Patient",
                                   FhirResource.resource_id == patient_id)).first()
    if resource is None:
        raise ValueError(f"Patient/{patient_id} not found in clinical store")
    body = dict(resource.body or {})
    body["id"] = patient_id
    return _transform_patient(wh_session, body)


def _transform_patient(wh_session: Session, body: dict) -> wh.Person:
    row = _upsert(wh_session, wh.Person, wh.Person.source_patient_id, body["id"])
    gender = body.get("gender")
    row.gender_concept_id = wh.lookup_concept(wh_session, wh.GENDER_SYSTEM, gender)
    row.gender_source_value = gender
    row.gender_source_concept_id = row.gender_concept_id
    birth = wh.parse_fhir_datetime(body.get("birthDate"))
    row.year_of_birth = birth.year if birth else None
    row.month_of_birth = birth.month if birth else None
    row.day_of_birth = birth.day if birth else None
    row.birth_datetime = birth
    row.person_source_value = f"Patient/{body['id']}"
    wh_session.flush()
    return row


def _transform_encounter(clin: Session, wh_session: Session, body: dict) -> None:
    person = _ensure_person(clin, wh_session, _patient_id_of(body))
    row = _upsert(wh_session, wh.VisitOccurrence,
                  wh.VisitOccurrence.source_encounter_id, body["id"])
    row.person_id = person.person_id
    class_system, class_code, _ = _first_coding(
        {"coding": [body["class"]]} if isinstance(body.get("class"), dict) else {})
    row.visit_concept_id = wh.lookup_concept(wh_session, class_system, class_code)
    row.visit_source_value = class_code
    type_codings = (body.get("type") or [{}])
    t_system, t_code, _ = _first_coding(type_codings[0] if type_codings else {})
    row.visit_source_concept_id = wh.lookup_concept(wh_session, t_system, t_code)
    period = body.get("period") or {}
    start = wh.parse_fhir_datetime(period.get("start"))
    end = wh.parse_fhir_datetime(period.get("end"))
    row.visit_start_datetime = start
    row.visit_start_date = start.date() if start else None
    row.visit_end_datetime = end
    row.visit_end_date = end.date() if end else None


def _transform_condition(clin: Session, wh_session: Session, body: dict) -> None:
    person = _ensure_person(clin, wh_session, _patient_id_of(body))
    row = _upsert(wh_session, wh.ConditionOccurrence,
                  wh.ConditionOccurrence.source_condition_id, body["id"])
    row.person_id = person.person_id
    system, code, _ = _first_coding(body.get("code"))
    row.condition_concept_id = wh.lookup_concept(wh_session, system, code)
    row.condition_source_value = code
    row.condition_source_concept_id = row.condition_concept_id
    onset = wh.parse_fhir_datetime(body.get("onsetDateTime")) \
        or wh.parse_fhir_datetime((body.get("onsetPeriod") or {}).get("start")) \
        or wh.parse_fhir_datetime(body.get("recordedDate"))
    row.condition_start_datetime = onset
    row.condition_start_date = onset.date() if onset else None
    row.visit_occurrence_id = _visit_id_for(wh_session, body)


def _transform_observation(clin: Session, wh_session: Session, body: dict) -> None:
    person = _ensure_person(clin, wh_session, _patient_id_of(body))
    system, code, _ = _first_coding(body.get("code"))
    concept_id = wh.lookup_concept(wh_session, system, code)
    effective = wh.parse_fhir_datetime(body.get("effectiveDateTime")) \
        or wh.parse_fhir_datetime((body.get("effectivePeriod") or {}).get("start"))
    value_q = body.get("valueQuantity")
    numeric = (isinstance(value_q, dict)
               and isinstance(value_q.get("value"), (int, float))
               and not isinstance(value_q.get("value"), bool))
    if numeric:
        row = _upsert(wh_session, wh.Measurement,
                      wh.Measurement.source_observation_id, body["id"])
        row.person_id = person.person_id
        row.measurement_concept_id = concept_id
        row.measurement_source_value = code
        row.measurement_source_concept_id = concept_id
        row.measurement_datetime = effective
        row.measurement_date = effective.date() if effective else None
        row.value_as_number = float(value_q["value"])
        row.unit_source_value = value_q.get("unit")
        row.visit_occurrence_id = _visit_id_for(wh_session, body)
        # A reprocessed observation that became non-numeric must not leave
        # a stale row in the other table.
        stale = wh_session.scalars(
            select(wh.ObservationOmop).where(
                wh.ObservationOmop.source_observation_id == body["id"])).first()
        if stale is not None:
            wh_session.delete(stale)
    else:
        row = _upsert(wh_session, wh.ObservationOmop,
                      wh.ObservationOmop.source_observation_id, body["id"])
        row.person_id = person.person_id
        row.observation_concept_id = concept_id
        row.observation_source_value = code
        row.observation_source_concept_id = concept_id
        row.observation_datetime = effective
        row.observation_date = effective.date() if effective else None
        if isinstance(body.get("valueString"), str):
            row.value_as_string = body["valueString"]
        elif isinstance(body.get("valueCodeableConcept"), dict):
            _, _, display = _first_coding(body["valueCodeableConcept"])
            row.value_as_string = display
        row.visit_occurrence_id = _visit_id_for(wh_session, body)
        stale = wh_session.scalars(
            select(wh.Measurement).where(
                wh.Measurement.source_observation_id == body["id"])).first()
        if stale is not None:
            wh_session.delete(stale)


def _transform_medication_request(clin: Session, wh_session: Session, body: dict) -> None:
    person = _ensure_person(clin, wh_session, _patient_id_of(body))
    row = _upsert(wh_session, wh.DrugExposure,
                  wh.DrugExposure.source_medication_request_id, body["id"])
    row.person_id = person.person_id
    system, code, _ = _first_coding(body.get("medicationCodeableConcept"))
    row.drug_concept_id = wh.lookup_concept(wh_session, system, code)
    row.drug_source_value = code
    row.drug_source_concept_id = row.drug_concept_id
    start = wh.parse_fhir_datetime(body.get("authoredOn"))
    row.drug_exposure_start_datetime = start
    row.drug_exposure_start_date = start.date() if start else None
    row.visit_occurrence_id = _visit_id_for(wh_session, body)


def _transform_procedure(clin: Session, wh_session: Session, body: dict) -> None:
    person = _ensure_person(clin, wh_session, _patient_id_of(body))
    row = _upsert(wh_session, wh.ProcedureOccurrence,
                  wh.ProcedureOccurrence.source_procedure_id, body["id"])
    row.person_id = person.person_id
    system, code, _ = _first_coding(body.get("code"))
    row.procedure_concept_id = wh.lookup_concept(wh_session, system, code)
    row.procedure_source_value = code
    row.procedure_source_concept_id = row.procedure_concept_id
    when = wh.parse_fhir_datetime(body.get("performedDateTime")) \
        or wh.parse_fhir_datetime((body.get("performedPeriod") or {}).get("start")) \
        or wh.parse_fhir_datetime(body.get("performedDateTime")) \
        or wh.parse_fhir_datetime(body.get("performedString"))
    row.procedure_datetime = when
    row.procedure_date = when.date() if when else None
    row.visit_occurrence_id = _visit_id_for(wh_session, body)


TRANSFORMS = {
    "Patient": lambda clin, s, body: _transform_patient(s, body),
    "Encounter": _transform_encounter,
    "Condition": _transform_condition,
    "Observation": _transform_observation,
    "MedicationRequest": _transform_medication_request,
    "Procedure": _transform_procedure,
}


def transform_event(clin: Session, wh_session: Session, event: OutboxEvent) -> str:
    """Apply one outbox event to the warehouse. Returns 'ok' or
    'no_target'. Raises on any transform failure (the drain dead-letters
    the event and continues)."""
    if event.resource_type in NO_TARGET_TYPES:
        return "no_target"
    transform = TRANSFORMS.get(event.resource_type)
    if transform is None:
        return "no_target"
    body = dict(event.payload or {})
    body.setdefault("id", event.resource_id)
    transform(clin, wh_session, body)
    wh_session.flush()
    return "ok"


# ---------------------------------------------------------------------------
# Drain
# ---------------------------------------------------------------------------

def run_etl(clinical_factory, warehouse_factory, limit: int | None = None) -> dict:
    """Drain pending outbox events in id order. Returns exact tallies:
    {processed, no_target, dead_letter, by_type}. Processed includes
    no_target events (their status becomes 'processed')."""
    stats = {"processed": 0, "no_target": 0, "dead_letter": 0, "by_type": {}}
    with warehouse_factory() as wh_session:
        wh.ensure_concept_map(wh_session)
    with clinical_factory() as clin, warehouse_factory() as wh_session:
        stmt = (select(OutboxEvent).where(OutboxEvent.status == "pending")
                .order_by(OutboxEvent.id.asc()))
        if limit:
            stmt = stmt.limit(limit)
        events = clin.scalars(stmt).all()
        for event in events:
            event.attempts += 1
            try:
                outcome = transform_event(clin, wh_session, event)
                wh_session.commit()
            except Exception as exc:  # noqa: BLE001 — dead-letter and continue
                wh_session.rollback()
                event.status = "dead_letter"
                event.error = f"{type(exc).__name__}: {exc}"[:2000]
                clin.commit()
                stats["dead_letter"] += 1
                stats["by_type"][event.resource_type] = \
                    stats["by_type"].get(event.resource_type, 0) + 1
                continue
            event.status = "processed"
            event.error = None
            event.processed_at = datetime.now(timezone.utc)
            clin.commit()
            stats["processed"] += 1
            if outcome == "no_target":
                stats["no_target"] += 1
            stats["by_type"][event.resource_type] = \
                stats["by_type"].get(event.resource_type, 0) + 1
    return stats


# ---------------------------------------------------------------------------
# CLI: python -m app.etl run | status
# ---------------------------------------------------------------------------

def _factories():
    from .db import Base, make_engine, make_session_factory

    engine = make_engine()
    Base.metadata.create_all(engine)
    return make_session_factory(engine), wh.warehouse_session_factory()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "run"
    clinical_factory, warehouse_factory = _factories()
    if command == "run":
        print(json.dumps(run_etl(clinical_factory, warehouse_factory), indent=2))
        return 0
    if command == "status":
        from .outbox import outbox_counts

        with clinical_factory() as clin:
            outbox = outbox_counts(clin)
        with warehouse_factory() as wh_session:
            wh.ensure_concept_map(wh_session)
            payload = {"outbox": outbox,
                       "warehouse": wh.table_counts(wh_session),
                       "mapping_coverage": wh.mapping_coverage(wh_session)}
        print(json.dumps(payload, indent=2))
        return 0
    print(f"unknown command '{command}' (expected: run | status)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
