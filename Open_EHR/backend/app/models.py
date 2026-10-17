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
    # Phase 6B (QC programme): control LEVEL (e.g. "L1"/"L2" — levels are
    # Studio config data on the material, this column just records which
    # level a run belongs to) and the Westgard-style rule flags computed
    # server-side at creation from the published QC programme config
    # (z-score against the material's configured mean/SD). `passed` keeps
    # its original meaning (measured inside the caller-stated expected
    # range) — the release gate reads `passed` exactly as before.
    level: Mapped[str | None] = mapped_column(String(40), nullable=True)
    flags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    z_score: Mapped[float | None] = mapped_column(Float, nullable=True)


class BloodUnit(Base):
    """Blood-bank unit inventory — Phase 6B (app.lab_benches).

    A physical blood product unit is inventory, not a patient FHIR
    resource, so — like PharmacyStock — it lives in its own table. The
    product types and blood groups it validates against are Studio
    terminology (the published lab catalog's `bloodProducts` + the ABO
    concept's value list). Lifecycle: available -> issued -> transfused
    (via a transfusion record) or returned to available; a unit past its
    expiry date can never be issued (409); a unit implicated in a
    transfusion reaction is quarantined and its patient held.
    """
    __tablename__ = "blood_units"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    unit_id: Mapped[str] = mapped_column(String(80), unique=True,
                                         nullable=False, index=True)
    product_type: Mapped[str] = mapped_column(String(60), nullable=False)
    blood_group: Mapped[str] = mapped_column(String(20), nullable=False)
    expiry_date: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO date
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="available")
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                       nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class BloodBankHold(Base):
    """Transfusion-reaction hold — Phase 6B (app.lab_benches).

    While a hold is active for a patient, issuing ANY further unit to
    that patient is refused (409) until a clinician clears the hold
    (the reaction workup). One active hold per patient at a time.
    """
    __tablename__ = "blood_bank_holds"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    unit_id: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    cleared_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                         nullable=True)


class LabEqaRecord(Base):
    """External quality assessment (proficiency testing) record —
    Phase 6B (app.lab_benches). One row per EQA event/analyte: the
    lab's reported value against the scheme's acceptable range;
    `passed` is computed server-side, never caller-asserted."""
    __tablename__ = "lab_eqa_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event: Mapped[str] = mapped_column(String(200), nullable=False)
    analyte_code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    reported_value: Mapped[float] = mapped_column(Float, nullable=False)
    acceptable_low: Mapped[float] = mapped_column(Float, nullable=False)
    acceptable_high: Mapped[float] = mapped_column(Float, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    recorded_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LabInstrument(Base):
    """Analyser/instrument registry — Phase 6B (app.lab_benches).
    Operational data (like LabQcRun): id, display name, bench, and a
    status of active / maintenance / down."""
    __tablename__ = "lab_instruments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(80), unique=True,
                                               nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    bench: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class LabInstrumentLog(Base):
    """Maintenance / calibration log entry for a registered instrument —
    Phase 6B (app.lab_benches)."""
    __tablename__ = "lab_instrument_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)  # maintenance | calibration
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
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


class AdtEvent(Base):
    """ADT event log — Phase 6 (app.inpatient).

    One row per inpatient admission-lifecycle event: A01 admit, A02
    transfer, A03 discharge, A08 update (HL7 v2 ADT event-type codes;
    their displays are Studio terminology config, key
    ``adt-event-types``). The event log is the operational record the
    bed board / census are audited against: every bed assignment or
    release performed through app.inpatient writes exactly one row here
    in the same transaction, alongside the hash-chained AuditLog entry.
    It is operational data, not a FHIR resource (the Encounter remains
    the clinical record; this is its movement history).
    """
    __tablename__ = "adt_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    encounter_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    from_location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    to_location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PriorAuth(Base):
    """Prior-authorisation record — Phase 7 (app.access).

    An authorisation is administrative data (a payer's decision about a
    service for a patient), not a clinical FHIR resource, so — like
    BloodUnit / LabQcRun — it lives in its own table. The referral and
    the orders it covers are FHIR resources; this row links to them by
    id. `approved_units` is the payer-granted ceiling (None = no unit
    limit in the authorisation); `used_units` is incremented server-side
    when an order signs against the authorisation, in the same
    transaction as the signature. Status runs on the published
    `workflow/prior-auth-flow` Studio config.
    """
    __tablename__ = "prior_auths"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    auth_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    auth_number: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    payer_code: Mapped[str] = mapped_column(String(80), nullable=False)
    service_code: Mapped[str] = mapped_column(String(120), nullable=False)
    referral_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="requested")
    approved_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_from: Mapped[str | None] = mapped_column(String(10), nullable=True)  # ISO date
    valid_to: Mapped[str | None] = mapped_column(String(10), nullable=True)  # ISO date
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class EligibilityCheck(Base):
    """Recorded eligibility check — Phase 7 (app.access).

    Every eligibility determination is recorded (who asked, for whom,
    which payer/service/date, and the full result snapshot) so the
    answer a registrar acted on is auditable later. The determination
    itself is a deterministic evaluation of the published Studio payer
    configuration — there is NO live payer (EDI 270/271) connection in
    this system, and the stored snapshot says so (`basis`).
    """
    __tablename__ = "eligibility_checks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    check_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    payer_code: Mapped[str] = mapped_column(String(80), nullable=False)
    service_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    service_date: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO date
    covered: Mapped[bool] = mapped_column(Boolean, nullable=False)
    copay: Mapped[float | None] = mapped_column(Float, nullable=True)
    coinsurance_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    checked_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WaitlistEntry(Base):
    """Appointment waitlist entry — Phase 7 (app.access).

    A patient's request for an appointment of a given slot type
    (published `terminology/slot-types`). Status runs on the published
    `workflow/waitlist-flow` config: waiting -> offered (a cancelled
    appointment freed a matching slot) -> booked (the offer was
    accepted and a real Appointment created) / declined / expired /
    cancelled. Queue order is priority (lower = sooner) then creation.
    """
    __tablename__ = "waitlist_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    slot_type: Mapped[str] = mapped_column(String(80), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="waiting")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    offered_appointment_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    offered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        nullable=True)
    appointment_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class CodingRecord(Base):
    """HIM coding record for one Encounter — Phase 7 (app.revenue).

    The coder's assigned ICD-10-CM diagnoses and CPT procedures for an
    encounter, validated at write time against the published Studio
    value sets (`terminology/coding-diagnoses`,
    `terminology/coding-procedures`); systems + displays are resolved
    from those sets server-side, never caller-asserted. One record per
    encounter. Claim generation consumes the record when one exists
    (its diagnoses become the claim's diagnosis section and its
    procedures become coded claim lines); the published claim config's
    `requireCoding` flag can make a record mandatory.
    """
    __tablename__ = "coding_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    encounter_id: Mapped[str] = mapped_column(String(120), unique=True,
                                              nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    diagnoses: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    procedures: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    coded_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class RemittanceBatch(Base):
    """One ingested X12 835 remittance file — Phase 7 (app.revenue).

    A batch is posted all-or-nothing: a malformed file is refused (422)
    before anything is written. Claim-level lines that cannot post
    (unknown claim, unmapped CLP02 status, claim not yet submitted,
    duplicate check) are NOT parse failures — they land in
    RemittanceException rows tied to the batch, never silently dropped.
    """
    __tablename__ = "remittance_batches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    check_number: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    payer_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    total_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exception_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    received_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RemittanceException(Base):
    """One unpostable CLP loop from an 835 batch — Phase 7 (app.revenue).
    `claim_ref` is the CLP01 value verbatim (it may name a claim this
    system does not hold — that is precisely why it is an exception)."""
    __tablename__ = "remittance_exceptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    claim_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DenialWorkItem(Base):
    """Denials-workqueue state for one claim — Phase 7 (app.revenue).

    The queue itself is derived (claims in `denied` status, or whose
    latest remittance outcome was denied / partially paid with denial
    adjustments); this row carries only the human work state on top:
    assignee, an append-only notes list, and the recorded resolution
    outcome. The claim's own lifecycle (appeal, rebill) is never
    duplicated here — outcomes reference the real claim actions.
    """
    __tablename__ = "denial_work_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    assignee: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    outcome_claim_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    updated_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class PatientPayment(Base):
    """A recorded patient payment against one claim — Phase 7
    (app.revenue). This is a LEDGER entry for a payment the practice
    recorded (portal "bill pay" in v1 records the payment; there is no
    card processor / payment gateway in this system, and the API + UI
    say so). Amounts are refused above the claim's patient-responsibility
    balance so the ledger cannot over-collect."""
    __tablename__ = "patient_payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payment_id: Mapped[str] = mapped_column(String(120), unique=True,
                                            nullable=False, index=True)
    claim_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    method: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    recorded_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Hl7OutboundMessage(Base):
    """One queued outbound HL7 v2 message — Phase 8 (app.interop).

    The interface-engine outbox. A row is created IN THE SAME
    TRANSACTION as the clinical event that produced the message (an
    AdtEvent row, an order signature, a lab release) for each enabled,
    subscribed endpoint in the published `integration/hl7-endpoints`
    config — so a committed event can never lack its queued messages,
    and a rolled-back event leaves none. `raw` is the exact HL7 text
    built for that endpoint (MSH-5/6 are per-endpoint). The dispatcher
    (app.interop.dispatch_pending) drains rows over MLLP; `attempts`
    counts every send try, `attempt_base` is the attempts value at the
    last replay, so the configured max-attempts budget applies per
    queueing (history is never rewritten — see Hl7SendAttempt).
    """
    __tablename__ = "hl7_outbound_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(120), unique=True,
                                             nullable=False, index=True)
    control_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    message_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    endpoint_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    source_kind: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    source_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    raw: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_base: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replay_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    ack_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Hl7SendAttempt(Base):
    """One dispatcher send attempt against one outbound message —
    Phase 8 (app.interop). Append-only: replaying a dead letter adds
    new attempts, it never edits or deletes these rows."""
    __tablename__ = "hl7_send_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outbound_id: Mapped[int] = mapped_column(
        ForeignKey("hl7_outbound_messages.id"), nullable=False, index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)  # ack | nak | error
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Hl7InboundMessage(Base):
    """One received HL7 v2 message — Phase 8 (app.interop).

    The inbound half of the interface log: every message the MLLP
    listener accepts off the wire is recorded verbatim with its
    disposition (accepted / rejected) and the ACK code returned, whether
    it landed as lab drafts, was recorded as an external ADT event, or
    was refused. Nothing arrives unrecorded.
    """
    __tablename__ = "hl7_inbound_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_type: Mapped[str] = mapped_column(String(40), nullable=False, default="",
                                              index=True)
    control_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    peer: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    raw: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="accepted", index=True)
    ack_code: Mapped[str] = mapped_column(String(10), nullable=False, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BulkExportJob(Base):
    """One FHIR Bulk Data ($export-style) job — Phase 8 (app.interop).

    System-level export of record-level NDJSON, gated by the
    `interop.export` capability and fully audited. The job row carries
    only metadata (status, per-type counts, error) — NEVER patient
    data; the NDJSON itself lives in BulkExportFile rows and is served
    only through the capability-gated download endpoint.
    """
    __tablename__ = "bulk_export_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(120), unique=True,
                                        nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="requested", index=True)
    resource_types: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    files: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class BulkExportFile(Base):
    """The NDJSON payload of one resource type for one bulk-export job
    — Phase 8 (app.interop). Stored in the database (portable across
    SQLite/PostgreSQL like every other payload in this codebase) rather
    than on a filesystem a test or a second node would not share."""
    __tablename__ = "bulk_export_files"
    __table_args__ = (UniqueConstraint("job_id", "resource_type"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(60), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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


class CohortSnapshot(Base):
    """One point-in-time member count of a published cohort (registry)
    definition — Phase 9 research workbench (app.pophealth).

    A snapshot records ONLY the aggregate count, the definition's
    published version at capture time, who captured it and when: the
    time series shows how a cohort's size moves without ever storing
    (or exposing) who is in it. Counts are computed by the same
    aggregate-only registry summary the /pophealth API serves."""
    __tablename__ = "cohort_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cohort_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    taken_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
