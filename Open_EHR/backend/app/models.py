import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
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
