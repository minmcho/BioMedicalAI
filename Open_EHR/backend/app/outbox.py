"""Transactional outbox — central write hook (Phase 3).

Every create/update of a clinical FHIR resource must produce exactly one
OutboxEvent row in the SAME database transaction as the resource write,
so the analytics warehouse (app.etl / app.warehouse) can never miss a
change and never sees a change that was rolled back.

This is wired centrally, not per endpoint: a SQLAlchemy `before_flush`
listener on the Session class watches for new/dirty FhirResource rows of
the watched types and appends the outbox row to the same flush. It
therefore covers every write path in the codebase — the generic FHIR
create/update (app.fhir.create_resource), the Appointment ADT transition,
and the lab module's direct row mutations (order/specimen status moves,
verify, release) — with no endpoint code involved.

Importing this module registers the listener exactly once (module
singleton). app.main imports it at startup.
"""
from sqlalchemy import event
from sqlalchemy.orm import Session

from .models import FhirResource, OutboxEvent

# Resource types that feed the OMOP warehouse. DiagnosticReport and
# ServiceRequest are captured (the outbox is the complete clinical change
# feed) even though the Phase 3 CDM subset has no target table for them —
# the ETL marks those events processed as documented no-ops.
WATCHED_TYPES = ("Patient", "Encounter", "Condition", "Observation",
                 "MedicationRequest", "Procedure", "DiagnosticReport",
                 "ServiceRequest")


def snapshot_of(row: FhirResource) -> dict:
    """The rendered resource (same shape the FHIR API returns) at the
    version being written — what the ETL transforms."""
    body = dict(row.body or {})
    body["id"] = row.resource_id
    body["meta"] = {**(body.get("meta") or {}), "versionId": str(row.version)}
    return body


def _event_for(row: FhirResource) -> OutboxEvent:
    return OutboxEvent(resource_type=row.resource_type,
                       resource_id=row.resource_id,
                       version=row.version,
                       payload=snapshot_of(row),
                       status="pending")


@event.listens_for(Session, "before_flush")
def _write_outbox_events(session: Session, flush_context, instances) -> None:
    # session.new: inserts. session.dirty: updates where an attribute
    # actually changed (is_modified guards against objects that were only
    # loaded/touched). Objects added here join the same flush, so the
    # outbox row commits or rolls back with the resource write itself.
    for row in list(session.new):
        if isinstance(row, FhirResource) and row.resource_type in WATCHED_TYPES:
            session.add(_event_for(row))
    for row in list(session.dirty):
        if (isinstance(row, FhirResource)
                and row.resource_type in WATCHED_TYPES
                and session.is_modified(row, include_collections=False)):
            session.add(_event_for(row))


def outbox_counts(session: Session) -> dict:
    """Pending / processed / dead-letter tallies for /warehouse/status."""
    from sqlalchemy import func, select

    rows = session.execute(
        select(OutboxEvent.status, func.count(OutboxEvent.id))
        .group_by(OutboxEvent.status)).all()
    counts = {"pending": 0, "processed": 0, "dead_letter": 0}
    for status, n in rows:
        if status in counts:
            counts[status] = n
    return counts
