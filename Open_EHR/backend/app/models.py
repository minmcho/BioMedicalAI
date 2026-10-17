import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    admin = "admin"
    configurator = "configurator"
    clinician = "clinician"
    viewer = "viewer"
    patient = "patient"


class InstallationStatus(str, enum.Enum):
    installed_draft = "installed_draft"
    approved = "approved"
    attached = "attached"
    enabled = "enabled"
    disabled = "disabled"


class ConfigStatus(str, enum.Enum):
    """Studio configuration lifecycle (docs/STUDIO_CUSTOMIZATION.md).

    draft -> published; publishing a new version moves the previously
    published one to superseded (kept for rollback). A published version may
    be disabled, after which the (kind, key) has no effective config until
    another version is published.
    """
    draft = "draft"
    published = "published"
    superseded = "superseded"
    disabled = "disabled"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    role: Mapped[Role] = mapped_column(Enum(Role), nullable=False)
    # For role=patient: the FHIR reference this portal user is linked to
    # (e.g. "Patient/pat-1"). Patient read scoping compares each resource's
    # patient_ref projection against this field. None for staff roles.
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """Append-only audit log. Hash chain: entry_hash = sha256(prev_hash + canonical payload)."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    entity: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModulePack(Base):
    __tablename__ = "module_packs"
    __table_args__ = (UniqueConstraint("name", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False)


class PackInstallation(Base):
    __tablename__ = "pack_installations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pack_id: Mapped[int] = mapped_column(ForeignKey("module_packs.id"), nullable=False)
    status: Mapped[InstallationStatus] = mapped_column(
        Enum(InstallationStatus), nullable=False, default=InstallationStatus.installed_draft
    )
    installed_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class StudioConfig(Base):
    """One version of one Studio configuration object.

    (kind, key, version) is unique. Payloads are plain JSON (portable across
    SQLite/PostgreSQL). Published rows are immutable: changing a published
    config means creating a new draft version. At most one version per
    (kind, key) is `published` at any time — that row is the effective
    config every consumer reads.
    """
    __tablename__ = "studio_configs"
    __table_args__ = (UniqueConstraint("kind", "key", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ConfigStatus] = mapped_column(
        Enum(ConfigStatus), nullable=False, default=ConfigStatus.draft
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    updated_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class LabQcRun(Base):
    """One laboratory QC (quality control) run — Phase 2 (Lab LIS).

    QC is analyser/control-material data, not a patient FHIR resource, so it
    lives in its own table. `passed` is computed server-side at creation
    (expected_low <= measured_value <= expected_high) and never accepted
    from the caller. Release gating reads the LATEST run per analyte: a
    failed latest run blocks verification/release of patient results for
    that analyte until a passing run exists after it (app.lab.qc_blockers).
    """
    __tablename__ = "lab_qc_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analyte_code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    control_material: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    expected_low: Mapped[float] = mapped_column(Float, nullable=False)
    expected_high: Mapped[float] = mapped_column(Float, nullable=False)
    measured_value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    performed_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LabCriticalNotification(Base):
    """Critical-result notification record — realignment addition
    (app.lab). One row per lab DiagnosticReport that contains a critical
    (LL/HH) value, created lazily on the first notification action.

    Patient-safety workflow: a RELEASED critical report starts in state
    'pending' (no row yet — the state is derived), a staff member records
    the phone notification with read-back confirmation ('notified'), and
    the receiving clinician acknowledges ('acknowledged'). The record is
    operational data, not part of the FHIR report body: it lives here so
    patient-facing reads of the report never carry notification internals
    (who was phoned, by whom) — the same separation as LabQcRun.
    """
    __tablename__ = "lab_critical_notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[str] = mapped_column(String(120), unique=True,
                                           nullable=False, index=True)
    order_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    notified_to: Mapped[str | None] = mapped_column(String(200), nullable=True)
    read_back: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    notified_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                         nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                             nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class PharmacyStock(Base):
    """Pharmacy stock counter — Phase 4A (app.pharmacy).

    One row per stock-tracked formulary medication code. The counter is
    server-side state, never a FHIR resource: dispensing decrements it in
    the same transaction as the MedicationDispense write, and dispensing
    more than the on-hand quantity is refused (409) before any write.
    Rows are seeded at startup from the published Studio formulary config
    (concept `initialStock`) when absent; after that the row is the truth.
    """
    __tablename__ = "pharmacy_stock"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    medication_code: Mapped[str] = mapped_column(String(60), unique=True,
                                                 nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class OutboxEvent(Base):
    """Transactional outbox (Phase 3) — one row per clinical resource
    create/update, written IN THE SAME TRANSACTION as the resource write
    itself (see app.outbox for the central flush hook that produces these;
    no endpoint writes outbox rows by hand).

    The ETL worker (app.etl) drains rows with status='pending' in id order
    into the OMOP warehouse, then stamps processed_at + status='processed'.
    A transform failure moves the row to status='dead_letter' with the
    error text kept on the row — visible via GET /warehouse/status, never
    silently dropped.
    """
    __tablename__ = "outbox_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    resource_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    # Resource version (meta.versionId) at the moment of the write.
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Full rendered-resource snapshot (body + id + meta.versionId).
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FhirResource(Base):
    __tablename__ = "fhir_resources"
    __table_args__ = (UniqueConstraint("resource_type", "resource_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    resource_id: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    body: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Portable relational projections for search (no vendor-specific types).
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    # First Practitioner/* actor found in body.participant (Appointment).
    practitioner_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    # body["status"] projected for inbox / status filtering.
    status: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    # Parsed body["start"] as naive UTC, for Appointment date-range search.
    start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
